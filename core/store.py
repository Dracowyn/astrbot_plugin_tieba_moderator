"""Synchronous SQLite persistence for patrol markers and moderation cases.

Marker expiry uses the last local write time, not Tieba's thread activity time
or reply count. Purging never deletes cases. Each mutation commits before it
returns so independent Store connections immediately observe completed writes.
WebUI JSON filtering requires SQLite JSON1 (built in since SQLite 3.38).
Reports are retained in save order; insertion and retention pruning are atomic.
"""

from __future__ import annotations

import dataclasses as dcs
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from .models import (
    Author,
    Case,
    CaseStatus,
    ContentItem,
    ItemKind,
    PatrolReport,
    Verdict,
)
from .settings import (
    Account,
    AccountDraft,
    ForumDraft,
    ForumEntry,
    GlobalSettings,
    SettingsSnapshot,
    default_global_settings,
    forum_data_from_dict,
    forum_data_to_dict,
    global_from_dict,
    global_to_dict,
)


class Store:
    """Persist patrol progress and cases using a connection owned by this thread."""

    def __init__(
        self, db_path: Path, *, clock: Callable[[], float] = time.time
    ) -> None:
        """Open a WAL database and initialize its tables if necessary.

        Args:
            db_path: Database file in an existing directory.
            clock: Clock returning Unix seconds for marker and failure expiry.
        """
        self._clock = clock
        self._connection = sqlite3.connect(db_path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL").fetchone()
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS thread_marks (
                tid INTEGER PRIMARY KEY,
                last_time INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS post_marks (
                pid INTEGER PRIMARY KEY,
                reply_num INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seen (
                key TEXT PRIMARY KEY,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS failures (
                key TEXT PRIMARY KEY,
                count INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL,
                verdict TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                handled_by TEXT NOT NULL DEFAULT '',
                handled_at INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS cases_status_id ON cases (status, id);
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at INTEGER NOT NULL,
                finished_at INTEGER NOT NULL,
                forums TEXT NOT NULL,
                reviewed INTEGER NOT NULL,
                reported INTEGER NOT NULL,
                auto_deleted INTEGER NOT NULL,
                skipped INTEGER NOT NULL,
                deferred INTEGER NOT NULL,
                errors TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings_global (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                data TEXT NOT NULL,
                revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                bduss TEXT NOT NULL,
                stoken TEXT NOT NULL,
                created_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS forums (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fname TEXT NOT NULL,
                fname_key TEXT NOT NULL UNIQUE,
                data TEXT NOT NULL,
                created_at INTEGER,
                updated_at INTEGER
            );
            """
        )

    def close(self) -> None:
        """Close the database connection and release its resources."""
        self._connection.close()

    def load_settings(self) -> SettingsSnapshot:
        """Read all settings in one transaction, defaulting an absent global row.

        Returns:
            Immutable settings from a single SQLite read snapshot. Reading an
            empty database does not insert defaults or advance its revision.
        """
        with self._connection:
            self._connection.execute("BEGIN")
            global_row = self._connection.execute(
                "SELECT data, revision FROM settings_global WHERE id = 1"
            ).fetchone()
            accounts = self._connection.execute(
                "SELECT * FROM accounts ORDER BY id"
            ).fetchall()
            forums = self._connection.execute(
                "SELECT * FROM forums ORDER BY id"
            ).fetchall()
        return SettingsSnapshot(
            global_=(
                global_from_dict(json.loads(global_row["data"]))
                if global_row is not None
                else default_global_settings()
            ),
            revision=global_row["revision"] if global_row is not None else 0,
            accounts=tuple(Account(**dict(row)) for row in accounts),
            forums=tuple(_forum_from_row(row) for row in forums),
        )

    def save_global(self, value: GlobalSettings) -> int:
        """Replace global settings atomically and return the new revision."""
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO settings_global (id, data, revision) VALUES (1, ?, 1)
                ON CONFLICT(id) DO UPDATE SET
                    data = excluded.data, revision = settings_global.revision + 1
                """,
                (json.dumps(global_to_dict(value), ensure_ascii=False),),
            )
            revision = self._connection.execute(
                "SELECT revision FROM settings_global WHERE id = 1"
            ).fetchone()[0]
        return revision

    def _bump_revision(self) -> None:
        """Advance settings revision inside the caller's mutation transaction."""
        self._connection.execute(
            """
            INSERT INTO settings_global (id, data, revision) VALUES (1, ?, 1)
            ON CONFLICT(id) DO UPDATE SET revision = settings_global.revision + 1
            """,
            (
                json.dumps(
                    global_to_dict(default_global_settings()), ensure_ascii=False
                ),
            ),
        )

    def insert_account(self, draft: AccountDraft) -> Account:
        """Insert a validated account and advance revision in the same commit."""
        now = int(self._clock())
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO accounts (name, bduss, stoken, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (draft.name, draft.bduss, draft.stoken, now, now),
            )
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            self._bump_revision()
        return Account(**dict(row))

    def update_account(self, account_id: int, draft: AccountDraft) -> Account | None:
        """Replace an account's name and supplied credentials, or return None.

        Args:
            account_id: Existing account identity.
            draft: Validated account; missing or empty credentials keep old ones.

        Returns:
            Updated immutable account, or None without a revision for missing IDs.
        """
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE accounts SET name = ?, bduss = COALESCE(NULLIF(?, ''), bduss),
                    stoken = COALESCE(NULLIF(?, ''), stoken), updated_at = ?
                WHERE id = ?
                """,
                (draft.name, draft.bduss, draft.stoken, int(self._clock()), account_id),
            )
            if cursor.rowcount == 0:
                return None
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            self._bump_revision()
        return Account(**dict(row))

    def delete_account(self, account_id: int) -> bool:
        """Delete an unreferenced account; service callers enforce references."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM accounts WHERE id = ?", (account_id,)
            )
            if cursor.rowcount == 0:
                return False
            self._bump_revision()
        return True

    def insert_forum(self, draft: ForumDraft) -> ForumEntry:
        """Insert a validated forum with a unique, stripped casefolded name key."""
        name = draft.fname.strip()
        now = int(self._clock())
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO forums (fname, fname_key, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name,
                    name.casefold(),
                    json.dumps(forum_data_to_dict(draft), ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM forums WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            self._bump_revision()
        return _forum_from_row(row)

    def update_forum(self, forum_id: int, draft: ForumDraft) -> ForumEntry | None:
        """Replace a forum's editable fields, keeping its identity and creation time."""
        name = draft.fname.strip()
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE forums SET fname = ?, fname_key = ?, data = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    name.casefold(),
                    json.dumps(forum_data_to_dict(draft), ensure_ascii=False),
                    int(self._clock()),
                    forum_id,
                ),
            )
            if cursor.rowcount == 0:
                return None
            row = self._connection.execute(
                "SELECT * FROM forums WHERE id = ?", (forum_id,)
            ).fetchone()
            self._bump_revision()
        return _forum_from_row(row)

    def delete_forum(self, forum_id: int) -> bool:
        """Delete a forum and advance revision only when the identity exists."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM forums WHERE id = ?", (forum_id,)
            )
            if cursor.rowcount == 0:
                return False
            self._bump_revision()
        return True

    def get_thread_mark(self, tid: int) -> int | None:
        """Read the last processed thread activity timestamp.

        Args:
            tid: Tieba thread ID.

        Returns:
            The saved last_time, or None if the thread is unmarked.
        """
        row = self._connection.execute(
            "SELECT last_time FROM thread_marks WHERE tid = ?", (tid,)
        ).fetchone()
        return row[0] if row is not None else None

    def set_thread_mark(self, tid: int, last_time: int) -> None:
        """Insert or refresh a thread marker and its local expiry timestamp.

        Args:
            tid: Tieba thread ID.
            last_time: Most recently processed thread activity timestamp.
        """
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO thread_marks (tid, last_time, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(tid) DO UPDATE SET
                    last_time = excluded.last_time,
                    updated_at = excluded.updated_at
                """,
                (tid, last_time, int(self._clock())),
            )

    def get_post_mark(self, pid: int) -> int | None:
        """Read the last processed comment count for a floor.

        Args:
            pid: Tieba floor ID.

        Returns:
            The saved reply_num, or None if the floor is unmarked.
        """
        row = self._connection.execute(
            "SELECT reply_num FROM post_marks WHERE pid = ?", (pid,)
        ).fetchone()
        return row[0] if row is not None else None

    def set_post_mark(self, pid: int, reply_num: int) -> None:
        """Insert or refresh a floor marker and its local expiry timestamp.

        Args:
            pid: Tieba floor ID.
            reply_num: Most recently processed comment count.
        """
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO post_marks (pid, reply_num, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(pid) DO UPDATE SET
                    reply_num = excluded.reply_num,
                    updated_at = excluded.updated_at
                """,
                (pid, reply_num, int(self._clock())),
            )

    def is_seen(self, key: str) -> bool:
        """Check whether a content identity has already been processed.

        Args:
            key: ContentItem.key to look up.

        Returns:
            Whether a seen marker exists.
        """
        return (
            self._connection.execute(
                "SELECT 1 FROM seen WHERE key = ?", (key,)
            ).fetchone()
            is not None
        )

    def mark_seen(self, key: str) -> None:
        """Mark content as processed, refreshing an existing marker's age.

        Args:
            key: ContentItem.key to mark.
        """
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO seen (key, updated_at) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (key, int(self._clock())),
            )

    def record_failure(self, key: str) -> int:
        """Atomically increment a content identity's failure count.

        Args:
            key: ContentItem.key whose review failed.

        Returns:
            Cumulative failures since the record was created or last purged.
        """
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO failures (key, count, updated_at) VALUES (?, 1, ?)
                ON CONFLICT(key) DO UPDATE SET
                    count = failures.count + 1,
                    updated_at = excluded.updated_at
                """,
                (key, int(self._clock())),
            )
            row = self._connection.execute(
                "SELECT count FROM failures WHERE key = ?", (key,)
            ).fetchone()
        return row[0]

    def purge(self, older_than: int) -> None:
        """Delete expired markers and failures without deleting any cases.

        Args:
            older_than: Exclusive Unix timestamp cutoff for last local writes.
        """
        with self._connection:
            for table in ("thread_marks", "post_marks", "seen", "failures"):
                self._connection.execute(
                    f"DELETE FROM {table} WHERE updated_at < ?", (older_than,)
                )

    def create_case(
        self, item: ContentItem, verdict: Verdict, status: CaseStatus, now: int
    ) -> Case:
        """Persist a new case with immutable JSON snapshots of its evidence.

        Args:
            item: Reviewed content and its author.
            verdict: Judge's result.
            status: Initial case status.
            now: Case creation time in Unix seconds.

        Returns:
            The new case with its generated ID and empty handling metadata.
        """
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO cases (item, verdict, status, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    json.dumps(dcs.asdict(item), ensure_ascii=False),
                    json.dumps(dcs.asdict(verdict), ensure_ascii=False),
                    status.value,
                    now,
                ),
            )
        return Case(
            id=cursor.lastrowid,
            item=item,
            verdict=verdict,
            status=status,
            created_at=now,
        )

    def get_case(self, case_id: int) -> Case | None:
        """Read a case and restore its frozen nested models.

        Args:
            case_id: Generated case ID.

        Returns:
            The case, or None if no such ID exists.
        """
        row = self._connection.execute(
            "SELECT * FROM cases WHERE id = ?", (case_id,)
        ).fetchone()
        return _case_from_row(row) if row is not None else None

    def list_cases(self, status: CaseStatus, limit: int) -> list[Case]:
        """List matching cases in descending ID order, newest first.

        Args:
            status: Exact status to include.
            limit: Maximum number of cases; nonpositive values return no cases.

        Returns:
            Matching cases, bounded by limit.
        """
        if limit <= 0:
            return []
        rows = self._connection.execute(
            "SELECT * FROM cases WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status.value, limit),
        ).fetchall()
        return [_case_from_row(row) for row in rows]

    def query_cases(
        self,
        *,
        statuses: tuple[CaseStatus, ...] = (),
        fname: str = "",
        fnames: tuple[str, ...] = (),
        category: str = "",
        kind: ItemKind | None = None,
        q: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[int, list[Case]]:
        """Query cases using SQLite JSON1 fields and literal substring matching.

        Requires SQLite's json_extract function, supplied by JSON1 and built in
        since SQLite 3.38. LIKE uses SQLite's default case-matching semantics;
        percent signs, underscores and backslashes in q are literal characters.

        Args:
            statuses: Allowed statuses; empty means no status restriction.
            fname: Exact forum name; empty disables this filter.
            fnames: Allowed forum keys, already stripped and casefolded by the
                caller; empty disables this filter. SQLite lower() folds ASCII
                only. Combined with all other filters, including exact fname.
            category: Exact verdict category; empty disables this filter.
            kind: Exact content kind, or None for all kinds.
            q: Substring of the title, body, author nickname or login name.
            offset: Number of matching cases to skip, clamped to at least zero.
            limit: Page size, clamped to the inclusive range 1 through 100.

        Returns:
            Total matching cases before pagination and the page in descending
            case-ID order, restored as frozen nested models.
        """
        clauses: list[str] = []
        parameters: list[str] = []
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            parameters.extend(status.value for status in statuses)
        if fnames:
            placeholders = ", ".join("?" for _ in fnames)
            clauses.append(f"lower(json_extract(item, '$.fname')) IN ({placeholders})")
            parameters.extend(fnames)
        for expression, value in (
            ("json_extract(item, '$.fname')", fname),
            ("json_extract(verdict, '$.category')", category),
            ("json_extract(item, '$.kind')", kind.value if kind is not None else None),
        ):
            if value:
                clauses.append(f"{expression} = ?")
                parameters.append(value)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            paths = ("$.title", "$.text", "$.author.nick_name", "$.author.user_name")
            clauses.append(
                "("
                + " OR ".join(
                    f"json_extract(item, '{path}') LIKE ? ESCAPE '\\'" for path in paths
                )
                + ")"
            )
            parameters.extend([f"%{escaped}%"] * len(paths))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = self._connection.execute(
            "SELECT COUNT(*) FROM cases" + where, parameters
        ).fetchone()[0]
        rows = self._connection.execute(
            "SELECT * FROM cases" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*parameters, max(1, min(100, limit)), max(0, offset)),
        ).fetchall()
        return total, [_case_from_row(row) for row in rows]

    def case_stats(self, since: int) -> list[tuple[str, CaseStatus, float]]:
        """Read raw statistics rows for the inclusive case creation-time window.

        Requires SQLite JSON1's json_extract function. Aggregation, including
        handling of review-failure categories, belongs to the WebUI API layer.

        Args:
            since: Inclusive Unix timestamp lower bound on created_at.

        Returns:
            Category, current status and confidence for each matching case.
        """
        rows = self._connection.execute(
            """
            SELECT json_extract(verdict, '$.category'), status,
                   json_extract(verdict, '$.confidence')
            FROM cases WHERE created_at >= ? ORDER BY id
            """,
            (since,),
        ).fetchall()
        return [(row[0], CaseStatus(row[1]), float(row[2])) for row in rows]

    def save_report(self, report: PatrolReport) -> None:
        """Persist a report and atomically retain the 500 most recently saved.

        Args:
            report: Immutable report; forum and error tuples are stored as JSON.
        """
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO reports (
                    started_at, finished_at, forums, reviewed, reported,
                    auto_deleted, skipped, deferred, errors
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.started_at,
                    report.finished_at,
                    json.dumps(report.forums, ensure_ascii=False),
                    report.reviewed,
                    report.reported,
                    report.auto_deleted,
                    report.skipped,
                    report.deferred,
                    json.dumps(report.errors, ensure_ascii=False),
                ),
            )
            self._connection.execute(
                """
                DELETE FROM reports WHERE id NOT IN (
                    SELECT id FROM reports ORDER BY id DESC LIMIT ?
                )
                """,
                (500,),
            )

    def list_reports(self, limit: int) -> list[PatrolReport]:
        """Read saved reports, newest saves first, including timestamp ties.

        Args:
            limit: Maximum reports to return; nonpositive values return none.

        Returns:
            Frozen reports with forum and error tuples restored from JSON.
        """
        if limit <= 0:
            return []
        rows = self._connection.execute(
            "SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            PatrolReport(
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                forums=tuple(json.loads(row["forums"])),
                reviewed=row["reviewed"],
                reported=row["reported"],
                auto_deleted=row["auto_deleted"],
                skipped=row["skipped"],
                deferred=row["deferred"],
                errors=tuple(json.loads(row["errors"])),
            )
            for row in rows
        ]

    def count_cases(self, status: CaseStatus) -> int:
        """Count cases currently in the specified status.

        Args:
            status: Exact status to count.

        Returns:
            Number of matching cases.
        """
        return self._connection.execute(
            "SELECT COUNT(*) FROM cases WHERE status = ?", (status.value,)
        ).fetchone()[0]

    def transition(
        self,
        case_id: int,
        *,
        from_: tuple[CaseStatus, ...],
        to: CaseStatus,
        by: str,
        now: int,
        note: str = "",
    ) -> Case | None:
        """Atomically change a case only if its current status is allowed.

        Read back the winning update within the same transaction while its
        write lock is held. A status mismatch never overwrites handling metadata.

        Args:
            case_id: Generated case ID.
            from_: Allowed current statuses; an empty tuple matches nothing.
            to: New status.
            by: Operator identity.
            now: Handling time in Unix seconds.
            note: Replacement handling note, empty by default.

        Returns:
            The updated case, or None for a missing ID or a status mismatch
            (including a transition already won by another connection).
        """
        if not from_:
            return None
        placeholders = ", ".join("?" for _ in from_)
        with self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE cases SET status = ?, handled_by = ?, handled_at = ?, note = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (to.value, by, now, note, case_id, *(status.value for status in from_)),
            )
            if cursor.rowcount == 0:
                return None
            row = self._connection.execute(
                "SELECT * FROM cases WHERE id = ?", (case_id,)
            ).fetchone()
        return _case_from_row(row)


def _case_from_row(row: sqlite3.Row) -> Case:
    """Restore nested dataclasses, enums, and tuples from a stored case row."""
    item = json.loads(row["item"])
    item["kind"] = ItemKind(item["kind"])
    item["author"] = Author(**item["author"])
    item["image_urls"] = tuple(item["image_urls"])
    return Case(
        id=row["id"],
        item=ContentItem(**item),
        verdict=Verdict(**json.loads(row["verdict"])),
        status=CaseStatus(row["status"]),
        created_at=row["created_at"],
        handled_by=row["handled_by"],
        handled_at=row["handled_at"],
        note=row["note"],
    )


def _forum_from_row(row: sqlite3.Row) -> ForumEntry:
    """Restore a forum's identity, timestamps, and typed immutable JSON fields."""
    return ForumEntry(
        id=row["id"],
        fname=row["fname"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        **forum_data_from_dict(json.loads(row["data"])),
    )
