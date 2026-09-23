from __future__ import annotations

import json
import time
import threading
import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from gpu_broker.api import create_app
from gpu_broker.config import Settings, initialize, load_settings
from gpu_broker.core import Broker, BrokerError
from gpu_broker.monitor import NvidiaMonitor


GPU = "GPU-test-managed"
DISPLAY = "GPU-test-display"


class FakeMonitor:
    def __init__(self):
        self.ok = True
        self.used = 1000
        self.total = 16000

    def read(self):
        return {
            "ok": self.ok, "timestamp": time.time(), "error": None if self.ok else "test outage",
            "source": "fake", "host": {"cpu_pct": 12, "ram_used_mib": 4096, "ram_total_mib": 16384},
            "gpus": [{
                "uuid": GPU, "name": "Test 4080", "total_mib": self.total,
                "used_mib": self.used, "free_mib": self.total - self.used,
                "utilization_pct": 20, "temperature_c": 43, "driver": "test",
            }, {"uuid": DISPLAY, "name": "Test 2070", "total_mib": 8000,
                "used_mib": 1000, "free_mib": 7000, "utilization_pct": 2,
                "temperature_c": 40, "driver": "test"}] if self.ok else [],
        }


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path, managed_gpu_uuid=GPU, display_gpu_uuid=DISPLAY,
        admin_token="admin-test-token", project_tokens={
            "minimax": "mini-test-token", "live_translate": "live-test-token", "manga": "manga-test-token"
        },
    )


def test_active_heartbeat_grace_config_defaults_and_override(tmp_path):
    initialize(tmp_path, GPU, DISPLAY)
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["active_heartbeat_grace_seconds"] == 180.0
    assert load_settings(tmp_path).active_heartbeat_grace_seconds == 180.0

    # Existing installations may edit config.json, and older configs without
    # the new field receive the safe default instead of failing to start.
    config["active_heartbeat_grace_seconds"] = 240.0
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert load_settings(tmp_path).active_heartbeat_grace_seconds == 240.0
    del config["active_heartbeat_grace_seconds"]
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert load_settings(tmp_path).active_heartbeat_grace_seconds == 180.0

    config["active_heartbeat_grace_seconds"] = -1.0
    config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="finite non-negative"):
        load_settings(tmp_path)


def test_settings_keeps_legacy_positional_field_order(tmp_path):
    configured = Settings(
        tmp_path, GPU, DISPLAY, "admin-test-token", {}, None,
        2.0, 10.0, 15.0, 90.0, 1024, 0.25,
    )
    assert configured.session_prepare_timeout_seconds == 90.0
    assert configured.safety_floor_mib == 1024
    assert configured.safety_ratio == 0.25
    assert configured.active_heartbeat_grace_seconds == 180.0


def setup_job(broker, project, profile, instance, external):
    broker.project_heartbeat(project, instance, "online", 0)
    job = broker.register_job(project, external, f"idempotent-{external}", external)
    permit = broker.request_permit(project, job["id"], profile["id"], f"permit-{external}",
                                   "inference", instance, None, None)
    return job, permit


def test_paused_serial_idempotent_and_fair(settings):
    broker = Broker(settings, FakeMonitor())
    try:
        broker.start()
        profile = broker.create_profile("minimax", "H3", "batch", 4000, 1800)
        job1, permit1 = setup_job(broker, "minimax", profile, "worker-a", "task-1")
        assert permit1["status"] == "WAITING" and permit1["reason"] == "WAIT_PAUSED"
        broker.set_allocation(True)
        assert broker.get_permit("minimax", permit1["id"])["status"] == "ACTIVE"
        job2, permit2 = setup_job(broker, "minimax", profile, "worker-a", "task-2")
        assert permit2["status"] == "WAITING" and permit2["reason"] == "WAIT_ACTIVE"
        assert broker.request_permit("minimax", job1["id"], profile["id"],
            "permit-task-1", "inference", "worker-a", None, None)["id"] == permit1["id"]
        with pytest.raises(BrokerError):
            broker.finish_permit("minimax", permit1["id"], "worker-a", "COMPLETED", False, 0)
        broker.finish_permit("minimax", permit1["id"], "worker-a", "COMPLETED", True, 0)
        assert broker.get_permit("minimax", permit2["id"])["status"] == "ACTIVE"
        broker.update_job("minimax", job1["id"], "COMPLETED", None, None)
        assert broker.request_permit("minimax", job1["id"], profile["id"],
            "permit-task-1", "inference", "worker-a", None, None)["id"] == permit1["id"]
    finally:
        broker.stop()


