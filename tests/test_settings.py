"""Pure settings inheritance and lossless JSON storage codecs."""

import json
from dataclasses import FrozenInstanceError, fields, replace

import pytest
from astrbot_plugin_tieba_moderator.core.settings import (
    DEFAULT_RULES,
    ForumDraft,
    ForumEntry,
    ForumOptions,
    ForumOverrides,
    SettingsSnapshot,
    default_global_settings,
    forum_data_from_dict,
    forum_data_to_dict,
    global_from_dict,
    global_to_dict,
    resolve,
)


def test_defaults_match_documented_values_and_original_rules():
    value = default_global_settings()
    assert value.patrol_enabled and value.review_images
    assert (value.interval_seconds, value.max_items_per_round) == (180, 60)
    assert (value.judge_concurrency, value.judge_timeout_seconds, value.max_images) == (
        2, 60, 4
    )
    assert value.sessions == () and value.operators == frozenset()
    assert value.defaults == ForumOptions(rules=DEFAULT_RULES)
    assert DEFAULT_RULES == (
        "1. 广告与引流：推广商品、代练代充、刷粮、留微信 / QQ / 群号 / 外站链接或二维码引导私下联系，均属违规。\n"
        "2. 色情与擦边：露骨的性描写、色情图片、以性暗示为卖点的擦边图文、招嫖或色情网站引流，均属违规。\n"
        "3. 辱骂与引战：人身攻击、辱骂其他用户或群体、地域黑、故意挑起对立的钓鱼帖，均属违规。\n"
        "4. 违法内容：赌博、毒品、诈骗、贩卖个人信息、暴力恐怖、政治敏感等违反法律法规的内容，均属违规。\n"
        "5. 灌水与刷屏：无意义的重复内容、乱码、与本吧主题无关的刷屏，属于违规；正常的简短回复（如「顶」「谢谢楼主」）不算。\n"
        "6. 正常讨论、提问、吐槽、玩梗不算违规；拿不准时给较低的置信度。"
    )


def test_global_codec_roundtrip_preserves_all_nondefault_fields():
    value = replace(
        default_global_settings(),
        patrol_enabled=False,
        interval_seconds=86400,
        max_items_per_round=500,
        judge_concurrency=8,
        judge_timeout_seconds=600,
        review_images=False,
        max_images=0,
        sessions=("b:group:2", "a:group:1"),
        operators=frozenset({"z", "a"}),
        defaults=ForumOptions(
            rules="自定义 ' rules\n第二行",
            provider_id="model-id",
            auto_delete_enabled=True,
            auto_delete_threshold=0.7,
            report_threshold=0.2,
            thread_pages=5,
            lookback_hours=168,
            review_posts=False,
            review_comments=False,
            skip_top_and_good=False,
            skip_bawu=False,
            whitelist=frozenset({"z", "中文"}),
        ),
    )
    encoded = global_to_dict(value)
    assert encoded["sessions"] == ["b:group:2", "a:group:1"]
    assert encoded["operators"] == ["a", "z"]
    decoded = global_from_dict(json.loads(json.dumps(encoded)))
    assert decoded == value
    assert isinstance(decoded.sessions, tuple)
    assert isinstance(decoded.operators, frozenset)
    assert isinstance(decoded.defaults.whitelist, frozenset)
    with pytest.raises(FrozenInstanceError):
        decoded.defaults.rules = "changed"
    assert global_from_dict(global_to_dict(default_global_settings())) == default_global_settings()


@pytest.mark.parametrize("overrides", [ForumOverrides(), ForumOverrides(
    rules="本吧规则", provider_id="", auto_delete_enabled=False,
    auto_delete_threshold=0, report_threshold=0, thread_pages=2, lookback_hours=12,
    review_posts=False, review_comments=False, skip_top_and_good=False, skip_bawu=False,
)])
def test_forum_codec_preserves_none_false_zero_and_collections(overrides):
    draft = ForumDraft(
        fname="论坛", enabled=False, account_id=7, sessions=("s2", "s1"),
        operators=frozenset({"o2", "o1"}), whitelist=frozenset({"w2", "w1"}),
        overrides=overrides,
    )
    entry = ForumEntry(id=9, created_at=100, updated_at=200, **{
        field.name: getattr(draft, field.name) for field in fields(draft)
    })
    encoded = forum_data_to_dict(draft)
    assert encoded == forum_data_to_dict(entry)
    assert set(encoded) == {"enabled", "account_id", "sessions", "operators", "whitelist", "overrides"}
    restored = ForumDraft(fname=draft.fname, **forum_data_from_dict(json.loads(json.dumps(encoded))))
    assert restored == draft


def test_resolve_inherits_union_sessions_and_clamps_without_mutation():
    global_ = replace(default_global_settings(), sessions=("global",), defaults=ForumOptions(
        rules="global rules", provider_id="global-provider", auto_delete_enabled=True,
        auto_delete_threshold=0.4, report_threshold=0.3, whitelist=frozenset({"g", "both"}),
    ))
    forum = ForumEntry(
        id=1, fname="Straße", account_id=2, created_at=1, updated_at=1,
        operators=frozenset({"local"}), whitelist=frozenset({"f", "both"}),
        overrides=ForumOverrides(report_threshold=0.8, auto_delete_enabled=False, provider_id=""),
    )
    snapshot = SettingsSnapshot(global_=global_, forums=(forum,))
    resolved = resolve(snapshot, "  STRASSE  ")
    assert resolved.configured and resolved.enabled
    assert resolved.fname == "Straße" and resolved.account_id == 2
    assert resolved.sessions == ("global",) and resolved.operators == frozenset({"local"})
    assert resolved.options == replace(global_.defaults, whitelist=frozenset({"g", "f", "both"}),
                                       auto_delete_enabled=False, report_threshold=0.4, provider_id="")
    assert snapshot.forums[0].overrides.report_threshold == 0.8
    own = replace(forum, enabled=False, sessions=("own",), overrides=ForumOverrides(rules="own rules"))
    result = resolve(replace(snapshot, forums=(own,)), "Straße")
    assert not result.enabled and result.sessions == ("own",)
    assert result.options.rules == "own rules"


def test_unknown_forum_uses_safe_defaults_and_no_local_permissions():
    value = default_global_settings()
    value = replace(value, sessions=("global",), defaults=replace(value.defaults, report_threshold=1))
    resolved = resolve(SettingsSnapshot(global_=value), " Unknown ")
    assert resolved.fname == "Unknown"
    assert not resolved.configured and not resolved.enabled
    assert resolved.account_id is None and resolved.operators == frozenset()
    assert resolved.sessions == ("global",)
    assert resolved.options.report_threshold == value.defaults.auto_delete_threshold


@pytest.mark.parametrize("field", fields(ForumOverrides), ids=lambda field: field.name)
def test_every_override_inherits_only_on_none(field):
    value = default_global_settings()
    default = getattr(value.defaults, field.name)
    replacement = {
        "rules": "own", "provider_id": "provider", "auto_delete_threshold": 0.7,
        "report_threshold": 0.2, "thread_pages": 3, "lookback_hours": 12,
    }.get(field.name, not default)
    forum = ForumEntry(id=1, fname="论坛", created_at=0, updated_at=0,
                       overrides=ForumOverrides(**{field.name: replacement}))
    snapshot = SettingsSnapshot(global_=value, forums=(forum,))
    assert getattr(resolve(snapshot, "论坛").options, field.name) == replacement
    inherited = replace(forum, overrides=ForumOverrides())
    assert getattr(resolve(replace(snapshot, forums=(inherited,)), "论坛").options, field.name) == default
