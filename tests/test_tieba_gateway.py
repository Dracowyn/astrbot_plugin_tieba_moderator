"""Offline gateway contract tests using real aiotieba response containers."""

import asyncio
import base64
import dataclasses as dcs
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import aiohttp
import pytest
from aiotieba.api._classdef import UserInfo
from aiotieba.api._classdef.contents import FragEmoji, FragLink, FragText, FragTiebaPlus
from aiotieba.api.get_bawu_info._classdef import BawuInfo, UserInfo_bawu
from aiotieba.api.get_comments import Comment, Comments
from aiotieba.api.get_comments._classdef import Contents_c, FragAt_c, Page_c
from aiotieba.api.get_posts import Post, Posts
from aiotieba.api.get_posts._classdef import (
    Contents_p,
    Forum_p,
    FragAt_p,
    FragImage_p,
    FragVideo_p,
    Page_p,
    Thread_p,
    UserInfo_p,
)
from aiotieba.api.get_threads import Thread, Threads
from aiotieba.enums import CommentSortType, PostSortType, ReqUInfo, ThreadSortType
from aiotieba.exception import BoolResponse, TiebaServerError
from astrbot_plugin_tieba_moderator.core.models import (
    Author,
    ContentItem,
    ImageFetchError,
    ItemKind,
    TiebaActionError,
)
from astrbot_plugin_tieba_moderator.services import tieba_gateway as module
from yarl import URL


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def gateway(monkeypatch):
    client = Mock()
    for name in (
        "__aenter__",
        "__aexit__",
        "get_threads",
        "get_posts",
        "get_comments",
        "get_image_bytes",
        "get_self_info",
        "get_bawu_info",
        "del_thread",
        "del_post",
        "recover_thread",
        "recover_post",
        "block",
    ):
        setattr(client, name, AsyncMock())
    client.__aenter__.return_value = client
    constructor = Mock(return_value=client)
    monkeypatch.setattr(module, "Client", constructor)
    instance = module.TiebaGateway("fake-bduss", "fake-stoken")
    constructor.assert_not_called()
    return instance, client


@pytest.fixture
def snap():
    return module.ThreadSnapshot(
        tid=100,
        fid=20,
        fname="forum",
        title="snapshot title",
        last_time=300,
        create_time=100,
        reply_num=10,
        is_top=False,
        is_good=False,
        is_livepost=False,
        author=Author(user_id=7, portrait="author"),
    )


@pytest.fixture
def item():
    return ContentItem(
        kind=ItemKind.POST,
        tid=100,
        fid=20,
        fname="forum",
        pid=200,
        floor=9,
        title="parent title",
        author=Author(user_id=7, portrait="author?t=123"),
    )


def post(*, floor=1, pid=101, contents=None, create_time=111):
    return Post(
        pid=pid,
        floor=floor,
        create_time=create_time,
        reply_num=27,
        user=UserInfo_p(
            user_id=7,
            portrait="portrait?t=1234567890",
            user_name="login",
            nick_name_new="nickname",
            level=5,
            is_bawu=True,
        ),
        contents=contents or Contents_p(objs=[FragText(text="complete body")]),
    )


def test_list_threads_metadata_and_reply_sort(gateway):
    gw, client = gateway
    thread = Thread(
        tid=100,
        fid=20,
        fname="forum",
        title="title",
        last_time=456,
        create_time=123,
        reply_num=8,
        is_top=True,
        is_good=True,
        is_livepost=True,
        user=post().user,
    )
    client.get_threads.side_effect = [Threads(objs=[thread]), Threads()]
    result = run(gw.list_threads("forum", 2))
    assert len(result) == 1
    assert result[0].author == Author(
        user_id=7,
        portrait="portrait",
        user_name="login",
        nick_name="nickname",
        level=5,
        is_bawu=True,
    )
    assert (result[0].tid, result[0].fid, result[0].fname, result[0].title) == (
        100,
        20,
        "forum",
        "title",
    )
    assert (result[0].last_time, result[0].create_time, result[0].reply_num) == (
        456,
        123,
        8,
    )
    assert result[0].is_top and result[0].is_good and result[0].is_livepost
    assert client.get_threads.await_args_list == [
        call("forum", pn=1, rn=30, sort=ThreadSortType.REPLY),
        call("forum", pn=2, rn=30, sort=ThreadSortType.REPLY),
    ]
    with pytest.raises(dcs.FrozenInstanceError):
        result[0].title = "changed"


