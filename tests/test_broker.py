from __future__ import annotations

import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from gpu_broker.api import create_app
from gpu_broker.config import Settings
from gpu_broker.core import Broker, BrokerError
from gpu_broker.monitor import NvidiaMonitor


GPU = "GPU-test-managed"
DISPLAY = "GPU-test-display"


class FakeMonitor:
    def __init__(self):
        self.ok = True
        self.used = 1000

    def read(self):
        return {
            "ok": self.ok, "timestamp": time.time(), "error": None if self.ok else "test outage",
            "source": "fake", "host": {"cpu_pct": 12, "ram_used_mib": 4096, "ram_total_mib": 16384},
            "gpus": [{
                "uuid": GPU, "name": "Test 4080", "total_mib": 16000,
                "used_mib": self.used, "free_mib": 16000 - self.used,
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
    settings = replace(settings, heartbeat_timeout_seconds=0.2)
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


def test_stalled_realtime_preparation_freezes_grants(settings):
    broker = Broker(replace(settings, session_prepare_timeout_seconds=1), FakeMonitor())
    try:
        broker.start()
        broker.set_allocation(True)
        broker.project_heartbeat("live_translate", "live-a", "online", 0)
        session = broker.request_session("live_translate", "stalled-session", "live-a")
        assert session["status"] == "PREPARING"
        with broker.transaction():
            broker.conn.execute("UPDATE sessions SET updated_at=? WHERE id=?",
                                (time.time() - 2, session["id"]))
        broker.poll()
        after = broker.get_session("live_translate", session["id"])
        assert after["status"] == "UNCERTAIN" and after["reason"] == "PREPARE_TIMEOUT"
    finally:
        broker.stop()


def test_api_auth_and_static(settings):
    app = create_app(settings, FakeMonitor())
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/v1/dashboard").status_code == 401
        assert client.get("/v1/dashboard", headers={"Authorization": "Bearer mini-test-token"}).status_code == 403
        response = client.get("/v1/dashboard", headers={"Authorization": "Bearer admin-test-token"})
        assert response.status_code == 200
        assert response.json()["allocation_enabled"] is False
        assert client.post("/v1/admin/allocation", json={"enabled": True},
            headers={"Authorization": "Bearer admin-test-token", "Origin": "http://evil.test"}).status_code == 403


def test_configured_public_origin_accepts_admin_write(settings):
    app = create_app(replace(settings, public_origin="https://gpu.example.org"), FakeMonitor())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer admin-test-token", "Origin": "https://gpu.example.org"}
        assert client.post("/v1/admin/allocation", json={"enabled": True}, headers=headers).status_code == 200
        assert client.post("/v1/admin/allocation", json={"enabled": False},
            headers={**headers, "Origin": "https://other.example.org"}).status_code == 403


def test_monitor_falls_back_when_nvml_read_fails(monkeypatch):
    monitor = object.__new__(NvidiaMonitor)
    monitor._nvml = object()
    monkeypatch.setattr(monitor, "_read_nvml", lambda: (_ for _ in ()).throw(RuntimeError("NVML read failed")))
    monkeypatch.setattr(monitor, "_read_smi", lambda: [{"uuid": GPU, "used_mib": 1}])
    result = monitor.read()
    assert result["ok"] is True and result["source"] == "nvidia-smi"
