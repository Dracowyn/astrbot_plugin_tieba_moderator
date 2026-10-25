"""Settings Web API: DTOs, error mapping, credential secrecy and check flow."""

import asyncio
import dataclasses as dcs
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_tieba_moderator.core.models import TiebaActionError
from astrbot_plugin_tieba_moderator.core.settings import (
    Account,
    ForumDraft,
    ForumEntry,
    ForumOptions,
    GlobalSettings,
    ResolvedForum,
    SettingsSnapshot,
)
from astrbot_plugin_tieba_moderator.core.validation import ValidationError

from astrbot.api.web import PluginRequest, bind_request_context


from astrbot_plugin_tieba_moderator.web import settings_routes as sr  # noqa: E402

SettingsConflict = sr.SettingsConflict
SettingsNotFound = sr.SettingsNotFound
SettingsRoutes = sr.SettingsRoutes
SettingsWebServices = sr.SettingsWebServices
provider_rows = sr.provider_rows

BDUSS = "test-only-bduss-value-0123456789"
STOKEN = "test-only-stoken-value-0123456789"
SECRETS = (BDUSS, STOKEN)

RULES = "1. 禁止广告"
DEFAULTS = ForumOptions(rules=RULES)


# --- request stand-in -------------------------------------------------------


class FakePayload:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeRawRequest:
    method = "POST"
    url = SimpleNamespace(path="/x")
    headers = {}
    cookies = {}
    client = SimpleNamespace(host="127.0.0.1")

    def __init__(self, payload=None):
        self.query_params = SimpleNamespace(multi_items=lambda: [])
        self._body = FakePayload(payload)

    async def json(self):
        return await self._body.json()


def call(handler, *, payload=None, username="alice", **params):
    """Run one handler with a bound request context."""
    raw = FakeRawRequest(payload)
    plugin_request = PluginRequest(raw, path_params=params, username=username)

    async def run():
        with bind_request_context(plugin_request):
            return await handler(**params)

    return asyncio.run(run())


def body(response):
    """Decode a JSON response body."""
    return json.loads(response.body)


def error(response):
    """Return the public error message."""
    return body(response)["message"]


def serialized(response):
    """Serialize a whole response exactly as the bridge would receive it."""
    return json.dumps(body(response), ensure_ascii=False, sort_keys=True)


# --- fake settings service --------------------------------------------------


def make_global(**changes):
    """Valid global settings."""
    return dcs.replace(
        GlobalSettings(
            defaults=DEFAULTS, sessions=("umo:1",), operators=frozenset({"9"})
        ),
        **changes,
    )


def make_account(account_id=1, name="main"):
    return Account(
        id=account_id,
        name=name,
        bduss=BDUSS,
        stoken=STOKEN,
        created_at=100,
        updated_at=200,
    )


def make_forum(forum_id=1, fname="Dragon", account_id=1, **changes):
    return dcs.replace(
        ForumEntry(
            id=forum_id,
            fname=fname,
            account_id=account_id,
            created_at=100,
            updated_at=200,
        ),
        **changes,
    )


class FakeSettings:
    """In-memory settings service with the contract's surface."""

    def __init__(self) -> None:
        self.snapshot_ = SettingsSnapshot(
            global_=make_global(),
            forums=(make_forum(),),
            accounts=(make_account(),),
            revision=3,
        )
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    def snapshot(self):
        return self.snapshot_

    def resolve(self, fname):
        for entry in self.snapshot_.forums:
            if entry.fname.casefold() == fname.casefold():
                return ResolvedForum(
                    fname=entry.fname,
                    enabled=entry.enabled,
                    account_id=entry.account_id,
                    options=DEFAULTS,
                    sessions=entry.sessions or self.snapshot_.global_.sessions,
                    operators=entry.operators,
                    configured=True,
                )
        return ResolvedForum(
            fname=fname,
            enabled=False,
            account_id=None,
            options=DEFAULTS,
            sessions=self.snapshot_.global_.sessions,
            operators=frozenset(),
            configured=False,
        )

    def _maybe_fail(self):
        if self.error is not None:
            raise self.error

    async def save_global(self, value):
        self.calls.append(("save_global", value))
        self._maybe_fail()
        self.snapshot_ = dcs.replace(self.snapshot_, global_=value, revision=4)
        return self.snapshot_

    async def create_forum(self, draft):
        self.calls.append(("create_forum", draft))
        self._maybe_fail()
        entry = make_forum(forum_id=2, fname=draft.fname, account_id=draft.account_id)
        self.snapshot_ = dcs.replace(
            self.snapshot_, forums=(*self.snapshot_.forums, entry), revision=4
        )
        return entry

    async def update_forum(self, forum_id, draft):
        self.calls.append(("update_forum", forum_id, draft))
        self._maybe_fail()
        entry = make_forum(forum_id=forum_id, fname=draft.fname)
        self.snapshot_ = dcs.replace(self.snapshot_, forums=(entry,), revision=4)
        return entry

    async def delete_forum(self, forum_id):
        self.calls.append(("delete_forum", forum_id))
        self._maybe_fail()
        self.snapshot_ = dcs.replace(self.snapshot_, forums=(), revision=4)

    async def create_account(self, draft):
        self.calls.append(("create_account", draft))
        self._maybe_fail()
        account = make_account(account_id=2, name=draft.name)
        self.snapshot_ = dcs.replace(
            self.snapshot_, accounts=(*self.snapshot_.accounts, account), revision=4
        )
        return account

    async def update_account(self, account_id, draft):
        self.calls.append(("update_account", account_id, draft))
        self._maybe_fail()
        return make_account(account_id=account_id, name=draft.name)

    async def delete_account(self, account_id):
        self.calls.append(("delete_account", account_id))
        self._maybe_fail()


