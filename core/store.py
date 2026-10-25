"""Synchronous SQLite persistence for patrol markers and moderation cases.

Marker expiry uses the last local write time, not Tieba's thread activity time
or reply count. Purging never deletes cases. Each mutation commits before it
returns so independent Store connections immediately observe completed writes.
"""

from __future__ import annotations

import dataclasses as dcs
import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from .models import Author, Case, CaseStatus, ContentItem, ItemKind, Verdict


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
            """
        )

    def close(self) -> None:
        """Close the database connection and release its resources."""
        self._connection.close()

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