def test_full_first_floor_and_ordered_fragments(gateway, snap):
    gw, client = gateway
    fragments = [
        FragText(text="full "),
        FragAt_p(text="@member"),
        FragEmoji(id="smile", desc="ignored"),
        FragLink(
            text="https://example.org",
            title="click",
            raw_url=URL("https://example.org"),
        ),
        FragImage_p(src="small", origin_src="large"),
        FragVideo_p(src="video", cover_src="cover", width=0),
        FragTiebaPlus(text="ad", url=URL("https://ads.example")),
        FragImage_p(src="last"),
    ]
    client.get_posts.return_value = Posts(
        objs=[post(floor=2), post(contents=Contents_p(objs=fragments))]
    )
    result, count = run(gw.get_thread_item(snap))
    assert count == 27
    assert result.kind is ItemKind.THREAD
    assert result.title == snap.title
    assert (
        result.text == "full @memberclick(https://example.org)ad(https://ads.example)"
    )
    assert result.image_urls == ("small", "cover", "last")
    assert result.has_video
    assert (result.floor, result.pid, result.create_time) == (1, 101, 111)
    assert (result.tid, result.fid, result.fname) == (100, 20, "forum")
    client.get_posts.assert_awaited_once_with(
        100, pn=1, rn=30, sort=PostSortType.ASC, with_comments=False
    )


def test_missing_first_floor_is_error(gateway, snap):
    gw, client = gateway
    client.get_posts.return_value = Posts(objs=[post(floor=2)])
    with pytest.raises(TiebaActionError, match="first floor"):
        run(gw.get_thread_item(snap))


def test_latest_posts_use_desc_and_include_thread_floor(gateway, snap):
    gw, client = gateway
    client.get_posts.return_value = Posts(
        objs=[post(floor=12, pid=112), post(floor=1), post(floor=0), post(floor=-1)]
    )
    result = run(gw.get_new_posts(snap, 0, 3))
    assert result.caught_up
    assert len(result.pairs) == 2
    content, count = result.pairs[0]
    assert content.kind is ItemKind.POST
    assert (content.pid, content.floor, content.title, count) == (
        112,
        12,
        snap.title,
        27,
    )
    first_floor, first_count = result.pairs[1]
    assert first_floor.kind is ItemKind.THREAD
    assert (first_floor.floor, first_floor.key, first_count) == (1, "thread:101", 27)
    client.get_posts.assert_awaited_once_with(
        100, pn=1, rn=30, sort=PostSortType.DESC, with_comments=False
    )


@pytest.mark.parametrize("last_time", [100, 300])
def test_first_floor_identity_matches_for_new_and_old_threads(gateway, snap, last_time):
    gw, client = gateway
    snap = dcs.replace(snap, last_time=last_time)
    client.get_posts.return_value = Posts(objs=[post()])
    first, count = run(gw.get_thread_item(snap))
    assert run(gw.get_new_posts(snap, 0, 3)).pairs == ((first, count),)
    assert first.kind is ItemKind.THREAD


def test_malformed_link_keeps_label_and_raw_text(gateway, snap):
    gw, client = gateway
    raw = "https://tieba.baidu.com/mo/q/checkurl?missing=target"
    client.get_posts.return_value = Posts(
        objs=[
            post(
                contents=Contents_p(
                    objs=[
                        FragText(text="before "),
                        FragLink(text=raw, title="broken link", raw_url=URL(raw)),
                        FragTiebaPlus(text="ad", url=URL("https://ads.example")),
                        FragText(text=" after"),
                    ]
                )
            )
        ]
    )
    result, _ = run(gw.get_thread_item(snap))
    assert result.text == f"before broken link({raw})ad(https://ads.example) after"


def test_get_thread_by_tid_uses_real_response_metadata(gateway):
    gw, client = gateway
    client.get_posts.return_value = Posts(
        objs=[post(floor=2), post()],
        forum=Forum_p(fid=42, fname="actual forum"),
        thread=Thread_p(tid=100, title="actual title"),
    )
    result = run(gw.get_thread_item_by_tid(100))
    assert (result.tid, result.fid, result.fname, result.title) == (
        100,
        42,
        "actual forum",
        "actual title",
    )
    assert (result.kind, result.floor, result.key) == (ItemKind.THREAD, 1, "thread:101")
    assert result.text == "complete body"
    assert result.author.portrait == "portrait"
    client.get_posts.assert_awaited_once_with(
        100, pn=1, rn=30, sort=PostSortType.ASC, with_comments=False
    )
    client.get_threads.assert_not_called()


@pytest.mark.parametrize("posts", [[], [post(floor=2)]])
def test_get_thread_by_tid_missing_first_floor_is_error(gateway, posts):
    gw, client = gateway
    client.get_posts.return_value = Posts(objs=posts)
    with pytest.raises(TiebaActionError, match="100.*first floor"):
        run(gw.get_thread_item_by_tid(100))


def test_get_thread_by_tid_deleted_thread_is_error(gateway):
    gw, client = gateway
    response = Posts()
    response.err = TiebaServerError(4, "thread deleted")
    client.get_posts.return_value = response
    with pytest.raises(TiebaActionError, match="4.*thread deleted"):
        run(gw.get_thread_item_by_tid(100))


