"""Settings data model (v0.3): accounts, forums and global defaults.

Settings are edited in the WebUI plugin view and stored in the plugin's own
SQLite database; the AstrBot plugin config is no longer used. Everything here
is pure and frozen, like ``core.models``. Construct with keyword arguments only
and change values with ``dataclasses.replace``.

Validation of untrusted input (Web API payloads) lives in ``core.validation``;
the classes below assume their values are already in range. See
``docs/settings.md`` for ranges, inheritance and the service contract.
"""

from __future__ import annotations

import dataclasses as dcs


@dcs.dataclass(frozen=True, kw_only=True)
class ForumOptions:
    """Fully resolved per-forum options; global defaults use the same shape."""

    rules: str
    provider_id: str = ""
    """Chat provider id; empty means the AstrBot default chat provider."""
    auto_delete_enabled: bool = False
    auto_delete_threshold: float = 0.9
    report_threshold: float = 0.6
    thread_pages: int = 1
    lookback_hours: int = 6
    review_posts: bool = True
    review_comments: bool = True
    skip_top_and_good: bool = True
    skip_bawu: bool = True
    whitelist: frozenset[str] = frozenset()
    """Exempt users (portrait or user_name). A forum's own list is added to the
    global list, never replacing it."""


@dcs.dataclass(frozen=True, kw_only=True)
class ForumOverrides:
    """Per-forum overrides; ``None`` inherits the global default.

    ``whitelist`` is not here: a forum's whitelist is always additive and lives
    on ``ForumEntry``.
    """

    rules: str | None = None
    provider_id: str | None = None
    auto_delete_enabled: bool | None = None
    auto_delete_threshold: float | None = None
    report_threshold: float | None = None
    thread_pages: int | None = None
    lookback_hours: int | None = None
    review_posts: bool | None = None
    review_comments: bool | None = None
    skip_top_and_good: bool | None = None
    skip_bawu: bool | None = None


@dcs.dataclass(frozen=True, kw_only=True)
class GlobalSettings:
    """Settings that apply to the whole plugin, plus the per-forum defaults."""

    patrol_enabled: bool = True
    interval_seconds: int = 180
    max_items_per_round: int = 60
    """Cap on reviewed items per round across all forums."""
    judge_concurrency: int = 2
    judge_timeout_seconds: int = 60
    review_images: bool = True
    max_images: int = 4
    sessions: tuple[str, ...] = ()
    """Default push sessions (unified_msg_origin)."""
    operators: frozenset[str] = frozenset()
    """Global operators: may handle cases of every forum."""
    defaults: ForumOptions


@dcs.dataclass(frozen=True, kw_only=True)
class Account:
    """A Tieba moderator account. Credentials never leave the process."""

    id: int
    name: str
    """Display label chosen by the admin; unique."""
    bduss: str = dcs.field(repr=False)
    stoken: str = dcs.field(repr=False)
    created_at: int
    updated_at: int


@dcs.dataclass(frozen=True, kw_only=True)
class ForumEntry:
    """A configured forum."""

    id: int
    fname: str
    """As typed by the admin (stripped); compared by ``casefold()``."""
    enabled: bool = True
    account_id: int | None = None
    """Moderator account used for reading and moderating; ``None`` means the
    forum is not patrolled until an account is chosen."""
    sessions: tuple[str, ...] = ()
    """Push sessions; empty falls back to ``GlobalSettings.sessions``."""
    operators: frozenset[str] = frozenset()
    """Local moderators: may handle this forum's cases only."""
    whitelist: frozenset[str] = frozenset()
    """Added to the global whitelist."""
    overrides: ForumOverrides = ForumOverrides()
    created_at: int
    updated_at: int


@dcs.dataclass(frozen=True, kw_only=True)
class ResolvedForum:
    """A forum with inheritance applied, as the patrol and commands use it."""

    fname: str
    enabled: bool
    account_id: int | None
    options: ForumOptions
    """Defaults merged with overrides; ``report_threshold`` is already capped
    at ``auto_delete_threshold``; ``whitelist`` is the union."""
    sessions: tuple[str, ...]
    """Resolved push sessions (own list, else the global list)."""
    operators: frozenset[str]
    """Local moderators only; global operators are on ``GlobalSettings``."""
    configured: bool
    """False for a forum name that has no ``ForumEntry`` (pure defaults)."""


@dcs.dataclass(frozen=True, kw_only=True)
class ForumDraft:
    """Validated input for creating or replacing a forum."""

    fname: str
    enabled: bool = True
    account_id: int | None = None
    sessions: tuple[str, ...] = ()
    operators: frozenset[str] = frozenset()
    whitelist: frozenset[str] = frozenset()
    overrides: ForumOverrides = ForumOverrides()