class FakeGateway:
    """Gateway double for the account check."""

    def __init__(self, *, me=None, error=None, bawu=None, bawu_error=None):
        self.me = me or SimpleNamespace(
            user_id=7, portrait="tb.1.x", user_name="mod", nick_name="吧务"
        )
        self.error = error
        self.bawu = bawu if bawu is not None else {"Dragon": True}
        self.bawu_error = bawu_error or {}
        self.bawu_calls: list[str] = []

    async def get_self_info(self):
        if self.error is not None:
            raise self.error
        return self.me

    async def is_bawu(self, fname, me):
        self.bawu_calls.append(fname)
        if fname in self.bawu_error:
            raise self.bawu_error[fname]
        return self.bawu.get(fname)


class FakePool:
    """Pool double exposing the lease the handler must hold."""

    def __init__(self, gateway=None):
        self.gateway = gateway
        self.asked: list[int] = []
        self.leased: list[int | None] = []

    def get(self, account_id):
        self.asked.append(account_id)
        return self.gateway

    @asynccontextmanager
    async def lease(self, account_id=None):
        self.leased.append(account_id)
        yield self.gateway


_UNSET = object()


def services(**changes):
    """Build a dependency bundle with fakes."""
    settings = changes.pop("settings", _UNSET)
    if settings is _UNSET:
        settings = FakeSettings()
    base = SettingsWebServices(
        settings=settings,
        pools=changes.pop("pools", None) or FakePool(FakeGateway()),
        list_providers=changes.pop(
            "list_providers", lambda: [("prov-a", "A", True), ("prov-b", "B", None)]
        ),
        redact=changes.pop("redact", lambda exc: f"redacted: {exc}"),
        redact_trace=changes.pop("redact_trace", lambda exc: f"trace: {exc}"),
    )
    return dcs.replace(base, **changes), settings


def routes_for(bundle=None):
    bundle = bundle or services()[0]
    return SettingsRoutes(lambda: bundle), bundle


# --- notifications ----------------------------------------------------------

FORUM_DTO = {
    "fname": "Dragon",
    "enabled": True,
    "account_id": 1,
    "sessions": [],
    "operators": [],
    "whitelist": [],
    "overrides": {},
}
GLOBAL_DTO = {
    "patrol_enabled": True,
    "interval_seconds": 180,
    "max_items_per_round": 60,
    "judge_concurrency": 2,
    "judge_timeout_seconds": 60,
    "review_images": True,
    "max_images": 4,
    "sessions": [],
    "operators": [],
    "defaults": {
        "rules": RULES,
        "provider_id": "",
        "auto_delete_enabled": False,
        "auto_delete_threshold": 0.9,
        "report_threshold": 0.6,
        "thread_pages": 1,
        "lookback_hours": 6,
        "review_posts": True,
        "review_comments": True,
        "skip_top_and_good": True,
        "skip_bawu": True,
        "whitelist": [],
    },
}


def account_dto(**changes):
    payload = {"name": "main", "bduss": BDUSS, "stoken": STOKEN}
    payload.update(changes)
    return payload


