import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from astrbot_plugin_tieba_moderator.core.models import (
    Author,
    ContentItem,
    ItemKind,
    JudgeError,
    Verdict,
)
from astrbot_plugin_tieba_moderator.core.settings import (
    ForumEntry,
    ForumOptions,
    ForumOverrides,
    GlobalSettings,
    SettingsSnapshot,
)
from astrbot_plugin_tieba_moderator.services import llm_judge
from astrbot_plugin_tieba_moderator.services.llm_judge import LlmJudge

from astrbot.core.exceptions import ProviderNotFoundError

GOOD_JSON = (
    '{"violation": true, "category": "广告引流", "confidence": 0.9, '
    '"reason": "留联系方式", "ban_days": 3}'
)


class FakeContext:
    def __init__(self, *, result=None, error=None, delay=0.0, default_provider=None):
        self.result = result
        self.error = error
        self.delay = delay
        self.default_provider = default_provider
        self.calls: list[dict] = []

    def get_using_provider(self, umo=None):
        return self.default_provider

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


def response(text: str, role: str = "assistant"):
    return SimpleNamespace(role=role, completion_text=text)


def make_item(images: tuple[str, ...] = ()) -> ContentItem:
    return ContentItem(
        kind=ItemKind.THREAD,
        fname="测试",
        fid=1,
        tid=10,
        pid=11,
        author=Author(user_id=1, portrait="p", nick_name="nick"),
        title="标题",
        text="加我微信",
        image_urls=images,
        floor=1,
    )


def make_judge(context, **overrides) -> LlmJudge:
    forum_rules = overrides.pop("forum_rules", {})
    defaults = ForumOptions(
        provider_id=overrides.pop("provider_id", "vision"),
        rules=overrides.pop("rules", "禁止广告"),
    )
    global_ = GlobalSettings(
        defaults=defaults,
        judge_timeout_seconds=overrides.pop("timeout_seconds", 5),
        **overrides,
    )
    snapshot = SettingsSnapshot(
        global_=global_,
        forums=tuple(
            ForumEntry(
                id=index,
                fname=fname.strip(),
                overrides=ForumOverrides(rules=rules),
                created_at=0,
                updated_at=0,
            )
            for index, (fname, rules) in enumerate(forum_rules.items(), 1)
        ),
    )
    settings = Mock(snapshot=Mock(return_value=snapshot))
    return LlmJudge(context, settings=settings)


@pytest.mark.asyncio
async def test_judge_forwards_prompts_and_images():
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(ctx)
    urls = ["data:image/png;base64,AAA", "data:image/png;base64,BBB"]

    verdict = await judge(make_item(("u1", "u2")), urls)

    assert verdict == Verdict(
        violation=True,
        category="广告引流",
        confidence=0.9,
        reason="留联系方式",
        ban_days=3,
    )
    call = ctx.calls[0]
    assert call["chat_provider_id"] == "vision"
    assert call["image_urls"] == urls
    assert "禁止广告" in call["system_prompt"]
    assert "加我微信" in call["prompt"]
    assert "附带 2 张图片" in call["prompt"]


@pytest.mark.asyncio
async def test_judge_caches_prompts_by_rule_text(monkeypatch):
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(
        ctx,
        forum_rules={
            " Python ": "只讨论编程",
            "游戏": "禁止剧透",
            "Straße": "只用德语",
        },
    )

    builder = Mock(wraps=llm_judge.build_system_prompt)
    monkeypatch.setattr(llm_judge, "build_system_prompt", builder)
    for fname in (" pYtHoN ", "游戏", " STRASSE ", "未知", "Python"):
        await judge(replace(make_item(), fname=fname), [])

    prompts = [call["system_prompt"] for call in ctx.calls]
    expected_rules = ("只讨论编程", "禁止剧透", "只用德语", "禁止广告", "只讨论编程")
    all_rules = {"只讨论编程", "禁止剧透", "只用德语", "禁止广告"}
    for prompt, rules in zip(prompts, expected_rules):
        assert rules in prompt
        assert all(other not in prompt for other in all_rules - {rules})
    assert prompts[0] is prompts[4]
    assert {call.args[0] for call in builder.call_args_list} == all_rules
    assert builder.call_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("rules", ["", " \t\n "])
@pytest.mark.parametrize(
    "fname, expected", [(" PYTHON ", "只讨论编程"), ("未知", "禁止广告")]
)
async def test_blank_temporary_rules_use_item_forum(rules, fname, expected):
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(ctx, forum_rules={"python": "只讨论编程"})
    item = replace(make_item(), fname=fname)

    await judge(item, [])
    await judge.judge_with_rules(item, [], rules)

    assert expected in ctx.calls[1]["system_prompt"]
    assert ctx.calls[0]["system_prompt"] is ctx.calls[1]["system_prompt"]


