"""Trusted local observations for the standalone 4080 Owner process.

The three project adapters must answer a fresh challenge with generic GPU-use
facts. Missing, stale, or malformed observations freeze admission in UNKNOWN.
This module neither starts models nor stops any project process.
"""

from __future__ import annotations

import hmac
import hashlib
import ipaddress
import json
import secrets
import sqlite3
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .monitor import NvidiaMonitor
from .owner import (
    PROJECTS,
    GpuObservation,
    ObservationBundle,
    ObservationState,
    OwnerCoordinator,
    ProjectObservation,
)


PROJECT_TOKEN_KEYS = {"h3": "minimax", "live": "live_translate", "manga": "manga"}


@dataclass(frozen=True)
class OwnerRuntimeSettings:
    data_dir: Path
    gpu_uuid: str
    idle_max_mib: int
    idle_max_utilization_pct: int
    observer_urls: dict[str, str]
    project_tokens: dict[str, str]
    admin_token: str


def _loopback_url(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("owner observer URL must be a loopback HTTP URL")
    url = urllib.parse.urlsplit(raw)
    if (url.scheme != "http" or not url.hostname or url.username or url.password
            or url.query or url.fragment or not url.path.startswith("/")):
        raise ValueError("owner observer URL must be a loopback HTTP URL")
    try:
        if not ipaddress.ip_address(url.hostname).is_loopback:
            raise ValueError("owner observer URL must use a loopback IP address")
    except ValueError as exc:
        raise ValueError("owner observer URL must use a loopback IP address") from exc
    if not url.port or not 1 <= url.port <= 65535:
        raise ValueError("owner observer URL must contain a TCP port")
    return raw


def load_owner_runtime_settings(data_dir: Path) -> OwnerRuntimeSettings:
    """Load explicit idle calibration and the existing project credentials.

    Refusing to guess an idle threshold prevents a freshly installed Owner
    service from treating a resident 4080 model as safe to hand off.
    """

    root = data_dir.resolve()
    config = json.loads((root / "owner.json").read_text(encoding="utf-8"))
    tokens = json.loads((root / "tokens.json").read_text(encoding="utf-8"))
    gpu_uuid = config.get("managed_gpu_uuid")
    if not isinstance(gpu_uuid, str) or not gpu_uuid.startswith("GPU-"):
        raise ValueError("owner.json needs a real managed_gpu_uuid")
    idle_max_mib = config.get("idle_max_mib")
    idle_max_util = config.get("idle_max_utilization_pct")
    if not isinstance(idle_max_mib, int) or isinstance(idle_max_mib, bool) or idle_max_mib < 0:
        raise ValueError("owner.json needs measured idle_max_mib")
    if not isinstance(idle_max_util, int) or isinstance(idle_max_util, bool) or not 0 <= idle_max_util <= 100:
        raise ValueError("owner.json needs idle_max_utilization_pct")
    raw_urls = config.get("observer_urls")
    if not isinstance(raw_urls, dict) or set(raw_urls) != set(PROJECT_TOKEN_KEYS):
        raise ValueError("owner.json needs exactly h3, live, manga observer URLs")
    urls = {project: _loopback_url(raw_urls[project]) for project in PROJECT_TOKEN_KEYS}
    raw_tokens = tokens.get("projects")
    if not isinstance(raw_tokens, dict):
        raise ValueError("tokens.json project credentials are unavailable")
    project_tokens = {}
    for project, key in PROJECT_TOKEN_KEYS.items():
        value = raw_tokens.get(key)
        if not isinstance(value, str) or len(value) < 32:
            raise ValueError(f"tokens.json needs a strong {key} credential")
        project_tokens[project] = value
    admin = tokens.get("admin")
    if not isinstance(admin, str) or len(admin) < 32:
        raise ValueError("tokens.json needs a strong administrator credential")
    if len(set((*project_tokens.values(), admin))) != 4:
        raise ValueError("Owner credentials must be distinct")
    broker_db = root / "broker.sqlite3"
    if broker_db.is_file():
        try:
            with closing(sqlite3.connect(
                broker_db.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3
            )) as connection:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'allocation_enabled'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise ValueError("Legacy allocation state cannot be verified") from exc
        if row is None or row[0] != "0":
            raise ValueError("Legacy allocation must be paused before Owner starts")
    return OwnerRuntimeSettings(
        root, gpu_uuid, idle_max_mib, idle_max_util, urls, project_tokens, admin,
    )


def make_authenticator(settings: OwnerRuntimeSettings):
    """Map an opaque credential to one project or a read-only administrator."""

    def authenticate(credential: str) -> str | None:
        if not isinstance(credential, str):
            return None
        if hmac.compare_digest(credential, settings.admin_token):
            return "admin"
        for project, token in settings.project_tokens.items():
            if hmac.compare_digest(credential, token):
                return project
        return None

    return authenticate


class LocalOwnerObserver:
    def __init__(self, settings: OwnerRuntimeSettings, monitor: NvidiaMonitor | None = None):
        self.settings = settings
        self.monitor = monitor or NvidiaMonitor()
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _project(self, project: str) -> ProjectObservation:
        challenge = secrets.token_urlsafe(24)
        url = self.settings.observer_urls[project]
        separator = "&" if "?" in url else "?"
        request = urllib.request.Request(
            f"{url}{separator}nonce={urllib.parse.quote(challenge)}",
            headers={
                "Cache-Control": "no-store",
            },
        )
        with self._opener.open(request, timeout=2.0) as response:
            if response.status != 200:
                raise RuntimeError("owner observer did not return 200")
            payload = json.loads(response.read(16385))
        if not isinstance(payload, dict) or payload.get("nonce") != challenge:
            raise ValueError("owner observer challenge mismatch")
        if payload.get("project") != project:
            raise ValueError("owner observer project mismatch")
        signature = payload.pop("signature", None)
        if not isinstance(signature, str):
            raise ValueError("owner observer signature missing")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        expected = hmac.new(
            self.settings.project_tokens[project].encode("utf-8"),
            canonical,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("owner observer signature invalid")
        return ProjectObservation(
            project=project,
            status=ObservationState(payload["status"]),
            observed_at=payload["observed_at"],
            owner_instance=payload.get("owner_instance"),
            model_released=payload.get("model_released"),
            child_processes_exited=payload.get("child_processes_exited"),
            entry_fenced=payload.get("entry_fenced"),
        )

    def _gpu(self) -> GpuObservation:
        snapshot = self.monitor.read()
        if not snapshot.get("ok"):
            raise RuntimeError("GPU telemetry unavailable")
        card = next(
            (card for card in snapshot.get("gpus", [])
             if card.get("uuid") == self.settings.gpu_uuid), None,
        )
        if card is None:
            raise RuntimeError("managed GPU not found in fresh telemetry")
        used = card.get("used_mib")
        util = card.get("utilization_pct")
        if not isinstance(used, int) or not isinstance(util, int):
            raise RuntimeError("managed GPU memory or utilization unavailable")
        return GpuObservation(
            observed_at=snapshot["timestamp"],
            healthy=True,
            safe_idle=(used <= self.settings.idle_max_mib
                       and util <= self.settings.idle_max_utilization_pct),
        )

    def __call__(self, projects: frozenset[str] = PROJECTS) -> ObservationBundle:
        if not isinstance(projects, frozenset) or not projects <= PROJECTS:
            raise ValueError("requested Owner projects are invalid")
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="owner-observe") as pool:
            pending = {name: pool.submit(self._project, name) for name in projects}
            gpu = pool.submit(self._gpu)
            return ObservationBundle(
                projects={name: future.result() for name, future in pending.items()},
                gpu=gpu.result(),
            )


def make_owner_coordinator(settings: OwnerRuntimeSettings) -> OwnerCoordinator:
    return OwnerCoordinator(
        settings.data_dir / "owner.sqlite3",
        settings.gpu_uuid,
        LocalOwnerObserver(settings),
        make_authenticator(settings),
    )
