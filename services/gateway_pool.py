"""Account gateways with operation-scoped leases and deferred retirement."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager

from ..core.settings import Account, SettingsSnapshot
from .tieba_gateway import TiebaGateway


class _Entry:
    """Track the users and one retirement task of a single gateway instance."""

    def __init__(self, account: Account, gateway: TiebaGateway) -> None:
        self.account = account
        self.gateway = gateway
        self.users = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.closing: asyncio.Task[None] | None = None


async def _finish_closing(tasks: Iterable[asyncio.Task[None]]) -> None:
    """Finish every close even when the waiter is repeatedly cancelled."""
    tasks = tuple(tasks)
    if not tasks:
        return
    completion = asyncio.gather(*tasks, return_exceptions=True)
    cancelled = False
    while not completion.done():
        try:
            await asyncio.shield(completion)
        except asyncio.CancelledError:
            cancelled = True
    results = completion.result()
    if cancelled:
        raise asyncio.CancelledError
    for result in results:
        if isinstance(result, BaseException):
            raise result


class GatewayPool:
    """Own current gateways and retire old credentials after their last lease.

    All bookkeeping runs synchronously on the owning event loop before the
    first suspension. Callers must acquire a lease, rather than use ``get`` or
    ``any`` across an await, whenever an operation needs a gateway to stay open.

    Args:
        snapshot: Initial immutable account configuration.
        factory: Constructor accepting BDUSS and STOKEN; injectable offline.
    """

    def __init__(
        self,
        snapshot: SettingsSnapshot,
        *,
        factory: Callable[[str, str], TiebaGateway] = TiebaGateway,
    ) -> None:
        self._factory = factory
        self._current = {
            account.id: _Entry(account, factory(account.bduss, account.stoken))
            for account in snapshot.accounts
        }
        self._retired: set[_Entry] = set()
        self._closed = False

    def get(self, account_id: int) -> TiebaGateway | None:
        """Return the current gateway without extending its lifetime."""
        entry = self._current.get(account_id)
        return entry.gateway if entry is not None else None

    def any(self) -> TiebaGateway | None:
        """Return any current gateway without extending its lifetime."""
        entry = next(iter(self._current.values()), None)
        return entry.gateway if entry is not None else None

    async def update(self, snapshot: SettingsSnapshot) -> None:
        """Replace changed credentials, closing only retirees without users.

        Name-only changes keep their gateway. Active retirees are not awaited;
        their final lease or shutdown will finish closing them.

        Args:
            snapshot: The latest persisted account configuration.

        Raises:
            RuntimeError: The pool has already begun shutdown.
        """
        if self._closed:
            raise RuntimeError("Gateway pool is closed")
        current: dict[int, _Entry] = {}
        created: list[_Entry] = []
        try:
            for account in snapshot.accounts:
                entry = self._current.get(account.id)
                if entry is None or (
                    entry.account.bduss != account.bduss
                    or entry.account.stoken != account.stoken
                ):
                    entry = _Entry(
                        account, self._factory(account.bduss, account.stoken)
                    )
                    created.append(entry)
                current[account.id] = entry
        except Exception:
            await _finish_closing(self._retire(entry) for entry in created)
            raise
        previous, self._current = self._current, current
        idle = []
        for account_id, entry in previous.items():
            if current.get(account_id) is not entry:
                closing = self._retire(entry)
                if not entry.users:
                    idle.append(closing)
        await _finish_closing(idle)

    @asynccontextmanager
    async def lease(
        self, account_id: int | None = None
    ) -> AsyncIterator[TiebaGateway | None]:
        """Pin a current account gateway, or any account when no id is given."""
        entry = (
            next(iter(self._current.values()), None)
            if account_id is None
            else self._current.get(account_id)
        )
        if entry is None:
            yield None
            return
        self._pin(entry)
        try:
            yield entry.gateway
        finally:
            await self._release((entry,))

    @asynccontextmanager
    async def lease_many(
        self, account_ids: Iterable[int]
    ) -> AsyncIterator[dict[int, TiebaGateway]]:
        """Pin all present accounts atomically, deduplicating repeated ids."""
        entries = {
            account_id: self._current[account_id]
            for account_id in account_ids
            if account_id in self._current
        }
        for entry in entries.values():
            self._pin(entry)
        try:
            yield {account_id: entry.gateway for account_id, entry in entries.items()}
        finally:
            await self._release(entries.values())

    async def close(self) -> None:
        """Reject new leases and finish every current or retired gateway once.

        Active leases may finish normally. Cancellation is propagated only
        after their gateways finish closing, so shutdown cannot leak sessions.
        Concurrent and repeated shutdown calls share the same closing tasks.
        """
        self._closed = True
        for entry in self._current.values():
            self._retire(entry)
        self._current.clear()
        await _finish_closing(
            entry.closing for entry in self._retired if entry.closing is not None
        )

    @staticmethod
    def _pin(entry: _Entry) -> None:
        entry.users += 1
        entry.idle.clear()

    async def _release(self, entries: Iterable[_Entry]) -> None:
        closing = []
        for entry in entries:
            entry.users -= 1
            if not entry.users:
                entry.idle.set()
                if entry.closing is not None:
                    closing.append(entry.closing)
        await _finish_closing(closing)

    def _retire(self, entry: _Entry) -> asyncio.Task[None]:
        if entry.closing is None:
            self._retired.add(entry)
            entry.closing = asyncio.create_task(self._close_entry(entry))
        return entry.closing

    async def _close_entry(self, entry: _Entry) -> None:
        try:
            await entry.idle.wait()
            await entry.gateway.close()
        finally:
            self._retired.discard(entry)
