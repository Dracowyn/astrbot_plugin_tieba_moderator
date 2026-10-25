"""Dashboard plugin-view backend: parameter validation and DTO serialization.

Handlers are plain methods registered by ``main.py`` through
``context.register_web_api``; they are not ``@filter`` handlers, so they may live
here instead of ``main.py``.

Dashboard authenticates every request before dispatch: the extension routes
depend on ``require_plugin_scope`` (astrbot/dashboard/api/plugins.py:398-438),
which rejects requests without a valid dashboard JWT or an API key carrying the
plugin scope (astrbot/dashboard/api/auth.py:167-195). A caller is therefore a
logged-in WebUI user, which this plugin treats as a moderator. Every input is
still validated: a moderator is trusted to moderate, not to send sane
parameters.

Error contract: 400 invalid parameters, 404 missing case or image, 409 a round
or a trial review is already running, 502 Tieba / image / judge failure with a
redacted message, 503 no account credentials, 500 anything else with a fixed
message (the traceback only reaches the log).
"""

from __future__ import annotations

import asyncio
import dataclasses as dcs
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from ..core.judge import FAILED_CATEGORY
from ..core.models import (
    ALLOWED_BAN_DAYS,
    Case,
    CaseStatus,
    ContentItem,
    ImageFetchError,
    ItemKind,
    JudgeError,
    PatrolReport,
    TiebaActionError,
    decide_action,
)
from ..core.store import Store
from ..services.case_service import CaseError, CaseService
from ..services.llm_judge import MAX_TEMP_RULES_CHARS, LlmJudge
from ..services.patrol import Patrol, PatrolSettings
from ..services.tieba_gateway import TiebaGateway

MAX_ID_DIGITS = 18
MAX_INDEX = 8
MAX_Q_CHARS = 50
MAX_FILTER_CHARS = 64
MAX_OFFSET = 1_000_000
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
MAX_STATS_DAYS = 90
DEFAULT_STATS_DAYS = 30
IMAGE_CACHE_SIZE = 64
"""Maximum number of cached image data URLs."""
IMAGE_CACHE_MAX_CHARS = 32 * 1024 * 1024
"""Maximum total characters of cached data URLs.

One image may be 5 MiB of bytes, which base64 encodes to roughly 6.7 MB of
text, so the entry count alone would let the cache hold several hundred MB.
"""
IMAGE_CACHE_MAX_ENTRY_CHARS = IMAGE_CACHE_MAX_CHARS // 8
"""Largest single data URL worth caching; bigger ones are returned uncached."""
OPERATOR_CHARS = 64

INTERNAL_ERROR = "服务器内部错误，详见 AstrBot 日志。"
NO_STORE_REPLY = "插件存储尚未就绪，请重启 AstrBot。"
NO_ACCOUNT_REPLY = "未配置吧务账号的 BDUSS / STOKEN，贴吧操作不可用。"
BUSY_PATROL_REPLY = "已有一轮手动巡查在进行，请稍后。"
BUSY_CHECK_REPLY = "已有一次试判在进行，请稍后。"

CASE_STATUSES = {status.value: status for status in CaseStatus}
CASE_KINDS = {kind.value: kind for kind in ItemKind}
_STATUS_QUERY_KEYS = ("status", "status[]")
_BUCKETS = 10


@dcs.dataclass(frozen=True, kw_only=True)
class WebServices:
    """Plugin dependencies read at request time.

    A handler must never capture these at registration time: the store, the
    gateway and the case service are created in ``initialize`` and dropped in
    ``terminate``, so every request asks the plugin for the current objects.

    Attributes:
        store: Case persistence, None before ``initialize``.
        patrol: Patrol runner, None when no account is configured.
        gateway: Tieba gateway, None when no account is configured.
        cases: Moderator actions, None when no account is configured.
        judge: The LLM judge, including its temporary-rules entry point.
        settings: Effective patrol settings.
        interval_seconds: Configured patrol interval.
        patrol_enabled: Whether the periodic loop is enabled in the config.
        loop_running: Whether the periodic loop task is alive right now.
        manual_running: Whether a manual round is in flight.
        start_patrol: Starts a manual round; False when one is already running.
        redact: Formats the message of an exception with credentials removed.
        redact_trace: Formats an exception's traceback with credentials removed.
    """

    store: Store | None
    patrol: Patrol | None
    gateway: TiebaGateway | None
    cases: CaseService | None
    judge: LlmJudge
    settings: PatrolSettings
    interval_seconds: int
    patrol_enabled: bool
    loop_running: bool
    manual_running: bool
    start_patrol: Callable[[], bool]
    redact: Callable[[BaseException], str]
    redact_trace: Callable[[BaseException], str]


