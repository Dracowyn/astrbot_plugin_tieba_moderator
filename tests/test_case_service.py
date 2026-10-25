"""Moderator actions preserve API ordering and conditional case updates."""

import asyncio
from dataclasses import replace
from itertools import count
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_tieba_moderator.core.models import (
    ALLOWED_BAN_DAYS,
    Author,
    CaseStatus,
    ContentItem,
    ItemKind,
    TiebaActionError,
    Verdict,
)
from astrbot_plugin_tieba_moderator.core.settings import (
    Account,
    ForumEntry,
    ForumOptions,
    GlobalSettings,
    SettingsSnapshot,
    resolve,
)
from astrbot_plugin_tieba_moderator.core.store import Store
from astrbot_plugin_tieba_moderator.services.case_service import CaseError, CaseService
from astrbot_plugin_tieba_moderator.services.gateway_pool import GatewayPool
from astrbot_plugin_tieba_moderator.services.settings_service import SettingsService

TRANSITIONS = [
    ("delete", CaseStatus.PENDING, CaseStatus.DELETED, ["delete"]),
    ("ignore", CaseStatus.PENDING, CaseStatus.IGNORED, []),
    ("ban", CaseStatus.PENDING, CaseStatus.BANNED, ["delete", "block"]),
    ("ban", CaseStatus.AUTO_DELETED, CaseStatus.BANNED, ["block"]),
    ("ban", CaseStatus.DELETED, CaseStatus.BANNED, ["block"]),
    ("restore", CaseStatus.AUTO_DELETED, CaseStatus.RESTORED, ["restore"]),
    ("restore", CaseStatus.DELETED, CaseStatus.RESTORED, ["restore"]),
    ("restore", CaseStatus.BANNED, CaseStatus.RESTORED, ["restore"]),
]
ALLOWED = {(operation, status) for operation, status, _, _ in TRANSITIONS}


@pytest.fixture
def setup_case(tmp_path):
    store = Store(tmp_path / "cases.db")
    item = ContentItem(
        kind=ItemKind.POST,
        fname="测试吧",
        fid=1,
        tid=2,
        pid=3,
        author=Author(user_id=4, portrait="author"),
    )
    verdict = Verdict(
        violation=True, category="广告", confidence=0.9, reason="重复推广", ban_days=1
    )
    gateway = SimpleNamespace(
        delete=AsyncMock(), block=AsyncMock(), restore=AsyncMock(), close=AsyncMock()
    )
    clock = Mock(side_effect=(value + 0.75 for value in count(1000)))
    snapshot = SettingsSnapshot(
        global_=GlobalSettings(defaults=ForumOptions(rules="rules")),
        accounts=(
            Account(
                id=1, name="moderator", bduss="fake-bduss", stoken="fake-stoken",
                created_at=1, updated_at=1,
            ),
        ),
        forums=(
            ForumEntry(
                id=1, fname=item.fname, account_id=1, created_at=1, updated_at=1
            ),
        ),
    )
    settings = SettingsService(store, snapshot=snapshot)
    factory = Mock(return_value=gateway)
    pool = GatewayPool(snapshot, factory=factory)
    service = CaseService(pool=pool, settings=settings, store=store, clock=clock)

    def create(status=CaseStatus.PENDING):
        return store.create_case(item, verdict, status, 100)

    yield SimpleNamespace(
        store=store,
        gateway=gateway,
        pool=pool,
        settings=settings,
        snapshot=snapshot,
        factory=factory,
        clock=clock,
        service=service,
        create=create,
    )
    asyncio.run(pool.close())
    store.close()


def invoke(service, operation, case_id, days=3):
    if operation == "ban":
        return asyncio.run(service.ban(case_id, days, "moderator"))
    return asyncio.run(getattr(service, operation)(case_id, "moderator"))


def record_order(setup, monkeypatch):
    """Record API calls and the real SQLite transition in one timeline."""
    events = []
    transition = setup.store.transition

    def update(*args, **kwargs):
        events.append("transition")
        return transition(*args, **kwargs)

    monkeypatch.setattr(setup.store, "transition", Mock(side_effect=update))
    for name in ("delete", "block", "restore"):
        getattr(setup.gateway, name).side_effect = lambda *args, action=name: (
            events.append(action)
        )
    return events


