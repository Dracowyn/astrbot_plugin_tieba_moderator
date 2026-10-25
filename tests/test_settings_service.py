"""Serialized settings mutations, integrity, callbacks and chat binding."""

import asyncio
import sqlite3
from dataclasses import replace

import pytest
from astrbot_plugin_tieba_moderator.core.forums import BindResult
from astrbot_plugin_tieba_moderator.core.settings import (
    AccountDraft,
    ForumDraft,
    ForumEntry,
    ForumOverrides,
    default_global_settings,
)
from astrbot_plugin_tieba_moderator.core.store import Store
from astrbot_plugin_tieba_moderator.services.settings_service import (
    SettingsConflict,
    SettingsError,
    SettingsNotFound,
    SettingsService,
)


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "settings.db", clock=lambda: 100)
    yield value
    value.close()


@pytest.fixture
def service(store):
    return SettingsService(store)


def draft(name="moderator"):
    return AccountDraft(name=name, bduss="fake-bduss", stoken="fake-stoken")


@pytest.mark.asyncio
async def test_crud_publishes_frozen_snapshots_after_persisting(store):
    changes = []

    async def changed(old, new):
        assert service.snapshot() is new
        assert store.load_settings() == new
        changes.append((old, new))

    service = SettingsService(store, on_change=changed)
    initial = service.snapshot()
    assert initial.global_ == default_global_settings() and initial.revision == 0
    assert not service.resolve("unknown").configured
    account = await service.create_account(draft())
    forum = await service.create_forum(ForumDraft(fname=" Python ", account_id=account.id))
    assert forum.fname == "Python"
    changed_forum = await service.update_forum(forum.id, ForumDraft(
        fname="PYTHON", enabled=False, sessions=("local",), overrides=ForumOverrides(rules="rules")
    ))
    assert changed_forum.created_at == forum.created_at and not changed_forum.enabled
    assert service.resolve(" python ").sessions == ("local",)
    account2 = await service.update_account(account.id, AccountDraft(name="renamed", bduss="", stoken=None))
    assert (account2.bduss, account2.stoken) == (account.bduss, account.stoken)
    account3 = await service.update_account(account.id, draft("renamed"))
    assert account3.id == account.id
    saved = await service.save_global(replace(initial.global_, interval_seconds=600))
    assert saved.global_.interval_seconds == 600
    await service.delete_forum(forum.id)
    await service.delete_account(account.id)
    assert service.snapshot().accounts == () and service.snapshot().forums == ()
    assert len(changes) == 8
    assert [new.revision for _, new in changes] == list(range(1, 9))
    assert all(old is initial if index == 0 else old is changes[index - 1][1]
               for index, (old, _) in enumerate(changes))
    assert initial.revision == 0 and initial.accounts == ()
    assert SettingsService(store).snapshot() == service.snapshot()
    assert SettingsService(store, snapshot=saved).snapshot() is saved


@pytest.mark.asyncio
async def test_uniqueness_integrity_and_not_found_never_write(service, store):
    account = await service.create_account(draft("Straße"))
    other = await service.create_account(draft("other"))
    forum = await service.create_forum(ForumDraft(fname="Straße", account_id=account.id))
    second = await service.create_forum(ForumDraft(fname="other"))
    before = service.snapshot()
    conflicts = [
        service.create_account(draft("STRASSE")),
        service.update_account(other.id, AccountDraft(name="STRASSE")),
        service.create_forum(ForumDraft(fname=" STRASSE ")),
        service.update_forum(second.id, ForumDraft(fname="STRASSE")),
        service.delete_account(account.id),
    ]
    for operation in conflicts:
        with pytest.raises(SettingsConflict):
            await operation
    missing = [
        service.create_forum(ForumDraft(fname="new", account_id=999)),
        service.update_forum(second.id, ForumDraft(fname="other", account_id=999)),
        service.update_forum(999, ForumDraft(fname="missing")),
        service.delete_forum(999),
        service.update_account(999, AccountDraft(name="missing")),
        service.delete_account(999),
    ]
    for operation in missing:
        with pytest.raises(SettingsNotFound):
            await operation
    assert service.snapshot() is before and store.load_settings() == before
    await service.update_forum(forum.id, ForumDraft(fname="STRASSE"))
    await service.update_account(account.id, AccountDraft(name="STRASSE"))
    await service.delete_account(account.id)


@pytest.mark.asyncio
async def test_referenced_account_message_counts_disabled_forums(service):
    account = await service.create_account(draft())
    await service.create_forum(ForumDraft(fname="a", account_id=account.id))
    await service.create_forum(ForumDraft(fname="b", account_id=account.id, enabled=False))
    with pytest.raises(SettingsConflict, match="仍有 2 个吧在使用该账号"):
        await service.delete_account(account.id)


