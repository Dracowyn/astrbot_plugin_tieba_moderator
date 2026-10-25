"""Web API for v0.3 settings: global options, accounts and forums.

Handlers are plain methods; the plugin registers them through
``context.register_web_api`` once the settings service exists. Every request is
authenticated by the Dashboard before dispatch (same layer as ``web/routes.py``,
see ``astrbot/dashboard/api/plugins.py:398-438`` and
``astrbot/dashboard/api/auth.py:167-195``); this module still validates all input
and never trusts the caller.

Credential handling: ``AccountDTO`` has no credential field at all, and abort
messages are built by ``core.validation``, which never echoes a credential. A
test serializes every response and asserts no test credential appears in it.

Error mapping: ``SettingsNotFound`` 404, ``SettingsConflict`` 409,
``ValidationError`` 400, ``TiebaActionError`` 502 (redacted), anything else 500
with a fixed message while the redacted traceback goes to the log.
"""

from __future__ import annotations

import asyncio
import dataclasses as dcs
from collections.abc import Callable, Iterable
from typing import Any

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

from ..core.models import TiebaActionError
from ..core.settings import (
    Account,
    ForumEntry,
    ForumOptions,
    ForumOverrides,
    GlobalSettings,
    ResolvedForum,
    SettingsSnapshot,
)
from ..core.validation import (
    ValidationError,
    parse_account_draft,
    parse_forum_draft,
    parse_global,
)
from ..services.settings_service import (
    SettingsConflict,
    SettingsError,
    SettingsNotFound,
)

MAX_ID_DIGITS = 18
INTERNAL_ERROR = "服务器内部错误，详见 AstrBot 日志。"
BUSY_CHECK_REPLY = "已有一次账号检测在进行，请稍后。"


@dcs.dataclass(frozen=True, kw_only=True)
class SettingsWebServices:
    """Dependencies for the settings handlers, read once per request.

    Attributes:
        settings: The settings service (``snapshot`` and the write methods);
            None before the plugin has initialised it.
        pools: The gateway pool, used by ``POST accounts/<id>/check``.
        list_providers: Returns ``(id, label, supports_image)`` triples for the
            chat providers AstrBot currently has configured. ``supports_image``
            is None when the modality information is unavailable.
        redact: Formats an exception message with credentials removed.
        redact_trace: Formats an exception traceback with credentials removed.
    """

    settings: Any
    pools: Any
    list_providers: Callable[[], Iterable[tuple[str, str, bool | None]]]
    redact: Callable[[BaseException], str]
    redact_trace: Callable[[BaseException], str]

    def provider_ids(self) -> frozenset[str]:
        """Ids of the providers a forum may select."""
        try:
            return frozenset(provider_id for provider_id, _, _ in self.list_providers())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                f"Listing chat providers failed: {type(exc).__name__}: {exc}"
            )
            return frozenset()

    def account_ids(self) -> frozenset[int]:
        """Ids of the accounts that currently exist."""
        return frozenset(account.id for account in self.settings.snapshot().accounts)


def _provider_id(raw: str) -> int | None:
    """Parse a decimal path parameter.

    Args:
        raw: Raw path segment.

    Returns:
        The positive integer, or None when the segment is not one.
    """
    text = (raw or "").strip()
    if not text.isascii() or not text.isdecimal() or len(text) > MAX_ID_DIGITS:
        return None
    value = int(text)
    return value or None


def _options_dto(options: ForumOptions) -> dict[str, Any]:
    """Serialize resolved or default forum options.

    Args:
        options: The options.

    Returns:
        An ``OptionsDTO``.
    """
    return {
        "rules": options.rules,
        "provider_id": options.provider_id,
        "auto_delete_enabled": options.auto_delete_enabled,
        "auto_delete_threshold": options.auto_delete_threshold,
        "report_threshold": options.report_threshold,
        "thread_pages": options.thread_pages,
        "lookback_hours": options.lookback_hours,
        "review_posts": options.review_posts,
        "review_comments": options.review_comments,
        "skip_top_and_good": options.skip_top_and_good,
        "skip_bawu": options.skip_bawu,
        "whitelist": sorted(options.whitelist),
    }


def _overrides_dto(overrides: ForumOverrides) -> dict[str, Any | None]:
    """Serialize overrides, keeping ``None`` for inherited values.

    Args:
        overrides: The overrides.

    Returns:
        A mapping with every override field present.
    """
    return {
        "rules": overrides.rules,
        "provider_id": overrides.provider_id,
        "auto_delete_enabled": overrides.auto_delete_enabled,
        "auto_delete_threshold": overrides.auto_delete_threshold,
        "report_threshold": overrides.report_threshold,
        "thread_pages": overrides.thread_pages,
        "lookback_hours": overrides.lookback_hours,
        "review_posts": overrides.review_posts,
        "review_comments": overrides.review_comments,
        "skip_top_and_good": overrides.skip_top_and_good,
        "skip_bawu": overrides.skip_bawu,
    }


