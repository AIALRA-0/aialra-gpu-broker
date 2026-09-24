from __future__ import annotations

import json
import sqlite3

from gpu_broker.owner_status import read_owner_status


GPU = "GPU-status-test"
SOURCES = ("h3", "live", "manga", "gpu")


def unknown_evidence():
    return {
        source: {
            "state": "UNKNOWN",
            "fresh": False,
            "reason": "MISSING",
            "observed_at": None,
            "age_seconds": None,
        }
        for source in SOURCES
    }


def test_owner_status_is_unknown_without_database_and_reads_owner_only_schema(tmp_path, monkeypatch):
    database = tmp_path / "owner.sqlite3"
    monkeypatch.setattr("gpu_broker.owner_status.time.time", lambda: 200.0)

    absent = read_owner_status(database, GPU)

    assert absent == {
        "configured": False,
        "state": "UNKNOWN",
        "stale": False,
        "owner_project": None,
        "acquired_at": None,
        "last_observed_at": None,
        "evidence_max_age_seconds": 10.0,
        "evidence_sources": unknown_evidence(),
    }
    assert not database.exists()

    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE owner_state ("
            "gpu_uuid TEXT PRIMARY KEY, state TEXT NOT NULL, owner_project TEXT, "
            "owner_instance TEXT, acquired_at REAL, last_observed_at REAL)"
        )
        connection.execute(
            "INSERT INTO owner_state VALUES (?, 'OWNED', 'h3', ?, 190.5, 195.25)",
            (GPU, "private-owner-instance"),
        )

    status = read_owner_status(database, GPU)

    assert status == {
        "configured": True,
        "state": "OWNED",
        "stale": False,
        "owner_project": "h3",
        "acquired_at": 190.5,
        "last_observed_at": 195.25,
        "evidence_max_age_seconds": 10.0,
        "evidence_sources": unknown_evidence(),
    }
    assert "owner_instance" not in status
    assert "private-owner-instance" not in json.dumps(status)
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        state = connection.execute(
            "SELECT state FROM owner_state WHERE gpu_uuid = ?", (GPU,)
        ).fetchone()[0]
    assert tables == {"owner_state"}
    assert state == "OWNED"


def test_expired_free_snapshot_projects_unknown_and_recent_free_remains_free(tmp_path, monkeypatch):
    database = tmp_path / "owner.sqlite3"
    now = 200.0
    monkeypatch.setattr("gpu_broker.owner_status.time.time", lambda: now)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE owner_state ("
            "gpu_uuid TEXT PRIMARY KEY, state TEXT NOT NULL, owner_project TEXT, "
            "owner_instance TEXT, acquired_at REAL, last_observed_at REAL)"
        )
        connection.execute(
            "INSERT INTO owner_state VALUES (?, 'FREE', NULL, NULL, NULL, ?)",
            (GPU, now - 10.01),
        )
        connection.execute(
            "INSERT INTO owner_state VALUES (?, 'FREE', NULL, NULL, NULL, ?)",
            ("GPU-recent", now - 9.99),
        )

    status = read_owner_status(database, GPU)
    recent = read_owner_status(database, "GPU-recent")

    assert status["configured"] is True
    assert status["state"] == "UNKNOWN"
    assert status["stale"] is True
    assert status["owner_project"] is None
    assert status["last_observed_at"] == now - 10.01
    assert recent["state"] == "FREE"
    assert recent["stale"] is False
    assert status["evidence_sources"] == unknown_evidence()


def test_expired_owned_snapshot_keeps_project_as_unconfirmed_history(tmp_path, monkeypatch):
    database = tmp_path / "owner.sqlite3"
    now = 200.0
    monkeypatch.setattr("gpu_broker.owner_status.time.time", lambda: now)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE owner_state ("
            "gpu_uuid TEXT PRIMARY KEY, state TEXT NOT NULL, owner_project TEXT, "
            "owner_instance TEXT, acquired_at REAL, last_observed_at REAL)"
        )
        connection.execute(
            "INSERT INTO owner_state VALUES (?, 'OWNED', 'h3', ?, ?, ?)",
            (GPU, "private-owner-instance", now - 120, now - 30),
        )

    status = read_owner_status(database, GPU)

    assert status["state"] == "UNKNOWN"
    assert status["stale"] is True
    assert status["owner_project"] == "h3"
    assert status["acquired_at"] is None