def test_three_projects_share_one_active_gpu_turn(settings):
    broker = Broker(settings, FakeMonitor())
    try:
        broker.start()
        profiles = {
            project: broker.create_profile(project, project, "batch", 2000, 120)
            for project in ("minimax", "live_translate", "manga")
        }
        broker.set_allocation(True)
        jobs_and_permits = {
            project: setup_job(
                broker, project, profiles[project], f"worker-{project}", f"task-{project}"
            )
            for project in ("minimax", "live_translate", "manga")
        }
        h3 = jobs_and_permits["minimax"][1]
        live = jobs_and_permits["live_translate"][1]
        manga = jobs_and_permits["manga"][1]
        assert broker.get_permit("minimax", h3["id"])["status"] == "ACTIVE"
        assert broker.get_permit("live_translate", live["id"])["status"] == "WAITING"
        assert broker.get_permit("manga", manga["id"])["status"] == "WAITING"

        broker.finish_permit("minimax", h3["id"], "worker-minimax", "COMPLETED", True, 0)
        remaining = [("live_translate", live), ("manga", manga)]
        statuses = [broker.get_permit(project, permit["id"])["status"] for project, permit in remaining]
        assert sorted(statuses) == ["ACTIVE", "WAITING"]
        first_project, first_permit = remaining[statuses.index("ACTIVE")]
        second_project, second_permit = remaining[statuses.index("WAITING")]
        broker.finish_permit(
            first_project, first_permit["id"], f"worker-{first_project}", "COMPLETED", True, 0
        )
        assert broker.get_permit(second_project, second_permit["id"])["status"] == "ACTIVE"
    finally:
        broker.stop()


def test_restart_freezes_until_reconciled(settings):
    monitor = FakeMonitor()
    broker = Broker(settings, monitor)
    broker.start()
    profile = broker.create_profile("minimax", "H3", "batch", 4000, 1800)
    broker.set_allocation(True)
    _, active = setup_job(broker, "minimax", profile, "worker-a", "task-1")
    broker.stop()

    restarted = Broker(settings, monitor)
    try:
        restarted.start()
        assert restarted.get_permit("minimax", active["id"])["status"] == "UNCERTAIN"
        _, waiting = setup_job(restarted, "minimax", profile, "worker-b", "task-2")
        assert waiting["status"] == "WAITING" and waiting["reason"] == "WAIT_RECONCILE"
        restarted.reconcile_permit(active["id"], "backend task gone and process stopped", True)
        assert restarted.get_permit("minimax", waiting["id"])["status"] == "ACTIVE"
    finally:
        restarted.stop()


def test_telemetry_and_vram_fail_closed(settings):
    monitor = FakeMonitor()
    broker = Broker(settings, monitor)
    try:
        broker.start()
        profile = broker.create_profile("manga", "Comfy", "batch", 5000, 1800)
        broker.set_allocation(True)
        monitor.ok = False
        broker.poll()
        _, permit = setup_job(broker, "manga", profile, "worker-a", "task-1")
        assert permit["reason"] == "WAIT_TELEMETRY"
        monitor.ok = True
        monitor.used = 12000
        broker.poll()
        assert broker.get_permit("manga", permit["id"])["reason"] == "WAIT_VRAM"
        monitor.used = 1000
        broker.poll()
        assert broker.get_permit("manga", permit["id"])["status"] == "ACTIVE"
    finally:
        broker.stop()


def test_cancel_does_not_release_and_lost_owner_freezes(settings):
    settings = replace(
        settings, heartbeat_timeout_seconds=0.2, active_heartbeat_grace_seconds=0.0
    )
    broker = Broker(settings, FakeMonitor())
    try:
        broker.start()
        profile = broker.create_profile("minimax", "H3", "batch", 4000, 1800)
        broker.set_allocation(True)
        _, first = setup_job(broker, "minimax", profile, "worker-a", "task-1")
        assert broker.cancel_permit("minimax", first["id"])["status"] == "CANCEL_REQUESTED"
        _, second = setup_job(broker, "minimax", profile, "worker-a", "task-2")
        assert second["status"] == "WAITING"
        time.sleep(0.25)
        broker.poll()
        assert broker.get_permit("minimax", first["id"])["status"] == "UNCERTAIN"
        assert broker.get_permit("minimax", second["id"])["reason"] == "WAIT_RECONCILE"
    finally:
        broker.stop()


