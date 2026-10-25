"""Field-level validation primitives for settings payloads.

Split out of ``core.validation`` so each module stays small enough to read in one
pass. These helpers are shared by the three payload parsers; none of them is a
public API, and only ``ValidationError`` is imported from here.

Two rules hold throughout:

- A payload must be a JSON object, unknown keys are rejected, and JSON types are
  strict (``type(x) is int``; ``bool`` is never an ``int``; a numeric string is
  never coerced).
- Sequences are bounded by length *before* iteration and strings by length
  *before* stripping or scanning, so an oversized value costs almost nothing.
"""

from __future__ import annotations

import math

MAX_INTERVAL_SECONDS = 86400
MIN_INTERVAL_SECONDS = 60
MAX_ITEMS_PER_ROUND = 500
MAX_JUDGE_CONCURRENCY = 8
MIN_JUDGE_TIMEOUT_SECONDS = 10
MAX_JUDGE_TIMEOUT_SECONDS = 600
MAX_IMAGES = 9
MAX_GLOBAL_SESSIONS = 20
MAX_GLOBAL_OPERATORS = 200

# Per-forum option ranges (§1.2).
MAX_RULES_CHARS = 8000
MAX_PROVIDER_ID_CHARS = 128
MAX_THREAD_PAGES = 5
MAX_LOOKBACK_HOURS = 168
MAX_WHITELIST = 1000
MAX_WHITELIST_CHARS = 128

# Accounts (§1.3).
MAX_ACCOUNTS = 20
MAX_ACCOUNT_NAME_CHARS = 32
MAX_BDUSS_CHARS = 512
MAX_STOKEN_CHARS = 256

# Forums (§1.4).
MAX_FORUM_NAME_CHARS = 64
MAX_FORUMS = 50
MAX_FORUM_SESSIONS = 20
MAX_FORUM_OPERATORS = 200
SESSION_CHARS_MIN = 1
SESSION_CHARS_MAX = 512
OPERATOR_CHARS_MIN = 1
OPERATOR_CHARS_MAX = 64


_WHITESPACE = " \t\n\r\v\f"
"""Whitespace stripped from list items and forum names."""


class ValidationError(ValueError):
    """A settings payload is malformed; the message is safe to show to a user."""


def _reject_unknown(payload: dict, allowed: frozenset[str], *, where: str) -> None:
    """Reject keys outside the contract so typos cannot be dropped silently.

    Args:
        payload: The decoded JSON object.
        allowed: Permitted keys.
        where: Field path used in the message.

    Raises:
        ValidationError: An unexpected key is present.
    """
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValidationError(f"{where or '请求体'} 包含未知字段：{_label(unknown[0])}")


def _label(key: object) -> str:
    """Render an untrusted key for a message without echoing control characters."""
    text = str(key)
    if len(text) > 40 or not text.isprintable():
        return "（非法字段名）"
    return text


def _as_dict(raw: object, *, where: str) -> dict:
    """Require a JSON object.

    Args:
        raw: Decoded payload.
        where: Field path used in the message.

    Returns:
        The mapping.

    Raises:
        ValidationError: The payload is not an object.
    """
    if not isinstance(raw, dict):
        raise ValidationError(f"{where}必须是 JSON 对象")
    return raw


def _require(payload: dict, key: str, *, where: str) -> object:
    """Read a required key.

    Args:
        payload: The object.
        key: Field name.
        where: Field path used in the message.

    Returns:
        The raw value.

    Raises:
        ValidationError: The key is absent.
    """
    if key not in payload:
        raise ValidationError(f"{_path(where, key)}不能为空")
    return payload[key]


def _path(where: str, key: str) -> str:
    """Join a field path.

    Args:
        where: Enclosing path.
        key: Field name.

    Returns:
        ``key`` when there is no prefix, otherwise ``where.key``.
    """
    return f"{where}.{key}" if where else key


def _bool(payload: dict, key: str, *, where: str) -> bool:
    """Read a required boolean, rejecting ints and strings.

    Args:
        payload: The object.
        key: Field name.
        where: Field path used in the message.

    Returns:
        The value.

    Raises:
        ValidationError: The value is not a JSON boolean.
    """
    value = _require(payload, key, where=where)
    if not isinstance(value, bool):
        raise ValidationError(f"{_path(where, key)}必须是 true 或 false")
    return value


def _int(payload: dict, key: str, *, where: str, low: int, high: int) -> int:
    """Read a required integer inside an inclusive range.

    ``bool`` is rejected even though it is an ``int`` subclass, and floats are
    rejected even when integral, because the contract says "整数".

    Args:
        payload: The object.
        key: Field name.
        where: Field path used in the message.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        The value.

    Raises:
        ValidationError: The value is not an in-range integer.
    """
    value = _require(payload, key, where=where)
    if type(value) is not int:
        raise ValidationError(f"{_path(where, key)}必须是 {low} 到 {high} 的整数")
    if not low <= value <= high:
        raise ValidationError(f"{_path(where, key)}必须是 {low} 到 {high} 的整数")
    return value


