"""Snapshot-backed immutable forum profiles and moderation scopes."""

from dataclasses import FrozenInstanceError, replace

import pytest
from astrbot_plugin_tieba_moderator.core.forums import (
    GLOBAL_SCOPE,
    BindResult,
    ForumConfig,
    ForumProfile,
    OperatorScope,
    missing_case_reply,
    pending_count,
)
from astrbot_plugin_tieba_moderator.core.models import CaseStatus
from astrbot_plugin_tieba_moderator.core.settings import (
    ForumEntry,
    ForumOptions,
    ForumOverrides,
    GlobalSettings,
    SettingsSnapshot,
)


def forum(fname="Python", **kwargs):
    return ForumEntry(id=1, fname=fname, created_at=1, updated_at=1, **kwargs)


def snapshot(*forums, **kwargs):
    return SettingsSnapshot(
        global_=GlobalSettings(defaults=ForumOptions(rules="  global rules\n"), **kwargs),
        forums=forums,
    )


def test_snapshot_keeps_global_defaults_and_all_configured_names():
    source = snapshot(forum(), forum("中文", enabled=False), sessions=("group",), operators=frozenset({"admin"}))
    parsed = ForumConfig.from_snapshot(source)
    assert parsed.forums == ("Python", "中文")
    assert tuple(parsed.profiles) == ("python", "中文")
    assert parsed.default_rules == "  global rules\n"
    assert parsed.default_sessions == ("group",)
    assert parsed.global_operators == frozenset({"admin"})
    assert parsed.profile("Python") == ForumProfile(fname="Python", rules="  global rules\n", sessions=("group",), operators=frozenset())


def test_lookup_uses_unicode_casefold_and_preserves_configured_spelling():
    parsed = ForumConfig.from_snapshot(snapshot(forum("Straße")))
    assert parsed.forums == ("Straße",)
    assert parsed.profile(" STRASSE ") is parsed.profiles["strasse"]
    assert parsed.profile("straße").fname == "Straße"


def test_rules_and_sessions_inherit_independently_and_keep_override_flags():
    text = "  global rules\n"
    parsed = ForumConfig.from_snapshot(snapshot(
        forum("explicit", overrides=ForumOverrides(rules=text), sessions=("group",)),
        forum("inherited"),
        forum("local", overrides=ForumOverrides(rules="  local\n"), sessions=("local",)),
        sessions=("group",),
    ))
    explicit = parsed.profile("explicit")
    assert explicit.rules == text
    assert explicit.rules_overridden and explicit.sessions_overridden
    for name in ("inherited", "unconfigured"):
        profile = parsed.profile(name)
        assert profile.rules == text and profile.sessions == ("group",)
        assert not profile.rules_overridden and not profile.sessions_overridden
    assert parsed.profile("local").rules == "  local\n"
    assert parsed.profile("local").sessions == ("local",)


def test_empty_snapshot_and_unknown_forum_use_defaults_without_local_permissions():
    parsed = ForumConfig.from_snapshot(snapshot())
    assert parsed.forums == () and parsed.profiles == {}
    assert parsed.profile(" unknown ") == ForumProfile(fname="unknown", rules="  global rules\n", sessions=(), operators=frozenset())


def test_snapshot_view_is_frozen_and_unchanged_after_replacement():
    source = snapshot(forum(sessions=("local",), operators=frozenset({"user"})), sessions=("global",))
    parsed = ForumConfig.from_snapshot(source)
    profile = parsed.profile("python")
    with pytest.raises(FrozenInstanceError):
        parsed.forums = ()
    with pytest.raises(FrozenInstanceError):
        profile.rules = "changed"
    with pytest.raises(TypeError):
        parsed.profiles["other"] = profile
    newer = replace(source, forums=(replace(source.forums[0], sessions=("new",)),))
    assert ForumConfig.from_snapshot(newer).profile("python").sessions == ("new",)
    assert profile.sessions == ("local",)
    assert replace(profile, rules="new").rules == "new"
    assert profile.rules == source.global_.defaults.rules


def test_dataclasses_are_keyword_only():
    with pytest.raises(TypeError):
        ForumProfile("test", "", (), frozenset())
    with pytest.raises(TypeError):
        ForumConfig((), "", (), frozenset(), {})
    assert BindResult(label="", total=1).total == 1


def test_disabled_and_unassigned_forums_retain_local_permission():
    parsed = ForumConfig.from_snapshot(snapshot(
        forum("Disabled", enabled=False, operators=frozenset({"u"})),
        forum("Unassigned", operators=frozenset({"u"})),
    ))
    scope = parsed.scope_for("u", is_admin=False)
    assert scope.forums == frozenset({"disabled", "unassigned"})
    assert parsed.may_moderate(scope, " DISABLED ")
    assert parsed.may_moderate(scope, "Unassigned")


def test_scope_normalizes_sender_identities_and_forum_keys():
    parsed = ForumConfig.from_snapshot(snapshot(forum("Dragon", operators=frozenset({"3001"})), operators=frozenset({"2001"})))
    assert parsed.is_global_operator(" 2001", is_admin=False)
    assert not parsed.is_global_operator("3001", is_admin=False)
    assert parsed.scope_for(" 3001 ", is_admin=False).forums == frozenset({"dragon"})
    assert not parsed.scope_for("nobody", is_admin=False).any
    assert parsed.scope_for("nobody", is_admin=True) == GLOBAL_SCOPE
    assert parsed.moderated_forums("") == frozenset()
    for name in ("", "   ", "\t\n"):
        assert not parsed.may_moderate(parsed.scope_for("3001", is_admin=False), name)


def test_pending_count_is_zero_without_scope():
    class Store:
        def query_cases(self, **kwargs):
            raise AssertionError("must not query without a scope")

    assert pending_count(Store(), OperatorScope(global_operator=False)) == 0


def test_pending_count_passes_sorted_forum_keys():
    seen = {}

    class Store:
        def query_cases(self, **kwargs):
            seen.update(kwargs)
            return 7, []

    scope = OperatorScope(global_operator=False, forums=frozenset({"b", "a"}))
    assert pending_count(Store(), scope) == 7
    assert seen["fnames"] == ("a", "b")
    assert seen["statuses"] == (CaseStatus.PENDING,)
    assert pending_count(Store(), GLOBAL_SCOPE) == 7
    assert seen["fnames"] == ()


def test_missing_case_reply_preserves_scope_privacy():
    assert missing_case_reply(7, GLOBAL_SCOPE) == "案件 #7 不存在。"
    assert missing_case_reply(7, OperatorScope(global_operator=False)) == "案件 #7 不存在或不在你负责的吧。"
