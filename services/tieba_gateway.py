"""Translate aiotieba responses into immutable moderation content.

Source contracts were checked against aiotieba 4.8.1: response containers use
``err`` for failures, while their truth value only describes list emptiness.
ImageBytes stores downloaded bytes in ``data``; BoolResponse uses ``err`` for
its truth value. See aiotieba/exception.py and api/_classdef/container.py.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses as dcs
import urllib.parse
from collections.abc import Sequence
from typing import Any

import aiohttp
from aiotieba import Client
from aiotieba.api.get_posts._classdef import Thread_p
from aiotieba.enums import CommentSortType, PostSortType, ThreadSortType

from ..core.models import (
    ALLOWED_BAN_DAYS,
    Author,
    ContentItem,
    ImageFetchError,
    ItemKind,
    TiebaActionError,
)

_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_TIMEOUT_SECONDS = 20
_IMAGE_CHUNK_BYTES = 64 * 1024
_MAX_IMAGE_REDIRECTS = 3
_IMAGE_HEADERS = {"Referer": "https://tieba.baidu.com/"}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_IMAGE_HOST_SUFFIXES = ("baidu.com", "bdimg.com", "bdstatic.com", "baidupcs.com")
"""Image CDNs Tieba serves post images and video covers from."""


def _is_allowed_image_url(url: str) -> bool:
    """Whether an untrusted image URL points at a Tieba image CDN over HTTP(S).

    Image URLs come from post content, so the plugin must never fetch arbitrary
    hosts: that would turn a post into a server-side request forgery primitive.

    Args:
        url: Candidate image URL.

    Returns:
        True for ``http``/``https`` URLs whose host is one of
        ``_IMAGE_HOST_SUFFIXES`` (or a subdomain of one).
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = host.lower().rstrip(".")
    if not host or not all(
        ch.isascii() and (ch.isalnum() or ch in ".-") for ch in host
    ):
        return False
    return any(
        host == suffix or host.endswith(f".{suffix}") for suffix in _IMAGE_HOST_SUFFIXES
    )


def _new_image_session() -> aiohttp.ClientSession:
    """Create the HTTP session used to download images.

    Kept as a module function so tests can substitute it without opening
    sockets.

    Returns:
        A session for image downloads only.
    """
    return aiohttp.ClientSession()


async def _read_limited(content: Any) -> bytes:
    """Read a streamed response body, aborting past the size limit.

    Args:
        content: ``aiohttp`` stream reader with ``iter_chunked``.

    Returns:
        The full body.

    Raises:
        ImageFetchError: The body exceeds ``_MAX_IMAGE_BYTES``.
    """
    buffer = bytearray()
    async for chunk in content.iter_chunked(_IMAGE_CHUNK_BYTES):
        buffer.extend(chunk)
        if len(buffer) > _MAX_IMAGE_BYTES:
            raise ImageFetchError("Image exceeds the 5 MiB limit")
    return bytes(buffer)


class RequestBudgetExceeded(Exception):
    """Signal that a patrol round has no content-read attempts remaining."""


