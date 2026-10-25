"""Persistence, expiry, and atomic-update tests for the SQLite store."""

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from itertools import count
from pathlib import Path
from threading import Barrier

import pytest
from astrbot_plugin_tieba_moderator.core.models import (
    Author,
    Case,
    CaseStatus,
    ContentItem,
    ItemKind,
    Verdict,
)
from astrbot_plugin_tieba_moderator.core.store import Store


@pytest.fixture
def item() -> ContentItem:
    return ContentItem(
        kind=ItemKind.COMMENT,
        fname="测试吧",
        fid=12,
        tid=345,
        pid=678,
        author=Author(
            user_id=90,
            portrait="test-portrait",
            user_name="test-user",
            nick_name="测试昵称",
            level=8,
            is_bawu=True,
        ),
        title="标题 ' quoted",
        text="正文\n链接(https://example.invalid/path)",
        image_urls=("https://example.invalid/a.png", "https://example.invalid/b.jpg"),
        create_time=10,
        floor=9,
        has_video=True,
    )


@pytest.fixture
def verdict() -> Verdict:
    return Verdict(
        violation=True,
        category="广告引流",
        confidence=0.95,
        reason="重复发布广告 ' quoted",
        ban_days=3,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    database = Store(tmp_path / "moderator.db", clock=count(100).__next__)
    try:
        yield database
    finally:
        database.close()


def test_new_database_uses_wal_and_has_no_records(tmp_path: Path) -> None:
    path = tmp_path / "moderator.db"
    database = Store(path)
    try:
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        connection.close()
        assert database.get_thread_mark(1) is None
        assert database.get_post_mark(1) is None
        assert not database.is_seen("thread:1")
        assert database.get_case(1) is None
        assert database.list_cases(CaseStatus.PENDING, 10) == []
        assert database.count_cases(CaseStatus.PENDING) == 0
    finally:
        database.close()


def test_marks_upsert_independently_and_failures_increment(store: Store) -> None:
    store.set_thread_mark(1, 0)
    store.set_post_mark(1, 0)
    assert store.get_thread_mark(1) == 0
    assert store.get_post_mark(1) == 0
    store.set_thread_mark(1, 1234)
    store.set_post_mark(1, 6)
    assert store.get_thread_mark(1) == 1234
    assert store.get_post_mark(1) == 6
    store.mark_seen("post:1")
    store.mark_seen("post:1")
    assert store.is_seen("post:1")
    assert not store.is_seen("thread:1")
    assert store.record_failure("post:1") == 1
    assert store.record_failure("post:1") == 2
    assert store.record_failure("thread:1") == 1
    assert store.record_failure("post:1") == 3


@pytest.mark.parametrize("kind", list(ItemKind))
def test_case_json_roundtrip_restores_frozen_models_and_enums(
    store: Store, item: ContentItem, verdict: Verdict, kind: ItemKind
) -> None:
    item = replace(item, kind=kind)
    case = store.create_case(item, verdict, CaseStatus.PENDING, 55)
    loaded = store.get_case(case.id)
    assert loaded == case
    assert loaded is not None
    assert loaded.item == item
    assert loaded.verdict == verdict
    assert isinstance(loaded, Case)
    assert isinstance(loaded.item, ContentItem)
    assert isinstance(loaded.item.author, Author)
    assert isinstance(loaded.verdict, Verdict)
    assert loaded.item.kind is kind
    assert loaded.status is CaseStatus.PENDING
    assert isinstance(loaded.item.image_urls, tuple)
    assert loaded.created_at == 55
    assert loaded.handled_at == 0
    assert loaded.handled_by == ""
    assert loaded.note == ""
    with pytest.raises(FrozenInstanceError):
        loaded.item.text = "changed"
    with pytest.raises(FrozenInstanceError):
        loaded.verdict.reason = "changed"


def test_reopen_preserves_all_records_and_case_ids(
    tmp_path: Path, item: ContentItem, verdict: Verdict
) -> None:
    path = tmp_path / "moderator.db"
    first = Store(path, clock=count(100).__next__)
    try:
        first.set_thread_mark(12, 34)
        first.set_post_mark(56, 7)
        first.mark_seen(item.key)
        assert first.record_failure(item.key) == 1
        case = first.create_case(item, verdict, CaseStatus.PENDING, 123)
        handled = first.transition(
            case.id,
            from_=(CaseStatus.PENDING,),
            to=CaseStatus.IGNORED,
            by="operator ' one",
            now=124,
            note="误判说明",
        )
    finally:
        first.close()
    reopened = Store(path, clock=count(200).__next__)
    try:
        assert reopened.get_thread_mark(12) == 34
        assert reopened.get_post_mark(56) == 7
        assert reopened.is_seen(item.key)
        assert reopened.record_failure(item.key) == 2
        assert reopened.get_case(case.id) == handled
        newer = reopened.create_case(item, verdict, CaseStatus.AUTO_DELETED, 125)
        assert newer.id > case.id
        assert reopened.count_cases(CaseStatus.IGNORED) == 1
    finally:
        reopened.close()


def test_purge_uses_last_write_time_and_preserves_cases(
    store: Store, item: ContentItem, verdict: Verdict
) -> None:
    # Every write advances the clock; marker payloads are not expiry timestamps.
    store.set_thread_mark(1, 999999)  # 100
    store.set_post_mark(1, 999999)  # 101
    store.mark_seen("post:old")  # 102
    assert store.record_failure("post:old") == 1  # 103
    store.set_thread_mark(2, 999999)  # 104
    store.set_post_mark(2, 999999)  # 105
    store.mark_seen("post:refreshed")  # 106
    assert store.record_failure("post:refreshed") == 1  # 107
    store.set_thread_mark(2, 0)  # 108: exactly the cutoff survives.
    store.set_post_mark(2, 0)  # 109
    store.mark_seen("post:refreshed")  # 110
    assert store.record_failure("post:refreshed") == 2  # 111
    case = store.create_case(item, verdict, CaseStatus.PENDING, 1)

    store.purge(108)

    assert store.get_thread_mark(1) is None
    assert store.get_post_mark(1) is None
    assert not store.is_seen("post:old")
    assert store.get_thread_mark(2) == 0
    assert store.get_post_mark(2) == 0
    assert store.is_seen("post:refreshed")
    assert store.record_failure("post:old") == 1
    assert store.record_failure("post:refreshed") == 3
    assert store.get_case(case.id) == case
    store.purge(1000)
    assert store.get_thread_mark(2) is None
    assert store.get_post_mark(2) is None
    assert not store.is_seen("post:refreshed")
    assert store.get_case(case.id) == case
    assert store.count_cases(CaseStatus.PENDING) == 1


def test_case_listing_filters_counts_and_limits(
    store: Store, item: ContentItem, verdict: Verdict
) -> None:
    first = store.create_case(item, verdict, CaseStatus.PENDING, 100)
    deleted = store.create_case(item, verdict, CaseStatus.AUTO_DELETED, 101)
    last = store.create_case(item, verdict, CaseStatus.PENDING, 102)
    assert store.count_cases(CaseStatus.PENDING) == 2
    assert store.count_cases(CaseStatus.AUTO_DELETED) == 1
    assert store.count_cases(CaseStatus.BANNED) == 0
    assert store.list_cases(CaseStatus.PENDING, 10) == [last, first]
    assert store.list_cases(CaseStatus.PENDING, 1) == [last]
    assert store.list_cases(CaseStatus.PENDING, 0) == []
    assert store.list_cases(CaseStatus.PENDING, -1) == []
    assert store.list_cases(CaseStatus.AUTO_DELETED, 10) == [deleted]
    assert store.list_cases(CaseStatus.BANNED, 10) == []


def test_transition_updates_only_matching_status_and_records_metadata(
    store: Store, item: ContentItem, verdict: Verdict
) -> None:
    case = store.create_case(item, verdict, CaseStatus.PENDING, 100)
    assert (
        store.transition(
            case.id,
            from_=(CaseStatus.DELETED,),
            to=CaseStatus.RESTORED,
            by="wrong",
            now=101,
        )
        is None
    )
    assert (
        store.transition(case.id, from_=(), to=CaseStatus.DELETED, by="empty", now=102)
        is None
    )
    assert (
        store.transition(
            case.id + 99,
            from_=(CaseStatus.PENDING,),
            to=CaseStatus.DELETED,
            by="missing",
            now=103,
        )
        is None
    )
    assert store.get_case(case.id) == case
    result = store.transition(
        case.id,
        from_=(CaseStatus.PENDING, CaseStatus.AUTO_DELETED),
        to=CaseStatus.DELETED,
        by="moderator",
        now=104,
        note="删除成功",
    )
    assert result == replace(
        case,
        status=CaseStatus.DELETED,
        handled_by="moderator",
        handled_at=104,
        note="删除成功",
    )
    restored = store.transition(
        case.id,
        from_=(CaseStatus.DELETED,),
        to=CaseStatus.RESTORED,
        by="other",
        now=105,
    )
    assert restored is not None
    assert restored.note == ""
    assert restored.created_at == case.created_at
    assert restored.handled_at == 105
    assert restored.handled_by == "other"
    assert store.count_cases(CaseStatus.PENDING) == 0
    assert store.count_cases(CaseStatus.RESTORED) == 1


def test_two_connections_compete_for_one_transition(
    tmp_path: Path, item: ContentItem, verdict: Verdict
) -> None:
    path = tmp_path / "moderator.db"
    database = Store(path)
    try:
        case = database.create_case(item, verdict, CaseStatus.PENDING, 100)
        barrier = Barrier(2, timeout=5)

        def compete(status: CaseStatus) -> Case | None:
            contender = Store(path)
            try:
                assert contender.get_case(case.id).status is CaseStatus.PENDING
                barrier.wait()
                return contender.transition(
                    case.id,
                    from_=(CaseStatus.PENDING,),
                    to=status,
                    by=status.value,
                    now=101,
                    note=status.value,
                )
            finally:
                contender.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(compete, (CaseStatus.DELETED, CaseStatus.IGNORED))
            )
        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        assert results.count(None) == 1
        assert database.get_case(case.id) == winners[0]
        assert winners[0].handled_by == winners[0].status.value
        assert winners[0].note == winners[0].status.value
    finally:
        database.close()


def test_two_connections_increment_failure_counter_without_lost_updates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "moderator.db"
    Store(path).close()
    barrier = Barrier(2, timeout=5)

    def increment(_: int) -> list[int]:
        contender = Store(path, clock=count(100).__next__)
        try:
            barrier.wait()
            return [contender.record_failure("post:1") for _ in range(10)]
        finally:
            contender.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(increment, range(2)))
    assert sorted(results[0] + results[1]) == list(range(1, 21))
