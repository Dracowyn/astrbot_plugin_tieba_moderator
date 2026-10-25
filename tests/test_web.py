"""Web API contract: parameter validation, DTOs, error mapping and wiring."""

import asyncio
import dataclasses as dcs
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot_plugin_tieba_moderator import main as entry
from astrbot_plugin_tieba_moderator.core.judge import FAILED_CATEGORY
from astrbot_plugin_tieba_moderator.core.models import (
    Author,
    Case,
    CaseStatus,
    ImageFetchError,
    ItemKind,
    JudgeError,
    PatrolReport,
    TiebaActionError,
    Verdict,
)
from astrbot_plugin_tieba_moderator.services.case_service import CaseError
from astrbot_plugin_tieba_moderator.services.patrol import PatrolSettings
from astrbot_plugin_tieba_moderator.web import routes as web

CASE_STATUS_NAMES = [status.value for status in CaseStatus]
from astrbot_plugin_tieba_moderator.web.routes import WebRoutes, WebServices

from astrbot_plugin_tieba_moderator.tests.fakes import FakeConfig, FakeGateway, make_item


# --- request stand-in -------------------------------------------------------


class FakePayload:
    """Minimal starlette-like request body source."""

    def __init__(self, payload) -> None:
        self._payload = payload

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    host = "127.0.0.1"


class FakeUrl:
    path = "/api/v1/plugins/extensions/astrbot_plugin_tieba_moderator/status"


class FakeRawRequest:
    """The pieces ``astrbot.api.web.PluginRequest`` reads."""

    def __init__(self, *, method="GET", query=(), payload=None, username=None):
        self.method = method
        self.url = FakeUrl()
        self.headers = {}
        self.cookies = {}
        self.client = FakeClient()
        self.query_params = SimpleNamespace(multi_items=lambda: list(query))
        self._body = FakePayload(payload)

    async def json(self):
        return await self._body.json()


def call(handler, *, method="GET", query=(), payload=None, username="alice", **params):
    """Run one handler with a bound request context.

    Args:
        handler: Handler coroutine.
        method: HTTP method.
        query: ``(key, value)`` pairs.
        payload: JSON body value, or an exception to raise while parsing.
        username: Dashboard user name.
        **params: Path parameters.

    Returns:
        The handler's return value.
    """
    raw = FakeRawRequest(method=method, query=query, payload=payload)
    plugin_request = PluginRequest(raw, path_params=params, username=username)

    async def run():
        with bind_request_context(plugin_request):
            return await handler(**params)

    return asyncio.run(run())


def body(response) -> dict:
    """Decode a JSON response body."""
    return json.loads(response.body)


def error(response) -> str:
    """Return the public error message of an error response."""
    return body(response)["message"]


# --- fake store -------------------------------------------------------------


class FakeStore:
    """In-memory stand-in for the store methods this backend uses."""

    def __init__(self) -> None:
        self.cases: dict[int, Case] = {}
        self.reports: list[PatrolReport] = []
        self.stats_rows: list[tuple[str, CaseStatus, float]] = []
        self.save_calls: list[PatrolReport] = []
        self.fail_save = False
        self.query_calls: list[dict] = []

    # Reads the route layer uses.
    def get_case(self, case_id):
        return self.cases.get(case_id)

    def count_cases(self, status):
        return sum(1 for case in self.cases.values() if case.status is status)

    def list_reports(self, limit):
        return self.reports[:limit]

    def query_cases(self, **kwargs):
        self.query_calls.append(kwargs)
        statuses = kwargs.get("statuses") or ()
        items = [
            case
            for case in sorted(self.cases.values(), key=lambda c: -c.id)
            if not statuses or case.status in statuses
        ]
        offset = kwargs.get("offset", 0)
        limit = kwargs.get("limit", 20)
        return len(items), items[offset : offset + limit]

    def case_stats(self, since):
        self.stats_since = since
        return list(self.stats_rows)

    def save_report(self, report):
        if self.fail_save:
            raise RuntimeError("disk full")
        self.save_calls.append(report)
        self.reports.insert(0, report)

    def transition(self, case_id, *, from_, to, by, now, note=""):
        case = self.cases.get(case_id)
        if case is None or case.status not in from_:
            return None
        updated = dcs.replace(case, status=to, handled_by=by, handled_at=now, note=note)
        self.cases[case_id] = updated
        return updated


def make_case(case_id=1, *, status=CaseStatus.PENDING, verdict=None, **changes):
    """Build a stored case."""
    return dcs.replace(
        Case(
            id=case_id,
            item=make_item(),
            verdict=verdict or Verdict(violation=True, category="广告", confidence=0.8),
            status=status,
            created_at=2_000_000_000,
        ),
        **changes,
    )


class FakeCaseService:
    """Records moderator actions instead of calling Tieba."""

    def __init__(self, store) -> None:
        self.store = store
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    async def _act(self, name, case_id, operator, days=0):
        self.calls.append((name, case_id, days, operator))
        if self.error is not None:
            raise self.error
        target = {
            "delete": CaseStatus.DELETED,
            "ban": CaseStatus.BANNED,
            "ignore": CaseStatus.IGNORED,
            "restore": CaseStatus.RESTORED,
        }[name]
        case = self.store.get_case(case_id)
        updated = dcs.replace(case, status=target, handled_by=operator, handled_at=9)
        self.store.cases[case_id] = updated
        return updated

    async def delete(self, case_id, operator):
        return await self._act("delete", case_id, operator)

    async def ban(self, case_id, days, operator):
        return await self._act("ban", case_id, operator, days)

    async def ignore(self, case_id, operator):
        return await self._act("ignore", case_id, operator)

    async def restore(self, case_id, operator):
        return await self._act("restore", case_id, operator)


