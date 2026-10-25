"""Shared data model for the Tieba moderator plugin.

Everything here is pure (no astrbot / aiotieba imports) and frozen. State
changes go through ``dataclasses.replace``; never construct these classes with
positional arguments, so that adding a field cannot silently shift values.
"""

from __future__ import annotations

import dataclasses as dcs
from collections.abc import Awaitable, Callable
from enum import StrEnum

ALLOWED_BAN_DAYS = (1, 3, 10)
"""Ban durations a normal moderator account can apply on Tieba."""


class ItemKind(StrEnum):
    """Which layer of a Tieba thread a piece of content belongs to."""

    THREAD = "thread"
    """The thread itself: title plus the first floor."""
    POST = "post"
    """A reply floor (floor >= 2)."""
    COMMENT = "comment"
    """A reply inside a floor (楼中楼)."""


class Action(StrEnum):
    """What the patrol does with a verdict."""

    NONE = "none"
    REPORT = "report"
    AUTO_DELETE = "auto_delete"


class CaseStatus(StrEnum):
    """Lifecycle of a reported or auto-deleted item."""

    PENDING = "pending"
    """Reported, waiting for a moderator."""
    AUTO_DELETED = "auto_deleted"
    """Deleted by the patrol without human confirmation."""
    DELETED = "deleted"
    """Deleted after a moderator confirmed."""
    BANNED = "banned"
    """Deleted and the author banned, confirmed by a moderator."""
    IGNORED = "ignored"
    """A moderator judged it a false positive; nothing was changed on Tieba."""
    RESTORED = "restored"
    """A deleted item was restored by a moderator."""


@dcs.dataclass(frozen=True, kw_only=True)
class Author:
    """Author of a piece of content.

    Attributes:
        user_id: Numeric Tieba user id.
        portrait: Portrait id without the ``?t=`` suffix; used for banning.
        user_name: Login user name, may be empty.
        nick_name: Display name.
        level: Level in the forum.
        is_bawu: Whether the author is a moderator of the forum.
    """

    user_id: int
    portrait: str
    user_name: str = ""
    nick_name: str = ""
    level: int = 0
    is_bawu: bool = False


@dcs.dataclass(frozen=True, kw_only=True)
class ContentItem:
    """One reviewable piece of content.

    Attributes:
        kind: Thread, post or comment.
        fname: Forum name.
        fid: Forum id.
        tid: Thread id.
        pid: For a thread, the pid of its first floor; otherwise the item's own pid.
        author: Who wrote it.
        title: The thread title. For posts and comments this is the parent
            thread's title, given to the judge as context.
        text: Plain text. Links are rendered as ``text(url)`` so link spam is visible.
        image_urls: Image URLs on Tieba's CDN, in order. A video contributes its cover.
        create_time: Unix seconds.
        floor: Floor number; 1 for a thread, the parent floor for a comment.
        has_video: Whether the content contains a video (only its cover is reviewed).
    """

    kind: ItemKind
    fname: str
    fid: int
    tid: int
    pid: int
    author: Author
    title: str = ""
    text: str = ""
    image_urls: tuple[str, ...] = ()
    create_time: int = 0
    floor: int = 0
    has_video: bool = False

    @property
    def key(self) -> str:
        """Stable identity used for de-duplication and case lookup."""
        return f"{self.kind.value}:{self.pid}"

    @property
    def link(self) -> str:
        """Web link a moderator can open."""
        if self.kind is ItemKind.THREAD:
            return f"https://tieba.baidu.com/p/{self.tid}"
        return f"https://tieba.baidu.com/p/{self.tid}?pid={self.pid}#{self.pid}"


@dcs.dataclass(frozen=True, kw_only=True)
class Verdict:
    """The judge's decision on one item.

    Attributes:
        violation: Whether the item breaks the rules.
        category: Short label such as ``广告引流``; empty when not a violation.
        confidence: 0.0 - 1.0, how sure the judge is about ``violation``.
        reason: One or two sentences a moderator can read.
        ban_days: Suggested ban, 0 or one of ``ALLOWED_BAN_DAYS``. Never executed
            automatically.
    """

    violation: bool
    category: str = ""
    confidence: float = 0.0
    reason: str = ""
    ban_days: int = 0


@dcs.dataclass(frozen=True, kw_only=True)
class Case:
    """A reported or auto-deleted item and what moderators did with it.

    Attributes:
        id: Auto-increment id shown to moderators.
        item: The content.
        verdict: The judge's verdict.
        status: Current status.
        created_at: Unix seconds.
        handled_by: Who changed the status last (``patrol`` or a sender id).
        handled_at: Unix seconds of the last status change, 0 if never.
        note: Free text, e.g. the Tieba error of a failed action.
    """

    id: int
    item: ContentItem
    verdict: Verdict
    status: CaseStatus
    created_at: int
    handled_by: str = ""
    handled_at: int = 0
    note: str = ""


@dcs.dataclass(frozen=True, kw_only=True)
class PatrolReport:
    """Summary of one patrol round.

    Attributes:
        started_at: Unix seconds.
        finished_at: Unix seconds.
        forums: Forums scanned.
        reviewed: Items sent to the judge.
        reported: Cases created as PENDING.
        auto_deleted: Cases created as AUTO_DELETED.
        skipped: Items marked seen without review (too old, whitelisted, moderators...).
        deferred: Items left for the next round because of the per-round cap.
        errors: Human-readable error lines, already free of credentials.
    """

    started_at: int
    finished_at: int
    forums: tuple[str, ...] = ()
    reviewed: int = 0
    reported: int = 0
    auto_deleted: int = 0
    skipped: int = 0
    deferred: int = 0
    errors: tuple[str, ...] = ()


class JudgeError(Exception):
    """The judge could not produce a verdict (LLM failure, unparsable output...)."""


class ImageFetchError(Exception):
    """An image could not be downloaded; the item must not be judged without it."""


class TiebaActionError(Exception):
    """A delete / ban / restore call was rejected by Tieba.

    The message is shown to moderators and must not contain credentials.
    """


JudgeFn = Callable[[ContentItem, list[str]], Awaitable[Verdict]]
"""``judge(item, image_data_urls)``; raises ``JudgeError`` on failure."""

NotifyFn = Callable[[Case], Awaitable[None]]
"""Pushes a new PENDING or AUTO_DELETED case to the moderators."""


def decide_action(
    verdict: Verdict,
    *,
    auto_delete_enabled: bool,
    auto_delete_threshold: float,
    report_threshold: float,
) -> Action:
    """Map a verdict to what the patrol should do.

    Args:
        verdict: The judge's verdict.
        auto_delete_enabled: Whether deleting without confirmation is allowed.
        auto_delete_threshold: Minimum confidence for automatic deletion.
        report_threshold: Minimum confidence for reporting to moderators.

    Returns:
        ``AUTO_DELETE`` only when enabled and confident enough, ``REPORT`` for
        other violations above the report threshold, otherwise ``NONE``.
    """
    if not verdict.violation:
        return Action.NONE
    if auto_delete_enabled and verdict.confidence >= auto_delete_threshold:
        return Action.AUTO_DELETE
    if verdict.confidence >= report_threshold:
        return Action.REPORT
    return Action.NONE
