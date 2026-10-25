"""Validation of untrusted settings payloads: every field's edges."""

import math

import pytest
from astrbot_plugin_tieba_moderator.core import validation as v
from astrbot_plugin_tieba_moderator.core.settings import (
    AccountDraft,
    ForumDraft,
    ForumOptions,
    ForumOverrides,
    GlobalSettings,
)
from astrbot_plugin_tieba_moderator.core.validation import (
    ValidationError,
    parse_account_draft,
    parse_forum_draft,
    parse_global,
)

PROVIDERS = frozenset({"prov-a", "prov-b"})
ACCOUNTS = frozenset({1, 2})
DEFAULT_RULES = "禁止广告"


def options(**changes):
    """A valid ``defaults`` payload."""
    payload = {
        "rules": DEFAULT_RULES,
        "provider_id": "",
        "auto_delete_enabled": False,
        "auto_delete_threshold": 0.9,
        "report_threshold": 0.6,
        "thread_pages": 1,
        "lookback_hours": 6,
        "review_posts": True,
        "review_comments": True,
        "skip_top_and_good": True,
        "skip_bawu": True,
        "whitelist": [],
    }
    payload.update(changes)
    return payload


def global_payload(**changes):
    """A valid ``GlobalDTO`` payload."""
    payload = {
        "patrol_enabled": True,
        "interval_seconds": 180,
        "max_items_per_round": 60,
        "judge_concurrency": 2,
        "judge_timeout_seconds": 60,
        "review_images": True,
        "max_images": 4,
        "sessions": [],
        "operators": [],
        "defaults": options(),
    }
    payload.update(changes)
    return payload


def forum_payload(**changes):
    """A valid ``ForumDraftDTO`` payload."""
    payload = {
        "fname": "Dragon",
        "enabled": True,
        "account_id": None,
        "sessions": [],
        "operators": [],
        "whitelist": [],
        "overrides": {},
    }
    payload.update(changes)
    return payload


def messages(excinfo):
    """Return the exception message."""
    return str(excinfo.value)


# --- top-level shape --------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "text", 5, [], [{}], True])
def test_global_requires_a_json_object(raw):
    with pytest.raises(ValidationError, match="JSON 对象"):
        parse_global(raw, provider_ids=PROVIDERS)


@pytest.mark.parametrize("raw", [None, "text", 5, [], True])
def test_forum_requires_a_json_object(raw):
    with pytest.raises(ValidationError, match="JSON 对象"):
        parse_forum_draft(raw, provider_ids=PROVIDERS, account_ids=ACCOUNTS)


@pytest.mark.parametrize("raw", [None, "text", 5, [], True])
def test_account_requires_a_json_object(raw):
    with pytest.raises(ValidationError, match="JSON 对象"):
        parse_account_draft(raw, creating=True)


def test_unknown_top_level_fields_are_rejected():
    with pytest.raises(ValidationError, match="未知字段：interval"):
        parse_global(global_payload(interval="x"), provider_ids=PROVIDERS)


def test_unknown_nested_fields_are_rejected():
    payload = global_payload()
    payload["defaults"]["max_image"] = 3
    with pytest.raises(ValidationError, match="defaults 包含未知字段：max_image"):
        parse_global(payload, provider_ids=PROVIDERS)


def test_unknown_forum_and_override_fields_are_rejected():
    with pytest.raises(ValidationError, match="未知字段：account"):
        parse_forum_draft(
            forum_payload(account=1), provider_ids=PROVIDERS, account_ids=ACCOUNTS
        )
    with pytest.raises(ValidationError, match="overrides 包含未知字段"):
        parse_forum_draft(
            forum_payload(overrides={"whitelist": []}),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )


def test_unknown_account_field_is_rejected():
    with pytest.raises(ValidationError, match="未知字段：password"):
        parse_account_draft(
            {"name": "a", "bduss": "b", "stoken": "s", "password": "x"}, creating=True
        )


