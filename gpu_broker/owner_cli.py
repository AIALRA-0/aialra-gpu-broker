"""Run the independent GPU Owner API on loopback only."""

from __future__ import annotations

import argparse
import os
import threading
from pathlib import Path

import uvicorn

from .config import default_data_dir
from .owner_api import create_owner_app
from .owner_runtime import load_owner_runtime_settings, make_owner_coordinator


class OwnerProcessLock:
    """Prevent two runtime servers from repeatedly resetting one Owner row."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None


    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
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
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Another GPU Owner process is running") from exc
        return self

    def __exit__(self, *_):
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


class OwnerObservationPoller:
    """Refresh direct facts; failures never stop an already running model."""

    def __init__(self, coordinator, credential: str, interval_seconds: float = 5.0):
        if interval_seconds <= 0:
            raise ValueError("Owner observation interval must be positive")
        self.coordinator = coordinator
        self.credential = credential
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Owner observation poller is already running")
        self._thread = threading.Thread(
            target=self._run, name="owner-direct-observation", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.coordinator.observe(self.credential)
            except Exception:
                # The last persisted sample ages out in the dashboard. No
                # monitoring exception may terminate the Owner API.
                pass
            self._stop.wait(self.interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local 4080 ownership coordinator")
    parser.add_argument("command", choices=["serve", "check-config"])
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--port", type=int, default=18767)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")
    settings = load_owner_runtime_settings(args.data_dir)
    if args.command == "check-config":
        # This validates local settings and credentials without printing any
        # secret, connecting to a project observer, or changing Owner state.
        print("Owner configuration is structurally valid")
        return
    with OwnerProcessLock(settings.data_dir / "owner.lock"):
        coordinator = make_owner_coordinator(settings)
        poller = OwnerObservationPoller(coordinator, settings.project_tokens["h3"])
        poller.start()
        try:
            uvicorn.run(
                create_owner_app(
                    coordinator,
                    settings.project_tokens,
                    settings.admin_token,
                ),
                host="127.0.0.1", port=args.port, workers=1, reload=False,
                access_log=False,
            )
        finally:
            poller.stop()


if __name__ == "__main__":
    main()
