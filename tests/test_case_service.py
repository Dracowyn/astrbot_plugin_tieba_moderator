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
from astrbot_plugin_tieba_moderator.core.store import Store
from astrbot_plugin_tieba_moderator.services.case_service import CaseError, CaseService

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
        delete=AsyncMock(), block=AsyncMock(), restore=AsyncMock()
    )
    clock = Mock(side_effect=(value + 0.75 for value in count(1000)))
    service = CaseService(gateway=gateway, store=store, clock=clock)

    def create(status=CaseStatus.PENDING):
        return store.create_case(item, verdict, status, 100)

    yield SimpleNamespace(
        store=store,
        gateway=gateway,
        clock=clock,
        service=service,
        create=create,
    )
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
    other = CaseService(gateway=setup.gateway, store=setup.store, clock=setup.clock)

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