def _path_int(raw: str, *, allow_zero: bool) -> int | None:
    """Parse a decimal path parameter.

    Args:
        raw: The raw path segment.
        allow_zero: Whether zero is a valid value.

    Returns:
        The value, or None when it is not an ASCII decimal integer within
        ``MAX_ID_DIGITS`` digits (or is zero when zero is not allowed).
    """
    text = (raw or "").strip()
    if not text.isascii() or not text.isdecimal() or len(text) > MAX_ID_DIGITS:
        return None
    value = int(text)
    return value if allow_zero or value else None


def _query_int(raw: str | None, *, default: int, low: int, high: int) -> int | None:
    """Parse a bounded integer query parameter, clamping to the range.

    Args:
        raw: The raw value; an empty value means "use the default".
        default: Value used when the parameter is absent.
        low: Lowest accepted value.
        high: Highest accepted value.

    Returns:
        The clamped value, or None when the parameter is not an integer.
    """
    if raw is None or not raw.strip():
        return default
    text = raw.strip()
    if not text.isascii() or len(text) > MAX_ID_DIGITS:
        return None
    if text.startswith("-"):
        if not text[1:].isdecimal():
            return None
        value = -int(text[1:])
    elif text.isdecimal():
        value = int(text)
    else:
        return None
    return min(high, max(low, value))


def _one_line(value: object, limit: int) -> str:
    """Flatten a stored field to a single bounded line.

    Args:
        value: Raw field value.
        limit: Maximum characters kept.

    Returns:
        The value with control characters removed and whitespace collapsed.
    """
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return " ".join(text.split())[:limit]


def _operator() -> str:
    """Return the audit label of the caller.

    Returns:
        ``webui:<username>``; ``webui:unknown`` when the dashboard did not
        report a user name. Control characters are stripped and the length is
        capped because the value is stored and later rendered.
    """
    name = _one_line(request.username or "unknown", OPERATOR_CHARS) or "unknown"
    return f"webui:{name}"


def _query_statuses() -> tuple[list[CaseStatus], str | None]:
    """Read the ``status`` filter in every form the view may send it.

    The bridge serializes a JavaScript object, so a multi-select arrives as one
    comma-separated string; axios may also expand an array to repeated
    ``status`` keys or to ``status[]`` keys. All three forms are accepted, may
    be mixed, and are merged in request order.

    Returns:
        The distinct statuses in request order and, when a value is not a
        ``CaseStatus``, a Chinese error message to return instead.
    """
    parts: list[str] = []
    for key in _STATUS_QUERY_KEYS:
        for raw in request.query.getlist(key):
            parts.extend(raw.split(","))
    statuses: list[CaseStatus] = []
    for part in parts:
        text = part.strip()
        if not text:
            continue
        status = CASE_STATUSES.get(text)
        if status is None:
            return [], f"未知的案件状态：{_one_line(text, 32)}"
        if status not in statuses:
            statuses.append(status)
    # Deduplication caps the result at the number of enum members by itself.
    return statuses, None


def _item_dto(item: ContentItem) -> dict[str, Any]:
    """Serialize one content item for the view.

    Args:
        item: Stored content.

    Returns:
        An ``ItemDTO``. Image URLs are deliberately omitted; the view reads
        images one at a time through the image endpoint.
    """
    author = item.author
    return {
        "kind": item.kind.value,
        "fname": item.fname,
        "tid": item.tid,
        "pid": item.pid,
        "floor": item.floor,
        "title": item.title,
        "text": item.text,
        "link": item.link,
        "image_count": len(item.image_urls),
        "has_video": item.has_video,
        "create_time": item.create_time,
        "author": {
            "nick_name": author.nick_name,
            "user_name": author.user_name,
            "portrait": author.portrait,
            "level": author.level,
            "is_bawu": author.is_bawu,
        },
    }