@pytest.mark.parametrize("total_pages", [1, 4])
def test_comments_follow_server_last_page_and_parent_context(
    gateway, item, total_pages
):
    gw, client = gateway
    comment = Comment(
        pid=302,
        floor=0,
        create_time=200,
        contents=Contents_c(objs=[FragAt_c(text="@who"), FragText(text=" latest")]),
        user=post().user,
    )
    first = Comments(objs=[comment], page=Page_c(page_size=7, total_page=total_pages))
    client.get_comments.side_effect = [first, Comments(objs=[comment])]
    result, caught_up = run(gw.get_comments(item, 99, 98, 3))
    assert caught_up
    assert len(result) == 1
    assert result[0].kind is ItemKind.COMMENT
    assert (
        result[0].floor,
        result[0].title,
        result[0].tid,
        result[0].fid,
        result[0].fname,
    ) == (9, item.title, 100, 20, "forum")
    assert result[0].pid == 302
    assert result[0].text == "@who latest"
    expected = [call(100, 200, pn=1, sort=CommentSortType.ASC)]
    if total_pages > 1:
        expected.append(call(100, 200, pn=total_pages, sort=CommentSortType.ASC))
    assert client.get_comments.await_args_list == expected


@pytest.mark.parametrize("reply_num,previous", [(0, None), (5, 5), (4, 8)])
def test_zero_comment_delta_no_request(gateway, item, reply_num, previous):
    gw, client = gateway
    assert run(gw.get_comments(item, reply_num, previous, 3)) == ([], True)
    client.get_comments.assert_not_called()


@pytest.mark.parametrize("operation", ["threads", "posts", "comments"])
def test_empty_successful_lists_are_not_errors(gateway, snap, item, operation):
    gw, client = gateway
    client.get_threads.return_value = Threads()
    client.get_posts.return_value = Posts()
    client.get_comments.return_value = Comments()
    awaitable = {
        "threads": lambda: gw.list_threads("forum", 1),
        "posts": lambda: gw.get_new_posts(snap, 0, 3),
        "comments": lambda: gw.get_comments(item, 1, None, 3),
    }[operation]()
    expected = {"threads": [], "posts": module.PostBatch(), "comments": ([], True)}
    assert run(awaitable) == expected[operation]


@pytest.mark.parametrize("operation", ["threads", "posts", "comments"])
def test_read_response_errors_are_not_empty_success(gateway, snap, item, operation):
    gw, client = gateway
    response = {"threads": Threads, "posts": Posts, "comments": Comments}[operation]()
    response.err = TiebaServerError(42, "read denied")
    getattr(client, "get_" + operation).return_value = response
    awaitable = {
        "threads": lambda: gw.list_threads("forum", 1),
        "posts": lambda: gw.get_new_posts(snap, 0, 3),
        "comments": lambda: gw.get_comments(item, 1, None, 3),
    }[operation]()
    with pytest.raises(TiebaActionError, match="42.*read denied"):
        run(awaitable)


def test_posts_cross_pages_stop_at_exact_boundary(gateway, snap):
    gw, client = gateway
    client.get_posts.side_effect = [
        Posts(
            objs=[post(floor=9, pid=109, create_time=400)],
            page=Page_p(current_page=1, total_page=5),
        ),
        Posts(
            objs=[
                post(floor=8, pid=108, create_time=301),
                post(floor=7, pid=107, create_time=300),
                post(floor=6, pid=106, create_time=200),
            ],
            page=Page_p(current_page=2, total_page=5),
        ),
    ]
    result = run(gw.get_new_posts(snap, 300, 4))
    assert result.caught_up
    assert [item.pid for item, _ in result.pairs] == [109, 108]
    assert all(item.create_time > 300 for item, _ in result.pairs)
    assert {item.pid: count for item, count in result.comment_parents}[107] == 27
    assert [args.kwargs["pn"] for args in client.get_posts.await_args_list] == [1, 2]


@pytest.mark.parametrize("last_page,expected", [(2, True), (3, False)])
def test_posts_page_cap_uses_server_metadata(gateway, snap, last_page, expected):
    gw, client = gateway
    client.get_posts.side_effect = [
        Posts(objs=[post(floor=4, pid=104)], page=Page_p(total_page=last_page)),
        Posts(objs=[post(floor=3, pid=103)], page=Page_p(total_page=last_page)),
    ]
    result = run(gw.get_new_posts(snap, 0, 2))
    assert result.caught_up is expected
    assert len(result.pairs) == 2
    assert client.get_posts.await_count == 2


def test_old_pinned_first_floor_does_not_hide_newer_replies(gateway, snap):
    gw, client = gateway
    client.get_posts.side_effect = [
        Posts(
            objs=[post(create_time=10), post(floor=9, pid=109, create_time=400)],
            page=Page_p(total_page=3),
        ),
        Posts(
            objs=[
                post(floor=8, pid=108, create_time=301),
                post(floor=7, pid=107, create_time=300),
            ],
            page=Page_p(total_page=3),
        ),
    ]
    result = run(gw.get_new_posts(snap, 300, 3))
    assert result.caught_up
    assert [item.pid for item, _ in result.pairs] == [109, 108]
    parent, count = result.comment_parents[0]
    assert (parent.kind, parent.floor, parent.key, count) == (
        ItemKind.THREAD,
        1,
        "thread:101",
        27,
    )
    assert client.get_posts.await_count == 2


