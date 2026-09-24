from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from gpu_broker.owner import (
    AuthorizationError,
    Decision,
    GpuObservation,
    ObservationBundle,
    ObservationState,
    OwnerCoordinator,
    OwnerState,
    ProjectObservation,
)


GPU = "GPU-owner-test"
TOKENS = {
    "h3-key": "h3",
    "live-key": "live",
    "manga-key": "manga",
    "admin-key": "admin",
}


class FakeClock:
    def __init__(self, value: float = 1_000.0):
        self.value = value
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.value

    def advance(self, seconds: float) -> float:
        with self.lock:
            self.value += seconds
            return self.value


class FakeProbe:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.statuses = {
            project: {
                "status": ObservationState.IDLE,
                "owner_instance": None,
                "model_released": True,
                "child_processes_exited": True,
                "entry_fenced": True,
            }
            for project in ("h3", "live", "manga")
        }
        self.gpu_healthy = True
        self.gpu_safe_idle = True
        self.returned: ObservationBundle | None = None
        self.barrier: threading.Barrier | None = None
        self._lock = threading.Lock()
        self.calls = 0

    def set_project(
        self,
        project: str,
        status: ObservationState,
        owner_instance: str | None = None,
        *,
        model_released: bool | None = True,
        child_processes_exited: bool | None = True,
        entry_fenced: bool | None = True,
    ) -> None:
        self.statuses[project] = {
            "status": status,
            "owner_instance": owner_instance,
            "model_released": model_released,
            "child_processes_exited": child_processes_exited,
            "entry_fenced": entry_fenced,
        }

    def __call__(self) -> ObservationBundle:
        with self._lock:
            self.calls += 1
            if self.returned is not None:
                return self.returned
            observed_at = self.clock.advance(0.01)
            bundle = ObservationBundle(
                projects={
                    project: ProjectObservation(
                        project=project,
                        observed_at=observed_at,
                        **values,
                    )
                    for project, values in self.statuses.items()
                },
                gpu=GpuObservation(
                    observed_at=observed_at,
                    healthy=self.gpu_healthy,
                    safe_idle=self.gpu_safe_idle,
                ),
            )
            barrier = self.barrier
        if barrier is not None:
            barrier.wait(timeout=3)
        return bundle


def make_owner(tmp_path, probe, clock):
    return OwnerCoordinator(
        tmp_path / "owner.sqlite3",
        GPU,
        probe,
        authenticator=lambda credential: TOKENS.get(credential),
        clock=clock,
    )


def test_startup_unknown_then_fresh_empty_observation_proves_free(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)

    assert owner.observe("h3-key").state is OwnerState.FREE
    assert owner.observe("h3-key").state is OwnerState.FREE

    assert owner.acquire("h3-key", "h3-first").decision is Decision.ACQUIRED

    # A new coordinator process never trusts the persisted OWNED value at startup.
    restarted = make_owner(tmp_path, probe, clock)
    recovered = restarted.observe("h3-key")
    assert recovered.state is OwnerState.FREE
    assert recovered.owner_instance is None
    initial = restarted.acquire("h3-key", "h3-instance-1")
    assert initial.decision is Decision.ACQUIRED
    assert initial.state is OwnerState.OWNED
    assert initial.owner_instance == "h3-instance-1"


def test_concurrent_acquire_has_one_winner(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    probe.barrier = threading.Barrier(2)
    owner_a = make_owner(tmp_path, probe, clock)
    owner_b = make_owner(tmp_path, probe, clock)
    probe.barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(owner_a.acquire, "h3-key", "h3-race"),
            pool.submit(owner_b.acquire, "live-key", "live-race"),
        ]
        results = [future.result(timeout=5) for future in futures]

    assert sum(result.decision is Decision.ACQUIRED for result in results) == 1
    assert all(result.decision in {Decision.ACQUIRED, Decision.UNKNOWN, Decision.WAITING}
               for result in results)
    winner = next(result for result in results if result.decision is Decision.ACQUIRED)
    assert winner.state is OwnerState.OWNED