def test_active_permit_tolerates_transient_and_long_scheduler_stalls(settings):
    settings = replace(
        settings, heartbeat_timeout_seconds=15.0, active_heartbeat_grace_seconds=180.0
    )
    broker = Broker(settings, FakeMonitor())
    try:
        broker.start()
        profile = broker.create_profile("minimax", "H3", "batch", 4000, 7200)
        broker.set_allocation(True)
        _, active = setup_job(broker, "minimax", profile, "worker-a", "stall-tolerant-task")

        # A synthetic 190-second scheduler pause remains inside the configured
        # 15 + 180 second liveness window, so another task cannot take the permit.
        for gap_seconds in (16.0, 190.0):
            with broker.transaction():
                broker.conn.execute(
                    "UPDATE permits SET heartbeat_at=? WHERE id=?",
                    (time.time() - gap_seconds, active["id"]),
                )
            broker.poll()
            assert broker.get_permit("minimax", active["id"])["status"] == "ACTIVE"

        renewed = broker.permit_heartbeat("minimax", active["id"], "worker-a", None)
        assert renewed["status"] == "ACTIVE"

        _, waiting = setup_job(broker, "minimax", profile, "worker-a", "must-wait-behind-active")
        assert waiting["status"] == "WAITING" and waiting["reason"] == "WAIT_ACTIVE"
    finally:
        broker.stop()


def test_sustained_active_permit_loss_becomes_uncertain_and_freezes_queue(settings):
    settings = replace(
        settings, heartbeat_timeout_seconds=15.0, active_heartbeat_grace_seconds=180.0
    )
    broker = Broker(settings, FakeMonitor())
    try:
        broker.start()
        profile = broker.create_profile("minimax", "H3", "batch", 4000, 7200)
        broker.set_allocation(True)
        _, active = setup_job(broker, "minimax", profile, "worker-a", "sustained-loss-task")
        with broker.transaction():
            broker.conn.execute(
                "UPDATE permits SET heartbeat_at=? WHERE id=?",
                (time.time() - 196.0, active["id"]),
            )

        broker.poll()
        uncertain = broker.get_permit("minimax", active["id"])
        assert uncertain["status"] == "UNCERTAIN" and uncertain["reason"] == "HEARTBEAT_LOST"
        _, waiting = setup_job(broker, "minimax", profile, "worker-a", "wait-for-reconcile")
        assert waiting["status"] == "WAITING" and waiting["reason"] == "WAIT_RECONCILE"
    finally:
        broker.stop()


def test_ready_session_uses_active_heartbeat_grace(settings):
    settings = replace(
        settings, heartbeat_timeout_seconds=15.0, active_heartbeat_grace_seconds=180.0
    )
    broker = Broker(settings, FakeMonitor())
    try:
        broker.start()
        broker.set_allocation(True)
        broker.project_heartbeat("minimax", "worker-a", "online", 0)
        session = broker.request_session("minimax", "long-running-task", "worker-a", "batch_task")
        broker.session_ready("minimax", session["id"], "worker-a")
        profile = broker.create_profile("minimax", "H3", "batch", 4000, 7200)
        job = broker.register_job("minimax", "session-task", "session-task-idem", "session-task")
        permit = broker.request_permit(
            "minimax", job["id"], profile["id"], "session-stage", "inference",
            "worker-a", None, session["id"],
        )
        assert permit["status"] == "ACTIVE"

        with broker.transaction():
            broker.conn.execute(
                "UPDATE sessions SET heartbeat_at=? WHERE id=?",
                (time.time() - 190.0, session["id"]),
            )
        broker.poll()
        assert broker.get_session("minimax", session["id"])["status"] == "READY"
        recovered = broker.session_heartbeat("minimax", session["id"], "worker-a")
        assert recovered["status"] == "READY"

        with broker.transaction():
            broker.conn.execute(
                "UPDATE sessions SET heartbeat_at=? WHERE id=?",
                (time.time() - 196.0, session["id"]),
            )
        broker.poll()
        stale_session = broker.get_session("minimax", session["id"])
        assert stale_session["status"] == "UNCERTAIN"
        assert stale_session["reason"] == "HEARTBEAT_LOST"
        # Session uncertainty must not silently release its active stage.
        assert broker.get_permit("minimax", permit["id"])["status"] == "ACTIVE"
    finally:
        broker.stop()