@pytest.mark.parametrize("operation,status,target,api_calls", TRANSITIONS)
def test_allowed_transitions_api_before_cas(
    setup_case, monkeypatch, operation, status, target, api_calls
):
    setup = setup_case
    case = setup.create(status)
    events = record_order(setup, monkeypatch)

    result = invoke(setup.service, operation, case.id)

    assert events == [*api_calls, "transition"]
    assert result == setup.store.get_case(case.id)
    assert result == replace(
        case, status=target, handled_by="moderator", handled_at=1000
    )
    assert setup.store.transition.call_args.kwargs["from_"] == (status,)
    setup.clock.assert_called_once_with()
    for name in api_calls:
        mock = getattr(setup.gateway, name)
        if name == "block":
            mock.assert_awaited_once_with(case.item, 3, "广告：重复推广")
        else:
            mock.assert_awaited_once_with(case.item)


@pytest.mark.parametrize(
    "operation,status",
    [
        (operation, status)
        for operation in ("delete", "ban", "ignore", "restore")
        for status in CaseStatus
        if (operation, status) not in ALLOWED
    ],
)
def test_illegal_transitions_are_chinese_errors_without_side_effects(
    setup_case, monkeypatch, operation, status
):
    setup = setup_case
    case = setup.create(status)
    events = record_order(setup, monkeypatch)

    with pytest.raises(CaseError, match=r"[\u4e00-\u9fff]"):
        invoke(setup.service, operation, case.id)

    assert setup.store.get_case(case.id) == case
    assert events == []
    setup.clock.assert_not_called()


@pytest.mark.parametrize("operation", ["delete", "ban", "ignore", "restore"])
def test_missing_case_is_chinese_error(setup_case, monkeypatch, operation):
    setup = setup_case
    events = record_order(setup, monkeypatch)
    with pytest.raises(CaseError, match="不存在"):
        invoke(setup.service, operation, 999)
    assert events == []
    setup.clock.assert_not_called()


@pytest.mark.parametrize("days", [-1, 0, 2, 7, 30, None, "3", True, 1.0])
def test_invalid_ban_days_never_delete(setup_case, monkeypatch, days):
    setup = setup_case
    case = setup.create()
    events = record_order(setup, monkeypatch)
    with pytest.raises(CaseError, match="天数"):
        invoke(setup.service, "ban", case.id, days)
    assert events == []
    assert setup.store.get_case(case.id) == case


@pytest.mark.parametrize("days", ALLOWED_BAN_DAYS)
def test_allowed_ban_days(setup_case, days):
    setup = setup_case
    case = setup.create()
    result = invoke(setup.service, "ban", case.id, days)
    assert result.status is CaseStatus.BANNED
    setup.gateway.block.assert_awaited_once_with(case.item, days, "广告：重复推广")


@pytest.mark.parametrize("operation,status,target,api_calls", TRANSITIONS)
def test_cas_race_raises_without_overwriting_winner(
    setup_case, monkeypatch, operation, status, target, api_calls
):
    setup = setup_case
    case = setup.create(status)
    events = record_order(setup, monkeypatch)
    winner = setup.store.transition(
        case.id,
        from_=(status,),
        to=CaseStatus.IGNORED,
        by="other",
        now=200,
    )
    events.clear()
    monkeypatch.setattr(setup.store, "get_case", Mock(return_value=case))

    with pytest.raises(CaseError, match="处理"):
        invoke(setup.service, operation, case.id)

    assert events == [*api_calls, "transition"]
    assert setup.store.list_cases(CaseStatus.IGNORED, 10) == [winner]


@pytest.mark.parametrize(
    "operation,status,failure",
    [
        ("delete", CaseStatus.PENDING, "delete"),
        ("ban", CaseStatus.PENDING, "delete"),
        ("ban", CaseStatus.AUTO_DELETED, "block"),
        ("ban", CaseStatus.DELETED, "block"),
        ("restore", CaseStatus.AUTO_DELETED, "restore"),
        ("restore", CaseStatus.DELETED, "restore"),
        ("restore", CaseStatus.BANNED, "restore"),
    ],
)
def test_api_failure_preserves_case(
    setup_case, monkeypatch, operation, status, failure
):
    setup = setup_case
    case = setup.create(status)
    error = TiebaActionError("贴吧错误码 4: rejected")
    getattr(setup.gateway, failure).side_effect = error
    transition = Mock(wraps=setup.store.transition)
    monkeypatch.setattr(setup.store, "transition", transition)

    with pytest.raises(TiebaActionError) as caught:
        invoke(setup.service, operation, case.id)

    assert caught.value is error
    assert setup.store.get_case(case.id) == case
    transition.assert_not_called()
    if failure == "delete":
        setup.gateway.block.assert_not_awaited()
    if operation == "ban" and status is not CaseStatus.PENDING:
        setup.gateway.delete.assert_not_awaited()
    setup.clock.assert_not_called()


