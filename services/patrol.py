"""Incremental patrol with bounded review concurrency and whole-thread progress.

Markers are committed only after every discovered item is resolved. Seen items
remain durable independently, so a review-capped or failed thread can resume
without judging successful items again. Pagination caps warn only for known sources;
first inspections establish a bounded baseline. Read-budget exhaustion leaves the
current thread's markers untouched. Queued runs start fresh rounds and read budgets.
"""

from __future__ import annotations

import asyncio
import dataclasses as dcs
import time
from collections.abc import Callable

from astrbot.api import logger

from ..core.judge import FAILED_CATEGORY, MAX_REASON_CHARS
from ..core.models import (
    Action,
    CaseStatus,
    ContentItem,
    JudgeError,
    JudgeFn,
    NotifyFn,
    PatrolReport,
    Verdict,
    decide_action,
)
from ..core.store import Store
from .tieba_gateway import (
    ReadBudget,
    RequestBudgetExceeded,
    ThreadSnapshot,
    TiebaGateway,
)


@dcs.dataclass(frozen=True, kw_only=True)
class PatrolSettings:
    """Patrol selection, review limits and automatic disposition settings."""

    forums: tuple[str, ...]
    thread_pages: int = 1
    lookback_hours: int = 6
    review_posts: bool = True
    review_comments: bool = True
    skip_top_and_good: bool = True
    skip_bawu: bool = True
    whitelist: frozenset[str] = frozenset()
    max_items_per_round: int = 60
    max_tieba_requests_per_round: int = 200
    max_post_pages: int = 3
    max_comment_pages: int = 3
    review_images: bool = True
    max_images: int = 4
    judge_concurrency: int = 2
    auto_delete_enabled: bool = False
    auto_delete_threshold: float = 0.9
    report_threshold: float = 0.6
    max_failures: int = 3
    max_consecutive_judge_failures: int = 3
    """Consecutive judge-side failures that abort the remainder of a round."""