class FakeJudge:
    """Judge whose verdict and temporary rules are inspectable."""

    def __init__(self, verdict=None, error=None) -> None:
        self.verdict = verdict or Verdict(violation=False)
        self.error = error
        self.calls: list[tuple] = []

    async def __call__(self, item, urls):
        self.calls.append((item, urls, None))
        if self.error is not None:
            raise self.error
        return self.verdict

    async def judge_with_rules(self, item, urls, rules):
        self.calls.append((item, urls, rules))
        if self.error is not None:
            raise self.error
        return self.verdict


SETTINGS = PatrolSettings(
    forums=("test",),
    auto_delete_enabled=False,
    auto_delete_threshold=0.9,
    report_threshold=0.6,
)


def services(**changes):
    """Build a WebServices bundle with an in-memory store."""
    store = FakeStore()
    base = WebServices(
        store=store,
        patrol=AsyncMock(),
        gateway=FakeGateway(),
        cases=FakeCaseService(store),
        judge=FakeJudge(),
        settings=SETTINGS,
        interval_seconds=180,
        patrol_enabled=True,
        loop_running=True,
        manual_running=False,
        start_patrol=lambda: True,
        redact=lambda exc: f"redacted: {exc}",
        redact_trace=lambda exc: f"trace: {exc}",
    )
    return dcs.replace(base, **changes)


def routes_for(bundle=None, *, clock=lambda: 2_000_000_000):
    """Build handlers over a fixed service bundle."""
    bundle = bundle or services()
    return WebRoutes(lambda: bundle, clock=clock), bundle


# --- status -----------------------------------------------------------------


def test_status_reports_state_and_newest_report():
    bundle = services(manual_running=True)
    bundle.store.cases[1] = make_case(1)
    bundle.store.reports = [
        PatrolReport(started_at=5, finished_at=9, forums=("test",), reviewed=3)
    ]
    routes, _ = routes_for(bundle)
    payload = body(call(routes.get_status))
    assert payload == {
        "has_account": True,
        "patrol_enabled": True,
        "loop_running": True,
        "manual_running": True,
        "interval_seconds": 180,
        "forums": ["test"],
        "auto_delete": {"enabled": False, "threshold": 0.9},
        "report_threshold": 0.6,
        "pending": 1,
        "last_report": {
            "started_at": 5,
            "finished_at": 9,
            "forums": ["test"],
            "reviewed": 3,
            "reported": 0,
            "auto_deleted": 0,
            "skipped": 0,
            "deferred": 0,
            "errors": [],
        },
    }


def test_status_without_store_or_report():
    routes, _ = routes_for(services(store=None, gateway=None))
    assert call(routes.get_status).status_code == 503
    routes, _ = routes_for()
    assert body(call(routes.get_status))["last_report"] is None


# --- cases ------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        pytest.param([("status", "pending,deleted")], id="comma-separated"),
        pytest.param([("status", "pending"), ("status", "deleted")], id="repeated"),
        pytest.param([("status[]", "pending"), ("status[]", "deleted")], id="brackets"),
        pytest.param(
            [("status", "pending"), ("status[]", "deleted")], id="mixed-forms"
        ),
        pytest.param(
            [("status", " pending , ,deleted ")], id="whitespace-and-empty-part"
        ),
        pytest.param(
            [("status", "pending,deleted"), ("status", "deleted")], id="duplicates"
        ),
    ],
)
def test_cases_accepts_every_status_encoding(query):
    bundle = services()
    routes, _ = routes_for(bundle)
    response = call(routes.get_cases, query=query)
    assert response.status_code == 200
    assert bundle.store.query_calls[0]["statuses"] == (
        CaseStatus.PENDING,
        CaseStatus.DELETED,
    )


def test_cases_status_rejects_unknown_value_in_each_form():
    routes, _ = routes_for()
    for query in (
        [("status", "pending,bogus")],
        [("status[]", "bogus")],
        [("status", "pending"), ("status[]", "BOGUS")],
    ):
        response = call(routes.get_cases, query=query)
        assert response.status_code == 400
        assert "未知的案件状态" in error(response)


def test_cases_status_allows_every_value_together():
    bundle = services()
    routes, _ = routes_for(bundle)
    every = ",".join(CASE_STATUS_NAMES)
    assert call(routes.get_cases, query=[("status", every)]).status_code == 200
    assert len(bundle.store.query_calls[0]["statuses"]) == len(CASE_STATUS_NAMES)


def test_cases_status_detail_is_not_echoed_at_length():
    routes, _ = routes_for()
    response = call(routes.get_cases, query=[("status", "x" * 5000)])
    assert response.status_code == 400
    assert len(error(response)) < 80


