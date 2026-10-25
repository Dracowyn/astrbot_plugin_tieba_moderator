"""Per-forum permission isolation: three kinds of senders, every command."""

import dataclasses as dcs
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_tieba_moderator import main as entry
from astrbot_plugin_tieba_moderator.core.forums import (
    GLOBAL_SCOPE,
    OperatorScope,
    parse_forum_config,
)
from astrbot_plugin_tieba_moderator.core.models import CaseStatus, Verdict
from astrbot_plugin_tieba_moderator.tests.fakes import (
    FakeConfig,
    FakeGateway,
    make_item,
)

GLOBAL_OP = "2001"
LOCAL_OP = "3001"
NOBODY = "4001"
ADMIN = "1001"
FORUM = "Dragon"
OTHER = "Other"


class Sender:
    """Command event whose admin flag and sender id tests can switch."""

    unified_msg_origin = "offline:group:test"

    def __init__(self, sender_id, *, is_admin=False):
        self.sender_id = sender_id
        self.admin = is_admin

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return self.sender_id

    def plain_result(self, text):
        return text


def config(**overrides):
    """Config with a global operator, a local moderator of one forum, and both forums."""
    values = {
        "forums": [FORUM],
        "forum_overrides": [
            {
                "__template_key": "forum",
                "fname": FORUM,
                "rules": "",
                "sessions": [],
                "operators": [LOCAL_OP],
            },
            {
                "__template_key": "forum",
                "fname": OTHER,
                "rules": "",
                "sessions": [],
                "operators": [],
            },
        ],
        "notify": {"sessions": [], "operators": [GLOBAL_OP]},
        "patrol": {"enabled": False},
    }
    values.update(overrides)
    return FakeConfig(**values)


def make_plugin(config_overrides=None):
    """Plugin with a fake store, gateway and case service."""
    context = FakeStoreContext()
    plugin = entry.TiebaModeratorPlugin(
        context, config(**(config_overrides or {}))
    )
    plugin._store = context.store
    plugin._gateway = FakeGateway()
    plugin._cases = context.cases
    plugin._patrol = AsyncMock()
    return plugin


class FakeStoreContext:
    """Context stub exposing a store and case-service double."""

    def __init__(self):
        self.registered = []
        self.store = FakeStore()
        self.cases = FakeCaseService(self.store)
        self.send_message = AsyncMock(return_value=True)

    def register_web_api(self, *args):
        self.registered.append(args)


class FakeStore:
    """Only the two reads the commands use."""

    def __init__(self):
        self.rows = {}
        self.queries = []

    def get_case(self, case_id):
        return self.rows.get(case_id)

    def query_cases(self, **kwargs):
        self.queries.append(kwargs)
        wanted = kwargs.get("fnames") or None
        statuses = kwargs.get("statuses") or ()
        items = [
            case
            for case in sorted(self.rows.values(), key=lambda c: -c.id)
            if (not statuses or case.status in statuses)
            and (wanted is None or case.item.fname.strip().casefold() in wanted)
        ]
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 20)
        return len(items), items[offset : offset + limit]


class FakeCaseService:
    """Records calls so tests can assert nothing reached Tieba."""

    def __init__(self, store):
        self.store = store
        self.calls = []

    async def delete(self, case_id, operator):
        self.calls.append(("delete", case_id, operator))
        return dcs.replace(self.store.get_case(case_id), status=CaseStatus.DELETED)

    async def ban(self, case_id, days, operator):
        self.calls.append(("ban", case_id, days, operator))
        return dcs.replace(self.store.get_case(case_id), status=CaseStatus.BANNED)

    async def ignore(self, case_id, operator):
        self.calls.append(("ignore", case_id, operator))
        return dcs.replace(self.store.get_case(case_id), status=CaseStatus.IGNORED)

    async def restore(self, case_id, operator):
        self.calls.append(("restore", case_id, operator))
        return dcs.replace(self.store.get_case(case_id), status=CaseStatus.RESTORED)


