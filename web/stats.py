"""Dashboard statistics aggregation and bounded forum case queries."""

from __future__ import annotations

from typing import Any

from ..core.judge import FAILED_CATEGORY
from ..core.models import CaseStatus
from ..core.store import Store

MAX_STATS_DAYS = 90
DEFAULT_STATS_DAYS = 30
STATS_PAGE_LIMIT = 100
STATS_MAX_CASES = 20_000
"""Bound one single-forum statistics scan to keep the request cheap."""

CASE_STATUSES = {status.value: status for status in CaseStatus}
_BUCKETS = 10


def _bucket_index(confidence: float) -> int:
    """Map a confidence to its 0.1-wide bucket.

    Args:
        confidence: Confidence in ``[0, 1]``.

    Returns:
        The bucket index, 0 to 9. The epsilon keeps values such as 0.7 (stored
        as 6.999999999999999 when multiplied) in the expected bucket.
    """
    return min(_BUCKETS - 1, max(0, int(confidence * _BUCKETS + 1e-9)))


def _forum_stats_rows(
    store: Store, fname_key: str, since: int
) -> list[tuple[str, CaseStatus, float]]:
    """Read statistics rows for one forum.

    ``Store.case_stats`` aggregates over every forum and has no forum filter, so
    a single-forum view pages through ``query_cases`` instead. Pages are bounded
    by ``STATS_PAGE_LIMIT`` and the loop stops at ``STATS_MAX_CASES``, so a huge
    history cannot turn one statistics request into an unbounded scan.

    Args:
        store: Case persistence.
        fname_key: Casefolded forum key, matching ``query_cases(fnames=...)``.
        since: Inclusive Unix timestamp lower bound on ``created_at``.

    Returns:
        Category, status and confidence for matching cases, newest first.
    """
    rows: list[tuple[str, CaseStatus, float]] = []
    offset = 0
    while len(rows) < STATS_MAX_CASES:
        _, page = store.query_cases(
            fnames=(fname_key,), offset=offset, limit=STATS_PAGE_LIMIT
        )
        if not page:
            break
        for case in page:
            if case.created_at >= since:
                rows.append(
                    (case.verdict.category, case.status, case.verdict.confidence)
                )
        if len(page) < STATS_PAGE_LIMIT:
            break
        offset += STATS_PAGE_LIMIT
    return rows


def _stats_payload(
    rows: list[tuple[str, CaseStatus, float]], since: int
) -> dict[str, Any]:
    """Aggregate raw case rows into the statistics payload.

    Args:
        rows: ``(category, status, confidence)`` triples from the store.
        since: Unix seconds the statistics start at.

    Returns:
        The ``GET stats`` payload. Cases whose category is ``审查失败`` are
        counted in ``review_failed`` and excluded from ``by_category`` and the
        confidence buckets, because they carry no judge confidence at all.
    """
    status_counts = {status.value: 0 for status in CaseStatus}
    categories: dict[str, dict[str, Any]] = {}
    buckets = [
        {
            "low": index / _BUCKETS,
            "high": (index + 1) / _BUCKETS,
            "total": 0,
            "confirmed": 0,
            "false_positive": 0,
        }
        for index in range(_BUCKETS)
    ]
    review_failed = 0
    for category, status, confidence in rows:
        status_counts[status.value] += 1
        if category == FAILED_CATEGORY:
            review_failed += 1
            continue
        entry = categories.get(category)
        if entry is None:
            entry = {"category": category, "total": 0}
            entry.update(dict.fromkeys(CASE_STATUSES, 0))
            categories[category] = entry
        entry["total"] += 1
        entry[status.value] += 1
        bucket = buckets[_bucket_index(confidence)]
        bucket["total"] += 1
        if status in (CaseStatus.DELETED, CaseStatus.BANNED, CaseStatus.AUTO_DELETED):
            bucket["confirmed"] += 1
        elif status in (CaseStatus.IGNORED, CaseStatus.RESTORED):
            bucket["false_positive"] += 1
    return {
        "since": since,
        "total": len(rows),
        "by_status": status_counts,
        "by_category": sorted(
            categories.values(), key=lambda row: (-row["total"], row["category"])
        ),
        "confidence_buckets": buckets,
        "review_failed": review_failed,
    }