def test_cases_listing_passes_filters_and_pages():
    bundle = services()
    for index in range(1, 6):
        bundle.store.cases[index] = make_case(index)
    routes, _ = routes_for(bundle)
    payload = body(
        call(
            routes.get_cases,
            query=[
                ("status", "pending"),
                ("status", "ignored"),
                ("kind", "thread"),
                ("fname", "test"),
                ("category", "广告"),
                ("q", "正文"),
                ("limit", "2"),
                ("offset", "1"),
            ],
        )
    )
    assert payload["total"] == 5
    assert [case["id"] for case in payload["items"]] == [4, 3]
    sent = bundle.store.query_calls[0]
    assert sent["statuses"] == (CaseStatus.PENDING, CaseStatus.IGNORED)
    assert sent["kind"] is ItemKind.THREAD
    assert (sent["fname"], sent["category"], sent["q"]) == ("test", "广告", "正文")
    assert (sent["offset"], sent["limit"]) == (1, 2)


def test_cases_dto_shape():
    bundle = services()
    bundle.store.cases[1] = make_case(1, note="备注")
    routes, _ = routes_for(bundle)
    case = body(call(routes.get_cases))["items"][0]
    assert case["review_failed"] is False
    assert case["item"] == {
        "kind": "thread",
        "fname": "test",
        "tid": 10,
        "pid": 100,
        "floor": 1,
        "title": "Topic",
        "text": "Body",
        "link": "https://tieba.baidu.com/p/10",
        "image_count": 0,
        "has_video": False,
        "create_time": 2_000_000_000,
        "author": {
            "nick_name": "",
            "user_name": "reader",
            "portrait": "fake_portrait",
            "level": 0,
            "is_bawu": False,
        },
    }
    assert case["verdict"] == {
        "violation": True,
        "category": "广告",
        "confidence": 0.8,
        "reason": "",
        "ban_days": 0,
    }
    assert "image_urls" not in case["item"]


@pytest.mark.parametrize(
    "query,match",
    [
        ([("status", "bogus")], "未知的案件状态"),
        ([("kind", "sticker")], "kind"),
        ([("fname", "x" * 65)], "fname"),
        ([("category", "x" * 65)], "category"),
        ([("q", "x" * 51)], "关键词"),
        ([("limit", "abc")], "limit"),
        ([("limit", "1.5")], "limit"),
        ([("offset", "-")], "offset"),
    ],
)
def test_cases_rejects_bad_parameters(query, match):
    routes, _ = routes_for()
    response = call(routes.get_cases, query=query)
    assert response.status_code == 400
    assert match in error(response)


def test_cases_clamps_limit_and_offset():
    bundle = services()
    routes, _ = routes_for(bundle)
    call(routes.get_cases, query=[("limit", "1000"), ("offset", "99999999")])
    assert bundle.store.query_calls[0]["limit"] == web.MAX_LIMIT
    assert bundle.store.query_calls[0]["offset"] == web.MAX_OFFSET
    call(routes.get_cases, query=[("limit", "0")])
    assert bundle.store.query_calls[1]["limit"] == 1
    call(routes.get_cases)
    assert bundle.store.query_calls[2]["limit"] == web.DEFAULT_LIMIT
    assert bundle.store.query_calls[2]["offset"] == 0


def test_cases_without_store_is_503():
    routes, _ = routes_for(services(store=None))
    assert call(routes.get_cases).status_code == 503


def test_case_detail_and_missing_case():
    bundle = services()
    bundle.store.cases[7] = make_case(7)
    routes, _ = routes_for(bundle)
    assert body(call(routes.get_case, case_id="7"))["id"] == 7
    response = call(routes.get_case, case_id="8")
    assert response.status_code == 404 and "#8 不存在" in error(response)


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "1.0", "1" * 19, "", "  "])
def test_case_detail_rejects_bad_ids(raw):
    routes, _ = routes_for()
    assert call(routes.get_case, case_id=raw).status_code == 400


def test_case_detail_without_store_is_503():
    routes, _ = routes_for(services(store=None))
    assert call(routes.get_case, case_id="1").status_code == 503


# --- images -----------------------------------------------------------------


def titled_case(**changes):
    """A case whose item has three images."""
    return dcs.replace(
        make_case(1, **changes),
        item=make_item(image_urls=("a", "b", "c")),
    )


def test_image_returns_data_url_and_caches():
    bundle = services()
    bundle.store.cases[1] = titled_case()
    routes, _ = routes_for(bundle)
    first = body(call(routes.get_case_image, case_id="1", index="0"))
    second = body(call(routes.get_case_image, case_id="1", index="0"))
    assert first == {"data_url": "data:image/png;base64,ZmFrZQ=="}
    assert second == first
    bundle.gateway.fetch_images.assert_awaited_once_with(["a"], 1)


def test_image_cache_is_bounded_and_evicts_oldest():
    bundle = services()
    for case_id in range(1, web.IMAGE_CACHE_SIZE + 2):
        bundle.store.cases[case_id] = dcs.replace(
            make_case(case_id),
            item=make_item(image_urls=("u",)),
        )
    routes, _ = routes_for(bundle)
    for case_id in range(1, web.IMAGE_CACHE_SIZE + 2):
        assert call(routes.get_case_image, case_id=str(case_id), index="0").status_code == 200
    assert len(routes._image_cache) == web.IMAGE_CACHE_SIZE
    # The oldest key was evicted and must be fetched again.
    calls_before = bundle.gateway.fetch_images.await_count
    call(routes.get_case_image, case_id="1", index="0")
    assert bundle.gateway.fetch_images.await_count == calls_before + 1
    # The newest key is still cached.
    call(routes.get_case_image, case_id=str(web.IMAGE_CACHE_SIZE + 1), index="0")
    assert bundle.gateway.fetch_images.await_count == calls_before + 1