def add_case(plugin, case_id, *, fname, status=CaseStatus.PENDING):
    """Insert a stored case with a controlled forum."""
    case = entry.Case(
        id=case_id,
        item=make_item(fname=fname),
        verdict=Verdict(violation=True, category="广告", confidence=0.9),
        status=status,
        created_at=2_000_000_000,
    )
    plugin._store.rows[case_id] = case
    return case


async def collect(agen):
    """Drain a command generator."""
    return [reply async for reply in agen]


# --- scope resolution -------------------------------------------------------


def test_scope_recognizes_admin_global_and_local_operators():
    forum_config, warnings = parse_forum_config(config())
    assert warnings == []
    assert forum_config.scope_for(ADMIN, is_admin=True) == GLOBAL_SCOPE
    assert forum_config.scope_for(GLOBAL_OP, is_admin=False) == GLOBAL_SCOPE
    local = forum_config.scope_for(LOCAL_OP, is_admin=False)
    assert local.global_operator is False
    assert local.forums == frozenset({"dragon"})
    assert local.any is True
    stranger = forum_config.scope_for(NOBODY, is_admin=False)
    assert stranger == OperatorScope(global_operator=False)
    assert stranger.any is False


def test_scope_compares_ids_without_surrounding_whitespace():
    forum_config, _ = parse_forum_config(config())
    assert forum_config.is_global_operator(" 2001 ", is_admin=False) is True
    assert forum_config.scope_for(" 3001", is_admin=False).forums == frozenset({"dragon"})


@pytest.mark.parametrize("fname", ["Dragon", "dragon", "DRAGON", " Dragon ", "dRAGON"])
def test_forum_names_match_regardless_of_case_and_padding(fname):
    forum_config, _ = parse_forum_config(config())
    scope = forum_config.scope_for(LOCAL_OP, is_admin=False)
    assert forum_config.may_moderate(scope, fname) is True


@pytest.mark.parametrize("fname", ["Other", "", "Dragon2", "Drag", "龙"])
def test_forum_names_do_not_match_other_forums(fname):
    forum_config, _ = parse_forum_config(config())
    scope = forum_config.scope_for(LOCAL_OP, is_admin=False)
    assert forum_config.may_moderate(scope, fname) is False


def test_global_operator_may_moderate_any_forum():
    forum_config, _ = parse_forum_config(config())
    assert forum_config.may_moderate(GLOBAL_SCOPE, "anything") is True


# --- run --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender,allowed",
    [
        (Sender(ADMIN, is_admin=True), True),
        (Sender(GLOBAL_OP), True),
        (Sender(LOCAL_OP), False),
        (Sender(NOBODY), False),
    ],
)
async def test_run_is_restricted_to_global_operators(sender, allowed):
    plugin = make_plugin()
    plugin._patrol.run_once = AsyncMock(
        side_effect=RuntimeError("stop after permission check")
    )
    plugin._settings = dcs.replace(plugin._settings, forums=(FORUM,))
    replies = await collect(plugin.tb_run(sender))
    if allowed:
        assert replies[0].startswith("开始巡查")
    else:
        assert replies == [entry.NO_PERMISSION_REPLY]
        plugin._patrol.run_once.assert_not_awaited()


# --- status -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender,shows_errors",
    [
        (Sender(ADMIN, is_admin=True), True),
        (Sender(GLOBAL_OP), True),
        (Sender(LOCAL_OP), False),
        (Sender(NOBODY), False),
    ],
)
async def test_status_error_details_are_for_global_operators_only(
    sender, shows_errors
):
    plugin = make_plugin()
    add_case(plugin, 1, fname=FORUM)
    plugin._last_report = entry.PatrolReport(
        started_at=1, finished_at=2, errors=("post:1: secret failure detail",)
    )
    text = (await collect(plugin.tb_status(sender)))[0]
    assert "巡查：" in text
    assert ("secret failure detail" in text) is shows_errors
    assert "错误 1" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender,expected",
    [
        (Sender(ADMIN, is_admin=True), 2),
        (Sender(GLOBAL_OP), 2),
        (Sender(LOCAL_OP), 1),
        (Sender(NOBODY), 0),
    ],
)
async def test_status_pending_count_is_scoped(sender, expected):
    plugin = make_plugin()
    add_case(plugin, 1, fname=FORUM)
    add_case(plugin, 2, fname=OTHER)
    text = (await collect(plugin.tb_status(sender)))[0]
    assert f"待处理案件：{expected}" in text


