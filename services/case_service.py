"""Apply moderator decisions before committing conditional case transitions."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..core.models import ALLOWED_BAN_DAYS, Case, CaseStatus, TiebaActionError

if TYPE_CHECKING:
    from ..core.store import Store
    from .tieba_gateway import TiebaGateway


class CaseError(Exception):
    """A case is missing, its status disallows an action, or an update lost a race."""


class CaseService:
    """Run explicit moderator actions using API-first, compare-and-swap updates.

    Actions on one service instance are serialized from validation through the
    conditional update. External writers can still win that update after a remote
    action, which cannot be rolled back. Such conflicts are reported rather than
    overwriting the winner. Restoring content does not lift an author's ban.
    """

    def __init__(
        self,
        *,
        gateway: TiebaGateway,
        store: Store,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Set the service dependencies.

        Args:
            gateway: Tieba action gateway.
            store: Case persistence with atomic conditional transitions.
            clock: Unix-time source sampled when an update is attempted.
        """
        self._gateway = gateway
        self._store = store
        self._clock = clock
        self._lock = asyncio.Lock()

    async def delete(self, case_id: int, operator: str) -> Case:
        """Delete a pending case's content and record the moderator.

        Args:
            case_id: Case identifier.
            operator: Moderator identifier.

        Returns:
            The updated deleted case.

        Raises:
            CaseError: The case is missing, ineligible, or concurrently handled.
            TiebaActionError: The deletion failed; the case is unchanged.
        """
        async with self._lock:
            case = self._require_case(case_id, (CaseStatus.PENDING,))
            await self._gateway.delete(case.item)
            return self._transition(case, CaseStatus.DELETED, operator)

    async def ban(self, case_id: int, days: int, operator: str) -> Case:
        """Delete pending content if needed, then ban its author.

        Args:
            case_id: Pending, automatically deleted, or manually deleted case.
            days: Allowed integer ban duration: 1, 3, or 10 days.
            operator: Moderator identifier.

        Returns:
            The updated banned case.

        Raises:
            CaseError: The duration or case is invalid, or an update lost a race.
            TiebaActionError: A remote action failed. A successful deletion is
                recorded as DELETED with a failure note if banning then fails.
                Already-deleted cases remain unchanged on a failed ban.
        """
        async with self._lock:
            if type(days) is not int or days not in ALLOWED_BAN_DAYS:
                raise CaseError("封禁天数只能是 1、3 或 10 天。")
            case = self._require_case(
                case_id,
                (CaseStatus.PENDING, CaseStatus.AUTO_DELETED, CaseStatus.DELETED),
            )
            deleted_now = case.status is CaseStatus.PENDING
            if deleted_now:
                await self._gateway.delete(case.item)
            reason = f"{case.verdict.category}：{case.verdict.reason}"[:50]
            try:
                await self._gateway.block(case.item, days, reason)
            except TiebaActionError as error:
                if deleted_now:
                    self._transition(
                        case, CaseStatus.DELETED, operator, note=f"封禁失败：{error}"
                    )
                raise
            return self._transition(case, CaseStatus.BANNED, operator)

    async def ignore(self, case_id: int, operator: str) -> Case:
        """Dismiss a pending report without performing a remote action.

        Args:
            case_id: Case identifier.
            operator: Moderator identifier.

        Returns:
            The updated ignored case.

        Raises:
            CaseError: The case is missing, ineligible, or concurrently handled.
        """
        async with self._lock:
            case = self._require_case(case_id, (CaseStatus.PENDING,))
            return self._transition(case, CaseStatus.IGNORED, operator)

    async def restore(self, case_id: int, operator: str) -> Case:
        """Restore deleted content without unbanning its author.

        Args:
            case_id: Automatically deleted, manually deleted, or banned case.
            operator: Moderator identifier.

        Returns:
            The updated restored case.

        Raises:
            CaseError: The case is missing, ineligible, or concurrently handled.
            TiebaActionError: Restoration failed; the case is unchanged.
        """
        async with self._lock:
            case = self._require_case(
                case_id,
                (CaseStatus.AUTO_DELETED, CaseStatus.DELETED, CaseStatus.BANNED),
            )
            await self._gateway.restore(case.item)
            return self._transition(case, CaseStatus.RESTORED, operator)

    def _require_case(self, case_id: int, allowed: tuple[CaseStatus, ...]) -> Case:
        case = self._store.get_case(case_id)
        if case is None:
            raise CaseError(f"案件 {case_id} 不存在。")
        if case.status not in allowed:
            raise CaseError(f"案件 {case_id} 当前状态不允许此操作。")
        return case

    def _transition(
        self, case: Case, target: CaseStatus, operator: str, *, note: str = ""
    ) -> Case:
        updated = self._store.transition(
            case.id,
            from_=(case.status,),
            to=target,
            by=operator,
            now=int(self._clock()),
            note=note,
        )
        if updated is None:
            raise CaseError(f"案件 {case.id} 已被其他吧务处理，请刷新后重试。")
        return updated