def test_posts_empty_page_ends_scan_and_overlaps_are_unique(gateway, snap):
    gw, client = gateway
    client.get_posts.side_effect = [
        Posts(objs=[post(floor=2, pid=102)], page=Page_p(has_more=True)),
        Posts(objs=[post(floor=2, pid=102)], page=Page_p(has_more=True)),
        Posts(),
    ]
    result = run(gw.get_new_posts(snap, 0, 4))
    assert result.caught_up
    assert len(result.pairs) == len(result.comment_parents) == 1
    assert client.get_posts.await_count == 3


def comment_page(ids, *, size=20, pages=2, count=21):
    return Comments(
        objs=[Comment(pid=pid, user=post().user) for pid in ids],
        page=Page_c(page_size=size, total_page=pages, total_count=count),
    )


@pytest.mark.parametrize("cap,caught_up", [(1, False), (2, True)])
def test_partial_comment_tail_requires_previous_page(gateway, item, cap, caught_up):
    gw, client = gateway
    client.get_comments.side_effect = [comment_page(range(1, 21)), comment_page([21])]
    budget = module.ReadBudget(2)
    result, complete = run(gw.get_comments(item, 21, 19, cap, budget=budget))
    assert complete is caught_up
    assert {entry.pid for entry in result} == ({21} if cap == 1 else set(range(1, 22)))
    assert client.get_comments.await_args_list == [
        call(100, 200, pn=1, sort=CommentSortType.ASC),
        call(100, 200, pn=2, sort=CommentSortType.ASC),
    ]
    assert budget.used == 2  # Probe is reused, not fetched again as a data page.


@pytest.mark.parametrize(
    "cap,expected_pages,caught_up",
    [
        (1, [1, 4], False),
        (2, [1, 4, 3], True),
        (4, [1, 4, 3], True),
    ],
)
def test_comments_server_size_and_data_page_cap(
    gateway, item, cap, expected_pages, caught_up
):
    gw, client = gateway
    client.get_comments.side_effect = [
        comment_page([1, 2, 3], size=3, pages=4, count=10),
        comment_page([10], size=3, pages=4, count=10),
        comment_page([7, 8, 9], size=3, pages=4, count=10),
    ]
    budget = module.ReadBudget(4)
    result, complete = run(gw.get_comments(item, 10, 6, cap, budget=budget))
    assert complete is caught_up
    assert {entry.pid for entry in result} == ({10} if cap == 1 else {7, 8, 9, 10})
    assert [
        args.kwargs["pn"] for args in client.get_comments.await_args_list
    ] == expected_pages
    assert budget.used == len(expected_pages)


def test_comments_incremental_scan_deduplicates_overlapping_pages(gateway, item):
    gw, client = gateway
    client.get_comments.side_effect = [
        comment_page([1, 2], size=2, pages=2, count=3),
        comment_page([2, 3], size=2, pages=2, count=3),
    ]
    result, caught_up = run(gw.get_comments(item, 3, 0, 3))
    assert caught_up
    assert [entry.pid for entry in result] == [2, 3, 1]


@pytest.mark.parametrize("pages", [1, 50])
def test_first_seen_parent_only_reads_latest_comment_page(gateway, item, pages):
    gw, client = gateway
    first = comment_page([1, 2], size=2, pages=pages, count=pages * 2)
    latest = comment_page([99, 100], size=2, pages=pages, count=pages * 2)
    client.get_comments.side_effect = [first, latest]
    budget = module.ReadBudget(2)
    result, caught_up = run(gw.get_comments(item, pages * 2, None, 3, budget=budget))
    assert caught_up
    assert [entry.pid for entry in result] == ([1, 2] if pages == 1 else [99, 100])
    assert [args.kwargs["pn"] for args in client.get_comments.await_args_list] == (
        [1] if pages == 1 else [1, pages]
    )
    assert budget.used == (1 if pages == 1 else 2)


def test_previously_seen_empty_parent_still_backtracks_and_reports_cap(gateway, item):
    gw, client = gateway
    client.get_comments.side_effect = [
        comment_page([1, 2], size=2, pages=5, count=10),
        comment_page([9, 10], size=2, pages=5, count=10),
        comment_page([7, 8], size=2, pages=5, count=10),
    ]
    result, caught_up = run(gw.get_comments(item, 10, 0, 2))
    assert not caught_up
    assert [entry.pid for entry in result] == [9, 10, 7, 8]
    assert [args.kwargs["pn"] for args in client.get_comments.await_args_list] == [
        1,
        5,
        4,
    ]


@pytest.mark.parametrize("size,pages", [(0, 2), (-1, 2), (20, 0)])
def test_nonempty_comments_require_valid_pagination(gateway, item, size, pages):
    gw, client = gateway
    client.get_comments.return_value = comment_page([1], size=size, pages=pages)
    with pytest.raises(TiebaActionError, match="pagination"):
        run(gw.get_comments(item, 1, None, 3))