@pytest.mark.parametrize("index", ["3", "9", "-1", "x", "1" * 19])
def test_image_index_validation(index):
    bundle = services()
    bundle.store.cases[1] = titled_case()
    routes, _ = routes_for(bundle)
    response = call(routes.get_case_image, case_id="1", index=index)
    assert response.status_code == 404
    bundle.gateway.fetch_images.assert_not_awaited()


@pytest.mark.parametrize("case_id", ["0", "abc", "9"])
def test_image_requires_an_existing_case(case_id):
    bundle = services()
    bundle.store.cases[1] = titled_case()
    routes, _ = routes_for(bundle)
    response = call(routes.get_case_image, case_id=case_id, index="0")
    assert response.status_code in (400, 404)
    bundle.gateway.fetch_images.assert_not_awaited()


def test_image_requires_credentials():
    bundle = services(gateway=None)
    bundle.store.cases[1] = titled_case()
    routes, _ = routes_for(bundle)
    assert call(routes.get_case_image, case_id="1", index="0").status_code == 503


def test_image_failures_are_mapped_and_redacted():
    bundle = services()
    bundle.store.cases[1] = titled_case()
    routes, _ = routes_for(bundle)
    bundle.gateway.fetch_images.side_effect = ImageFetchError("cdn said no")
    response = call(routes.get_case_image, case_id="1", index="0")
    assert response.status_code == 502
    assert error(response) == "redacted: cdn said no"

    bundle.gateway.fetch_images.side_effect = RuntimeError("boom")
    response = call(routes.get_case_image, case_id="1", index="0")
    assert response.status_code == 500
    assert error(response) == web.INTERNAL_ERROR

    bundle.gateway.fetch_images.side_effect = None
    bundle.gateway.fetch_images.return_value = []
    assert call(routes.get_case_image, case_id="1", index="0").status_code == 502


def test_image_without_store_is_503():
    routes, _ = routes_for(services(store=None))
    assert call(routes.get_case_image, case_id="1", index="0").status_code == 503


# --- actions ----------------------------------------------------------------


@pytest.mark.parametrize(
    "action,target",
    [
        ("post_delete", CaseStatus.DELETED),
        ("post_ignore", CaseStatus.IGNORED),
        ("post_restore", CaseStatus.RESTORED),
    ],
)
def test_actions_transition_and_record_the_dashboard_user(action, target):
    bundle = services()
    bundle.store.cases[1] = make_case(1)
    routes, _ = routes_for(bundle)
    payload = body(call(getattr(routes, action), case_id="1"))
    assert payload["status"] == target.value
    assert payload["handled_by"] == "webui:alice"
    assert bundle.cases.calls[0][3] == "webui:alice"


@pytest.mark.parametrize("days", [1, 3, 10])
def test_ban_accepts_only_allowed_integers(days):
    bundle = services()
    bundle.store.cases[1] = make_case(1)
    routes, _ = routes_for(bundle)
    payload = body(
        call(routes.post_ban, method="POST", payload={"days": days}, case_id="1")
    )
    assert payload["status"] == "banned"
    assert bundle.cases.calls[0][:3] == ("ban", 1, days)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"days": "3"},
        {"days": True},
        {"days": 3.0},
        {"days": 2},
        {"days": 0},
        {"days": None},
    ],
)
def test_ban_rejects_bad_payloads(payload):
    bundle = services()
    bundle.store.cases[1] = make_case(1)
    routes, _ = routes_for(bundle)
    response = call(routes.post_ban, method="POST", payload=payload, case_id="1")
    assert response.status_code == 400
    assert "封禁天数" in error(response) or "JSON 对象" in error(response)
    assert bundle.cases.calls == []


def test_ban_rejects_unparsable_body():
    bundle = services()
    bundle.store.cases[1] = make_case(1)
    routes, _ = routes_for(bundle)
    response = call(
        routes.post_ban, method="POST", payload=ValueError("bad json"), case_id="1"
    )
    assert response.status_code == 400


@pytest.mark.parametrize("action", ["post_delete", "post_ban", "post_ignore", "post_restore"])
def test_actions_reject_bad_ids(action):
    routes, _ = routes_for()
    kwargs = {"payload": {"days": 3}} if action == "post_ban" else {}
    response = call(getattr(routes, action), method="POST", case_id="0", **kwargs)
    assert response.status_code == 400


@pytest.mark.parametrize("action", ["post_delete", "post_ignore", "post_restore"])
def test_actions_require_existing_case_and_credentials(action):
    bundle = services()
    routes, _ = routes_for(bundle)
    response = call(getattr(routes, action), method="POST", case_id="5")
    assert response.status_code == 404

    bundle = services(gateway=None, cases=None)
    bundle.store.cases[1] = make_case(1)
    routes, _ = routes_for(bundle)
    assert call(getattr(routes, action), method="POST", case_id="1").status_code == 503


def test_actions_without_store_is_503():
    routes, _ = routes_for(services(store=None, cases=None))
    assert call(routes.post_delete, method="POST", case_id="1").status_code == 503


@pytest.mark.parametrize(
    "failure,status,match",
    [
        (CaseError("案件 1 当前状态不允许此操作。"), 400, "不允许此操作"),
        (TiebaActionError("code=4, errmsg=denied"), 502, "redacted"),
        (JudgeError("模型超时"), 502, "redacted"),
        (ImageFetchError("图挂了"), 502, "redacted"),
        (RuntimeError("kaboom"), 500, web.INTERNAL_ERROR),
    ],
)
def test_action_errors_are_mapped(failure, status, match):
    bundle = services()
    bundle.store.cases[1] = make_case(1)
    bundle.cases.error = failure
    routes, _ = routes_for(bundle)
    response = call(routes.post_delete, method="POST", case_id="1")
    assert response.status_code == status
    assert match in error(response)