class ReadBudget:
    """Track logical content API attempts, excluding client setup and images.

    Args:
        limit: Maximum number of content API attempts for this round.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        """Return the number of attempts still available."""
        return max(0, self.limit - self.used)

    def consume(self) -> None:
        """Reserve one attempt, including attempts that subsequently fail.

        Raises:
            RequestBudgetExceeded: No attempt remains.
        """
        if not self.remaining:
            raise RequestBudgetExceeded("Tieba read request budget exhausted")
        self.used += 1


@dcs.dataclass(frozen=True, kw_only=True)
class PostBatch:
    """Separate new bodies from comment-parent evidence on fetched pages.

    ``pairs`` contains only bodies newer than the timestamp boundary.
    ``comment_parents`` also retains old boundary and pinned first floors for
    comment-count scans, never for reviewing their historical bodies.
    ``caught_up`` is false when the page cap leaves newer floors unread.
    """

    pairs: tuple[tuple[ContentItem, int], ...] = ()
    caught_up: bool = True
    comment_parents: tuple[tuple[ContentItem, int], ...] = ()


@dcs.dataclass(frozen=True, kw_only=True)
class ThreadSnapshot:
    """Forum-list metadata used to decide which threads need inspection."""

    tid: int
    fid: int
    fname: str
    title: str
    last_time: int
    create_time: int
    reply_num: int
    is_top: bool
    is_good: bool
    is_livepost: bool
    author: Author


def _author(user: Any) -> Author:
    return Author(
        user_id=user.user_id,
        portrait=user.portrait.partition("?t=")[0],
        user_name=user.user_name,
        nick_name=user.nick_name,
        level=user.level,
        is_bawu=user.is_bawu,
    )


def _content(
    source: Any,
    parent: ThreadSnapshot | ContentItem | Thread_p,
    kind: ItemKind,
    floor: int,
) -> ContentItem:
    text = []
    images = []
    has_video = False
    # Walk the mixed fragment sequence, not contents.text (which loses links).
    # Link.text is the URL; Link.title is the visible label. TiebaPlus instead
    # uses text for the label. Video.src is a movie, never a reviewable image.
    for fragment in source.contents:
        if hasattr(fragment, "cover_src"):
            has_video = True
            if fragment.cover_src:
                images.append(fragment.cover_src)
        elif hasattr(fragment, "origin_src"):
            if fragment.src:
                images.append(fragment.src)
        elif hasattr(fragment, "raw_url"):
            label = fragment.title or fragment.text
            try:
                url = fragment.url
            except Exception:
                # Malformed external-link wrappers may lack the url parameter.
                url = fragment.text
            text.append(f"{label}({url})")
        elif hasattr(fragment, "url"):
            label = getattr(fragment, "title", "") or fragment.text
            text.append(f"{label}({fragment.url})")
        elif hasattr(fragment, "text"):
            text.append(fragment.text)
    return ContentItem(
        kind=kind,
        fname=parent.fname,
        fid=parent.fid,
        tid=parent.tid,
        pid=source.pid,
        author=_author(source.user),
        title=parent.title,
        text="".join(text),
        image_urls=tuple(images),
        create_time=source.create_time,
        floor=floor,
        has_video=has_video,
    )


def _failure(operation: str, error: Any) -> str:
    """Keep the server code and message when aiotieba wraps a rejection."""
    code = getattr(error, "code", "unknown")
    message = getattr(error, "msg", str(error))
    return f"{operation} failed: code={code}, errmsg={message}"


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    # JPEG magic and unrecognized formats both use the required JPEG fallback.
    return "jpeg"


class TiebaGateway:
    """Own a lazily entered, long-lived aiotieba client.

    Args:
        bduss: Moderator account BDUSS.
        stoken: Moderator account STOKEN.
    """

    def __init__(self, bduss: str, stoken: str) -> None:
        self._client = Client(bduss, stoken)
        self._lifecycle_lock = asyncio.Lock()
        self._entered = False
        self._closed = False
        self._image_session: aiohttp.ClientSession | None = None

    async def _ensure_client(self) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise TiebaActionError("Tieba gateway is closed")
            if not self._entered:
                await self._client.__aenter__()
                self._entered = True

    async def _ensure_image_session(self) -> aiohttp.ClientSession:
        """Return the shared image session, creating it on first use.

        Returns:
            The session used for image CDN downloads.

        Raises:
            ImageFetchError: The gateway has already been closed.
        """
        async with self._lifecycle_lock:
            if self._closed:
                raise ImageFetchError("Tieba gateway is closed")
            if self._image_session is None:
                self._image_session = _new_image_session()
            return self._image_session

    async def _request(
        self,
        operation: str,
        *args: Any,
        write: bool = False,
        budget: ReadBudget | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            await self._ensure_client()
            if budget is not None and operation in {
                "get_threads",
                "get_posts",
                "get_comments",
            }:
                budget.consume()
            response = await getattr(self._client, operation)(*args, **kwargs)
        except (TiebaActionError, RequestBudgetExceeded):
            raise
        except Exception as error:
            raise TiebaActionError(_failure(operation, error)) from error
        error = getattr(response, "err", None)
        if error is not None:
            raise TiebaActionError(_failure(operation, error)) from error
        # Empty successful list responses are false too: check bool only for
        # writes, or explicit False/None instead of conflating them with lists.
        if response is None or response is False or (write and not response):
            raise TiebaActionError(_failure(operation, "unsuccessful response"))
        return response

    async def close(self) -> None:
        """Release the client once; closing an unused gateway needs no session.

        Returns:
            None. The gateway cannot be reused after closing.
        """
        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            if self._image_session is not None:
                await self._image_session.close()
                self._image_session = None
            if self._entered:
                await self._client.__aexit__(None, None, None)

    async def list_threads(
        self, fname: str, pages: int, *, budget: ReadBudget | None = None
    ) -> list[ThreadSnapshot]:
        """Read forum pages sorted by reply time.

        Args:
            fname: Forum name.
            pages: Number of pages to read, starting at one.
            budget: Optional round-local budget; each attempted page costs one.

        Returns:
            Thread metadata in the server's reply-time ordering.

        Raises:
            TiebaActionError: A request fails.
            RequestBudgetExceeded: The supplied budget is exhausted.
        """
        snapshots = []
        for page in range(1, pages + 1):
            response = await self._request(
                "get_threads",
                fname,
                pn=page,
                rn=30,
                sort=ThreadSortType.REPLY,
                budget=budget,
            )
            for thread in response:
                snapshots.append(
                    ThreadSnapshot(
                        tid=thread.tid,
                        fid=thread.fid,
                        fname=thread.fname,
                        title=thread.title,
                        last_time=thread.last_time,
                        create_time=thread.create_time,
                        reply_num=thread.reply_num,
                        is_top=thread.is_top,
                        is_good=thread.is_good,
                        is_livepost=thread.is_livepost,
                        author=_author(thread.user),
                    )
                )
        return snapshots

    async def get_thread_item(
        self, snap: ThreadSnapshot, *, budget: ReadBudget | None = None
    ) -> tuple[ContentItem, int]:
        """Fetch the complete first-floor body, retaining the snapshot title.

        Args:
            snap: Parent thread metadata, not a source of body fragments.
            budget: Optional round-local budget for the first-floor request.

        Returns:
            The thread item and its first-floor subreply count. The ASC request
            ensures newly inspected threads include floor-one subreplies even
            when the newest DESC page does not include floor one.

        Raises:
            TiebaActionError: The request fails or floor one is missing.
            RequestBudgetExceeded: The supplied budget is exhausted.
        """
        posts = await self._request(
            "get_posts",
            snap.tid,
            pn=1,
            rn=30,
            sort=PostSortType.ASC,
            with_comments=False,
            budget=budget,
        )
        for post in posts:
            if post.floor == 1:
                return _content(post, snap, ItemKind.THREAD, 1), post.reply_num
        raise TiebaActionError(f"Thread {snap.tid}: first floor is missing")

    async def get_thread_item_by_tid(self, tid: int) -> ContentItem:
        """Fetch a thread's metadata and complete first floor in one request.

        Args:
            tid: Thread identifier.

        Returns:
            A thread item with its title and forum metadata from the response.

        Raises:
            TiebaActionError: The request fails or the first floor is missing,
                including missing or deleted threads.
        """
        posts = await self._request(
            "get_posts",
            tid,
            pn=1,
            rn=30,
            sort=PostSortType.ASC,
            with_comments=False,
        )
        for post in posts:
            if post.floor == 1:
                # Posts exposes title on thread and forum identity on forum.
                parent = dcs.replace(
                    posts.thread, tid=tid, fid=posts.forum.fid, fname=posts.forum.fname
                )
                return _content(post, parent, ItemKind.THREAD, 1)
        raise TiebaActionError(f"Thread {tid}: first floor is missing")

    async def get_new_posts(
        self,
        snap: ThreadSnapshot,
        since: int,
        max_pages: int,
        *,
        budget: ReadBudget | None = None,
    ) -> PostBatch:
        """Read descending pages until the timestamp boundary or page cap.

        Args:
            snap: Parent thread metadata.
            since: Exclusive lower creation-time boundary for body review.
            max_pages: Maximum number of descending pages to read.
            budget: Optional round-local content request budget.

        Returns:
            New bodies, completion status, and separate comment-parent evidence
            from fetched pages. An old pinned first floor does not hide newer
            replies on the same page or stop pagination by itself.

        Raises:
            TiebaActionError: A content request fails.
            RequestBudgetExceeded: The supplied budget is exhausted.
        """
        pairs = {}
        parents = {}
        caught_up = False
        for number in range(1, max_pages + 1):
            posts = await self._request(
                "get_posts",
                snap.tid,
                pn=number,
                rn=30,
                sort=PostSortType.DESC,
                with_comments=False,
                budget=budget,
            )
            if not posts:
                caught_up = True
                break
            boundary = False
            for post in posts:
                if post.floor < 1:
                    continue
                item = _content(
                    post,
                    snap,
                    ItemKind.THREAD if post.floor == 1 else ItemKind.POST,
                    post.floor,
                )
                pair = (item, post.reply_num)
                parents[item.key] = pair
                if post.create_time > since:
                    pairs[item.key] = pair
                elif post.floor != 1:
                    boundary = True
            page = posts.page
            more = number < page.total_page if page.total_page > 0 else page.has_more
            if boundary or not more:
                caught_up = True
                break
        return PostBatch(
            pairs=tuple(pairs.values()),
            caught_up=caught_up,
            comment_parents=tuple(parents.values()),
        )

    async def get_comments(
        self,
        post: ContentItem,
        reply_num: int,
        previous: int | None,
        max_pages: int,
        *,
        budget: ReadBudget | None = None,
    ) -> tuple[list[ContentItem], bool]:
        """Read the newest comment pages covering the count increase.

        Args:
            post: Parent floor, supplying title and floor context.
            reply_num: Latest observed subreply count.
            previous: Last resolved count. None establishes a baseline by reading
                only the final data page, without backtracking historical comments.
            max_pages: Maximum data pages; the metadata probe is separate.
            budget: Optional budget charged for each actual API attempt.

        Returns:
            Deduplicated comments and whether the data-page cap was sufficient.
            An unseen parent needs only the final page and is considered complete.
            Page one is probed for server pagination and reused if needed.
            Whole pages are returned; the caller filters already-seen IDs.

        Raises:
            TiebaActionError: A request fails or nonempty pagination is invalid.
            RequestBudgetExceeded: The supplied budget is exhausted.
        """
        delta = max(0, reply_num - (previous or 0))
        if not delta:
            return [], True
        probe = await self._request(
            "get_comments",
            post.tid,
            post.pid,
            pn=1,
            sort=CommentSortType.ASC,
            budget=budget,
        )
        if not probe:
            return [], True
        page = probe.page
        size, last = page.page_size, page.total_page
        if size <= 0 or last <= 0:
            raise TiebaActionError("get_comments: invalid nonempty pagination")
        if max_pages <= 0:
            return [], False
        items = {}
        needed_pages = 1 if previous is None else last
        for offset in range(min(last, max_pages)):
            number = last - offset
            response = (
                probe
                if number == 1
                else await self._request(
                    "get_comments",
                    post.tid,
                    post.pid,
                    pn=number,
                    sort=CommentSortType.ASC,
                    budget=budget,
                )
            )
            if not response:
                return list(items.values()), True
            if offset == 0 and previous is not None:
                # The tail may contain only one entry: ceil(delta / size)
                # would miss the preceding page's new entries in that case.
                remaining = max(0, delta - len(response))
                needed_pages = min(last, 1 + (remaining + size - 1) // size)
            for comment in response:
                item = _content(comment, post, ItemKind.COMMENT, post.floor)
                items[item.key] = item
            if offset + 1 >= needed_pages:
                return list(items.values()), True
        return list(items.values()), False

    async def fetch_images(self, urls: Sequence[str], limit: int) -> list[str]:
        """Download bounded images and encode them with magic-derived types.

        Downloads go straight to the image CDN with ``aiohttp`` instead of
        aiotieba's ``get_image_bytes``, which rejects every content type except
        JPEG/PNG/BMP and therefore fails deterministically on the GIF and WebP
        images that are common in Tieba posts.

        Args:
            urls: Ordered image URLs from post content.
            limit: Maximum number to download; nonpositive means none.

        Returns:
            Base64 data URLs in input order.

        Raises:
            ImageFetchError: A URL is not a Tieba image CDN address, a download
                fails or returns a non-200 status, the body is empty or exceeds
                5 MiB, or the gateway is closed.
        """
        selected = urls[: max(0, limit)]
        if not selected:
            return []
        for url in selected:
            if not _is_allowed_image_url(url):
                raise ImageFetchError("Image URL is not a Tieba image host")
        session = await self._ensure_image_session()
        timeout = aiohttp.ClientTimeout(total=_IMAGE_TIMEOUT_SECONDS)
        images = []
        for url in selected:
            try:
                data = await self._download_image(session, url, timeout)
            except ImageFetchError:
                raise
            except Exception as error:
                raise ImageFetchError(f"Image download failed: {error}") from error
            encoded = base64.b64encode(data).decode("ascii")
            images.append(f"data:image/{_image_type(data)};base64,{encoded}")
        return images

    async def _download_image(
        self, session: aiohttp.ClientSession, url: str, timeout: aiohttp.ClientTimeout
    ) -> bytes:
        """Fetch one image body, following a bounded number of redirects.

        Args:
            session: Session owned by this gateway.
            url: An already allowlisted image URL.
            timeout: Total timeout for the whole request chain.

        Returns:
            The raw image bytes, never larger than ``_MAX_IMAGE_BYTES``.

        Raises:
            ImageFetchError: The status is not 200, the body is empty or too
                large, or too many redirects were observed.
        """
        for _ in range(_MAX_IMAGE_REDIRECTS + 1):
            async with session.get(
                url,
                headers=_IMAGE_HEADERS,
                timeout=timeout,
                allow_redirects=False,
            ) as response:
                if response.status in _REDIRECT_STATUSES:
                    location = response.headers.get("Location", "")
                    url = urllib.parse.urljoin(url, location)
                    if not _is_allowed_image_url(url):
                        raise ImageFetchError("Image redirect left the Tieba CDN")
                    continue
                if response.status != 200:
                    raise ImageFetchError(
                        f"Image download failed: HTTP {response.status}"
                    )
                data = await _read_limited(response.content)
            if not data:
                raise ImageFetchError("Image download returned empty data")
            return data
        raise ImageFetchError("Image download redirected too many times")

    async def delete(self, item: ContentItem) -> None:
        """Delete a thread or an individual floor/comment.

        Args:
            item: Content to delete.

        Raises:
            TiebaActionError: Tieba rejects the write or the request fails.
        """
        if item.kind is ItemKind.THREAD:
            await self._request("del_thread", item.fid, item.tid, write=True)
        else:
            await self._request("del_post", item.fid, item.tid, item.pid, write=True)

    async def restore(self, item: ContentItem) -> None:
        """Restore deleted content without changing the author's ban state.

        Args:
            item: Content to restore.

        Raises:
            TiebaActionError: Tieba rejects the write or the request fails.
        """
        if item.kind is ItemKind.THREAD:
            await self._request("recover_thread", item.fid, item.tid, write=True)
        else:
            await self._request("recover_post", item.fid, item.pid, write=True)

    async def block(self, item: ContentItem, days: int, reason: str) -> None:
        """Ban an item's author for an explicitly permitted duration.

        Args:
            item: Content identifying the forum and author.
            days: One of ALLOWED_BAN_DAYS.
            reason: Moderator-supplied ban reason.

        Raises:
            ValueError: The duration is not an allowed integer.
            TiebaActionError: Tieba rejects the write or the request fails.
        """
        if type(days) is not int or days not in ALLOWED_BAN_DAYS:
            raise ValueError(f"Ban days must be one of {ALLOWED_BAN_DAYS}")
        await self._request(
            "block",
            item.fid,
            item.author.portrait.partition("?t=")[0],
            day=days,
            reason=reason,
            write=True,
        )
