"""Persistence, expiry, and atomic-update tests for the SQLite store."""

import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict, replace
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
    PatrolReport,
    Verdict,
)
from astrbot_plugin_tieba_moderator.core.settings import (
    AccountDraft,
    ForumDraft,
    ForumOverrides,
    SettingsSnapshot,
    default_global_settings,
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


@pytest.fixture
def query_cases_data(store, item):
    rows = [
        (
            "龙",
            ItemKind.THREAD,
            CaseStatus.PENDING,
            "广告",
            "target title",
            "quiet",
            "alpha",
            "one",
        ),
        (
            "龙",
            ItemKind.POST,
            CaseStatus.DELETED,
            "辱骂",
            "beta",
            "target body",
            "beta",
            "two",
        ),
        (
            "猫",
            ItemKind.COMMENT,
            CaseStatus.PENDING,
            "广告",
            "gamma",
            "quiet",
            "target nick",
            "three",
        ),
        (
            "龙",
            ItemKind.POST,
            CaseStatus.AUTO_DELETED,
            "广告",
            "delta",
            "quiet",
            "delta",
            "target login",
        ),
        (
            "龙脉",
            ItemKind.THREAD,
            CaseStatus.IGNORED,
            "广告引流",
            "fifth",
            "quiet",
            "epsilon",
            "five",
        ),
    ]
    cases = []
    for index, (fname, kind, status, category, title, text, nick, name) in enumerate(
        rows
    ):
        content = replace(
            item,
            pid=100 + index,
            fname=fname,
            kind=kind,
            title=title,
            text=text,
            author=replace(item.author, nick_name=nick, user_name=name),
        )
        cases.append(
            store.create_case(
                content,
                Verdict(violation=True, category=category, confidence=0.9),
                status,
                100 + index,
            )
        )
    return cases


def test_query_cases_empty_database(store):
    assert store.query_cases() == (0, [])


@pytest.mark.parametrize(
    "filters,indices",
    [
        ({}, [4, 3, 2, 1, 0]),
        ({"statuses": ()}, [4, 3, 2, 1, 0]),
        ({"fnames": ()}, [4, 3, 2, 1, 0]),
        ({"fnames": ("龙", "猫")}, [3, 2, 1, 0]),
        ({"fnames": ("missing",)}, []),
        ({"fnames": ("龙", "猫"), "statuses": (CaseStatus.PENDING,)}, [2, 0]),
        ({"fnames": ("龙", "猫"), "fname": "龙"}, [3, 1, 0]),
        ({"fnames": ("猫",), "fname": "龙"}, []),
        ({"statuses": (CaseStatus.PENDING,)}, [2, 0]),
        ({"statuses": (CaseStatus.PENDING, CaseStatus.DELETED)}, [2, 1, 0]),
        ({"fname": "龙"}, [3, 1, 0]),
        ({"category": "广告"}, [3, 2, 0]),
        ({"kind": ItemKind.POST}, [3, 1]),
        ({"kind": ItemKind.COMMENT}, [2]),
        ({"kind": ItemKind.THREAD}, [4, 0]),
        ({"q": "target"}, [3, 2, 1, 0]),
        ({"q": "title"}, [0]),
        ({"q": "body"}, [1]),
        ({"q": "nick"}, [2]),
        ({"q": "login"}, [3]),
        ({"q": "龙"}, []),
        ({"q": "广告"}, []),
        ({"fname": "missing"}, []),
        ({"category": "missing"}, []),
        ({"statuses": (CaseStatus.RESTORED,)}, []),
        ({"fname": "龙", "q": "target"}, [3, 1, 0]),
        (
            {
                "statuses": (CaseStatus.PENDING,),
                "fname": "龙",
                "fnames": ("龙", "猫"),
                "category": "广告",
                "kind": ItemKind.THREAD,
                "q": "title",
            },
            [0],
        ),
        ({"fname": "龙", "kind": ItemKind.POST, "category": "广告", "q": "login"}, [3]),
    ],
)
def test_query_cases_filters_and_combinations(
    store, query_cases_data, filters, indices
):
    total, cases = store.query_cases(**filters)
    assert total == len(indices)
    assert cases == [query_cases_data[index] for index in indices]


@pytest.mark.parametrize(
    "keyword,match,near_match",
    [
        ("%", "50% sale", "50X sale"),
        ("_", "a_b", "axb"),
        ("\\", "a\\b", "ab"),
        ("%_\\path", "%_\\path", "XXpath"),
        ("O'Reilly", "O'Reilly", "Other"),
        ("中文", "这里有中文", "这里没有"),
    ],
)
@pytest.mark.parametrize("field", ["title", "text", "nick_name", "user_name"])
def test_query_cases_treats_like_metacharacters_as_literal_substrings(
    store, item, verdict, keyword, match, near_match, field
):
    plain = replace(
        item,
        title="plain",
        text="plain",
        author=replace(item.author, nick_name="plain", user_name="plain"),
    )

    def content(value):
        value = f"before {value} after"
        if field in ("nick_name", "user_name"):
            return replace(plain, author=replace(plain.author, **{field: value}))
        return replace(plain, **{field: value})

    expected = store.create_case(content(match), verdict, CaseStatus.PENDING, 1)
    store.create_case(content(near_match), verdict, CaseStatus.PENDING, 2)
    assert store.query_cases(q=keyword) == (1, [expected])


def test_query_cases_exact_filters_accept_literal_quotes(store, item, verdict):
    case = store.create_case(
        replace(item, fname="O'Reilly_%"),
        replace(verdict, category="quoted_'%"),
        CaseStatus.PENDING,
        1,
    )
    store.create_case(item, verdict, CaseStatus.PENDING, 2)
    assert store.query_cases(fname="O'Reilly_%", category="quoted_'%") == (1, [case])


def test_query_cases_fnames_matches_ascii_and_chinese_without_changing_names(
    store: Store, item: ContentItem, verdict: Verdict
) -> None:
    names = ("PyThOn", "python", "中文Python", "中文", "other")
    cases = [
        store.create_case(replace(item, fname=name), verdict, CaseStatus.PENDING, index)
        for index, name in enumerate(names)
    ]

    assert store.query_cases(fnames=("python", "中文python", "中文")) == (
        4,
        list(reversed(cases[:4])),
    )
    assert store.query_cases(fnames=("python",), fname="PyThOn") == (1, [cases[0]])
    assert store.query_cases(fnames=("python",), fname="PYTHON") == (0, [])


@pytest.mark.parametrize("fname", ["O'Reilly_%", "x') OR 1=1 --"])
def test_query_cases_fnames_treats_quoted_names_as_literals(
    store: Store, item: ContentItem, verdict: Verdict, fname: str
) -> None:
    expected = store.create_case(
        replace(item, fname=fname), verdict, CaseStatus.PENDING, 1
    )
    store.create_case(item, verdict, CaseStatus.PENDING, 2)

    assert store.query_cases(fnames=(fname.casefold(),)) == (1, [expected])


def test_query_cases_fnames_pagination_preserves_filtered_total(
    store, query_cases_data
):
    filters = {"fnames": ("龙", "猫"), "statuses": (CaseStatus.PENDING,)}

    assert store.query_cases(**filters, offset=0, limit=1) == (
        2,
        [query_cases_data[2]],
    )
    assert store.query_cases(**filters, offset=1, limit=1) == (
        2,
        [query_cases_data[0]],
    )
    assert store.query_cases(**filters, offset=2, limit=1) == (2, [])
    assert store.query_cases(**filters, offset=999, limit=20) == (2, [])


def test_query_cases_pagination_preserves_filtered_total(store, query_cases_data):
    assert store.query_cases(fname="龙", offset=1, limit=1) == (
        3,
        [query_cases_data[1]],
    )
    assert store.query_cases(fname="龙", offset=3, limit=1) == (3, [])
    assert store.query_cases(fname="龙", offset=999, limit=20) == (3, [])


def test_query_cases_clamps_offset_and_limit(store, item, verdict):
    cases = [
        store.create_case(item, verdict, CaseStatus.PENDING, now) for now in range(105)
    ]
    assert store.query_cases(offset=-10, limit=1000) == (
        105,
        list(reversed(cases))[:100],
    )
    assert store.query_cases(limit=0) == (105, [cases[-1]])
    assert store.query_cases(limit=-10) == (105, [cases[-1]])
    assert store.query_cases(offset=100, limit=100) == (105, list(reversed(cases[:5])))


def test_case_stats_creation_time_window_and_current_status(store, item, verdict):
    assert store.case_stats(100) == []
    before = store.create_case(item, verdict, CaseStatus.PENDING, 99)
    edge = store.create_case(
        item, replace(verdict, confidence=0), CaseStatus.PENDING, 100
    )
    store.create_case(item, replace(verdict, confidence=1.0), CaseStatus.BANNED, 101)
    store.create_case(
        item, Verdict(violation=False, category="审查失败"), CaseStatus.PENDING, 102
    )
    store.transition(
        before.id,
        from_=(CaseStatus.PENDING,),
        to=CaseStatus.DELETED,
        by="moderator",
        now=200,
    )
    store.transition(
        edge.id,
        from_=(CaseStatus.PENDING,),
        to=CaseStatus.IGNORED,
        by="moderator",
        now=201,
    )
    rows = store.case_stats(100)
    assert sorted(rows) == sorted(
        [
            (verdict.category, CaseStatus.IGNORED, 0.0),
            (verdict.category, CaseStatus.BANNED, 1.0),
            ("审查失败", CaseStatus.PENDING, 0.0),
        ]
    )
    assert all(
        isinstance(status, CaseStatus) and isinstance(confidence, float)
        for _, status, confidence in rows
    )
    assert store.case_stats(103) == []


@pytest.fixture
def report() -> PatrolReport:
    return PatrolReport(
        started_at=100,
        finished_at=120,
        forums=("龙", "测试吧"),
        reviewed=12,
        reported=3,
        auto_deleted=2,
        skipped=5,
        deferred=7,
        errors=("下载失败：离线", "quoted ' error\nsecond line"),
    )


def test_reports_roundtrip_reopen_and_descending_save_order(tmp_path, report):
    path = tmp_path / "reports.db"
    store = Store(path, clock=count(100).__next__)
    newer = replace(report, started_at=130, finished_at=150, forums=(), errors=())
    same_time = replace(newer, reviewed=99)
    try:
        assert store.list_reports(20) == []
        store.save_report(report)
        store.save_report(newer)
        store.save_report(same_time)
    finally:
        store.close()
    reopened = Store(path, clock=count(200).__next__)
    try:
        loaded = reopened.list_reports(20)
        assert loaded == [same_time, newer, report]
        assert all(isinstance(value, PatrolReport) for value in loaded)
        assert isinstance(loaded[-1].forums, tuple)
        assert isinstance(loaded[-1].errors, tuple)
        with pytest.raises(FrozenInstanceError):
            loaded[-1].reviewed = 0
        assert reopened.list_reports(1) == [same_time]
        assert reopened.list_reports(0) == []
        assert reopened.list_reports(-1) == []
    finally:
        reopened.close()


def test_save_report_retains_exactly_latest_500_without_touching_cases(
    store, report, item, verdict
):
    case = store.create_case(item, verdict, CaseStatus.PENDING, 1)
    store.mark_seen(item.key)
    reports = [
        replace(report, started_at=index, finished_at=index + 1) for index in range(505)
    ]
    for value in reports[:500]:
        store.save_report(value)
    assert store.list_reports(1000) == list(reversed(reports[:500]))
    for value in reports[500:]:
        store.save_report(value)
    assert store.list_reports(1000) == list(reversed(reports[5:]))
    assert store.get_case(case.id) == case
    assert store.is_seen(item.key)


def test_opening_legacy_database_adds_reports_and_preserves_existing_case(
    tmp_path, item, verdict, report
):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            CREATE TABLE cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL, verdict TEXT NOT NULL, status TEXT NOT NULL,
                created_at INTEGER NOT NULL, handled_by TEXT NOT NULL DEFAULT '',
                handled_at INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE seen (key TEXT PRIMARY KEY, updated_at INTEGER NOT NULL);
        """)
        with connection:
            connection.execute(
                "INSERT INTO cases (id, item, verdict, status, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    7,
                    json.dumps(asdict(item)),
                    json.dumps(asdict(verdict)),
                    "pending",
                    50,
                ),
            )
            connection.execute(
                "INSERT INTO seen (key, updated_at) VALUES (?, ?)", (item.key, 60)
            )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'reports'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()
    database = Store(path, clock=count(100).__next__)
    try:
        assert database.get_case(7) == Case(
            id=7, item=item, verdict=verdict, status=CaseStatus.PENDING, created_at=50
        )
        assert database.is_seen(item.key)
        assert database.list_reports(20) == []
        database.save_report(report)
        assert database.list_reports(20) == [report]
        assert database.query_cases() == (1, [database.get_case(7)])
        assert database.create_case(item, verdict, CaseStatus.PENDING, 70).id == 8
    finally:
        database.close()
    reopened = Store(path, clock=count(200).__next__)
    try:
        assert reopened.list_reports(20) == [report]
    finally:
        reopened.close()


def test_settings_default_read_does_not_create_global_row(store):
    assert store.load_settings() == SettingsSnapshot(global_=default_global_settings())
    assert store._connection.execute("SELECT COUNT(*) FROM settings_global").fetchone()[0] == 0


def test_settings_crud_roundtrip_revision_timestamps_and_reopen(tmp_path):
    path = tmp_path / "settings.db"
    store = Store(path, clock=count(100).__next__)
    try:
        global_ = replace(default_global_settings(), sessions=("session",),
                          operators=frozenset({"operator"}))
        assert store.save_global(global_) == 1
        account = store.insert_account(AccountDraft(name="O'Reilly", bduss="fake-bduss", stoken="fake-stoken"))
        assert account.created_at == account.updated_at == 100
        forum = store.insert_forum(ForumDraft(fname=" Straße ", account_id=account.id,
            sessions=("local",), operators=frozenset({"mod"}), whitelist=frozenset({"safe"}),
            overrides=ForumOverrides(provider_id="model", rules="quoted ' rules")))
        assert forum.fname == "Straße"
        assert forum.created_at == forum.updated_at == 101
        before = store.load_settings()
        assert before == SettingsSnapshot(global_=global_, accounts=(account,), forums=(forum,), revision=3)
        changed_account = store.update_account(account.id, AccountDraft(name="renamed", bduss="new-fake"))
        assert changed_account == replace(account, name="renamed", bduss="new-fake", updated_at=102)
        changed_forum = store.update_forum(forum.id, ForumDraft(fname="new", enabled=False))
        assert changed_forum.created_at == 101 and changed_forum.updated_at == 103
        assert not changed_forum.enabled and changed_forum.account_id is None
        assert changed_forum.sessions == () and changed_forum.overrides == ForumOverrides()
        assert store.save_global(global_) == 6
        expected = store.load_settings()
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert reopened.load_settings() == expected
        assert reopened.delete_forum(forum.id)
        assert reopened.load_settings().revision == 7
        assert reopened.delete_account(account.id)
        assert reopened.load_settings() == SettingsSnapshot(global_=global_, revision=8)
    finally:
        reopened.close()


def test_settings_missing_ids_do_not_advance_revision(store):
    assert store.update_account(1, AccountDraft(name="missing")) is None
    assert store.update_forum(1, ForumDraft(fname="missing")) is None
    assert not store.delete_account(1)
    assert not store.delete_forum(1)
    assert store.load_settings().revision == 0


def test_account_empty_update_credentials_preserve_saved_values(store):
    account = store.insert_account(AccountDraft(name="a", bduss="fake-b", stoken="fake-s"))
    for bduss, stoken in ((None, None), ("", "")):
        updated = store.update_account(account.id, AccountDraft(name="a", bduss=bduss, stoken=stoken))
        assert (updated.bduss, updated.stoken) == ("fake-b", "fake-s")
    updated = store.update_account(account.id, AccountDraft(name="a", stoken="replacement"))
    assert updated.stoken == "replacement" and updated.bduss == "fake-b"


def test_forum_unique_casefold_key_rolls_back_conflicting_insert_and_update(store):
    first = store.insert_forum(ForumDraft(fname="Straße"))
    second = store.insert_forum(ForumDraft(fname="second"))
    before = store.load_settings()
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_forum(ForumDraft(fname=" STRASSE "))
    with pytest.raises(sqlite3.IntegrityError):
        store.update_forum(second.id, ForumDraft(fname="strasse"))
    assert store.load_settings() == before
    assert first.fname == "Straße"


@pytest.mark.parametrize("operation", ["global", "insert_account", "update_account", "delete_account",
                                        "insert_forum", "update_forum", "delete_forum"])
def test_revision_failure_rolls_back_entire_settings_mutation(store, operation):
    account = store.insert_account(AccountDraft(name="a", bduss="fake-b", stoken="fake-s"))
    forum = store.insert_forum(ForumDraft(fname="f"))
    before = store.load_settings()
    store._connection.execute("""
        CREATE TRIGGER reject_revision BEFORE UPDATE ON settings_global
        BEGIN SELECT RAISE(ABORT, 'revision failure'); END
    """)
    operations = {
        "global": lambda: store.save_global(replace(before.global_, interval_seconds=700)),
        "insert_account": lambda: store.insert_account(AccountDraft(name="new", bduss="fake-b", stoken="fake-s")),
        "update_account": lambda: store.update_account(account.id, AccountDraft(name="changed")),
        "delete_account": lambda: store.delete_account(account.id),
        "insert_forum": lambda: store.insert_forum(ForumDraft(fname="new")),
        "update_forum": lambda: store.update_forum(forum.id, ForumDraft(fname="changed")),
        "delete_forum": lambda: store.delete_forum(forum.id),
    }
    with pytest.raises(sqlite3.IntegrityError, match="revision failure"):
        operations[operation]()
    assert store.load_settings() == before


def test_settings_mutations_preserve_existing_moderation_data(store, item, verdict, report):
    case = store.create_case(item, verdict, CaseStatus.PENDING, 1)
    store.set_thread_mark(10, 20)
    store.set_post_mark(30, 40)
    store.mark_seen("seen")
    store.record_failure("failed")
    store.save_report(report)
    store.save_global(default_global_settings())
    account = store.insert_account(AccountDraft(name="a", bduss="fake-b", stoken="fake-s"))
    forum = store.insert_forum(ForumDraft(fname="f", account_id=account.id))
    store.delete_forum(forum.id)
    store.delete_account(account.id)
    assert store.get_case(case.id) == case and store.list_reports(1) == [report]
    assert store.get_thread_mark(10) == 20 and store.get_post_mark(30) == 40
    assert store.is_seen("seen") and store.record_failure("failed") == 2


def test_concurrent_store_settings_writes_increment_revision_without_lost_updates(tmp_path):
    path = tmp_path / "concurrent-settings.db"
    Store(path).close()
    barrier = Barrier(2, timeout=5)

    def insert(worker):
        database = Store(path)
        try:
            barrier.wait()
            for index in range(10):
                database.insert_forum(ForumDraft(fname=f"{worker}-{index}"))
        finally:
            database.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(insert, range(2)))
    database = Store(path)
    try:
        snapshot = database.load_settings()
        assert snapshot.revision == 20 and len(snapshot.forums) == 20
        assert len({forum.id for forum in snapshot.forums}) == 20
    finally:
        database.close()