@pytest.mark.asyncio
async def test_account_and_forum_limits_allow_replacements(service):
    accounts = [await service.create_account(draft(f"a{i}")) for i in range(20)]
    with pytest.raises(SettingsConflict, match="20"):
        await service.create_account(draft("too-many"))
    await service.update_account(accounts[0].id, AccountDraft(name="renamed"))
    forums = [await service.create_forum(ForumDraft(fname=f"f{i}")) for i in range(50)]
    with pytest.raises(SettingsConflict, match="50"):
        await service.create_forum(ForumDraft(fname="too-many"))
    await service.update_forum(forums[0].id, ForumDraft(fname="renamed"))
    await service.delete_forum(forums[-1].id)
    await service.create_forum(ForumDraft(fname="replacement"))
    await service.delete_account(accounts[-1].id)
    await service.create_account(draft("replacement"))


@pytest.mark.asyncio
async def test_credentials_missing_on_create_never_reveal_secrets(service):
    with pytest.raises(SettingsError) as error:
        await service.create_account(AccountDraft(name="a", bduss="private-value"))
    assert "private-value" not in str(error.value)
    assert service.snapshot().revision == 0


@pytest.mark.asyncio
async def test_persistence_failure_rolls_back_snapshot_and_skips_callback(store):
    calls = []

    async def changed(old, new):
        calls.append(new.revision)

    service = SettingsService(store, on_change=changed)
    before = service.snapshot()
    store._connection.execute("""
        CREATE TRIGGER reject_revision BEFORE INSERT ON settings_global
        BEGIN SELECT RAISE(ABORT, 'test failure'); END
    """)
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        await service.create_account(draft())
    assert service.snapshot() is before and store.load_settings() == before
    assert calls == []
    store._connection.execute("DROP TRIGGER reject_revision")
    await service.create_account(draft())
    assert calls == [1]


@pytest.mark.asyncio
async def test_callback_failure_leaves_committed_snapshot_and_releases_lock(store):
    async def broken(old, new):
        if new.revision == 1:
            raise RuntimeError("callback failed")

    service = SettingsService(store, on_change=broken)
    with pytest.raises(RuntimeError, match="callback failed"):
        await service.create_account(draft())
    assert service.snapshot() == store.load_settings()
    assert service.snapshot().revision == 1
    await service.save_global(service.snapshot().global_)
    assert service.snapshot().revision == 2


@pytest.mark.asyncio
async def test_writes_wait_for_callbacks_and_preserve_revision_order(store):
    entered, release, attempted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    changes = []

    async def changed(old, new):
        changes.append((old.revision, new.revision))
        if new.revision == 1:
            entered.set()
            await release.wait()

    service = SettingsService(store, on_change=changed)
    first = asyncio.create_task(service.create_account(draft("first")))
    await entered.wait()

    async def second_write():
        attempted.set()
        return await service.create_account(draft("second"))

    second = asyncio.create_task(second_write())
    await attempted.wait()
    assert not second.done()
    assert service.snapshot().revision == store.load_settings().revision == 1
    release.set()
    await asyncio.gather(first, second)
    assert changes == [(0, 1), (1, 2)]
    assert [account.name for account in service.snapshot().accounts] == ["first", "second"]


@pytest.mark.asyncio
async def test_concurrent_duplicates_have_one_winner(service):
    results = await asyncio.gather(*(service.create_account(draft("same")) for _ in range(8)),
                                   return_exceptions=True)
    assert sum(isinstance(result, SettingsConflict) for result in results) == 7
    assert service.snapshot().revision == 1
    results = await asyncio.gather(*(service.create_forum(ForumDraft(fname="same")) for _ in range(8)),
                                   return_exceptions=True)
    assert sum(isinstance(result, SettingsConflict) for result in results) == 7
    assert service.snapshot().revision == 2


@pytest.mark.asyncio
async def test_bind_global_and_forum_are_persisted_without_copying_inherited_sessions(service, store):
    account = await service.create_account(draft())
    forum = await service.create_forum(ForumDraft(fname="Python", account_id=account.id,
        operators=frozenset({"mod"}), overrides=ForumOverrides(rules="own")))
    assert await service.bind_session(None, "global") == BindResult(label="", total=1)
    assert await service.bind_session("", "global") == "当前会话已经在推送列表里。"
    assert service.resolve("python").sessions == ("global",)
    assert await service.bind_session(" PYTHON ", "local") == BindResult(label="Python 吧的", total=1)
    assert await service.bind_session("python", "local") == "当前会话已经在Python 吧的推送列表里。"
    assert await service.bind_session("Python", "other") == BindResult(label="Python 吧的", total=2)
    loaded = SettingsService(store)
    assert loaded.resolve("python").sessions == ("local", "other")
    assert loaded.resolve("python").options.rules == "own"
    assert loaded.resolve("python").operators == frozenset({"mod"})
    assert loaded.snapshot().forums[0].id == forum.id
    assert loaded.snapshot().revision == 5


