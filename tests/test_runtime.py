"""Settings changes must not interrupt an active patrol or strand its clients."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from astrbot_plugin_tieba_moderator import main as entry
from astrbot_plugin_tieba_moderator.core.models import PatrolReport
from astrbot_plugin_tieba_moderator.core.settings import AccountDraft, ForumDraft
from astrbot_plugin_tieba_moderator.services.gateway_pool import GatewayPool
from astrbot_plugin_tieba_moderator.tests.fakes import FakeEvent, FakeGateway


def test_empty_install_hot_start_interval_restart_and_client_retirement(tmp_path, monkeypatch):
    clients = []

    def factory(*_):
        client = FakeGateway()
        clients.append(client)
        return client

    monkeypatch.setattr(entry.StarTools, "get_data_dir", lambda _: tmp_path)
    monkeypatch.setattr(entry, "GatewayPool", lambda snapshot: GatewayPool(snapshot, factory=factory))

    async def scenario():
        plugin = entry.TiebaModeratorPlugin(SimpleNamespace())
        await plugin.initialize()
        assert plugin._task is None
        assert plugin._cases is not None
        service = plugin._settings_service
        entered, release, restarted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        delays = []
        run_loop = plugin._patrol_loop

        async def observed_loop(delay=entry.STARTUP_DELAY_SECONDS):
            delays.append(delay)
            if delay == 75:
                restarted.set()
            await run_loop(delay)

        plugin._patrol_loop = observed_loop
        account = await service.create_account(AccountDraft(name="alpha", bduss="offline-a", stoken="offline-b"))
        assert plugin._task is None

        async def round_once():
            async with plugin._pool.lease(account.id) as client:
                entered.set()
                await release.wait()
                await client.delete(None)
            return PatrolReport(started_at=1, finished_at=2)

        plugin._patrol = SimpleNamespace(run_once=round_once)
        forum = await service.create_forum(ForumDraft(fname="alpha", account_id=account.id))
        await asyncio.wait_for(entered.wait(), 1)
        original_task = plugin._task
        await service.save_global(replace(service.snapshot().global_, interval_seconds=75))
        await service.update_account(account.id, AccountDraft(name="alpha", bduss="offline-new"))
        assert plugin._task is original_task and not original_task.done()
        clients[0].close.assert_not_awaited()
        release.set()
        await asyncio.wait_for(original_task, 1)
        await asyncio.wait_for(restarted.wait(), 1)
        assert delays == [0, 75]
        clients[0].delete.assert_awaited_once()
        clients[0].close.assert_awaited_once()
        clients[1].close.assert_not_awaited()
        assert plugin._store.list_reports(1)[0].finished_at == 2
        await service.update_forum(forum.id, ForumDraft(fname="alpha", enabled=False, account_id=account.id))
        stopped = plugin._task
        await asyncio.wait_for(stopped, 1)
        assert plugin._task is None
        await plugin.terminate()
        clients[1].close.assert_awaited_once()

    asyncio.run(scenario())


def test_status_distinguishes_disabled_and_unassigned_forums(tmp_path, monkeypatch):
    monkeypatch.setattr(entry.StarTools, "get_data_dir", lambda _: tmp_path)

    async def scenario():
        plugin = entry.TiebaModeratorPlugin(SimpleNamespace(register_web_api=lambda *args: None))
        await plugin.initialize()
        service = plugin._settings_service
        await service.create_forum(ForumDraft(fname="disabled", enabled=False))
        await service.create_forum(ForumDraft(fname="unassigned"))
        text = "\n".join([value async for value in plugin.tb_status(FakeEvent())])
        assert "disabled 吧：已停用" in text
        assert "unassigned 吧：未选择账号" in text
        assert plugin._task is None
        await plugin.terminate()

    asyncio.run(scenario())


def test_uninitialized_bind_is_refused_without_persisting():
    async def scenario():
        plugin = entry.TiebaModeratorPlugin(SimpleNamespace())
        replies = [value async for value in plugin.tb_bind(FakeEvent())]
        assert "尚未就绪" in replies[0]

    asyncio.run(scenario())