def test_admin_job_cancel_is_persistent_and_blocks_new_gpu(settings):
    broker = Broker(settings, FakeMonitor())
    try:
        broker.start()
        profile = broker.create_profile("minimax", "H3", "batch", 4000, 1800)
        broker.set_allocation(True)
        job, permit = setup_job(broker, "minimax", profile, "worker-a", "task-1")
        assert broker.cancel_job(job["id"])["status"] == "CANCEL_REQUESTED"
        assert broker.cancel_job(job["id"])["status"] == "CANCEL_REQUESTED"
        assert broker.get_permit("minimax", permit["id"])["status"] == "CANCEL_REQUESTED"
        with pytest.raises(BrokerError):
            broker.request_permit("minimax", job["id"], profile["id"], "new-stage",
                                  "inference", "worker-a", None, None)
        assert broker.get_job("minimax", job["id"])["status"] == "CANCEL_REQUESTED"
        broker.finish_permit("minimax", permit["id"], "worker-a", "CANCELLED", True, 0)
        broker.update_job("minimax", job["id"], "CANCELLED", None, None)
        assert broker.cancel_job(job["id"])["status"] == "CANCELLED"
    finally:
        broker.stop()


def test_verified_backup_and_maintenance(settings):
    broker = Broker(settings, FakeMonitor())
    try:
        broker.start()
        result = broker.maintenance()
        assert result["backup"] is not None
        assert result["backup"]["integrity"] == "ok"
        assert broker.maintenance()["backup"] is None
        assert broker.integrity_check()["integrity"] == "ok"
    finally:
        broker.stop()


def test_realtime_prepares_after_batch_and_protects_session(settings):
    broker = Broker(settings, FakeMonitor())
    try:
        broker.start()
        batch_profile = broker.create_profile("minimax", "H3", "batch", 4000, 1800)
        live_profile = broker.create_profile("live_translate", "ASR", "realtime", 1500, 3600)
        broker.set_allocation(True)
        _, batch = setup_job(broker, "minimax", batch_profile, "worker-a", "task-1")
        broker.project_heartbeat("live_translate", "live-a", "online", 0)
        session = broker.request_session("live_translate", "session-one", "live-a")
        assert session["status"] == "REQUESTED"
        broker.finish_permit("minimax", batch["id"], "worker-a", "COMPLETED", True, 0)
        assert broker.get_session("live_translate", session["id"])["status"] == "PREPARING"
        session = broker.session_ready("live_translate", session["id"], "live-a")
        assert session["status"] == "READY"
        job = broker.register_job("live_translate", "speech-1", "speech-idem", "speech")
        live = broker.request_permit("live_translate", job["id"], live_profile["id"],
                                     "speech-permit", "ASR", "live-a", None, session["id"])
        assert live["status"] == "ACTIVE"
        with pytest.raises(BrokerError):
            broker.close_session("live_translate", session["id"], "live-a", False)
        broker.finish_permit("live_translate", live["id"], "live-a", "COMPLETED", True, 0)
        broker.close_session("live_translate", session["id"], "live-a", True)
        assert broker.get_session("live_translate", session["id"])["status"] == "CLOSED"
    finally:
        broker.stop()


def test_batch_task_keeps_exclusive_turn_between_stages(settings):
    monitor = FakeMonitor()
    monitor.used = 1000
    broker = Broker(settings, monitor)
    try:
        broker.start()
        h3_profile = broker.create_profile("minimax", "H3 measured estimate", "batch", 5000, 1800)
        other_profile = broker.create_profile("manga", "Other batch", "batch", 2000, 1800)
        assert h3_profile["respect_vram"] is False
        broker.set_allocation(True)
        broker.project_heartbeat("minimax", "h3-worker", "online", 0)
        session = broker.request_session("minimax", "whole-h3-task", "h3-worker", "batch_task")
        assert session["status"] == "PREPARING"
        session = broker.session_ready("minimax", session["id"], "h3-worker")
        assert session["kind"] == "batch_task" and session["status"] == "READY"
        job = broker.register_job("minimax", "whole-task", "whole-task-idem", "whole-task")
        first = broker.request_permit("minimax", job["id"], h3_profile["id"],
                                      "first-stage", "video", "h3-worker", None, session["id"])
        # The task owns the Broker turn and the current admission snapshot has
        # enough room for its peak plus the fixed exclusive headroom.
        assert first["status"] == "ACTIVE"
        broker.project_heartbeat("manga", "other-worker", "online", 0)
        other_job = broker.register_job("manga", "later-task", "later-task-idem", "later-task")
        later = broker.request_permit("manga", other_job["id"], other_profile["id"],
                                      "later-stage", "image", "other-worker", None, None)
        assert later["status"] == "WAITING"
        broker.finish_permit("minimax", first["id"], "h3-worker", "COMPLETED", True, 0)
        assert broker.get_permit("manga", later["id"])["reason"] == "WAIT_TASK"
        second = broker.request_permit("minimax", job["id"], h3_profile["id"],
                                       "second-stage", "video", "h3-worker", None, session["id"])
        assert second["status"] == "ACTIVE"
        broker.finish_permit("minimax", second["id"], "h3-worker", "COMPLETED", True, 0)
        assert broker.get_permit("manga", later["id"])["status"] == "WAITING"
        monitor.used = 1000
        broker.poll()
        broker.close_session("minimax", session["id"], "h3-worker", True)
        assert broker.get_permit("manga", later["id"])["status"] == "ACTIVE"
    finally:
        broker.stop()