def test_unknown_field_name_is_not_echoed_when_unprintable():
    payload = global_payload()
    payload["bad\nkey"] = 1
    with pytest.raises(ValidationError, match="非法字段名"):
        parse_global(payload, provider_ids=PROVIDERS)


def test_unknown_field_name_is_truncated():
    payload = global_payload()
    payload["k" * 200] = 1
    with pytest.raises(ValidationError, match="非法字段名"):
        parse_global(payload, provider_ids=PROVIDERS)


# --- missing keys -----------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "patrol_enabled",
        "interval_seconds",
        "max_items_per_round",
        "judge_concurrency",
        "judge_timeout_seconds",
        "review_images",
        "max_images",
        "sessions",
        "operators",
        "defaults",
    ],
)
def test_global_requires_every_key(key):
    payload = global_payload()
    del payload[key]
    with pytest.raises(ValidationError, match=key):
        parse_global(payload, provider_ids=PROVIDERS)


@pytest.mark.parametrize(
    "key",
    [
        "rules",
        "provider_id",
        "auto_delete_enabled",
        "auto_delete_threshold",
        "report_threshold",
        "thread_pages",
        "lookback_hours",
        "review_posts",
        "review_comments",
        "skip_top_and_good",
        "skip_bawu",
        "whitelist",
    ],
)
def test_defaults_require_every_key(key):
    payload = global_payload()
    del payload["defaults"][key]
    with pytest.raises(ValidationError, match=key):
        parse_global(payload, provider_ids=PROVIDERS)


@pytest.mark.parametrize(
    "key", ["fname", "enabled", "sessions", "operators", "whitelist", "overrides"]
)
def test_forum_requires_every_key(key):
    payload = forum_payload()
    del payload[key]
    with pytest.raises(ValidationError, match=key):
        parse_forum_draft(payload, provider_ids=PROVIDERS, account_ids=ACCOUNTS)


@pytest.mark.parametrize("key", ["name", "bduss", "stoken"])
def test_account_create_requires_every_key(key):
    payload = {"name": "a", "bduss": "b" * 10, "stoken": "s" * 10}
    del payload[key]
    with pytest.raises(ValidationError, match=key):
        parse_account_draft(payload, creating=True)


# --- integer ranges ---------------------------------------------------------


@pytest.mark.parametrize(
    "field,low,high",
    [
        ("interval_seconds", 60, 86400),
        ("max_items_per_round", 1, 500),
        ("judge_concurrency", 1, 8),
        ("judge_timeout_seconds", 10, 600),
        ("max_images", 0, 9),
    ],
)
@pytest.mark.parametrize("value", ["low", "high", "below", "above"])
def test_global_integer_bounds(field, low, high, value):
    actual = {"low": low, "high": high, "below": low - 1, "above": high + 1}[value]
    payload = global_payload(**{field: actual})
    if value in ("low", "high"):
        assert getattr(parse_global(payload, provider_ids=PROVIDERS), field) == actual
    else:
        with pytest.raises(ValidationError, match=f"{field}必须是 {low} 到 {high}"):
            parse_global(payload, provider_ids=PROVIDERS)


@pytest.mark.parametrize(
    "field",
    ["interval_seconds", "max_items_per_round", "judge_concurrency", "max_images"],
)
@pytest.mark.parametrize("value", [True, False, "60", 60.0, 60.5, None, [], {}])
def test_global_integer_fields_reject_bool_string_float(field, value):
    with pytest.raises(ValidationError, match=f"{field}必须是"):
        parse_global(global_payload(**{field: value}), provider_ids=PROVIDERS)