def test_partial_ban_records_deletion_and_failure_note(setup_case, monkeypatch):
    setup = setup_case
    case = setup.create()
    events = record_order(setup, monkeypatch)
    error = TiebaActionError("贴吧错误码 12: permission denied")

    async def fail_block(*args):
        events.append("block")
        raise error

    setup.gateway.block.side_effect = fail_block
    with pytest.raises(TiebaActionError) as caught:
        invoke(setup.service, "ban", case.id)

    assert caught.value is error
    result = setup.store.get_case(case.id)
    assert result.status is CaseStatus.DELETED
    assert str(error) in result.note
    assert result.handled_by == "moderator"
    assert result.handled_at == 1000
    assert events == ["delete", "block", "transition"]


def test_partial_ban_cas_race_does_not_overwrite_other_operator(
    setup_case, monkeypatch
):
    setup = setup_case
    case = setup.create()
    error = TiebaActionError("贴吧错误码 12: permission denied")
    setup.gateway.block.side_effect = error
    transition = Mock(return_value=None)
    monkeypatch.setattr(setup.store, "transition", transition)

    with pytest.raises(CaseError, match="处理") as caught:
        invoke(setup.service, "ban", case.id)

    assert caught.value.__context__ is error
    assert setup.store.get_case(case.id) == case
    assert transition.call_args.kwargs["to"] is CaseStatus.DELETED
    assert str(error) in transition.call_args.kwargs["note"]


@pytest.mark.parametrize("reason", ["短理由", "长" * 100])
def test_ban_reason_uses_category_and_fifty_character_limit(
    setup_case, monkeypatch, reason
):
    setup = setup_case
    case = setup.create(CaseStatus.DELETED)
    verdict = replace(case.verdict, reason=reason)
    monkeypatch.setattr(
        setup.store, "get_case", Mock(return_value=replace(case, verdict=verdict))
    )

    invoke(setup.service, "ban", case.id)

    setup.gateway.block.assert_awaited_once_with(
        case.item, 3, f"{verdict.category}：{reason}"[:50]
    )


def test_injected_clock_advances_between_operations(setup_case):
    setup = setup_case
    case = setup.create()
    deleted = invoke(setup.service, "delete", case.id)
    restored = invoke(setup.service, "restore", case.id)
    assert deleted.handled_at == 1000
    assert restored.handled_at == 1001


@pytest.mark.parametrize(
    "operation,status,remote,target",
    [
        ("delete", CaseStatus.PENDING, "delete", CaseStatus.DELETED),
        ("ban", CaseStatus.PENDING, "delete", CaseStatus.BANNED),
        ("restore", CaseStatus.DELETED, "restore", CaseStatus.RESTORED),
    ],
)
def test_concurrent_actions_call_gateway_once(
    setup_case, operation, status, remote, target
):
    setup = setup_case
    case = setup.create(status)

    async def yield_action(*args):
        await asyncio.sleep(0)

    getattr(setup.gateway, remote).side_effect = yield_action

    async def run():
        method = getattr(setup.service, operation)
        args = (case.id, 3) if operation == "ban" else (case.id,)
        return await asyncio.gather(
            method(*args, "first"),
            method(*args, "second"),
            return_exceptions=True,
        )

    winner, loser = asyncio.run(run())

    assert winner.status is target
    assert winner.handled_by == "first"
    assert isinstance(loser, CaseError)
    assert setup.store.get_case(case.id) == winner
    getattr(setup.gateway, remote).assert_awaited_once_with(case.item)
    if operation == "ban":
        setup.gateway.block.assert_awaited_once_with(case.item, 3, "广告：重复推广")


@pytest.mark.parametrize("operation", ["delete", "ban", "ignore", "restore"])
def test_all_actions_wait_before_reading_or_validating(
    setup_case, monkeypatch, operation
):
    setup = setup_case
    case = setup.create()
    get_case = Mock(wraps=setup.store.get_case)
    monkeypatch.setattr(setup.store, "get_case", get_case)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_delete(item):
            entered.set()
            await release.wait()

        setup.gateway.delete.side_effect = hold_delete
        first = asyncio.create_task(setup.service.delete(case.id, "first"))
        await entered.wait()
        method = getattr(setup.service, operation)
        args = (case.id, 2) if operation == "ban" else (case.id,)
        second = asyncio.create_task(method(*args, "second"))
        try:
            await asyncio.sleep(0)
            assert not second.done()
            get_case.assert_called_once_with(case.id)
        finally:
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
        return results

    first, second = asyncio.run(run())

    assert first.status is CaseStatus.DELETED
    if operation == "restore":
        assert second.status is CaseStatus.RESTORED
    else:
        assert isinstance(second, CaseError)