def _global_dto(settings: GlobalSettings) -> dict[str, Any]:
    """Serialize global settings.

    Args:
        settings: The global settings.

    Returns:
        A ``GlobalDTO``.
    """
    return {
        "patrol_enabled": settings.patrol_enabled,
        "interval_seconds": settings.interval_seconds,
        "max_items_per_round": settings.max_items_per_round,
        "judge_concurrency": settings.judge_concurrency,
        "judge_timeout_seconds": settings.judge_timeout_seconds,
        "review_images": settings.review_images,
        "max_images": settings.max_images,
        "sessions": list(settings.sessions),
        "operators": sorted(settings.operators),
        "defaults": _options_dto(settings.defaults),
    }


def _forum_dto(entry: ForumEntry, resolved: ResolvedForum) -> dict[str, Any]:
    """Serialize one forum with its inherited effective options.

    Args:
        entry: The stored forum.
        resolved: The forum with inheritance applied.

    Returns:
        A ``ForumDTO``. ``problem`` explains why the forum is not patrolled.
    """
    if not entry.enabled:
        problem = "已停用"
    elif entry.account_id is None:
        problem = "未选择账号"
    else:
        problem = ""
    return {
        "id": entry.id,
        "fname": entry.fname,
        "enabled": entry.enabled,
        "account_id": entry.account_id,
        "sessions": list(entry.sessions),
        "operators": sorted(entry.operators),
        "whitelist": sorted(entry.whitelist),
        "overrides": _overrides_dto(entry.overrides),
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "effective": _options_dto(resolved.options),
        "patrolled": resolved.enabled and resolved.account_id is not None,
        "problem": problem,
    }