def test_operator_label_is_sanitized():
    bundle = services()
    bundle.store.cases[1] = make_case(1)
    routes, _ = routes_for(bundle)
    body(call(routes.post_ignore, method="POST", case_id="1", username="a\nb\u202e" + "x" * 90))
    operator = bundle.cases.calls[0][3]
    assert operator.startswith("webui:a b")
    assert len(operator) <= len("webui:") + web.OPERATOR_CHARS
    assert "\n" not in operator and "\u202e" not in operator

    body(call(routes.post_ignore, method="POST", case_id="1", username=None))
    assert bundle.cases.calls[1][3] == "webui:unknown"


# --- statistics -------------------------------------------------------------


def test_stats_aggregates_and_excludes_review_failed():
    bundle = services()
    bundle.store.stats_rows = [
        ("广告", CaseStatus.DELETED, 0.95),
        ("广告", CaseStatus.IGNORED, 0.65),
        ("广告", CaseStatus.BANNED, 0.85),
        ("辱骂", CaseStatus.RESTORED, 0.75),
        (FAILED_CATEGORY, CaseStatus.PENDING, 0.0),
    ]
    routes, _ = routes_for(bundle, clock=lambda: 2_000_000_000)
    payload = body(call(routes.get_stats, query=[("days", "7")]))
    assert payload["since"] == 2_000_000_000 - 7 * 86400
    assert bundle.store.stats_since == payload["since"]
    assert payload["total"] == 5
    assert payload["review_failed"] == 1
    assert payload["by_status"] == {
        "pending": 1,
        "auto_deleted": 0,
        "deleted": 1,
        "banned": 1,
        "ignored": 1,
        "restored": 1,
    }
    assert payload["by_category"] == [
        {
            "category": "广告",
            "total": 3,
            "pending": 0,
            "auto_deleted": 0,
            "deleted": 1,
            "banned": 1,
            "ignored": 1,
            "restored": 0,
        },
        {
            "category": "辱骂",
            "total": 1,
            "pending": 0,
            "auto_deleted": 0,
            "deleted": 0,
            "banned": 0,
            "ignored": 0,
            "restored": 1,
        },
    ]
    buckets = payload["confidence_buckets"]
    assert len(buckets) == 10
    assert buckets[9] == {
        "low": 0.9,
        "high": 1.0,
        "total": 1,
        "confirmed": 1,
        "false_positive": 0,
    }
    assert buckets[6]["total"] == 1
    assert buckets[6]["false_positive"] == 1
    assert buckets[8]["confirmed"] == 1
    assert buckets[7]["false_positive"] == 1
    assert buckets[0]["total"] == 0
    assert sum(bucket["total"] for bucket in buckets) == 4


def test_stats_days_validation_and_default():
    bundle = services()
    routes, _ = routes_for(bundle)
    call(routes.get_stats)
    assert bundle.store.stats_since == 2_000_000_000 - 30 * 86400
    for raw in ["0", "91", "abc", "-1", "1.0", "1" * 5]:
        response = call(routes.get_stats, query=[("days", raw)])
        assert response.status_code == 400, raw
    assert call(routes.get_stats, query=[("days", "90")]).status_code == 200


def test_stats_without_store_is_503():
    routes, _ = routes_for(services(store=None))
    assert call(routes.get_stats).status_code == 503


def test_bucket_index_is_clamped():
    assert web._bucket_index(0.0) == 0
    assert web._bucket_index(1.0) == 9
    assert web._bucket_index(0.7) == 7
    assert web._bucket_index(0.6999999) == 6
    assert web._bucket_index(-1.0) == 0
    assert web._bucket_index(2.0) == 9


# --- reports ----------------------------------------------------------------


def test_reports_returns_newest_first():
    bundle = services()
    bundle.store.reports = [
        PatrolReport(started_at=9, finished_at=10, reviewed=1),
        PatrolReport(started_at=1, finished_at=2, reviewed=2),
    ]
    routes, _ = routes_for(bundle)
    payload = body(call(routes.get_reports, query=[("limit", "1")]))
    assert [report["reviewed"] for report in payload] == [1]


@pytest.mark.parametrize("raw", ["0", "abc", "101"])
def test_reports_rejects_bad_limit(raw):
    routes, _ = routes_for()
    assert call(routes.get_reports, query=[("limit", raw)]).status_code in (400, 200)


def test_reports_limit_is_clamped():
    bundle = services()
    routes, _ = routes_for(bundle)
    assert call(routes.get_reports, query=[("limit", "1000")]).status_code == 200
    assert call(routes.get_reports, query=[("limit", "0")]).status_code == 200


def test_reports_without_store_is_503():
    routes, _ = routes_for(services(store=None))
    assert call(routes.get_reports).status_code == 503


# --- patrol run -------------------------------------------------------------


def test_patrol_run_starts_and_reports_busy():
    started: list[bool] = []

    def start():
        started.append(True)
        return len(started) == 1

    bundle = services(start_patrol=start)
    routes, _ = routes_for(bundle)
    assert body(call(routes.post_patrol_run, method="POST")) == {"started": True}
    response = call(routes.post_patrol_run, method="POST")
    assert response.status_code == 409
    assert "手动巡查" in error(response)


