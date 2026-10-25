"""The plugin-view route table, kept apart from the plugin lifecycle.

``main.py`` registers these with ``context.register_web_api``; keeping the table
here holds the entry module within its size budget and makes the full route
surface reviewable in one place.

Every route is matched by ``astrbot/dashboard/api/plugins.py:196-222``, which
passes each ``<name>`` segment to the handler as a keyword argument, and is
gated by ``require_plugin_scope`` (``:398-438``) plus the dashboard JWT check in
``astrbot/dashboard/api/auth.py:167-195``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def route_table(web: Any, settings: Any) -> Iterable[tuple[str, Any, list[str], str]]:
    """Describe every plugin-view route.

    Args:
        web: The case, patrol and statistics handler object.
        settings: The settings handler object.

    Returns:
        ``(path, handler, methods, description)`` tuples, without the plugin
        prefix; the caller prepends ``/{plugin_name}/``.
    """
    return (
        ("status", web.get_status, ["GET"], "Patrol status"),
        ("cases", web.get_cases, ["GET"], "List cases"),
        ("cases/<case_id>", web.get_case, ["GET"], "Get one case"),
        (
            "cases/<case_id>/images/<index>",
            web.get_case_image,
            ["GET"],
            "Get one case image",
        ),
        ("cases/<case_id>/delete", web.post_delete, ["POST"], "Delete case content"),
        ("cases/<case_id>/ban", web.post_ban, ["POST"], "Delete and ban"),
        ("cases/<case_id>/ignore", web.post_ignore, ["POST"], "Ignore case"),
        ("cases/<case_id>/restore", web.post_restore, ["POST"], "Restore content"),
        ("stats", web.get_stats, ["GET"], "Case statistics"),
        ("reports", web.get_reports, ["GET"], "Patrol reports"),
        ("patrol/run", web.post_patrol_run, ["POST"], "Start a patrol round"),
        ("check", web.post_check, ["POST"], "Trial review of a thread"),
        ("settings", settings.get_settings, ["GET"], "All settings"),
        ("settings/global", settings.post_global, ["POST"], "Save global settings"),
        ("forums", settings.post_forum_create, ["POST"], "Create a forum"),
        ("forums/<forum_id>", settings.post_forum_update, ["POST"], "Replace forum"),
        (
            "forums/<forum_id>/delete",
            settings.post_forum_delete,
            ["POST"],
            "Delete forum",
        ),
        ("accounts", settings.post_account_create, ["POST"], "Create an account"),
        (
            "accounts/<account_id>",
            settings.post_account_update,
            ["POST"],
            "Replace account",
        ),
        (
            "accounts/<account_id>/delete",
            settings.post_account_delete,
            ["POST"],
            "Delete account",
        ),
        (
            "accounts/<account_id>/check",
            settings.post_account_check,
            ["POST"],
            "Check account",
        ),
        ("providers", settings.get_providers, ["GET"], "Chat providers"),
    )
