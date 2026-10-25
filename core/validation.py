"""Validate untrusted settings payloads from the WebUI plugin view.

Every parser takes ``object`` and returns a frozen settings dataclass, or raises
``ValidationError`` with a Chinese message naming the offending field. Nothing
here mutates configuration, touches the network or stores state: the settings
service calls these before writing.

Rules (``docs/settings.md`` §1 and §4):

- A payload must be a JSON object; missing required keys are an error.
- Unknown keys are rejected so a typo cannot be silently dropped.
- JSON types are strict: ``type(x) is int`` (``bool`` is not an int), a float is
  accepted where a number is expected, and a numeric string is never coerced.
- Sequences are bounded by length *before* they are iterated, so a huge array
  costs nothing.
- String length is checked *before* stripping or casefolding, so a multi-megabyte
  string never reaches those calls.
- Credentials are only format-checked. A ``ValidationError`` message never
  contains any part of a credential, not even a length, because error messages
  reach the browser and the log.
"""

from __future__ import annotations

from .settings import (
    AccountDraft,
    ForumDraft,
    ForumOptions,
    ForumOverrides,
    GlobalSettings,
)
from .validation_fields import (  # noqa: F401 - re-exported for tests
    MAX_ACCOUNT_NAME_CHARS,
    MAX_BDUSS_CHARS,
    MAX_FORUM_NAME_CHARS,
    MAX_FORUM_OPERATORS,
    MAX_FORUM_SESSIONS,
    MAX_GLOBAL_OPERATORS,
    MAX_GLOBAL_SESSIONS,
    MAX_IMAGES,
    MAX_INTERVAL_SECONDS,
    MAX_ITEMS_PER_ROUND,
    MAX_JUDGE_CONCURRENCY,
    MAX_JUDGE_TIMEOUT_SECONDS,
    MAX_LOOKBACK_HOURS,
    MAX_PROVIDER_ID_CHARS,
    MAX_RULES_CHARS,
    MAX_STOKEN_CHARS,
    MAX_THREAD_PAGES,
    MAX_WHITELIST,
    MAX_WHITELIST_CHARS,
    MIN_INTERVAL_SECONDS,
    MIN_JUDGE_TIMEOUT_SECONDS,
    OPERATOR_CHARS_MAX,
    SESSION_CHARS_MAX,
    ValidationError,
    _as_dict,
    _bool,
    _check_threshold_order,
    _frozenset,
    _has_control,
    _int,
    _optional_int,
    _optional_threshold,
    _path,
    _provider_id,
    _reject_unknown,
    _require,
    _required_threshold,
    _rules,
    _string_list,
    _text,
)

_GLOBAL_KEYS = frozenset(
    {
        "patrol_enabled",
        "interval_seconds",
        "max_items_per_round",
        "judge_concurrency",
        "judge_timeout_seconds",
        "review_images",
        "max_images",
        "sessions",
        "operators",
        "defaults",
    }
)
_OPTIONS_KEYS = frozenset(
    {
        "rules",
        "provider_id",
        "auto_delete_enabled",
        "auto_delete_threshold",
        "report_threshold",
        "thread_pages",
        "lookback_hours",
        "review_posts",
        "review_comments",
        "skip_top_and_good",
        "skip_bawu",
        "whitelist",
    }
)
_OVERRIDE_KEYS = _OPTIONS_KEYS - {"whitelist"}
_FORUM_KEYS = frozenset(
    {
        "fname",
        "enabled",
        "account_id",
        "sessions",
        "operators",
        "whitelist",
        "overrides",
    }
)
_ACCOUNT_CREATE_KEYS = frozenset({"name", "bduss", "stoken"})
_ACCOUNT_UPDATE_KEYS = frozenset({"name", "bduss", "stoken"})

_WHITESPACE = " \t\n\r\v\f"
"""Whitespace stripped from list items and forum names."""


