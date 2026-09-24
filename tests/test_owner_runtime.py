from __future__ import annotations

import json
import hashlib
import hmac
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from gpu_broker.owner import ObservationState
from gpu_broker.owner_runtime import (
    LocalOwnerObserver,
    OwnerRuntimeSettings,
    _loopback_url,
    load_owner_runtime_settings,
    make_authenticator,
)


class IdleAdapter(BaseHTTPRequestHandler):
    def do_GET(self):
        # The challenge never transmits the project credential to a port that
        # may have been taken over while the real adapter is stopped.
        assert self.headers.get("Authorization") is None
        nonce = parse_qs(urlsplit(self.path).query).get("nonce", [None])[0]
        payload = {
            "nonce": nonce,
            "project": self.server.project,
            "status": "IDLE",
            "observed_at": time.time(),
            "owner_instance": None,
            "model_released": True,
            "child_processes_exited": True,
            "entry_fenced": True,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        payload["signature"] = hmac.new(
            self.server.token.encode(), canonical, hashlib.sha256,
        ).hexdigest()
        if getattr(self.server, "tamper", False):
            payload["entry_fenced"] = False
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class FakeMonitor:
    def read(self):
        return {
            "ok": True,
            "timestamp": time.time(),
            "gpus": [{
                "uuid": "GPU-owner-test", "used_mib": 700,
                "utilization_pct": 0,
            }],
        }


def test_local_observer_fetches_three_fresh_authenticated_facts(tmp_path):
    servers = []
    threads = []
    tokens = {project: project + "-" + "x" * 32 for project in ("h3", "live", "manga")}
    try:
        urls = {}
        for project in tokens:
            server = ThreadingHTTPServer(("127.0.0.1", 0), IdleAdapter)
            server.project = project
            server.token = tokens[project]
            server.tamper = False
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append(server)
            threads.append(thread)
            urls[project] = f"http://127.0.0.1:{server.server_port}/owner/observe"

        settings = OwnerRuntimeSettings(
            tmp_path, "GPU-owner-test", 1000, 5, urls, tokens, "admin-" + "y" * 32,
        )
        bundle = LocalOwnerObserver(settings, monitor=FakeMonitor())()
        assert set(bundle.projects) == {"h3", "live", "manga"}
        assert all(item.status is ObservationState.IDLE and item.entry_fenced is True
                   for item in bundle.projects.values())
        assert bundle.gpu.healthy and bundle.gpu.safe_idle
        identity = make_authenticator(settings)
        assert identity(tokens["h3"]) == "h3"
        assert identity(settings.admin_token) == "admin"
        assert identity("unknown") is None

        servers[0].tamper = True
        with pytest.raises(ValueError, match="signature invalid"):
            LocalOwnerObserver(settings, monitor=FakeMonitor())()
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


@pytest.mark.parametrize("url", [
    "http://example.com:9000/observe",
    "http://localhost:9000/observe",
    "https://127.0.0.1:9000/observe",
    "http://127.0.0.1@evil.example:9000/observe",
])
def test_owner_observers_must_use_direct_loopback(url):
    with pytest.raises(ValueError):
        _loopback_url(url)


def test_owner_runtime_refuses_guessed_idle_threshold(tmp_path):
    (tmp_path / "owner.json").write_text(json.dumps({
        "managed_gpu_uuid": "GPU-owner-test",
        "idle_max_utilization_pct": 5,
        "observer_urls": {
            project: "http://127.0.0.1:9000/observe"
            for project in ("h3", "live", "manga")
        },
    }), encoding="utf-8")
    (tmp_path / "tokens.json").write_text(json.dumps({
        "admin": "a" * 40,
        "projects": {
            "minimax": "h" * 40,
            "live_translate": "l" * 40,
            "manga": "m" * 40,
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="measured idle_max_mib"):
        load_owner_runtime_settings(tmp_path)

    config = json.loads((tmp_path / "owner.json").read_text(encoding="utf-8"))
    config["idle_max_mib"] = 100
    config["idle_max_utilization_pct"] = True
    (tmp_path / "owner.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="idle_max_utilization_pct"):
        load_owner_runtime_settings(tmp_path)

    config["idle_max_utilization_pct"] = 5
    (tmp_path / "owner.json").write_text(json.dumps(config), encoding="utf-8")
    with sqlite3.connect(tmp_path / "broker.sqlite3") as connection:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO meta VALUES ('allocation_enabled', '1')")
    with pytest.raises(ValueError, match="Legacy allocation must be paused"):
        load_owner_runtime_settings(tmp_path)
    with sqlite3.connect(tmp_path / "broker.sqlite3") as connection:
        connection.execute("UPDATE meta SET value='0' WHERE key='allocation_enabled'")
    assert load_owner_runtime_settings(tmp_path).idle_max_mib == 100