@pytest.mark.parametrize(
    "field,low,high",
    [("thread_pages", 1, 5), ("lookback_hours", 1, 168)],
)
@pytest.mark.parametrize("value", ["low", "high", "below", "above"])
def test_defaults_integer_bounds(field, low, high, value):
    actual = {"low": low, "high": high, "below": low - 1, "above": high + 1}[value]
    payload = global_payload()
    payload["defaults"][field] = actual
    if value in ("low", "high"):
        parsed = parse_global(payload, provider_ids=PROVIDERS)
        assert getattr(parsed.defaults, field) == actual
    else:
        with pytest.raises(ValidationError, match=f"defaults.{field}必须是"):
            parse_global(payload, provider_ids=PROVIDERS)


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_defaults_integer_fields_reject_bool_string_float(value):
    payload = global_payload()
    payload["defaults"]["thread_pages"] = value
    with pytest.raises(ValidationError, match="thread_pages"):
        parse_global(payload, provider_ids=PROVIDERS)


# --- booleans ---------------------------------------------------------------


@pytest.mark.parametrize("field", ["patrol_enabled", "review_images"])
@pytest.mark.parametrize("value", [1, 0, "true", None, [], "false"])
def test_global_booleans_are_strict(field, value):
    with pytest.raises(ValidationError, match=f"{field}必须是 true 或 false"):
        parse_global(global_payload(**{field: value}), provider_ids=PROVIDERS)


@pytest.mark.parametrize("value", [1, "true", None])
def test_forum_enabled_is_strict(value):
    with pytest.raises(ValidationError, match="enabled必须是 true 或 false"):
        parse_forum_draft(
            forum_payload(enabled=value),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )


@pytest.mark.parametrize(
    "field", ["review_posts", "review_comments", "skip_top_and_good", "skip_bawu"]
)
def test_override_booleans_reject_non_bools(field):
    with pytest.raises(ValidationError, match=field):
        parse_forum_draft(
            forum_payload(overrides={field: 1}),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )


# --- thresholds -------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0, 0.5, 1])
def test_threshold_accepts_int_and_float_in_range(value):
    payload = global_payload()
    payload["defaults"]["auto_delete_threshold"] = value
    payload["defaults"]["report_threshold"] = min(value, 0.6) if value >= 0.6 else 0.0
    parsed = parse_global(payload, provider_ids=PROVIDERS)
    assert parsed.defaults.auto_delete_threshold == float(value)


@pytest.mark.parametrize("value", [-0.01, 1.01, 2, -1])
def test_threshold_rejects_out_of_range(value):
    payload = global_payload()
    payload["defaults"]["auto_delete_threshold"] = value
    payload["defaults"]["report_threshold"] = 0.0
    with pytest.raises(ValidationError, match="auto_delete_threshold"):
        parse_global(payload, provider_ids=PROVIDERS)


@pytest.mark.parametrize("value", [True, "0.5", None, [], {}])
def test_threshold_rejects_wrong_types(value):
    payload = global_payload()
    payload["defaults"]["report_threshold"] = value
    with pytest.raises(ValidationError, match="report_threshold"):
        parse_global(payload, provider_ids=PROVIDERS)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_threshold_rejects_non_finite(value):
    payload = global_payload()
    payload["defaults"]["auto_delete_threshold"] = value
    payload["defaults"]["report_threshold"] = 0.0
    with pytest.raises(ValidationError, match="auto_delete_threshold"):
        parse_global(payload, provider_ids=PROVIDERS)


def test_report_threshold_must_not_exceed_auto_delete():
    payload = global_payload()
    payload["defaults"]["auto_delete_threshold"] = 0.5
    payload["defaults"]["report_threshold"] = 0.6
    with pytest.raises(ValidationError, match="report_threshold不能大于"):
        parse_global(payload, provider_ids=PROVIDERS)


def test_equal_thresholds_are_allowed():
    payload = global_payload()
    payload["defaults"]["auto_delete_threshold"] = 0.7
    payload["defaults"]["report_threshold"] = 0.7
    parsed = parse_global(payload, provider_ids=PROVIDERS)
    assert parsed.defaults.report_threshold == 0.7


def test_override_thresholds_are_checked_as_a_pair():
    with pytest.raises(ValidationError, match="report_threshold不能大于"):
        parse_forum_draft(
            forum_payload(
                overrides={"auto_delete_threshold": 0.4, "report_threshold": 0.5}
            ),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )


