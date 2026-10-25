import asyncio
from types import SimpleNamespace

import pytest
from astrbot.core.exceptions import ProviderNotFoundError

from astrbot_plugin_tieba_moderator.core.models import (
    Author,
    ContentItem,
    ItemKind,
    JudgeError,
    Verdict,
)
from astrbot_plugin_tieba_moderator.services.llm_judge import LlmJudge

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
    kwargs = dict(
        provider_id="vision",
        rules="禁止广告",
        timeout_seconds=5,
        review_images=True,
        max_images=4,
    )
    kwargs.update(overrides)
    return LlmJudge(context, **kwargs)


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