def test_patrol_run_requires_store_and_credentials():
    routes, _ = routes_for(services(store=None))
    assert call(routes.post_patrol_run, method="POST").status_code == 503
    routes, _ = routes_for(services(patrol=None))
    assert call(routes.post_patrol_run, method="POST").status_code == 503


# --- check ------------------------------------------------------------------


def test_check_judges_with_temporary_rules():
    bundle = services(
        judge=FakeJudge(
            Verdict(violation=True, category="广告", confidence=0.8, ban_days=3)
        )
    )
    routes, _ = routes_for(bundle)
    payload = body(
        call(
            routes.post_check,
            method="POST",
            payload={"tid": 10, "rules": "  新吧规  "},
        )
    )
    assert payload["action"] == "report"
    assert payload["verdict"]["category"] == "广告"
    assert payload["images_reviewed"] == 0
    assert payload["images_total"] == 0
    assert payload["item"]["tid"] == 10
    item, urls, rules = bundle.judge.calls[0]
    assert (item.tid, urls, rules) == (10, [], "新吧规")


def test_check_uses_configured_rules_when_empty():
    bundle = services()
    routes, _ = routes_for(bundle)
    body(call(routes.post_check, method="POST", payload={"tid": 10}))
    assert bundle.judge.calls[0][2] == ""


def test_check_downloads_images_per_settings():
    bundle = services(
        settings=dcs.replace(SETTINGS, review_images=True, max_images=2),
        judge=FakeJudge(),
    )
    bundle.gateway.get_thread_item_by_tid.return_value = make_item(
        image_urls=("a", "b", "c")
    )
    routes, _ = routes_for(bundle)
    payload = body(call(routes.post_check, method="POST", payload={"tid": 10}))
    bundle.gateway.fetch_images.assert_awaited_once_with(("a", "b", "c"), 2)
    assert payload["images_reviewed"] == 1
    assert payload["images_total"] == 3


@pytest.mark.parametrize(
    "payload,match",
    [
        (None, "JSON 对象"),
        ([], "JSON 对象"),
        ({}, "tid"),
        ({"tid": "10"}, "tid"),
        ({"tid": True}, "tid"),
        ({"tid": 0}, "tid"),
        ({"tid": -1}, "tid"),
        ({"tid": 10**19}, "tid"),
        ({"tid": 10, "rules": 5}, "rules"),
        ({"tid": 10, "rules": "x" * 8001}, "临时吧规"),
    ],
)
def test_check_rejects_bad_payloads(payload, match):
    bundle = services()
    routes, _ = routes_for(bundle)
    response = call(routes.post_check, method="POST", payload=payload)
    assert response.status_code == 400
    assert match in error(response)
    bundle.gateway.get_thread_item_by_tid.assert_not_awaited()


def test_check_accepts_maximum_rule_length():
    bundle = services()
    routes, _ = routes_for(bundle)
    response = call(
        routes.post_check, method="POST", payload={"tid": 1, "rules": "x" * 8000}
    )
    assert response.status_code == 200


def test_check_requires_store_and_credentials():
    routes, _ = routes_for(services(store=None))
    assert call(routes.post_check, method="POST", payload={"tid": 10}).status_code == 503
    routes, _ = routes_for(services(gateway=None))
    response = call(routes.post_check, method="POST", payload={"tid": 10})
    assert response.status_code == 503


@pytest.mark.parametrize(
    "failure,status",
    [
        (JudgeError("模型超时"), 502),
        (ImageFetchError("图挂了"), 502),
        (TiebaActionError("code=4, errmsg=denied"), 502),
        (RuntimeError("kaboom"), 500),
    ],
)
def test_check_failures_are_mapped(failure, status):
    bundle = services(judge=FakeJudge(error=failure))
    routes, _ = routes_for(bundle)
    response = call(routes.post_check, method="POST", payload={"tid": 10})
    assert response.status_code == status
    assert "kaboom" not in error(response)


def test_check_is_mutually_exclusive():
    bundle = services()
    routes, _ = routes_for(bundle)
    routes._check_lock = asyncio.Lock()

    async def scenario():
        async with routes._check_lock:
            raw = FakeRawRequest(method="POST", payload={"tid": 10})
            plugin_request = PluginRequest(raw, path_params={}, username="alice")
            with bind_request_context(plugin_request):
                return await routes.post_check()

    response = asyncio.run(scenario())
    assert response.status_code == 409
    assert "试判" in error(response)


# --- value helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", None),
        ("1", 1),
        ("0007", 7),
        ("1" * 18, int("1" * 18)),
        ("1" * 19, None),
        ("-1", None),
        ("+1", None),
        ("１", None),
        ("", None),
        (None, None),
    ],
)
def test_path_int(raw, expected):
    assert web._path_int(raw, allow_zero=False) == expected


def test_path_int_allows_zero_for_indexes():
    assert web._path_int("0", allow_zero=True) == 0


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 20),
        ("", 20),
        ("5", 5),
        ("0", 1),
        ("1000", 100),
        ("-5", 1),
        ("abc", None),
        ("1.5", None),
        ("-", None),
        ("1" * 19, None),
    ],
)
def test_query_int(raw, expected):
    assert web._query_int(raw, default=20, low=1, high=100) == expected