def test_override_with_only_one_threshold_is_allowed():
    draft = parse_forum_draft(
        forum_payload(overrides={"report_threshold": 0.95}),
        provider_ids=PROVIDERS,
        account_ids=ACCOUNTS,
    )
    assert draft.overrides.report_threshold == 0.95
    assert draft.overrides.auto_delete_threshold is None


# --- strings ----------------------------------------------------------------


def test_rules_are_required_and_bounded():
    payload = global_payload()
    payload["defaults"]["rules"] = " " * 10
    with pytest.raises(ValidationError, match="defaults.rules不能为空"):
        parse_global(payload, provider_ids=PROVIDERS)

    payload["defaults"]["rules"] = "x" * (v.MAX_RULES_CHARS + 1)
    with pytest.raises(ValidationError, match="不能超过 8000"):
        parse_global(payload, provider_ids=PROVIDERS)

    payload["defaults"]["rules"] = "x" * v.MAX_RULES_CHARS
    assert len(parse_global(payload, provider_ids=PROVIDERS).defaults.rules) == 8000


def test_rules_allow_newlines_and_tabs():
    payload = global_payload()
    payload["defaults"]["rules"] = "1. 广告\n2. 辱骂\t违规"
    parsed = parse_global(payload, provider_ids=PROVIDERS)
    assert parsed.defaults.rules == "1. 广告\n2. 辱骂\t违规"


def test_rules_reject_control_and_zero_width_characters():
    payload = global_payload()
    for rules in ("规则\x00", "规则\u200b", "规则\u202e", "规则\u00a0x"):
        payload["defaults"]["rules"] = rules
        with pytest.raises(ValidationError, match="控制字符"):
            parse_global(payload, provider_ids=PROVIDERS)


def test_rules_reject_non_strings():
    payload = global_payload()
    for value in (None, 5, True, [], {}):
        payload["defaults"]["rules"] = value
        with pytest.raises(ValidationError, match="rules必须是字符串"):
            parse_global(payload, provider_ids=PROVIDERS)


def test_provider_id_must_be_known_or_empty():
    payload = global_payload()
    payload["defaults"]["provider_id"] = "prov-a"
    assert (
        parse_global(payload, provider_ids=PROVIDERS).defaults.provider_id == "prov-a"
    )
    payload["defaults"]["provider_id"] = ""
    assert parse_global(payload, provider_ids=PROVIDERS).defaults.provider_id == ""

    payload["defaults"]["provider_id"] = "missing"
    with pytest.raises(ValidationError, match="不是当前可用的模型提供商"):
        parse_global(payload, provider_ids=PROVIDERS)

    payload["defaults"]["provider_id"] = "p" * (v.MAX_PROVIDER_ID_CHARS + 1)
    with pytest.raises(ValidationError, match="不能超过"):
        parse_global(payload, provider_ids=PROVIDERS)


def test_provider_id_is_checked_inside_overrides():
    with pytest.raises(ValidationError, match="overrides.provider_id"):
        parse_forum_draft(
            forum_payload(overrides={"provider_id": "nope"}),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )


# --- list handling ----------------------------------------------------------


def test_string_lists_are_stripped_deduplicated_and_ordered():
    payload = global_payload(sessions=[" b ", "a", "b", "a "], operators=[" 1 ", "1"])
    parsed = parse_global(payload, provider_ids=PROVIDERS)
    assert parsed.sessions == ("b", "a")
    assert parsed.operators == frozenset({"1"})


def test_string_lists_reject_non_arrays():
    for field in ("sessions", "operators"):
        for value in (None, "x", 5, {}, True):
            with pytest.raises(ValidationError, match=f"{field}必须是数组"):
                parse_global(global_payload(**{field: value}), provider_ids=PROVIDERS)


def test_string_list_length_is_checked_before_items():
    # A huge array must be rejected without iterating it.
    payload = global_payload(sessions=["x"] * (v.MAX_GLOBAL_SESSIONS + 1))
    with pytest.raises(ValidationError, match="sessions最多 20 条"):
        parse_global(payload, provider_ids=PROVIDERS)