def test_batch_task_exclusive_profile_grants_with_fixed_headroom(settings):
    monitor = FakeMonitor()
    monitor.total = 16384
    monitor.used = 300
    broker = Broker(settings, monitor)
    try:
        broker.start()
        profile = broker.create_profile("minimax", "Representative video", "batch", 14800, 1800)
        assert profile["respect_vram"] is False
        broker.set_allocation(True)
        broker.project_heartbeat("minimax", "h3-worker", "online", 0)
        session = broker.request_session("minimax", "quiet-task", "h3-worker", "batch_task")
        session = broker.session_ready("minimax", session["id"], "h3-worker")
        job = broker.register_job("minimax", "quiet-job", "quiet-job-idem", "quiet job")
        permit = broker.request_permit("minimax", job["id"], profile["id"],
                                       "quiet-stage", "video", "h3-worker", None, session["id"])

        # 300 baseline + 14,800 representative growth + 1,024 headroom fits
        # on a 16,384 MiB card. The configurable 2,048 MiB reserve would reject it.
        assert permit["status"] == "ACTIVE"
    finally:
        broker.stop()


def test_batch_task_exclusive_profile_waits_when_unmanaged_gpu_use_is_high(settings):
    monitor = FakeMonitor()
    monitor.total = 16384
    monitor.used = 7800
    broker = Broker(settings, monitor)
    try:
        broker.start()
        profile = broker.create_profile("minimax", "Representative video", "batch", 14800, 1800)
        broker.set_allocation(True)
        broker.project_heartbeat("minimax", "h3-worker", "online", 0)
        session = broker.request_session("minimax", "busy-task", "h3-worker", "batch_task")
        session = broker.session_ready("minimax", session["id"], "h3-worker")
        job = broker.register_job("minimax", "busy-job", "busy-job-idem", "busy job")
        permit = broker.request_permit("minimax", job["id"], profile["id"],
                                       "busy-stage", "video", "h3-worker", None, session["id"])

        # Admission telemetry includes a legacy/unmanaged allocation at roughly
        # 7.8 GiB. The stage cannot fit alongside it and must remain queued.
        assert permit["status"] == "WAITING"
        assert permit["reason"] == "WAIT_VRAM"
    finally:
        broker.stop()


def test_batch_task_exclusive_profile_that_cannot_fit_is_rejected(settings):
    monitor = FakeMonitor()
    monitor.total = 16384
    monitor.used = 300
    broker = Broker(settings, monitor)
    try:
        broker.start()
        profile = broker.create_profile("minimax", "Impossible video", "batch", 15361, 1800)
        broker.set_allocation(True)
        broker.project_heartbeat("minimax", "h3-worker", "online", 0)
        session = broker.request_session("minimax", "impossible-task", "h3-worker", "batch_task")
        session = broker.session_ready("minimax", session["id"], "h3-worker")
        job = broker.register_job("minimax", "impossible-job", "impossible-job-idem", "impossible job")
        permit = broker.request_permit("minimax", job["id"], profile["id"],
                                       "impossible-stage", "video", "h3-worker", None, session["id"])

        # 15,361 + 1,024 exceeds the 16,384 MiB device even at zero use.
        assert permit["status"] == "WAITING"
        assert permit["reason"] == "PROFILE_NOT_FIT"
    finally:
        broker.stop()