# --- reads ------------------------------------------------------------------


def test_get_settings_returns_every_block():
    routes, _ = routes_for()
    payload = body(call(routes.get_settings))
    assert payload["revision"] == 3
    assert payload["global"]["interval_seconds"] == 180
    assert payload["global"]["sessions"] == ["umo:1"]
    assert payload["global"]["operators"] == ["9"]
    assert payload["global"]["defaults"]["rules"] == RULES
    forum = payload["forums"][0]
    assert forum["id"] == 1
    assert forum["fname"] == "Dragon"
    assert forum["effective"]["rules"] == RULES
    assert forum["patrolled"] is True
    assert forum["problem"] == ""
    assert payload["accounts"][0]["forums"] == ["Dragon"]


@pytest.mark.parametrize(
    "entry,problem",
    [
        (make_forum(enabled=False), "已停用"),
        (make_forum(account_id=None), "未选择账号"),
        (make_forum(enabled=False, account_id=None), "已停用"),
    ],
)
def test_forum_problem_explains_why_it_is_not_patrolled(entry, problem):
    bundle, settings = services()
    settings.snapshot_ = dcs.replace(settings.snapshot_, forums=(entry,))
    routes, _ = routes_for(bundle)
    forum = body(call(routes.get_settings))["forums"][0]
    assert forum["problem"] == problem
    assert forum["patrolled"] is False


def test_account_without_a_forum_has_an_empty_usage_list():
    routes, _ = routes_for()
    settings = routes._services().settings
    settings.snapshot_ = dcs.replace(settings.snapshot_, forums=())
    assert body(call(routes.get_settings))["accounts"][0]["forums"] == []


def test_get_settings_requires_the_service():
    bundle, _ = services(settings=None)
    routes, _ = routes_for(bundle)
    assert call(routes.get_settings).status_code == 503


def test_get_providers_lists_ids_labels_and_image_support():
    routes, _ = routes_for()
    assert body(call(routes.get_providers)) == [
        {"id": "prov-a", "label": "A", "supports_image": True},
        {"id": "prov-b", "label": "B", "supports_image": None},
    ]


def test_provider_ids_are_a_set():
    bundle, _ = services()
    assert bundle.provider_ids() == frozenset({"prov-a", "prov-b"})


def test_provider_listing_failure_yields_no_ids(caplog):
    def boom():
        raise RuntimeError("provider manager offline")

    bundle, _ = services(list_providers=boom)
    assert bundle.provider_ids() == frozenset()


def test_account_ids_come_from_the_snapshot():
    bundle, _ = services()
    assert bundle.account_ids() == frozenset({1})


# --- write success ----------------------------------------------------------


def test_post_global_saves_and_returns_the_snapshot():
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    response = call(routes.post_global, payload=GLOBAL_DTO)
    assert response.status_code == 200
    assert body(response)["revision"] == 4
    assert settings.calls[0][0] == "save_global"
    parsed = settings.calls[0][1]
    assert parsed.defaults.rules == RULES
    assert parsed.interval_seconds == 180


def test_post_forum_create_uses_the_draft():
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    response = call(routes.post_forum_create, payload=FORUM_DTO)
    assert response.status_code == 200
    name, draft = settings.calls[0]
    assert name == "create_forum"
    assert isinstance(draft, ForumDraft)
    assert draft.fname == "Dragon"
    assert draft.account_id == 1


def test_post_forum_update_uses_the_path_id():
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    response = call(routes.post_forum_update, payload=FORUM_DTO, forum_id="7")
    assert response.status_code == 200
    assert settings.calls[0][:2] == ("update_forum", 7)


def test_post_forum_delete():
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    assert call(routes.post_forum_delete, forum_id="5").status_code == 200
    assert settings.calls == [("delete_forum", 5)]


def test_post_account_create():
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    response = call(routes.post_account_create, payload=account_dto())
    assert response.status_code == 200
    name, draft = settings.calls[0]
    assert name == "create_account"
    assert draft.name == "main"
    assert draft.bduss == BDUSS


def test_post_account_update_allows_a_name_only_payload():
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    response = call(
        routes.post_account_update, payload={"name": "renamed"}, account_id="1"
    )
    assert response.status_code == 200
    assert settings.calls[0][:2] == ("update_account", 1)
    assert settings.calls[0][2].bduss is None


def test_post_account_delete():
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    assert call(routes.post_account_delete, account_id="4").status_code == 200
    assert settings.calls == [("delete_account", 4)]