def test_string_list_item_types_are_strict():
    for value in (5, None, True, [], {}):
        with pytest.raises(ValidationError, match="sessions必须是字符串"):
            parse_global(global_payload(sessions=[value]), provider_ids=PROVIDERS)


def test_string_list_item_bounds():
    with pytest.raises(ValidationError, match="不能为空"):
        parse_global(global_payload(sessions=["   "]), provider_ids=PROVIDERS)
    with pytest.raises(ValidationError, match="不能超过 512"):
        parse_global(
            global_payload(sessions=["x" * (v.SESSION_CHARS_MAX + 1)]),
            provider_ids=PROVIDERS,
        )
    assert parse_global(
        global_payload(sessions=["x" * v.SESSION_CHARS_MAX]), provider_ids=PROVIDERS
    ).sessions == ("x" * v.SESSION_CHARS_MAX,)


def test_operators_reject_whitespace_and_controls():
    for value in ("a b", "a\tb", "a\x00", "a\u200b", "a\nb"):
        with pytest.raises(ValidationError, match="operators"):
            parse_global(global_payload(operators=[value]), provider_ids=PROVIDERS)


def test_operators_accept_plain_ids():
    parsed = parse_global(
        global_payload(operators=["12345", "qq:67890"]), provider_ids=PROVIDERS
    )
    assert parsed.operators == frozenset({"12345", "qq:67890"})


def test_whitelist_is_a_set_in_options():
    payload = global_payload()
    payload["defaults"]["whitelist"] = ["tb.1.a", " tb.1.a ", "tb.1.b"]
    parsed = parse_global(payload, provider_ids=PROVIDERS)
    assert parsed.defaults.whitelist == frozenset({"tb.1.a", "tb.1.b"})


def test_whitelist_item_bounds():
    payload = global_payload()
    payload["defaults"]["whitelist"] = ["x" * (v.MAX_WHITELIST_CHARS + 1)]
    with pytest.raises(ValidationError, match="不能超过 128"):
        parse_global(payload, provider_ids=PROVIDERS)
    payload["defaults"]["whitelist"] = [""]
    with pytest.raises(ValidationError, match="不能为空"):
        parse_global(payload, provider_ids=PROVIDERS)


# --- forums -----------------------------------------------------------------


@pytest.mark.parametrize("fname", [" Dragon ", "龙", "a-b_c"])
def test_forum_name_is_stripped_and_kept(fname):
    draft = parse_forum_draft(
        forum_payload(fname=fname), provider_ids=PROVIDERS, account_ids=ACCOUNTS
    )
    assert draft.fname == fname.strip()


@pytest.mark.parametrize("fname", ["", "   "])
def test_forum_name_cannot_be_blank(fname):
    with pytest.raises(ValidationError, match="fname不能为空"):
        parse_forum_draft(
            forum_payload(fname=fname), provider_ids=PROVIDERS, account_ids=ACCOUNTS
        )


def test_forum_name_max_length():
    ok = "x" * v.MAX_FORUM_NAME_CHARS
    assert (
        parse_forum_draft(
            forum_payload(fname=f"  {ok}  "),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        ).fname
        == ok
    )


def test_forum_name_rejects_controls_and_overlong():
    for fname in ("x" * (v.MAX_FORUM_NAME_CHARS + 1), "a\x00b", "a\u200bb", "a\nb"):
        with pytest.raises(ValidationError):
            parse_forum_draft(
                forum_payload(fname=fname),
                provider_ids=PROVIDERS,
                account_ids=ACCOUNTS,
            )


def test_forum_name_keeps_a_trailing_ba_suffix():
    # The contract says to warn but not rewrite; validation must not strip it.
    draft = parse_forum_draft(
        forum_payload(fname="龙吧"), provider_ids=PROVIDERS, account_ids=ACCOUNTS
    )
    assert draft.fname == "龙吧"


