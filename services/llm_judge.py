"""LLM judge backed by an AstrBot chat provider; implements ``JudgeFn``."""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.exceptions import ProviderNotFoundError

from ..core.judge import build_system_prompt, build_user_prompt, parse_verdict
from ..core.models import ContentItem, JudgeError, Verdict
from ..core.settings import SettingsSnapshot, resolve
from .settings_service import SettingsService

MAX_TEMP_RULES_CHARS = 8000
"""Longest temporary rule text accepted by the WebUI trial review."""


class LlmJudge:
    """Judge one item with a multimodal chat provider.

    Every failure (no provider, timeout, provider error, unparsable output) is
    raised as ``JudgeError`` so the patrol can count it and retry later; a
    failure never turns into a "no violation" verdict.
    """

    def __init__(self, context: Context, *, settings: SettingsService) -> None:
        """Use live settings, caching system prompts by their exact rule text.

        Args:
            context: The AstrBot star context.
            settings: Current immutable settings snapshots.
        """
        self._context = context
        self._settings = settings
        self._prompts: dict[str, str] = {}

    def _prompt(self, rules: str) -> str:
        """Build each distinct rule prompt only once."""
        if rules not in self._prompts:
            self._prompts[rules] = build_system_prompt(rules)
        return self._prompts[rules]

    async def __call__(
        self,
        item: ContentItem,
        image_data_urls: list[str],
        *,
        snapshot: SettingsSnapshot | None = None,
    ) -> Verdict:
        """Judge one item with the configured forum rules.

        Args:
            item: The item to judge.
            image_data_urls: Images already downloaded by the patrol, as data URLs.
            snapshot: Optional pinned patrol-round settings; otherwise use current.

        Returns:
            The parsed verdict.

        Raises:
            JudgeError: Provider missing, images missing, timeout, provider error
                or unparsable output.
        """
        current = snapshot if snapshot is not None else self._settings.snapshot()
        options = resolve(current, item.fname).options
        return await self._judge(
            item,
            image_data_urls,
            self._prompt(options.rules),
            current,
            options.provider_id,
        )

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
        snapshot = self._settings.snapshot()
        options = resolve(snapshot, item.fname).options
        return await self._judge(
            item,
            image_data_urls,
            self._prompt(rules if rules.strip() else options.rules),
            snapshot,
            options.provider_id,
        )

    async def _judge(
        self,
        item: ContentItem,
        image_data_urls: list[str],
        system_prompt: str,
        snapshot: SettingsSnapshot,
        provider_id: str,
    ) -> Verdict:
        """Run one judge call with an explicit system prompt.

        Args:
            item: The item to judge.
            image_data_urls: Downloaded images as data URLs.
            system_prompt: System prompt carrying the rules to apply.
            snapshot: Settings pinned for this call's timeout and image policy.
            provider_id: The resolved forum provider, or empty for the default.

        Returns:
            The parsed verdict.

        Raises:
            JudgeError: Provider missing, images missing, timeout, provider error
                or unparsable output.
        """
        settings = snapshot.global_
        expected = (
            min(len(item.image_urls), settings.max_images)
            if settings.review_images
            else 0
        )
        if len(image_data_urls) < expected:
            # Judging without the images would let image-only spam through.
            raise JudgeError(
                f"应审查 {expected} 张图片，实际只拿到 {len(image_data_urls)} 张"
            )

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
                timeout=settings.judge_timeout_seconds,
            )
        except TimeoutError:
            raise JudgeError(
                f"LLM 调用超时（{settings.judge_timeout_seconds:g} 秒）"
            ) from None
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