def _verdict_dto(verdict: Any) -> dict[str, Any]:
    """Serialize a verdict.

    Args:
        verdict: Stored or fresh verdict.

    Returns:
        A ``VerdictDTO``.
    """
    return {
        "violation": verdict.violation,
        "category": verdict.category,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
        "ban_days": verdict.ban_days,
    }


def _case_dto(case: Case) -> dict[str, Any]:
    """Serialize a case.

    Args:
        case: Stored case.

    Returns:
        A ``CaseDTO``.
    """
    return {
        "id": case.id,
        "status": case.status.value,
        "created_at": case.created_at,
        "handled_by": case.handled_by,
        "handled_at": case.handled_at,
        "note": case.note,
        "review_failed": case.verdict.category == FAILED_CATEGORY,
        "item": _item_dto(case.item),
        "verdict": _verdict_dto(case.verdict),
    }


def _report_dto(report: PatrolReport) -> dict[str, Any]:
    """Serialize a patrol report.

    Args:
        report: Stored report.

    Returns:
        A ``ReportDTO``.
    """
    return {
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "forums": list(report.forums),
        "reviewed": report.reviewed,
        "reported": report.reported,
        "auto_deleted": report.auto_deleted,
        "skipped": report.skipped,
        "deferred": report.deferred,
        "errors": list(report.errors),
    }


def _bucket_index(confidence: float) -> int:
    """Map a confidence to its 0.1-wide bucket.

    Args:
        confidence: Confidence in ``[0, 1]``.

    Returns:
        The bucket index, 0 to 9. The epsilon keeps values such as 0.7 (stored
        as 6.999999999999999 when multiplied) in the expected bucket.
    """
    return min(_BUCKETS - 1, max(0, int(confidence * _BUCKETS + 1e-9)))


def _stats_payload(
    rows: list[tuple[str, CaseStatus, float]], since: int
) -> dict[str, Any]:
    """Aggregate raw case rows into the statistics payload.

    Args:
        rows: ``(category, status, confidence)`` triples from the store.
        since: Unix seconds the statistics start at.

    Returns:
        The ``GET stats`` payload. Cases whose category is ``审查失败`` are
        counted in ``review_failed`` and excluded from ``by_category`` and the
        confidence buckets, because they carry no judge confidence at all.
    """
    status_counts = {status.value: 0 for status in CaseStatus}
    categories: dict[str, dict[str, Any]] = {}
    buckets = [
        {
            "low": index / _BUCKETS,
            "high": (index + 1) / _BUCKETS,
            "total": 0,
            "confirmed": 0,
            "false_positive": 0,
        }
        for index in range(_BUCKETS)
    ]
    review_failed = 0
    for category, status, confidence in rows:
        status_counts[status.value] += 1
        if category == FAILED_CATEGORY:
            review_failed += 1
            continue
        entry = categories.get(category)
        if entry is None:
            entry = {"category": category, "total": 0}
            entry.update(dict.fromkeys(CASE_STATUSES, 0))
            categories[category] = entry
        entry["total"] += 1
        entry[status.value] += 1
        bucket = buckets[_bucket_index(confidence)]
        bucket["total"] += 1
        if status in (CaseStatus.DELETED, CaseStatus.BANNED, CaseStatus.AUTO_DELETED):
            bucket["confirmed"] += 1
        elif status in (CaseStatus.IGNORED, CaseStatus.RESTORED):
            bucket["false_positive"] += 1
    return {
        "since": since,
        "total": len(rows),
        "by_status": status_counts,
        "by_category": sorted(
            categories.values(), key=lambda row: (-row["total"], row["category"])
        ),
        "confidence_buckets": buckets,
        "review_failed": review_failed,
    }


