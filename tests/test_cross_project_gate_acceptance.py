from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from gpu_broker.api import create_app
from gpu_broker.config import Settings
from gpu_broker.core import Broker


GPU = "GPU-cross-project-test"
DISPLAY = "GPU-cross-project-display"
PROJECTS = ("minimax", "live_translate", "manga")


class FakeMonitor:
    def read(self):
        return {
            "ok": True,
            "timestamp": time.time(),
            "error": None,
            "source": "cross-project-test",
            "host": {"cpu_pct": 0, "ram_used_mib": 0, "ram_total_mib": 1},
            "gpus": [
                {
                    "uuid": GPU,
                    "name": "Isolated test GPU",
                    "total_mib": 16000,
                    "used_mib": 1000,
                    "free_mib": 15000,
                    "utilization_pct": 0,
                    "temperature_c": 30,
                    "driver": "test",
                },
                {
                    "uuid": DISPLAY,
                    "name": "Isolated display GPU",
                    "total_mib": 8000,
                    "used_mib": 1000,
                    "free_mib": 7000,
                    "utilization_pct": 0,
                    "temperature_c": 30,
                    "driver": "test",
                },
            ],
        }


class SimulatedProjectGpuClient:
    """Exercise the project-token API contract with a counted fake model call."""

    def __init__(self, http, project: str, token: str, profile_id: str):
        self.http = http
        self.project = project
        self.owner = f"{project}-offline-test-worker"
        self.profile_id = profile_id
        self.headers = {"Authorization": f"Bearer {token}"}
        self.invocations = 0
        self.job_id: str | None = None
        self.permit_id: str | None = None

    def request(self, method: str, path: str, *, json: dict | None = None) -> dict:
        response = self.http.request(method, path, headers=self.headers, json=json)
        assert response.status_code in (200, 201), (method, path, response.status_code, response.text)
        return response.json()

    def prepare(self, suffix: str) -> None:
        prefix = f"/v1/projects/{self.project}"
        self.request("POST", prefix + "/heartbeat", json={
            "instance_id": self.owner, "status": "online", "resident_mib": 0,
        })
        job = self.request("POST", prefix + "/jobs", json={
            "external_id": f"offline-{self.project}-{suffix}",
            "idempotency_key": f"offline-{self.project}-{suffix}-idempotency",
            "label": f"{self.project} offline acceptance",
        })
        self.job_id = job["id"]
        self.request("PATCH", f"{prefix}/jobs/{self.job_id}", json={"status": "WAITING_GPU"})
        permit = self.request("POST", prefix + "/permits", json={
            "job_id": self.job_id,
            "profile_id": self.profile_id,
            "request_key": f"offline-{self.project}-{suffix}-request",
            "stage": "fake_model_execution",
            "owner_instance": self.owner,
        })
        self.permit_id = permit["id"]

    def permit(self) -> dict:
        assert self.permit_id is not None
        return self.request(
            "GET", f"/v1/projects/{self.project}/permits/{self.permit_id}"
        )

    def invoke_if_active(self, *, finish: bool = True) -> bool:
        """The fake model is called only after observing ACTIVE from the shared API."""
        permit = self.permit()
        if permit["status"] != "ACTIVE":
            return False

        self.invocations += 1
        if finish:
            assert self.job_id is not None and self.permit_id is not None
            prefix = f"/v1/projects/{self.project}"
            self.request("PATCH", f"{prefix}/jobs/{self.job_id}", json={"status": "RUNNING"})
            self.request("POST", f"{prefix}/permits/{self.permit_id}/finish", json={
                "owner_instance": self.owner,
                "result": "COMPLETED",
                "backend_confirmed_inactive": True,
                "resident_mib": 0,
            })
            self.request("PATCH", f"{prefix}/jobs/{self.job_id}", json={"status": "COMMITTING"})
            self.request("PATCH", f"{prefix}/jobs/{self.job_id}", json={"status": "COMPLETED"})
        return True

    def reconcile_confirmed_inactive(self) -> None:
        assert self.permit_id is not None
        self.request(
            "POST",
            f"/v1/projects/{self.project}/permits/{self.permit_id}/reconcile",
            json={
                "backend_confirmed_inactive": True,
                "evidence": "Fake backend process exited before Broker restart test",
            },
        )


def make_settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        managed_gpu_uuid=GPU,
        display_gpu_uuid=DISPLAY,
        admin_token="cross-project-admin-test-token",
        project_tokens={
            "minimax": "cross-project-h3-test-token",
            "live_translate": "cross-project-live-test-token",
            "manga": "cross-project-manga-test-token",
        },
        sample_interval_seconds=60,
        stale_sample_seconds=10,
    )