@pytest.mark.asyncio
async def test_temporary_forum_rules_do_not_leak_into_later_calls():
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(ctx, forum_rules={"python": "只讨论编程", "游戏": "禁止剧透"})
    items = [replace(make_item(), fname=fname) for fname in ("Python", "游戏", "未知")]
    for item in items:
        await judge(item, [])
    configured_prompts = [call["system_prompt"] for call in ctx.calls]

    await judge.judge_with_rules(items[0], [], "临时吧规：允许交易")
    temporary_prompt = ctx.calls[3]["system_prompt"]
    assert "临时吧规：允许交易" in temporary_prompt
    assert all(
        rules not in temporary_prompt
        for rules in ("只讨论编程", "禁止剧透", "禁止广告")
    )

    for item in items:
        await judge(item, [])
        await judge.judge_with_rules(item, [], " ")
    assert [call["system_prompt"] for call in ctx.calls[4:]] == [
        prompt for prompt in configured_prompts for _ in range(2)
    ]


@pytest.mark.asyncio
async def test_judge_without_images_passes_none():
    ctx = FakeContext(result=response(GOOD_JSON))
    await make_judge(ctx)(make_item(), [])
    assert ctx.calls[0]["image_urls"] is None


@pytest.mark.asyncio
async def test_judge_reports_images_beyond_limit_as_skipped():
    ctx = FakeContext(result=response(GOOD_JSON))
    item = make_item(tuple(f"u{i}" for i in range(6)))
    await make_judge(ctx)(item, ["data:a"] * 4)
    assert "另有 2 张图片没有提供给你审查" in ctx.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_judge_reports_images_skipped_when_review_disabled():
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(ctx, review_images=False)
    await judge(make_item(("u1", "u2")), [])
    assert "另有 2 张图片没有提供给你审查" in ctx.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_judge_refuses_when_expected_images_missing():
    ctx = FakeContext(result=response(GOOD_JSON))
    with pytest.raises(JudgeError, match="图片"):
        await make_judge(ctx)(make_item(("u1", "u2")), ["data:a"])
    assert ctx.calls == []


@pytest.mark.asyncio
async def test_judge_falls_back_to_default_provider():
    provider = SimpleNamespace(meta=lambda: SimpleNamespace(id="default-chat"))
    ctx = FakeContext(result=response(GOOD_JSON), default_provider=provider)
    await make_judge(ctx, provider_id="")(make_item(), [])
    assert ctx.calls[0]["chat_provider_id"] == "default-chat"


@pytest.mark.asyncio
async def test_judge_raises_when_no_provider_at_all():
    ctx = FakeContext(result=response(GOOD_JSON))
    with pytest.raises(JudgeError, match="模型提供商"):
        await make_judge(ctx, provider_id="")(make_item(), [])
    assert ctx.calls == []


@pytest.mark.asyncio
async def test_judge_wraps_provider_not_found():
    ctx = FakeContext(error=ProviderNotFoundError("Provider vision not found"))
    with pytest.raises(JudgeError, match="找不到模型提供商 vision"):
        await make_judge(ctx)(make_item(), [])


@pytest.mark.asyncio
async def test_judge_wraps_timeout():
    ctx = FakeContext(result=response(GOOD_JSON), delay=1)
    with pytest.raises(JudgeError, match="超时"):
        await make_judge(ctx, timeout_seconds=0.01)(make_item(), [])


@pytest.mark.asyncio
async def test_judge_wraps_exception_without_leaking_message():
    ctx = FakeContext(error=RuntimeError("secret-api-key-in-message"))
    with pytest.raises(JudgeError) as info:
        await make_judge(ctx)(make_item(), [])
    assert "RuntimeError" in str(info.value)
    assert "secret-api-key" not in str(info.value)


@pytest.mark.asyncio
async def test_judge_wraps_error_response():
    ctx = FakeContext(result=response("upstream 500", role="err"))
    with pytest.raises(JudgeError, match="错误"):
        await make_judge(ctx)(make_item(), [])


@pytest.mark.asyncio
async def test_judge_wraps_none_response():
    ctx = FakeContext(result=None)
    with pytest.raises(JudgeError):
        await make_judge(ctx)(make_item(), [])


@pytest.mark.asyncio
async def test_judge_wraps_unparsable_output():
    ctx = FakeContext(result=response('{"violation": "false", "confidence": 0.1}'))
    with pytest.raises(JudgeError, match="布尔"):
        await make_judge(ctx)(make_item(), [])