# --- list -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender,expected_ids",
    [
        (Sender(ADMIN, is_admin=True), [2, 1]),
        (Sender(GLOBAL_OP), [2, 1]),
        (Sender(LOCAL_OP), [1]),
        (Sender(NOBODY), []),
    ],
)
async def test_list_shows_only_moderated_forums(sender, expected_ids):
    plugin = make_plugin()
    add_case(plugin, 1, fname=FORUM)
    add_case(plugin, 2, fname=OTHER)
    replies = await collect(plugin.tb_list(sender))
    if not expected_ids:
        assert replies == [entry.NO_PERMISSION_REPLY]
        assert plugin._store.queries == []
        return
    shown = [
        int(line.split("#")[1].split(" ")[0])
        for line in replies[0].splitlines()
        if line.startswith("#")
    ]
    assert shown == expected_ids
    assert f"待处理案件 {len(expected_ids)} 个" in replies[0]


@pytest.mark.asyncio
async def test_list_count_and_page_use_the_same_scope():
    plugin = make_plugin()
    for case_id in range(1, 6):
        add_case(plugin, case_id, fname=FORUM)
    add_case(plugin, 9, fname=OTHER)
    replies = await collect(plugin.tb_list(Sender(LOCAL_OP)))
    assert "待处理案件 5 个" in replies[0]
    assert "#9" not in replies[0]
    sent = plugin._store.queries[-1]
    assert sent["fnames"] == ("dragon",)
    assert sent["statuses"] == (CaseStatus.PENDING,)


@pytest.mark.asyncio
async def test_list_matches_forum_spelling_differences():
    plugin = make_plugin()
    add_case(plugin, 1, fname=" dragon ")
    add_case(plugin, 2, fname="DRAGON")
    add_case(plugin, 3, fname=OTHER)
    replies = await collect(plugin.tb_list(Sender(LOCAL_OP)))
    assert "待处理案件 2 个" in replies[0]


# --- show -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_allows_own_forum_and_hides_others():
    plugin = make_plugin()
    add_case(plugin, 1, fname=FORUM)
    add_case(plugin, 2, fname=OTHER)

    own = (await collect(plugin.tb_show(Sender(LOCAL_OP), "1")))[0]
    assert "【贴吧巡查】案件 #1" in own

    foreign = (await collect(plugin.tb_show(Sender(LOCAL_OP), "2")))[0]
    assert foreign == "案件 #2 不存在或不在你负责的吧。"
    assert "广告" not in foreign and "test" not in foreign

    missing = (await collect(plugin.tb_show(Sender(LOCAL_OP), "77")))[0]
    assert missing == "案件 #77 不存在或不在你负责的吧。"


@pytest.mark.asyncio
async def test_show_global_operator_keeps_the_plain_missing_reply():
    plugin = make_plugin()
    add_case(plugin, 2, fname=OTHER)
    assert "【贴吧巡查】案件 #2" in (await collect(plugin.tb_show(Sender(GLOBAL_OP), "2")))[0]
    assert (await collect(plugin.tb_show(Sender(ADMIN, is_admin=True), "7")))[0] == (
        "案件 #7 不存在。"
    )


@pytest.mark.asyncio
async def test_show_rejects_senders_without_any_scope():
    plugin = make_plugin()
    add_case(plugin, 1, fname=FORUM)
    assert await collect(plugin.tb_show(Sender(NOBODY), "1")) == [
        entry.NO_PERMISSION_REPLY
    ]