def _optional_int(
    payload: dict, key: str, *, where: str, low: int, high: int
) -> int | None:
    """Read an optional integer inside an inclusive range.

    Args:
        payload: The object.
        key: Field name.
        where: Field path used in the message.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        The value, or None when the key is absent or JSON null.

    Raises:
        ValidationError: The value is present but not an in-range integer.
    """
    value = payload.get(key)
    if value is None:
        return None
    if type(value) is not int:
        raise ValidationError(f"{_path(where, key)}必须是 {low} 到 {high} 的整数")
    if not low <= value <= high:
        raise ValidationError(f"{_path(where, key)}必须是 {low} 到 {high} 的整数")
    return value


def _threshold(value: object, *, field: str, low: float, high: float) -> float:
    """Validate a threshold.

    Args:
        value: Raw value.
        field: Field path used in the message.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        The threshold as a float. Integers are accepted and widened.

    Raises:
        ValidationError: The value is not a finite in-range number.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationError(f"{field}必须是 {low:g} 到 {high:g} 的数字")
    number = float(value)
    if not math.isfinite(number):
        # JSON has no NaN/Infinity, but a non-standard encoder can produce them.
        raise ValidationError(f"{field}必须是 {low:g} 到 {high:g} 的数字")
    if not low <= number <= high:
        raise ValidationError(f"{field}必须是 {low:g} 到 {high:g} 的数字")
    return number


def _required_threshold(
    payload: dict, key: str, *, where: str, low: float, high: float
) -> float:
    """Read a required threshold.

    Args:
        payload: The object.
        key: Field name.
        where: Field path used in the message.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        The validated number.

    Raises:
        ValidationError: The value is missing or invalid.
    """
    value = _require(payload, key, where=where)
    return _threshold(value, field=_path(where, key), low=low, high=high)


def _optional_threshold(
    payload: dict, key: str, *, where: str, low: float, high: float
) -> float | None:
    """Read an optional threshold.

    Args:
        payload: The object.
        key: Field name.
        where: Field path used in the message.
        low: Inclusive lower bound.
        high: Inclusive upper bound.

    Returns:
        The validated number, or None when absent or null.

    Raises:
        ValidationError: The value is present but invalid.
    """
    value = payload.get(key)
    if value is None:
        return None
    return _threshold(value, field=_path(where, key), low=low, high=high)


_STRIP_SLACK = 8
"""Surrounding whitespace allowed beyond the field limit before stripping."""


def _text(
    value: object,
    *,
    field: str,
    max_chars: int,
    min_chars: int = 0,
    allow_control: bool = False,
    no_whitespace: bool = False,
) -> str:
    """Validate a string: raw length first, then content, then stripped length.

    The raw length is checked before any strip so a multi-megabyte string never
    reaches a scan; a small allowance covers padding around a value that is
    itself at the limit, and the stripped value is then checked against the real
    bound.

    Args:
        value: Raw value.
        field: Field path used in the message.
        max_chars: Maximum accepted length after stripping.
        min_chars: Minimum length after stripping.
        allow_control: Whether newlines and tabs are permitted (multi-line text).
        no_whitespace: Whether whitespace inside the value is forbidden.

    Returns:
        The stripped text.

    Raises:
        ValidationError: The value is not a string, is too long, is empty, or
            contains characters the field must not hold.
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field}必须是字符串")
    if len(value) > max_chars + _STRIP_SLACK:
        raise ValidationError(f"{field}不能超过 {max_chars} 个字符")
    if not allow_control and _has_control(value):
        raise ValidationError(f"{field}不能包含控制字符")
    text = value.strip(_WHITESPACE)
    if no_whitespace and any(ch.isspace() for ch in text):
        # Padding is a paste artifact; whitespace inside the value is not.
        raise ValidationError(f"{field}不能包含空白字符")
    if len(text) > max_chars:
        raise ValidationError(f"{field}不能超过 {max_chars} 个字符")
    if len(text) < min_chars:
        raise ValidationError(f"{field}不能为空")
    return text


def _has_control(value: str) -> bool:
    """Whether a string holds C0/C1 controls or zero-width/bidi characters.

    Tab and newline count as control characters here; callers that allow
    multi-line text use ``allow_control``.

    Args:
        value: The string.

    Returns:
        True when any forbidden character is present.
    """
    for ch in value:
        if ch in "\t\n\r\v\f":
            return True
        if ch.isspace() and ch != " ":
            # Non-breaking and other Unicode spaces would render inconsistently.
            return True
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            return True
        if 0x200B <= code <= 0x200F or 0x202A <= code <= 0x202E:
            return True
        if 0x2066 <= code <= 0x2069 or code == 0xFEFF:
            return True
    return False