def test_one_line_strips_controls_and_collapses_space():
    assert web._one_line("a\nb\tc  d\x00e", 100) == "a b c d e"
    assert web._one_line("x" * 100, 5) == "xxxxx"


# --- plugin wiring ----------------------------------------------------------


class FakeContextForWeb:
    """Star context stub that records Web API registrations."""

    def __init__(self) -> None:
        self.registered: list[tuple] = []
        self.send_message = AsyncMock(return_value=True)
        self.llm_generate = AsyncMock()

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append((route, handler, methods, desc))

    def get_using_provider(self):
        return None


def make_plugin(**config):
    context = FakeContextForWeb()
    values = {
        "forums": ["test"],
        "patrol": {"enabled": False},
        "notify": {"sessions": []},
    }
    values.update(config)
    return entry.TiebaModeratorPlugin(context, FakeConfig(**values)), context


def test_registration_covers_every_route_under_the_plugin_prefix():
    plugin, context = make_plugin()
    routes = {route for route, *_ in context.registered}
    assert routes == {
        f"/{entry.PLUGIN_NAME}/status",
        f"/{entry.PLUGIN_NAME}/cases",
        f"/{entry.PLUGIN_NAME}/cases/<case_id>",
        f"/{entry.PLUGIN_NAME}/cases/<case_id>/images/<index>",
        f"/{entry.PLUGIN_NAME}/cases/<case_id>/delete",
        f"/{entry.PLUGIN_NAME}/cases/<case_id>/ban",
        f"/{entry.PLUGIN_NAME}/cases/<case_id>/ignore",
        f"/{entry.PLUGIN_NAME}/cases/<case_id>/restore",
        f"/{entry.PLUGIN_NAME}/stats",
        f"/{entry.PLUGIN_NAME}/reports",
        f"/{entry.PLUGIN_NAME}/patrol/run",
        f"/{entry.PLUGIN_NAME}/check",
    }
    by_route = {route: methods for route, _, methods, _ in context.registered}
    assert by_route[f"/{entry.PLUGIN_NAME}/status"] == ["GET"]
    assert by_route[f"/{entry.PLUGIN_NAME}/patrol/run"] == ["POST"]
    assert all(desc for _, _, _, desc in context.registered)
    assert plugin._web is not None


def test_services_reflect_live_dependencies():
    plugin, _ = make_plugin()
    assert plugin._web_services().store is None
    assert plugin._web_services().gateway is None
    store = FakeStore()
    plugin._store = store
    plugin._gateway = FakeGateway()
    bundle = plugin._web_services()
    assert bundle.store is store and bundle.gateway is plugin._gateway
    assert bundle.patrol_enabled is False
    assert bundle.manual_running is False
    assert bundle.loop_running is False
    assert bundle.redact(ValueError("x")) == "x"


def test_start_manual_patrol_refuses_a_second_round():
    plugin, _ = make_plugin()
    assert plugin._start_manual_patrol() is False  # no patrol yet

    async def scenario():
        release = asyncio.Event()

        async def round_once():
            await release.wait()
            return PatrolReport(started_at=1, finished_at=2)

        plugin._patrol = SimpleNamespace(run_once=round_once)
        assert plugin._start_manual_patrol() is True
        assert plugin._start_manual_patrol() is False
        assert plugin._web_services().manual_running is True
        release.set()
        report = await plugin._manual_task
        assert report.finished_at == 2
        assert plugin._web_services().manual_running is False
        # A finished round frees the slot again.
        plugin._patrol = SimpleNamespace(run_once=AsyncMock(return_value=report))
        assert plugin._start_manual_patrol() is True
        await plugin._manual_task

    asyncio.run(scenario())


def test_finish_round_saves_the_report_and_survives_storage_errors(caplog):
    plugin, _ = make_plugin()
    store = FakeStore()
    plugin._store = store
    report = PatrolReport(started_at=1, finished_at=2, errors=("post:1 failed",))
    plugin._finish_round(report)
    assert store.save_calls == [report]
    assert plugin._last_report is report

    store.fail_save = True
    plugin._finish_round(report)
    assert plugin._last_report is report


def test_manual_round_failure_returns_none_without_saving():
    plugin, _ = make_plugin()
    store = FakeStore()
    plugin._store = store

    async def failing():
        raise RuntimeError("round exploded")

    async def scenario():
        plugin._patrol = SimpleNamespace(run_once=failing)
        assert plugin._start_manual_patrol() is True
        assert await plugin._manual_task is None
        assert store.save_calls == []

    asyncio.run(scenario())


def test_terminate_cancels_a_running_manual_round():
    plugin, _ = make_plugin()

    async def scenario():
        entered = asyncio.Event()

        async def round_once():
            entered.set()
            await asyncio.sleep(3600)

        plugin._patrol = SimpleNamespace(run_once=round_once)
        assert plugin._start_manual_patrol() is True
        task = plugin._manual_task
        await entered.wait()
        await plugin.terminate()
        assert task.cancelled()
        assert plugin._manual_task is None

    asyncio.run(scenario())


def test_tb_run_shares_the_manual_flag_and_reports_the_outcome():
    plugin, _ = make_plugin()
    store = FakeStore()
    plugin._store = store
    report = PatrolReport(started_at=1, finished_at=2, reported=1)

    async def scenario():
        plugin._patrol = SimpleNamespace(run_once=AsyncMock(return_value=report))
        event = SimpleNamespace(
            is_admin=lambda: True,
            get_sender_id=lambda: "admin",
            plain_result=lambda text: text,
        )
        replies = [reply async for reply in plugin.tb_run(event)]
        assert replies[0].startswith("开始巡查")
        assert "巡查完成" in replies[-1]
        assert store.save_calls == [report]

    asyncio.run(scenario())