# --- path validation --------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "-1", "x", "1.0", "1" * 19, "", "  "])
@pytest.mark.parametrize(
    "handler_name",
    [
        "post_forum_update",
        "post_forum_delete",
        "post_account_update",
        "post_account_delete",
        "post_account_check",
    ],
)
def test_path_ids_are_validated(handler_name, raw):
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    params = {"forum_id": raw} if "forum" in handler_name else {"account_id": raw}
    response = call(getattr(routes, handler_name), payload=FORUM_DTO, **params)
    assert response.status_code == 400
    assert "正整数" in error(response)
    assert settings.calls == []


# --- validation mapping -----------------------------------------------------


@pytest.mark.parametrize(
    "payload,match",
    [
        (None, "JSON 对象"),
        ([], "JSON 对象"),
        ({}, "defaults"),
        ({**GLOBAL_DTO, "interval_seconds": "180"}, "interval_seconds"),
        ({**GLOBAL_DTO, "extra": 1}, "未知字段"),
    ],
)
def test_post_global_reports_validation_errors(payload, match):
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    response = call(routes.post_global, payload=payload)
    assert response.status_code == 400
    assert match in error(response)
    assert settings.calls == []


@pytest.mark.parametrize(
    "payload,match",
    [
        (None, "JSON 对象"),
        ({}, "fname"),
        ({**FORUM_DTO, "fname": "x" * 65}, "fname"),
        ({**FORUM_DTO, "account_id": 99}, "account_id"),
        ({**FORUM_DTO, "overrides": {"provider_id": "missing"}}, "provider_id"),
    ],
)
def test_post_forum_reports_validation_errors(payload, match):
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    response = call(routes.post_forum_create, payload=payload)
    assert response.status_code == 400
    assert match in error(response)
    assert settings.calls == []


def test_account_create_requires_credentials():
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    response = call(routes.post_account_create, payload={"name": "a"})
    assert response.status_code == 400
    assert "bduss" in error(response)
    assert settings.calls == []


# --- service error mapping --------------------------------------------------


@pytest.mark.parametrize(
    "failure,status,match",
    [
        (SettingsNotFound("账号 9 不存在"), 404, "不存在"),
        (SettingsConflict("已有同名账号"), 409, "同名"),
        (TiebaActionError("code=4, errmsg=denied"), 502, "redacted"),
        (RuntimeError("kaboom"), 500, sr.INTERNAL_ERROR),
        (ValueError("bad"), 500, sr.INTERNAL_ERROR),
    ],
)
def test_service_errors_are_mapped(failure, status, match):
    bundle, settings = services()
    settings.error = failure
    routes, _ = routes_for(bundle)
    response = call(routes.post_global, payload=GLOBAL_DTO)
    assert response.status_code == status
    assert match in error(response)


def test_internal_error_hides_the_exception_text():
    bundle, settings = services()
    settings.error = RuntimeError("secret internal path /srv/astrbot/data")
    routes, _ = routes_for(bundle)
    response = call(routes.post_global, payload=GLOBAL_DTO)
    assert response.status_code == 500
    text = serialized(response)
    assert "secret internal path" not in text
    assert "/srv/" not in text


def test_writes_require_the_service():
    bundle, _ = services(settings=None)
    routes, _ = routes_for(bundle)
    cases = (
        (routes.post_global, GLOBAL_DTO, {}),
        (routes.post_forum_create, FORUM_DTO, {}),
        (routes.post_forum_update, FORUM_DTO, {"forum_id": "1"}),
        (routes.post_forum_delete, None, {"forum_id": "1"}),
        (routes.post_account_create, account_dto(), {}),
        (routes.post_account_update, account_dto(), {"account_id": "1"}),
        (routes.post_account_delete, None, {"account_id": "1"}),
    )
    for handler, payload, params in cases:
        response = call(handler, payload=payload, **params)
        assert response.status_code == 503, handler.__name__


# --- credential secrecy -----------------------------------------------------


def test_account_dto_has_no_credential_fields():
    routes, _ = routes_for()
    account = body(call(routes.get_settings))["accounts"][0]
    assert set(account) == {
        "id",
        "name",
        "has_credentials",
        "forums",
        "created_at",
        "updated_at",
    }
    assert account["has_credentials"] is True


