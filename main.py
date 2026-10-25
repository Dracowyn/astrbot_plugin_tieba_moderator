"""AstrBot plugin entry: persisted settings, patrol lifecycle and ``/tb``."""

from __future__ import annotations

import asyncio
import dataclasses as dcs
import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .core.forums import (
    ForumConfig,
    OperatorScope,
    missing_case_reply,
    parse_case_id,
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
from .core.settings import SettingsSnapshot, default_global_settings, resolve
from .core.store import Store
from .services.case_service import CaseError, CaseService
from .services.gateway_pool import GatewayPool
from .services.llm_judge import LlmJudge
from .services.patrol import Patrol
from .services.render import (
    FORUM_CHARS,
    TITLE_CHARS,
    case_list_line,
    format_case,
    format_report,
    one_line,
)
from .services.settings_service import SettingsError, SettingsService
from .web.route_table import route_table
from .web.routes import WebRoutes, WebServices
from .web.settings_routes import (
    SettingsRoutes,
    SettingsWebServices,
    provider_rows,
)

PLUGIN_NAME = "astrbot_plugin_tieba_moderator"
DB_FILE = "moderator.db"
STARTUP_DELAY_SECONDS = 30
"""Give the messaging platforms time to connect before the first push."""
LIST_LIMIT = 20
DAYS_TEXT = "|".join(str(d) for d in ALLOWED_BAN_DAYS)
NO_ACCOUNT_REPLY = "未配置可用的吧务账号，请在插件页设置。"
NO_PERMISSION_REPLY = (
    "你没有处理案件的权限（需要 AstrBot 管理员、全局操作员，或该吧的吧务）。"
)
ADMIN_ONLY_REPLY = "该指令只有 AstrBot 管理员可以使用。"


class TiebaModeratorPlugin(Star):
    """Patrol Tieba forums and let an LLM flag rule-breaking content."""

    def __init__(self, context: Context, config=None) -> None:
        """Register commands and routes without reading AstrBot configuration.

        Args:
            context: The star context.
            config: Ignored; retained only for AstrBot's constructor convention.
        """
        super().__init__(context)
        self._settings_service: SettingsService | None = None
        self._store: Store | None = None
        self._pool: GatewayPool | None = None
        self._judge: LlmJudge | None = None
        self._patrol: Patrol | None = None
        self._cases: CaseService | None = None
        self._task: asyncio.Task | None = None
        self._manual_task: asyncio.Task | None = None
        self._loop_wakeup = asyncio.Event()
        self._terminating = False
        self._last_report: PatrolReport | None = None
        self._secrets: set[str] = set()
        """Every credential seen since process start, including replaced ones.

        Redaction must cover a credential that is no longer in the settings: an
        error raised while it was current can still be logged later, and an
        aiotieba ValueError echoes the offending value in full.
        """
        self._web = WebRoutes(self._web_services)
        self._settings_web = SettingsRoutes(self._settings_web_services)
        self._register_web_apis()

    def _snapshot(self) -> SettingsSnapshot:
        """Read the current settings, including before storage is initialized."""
        if self._settings_service is not None:
            return self._settings_service.snapshot()
        return SettingsSnapshot(global_=default_global_settings())

    @property
    def _forum_config(self) -> ForumConfig:
        """Build the permission and notification view from current settings."""
        return ForumConfig.from_snapshot(self._snapshot())

    @property
    def _enabled(self) -> bool:
        return self._snapshot().global_.patrol_enabled

    @property
    def _interval(self) -> int:
        return self._snapshot().global_.interval_seconds

    def _can_patrol(self) -> bool:
        """Whether at least one enabled forum has a usable account."""
        return self._pool is not None and any(
            forum.enabled and self._pool.get(forum.account_id) is not None
            for forum in self._snapshot().forums
            if forum.account_id is not None
        )

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
        for path, handler, methods, desc in route_table(self._web, self._settings_web):
            register(f"/{PLUGIN_NAME}/{path}", handler, methods, desc)

    @property
    def _manual_running(self) -> bool:
        """Whether a manual patrol round is in flight.

        Derived from the task so there is a single source of truth.
        """
        return self._manual_task is not None and not self._manual_task.done()

    def _settings_web_services(self) -> SettingsWebServices:
        """Read the current dependencies for a settings Web API call.

        Returns:
            A snapshot of the live services, including a provider lister bound
            to the current AstrBot context.
        """
        return SettingsWebServices(
            settings=self._settings_service,
            pools=self._pool,
            list_providers=lambda: provider_rows(self.context),
            redact=lambda exc: self._redact(exc, brief=True),
            redact_trace=self._redact,
        )

    def _web_services(self) -> WebServices:
        """Read the current dependencies for a Web API call.

        Returns:
            A snapshot of the live services. Handlers call this per request, so
            a reload that replaces the store or the gateway is observed.
        """
        return WebServices(
            store=self._store,
            patrol=self._patrol,
            pool=self._pool,
            cases=self._cases,
            judge=self._judge,
            settings=self._snapshot(),
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
        """Load plugin settings and construct services even when unconfigured."""
        self._store = Store(StarTools.get_data_dir(PLUGIN_NAME) / DB_FILE)
        snapshot = self._store.load_settings()
        self._learn_credentials(snapshot)
        self._settings_service = SettingsService(
            self._store, snapshot=snapshot, on_change=self._on_settings_change
        )
        self._pool = GatewayPool(snapshot)
        self._judge = LlmJudge(self.context, settings=self._settings_service)
        self._patrol = Patrol(
            pool=self._pool,
            store=self._store,
            judge=self._judge,
            notify=self._notify,
            settings=self._settings_service,
            scrub=self._scrub,
        )
        self._cases = CaseService(
            pool=self._pool, settings=self._settings_service, store=self._store
        )
        if not self._can_patrol():
            logger.info("No patrol-ready forums; waiting for plugin settings.")
        elif self._enabled:
            self._task = asyncio.create_task(self._patrol_loop(STARTUP_DELAY_SECONDS))

    async def _on_settings_change(
        self, old: SettingsSnapshot, new: SettingsSnapshot
    ) -> None:
        """Refresh clients, then restart scheduling without cancelling a round.

        The callback never waits for a round: it may be holding the settings
        write lock. The loop owns its successor and starts a fresh interval
        after the current round releases its pinned gateways.
        """
        # Harvest before any client is rebuilt, so a construction error is
        # already redactable when it is logged.
        self._learn_credentials(new)
        if self._pool is not None:
            await self._pool.update(new)
        schedule_changed = (
            old.global_.patrol_enabled != new.global_.patrol_enabled
            or old.global_.interval_seconds != new.global_.interval_seconds
            or not self._can_patrol()
        )
        if self._task is not None and not self._task.done():
            if schedule_changed:
                self._loop_wakeup.set()
        elif not self._terminating and self._enabled and self._can_patrol():
            self._loop_wakeup = asyncio.Event()
            self._task = asyncio.create_task(self._patrol_loop(0))

    async def terminate(self) -> None:
        """Stop patrol tasks, drain gateway users, then close the store."""
        self._terminating = True
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
        self._task = self._manual_task = None
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception as exc:
                logger.warning(f"Failed to close Tieba pool:\n{self._redact(exc)}")
            self._pool = None
        if self._store is not None:
            self._store.close()
            self._store = None

    async def _patrol_loop(self, delay: float = STARTUP_DELAY_SECONDS) -> None:
        """Wait interruptibly, but let an active round finish on settings edits."""
        try:
            while True:
                try:
                    await asyncio.wait_for(self._loop_wakeup.wait(), timeout=delay)
                    return
                except TimeoutError:
                    pass
                try:
                    report = await self._patrol.run_once()
                    self._finish_round(report)
                except Exception as exc:
                    logger.error(f"Patrol round failed:\n{self._redact(exc)}")
                if self._loop_wakeup.is_set():
                    return
                delay = self._interval
        finally:
            self._task = None
            if not self._terminating and self._enabled and self._can_patrol():
                self._loop_wakeup = asyncio.Event()
                self._task = asyncio.create_task(self._patrol_loop(self._interval))

    def _finish_round(self, report: PatrolReport) -> None:
        """Remember a finished round and persist its summary.

        Args:
            report: The report of the round that just finished.
        """
        report = dcs.replace(
            report,
            errors=tuple(self._scrub(error) for error in report.errors),
        )
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
        """Format an exception with every known credential removed.

        Args:
            exc: The exception.
            brief: Only the message (for replies); otherwise the full traceback
                (for logs).

        Returns:
            Text with any BDUSS / STOKEN value replaced by ``***``.
        """
        return self._scrub(
            str(exc) if brief else "".join(traceback.format_exception(exc))
        )

    def _scrub(self, text: str) -> str:
        """Replace every credential this process has loaded with ``***``.

        Longest first, so a credential that contains another as a prefix cannot
        leave a fragment behind.

        Args:
            text: Any text that may quote an exception or a request.

        Returns:
            The text with each known credential removed.
        """
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        return text

    def _learn_credentials(self, snapshot: SettingsSnapshot) -> None:
        """Remember credentials from a snapshot for later redaction.

        The set only grows: a credential removed from the settings may still be
        inside an exception object that is logged afterwards.

        Args:
            snapshot: Settings to harvest account credentials from.
        """
        for account in snapshot.accounts:
            for value in (account.bduss, account.stoken):
                if value:
                    self._secrets.add(value)

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
        snapshot = self._snapshot()
        if self._task is not None and not self._task.done():
            state = f"运行中，每 {self._interval} 秒一轮"
        elif not self._enabled:
            state = "定时巡查已关闭（可用 /tb run 手动巡查）"
        else:
            state = "未运行（没有可巡查的吧）"
        names = {account.id: account.name for account in snapshot.accounts}
        lines = [f"巡查：{state}"]
        for forum in snapshot.forums:
            resolved = resolve(snapshot, forum.fname)
            problem = (
                "已停用"
                if not forum.enabled
                else ("未选择账号" if forum.account_id is None else "可巡查")
            )
            options = resolved.options
            account_name = names.get(forum.account_id, "未选择")
            auto = "开启" if options.auto_delete_enabled else "关闭"
            labels = []
            if forum.overrides.rules:
                labels.append("独立吧规")
            if forum.sessions:
                labels.append("独立推送")
            lines.append(
                f"{one_line(forum.fname, FORUM_CHARS)} 吧：{problem} | "
                f"账号：{one_line(account_name, 32)} | 自动删除：{auto}"
                f"（阈值 {options.auto_delete_threshold:.2f}） | "
                f"上报阈值 {options.report_threshold:.2f}"
                + (f" | {'、'.join(labels)}" if labels else "")
            )
        if not snapshot.forums:
            lines.append("吧：未配置，请在插件页设置。")
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
        if self._pool is None or self._pool.any() is None:
            yield event.plain_result(NO_ACCOUNT_REPLY)
            return
        if not self._can_patrol():
            yield event.plain_result("没有配置可巡查的吧。")
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
        cid = parse_case_id(case_id)
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
        cid = parse_case_id(case_id)
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
        thread_id = parse_case_id(tid)
        if thread_id is None:
            yield event.plain_result("用法：/tb check <帖子 tid>，tid 是正整数。")
            return
        if self._pool is None or self._pool.any() is None:
            yield event.plain_result(NO_ACCOUNT_REPLY)
            return
        try:
            async with self._pool.lease() as gateway:
                if gateway is None:
                    yield event.plain_result(NO_ACCOUNT_REPLY)
                    return
                item = await gateway.get_thread_item_by_tid(thread_id)
                settings = self._snapshot().global_
                urls: list[str] = []
                if settings.review_images and item.image_urls:
                    urls = await gateway.fetch_images(
                        item.image_urls, settings.max_images
                    )
                verdict = await self._judge(item, urls)
        except (JudgeError, ImageFetchError, TiebaActionError) as exc:
            yield event.plain_result(f"审查失败：{self._redact(exc, brief=True)}")
            return
        except Exception as exc:
            logger.error(f"Check of thread {thread_id} failed:\n{self._redact(exc)}")
            yield event.plain_result("审查失败，详见 AstrBot 日志。")
            return
        s = resolve(self._snapshot(), item.fname).options
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
        if self._settings_service is None:
            yield event.plain_result("插件存储尚未就绪，请稍后重试。")
            return
        try:
            result = await self._settings_service.bind_session(
                fname.strip() or None, event.unified_msg_origin
            )
        except SettingsError as exc:
            yield event.plain_result(str(exc))
            return
        if isinstance(result, str):
            yield event.plain_result(result)
            return
        yield event.plain_result(
            f"已把当前会话加入{result.label}推送列表，共 {result.total} 个。"
        )
