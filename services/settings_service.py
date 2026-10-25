"""Persist settings mutations serially and publish immutable runtime snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace

from ..core.forums import BindResult
from ..core.settings import (
    Account,
    AccountDraft,
    ForumDraft,
    ForumEntry,
    GlobalSettings,
    ResolvedForum,
    SettingsSnapshot,
    resolve,
)
from ..core.store import Store
from ..core.validation_fields import is_safe_session


class SettingsError(Exception):
    """A Chinese settings failure that may be shown directly to the user."""


class SettingsNotFound(SettingsError):
    """A requested forum or account no longer exists."""


class SettingsConflict(SettingsError):
    """A mutation conflicts with names, references, or collection limits."""


class SettingsService:
    """Own the single write lock and current immutable settings snapshot.

    The store is synchronous and belongs to the same thread as this service.
    Callbacks run after the commit and snapshot swap, while the write lock is
    still held. They must not call mutation methods on this service recursively.
    A callback failure does not undo an already committed settings mutation.
    """

    def __init__(
        self,
        store: Store,
        snapshot: SettingsSnapshot | None = None,
        on_change: Callable[[SettingsSnapshot, SettingsSnapshot], Awaitable[None]]
        | None = None,
    ) -> None:
        """Load persisted settings unless an initial snapshot was supplied."""
        self._store = store
        self._snapshot = snapshot if snapshot is not None else store.load_settings()
        self._on_change = on_change
        self._lock = asyncio.Lock()

    def snapshot(self) -> SettingsSnapshot:
        """Return the current immutable snapshot without waiting for a writer."""
        return self._snapshot

    def resolve(self, fname: str) -> ResolvedForum:
        """Resolve a configured or unknown forum using current settings."""
        return resolve(self._snapshot, fname)

    async def _publish(self, snapshot: SettingsSnapshot) -> SettingsSnapshot:
        """Swap a committed snapshot and await its callback under the write lock."""
        old = self._snapshot
        new = replace(snapshot, revision=old.revision + 1)
        self._snapshot = new
        if self._on_change is not None:
            await self._on_change(old, new)
        return new

    def _account(self, account_id: int) -> Account:
        """Find an account or raise a user-facing missing-record error."""
        for account in self._snapshot.accounts:
            if account.id == account_id:
                return account
        raise SettingsNotFound("账号不存在")

    def _forum(self, forum_id: int) -> ForumEntry:
        """Find a forum or raise a user-facing missing-record error."""
        for forum in self._snapshot.forums:
            if forum.id == forum_id:
                return forum
        raise SettingsNotFound("吧不存在")

    def _check_account_name(self, name: str, account_id: int | None = None) -> None:
        """Reject Unicode-casefolded name collisions, excluding the edited row."""
        if any(
            account.id != account_id and account.name.casefold() == name.casefold()
            for account in self._snapshot.accounts
        ):
            raise SettingsConflict("账号名称已存在")

    def _check_forum(self, draft: ForumDraft, forum_id: int | None = None) -> None:
        """Check forum uniqueness and account references under the write lock."""
        key = draft.fname.strip().casefold()
        if any(
            forum.id != forum_id and forum.fname.casefold() == key
            for forum in self._snapshot.forums
        ):
            raise SettingsConflict("吧名已存在")
        if draft.account_id is not None:
            self._account(draft.account_id)

    async def save_global(self, value: GlobalSettings) -> SettingsSnapshot:
        """Save validated global settings and publish their new revision."""
        async with self._lock:
            self._store.save_global(value)
            return await self._publish(replace(self._snapshot, global_=value))

    async def create_account(self, draft: AccountDraft) -> Account:
        """Create an account, enforcing the twenty-account limit and unique name."""
        async with self._lock:
            if len(self._snapshot.accounts) >= 20:
                raise SettingsConflict("最多可添加 20 个账号")
            self._check_account_name(draft.name)
            if not draft.bduss or not draft.stoken:
                raise SettingsError("新增账号必须填写 BDUSS 和 STOKEN")
            account = self._store.insert_account(draft)
            await self._publish(
                replace(self._snapshot, accounts=(*self._snapshot.accounts, account))
            )
            return account

    async def update_account(self, account_id: int, draft: AccountDraft) -> Account:
        """Replace an account's name and only its nonempty supplied credentials."""
        async with self._lock:
            self._account(account_id)
            self._check_account_name(draft.name, account_id)
            account = self._store.update_account(account_id, draft)
            if account is None:
                raise SettingsNotFound("账号不存在")
            await self._publish(
                replace(
                    self._snapshot,
                    accounts=tuple(
                        account if entry.id == account_id else entry
                        for entry in self._snapshot.accounts
                    ),
                )
            )
            return account

    async def delete_account(self, account_id: int) -> None:
        """Delete an account only when no configured forum still references it."""
        async with self._lock:
            self._account(account_id)
            references = sum(
                forum.account_id == account_id for forum in self._snapshot.forums
            )
            if references:
                raise SettingsConflict(f"仍有 {references} 个吧在使用该账号")
            if not self._store.delete_account(account_id):
                raise SettingsNotFound("账号不存在")
            await self._publish(
                replace(
                    self._snapshot,
                    accounts=tuple(
                        entry
                        for entry in self._snapshot.accounts
                        if entry.id != account_id
                    ),
                )
            )

    async def create_forum(self, draft: ForumDraft) -> ForumEntry:
        """Create a forum, enforcing the fifty-forum limit and account integrity."""
        async with self._lock:
            if len(self._snapshot.forums) >= 50:
                raise SettingsConflict("最多可添加 50 个吧")
            self._check_forum(draft)
            forum = self._store.insert_forum(draft)
            await self._publish(
                replace(self._snapshot, forums=(*self._snapshot.forums, forum))
            )
            return forum

    async def update_forum(self, forum_id: int, draft: ForumDraft) -> ForumEntry:
        """Replace a validated forum without changing its ID or creation time."""
        async with self._lock:
            self._forum(forum_id)
            self._check_forum(draft, forum_id)
            forum = self._store.update_forum(forum_id, draft)
            if forum is None:
                raise SettingsNotFound("吧不存在")
            await self._publish(
                replace(
                    self._snapshot,
                    forums=tuple(
                        forum if entry.id == forum_id else entry
                        for entry in self._snapshot.forums
                    ),
                )
            )
            return forum

    async def delete_forum(self, forum_id: int) -> None:
        """Remove a forum while leaving moderation history untouched."""
        async with self._lock:
            self._forum(forum_id)
            if not self._store.delete_forum(forum_id):
                raise SettingsNotFound("吧不存在")
            await self._publish(
                replace(
                    self._snapshot,
                    forums=tuple(
                        entry for entry in self._snapshot.forums if entry.id != forum_id
                    ),
                )
            )

    async def bind_session(self, fname: str | None, session: str) -> BindResult | str:
        """Append a chat session to global defaults or a currently patrolled forum.

        Args:
            fname: Existing forum name, or None/empty for the global list.
            session: Unified message origin, stored only after length/control checks.

        Returns:
            A binding result on success, otherwise a safe Chinese refusal. Local
            bindings start a local list rather than copying inherited sessions.
        """
        async with self._lock:
            name = (fname or "").strip()
            if len(name) > 64:
                return "吧名过长，不能绑定推送会话。"
            if not session.strip() or not is_safe_session(session):
                # Same character rules as the Web API: a chat-supplied session
                # string must not smuggle zero-width or bidi characters into
                # the store and back into a push.
                return "推送会话必须是 1 到 512 字符且不含控制字符。"
            forum = None
            label = ""
            if name:
                forum = next(
                    (
                        entry
                        for entry in self._snapshot.forums
                        if entry.fname.casefold() == name.casefold()
                    ),
                    None,
                )
                if forum is None or not forum.enabled or forum.account_id is None:
                    return f"未巡查 {' '.join(name.split())} 吧，不能绑定推送会话。"
                sessions = forum.sessions
                label = f"{forum.fname} 吧的"
            else:
                sessions = self._snapshot.global_.sessions
            if session in sessions:
                return f"当前会话已经在{label}推送列表里。"
            if len(sessions) >= 20:
                return f"{label}推送会话最多为 20 条。"
            updated = (*sessions, session)
            if forum is None:
                value = replace(self._snapshot.global_, sessions=updated)
                self._store.save_global(value)
                new = replace(self._snapshot, global_=value)
            else:
                draft = ForumDraft(
                    fname=forum.fname,
                    enabled=forum.enabled,
                    account_id=forum.account_id,
                    sessions=updated,
                    operators=forum.operators,
                    whitelist=forum.whitelist,
                    overrides=forum.overrides,
                )
                updated_forum = self._store.update_forum(forum.id, draft)
                if updated_forum is None:
                    raise SettingsNotFound("吧不存在")
                new = replace(
                    self._snapshot,
                    forums=tuple(
                        updated_forum if entry.id == forum.id else entry
                        for entry in self._snapshot.forums
                    ),
                )
            await self._publish(new)
            return BindResult(label=label, total=len(updated))
