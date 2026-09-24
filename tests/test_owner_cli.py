import threading
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gpu_broker.owner_cli import OwnerObservationPoller, OwnerProcessLock


def test_second_owner_process_cannot_take_same_lock(tmp_path):
    path = tmp_path / "owner.lock"
    with OwnerProcessLock(path):
        with pytest.raises(RuntimeError, match="Another GPU Owner"):
            with OwnerProcessLock(path):
                pass
    with OwnerProcessLock(path):
        pass


def test_background_observation_recovers_after_a_failed_probe():
    observed_twice = threading.Event()

    class Coordinator:
        def __init__(self):
            self.calls = 0

        def observe(self, credential):
            assert credential == "fixture"
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary observer failure")
            observed_twice.set()

    coordinator = Coordinator()
    poller = OwnerObservationPoller(
        coordinator, "fixture", interval_seconds=0.01
    )
    poller.start()
    try:
        assert observed_twice.wait(2)
    finally:
        poller.stop()
    assert coordinator.calls >= 2


def test_second_process_cannot_start_on_the_same_owner_database(tmp_path):
    path = tmp_path / "owner.lock"
    probe = (
        "from pathlib import Path\n"
        "from gpu_broker.owner_cli import OwnerProcessLock\n"
        "with OwnerProcessLock(Path(__import__('sys').argv[1])): pass\n"
    )
    with OwnerProcessLock(path):
        child = subprocess.run(
            [sys.executable, "-c", probe, str(path)],
            capture_output=True, text=True, timeout=10,
        )
        assert child.returncode != 0
        assert "Another GPU Owner process is running" in child.stderr


def test_check_config_does_not_start_or_mutate_owner(tmp_path, monkeypatch, capsys):
    from gpu_broker import owner_cli

    monkeypatch.setattr(sys, "argv", ["gpu-owner", "check-config", "--data-dir", str(tmp_path)])
    monkeypatch.setattr(
        owner_cli, "load_owner_runtime_settings",
        lambda path: SimpleNamespace(data_dir=path),
    )

    def forbidden(_settings):
        raise AssertionError("configuration validation must not initialize Owner")

    monkeypatch.setattr(owner_cli, "make_owner_coordinator", forbidden)
    owner_cli.main()
    assert capsys.readouterr().out.strip() == "Owner configuration is structurally valid"
    assert not (tmp_path / "owner.sqlite3").exists()