def test_empty_comment_tail_is_success(gateway, item):
    gw, client = gateway
    client.get_comments.side_effect = [comment_page([1]), Comments()]
    assert run(gw.get_comments(item, 21, None, 3)) == ([], True)


def test_zero_page_caps_do_not_claim_caught_up(gateway, snap, item):
    gw, client = gateway
    assert run(gw.get_new_posts(snap, 0, 0)) == module.PostBatch(caught_up=False)
    client.get_posts.assert_not_called()
    client.get_comments.return_value = comment_page([1])
    assert run(gw.get_comments(item, 21, None, 0)) == ([], False)


def test_read_budget_exact_limit_and_batch_immutability():
    budget = module.ReadBudget(1)
    assert (budget.used, budget.remaining) == (0, 1)
    budget.consume()
    assert (budget.used, budget.remaining) == (1, 0)
    with pytest.raises(module.RequestBudgetExceeded):
        budget.consume()
    assert budget.used == 1
    with pytest.raises(module.RequestBudgetExceeded):
        module.ReadBudget(0).consume()
    with pytest.raises(dcs.FrozenInstanceError):
        module.PostBatch().caught_up = False


def test_budget_shared_across_content_apis_and_excludes_manual_reads(
    gateway, snap, item
):
    gw, client = gateway
    budget = module.ReadBudget(4)
    client.get_threads.return_value = Threads()
    client.get_posts.return_value = Posts(objs=[post()])
    client.get_comments.return_value = comment_page([1], pages=1)
    run(gw.list_threads("forum", 1, budget=budget))
    run(gw.get_thread_item(snap, budget=budget))
    run(gw.get_new_posts(snap, 0, 1, budget=budget))
    run(gw.get_comments(item, 1, None, 1, budget=budget))
    assert budget.used == 4
    run(gw.get_thread_item_by_tid(100))
    assert budget.used == 4
    with pytest.raises(module.RequestBudgetExceeded):
        run(gw.list_threads("forum", 1, budget=budget))
    assert client.get_threads.await_count == 1
    client.__aenter__.assert_awaited_once()


@pytest.mark.parametrize("failure", ["raised", "response"])
def test_failed_content_attempts_consume_budget(gateway, failure):
    gw, client = gateway
    budget = module.ReadBudget(1)
    if failure == "raised":
        client.get_threads.side_effect = RuntimeError("offline failure")
    else:
        response = Threads()
        response.err = TiebaServerError(42, "offline failure")
        client.get_threads.return_value = response
    with pytest.raises(TiebaActionError, match="offline failure"):
        run(gw.list_threads("forum", 1, budget=budget))
    assert budget.used == 1
    with pytest.raises(module.RequestBudgetExceeded):
        run(gw.list_threads("forum", 1, budget=budget))
    assert client.get_threads.await_count == 1


def test_client_entry_failure_does_not_consume_content_budget(gateway):
    gw, client = gateway
    budget = module.ReadBudget(1)
    client.__aenter__.side_effect = RuntimeError("entry failed")
    with pytest.raises(TiebaActionError, match="entry failed"):
        run(gw.list_threads("forum", 1, budget=budget))
    assert budget.used == 0
    client.get_threads.assert_not_called()


def test_budget_exhaustion_between_post_pages_stops_exactly(gateway, snap):
    gw, client = gateway
    client.get_posts.return_value = Posts(
        objs=[post(floor=2)], page=Page_p(total_page=3)
    )
    budget = module.ReadBudget(1)
    with pytest.raises(module.RequestBudgetExceeded):
        run(gw.get_new_posts(snap, 0, 3, budget=budget))
    assert budget.used == client.get_posts.await_count == 1


def test_budget_counts_comment_probe_before_tail(gateway, item):
    gw, client = gateway
    client.get_comments.return_value = comment_page([1])
    budget = module.ReadBudget(1)
    with pytest.raises(module.RequestBudgetExceeded):
        run(gw.get_comments(item, 21, 20, 1, budget=budget))
    assert budget.used == client.get_comments.await_count == 1


