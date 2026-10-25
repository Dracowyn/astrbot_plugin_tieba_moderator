"""Forum-specific routing, binding and public command behavior without network."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_tieba_moderator import main as entry
from astrbot_plugin_tieba_moderator.core.models import Case, CaseStatus, Verdict
from astrbot_plugin_tieba_moderator.core.store import Store
from astrbot_plugin_tieba_moderator.tests.fakes import (
    FakeConfig,
    FakeEvent,
    FakeGateway,
    make_item,
)
from astrbot_plugin_tieba_moderator.web import routes as web


@pytest.fixture
def plugin_factory(tmp_path):
    """Build plugins with real local stores and offline platform/provider endpoints."""
    stores = []

    def create(**values):
        config = FakeConfig(**values)
        context = SimpleNamespace(
            register_web_api=lambda *args: None,
            send_message=AsyncMock(return_value=True),
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(
                    role="assistant",
                    completion_text='{"violation": false, "confidence": 0.9}',
                )
            ),
        )
        plugin = entry.TiebaModeratorPlugin(context, config)
        plugin._store = Store(tmp_path / f"forum-{len(stores)}.db")
        stores.append(plugin._store)
        return plugin

    yield create
    for store in stores:
        store.close()


def case_in(fname):
    """Create a case whose source forum determines the push route."""
    return Case(
        id=12,
        item=make_item(fname=fname),
        verdict=Verdict(violation=True),
        status=CaseStatus.PENDING,
        created_at=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fname,expected",
    [
        (" PYTHON ", ["local", "second"]),
        ("龙", ["global"]),
        ("unknown", ["global"]),
    ],
)
async def test_notify_selects_only_the_effective_forum_sessions(
    plugin_factory, fname, expected
):
    plugin = plugin_factory(
        notify={"sessions": ["global"]},
        forum_overrides=[
            {"fname": "Python", "sessions": [" local ", "local", "second"]},
            {"fname": "龙", "sessions": []},
        ],
    )
    await plugin._notify(case_in(fname))
    assert [c.args[0] for c in plugin.context.send_message.await_args_list] == expected


@pytest.mark.asyncio
async def test_missing_route_names_the_case_and_forum(plugin_factory):
    plugin = plugin_factory(forum_overrides=[{"fname": "龙"}])
    with pytest.raises(
        RuntimeError, match=r"案件 #12（龙 吧）未推送：没有绑定推送会话"
    ):
        await plugin._notify(case_in("龙"))
    plugin.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_forum_bind_creates_override_without_copying_global_route(plugin_factory):
    plugin = plugin_factory(forums=["Python"], notify={"sessions": ["global"]})
    event = FakeEvent()
    replies = [reply async for reply in plugin.tb_bind(event, " pYTHON ")]
    assert "Python" in replies[0]
    assert plugin.config["forum_overrides"] == [
        {
            "__template_key": "forum",
            "fname": "Python",
            "rules": "",
            "sessions": [event.unified_msg_origin],
            "operators": [],
        }
    ]
    assert plugin.config["notify"]["sessions"] == ["global"]
    await plugin._notify(case_in("PYTHON"))
    assert plugin.context.send_message.await_args.args[0] == event.unified_msg_origin
    assert plugin.config.saves == 1


@pytest.mark.asyncio
async def test_forum_bind_appends_to_first_override_preserving_every_other_entry(
    plugin_factory,
):
    entries = [
        None,
        {
            "fname": " Python ",
            "rules": "local rule",
            "sessions": ["old"],
            "operators": ["local-user"],
            "extension": {"keep": True},
        },
        {"fname": "PYTHON", "sessions": ["duplicate"]},
        {"fname": "龙", "sessions": ["other"]},
    ]
    before = deepcopy(entries)
    plugin = plugin_factory(forum_overrides=entries)
    event = FakeEvent()
    [reply async for reply in plugin.tb_bind(event, "python")]
    expected = deepcopy(before)
    expected[1]["sessions"].append(event.unified_msg_origin)
    assert plugin.config["forum_overrides"] == expected
    assert entries == before
    [reply async for reply in plugin.tb_bind(event, "PYTHON")]
    assert plugin.config.saves == 1
    await plugin._notify(case_in("python"))
    assert [c.args[0] for c in plugin.context.send_message.await_args_list] == [
        "old",
        event.unified_msg_origin,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("sessions", [None, "not-a-list", [], [" "]])
async def test_forum_bind_replaces_empty_or_invalid_local_sessions(
    plugin_factory, sessions
):
    plugin = plugin_factory(
        notify={"sessions": ["global"]},
        forum_overrides=[{"fname": "龙", "sessions": sessions}],
    )
    event = FakeEvent()
    [reply async for reply in plugin.tb_bind(event, "龙")]
    assert plugin.config["forum_overrides"][0]["sessions"] == [event.unified_msg_origin]
    await plugin._notify(case_in("龙"))
    assert [c.args[0] for c in plugin.context.send_message.await_args_list] == [
        event.unified_msg_origin
    ]


@pytest.mark.asyncio
async def test_bind_rejects_unpatrolled_forum_without_saving(plugin_factory):
    plugin = plugin_factory(forums=["Python"], forum_overrides=[{"fname": "龙"}])
    before = deepcopy(plugin.config)
    replies = [reply async for reply in plugin.tb_bind(FakeEvent(), "other")]
    assert "未" in replies[0] and "巡查" in replies[0]
    assert plugin.config == before
    assert plugin.config.saves == 0


@pytest.mark.asyncio
async def test_global_bind_refreshes_inherited_routes_only(plugin_factory):
    plugin = plugin_factory(
        notify={"sessions": [], "operators": ["global-user"]},
        forum_overrides=[{"fname": "龙", "sessions": ["local"]}],
    )
    event = FakeEvent()
    [reply async for reply in plugin.tb_bind(event)]
    [reply async for reply in plugin.tb_bind(event)]
    await plugin._notify(case_in("Python"))
    await plugin._notify(case_in("龙"))
    assert [c.args[0] for c in plugin.context.send_message.await_args_list] == [
        event.unified_msg_origin,
        "local",
    ]
    assert plugin.config["notify"]["operators"] == ["global-user"]
    assert plugin.config.saves == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fname", ["", "龙"])
async def test_bind_remains_administrator_only(plugin_factory, fname):
    plugin = plugin_factory(forums=["龙"])
    event = FakeEvent()
    event.is_admin = lambda: False
    assert [reply async for reply in plugin.tb_bind(event, fname)] == [
        entry.ADMIN_ONLY_REPLY
    ]
    assert plugin.config.saves == 0


@pytest.mark.asyncio
async def test_status_lists_union_with_explicit_override_labels_but_no_ids(
    plugin_factory,
):
    plugin = plugin_factory(
        forums=["Python", "PYTHON", "默认"],
        judge={"rules": "same rule"},
        notify={"sessions": ["global-session"], "operators": ["global-user"]},
        forum_overrides=[
            {
                "fname": "python",
                "rules": "same rule",
                "sessions": ["global-session"],
                "operators": ["local-user"],
            },
            {"fname": "龙", "rules": "local rule"},
            {"fname": "推送", "sessions": ["local-session"]},
            {"fname": "默认", "rules": "  ", "sessions": [" "]},
        ],
    )
    reply = await anext(plugin.tb_status(FakeEvent()))
    assert (
        "吧：Python（独立吧规、独立推送）、默认、龙（独立吧规）、推送（独立推送）"
        in reply
    )
    for private in ("global-session", "global-user", "local-session", "local-user"):
        assert private not in reply


def test_web_status_reports_the_merged_patrol_forums(plugin_factory):
    plugin = plugin_factory(
        forums=["Python"], forum_overrides=[{"fname": "龙"}, {"fname": "PYTHON"}]
    )
    response = asyncio.run(plugin._web.get_status())
    assert json.loads(response.body)["forums"] == ["Python", "龙"]


def test_web_trial_uses_the_fetched_forum_rules_when_temporary_rules_are_blank(
    plugin_factory, monkeypatch
):
    plugin = plugin_factory(
        judge={"provider_id": "offline", "rules": "global rule"},
        forum_overrides=[{"fname": "Python", "rules": "forum rule"}],
    )
    plugin._gateway = FakeGateway()
    plugin._gateway.get_thread_item_by_tid.return_value = make_item(fname=" PYTHON ")
    monkeypatch.setattr(
        web,
        "request",
        SimpleNamespace(
            json=AsyncMock(return_value={"tid": 10, "rules": "  "}),
        ),
    )
    response = asyncio.run(plugin._web.post_check())
    assert response.status_code == 200
    prompt = plugin.context.llm_generate.await_args.kwargs["system_prompt"]
    assert "forum rule" in prompt and "global rule" not in prompt
    assert plugin._store.query_cases()[0] == 0


@pytest.mark.asyncio
async def test_chat_trial_uses_the_fetched_forum_rules(plugin_factory):
    plugin = plugin_factory(
        judge={"provider_id": "offline", "rules": "global rule"},
        forum_overrides=[{"fname": "Python", "rules": "forum rule"}],
    )
    plugin._gateway = FakeGateway()
    plugin._gateway.get_thread_item_by_tid.return_value = make_item(fname="PYTHON")
    replies = [reply async for reply in plugin.tb_check(FakeEvent(), "10")]
    assert "审查结果" in replies[0]
    prompt = plugin.context.llm_generate.await_args.kwargs["system_prompt"]
    assert "forum rule" in prompt and "global rule" not in prompt
    assert plugin._store.query_cases()[0] == 0