def _options(
    payload: dict, *, where: str, provider_ids: frozenset[str]
) -> ForumOptions:
    """Build fully specified per-forum options (the global defaults).

    Args:
        payload: The ``defaults`` object.
        where: Field path used in the message.
        provider_ids: Known provider ids.

    Returns:
        The validated options.

    Raises:
        ValidationError: A field is missing or invalid.
    """
    rules = _rules(_require(payload, "rules", where=where), field=_path(where, "rules"))
    if not rules:
        raise ValidationError(f"{_path(where, 'rules')}不能为空")
    auto_delete = _required_threshold(
        payload, "auto_delete_threshold", where=where, low=0.0, high=1.0
    )
    report = _required_threshold(
        payload, "report_threshold", where=where, low=0.0, high=1.0
    )
    _check_threshold_order(auto_delete, report, where=where)
    return ForumOptions(
        rules=rules,
        provider_id=_provider_id(
            _require(payload, "provider_id", where=where),
            field=_path(where, "provider_id"),
            provider_ids=provider_ids,
        ),
        auto_delete_enabled=_bool(payload, "auto_delete_enabled", where=where),
        auto_delete_threshold=auto_delete,
        report_threshold=report,
        thread_pages=_int(
            payload, "thread_pages", where=where, low=1, high=MAX_THREAD_PAGES
        ),
        lookback_hours=_int(
            payload, "lookback_hours", where=where, low=1, high=MAX_LOOKBACK_HOURS
        ),
        review_posts=_bool(payload, "review_posts", where=where),
        review_comments=_bool(payload, "review_comments", where=where),
        skip_top_and_good=_bool(payload, "skip_top_and_good", where=where),
        skip_bawu=_bool(payload, "skip_bawu", where=where),
        whitelist=_frozenset(
            _require(payload, "whitelist", where=where),
            field=_path(where, "whitelist"),
            max_items=MAX_WHITELIST,
            max_chars=MAX_WHITELIST_CHARS,
        ),
    )


def _overrides(
    payload: object, *, where: str, provider_ids: frozenset[str]
) -> ForumOverrides:
    """Build per-forum overrides; ``null`` or absent means inherit.

    Args:
        payload: The ``overrides`` object.
        where: Field path used in the message.
        provider_ids: Known provider ids.

    Returns:
        The validated overrides.

    Raises:
        ValidationError: A field is present but invalid.
    """
    if payload is None:
        return ForumOverrides()
    data = _as_dict(payload, where=where)
    _reject_unknown(data, _OVERRIDE_KEYS, where=where)

    rules = data.get("rules")
    rules_value: str | None = None
    if rules is not None:
        text = _rules(rules, field=_path(where, "rules"))
        # An empty override means "inherit"; store None rather than "".
        rules_value = text or None

    provider = data.get("provider_id")
    provider_value: str | None = None
    if provider is not None:
        provider_value = (
            _provider_id(
                provider,
                field=_path(where, "provider_id"),
                provider_ids=provider_ids,
            )
            or None
        )

    auto_delete = _optional_threshold(
        data, "auto_delete_threshold", where=where, low=0.0, high=1.0
    )
    report = _optional_threshold(
        data, "report_threshold", where=where, low=0.0, high=1.0
    )
    _check_threshold_order(auto_delete, report, where=where)

    enabled = data.get("auto_delete_enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValidationError(
            f"{_path(where, 'auto_delete_enabled')}必须是 true 或 false"
        )

    booleans: dict[str, bool | None] = {}
    for key in ("review_posts", "review_comments", "skip_top_and_good", "skip_bawu"):
        value = data.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValidationError(f"{_path(where, key)}必须是 true 或 false")
        booleans[key] = value

    return ForumOverrides(
        rules=rules_value,
        provider_id=provider_value,
        auto_delete_enabled=enabled,
        auto_delete_threshold=auto_delete,
        report_threshold=report,
        thread_pages=_optional_int(
            data, "thread_pages", where=where, low=1, high=MAX_THREAD_PAGES
        ),
        lookback_hours=_optional_int(
            data, "lookback_hours", where=where, low=1, high=MAX_LOOKBACK_HOURS
        ),
        review_posts=booleans["review_posts"],
        review_comments=booleans["review_comments"],
        skip_top_and_good=booleans["skip_top_and_good"],
        skip_bawu=booleans["skip_bawu"],
    )


def parse_global(raw: object, *, provider_ids: frozenset[str]) -> GlobalSettings:
    """Validate a complete ``GlobalDTO``.

    Args:
        raw: Decoded request body.
        provider_ids: Chat provider ids that currently exist.

    Returns:
        The validated global settings.

    Raises:
        ValidationError: The payload is not a complete, valid global object.
    """
    data = _as_dict(raw, where="")
    _reject_unknown(data, _GLOBAL_KEYS, where="")
    defaults = _as_dict(_require(data, "defaults", where=""), where="defaults")
    _reject_unknown(defaults, _OPTIONS_KEYS, where="defaults")
    return GlobalSettings(
        patrol_enabled=_bool(data, "patrol_enabled", where=""),
        interval_seconds=_int(
            data,
            "interval_seconds",
            where="",
            low=MIN_INTERVAL_SECONDS,
            high=MAX_INTERVAL_SECONDS,
        ),
        max_items_per_round=_int(
            data, "max_items_per_round", where="", low=1, high=MAX_ITEMS_PER_ROUND
        ),
        judge_concurrency=_int(
            data, "judge_concurrency", where="", low=1, high=MAX_JUDGE_CONCURRENCY
        ),
        judge_timeout_seconds=_int(
            data,
            "judge_timeout_seconds",
            where="",
            low=MIN_JUDGE_TIMEOUT_SECONDS,
            high=MAX_JUDGE_TIMEOUT_SECONDS,
        ),
        review_images=_bool(data, "review_images", where=""),
        max_images=_int(data, "max_images", where="", low=0, high=MAX_IMAGES),
        sessions=_string_list(
            _require(data, "sessions", where=""),
            field="sessions",
            max_items=MAX_GLOBAL_SESSIONS,
            max_chars=SESSION_CHARS_MAX,
        ),
        operators=_frozenset(
            _require(data, "operators", where=""),
            field="operators",
            max_items=MAX_GLOBAL_OPERATORS,
            max_chars=OPERATOR_CHARS_MAX,
            no_whitespace=True,
        ),
        defaults=_options(defaults, where="defaults", provider_ids=provider_ids),
    )