@pytest.mark.parametrize("account_id", [1, 2, None])
def test_forum_account_id_valid_values(account_id):
    draft = parse_forum_draft(
        forum_payload(account_id=account_id),
        provider_ids=PROVIDERS,
        account_ids=ACCOUNTS,
    )
    assert draft.account_id == account_id


@pytest.mark.parametrize("account_id", [0, -1, 3, "1", 1.0, True])
def test_forum_account_id_rejects_invalid_values(account_id):
    with pytest.raises(ValidationError, match="account_id"):
        parse_forum_draft(
            forum_payload(account_id=account_id),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )


def test_forum_overrides_can_be_absent_or_null():
    for value in ({}, None):
        draft = parse_forum_draft(
            forum_payload(overrides=value),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )
        assert draft.overrides == ForumOverrides()


def test_forum_overrides_reject_non_objects():
    for value in ("x", 5, [], True):
        with pytest.raises(ValidationError, match="overrides必须是 JSON 对象"):
            parse_forum_draft(
                forum_payload(overrides=value),
                provider_ids=PROVIDERS,
                account_ids=ACCOUNTS,
            )


def test_empty_rules_override_becomes_none():
    draft = parse_forum_draft(
        forum_payload(overrides={"rules": "   "}),
        provider_ids=PROVIDERS,
        account_ids=ACCOUNTS,
    )
    assert draft.overrides.rules is None


def test_empty_provider_override_becomes_none():
    draft = parse_forum_draft(
        forum_payload(overrides={"provider_id": ""}),
        provider_ids=PROVIDERS,
        account_ids=ACCOUNTS,
    )
    assert draft.overrides.provider_id is None


def test_overrides_keep_explicit_values():
    draft = parse_forum_draft(
        forum_payload(
            overrides={
                "rules": " 本吧规则 ",
                "provider_id": "prov-b",
                "auto_delete_enabled": True,
                "auto_delete_threshold": 0.8,
                "report_threshold": 0.5,
                "thread_pages": 3,
                "lookback_hours": 24,
                "review_posts": False,
                "review_comments": False,
                "skip_top_and_good": False,
                "skip_bawu": False,
            }
        ),
        provider_ids=PROVIDERS,
        account_ids=ACCOUNTS,
    )
    assert draft.overrides == ForumOverrides(
        rules="本吧规则",
        provider_id="prov-b",
        auto_delete_enabled=True,
        auto_delete_threshold=0.8,
        report_threshold=0.5,
        thread_pages=3,
        lookback_hours=24,
        review_posts=False,
        review_comments=False,
        skip_top_and_good=False,
        skip_bawu=False,
    )


def test_override_null_means_inherit():
    draft = parse_forum_draft(
        forum_payload(
            overrides={
                "rules": None,
                "provider_id": None,
                "auto_delete_enabled": None,
                "auto_delete_threshold": None,
                "report_threshold": None,
                "thread_pages": None,
                "lookback_hours": None,
                "review_posts": None,
                "review_comments": None,
                "skip_top_and_good": None,
                "skip_bawu": None,
            }
        ),
        provider_ids=PROVIDERS,
        account_ids=ACCOUNTS,
    )
    assert draft.overrides == ForumOverrides()


def test_forum_lists_are_validated_like_global_ones():
    with pytest.raises(ValidationError, match="operators最多"):
        parse_forum_draft(
            forum_payload(operators=["x"] * (v.MAX_FORUM_OPERATORS + 1)),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )
    with pytest.raises(ValidationError, match="sessions最多"):
        parse_forum_draft(
            forum_payload(sessions=["x"] * (v.MAX_FORUM_SESSIONS + 1)),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )


# --- accounts ---------------------------------------------------------------

BDUSS = "b" * 192
STOKEN = "s" * 64


def test_account_create_accepts_valid_credentials():
    draft = parse_account_draft(
        {"name": " 主号 ", "bduss": BDUSS, "stoken": STOKEN}, creating=True
    )
    assert draft == AccountDraft(name="主号", bduss=BDUSS, stoken=STOKEN)


