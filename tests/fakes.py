"""Offline content and advancing-clock fixtures for patrol scenarios."""

from dataclasses import replace
from unittest.mock import AsyncMock

from astrbot_plugin_tieba_moderator.core.models import Author, ContentItem, ItemKind
from astrbot_plugin_tieba_moderator.services.tieba_gateway import (
    PostBatch,
    ThreadSnapshot,
)


class Clock:
    """Advance one second on every observation."""

    def __init__(self, now: int = 2_000_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        self.now += 1
        return float(self.now)


def make_item(**changes) -> ContentItem:
    """Build content with recent timestamps and synthetic identifiers."""
    return replace(
        ContentItem(
            kind=ItemKind.THREAD,
            fname="test",
            fid=1,
            tid=10,
            pid=100,
            author=Author(user_id=1, portrait="fake_portrait", user_name="reader"),
            title="Topic",
            text="Body",
            create_time=2_000_000_000,
            floor=1,
        ),
        **changes,
    )


def make_snapshot(**changes) -> ThreadSnapshot:
    """Build a thread-list row without contacting Tieba."""
    return replace(
        ThreadSnapshot(
            tid=10,
            fid=1,
            fname="test",
            title="Topic",
            last_time=2_000_000_000,
            create_time=2_000_000_000,
            reply_num=0,
            is_top=False,
            is_good=False,
            is_livepost=False,
            author=make_item().author,
        ),
        **changes,
    )


class _ReadMock(AsyncMock):
    """Charge one synthetic page per read while preserving mock configuration."""

    async def _execute_mock_call(self, *args, **kwargs):
        budget = kwargs.get("budget")
        if budget is not None:
            budget.consume()
        return await super()._execute_mock_call(*args, **kwargs)


class FakeGateway:
    """In-memory async endpoint substitutes; never construct a real client."""

    def __init__(self) -> None:
        self.list_threads = _ReadMock(return_value=[make_snapshot()])
        self.get_thread_item = _ReadMock(return_value=(make_item(), 0))
        self.get_thread_item_by_tid = AsyncMock(return_value=make_item())
        self.get_new_posts = _ReadMock(return_value=PostBatch())
        self.get_comments = _ReadMock(return_value=([], True))
        self.fetch_images = AsyncMock(return_value=["data:image/png;base64,ZmFrZQ=="])
        self.delete = AsyncMock()
        self.restore = AsyncMock()
        self.block = AsyncMock()
        self.close = AsyncMock()


class FakeEvent:
    """Command event with a local plain-text reply surface."""

    unified_msg_origin = "offline:group:test"

    def is_admin(self) -> bool:
        """Identify the synthetic operator as an administrator."""
        return True

    def get_sender_id(self) -> str:
        """Return the synthetic moderator identity."""
        return "offline-moderator"

    def plain_result(self, text: str) -> str:
        """Expose a reply for assertions without a messaging platform."""
        return text