def test_cross_instance_concurrent_delete_preserves_cas_winner(setup_case):
    setup = setup_case
    case = setup.create()
    other = CaseService(
        pool=setup.pool, settings=setup.settings, store=setup.store, clock=setup.clock
    )

    async def yield_delete(item):
        await asyncio.sleep(0)

    setup.gateway.delete.side_effect = yield_delete

    async def run():
        return await asyncio.gather(
            setup.service.delete(case.id, "first"),
            other.delete(case.id, "second"),
            return_exceptions=True,
        )

    winner, loser = asyncio.run(run())

    assert winner.status is CaseStatus.DELETED
    assert winner.handled_by == "first"
    assert isinstance(loser, CaseError)
    assert "处理" in str(loser)
    assert setup.gateway.delete.await_count == 2
    assert setup.store.get_case(case.id) == winner


@pytest.mark.parametrize("operation,status", [
    ("delete", CaseStatus.PENDING),
    ("ban", CaseStatus.PENDING),
    ("restore", CaseStatus.DELETED),
])
@pytest.mark.parametrize("missing", ["forum", "account", "gateway"])
def test_missing_account_has_exact_error_without_side_effects(
    setup_case, monkeypatch, operation, status, missing
):
    setup = setup_case
    case = setup.create(status)
    current = setup.snapshot
    if missing == "forum":
        current = replace(current, forums=())
    else:
        forum = replace(current.forums[0], account_id=None if missing == "account" else 99)
        current = replace(current, forums=(forum,))
    monkeypatch.setattr(setup.settings, "resolve", lambda fname: resolve(current, fname))
    events = record_order(setup, monkeypatch)
    with pytest.raises(CaseError) as caught:
        invoke(setup.service, operation, case.id)
    assert str(caught.value) == f"{case.item.fname} 吧没有可用的吧务账号"
    assert events == []
    assert setup.store.get_case(case.id) == case
    setup.clock.assert_not_called()


def test_ignore_needs_neither_configured_forum_nor_gateway(setup_case, monkeypatch):
    setup = setup_case
    case = setup.create()
    monkeypatch.setattr(setup.settings, "resolve", Mock(side_effect=AssertionError))
    asyncio.run(setup.pool.close())
    result = invoke(setup.service, "ignore", case.id)
    assert result.status is CaseStatus.IGNORED
    setup.gateway.delete.assert_not_awaited()
    setup.gateway.block.assert_not_awaited()
    setup.gateway.restore.assert_not_awaited()


def test_disabled_forum_can_still_be_moderated(setup_case, monkeypatch):
    setup = setup_case
    case = setup.create()
    current = replace(
        setup.snapshot, forums=(replace(setup.snapshot.forums[0], enabled=False),)
    )
    monkeypatch.setattr(setup.settings, "resolve", lambda fname: resolve(current, fname))
    assert invoke(setup.service, "delete", case.id).status is CaseStatus.DELETED
    setup.gateway.delete.assert_awaited_once_with(case.item)


def test_each_case_selects_its_own_account(setup_case, monkeypatch):
    setup = setup_case
    first = setup.create()
    second = setup.store.create_case(
        replace(first.item, fname="Other", tid=20, pid=30),
        first.verdict, CaseStatus.PENDING, 100,
    )
    other = SimpleNamespace(delete=AsyncMock(), close=AsyncMock())
    setup.factory.return_value = other
    current = replace(
        setup.snapshot,
        accounts=(*setup.snapshot.accounts, replace(setup.snapshot.accounts[0], id=2)),
        forums=(*setup.snapshot.forums, replace(
            setup.snapshot.forums[0], id=2, fname="other", account_id=2
        )),
    )
    monkeypatch.setattr(setup.settings, "resolve", lambda fname: resolve(current, fname))

    async def scenario():
        await setup.pool.update(current)
        await setup.service.delete(first.id, "first")
        await setup.service.delete(second.id, "second")

    asyncio.run(scenario())
    setup.gateway.delete.assert_awaited_once_with(first.item)
    other.delete.assert_awaited_once_with(second.item)


