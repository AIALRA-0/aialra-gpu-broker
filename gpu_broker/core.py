from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import PROJECTS, Settings
from .monitor import Monitor


def _id() -> str:
    return str(uuid.uuid4())


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _profile_row(row: sqlite3.Row) -> dict[str, Any]:
    profile = dict(row)
    profile["respect_vram"] = bool(profile["respect_vram"])
    return profile


class BrokerError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class InstanceLock:
    """A one-byte OS lock prevents a second server from scheduling the same DB."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another GPU Broker instance owns the data directory") from exc

    def release(self) -> None:
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


class Broker:
    def __init__(self, settings: Settings, monitor: Monitor):
        self.settings = settings
        self.monitor = monitor
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._instance_lock = InstanceLock(settings.lock_path)
        self._started = False
        self._snapshot: dict[str, Any] | None = None
        self.conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # One local writer and FULL rollback-journal commits avoid the documented
        # multi-connection WAL-reset bug in bundled, older Python SQLite builds.
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._schema()

    def _schema(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 3:
            raise RuntimeError(f"Database schema {version} is newer than this broker")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1, last_seen REAL,
                instance_id TEXT, reported_status TEXT, reported_resident_mib INTEGER NOT NULL DEFAULT 0,
                usage_seconds REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                label TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('realtime','batch')),
                peak_growth_mib INTEGER NOT NULL CHECK(peak_growth_mib > 0),
                max_seconds INTEGER NOT NULL CHECK(max_seconds > 0),
                respect_vram INTEGER NOT NULL DEFAULT 0 CHECK(respect_vram IN (0,1)),
                enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                external_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                label TEXT NOT NULL, status TEXT NOT NULL, backend_id TEXT,
                error_code TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(project_id, idempotency_key), UNIQUE(project_id, external_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                request_key TEXT NOT NULL, owner_instance TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'realtime',
                status TEXT NOT NULL, reason TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL, heartbeat_at REAL NOT NULL,
                UNIQUE(project_id, request_key)
            );
            CREATE TABLE IF NOT EXISTS permits (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                profile_id TEXT NOT NULL REFERENCES profiles(id),
                session_id TEXT REFERENCES sessions(id),
                request_key TEXT NOT NULL, stage TEXT NOT NULL,
                owner_instance TEXT NOT NULL, backend_id TEXT,
                status TEXT NOT NULL, reason TEXT, created_at REAL NOT NULL,
                granted_at REAL, heartbeat_at REAL, finished_at REAL,
                result TEXT, resident_mib INTEGER NOT NULL DEFAULT 0,
                UNIQUE(job_id, request_key)
            );
            CREATE INDEX IF NOT EXISTS permits_status_created ON permits(status, created_at);
            CREATE INDEX IF NOT EXISTS sessions_status_created ON sessions(status, created_at);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
                project_id TEXT, job_id TEXT, permit_id TEXT, session_id TEXT,
                detail TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                gpu_uuid TEXT NOT NULL, used_mib INTEGER NOT NULL,
                total_mib INTEGER NOT NULL, utilization_pct INTEGER NOT NULL,
                temperature_c REAL
            );
            CREATE INDEX IF NOT EXISTS samples_gpu_ts ON samples(gpu_uuid, ts);
            """
        )
        if "kind" not in {row[1] for row in self.conn.execute("PRAGMA table_info(sessions)")}:
            self.conn.execute("ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'realtime'")
        if "respect_vram" not in {row[1] for row in self.conn.execute("PRAGMA table_info(profiles)")}:
            self.conn.execute(
                "ALTER TABLE profiles ADD COLUMN respect_vram INTEGER NOT NULL DEFAULT 0 "
                "CHECK(respect_vram IN (0,1))"
            )
        with self.transaction():
            for project, label in PROJECTS.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO projects(id,label) VALUES (?,?)", (project, label)
                )
            self.conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('allocation_enabled','0')")
            self.conn.execute("PRAGMA user_version=3")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                # SQLite can cancel a transaction itself after a fatal I/O or
                # disk-full error. Preserve that original exception instead of
                # hiding it behind "cannot rollback - no transaction is active".
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def _event(
        self,
        kind: str,
        *,
        project_id: str | None = None,
        job_id: str | None = None,
        permit_id: str | None = None,
        session_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events(ts,kind,project_id,job_id,permit_id,session_id,detail) "
            "VALUES (?,?,?,?,?,?,?)",
            (time.time(), kind, project_id, job_id, permit_id, session_id, json.dumps(detail or {})),
        )

    def _one(self, query: str, args: tuple = ()) -> dict:
        result = _row(self.conn.execute(query, args).fetchone())
        if result is None:
            raise BrokerError(404, "Record not found")
        return result

    def _project(self, project_id: str) -> dict:
        return self._one("SELECT * FROM projects WHERE id=?", (project_id,))

    def _same_project(self, table: str, record_id: str, project_id: str) -> dict:
        if table == "jobs":
            query = "SELECT * FROM jobs WHERE id=?"
        elif table == "sessions":
            query = "SELECT * FROM sessions WHERE id=?"
        elif table == "permits":
            query = (
                "SELECT permits.*, jobs.project_id FROM permits "
                "JOIN jobs ON jobs.id=permits.job_id WHERE permits.id=?"
            )
        else:
            raise ValueError(table)
        record = self._one(query, (record_id,))
        if record["project_id"] != project_id:
            raise BrokerError(403, "Record belongs to another project")
        return record

    def start(self) -> None:
        if self._started:
            return
        self._instance_lock.acquire()
        try:
            with self.transaction():
                now = time.time()
                for permit in self.conn.execute(
                    "SELECT id,job_id FROM permits WHERE status IN ('ACTIVE','CANCEL_REQUESTED')"
                ).fetchall():
                    self.conn.execute(
                        "UPDATE permits SET status='UNCERTAIN',reason='BROKER_RESTART' WHERE id=?",
                        (permit["id"],),
                    )
                    self._event("PERMIT_UNCERTAIN", job_id=permit["job_id"], permit_id=permit["id"])
                for session in self.conn.execute(
                    "SELECT id,project_id FROM sessions WHERE status IN ('PREPARING','READY','CLOSING')"
                ).fetchall():
                    self.conn.execute(
                        "UPDATE sessions SET status='UNCERTAIN',reason='BROKER_RESTART',updated_at=? WHERE id=?",
                        (now, session["id"]),
                    )
                    self._event(
                        "SESSION_UNCERTAIN", project_id=session["project_id"], session_id=session["id"]
                    )
            self._started = True
            self.poll()
        except BaseException:
            self._instance_lock.release()
            raise

    def stop(self) -> None:
        if self._started:
            self._started = False
            self._instance_lock.release()
        close = getattr(self.monitor, "close", None)
        if callable(close):
            close()
        with self._lock:
            self.conn.close()

    def poll(self) -> dict:
        snapshot = self.monitor.read()
        with self.transaction():
            previous_ok = bool(self._snapshot and self._snapshot.get("ok"))
            self._snapshot = snapshot
            if not snapshot.get("ok") and previous_ok:
                self._event("TELEMETRY_LOST", detail={"error": snapshot.get("error")})
            if snapshot.get("ok") and not previous_ok:
                self._event("TELEMETRY_READY")
            for gpu in snapshot.get("gpus", []):
                self.conn.execute(
                    "INSERT INTO samples(ts,gpu_uuid,used_mib,total_mib,utilization_pct,temperature_c) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        snapshot["timestamp"], gpu["uuid"], gpu["used_mib"],
                        gpu["total_mib"], gpu["utilization_pct"], gpu.get("temperature_c"),
                    ),
                )
            self._tick(time.time())
        return snapshot

    def prune_samples(self) -> int:
        with self.transaction():
            result = self.conn.execute("DELETE FROM samples WHERE ts < ?", (time.time() - 72 * 3600,))
            return result.rowcount

    def _managed_card(self, now: float) -> dict | None:
        if not self._snapshot or not self._snapshot.get("ok"):
            return None
        if now - float(self._snapshot["timestamp"]) > self.settings.stale_sample_seconds:
            return None
        return next(
            (card for card in self._snapshot["gpus"] if card["uuid"] == self.settings.managed_gpu_uuid),
            None,
        )

    def _allocation_enabled(self) -> bool:
        return self.conn.execute(
            "SELECT value FROM meta WHERE key='allocation_enabled'"
        ).fetchone()[0] == "1"

    def set_allocation(self, enabled: bool) -> dict:
        with self.transaction():
            self.conn.execute(
                "UPDATE meta SET value=? WHERE key='allocation_enabled'", ("1" if enabled else "0",)
            )
            self._event("ALLOCATION_ENABLED" if enabled else "ALLOCATION_PAUSED")
            self._tick(time.time())
        return {"allocation_enabled": enabled}

    def _reason(self, permit_id: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE permits SET reason=? WHERE id=? AND status='WAITING' AND reason IS NOT ?",
            (reason, permit_id, reason),
        )

    def _all_waiting(self, reason: str) -> None:
        self.conn.execute("UPDATE permits SET reason=? WHERE status='WAITING'", (reason,))

    def _tick(self, now: float) -> None:
        # Called only inside a write transaction; no two requests can grant at once.
        for permit in self.conn.execute(
            "SELECT permits.*, jobs.project_id FROM permits "
            "JOIN jobs ON jobs.id=permits.job_id "
            "WHERE permits.status IN ('ACTIVE','CANCEL_REQUESTED')"
        ).fetchall():
            project = self._project(permit["project_id"])
            stale = permit["heartbeat_at"] is None or (
                now - permit["heartbeat_at"] > self.settings.heartbeat_timeout_seconds
            )
            replaced = project["instance_id"] != permit["owner_instance"]
            if stale or replaced:
                reason = "OWNER_REPLACED" if replaced else "HEARTBEAT_LOST"
                self.conn.execute(
                    "UPDATE permits SET status='UNCERTAIN',reason=? WHERE id=?", (reason, permit["id"])
                )
                self._event(
                    "PERMIT_UNCERTAIN", project_id=permit["project_id"],
                    job_id=permit["job_id"], permit_id=permit["id"], detail={"reason": reason},
                )

        for session in self.conn.execute(
            "SELECT * FROM sessions WHERE status IN ('PREPARING','READY','CLOSING')"
        ).fetchall():
            project = self._project(session["project_id"])
            stale = now - session["heartbeat_at"] > self.settings.heartbeat_timeout_seconds
            replaced = project["instance_id"] != session["owner_instance"]
            prepare_timeout = session["status"] == "PREPARING" and \
                now - session["updated_at"] > self.settings.session_prepare_timeout_seconds
            if stale or replaced or prepare_timeout:
                reason = "OWNER_REPLACED" if replaced else \
                    "PREPARE_TIMEOUT" if prepare_timeout else "HEARTBEAT_LOST"
                self.conn.execute(
                    "UPDATE sessions SET status='UNCERTAIN',reason=?,updated_at=? WHERE id=?",
                    (reason, now, session["id"]),
                )
                self._event(
                    "SESSION_UNCERTAIN", project_id=session["project_id"],
                    session_id=session["id"], detail={"reason": reason},
                )

        for session in self.conn.execute(
            "SELECT * FROM sessions WHERE status='REQUESTED'"
        ).fetchall():
            project = self._project(session["project_id"])
            if now - session["heartbeat_at"] > self.settings.heartbeat_timeout_seconds or \
                project["instance_id"] != session["owner_instance"]:
                self.conn.execute(
                    "UPDATE sessions SET status='CLOSED',reason='OWNER_LOST',updated_at=? WHERE id=?",
                    (now, session["id"]),
                )
                self._event("SESSION_CLOSED", project_id=session["project_id"],
                            session_id=session["id"], detail={"reason": "OWNER_LOST"})

        if self.conn.execute("SELECT 1 FROM permits WHERE status='UNCERTAIN' LIMIT 1").fetchone() or \
            self.conn.execute("SELECT 1 FROM sessions WHERE status='UNCERTAIN' LIMIT 1").fetchone():
            self._all_waiting("WAIT_RECONCILE")
            return
        for closing in self.conn.execute(
            "SELECT * FROM sessions WHERE status='CLOSING'"
        ).fetchall():
            if self.conn.execute(
                "SELECT 1 FROM permits WHERE session_id=? AND status IN ('ACTIVE','CANCEL_REQUESTED') LIMIT 1",
                (closing["id"],),
            ).fetchone():
                continue
            for pending in self.conn.execute(
                "SELECT id,job_id FROM permits WHERE session_id=? AND status='WAITING'",
                (closing["id"],),
            ).fetchall():
                self.conn.execute(
                    "UPDATE permits SET status='CANCELLED',reason='SESSION_CLOSED',finished_at=? WHERE id=?",
                    (now, pending["id"]),
                )
                self._event("PERMIT_CANCELLED", project_id=closing["project_id"],
                            job_id=pending["job_id"], permit_id=pending["id"],
                            detail={"reason": "SESSION_CLOSED"})
            self.conn.execute(
                "UPDATE sessions SET status='CLOSED',reason=NULL,updated_at=? WHERE id=?",
                (now, closing["id"]),
            )
            self._event("SESSION_CLOSED", project_id=closing["project_id"], session_id=closing["id"])
        if not self._allocation_enabled():
            self._all_waiting("WAIT_PAUSED")
            return
        card = self._managed_card(now)
        if card is None:
            self._all_waiting("WAIT_TELEMETRY")
            return
        active = self.conn.execute(
            "SELECT * FROM permits WHERE status IN ('ACTIVE','CANCEL_REQUESTED') LIMIT 1"
        ).fetchone()
        if active is not None:
            self._all_waiting("WAIT_ACTIVE")
            return

        session = self.conn.execute(
            "SELECT * FROM sessions WHERE status IN ('PREPARING','READY','CLOSING') "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if session is None:
            session = self.conn.execute(
                "SELECT * FROM sessions WHERE status='REQUESTED' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if session is not None:
                self.conn.execute(
                    "UPDATE sessions SET status='PREPARING',reason=NULL,updated_at=? WHERE id=?",
                    (now, session["id"]),
                )
                self._event(
                    "SESSION_PREPARING", project_id=session["project_id"], session_id=session["id"]
                )
        if session is not None:
            self._all_waiting("WAIT_TASK" if session["kind"] == "batch_task" else "WAIT_REALTIME")
            waiting = self.conn.execute(
                "SELECT p.*, profiles.peak_growth_mib, profiles.max_seconds, profiles.respect_vram, "
                "jobs.project_id, projects.last_seen, projects.instance_id, projects.enabled "
                "FROM permits p JOIN profiles ON profiles.id=p.profile_id "
                "JOIN jobs ON jobs.id=p.job_id JOIN projects ON projects.id=jobs.project_id "
                "WHERE p.status='WAITING' AND p.session_id=? ORDER BY p.created_at LIMIT 1",
                (session["id"],),
            ).fetchone()
            if waiting is not None:
                self._try_grant(waiting, card, now)
            return

        waiting = self.conn.execute(
            "SELECT p.*, profiles.peak_growth_mib, profiles.max_seconds, profiles.respect_vram, "
            "jobs.project_id, projects.last_seen, projects.instance_id, projects.enabled, "
            "projects.usage_seconds, projects.weight "
            "FROM permits p JOIN profiles ON profiles.id=p.profile_id "
            "JOIN jobs ON jobs.id=p.job_id JOIN projects ON projects.id=jobs.project_id "
            "WHERE p.status='WAITING' AND p.session_id IS NULL "
            "ORDER BY (projects.usage_seconds / projects.weight), p.created_at"
        ).fetchall()
        for candidate in waiting:
            if self._try_grant(candidate, card, now):
                for other in waiting:
                    if other["id"] != candidate["id"]:
                        self._reason(other["id"], "WAIT_ACTIVE")
                return

    def _try_grant(self, permit: sqlite3.Row, card: dict, now: float) -> bool:
        if not permit["enabled"] or permit["instance_id"] != permit["owner_instance"] or \
            permit["last_seen"] is None or \
            now - permit["last_seen"] > self.settings.heartbeat_timeout_seconds:
            self._reason(permit["id"], "WAIT_PROJECT_OFFLINE")
            return False
        task_session = None
        if permit["session_id"]:
            task_session = self.conn.execute(
                "SELECT kind,status FROM sessions WHERE id=?", (permit["session_id"],)
            ).fetchone()
        exclusive_task = bool(task_session and task_session["kind"] == "batch_task")
        if exclusive_task and task_session["status"] != "READY":
            self._reason(permit["id"], "WAIT_TASK_READY")
            return False
        if not exclusive_task or bool(permit["respect_vram"]):
            safety = max(self.settings.safety_floor_mib, int(card["total_mib"] * self.settings.safety_ratio))
            if permit["peak_growth_mib"] + safety > card["total_mib"]:
                self._reason(permit["id"], "PROFILE_NOT_FIT")
                return False
            if card["used_mib"] + permit["peak_growth_mib"] + safety > card["total_mib"]:
                self._reason(permit["id"], "WAIT_VRAM")
                return False
        self.conn.execute(
            "UPDATE permits SET status='ACTIVE',reason=NULL,granted_at=?,heartbeat_at=? WHERE id=?",
            (now, now, permit["id"]),
        )
        self._event(
            "PERMIT_GRANTED", project_id=permit["project_id"], job_id=permit["job_id"],
            permit_id=permit["id"], detail={"profile_id": permit["profile_id"],
                                            "peak_growth_mib": permit["peak_growth_mib"],
                                            "exclusive_task": exclusive_task},
        )
        return True

    def integrity_check(self) -> dict:
        with self._lock:
            result = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
            journal = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        return {
            "integrity": result,
            "sqlite_version": sqlite3.sqlite_version,
            "journal_mode": journal,
            "synchronous": "FULL",
        }

    def backup(self) -> dict:
        backup_dir = self.settings.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = backup_dir / f"broker-{stamp}-{uuid.uuid4().hex[:6]}.sqlite3"
        with self._lock, sqlite3.connect(destination) as target:
            self.conn.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Backup integrity check failed: {check}")
        return {"path": str(destination), "bytes": destination.stat().st_size, "integrity": check}

    def maintenance(self) -> dict:
        """Prune samples and keep a verified daily backup for 14 days."""
        removed_samples = self.prune_samples()
        with self._lock:
            record = self.conn.execute("SELECT value FROM meta WHERE key='last_backup_at'").fetchone()
        last_backup = float(record[0]) if record else 0.0
        backup = None
        if time.time() - last_backup >= 24 * 3600:
            backup = self.backup()
            with self.transaction():
                self.conn.execute(
                    "INSERT INTO meta(key,value) VALUES ('last_backup_at',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(time.time()),),
                )
                self._event("BACKUP_CREATED", detail={"path": backup["path"], "bytes": backup["bytes"]})
        backup_dir = self.settings.data_dir / "backups"
        removed_backups = 0
        if backup_dir.is_dir():
            threshold = time.time() - 14 * 24 * 3600
            for path in backup_dir.glob("broker-*.sqlite3"):
                if path.is_file() and path.stat().st_mtime < threshold:
                    path.unlink()
                    removed_backups += 1
        return {"removed_samples": removed_samples, "backup": backup,
                "removed_backups": removed_backups}

    def project_heartbeat(
        self, project_id: str, instance_id: str, reported_status: str, resident_mib: int
    ) -> dict:
        if resident_mib < 0:
            raise BrokerError(422, "resident_mib cannot be negative")
        with self.transaction():
            project = self._project(project_id)
            now = time.time()
            self.conn.execute(
                "UPDATE projects SET last_seen=?,instance_id=?,reported_status=?,reported_resident_mib=? WHERE id=?",
                (now, instance_id, reported_status[:100], resident_mib, project_id),
            )
            if project["instance_id"] != instance_id:
                self._event(
                    "PROJECT_INSTANCE", project_id=project_id,
                    detail={"instance_id": instance_id},
                )
            self._tick(now)
            return self._project(project_id)

    def create_profile(
        self, project_id: str, label: str, kind: str, peak_growth_mib: int, max_seconds: int,
        respect_vram: bool = False,
    ) -> dict:
        if kind not in ("realtime", "batch") or peak_growth_mib <= 0 or max_seconds <= 0:
            raise BrokerError(422, "Invalid profile resource bounds")
        with self.transaction():
            self._project(project_id)
            profile_id = _id()
            self.conn.execute(
                "INSERT INTO profiles(id,project_id,label,kind,peak_growth_mib,max_seconds,respect_vram,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (profile_id, project_id, label[:120], kind, peak_growth_mib, max_seconds,
                 int(bool(respect_vram)), time.time()),
            )
            self._event("PROFILE_CREATED", project_id=project_id, detail={"profile_id": profile_id})
            return _profile_row(self.conn.execute(
                "SELECT * FROM profiles WHERE id=?", (profile_id,)
            ).fetchone())

    def update_profile(self, profile_id: str, enabled: bool) -> dict:
        with self.transaction():
            profile = self._one("SELECT * FROM profiles WHERE id=?", (profile_id,))
            if not enabled and self.conn.execute(
                "SELECT 1 FROM permits WHERE profile_id=? AND status IN ('WAITING','ACTIVE','CANCEL_REQUESTED','UNCERTAIN') LIMIT 1",
                (profile_id,),
            ).fetchone():
                raise BrokerError(409, "Profile has unfinished permits")
            self.conn.execute("UPDATE profiles SET enabled=? WHERE id=?", (int(enabled), profile_id))
            self._event("PROFILE_UPDATED", project_id=profile["project_id"], detail={"profile_id": profile_id, "enabled": enabled})
            return _profile_row(self.conn.execute(
                "SELECT * FROM profiles WHERE id=?", (profile_id,)
            ).fetchone())

    def register_job(
        self, project_id: str, external_id: str, idempotency_key: str, label: str
    ) -> dict:
        with self.transaction():
            self._project(project_id)
            existing = self.conn.execute(
                "SELECT * FROM jobs WHERE project_id=? AND idempotency_key=?",
                (project_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["external_id"] != external_id:
                    raise BrokerError(409, "Idempotency key belongs to another external job")
                return dict(existing)
            if self.conn.execute(
                "SELECT 1 FROM jobs WHERE project_id=? AND external_id=?", (project_id, external_id)
            ).fetchone():
                raise BrokerError(409, "External job ID already exists with another key")
            job_id = _id()
            now = time.time()
            self.conn.execute(
                "INSERT INTO jobs(id,project_id,external_id,idempotency_key,label,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'ACCEPTED',?,?)",
                (job_id, project_id, external_id, idempotency_key, label[:160], now, now),
            )
            self._event("JOB_REGISTERED", project_id=project_id, job_id=job_id)
            return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def update_job(
        self, project_id: str, job_id: str, status: str, backend_id: str | None, error_code: str | None
    ) -> dict:
        allowed = {
            "ACCEPTED", "WAITING_GPU", "RUNNING", "COMMITTING", "COMPLETED",
            "FAILED", "CANCEL_REQUESTED", "CANCELLED", "NEEDS_RECOVERY",
        }
        if status not in allowed:
            raise BrokerError(422, "Unknown job status")
        with self.transaction():
            job = self._same_project("jobs", job_id, project_id)
            if job["status"] in ("COMPLETED", "CANCELLED") and status != job["status"]:
                raise BrokerError(409, "Terminal job cannot change status")
            self.conn.execute(
                "UPDATE jobs SET status=?,backend_id=COALESCE(?,backend_id),error_code=?,updated_at=? WHERE id=?",
                (status, backend_id, error_code, time.time(), job_id),
            )
            if job["status"] != status:
                self._event("JOB_STATUS", project_id=project_id, job_id=job_id, detail={"status": status})
            return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def request_session(self, project_id: str, request_key: str, owner_instance: str,
                        kind: str = "realtime") -> dict:
        if kind not in ("realtime", "batch_task"):
            raise BrokerError(422, "Unknown session kind")
        with self.transaction():
            project = self._project(project_id)
            existing = self.conn.execute(
                "SELECT * FROM sessions WHERE project_id=? AND request_key=?",
                (project_id, request_key),
            ).fetchone()
            if existing:
                if existing["owner_instance"] != owner_instance or existing["kind"] != kind:
                    raise BrokerError(409, "Session key belongs to another owner or kind")
                return dict(existing)
            if project["instance_id"] != owner_instance or project["last_seen"] is None or \
                time.time() - project["last_seen"] > self.settings.heartbeat_timeout_seconds:
                raise BrokerError(409, "Project instance must heartbeat before requesting a session")
            session_id = _id()
            now = time.time()
            self.conn.execute(
                "INSERT INTO sessions(id,project_id,request_key,owner_instance,kind,status,reason,created_at,updated_at,heartbeat_at) "
                "VALUES (?,?,?,?,?,'REQUESTED','WAIT_BATCH',?,?,?)",
                (session_id, project_id, request_key, owner_instance, kind, now, now, now),
            )
            self._event("SESSION_REQUESTED", project_id=project_id, session_id=session_id)
            self._tick(now)
            return self._one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def session_heartbeat(self, project_id: str, session_id: str, owner_instance: str) -> dict:
        with self.transaction():
            session = self._same_project("sessions", session_id, project_id)
            if session["owner_instance"] != owner_instance or session["status"] not in (
                "REQUESTED", "PREPARING", "READY", "CLOSING"
            ):
                raise BrokerError(409, "Session cannot be renewed; reconcile its state")
            self.conn.execute(
                "UPDATE sessions SET heartbeat_at=?,updated_at=? WHERE id=?",
                (time.time(), time.time(), session_id),
            )
            self._tick(time.time())
            return self._one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def session_ready(self, project_id: str, session_id: str, owner_instance: str) -> dict:
        with self.transaction():
            session = self._same_project("sessions", session_id, project_id)
            if session["owner_instance"] != owner_instance or session["status"] != "PREPARING":
                raise BrokerError(409, "Only a preparing session can become ready")
            now = time.time()
            self.conn.execute(
                "UPDATE sessions SET status='READY',reason=NULL,updated_at=?,heartbeat_at=? WHERE id=?",
                (now, now, session_id),
            )
            self._event("SESSION_READY", project_id=project_id, session_id=session_id)
            self._tick(now)
            return self._one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def close_session(self, project_id: str, session_id: str, owner_instance: str,
                      backend_confirmed_inactive: bool) -> dict:
        if not backend_confirmed_inactive:
            raise BrokerError(409, "Backend inactivity must be confirmed before closing a session")
        with self.transaction():
            session = self._same_project("sessions", session_id, project_id)
            if session["status"] == "CLOSED":
                return session
            if session["owner_instance"] != owner_instance:
                raise BrokerError(409, "Only the current owner may close a session")
            if session["status"] == "UNCERTAIN":
                raise BrokerError(409, "Uncertain session requires administrator reconciliation")
            self.conn.execute(
                "UPDATE sessions SET status='CLOSING',reason='WAIT_ACTIVE',updated_at=? WHERE id=?",
                (time.time(), session_id),
            )
            self._event("SESSION_CLOSING", project_id=project_id, session_id=session_id)
            self._tick(time.time())
            return self._one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def request_permit(
        self, project_id: str, job_id: str, profile_id: str, request_key: str,
        stage: str, owner_instance: str, backend_id: str | None, session_id: str | None
    ) -> dict:
        with self.transaction():
            job = self._same_project("jobs", job_id, project_id)
            existing = self.conn.execute(
                "SELECT * FROM permits WHERE job_id=? AND request_key=?", (job_id, request_key)
            ).fetchone()
            if existing:
                if existing["profile_id"] != profile_id or existing["stage"] != stage or \
                    existing["owner_instance"] != owner_instance or existing["session_id"] != session_id:
                    raise BrokerError(409, "Permit key belongs to another stage, profile, owner, or session")
                return dict(existing)
            if job["status"] in ("COMPLETED", "CANCELLED", "FAILED", "CANCEL_REQUESTED"):
                raise BrokerError(409, "Finished or cancelling job cannot request GPU")
            profile = self._one("SELECT * FROM profiles WHERE id=?", (profile_id,))
            if profile["project_id"] != project_id or not profile["enabled"]:
                raise BrokerError(409, "Profile is not enabled for this project")
            project = self._project(project_id)
            if project["instance_id"] != owner_instance or project["last_seen"] is None or \
                time.time() - project["last_seen"] > self.settings.heartbeat_timeout_seconds:
                raise BrokerError(409, "Project instance must heartbeat before requesting GPU")
            if profile["kind"] == "realtime":
                if not session_id:
                    raise BrokerError(422, "Realtime permit requires a protected session")
                session = self._same_project("sessions", session_id, project_id)
                if session["kind"] != "realtime":
                    raise BrokerError(409, "Realtime permit requires a realtime session")
                if session["status"] not in ("REQUESTED", "PREPARING", "READY"):
                    raise BrokerError(409, "Realtime session is not available")
                if session["owner_instance"] != owner_instance:
                    raise BrokerError(409, "Session belongs to another process instance")
            elif session_id is not None:
                session = self._same_project("sessions", session_id, project_id)
                if session["kind"] != "batch_task" or session["status"] not in (
                    "REQUESTED", "PREPARING", "READY"
                ) or session["owner_instance"] != owner_instance:
                    raise BrokerError(409, "Batch permit requires an owned batch task session")
            permit_id = _id()
            now = time.time()
            self.conn.execute(
                "INSERT INTO permits(id,job_id,profile_id,session_id,request_key,stage,owner_instance,backend_id,status,reason,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,'WAITING','WAIT_PAUSED',?)",
                (permit_id, job_id, profile_id, session_id, request_key, stage[:100], owner_instance, backend_id, now),
            )
            self._event("PERMIT_REQUESTED", project_id=project_id, job_id=job_id, permit_id=permit_id)
            self._tick(now)
            return self._one("SELECT * FROM permits WHERE id=?", (permit_id,))

    def permit_heartbeat(
        self, project_id: str, permit_id: str, owner_instance: str, backend_id: str | None
    ) -> dict:
        with self.transaction():
            permit = self._same_project("permits", permit_id, project_id)
            if permit["owner_instance"] != owner_instance or permit["status"] not in (
                "ACTIVE", "CANCEL_REQUESTED"
            ):
                raise BrokerError(409, "Permit cannot be renewed; reconcile its state")
            self.conn.execute(
                "UPDATE permits SET heartbeat_at=?,backend_id=COALESCE(?,backend_id) WHERE id=?",
                (time.time(), backend_id, permit_id),
            )
            self._tick(time.time())
            return self._one("SELECT * FROM permits WHERE id=?", (permit_id,))

    def finish_permit(
        self, project_id: str, permit_id: str, owner_instance: str,
        result: str, backend_confirmed_inactive: bool, resident_mib: int
    ) -> dict:
        if result not in ("COMPLETED", "FAILED", "CANCELLED") or resident_mib < 0:
            raise BrokerError(422, "Invalid result or resident_mib")
        if not backend_confirmed_inactive:
            raise BrokerError(409, "Backend inactivity must be confirmed before releasing a permit")
        with self.transaction():
            permit = self._same_project("permits", permit_id, project_id)
            if permit["owner_instance"] != owner_instance or permit["status"] not in (
                "ACTIVE", "CANCEL_REQUESTED"
            ):
                raise BrokerError(409, "Only the current owner may finish an active permit")
            now = time.time()
            self.conn.execute(
                "UPDATE permits SET status='FINISHED',result=?,reason=NULL,finished_at=?,resident_mib=? WHERE id=?",
                (result, now, resident_mib, permit_id),
            )
            self.conn.execute(
                "UPDATE projects SET usage_seconds=usage_seconds+? WHERE id=?",
                (max(0, now - permit["granted_at"]), project_id),
            )
            self._event(
                "PERMIT_FINISHED", project_id=project_id, job_id=permit["job_id"],
                permit_id=permit_id, detail={"result": result, "resident_mib": resident_mib},
            )
            self._tick(now)
            return self._one("SELECT * FROM permits WHERE id=?", (permit_id,))

    def cancel_permit(self, project_id: str, permit_id: str) -> dict:
        with self.transaction():
            permit = self._same_project("permits", permit_id, project_id)
            if permit["status"] == "WAITING":
                self.conn.execute(
                    "UPDATE permits SET status='CANCELLED',reason=NULL,finished_at=? WHERE id=?",
                    (time.time(), permit_id),
                )
                self._event("PERMIT_CANCELLED", project_id=project_id, job_id=permit["job_id"], permit_id=permit_id)
            elif permit["status"] == "ACTIVE":
                self.conn.execute(
                    "UPDATE permits SET status='CANCEL_REQUESTED',reason='WAIT_BACKEND_STOP' WHERE id=?",
                    (permit_id,),
                )
                self._event("PERMIT_CANCEL_REQUESTED", project_id=project_id, job_id=permit["job_id"], permit_id=permit_id)
            self._tick(time.time())
            return self._one("SELECT * FROM permits WHERE id=?", (permit_id,))

    def cancel_job(self, job_id: str) -> dict:
        """Request cancellation for the business job and every unfinished permit."""
        with self.transaction():
            job = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
            if job["status"] in ("COMPLETED", "FAILED", "CANCELLED", "CANCEL_REQUESTED"):
                return job
            now = time.time()
            self.conn.execute(
                "UPDATE jobs SET status='CANCEL_REQUESTED',updated_at=? WHERE id=?",
                (now, job_id),
            )
            for permit in self.conn.execute(
                "SELECT id,status FROM permits WHERE job_id=? AND status IN ('WAITING','ACTIVE')",
                (job_id,),
            ).fetchall():
                if permit["status"] == "WAITING":
                    self.conn.execute(
                        "UPDATE permits SET status='CANCELLED',reason=NULL,finished_at=? WHERE id=?",
                        (now, permit["id"]),
                    )
                    self._event("PERMIT_CANCELLED", project_id=job["project_id"],
                                job_id=job_id, permit_id=permit["id"])
                else:
                    self.conn.execute(
                        "UPDATE permits SET status='CANCEL_REQUESTED',reason='WAIT_BACKEND_STOP' WHERE id=?",
                        (permit["id"],),
                    )
                    self._event("PERMIT_CANCEL_REQUESTED", project_id=job["project_id"],
                                job_id=job_id, permit_id=permit["id"])
            self._event("JOB_CANCEL_REQUESTED", project_id=job["project_id"], job_id=job_id)
            self._tick(now)
            return self._one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def reconcile_permit(self, permit_id: str, evidence: str, backend_confirmed_inactive: bool) -> dict:
        if not backend_confirmed_inactive or len(evidence.strip()) < 10:
            raise BrokerError(422, "Provide specific evidence that the backend is inactive")
        with self.transaction():
            permit = self._one("SELECT * FROM permits WHERE id=?", (permit_id,))
            if permit["status"] != "UNCERTAIN":
                raise BrokerError(409, "Only uncertain permits require reconciliation")
            now = time.time()
            self.conn.execute(
                "UPDATE permits SET status='FINISHED',result='RECONCILED',reason=NULL,finished_at=? WHERE id=?",
                (now, permit_id),
            )
            project_id = self._one("SELECT project_id FROM jobs WHERE id=?", (permit["job_id"],))["project_id"]
            self._event(
                "PERMIT_RECONCILED", project_id=project_id, job_id=permit["job_id"],
                permit_id=permit_id, detail={"evidence": evidence[:300]},
            )
            self._tick(now)
            return self._one("SELECT * FROM permits WHERE id=?", (permit_id,))

    def reconcile_session(self, session_id: str, evidence: str, backend_confirmed_inactive: bool) -> dict:
        if not backend_confirmed_inactive or len(evidence.strip()) < 10:
            raise BrokerError(422, "Provide specific evidence that the session is inactive")
        with self.transaction():
            session = self._one("SELECT * FROM sessions WHERE id=?", (session_id,))
            if session["status"] != "UNCERTAIN":
                raise BrokerError(409, "Only uncertain sessions require reconciliation")
            if self.conn.execute(
                "SELECT 1 FROM permits WHERE session_id=? AND status IN ('ACTIVE','CANCEL_REQUESTED','UNCERTAIN') LIMIT 1",
                (session_id,),
            ).fetchone():
                raise BrokerError(409, "Resolve associated permits first")
            now = time.time()
            self.conn.execute(
                "UPDATE sessions SET status='CLOSED',reason=NULL,updated_at=? WHERE id=?",
                (now, session_id),
            )
            self._event(
                "SESSION_RECONCILED", project_id=session["project_id"],
                session_id=session_id, detail={"evidence": evidence[:300]},
            )
            self._tick(now)
            return self._one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def get_job(self, project_id: str, job_id: str) -> dict:
        with self._lock:
            job = self._same_project("jobs", job_id, project_id)
            job["permits"] = [
                dict(row) for row in self.conn.execute(
                    "SELECT * FROM permits WHERE job_id=? ORDER BY created_at", (job_id,)
                ).fetchall()
            ]
            return job

    def get_permit(self, project_id: str, permit_id: str) -> dict:
        with self._lock:
            return self._same_project("permits", permit_id, project_id)

    def get_session(self, project_id: str, session_id: str) -> dict:
        with self._lock:
            return self._same_project("sessions", session_id, project_id)

    def events(self, after: int = 0, limit: int = 200) -> list[dict]:
        with self._lock:
            records = self.conn.execute(
                "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                (after, max(1, min(limit, 500))),
            ).fetchall()
            return [{**dict(row), "detail": json.loads(row["detail"])} for row in records]

    def history(self, gpu_uuid: str, minutes: int = 60, limit: int = 800) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts,used_mib,total_mib,utilization_pct,temperature_c FROM samples "
                "WHERE gpu_uuid=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                (gpu_uuid, time.time() - max(1, min(minutes, 4320)) * 60, max(1, min(limit, 3000))),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]

    def dashboard(self) -> dict:
        with self._lock:
            now = time.time()
            self._tick_readonly_guard(now)
            projects = [dict(row) for row in self.conn.execute("SELECT * FROM projects ORDER BY id")]
            profiles = [
                _profile_row(row)
                for row in self.conn.execute("SELECT * FROM profiles ORDER BY created_at DESC")
            ]
            jobs = [dict(row) for row in self.conn.execute(
                "SELECT * FROM jobs ORDER BY CASE WHEN status IN ('COMPLETED','FAILED','CANCELLED') "
                "THEN 1 ELSE 0 END, updated_at DESC LIMIT 200"
            )]
            sessions = [dict(row) for row in self.conn.execute(
                "SELECT * FROM sessions ORDER BY CASE WHEN status IN ('REQUESTED','PREPARING','READY','CLOSING','UNCERTAIN') "
                "THEN 0 ELSE 1 END, created_at DESC LIMIT 100"
            )]
            permits = [
                dict(row) for row in self.conn.execute(
                    "SELECT permits.*,profiles.label AS profile_label,profiles.kind,profiles.peak_growth_mib,"
                    "profiles.max_seconds,jobs.project_id,jobs.label AS job_label "
                    "FROM permits JOIN profiles ON profiles.id=permits.profile_id "
                    "JOIN jobs ON jobs.id=permits.job_id ORDER BY "
                    "CASE WHEN permits.status IN ('WAITING','ACTIVE','CANCEL_REQUESTED','UNCERTAIN') "
                    "THEN 0 ELSE 1 END, permits.created_at DESC LIMIT 200"
                )
            ]
            events = [
                {**dict(row), "detail": json.loads(row["detail"])}
                for row in self.conn.execute("SELECT * FROM events ORDER BY seq DESC LIMIT 60")
            ]
            snapshot = self._snapshot or {"ok": False, "error": "No sample yet", "gpus": [], "host": {}, "timestamp": None}
            card = self._managed_card(now)
            counts = {
                "waiting": self.conn.execute("SELECT COUNT(*) FROM permits WHERE status='WAITING'").fetchone()[0],
                "active": self.conn.execute("SELECT COUNT(*) FROM permits WHERE status IN ('ACTIVE','CANCEL_REQUESTED')").fetchone()[0],
                "uncertain": self.conn.execute("SELECT COUNT(*) FROM permits WHERE status='UNCERTAIN'").fetchone()[0]
                    + self.conn.execute("SELECT COUNT(*) FROM sessions WHERE status='UNCERTAIN'").fetchone()[0],
                "ready_sessions": self.conn.execute("SELECT COUNT(*) FROM sessions WHERE status='READY'").fetchone()[0],
            }
            uncertain = counts["uncertain"] > 0
            alerts: list[dict] = []
            if card is None:
                alerts.append({"level": "critical", "code": "TELEMETRY", "text": "受管 GPU 观测数据缺失或已过期，新准入已停止"})
            else:
                safety = max(self.settings.safety_floor_mib, int(card["total_mib"] * self.settings.safety_ratio))
                if card["free_mib"] < safety:
                    alerts.append({"level": "warning", "code": "VRAM", "text": "受管 GPU 空闲显存低于安全余量"})
            if uncertain:
                alerts.append({"level": "critical", "code": "RECONCILE", "text": "任务或实时会话状态待核实，新准入已冻结"})
            if not self._allocation_enabled():
                alerts.append({"level": "info", "code": "PAUSED", "text": "协调服务处于观察模式，尚未发放新许可"})
            for project in projects:
                if project["last_seen"] is None or now - project["last_seen"] > self.settings.heartbeat_timeout_seconds:
                    alerts.append({"level": "info", "code": "PROJECT_OFFLINE", "text": f"{project['label']} 尚未上报或心跳已过期"})
            for permit in permits:
                if permit["status"] in ("ACTIVE", "CANCEL_REQUESTED") and permit["granted_at"] and \
                    now - permit["granted_at"] > permit["max_seconds"]:
                    alerts.append({"level": "warning", "code": "LONG_RUNNING", "text": f"{permit['job_label']} 已超过画像最长运行时间"})
            return {
                "timestamp": now,
                "allocation_enabled": self._allocation_enabled(),
                "managed_gpu_uuid": self.settings.managed_gpu_uuid,
                "display_gpu_uuid": self.settings.display_gpu_uuid,
                "safety_floor_mib": self.settings.safety_floor_mib,
                "safety_ratio": self.settings.safety_ratio,
                "snapshot": snapshot,
                "projects": projects,
                "profiles": profiles,
                "jobs": jobs,
                "sessions": sessions,
                "permits": permits,
                "events": events,
                "alerts": alerts,
                "counts": counts,
            }

    def _tick_readonly_guard(self, now: float) -> None:
        # UI reads do not change scheduling state; this method documents that boundary.
        del now