def _string_list(
    value: object,
    *,
    field: str,
    max_items: int,
    max_chars: int,
    min_chars: int = 1,
    allow_duplicates: bool = False,
    no_whitespace: bool = False,
) -> tuple[str, ...]:
    """Validate a list of strings.

    Length is checked before iteration, and item length before stripping.

    Args:
        value: Raw value; must be a JSON array.
        field: Field path used in the message.
        max_items: Maximum number of items.
        max_chars: Maximum characters per item.
        min_chars: Minimum characters per item after stripping.
        allow_duplicates: Whether repeated items are kept.
        no_whitespace: Whether whitespace inside an item is forbidden.

    Returns:
        Stripped items in input order, deduplicated unless allowed.

    Raises:
        ValidationError: The value is not an array, is too long, or holds an
            invalid item.
    """
    if not isinstance(value, list):
        raise ValidationError(f"{field}必须是数组")
    if len(value) > max_items:
        raise ValidationError(f"{field}最多 {max_items} 条")
    items: list[str] = []
    for item in value:
        text = _text(
            item,
            field=field,
            max_chars=max_chars,
            min_chars=min_chars,
            no_whitespace=no_whitespace,
        )
        items.append(text)
    return tuple(items) if allow_duplicates else tuple(dict.fromkeys(items))


def _frozenset(
    value: object,
    *,
    field: str,
    max_items: int,
    max_chars: int,
    no_whitespace: bool = False,
):
    """Validate a list of strings into an immutable set.

    Args:
        value: Raw value.
        field: Field path used in the message.
        max_items: Maximum number of items.
        max_chars: Maximum characters per item.
        no_whitespace: Whether whitespace inside an item is forbidden.

    Returns:
        The unique items.

    Raises:
        ValidationError: The value is invalid.
    """
    return frozenset(
        _string_list(
            value,
            field=field,
            max_items=max_items,
            max_chars=max_chars,
            no_whitespace=no_whitespace,
        )
    )


def _provider_id(value: object, *, field: str, provider_ids: frozenset[str]) -> str:
    """Validate a chat provider id against the live provider list.

    Args:
        value: Raw value.
        field: Field path used in the message.
        provider_ids: Ids currently configured in AstrBot.

    Returns:
        The provider id, or an empty string for "use the AstrBot default".

    Raises:
        ValidationError: The id is malformed or unknown.
    """
    text = _text(value, field=field, max_chars=MAX_PROVIDER_ID_CHARS)
    if text and text not in provider_ids:
        raise ValidationError(f"{field}不是当前可用的模型提供商")
    return text


def _rules(value: object, *, field: str) -> str:
    """Validate forum rules text, which may span lines.

    Args:
        value: Raw value.
        field: Field path used in the message.

    Returns:
        The rules text.

    Raises:
        ValidationError: The value is not a string or is too long.
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field}必须是字符串")
    if len(value) > MAX_RULES_CHARS:
        raise ValidationError(f"{field}不能超过 {MAX_RULES_CHARS} 个字符")
    for ch in value:
        # Rules are multi-line text: tab, newline and carriage return are fine.
        if ch in "\t\n\r":
            continue
        if _has_control(ch):
            raise ValidationError(f"{field}不能包含控制字符")
    return value.strip(_WHITESPACE)


def _check_threshold_order(
    auto_delete: float | None, report: float | None, *, where: str
) -> None:
    """Enforce ``report_threshold <= auto_delete_threshold`` when both are given.

    Args:
        auto_delete: Auto-delete threshold, or None when inheriting.
        report: Report threshold, or None when inheriting.
        where: Field path used in the message.

    Raises:
        ValidationError: The pair is inverted.
    """
    if auto_delete is None or report is None:
        return
    if report > auto_delete:
        raise ValidationError(
            f"{_path(where, 'report_threshold')}不能大于 "
            f"{_path(where, 'auto_delete_threshold')}"
        )


def is_safe_session(session: str) -> bool:
    """Whether a push session is safe to store and later echo.

    ``/tb bind`` reaches the settings service directly, without a payload
    parser, so the service uses this to apply the same character rules the Web
    API enforces: 1-512 characters, no C0/C1 control characters, no zero-width
    or bidi overrides.

    Args:
        session: Unified message origin from chat.

    Returns:
        True when the value may be stored.
    """
    return bool(session) and len(session) <= 512 and not _has_control(session)