def parse_forum_draft(
    raw: object,
    *,
    provider_ids: frozenset[str],
    account_ids: frozenset[int],
) -> ForumDraft:
    """Validate a complete ``ForumDraftDTO``.

    Args:
        raw: Decoded request body.
        provider_ids: Chat provider ids that currently exist.
        account_ids: Ids of the accounts that currently exist.

    Returns:
        The validated forum draft.

    Raises:
        ValidationError: The payload is not a complete, valid forum object.
    """
    data = _as_dict(raw, where="")
    _reject_unknown(data, _FORUM_KEYS, where="")
    fname = _text(
        _require(data, "fname", where=""),
        field="fname",
        max_chars=MAX_FORUM_NAME_CHARS,
        min_chars=1,
    )
    account_id = data.get("account_id")
    if account_id is not None:
        if type(account_id) is not int or account_id <= 0:
            raise ValidationError("account_id 必须是账号编号或 null")
        if account_id not in account_ids:
            raise ValidationError("account_id 指向的账号不存在")
    return ForumDraft(
        fname=fname,
        enabled=_bool(data, "enabled", where=""),
        account_id=account_id,
        sessions=_string_list(
            _require(data, "sessions", where=""),
            field="sessions",
            max_items=MAX_FORUM_SESSIONS,
            max_chars=SESSION_CHARS_MAX,
        ),
        operators=_frozenset(
            _require(data, "operators", where=""),
            field="operators",
            max_items=MAX_FORUM_OPERATORS,
            max_chars=OPERATOR_CHARS_MAX,
            no_whitespace=True,
        ),
        whitelist=_frozenset(
            _require(data, "whitelist", where=""),
            field="whitelist",
            max_items=MAX_WHITELIST,
            max_chars=MAX_WHITELIST_CHARS,
        ),
        overrides=_overrides(
            _require(data, "overrides", where=""),
            where="overrides",
            provider_ids=provider_ids,
        ),
    )


def parse_account_draft(raw: object, *, creating: bool) -> AccountDraft:
    """Validate an account create or update payload.

    Credential errors never echo the value: the message names the field only.

    Args:
        raw: Decoded request body.
        creating: True for create (both credentials required), False for update
            (absent or empty credentials mean "keep the stored value").

    Returns:
        The validated draft.

    Raises:
        ValidationError: The payload is invalid.
    """
    data = _as_dict(raw, where="")
    _reject_unknown(
        data, _ACCOUNT_CREATE_KEYS if creating else _ACCOUNT_UPDATE_KEYS, where=""
    )
    name = _text(
        _require(data, "name", where=""),
        field="name",
        max_chars=MAX_ACCOUNT_NAME_CHARS,
        min_chars=1,
    )
    bduss = _credential(
        data.get("bduss"), field="bduss", max_chars=MAX_BDUSS_CHARS, required=creating
    )
    stoken = _credential(
        data.get("stoken"),
        field="stoken",
        max_chars=MAX_STOKEN_CHARS,
        required=creating,
    )
    return AccountDraft(name=name, bduss=bduss, stoken=stoken)


def _credential(
    value: object, *, field: str, max_chars: int, required: bool
) -> str | None:
    """Validate one credential without ever echoing it.

    Args:
        value: Raw value.
        field: Field path used in the message.
        max_chars: Maximum accepted length.
        required: Whether an empty value is an error.

    Returns:
        The credential, or None when it is absent or empty on update.

    Raises:
        ValidationError: The value is invalid.
    """
    if value is None:
        if required:
            raise ValidationError(f"{field}不能为空")
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field}必须是字符串")
    if len(value) > max_chars:
        raise ValidationError(f"{field}长度超出上限")
    if not value:
        if required:
            raise ValidationError(f"{field}不能为空")
        return None
    if not value.isascii() or not value.isprintable() or _has_control(value):
        raise ValidationError(f"{field}只能包含可打印 ASCII 字符")
    if any(ch.isspace() for ch in value):
        raise ValidationError(f"{field}不能包含空白字符")
    return value