def test_no_response_contains_a_credential():
    bundle, settings = services()
    routes, _ = routes_for(bundle)
    responses = [
        call(routes.get_settings),
        call(routes.get_providers),
        call(routes.post_global, payload=GLOBAL_DTO),
        call(routes.post_forum_create, payload=FORUM_DTO),
        call(routes.post_forum_update, payload=FORUM_DTO, forum_id="1"),
        call(routes.post_forum_delete, forum_id="1"),
        call(routes.post_account_create, payload=account_dto()),
        call(routes.post_account_update, payload=account_dto(), account_id="1"),
        call(routes.post_account_delete, account_id="1"),
        call(routes.post_account_check, account_id="1"),
        # Error paths too: a rejected payload echoes the offending field name only.
        call(routes.post_account_create, payload=account_dto(bduss=BDUSS + " ")),
        call(routes.post_account_create, payload={"name": "a"}),
        call(
            routes.post_account_update,
            payload={"name": "a", "stoken": STOKEN * 9},
            account_id="1",
        ),
    ]
    for response in responses:
        text = serialized(response)
        for secret in SECRETS:
            assert secret not in text, secret
            assert secret[:8] not in text, secret[:8]


def test_validation_error_for_a_bad_credential_names_only_the_field():
    routes, _ = routes_for()
    response = call(routes.post_account_create, payload=account_dto(bduss="has space"))
    assert response.status_code == 400
    assert "bduss" in error(response)
    assert "has space" not in error(response)


def test_account_check_response_has_no_credential():
    routes, _ = routes_for()
    response = call(routes.post_account_check, account_id="1")
    text = serialized(response)
    for secret in SECRETS:
        assert secret not in text


# --- account check ----------------------------------------------------------


def test_check_reports_self_info_and_moderator_status():
    gateway = FakeGateway(bawu={"Dragon": True})
    bundle, _ = services(pools=FakePool(gateway))
    routes, _ = routes_for(bundle)
    payload = body(call(routes.post_account_check, account_id="1"))
    assert payload == {
        "ok": True,
        "user_name": "mod",
        "nick_name": "吧务",
        "message": "凭据有效。",
        "forums": [{"fname": "Dragon", "is_bawu": True}],
    }
    assert gateway.bawu_calls == ["Dragon"]


def test_check_reports_forums_where_the_account_is_not_a_moderator():
    gateway = FakeGateway(bawu={"Dragon": False})
    bundle, _ = services(pools=FakePool(gateway))
    routes, _ = routes_for(bundle)
    payload = body(call(routes.post_account_check, account_id="1"))
    assert payload["ok"] is False
    assert payload["message"] == "该账号不是以下吧的吧务：Dragon"
    assert payload["forums"] == [{"fname": "Dragon", "is_bawu": False}]


def test_check_reports_unknown_forum_status_as_null():
    gateway = FakeGateway(bawu={}, bawu_error={"Dragon": RuntimeError("timeout")})
    bundle, _ = services(pools=FakePool(gateway))
    routes, _ = routes_for(bundle)
    payload = body(call(routes.post_account_check, account_id="1"))
    assert payload["forums"] == [{"fname": "Dragon", "is_bawu": None}]
    assert payload["ok"] is False
    assert "查询失败" in payload["message"]


def test_check_one_failing_forum_does_not_hide_the_others():
    settings = FakeSettings()
    settings.snapshot_ = dcs.replace(
        settings.snapshot_,
        forums=(make_forum(1, "A"), make_forum(2, "B"), make_forum(3, "C")),
    )
    gateway = FakeGateway(
        bawu={"A": True, "B": True}, bawu_error={"B": RuntimeError("x")}
    )
    bundle, _ = services(settings=settings, pools=FakePool(gateway))
    routes, _ = routes_for(bundle)
    payload = body(call(routes.post_account_check, account_id="1"))
    assert gateway.bawu_calls == ["A", "B", "C"]
    assert payload["forums"] == [
        {"fname": "A", "is_bawu": True},
        {"fname": "B", "is_bawu": None},
        {"fname": "C", "is_bawu": None},
    ]


def test_check_only_queries_forums_using_that_account():
    settings = FakeSettings()
    settings.snapshot_ = dcs.replace(
        settings.snapshot_,
        forums=(make_forum(1, "A", account_id=1), make_forum(2, "B", account_id=2)),
    )
    gateway = FakeGateway(bawu={"A": True})
    bundle, _ = services(settings=settings, pools=FakePool(gateway))
    routes, _ = routes_for(bundle)
    body(call(routes.post_account_check, account_id="1"))
    assert gateway.bawu_calls == ["A"]


