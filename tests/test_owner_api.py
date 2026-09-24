from __future__ import annotations

import asyncio
import secrets
import sqlite3
import threading
import time

from fastapi.testclient import TestClient
import httpx

from gpu_broker.owner import (
    GpuObservation,
    ObservationBundle,
    ObservationState,
    OwnerCoordinator,
    ProjectObservation,
)
from gpu_broker.owner_api import create_owner_app
from gpu_broker.owner_auth import sign_request, verify_response


def _headers(
    token: str,
    project: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    nonce: str | None = None,
    timestamp: float | None = None,
) -> tuple[dict[str, str], str]:
    actual_nonce = nonce or secrets.token_urlsafe(24)
    actual_timestamp = time.time() if timestamp is None else timestamp
    signed_body = body or {}
    return {
        "X-Owner-Project": project,
        "X-Owner-Nonce": actual_nonce,
        "X-Owner-Timestamp": str(actual_timestamp),
        "X-Owner-Signature": sign_request(
            token,
            project=project,
            nonce=actual_nonce,
            timestamp=actual_timestamp,
            method=method,
            path=path,
            body=signed_body,
        ),
    }, actual_nonce


def _signed_result(response, token: str, nonce: str) -> dict:
    assert response.status_code == 200
    envelope = response.json()
    assert envelope["nonce"] == nonce
    assert verify_response(
        token,
        nonce=nonce,
        result=envelope["result"],
        signature=envelope["signature"],
    )
    return envelope["result"]


def test_slow_direct_observation_does_not_block_health(tmp_path):
    started = threading.Event()
    finish = threading.Event()
    tokens = {name: name + "-" + name[0] * 40 for name in ("h3", "live", "manga")}
    admin_token = "admin-" + "a" * 40
    identities = {value: name for name, value in tokens.items()}
    identities[admin_token] = "admin"

    def slow_observation(projects):
        started.set()
        finish.wait(timeout=2)
        now = time.time()
        return ObservationBundle(
            projects={
                project: ProjectObservation(
                    project=project, status=ObservationState.IDLE, observed_at=now,
                    model_released=True, child_processes_exited=True, entry_fenced=True,
                )
                for project in projects
            },
            gpu=GpuObservation(observed_at=now, healthy=True, safe_idle=True),
        )

    coordinator = OwnerCoordinator(
        tmp_path / "owner.sqlite3", "GPU-test", slow_observation,
        authenticator=identities.get,
    )
    app = create_owner_app(coordinator, tokens, admin_token)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            headers, _ = _headers(tokens["h3"], "h3", "POST", "/v1/owner/observe")
            observe_task = asyncio.create_task(
                client.post("/v1/owner/observe", headers=headers),
            )
            try:
                assert await asyncio.to_thread(started.wait, 1)
                health = await asyncio.wait_for(client.get("/v1/health"), timeout=1)
                assert health.status_code == 200
                assert health.json() == {"running": True}
            finally:
                finish.set()
                await observe_task

    asyncio.run(check())


def test_loopback_owner_contract_uses_mutual_hmac_and_read_only_snapshot(tmp_path):
    probes = 0
    active_window = False
    tokens = {
        "h3": "h3-secret-" + "h" * 40,
        "live": "live-secret-" + "l" * 40,
        "manga": "manga-secret-" + "m" * 40,
    }
    admin_token = "admin-secret-" + "a" * 40

    def observe(projects):
        nonlocal probes
        probes += 1
        now = time.time()
        return ObservationBundle(
            projects={
                project: ProjectObservation(
                    project=project,
                    status=(ObservationState.BUSY if active_window and project == "h3"
                            else ObservationState.IDLE),
                    observed_at=now,
                    owner_instance="h3-owner-instance" if active_window and project == "h3" else None,
                    model_released=not (active_window and project == "h3"),
                    child_processes_exited=not (active_window and project == "h3"),
                    entry_fenced=not (active_window and project == "h3"),
                )
                for project in projects
            },
            gpu=GpuObservation(observed_at=now, healthy=True, safe_idle=True),
        )

    identities = {token: project for project, token in tokens.items()}
    identities[admin_token] = "admin"
    coordinator = OwnerCoordinator(
        tmp_path / "owner.sqlite3", "GPU-test", observe,
        authenticator=identities.get,
    )
    client = TestClient(create_owner_app(coordinator, tokens, admin_token))
    assert client.get("/v1/health").json() == {"running": True}

    # A captured/reused Bearer token is never accepted by the new Owner API.
    assert client.get(
        "/v1/owner", headers={"Authorization": f"Bearer {admin_token}"},
    ).status_code == 401
    assert client.post(
        "/v1/owner/acquire",
        headers={"Authorization": f"Bearer {tokens['h3']}"},
        json={"owner_instance": "h3-owner-instance"},
    ).status_code == 401

    admin_headers, admin_nonce = _headers(
        admin_token, "admin", "GET", "/v1/owner",
    )
    admin_snapshot = _signed_result(
        client.get("/v1/owner", headers=admin_headers), admin_token, admin_nonce,
    )
    assert admin_snapshot["state"] == "UNKNOWN"
    assert probes == 0  # Read-only signed snapshots do not trigger a probe.

    admin_headers, _ = _headers(
        admin_token, "admin", "POST", "/v1/owner/acquire",
        {"owner_instance": "admin-cannot-acquire"},
    )
    assert client.post(
        "/v1/owner/acquire", headers=admin_headers,
        json={"owner_instance": "admin-cannot-acquire"},
    ).status_code == 401

    h3_headers, h3_nonce = _headers(
        tokens["h3"], "h3", "POST", "/v1/owner/acquire",
        {"owner_instance": "h3-owner-instance"},
    )
    acquired = client.post(
        "/v1/owner/acquire", headers=h3_headers,
        json={"owner_instance": "h3-owner-instance"},
    )
    acquired_result = _signed_result(acquired, tokens["h3"], h3_nonce)
    assert acquired_result["decision"] == "ACQUIRED"
    assert acquired_result["owner_project"] == "h3"
    assert acquired.headers["Cache-Control"] == "no-store"
    active_window = True

    live_body = {"owner_instance": "live-owner-instance"}
    live_headers, live_nonce = _headers(
        tokens["live"], "live", "POST", "/v1/owner/acquire", live_body,
    )
    waiting = _signed_result(
        client.post("/v1/owner/acquire", headers=live_headers, json=live_body),
        tokens["live"], live_nonce,
    )
    assert waiting["decision"] == "WAITING"
    assert waiting["owner_instance"] is None

    release_body = {"owner_instance": "h3-owner-instance"}
    release_headers, release_nonce = _headers(
        tokens["live"], "live", "POST", "/v1/owner/release", release_body,
    )
    rejected = _signed_result(
        client.post("/v1/owner/release", headers=release_headers, json=release_body),
        tokens["live"], release_nonce,
    )
    assert rejected["decision"] == "REJECTED"

    admin_headers, admin_nonce = _headers(
        admin_token, "admin", "GET", "/v1/owner",
    )
    admin_snapshot = _signed_result(
        client.get("/v1/owner", headers=admin_headers), admin_token, admin_nonce,
    )
    assert admin_snapshot["state"] == "OWNED"
    assert admin_snapshot["owner_instance"] is None

    # The signed read-only endpoint must not advertise an expired OWNED
    # observation as current, even while the durable row remains intact.
    probe_count = probes
    coordinator._clock = lambda: time.time() + 11
    stale_headers, stale_nonce = _headers(
        tokens["h3"], "h3", "GET", "/v1/owner",
    )
    stale_snapshot = _signed_result(
        client.get("/v1/owner", headers=stale_headers), tokens["h3"], stale_nonce,
    )
    assert stale_snapshot["state"] == "UNKNOWN"
    assert stale_snapshot["decision"] == "UNKNOWN"
    assert stale_snapshot["owner_instance"] == "h3-owner-instance"
    assert probes == probe_count
    with sqlite3.connect(tmp_path / "owner.sqlite3") as connection:
        assert connection.execute("SELECT state FROM owner_state").fetchone()[0] == "OWNED"

    # Owner v1 still grants without any legacy Broker business tables.
    with sqlite3.connect(tmp_path / "owner.sqlite3") as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert not tables.intersection({"jobs", "sessions", "permits", "profiles"})


