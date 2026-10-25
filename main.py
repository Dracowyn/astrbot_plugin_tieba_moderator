"""AstrBot plugin entry: config, background patrol, push messages and ``/tb``."""

from __future__ import annotations

import asyncio
import traceback

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.forums import (
    OperatorScope,
    bind_session,
    missing_case_reply,
    parse_forum_config,
    pending_count,
    scope_forum_keys,
)
from .core.judge import FAILED_CATEGORY
from .core.models import (
    ALLOWED_BAN_DAYS,
    Case,
    CaseStatus,
    ImageFetchError,
    JudgeError,
    PatrolReport,
    TiebaActionError,
    decide_action,
)
from .core.store import Store
from .services.case_service import CaseError, CaseService
from .services.llm_judge import LlmJudge
from .services.patrol import Patrol, PatrolSettings
from .services.render import (
    FORUM_CHARS,
    TITLE_CHARS,
    case_list_line,
    format_case,
    format_report,
    one_line,
)
from .services.tieba_gateway import TiebaGateway
from .web.routes import WebRoutes, WebServices

PLUGIN_NAME = "astrbot_plugin_tieba_moderator"
DB_FILE = "moderator.db"
MIN_INTERVAL_SECONDS = 60
STARTUP_DELAY_SECONDS = 30
"""Give the messaging platforms time to connect before the first push."""
LIST_LIMIT = 20
DAYS_TEXT = "|".join(str(d) for d in ALLOWED_BAN_DAYS)
NO_ACCOUNT_REPLY = "未配置吧务账号的 BDUSS / STOKEN，巡查和贴吧操作不可用。"
NO_PERMISSION_REPLY = (
    "你没有处理案件的权限（需要 AstrBot 管理员、全局操作员，或该吧的吧务）。"
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

        self._forum_config, warnings = parse_forum_config(config)
        for warning in warnings:
            logger.warning(warning)
        whitelist = [str(w).strip() for w in patrol.get("whitelist") or []]
        review_images = bool(judge.get("review_images", True))
        max_images = int(_clamp_number(judge.get("max_images"), 4, 0, 9))
        self._settings = PatrolSettings(
            forums=self._forum_config.forums,
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
            rules=self._forum_config.default_rules,
            forum_rules={
                key: profile.rules
                for key, profile in self._forum_config.profiles.items()
            },
            timeout_seconds=_clamp_number(judge.get("timeout_seconds"), 60, 10, 600),
            review_images=review_images,
            max_images=max_images,
        )

        self._store: Store | None = None
        self._gateway: TiebaGateway | None = None
        self._patrol: Patrol | None = None
        self._cases: CaseService | None = None
        self._task: asyncio.Task | None = None
        self._manual_task: asyncio.Task | None = None
        """Background manual round, shared by ``/tb run`` and ``POST patrol/run``."""
        self._last_report: PatrolReport | None = None
        self._web = WebRoutes(self._web_services)
        self._register_web_apis()

    # ------------------------------------------------------------------
    # Dashboard plugin view
    # ------------------------------------------------------------------

    def _register_web_apis(self) -> None:
        """Register every plugin-view route under the plugin-name prefix.

        Routes are matched by ``astrbot/dashboard/api/plugins.py:196-222``, which
        passes each ``<name>`` segment as a keyword argument to the handler and
        requires the dashboard JWT or a plugin-scoped API key
        (``:398-438`` and ``astrbot/dashboard/api/auth.py:167-195``).
        """
        register = getattr(self.context, "register_web_api", None)
        if register is None:
            # Star contexts always expose register_web_api
            # (astrbot/core/star/context.py:736); only a test double lacks it.
            logger.warning(
                "The star context has no register_web_api; the WebUI plugin view "
                "routes are not registered."
            )
            return
        web = self._web
        routes: tuple[tuple[str, object, list[str], str], ...] = (
            ("status", web.get_status, ["GET"], "Patrol status"),
            ("cases", web.get_cases, ["GET"], "List cases"),
            ("cases/<case_id>", web.get_case, ["GET"], "Get one case"),
            (
                "cases/<case_id>/images/<index>",
                web.get_case_image,
                ["GET"],
                "Get one case image",
            ),
            (
                "cases/<case_id>/delete",
                web.post_delete,
                ["POST"],
                "Delete case content",
            ),
            ("cases/<case_id>/ban", web.post_ban, ["POST"], "Delete and ban"),
            ("cases/<case_id>/ignore", web.post_ignore, ["POST"], "Ignore case"),
            (
                "cases/<case_id>/restore",
                web.post_restore,
                ["POST"],
                "Restore case content",
            ),
            ("stats", web.get_stats, ["GET"], "Case statistics"),
            ("reports", web.get_reports, ["GET"], "Patrol reports"),
            ("patrol/run", web.post_patrol_run, ["POST"], "Start a patrol round"),
            ("check", web.post_check, ["POST"], "Trial review of a thread"),
        )
        for path, handler, methods, desc in routes:
            register(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)

    @property
    def _manual_running(self) -> bool:
        """Whether a manual patrol round is in flight.

        Derived from the task so there is a single source of truth.
        """
        return self._manual_task is not None and not self._manual_task.done()

    def _web_services(self) -> WebServices:
        """Read the current dependencies for a Web API call.

        Returns:
            A snapshot of the live services. Handlers call this per request, so
            a reload that replaces the store or the gateway is observed.
        """
        return WebServices(
            store=self._store,
            patrol=self._patrol,
            gateway=self._gateway,
            cases=self._cases,
            judge=self._judge,
            settings=self._settings,
            interval_seconds=self._interval,
            patrol_enabled=self._enabled,
            loop_running=self._task is not None and not self._task.done(),
            manual_running=self._manual_running,
            start_patrol=self._start_manual_patrol,
            redact=lambda exc: self._redact(exc, brief=True),
            redact_trace=self._redact,
        )

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
        """Stop the loops and release the gateway and the store."""
        for name, task in (
            ("manual patrol", self._manual_task),
            ("patrol", self._task),
        ):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning(
                        f"The {name} task ended with an error:\n{self._redact(exc)}"
                    )
        self._task = None
        self._manual_task = None
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
                self._finish_round(report)
            except Exception as exc:
                logger.error(f"Patrol round failed:\n{self._redact(exc)}")
            await asyncio.sleep(self._interval)

    def _finish_round(self, report: PatrolReport) -> None:
        """Remember a finished round and persist its summary.

        Args:
            report: The report of the round that just finished.
        """
        self._last_report = report
        if report.errors:
            logger.warning(f"Patrol round finished:\n{format_report(report)}")
        if self._store is not None:
            try:
                self._store.save_report(report)
            except Exception as exc:
                # A report is a convenience; losing one must not stop the loop.
                logger.warning(f"Saving the patrol report failed:\n{self._redact(exc)}")

    def _start_manual_patrol(self) -> bool:
        """Start a manual round in the background.

        Serves both ``/tb run`` and ``POST patrol/run`` so the two cannot run at
        the same time.

        Returns:
            True when a round was started, False when one is already running.
        """
        if self._patrol is None:
            return False
        if self._manual_task is not None and not self._manual_task.done():
            return False
        self._manual_task = asyncio.create_task(self._run_manual_patrol())
        return True

    async def _run_manual_patrol(self) -> PatrolReport | None:
        """Run one manual round, recording its report.

        Returns:
            The report, or None when the round failed.
        """
        try:
            report = await self._patrol.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Manual patrol round failed:\n{self._redact(exc)}")
            return None
        self._finish_round(report)
        return report

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
        sessions = self._forum_config.profile(case.item.fname).sessions
        if not sessions:
            raise RuntimeError(
                f"案件 #{case.id}（{case.item.fname} 吧）未推送：没有绑定推送会话"
            )
        text = format_case(case, days_text=DAYS_TEXT, failed_category=FAILED_CATEGORY)
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

    def _scope(self, event: AstrMessageEvent) -> OperatorScope:
        """Resolve what the command sender may moderate.

        Args:
            event: The command event.

        Returns:
            A global scope for AstrBot administrators and global operators,
            the locally moderated forums for a forum moderator, otherwise an
            empty scope.
        """
        return self._forum_config.scope_for(
            str(event.get_sender_id()), is_admin=event.is_admin()
        )

    def _case_for(self, event: AstrMessageEvent, case_id: int) -> Case | None:
        """Load a case the sender is allowed to see.

        A forum moderator gets None for a case in another forum, exactly as for
        a missing case, so the reply cannot be used to probe case ids.

        Args:
            event: The command event.
            case_id: Case identifier.

        Returns:
            The case, or None when it does not exist or is out of scope.
        """
        if self._store is None:
            return None
        case = self._store.get_case(case_id)
        if case is None:
            return None
        if not self._forum_config.may_moderate(self._scope(event), case.item.fname):
            return None
        return case

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
        forums = []
        for fname in self._forum_config.forums:
            profile = self._forum_config.profile(fname)
            labels = []
            if profile.rules_overridden:
                labels.append("独立吧规")
            if profile.sessions_overridden:
                labels.append("独立推送")
            forums.append(fname + (f"（{'、'.join(labels)}）" if labels else ""))
        lines = [
            f"巡查：{state}",
            f"吧：{'、'.join(forums) or '未配置'}",
            f"自动删除：{auto} | 上报阈值 {s.report_threshold:.2f}",
        ]
        scope = self._scope(event)
        if self._store is not None:
            lines.append(f"待处理案件：{pending_count(self._store, scope)}")
        if self._last_report is not None:
            # Error lines quote exception text from any forum, so they are for
            # global operators only (forum-config contract §5).
            lines.append(
                "上一轮："
                + format_report(self._last_report, errors=scope.global_operator)
            )
        else:
            lines.append("上一轮：本次启动后还没有巡查过")
        yield event.plain_result("\n".join(lines))

    @tb.command("run")
    async def tb_run(self, event: AstrMessageEvent):
        """立即巡查一轮"""
        if not self._scope(event).global_operator:
            # One round covers every forum, so local moderators may not start it.
            yield event.plain_result(NO_PERMISSION_REPLY)
            return
        if self._patrol is None:
            yield event.plain_result(NO_ACCOUNT_REPLY)
            return
        if not self._settings.forums:
            yield event.plain_result("没有配置要巡查的吧。")
            return
        if not self._start_manual_patrol():
            yield event.plain_result("已有一轮手动巡查在进行，请稍后。")
            return
        yield event.plain_result("开始巡查，完成后回复结果……")
        task = self._manual_task
        try:
            report = await task
        except asyncio.CancelledError:
            if task.cancelled():
                # terminate() cancelled the round; there is nobody to reply to.
                return
            raise
        if report is None:
            yield event.plain_result("巡查失败，详见 AstrBot 日志。")
            return
        yield event.plain_result("巡查完成：" + format_report(report, errors=True))

    @tb.command("list")
    async def tb_list(self, event: AstrMessageEvent):
        """列出待处理案件"""
        scope = self._scope(event)
        if not scope.any:
            yield event.plain_result(NO_PERMISSION_REPLY)
            return
        if self._store is None:
            yield event.plain_result("插件存储尚未就绪，请稍后重试。")
            return
        total, cases = self._store.query_cases(
            statuses=(CaseStatus.PENDING,),
            fnames=scope_forum_keys(scope),
            limit=LIST_LIMIT,
        )
        if not total:
            yield event.plain_result("没有待处理的案件。")
            return
        lines = [f"待处理案件 {total} 个（最多显示 {LIST_LIMIT} 个）："]
        lines += [
            case_list_line(case, failed_category=FAILED_CATEGORY) for case in cases
        ]
        lines.append("详情：/tb show <编号>")
        yield event.plain_result("\n".join(lines))

    @tb.command("show")
    async def tb_show(self, event: AstrMessageEvent, case_id: str = ""):
        """查看案件详情"""
        scope = self._scope(event)
        if not scope.any:
            yield event.plain_result(NO_PERMISSION_REPLY)
            return
        cid = _parse_id(case_id)
        if cid is None:
            yield event.plain_result("用法：/tb show <案件编号>，编号是正整数。")
            return
        case = self._case_for(event, cid)
        if case is None:
            yield event.plain_result(missing_case_reply(cid, scope))
            return
        yield event.plain_result(
            format_case(
                case,
                days_text=DAYS_TEXT,
                failed_category=FAILED_CATEGORY,
                detail=True,
            )
        )

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
        scope = self._scope(event)
        if not scope.any:
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
        # Scope is checked before CaseService: an out-of-scope id must not reach
        # the case service, which would otherwise act on another forum.
        if self._case_for(event, cid) is None:
            yield event.plain_result(missing_case_reply(cid, scope))
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
            f"标题：{one_line(item.title, TITLE_CHARS)} | "
            f"吧名：{one_line(item.fname, FORUM_CHARS)}",
            f"违规：{'是' if verdict.violation else '否'} | "
            f"类别：{verdict.category or '无'} | 置信度 {verdict.confidence:.2f}",
            f"理由：{verdict.reason or '无'}",
            f"建议封禁：{ban}",
            f"按当前阈值，巡查时会：{outcome[action.value]}",
            f"图片：审查了 {len(urls)} 张，共 {len(item.image_urls)} 张",
        ]
        yield event.plain_result("\n".join(lines))

    @tb.command("bind")
    async def tb_bind(self, event: AstrMessageEvent, fname: str = ""):
        """把当前会话加入全局或指定吧的推送列表"""
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_REPLY)
            return
        result = bind_session(
            self.config, self._forum_config, fname.strip(), event.unified_msg_origin
        )
        if isinstance(result, str):
            yield event.plain_result(result)
            return
        self.config.save_config()
        self._forum_config, warnings = parse_forum_config(self.config)
        for warning in warnings:
            logger.warning(warning)
        yield event.plain_result(
            f"已把当前会话加入{result.label}推送列表，共 {result.total} 个。"
        )