@pytest.mark.parametrize(
    "name,ok",
    [
        ("a", True),
        ("a" * 32, True),
        ("a" * 33, False),
        ("", False),
        ("   ", False),
        ("a\nb", False),
        ("a\x00", False),
    ],
)
def test_account_name_bounds(name, ok):
    payload = {"name": name, "bduss": BDUSS, "stoken": STOKEN}
    if ok:
        assert parse_account_draft(payload, creating=True).name == name.strip()
    else:
        with pytest.raises(ValidationError, match="name"):
            parse_account_draft(payload, creating=True)


@pytest.mark.parametrize(
    "field,max_chars",
    [("bduss", 512), ("stoken", 256)],
)
def test_credential_length_bounds(field, max_chars):
    payload = {"name": "a", "bduss": BDUSS, "stoken": STOKEN}
    payload[field] = "x" * max_chars
    assert (
        getattr(parse_account_draft(payload, creating=True), field) == "x" * max_chars
    )
    payload[field] = "x" * (max_chars + 1)
    with pytest.raises(ValidationError, match=f"{field}长度超出上限"):
        parse_account_draft(payload, creating=True)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("bduss", "has space"),
        ("bduss", "tab\there"),
        ("bduss", "new\nline"),
        ("bduss", "中文"),
        ("bduss", "zw\u200bsp"),
        ("stoken", "a b"),
        ("stoken", "café"),
    ],
)
def test_credentials_must_be_printable_ascii_without_whitespace(field, bad):
    payload = {"name": "a", "bduss": BDUSS, "stoken": STOKEN}
    payload[field] = bad
    with pytest.raises(ValidationError) as info:
        parse_account_draft(payload, creating=True)
    assert bad not in str(info.value)


def test_credentials_reject_non_strings():
    for value in (5, True, [], {}):
        with pytest.raises(ValidationError, match="bduss必须是字符串"):
            parse_account_draft(
                {"name": "a", "bduss": value, "stoken": STOKEN}, creating=True
            )


def test_create_requires_nonempty_credentials():
    for value in (None, ""):
        with pytest.raises(ValidationError, match="bduss不能为空"):
            parse_account_draft(
                {"name": "a", "bduss": value, "stoken": STOKEN}, creating=True
            )
        with pytest.raises(ValidationError, match="stoken不能为空"):
            parse_account_draft(
                {"name": "a", "bduss": BDUSS, "stoken": value}, creating=True
            )


@pytest.mark.parametrize("value", [None, ""])
def test_update_keeps_credentials_when_omitted_or_empty(value):
    draft = parse_account_draft(
        {"name": "a", "bduss": value, "stoken": value}, creating=False
    )
    assert draft.bduss is None and draft.stoken is None


def test_update_accepts_a_name_only_payload():
    draft = parse_account_draft({"name": "renamed"}, creating=False)
    assert draft.name == "renamed"
    assert draft.bduss is None and draft.stoken is None


def test_update_replaces_credentials_when_given():
    draft = parse_account_draft(
        {"name": "a", "bduss": "new-bduss", "stoken": "new-stoken"}, creating=False
    )
    assert (draft.bduss, draft.stoken) == ("new-bduss", "new-stoken")


# --- credential secrecy -----------------------------------------------------


SECRETS = ("super-secret-bduss-value", "super-secret-stoken-value")


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "a", "bduss": SECRETS[0] + " ", "stoken": SECRETS[1]},
        {"name": "a", "bduss": SECRETS[0] + "\n", "stoken": SECRETS[1]},
        {"name": "a", "bduss": "x" * 600 + SECRETS[0], "stoken": SECRETS[1]},
        {"name": "a", "bduss": SECRETS[0], "stoken": SECRETS[1], "extra": 1},
        {"name": SECRETS[0] * 3, "bduss": SECRETS[0], "stoken": SECRETS[1]},
        {"name": "", "bduss": SECRETS[0], "stoken": SECRETS[1]},
    ],
)
def test_validation_errors_never_contain_a_credential(payload):
    with pytest.raises(ValidationError) as info:
        parse_account_draft(payload, creating=True)
    text = str(info.value)
    assert SECRETS[0] not in text
    assert SECRETS[1] not in text