def test_owner_api_rejects_tampered_body_and_stale_timestamp(tmp_path):
    tokens = {
        "h3": "h3-secret-" + "h" * 40,
        "live": "live-secret-" + "l" * 40,
        "manga": "manga-secret-" + "m" * 40,
    }
    admin_token = "admin-secret-" + "a" * 40

    def observe(projects):
        now = time.time()
        projects = {
            name: ProjectObservation(
                name, ObservationState.IDLE, now, model_released=True,
                child_processes_exited=True, entry_fenced=True,
            )
            for name in projects
        }
        return ObservationBundle(projects, GpuObservation(now, True, True))

    identities = {token: project for project, token in tokens.items()}
    identities[admin_token] = "admin"
    coordinator = OwnerCoordinator(
        tmp_path / "owner.sqlite3", "GPU-test", observe,
        authenticator=identities.get,
    )
    client = TestClient(create_owner_app(coordinator, tokens, admin_token))

    original = {"owner_instance": "signed-owner-instance"}
    headers, _ = _headers(
        tokens["h3"], "h3", "POST", "/v1/owner/acquire", original,
    )
    tampered = {"owner_instance": "changed-owner-instance"}
    assert client.post(
        "/v1/owner/acquire", headers=headers, json=tampered,
    ).status_code == 401
    assert client.post(
        "/v1/owner/acquire", headers=headers,
        content=b"x" * 4097,
    ).status_code == 413

    stale_headers, _ = _headers(
        tokens["h3"], "h3", "GET", "/v1/owner",
        timestamp=time.time() - 11.0,
    )
    assert client.get("/v1/owner", headers=stale_headers).status_code == 401


def test_owner_api_rejects_replayed_nonce_and_forged_response(tmp_path):
    tokens = {
        "h3": "h3-secret-" + "h" * 40,
        "live": "live-secret-" + "l" * 40,
        "manga": "manga-secret-" + "m" * 40,
    }
    admin_token = "admin-secret-" + "a" * 40

    def observe(projects):
        now = time.time()
        projects = {
            name: ProjectObservation(
                name, ObservationState.IDLE, now, model_released=True,
                child_processes_exited=True, entry_fenced=True,
            )
            for name in projects
        }
        return ObservationBundle(projects, GpuObservation(now, True, True))

    identities = {token: project for project, token in tokens.items()}
    identities[admin_token] = "admin"
    coordinator = OwnerCoordinator(
        tmp_path / "owner.sqlite3", "GPU-test", observe,
        authenticator=identities.get,
    )
    client = TestClient(create_owner_app(coordinator, tokens, admin_token))
    nonce = secrets.token_urlsafe(24)
    headers, _ = _headers(
        admin_token, "admin", "GET", "/v1/owner", nonce=nonce,
    )
    first = client.get("/v1/owner", headers=headers)
    result = _signed_result(first, admin_token, nonce)
    assert result["state"] == "UNKNOWN"
    assert client.get("/v1/owner", headers=headers).status_code == 401

    assert not verify_response(
        admin_token,
        nonce=nonce,
        result={**result, "state": "FREE"},
        signature=first.json()["signature"],
    )