def test_batch_task_profile_can_require_live_vram_capacity(settings):
    monitor = FakeMonitor()
    monitor.used = 10000
    broker = Broker(settings, monitor)
    try:
        broker.start()
        profile = broker.create_profile(
            "minimax", "H3 guarded experiment", "batch", 5000, 1800, respect_vram=True
        )
        broker.set_allocation(True)
        broker.project_heartbeat("minimax", "h3-worker", "online", 0)
        session = broker.request_session("minimax", "guarded-task", "h3-worker", "batch_task")
        session = broker.session_ready("minimax", session["id"], "h3-worker")
        job = broker.register_job("minimax", "guarded-job", "guarded-job-idem", "guarded job")
        permit = broker.request_permit("minimax", job["id"], profile["id"],
                                       "guarded-stage", "video", "h3-worker", None, session["id"])

        # respect_vram=true preserves the configurable safety reserve for
        # batch_task profiles. 10,000 + 5,000 + 2,048 exceeds 16,000.
        assert permit["status"] == "WAITING"
        assert permit["reason"] == "WAIT_VRAM"

        monitor.used = 1000
        broker.poll()
        assert broker.get_permit("minimax", permit["id"])["status"] == "ACTIVE"
    finally:
        broker.stop()


def test_old_database_adds_respect_vram_with_legacy_default(settings):
    with sqlite3.connect(settings.db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE projects (id TEXT PRIMARY KEY, label TEXT NOT NULL);
            INSERT INTO projects(id,label) VALUES ('minimax','MiniMax H3');
            CREATE TABLE profiles (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                label TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('realtime','batch')),
                peak_growth_mib INTEGER NOT NULL CHECK(peak_growth_mib > 0),
                max_seconds INTEGER NOT NULL CHECK(max_seconds > 0),
                enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
            );
            INSERT INTO profiles(id,project_id,label,kind,peak_growth_mib,max_seconds,created_at)
                VALUES ('legacy-profile','minimax','Legacy','batch',4000,1800,1);
            PRAGMA user_version=2;
            """
        )

    broker = Broker(settings, FakeMonitor())
    try:
        legacy = broker._one("SELECT * FROM profiles WHERE id='legacy-profile'")
        assert legacy["respect_vram"] == 0
        assert broker.conn.execute("PRAGMA user_version").fetchone()[0] == 4
        created = broker.create_profile("minimax", "New default", "batch", 4000, 1800)
        assert created["respect_vram"] is False
    finally:
        broker.stop()


def test_stalled_realtime_preparation_freezes_grants(settings):
    broker = Broker(replace(settings, session_prepare_timeout_seconds=1), FakeMonitor())
    try:
        broker.start()
        broker.set_allocation(True)
        broker.project_heartbeat("live_translate", "live-a", "online", 0)
        session = broker.request_session("live_translate", "stalled-session", "live-a")
        assert session["status"] == "PREPARING"
        with broker.transaction():
            broker.conn.execute("UPDATE sessions SET preparing_at=?,updated_at=?,heartbeat_at=? WHERE id=?",
                                (time.time() - 2, time.time(), time.time(), session["id"]))
        broker.poll()
        after = broker.get_session("live_translate", session["id"])
        assert after["status"] == "UNCERTAIN" and after["reason"] == "PREPARE_TIMEOUT"
    finally:
        broker.stop()


def test_preparing_timeout_is_not_extended_by_session_heartbeats(settings):
    broker = Broker(
        replace(settings, session_prepare_timeout_seconds=1.0), FakeMonitor()
    )
    try:
        broker.start()
        broker.set_allocation(True)
        broker.project_heartbeat("live_translate", "live-a", "online", 0)
        session = broker.request_session("live_translate", "preparing-timeout", "live-a")
        assert session["status"] == "PREPARING"
        assert session["preparing_at"] is not None

        with broker.transaction():
            broker.conn.execute(
                "UPDATE sessions SET preparing_at=?,updated_at=?,heartbeat_at=? WHERE id=?",
                (time.time() - 2, time.time(), time.time(), session["id"]),
            )

        after_heartbeat = broker.session_heartbeat("live_translate", session["id"], "live-a")
        assert after_heartbeat["status"] == "UNCERTAIN"
        assert after_heartbeat["reason"] == "PREPARE_TIMEOUT"
    finally:
        broker.stop()


def test_requested_session_still_uses_regular_online_timeout(settings):
    broker = Broker(
        replace(
            settings,
            heartbeat_timeout_seconds=0.05,
            active_heartbeat_grace_seconds=120.0,
        ),
        FakeMonitor(),
    )
    try:
        broker.start()
        broker.project_heartbeat("live_translate", "live-a", "online", 0)
        session = broker.request_session("live_translate", "requested-timeout", "live-a")
        assert session["status"] == "REQUESTED"
        with broker.transaction():
            broker.conn.execute(
                "UPDATE sessions SET heartbeat_at=? WHERE id=?",
                (time.time() - 0.06, session["id"]),
            )

        broker.poll()
        expired = broker.get_session("live_translate", session["id"])
        assert expired["status"] == "CLOSED" and expired["reason"] == "OWNER_LOST"
    finally:
        broker.stop()


def test_legacy_preparing_session_migration_starts_bounded_window(tmp_path):
    now = time.time()
    db_path = tmp_path / "broker.sqlite3"
    with sqlite3.connect(db_path) as legacy:
        legacy.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY, label TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1, last_seen REAL,
                instance_id TEXT, reported_status TEXT, reported_resident_mib INTEGER NOT NULL DEFAULT 0,
                usage_seconds REAL NOT NULL DEFAULT 0
            );
            INSERT INTO projects(id,label,last_seen,instance_id)
                VALUES ('live_translate','Live Translate',0,'legacy-worker');
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                request_key TEXT NOT NULL, owner_instance TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'realtime', status TEXT NOT NULL, reason TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, heartbeat_at REAL NOT NULL,
                UNIQUE(project_id, request_key)
            );
            PRAGMA user_version=3;
            """
        )
        legacy.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("legacy-session", "live_translate", "legacy-key", "legacy-worker", "realtime",
             "PREPARING", None, now - 1000, now - 1000, now),
        )

    settings = Settings(
        data_dir=tmp_path, managed_gpu_uuid=GPU, display_gpu_uuid=DISPLAY,
        admin_token="admin-test-token", project_tokens={
            "minimax": "mini-test-token", "live_translate": "live-test-token", "manga": "manga-test-token"
        },
        session_prepare_timeout_seconds=1.0,
    )
    broker = Broker(settings, FakeMonitor())
    try:
        migrated = broker.get_session("live_translate", "legacy-session")
        assert migrated["preparing_at"] >= now
        assert broker.conn.execute("PRAGMA user_version").fetchone()[0] == 4
        broker.poll()
        assert broker.get_session("live_translate", "legacy-session")["status"] == "PREPARING"
    finally:
        broker.stop()