@pytest.mark.asyncio
async def test_bind_refuses_unknown_disabled_unassigned_and_oversized_names(service):
    account = await service.create_account(draft())
    await service.create_forum(ForumDraft(fname="disabled", account_id=account.id, enabled=False))
    await service.create_forum(ForumDraft(fname="unassigned"))
    before = service.snapshot()
    for name in ("missing", "disabled", "unassigned", "bad\nname"):
        response = await service.bind_session(name, "session")
        assert "未巡查" in response and "\n" not in response
    assert await service.bind_session("x" * 65, "session") == "吧名过长，不能绑定推送会话。"
    assert service.snapshot() is before


@pytest.mark.asyncio
async def test_bind_limits_validate_sessions_and_concurrent_append(service):
    for session in ("", "x" * 513, "bad\nsession", "bad\x7fsession"):
        assert isinstance(await service.bind_session(None, session), str)
    assert service.snapshot().revision == 0
    results = await asyncio.gather(*(service.bind_session(None, f"s{i}") for i in range(20)))
    assert all(isinstance(result, BindResult) for result in results)
    assert service.snapshot().global_.sessions == tuple(f"s{i}" for i in range(20))
    assert "20" in await service.bind_session(None, "overflow")
    assert "已经" in await service.bind_session(None, "s0")
    account = await service.create_account(draft())
    await service.create_forum(ForumDraft(fname="full", account_id=account.id,
                                        sessions=tuple(f"s{i}" for i in range(20))))
    assert "20" in await service.bind_session("full", "overflow")


@pytest.mark.asyncio
async def test_global_mutations_preserve_supplied_snapshot_and_do_not_reload(store, monkeypatch):
    initial = SettingsService(store).snapshot()
    synthetic = ForumEntry(id=77, fname="synthetic", created_at=0, updated_at=0)
    supplied = replace(initial, forums=(synthetic,))
    service = SettingsService(store, snapshot=supplied)

    def unexpected_reload():
        raise AssertionError("mutations must not reload unrelated settings")

    monkeypatch.setattr(store, "load_settings", unexpected_reload)
    saved = await service.save_global(replace(supplied.global_, interval_seconds=700))
    assert saved.forums == (synthetic,) and saved.revision == 1
    await service.bind_session(None, "session")
    assert service.snapshot().forums == (synthetic,)
    assert service.snapshot().global_.sessions == ("session",)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update_account", "delete_account", "update_forum",
                                       "delete_forum", "bind"])
async def test_missing_store_rows_never_publish_false_success(service, store, operation):
    account = await service.create_account(draft())
    forum = await service.create_forum(ForumDraft(fname="forum", account_id=account.id))
    if operation.endswith("account"):
        await service.update_forum(forum.id, ForumDraft(fname="forum"))
        store.delete_account(account.id)
    else:
        store.delete_forum(forum.id)
    before = service.snapshot()
    operations = {
        "update_account": lambda: service.update_account(account.id, AccountDraft(name="new")),
        "delete_account": lambda: service.delete_account(account.id),
        "update_forum": lambda: service.update_forum(forum.id, ForumDraft(fname="new")),
        "delete_forum": lambda: service.delete_forum(forum.id),
        "bind": lambda: service.bind_session("forum", "session"),
    }
    with pytest.raises(SettingsNotFound):
        await operations[operation]()
    assert service.snapshot() is before


@pytest.mark.asyncio
async def test_cancelled_callback_keeps_commit_and_releases_write_lock(store):
    entered = asyncio.Event()

    async def changed(old, new):
        if new.revision == 1:
            entered.set()
            await asyncio.Event().wait()

    service = SettingsService(store, on_change=changed)
    task = asyncio.create_task(service.create_account(draft()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert service.snapshot() == store.load_settings()
    await service.create_forum(ForumDraft(fname="after-cancel"))
    assert service.snapshot().revision == 2


@pytest.mark.asyncio
async def test_concurrent_duplicate_bind_only_commits_once(service):
    results = await asyncio.gather(*(service.bind_session(None, "session") for _ in range(8)))
    assert sum(isinstance(result, BindResult) for result in results) == 1
    assert service.snapshot().revision == 1
    assert service.snapshot().global_.sessions == ("session",)
