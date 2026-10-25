"""AstrBot plugin entry: config, background patrol, push messages and ``/tb``."""

from __future__ import annotations

import asyncio
import re
import time
import traceback

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.judge import FAILED_CATEGORY
from .core.models import (
    ALLOWED_BAN_DAYS,
    Case,
    CaseStatus,
    ImageFetchError,
    ItemKind,
    JudgeError,
    PatrolReport,
    TiebaActionError,
    decide_action,
)
from .core.store import Store
from .services.case_service import CaseError, CaseService
from .services.llm_judge import LlmJudge
from .services.patrol import Patrol, PatrolSettings
from .services.tieba_gateway import TiebaGateway

PLUGIN_NAME = "astrbot_plugin_tieba_moderator"
DB_FILE = "moderator.db"
MIN_INTERVAL_SECONDS = 60
STARTUP_DELAY_SECONDS = 30
"""Give the messaging platforms time to connect before the first push."""
EXCERPT_CHARS = 120
TITLE_CHARS = 40
NAME_CHARS = 40
CATEGORY_CHARS = 20
REASON_CHARS = 200
NOTE_CHARS = 300
FORUM_CHARS = 40
ERROR_CHARS = 200
LIST_LIMIT = 20
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
DAYS_TEXT = "|".join(str(d) for d in ALLOWED_BAN_DAYS)
NO_ACCOUNT_REPLY = "未配置吧务账号的 BDUSS / STOKEN，巡查和贴吧操作不可用。"
NO_PERMISSION_REPLY = (
    "你没有处理案件的权限（需要 AstrBot 管理员或在 notify.operators 中）。"
)
ADMIN_ONLY_REPLY = "该指令只有 AstrBot 管理员可以使用。"


def _clamp_number(raw: object, default: float, low: float, high: float) -> float:
    """Coerce a config value to a number inside ``[low, high]``.

    Args:
        raw: Value from the config.
        default: Used when the value is not a number.
        low: Lower bound.
        high: Upper bound.

    Returns:
        The clamped value.
    """
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = default
    if value != value:  # NaN
        value = default
    return min(high, max(low, value))


def _parse_id(raw: str) -> int | None:
    """Parse a positive id argument that fits in SQLite's INTEGER.

    Args:
        raw: The raw command argument.

    Returns:
        The id, or None when it is not a positive decimal integer.
    """
    raw = raw.strip()
    if not raw.isdecimal() or len(raw) > 18 or int(raw) <= 0:
        return None
    return int(raw)


_CONTROL_CHARS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]"
)
"""Control and bidirectional-override characters that must not reach a push."""