def test_owner_status_reports_per_source_evidence_freshness_without_inference(
    tmp_path, monkeypatch,
):
    database = tmp_path / "owner.sqlite3"
    now = 200.0
    monkeypatch.setattr("gpu_broker.owner_status.time.time", lambda: now)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE owner_state ("
            "gpu_uuid TEXT PRIMARY KEY, state TEXT NOT NULL, owner_project TEXT, "
            "owner_instance TEXT, acquired_at REAL, last_observed_at REAL)"
        )
        connection.execute(
            "CREATE TABLE owner_evidence_versions ("
            "gpu_uuid TEXT NOT NULL, source TEXT NOT NULL, observed_at REAL NOT NULL, "
            "PRIMARY KEY (gpu_uuid, source))"
        )
        connection.execute(
            "INSERT INTO owner_state VALUES (?, 'OWNED', 'h3', ?, ?, ?) ",
            (GPU, "private-owner-instance", now - 5, now - 1),
        )
        connection.executemany(
            "INSERT INTO owner_evidence_versions VALUES (?, ?, ?)",
            [
                (GPU, "h3", now - 2),
                (GPU, "live", now - 11),
                # No manga row: missing evidence must remain UNKNOWN.
                (GPU, "gpu", now + 1),
            ],
        )

    status = read_owner_status(database, GPU)

    assert status["state"] == "OWNED"
    assert status["owner_project"] == "h3"
    assert status["evidence_sources"] == {
        "h3": {
            "state": "FRESH", "fresh": True, "reason": "FRESH",
            "observed_at": now - 2, "age_seconds": 2,
        },
        "live": {
            "state": "UNKNOWN", "fresh": False, "reason": "EXPIRED",
            "observed_at": now - 11, "age_seconds": 11,
        },
        "manga": unknown_evidence()["manga"],
        "gpu": {
            "state": "UNKNOWN", "fresh": False, "reason": "INVALID_TIMESTAMP",
            "observed_at": now + 1, "age_seconds": None,
        },
    }

    # Projection is read-only: it adds no status or evidence rows.
    with sqlite3.connect(database) as connection:
        sources = {
            row[0] for row in connection.execute(
                "SELECT source FROM owner_evidence_versions WHERE gpu_uuid = ?", (GPU,)
            )
        }
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert sources == {"h3", "live", "gpu"}
    assert tables == {"owner_state", "owner_evidence_versions"}


def test_owner_state_and_source_timestamps_come_from_one_sqlite_snapshot(
    tmp_path, monkeypatch,
):
    from gpu_broker import owner_status

    database = tmp_path / "owner.sqlite3"
    now = 200.0
    monkeypatch.setattr(owner_status.time, "time", lambda: now)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE owner_state ("
            "gpu_uuid TEXT PRIMARY KEY, state TEXT NOT NULL, owner_project TEXT, "
            "owner_instance TEXT, acquired_at REAL, last_observed_at REAL)"
        )
        connection.execute(
            "CREATE TABLE owner_evidence_versions ("
            "gpu_uuid TEXT NOT NULL, source TEXT NOT NULL, observed_at REAL NOT NULL, "
            "PRIMARY KEY (gpu_uuid, source))"
        )
        connection.execute(
            "INSERT INTO owner_state VALUES (?, 'FREE', NULL, NULL, NULL, ?)",
            (GPU, now - 2),
        )
        connection.executemany(
            "INSERT INTO owner_evidence_versions VALUES (?, ?, ?)",
            [(GPU, source, now - 2) for source in SOURCES],
        )

    real_connect = sqlite3.connect

    class ReadConnectionProxy:
        def __init__(self, connection):
            self.connection = connection
            self.updated = False

        def execute(self, sql, parameters=()):
            cursor = self.connection.execute(sql, parameters)
            if "FROM owner_state" in sql and not self.updated:
                self.updated = True
                # Simulate the Owner poller committing a newer H3 sample after
                # state was read but before the evidence query starts.
                with real_connect(database) as writer:
                    writer.execute(
                        "UPDATE owner_evidence_versions SET observed_at = ? "
                        "WHERE gpu_uuid = ? AND source = 'h3'",
                        (now - 1, GPU),
                    )
            return cursor

        def close(self):
            self.connection.close()

        def __setattr__(self, name, value):
            if name in {"connection", "updated"}:
                object.__setattr__(self, name, value)
            else:
                setattr(self.connection, name, value)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        owner_status.sqlite3,
        "connect",
        lambda *args, **kwargs: ReadConnectionProxy(real_connect(*args, **kwargs)),
    )

    status = owner_status.read_owner_status(database, GPU)

    # The read transaction sees the pre-commit version for both queries. A
    # later dashboard request will pick up the newer sample consistently.
    assert status["last_observed_at"] == now - 2
    assert status["evidence_sources"]["h3"]["observed_at"] == now - 2