def test_api_auth_and_static(settings):
    monitor = FakeMonitor()
    app = create_app(settings, monitor)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/v1/ready").status_code == 200
        monitor.ok = False
        app.state.broker.poll()
        assert client.get("/v1/ready").status_code == 503
        assert client.get("/v1/dashboard").status_code == 401
        assert client.get("/v1/dashboard", headers={"Authorization": "Bearer mini-test-token"}).status_code == 403
        response = client.get("/v1/dashboard", headers={"Authorization": "Bearer admin-test-token"})
        assert response.status_code == 200
        assert response.json()["allocation_enabled"] is False
        assert response.json()["heartbeat_timeout_seconds"] == 15.0
        assert response.json()["active_heartbeat_grace_seconds"] == 180.0
        assert client.post("/v1/admin/allocation", json={"enabled": True},
            headers={"Authorization": "Bearer admin-test-token", "Origin": "http://evil.test"}).status_code == 403


def test_configured_public_origin_accepts_admin_write(settings):
    app = create_app(replace(settings, public_origin="https://gpu.example.org"), FakeMonitor())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer admin-test-token", "Origin": "https://gpu.example.org"}
        assert client.post("/v1/admin/allocation", json={"enabled": True}, headers=headers).status_code == 200
        assert client.post("/v1/admin/allocation", json={"enabled": False},
            headers={**headers, "Origin": "https://other.example.org"}).status_code == 403