def _one_line(value: object, limit: int) -> str:
    """Render an untrusted field as one safe line of plain text.

    Titles, nicknames, verdict text and moderator notes are attacker-influenced.
    A newline in any of them would let the author forge an extra line in the
    push card, including a fake ``处理：/tb ban ...`` hint, so every field that
    reaches a message goes through here.

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


def format_case(case: Case, *, detail: bool = False) -> str:
    """Render a case as plain text for moderators.

    No images are attached on purpose: the content may be pornographic and
    pushing it could get the group or the bot flagged.

    Args:
        case: The case to render.
        detail: Also show handler and note (for ``/tb show``).

    Returns:
        Plain text.
    """
    item, verdict = case.item, case.verdict
    author = item.author
    who = _one_line(author.nick_name, NAME_CHARS) or _one_line(
        author.user_name, NAME_CHARS
    )
    if not who:
        who = str(author.user_id)
    elif author.user_name and author.user_name != who:
        who = f"{who}（{_one_line(author.user_name, NAME_CHARS)}）"
    kind = KIND_NAMES[item.kind]
    if item.kind is ItemKind.POST:
        kind += f"（{item.floor} 楼）"
    elif item.kind is ItemKind.COMMENT:
        kind += f"（{item.floor} 楼的楼中楼）"
    excerpt = _one_line(item.text, EXCERPT_CHARS)
    if item.kind is ItemKind.THREAD and item.title:
        excerpt = f"【{_one_line(item.title, TITLE_CHARS)}】{excerpt}"
    category = _one_line(verdict.category, CATEGORY_CHARS) or "未分类"
    failed = verdict.category == FAILED_CATEGORY

    lines = [
        f"【贴吧巡查】案件 #{case.id} {STATUS_NAMES[case.status]}",
        f"吧：{_one_line(item.fname, FORUM_CHARS)}吧 | 类型：{kind}",
        f"作者：{who}",
        f"链接：{_one_line(item.link, 120)}",
    ]
    if failed:
        # A failed review has no usable confidence or ban suggestion.
        lines.append("判定：审查失败（未能自动判定）")
    else:
        lines.append(f"判定：{category} | 置信度 {verdict.confidence:.2f}")
        ban = f"{verdict.ban_days} 天（需吧务确认）" if verdict.ban_days else "不封禁"
        lines.append(f"建议封禁：{ban}")
    lines += [
        f"理由：{_one_line(verdict.reason, REASON_CHARS) or '无'}",
        f"摘录：{excerpt or '（无文字）'}",
    ]
    if item.image_urls:
        media = "视频（封面）" if item.has_video else f"{len(item.image_urls)} 张图片"
        lines.append(f"含{media}，消息不附图，请点链接查看。")
    if failed:
        lines.append("注意：这条内容没能自动审查，请点链接人工判断后再决定是否处理。")
    if detail and case.handled_by:
        handled_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(case.handled_at))
        lines.append(
            f"处理人：{_one_line(case.handled_by, NAME_CHARS)}（{handled_at}）"
        )
    if detail and case.note:
        lines.append(f"备注：{_one_line(case.note, NOTE_CHARS)}")
    if case.status is CaseStatus.PENDING:
        lines.append(
            f"处理：/tb del {case.id} | /tb ban {case.id} <{DAYS_TEXT}> | "
            f"/tb ignore {case.id}"
        )
    elif case.status is CaseStatus.AUTO_DELETED:
        lines.append(
            f"巡查已自动删除该内容。误删请恢复：/tb restore {case.id}；"
            f"需要封禁：/tb ban {case.id} <{DAYS_TEXT}>"
        )
    elif case.status is CaseStatus.DELETED:
        lines.append(
            f"恢复：/tb restore {case.id} | 封禁：/tb ban {case.id} <{DAYS_TEXT}>"
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
            f"- {_one_line(e, ERROR_CHARS)}" for e in report.errors[:REPORT_ERROR_LINES]
        ]
        if len(report.errors) > REPORT_ERROR_LINES:
            lines.append(
                f"- ……另有 {len(report.errors) - REPORT_ERROR_LINES} 条，见日志"
            )
    return "\n".join(lines)


class TiebaModeratorPlugin(Star):
    """Patrol Tieba forums and let an LLM flag rule-breaking content."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        """Parse and clamp the config; resources are opened in ``initialize``.

        Args:
            context: The star context.
            config: The plugin config.
        """
        super().__init__(context, config)
        self.config = config
        account = config.get("account") or {}
        patrol = config.get("patrol") or {}
        judge = config.get("judge") or {}
        action = config.get("action") or {}

        self._bduss = str(account.get("bduss") or "").strip()
        self._stoken = str(account.get("stoken") or "").strip()
        self._enabled = bool(patrol.get("enabled", True))
        self._interval = int(
            _clamp_number(
                patrol.get("interval_seconds"), 180, MIN_INTERVAL_SECONDS, 86400
            )
        )

        auto_threshold = _clamp_number(action.get("auto_delete_threshold"), 0.9, 0, 1)
        report_threshold = _clamp_number(action.get("report_threshold"), 0.6, 0, 1)
        if report_threshold > auto_threshold:
            logger.warning(
                f"report_threshold {report_threshold} is above auto_delete_threshold "
                f"{auto_threshold}; using {auto_threshold} for both."
            )
            report_threshold = auto_threshold

        forums = [str(f).strip() for f in config.get("forums") or []]
        whitelist = [str(w).strip() for w in patrol.get("whitelist") or []]
        review_images = bool(judge.get("review_images", True))
        max_images = int(_clamp_number(judge.get("max_images"), 4, 0, 9))
        self._settings = PatrolSettings(
            forums=tuple(dict.fromkeys(f for f in forums if f)),
            thread_pages=int(_clamp_number(patrol.get("thread_pages"), 1, 1, 5)),
            lookback_hours=int(_clamp_number(patrol.get("lookback_hours"), 6, 1, 168)),
            review_posts=bool(patrol.get("review_posts", True)),
            review_comments=bool(patrol.get("review_comments", True)),
            skip_top_and_good=bool(patrol.get("skip_top_and_good", True)),
            skip_bawu=bool(patrol.get("skip_bawu", True)),
            whitelist=frozenset(w for w in whitelist if w),
            max_items_per_round=int(
                _clamp_number(patrol.get("max_items_per_round"), 60, 1, 500)
            ),
            review_images=review_images,
            max_images=max_images,
            judge_concurrency=int(_clamp_number(judge.get("concurrency"), 2, 1, 8)),
            auto_delete_enabled=bool(action.get("auto_delete", False)),
            auto_delete_threshold=auto_threshold,
            report_threshold=report_threshold,
        )
        self._judge = LlmJudge(
            context,
            provider_id=str(judge.get("provider_id") or ""),
            rules=str(judge.get("rules") or ""),
            timeout_seconds=_clamp_number(judge.get("timeout_seconds"), 60, 10, 600),
            review_images=review_images,
            max_images=max_images,
        )

        self._store: Store | None = None
        self._gateway: TiebaGateway | None = None
        self._patrol: Patrol | None = None
        self._cases: CaseService | None = None
        self._task: asyncio.Task | None = None
        self._last_report: PatrolReport | None = None
        self._manual_running = False
        """True while a ``/tb run`` round is in flight, so repeats are refused."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Open the store, build the services and start the patrol loop."""
        self._store = Store(StarTools.get_data_dir(PLUGIN_NAME) / DB_FILE)
        if not self._bduss or not self._stoken:
            logger.warning(
                "Tieba credentials are missing (account.bduss / account.stoken); "
                "the patrol is not started."
            )
            return
        try:
            self._gateway = TiebaGateway(self._bduss, self._stoken)
        except ValueError:
            # aiotieba echoes the offending secret in its ValueError message, so
            # neither the message nor the traceback may be logged.
            logger.error(
                "account.bduss / account.stoken 格式不正确，"
                "请重新从浏览器 Cookie 复制值本身，不要带 BDUSS= 前缀或引号；"
                "巡查和贴吧操作已停用。"
            )
            return
        self._patrol = Patrol(
            gateway=self._gateway,
            store=self._store,
            judge=self._judge,
            notify=self._notify,
            settings=self._settings,
        )
        self._cases = CaseService(gateway=self._gateway, store=self._store)
        if not self._settings.forums:
            logger.warning("No forums configured; the patrol is not started.")
            return
        if not self._enabled:
            logger.info("Patrol is disabled in the config; commands still work.")
            return
        self._task = asyncio.create_task(self._patrol_loop())
        logger.info(
            f"Tieba patrol started: forums={list(self._settings.forums)}, "
            f"interval={self._interval}s, "
            f"auto_delete={self._settings.auto_delete_enabled}"
        )

    async def terminate(self) -> None:
        """Stop the loop and release the gateway and the store."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"Patrol loop ended with an error:\n{self._redact(exc)}")
            self._task = None
        if self._gateway is not None:
            try:
                await self._gateway.close()
            except Exception as exc:
                logger.warning(f"Failed to close Tieba gateway:\n{self._redact(exc)}")
            self._gateway = None
        if self._store is not None:
            self._store.close()
            self._store = None

    async def _patrol_loop(self) -> None:
        """Run a patrol round every ``interval`` seconds until cancelled."""
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while True:
            try:
                report = await self._patrol.run_once()
                self._last_report = report
                if report.errors:
                    logger.warning(f"Patrol round finished:\n{format_report(report)}")
            except Exception as exc:
                logger.error(f"Patrol round failed:\n{self._redact(exc)}")
            await asyncio.sleep(self._interval)

    def _redact(self, exc: BaseException, *, brief: bool = False) -> str:
        """Format an exception with credentials removed.

        Args:
            exc: The exception.
            brief: Only the message (for replies); otherwise the full traceback
                (for logs).

        Returns:
            Text with any BDUSS / STOKEN value replaced by ``***``.
        """
        text = str(exc) if brief else "".join(traceback.format_exception(exc))
        for secret in (self._bduss, self._stoken):
            if secret:
                text = text.replace(secret, "***")
        return text

    async def _notify(self, case: Case) -> None:
        """Push a new case to every bound session, as plain text only.

        Args:
            case: A new PENDING or AUTO_DELETED case.

        Raises:
            RuntimeError: No session is bound, or at least one push failed.
        """
        notify = self.config.get("notify") or {}
        sessions = [str(s) for s in notify.get("sessions") or []]
        if not sessions:
            raise RuntimeError(f"案件 #{case.id} 未推送：没有绑定推送会话（/tb bind）")
        text = format_case(case)
        failed = []
        for umo in sessions:
            try:
                ok = await self.context.send_message(umo, MessageChain().message(text))
            except Exception as exc:
                logger.warning(f"Push to {umo} failed: {type(exc).__name__}: {exc}")
                ok = False
            if not ok:
                failed.append(umo)
        if failed:
            logger.warning(
                f"Case {case.id}: push failed for sessions {', '.join(failed)}"
            )
            # Session ids stay in the log: the error reaches the report and the
            # chat, where platform / group identifiers are not needed.
            raise RuntimeError(
                f"案件 #{case.id} 推送失败：{len(sessions)} 个会话中有 "
                f"{len(failed)} 个未送达，详情见日志。"
            )

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _can_operate(self, event: AstrMessageEvent) -> bool:
        """Whether the sender may handle cases.

        Args:
            event: The command event.

        Returns:
            True for AstrBot admins and senders listed in ``notify.operators``.
        """
        if event.is_admin():
            return True
        operators = (self.config.get("notify") or {}).get("operators") or []
        return str(event.get_sender_id()) in {str(o).strip() for o in operators}

    @filter.command_group("tb")
    def tb(self):
        """贴吧 AI 巡查"""

    @tb.command("status")
    async def tb_status(self, event: AstrMessageEvent):
        """查看巡查状态、上一轮报告和待处理数"""
        if self._patrol is None:
            state = "未运行（缺少账号凭据）"
        elif self._task is not None and not self._task.done():
            state = f"运行中，每 {self._interval} 秒一轮"
        elif not self._enabled:
            state = "定时巡查已关闭（可用 /tb run 手动巡查）"
        else:
            state = "未运行（没有配置吧名）"
        s = self._settings
        auto = (
            f"开启（阈值 {s.auto_delete_threshold:.2f}）"
            if s.auto_delete_enabled
            else "关闭"
        )
        lines = [
            f"巡查：{state}",
            f"吧：{'、'.join(s.forums) or '未配置'}",
            f"自动删除：{auto} | 上报阈值 {s.report_threshold:.2f}",
        ]
        if self._store is not None:
            lines.append(f"待处理案件：{self._store.count_cases(CaseStatus.PENDING)}")
        if self._last_report is not None:
            lines.append(
                "上一轮："
                + format_report(self._last_report, errors=self._can_operate(event))
            )
        else:
            lines.append("上一轮：本次启动后还没有巡查过")
        yield event.plain_result("\n".join(lines))

    @tb.command("run")
    async def tb_run(self, event: AstrMessageEvent):
        """立即巡查一轮"""
        if not self._can_operate(event):
            yield event.plain_result(NO_PERMISSION_REPLY)
            return
        if self._patrol is None:
            yield event.plain_result(NO_ACCOUNT_REPLY)
            return
        if not self._settings.forums:
            yield event.plain_result("没有配置要巡查的吧。")
            return
        if self._manual_running:
            yield event.plain_result("已有一轮手动巡查在进行，请稍后。")
            return
        self._manual_running = True
        try:
            yield event.plain_result("开始巡查，完成后回复结果……")
            try:
                report = await self._patrol.run_once()
            except Exception as exc:
                logger.error(f"Manual patrol round failed:\n{self._redact(exc)}")
                yield event.plain_result("巡查失败，详见 AstrBot 日志。")
                return
            self._last_report = report
            yield event.plain_result("巡查完成：" + format_report(report, errors=True))
        finally:
            self._manual_running = False

    @tb.command("list")
    async def tb_list(self, event: AstrMessageEvent):
        """列出待处理案件"""
        if not self._can_operate(event):
            yield event.plain_result(NO_PERMISSION_REPLY)
            return
        total = self._store.count_cases(CaseStatus.PENDING)
        if not total:
            yield event.plain_result("没有待处理的案件。")
            return
        lines = [f"待处理案件 {total} 个（最多显示 {LIST_LIMIT} 个）："]
        for case in self._store.list_cases(CaseStatus.PENDING, LIST_LIMIT):
            item, verdict = case.item, case.verdict
            source = item.title if item.kind is ItemKind.THREAD else item.text
            excerpt = _one_line(source, 30)
            who = _one_line(
                item.author.nick_name or item.author.user_name, NAME_CHARS
            ) or (str(item.author.user_id))
            category = _one_line(verdict.category, CATEGORY_CHARS) or "未分类"
            confidence = (
                "审查失败"
                if verdict.category == FAILED_CATEGORY
                else f"{verdict.confidence:.2f}"
            )
            lines.append(
                f"#{case.id} [{_one_line(item.fname, FORUM_CHARS)}] "
                f"{KIND_NAMES[item.kind]} {category} {confidence} {who}：{excerpt}"
            )
        lines.append("详情：/tb show <编号>")
        yield event.plain_result("\n".join(lines))

    @tb.command("show")
    async def tb_show(self, event: AstrMessageEvent, case_id: str = ""):
        """查看案件详情"""
        if not self._can_operate(event):
            yield event.plain_result(NO_PERMISSION_REPLY)
            return
        cid = _parse_id(case_id)
        if cid is None:
            yield event.plain_result("用法：/tb show <案件编号>，编号是正整数。")
            return
        case = self._store.get_case(cid)
        if case is None:
            yield event.plain_result(f"案件 #{cid} 不存在。")
            return
        yield event.plain_result(format_case(case, detail=True))

    @tb.command("del")
    async def tb_del(self, event: AstrMessageEvent, case_id: str = ""):
        """确认删除"""
        async for result in self._handle_case(event, "del", case_id):
            yield result

    @tb.command("ban")
    async def tb_ban(self, event: AstrMessageEvent, case_id: str = "", days: str = ""):
        """删除并封禁"""
        async for result in self._handle_case(event, "ban", case_id, days):
            yield result

    @tb.command("ignore")
    async def tb_ignore(self, event: AstrMessageEvent, case_id: str = ""):
        """判为误报"""
        async for result in self._handle_case(event, "ignore", case_id):
            yield result

    @tb.command("restore")
    async def tb_restore(self, event: AstrMessageEvent, case_id: str = ""):
        """恢复被删内容"""
        async for result in self._handle_case(event, "restore", case_id):
            yield result

    async def _handle_case(
        self, event: AstrMessageEvent, op: str, case_id: str, days: str = ""
    ):
        """Shared body of del / ban / ignore / restore.

        Args:
            event: The command event.
            op: One of ``del``, ``ban``, ``ignore``, ``restore``.
            case_id: Raw case id argument.
            days: Raw ban days argument (``ban`` only).

        Yields:
            Reply results.
        """
        if not self._can_operate(event):
            yield event.plain_result(NO_PERMISSION_REPLY)
            return
        usage = (
            f"/tb ban <案件编号> <{DAYS_TEXT}>"
            if op == "ban"
            else f"/tb {op} <案件编号>"
        )
        cid = _parse_id(case_id)
        if cid is None:
            yield event.plain_result(f"用法：{usage}，编号是正整数。")
            return
        days = days.strip()
        if op == "ban" and (
            len(days) > 2 or not days.isdecimal() or int(days) not in ALLOWED_BAN_DAYS
        ):
            # The length check runs first: int() on a megabyte of digits is a
            # needlessly expensive parse triggered from chat.
            yield event.plain_result(f"用法：{usage}，封禁天数只能是 {DAYS_TEXT}。")
            return
        if self._cases is None:
            yield event.plain_result(NO_ACCOUNT_REPLY)
            return
        operator = str(event.get_sender_id())
        try:
            if op == "del":
                case = await self._cases.delete(cid, operator)
            elif op == "ban":
                case = await self._cases.ban(cid, int(days), operator)
            elif op == "ignore":
                case = await self._cases.ignore(cid, operator)
            else:
                case = await self._cases.restore(cid, operator)
        except CaseError as exc:
            yield event.plain_result(str(exc))
            return
        except TiebaActionError as exc:
            yield event.plain_result(f"贴吧操作失败：{self._redact(exc, brief=True)}")
            return
        except Exception as exc:
            logger.error(f"Case {op} #{cid} failed:\n{self._redact(exc)}")
            yield event.plain_result("操作失败，详见 AstrBot 日志。")
            return
        done = {
            "del": "已删除",
            "ban": f"已删除并封禁 {days} 天",
            "ignore": "已标记为误报",
            "restore": "已恢复内容（封禁不会解除）",
        }[op]
        yield event.plain_result(f"案件 #{case.id} {done}。")

    @tb.command("check")
    async def tb_check(self, event: AstrMessageEvent, tid: str = ""):
        """Review a thread with its title and forum, without taking action.

        Args:
            event: Administrator command event.
            tid: Raw Tieba thread ID.

        Yields:
            The review result with its source title and forum, or an error.
        """
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_REPLY)
            return
        thread_id = _parse_id(tid)
        if thread_id is None:
            yield event.plain_result("用法：/tb check <帖子 tid>，tid 是正整数。")
            return
        if self._gateway is None:
            yield event.plain_result(NO_ACCOUNT_REPLY)
            return
        try:
            item = await self._gateway.get_thread_item_by_tid(thread_id)
            urls: list[str] = []
            if self._settings.review_images and item.image_urls:
                urls = await self._gateway.fetch_images(
                    item.image_urls, self._settings.max_images
                )
            verdict = await self._judge(item, urls)
        except (JudgeError, ImageFetchError, TiebaActionError) as exc:
            yield event.plain_result(f"审查失败：{self._redact(exc, brief=True)}")
            return
        except Exception as exc:
            logger.error(f"Check of thread {thread_id} failed:\n{self._redact(exc)}")
            yield event.plain_result("审查失败，详见 AstrBot 日志。")
            return
        s = self._settings
        action = decide_action(
            verdict,
            auto_delete_enabled=s.auto_delete_enabled,
            auto_delete_threshold=s.auto_delete_threshold,
            report_threshold=s.report_threshold,
        )
        outcome = {"none": "不处理", "report": "推送给吧务", "auto_delete": "自动删除"}
        ban = f"{verdict.ban_days} 天" if verdict.ban_days else "不封禁"
        lines = [
            f"审查结果（未做任何处置）：帖子 {thread_id}",
            f"标题：{item.title} | 吧名：{item.fname}",
            f"违规：{'是' if verdict.violation else '否'} | "
            f"类别：{verdict.category or '无'} | 置信度 {verdict.confidence:.2f}",
            f"理由：{verdict.reason or '无'}",
            f"建议封禁：{ban}",
            f"按当前阈值，巡查时会：{outcome[action.value]}",
            f"图片：审查了 {len(urls)} 张，共 {len(item.image_urls)} 张",
        ]
        yield event.plain_result("\n".join(lines))

    @tb.command("bind")
    async def tb_bind(self, event: AstrMessageEvent):
        """把当前会话加入推送列表"""
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_REPLY)
            return
        umo = event.unified_msg_origin
        notify = self.config.get("notify") or {}
        sessions = [str(s) for s in notify.get("sessions") or []]
        if umo in sessions:
            yield event.plain_result("当前会话已经在推送列表里。")
            return
        self.config["notify"] = {**notify, "sessions": [*sessions, umo]}
        self.config.save_config()
        yield event.plain_result(
            f"已把当前会话加入推送列表，共 {len(sessions) + 1} 个。"
        )
