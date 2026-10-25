"""LLM judge backed by an AstrBot chat provider; implements ``JudgeFn``."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.exceptions import ProviderNotFoundError

from ..core.judge import build_system_prompt, build_user_prompt, parse_verdict
from ..core.models import ContentItem, JudgeError, Verdict

MAX_TEMP_RULES_CHARS = 8000
"""Longest temporary rule text accepted by the WebUI trial review."""


class LlmJudge:
    """Judge one item with a multimodal chat provider.

    Every failure (no provider, timeout, provider error, unparsable output) is
    raised as ``JudgeError`` so the patrol can count it and retry later; a
    failure never turns into a "no violation" verdict.
    """

    def __init__(
        self,
        context: Context,
        *,
        provider_id: str,
        rules: str,
        timeout_seconds: float,
        review_images: bool,
        max_images: int,
        forum_rules: Mapping[str, str] | None = None,
    ) -> None:
        """Create the judge.

        Args:
            context: The AstrBot star context.
            provider_id: Chat provider id; empty means the default chat provider.
            rules: Forum rules written by the moderators.
            timeout_seconds: Timeout for one LLM call.
            review_images: Whether images are supposed to be reviewed.
            max_images: Maximum images per item when reviewing images.
            forum_rules: Rules by forum name; unknown forums use ``rules``.
        """
        self._context = context
        self._provider_id = provider_id
        self._system_prompt = build_system_prompt(rules)
        self._forum_prompts = {
            fname.strip().casefold(): build_system_prompt(forum_rule)
            for fname, forum_rule in (forum_rules or {}).items()
        }
        self._timeout = timeout_seconds
        self._review_images = review_images
        self._max_images = max_images

    async def __call__(self, item: ContentItem, image_data_urls: list[str]) -> Verdict:
        """Judge one item with the configured forum rules.

        Args:
            item: The item to judge.
            image_data_urls: Images already downloaded by the patrol, as data URLs.

        Returns:
            The parsed verdict.

        Raises:
            JudgeError: Provider missing, images missing, timeout, provider error
                or unparsable output.
        """
        system_prompt = self._forum_prompts.get(
            item.fname.strip().casefold(), self._system_prompt
        )
        return await self._judge(item, image_data_urls, system_prompt)

    async def judge_with_rules(
        self, item: ContentItem, image_data_urls: list[str], rules: str
    ) -> Verdict:
        """Judge one item with temporary rules, without changing this instance.

        Used by the WebUI trial review: the moderators can try a candidate rule
        text without saving it. The temporary text is as trusted as the
        configured rules (only a dashboard user can submit it), so it goes into
        the system prompt; the length limit is enforced by the caller.

        Args:
            item: The item to judge.
            image_data_urls: Downloaded images as data URLs.
            rules: Temporary rule text; empty or whitespace-only text falls back
                to the item's configured forum rules.

        Returns:
            The parsed verdict.

        Raises:
            JudgeError: Same conditions as ``__call__``.
        """
        if not rules.strip():
            return await self(item, image_data_urls)
        return await self._judge(item, image_data_urls, build_system_prompt(rules))

    async def _judge(
        self, item: ContentItem, image_data_urls: list[str], system_prompt: str
    ) -> Verdict:
        """Run one judge call with an explicit system prompt.

        Args:
            item: The item to judge.
            image_data_urls: Downloaded images as data URLs.
            system_prompt: System prompt carrying the rules to apply.

        Returns:
            The parsed verdict.

        Raises:
            JudgeError: Provider missing, images missing, timeout, provider error
                or unparsable output.
        """
        expected = (
            min(len(item.image_urls), self._max_images) if self._review_images else 0
        )
        if len(image_data_urls) < expected:
            # Judging without the images would let image-only spam through.
            raise JudgeError(
                f"应审查 {expected} 张图片，实际只拿到 {len(image_data_urls)} 张"
            )

        provider_id = self._provider_id
        if not provider_id:
            provider = self._context.get_using_provider()
            if provider is None:
                raise JudgeError("没有配置审核用的模型提供商，也没有默认对话模型")
            provider_id = provider.meta().id

        prompt = build_user_prompt(
            item,
            image_count=len(image_data_urls),
            images_skipped=max(0, len(item.image_urls) - len(image_data_urls)),
        )
        try:
            resp = await asyncio.wait_for(
                self._context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    image_urls=list(image_data_urls) or None,
                ),
                timeout=self._timeout,
            )
        except TimeoutError:
            raise JudgeError(f"LLM 调用超时（{self._timeout:g} 秒）") from None
        except ProviderNotFoundError:
            raise JudgeError(f"找不到模型提供商 {provider_id}") from None
        except Exception as exc:
            logger.warning(
                f"LLM judge call failed for {item.key}: {type(exc).__name__}: "
                f"{str(exc)[:300]}"
            )
            raise JudgeError(f"LLM 调用失败：{type(exc).__name__}") from exc

        if resp is None or resp.role == "err":
            detail = (resp.completion_text or "")[:300] if resp is not None else ""
            logger.warning(f"LLM judge returned an error for {item.key}: {detail}")
            raise JudgeError("LLM 返回了错误")
        return parse_verdict(resp.completion_text or "")