@pytest.mark.parametrize("operation,status,remote", [
    ("delete", CaseStatus.PENDING, "delete"),
    ("ban", CaseStatus.PENDING, "delete"),
    ("restore", CaseStatus.DELETED, "restore"),
])
@pytest.mark.parametrize("change", ["replace", "delete"])
def test_operation_keeps_gateway_through_remote_actions_and_commit(
    setup_case, monkeypatch, operation, status, remote, change
):
    setup = setup_case
    case = setup.create(status)
    old = setup.gateway
    new = SimpleNamespace(
        delete=AsyncMock(), block=AsyncMock(), restore=AsyncMock(), close=AsyncMock()
    )
    setup.factory.return_value = new
    changed = replace(
        setup.snapshot,
        accounts=(() if change == "delete" else (
            replace(setup.snapshot.accounts[0], bduss="replacement"),
        )),
    )
    transition = setup.store.transition

    def commit(*args, **kwargs):
        old.close.assert_not_awaited()
        return transition(*args, **kwargs)

    monkeypatch.setattr(setup.store, "transition", commit)

    async def change_settings(*args):
        await setup.pool.update(changed)
        old.close.assert_not_awaited()

    getattr(old, remote).side_effect = change_settings
    result = invoke(setup.service, operation, case.id)
    assert result == setup.store.get_case(case.id)
    old.close.assert_awaited_once_with()
    new.delete.assert_not_awaited()
    new.block.assert_not_awaited()
    new.restore.assert_not_awaited()
    if operation == "ban":
        old.block.assert_awaited_once_with(case.item, 3, "广告：重复推广")


def test_partial_ban_failure_commits_before_retired_gateway_closes(setup_case, monkeypatch):
    setup = setup_case
    case = setup.create()
    transition = setup.store.transition

    def commit(*args, **kwargs):
        setup.gateway.close.assert_not_awaited()
        return transition(*args, **kwargs)

    monkeypatch.setattr(setup.store, "transition", commit)

    async def deleted(*args):
        await setup.pool.update(replace(setup.snapshot, accounts=()))

    setup.gateway.delete.side_effect = deleted
    setup.gateway.block.side_effect = TiebaActionError("blocked")
    with pytest.raises(TiebaActionError, match="blocked"):
        invoke(setup.service, "ban", case.id)
    assert setup.store.get_case(case.id).status is CaseStatus.DELETED
    setup.gateway.close.assert_awaited_once_with()


def test_waiting_action_resolves_account_only_after_acquiring_lock(setup_case, monkeypatch):
    setup = setup_case
    case = setup.create()
    current = [setup.snapshot]
    resolution = Mock(side_effect=lambda fname: resolve(current[0], fname))
    monkeypatch.setattr(setup.settings, "resolve", resolution)
    new = SimpleNamespace(delete=AsyncMock(), restore=AsyncMock(), close=AsyncMock())
    setup.factory.return_value = new

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_delete(*args):
            entered.set()
            await release.wait()

        setup.gateway.delete.side_effect = slow_delete
        first = asyncio.create_task(setup.service.delete(case.id, "first"))
        await entered.wait()
        second = asyncio.create_task(setup.service.restore(case.id, "second"))
        await asyncio.sleep(0)
        resolution.assert_called_once_with(case.item.fname)
        current[0] = replace(setup.snapshot, accounts=(
            replace(setup.snapshot.accounts[0], bduss="replacement"),
        ))
        await setup.pool.update(current[0])
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    setup.gateway.delete.assert_awaited_once_with(case.item)
    setup.gateway.restore.assert_not_awaited()
    new.restore.assert_awaited_once_with(case.item)
    setup.gateway.close.assert_awaited_once_with()


def test_cancelled_action_releases_lease_and_lock_without_commit(setup_case, monkeypatch):
    setup = setup_case
    case = setup.create()
    transition = Mock(wraps=setup.store.transition)
    monkeypatch.setattr(setup.store, "transition", transition)

    async def scenario():
        entered = asyncio.Event()

        async def slow_delete(*args):
            entered.set()
            await asyncio.Event().wait()

        setup.gateway.delete.side_effect = slow_delete
        action = asyncio.create_task(setup.service.delete(case.id, "first"))
        await entered.wait()
        await setup.pool.update(replace(setup.snapshot, accounts=()))
        action.cancel()
        with pytest.raises(asyncio.CancelledError):
            await action
        transition.assert_not_called()
        assert setup.store.get_case(case.id) == case
        setup.gateway.close.assert_awaited_once_with()
        assert (await setup.service.ignore(case.id, "second")).status is CaseStatus.IGNORED

    asyncio.run(scenario())
