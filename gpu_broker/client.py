"""Small synchronous adapter for the three local projects.

The adapter deliberately never starts a backend task itself. Project code must
check that a permit is ACTIVE immediately before GPU model load or submission.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class BrokerClientError(RuntimeError):
    pass


@dataclass
class BrokerClient:
    project_id: str
    token: str
    base_url: str = "http://127.0.0.1:18765"
    timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        if self.project_id not in {"minimax", "live_translate", "manga"}:
            raise ValueError("Unknown project")
        parsed = urlparse(self.base_url)
        local = parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
        remote = parsed.scheme == "https" and bool(parsed.hostname)
        if not (local or remote) or parsed.username or parsed.password or parsed.path not in ("", "/"):
            raise ValueError("Broker URL must be loopback HTTP or HTTPS without credentials or a path")
        self.base_url = self.base_url.rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            self.base_url + path, data=encoded, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                detail = json.load(exc).get("detail", str(exc))
            except Exception:
                detail = str(exc)
            raise BrokerClientError(f"Broker HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BrokerClientError(f"Broker unreachable: {exc}") from exc

    @property
    def prefix(self) -> str:
        return f"/v1/projects/{self.project_id}"

    def heartbeat(self, instance_id: str, status: str = "online", resident_mib: int = 0) -> dict:
        return self._request("POST", self.prefix + "/heartbeat", {
            "instance_id": instance_id, "status": status, "resident_mib": resident_mib,
        })

    def register_job(self, external_id: str, idempotency_key: str, label: str) -> dict:
        return self._request("POST", self.prefix + "/jobs", {
            "external_id": external_id, "idempotency_key": idempotency_key, "label": label,
        })

    def update_job(self, job_id: str, status: str, backend_id: str | None = None,
                   error_code: str | None = None) -> dict:
        return self._request("PATCH", self.prefix + f"/jobs/{job_id}", {
            "status": status, "backend_id": backend_id, "error_code": error_code,
        })

    def get_job(self, job_id: str) -> dict:
        return self._request("GET", self.prefix + f"/jobs/{job_id}")

    def request_session(self, request_key: str, instance_id: str) -> dict:
        return self._request("POST", self.prefix + "/sessions", {
            "request_key": request_key, "owner_instance": instance_id,
        })

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", self.prefix + f"/sessions/{session_id}")

    def session_heartbeat(self, session_id: str, instance_id: str) -> dict:
        return self._request("POST", self.prefix + f"/sessions/{session_id}/heartbeat",
                             {"owner_instance": instance_id})

    def session_ready(self, session_id: str, instance_id: str) -> dict:
        return self._request("POST", self.prefix + f"/sessions/{session_id}/ready",
                             {"owner_instance": instance_id})

    def close_session(self, session_id: str, instance_id: str,
                      backend_confirmed_inactive: bool) -> dict:
        return self._request("POST", self.prefix + f"/sessions/{session_id}/close", {
            "owner_instance": instance_id,
            "backend_confirmed_inactive": backend_confirmed_inactive,
        })

    def request_permit(self, job_id: str, profile_id: str, request_key: str,
                       stage: str, instance_id: str, backend_id: str | None = None,
                       session_id: str | None = None) -> dict:
        return self._request("POST", self.prefix + "/permits", {
            "job_id": job_id, "profile_id": profile_id, "request_key": request_key,
            "stage": stage, "owner_instance": instance_id,
            "backend_id": backend_id, "session_id": session_id,
        })

    def get_permit(self, permit_id: str) -> dict:
        return self._request("GET", self.prefix + f"/permits/{permit_id}")

    def permit_heartbeat(self, permit_id: str, instance_id: str,
                         backend_id: str | None = None) -> dict:
        return self._request("POST", self.prefix + f"/permits/{permit_id}/heartbeat", {
            "owner_instance": instance_id, "backend_id": backend_id,
        })

    def finish_permit(self, permit_id: str, instance_id: str, result: str,
                      backend_confirmed_inactive: bool, resident_mib: int = 0) -> dict:
        return self._request("POST", self.prefix + f"/permits/{permit_id}/finish", {
            "owner_instance": instance_id, "result": result,
            "backend_confirmed_inactive": backend_confirmed_inactive,
            "resident_mib": resident_mib,
        })

    def cancel_permit(self, permit_id: str) -> dict:
        return self._request("POST", self.prefix + f"/permits/{permit_id}/cancel")

    def wait_for_permit(self, permit_id: str, instance_id: str, deadline_seconds: float,
                        poll_seconds: float = 2.0) -> dict:
        """Keep the project alive while waiting; never return a non-ACTIVE grant."""
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            self.heartbeat(instance_id)
            permit = self.get_permit(permit_id)
            if permit["status"] == "ACTIVE":
                return permit
            if permit["status"] != "WAITING":
                raise BrokerClientError(f"Permit ended before grant: {permit['status']}")
            time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
        raise BrokerClientError("Permit wait deadline exceeded; do not start the GPU task")