def _account_dto(account: Account, used_by: list[str]) -> dict[str, Any]:
    """Serialize an account without any credential material.

    Args:
        account: The stored account.
        used_by: Forum names that reference it.

    Returns:
        An ``AccountDTO``; there is deliberately no ``bduss`` or ``stoken`` key.
    """
    return {
        "id": account.id,
        "name": account.name,
        "has_credentials": bool(account.bduss and account.stoken),
        "forums": used_by,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _settings_dto(
    snapshot: SettingsSnapshot, resolve: Callable[[str], ResolvedForum]
) -> dict[str, Any]:
    """Serialize a whole snapshot.

    Args:
        snapshot: The settings snapshot.
        resolve: The settings service's ``resolve``; it is the single owner of
            inheritance, so this module never reimplements it.

    Returns:
        A ``SettingsDTO``.
    """
    usage: dict[int, list[str]] = {}
    for entry in snapshot.forums:
        if entry.account_id is not None:
            usage.setdefault(entry.account_id, []).append(entry.fname)
    return {
        "revision": snapshot.revision,
        "global": _global_dto(snapshot.global_),
        "forums": [
            _forum_dto(entry, resolve(entry.fname)) for entry in snapshot.forums
        ],
        "accounts": [
            _account_dto(account, sorted(usage.get(account.id, ())))
            for account in snapshot.accounts
        ],
    }


class SettingsRoutes:
    """Settings handlers, one instance per plugin."""

    def __init__(self, services: Callable[[], SettingsWebServices]) -> None:
        """Create the handlers.

        Args:
            services: Reads the current dependencies; called once per request so
                a reload never leaves a handler holding stale objects.
        """
        self._services = services
        self._check_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_settings(self) -> Any:
        """Return every setting in one payload."""
        services = self._services()
        if services.settings is None:
            return error_response("设置服务尚未就绪，请稍后重试。", status_code=503)
        return json_response(
            _settings_dto(services.settings.snapshot(), services.settings.resolve)
        )

    async def get_providers(self) -> Any:
        """List chat providers a forum may select."""
        services = self._services()
        providers = [
            {"id": provider_id, "label": label, "supports_image": supports_image}
            for provider_id, label, supports_image in services.list_providers()
        ]
        return json_response(providers)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def post_global(self) -> Any:
        """Replace the global settings and the per-forum defaults."""
        services = self._services()
        if services.settings is None:
            return error_response("设置服务尚未就绪，请稍后重试。", status_code=503)
        payload = await request.json(default=None)
        try:
            value = parse_global(payload, provider_ids=services.provider_ids())
        except ValidationError as exc:
            return error_response(str(exc))
        return await self._run(services, lambda: services.settings.save_global(value))

    async def post_forum_create(self) -> Any:
        """Create a forum from a complete draft."""
        return await self._write_forum(forum_id=None)

    async def post_forum_update(self, forum_id: str) -> Any:
        """Replace a forum from a complete draft.

        Args:
            forum_id: Forum id from the path.
        """
        return await self._write_forum(forum_id=forum_id)

    async def _write_forum(self, forum_id: str | None) -> Any:
        """Shared body of forum create and update.

        Args:
            forum_id: Raw id for update, None for create.

        Returns:
            The new ``SettingsDTO`` or an error response.
        """
        services = self._services()
        if services.settings is None:
            return error_response("设置服务尚未就绪，请稍后重试。", status_code=503)
        target = None
        if forum_id is not None:
            target = _provider_id(forum_id)
            if target is None:
                return error_response("吧编号必须是正整数")
        payload = await request.json(default=None)
        try:
            draft = parse_forum_draft(
                payload,
                provider_ids=services.provider_ids(),
                account_ids=services.account_ids(),
            )
        except ValidationError as exc:
            return error_response(str(exc))
        if target is None:
            return await self._run(
                services, lambda: services.settings.create_forum(draft)
            )
        return await self._run(
            services, lambda: services.settings.update_forum(target, draft)
        )

    async def post_forum_delete(self, forum_id: str) -> Any:
        """Delete a forum.

        Args:
            forum_id: Forum id from the path.
        """
        services = self._services()
        if services.settings is None:
            return error_response("设置服务尚未就绪，请稍后重试。", status_code=503)
        target = _provider_id(forum_id)
        if target is None:
            return error_response("吧编号必须是正整数")
        return await self._run(services, lambda: services.settings.delete_forum(target))

    async def post_account_create(self) -> Any:
        """Create a moderator account."""
        return await self._write_account(account_id=None)

    async def post_account_update(self, account_id: str) -> Any:
        """Update an account; absent credentials keep the stored ones.

        Args:
            account_id: Account id from the path.
        """
        return await self._write_account(account_id=account_id)

    async def _write_account(self, account_id: str | None) -> Any:
        """Shared body of account create and update.

        Args:
            account_id: Raw id for update, None for create.

        Returns:
            The new ``SettingsDTO`` or an error response.
        """
        services = self._services()
        if services.settings is None:
            return error_response("设置服务尚未就绪，请稍后重试。", status_code=503)
        target = None
        if account_id is not None:
            target = _provider_id(account_id)
            if target is None:
                return error_response("账号编号必须是正整数")
        payload = await request.json(default=None)
        try:
            draft = parse_account_draft(payload, creating=target is None)
        except ValidationError as exc:
            return error_response(str(exc))
        if target is None:
            return await self._run(
                services, lambda: services.settings.create_account(draft)
            )
        return await self._run(
            services, lambda: services.settings.update_account(target, draft)
        )

    async def post_account_delete(self, account_id: str) -> Any:
        """Delete an account that no forum references.

        Args:
            account_id: Account id from the path.
        """
        services = self._services()
        if services.settings is None:
            return error_response("设置服务尚未就绪，请稍后重试。", status_code=503)
        target = _provider_id(account_id)
        if target is None:
            return error_response("账号编号必须是正整数")
        return await self._run(
            services, lambda: services.settings.delete_account(target)
        )

    # ------------------------------------------------------------------
    # Account health check
    # ------------------------------------------------------------------

    async def post_account_check(self, account_id: str) -> Any:
        """Check one account's credentials and its moderator status per forum.

        Only one check runs at a time: each one costs a Tieba round trip per
        forum and holds a gateway.

        Args:
            account_id: Account id from the path.

        Returns:
            ``{"ok", "user_name", "nick_name", "message", "forums"}``.
        """
        services = self._services()
        if services.settings is None:
            return error_response("设置服务尚未就绪，请稍后重试。", status_code=503)
        target = _provider_id(account_id)
        if target is None:
            return error_response("账号编号必须是正整数")
        if self._check_lock.locked():
            return error_response(BUSY_CHECK_REPLY, status_code=409)

        async with self._check_lock:
            snapshot = services.settings.snapshot()
            forums = [
                entry.fname for entry in snapshot.forums if entry.account_id == target
            ]
            # A lease pins the gateway for every await below: ``get`` alone does
            # not extend its lifetime, so a settings edit could retire it mid
            # request and the next call would hit a closed client.
            async with services.pools.lease(target) as gateway:
                if gateway is None:
                    return error_response(
                        "该账号没有可用的网关，请检查凭据。", status_code=503
                    )
                try:
                    me = await gateway.get_self_info()
                except TiebaActionError as exc:
                    return error_response(
                        services.redact(exc) or "账号检测失败", status_code=502
                    )
                except Exception as exc:
                    logger.error(
                        f"Account check for {target} failed:\n"
                        f"{services.redact_trace(exc)}"
                    )
                    return error_response(INTERNAL_ERROR, status_code=500)

                results = []
                for fname in forums:
                    # One forum's failure must not hide the others.
                    try:
                        is_bawu = await gateway.is_bawu(fname, me)
                    except Exception as exc:
                        logger.warning(
                            f"Moderator check for {target} in {fname} failed: "
                            f"{services.redact(exc)}"
                        )
                        is_bawu = None
                    results.append({"fname": fname, "is_bawu": is_bawu})

        not_bawu = [row["fname"] for row in results if row["is_bawu"] is False]
        unknown = [row["fname"] for row in results if row["is_bawu"] is None]
        if not_bawu:
            message = "该账号不是以下吧的吧务：" + "、".join(not_bawu)
        elif unknown:
            message = "凭据有效，部分吧的吧务状态查询失败：" + "、".join(unknown)
        else:
            message = "凭据有效。"
        return json_response(
            {
                # True only when nothing is known to be wrong: a forum that
                # could not be queried is not a clean bill of health.
                "ok": not not_bawu and not unknown,
                "user_name": me.user_name,
                "nick_name": me.nick_name,
                "message": message,
                "forums": results,
            }
        )

    # ------------------------------------------------------------------
    # Shared error mapping
    # ------------------------------------------------------------------

    async def _run(
        self, services: SettingsWebServices, action: Callable[[], Any]
    ) -> Any:
        """Run a write and map failures onto the documented status codes.

        Args:
            services: The dependency bundle, for redaction.
            action: Coroutine factory performing the write.

        Returns:
            The refreshed ``SettingsDTO`` or an error response.
        """
        try:
            await action()
        except ValidationError as exc:
            # The settings service re-checks domain rules (for example a forum
            # name that collides after casefolding); those are 400 as well.
            return error_response(str(exc))
        except SettingsNotFound as exc:
            return error_response(str(exc), status_code=404)
        except SettingsConflict as exc:
            return error_response(str(exc), status_code=409)
        except TiebaActionError as exc:
            return error_response(
                services.redact(exc) or "贴吧操作失败", status_code=502
            )
        except Exception as exc:
            logger.error(f"Settings write failed:\n{services.redact_trace(exc)}")
            return error_response(INTERNAL_ERROR, status_code=500)
        return json_response(
            _settings_dto(services.settings.snapshot(), services.settings.resolve)
        )


def provider_rows(context: Any) -> list[tuple[str, str, bool | None]]:
    """Describe AstrBot's chat providers for ``GET providers``.

    ``supports_image`` comes from ``provider_config["modalities"]``
    (astrbot/core/astr_main_agent.py:1372-1376). An empty list is treated as
    "not configured" there and means every modality is allowed, so it maps to
    True; a missing or malformed value maps to None.

    Args:
        context: The AstrBot star context.

    Returns:
        ``(id, label, supports_image)`` triples in AstrBot's order.
    """
    rows: list[tuple[str, str, bool | None]] = []
    try:
        providers = context.get_all_providers() or []
    except Exception as exc:
        logger.warning(f"Listing chat providers failed: {type(exc).__name__}: {exc}")
        return rows
    for provider in providers:
        try:
            meta = provider.meta()
        except Exception as exc:
            logger.warning(f"Skipping a provider without meta(): {type(exc).__name__}")
            continue
        provider_id = str(getattr(meta, "id", "") or "").strip()
        if not provider_id:
            continue
        model = str(getattr(meta, "model", "") or "").strip()
        label = f"{provider_id}（{model}）" if model else provider_id
        config = getattr(provider, "provider_config", None)
        modalities = config.get("modalities") if isinstance(config, dict) else None
        if isinstance(modalities, list):
            supports_image: bool | None = not modalities or "image" in modalities
        else:
            supports_image = None
        rows.append((provider_id, label, supports_image))
    return rows


__all__ = [
    "INTERNAL_ERROR",
    "MAX_ID_DIGITS",
    "SettingsConflict",
    "SettingsError",
    "SettingsNotFound",
    "SettingsRoutes",
    "SettingsWebServices",
    "provider_rows",
]