def test_unknown_needs_matching_owner_and_rejects_other_project_conflict(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    acquired = owner.acquire("h3-key", "h3-current")
    assert acquired.decision is Decision.ACQUIRED

    probe.set_project("h3", ObservationState.BUSY, "h3-current")
    probe.set_project("manga", ObservationState.BUSY, "manga-unregistered")
    probe.gpu_safe_idle = False
    conflict = owner.observe("live-key")
    assert conflict.state is OwnerState.UNKNOWN
    assert conflict.owner_project == "h3"

    probe.set_project("manga", ObservationState.IDLE)
    recovered = owner.observe("h3-key")
    assert recovered.state is OwnerState.OWNED
    assert recovered.owner_project == "h3"
    assert recovered.owner_instance == "h3-current"


def test_release_needs_complete_fresh_proof_and_never_kills_or_frees_on_timeout(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    acquired = owner.acquire("h3-key", "h3-release")
    assert acquired.decision is Decision.ACQUIRED

    # An IDLE task report is insufficient while model and child-process facts
    # are missing, and a high GPU snapshot prevents handoff.
    probe.set_project(
        "h3", ObservationState.IDLE, "h3-release",
        model_released=None, child_processes_exited=None, entry_fenced=False,
    )
    probe.gpu_safe_idle = False
    uncertain = owner.release("h3-key", "h3-release")
    assert uncertain.decision is Decision.UNKNOWN
    assert uncertain.state is OwnerState.UNKNOWN
    assert uncertain.owner_instance == "h3-release"

    # A direct fresh observation of all three idle projects plus safe GPU
    # telemetry recovers UNKNOWN to FREE and retires the ambiguous claim.
    probe.set_project("h3", ObservationState.IDLE, "h3-release")
    probe.gpu_safe_idle = True
    released = owner.release("h3-key", "h3-release")
    assert released.decision is Decision.RELEASED
    assert released.state is OwnerState.FREE

    # Evidence age expiring freezes the state; it cannot turn an Owner into FREE.
    current_bundle = probe()
    probe.returned = current_bundle
    clock.advance(11)
    aged = owner.observe("live-key")
    assert aged.decision is Decision.UNKNOWN
    assert aged.state is OwnerState.UNKNOWN


def test_old_observation_cannot_overwrite_newer_observation(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    assert owner.observe("h3-key").state is OwnerState.FREE

    entered = threading.Event()
    continue_old = threading.Event()
    old_bundle = probe()

    def delayed_old_probe():
        entered.set()
        assert continue_old.wait(timeout=3)
        return old_bundle

    # Hold an older in-flight result while a later request records newer facts.
    owner._observation_provider = delayed_old_probe
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_future = pool.submit(owner.observe, "h3-key")
        assert entered.wait(timeout=3)
        owner._observation_provider = probe
        newer = owner.observe("live-key")
        assert newer.state is OwnerState.FREE
        continue_old.set()
        old = old_future.result(timeout=3)

    assert old.decision is Decision.UNKNOWN
    assert owner.observe("manga-key").state is OwnerState.FREE


def test_sample_expiring_while_sqlite_writer_lock_waits_cannot_admit(tmp_path, monkeypatch):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    generation = owner._begin_observation()
    sampled = probe()
    original_connect = owner._connect

    class DelayedConnection:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *args):
            result = self.inner.execute(sql, *args)
            if sql == "BEGIN IMMEDIATE":
                clock.advance(11)
            return result

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(owner, "_connect", lambda: DelayedConnection(original_connect()))
    result = owner._apply_observation(
        generation, sampled, Decision.ACQUIRED, "h3", "h3-lock-delayed-0001"
    )
    assert result.state is OwnerState.UNKNOWN
    assert result.decision is Decision.UNKNOWN
    assert result.reason == "stale_observation"


def test_older_source_timestamps_make_state_unknown(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    old_bundle = probe()
    assert owner.observe("h3-key").state is OwnerState.FREE

    # A current request that replays older source samples cannot authorize work.
    probe.returned = old_bundle
    stale = owner.acquire("live-key", "live-stale-evidence")
    assert stale.decision is Decision.UNKNOWN
    assert stale.state is OwnerState.UNKNOWN


def test_replayed_idle_observation_cannot_release_active_owner(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    idle_bundle = probe()
    probe.returned = idle_bundle
    owner = make_owner(tmp_path, probe, clock)
    assert owner.acquire("h3-key", "h3-replay-window").decision is Decision.ACQUIRED

    # The underlying adapter has become busy. Replaying the exact all-idle
    # bundle from acquire is still within ten seconds, but is not a new fact.
    probe.set_project("h3", ObservationState.BUSY, "h3-replay-window")
    attempt = owner.release("h3-key", "h3-replay-window")
    assert attempt.decision is Decision.UNKNOWN
    assert attempt.state is OwnerState.UNKNOWN
    assert attempt.owner_instance == "h3-replay-window"


def test_unknown_recovers_to_matching_busy_owner_only(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    assert owner.acquire("h3-key", "h3-recovery").decision is Decision.ACQUIRED

    # Simulate coordinator restart: constructor sets the persisted Owner to UNKNOWN.
    restarted = make_owner(tmp_path, probe, clock)
    probe.set_project("h3", ObservationState.BUSY, "h3-recovery")
    probe.set_project("manga", ObservationState.BUSY, "manga-conflict")
    probe.gpu_safe_idle = False
    conflict = restarted.observe("live-key")
    assert conflict.state is OwnerState.UNKNOWN

    probe.set_project("manga", ObservationState.IDLE)
    recovered = restarted.observe("h3-key")
    assert recovered.state is OwnerState.OWNED
    assert recovered.owner_project == "h3"
    assert recovered.owner_instance == "h3-recovery"


def test_matching_busy_owner_recovers_during_gpu_stage_gap(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    assert owner.acquire("h3-key", "h3-video-window").decision is Decision.ACQUIRED

    restarted = make_owner(tmp_path, probe, clock)
    probe.set_project("h3", ObservationState.BUSY, "h3-video-window")
    probe.gpu_safe_idle = True  # low instantaneous use between video stages

    recovered = restarted.observe("h3-key")
    assert recovered.state is OwnerState.OWNED
    assert recovered.owner_project == "h3"
    assert recovered.owner_instance == "h3-video-window"


def test_unfenced_idle_project_cannot_free_unknown(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    assert owner.acquire("h3-key", "h3-window").decision is Decision.ACQUIRED

    # Startup makes the persisted claim UNKNOWN. The model and child process
    # are gone, but the project has not closed the GPU entry for the next stage.
    restarted = make_owner(tmp_path, probe, clock)
    probe.set_project("h3", ObservationState.IDLE, "h3-window", entry_fenced=False)
    result = restarted.observe("h3-key")
    assert result.state is OwnerState.UNKNOWN
    assert result.owner_instance == "h3-window"


def test_late_release_and_reacquire_id_cannot_change_new_owner(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)

    first = owner.acquire("h3-key", "h3-old")
    assert first.decision is Decision.ACQUIRED
    assert owner.release("h3-key", "h3-old").decision is Decision.RELEASED

    second = owner.acquire("live-key", "live-current")
    assert second.decision is Decision.ACQUIRED

    # A delayed release is explicitly rejected after a new Owner exists; it
    # cannot clear the later owner's row. A released instance cannot reacquire.
    late_release = owner.release("h3-key", "h3-old")
    assert late_release.decision is Decision.REJECTED
    assert late_release.state is OwnerState.OWNED
    assert late_release.owner_project == "live"
    late_acquire = owner.acquire("h3-key", "h3-old")
    assert late_acquire.decision is Decision.REJECTED
    assert late_acquire.state is OwnerState.OWNED
    assert late_acquire.owner_project == "live"


def test_credentials_are_required_and_project_identity_is_not_request_data(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)

    with pytest.raises(AuthorizationError):
        owner.acquire("not-a-project-key", "forged-h3")
    with pytest.raises(AuthorizationError):
        owner.observe("")

    assert owner.acquire("manga-key", "manga-instance").owner_project == "manga"


def test_snapshot_is_authenticated_and_does_not_probe_or_change_state(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)

    initial = owner.snapshot("admin-key")
    assert initial.state is OwnerState.UNKNOWN
    assert initial.decision is Decision.UNKNOWN
    assert probe.calls == 0

    assert owner.observe("h3-key").state is OwnerState.FREE
    after_observe = probe.calls
    snapshot = owner.snapshot("admin-key")
    assert snapshot.state is OwnerState.FREE
    assert snapshot.decision is Decision.OBSERVED
    assert probe.calls == after_observe

    clock.advance(11)
    stale = owner.snapshot("admin-key")
    assert stale.state is OwnerState.UNKNOWN
    assert stale.decision is Decision.UNKNOWN
    assert stale.reason == "stale_observation"
    assert probe.calls == after_observe
    assert owner.observe("h3-key").state is OwnerState.FREE

    acquired = owner.acquire("h3-key", "h3-snapshot-window")
    assert acquired.decision is Decision.ACQUIRED
    clock.advance(11)
    stale_owner = owner.snapshot("h3-key")
    assert stale_owner.state is OwnerState.UNKNOWN
    assert stale_owner.owner_project == "h3"
    assert stale_owner.owner_instance == "h3-snapshot-window"
    assert owner.snapshot("admin-key").owner_instance is None

    with pytest.raises(AuthorizationError):
        owner.acquire("admin-key", "forbidden")


def test_three_project_handoff_waits_for_direct_release_proof(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    assert owner.observe("h3-key").state is OwnerState.FREE

    sequence = ("h3", "live", "manga", "h3")
    credentials = {project: f"{project}-key" for project in sequence}
    instances = [f"{project}-handoff-{index}" for index, project in enumerate(sequence)]
    first = owner.acquire(credentials[sequence[0]], instances[0])
    assert first.decision is Decision.ACQUIRED

    for index, (current_project, next_project) in enumerate(zip(sequence, sequence[1:])):
        current_instance = instances[index]
        next_instance = instances[index + 1]
        probe.set_project(
            current_project, ObservationState.BUSY, current_instance,
            model_released=False, entry_fenced=False,
        )
        probe.gpu_safe_idle = False
        waiting = owner.acquire(credentials[next_project], next_instance)
        assert waiting.decision is Decision.WAITING
        assert waiting.owner_project == current_project

        # Only after the current project's own entry, model, children, and
        # whole-card facts are safe can the next project take the 4080.
        probe.set_project(current_project, ObservationState.IDLE, current_instance)
        probe.gpu_safe_idle = True
        released = owner.release(credentials[current_project], current_instance)
        assert released.decision is Decision.RELEASED
        acquired = owner.acquire(credentials[next_project], next_instance)
        assert acquired.decision is Decision.ACQUIRED
        assert acquired.owner_project == next_project


def test_idle_poll_does_not_release_a_claim_without_handoff_request(tmp_path):
    clock = FakeClock()
    probe = FakeProbe(clock)
    owner = make_owner(tmp_path, probe, clock)
    assert owner.acquire("h3-key", "h3-clean-window").decision is Decision.ACQUIRED

    # H3 has closed entry and unloaded its model, but its handoff request has
    # not arrived. Background observation must retain the legal Owner.
    probe.set_project("h3", ObservationState.IDLE, "h3-clean-window")
    for _ in range(3):
        observed = owner.observe("live-key")
        assert observed.state is OwnerState.OWNED
        assert observed.owner_project == "h3"
    waiting = owner.acquire("live-key", "live-next-window")
    assert waiting.decision is Decision.WAITING

    released = owner.release("h3-key", "h3-clean-window")
    assert released.decision is Decision.RELEASED
    assert owner.acquire("live-key", "live-next-window").decision is Decision.ACQUIRED