def test_credential_error_does_not_leak_a_prefix():
    secret = "ABCDEFGHIJKLMNOP"
    with pytest.raises(ValidationError) as info:
        parse_account_draft(
            {"name": "a", "bduss": secret + " ", "stoken": STOKEN}, creating=True
        )
    assert secret[:4] not in str(info.value)


# --- full objects round-trip ------------------------------------------------


def test_global_parses_into_frozen_settings():
    parsed = parse_global(global_payload(), provider_ids=PROVIDERS)
    assert isinstance(parsed, GlobalSettings)
    assert isinstance(parsed.defaults, ForumOptions)
    assert parsed.sessions == ()
    assert parsed.defaults.provider_id == ""
    with pytest.raises(Exception):
        parsed.interval_seconds = 5  # type: ignore[misc]


def test_forum_parses_into_frozen_draft():
    draft = parse_forum_draft(
        forum_payload(), provider_ids=PROVIDERS, account_ids=ACCOUNTS
    )
    assert isinstance(draft, ForumDraft)
    with pytest.raises(Exception):
        draft.fname = "x"  # type: ignore[misc]


def test_whitespace_only_tab_session_is_rejected():
    with pytest.raises(ValidationError):
        parse_global(global_payload(sessions=["a\tb"]), provider_ids=PROVIDERS)


def test_non_breaking_space_in_a_session_is_rejected():
    with pytest.raises(ValidationError, match="控制字符"):
        parse_global(global_payload(sessions=["a\u00a0b"]), provider_ids=PROVIDERS)


# --- remaining branches -----------------------------------------------------


@pytest.mark.parametrize("value", [True, "2", 2.5])
def test_override_integers_reject_wrong_types(value):
    for field in ("thread_pages", "lookback_hours"):
        with pytest.raises(ValidationError, match=f"overrides.{field}必须是"):
            parse_forum_draft(
                forum_payload(overrides={field: value}),
                provider_ids=PROVIDERS,
                account_ids=ACCOUNTS,
            )


@pytest.mark.parametrize("value", [0, 169])
def test_lookback_override_rejects_out_of_range(value):
    with pytest.raises(ValidationError, match="overrides.lookback_hours必须是"):
        parse_forum_draft(
            forum_payload(overrides={"lookback_hours": value}),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )


@pytest.mark.parametrize("value", [0, 6])
def test_thread_pages_override_rejects_out_of_range(value):
    with pytest.raises(ValidationError, match="overrides.thread_pages必须是"):
        parse_forum_draft(
            forum_payload(overrides={"thread_pages": value}),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )


@pytest.mark.parametrize(
    "field,value", [("thread_pages", 5), ("lookback_hours", 168), ("thread_pages", 1)]
)
def test_override_integers_accept_the_bounds(field, value):
    draft = parse_forum_draft(
        forum_payload(overrides={field: value}),
        provider_ids=PROVIDERS,
        account_ids=ACCOUNTS,
    )
    assert getattr(draft.overrides, field) == value


@pytest.mark.parametrize(
    "ch",
    ["\ufeff", "\u2066", "\u202e", "\u200b", "\x7f", "\x9f", "\x1f", "\u00a0"],
)
def test_control_detection_covers_every_range(ch):
    payload = global_payload()
    payload["defaults"]["rules"] = f"x{ch}y"
    with pytest.raises(ValidationError, match="控制字符"):
        parse_global(payload, provider_ids=PROVIDERS)


def test_non_string_override_booleans_are_rejected():
    with pytest.raises(ValidationError, match="auto_delete_enabled"):
        parse_forum_draft(
            forum_payload(overrides={"auto_delete_enabled": 0}),
            provider_ids=PROVIDERS,
            account_ids=ACCOUNTS,
        )
