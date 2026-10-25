"""Offline patrol behavior, incremental progress and concurrency regressions."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_tieba_moderator import main as entry
from astrbot_plugin_tieba_moderator.core.judge import FAILED_CATEGORY
from astrbot_plugin_tieba_moderator.core.models import (
    Author,
    CaseStatus,
    ImageFetchError,
    ItemKind,
    JudgeError,
    TiebaActionError,
    Verdict,
)
from astrbot_plugin_tieba_moderator.core.store import Store
from astrbot_plugin_tieba_moderator.services import tieba_gateway as gateway_module
from astrbot_plugin_tieba_moderator.services.case_service import CaseService
from astrbot_plugin_tieba_moderator.services.patrol import Patrol, PatrolSettings
from astrbot_plugin_tieba_moderator.services.tieba_gateway import PostBatch
from astrbot_plugin_tieba_moderator.tests.fakes import (
    Clock,
    FakeConfig,
    FakeEvent,
    FakeGateway,
    make_item,
    make_snapshot,
)


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    store = Store(tmp_path / "moderator.db", clock=clock)
    gateway = FakeGateway()
    judge = AsyncMock(return_value=Verdict(violation=False))
    notify = AsyncMock()

    def build(**settings):
        return Patrol(
            gateway=gateway,
            store=store,
            judge=judge,
            notify=notify,
            settings=PatrolSettings(forums=("test",), **settings),
            clock=clock,
        )

    yield store, gateway, judge, notify, build
    store.close()


@pytest.mark.asyncio
async def test_first_thread_then_only_new_floor(env):
    store, gateway, judge, _, build = env
    patrol = build()
    first = await patrol.run_once()
    assert first.reviewed == 1
    assert store.is_seen("thread:100")
    assert store.get_thread_mark(10) == 2_000_000_000
    assert (await patrol.run_once()).reviewed == 0
    post = make_item(kind=ItemKind.POST, pid=101, floor=2, create_time=2_000_000_010)
    gateway.list_threads.return_value = [make_snapshot(last_time=2_000_000_010)]
    gateway.get_new_posts.return_value = PostBatch(pairs=((post, 0),))
    assert (await patrol.run_once()).reviewed == 1
    assert [call.args[0].key for call in judge.await_args_list] == [
        "thread:100",
        "post:101",
    ]
    assert store.get_post_mark(101) == 0


@pytest.mark.asyncio
async def test_comment_count_increment_only_reviews_unseen_comments(env):
    store, gateway, judge, _, build = env
    post = make_item(kind=ItemKind.POST, pid=101, floor=2)
    old = make_item(kind=ItemKind.COMMENT, pid=201, floor=2)
    new = replace(old, pid=202)
    gateway.get_new_posts.return_value = PostBatch(pairs=((post, 1),))
    gateway.get_comments.return_value = ([old], True)
    patrol = build()
    assert (await patrol.run_once()).reviewed == 3
    gateway.list_threads.return_value = [make_snapshot(last_time=2_000_000_020)]
    gateway.get_new_posts.return_value = PostBatch(comment_parents=((post, 2),))
    gateway.get_comments.return_value = ([old, new], True)
    assert (await patrol.run_once()).reviewed == 1
    assert judge.await_args.args[0] == new
    assert store.get_post_mark(101) == 2
    gateway.list_threads.return_value = [make_snapshot(last_time=2_000_000_030)]
    assert (await patrol.run_once()).reviewed == 0
    assert gateway.get_comments.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("desc_has_first_floor", [False, True])
async def test_new_thread_reviews_first_floor_comments_once(env, desc_has_first_floor):
    store, gateway, judge, _, build = env
    first = make_item(text="complete ASC body")
    comment = make_item(kind=ItemKind.COMMENT, pid=201, floor=1)
    gateway.get_thread_item.return_value = (first, 1)
    gateway.get_new_posts.return_value = PostBatch(
        pairs=((replace(first, text="DESC body"), 2),) if desc_has_first_floor else ()
    )
    gateway.get_comments.return_value = ([comment], True)
    report = await build().run_once()
    assert report.reviewed == 2
    assert [call.args[0] for call in judge.await_args_list] == [first, comment]
    assert gateway.get_comments.await_count == 1
    assert gateway.get_comments.await_args.args[1] == (2 if desc_has_first_floor else 1)
    assert store.get_post_mark(first.pid) == (2 if desc_has_first_floor else 1)


@pytest.mark.asyncio
async def test_old_thread_first_floor_comment_increment(env):
    store, gateway, judge, _, build = env
    first = make_item()
    old = make_item(kind=ItemKind.COMMENT, pid=201, floor=1)
    new = replace(old, pid=202)
    gateway.get_thread_item.return_value = (first, 1)
    gateway.get_comments.return_value = ([old], True)
    patrol = build()
    assert (await patrol.run_once()).reviewed == 2
    gateway.list_threads.return_value = [make_snapshot(last_time=2_000_000_020)]
    gateway.get_new_posts.return_value = PostBatch(comment_parents=((first, 2),))
    gateway.get_comments.return_value = ([old, new], True)
    assert (await patrol.run_once()).reviewed == 1
    assert [call.args[0].key for call in judge.await_args_list] == [
        "thread:100",
        "comment:201",
        "comment:202",
    ]
    assert gateway.get_thread_item.await_count == 1
    assert store.get_post_mark(first.pid) == 2


@pytest.mark.asyncio
async def test_first_floor_comment_deferred_without_desc_floor_one(env):
    store, gateway, judge, _, build = env
    first = make_item()
    comment = make_item(kind=ItemKind.COMMENT, pid=201, floor=1)
    gateway.get_thread_item.return_value = (first, 1)
    gateway.get_comments.return_value = ([comment], True)
    patrol = build(max_items_per_round=1)
    report = await patrol.run_once()
    assert (report.reviewed, report.deferred) == (1, 1)
    assert store.get_thread_mark(first.tid) is None
    assert store.get_post_mark(first.pid) is None
    assert (await patrol.run_once()).reviewed == 1
    assert [call.args[0].key for call in judge.await_args_list] == [
        "thread:100",
        "comment:201",
    ]
    assert store.get_post_mark(first.pid) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("filter_kind", ["old", "name", "portrait", "bawu"])
async def test_filtered_items_seen_without_judging(env, filter_kind):
    store, gateway, judge, _, build = env
    item = make_item()
    settings = {}
    if filter_kind == "old":
        item = replace(item, create_time=1)
    elif filter_kind == "bawu":
        item = replace(item, author=replace(item.author, is_bawu=True))
    else:
        settings["whitelist"] = frozenset(
            [item.author.user_name if filter_kind == "name" else item.author.portrait]
        )
    gateway.get_thread_item.return_value = (item, 0)
    report = await build(**settings).run_once()
    assert report.skipped == 1
    assert store.is_seen(item.key)
    judge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["is_top", "is_good", "is_livepost"])
async def test_skipped_thread_types_are_not_fetched(env, flag):
    store, gateway, judge, _, build = env
    gateway.list_threads.return_value = [make_snapshot(**{flag: True})]
    await build().run_once()
    gateway.get_thread_item.assert_not_awaited()
    judge.assert_not_awaited()
    assert store.get_thread_mark(10) is None


@pytest.mark.asyncio
async def test_disabled_filters_and_post_review(env):
    _, gateway, judge, _, build = env
    gateway.list_threads.return_value = [make_snapshot(is_top=True)]
    item = make_item()
    gateway.get_thread_item.return_value = (
        replace(item, author=replace(item.author, is_bawu=True)),
        0,
    )
    report = await build(
        skip_top_and_good=False, skip_bawu=False, review_posts=False
    ).run_once()
    assert report.reviewed == 1
    judge.assert_awaited_once()
    gateway.get_new_posts.assert_not_awaited()


@pytest.mark.asyncio
async def test_cap_preserves_entire_thread_marks_until_next_round(env):
    store, gateway, judge, _, build = env
    post = make_item(kind=ItemKind.POST, pid=101, floor=2)
    comment = make_item(kind=ItemKind.COMMENT, pid=201, floor=2)
    gateway.get_new_posts.return_value = PostBatch(pairs=((post, 1),))
    gateway.get_comments.return_value = ([comment], True)
    patrol = build(max_items_per_round=2)
    report = await patrol.run_once()
    assert (report.reviewed, report.deferred) == (2, 1)
    assert store.get_thread_mark(10) is None
    assert store.get_post_mark(101) is None
    assert not store.is_seen(comment.key)
    report = await patrol.run_once()
    assert (report.reviewed, report.deferred) == (1, 0)
    assert store.get_thread_mark(10) == 2_000_000_000
    assert store.get_post_mark(101) == 1
    assert [call.args[0].key for call in judge.await_args_list] == [
        "thread:100",
        "post:101",
        "comment:201",
    ]


@pytest.mark.asyncio
async def test_cap_is_round_wide_and_duplicate_items_do_not_consume_it(env):
    store, gateway, judge, _, build = env
    post = make_item(kind=ItemKind.POST, pid=101, floor=2)
    gateway.get_new_posts.return_value = PostBatch(pairs=((post, 0), (post, 0)))
    second = make_snapshot(tid=20)
    gateway.list_threads.return_value = [make_snapshot(), second]
    gateway.get_thread_item.side_effect = lambda snap, *, budget: (
        make_item(tid=snap.tid, pid=snap.tid * 10),
        0,
    )
    report = await build(max_items_per_round=2).run_once()
    assert (report.reviewed, report.deferred) == (2, 1)
    assert store.get_thread_mark(10) is not None
    assert store.get_thread_mark(20) is None
    assert judge.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [JudgeError("invalid judgment"), RuntimeError("judge down")]
)
async def test_failure_retries_then_creates_review_failed_case(env, error):
    store, _, judge, notify, build = env
    judge.side_effect = error
    patrol = build(max_failures=2)
    first = await patrol.run_once()
    assert first.reviewed == 1
    assert first.reported == 0
    assert not store.is_seen("thread:100")
    assert store.get_thread_mark(10) is None
    assert store.count_cases(CaseStatus.PENDING) == 0
    second = await patrol.run_once()
    assert second.reviewed == 1
    assert second.reported == 1
    assert store.is_seen("thread:100")
    assert store.get_thread_mark(10) is not None
    assert second.errors
    # The abandoned item is handed to a moderator, never silently dropped.
    case = store.list_cases(CaseStatus.PENDING, 1)[0]
    assert case.verdict.category == "审查失败"
    assert case.verdict.violation is False
    assert case.verdict.confidence == 0.0
    assert "自动审查失败 2 次" in case.verdict.reason
    assert str(error) in case.verdict.reason
    notify.assert_awaited_once_with(case)
    assert (await patrol.run_once()).reviewed == 0


@pytest.mark.asyncio
async def test_failed_case_notification_error_keeps_case_and_marks_seen(env):
    store, _, judge, notify, build = env
    judge.side_effect = ImageFetchError("download failed")
    notify.side_effect = RuntimeError("no session")
    report = await build(max_failures=1).run_once()
    assert report.reported == 1
    assert any("notification failed" in error for error in report.errors)
    assert store.count_cases(CaseStatus.PENDING) == 1
    assert store.is_seen("thread:100")


@pytest.mark.asyncio
async def test_consecutive_judge_errors_abort_the_round_without_consuming_retries(env):
    store, gateway, judge, notify, build = env
    posts = [
        make_item(kind=ItemKind.POST, pid=101 + index, floor=2) for index in range(5)
    ]
    gateway.get_new_posts.return_value = PostBatch(
        pairs=tuple((post, 0) for post in posts)
    )
    judge.side_effect = JudgeError("provider unavailable")
    patrol = build(max_failures=2, max_consecutive_judge_failures=3)
    report = await patrol.run_once()
    # With judge_concurrency=3, three items reach the judge and trip the breaker;
    # the rest of the batch is deferred without counting a failure.
    assert report.reviewed == 3
    assert report.deferred == 3
    assert report.reported == 0
    assert store.count_cases(CaseStatus.PENDING) == 0
    assert any("模型连续失败" in error for error in report.errors)
    # Retries were counted only for the items that actually reached the model.
    assert not store.is_seen("thread:100")
    assert store.get_thread_mark(10) is None
    assert notify.await_count == 0
    judge.side_effect = None
    judge.return_value = Verdict(violation=False)
    assert (await patrol.run_once()).reviewed == 6


@pytest.mark.asyncio
async def test_judge_breaker_resets_after_a_success(env):
    store, gateway, judge, _, build = env
    posts = [
        make_item(kind=ItemKind.POST, pid=101 + index, floor=2) for index in range(4)
    ]
    gateway.get_new_posts.return_value = PostBatch(
        pairs=tuple((post, 0) for post in posts)
    )
    calls = 0

    async def judging(item, images):
        nonlocal calls
        calls += 1
        if calls in (1, 2, 4, 5):
            raise JudgeError("provider unavailable")
        return Verdict(violation=False)

    judge.side_effect = judging
    report = await build(max_failures=5, max_consecutive_judge_failures=3).run_once()
    assert report.reviewed == 5
    assert report.deferred == 0
    assert not any("模型连续失败" in error for error in report.errors)


@pytest.mark.asyncio
async def test_failed_judgment_can_succeed_next_round(env):
    store, _, judge, _, build = env
    judge.side_effect = [JudgeError("retry"), Verdict(violation=False)]
    patrol = build()
    await patrol.run_once()
    assert (await patrol.run_once()).reviewed == 1
    assert store.is_seen("thread:100")
    assert store.get_thread_mark(10) is not None


@pytest.mark.asyncio
async def test_failed_comment_does_not_repeat_successful_siblings(env):
    store, gateway, judge, _, build = env
    post = make_item(kind=ItemKind.POST, pid=101, floor=2)
    comment = make_item(kind=ItemKind.COMMENT, pid=201, floor=2)
    gateway.get_new_posts.return_value = PostBatch(pairs=((post, 1),))
    gateway.get_comments.return_value = ([comment], True)

    async def judging(item, images):
        if item.kind is ItemKind.COMMENT:
            raise JudgeError("comment unavailable")
        return Verdict(violation=False)

    judge.side_effect = judging
    patrol = build()
    assert (await patrol.run_once()).reviewed == 3
    assert store.get_thread_mark(10) is None
    assert store.get_post_mark(101) is None
    assert store.is_seen(post.key)
    judge.side_effect = None
    assert (await patrol.run_once()).reviewed == 1
    assert judge.await_args.args[0] == comment
    assert store.get_post_mark(101) == 1
    assert store.get_thread_mark(10) is not None


@pytest.mark.asyncio
async def test_image_failure_never_calls_judge_and_retries(env):
    store, gateway, judge, _, build = env
    gateway.get_thread_item.return_value = (make_item(image_urls=("fake://image",)), 0)
    gateway.fetch_images.side_effect = ImageFetchError("download failed")
    patrol = build()
    report = await patrol.run_once()
    assert report.reviewed == 0
    assert not store.is_seen("thread:100")
    assert store.get_thread_mark(10) is None
    judge.assert_not_awaited()
    gateway.fetch_images.side_effect = None
    await patrol.run_once()
    assert judge.await_args.args[1] == ["data:image/png;base64,ZmFrZQ=="]


@pytest.mark.asyncio
async def test_images_can_be_disabled(env):
    _, gateway, judge, _, build = env
    gateway.get_thread_item.return_value = (make_item(image_urls=("fake://image",)), 0)
    await build(review_images=False).run_once()
    gateway.fetch_images.assert_not_awaited()
    assert judge.await_args.args[1] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["report", "auto", "failed_auto", "below"])
async def test_verdict_disposition_and_notification(env, action):
    store, gateway, judge, notify, build = env
    judge.return_value = Verdict(
        violation=True,
        category="spam",
        confidence=0.5 if action == "below" else 0.95,
        reason="advertisement",
    )
    if action == "failed_auto":
        gateway.delete.side_effect = TiebaActionError("code=4 delete rejected")
    report = await build(auto_delete_enabled=action != "report").run_once()
    if action == "below":
        assert report.reported == report.auto_deleted == 0
        notify.assert_not_awaited()
        gateway.delete.assert_not_awaited()
    else:
        status = CaseStatus.AUTO_DELETED if action == "auto" else CaseStatus.PENDING
        case = store.list_cases(status, 10)[0]
        assert notify.await_args.args[0] == case
        assert (report.auto_deleted, report.reported) == (
            (1, 0) if action == "auto" else (0, 1)
        )
        if action == "failed_auto":
            assert "delete rejected" in case.note
        if action == "report":
            gateway.delete.assert_not_awaited()
    assert store.is_seen("thread:100")


@pytest.mark.asyncio
async def test_notification_failure_does_not_change_case_or_progress(env):
    store, _, judge, notify, build = env
    judge.return_value = Verdict(violation=True, confidence=0.8)
    notify.side_effect = RuntimeError("notification unavailable")
    report = await build().run_once()
    assert report.reported == 1
    assert any("notification unavailable" in error for error in report.errors)
    assert store.count_cases(CaseStatus.PENDING) == 1
    assert store.is_seen("thread:100")
    assert store.get_thread_mark(10) is not None


@pytest.mark.asyncio
async def test_forum_error_does_not_abort_next_forum(env):
    store, gateway, judge, notify, _ = env
    gateway.list_threads.side_effect = [RuntimeError("bad forum"), [make_snapshot()]]
    patrol = Patrol(
        gateway=gateway,
        store=store,
        judge=judge,
        notify=notify,
        settings=PatrolSettings(forums=("bad", "test")),
        clock=Clock(),
    )
    report = await patrol.run_once()
    assert report.reviewed == 1
    assert report.forums == ("bad", "test")
    assert any("bad forum" in error for error in report.errors)


@pytest.mark.asyncio
async def test_thread_error_does_not_abort_next_thread_or_advance_marks(env):
    store, gateway, _, _, build = env
    gateway.list_threads.return_value = [make_snapshot(), make_snapshot(tid=20)]
    gateway.get_thread_item.side_effect = [
        RuntimeError("bad thread"),
        (make_item(tid=20, pid=200), 0),
    ]
    report = await build().run_once()
    assert report.reviewed == 1
    assert store.get_thread_mark(10) is None
    assert store.get_thread_mark(20) is not None
    assert any("bad thread" in error for error in report.errors)


@pytest.mark.asyncio
async def test_comment_fetch_failure_keeps_marks_for_retry(env):
    store, gateway, _, _, build = env
    post = make_item(kind=ItemKind.POST, pid=101, floor=2)
    gateway.get_new_posts.return_value = PostBatch(pairs=((post, 1),))
    gateway.get_comments.side_effect = RuntimeError("comments unavailable")
    patrol = build()
    report = await patrol.run_once()
    assert report.errors
    assert store.get_thread_mark(10) is None
    assert store.get_post_mark(101) is None
    gateway.get_comments.side_effect = None
    assert (await patrol.run_once()).reviewed == 2


@pytest.mark.asyncio
async def test_comments_disabled_does_not_fetch_them(env):
    store, gateway, _, _, build = env
    gateway.get_new_posts.return_value = PostBatch(
        pairs=((make_item(kind=ItemKind.POST, pid=101, floor=2), 5),)
    )
    await build(review_comments=False).run_once()
    gateway.get_comments.assert_not_awaited()
    assert store.get_post_mark(101) == 5


@pytest.mark.asyncio
async def test_run_once_calls_do_not_overlap(env):
    _, gateway, _, _, build = env
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    active = 0
    peak = 0

    async def listing(*args, **kwargs):
        nonlocal calls, active, peak
        calls += 1
        active += 1
        peak = max(peak, active)
        entered.set()
        await release.wait()
        active -= 1
        return [make_snapshot()]

    gateway.list_threads.side_effect = listing
    patrol = build()
    first = asyncio.create_task(patrol.run_once())
    await entered.wait()
    second = asyncio.create_task(patrol.run_once())
    await asyncio.sleep(0)
    assert calls == 1
    assert not second.done()
    release.set()
    reports = await asyncio.gather(first, second)
    assert peak == 1
    assert sum(report.reviewed for report in reports) == 1
    assert reports[1].started_at >= reports[0].finished_at


@pytest.mark.asyncio
async def test_judge_concurrency_is_bounded_and_used(env):
    _, gateway, judge, _, build = env
    gateway.get_new_posts.return_value = PostBatch(
        pairs=tuple(
            (make_item(kind=ItemKind.POST, pid=101 + index, floor=2 + index), 0)
            for index in range(4)
        )
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def judging(*args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1
        return Verdict(violation=False)

    judge.side_effect = judging
    task = asyncio.create_task(build(judge_concurrency=2).run_once())
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert peak == 2
    release.set()
    report = await task
    assert report.reviewed == 5
    assert peak == 2


@pytest.mark.asyncio
async def test_round_purges_old_deduplication_but_preserves_recent(tmp_path):
    clock = Clock(1)
    store = Store(tmp_path / "purge.db", clock=clock)
    try:
        store.mark_seen("post:old")
        clock.now = 2_000_000_000
        store.mark_seen("post:recent")
        gateway = FakeGateway()
        gateway.list_threads.return_value = []
        patrol = Patrol(
            gateway=gateway,
            store=store,
            judge=AsyncMock(),
            notify=AsyncMock(),
            settings=PatrolSettings(forums=("test",)),
            clock=clock,
        )
        await patrol.run_once()
        assert not store.is_seen("post:old")
        assert store.is_seen("post:recent")
    finally:
        store.close()


async def _collect(agen) -> list[str]:
    """Drain a command generator into a list of replies."""
    return [reply async for reply in agen]


@pytest.fixture
def command_env(tmp_path):
    """Run commands against real services and an in-memory remote substitute."""
    clock = Clock()
    config = FakeConfig(
        forums=["test"],
        patrol={"enabled": False},
        notify={"sessions": ["offline:group:test"]},
    )
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    plugin = entry.TiebaModeratorPlugin(context, config)
    store = Store(tmp_path / "commands.db", clock=clock)
    plugin._store = store
    plugin._gateway = FakeGateway()
    plugin._judge = AsyncMock(
        return_value=Verdict(violation=True, confidence=0.8, reason="spam evidence")
    )
    plugin._cases = CaseService(
        gateway=plugin._gateway, store=plugin._store, clock=clock
    )
    plugin._patrol = Patrol(
        gateway=plugin._gateway,
        store=plugin._store,
        judge=plugin._judge,
        notify=plugin._notify,
        settings=plugin._settings,
        clock=clock,
    )
    yield plugin, FakeEvent()
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("review_images", [False, True])
async def test_check_uses_real_metadata_without_creating_cases(
    command_env, review_images
):
    plugin, event = command_env
    item = make_item(
        title="actual thread title", fname="actual forum", image_urls=("fake://image",)
    )
    plugin._gateway.get_thread_item_by_tid.return_value = item
    plugin._settings = replace(plugin._settings, review_images=review_images)
    replies = [reply async for reply in plugin.tb_check(event, "10")]
    assert item.title in replies[0] and item.fname in replies[0]
    assert plugin._judge.await_args.args[0] == item
    assert plugin._judge.await_args.args[1] == (
        ["data:image/png;base64,ZmFrZQ=="] if review_images else []
    )
    assert plugin._store.count_cases(CaseStatus.PENDING) == 0
    plugin._gateway.delete.assert_not_awaited()
    plugin._gateway.block.assert_not_awaited()
    plugin._gateway.get_thread_item.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tid", ["", "0", "-1", "x", "1" * 19])
async def test_check_invalid_tid_does_not_fetch_or_judge(command_env, tid):
    plugin, event = command_env
    [reply async for reply in plugin.tb_check(event, tid)]
    plugin._gateway.get_thread_item_by_tid.assert_not_awaited()
    plugin._judge.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_without_account_does_not_judge(command_env):
    plugin, event = command_env
    plugin._gateway = None
    [reply async for reply in plugin.tb_check(event, "10")]
    plugin._judge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [TiebaActionError("thread deleted"), ImageFetchError("download failed")]
)
async def test_check_fetch_failure_stops_review(command_env, failure):
    plugin, event = command_env
    plugin._gateway.get_thread_item_by_tid.side_effect = failure
    replies = [reply async for reply in plugin.tb_check(event, "10")]
    assert str(failure) in replies[0]
    plugin._judge.assert_not_awaited()
    assert plugin._store.count_cases(CaseStatus.PENDING) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [JudgeError("judge unavailable"), RuntimeError("judge interrupted")]
)
async def test_check_failed_judgment_never_takes_action(command_env, failure):
    plugin, event = command_env
    plugin._judge.side_effect = failure
    [reply async for reply in plugin.tb_check(event, "10")]
    assert plugin._store.count_cases(CaseStatus.PENDING) == 0
    plugin._gateway.delete.assert_not_awaited()
    plugin._gateway.block.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_patrol_reports_and_commands_transition_real_case(command_env):
    plugin, event = command_env
    [reply async for reply in plugin.tb_run(event)]
    assert plugin._last_report.reported == 1
    assert plugin.context.send_message.await_count == 1
    case = plugin._store.list_cases(CaseStatus.PENDING, 1)[0]
    listing = [reply async for reply in plugin.tb_list(event)]
    details = [reply async for reply in plugin.tb_show(event, str(case.id))]
    status = [reply async for reply in plugin.tb_status(event)]
    assert f"#{case.id}" in listing[0] and case.item.title in listing[0]
    assert case.item.link in details[0]
    assert case.item.fname in status[0]
    [reply async for reply in plugin.tb_del(event, str(case.id))]
    assert plugin._store.get_case(case.id).status is CaseStatus.DELETED
    plugin._gateway.delete.assert_awaited_once()
    [reply async for reply in plugin.tb_ban(event, str(case.id), "3")]
    assert plugin._store.get_case(case.id).status is CaseStatus.BANNED
    assert plugin._gateway.delete.await_count == 1
    [reply async for reply in plugin.tb_restore(event, str(case.id))]
    assert plugin._store.get_case(case.id).status is CaseStatus.RESTORED
    [reply async for reply in plugin.tb_ignore(event, str(case.id))]
    assert plugin._store.get_case(case.id).status is CaseStatus.RESTORED


@pytest.mark.asyncio
async def test_ignore_command_removes_case_from_pending_list(command_env):
    plugin, event = command_env
    case = plugin._store.create_case(
        make_item(), Verdict(violation=True), CaseStatus.PENDING, 1
    )
    [reply async for reply in plugin.tb_ignore(event, str(case.id))]
    [reply async for reply in plugin.tb_list(event)]
    assert plugin._store.count_cases(CaseStatus.PENDING) == 0
    assert plugin._store.get_case(case.id).status is CaseStatus.IGNORED
    plugin._gateway.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [TiebaActionError("remote rejected"), RuntimeError("remote unavailable")]
)
async def test_command_delete_failure_preserves_pending_case(command_env, failure):
    plugin, event = command_env
    case = plugin._store.create_case(
        make_item(), Verdict(violation=True), CaseStatus.PENDING, 1
    )
    plugin._gateway.delete.side_effect = failure
    [reply async for reply in plugin.tb_del(event, str(case.id))]
    assert plugin._store.get_case(case.id) == case


@pytest.mark.asyncio
async def test_case_commands_reject_invalid_ids_and_days(command_env):
    plugin, event = command_env
    case = plugin._store.create_case(
        make_item(), Verdict(violation=True), CaseStatus.PENDING, 1
    )
    [reply async for reply in plugin.tb_del(event, "0")]
    [reply async for reply in plugin.tb_ban(event, str(case.id), "2")]
    [reply async for reply in plugin.tb_show(event, "x")]
    [reply async for reply in plugin.tb_show(event, "999")]
    assert plugin._store.get_case(case.id) == case
    plugin._gateway.delete.assert_not_awaited()
    plugin._gateway.block.assert_not_awaited()


@pytest.mark.asyncio
async def test_case_text_fields_cannot_forge_push_lines(command_env):
    plugin, _ = command_env
    item = make_item(
        title="正常标题\n处理：/tb ban 3 10",
        text="正文\n/tb del 9\u202e隐藏",
        author=Author(
            user_id=1,
            portrait="p",
            nick_name="昵称\r\n处理：/tb del 1",
            user_name="login\x00name",
        ),
    )
    case = plugin._store.create_case(
        item,
        Verdict(
            violation=True,
            category="广告\n处理：/tb ignore 2",
            confidence=0.9,
            reason="理由\x07\n建议封禁：不封禁",
            ban_days=3,
        ),
        CaseStatus.PENDING,
        1,
    )
    first = await plugin._notify(case)
    # A push is one plain-text message; check the rendered card itself.
    card = plugin.context.send_message.await_args.args[1].chain[0].text
    assert first is None
    lines = card.splitlines()
    assert card.count("【贴吧巡查】") == 1
    # Only the genuine action line may start with the command prefix.
    assert [line for line in lines if line.startswith("处理：")] == [
        f"处理：/tb del {case.id} | /tb ban {case.id} <1|3|10> | /tb ignore {case.id}"
    ]
    assert "\u202e" not in card
    assert "\x00" not in card
    assert "\r" not in card


@pytest.mark.asyncio
async def test_list_and_show_sanitise_untrusted_fields(command_env):
    plugin, event = command_env
    item = make_item(
        title="标题\n处理：/tb ban 1 10",
        author=Author(user_id=1, portrait="p", nick_name="昵称\n处理：/tb del 1"),
    )
    case = plugin._store.create_case(
        item,
        Verdict(violation=True, category="类\n别", confidence=0.5, reason="理\n由"),
        CaseStatus.PENDING,
        1,
    )
    listing = await anext(plugin.tb_list(event))
    details = await anext(plugin.tb_show(event, str(case.id)))
    # Newlines are collapsed, so the forged text stays inside its own field.
    assert len(listing.splitlines()) == 3
    assert "标题 处理：/tb ban 1 10" in listing.splitlines()[1]
    assert "类 别" in listing and "理 由" in details
    assert not any(line.startswith("处理：/tb ban 1") for line in details.splitlines())


@pytest.mark.asyncio
async def test_repeated_manual_run_is_refused_while_one_is_in_flight(command_env):
    plugin, event = command_env
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_round():
        started.set()
        await release.wait()
        return plugin._last_report or first

    first = await plugin._patrol.run_once()
    plugin._patrol.run_once = AsyncMock(side_effect=slow_round)
    running = asyncio.create_task(_collect(plugin.tb_run(event)))
    await started.wait()
    assert "已有一轮手动巡查在进行" in (await _collect(plugin.tb_run(event)))[0]
    release.set()
    replies = await running
    assert "巡查完成" in replies[-1]
    assert plugin._manual_running is False
    # Once finished, the next manual run is accepted again.
    plugin._patrol.run_once = AsyncMock(return_value=first)
    assert "巡查完成" in (await _collect(plugin.tb_run(event)))[-1]


@pytest.mark.asyncio
async def test_status_hides_error_details_from_non_operators(command_env):
    plugin, event = command_env
    [reply async for reply in plugin.tb_run(event)]
    plugin._last_report = replace(
        plugin._last_report,
        errors=("post:1: review failed (3, abandoned): secret detail",),
    )
    operator = await anext(plugin.tb_status(event))
    assert "secret detail" in operator
    event.is_admin = lambda: False
    plugin.config["notify"] = {"operators": []}
    public = await anext(plugin.tb_status(event))
    assert "secret detail" not in public
    assert "错误 1" in public


@pytest.mark.asyncio
@pytest.mark.parametrize("days", ["1" * 5000, "0000000003", "1_0", " 3 "])
async def test_ban_days_parsing_is_bounded(command_env, days):
    plugin, event = command_env
    case = plugin._store.create_case(
        make_item(), Verdict(violation=True), CaseStatus.PENDING, 1
    )
    replies = await _collect(plugin.tb_ban(event, str(case.id), days))
    if days.strip() == "3":
        assert plugin._store.get_case(case.id).status is CaseStatus.BANNED
    else:
        assert "封禁天数只能是" in replies[0]
        assert plugin._store.get_case(case.id).status is CaseStatus.PENDING
        plugin._gateway.block.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_review_case_renders_as_manual_check(command_env):
    plugin, event = command_env
    case = plugin._store.create_case(
        make_item(),
        Verdict(
            violation=False,
            category=FAILED_CATEGORY,
            confidence=0.0,
            reason="自动审查失败 3 次，请人工查看：timeout",
        ),
        CaseStatus.PENDING,
        1,
    )
    card = await anext(plugin.tb_show(event, str(case.id)))
    assert "审查失败（未能自动判定）" in card
    assert "置信度" not in card
    assert "建议封禁" not in card
    assert "请点链接人工判断" in card
    listing = await anext(plugin.tb_list(event))
    assert "审查失败" in listing and "0.00" not in listing


@pytest.mark.asyncio
async def test_manual_patrol_failure_preserves_previous_report(command_env):
    plugin, event = command_env
    [reply async for reply in plugin.tb_run(event)]
    previous = plugin._last_report
    plugin._patrol.run_once = AsyncMock(side_effect=RuntimeError("round failed"))
    [reply async for reply in plugin.tb_run(event)]
    assert plugin._last_report is previous
    assert plugin._store.count_cases(CaseStatus.PENDING) == 1


@pytest.mark.asyncio
async def test_binding_is_idempotent_and_preserves_other_notification_settings(
    command_env,
):
    plugin, event = command_env
    plugin.config["notify"] = {"sessions": [], "operators": ["moderator"]}
    [reply async for reply in plugin.tb_bind(event)]
    [reply async for reply in plugin.tb_bind(event)]
    assert plugin.config["notify"] == {
        "sessions": [event.unified_msg_origin],
        "operators": ["moderator"],
    }
    assert plugin.config.saves == 1


@pytest.mark.asyncio
async def test_notification_attempts_all_sessions_and_reports_partial_failure(
    command_env,
):
    plugin, _ = command_env
    plugin.config["notify"] = {"sessions": ["first", "second", "third"]}
    plugin.context.send_message.side_effect = [
        RuntimeError("platform offline"),
        False,
        True,
    ]
    case = plugin._store.create_case(
        make_item(), Verdict(violation=True), CaseStatus.PENDING, 1
    )
    with pytest.raises(RuntimeError):
        await plugin._notify(case)
    assert [call.args[0] for call in plugin.context.send_message.await_args_list] == [
        "first",
        "second",
        "third",
    ]
    assert plugin._store.get_case(case.id) == case
    plugin.config["notify"] = {"sessions": []}
    with pytest.raises(RuntimeError):
        await plugin._notify(case)
    assert plugin.context.send_message.await_count == 3


@pytest.mark.asyncio
async def test_initialize_rejects_malformed_credentials_without_echoing_them(
    tmp_path, monkeypatch
):
    logged: list[str] = []
    monkeypatch.setattr(entry.StarTools, "get_data_dir", lambda name: tmp_path)
    monkeypatch.setattr(
        entry, "logger", SimpleNamespace(error=logged.append, warning=logged.append)
    )
    bad_bduss = "b" * 40
    bad_stoken = "s" * 20
    plugin = entry.TiebaModeratorPlugin(
        SimpleNamespace(send_message=AsyncMock(return_value=True)),
        FakeConfig(
            account={"bduss": bad_bduss, "stoken": bad_stoken},
            forums=["test"],
            patrol={"enabled": True},
        ),
    )
    await plugin.initialize()
    try:
        assert plugin._gateway is None
        assert plugin._patrol is None
        assert plugin._cases is None
        assert plugin._task is None
        text = "\n".join(logged)
        assert "格式不正确" in text
        assert "不要带 BDUSS= 前缀或引号" in text
        assert bad_bduss not in text and bad_stoken not in text
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_initialize_manual_mode_and_terminate_release_services(
    tmp_path, monkeypatch
):
    gateway = FakeGateway()
    clock = Clock()
    monkeypatch.setattr(entry.StarTools, "get_data_dir", lambda name: tmp_path)
    monkeypatch.setattr(entry, "TiebaGateway", lambda *args: gateway)
    monkeypatch.setattr(entry, "Store", lambda path: Store(path, clock=clock))
    plugin = entry.TiebaModeratorPlugin(
        SimpleNamespace(send_message=AsyncMock(return_value=True)),
        FakeConfig(
            account={"bduss": "fake-bduss", "stoken": "fake-stoken"},
            forums=["test"],
            patrol={"enabled": False},
        ),
    )
    await plugin.initialize()
    try:
        assert plugin._task is None
        assert plugin._cases is not None and plugin._patrol is not None
        case = plugin._store.create_case(
            make_item(), Verdict(violation=True), CaseStatus.PENDING, 1
        )
        await plugin._cases.ignore(case.id, "moderator")
    finally:
        await plugin.terminate()
    gateway.close.assert_awaited_once()
    with_store = Store(tmp_path / entry.DB_FILE, clock=clock)
    try:
        assert with_store.get_case(case.id).status is CaseStatus.IGNORED
    finally:
        with_store.close()


@pytest.mark.asyncio
async def test_background_loop_records_round_and_terminates(command_env, monkeypatch):
    plugin, event = command_env
    entered = asyncio.Event()
    blocker = asyncio.Event()
    sleep_calls = 0

    async def sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            entered.set()
            await blocker.wait()

    monkeypatch.setattr(entry.asyncio, "sleep", sleep)
    plugin._enabled = True
    plugin._task = asyncio.create_task(plugin._patrol_loop())
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert plugin._last_report.reported == 1
    status = [reply async for reply in plugin.tb_status(event)]
    assert str(plugin._interval) in status[0]
    await plugin.terminate()
    assert plugin._task is None and plugin._gateway is None


@pytest.mark.asyncio
async def test_page_caps_warn_and_advance_successful_thread_and_comment_marks(env):
    store, gateway, judge, _, build = env
    previous_time = 1_999_999_990
    store.set_thread_mark(10, previous_time)
    post = make_item(kind=ItemKind.POST, pid=101, floor=2)
    comment = make_item(kind=ItemKind.COMMENT, pid=201, floor=2)
    store.set_post_mark(post.pid, 2)
    gateway.get_new_posts.return_value = PostBatch(pairs=((post, 10),), caught_up=False)
    gateway.get_comments.return_value = ([comment], False)
    report = await build(max_post_pages=2, max_comment_pages=4).run_once()
    assert report.reviewed == 2
    assert report.deferred == 0
    assert report.errors == (
        "帖子 10 两轮之间新增楼层过多，较早的 2 页之外的新楼层未审查",
        "帖子 10 楼层 101 两轮之间新增楼中楼过多，较早的 4 页之外的新楼中楼未审查",
    )
    assert gateway.get_new_posts.await_args.args == (make_snapshot(), previous_time, 2)
    assert gateway.get_comments.await_args.args == (post, 10, 2, 4)
    assert store.get_thread_mark(10) == make_snapshot().last_time
    assert store.get_post_mark(101) == 10
    assert [call.args[0] for call in judge.await_args_list] == [post, comment]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_pagination_warning_does_not_override_unresolved_reviews(env, failure):
    store, gateway, judge, _, build = env
    previous_time = 1_999_999_990
    store.set_thread_mark(10, previous_time)
    post = make_item(kind=ItemKind.POST, pid=101, floor=2)
    gateway.get_new_posts.return_value = PostBatch(pairs=((post, 0),), caught_up=False)
    if failure:
        judge.side_effect = JudgeError("retry")
    report = await build(max_items_per_round=1 if failure else 0).run_once()
    assert report.errors
    assert store.get_thread_mark(10) == previous_time


@pytest.mark.asyncio
async def test_old_boundary_parent_scans_comments_without_reviewing_stale_body(env):
    store, gateway, judge, _, build = env
    store.set_thread_mark(10, 1_999_999_995)
    stale = make_item(kind=ItemKind.POST, pid=101, floor=2, create_time=1_999_999_990)
    comment = make_item(kind=ItemKind.COMMENT, pid=201, floor=2)
    gateway.get_new_posts.return_value = PostBatch(comment_parents=((stale, 1),))
    gateway.get_comments.return_value = ([comment], True)
    report = await build().run_once()
    assert report.reviewed == 1
    judge.assert_awaited_once_with(comment, [])
    assert not store.is_seen(stale.key)
    assert store.get_post_mark(stale.pid) == 1
    gateway.get_thread_item.assert_not_awaited()


@pytest.mark.parametrize(
    "setting,value",
    [
        ("max_tieba_requests_per_round", -1),
        ("max_post_pages", 0),
        ("max_comment_pages", 0),
        ("thread_pages", 0),
    ],
)
def test_invalid_read_limits_rejected(env, setting, value):
    *_, build = env
    with pytest.raises(ValueError, match=setting):
        build(**{setting: value})


class _PatrolPage(list):
    """Expose concrete response rows and server pagination without any I/O."""

    def __init__(self, rows, *, total_page=1, page_size=30, total_count=None):
        super().__init__(rows)
        self.err = None
        self.page = SimpleNamespace(
            total_page=total_page,
            page_size=page_size,
            total_count=len(rows) if total_count is None else total_count,
        )


class _PatrolClient:
    """Record real gateway content attempts using deterministic server pages."""

    def __init__(self):
        self.calls = []
        self.snapshots = [make_snapshot()]
        self.comment_count = 0
        self.user = SimpleNamespace(
            user_id=1,
            portrait="offline",
            user_name="reader",
            nick_name="",
            level=1,
            is_bawu=False,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get_threads(self, fname, *, pn, rn, sort):
        self.calls.append(("threads", fname, pn))
        return _PatrolPage(
            [
                SimpleNamespace(
                    tid=snap.tid,
                    fid=snap.fid,
                    fname=fname,
                    title=snap.title,
                    last_time=snap.last_time,
                    create_time=snap.create_time,
                    reply_num=snap.reply_num,
                    is_top=snap.is_top,
                    is_good=snap.is_good,
                    is_livepost=snap.is_livepost,
                    user=self.user,
                )
                for snap in self.snapshots
            ]
            if pn == 1
            else []
        )

    async def get_posts(self, tid, *, pn, rn, sort, with_comments):
        self.calls.append(("posts", tid, pn, sort))
        first = sort == gateway_module.PostSortType.ASC
        return _PatrolPage(
            [
                SimpleNamespace(
                    pid=tid * 10 if first else tid * 10 + 1,
                    floor=1 if first else 2,
                    create_time=2_000_000_000,
                    reply_num=0 if first else self.comment_count,
                    user=self.user,
                    contents=[SimpleNamespace(text="complete body")],
                )
            ]
        )

    async def get_comments(self, tid, pid, *, pn, sort):
        self.calls.append(("comments", tid, pid, pn))
        return _PatrolPage(
            [
                SimpleNamespace(
                    pid=200 + pn,
                    create_time=2_000_000_000,
                    user=self.user,
                    contents=[SimpleNamespace(text=f"comment {pn}")],
                )
            ],
            total_page=2,
            page_size=1,
            total_count=2,
        )


@pytest.fixture
def budget_env(env, monkeypatch):
    store, _, judge, notify, _ = env
    client = _PatrolClient()
    monkeypatch.setattr(gateway_module, "Client", lambda *args: client)
    gateway = gateway_module.TiebaGateway("offline", "offline")

    def build(**settings):
        return Patrol(
            gateway=gateway,
            store=store,
            judge=judge,
            notify=notify,
            settings=PatrolSettings(
                forums=settings.pop("forums", ("test",)), **settings
            ),
            clock=Clock(),
        )

    return store, client, judge, build


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit,reviewed,deferred", [(0, 0, 0), (1, 0, 1), (2, 0, 1), (3, 2, 0)]
)
async def test_real_gateway_budget_before_during_and_exact_collection(
    budget_env, limit, reviewed, deferred
):
    store, client, judge, build = budget_env
    report = await build(max_tieba_requests_per_round=limit).run_once()
    assert len(client.calls) == limit
    assert report.reviewed == reviewed
    assert report.deferred == deferred
    assert judge.await_count == reviewed
    if limit < 3:
        assert store.get_thread_mark(10) is None
        assert store.get_post_mark(101) is None
        assert report.errors == ("本轮贴吧请求达到上限，剩余帖子顺延",)
    else:
        assert store.get_thread_mark(10) == 2_000_000_000
        assert store.get_post_mark(101) == 0
        assert report.errors == ()


@pytest.mark.asyncio
async def test_budget_defers_known_eligible_threads_not_unlisted_forum_counts(
    budget_env,
):
    store, client, _, build = budget_env
    client.snapshots = [
        make_snapshot(),
        make_snapshot(tid=20),
        make_snapshot(tid=30),
        make_snapshot(tid=40, is_top=True),
        make_snapshot(tid=50, is_livepost=True),
    ]
    report = await build(
        max_tieba_requests_per_round=3, forums=("test", "unlisted")
    ).run_once()
    assert len(client.calls) == 3
    assert report.deferred == 2
    assert report.errors == ("本轮贴吧请求达到上限，剩余帖子顺延",)
    assert store.get_thread_mark(10) == 2_000_000_000
    assert store.get_thread_mark(20) is None
    assert store.get_thread_mark(30) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,reviewed", [(4, 0), (5, 3)])
async def test_budget_counts_comment_probe_and_newest_page(budget_env, limit, reviewed):
    store, client, judge, build = budget_env
    client.comment_count = 2
    report = await build(max_tieba_requests_per_round=limit).run_once()
    assert len(client.calls) == limit
    assert report.reviewed == reviewed
    assert [call[-1] for call in client.calls if call[0] == "comments"] == (
        [1] if limit == 4 else [1, 2]
    )
    if limit == 4:
        assert report.deferred == 1
        assert store.get_thread_mark(10) is None
        assert store.get_post_mark(101) is None
    else:
        assert {call.args[0].key for call in judge.await_args_list} == {
            "thread:100",
            "post:101",
            "comment:202",
        }
        assert report.errors == ()
        assert store.get_post_mark(101) == 2
        assert store.get_thread_mark(10) == 2_000_000_000


@pytest.mark.asyncio
async def test_budget_is_fresh_each_round(budget_env):
    store, client, _, build = budget_env
    patrol = build(max_tieba_requests_per_round=2)
    first = await patrol.run_once()
    second = await patrol.run_once()
    assert len(client.calls) == 4
    assert first.reviewed == 0
    assert second.reviewed == 0
    assert first.deferred == second.deferred == 1
    assert store.get_thread_mark(10) is None


@pytest.mark.asyncio
async def test_listing_is_limited_to_remaining_budget_without_losing_known_threads(
    budget_env,
):
    store, client, _, build = budget_env
    report = await build(thread_pages=5, max_tieba_requests_per_round=1).run_once()
    assert client.calls == [("threads", "test", 1)]
    assert report.deferred == 1
    assert report.errors == ("本轮贴吧请求达到上限，剩余帖子顺延",)
    assert store.get_thread_mark(10) is None


@pytest.mark.asyncio
async def test_exhausted_budget_does_not_block_images_or_moderation(env):
    store, gateway, judge, notify, build = env
    item = make_item(image_urls=("fake://image",))
    gateway.get_thread_item.return_value = (item, 0)
    judge.return_value = Verdict(violation=True, confidence=0.99)
    report = await build(
        max_tieba_requests_per_round=3, auto_delete_enabled=True
    ).run_once()
    budget = gateway.get_new_posts.await_args.kwargs["budget"]
    assert budget.used == 3
    assert budget.remaining == 0
    assert report.errors == ()
    assert report.auto_deleted == 1
    gateway.fetch_images.assert_awaited_once_with(item.image_urls, 4)
    gateway.delete.assert_awaited_once_with(item)
    notify.assert_awaited_once()
    assert store.get_thread_mark(10) == 2_000_000_000


@pytest.mark.asyncio
async def test_exact_budget_with_only_filtered_remaining_threads_does_not_warn(
    budget_env,
):
    _, client, _, build = budget_env
    client.snapshots = [make_snapshot(is_top=True)]
    report = await build(max_tieba_requests_per_round=1).run_once()
    assert len(client.calls) == 1
    assert report.deferred == 0
    assert report.errors == ()


@pytest.mark.asyncio
async def test_real_gateway_old_boundary_parent_keeps_comment_increment(budget_env):
    store, client, judge, build = budget_env
    store.set_thread_mark(10, 2_000_000_000)
    store.set_post_mark(101, 0)
    client.snapshots = [make_snapshot(last_time=2_000_000_010)]
    client.comment_count = 2
    report = await build(max_tieba_requests_per_round=4).run_once()
    assert len(client.calls) == 4
    assert report.errors == ()
    assert report.reviewed == 2
    assert {call.args[0].key for call in judge.await_args_list} == {
        "comment:201",
        "comment:202",
    }
    assert not store.is_seen("post:101")
    assert not store.is_seen("thread:100")
    assert store.get_post_mark(101) == 2
    assert store.get_thread_mark(10) == 2_000_000_010


@pytest.mark.asyncio
async def test_round_budget_object_is_not_shared_with_manual_check(env):
    _, gateway, _, _, build = env
    patrol = build(max_tieba_requests_per_round=3)
    await patrol.run_once()
    first = gateway.list_threads.await_args.kwargs["budget"]
    await gateway.get_thread_item_by_tid(10)
    assert first.used == 3
    await patrol.run_once()
    second = gateway.list_threads.await_args.kwargs["budget"]
    assert second is not first
    assert second.used == 1
    assert first.used == 3


@pytest.mark.asyncio
async def test_failed_content_attempt_consumes_budget_and_defers_next_thread(env):
    store, gateway, judge, _, build = env
    gateway.list_threads.return_value = [make_snapshot(), make_snapshot(tid=20)]
    gateway.get_thread_item.side_effect = TiebaActionError("read failed")
    report = await build(max_tieba_requests_per_round=2).run_once()
    assert gateway.list_threads.await_args.kwargs["budget"].used == 2
    assert report.deferred == 1
    assert report.errors == (
        "Thread 10: read failed",
        "本轮贴吧请求达到上限，剩余帖子顺延",
    )
    assert store.get_thread_mark(10) is None
    assert store.get_thread_mark(20) is None
    judge.assert_not_awaited()


@pytest.mark.asyncio
async def test_mid_collection_budget_defers_current_and_remaining_known_threads(
    budget_env,
):
    store, client, judge, build = budget_env
    client.snapshots = [make_snapshot(), make_snapshot(tid=20), make_snapshot(tid=30)]
    client.comment_count = 2
    report = await build(max_tieba_requests_per_round=4).run_once()
    assert len(client.calls) == 4
    assert report.reviewed == 0
    assert report.deferred == 3
    assert report.errors == ("本轮贴吧请求达到上限，剩余帖子顺延",)
    assert not store.is_seen("thread:100")
    assert not store.is_seen("post:101")
    assert store.get_post_mark(101) is None
    assert all(store.get_thread_mark(tid) is None for tid in (10, 20, 30))
    judge.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mark", [None, 0, 1_999_900_000, 1_999_999_995])
async def test_post_query_cutoff_uses_round_lookback_or_newer_mark(env, mark):
    store, gateway, _, _, build = env
    if mark is not None:
        store.set_thread_mark(10, mark)
    report = await build(lookback_hours=6).run_once()
    assert gateway.get_new_posts.await_args.args[1] == max(
        mark or 0, report.started_at - 6 * 3600
    )


@pytest.mark.asyncio
async def test_first_inspection_caps_establish_baseline_without_warnings(env):
    store, gateway, judge, _, build = env
    parent = make_item(kind=ItemKind.POST, pid=101, floor=2)
    comment = make_item(kind=ItemKind.COMMENT, pid=201, floor=2)
    gateway.get_new_posts.return_value = PostBatch(
        pairs=((parent, 100),), caught_up=False
    )
    gateway.get_comments.return_value = ([comment], True)
    report = await build().run_once()
    assert report.errors == ()
    assert report.reviewed == 3
    assert [call.args[0].key for call in judge.await_args_list] == [
        "thread:100",
        "post:101",
        "comment:201",
    ]
    assert store.get_thread_mark(10) == make_snapshot().last_time
    assert store.get_post_mark(101) == 100


@pytest.mark.asyncio
async def test_first_old_hot_thread_stops_at_lookback_and_only_reads_comment_tail(
    budget_env,
):
    store, client, judge, build = budget_env
    old_time = 1_999_900_000
    client.snapshots = [make_snapshot(create_time=old_time)]

    async def old_posts(tid, *, pn, rn, sort, with_comments):
        client.calls.append(("posts", tid, pn, sort))
        first = sort == gateway_module.PostSortType.ASC
        return _PatrolPage(
            [
                SimpleNamespace(
                    pid=100 if first else 101,
                    floor=1 if first else 2,
                    create_time=old_time,
                    reply_num=0 if first else 100,
                    user=client.user,
                    contents=[SimpleNamespace(text="old body")],
                )
            ],
            total_page=100,
        )

    async def old_comments(tid, pid, *, pn, sort):
        client.calls.append(("comments", tid, pid, pn))
        return _PatrolPage(
            [
                SimpleNamespace(
                    pid=200 + pn,
                    create_time=old_time,
                    user=client.user,
                    contents=[SimpleNamespace(text="old comment")],
                )
            ],
            total_page=100,
            page_size=1,
            total_count=100,
        )

    client.get_posts = old_posts
    client.get_comments = old_comments
    report = await build(lookback_hours=6).run_once()
    assert report.errors == ()
    assert report.reviewed == report.reported == report.deferred == 0
    assert report.skipped == 2
    assert [
        call[2]
        for call in client.calls
        if call[0] == "posts" and call[3] == gateway_module.PostSortType.DESC
    ] == [1]
    assert [call[3] for call in client.calls if call[0] == "comments"] == [1, 100]
    assert len(client.calls) == 5
    judge.assert_not_awaited()
    assert store.get_thread_mark(10) == make_snapshot().last_time
    assert store.get_post_mark(101) == 100