# --- image cache budgets ----------------------------------------------------


def cached_case(*, images=("u",)):
    """A case with image URLs, inserted into a fresh service bundle."""
    bundle = services()
    bundle.store.cases[1] = dcs.replace(
        make_case(1), item=make_item(image_urls=tuple(images))
    )
    routes, _ = routes_for(bundle)
    return routes, bundle


def cache_total(routes):
    """Recompute the cache size from the entries, to check the running count."""
    return sum(len(value) for value in routes._image_cache.values())


def test_total_chars_limit_evicts_oldest_entries(monkeypatch):
    monkeypatch.setattr(web, "IMAGE_CACHE_MAX_CHARS", 10_000)
    monkeypatch.setattr(web, "IMAGE_CACHE_MAX_ENTRY_CHARS", 4_000)
    routes, bundle = cached_case(images=tuple(f"u{index}" for index in range(9)))
    payload = "data:image/jpeg;base64," + "A" * 3_900
    bundle.gateway.fetch_images.return_value = [payload]
    for index in range(9):
        assert call(routes.get_case_image, case_id="1", index=str(index)).status_code == 200
    # Two entries fit under 10_000 chars; a third would exceed it.
    assert len(routes._image_cache) == 2
    assert cache_total(routes) <= 10_000
    assert routes._image_cache_chars == cache_total(routes)
    assert list(routes._image_cache) == ["1:7", "1:8"]


def test_running_char_count_matches_the_entries_after_evictions(monkeypatch):
    monkeypatch.setattr(web, "IMAGE_CACHE_MAX_CHARS", 10_000)
    monkeypatch.setattr(web, "IMAGE_CACHE_MAX_ENTRY_CHARS", 4_000)
    routes, bundle = cached_case(images=tuple(f"u{index}" for index in range(6)))
    bundle.gateway.fetch_images.return_value = ["x" * 4_000]
    for index in range(6):
        call(routes.get_case_image, case_id="1", index=str(index))
    assert routes._image_cache_chars == cache_total(routes) == 8_000
    # A cache hit must not change the accounting.
    call(routes.get_case_image, case_id="1", index="5")
    assert routes._image_cache_chars == cache_total(routes) == 8_000
    # Re-fetching an evicted key is a new insert, not double counting.
    call(routes.get_case_image, case_id="1", index="0")
    assert routes._image_cache_chars == cache_total(routes)
    assert len(routes._image_cache) == 2


def test_oversized_single_image_is_served_but_not_cached(monkeypatch):
    monkeypatch.setattr(web, "IMAGE_CACHE_MAX_CHARS", 1_000)
    monkeypatch.setattr(web, "IMAGE_CACHE_MAX_ENTRY_CHARS", 125)
    routes, bundle = cached_case()
    bundle.gateway.fetch_images.return_value = ["y" * 126]
    response = call(routes.get_case_image, case_id="1", index="0")
    assert response.status_code == 200
    assert body(response) == {"data_url": "y" * 126}
    assert routes._image_cache == {}
    assert routes._image_cache_chars == 0
    # It is fetched again next time because nothing was cached.
    call(routes.get_case_image, case_id="1", index="0")
    assert bundle.gateway.fetch_images.await_count == 2


def test_entry_size_boundary_is_inclusive(monkeypatch):
    monkeypatch.setattr(web, "IMAGE_CACHE_MAX_CHARS", 1_000)
    monkeypatch.setattr(web, "IMAGE_CACHE_MAX_ENTRY_CHARS", 125)
    routes, bundle = cached_case()
    bundle.gateway.fetch_images.return_value = ["z" * 125]
    call(routes.get_case_image, case_id="1", index="0")
    assert routes._image_cache == {"1:0": "z" * 125}
    assert routes._image_cache_chars == 125


def test_entry_count_still_caps_the_cache(monkeypatch):
    monkeypatch.setattr(web, "IMAGE_CACHE_SIZE", 2)
    routes, bundle = cached_case(images=tuple(f"u{index}" for index in range(4)))
    bundle.gateway.fetch_images.return_value = ["q" * 10]
    for index in range(4):
        call(routes.get_case_image, case_id="1", index=str(index))
    assert list(routes._image_cache) == ["1:2", "1:3"]
    assert routes._image_cache_chars == 20


def test_cache_hit_does_not_refetch_and_stays_within_budget(monkeypatch):
    monkeypatch.setattr(web, "IMAGE_CACHE_MAX_CHARS", 100)
    routes, bundle = cached_case()
    bundle.gateway.fetch_images.return_value = ["w" * 60]
    call(routes.get_case_image, case_id="1", index="0")
    call(routes.get_case_image, case_id="1", index="0")
    assert bundle.gateway.fetch_images.await_count == 1
    assert routes._image_cache_chars == cache_total(routes) == 60


def test_replacing_an_entry_adjusts_the_running_count():
    routes, _ = cached_case()
    routes._remember_image("1:0", "a" * 100)
    assert routes._image_cache_chars == 100
    routes._remember_image("1:0", "b" * 40)
    assert routes._image_cache == {"1:0": "b" * 40}
    assert routes._image_cache_chars == 40 == cache_total(routes)