class Patrol:
    """Coordinate one gateway, persistent progress and moderation callbacks."""

    def __init__(
        self,
        *,
        gateway: TiebaGateway,
        store: Store,
        judge: JudgeFn,
        notify: NotifyFn,
        settings: PatrolSettings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Configure an independently serialized patrol.

        Args:
            gateway: Tieba content and moderation gateway.
            store: Synchronous persistent progress and case store.
            judge: Async content reviewer.
            notify: Async case notification callback.
            settings: Selection, limits and action thresholds.
            clock: Injectable Unix clock used for reports and case timestamps.

        Raises:
            ValueError: Concurrency, failure limits or page limits are not
                positive, or a per-round item or request limit is negative.
        """
        if settings.judge_concurrency < 1:
            raise ValueError("judge_concurrency must be positive")
        if settings.max_failures < 1:
            raise ValueError("max_failures must be positive")
        if settings.max_consecutive_judge_failures < 1:
            raise ValueError("max_consecutive_judge_failures must be positive")
        if settings.max_items_per_round < 0:
            raise ValueError("max_items_per_round must not be negative")
        if settings.max_tieba_requests_per_round < 0:
            raise ValueError("max_tieba_requests_per_round must not be negative")
        for name in ("thread_pages", "max_post_pages", "max_comment_pages"):
            if getattr(settings, name) < 1:
                raise ValueError(f"{name} must be positive")
        self._gateway = gateway
        self._store = store
        self._judge = judge
        self._notify = notify
        self._settings = settings
        self._clock = clock
        self._lock = asyncio.Lock()

    async def run_once(self) -> PatrolReport:
        """Run one round, waiting for any active round before starting anew.

        Returns:
            An immutable report including isolated forum, thread and item errors.
        """
        async with self._lock:
            return await self._run_round()

    async def _collect(
        self, snap: ThreadSnapshot, budget: ReadBudget, cutoff: int
    ) -> tuple[list[ContentItem], dict[int, int], list[str]]:
        """Collect new bodies and comments, separating old parent evidence.

        ASC guarantees complete first-floor evidence for newly seen threads.
        DESC parent evidence updates reply counts without reviewing stale bodies.
        The round's lookback cutoff bounds all body backtracking. Pagination caps
        warn only for previously marked threads and comment parents.
        Budget exhaustion propagates so no partial collection advances markers.
        """
        settings = self._settings
        items: dict[str, ContentItem] = {}
        floors: dict[int, tuple[ContentItem, int]] = {}
        post_marks: dict[int, int] = {}
        warnings: list[str] = []
        mark = self._store.get_thread_mark(snap.tid)
        if mark is None:
            item, replies = await self._gateway.get_thread_item(snap, budget=budget)
            items[item.key] = item
            floors[item.pid] = (item, replies)
        if settings.review_posts and (mark is None or snap.last_time > mark):
            batch = await self._gateway.get_new_posts(
                snap, max(mark or 0, cutoff), settings.max_post_pages, budget=budget
            )
            for post, replies in batch.pairs:
                # Preserve the complete ASC body but use the later DESC count.
                item = items.setdefault(post.key, post)
                floors[post.pid] = (item, replies)
            for post, replies in batch.comment_parents:
                floors[post.pid] = (items.get(post.key, post), replies)
            if mark is not None and not batch.caught_up:
                warnings.append(
                    f"帖子 {snap.tid} 两轮之间新增楼层过多，较早的 "
                    f"{settings.max_post_pages} 页之外的新楼层未审查"
                )
            for post, replies in floors.values():
                post_marks[post.pid] = replies
                previous = self._store.get_post_mark(post.pid)
                if settings.review_comments and replies > (previous or 0):
                    comments, caught_up = await self._gateway.get_comments(
                        post,
                        replies,
                        previous,
                        settings.max_comment_pages,
                        budget=budget,
                    )
                    for comment in comments:
                        items[comment.key] = comment
                    if previous is not None and not caught_up:
                        warnings.append(
                            f"帖子 {snap.tid} 楼层 {post.pid} "
                            "两轮之间新增楼中楼过多，较早的 "
                            f"{settings.max_comment_pages} 页之外的新楼中楼未审查"
                        )
        return list(items.values()), post_marks, warnings

    def _skip(self, item: ContentItem, cutoff: int) -> bool:
        settings = self._settings
        return (
            item.create_time < cutoff
            or item.author.user_name in settings.whitelist
            or item.author.portrait in settings.whitelist
            or (settings.skip_bawu and item.author.is_bawu)
        )

    async def _run_round(self) -> PatrolReport:
        settings = self._settings
        started_at = int(self._clock())
        report = PatrolReport(
            started_at=started_at, finished_at=started_at, forums=settings.forums
        )
        cutoff = started_at - settings.lookback_hours * 3600
        semaphore = asyncio.Semaphore(settings.judge_concurrency)
        remaining = settings.max_items_per_round
        # Only explicit content API page attempts consume this round-local quota.
        # Images, writes and concurrent manual checks never receive this object.
        budget = ReadBudget(settings.max_tieba_requests_per_round)
        budget_warned = False
        errors: list[str] = []
        consecutive_judge_failures = 0
        judge_tripped = False

        def error(message: str) -> None:
            errors.append(message)
            logger.warning("Tieba patrol: %s", message)

        def warn_budget() -> None:
            nonlocal budget_warned
            if not budget_warned:
                error("本轮贴吧请求达到上限，剩余帖子顺延")
                budget_warned = True

        async def abandon(item: ContentItem, exc: BaseException, failures: int) -> None:
            """Hand an unreviewable item to moderators instead of dropping it.

            Args:
                item: Content that could not be judged.
                exc: The last error, copyable into the case note.
                failures: Total failed attempts for this key.
            """
            nonlocal report
            verdict = Verdict(
                violation=False,
                category=FAILED_CATEGORY,
                confidence=0.0,
                reason=f"自动审查失败 {failures} 次，请人工查看：{exc}"[
                    :MAX_REASON_CHARS
                ],
            )
            case = self._store.create_case(
                item, verdict, CaseStatus.PENDING, int(self._clock())
            )
            report = dcs.replace(report, reported=report.reported + 1)
            try:
                await self._notify(case)
            except Exception as notify_exc:
                error(f"Case {case.id} notification failed: {notify_exc}")
            self._store.mark_seen(item.key)

        async def review(item: ContentItem) -> bool:
            nonlocal report, consecutive_judge_failures, judge_tripped
            async with semaphore:
                if judge_tripped:
                    # Left for the next round: no failure counted, no marker.
                    report = dcs.replace(report, deferred=report.deferred + 1)
                    return False
                try:
                    images = []
                    if settings.review_images and item.image_urls:
                        images = await self._gateway.fetch_images(
                            item.image_urls, settings.max_images
                        )
                    report = dcs.replace(report, reviewed=report.reviewed + 1)
                    verdict = await self._judge(item, images)
                    consecutive_judge_failures = 0
                    action = decide_action(
                        verdict,
                        auto_delete_enabled=settings.auto_delete_enabled,
                        auto_delete_threshold=settings.auto_delete_threshold,
                        report_threshold=settings.report_threshold,
                    )
                    if action is not Action.NONE:
                        status = CaseStatus.PENDING
                        note = ""
                        if action is Action.AUTO_DELETE:
                            try:
                                await self._gateway.delete(item)
                            except Exception as exc:
                                note = f"自动删除失败：{exc}"
                                error(f"{item.key}: Automatic deletion failed: {exc}")
                            else:
                                status = CaseStatus.AUTO_DELETED
                        case = self._store.create_case(
                            item, verdict, status, int(self._clock())
                        )
                        if note:
                            updated = self._store.transition(
                                case.id,
                                from_=(CaseStatus.PENDING,),
                                to=CaseStatus.PENDING,
                                by="patrol",
                                now=int(self._clock()),
                                note=note,
                            )
                            if updated is None:
                                raise RuntimeError(
                                    "New patrol case changed unexpectedly"
                                )
                            case = updated
                        if status is CaseStatus.AUTO_DELETED:
                            report = dcs.replace(
                                report, auto_deleted=report.auto_deleted + 1
                            )
                        else:
                            report = dcs.replace(report, reported=report.reported + 1)
                        try:
                            await self._notify(case)
                        except Exception as exc:
                            error(f"Case {case.id} notification failed: {exc}")
                    self._store.mark_seen(item.key)
                    return True
                except Exception as exc:
                    if isinstance(exc, JudgeError):
                        consecutive_judge_failures += 1
                        if (
                            consecutive_judge_failures
                            >= settings.max_consecutive_judge_failures
                        ):
                            judge_tripped = True
                            error(
                                "模型连续失败，本轮提前停止审查（剩余条目顺延到下一轮）"
                            )
                    failures = self._store.record_failure(item.key)
                    abandoned = failures >= settings.max_failures
                    disposition = "abandoned" if abandoned else "will retry"
                    error(
                        f"{item.key}: review failed ({failures}, {disposition}): {exc}"
                    )
                    if abandoned:
                        await abandon(item, exc, failures)
                        return True
                    return False

        for fname in settings.forums:
            if not budget.remaining:
                # Unlisted forums may contain work, but their thread counts are
                # unknown: deferred counts only fetched eligible snapshots.
                warn_budget()
                break
            pages = min(settings.thread_pages, budget.remaining)
            if pages < settings.thread_pages:
                warn_budget()
            try:
                snapshots = await self._gateway.list_threads(
                    fname, pages, budget=budget
                )
            except RequestBudgetExceeded:
                warn_budget()
                break
            except Exception as exc:
                error(f"Forum {fname}: {exc}")
                continue
            for snap in snapshots:
                if snap.is_livepost or (
                    settings.skip_top_and_good and (snap.is_top or snap.is_good)
                ):
                    continue
                if not budget.remaining:
                    report = dcs.replace(report, deferred=report.deferred + 1)
                    warn_budget()
                    continue
                try:
                    items, post_marks, warnings = await self._collect(
                        snap, budget, cutoff
                    )
                    for warning in warnings:
                        error(warning)
                    selected: list[ContentItem] = []
                    complete = True
                    for item in items:
                        if self._store.is_seen(item.key):
                            continue
                        if self._skip(item, cutoff):
                            self._store.mark_seen(item.key)
                            report = dcs.replace(report, skipped=report.skipped + 1)
                        elif remaining:
                            selected.append(item)
                            remaining -= 1
                        else:
                            complete = False
                            report = dcs.replace(report, deferred=report.deferred + 1)
                    # Await every child even if a storage failure escapes review;
                    # no task may outlive the round and its serialization lock.
                    results = await asyncio.gather(
                        *(review(item) for item in selected), return_exceptions=True
                    )
                    for result in results:
                        if isinstance(result, BaseException):
                            if isinstance(result, asyncio.CancelledError):
                                raise result
                            complete = False
                            error(f"Thread {snap.tid}: {result}")
                        elif not result:
                            complete = False
                    if complete:
                        for pid, replies in post_marks.items():
                            self._store.set_post_mark(pid, replies)
                        self._store.set_thread_mark(snap.tid, snap.last_time)
                except RequestBudgetExceeded:
                    report = dcs.replace(report, deferred=report.deferred + 1)
                    warn_budget()
                except Exception as exc:
                    error(f"Thread {snap.tid}: {exc}")
        self._store.purge(started_at - 30 * 24 * 3600)
        return dcs.replace(report, finished_at=int(self._clock()), errors=tuple(errors))
