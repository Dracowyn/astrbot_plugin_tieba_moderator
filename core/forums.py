"""Immutable forum profiles and unchanged moderation permission scopes."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .models import CaseStatus
from .settings import SettingsSnapshot


@dataclass(frozen=True, kw_only=True)
class ForumProfile:
    """Resolved settings for one forum, retaining its configured spelling."""

    fname: str
    rules: str
    sessions: tuple[str, ...]
    operators: frozenset[str]
    rules_overridden: bool = False
    sessions_overridden: bool = False


@dataclass(frozen=True, kw_only=True)
class OperatorScope:
    """What one sender is allowed to moderate.

    Attributes:
        global_operator: True for AstrBot administrators and global operators;
            such a sender may act on every forum and sees report error details.
        forums: Casefolded keys of the forums this sender moderates locally.
            Empty for a sender with no permission at all.
    """

    global_operator: bool
    forums: frozenset[str] = frozenset()

    @property
    def any(self) -> bool:
        """Whether this scope may do anything at all."""
        return self.global_operator or bool(self.forums)


GLOBAL_SCOPE = OperatorScope(global_operator=True)
"""Scope of an AstrBot administrator or a global operator."""


def _identity(sender_id: object) -> str:
    """Normalize a user id for comparison with configured operators."""
    return str(sender_id or "").strip()


@dataclass(frozen=True, kw_only=True)
class ForumConfig:
    """Immutable snapshot of forum settings and their global defaults."""

    forums: tuple[str, ...]
    default_rules: str
    default_sessions: tuple[str, ...]
    global_operators: frozenset[str]
    profiles: Mapping[str, ForumProfile]

    @classmethod
    def from_snapshot(cls, snapshot: SettingsSnapshot) -> "ForumConfig":
        """Build profiles for every configured forum, including disabled ones."""
        global_ = snapshot.global_
        profiles = {
            forum.fname.strip().casefold(): ForumProfile(
                fname=forum.fname,
                rules=(
                    global_.defaults.rules
                    if forum.overrides.rules is None
                    else forum.overrides.rules
                ),
                sessions=forum.sessions or global_.sessions,
                operators=forum.operators,
                rules_overridden=forum.overrides.rules is not None,
                sessions_overridden=bool(forum.sessions),
            )
            for forum in snapshot.forums
        }
        return cls(
            forums=tuple(forum.fname for forum in snapshot.forums),
            default_rules=global_.defaults.rules,
            default_sessions=global_.sessions,
            global_operators=global_.operators,
            profiles=MappingProxyType(profiles),
        )

    def profile(self, fname: str) -> ForumProfile:
        """Resolve an override or return global defaults for the requested forum.

        Args:
            fname: Forum name, compared without surrounding whitespace or case.

        Returns:
            The configured profile or a profile with no local operators.
        """
        name = fname.strip()
        profile = self.profiles.get(name.casefold())
        if profile is not None:
            return profile
        return ForumProfile(
            fname=name,
            rules=self.default_rules,
            sessions=self.default_sessions,
            operators=frozenset(),
        )

    def forum_key(self, fname: str) -> str:
        """Normalize a forum name for comparisons.

        Args:
            fname: Raw forum name from configuration, a stored case or chat.

        Returns:
            ``fname.strip().casefold()``, the key used by every comparison
            (forum-config contract §2).
        """
        return fname.strip().casefold()

    def is_global_operator(self, sender_id: str, *, is_admin: bool) -> bool:
        """Whether a sender may act on every forum.

        Args:
            sender_id: Platform user id of the sender.
            is_admin: Whether AstrBot reports the sender as an administrator.

        Returns:
            True for AstrBot administrators and global operators.
        """
        if is_admin:
            return True
        return _identity(sender_id) in self.global_operators

    def moderated_forums(self, sender_id: str) -> frozenset[str]:
        """Casefolded keys of the forums whose local moderators include a sender.

        Args:
            sender_id: Platform user id of the sender.

        Returns:
            Forum keys, empty when the sender moderates no forum.
        """
        who = _identity(sender_id)
        if not who:
            return frozenset()
        return frozenset(
            key for key, profile in self.profiles.items() if who in profile.operators
        )

    def scope_for(self, sender_id: str, *, is_admin: bool) -> OperatorScope:
        """Resolve what a sender is allowed to moderate.

        Args:
            sender_id: Platform user id of the sender.
            is_admin: Whether AstrBot reports the sender as an administrator.

        Returns:
            The sender's scope; see ``OperatorScope``.
        """
        if self.is_global_operator(sender_id, is_admin=is_admin):
            return GLOBAL_SCOPE
        return OperatorScope(
            global_operator=False, forums=self.moderated_forums(sender_id)
        )

    def may_moderate(self, scope: OperatorScope, fname: str) -> bool:
        """Whether a scope allows acting on a case from the given forum.

        Args:
            scope: The sender's resolved scope.
            fname: Forum name recorded on the case.

        Returns:
            True for global operators, and for local moderators of that forum.
        """
        if scope.global_operator:
            return True
        return self.forum_key(fname) in scope.forums


def scope_forum_keys(scope: OperatorScope) -> tuple[str, ...]:
    """Forum keys a scope may list, for ``query_cases``.

    ``query_cases`` treats an empty ``fnames`` as "no forum filter", so this
    value alone cannot express "may see nothing". Callers must either test
    ``scope.any`` first or use ``pending_count``, which does.

    Args:
        scope: The sender's scope.

    Returns:
        An empty tuple for global operators (no forum filter), otherwise the
        sorted casefolded keys of the moderated forums.
    """
    if scope.global_operator:
        return ()
    return tuple(sorted(scope.forums))


def pending_count(store: Any, scope: OperatorScope) -> int:
    """Count pending cases a scope may see.

    Args:
        store: Case persistence exposing ``query_cases``.
        scope: The sender's scope.

    Returns:
        The number of pending cases in scope; zero for a sender without one,
        because an empty forum filter would otherwise mean "every forum".
    """
    if not scope.any:
        return 0
    total, _ = store.query_cases(
        statuses=(CaseStatus.PENDING,), fnames=scope_forum_keys(scope), limit=1
    )
    return total


def missing_case_reply(case_id: int, scope: OperatorScope) -> str:
    """Reply for a case that is missing or out of the sender's scope.

    Local moderators get the same wording either way, so probing case ids does
    not reveal which forums have cases (forum-config contract §5).

    Args:
        case_id: The requested case id.
        scope: The sender's scope.

    Returns:
        Plain text.
    """
    if scope.global_operator:
        return f"案件 #{case_id} 不存在。"
    return f"案件 #{case_id} 不存在或不在你负责的吧。"


@dataclass(frozen=True, kw_only=True)
class BindResult:
    """A session that was appended to a push list.

    Attributes:
        label: Chinese label of the target list, e.g. ``"龙 吧的"`` or ``""``.
        total: Number of sessions in that list after the append.
    """

    label: str
    total: int


def parse_case_id(raw: str) -> int | None:
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
