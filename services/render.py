"""Plain-text rendering for ``/tb`` replies and moderator push messages.

Everything rendered here can contain attacker-controlled text (titles, nicknames,
verdict reasons, moderator notes), so each field is flattened to one line by
``one_line`` before it is joined into a multi-line message.
"""

from __future__ import annotations

import re
import time

from ..core.models import Case, CaseStatus, ItemKind, PatrolReport

EXCERPT_CHARS = 120
TITLE_CHARS = 40
NAME_CHARS = 40
CATEGORY_CHARS = 20
REASON_CHARS = 200
NOTE_CHARS = 300
FORUM_CHARS = 40
ERROR_CHARS = 200
REPORT_ERROR_LINES = 3

KIND_NAMES = {
    ItemKind.THREAD: "主题帖",
    ItemKind.POST: "楼层",
    ItemKind.COMMENT: "楼中楼",
}
STATUS_NAMES = {
    CaseStatus.PENDING: "待处理",
    CaseStatus.AUTO_DELETED: "已自动删除",
    CaseStatus.DELETED: "已删除",
    CaseStatus.BANNED: "已删除并封禁",
    CaseStatus.IGNORED: "已忽略",
    CaseStatus.RESTORED: "已恢复",
}

_CONTROL_CHARS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]"
)
"""Control and bidirectional-override characters that must not reach a message."""


def one_line(value: object, limit: int) -> str:
    """Render an untrusted field as one safe line of plain text.

    A newline in a title, nickname or verdict reason would let the author forge
    an extra line in the push card, including a fake ``处理：/tb ban ...`` hint,
    so every field that reaches a message goes through here.

    Args:
        value: The raw field; converted with ``str``.
        limit: Maximum characters kept.

    Returns:
        Text with control and bidi characters dropped, whitespace collapsed and
        the length capped.
    """
    text = _CONTROL_CHARS.sub("", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def format_case(
    case: Case, *, days_text: str, failed_category: str, detail: bool = False
) -> str:
    """Render a case as plain text for moderators.

    No images are attached on purpose: the content may be pornographic and
    pushing it could get the group or the bot flagged.

    Args:
        case: The case to render.
        days_text: Allowed ban durations, e.g. ``1|3|10``.
        failed_category: Verdict category of an unreviewable item.
        detail: Also show handler and note (for ``/tb show``).

    Returns:
        Plain text.
    """
    item, verdict = case.item, case.verdict
    author = item.author
    who = one_line(author.nick_name, NAME_CHARS) or one_line(
        author.user_name, NAME_CHARS
    )
    if not who:
        who = str(author.user_id)
    elif author.user_name and author.user_name != who:
        who = f"{who}（{one_line(author.user_name, NAME_CHARS)}）"
    kind = KIND_NAMES[item.kind]
    if item.kind is ItemKind.POST:
        kind += f"（{item.floor} 楼）"
    elif item.kind is ItemKind.COMMENT:
        kind += f"（{item.floor} 楼的楼中楼）"
    excerpt = one_line(item.text, EXCERPT_CHARS)
    if item.kind is ItemKind.THREAD and item.title:
        excerpt = f"【{one_line(item.title, TITLE_CHARS)}】{excerpt}"
    category = one_line(verdict.category, CATEGORY_CHARS) or "未分类"
    failed = verdict.category == failed_category

    lines = [
        f"【贴吧巡查】案件 #{case.id} {STATUS_NAMES[case.status]}",
        f"吧：{one_line(item.fname, FORUM_CHARS)}吧 | 类型：{kind}",
        f"作者：{who}",
        f"链接：{one_line(item.link, 120)}",
    ]
    if failed:
        # A failed review has no usable confidence or ban suggestion.
        lines.append("判定：审查失败（未能自动判定）")
    else:
        lines.append(f"判定：{category} | 置信度 {verdict.confidence:.2f}")
        ban = f"{verdict.ban_days} 天（需吧务确认）" if verdict.ban_days else "不封禁"
        lines.append(f"建议封禁：{ban}")
    lines += [
        f"理由：{one_line(verdict.reason, REASON_CHARS) or '无'}",
        f"摘录：{excerpt or '（无文字）'}",
    ]
    if item.image_urls:
        media = "视频（封面）" if item.has_video else f"{len(item.image_urls)} 张图片"
        lines.append(f"含{media}，消息不附图，请点链接查看。")
    if failed:
        lines.append("注意：这条内容没能自动审查，请点链接人工判断后再决定是否处理。")
    if detail and case.handled_by:
        handled_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(case.handled_at))
        lines.append(f"处理人：{one_line(case.handled_by, NAME_CHARS)}（{handled_at}）")
    if detail and case.note:
        lines.append(f"备注：{one_line(case.note, NOTE_CHARS)}")
    if case.status is CaseStatus.PENDING:
        lines.append(
            f"处理：/tb del {case.id} | /tb ban {case.id} <{days_text}> | "
            f"/tb ignore {case.id}"
        )
    elif case.status is CaseStatus.AUTO_DELETED:
        lines.append(
            f"巡查已自动删除该内容。误删请恢复：/tb restore {case.id}；"
            f"需要封禁：/tb ban {case.id} <{days_text}>"
        )
    elif case.status is CaseStatus.DELETED:
        lines.append(
            f"恢复：/tb restore {case.id} | 封禁：/tb ban {case.id} <{days_text}>"
        )
    elif case.status is CaseStatus.BANNED:
        lines.append(f"恢复内容（不解封）：/tb restore {case.id}")
    return "\n".join(lines)


def format_report(report: PatrolReport, *, errors: bool = True) -> str:
    """Summarize a patrol report in a few lines.

    Args:
        report: The report.
        errors: Include the error lines. Turned off for viewers who may not
            handle cases, because the lines quote exception text.

    Returns:
        Plain text.
    """
    finished = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(report.finished_at))
    lines = [
        f"{finished}，用时 {max(0, report.finished_at - report.started_at)} 秒，"
        f"巡查 {len(report.forums)} 个吧",
        f"审查 {report.reviewed} | 上报 {report.reported} | "
        f"自动删除 {report.auto_deleted} | 跳过 {report.skipped} | "
        f"顺延 {report.deferred} | 错误 {len(report.errors)}",
    ]
    if errors:
        lines += [
            f"- {one_line(e, ERROR_CHARS)}" for e in report.errors[:REPORT_ERROR_LINES]
        ]
        if len(report.errors) > REPORT_ERROR_LINES:
            lines.append(
                f"- ……另有 {len(report.errors) - REPORT_ERROR_LINES} 条，见日志"
            )
    return "\n".join(lines)


def case_list_line(case: Case, *, failed_category: str) -> str:
    """Render one row of the ``/tb list`` reply.

    Args:
        case: The case.
        failed_category: Verdict category of an unreviewable item.

    Returns:
        One line of plain text.
    """
    item, verdict = case.item, case.verdict
    source = item.title if item.kind is ItemKind.THREAD else item.text
    excerpt = one_line(source, 30)
    who = one_line(item.author.nick_name or item.author.user_name, NAME_CHARS) or str(
        item.author.user_id
    )
    category = one_line(verdict.category, CATEGORY_CHARS) or "未分类"
    confidence = (
        "审查失败"
        if verdict.category == failed_category
        else f"{verdict.confidence:.2f}"
    )
    return (
        f"#{case.id} [{one_line(item.fname, FORUM_CHARS)}] "
        f"{KIND_NAMES[item.kind]} {category} {confidence} {who}：{excerpt}"
    )