class FakeContent:
    """Minimal stand-in for an aiohttp streamed body."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def iter_chunked(self, size):
        for start in range(0, len(self._data), size):
            yield self._data[start : start + size]


class FakeResponse:
    """Minimal stand-in for an aiohttp response context manager."""

    def __init__(self, status: int = 200, data: bytes = b"", headers=None) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    """Records requests and replays canned responses."""

    def __init__(self, responses, log) -> None:
        self.responses = responses
        self.requests = log
        self.closed = False

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self):
        self.closed = True


CDN_URL = "https://imgsrc.baidu.com/forum/pic/item/abc.jpg"
CDN_URL_2 = "https://tiebapic.baidu.com/forum/pic/item/def.png"


@pytest.fixture
def image_gateway(gateway, monkeypatch):
    """Gateway whose image session is a fake sharing one response queue."""
    gw, client = gateway
    responses: list = []
    requests: list = []
    created: list[FakeSession] = []

    def make_session():
        session = FakeSession(responses, requests)
        created.append(session)
        return session

    monkeypatch.setattr(module, "_new_image_session", make_session)
    return SimpleNamespace(
        gateway=gw, responses=responses, requests=requests, created=created
    )


def test_exhausted_read_budget_does_not_affect_images_or_writes(
    gateway, image_gateway, item
):
    gw, client = gateway
    budget = module.ReadBudget(1)
    client.get_threads.return_value = Threads()
    run(gw.list_threads("forum", 1, budget=budget))
    assert budget.remaining == 0
    image_gateway.responses.append(FakeResponse(data=b"GIF89abody"))
    assert run(gw.fetch_images([CDN_URL], 1))[0].startswith("data:image/gif;")
    for name in ("del_post", "recover_post", "block"):
        getattr(client, name).return_value = BoolResponse()
    run(gw.delete(item))
    run(gw.restore(item))
    run(gw.block(item, 1, "reason"))
    # Even an explicitly supplied read budget is never charged for writes.
    run(gw._request("del_post", 20, 100, 200, write=True, budget=budget))
    assert budget.used == 1
    assert client.del_post.await_count == 2
    client.get_image_bytes.assert_not_called()
    with pytest.raises(module.RequestBudgetExceeded):
        run(gw.list_threads("forum", 1, budget=budget))


def test_image_session_is_reused_and_closed(image_gateway):
    gw = image_gateway.gateway
    image_gateway.responses.append(FakeResponse(data=b"\xff\xd8\xff"))
    run(gw.fetch_images([CDN_URL], 1))
    image_gateway.responses.append(FakeResponse(data=b"\xff\xd8\xff"))
    run(gw.fetch_images([CDN_URL], 1))
    assert len(image_gateway.created) == 1
    run(gw.close())
    assert image_gateway.created[0].closed is True
    with pytest.raises(ImageFetchError, match="closed"):
        run(gw.fetch_images([CDN_URL], 1))


@pytest.mark.parametrize(
    "data,mime",
    [
        (b"\xff\xd8\xffbody", "jpeg"),
        (b"\x89PNG\r\n\x1a\nbody", "png"),
        (b"BMbody", "bmp"),
        (b"GIF87abody", "gif"),
        (b"GIF89abody", "gif"),
        (b"RIFF1234WEBPbody", "webp"),
        (b"RIFFnot-webp", "jpeg"),
        (b"unknown", "jpeg"),
    ],
)
def test_image_magic_and_limit(image_gateway, data, mime):
    image_gateway.responses.append(FakeResponse(data=data))
    assert run(image_gateway.gateway.fetch_images([CDN_URL, CDN_URL_2], 1)) == [
        f"data:image/{mime};base64," + base64.b64encode(data).decode("ascii")
    ]
    assert [url for url, _ in image_gateway.requests] == [CDN_URL]


def test_image_request_shape(image_gateway):
    image_gateway.responses.append(FakeResponse(data=b"\xff\xd8\xffbody"))
    run(image_gateway.gateway.fetch_images([CDN_URL], 1))
    url, kwargs = image_gateway.requests[0]
    assert url == CDN_URL
    assert kwargs["headers"] == {"Referer": "https://tieba.baidu.com/"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"].total == 20


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.jpg",
        "http://localhost/a.jpg",
        "https://baidu.com.evil.example/a.jpg",
        "https://notbaidu.com/a.jpg",
        "ftp://imgsrc.baidu.com/a.jpg",
        "javascript:alert(1)",
        "//imgsrc.baidu.com/a.jpg",
        "",
        "https://user:pw@evil.example/a.jpg",
        "https://imgsrc.baidu.com@evil.example/a.jpg",
    ],
)
def test_non_cdn_urls_are_rejected_before_any_request(image_gateway, url):
    with pytest.raises(ImageFetchError, match="Tieba image host"):
        run(image_gateway.gateway.fetch_images([url], 1))
    assert image_gateway.created == []


@pytest.mark.parametrize("limit", [0, -1])
def test_nonpositive_image_limit(image_gateway, limit):
    assert run(image_gateway.gateway.fetch_images([CDN_URL], limit)) == []
    assert image_gateway.created == []


def test_redirect_inside_the_cdn_is_followed(image_gateway):
    image_gateway.responses += [
        FakeResponse(status=302, headers={"Location": CDN_URL_2}),
        FakeResponse(data=b"\xff\xd8\xffbody"),
    ]
    assert len(run(image_gateway.gateway.fetch_images([CDN_URL], 1))) == 1
    assert [url for url, _ in image_gateway.requests] == [CDN_URL, CDN_URL_2]


def test_redirect_outside_the_cdn_is_refused(image_gateway):
    image_gateway.responses.append(
        FakeResponse(status=302, headers={"Location": "https://evil.example/a.jpg"})
    )
    with pytest.raises(ImageFetchError, match="redirect"):
        run(image_gateway.gateway.fetch_images([CDN_URL], 1))
    assert len(image_gateway.requests) == 1


def test_too_many_redirects(image_gateway):
    image_gateway.responses += [
        FakeResponse(status=302, headers={"Location": CDN_URL}) for _ in range(4)
    ]
    with pytest.raises(ImageFetchError, match="too many"):
        run(image_gateway.gateway.fetch_images([CDN_URL], 1))


@pytest.mark.parametrize("mode", ["empty", "oversize", "status", "exception"])
def test_image_failures(image_gateway, mode):
    response = {
        "empty": FakeResponse(data=b""),
        "oversize": FakeResponse(data=b"x" * (5 * 1024 * 1024 + 1)),
        "status": FakeResponse(status=404),
        "exception": aiohttp.ClientError("boom"),
    }[mode]
    image_gateway.responses.append(response)
    with pytest.raises(ImageFetchError):
        run(image_gateway.gateway.fetch_images([CDN_URL], 1))


def test_image_size_boundary_and_partial_failure(image_gateway):
    image_gateway.responses.append(FakeResponse(data=b"x" * (5 * 1024 * 1024)))
    assert len(run(image_gateway.gateway.fetch_images([CDN_URL], 1))) == 1
    image_gateway.responses += [
        FakeResponse(data=b"\xff\xd8\xff"),
        FakeResponse(status=500),
    ]
    with pytest.raises(ImageFetchError):
        run(image_gateway.gateway.fetch_images([CDN_URL, CDN_URL_2], 2))


@pytest.mark.parametrize("kind", list(ItemKind))
@pytest.mark.parametrize("action", ["delete", "restore"])
def test_write_routes_and_arguments(gateway, item, kind, action):
    gw, client = gateway
    item = dcs.replace(item, kind=kind)
    name = ("del_" if action == "delete" else "recover_") + (
        "thread" if kind is ItemKind.THREAD else "post"
    )
    method = getattr(client, name)
    method.return_value = BoolResponse()
    run(getattr(gw, action)(item))
    args = (
        (20, 100)
        if kind is ItemKind.THREAD
        else ((20, 100, 200) if action == "delete" else (20, 200))
    )
    method.assert_awaited_once_with(*args)


@pytest.mark.parametrize("days", [1, 3, 10])
def test_block_arguments(gateway, item, days):
    gw, client = gateway
    client.block.return_value = BoolResponse()
    run(gw.block(item, days, "reason"))
    client.block.assert_awaited_once_with(20, "author", day=days, reason="reason")


@pytest.mark.parametrize("days", [0, 2, 30, -1, True, 1.0, "1"])
def test_invalid_ban_duration_no_side_effect(gateway, item, days):
    gw, client = gateway
    with pytest.raises(ValueError):
        run(gw.block(item, days, "reason"))
    client.block.assert_not_called()


@pytest.mark.parametrize(
    "action,method",
    [("delete", "del_post"), ("restore", "recover_post"), ("block", "block")],
)
@pytest.mark.parametrize("failure", ["response", "false", "exception"])
def test_write_failures(gateway, item, action, method, failure):
    gw, client = gateway
    response = BoolResponse()
    response.err = TiebaServerError(88, "not allowed")
    mock = getattr(client, method)
    if failure == "exception":
        mock.side_effect = TiebaServerError(88, "not allowed")
    else:
        mock.return_value = False if failure == "false" else response
    with pytest.raises(TiebaActionError) as caught:
        run(
            gw.block(item, 1, "reason")
            if action == "block"
            else getattr(gw, action)(item)
        )
    if failure != "false":
        assert "88" in str(caught.value) and "not allowed" in str(caught.value)


def test_client_entered_once_concurrently_and_closed_once(gateway):
    gw, client = gateway
    client.get_threads.return_value = Threads()

    async def scenario():
        await asyncio.gather(gw.list_threads("a", 1), gw.list_threads("b", 1))
        await gw.close()
        await gw.close()
        with pytest.raises(TiebaActionError, match="closed"):
            await gw.list_threads("a", 1)

    run(scenario())
    client.__aenter__.assert_awaited_once()
    client.__aexit__.assert_awaited_once_with(None, None, None)


def test_close_unused_client(gateway):
    gw, client = gateway
    run(gw.close())
    client.__aenter__.assert_not_called()
    client.__aexit__.assert_not_called()


def test_self_info_uses_installed_identity_fields_and_normalizes_portrait(gateway):
    gw, client = gateway
    client.get_self_info.return_value = UserInfo(
        user_id=42, portrait="portrait?t=123", user_name="login",
        nick_name_new="nickname", nick_name_old="old nickname",
    )
    result = run(gw.get_self_info())
    assert result == module.SelfInfo(
        user_id=42, portrait="portrait", user_name="login", nick_name="nickname"
    )
    client.get_self_info.assert_awaited_once_with(ReqUInfo.BASIC | ReqUInfo.NICK_NAME)
    with pytest.raises(dcs.FrozenInstanceError):
        result.user_id = 100


def test_self_info_supports_blank_login_and_old_nickname(gateway):
    gw, client = gateway
    client.get_self_info.return_value = UserInfo(
        user_id=42, portrait="portrait", nick_name_old="old nickname"
    )
    result = run(gw.get_self_info())
    assert result.user_name == ""
    assert result.nick_name == "old nickname"


@pytest.mark.parametrize("response", [
    UserInfo(), None, False, SimpleNamespace(),
    UserInfo(user_id=-1, portrait="portrait"),
    UserInfo(user_id=True, portrait="portrait"),
    UserInfo(user_id="42", portrait="portrait"),
    UserInfo(user_id=42),
    UserInfo(user_id=42, portrait="?t=123"),
    UserInfo(user_id=42, portrait=None),
    UserInfo(user_id=42, portrait="portrait", user_name=None),
    SimpleNamespace(user_id=42, portrait="portrait", user_name="", nick_name=None),
])
def test_invalid_self_info_is_tieba_action_error(gateway, response):
    gw, client = gateway
    client.get_self_info.return_value = response
    with pytest.raises(TiebaActionError):
        run(gw.get_self_info())


@pytest.mark.parametrize("wrapped", [True, False])
def test_self_info_request_failure_is_tieba_action_error(gateway, wrapped):
    gw, client = gateway
    error = TiebaServerError(4, "not logged in")
    if wrapped:
        response = UserInfo()
        response.err = error
        client.get_self_info.return_value = response
    else:
        client.get_self_info.side_effect = error
    with pytest.raises(TiebaActionError, match="无法获取有效的贴吧账号信息"):
        run(gw.get_self_info())


@pytest.mark.parametrize("users,expected", [
    ([UserInfo_bawu(user_id=42, portrait="other")], True),
    ([UserInfo_bawu(user_id=0, portrait="portrait?t=100")], True),
    ([UserInfo_bawu(user_id=7, portrait="other")], False),
    ([UserInfo_bawu()], False),
    ([], False),
])
def test_bawu_uses_real_team_all_members_and_stable_identity(gateway, users, expected):
    gw, client = gateway
    client.get_bawu_info.return_value = BawuInfo(all=users)
    me = module.SelfInfo(
        user_id=42, portrait="portrait?t=200", user_name="login", nick_name="nickname"
    )
    assert run(gw.is_bawu("forum", me)) is expected
    client.get_bawu_info.assert_awaited_once_with("forum")


def test_empty_bawu_identifiers_never_match(gateway):
    gw, client = gateway
    client.get_bawu_info.return_value = BawuInfo(all=[UserInfo_bawu()])
    me = module.SelfInfo(user_id=0, portrait="", user_name="", nick_name="")
    assert run(gw.is_bawu("forum", me)) is False


@pytest.mark.parametrize("failure", ["exception", "wrapped", "none", "false", "malformed"])
def test_bawu_query_failure_is_unknown_not_false(gateway, failure):
    gw, client = gateway
    error = TiebaServerError(4, "denied")
    if failure == "exception":
        client.get_bawu_info.side_effect = error
    elif failure == "wrapped":
        response = BawuInfo()
        response.err = error
        client.get_bawu_info.return_value = response
    else:
        client.get_bawu_info.return_value = {
            "none": None, "false": False, "malformed": SimpleNamespace(all=None)
        }[failure]
    me = module.SelfInfo(user_id=42, portrait="portrait", user_name="", nick_name="")
    assert run(gw.is_bawu("forum", me)) is None


@pytest.mark.parametrize("operation", ["self", "bawu"])
def test_account_checks_propagate_cancellation(gateway, operation):
    gw, client = gateway
    client.get_self_info.side_effect = asyncio.CancelledError
    client.get_bawu_info.side_effect = asyncio.CancelledError
    me = module.SelfInfo(user_id=42, portrait="portrait", user_name="", nick_name="")
    with pytest.raises(asyncio.CancelledError):
        run(gw.get_self_info() if operation == "self" else gw.is_bawu("forum", me))


def test_client_constructed_only_once_on_first_authenticated_request(gateway):
    gw, client = gateway
    client.get_threads.return_value = Threads()

    async def scenario():
        module.Client.assert_not_called()
        await asyncio.gather(gw.list_threads("first", 1), gw.list_threads("second", 1))
        module.Client.assert_called_once_with("fake-bduss", "fake-stoken")
        await gw.close()

    run(scenario())


def test_client_cookie_format_failure_is_deferred_and_redacted(monkeypatch):
    client_factory = Mock(side_effect=ValueError("invalid cookie fake-secret-cookie"))
    monkeypatch.setattr(module, "Client", client_factory)
    gw = module.TiebaGateway("fake-secret-cookie", "fake-stoken")
    client_factory.assert_not_called()
    with pytest.raises(TiebaActionError) as caught:
        run(gw.get_self_info())
    assert str(caught.value) == "无法获取有效的贴吧账号信息"
    assert "fake-secret-cookie" not in str(caught.value)
    client_factory.assert_called_once_with("fake-secret-cookie", "fake-stoken")
    run(gw.close())


def test_unused_gateway_close_never_constructs_client(gateway):
    gw, client = gateway
    run(gw.close())
    module.Client.assert_not_called()
    client.__aexit__.assert_not_awaited()