# --- del / ban / ignore / restore -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,kwargs",
    [
        ("tb_del", {}),
        ("tb_ban", {"days": "3"}),
        ("tb_ignore", {}),
        ("tb_restore", {}),
    ],
)
async def test_case_actions_stop_before_the_case_service_when_out_of_scope(
    command, kwargs
):
    plugin = make_plugin()
    add_case(plugin, 2, fname=OTHER, status=CaseStatus.PENDING)
    replies = await collect(getattr(plugin, command)(Sender(LOCAL_OP), "2", **kwargs))
    assert replies == ["案件 #2 不存在或不在你负责的吧。"]
    assert plugin._cases.calls == []
    plugin._gateway.delete.assert_not_awaited()
    plugin._gateway.block.assert_not_awaited()
    plugin._gateway.restore.assert_not_awaited()
    # The case is untouched.
    assert plugin._store.get_case(2).status is CaseStatus.PENDING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,kwargs,expected",
    [
        ("tb_del", {}, ("delete", 1)),
        ("tb_ban", {"days": "10"}, ("ban", 1)),
        ("tb_ignore", {}, ("ignore", 1)),
        ("tb_restore", {}, ("restore", 1)),
    ],
)
async def test_case_actions_allow_the_local_moderators_own_forum(
    command, kwargs, expected
):
    plugin = make_plugin()
    add_case(plugin, 1, fname=FORUM, status=CaseStatus.AUTO_DELETED)
    replies = await collect(getattr(plugin, command)(Sender(LOCAL_OP), "1", **kwargs))
    assert replies[0].startswith("案件 #1 ")
    action, case_id = expected
    assert plugin._cases.calls[0][:2] == (action, case_id)
    # The operator string is still the raw platform id, not a role label.
    assert plugin._cases.calls[0][-1] == LOCAL_OP


@pytest.mark.asyncio
async def test_case_actions_for_nobody_do_not_touch_the_store():
    plugin = make_plugin()
    add_case(plugin, 1, fname=FORUM)
    replies = await collect(plugin.tb_del(Sender(NOBODY), "1"))
    assert replies == [entry.NO_PERMISSION_REPLY]
    assert plugin._cases.calls == []


@pytest.mark.asyncio
async def test_case_actions_validate_the_id_and_days_before_scope_lookup():
    plugin = make_plugin()
    add_case(plugin, 1, fname=FORUM)
    assert "编号是正整数" in (await collect(plugin.tb_del(Sender(LOCAL_OP), "0")))[0]
    assert "封禁天数只能是" in (
        await collect(plugin.tb_ban(Sender(LOCAL_OP), "1", "9999"))
    )[0]
    assert plugin._cases.calls == []


@pytest.mark.asyncio
async def test_global_operator_can_act_on_any_forum():
    plugin = make_plugin()
    add_case(plugin, 2, fname=OTHER)
    replies = await collect(plugin.tb_del(Sender(GLOBAL_OP), "2"))
    assert replies[0].startswith("案件 #2 已删除")
    assert plugin._cases.calls[0][-1] == GLOBAL_OP


# --- check / bind -----------------------------------------------------------


@pytest.mark.asyncio
async def test_check_and_bind_stay_admin_only():
    plugin = make_plugin()
    for sender in (Sender(GLOBAL_OP), Sender(LOCAL_OP), Sender(NOBODY)):
        assert await collect(plugin.tb_check(sender, "10")) == [entry.ADMIN_ONLY_REPLY]
        assert await collect(plugin.tb_bind(sender, FORUM)) == [entry.ADMIN_ONLY_REPLY]
    plugin._gateway.get_thread_item_by_tid.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_never_creates_a_forum_that_is_not_patrolled():
    plugin = make_plugin()
    sender = Sender(ADMIN, is_admin=True)
    sender.unified_msg_origin = "offline:group:test"
    replies = await collect(plugin.tb_bind(sender, "Evil"))
    assert replies == ["未巡查 Evil 吧，不能绑定推送会话。"]
    assert plugin.config["forum_overrides"] == config()["forum_overrides"]


