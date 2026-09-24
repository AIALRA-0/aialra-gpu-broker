"""Small persistent coordinator for ownership of one GPU.

This module deliberately has no HTTP server, process control, GPU client, or
dependency on the legacy broker tables. Callers inject both project
authentication and a trusted provider that gathers current observations from
the three project adapters and the GPU sampler.
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping


PROJECTS = frozenset({"h3", "live", "manga"})


class OwnerState(str, Enum):
    FREE = "FREE"
    OWNED = "OWNED"
    UNKNOWN = "UNKNOWN"


class Decision(str, Enum):
    OBSERVED = "OBSERVED"
    ACQUIRED = "ACQUIRED"
    WAITING = "WAITING"
    RELEASED = "RELEASED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


class ObservationState(str, Enum):
    BUSY = "BUSY"
    IDLE = "IDLE"
    UNKNOWN = "UNKNOWN"


class AuthorizationError(PermissionError):
    """The injected authenticator did not establish a valid project identity."""


@dataclass(frozen=True)
class ProjectObservation:
    """Fresh, generic GPU-use facts reported by one project adapter.

    ``owner_instance`` identifies the adapter's still-held Owner claim. IDLE
    is the adapter's direct assertion that its GPU work and model residency
    have ended. The separate entry fence prevents a next stage from starting
    during handoff. Optional detail fields help diagnose a disagreement but
    are not independent coverage requirements for every project.
    """

    project: str
    status: ObservationState
    observed_at: float
    owner_instance: str | None = None
    model_released: bool | None = None
    child_processes_exited: bool | None = None
    entry_fenced: bool | None = None

    def __post_init__(self) -> None:
        if self.project not in PROJECTS:
            raise ValueError("project must be h3, live, or manga")
        if not isinstance(self.status, ObservationState):
            raise ValueError("status must be an ObservationState")
        if not _valid_timestamp(self.observed_at):
            raise ValueError("observed_at must be a finite timestamp")
        if self.owner_instance is not None and not _valid_instance(self.owner_instance):
            raise ValueError("owner_instance is invalid")
        for detail in (self.model_released, self.child_processes_exited):
            if detail is not None and not isinstance(detail, bool):
                raise ValueError("release detail must be a boolean or None")
        if self.entry_fenced is not None and not isinstance(self.entry_fenced, bool):
            raise ValueError("entry_fenced must be a boolean or None")


@dataclass(frozen=True)
class GpuObservation:
    """A GPU sampler result with a pre-calibrated safe-idle classification."""

    observed_at: float
    healthy: bool
    safe_idle: bool

    def __post_init__(self) -> None:
        if not _valid_timestamp(self.observed_at):
            raise ValueError("observed_at must be a finite timestamp")
        if not isinstance(self.healthy, bool) or not isinstance(self.safe_idle, bool):
            raise ValueError("healthy and safe_idle must be booleans")


@dataclass(frozen=True)
class ObservationBundle:
    """One provider response containing all three project and GPU observations."""

    projects: Mapping[str, ProjectObservation]
    gpu: GpuObservation


@dataclass(frozen=True)
class OwnerResult:
    decision: Decision
    state: OwnerState
    owner_project: str | None
    owner_instance: str | None
    acquired_at: float | None
    last_observed_at: float | None
    reason: str


Authenticator = Callable[[str], str | None]
ObservationProvider = Callable[[], ObservationBundle]
Clock = Callable[[], float]


class OwnerCoordinator:
    """Persistent FREE / OWNED / UNKNOWN state for one GPU.

    Args:
        database_path: Dedicated SQLite file for Owner v1.
        gpu_uuid: Stable UUID used as the unique Owner row key.
        observation_provider: Trusted callback returning fresh observations for
            exactly h3, live, manga, and the managed GPU. The provider must
            obtain direct, current samples (the later API adapter will use a
            per-request challenge); timestamps use the host wall clock. A
            repeated or older sample is rejected because it cannot establish
            a fresh release or admission fact.
        authenticator: Callback mapping a project credential to one canonical
            project name, or ``None`` when invalid. No project identity is
            accepted from an operation argument.
        clock: Wall-clock timestamp source, injectable for deterministic tests.
        max_observation_age_seconds: Maximum evidence age; expiration makes the
            state UNKNOWN and never releases an Owner.

    Operations:
        ``snapshot(credential)`` reads state without probing or changing it,
            projecting expired evidence as UNKNOWN;
            the authenticator may grant the special ``admin`` identity only
            this read-only operation.
        ``observe(credential)`` refreshes shared evidence and reconciles state.
        ``acquire(credential, owner_instance)`` atomically claims a proven FREE
        row, or returns WAITING / UNKNOWN.
        ``release(credential, owner_instance)`` requires the matching active
        claim and fresh all-project plus GPU release proof.

    The coordinator exposes no task cancellation or process termination
    operation. Database writes use ``BEGIN IMMEDIATE`` so independent threads
    and processes share the same single-winner acquire point.
    """

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        gpu_uuid: str,
        observation_provider: ObservationProvider,
        authenticator: Authenticator,
        clock: Clock = time.time,
        max_observation_age_seconds: float = 10.0,
    ) -> None:
        path_text = os.fspath(database_path)
        if not path_text or path_text == ":memory:":
            raise ValueError("database_path must name a persistent SQLite file")
        if not isinstance(gpu_uuid, str) or not gpu_uuid.strip():
            raise ValueError("gpu_uuid must be a non-empty string")
        if not callable(observation_provider) or not callable(authenticator):
            raise TypeError("observation_provider and authenticator must be callable")
        if not math.isfinite(max_observation_age_seconds) or max_observation_age_seconds <= 0:
            raise ValueError("max_observation_age_seconds must be finite and positive")

        self.database_path = Path(path_text)
        self.gpu_uuid = gpu_uuid
        self._observation_provider = observation_provider
        self._authenticator = authenticator
        self._clock = clock
        self._max_observation_age_seconds = float(max_observation_age_seconds)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def observe(self, credential: str) -> OwnerResult:
        """Refresh observations and return the resulting owner summary."""

        project = self._authenticate(credential)
        generation = self._begin_observation()
        bundle = self._collect()
        return self._apply_observation(generation, bundle, Decision.OBSERVED, project)

    def snapshot(self, credential: str) -> OwnerResult:
        """Return stored state, projecting expired evidence as UNKNOWN.

        A future HTTP layer must authorize this call before invoking it. The
        injected authenticator can map an administrator credential to
        ``"admin"``; that identity is rejected by acquire, release, and observe.
        """

        principal = self._authenticate(credential, allow_admin=True)
        with closing(self._connect()) as connection:
            row = self._row(connection)
            observed_at = row["last_observed_at"]
            now = self._clock()
            fresh = (
                _valid_timestamp(now)
                and _valid_timestamp(observed_at)
                and 0 <= now - observed_at <= self._max_observation_age_seconds
            )
            if row["state"] != OwnerState.UNKNOWN.value and not fresh:
                # A read-only snapshot must never claim a stale FREE/OWNED
                # state is current. Keep the last owner identity for recovery;
                # only a fresh direct observe may change the durable row.
                return replace(
                    self._result(row, principal, Decision.UNKNOWN, "stale_observation"),
                    state=OwnerState.UNKNOWN,
                )
            decision = (
                Decision.OBSERVED
                if row["state"] != OwnerState.UNKNOWN.value
                else Decision.UNKNOWN
            )
            return self._result(row, principal, decision, "stored_snapshot")

    def acquire(self, credential: str, owner_instance: str) -> OwnerResult:
        """Acquire ownership using the identity established by ``credential``."""

        project = self._authenticate(credential)
        self._validate_instance(owner_instance)
        initial = self._check_acquire_instance(project, owner_instance)
        if initial is not None:
            return initial

        generation = self._begin_observation()
        bundle = self._collect()
        return self._apply_observation(
            generation, bundle, Decision.ACQUIRED, project, owner_instance
        )

    def release(self, credential: str, owner_instance: str) -> OwnerResult:
        """Release the matching claim only after current all-project proof."""

        project = self._authenticate(credential)
        self._validate_instance(owner_instance)
        generation, initial = self._begin_release(project, owner_instance)
        if initial is not None:
            return initial

        bundle = self._collect()
        return self._apply_observation(
            generation, bundle, Decision.RELEASED, project, owner_instance
        )

    def _initialize_database(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS owner_state (
                    gpu_uuid TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN ('FREE', 'OWNED', 'UNKNOWN')),
                    owner_project TEXT,
                    owner_instance TEXT,
                    acquired_at REAL,
                    last_observed_at REAL,
                    observation_generation INTEGER NOT NULL DEFAULT 0,
                    CHECK(owner_project IS NULL OR owner_project IN ('h3', 'live', 'manga')),
                    CHECK(
                        state = 'UNKNOWN'
                        OR (state = 'FREE' AND owner_project IS NULL
                            AND owner_instance IS NULL AND acquired_at IS NULL)
                        OR (state = 'OWNED' AND owner_project IS NOT NULL
                            AND owner_instance IS NOT NULL AND acquired_at IS NOT NULL)
                    )
                );

                CREATE TABLE IF NOT EXISTS owner_claims (
                    gpu_uuid TEXT NOT NULL,
                    owner_instance TEXT NOT NULL,
                    project TEXT NOT NULL CHECK(project IN ('h3', 'live', 'manga')),
                    acquired_at REAL NOT NULL,
                    released_at REAL,
                    PRIMARY KEY (gpu_uuid, owner_instance),
                    FOREIGN KEY (gpu_uuid) REFERENCES owner_state(gpu_uuid)
                );

                CREATE TABLE IF NOT EXISTS owner_evidence_versions (
                    gpu_uuid TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('h3', 'live', 'manga', 'gpu')),
                    observed_at REAL NOT NULL,
                    PRIMARY KEY (gpu_uuid, source),
                    FOREIGN KEY (gpu_uuid) REFERENCES owner_state(gpu_uuid)
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO owner_state
                    (gpu_uuid, state, owner_project, owner_instance, acquired_at,
                     last_observed_at, observation_generation)
                VALUES (?, 'UNKNOWN', NULL, NULL, NULL, NULL, 0)
                """,
                (self.gpu_uuid,),
            )
            # A process restart always requires direct reconciliation. Preserve
            # the last owner identity so it can be recovered without hot takeover.
            connection.execute(
                "UPDATE owner_state SET state = 'UNKNOWN', observation_generation = "
                "observation_generation + 1 WHERE gpu_uuid = ?",
                (self.gpu_uuid,),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _authenticate(self, credential: str, *, allow_admin: bool = False) -> str:
        if not isinstance(credential, str) or not credential:
            raise AuthorizationError("valid project credentials are required")
        try:
            project = self._authenticator(credential)
        except Exception as exc:
            raise AuthorizationError("valid project credentials are required") from exc
        if project not in PROJECTS and not (allow_admin and project == "admin"):
            raise AuthorizationError("valid project credentials are required")
        return project

    def _validate_instance(self, owner_instance: str) -> None:
        if not _valid_instance(owner_instance):
            raise ValueError("owner_instance must be a non-empty opaque string")

    def _check_acquire_instance(
        self, project: str, owner_instance: str
    ) -> OwnerResult | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            claim = self._claim(connection, owner_instance)
            if claim is not None:
                if claim["project"] != project:
                    connection.commit()
                    return self._result(row, project, Decision.REJECTED, "instance_project_mismatch")
                if claim["released_at"] is not None:
                    connection.commit()
                    return self._result(row, project, Decision.REJECTED, "instance_already_released")
                if (row["owner_project"], row["owner_instance"]) != (project, owner_instance):
                    connection.commit()
                    return self._result(row, project, Decision.REJECTED, "instance_already_used")

        return None

    def _begin_release(
        self, project: str, owner_instance: str
    ) -> tuple[int | None, OwnerResult | None]:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            claim = self._claim(connection, owner_instance)
            if claim is None or claim["project"] != project:
                connection.commit()
                return None, self._result(row, project, Decision.REJECTED, "not_owner_instance")
            if claim["released_at"] is not None:
                connection.commit()
                if row["owner_instance"] is not None:
                    return None, self._result(
                        row, project, Decision.REJECTED, "stale_owner_instance"
                    )
                return None, self._result(row, project, Decision.RELEASED, "already_released")
            if (row["owner_project"], row["owner_instance"]) != (project, owner_instance):
                connection.commit()
                return None, self._result(row, project, Decision.REJECTED, "stale_owner_instance")

            generation = int(row["observation_generation"]) + 1
            connection.execute(
                "UPDATE owner_state SET observation_generation = ? WHERE gpu_uuid = ?",
                (generation, self.gpu_uuid),
            )
            connection.commit()
            return generation, None

    def _begin_observation(self) -> int:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection)
            generation = int(row["observation_generation"]) + 1
            connection.execute(
                "UPDATE owner_state SET observation_generation = ? WHERE gpu_uuid = ?",
                (generation, self.gpu_uuid),
            )
            connection.commit()
            return generation

    def _collect(self) -> ObservationBundle | None:
        try:
            bundle = self._observation_provider()
            return bundle if isinstance(bundle, ObservationBundle) else None
        except Exception:
            return None

    def _apply_observation(
        self,
        generation: int,
        bundle: ObservationBundle | None,
        operation: Decision,
        project: str,
        owner_instance: str | None = None,
    ) -> OwnerResult:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Validate freshness at the serialization point. A concurrent
            # writer may have held SQLite's lock long enough for a sample to
            # expire while this call waited for BEGIN IMMEDIATE.
            now = self._clock()
            row = self._row(connection)
            if int(row["observation_generation"]) != generation:
                return self._result(
                    row, project, Decision.UNKNOWN, "superseded_observation"
                )

            if bundle is None:
                self._set_unknown(connection, row)
                connection.commit()
                return self._result(
                    self._row(connection), project, Decision.UNKNOWN, "observation_unavailable"
                )

            evidence_problem, sampled_at = self._validate_evidence(connection, bundle, now)
            if evidence_problem is not None:
                self._set_unknown(connection, row)
                connection.commit()
                return self._result(
                    self._row(connection), project, Decision.UNKNOWN, evidence_problem
                )

            self._save_evidence_versions(connection, bundle, sampled_at)
            current = self._row(connection)
            active_claim = self._active_claim_for_row(connection, current)
            all_idle = self._all_idle_proof(bundle)
            owner_confirmed = self._owner_confirmed(current, active_claim, bundle)
            next_state = current["state"]
            if current["state"] == OwnerState.FREE.value:
                next_state = OwnerState.FREE.value if all_idle else OwnerState.UNKNOWN.value
            elif current["state"] == OwnerState.OWNED.value:
                # An active claim is not implicitly released just because a
                # polling pass finds the entry fenced and the GPU idle. The
                # project still has to request a verified handoff. A restart
                # starts in UNKNOWN, where fresh all-idle facts can recover
                # FREE without trusting a lost client response.
                clean_but_unreleased = (
                    all_idle and active_claim is not None
                    and active_claim["project"] == current["owner_project"]
                )
                next_state = (
                    OwnerState.OWNED.value
                    if owner_confirmed or clean_but_unreleased
                    else OwnerState.UNKNOWN.value
                )
            else:  # UNKNOWN
                if all_idle:
                    next_state = OwnerState.FREE.value
                    if active_claim is not None:
                        connection.execute(
                            "UPDATE owner_claims SET released_at = ? "
                            "WHERE gpu_uuid = ? AND owner_instance = ? AND released_at IS NULL",
                            (now, self.gpu_uuid, current["owner_instance"]),
                        )
                elif owner_confirmed:
                    next_state = OwnerState.OWNED.value
                else:
                    next_state = OwnerState.UNKNOWN.value

            if next_state == OwnerState.FREE.value:
                connection.execute(
                    """UPDATE owner_state SET state = 'FREE', owner_project = NULL,
                       owner_instance = NULL, acquired_at = NULL, last_observed_at = ?
                       WHERE gpu_uuid = ?""",
                    (sampled_at, self.gpu_uuid),
                )
            elif next_state == OwnerState.UNKNOWN.value:
                connection.execute(
                    "UPDATE owner_state SET state = 'UNKNOWN', last_observed_at = ? "
                    "WHERE gpu_uuid = ?",
                    (sampled_at, self.gpu_uuid),
                )
            else:
                connection.execute(
                    "UPDATE owner_state SET state = 'OWNED', last_observed_at = ? "
                    "WHERE gpu_uuid = ?",
                    (sampled_at, self.gpu_uuid),
                )

            if operation == Decision.RELEASED:
                if all_idle and (current["state"] in {OwnerState.OWNED.value, OwnerState.UNKNOWN.value}) \
                        and (current["owner_project"], current["owner_instance"]) == (project, owner_instance):
                    connection.execute(
                        """UPDATE owner_claims SET released_at = ?
                           WHERE gpu_uuid = ? AND owner_instance = ? AND released_at IS NULL""",
                        (now, self.gpu_uuid, owner_instance),
                    )
                    connection.execute(
                        """UPDATE owner_state SET state = 'FREE', owner_project = NULL,
                           owner_instance = NULL, acquired_at = NULL
                           WHERE gpu_uuid = ?""",
                        (self.gpu_uuid,),
                    )
                    result = self._result(
                        self._row(connection), project, Decision.RELEASED, "release_verified"
                    )
                else:
                    # release is a handoff claim, not a synonym for task
                    # completion. Any missing or contradictory proof freezes new work.
                    self._set_unknown(connection, self._row(connection))
                    result = self._result(
                        self._row(connection), project, Decision.UNKNOWN, "release_unverified"
                    )
            elif operation == Decision.ACQUIRED:
                updated = self._row(connection)
                if updated["state"] == OwnerState.FREE.value:
                    prior = self._claim(connection, owner_instance or "")
                    if prior is not None:
                        self._set_unknown(connection, updated)
                        result = self._result(
                            self._row(connection), project, Decision.REJECTED, "instance_already_used"
                        )
                    else:
                        acquired_at = now
                        connection.execute(
                            """INSERT INTO owner_claims
                               (gpu_uuid, owner_instance, project, acquired_at, released_at)
                               VALUES (?, ?, ?, ?, NULL)""",
                            (self.gpu_uuid, owner_instance, project, acquired_at),
                        )
                        connection.execute(
                            """UPDATE owner_state SET state = 'OWNED', owner_project = ?,
                               owner_instance = ?, acquired_at = ? WHERE gpu_uuid = ?""",
                            (project, owner_instance, acquired_at, self.gpu_uuid),
                        )
                        result = self._result(
                            self._row(connection), project, Decision.ACQUIRED, "acquired"
                        )
                elif updated["state"] == OwnerState.OWNED.value:
                    same_owner = (updated["owner_project"], updated["owner_instance"]) == (
                        project, owner_instance
                    )
                    result = self._result(
                        updated,
                        project,
                        Decision.ACQUIRED if same_owner else Decision.WAITING,
                        "same_instance" if same_owner else "owned_by_another_instance",
                    )
                else:
                    result = self._result(
                        updated, project, Decision.UNKNOWN, "owner_state_unknown"
                    )
            else:
                updated = self._row(connection)
                result = self._result(
                    updated,
                    project,
                    Decision.OBSERVED if updated["state"] != OwnerState.UNKNOWN.value else Decision.UNKNOWN,
                    "fresh_observation" if updated["state"] != OwnerState.UNKNOWN.value else "observation_conflict",
                )

            connection.commit()
            return result

    def _validate_evidence(
        self, connection: sqlite3.Connection, bundle: ObservationBundle, now: float
    ) -> tuple[str | None, float]:
        if not _valid_timestamp(now):
            return "invalid_clock", 0.0
        if not isinstance(bundle.projects, Mapping) or set(bundle.projects) != PROJECTS:
            return "incomplete_project_observations", 0.0
        if not isinstance(bundle.gpu, GpuObservation):
            return "missing_gpu_observation", 0.0

        timestamps: dict[str, float] = {}
        for project in sorted(PROJECTS):
            observation = bundle.projects[project]
            if not isinstance(observation, ProjectObservation) or observation.project != project:
                return "invalid_project_observation", 0.0
            timestamps[project] = observation.observed_at
        timestamps["gpu"] = bundle.gpu.observed_at

        for source, observed_at in timestamps.items():
            age = now - observed_at
            if age < 0 or age > self._max_observation_age_seconds:
                return "stale_observation", 0.0
            previous = connection.execute(
                "SELECT observed_at FROM owner_evidence_versions WHERE gpu_uuid = ? AND source = ?",
                (self.gpu_uuid, source),
            ).fetchone()
            if previous is not None and observed_at <= float(previous["observed_at"]):
                return "out_of_order_observation", 0.0

        return None, min(timestamps.values())

    def _save_evidence_versions(
        self, connection: sqlite3.Connection, bundle: ObservationBundle, sampled_at: float
    ) -> None:
        del sampled_at  # Per-source timestamps are the ordering authority.
        for project, observation in bundle.projects.items():
            connection.execute(
                """INSERT INTO owner_evidence_versions (gpu_uuid, source, observed_at)
                   VALUES (?, ?, ?) ON CONFLICT(gpu_uuid, source)
                   DO UPDATE SET observed_at = excluded.observed_at""",
                (self.gpu_uuid, project, observation.observed_at),
            )
        connection.execute(
            """INSERT INTO owner_evidence_versions (gpu_uuid, source, observed_at)
               VALUES (?, 'gpu', ?) ON CONFLICT(gpu_uuid, source)
               DO UPDATE SET observed_at = excluded.observed_at""",
            (self.gpu_uuid, bundle.gpu.observed_at),
        )

    @staticmethod
    def _idle_observation(observation: ProjectObservation) -> bool:
        return (
            observation.status == ObservationState.IDLE
            and observation.entry_fenced is True
            and observation.model_released is not False
            and observation.child_processes_exited is not False
        )

    def _all_idle_proof(self, bundle: ObservationBundle) -> bool:
        return (
            all(self._idle_observation(bundle.projects[p]) for p in PROJECTS)
            and bundle.gpu.healthy
            and bundle.gpu.safe_idle
        )

    def _owner_confirmed(
        self,
        row: sqlite3.Row,
        active_claim: sqlite3.Row | None,
        bundle: ObservationBundle,
    ) -> bool:
        project = row["owner_project"]
        instance = row["owner_instance"]
        if project not in PROJECTS or not instance or active_claim is None:
            return False
        if active_claim["project"] != project or active_claim["released_at"] is not None:
            return False
        owner = bundle.projects[project]
        if owner.owner_instance != instance:
            return False
        if not all(
            self._idle_observation(bundle.projects[other])
            for other in PROJECTS
            if other != project
        ):
            return False
        if owner.status == ObservationState.BUSY:
            # ``safe_idle`` is a release classifier only. A project can retain
            # an open Owner window between GPU stages while instantaneous card
            # use is low; the matching project claim remains decisive.
            return bundle.gpu.healthy
        return False

    def _set_unknown(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        connection.execute(
            "UPDATE owner_state SET state = 'UNKNOWN' WHERE gpu_uuid = ?",
            (row["gpu_uuid"],),
        )

    def _active_claim_for_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> sqlite3.Row | None:
        if not row["owner_instance"]:
            return None
        claim = self._claim(connection, row["owner_instance"])
        if claim is None or claim["project"] != row["owner_project"] or claim["released_at"] is not None:
            return None
        return claim

    def _claim(self, connection: sqlite3.Connection, owner_instance: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM owner_claims WHERE gpu_uuid = ? AND owner_instance = ?",
            (self.gpu_uuid, owner_instance),
        ).fetchone()

    def _row(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM owner_state WHERE gpu_uuid = ?", (self.gpu_uuid,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Owner state row is missing")
        return row

    def _result(
        self,
        row: sqlite3.Row,
        authenticated_project: str,
        decision: Decision,
        reason: str,
    ) -> OwnerResult:
        instance = row["owner_instance"]
        if row["owner_project"] != authenticated_project:
            instance = None
        return OwnerResult(
            decision=decision,
            state=OwnerState(row["state"]),
            owner_project=row["owner_project"],
            owner_instance=instance,
            acquired_at=row["acquired_at"],
            last_observed_at=row["last_observed_at"],
            reason=reason,
        )


def _valid_timestamp(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))


def _valid_instance(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 256 and "\x00" not in value