class WebRoutes:
    """Dashboard plugin-view handlers for one plugin instance."""

    def __init__(
        self,
        services: Callable[[], WebServices],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the handlers.

        Args:
            services: Reads the current plugin dependencies; called once per
                request so a reload never leaves a handler holding dead objects.
            clock: Unix-time source for statistics windows.
        """
        self._services = services
        self._clock = clock
        self._image_cache: OrderedDict[str, str] = OrderedDict()
        self._image_cache_chars = 0
        """Total characters held by ``_image_cache``, maintained incrementally."""
        self._check_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_status(self) -> Any:
        """Return patrol state, thresholds and the newest report."""
        services = self._services()
        if services.store is None:
            return error_response(NO_STORE_REPLY, status_code=503)
        reports = services.store.list_reports(1)
        settings = services.settings
        return json_response(
            {
                "has_account": services.gateway is not None,
                "patrol_enabled": services.patrol_enabled,
                "loop_running": services.loop_running,
                "manual_running": services.manual_running,
                "interval_seconds": services.interval_seconds,
                "forums": list(settings.forums),
                "auto_delete": {
                    "enabled": settings.auto_delete_enabled,
                    "threshold": settings.auto_delete_threshold,
                },
                "report_threshold": settings.report_threshold,
                "pending": services.store.count_cases(CaseStatus.PENDING),
                "last_report": _report_dto(reports[0]) if reports else None,
            }
        )

    # ------------------------------------------------------------------
    # Cases
    # ------------------------------------------------------------------

    async def get_cases(self) -> Any:
        """List cases with filtering, newest first."""
        services = self._services()
        if services.store is None:
            return error_response(NO_STORE_REPLY, status_code=503)

        statuses, invalid = _query_statuses()
        if invalid is not None:
            return error_response(invalid)

        kind_raw = (request.query.get("kind") or "").strip()
        kind = None
        if kind_raw:
            kind = CASE_KINDS.get(kind_raw)
            if kind is None:
                return error_response("kind 只能是 thread、post 或 comment")

        fname = (request.query.get("fname") or "").strip()
        category = (request.query.get("category") or "").strip()
        q = (request.query.get("q") or "").strip()
        for label, value in (("fname", fname), ("category", category)):
            if len(value) > MAX_FILTER_CHARS:
                return error_response(f"{label} 过长")
        if len(q) > MAX_Q_CHARS:
            return error_response(f"关键词不能超过 {MAX_Q_CHARS} 字")

        limit = _query_int(
            request.query.get("limit"),
            default=DEFAULT_LIMIT,
            low=1,
            high=MAX_LIMIT,
        )
        offset = _query_int(
            request.query.get("offset"), default=0, low=0, high=MAX_OFFSET
        )
        if limit is None or offset is None:
            return error_response("limit / offset 必须是整数")

        total, cases = services.store.query_cases(
            statuses=tuple(statuses),
            fname=fname,
            category=category,
            kind=kind,
            q=q,
            offset=offset,
            limit=limit,
        )
        return json_response(
            {"total": total, "items": [_case_dto(case) for case in cases]}
        )

    async def get_case(self, case_id: str) -> Any:
        """Return one case.

        Args:
            case_id: Case id from the path.
        """
        services = self._services()
        if services.store is None:
            return error_response(NO_STORE_REPLY, status_code=503)
        cid = _path_int(case_id, allow_zero=False)
        if cid is None:
            return error_response("案件编号必须是正整数")
        case = services.store.get_case(cid)
        if case is None:
            return error_response(f"案件 #{cid} 不存在", status_code=404)
        return json_response(_case_dto(case))

    async def get_case_image(self, case_id: str, index: str) -> Any:
        """Return one image of a case as a data URL.

        The URL always comes from the stored case; no URL is accepted from the
        caller, so this endpoint cannot be used to probe arbitrary hosts.

        Args:
            case_id: Case id from the path.
            index: Zero-based image index from the path.
        """
        services = self._services()
        if services.store is None:
            return error_response(NO_STORE_REPLY, status_code=503)
        cid = _path_int(case_id, allow_zero=False)
        if cid is None:
            return error_response("案件编号必须是正整数")
        image_index = _path_int(index, allow_zero=True)
        if image_index is None or image_index > MAX_INDEX:
            return error_response("图片序号不合法", status_code=404)
        case = services.store.get_case(cid)
        if case is None:
            return error_response(f"案件 #{cid} 不存在", status_code=404)
        urls = case.item.image_urls
        if image_index >= len(urls):
            return error_response("图片序号不存在", status_code=404)
        if services.gateway is None:
            return error_response(NO_ACCOUNT_REPLY, status_code=503)

        key = f"{cid}:{image_index}"
        cached = self._image_cache.get(key)
        if cached is not None:
            self._image_cache.move_to_end(key)
            return json_response({"data_url": cached})
        try:
            data_urls = await services.gateway.fetch_images([urls[image_index]], 1)
        except ImageFetchError as exc:
            return error_response(
                services.redact(exc) or "图片下载失败", status_code=502
            )
        except Exception as exc:
            logger.error(
                f"Fetching image {index} of case {cid} failed:\n"
                f"{services.redact_trace(exc)}"
            )
            return error_response(INTERNAL_ERROR, status_code=500)
        if not data_urls:
            return error_response("图片下载失败", status_code=502)
        data_url = data_urls[0]
        self._remember_image(key, data_url)
        return json_response({"data_url": data_url})

    def _remember_image(self, key: str, data_url: str) -> None:
        """Cache one data URL within the entry-count and total-size budgets.

        Oversized single images are returned to the caller but never cached: one
        entry must not be able to consume the whole budget.

        Args:
            key: ``"<case_id>:<index>"`` cache key.
            data_url: The data URL to cache.
        """
        if len(data_url) > IMAGE_CACHE_MAX_ENTRY_CHARS:
            return
        previous = self._image_cache.pop(key, None)
        if previous is not None:
            self._image_cache_chars -= len(previous)
        self._image_cache[key] = data_url
        self._image_cache_chars += len(data_url)
        while self._image_cache and (
            len(self._image_cache) > IMAGE_CACHE_SIZE
            or self._image_cache_chars > IMAGE_CACHE_MAX_CHARS
        ):
            _, evicted = self._image_cache.popitem(last=False)
            self._image_cache_chars -= len(evicted)

    async def post_delete(self, case_id: str) -> Any:
        """Delete a pending case's content.

        Args:
            case_id: Case id from the path.
        """
        return await self._act(case_id, "delete")

    async def post_ban(self, case_id: str) -> Any:
        """Delete and ban, using the leader-supplied duration.

        Args:
            case_id: Case id from the path.
        """
        return await self._act(case_id, "ban")

    async def post_ignore(self, case_id: str) -> Any:
        """Dismiss a pending case.

        Args:
            case_id: Case id from the path.
        """
        return await self._act(case_id, "ignore")

    async def post_restore(self, case_id: str) -> Any:
        """Restore deleted content.

        Args:
            case_id: Case id from the path.
        """
        return await self._act(case_id, "restore")

    async def _act(self, case_id: str, action: str) -> Any:
        """Run a moderator action and serialize the updated case.

        Args:
            case_id: Raw case id from the path.
            action: One of ``delete``, ``ban``, ``ignore``, ``restore``.

        Returns:
            The updated ``CaseDTO`` or an error response.
        """
        services = self._services()
        if services.store is None:
            return error_response(NO_STORE_REPLY, status_code=503)
        cid = _path_int(case_id, allow_zero=False)
        if cid is None:
            return error_response("案件编号必须是正整数")

        days = 0
        if action == "ban":
            payload = await request.json(default=None)
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象")
            days = payload.get("days")
            if type(days) is not int or days not in ALLOWED_BAN_DAYS:
                return error_response("封禁天数只能是 1、3 或 10 天")
        if services.cases is None:
            return error_response(NO_ACCOUNT_REPLY, status_code=503)
        case = services.store.get_case(cid)
        if case is None:
            return error_response(f"案件 #{cid} 不存在", status_code=404)

        operator = _operator()
        try:
            if action == "delete":
                updated = await services.cases.delete(cid, operator)
            elif action == "ban":
                updated = await services.cases.ban(cid, days, operator)
            elif action == "ignore":
                updated = await services.cases.ignore(cid, operator)
            else:
                updated = await services.cases.restore(cid, operator)
        except CaseError as exc:
            return error_response(services.redact(exc) or str(exc))
        except (TiebaActionError, ImageFetchError, JudgeError) as exc:
            return error_response(
                services.redact(exc) or "贴吧操作失败", status_code=502
            )
        except Exception as exc:
            logger.error(
                f"Web {action} on case {cid} failed:\n{services.redact_trace(exc)}"
            )
            return error_response(INTERNAL_ERROR, status_code=500)
        return json_response(_case_dto(updated))

    # ------------------------------------------------------------------
    # Statistics and reports
    # ------------------------------------------------------------------

    async def get_stats(self) -> Any:
        """Aggregate cases created within the requested window."""
        services = self._services()
        if services.store is None:
            return error_response(NO_STORE_REPLY, status_code=503)
        raw_days = request.query.get("days")
        if raw_days is None or not raw_days.strip():
            days = DEFAULT_STATS_DAYS
        else:
            text = raw_days.strip()
            if not text.isascii() or not text.isdecimal() or len(text) > 4:
                return error_response(f"days 必须是 1 到 {MAX_STATS_DAYS} 的整数")
            days = int(text)
            if not 1 <= days <= MAX_STATS_DAYS:
                return error_response(f"days 必须是 1 到 {MAX_STATS_DAYS} 的整数")
        since = int(self._clock()) - days * 86400
        rows = services.store.case_stats(since)
        return json_response(_stats_payload(rows, since))

    async def get_reports(self) -> Any:
        """Return the newest patrol reports."""
        services = self._services()
        if services.store is None:
            return error_response(NO_STORE_REPLY, status_code=503)
        limit = _query_int(
            request.query.get("limit"),
            default=DEFAULT_LIMIT,
            low=1,
            high=MAX_LIMIT,
        )
        if limit is None:
            return error_response("limit 必须是整数")
        return json_response(
            [_report_dto(report) for report in services.store.list_reports(limit)]
        )

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def post_patrol_run(self) -> Any:
        """Start a manual patrol round in the background."""
        services = self._services()
        if services.store is None:
            return error_response(NO_STORE_REPLY, status_code=503)
        if services.patrol is None:
            return error_response(NO_ACCOUNT_REPLY, status_code=503)
        if not services.start_patrol():
            return error_response(BUSY_PATROL_REPLY, status_code=409)
        return json_response({"started": True})

    async def post_check(self) -> Any:
        """Judge one thread without taking any action."""
        services = self._services()
        if services.store is None:
            return error_response(NO_STORE_REPLY, status_code=503)
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        tid = payload.get("tid")
        if type(tid) is not int or not 0 < tid < 10**MAX_ID_DIGITS:
            return error_response("tid 必须是正整数")
        raw_rules = payload.get("rules")
        if raw_rules is None:
            rules = ""
        elif isinstance(raw_rules, str):
            rules = raw_rules.strip()
        else:
            return error_response("rules 必须是字符串")
        if len(rules) > MAX_TEMP_RULES_CHARS:
            return error_response(f"临时吧规不能超过 {MAX_TEMP_RULES_CHARS} 字")
        if services.gateway is None:
            return error_response(NO_ACCOUNT_REPLY, status_code=503)
        if self._check_lock.locked():
            return error_response(BUSY_CHECK_REPLY, status_code=409)

        async with self._check_lock:
            settings = services.settings
            try:
                item = await services.gateway.get_thread_item_by_tid(tid)
                urls: list[str] = []
                if settings.review_images and item.image_urls:
                    urls = await services.gateway.fetch_images(
                        item.image_urls, settings.max_images
                    )
                verdict = await services.judge.judge_with_rules(item, urls, rules)
            except (JudgeError, ImageFetchError, TiebaActionError) as exc:
                return error_response(
                    services.redact(exc) or "审查失败", status_code=502
                )
            except Exception as exc:
                logger.error(
                    f"Web check of thread {tid} failed:\n{services.redact_trace(exc)}"
                )
                return error_response(INTERNAL_ERROR, status_code=500)

        action = decide_action(
            verdict,
            auto_delete_enabled=settings.auto_delete_enabled,
            auto_delete_threshold=settings.auto_delete_threshold,
            report_threshold=settings.report_threshold,
        )
        return json_response(
            {
                "item": _item_dto(item),
                "verdict": _verdict_dto(verdict),
                "action": action.value,
                "images_reviewed": len(urls),
                "images_total": len(item.image_urls),
            }
        )