def test_admin_can_create_profile_that_checks_live_vram(settings):
    app = create_app(settings, FakeMonitor())
    with TestClient(app) as client:
        response = client.post(
            "/v1/profiles",
            json={"project_id": "minimax", "label": "guarded experiment", "kind": "batch",
                  "peak_growth_mib": 5000, "max_seconds": 1800, "respect_vram": True},
            headers={"Authorization": "Bearer admin-test-token"},
        )
        assert response.status_code == 201
        assert response.json()["respect_vram"] is True
        dashboard = client.get(
            "/v1/dashboard", headers={"Authorization": "Bearer admin-test-token"}
        ).json()
        guarded = next(profile for profile in dashboard["profiles"]
                       if profile["id"] == response.json()["id"])
        assert guarded["respect_vram"] is True
        legacy = client.post(
            "/v1/profiles",
            json={"project_id": "minimax", "label": "legacy default", "kind": "batch",
                  "peak_growth_mib": 5000, "max_seconds": 1800},
            headers={"Authorization": "Bearer admin-test-token"},
        )
        assert legacy.status_code == 201
        assert legacy.json()["respect_vram"] is False


def test_project_can_reconcile_only_its_own_confirmed_inactive_task(settings):
    app = create_app(settings, FakeMonitor())
    with TestClient(app) as client:
        broker = app.state.broker
        profile = broker.create_profile("minimax", "H3", "batch", 4000, 1800)
        broker.set_allocation(True)
        broker.project_heartbeat("minimax", "h3-worker", "online", 0)
        session = broker.request_session("minimax", "recovery-session", "h3-worker", "batch_task")
        broker.session_ready("minimax", session["id"], "h3-worker")
        job = broker.register_job("minimax", "recovery-job", "recovery-idem", "recovery")
        permit = broker.request_permit("minimax", job["id"], profile["id"],
                                       "recovery-stage", "video", "h3-worker", None, session["id"])
        with broker.transaction():
            broker.conn.execute("UPDATE permits SET status='UNCERTAIN' WHERE id=?", (permit["id"],))
            broker.conn.execute("UPDATE sessions SET status='UNCERTAIN' WHERE id=?", (session["id"],))
        payload = {"backend_confirmed_inactive": True,
                   "evidence": "Comfy history finished and queue empty for exact backend UUID"}
        wrong = {"Authorization": "Bearer manga-test-token"}
        own = {"Authorization": "Bearer mini-test-token"}
        permit_url = f"/v1/projects/minimax/permits/{permit['id']}/reconcile"
        session_url = f"/v1/projects/minimax/sessions/{session['id']}/reconcile"
        assert client.post(permit_url, json=payload, headers=wrong).status_code == 403
        assert client.post(permit_url, json={**payload, "backend_confirmed_inactive": False},
                           headers=own).status_code == 422
        assert client.post(permit_url, json=payload, headers=own).json()["status"] == "FINISHED"
        assert client.post(session_url, json=payload, headers=own).json()["status"] == "CLOSED"


def test_monitor_falls_back_when_nvml_read_fails(monkeypatch):
    monitor = object.__new__(NvidiaMonitor)
    monitor._nvml = object()
    monkeypatch.setattr(monitor, "_read_nvml", lambda: (_ for _ in ()).throw(RuntimeError("NVML read failed")))
    monkeypatch.setattr(monitor, "_read_smi", lambda: [{"uuid": GPU, "used_mib": 1}])
    result = monitor.read()
    assert result["ok"] is True and result["source"] == "nvidia-smi"


def test_monitor_defaults_to_timeout_bounded_out_of_process_smi(monkeypatch):
    monitor = NvidiaMonitor()
    assert monitor._nvml is None
    assert monitor._nvml_error == "in_process_nvml_disabled"
    monkeypatch.setattr(
        monitor, "_read_nvml",
        lambda: (_ for _ in ()).throw(AssertionError("in-process NVML must not run")),
    )
    monkeypatch.setattr(monitor, "_read_smi", lambda: [{"uuid": GPU, "used_mib": 1}])

    result = monitor.read()

    assert result["ok"] is True
    assert result["source"] == "nvidia-smi"


def test_transaction_preserves_error_after_sqlite_cancels_transaction():
    class CancelledConnection:
        def __init__(self):
            self.in_transaction = False
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)
            if statement == "BEGIN IMMEDIATE":
                self.in_transaction = True

    broker = object.__new__(Broker)
    broker._lock = threading.RLock()
    broker.conn = CancelledConnection()

    with pytest.raises(RuntimeError, match="original disk full"):
        with broker.transaction():
            # Fatal SQLite I/O errors may end the transaction before Python's
            # context manager receives the exception.
            broker.conn.in_transaction = False
            raise RuntimeError("original disk full")

    assert broker.conn.statements == ["BEGIN IMMEDIATE"]