def test_check_without_a_forum_is_still_ok():
    settings = FakeSettings()
    settings.snapshot_ = dcs.replace(settings.snapshot_, forums=())
    bundle, _ = services(settings=settings)
    routes, _ = routes_for(bundle)
    payload = body(call(routes.post_account_check, account_id="1"))
    assert payload["ok"] is True
    assert payload["forums"] == []


def test_check_requires_a_gateway():
    bundle, _ = services(pools=FakePool(None))
    routes, _ = routes_for(bundle)
    assert call(routes.post_account_check, account_id="1").status_code == 503


def test_check_reports_invalid_credentials_as_502_and_redacts():
    gateway = FakeGateway(error=TiebaActionError("code=1, errmsg=invalid cookie"))
    bundle, _ = services(pools=FakePool(gateway))
    routes, _ = routes_for(bundle)
    response = call(routes.post_account_check, account_id="1")
    assert response.status_code == 502
    assert error(response) == "redacted: code=1, errmsg=invalid cookie"


def test_check_hides_unexpected_failures():
    gateway = FakeGateway(error=RuntimeError("secret internals"))
    bundle, _ = services(pools=FakePool(gateway))
    routes, _ = routes_for(bundle)
    response = call(routes.post_account_check, account_id="1")
    assert response.status_code == 500
    text = serialized(response)
    assert "secret internals" not in text


def test_check_is_mutually_exclusive():
    bundle, _ = services()
    routes, _ = routes_for(bundle)
    routes._check_lock = asyncio.Lock()

    async def scenario():
        async with routes._check_lock:
            raw = FakeRawRequest()
            plugin_request = PluginRequest(raw, path_params={}, username="alice")
            with bind_request_context(plugin_request):
                return await routes.post_account_check(account_id="1")

    response = asyncio.run(scenario())
    assert response.status_code == 409
    assert "检测" in error(response)


def test_check_requires_the_service():
    bundle, _ = services(settings=None)
    routes, _ = routes_for(bundle)
    assert call(routes.post_account_check, account_id="1").status_code == 503


# --- provider_rows ----------------------------------------------------------


class FakeProvider:
    def __init__(self, provider_id, model, config=None, meta_error=None):
        self._meta = SimpleNamespace(id=provider_id, model=model)
        self.provider_config = config
        self._meta_error = meta_error

    def meta(self):
        if self._meta_error is not None:
            raise self._meta_error
        return self._meta


def test_provider_rows_reads_meta_and_modalities():
    context = SimpleNamespace(
        get_all_providers=lambda: [
            FakeProvider("a", "gpt-4o", {"modalities": ["text", "image"]}),
            FakeProvider("b", "text-only", {"modalities": ["text"]}),
            FakeProvider("c", "no-modalities", {}),
            FakeProvider("d", "empty-list", {"modalities": []}),
            FakeProvider("e", "", {"modalities": "image"}),
        ]
    )
    assert provider_rows(context) == [
        ("a", "a（gpt-4o）", True),
        ("b", "b（text-only）", False),
        ("c", "c（no-modalities）", None),
        ("d", "d（empty-list）", True),
        ("e", "e", None),
    ]


def test_provider_rows_skips_broken_providers():
    context = SimpleNamespace(
        get_all_providers=lambda: [
            FakeProvider("", "m"),
            FakeProvider("bad", "m", meta_error=RuntimeError("no type")),
            FakeProvider("good", "m", {"modalities": ["image"]}),
        ]
    )
    assert provider_rows(context) == [("good", "good（m）", True)]


def test_provider_rows_survives_a_broken_context():
    context = SimpleNamespace(
        get_all_providers=lambda: (_ for _ in ()).throw(RuntimeError("offline"))
    )
    assert provider_rows(context) == []


def test_provider_rows_ignores_non_dict_config():
    context = SimpleNamespace(
        get_all_providers=lambda: [FakeProvider("a", "m", config=["modalities"])]
    )
    assert provider_rows(context) == [("a", "a（m）", None)]


def test_service_level_validation_error_is_400():
    bundle, settings = services()
    settings.error = ValidationError("吧名与已有吧冲突")
    routes, _ = routes_for(bundle)
    response = call(routes.post_forum_create, payload=FORUM_DTO)
    assert response.status_code == 400
    assert error(response) == "吧名与已有吧冲突"
