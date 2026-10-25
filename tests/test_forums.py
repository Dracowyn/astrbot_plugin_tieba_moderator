"""Pure forum parsing, tolerance, fallback and immutable snapshots."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest
from astrbot_plugin_tieba_moderator.core.forums import (
    GLOBAL_SCOPE,
    BindResult,
    ForumConfig,
    ForumProfile,
    OperatorScope,
    bind_session,
    parse_forum_config,
    pending_count,
)
from astrbot_plugin_tieba_moderator.core.models import CaseStatus


def test_legacy_configuration_keeps_global_defaults():
    config = {
        "forums": ["Python", "中文"],
        "judge": {"rules": "  global rules\n"},
        "notify": {"sessions": ["group"], "operators": ["admin"]},
    }
    snapshot = deepcopy(config)
    parsed, warnings = parse_forum_config(config)
    assert config == snapshot
    assert warnings == []
    assert parsed.forums == ("Python", "中文")
    assert parsed.default_rules == "  global rules\n"
    assert parsed.default_sessions == ("group",)
    assert parsed.global_operators == frozenset({"admin"})
    assert parsed.profiles == {}
    assert parsed.profile("Python") == ForumProfile(
        fname="Python",
        rules="  global rules\n",
        sessions=("group",),
        operators=frozenset(),
    )


def test_union_uses_unicode_casefold_and_preserves_first_spelling():
    parsed, warnings = parse_forum_config(
        {
            "forums": [" Python ", "PYTHON", "Straße", "STRASSE", "中文"],
            "forum_overrides": [
                {"fname": " python ", "rules": "local"},
                {"fname": "STRASSE"},
                {"fname": " 新增 "},
                {"fname": "新增", "rules": "ignored"},
                {"fname": "PYTHON", "rules": "ignored"},
            ],
        }
    )
    assert parsed.forums == ("Python", "Straße", "中文", "新增")
    assert tuple(parsed.profiles) == ("python", "strasse", "新增")
    assert parsed.profile(" PYTHON ").fname == "python"
    assert parsed.profile("Python").rules == "local"
    assert parsed.profile("straße") is parsed.profiles["strasse"]
    assert parsed.profile("新增").rules == ""
    assert len(warnings) == 2
    assert all("Duplicate" in warning for warning in warnings)


@pytest.mark.parametrize("invalid", [None, 42, True, [], {}, "", " \t\n"])
def test_invalid_override_names_are_skipped_with_warning(invalid):
    parsed, warnings = parse_forum_config({"forum_overrides": [{"fname": invalid}]})
    assert parsed.forums == ()
    assert parsed.profiles == {}
    assert len(warnings) == 1
    assert "fname" in warnings[0]


@pytest.mark.parametrize("entry", [None, 3, "python", [], True])
def test_non_dict_overrides_are_skipped_with_warning(entry):
    parsed, warnings = parse_forum_config({"forum_overrides": [entry, {"fname": "ok"}]})
    assert parsed.forums == ("ok",)
    assert len(warnings) == 1
    assert "dictionary" in warnings[0]


def test_missing_override_name_is_skipped():
    parsed, warnings = parse_forum_config({"forum_overrides": [{}]})
    assert parsed.forums == ()
    assert len(warnings) == 1


@pytest.mark.parametrize("invalid", [None, 42, True, {}, "", "  "])
def test_invalid_patrol_names_are_skipped_with_warning(invalid):
    parsed, warnings = parse_forum_config({"forums": [invalid, "valid"]})
    assert parsed.forums == ("valid",)
    assert len(warnings) == 1


@pytest.mark.parametrize("invalid", [None, 42, True, "python", {}, ("python",)])
def test_malformed_outer_lists_are_empty(invalid):
    parsed, warnings = parse_forum_config(
        {"forums": invalid, "forum_overrides": invalid}
    )
    assert parsed.forums == ()
    assert parsed.profiles == {}
    assert warnings == []


@pytest.mark.parametrize("invalid", [None, 42, True, "invalid", [], ()])
def test_malformed_global_sections_use_empty_defaults(invalid):
    parsed, warnings = parse_forum_config({"judge": invalid, "notify": invalid})
    assert parsed.default_rules == ""
    assert parsed.default_sessions == ()
    assert parsed.global_operators == frozenset()
    assert warnings == []


@pytest.mark.parametrize("invalid", [None, 42, True, [], {}, ()])
def test_non_string_rules_and_non_list_destinations_fall_back(invalid):
    parsed, warnings = parse_forum_config(
        {
            "judge": {"rules": "global"},
            "notify": {"sessions": ["global"], "operators": ["admin"]},
            "forum_overrides": [
                {
                    "fname": "test",
                    "rules": invalid,
                    "sessions": invalid,
                    "operators": invalid,
                }
            ],
        }
    )
    profile = parsed.profile("test")
    assert profile.rules == "global"
    assert profile.sessions == ("global",)
    assert profile.operators == frozenset()
    assert not profile.rules_overridden
    assert not profile.sessions_overridden
    assert warnings == []


@pytest.mark.parametrize("invalid", [None, 42, True, {}, (), "not a list"])
def test_malformed_global_values_use_empty_defaults(invalid):
    parsed, _ = parse_forum_config(
        {
            "judge": {"rules": 42},
            "notify": {"sessions": invalid, "operators": invalid},
        }
    )
    assert parsed.default_rules == ""
    assert parsed.default_sessions == ()
    assert parsed.global_operators == frozenset()


def test_list_values_are_stringified_trimmed_and_ordered_deduplicated():
    values = [" b ", "a", "", " \t", "b", 7, "7", None, False, {}, []]
    expected = ("b", "a", "7", "None", "False", "{}", "[]")
    parsed, _ = parse_forum_config(
        {
            "notify": {"sessions": values, "operators": values},
            "forum_overrides": [
                {"fname": "test", "sessions": values, "operators": values}
            ],
        }
    )
    assert parsed.default_sessions == expected
    assert parsed.global_operators == frozenset(expected)
    assert parsed.profile("test").sessions == expected
    assert parsed.profile("test").operators == frozenset(expected)
    assert parsed.profile("test").sessions_overridden


def test_rules_preserve_nonblank_text_and_empty_overrides_fall_back():
    text = " \n  keep this layout\n\t"
    parsed, _ = parse_forum_config(
        {
            "judge": {"rules": text},
            "notify": {"sessions": ["group"]},
            "forum_overrides": [
                {"fname": "explicit", "rules": text, "sessions": ["group"]},
                {"fname": "blank", "rules": " \n\t", "sessions": ["", " "]},
                {"fname": "missing"},
                {"fname": "local", "rules": "  local\n", "sessions": ["local"]},
            ],
        }
    )
    explicit = parsed.profile("explicit")
    assert explicit.rules == text
    assert explicit.rules_overridden and explicit.sessions_overridden
    for fname in ("blank", "missing", "unconfigured"):
        profile = parsed.profile(fname)
        assert profile.rules == text
        assert profile.sessions == ("group",)
        assert not profile.rules_overridden
        assert not profile.sessions_overridden
    assert parsed.profile("local").rules == "  local\n"
    assert parsed.profile("local").sessions == ("local",)


def test_empty_configuration_and_blank_global_rules():
    parsed, warnings = parse_forum_config({})
    assert parsed.forums == ()
    assert warnings == []
    assert parsed.profile(" unknown ") == ForumProfile(
        fname="unknown", rules="", sessions=(), operators=frozenset()
    )
    blank, _ = parse_forum_config({"judge": {"rules": " \n\t"}})
    assert blank.default_rules == ""


def test_parsed_result_is_frozen_and_detached_from_input():
    config = {
        "forums": ["test"],
        "notify": {"sessions": ["global"], "operators": ["admin"]},
        "forum_overrides": [
            {"fname": "test", "sessions": ["local"], "operators": ["user"]}
        ],
    }
    original = deepcopy(config)
    parsed, _ = parse_forum_config(config)
    assert config == original
    profile = parsed.profile("test")
    with pytest.raises(FrozenInstanceError):
        parsed.forums = ()
    with pytest.raises(FrozenInstanceError):
        profile.rules = "changed"
    with pytest.raises(TypeError):
        parsed.profiles["other"] = profile
    config["forums"].append("other")
    config["notify"]["sessions"].append("other")
    config["notify"]["operators"].append("other")
    config["forum_overrides"][0]["sessions"].append("other")
    config["forum_overrides"][0]["operators"].append("other")
    assert parsed.forums == ("test",)
    assert parsed.default_sessions == ("global",)
    assert parsed.global_operators == frozenset({"admin"})
    assert profile.sessions == ("local",)
    assert profile.operators == frozenset({"user"})
    assert replace(profile, rules="new").rules == "new"
    assert profile.rules == ""


def test_dataclasses_are_keyword_only():
    with pytest.raises(TypeError):
        ForumProfile("test", "", (), frozenset())
    with pytest.raises(TypeError):
        ForumConfig((), "", (), frozenset(), {})


# --- scopes and binding -----------------------------------------------------


def test_scope_normalizes_identities_and_forum_keys():
    config, _ = parse_forum_config(
        {
            "forums": ["Dragon"],
            "forum_overrides": [{"fname": "Dragon", "operators": [" 3001 "]},],
            "notify": {"operators": ["2001 "]},
        }
    )
    assert config.is_global_operator("2001", is_admin=False) is True
    assert config.is_global_operator(" 2001", is_admin=False) is True
    assert config.is_global_operator("3001", is_admin=False) is False
    assert config.scope_for("3001", is_admin=False).forums == frozenset({"dragon"})
    assert config.scope_for("nobody", is_admin=False).any is False
    assert config.scope_for("nobody", is_admin=True).global_operator is True
    assert config.moderated_forums("") == frozenset()


def test_scope_never_grants_a_blank_forum_name():
    config, _ = parse_forum_config(
        {"forums": ["Dragon"], "forum_overrides": [{"fname": "Dragon", "operators": ["u"]}]}
    )
    scope = config.scope_for("u", is_admin=False)
    for fname in ("", "   ", "\t\n"):
        assert config.may_moderate(scope, fname) is False


def test_pending_count_is_zero_for_a_sender_without_scope():
    class Store:
        def query_cases(self, **kwargs):
            raise AssertionError("must not query without a scope")

    assert pending_count(Store(), OperatorScope(global_operator=False)) == 0


def test_pending_count_passes_the_extracted_forum_keys():
    seen = {}

    class Store:
        def query_cases(self, **kwargs):
            seen.update(kwargs)
            return 7, []

    config, _ = parse_forum_config(
        {
            "forums": ["B", "a"],
            "forum_overrides": [
                {"fname": "B", "operators": ["u"]},
                {"fname": "a", "operators": ["u"]},
            ],
        }
    )
    assert pending_count(Store(), config.scope_for("u", is_admin=False)) == 7
    assert seen["fnames"] == ("a", "b")
    assert seen["statuses"] == (CaseStatus.PENDING,)
    assert pending_count(Store(), GLOBAL_SCOPE) == 7
    assert seen["fnames"] == ()


@pytest.mark.parametrize("notify", ["oops", 5, None, [], {"sessions": "x"}])
def test_bind_tolerates_a_malformed_notify_section(notify):
    config = {"forums": ["Dragon"], "notify": notify}
    parsed, _ = parse_forum_config(config)
    result = bind_session(config, parsed, "", "offline:group:test")
    assert isinstance(result, BindResult)
    assert config["notify"] == {"sessions": ["offline:group:test"]}


def test_bind_is_idempotent_and_keeps_other_sessions():
    config = {
        "forums": ["Dragon"],
        "forum_overrides": [{"fname": "Dragon", "sessions": ["a", "b"]}],
        "notify": {"sessions": []},
    }
    parsed, _ = parse_forum_config(config)
    assert bind_session(config, parsed, "dragon", "c").total == 3
    assert config["forum_overrides"][0]["sessions"] == ["a", "b", "c"]
    assert bind_session(config, parsed, "dragon", "c") == (
        "当前会话已经在Dragon 吧的推送列表里。"
    )


def test_bind_refuses_a_forum_that_is_not_patrolled():
    config = {"forums": ["Dragon"], "notify": {"sessions": []}}
    parsed, _ = parse_forum_config(config)
    assert bind_session(config, parsed, "Evil", "s") == (
        "未巡查 Evil 吧，不能绑定推送会话。"
    )
    assert "forum_overrides" not in config


def test_bind_bounds_the_echoed_argument():
    config = {"forums": ["Dragon"], "notify": {"sessions": []}}
    parsed, _ = parse_forum_config(config)
    assert "吧名过长" in bind_session(config, parsed, "x" * 65, "s")
    assert "吧名过长" in bind_session(config, parsed, "x" * 50000, "s")
    # Newlines in a shorter argument are flattened before the echo.
    echoed = bind_session(config, parsed, "Evil\nLine", "s")
    assert "\n" not in echoed
