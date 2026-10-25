"""Parse global defaults and per-forum overrides without mutating configuration."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .models import CaseStatus


@dataclass(frozen=True, kw_only=True)
class ForumProfile:
    """Resolved settings for one forum, retaining its configured spelling."""

    fname: str
    rules: str
    sessions: tuple[str, ...]
    operators: frozenset[str]
    rules_overridden: bool = False
    sessions_overridden: bool = False


MAX_FORUM_ARG_CHARS = 64
"""Longest forum name accepted from chat when binding a session."""


def _reply_text(value: str) -> str:
    """Flatten chat text that is about to be echoed back into a reply."""
    return " ".join(str(value).split())


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


def _string_list(value: object) -> tuple[str, ...]:
    """Normalize a list into nonblank, distinct strings in input order."""
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(text for item in value if (text := str(item).strip())))


def parse_forum_config(config: Mapping[str, Any]) -> tuple[ForumConfig, list[str]]:
    """Parse tolerant forum settings without modifying the supplied mapping.

    Args:
        config: Plugin configuration containing global and per-forum settings.

    Returns:
        An immutable resolved snapshot and English warnings for skipped entries.
    """
    judge = config.get("judge", {})
    notify = config.get("notify", {})
    if not isinstance(judge, Mapping):
        judge = {}
    if not isinstance(notify, Mapping):
        notify = {}
    default_rules = judge.get("rules")
    if not isinstance(default_rules, str) or not default_rules.strip():
        default_rules = ""
    default_sessions = _string_list(notify.get("sessions"))
    global_operators = frozenset(
        _identity(value) for value in _string_list(notify.get("operators"))
    )
    warnings: list[str] = []
    forums: dict[str, str] = {}
    raw_forums = config.get("forums", [])
    if not isinstance(raw_forums, list):
        raw_forums = []
    for index, fname in enumerate(raw_forums):
        if not isinstance(fname, str) or not fname.strip():
            warnings.append(
                f"Skipping forums[{index}]: fname must be a nonblank string."
            )
            continue
        name = fname.strip()
        forums.setdefault(name.casefold(), name)

    profiles: dict[str, ForumProfile] = {}
    overrides = config.get("forum_overrides", [])
    if not isinstance(overrides, list):
        overrides = []
    for index, entry in enumerate(overrides):
        if not isinstance(entry, dict):
            warnings.append(
                f"Skipping forum_overrides[{index}]: expected a dictionary."
            )
            continue
        fname = entry.get("fname")
        if not isinstance(fname, str) or not fname.strip():
            warnings.append(
                f"Skipping forum_overrides[{index}]: fname must be a nonblank string."
            )
            continue
        name = fname.strip()
        key = name.casefold()
        if key in profiles:
            warnings.append(
                f"Duplicate forum override at index {index} for {name!r}; keeping the first."
            )
            continue
        rules = entry.get("rules")
        if not isinstance(rules, str) or not rules.strip():
            rules = ""
        sessions = _string_list(entry.get("sessions"))
        profiles[key] = ForumProfile(
            fname=name,
            rules=rules or default_rules,
            sessions=sessions or default_sessions,
            operators=frozenset(
                _identity(value) for value in _string_list(entry.get("operators"))
            ),
            rules_overridden=bool(rules),
            sessions_overridden=bool(sessions),
        )
        forums.setdefault(key, name)
    return ForumConfig(
        forums=tuple(forums.values()),
        default_rules=default_rules,
        default_sessions=default_sessions,
        global_operators=global_operators,
        profiles=MappingProxyType(profiles),
    ), warnings


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


def _sessions_of(target: Mapping) -> list[str]:
    """Read a push-session list tolerantly.

    Args:
        target: A config section that may hold a ``sessions`` list.

    Returns:
        Nonblank, distinct session strings in input order.
    """
    raw = target.get("sessions")
    if not isinstance(raw, list):
        return []
    return list(
        dict.fromkeys(str(value).strip() for value in raw if str(value).strip())
    )


def bind_session(
    config: dict, forum_config: ForumConfig, fname: str, session: str
) -> BindResult | str:
    """Append a session to the global or a per-forum push list, in memory only.

    The caller persists the config and reparses ``ForumConfig``; keeping the
    mutation here keeps ``main.py`` to command wiring and lets the write path be
    tested without a Star instance.

    A forum name typed in chat only ever selects an existing, patrolled forum:
    the stored ``fname`` always comes from the parsed config, never from the
    raw argument, so the argument cannot introduce another entry.

    Args:
        config: Plugin configuration, mutated in place on success.
        forum_config: Currently parsed config, used to validate the forum name.
        fname: Raw forum argument; empty selects the global list.
        session: Session origin to append.

    Returns:
        A Chinese reply on refusal, otherwise the append result.
    """
    label = ""
    if fname:
        if len(fname) > MAX_FORUM_ARG_CHARS:
            # The argument is echoed in the refusal, so bound it first: chat text
            # must not be able to produce a multi-kilobyte reply.
            return "吧名过长，不能绑定推送会话。"
        key = fname.casefold()
        names = {name.casefold(): name for name in forum_config.forums}
        if key not in names:
            return f"未巡查 {_reply_text(fname)} 吧，不能绑定推送会话。"
        entries = config.get("forum_overrides")
        entries = list(entries) if isinstance(entries, list) else []
        index = next(
            (
                position
                for position, value in enumerate(entries)
                if isinstance(value, dict)
                and isinstance(value.get("fname"), str)
                and value["fname"].strip().casefold() == key
            ),
            len(entries),
        )
        target = (
            entries[index]
            if index < len(entries)
            else {
                "__template_key": "forum",
                "fname": names[key],
                "rules": "",
                "sessions": [],
                "operators": [],
            }
        )
        label = f"{names[key]} 吧的"
    else:
        target = config.get("notify")
        if not isinstance(target, Mapping):
            # A malformed notify section must not abort the command.
            target = {}

    sessions = _sessions_of(target)
    if session in sessions:
        return f"当前会话已经在{label}推送列表里。"
    updated = {**target, "sessions": [*sessions, session]}
    if fname:
        entries[index : index + 1] = [updated]
        config["forum_overrides"] = entries
    else:
        config["notify"] = updated
    return BindResult(label=label, total=len(sessions) + 1)