def make_clients(
    http: TestClient,
    settings: Settings,
    broker: Broker,
    suffix: str,
    profile_ids: dict[str, str] | None = None,
):
    clients = []
    for project in PROJECTS:
        if profile_ids is None:
            profile = broker.create_profile(project, f"{project} test", "batch", 1000, 60)
            profile_id = profile["id"]
        else:
            profile_id = profile_ids[project]
        client = SimulatedProjectGpuClient(
            http, project, settings.project_tokens[project], profile_id
        )
        client.prepare(suffix)
        clients.append(client)
    return clients


def assert_at_most_one_active(clients: list[SimulatedProjectGpuClient]) -> None:
    assert sum(client.permit()["status"] == "ACTIVE" for client in clients) <= 1


def test_three_project_fake_calls_obey_pause_contention_and_restart_reconcile(tmp_path):
    """One isolated Broker fails closed while paused or uncertain, then drains waiters."""
    settings = make_settings(tmp_path)
    monitor = FakeMonitor()
    first_app = create_app(settings, monitor)

    with TestClient(first_app) as http:
        first_broker = first_app.state.broker
        clients = make_clients(http, settings, first_broker, "paused-wave")

        assert [client.permit()["reason"] for client in clients] == [
            "WAIT_PAUSED", "WAIT_PAUSED", "WAIT_PAUSED",
        ]
        assert [client.invoke_if_active() for client in clients] == [False, False, False]
        assert [client.invocations for client in clients] == [0, 0, 0]

        admin = {"Authorization": f"Bearer {settings.admin_token}"}
        enabled = http.post("/v1/admin/allocation", json={"enabled": True}, headers=admin)
        assert enabled.status_code == 200
        assert_at_most_one_active(clients)

        # Release each permit only after its fake backend call completes. Each waiter
        # must become runnable in turn, with no second project active at the same time.
        completed_projects = set()
        while len(completed_projects) < len(clients):
            active = [client for client in clients if client.permit()["status"] == "ACTIVE"]
            assert len(active) == 1
            current = active[0]
            assert current.project not in completed_projects
            assert current.invoke_if_active()
            completed_projects.add(current.project)
            assert_at_most_one_active(clients)

        assert [client.invocations for client in clients] == [1, 1, 1]

        # Start another wave and lose the Broker while one fake backend call is active.
        # The adapter contract must not replay that uncertain call after restart.
        profile_ids = {client.project: client.profile_id for client in clients}
        uncertain_wave = make_clients(
            http, settings, first_broker, "restart-wave", profile_ids
        )
        active = [client for client in uncertain_wave if client.permit()["status"] == "ACTIVE"]
        assert len(active) == 1
        uncertain_owner = active[0]
        assert uncertain_owner.invoke_if_active(finish=False)
        assert [client.invoke_if_active() for client in uncertain_wave if client is not uncertain_owner] == [
            False, False,
        ]
        assert_at_most_one_active(uncertain_wave)
        uncertain_rows = [
            (client.project, client.profile_id, client.job_id, client.permit_id, client.invocations)
            for client in uncertain_wave
        ]

    restarted_app = create_app(settings, monitor)
    with TestClient(restarted_app) as http:
        restored_wave = []
        for project, profile_id, job_id, permit_id, invocations in uncertain_rows:
            restored = SimulatedProjectGpuClient(
                http, project, settings.project_tokens[project], profile_id
            )
            restored.job_id = job_id
            restored.permit_id = permit_id
            restored.invocations = invocations
            restored_wave.append(restored)

        statuses = [client.permit() for client in restored_wave]
        uncertain_index = next(
            index for index, permit in enumerate(statuses) if permit["status"] == "UNCERTAIN"
        )
        assert statuses[uncertain_index]["reason"] == "BROKER_RESTART"
        for index, permit in enumerate(statuses):
            if index != uncertain_index:
                assert permit["status"] == "WAITING"
                assert permit["reason"] == "WAIT_RECONCILE"
        assert [client.invoke_if_active() for client in restored_wave] == [False, False, False]
        assert [client.invocations for client in restored_wave] == [1, 0, 0]

        uncertain_owner = restored_wave[uncertain_index]
        uncertain_owner.reconcile_confirmed_inactive()

        pending = [
            client for client in restored_wave if client is not uncertain_owner
        ]
        completed_projects = set()
        while len(completed_projects) < len(pending):
            active = [client for client in pending if client.permit()["status"] == "ACTIVE"]
            assert len(active) == 1
            current = active[0]
            assert current.invoke_if_active()
            completed_projects.add(current.project)
            assert_at_most_one_active(restored_wave)

        assert uncertain_owner.invocations == 1
        assert sorted(client.invocations for client in restored_wave) == [1, 1, 1]
