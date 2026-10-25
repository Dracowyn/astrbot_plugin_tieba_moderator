"""Forum routing, persistent binding and public commands without network."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_tieba_moderator import main as entry
from astrbot_plugin_tieba_moderator.core.models import Case, CaseStatus, Verdict
from astrbot_plugin_tieba_moderator.core.settings import (
    AccountDraft, ForumDraft, ForumOptions, ForumOverrides, GlobalSettings,
)
from astrbot_plugin_tieba_moderator.core.store import Store
from astrbot_plugin_tieba_moderator.services.gateway_pool import GatewayPool
from astrbot_plugin_tieba_moderator.services.llm_judge import LlmJudge
from astrbot_plugin_tieba_moderator.services.settings_service import SettingsService
from astrbot_plugin_tieba_moderator.tests.fakes import FakeEvent, FakeGateway, make_item
from astrbot_plugin_tieba_moderator.web import routes as web


@pytest.fixture
def plugin_factory(tmp_path):
    """Build offline plugins with persisted settings and fake gateway clients."""
    stores = []

    def create(*, forums=(), global_=None):
        context = SimpleNamespace(
            register_web_api=lambda *args: None,
            send_message=AsyncMock(return_value=True),
            llm_generate=AsyncMock(return_value=SimpleNamespace(
                role="assistant", completion_text='{"violation": false, "confidence": 0.9}',
            )),
        )
        plugin = entry.TiebaModeratorPlugin(context)
        plugin._store = Store(tmp_path / f"forum-{len(stores)}.db")
        stores.append(plugin._store)
        plugin._store.save_global(global_ or GlobalSettings(defaults=ForumOptions(rules="global rule", provider_id="offline")))
        plugin._store.insert_account(AccountDraft(name="offline", bduss="fake", stoken="fake"))
        for draft in forums:
            plugin._store.insert_forum(draft)
        plugin._settings_service = SettingsService(plugin._store)
        plugin._pool = GatewayPool(plugin._snapshot(), factory=lambda bduss, stoken: FakeGateway())
        plugin._judge = LlmJudge(context, settings=plugin._settings_service)
        return plugin

    yield create
    for store in stores:
        store.close()


def global_settings(*, sessions=(), operators=frozenset(), rules="global rule"):
    return GlobalSettings(defaults=ForumOptions(rules=rules, provider_id="offline"), sessions=sessions, operators=operators)


def case_in(fname):
    return Case(id=12, item=make_item(fname=fname), verdict=Verdict(violation=True), status=CaseStatus.PENDING, created_at=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("fname,expected", [(" PYTHON ", ["local", "second"]), ("龙", ["global"]), ("unknown", ["global"])])
async def test_notify_selects_only_effective_forum_sessions(plugin_factory, fname, expected):
    plugin = plugin_factory(global_=global_settings(sessions=("global",)), forums=(
        ForumDraft(fname="Python", sessions=("local", "second")), ForumDraft(fname="龙"),
    ))
    await plugin._notify(case_in(fname))
    assert [call.args[0] for call in plugin.context.send_message.await_args_list] == expected


@pytest.mark.asyncio
async def test_missing_route_names_case_and_forum(plugin_factory):
    plugin = plugin_factory(forums=(ForumDraft(fname="龙"),))
    with pytest.raises(RuntimeError, match=r"案件 #12（龙 吧）未推送：没有绑定推送会话"):
        await plugin._notify(case_in("龙"))
    plugin.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_forum_bind_sets_local_route_without_copying_global_route(plugin_factory):
    plugin = plugin_factory(global_=global_settings(sessions=("global",)), forums=(ForumDraft(fname="Python", account_id=1),))
    event = FakeEvent()
    before = plugin._snapshot()
    replies = [reply async for reply in plugin.tb_bind(event, " pYTHON ")]
    assert "Python" in replies[0]
    after = plugin._snapshot()
    assert after.forums[0].sessions == (event.unified_msg_origin,)
    assert after.global_ == before.global_
    assert after.revision == before.revision + 1
    assert plugin._store.load_settings() == after
    await plugin._notify(case_in("PYTHON"))
    assert plugin.context.send_message.await_args.args[0] == event.unified_msg_origin


@pytest.mark.asyncio
async def test_forum_bind_preserves_options_other_forums_and_is_idempotent(plugin_factory):
    plugin = plugin_factory(forums=(
        ForumDraft(fname="Python", account_id=1, overrides=ForumOverrides(rules="local rule"), sessions=("old",), operators=frozenset({"local-user"})),
        ForumDraft(fname="龙", sessions=("other",)),
    ))
    event = FakeEvent()
    before = plugin._snapshot()
    [reply async for reply in plugin.tb_bind(event, "python")]
    after = plugin._snapshot()
    assert after.forums[0].sessions == ("old", event.unified_msg_origin)
    assert replace(after.forums[0], sessions=before.forums[0].sessions, updated_at=before.forums[0].updated_at) == before.forums[0]
    assert after.forums[1] == before.forums[1]
    assert after.global_ == before.global_
    assert before.forums[0].sessions == ("old",)
    duplicate = [reply async for reply in plugin.tb_bind(event, "PYTHON")]
    assert "已经" in duplicate[0]
    assert plugin._snapshot() == after
    await plugin._notify(case_in("python"))
    assert [call.args[0] for call in plugin.context.send_message.await_args_list] == ["old", event.unified_msg_origin]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["other", "Disabled", "Unassigned"])
async def test_bind_rejects_unpatrolled_forum_without_saving(plugin_factory, name):
    plugin = plugin_factory(forums=(ForumDraft(fname="Python", account_id=1), ForumDraft(fname="Disabled", enabled=False, account_id=1), ForumDraft(fname="Unassigned")))
    before = plugin._snapshot()
    replies = [reply async for reply in plugin.tb_bind(FakeEvent(), name)]
    assert "未" in replies[0] and "巡查" in replies[0]
    assert plugin._snapshot() == before
    assert plugin._store.load_settings() == before


@pytest.mark.asyncio
async def test_global_bind_refreshes_inherited_routes_only(plugin_factory):
    plugin = plugin_factory(global_=global_settings(operators=frozenset({"global-user"})), forums=(ForumDraft(fname="龙", sessions=("local",)),))
    event = FakeEvent()
    before = plugin._snapshot()
    [reply async for reply in plugin.tb_bind(event)]
    [reply async for reply in plugin.tb_bind(event)]
    await plugin._notify(case_in("Python"))
    await plugin._notify(case_in("龙"))
    assert [call.args[0] for call in plugin.context.send_message.await_args_list] == [event.unified_msg_origin, "local"]
    assert plugin._snapshot().global_.operators == frozenset({"global-user"})
    assert plugin._snapshot().revision == before.revision + 1
    assert plugin._snapshot().forums == before.forums


@pytest.mark.asyncio
@pytest.mark.parametrize("fname", ["", "龙"])
async def test_bind_remains_administrator_only(plugin_factory, fname):
    plugin = plugin_factory(forums=(ForumDraft(fname="龙", account_id=1),))
    before = plugin._snapshot()
    event = FakeEvent()
    event.is_admin = lambda: False
    assert [reply async for reply in plugin.tb_bind(event, fname)] == [entry.ADMIN_ONLY_REPLY]
    assert plugin._snapshot() == before


@pytest.mark.asyncio
async def test_status_lists_configured_forums_with_override_labels_but_no_ids(plugin_factory):
    plugin = plugin_factory(global_=global_settings(sessions=("global-session",), operators=frozenset({"global-user"}), rules="same rule"), forums=(
        ForumDraft(fname="Python", overrides=ForumOverrides(rules="same rule"), sessions=("global-session",), operators=frozenset({"local-user"})),
        ForumDraft(fname="默认"),
        ForumDraft(fname="龙", overrides=ForumOverrides(rules="local rule")),
        ForumDraft(fname="推送", sessions=("local-session",)),
    ))
    reply = await anext(plugin.tb_status(FakeEvent()))
    for fname, labels in (("Python", ("独立吧规", "独立推送")), ("默认", ()), ("龙", ("独立吧规",)), ("推送", ("独立推送",))):
        line = next(line for line in reply.splitlines() if line.startswith(fname))
        assert all(label in line for label in labels)
    for private in ("global-session", "global-user", "local-session", "local-user"):
        assert private not in reply


def test_web_status_reports_all_configured_forums(plugin_factory):
    plugin = plugin_factory(forums=(ForumDraft(fname="Python", account_id=1), ForumDraft(fname="龙", enabled=False)))
    response = asyncio.run(plugin._web.get_status())
    payload = json.loads(response.body)
    forums = payload["forums"]
    assert [forum["fname"] for forum in forums] == ["Python", "龙"]
    # A disabled forum stays visible with the reason it is not patrolled.
    assert [forum["problem"] for forum in forums] == ["", "已停用"]
    assert payload["configured"] is True


def test_web_trial_uses_fetched_forum_rules_when_temporary_rules_blank(plugin_factory, monkeypatch):
    plugin = plugin_factory(forums=(ForumDraft(fname="Python", overrides=ForumOverrides(rules="forum rule")),))
    plugin._pool.any().get_thread_item_by_tid.return_value = make_item(fname=" PYTHON ")
    monkeypatch.setattr(web, "request", SimpleNamespace(json=AsyncMock(return_value={"tid": 10, "rules": "  "})))
    response = asyncio.run(plugin._web.post_check())
    assert response.status_code == 200
    prompt = plugin.context.llm_generate.await_args.kwargs["system_prompt"]
    assert "forum rule" in prompt and "global rule" not in prompt
    assert plugin._store.query_cases()[0] == 0


@pytest.mark.asyncio
async def test_chat_trial_uses_fetched_forum_rules(plugin_factory):
    plugin = plugin_factory(forums=(ForumDraft(fname="Python", overrides=ForumOverrides(rules="forum rule")),))
    plugin._pool.any().get_thread_item_by_tid.return_value = make_item(fname="PYTHON")
    replies = [reply async for reply in plugin.tb_check(FakeEvent(), "10")]
    assert "审查结果" in replies[0]
    prompt = plugin.context.llm_generate.await_args.kwargs["system_prompt"]
    assert "forum rule" in prompt and "global rule" not in prompt
    assert plugin._store.query_cases()[0] == 0