@dcs.dataclass(frozen=True, kw_only=True)
class AccountDraft:
    """Validated input for creating or updating an account.

    ``None`` credentials mean "keep the stored value" on update; on create both
    must be set (``core.validation`` enforces this).
    """

    name: str
    bduss: str | None = dcs.field(default=None, repr=False)
    stoken: str | None = dcs.field(default=None, repr=False)


@dcs.dataclass(frozen=True, kw_only=True)
class SettingsSnapshot:
    """Immutable view of all settings at one point in time."""

    global_: GlobalSettings
    forums: tuple[ForumEntry, ...] = ()
    accounts: tuple[Account, ...] = ()
    revision: int = 0
    """Incremented on every successful save."""


DEFAULT_RULES = (
    "1. 广告与引流：推广商品、代练代充、刷粮、留微信 / QQ / 群号 / 外站链接或二维码引导私下联系，均属违规。\n"
    "2. 色情与擦边：露骨的性描写、色情图片、以性暗示为卖点的擦边图文、招嫖或色情网站引流，均属违规。\n"
    "3. 辱骂与引战：人身攻击、辱骂其他用户或群体、地域黑、故意挑起对立的钓鱼帖，均属违规。\n"
    "4. 违法内容：赌博、毒品、诈骗、贩卖个人信息、暴力恐怖、政治敏感等违反法律法规的内容，均属违规。\n"
    "5. 灌水与刷屏：无意义的重复内容、乱码、与本吧主题无关的刷屏，属于违规；正常的简短回复（如「顶」「谢谢楼主」）不算。\n"
    "6. 正常讨论、提问、吐槽、玩梗不算违规；拿不准时给较低的置信度。"
)


def default_global_settings() -> GlobalSettings:
    """Return the documented defaults, including the original v0.2 rule text."""
    return GlobalSettings(defaults=ForumOptions(rules=DEFAULT_RULES))


def resolve(snapshot: SettingsSnapshot, fname: str) -> ResolvedForum:
    """Resolve one forum without changing its snapshot or inherited settings.

    Args:
        snapshot: Immutable settings to use for the entire resolution.
        fname: Forum name, matched after stripping and Unicode case folding.

    Returns:
        Effective options with additive whitelist and capped report threshold.
        Unknown forums have no account, local permissions, or patrol eligibility.
    """
    name = fname.strip()
    key = name.casefold()
    forum = next(
        (entry for entry in snapshot.forums if entry.fname.strip().casefold() == key),
        None,
    )
    options = snapshot.global_.defaults
    if forum is not None:
        overrides = {
            field.name: value
            for field in dcs.fields(ForumOverrides)
            if (value := getattr(forum.overrides, field.name)) is not None
        }
        options = dcs.replace(
            options, **overrides, whitelist=options.whitelist | forum.whitelist
        )
    options = dcs.replace(
        options,
        report_threshold=min(options.report_threshold, options.auto_delete_threshold),
    )
    return ResolvedForum(
        fname=forum.fname if forum is not None else name,
        enabled=forum.enabled if forum is not None else False,
        account_id=forum.account_id if forum is not None else None,
        options=options,
        sessions=(forum.sessions if forum is not None else ())
        or snapshot.global_.sessions,
        operators=forum.operators if forum is not None else frozenset(),
        configured=forum is not None,
    )


def global_to_dict(value: GlobalSettings) -> dict[str, object]:
    """Encode global settings as JSON-compatible values with stable set order."""
    data = dcs.asdict(value)
    data["sessions"] = list(value.sessions)
    data["operators"] = sorted(value.operators)
    data["defaults"]["whitelist"] = sorted(value.defaults.whitelist)
    return data


def global_from_dict(data: dict[str, object]) -> GlobalSettings:
    """Restore immutable global settings from their trusted stored JSON shape."""
    values = dict(data)
    defaults = dict(values["defaults"])
    defaults["whitelist"] = frozenset(defaults["whitelist"])
    values["defaults"] = ForumOptions(**defaults)
    values["sessions"] = tuple(values["sessions"])
    values["operators"] = frozenset(values["operators"])
    return GlobalSettings(**values)


def forum_data_to_dict(value: ForumDraft | ForumEntry) -> dict[str, object]:
    """Encode a forum's JSON column, excluding its separately stored identity."""
    return {
        "enabled": value.enabled,
        "account_id": value.account_id,
        "sessions": list(value.sessions),
        "operators": sorted(value.operators),
        "whitelist": sorted(value.whitelist),
        "overrides": dcs.asdict(value.overrides),
    }


def forum_data_from_dict(data: dict[str, object]) -> dict[str, object]:
    """Restore typed forum constructor kwargs, excluding identity and timestamps."""
    return {
        "enabled": data["enabled"],
        "account_id": data["account_id"],
        "sessions": tuple(data["sessions"]),
        "operators": frozenset(data["operators"]),
        "whitelist": frozenset(data["whitelist"]),
        "overrides": ForumOverrides(**data["overrides"]),
    }