@pytest.mark.asyncio
async def test_judge_handles_empty_completion():
    ctx = FakeContext(result=response(None))  # type: ignore[arg-type]
    with pytest.raises(JudgeError, match="为空"):
        await make_judge(ctx)(make_item(), [])


# --- temporary rules (WebUI trial review) -----------------------------------


@pytest.mark.asyncio
async def test_judge_with_rules_uses_temporary_rules_and_keeps_instance_state():
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(ctx)
    configured_snapshot = judge._settings.snapshot()

    verdict = await judge.judge_with_rules(make_item(), [], "临时吧规：允许交易")

    assert verdict.violation is True
    first = ctx.calls[0]["system_prompt"]
    assert "临时吧规：允许交易" in first
    assert "禁止广告" not in first
    assert judge._settings.snapshot() is configured_snapshot

    # The configured rules still apply to the next patrol call.
    await judge(make_item(), [])
    assert "禁止广告" in ctx.calls[1]["system_prompt"]


@pytest.mark.asyncio
async def test_judge_with_rules_falls_back_to_configured_rules():
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(ctx)

    await judge.judge_with_rules(make_item(), [], "   ")

    assert "禁止广告" in ctx.calls[0]["system_prompt"]


@pytest.mark.asyncio
async def test_judge_with_rules_keeps_the_missing_image_guard():
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(ctx)

    with pytest.raises(JudgeError, match="图片"):
        await judge.judge_with_rules(make_item(("u1",)), [], "规则")
    assert ctx.calls == []


@pytest.mark.asyncio
async def test_judge_with_rules_wraps_provider_failures():
    ctx = FakeContext(result=response(GOOD_JSON))
    ctx.error = RuntimeError("secret-api-key rejected")
    judge = make_judge(ctx)

    with pytest.raises(JudgeError) as info:
        await judge.judge_with_rules(make_item(), [], "规则")
    assert "secret-api-key" not in str(info.value)


def test_temporary_rules_limit_is_documented():
    assert llm_judge.MAX_TEMP_RULES_CHARS == 8000


@pytest.mark.asyncio
async def test_live_rules_models_and_explicit_round_snapshot(monkeypatch):
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(ctx, forum_rules={"测试": "旧吧规", "共享": "旧吧规"})
    original = judge._settings.snapshot()
    builder = Mock(wraps=llm_judge.build_system_prompt)
    monkeypatch.setattr(llm_judge, "build_system_prompt", builder)
    await judge(make_item(), [])
    await judge(replace(make_item(), fname="共享"), [])
    assert builder.call_count == 1
    changed = replace(
        original,
        forums=(
            replace(
                original.forums[0],
                overrides=ForumOverrides(rules="新吧规", provider_id="new-model"),
            ),
        ),
    )
    judge._settings.snapshot.return_value = changed
    await judge(make_item(), [])
    await judge(make_item(), [], snapshot=original)
    await judge.judge_with_rules(make_item(), [], "临时规则")
    await judge.judge_with_rules(make_item(), [], " ")
    assert [call["chat_provider_id"] for call in ctx.calls] == [
        "vision", "vision", "new-model", "vision", "new-model", "new-model"
    ]
    assert "新吧规" in ctx.calls[2]["system_prompt"]
    assert ctx.calls[0]["system_prompt"] is ctx.calls[3]["system_prompt"]
    assert "临时规则" in ctx.calls[4]["system_prompt"]
    assert ctx.calls[2]["system_prompt"] is ctx.calls[5]["system_prompt"]
    assert builder.call_count == 3


@pytest.mark.asyncio
async def test_live_image_limits_and_timeout_keep_round_override():
    ctx = FakeContext(result=response(GOOD_JSON))
    judge = make_judge(ctx)
    original = judge._settings.snapshot()
    changed = replace(
        original,
        global_=replace(
            original.global_, max_images=1, judge_timeout_seconds=0.01
        ),
    )
    judge._settings.snapshot.return_value = changed
    item = make_item(("one", "two"))
    await judge(item, ["data:a"])
    with pytest.raises(JudgeError, match="图片"):
        await judge(item, ["data:a"], snapshot=original)
    ctx.delay = 0.02
    with pytest.raises(JudgeError, match="超时"):
        await judge(make_item(), [])
    await judge(make_item(), [], snapshot=original)
    judge._settings.snapshot.return_value = replace(
        changed, global_=replace(changed.global_, review_images=False)
    )
    ctx.delay = 0
    await judge(item, [])
