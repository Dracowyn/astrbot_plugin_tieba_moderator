"""Offline leases keep retired clients alive until their final user exits."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from astrbot_plugin_tieba_moderator.core.settings import (
    Account,
    ForumOptions,
    GlobalSettings,
    SettingsSnapshot,
)
from astrbot_plugin_tieba_moderator.services.gateway_pool import GatewayPool


def snapshot(*accounts):
    return SettingsSnapshot(
        global_=GlobalSettings(defaults=ForumOptions(rules="rules")),
        accounts=tuple(accounts),
    )


def account(account_id=1, **changes):
    return replace(
        Account(
            id=account_id,
            name=f"account-{account_id}",
            bduss=f"fake-bduss-{account_id}",
            stoken=f"fake-stoken-{account_id}",
            created_at=1,
            updated_at=1,
        ),
        **changes,
    )


@pytest.fixture
def factory():
    return Mock(side_effect=lambda *args: SimpleNamespace(close=AsyncMock()))


def test_empty_pool_and_missing_leases(factory):
    async def scenario():
        pool = GatewayPool(snapshot(), factory=factory)
        assert pool.get(1) is None
        assert pool.any() is None
        async with pool.lease() as gateway:
            assert gateway is None
        async with pool.lease(1) as gateway:
            assert gateway is None
        async with pool.lease_many([1, 1, 2]) as gateways:
            assert gateways == {}
        await pool.update(snapshot())
        await pool.close()
        await pool.close()
        factory.assert_not_called()

    asyncio.run(scenario())


def test_creation_name_only_update_and_idempotent_shutdown(factory):
    async def scenario():
        first, second = account(), account(2)
        pool = GatewayPool(snapshot(first, second), factory=factory)
        one, two = pool.get(1), pool.get(2)
        assert one is not two
        assert pool.any() is one
        assert factory.call_args_list == [
            call(first.bduss, first.stoken), call(second.bduss, second.stoken)
        ]
        await pool.update(snapshot(replace(first, name="renamed", updated_at=2), second))
        assert pool.get(1) is one
        assert factory.call_count == 2
        async with pool.lease() as gateway:
            assert gateway is one
        async with pool.lease(2) as gateway:
            assert gateway is two
        async with pool.lease_many([2, 1, 2, 99]) as gateways:
            assert gateways == {1: one, 2: two}
        one.close.assert_not_awaited()
        two.close.assert_not_awaited()
        await asyncio.gather(pool.close(), pool.close())
        await pool.close()
        one.close.assert_awaited_once_with()
        two.close.assert_awaited_once_with()
        assert pool.get(1) is None
        assert pool.any() is None
        async with pool.lease(1) as gateway:
            assert gateway is None
        async with pool.lease_many([1, 2]) as gateways:
            assert gateways == {}
        with pytest.raises(RuntimeError, match="closed"):
            await pool.update(snapshot(first))
        assert factory.call_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["bduss", "stoken"])
def test_idle_replacement_and_deletion_close_before_update_returns(factory, field):
    async def scenario():
        first = account()
        pool = GatewayPool(snapshot(first), factory=factory)
        old = pool.get(1)
        await pool.update(snapshot(replace(first, **{field: "new-fake-credential"})))
        current = pool.get(1)
        assert current is not old
        old.close.assert_awaited_once_with()
        current.close.assert_not_awaited()
        await pool.update(snapshot())
        current.close.assert_awaited_once_with()
        assert pool.any() is None
        await pool.close()
        old.close.assert_awaited_once_with()
        current.close.assert_awaited_once_with()

    asyncio.run(scenario())


def test_replacement_and_delete_defer_until_last_nested_lease(factory):
    async def scenario():
        first = account()
        pool = GatewayPool(snapshot(first), factory=factory)
        async with pool.lease(1) as old:
            async with pool.lease_many([1, 1]) as gateways:
                assert gateways[1] is old
                await pool.update(snapshot(replace(first, bduss="replacement")))
                new = pool.get(1)
                async with pool.lease(1) as leased_new:
                    assert leased_new is new
                    await pool.update(snapshot())
                    assert pool.get(1) is None
                    old.close.assert_not_awaited()
                    new.close.assert_not_awaited()
                new.close.assert_awaited_once_with()
            old.close.assert_not_awaited()
        old.close.assert_awaited_once_with()
        await pool.close()
        old.close.assert_awaited_once_with()
        new.close.assert_awaited_once_with()

    asyncio.run(scenario())


def test_many_pins_every_account_before_first_suspension(factory):
    async def scenario():
        pool = GatewayPool(snapshot(account(), account(2)), factory=factory)
        one, two = pool.get(1), pool.get(2)
        removal = asyncio.create_task(pool.update(snapshot()))
        async with pool.lease_many([1, 2]) as gateways:
            await removal
            assert gateways == {1: one, 2: two}
            one.close.assert_not_awaited()
            two.close.assert_not_awaited()
        one.close.assert_awaited_once_with()
        two.close.assert_awaited_once_with()
        await pool.close()

    asyncio.run(scenario())


def test_shutdown_waits_active_current_and_retired_instances(factory):
    async def scenario():
        first = account()
        pool = GatewayPool(snapshot(first), factory=factory)
        async with pool.lease(1) as old:
            await pool.update(snapshot(replace(first, bduss="replacement")))
            async with pool.lease(1) as current:
                shutdown = asyncio.create_task(pool.close())
                await asyncio.sleep(0)
                assert not shutdown.done()
                assert pool.any() is None
                old.close.assert_not_awaited()
                current.close.assert_not_awaited()
            current.close.assert_awaited_once_with()
            old.close.assert_not_awaited()
            assert not shutdown.done()
        await shutdown
        old.close.assert_awaited_once_with()
        await pool.close()
        current.close.assert_awaited_once_with()

    asyncio.run(scenario())


@pytest.mark.parametrize("many", [False, True])
def test_cancelled_lease_body_releases_retired_clients(factory, many):
    async def scenario():
        pool = GatewayPool(snapshot(account()), factory=factory)
        old = pool.get(1)
        entered = asyncio.Event()

        async def operation():
            lease = pool.lease_many([1]) if many else pool.lease(1)
            async with lease:
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(operation())
        await entered.wait()
        await pool.update(snapshot())
        old.close.assert_not_awaited()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        old.close.assert_awaited_once_with()
        await pool.close()
        old.close.assert_awaited_once_with()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["update", "release", "close"])
def test_cancellation_during_closure_waits_and_never_cancels_client(factory, operation):
    async def scenario():
        pool = GatewayPool(snapshot(account()), factory=factory)
        gateway = pool.get(1)
        closing = asyncio.Event()
        finish = asyncio.Event()
        closed = asyncio.Event()

        async def slow_close():
            closing.set()
            await finish.wait()
            closed.set()

        gateway.close.side_effect = slow_close
        if operation == "release":
            lease = pool.lease(1)
            await lease.__aenter__()
            await pool.update(snapshot())
            task = asyncio.create_task(lease.__aexit__(None, None, None))
        elif operation == "update":
            task = asyncio.create_task(pool.update(snapshot()))
        else:
            task = asyncio.create_task(pool.close())
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not closed.is_set()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
        await pool.close()
        gateway.close.assert_awaited_once_with()

    asyncio.run(scenario())


def test_cancelled_shutdown_keeps_waiting_for_active_lease(factory):
    async def scenario():
        pool = GatewayPool(snapshot(account()), factory=factory)
        async with pool.lease(1) as gateway:
            shutdown = asyncio.create_task(pool.close())
            await asyncio.sleep(0)
            shutdown.cancel()
            await asyncio.sleep(0)
            assert not shutdown.done()
            gateway.close.assert_not_awaited()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        await pool.close()
        gateway.close.assert_awaited_once_with()

    asyncio.run(scenario())


def test_failed_factory_update_keeps_current_and_closes_new_instances(factory):
    async def scenario():
        first = account()
        pool = GatewayPool(snapshot(first), factory=factory)
        original = pool.get(1)
        unused = SimpleNamespace(close=AsyncMock())
        factory.side_effect = [unused, RuntimeError("factory failed")]
        with pytest.raises(RuntimeError, match="factory failed"):
            await pool.update(snapshot(first, account(2), account(3)))
        assert pool.get(1) is original
        assert pool.get(2) is None
        original.close.assert_not_awaited()
        unused.close.assert_awaited_once_with()
        await pool.close()
        original.close.assert_awaited_once_with()
        unused.close.assert_awaited_once_with()

    asyncio.run(scenario())


def test_close_failure_does_not_skip_other_clients_or_retry_closure(factory):
    async def scenario():
        pool = GatewayPool(snapshot(account(), account(2)), factory=factory)
        first, second = pool.get(1), pool.get(2)
        first.close.side_effect = RuntimeError("close failed")
        with pytest.raises(RuntimeError, match="close failed"):
            await pool.close()
        first.close.assert_awaited_once_with()
        second.close.assert_awaited_once_with()
        await pool.close()
        first.close.assert_awaited_once_with()
        second.close.assert_awaited_once_with()

    asyncio.run(scenario())


def test_lease_exception_preserved_and_all_references_released(factory):
    async def scenario():
        pool = GatewayPool(snapshot(account(), account(2)), factory=factory)
        one, two = pool.get(1), pool.get(2)
        with pytest.raises(ValueError, match="operation failed"):
            async with pool.lease_many([1, 2]):
                await pool.update(snapshot())
                raise ValueError("operation failed")
        one.close.assert_awaited_once_with()
        two.close.assert_awaited_once_with()
        await pool.close()

    asyncio.run(scenario())


def test_account_addition_preserves_existing_gateway(factory):
    async def scenario():
        first = account()
        pool = GatewayPool(snapshot(first), factory=factory)
        original = pool.get(1)
        await pool.update(snapshot(first, account(2)))
        added = pool.get(2)
        assert pool.get(1) is original
        assert added is not None and added is not original
        assert factory.call_count == 2
        original.close.assert_not_awaited()
        added.close.assert_not_awaited()
        await pool.close()
        original.close.assert_awaited_once_with()
        added.close.assert_awaited_once_with()

    asyncio.run(scenario())


def test_update_publishes_new_gateway_before_waiting_for_idle_close(factory):
    async def scenario():
        first = account()
        pool = GatewayPool(snapshot(first), factory=factory)
        old = pool.get(1)
        closing = asyncio.Event()
        finish = asyncio.Event()

        async def slow_close():
            closing.set()
            await finish.wait()

        old.close.side_effect = slow_close
        update = asyncio.create_task(pool.update(snapshot(replace(first, bduss="new"))))
        await closing.wait()
        current = pool.get(1)
        assert current is not old
        assert not update.done()
        async with pool.lease(1) as gateway:
            assert gateway is current
            current.close.assert_not_awaited()
            finish.set()
            await update
        await pool.close()
        old.close.assert_awaited_once_with()
        current.close.assert_awaited_once_with()

    asyncio.run(scenario())
