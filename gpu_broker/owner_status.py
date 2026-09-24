"""Read-only projection of the separate Owner v1 database for the dashboard."""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path


OWNER_MAX_OBSERVATION_AGE_SECONDS = 10.0
_OWNER_STATES = frozenset({"FREE", "OWNED", "UNKNOWN"})
_OWNER_PROJECTS = frozenset({"h3", "live", "manga"})
_EVIDENCE_SOURCES = ("h3", "live", "manga", "gpu")


def read_owner_status(database_path: Path, gpu_uuid: str) -> dict:
    """Return a sanitized status snapshot without opening the database for writes.

    A missing database or row means the state is UNKNOWN. Persisted state whose
    last observation is older than Owner's 10-second evidence window also
    projects as UNKNOWN. This function never constructs an OwnerCoordinator,
    gathers observations, or changes Owner state.
    """

    path = Path(database_path)
    result = {
        "configured": False,
        "state": "UNKNOWN",
        "stale": False,
        "owner_project": None,
        "acquired_at": None,
        "last_observed_at": None,
        "evidence_max_age_seconds": OWNER_MAX_OBSERVATION_AGE_SECONDS,
        "evidence_sources": _empty_evidence_sources(),
    }
    try:
        if not path.is_file():
            return result
        result["configured"] = True
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as connection:
            connection.row_factory = sqlite3.Row
            # Read the state and four evidence timestamps from one SQLite
            # snapshot. The Owner poller may commit another observation while
            # the dashboard is loading, but this response must not mix generations.
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT state, owner_project, acquired_at, last_observed_at "
                "FROM owner_state WHERE gpu_uuid = ?",
                (gpu_uuid,),
            ).fetchone()
            try:
                evidence_rows = connection.execute(
                    "SELECT source, observed_at FROM owner_evidence_versions "
                    "WHERE gpu_uuid = ?",
                    (gpu_uuid,),
                ).fetchall()
            except sqlite3.Error:
                # An older or incomplete Owner database provides no trustworthy
                # per-source evidence; keep those entries UNKNOWN.
                evidence_rows = []
    except (OSError, sqlite3.Error, ValueError):
        return result

    now = time.time()
    evidence_by_source = {
        evidence_row["source"]: _evidence_status(evidence_row["observed_at"], now)
        for evidence_row in evidence_rows
        if evidence_row["source"] in _EVIDENCE_SOURCES
    }
    result["evidence_sources"] = {
        source: evidence_by_source.get(source, _evidence_status(None, now))
        for source in _EVIDENCE_SOURCES
    }

    if row is None:
        return result

    state = row["state"]
    if state not in _OWNER_STATES:
        return result
    last_observed_at = _timestamp(row["last_observed_at"])
    result["last_observed_at"] = last_observed_at
    age = now - last_observed_at if last_observed_at is not None else None
    fresh = age is not None and 0 <= age <= OWNER_MAX_OBSERVATION_AGE_SECONDS
    result["stale"] = last_observed_at is not None and not fresh
    result["state"] = state if state == "UNKNOWN" or fresh else "UNKNOWN"
    if state == "OWNED" and row["owner_project"] in _OWNER_PROJECTS:
        result["owner_project"] = row["owner_project"]
        if result["state"] == "OWNED":
            result["acquired_at"] = _timestamp(row["acquired_at"])
    return result


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timestamp = float(value)
    return timestamp if math.isfinite(timestamp) else None


def _empty_evidence_sources() -> dict[str, dict]:
    return {source: _evidence_status(None, time.time()) for source in _EVIDENCE_SOURCES}


def _evidence_status(value: object, now: float) -> dict:
    observed_at = _timestamp(value)
    if observed_at is None:
        return {
            "state": "UNKNOWN",
            "fresh": False,
            "reason": "MISSING",
            "observed_at": None,
            "age_seconds": None,
        }
    age = now - observed_at
    if age < 0:
        return {
            "state": "UNKNOWN",
            "fresh": False,
            "reason": "INVALID_TIMESTAMP",
            "observed_at": observed_at,
            "age_seconds": None,
        }
    fresh = age <= OWNER_MAX_OBSERVATION_AGE_SECONDS
    return {
        "state": "FRESH" if fresh else "UNKNOWN",
        "fresh": fresh,
        "reason": "FRESH" if fresh else "EXPIRED",
        "observed_at": observed_at,
        "age_seconds": age,
    }