@pytest.mark.asyncio
async def test_bind_uses_the_configured_spelling_and_leaves_other_keys_alone():
    plugin = make_plugin()
    sender = Sender(ADMIN, is_admin=True)
    sender.unified_msg_origin = "offline:group:test"
    before = dict(plugin.config)
    replies = await collect(plugin.tb_bind(sender, "  dRaGoN  "))
    assert replies[0].startswith("已把当前会话加入Dragon 吧的推送列表")
    entries = plugin.config["forum_overrides"]
    assert entries[0]["fname"] == "Dragon"
    assert entries[0]["sessions"] == ["offline:group:test"]
    assert entries[1] == before["forum_overrides"][1]
    assert plugin.config["notify"] == before["notify"]
    assert set(plugin.config) == set(before)


@pytest.mark.asyncio
async def test_bind_creates_a_minimal_override_for_a_plain_configured_forum():
    plugin = make_plugin(
        {"forums": ["Plain"], "forum_overrides": [], "notify": {"sessions": []}}
    )
    sender = Sender(ADMIN, is_admin=True)
    sender.unified_msg_origin = "offline:group:test"
    await collect(plugin.tb_bind(sender, "plain"))
    assert plugin.config["forum_overrides"] == [
        {
            "__template_key": "forum",
            "fname": "Plain",
            "rules": "",
            "sessions": ["offline:group:test"],
            "operators": [],
        }
    ]


@pytest.mark.asyncio
async def test_bind_without_argument_updates_only_the_global_list():
    plugin = make_plugin()
    sender = Sender(ADMIN, is_admin=True)
    sender.unified_msg_origin = "offline:group:test"
    before = list(plugin.config["forum_overrides"])
    await collect(plugin.tb_bind(sender))
    assert plugin.config["notify"]["sessions"] == ["offline:group:test"]
    assert plugin.config["forum_overrides"] == before


# --- hostile bind arguments -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("arg", ["x" * 65, "x" * 20000])
async def test_bind_rejects_an_overlong_forum_argument(arg):
    plugin = make_plugin()
    sender = Sender(ADMIN, is_admin=True)
    before = list(plugin.config["forum_overrides"])
    replies = await collect(plugin.tb_bind(sender, arg))
    assert replies == ["吧名过长，不能绑定推送会话。"]
    assert len(replies[0]) < 40
    assert plugin.config["forum_overrides"] == before


@pytest.mark.asyncio
async def test_bind_refusal_does_not_echo_newlines_from_the_argument():
    plugin = make_plugin()
    sender = Sender(ADMIN, is_admin=True)
    replies = await collect(plugin.tb_bind(sender, "Evil\n生成：/tb bind Other"))
    assert "\n" not in replies[0]
    assert plugin.config["forum_overrides"] == config()["forum_overrides"]


@pytest.mark.asyncio
async def test_bind_cannot_inject_another_forum_entry_or_config_key():
    plugin = make_plugin()
    sender = Sender(ADMIN, is_admin=True)
    sender.unified_msg_origin = "offline:group:test"
    before_keys = set(plugin.config)
    before_entries = list(plugin.config["forum_overrides"])
    for arg in ("Dragon,Other", "Dragon\nOther", "Dragon\x00Other", "Dragon/../Other"):
        plugin.config["forum_overrides"] = list(before_entries)
        replies = await collect(plugin.tb_bind(sender, arg))
        assert replies[0].startswith("未巡查"), arg
        assert plugin.config["forum_overrides"] == before_entries
        assert set(plugin.config) == before_keys


@pytest.mark.asyncio
async def test_bind_rejects_a_forum_that_is_only_in_overrides_but_duplicated():
    plugin = make_plugin(
        {
            "forums": [],
            "forum_overrides": [
                {"__template_key": "forum", "fname": "Dup", "sessions": [], "operators": []},
                {"__template_key": "forum", "fname": "dup", "sessions": [], "operators": []},
            ],
            "notify": {"sessions": []},
        }
    )
    sender = Sender(ADMIN, is_admin=True)
    sender.unified_msg_origin = "offline:group:test"
    await collect(plugin.tb_bind(sender, "DUP"))
    entries = plugin.config["forum_overrides"]
    assert entries[0]["sessions"] == ["offline:group:test"]
    assert entries[1]["sessions"] == []
    assert len(entries) == 2
