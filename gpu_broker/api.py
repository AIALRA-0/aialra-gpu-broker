from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .core import Broker, BrokerError
from .monitor import Monitor, NvidiaMonitor

logger = logging.getLogger(__name__)


class ProjectHeartbeat(BaseModel):
    instance_id: str = Field(min_length=4, max_length=128)
    status: str = Field(default="online", max_length=100)
    resident_mib: int = Field(default=0, ge=0)


class ProfileCreate(BaseModel):
    project_id: Literal["minimax", "live_translate", "manga"]
    label: str = Field(min_length=2, max_length=120)
    kind: Literal["realtime", "batch"]
    peak_growth_mib: int = Field(gt=0, le=65536)
    max_seconds: int = Field(gt=0, le=86400)


class ProfileUpdate(BaseModel):
    enabled: bool


class JobCreate(BaseModel):
    external_id: str = Field(min_length=1, max_length=180)
    idempotency_key: str = Field(min_length=8, max_length=180)
    label: str = Field(min_length=1, max_length=160)


class JobUpdate(BaseModel):
    status: str
    backend_id: str | None = Field(default=None, max_length=180)
    error_code: str | None = Field(default=None, max_length=100)


class SessionCreate(BaseModel):
    request_key: str = Field(min_length=8, max_length=180)
    owner_instance: str = Field(min_length=4, max_length=128)
    kind: Literal["realtime", "batch_task"] = "realtime"


class SessionClose(BaseModel):
    owner_instance: str = Field(min_length=4, max_length=128)
    backend_confirmed_inactive: bool


class OwnerHeartbeat(BaseModel):
    owner_instance: str = Field(min_length=4, max_length=128)
    backend_id: str | None = Field(default=None, max_length=180)


class PermitCreate(BaseModel):
    job_id: str
    profile_id: str
    request_key: str = Field(min_length=8, max_length=180)
    stage: str = Field(min_length=2, max_length=100)
    owner_instance: str = Field(min_length=4, max_length=128)
    backend_id: str | None = Field(default=None, max_length=180)
    session_id: str | None = None


class PermitFinish(BaseModel):
    owner_instance: str
    result: Literal["COMPLETED", "FAILED", "CANCELLED"]
    backend_confirmed_inactive: bool
    resident_mib: int = Field(default=0, ge=0)


class AllocationChange(BaseModel):
    enabled: bool


class Reconcile(BaseModel):
    backend_confirmed_inactive: bool
    evidence: str = Field(min_length=10, max_length=300)


def create_app(settings: Settings, monitor: Monitor | None = None) -> FastAPI:
    broker = Broker(settings, monitor or NvidiaMonitor())

    async def polling() -> None:
        counter = 0
        while True:
            await asyncio.sleep(settings.sample_interval_seconds)
            try:
                await asyncio.to_thread(broker.poll)
                counter += 1
                if counter % 900 == 0:
                    await asyncio.to_thread(broker.maintenance)
            except Exception:
                # A failed poll does not turn an old GPU reading into free capacity.
                # The freshness check stops grants until a valid sample is available.
                logger.exception("GPU broker poll failed; fresh telemetry is required for grants")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        broker.start()
        try:
            await asyncio.to_thread(broker.maintenance)
        except Exception:
            logger.exception("GPU broker maintenance failed; retrying on the next interval")
        task = asyncio.create_task(polling())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            broker.stop()

    app = FastAPI(
        title="AIALRA GPU Broker",
        version=__version__,
        description="Local GPU admission, reconciliation, and monitoring",
        lifespan=lifespan,
    )
    app.state.broker = broker
    static_dir = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if origin not in {expected, settings.public_origin}:
                return JSONResponse({"detail": "Cross-origin write is refused"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(BrokerError)
    async def broker_error(_: Request, exc: BrokerError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    def identity(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required")
        token = authorization[7:]
        if secrets.compare_digest(token, settings.admin_token):
            return "admin"
        for project_id, project_token in settings.project_tokens.items():
            if secrets.compare_digest(token, project_token):
                return project_id
        raise HTTPException(401, "Invalid token")

    def admin(role: str = Depends(identity)) -> str:
        if role != "admin":
            raise HTTPException(403, "Administrator token required")
        return role

    def project_allowed(project_id: str, role: str) -> None:
        if role != "admin" and role != project_id:
            raise HTTPException(403, "Token does not belong to this project")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/v1/health")
    def health():
        snapshot = broker._snapshot
        return {
            "version": __version__,
            "running": broker._started,
            "telemetry_ok": bool(snapshot and snapshot.get("ok") and broker._managed_card(__import__("time").time())),
            "allocation_enabled": broker._allocation_enabled(),
        }

    @app.get("/v1/ready")
    def ready():
        snapshot = broker._snapshot
        healthy = bool(snapshot and snapshot.get("ok") and broker._managed_card(__import__("time").time()))
        return JSONResponse({"ready": healthy}, status_code=200 if healthy else 503)

    @app.get("/v1/me")
    def me(role: str = Depends(identity)):
        return {"role": role}

    @app.get("/v1/dashboard")
    def dashboard(_: str = Depends(admin)):
        return broker.dashboard()

    @app.get("/v1/history")
    def history(
        gpu_uuid: str, minutes: int = Query(60, ge=1, le=4320),
        _: str = Depends(admin),
    ):
        return {"gpu_uuid": gpu_uuid, "samples": broker.history(gpu_uuid, minutes)}

    @app.get("/v1/events")
    def events(after: int = Query(0, ge=0), _: str = Depends(admin)):
        return {"events": broker.events(after)}

    @app.post("/v1/admin/allocation")
    def allocation(payload: AllocationChange, _: str = Depends(admin)):
        return broker.set_allocation(payload.enabled)

    @app.post("/v1/admin/backup")
    def backup(_: str = Depends(admin)):
        return broker.backup()

    @app.post("/v1/admin/jobs/{job_id}/cancel")
    def admin_cancel_job(job_id: str, _: str = Depends(admin)):
        return broker.cancel_job(job_id)

    @app.get("/v1/admin/doctor")
    def doctor(_: str = Depends(admin)):
        return {
            "database": broker.integrity_check(),
            "managed_gpu_uuid": settings.managed_gpu_uuid,
            "monitor": broker._snapshot,
            "allocation_enabled": broker._allocation_enabled(),
        }

    @app.post("/v1/profiles", status_code=201)
    def create_profile(payload: ProfileCreate, _: str = Depends(admin)):
        return broker.create_profile(
            payload.project_id, payload.label, payload.kind,
            payload.peak_growth_mib, payload.max_seconds,
        )

    @app.patch("/v1/profiles/{profile_id}")
    def update_profile(profile_id: str, payload: ProfileUpdate, _: str = Depends(admin)):
        return broker.update_profile(profile_id, payload.enabled)

    @app.post("/v1/projects/{project_id}/heartbeat")
    def project_heartbeat(project_id: str, payload: ProjectHeartbeat, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.project_heartbeat(project_id, payload.instance_id, payload.status, payload.resident_mib)

    @app.post("/v1/projects/{project_id}/jobs", status_code=201)
    def register_job(project_id: str, payload: JobCreate, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.register_job(project_id, payload.external_id, payload.idempotency_key, payload.label)

    @app.get("/v1/projects/{project_id}/jobs/{job_id}")
    def get_job(project_id: str, job_id: str, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.get_job(project_id, job_id)

    @app.patch("/v1/projects/{project_id}/jobs/{job_id}")
    def update_job(project_id: str, job_id: str, payload: JobUpdate, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.update_job(project_id, job_id, payload.status, payload.backend_id, payload.error_code)

    @app.post("/v1/projects/{project_id}/sessions", status_code=201)
    def session_create(project_id: str, payload: SessionCreate, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.request_session(project_id, payload.request_key, payload.owner_instance, payload.kind)

    @app.get("/v1/projects/{project_id}/sessions/{session_id}")
    def session_get(project_id: str, session_id: str, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.get_session(project_id, session_id)

    @app.post("/v1/projects/{project_id}/sessions/{session_id}/heartbeat")
    def session_heartbeat(project_id: str, session_id: str, payload: OwnerHeartbeat, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.session_heartbeat(project_id, session_id, payload.owner_instance)

    @app.post("/v1/projects/{project_id}/sessions/{session_id}/ready")
    def session_ready(project_id: str, session_id: str, payload: OwnerHeartbeat, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.session_ready(project_id, session_id, payload.owner_instance)

    @app.post("/v1/projects/{project_id}/sessions/{session_id}/close")
    def session_close(project_id: str, session_id: str, payload: SessionClose, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.close_session(project_id, session_id, payload.owner_instance, payload.backend_confirmed_inactive)

    @app.post("/v1/projects/{project_id}/permits", status_code=201)
    def permit_create(project_id: str, payload: PermitCreate, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.request_permit(
            project_id, payload.job_id, payload.profile_id, payload.request_key,
            payload.stage, payload.owner_instance, payload.backend_id, payload.session_id,
        )

    @app.get("/v1/projects/{project_id}/permits/{permit_id}")
    def permit_get(project_id: str, permit_id: str, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.get_permit(project_id, permit_id)

    @app.post("/v1/projects/{project_id}/permits/{permit_id}/heartbeat")
    def permit_heartbeat(project_id: str, permit_id: str, payload: OwnerHeartbeat, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.permit_heartbeat(project_id, permit_id, payload.owner_instance, payload.backend_id)

    @app.post("/v1/projects/{project_id}/permits/{permit_id}/finish")
    def permit_finish(project_id: str, permit_id: str, payload: PermitFinish, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.finish_permit(
            project_id, permit_id, payload.owner_instance, payload.result,
            payload.backend_confirmed_inactive, payload.resident_mib,
        )

    @app.post("/v1/projects/{project_id}/permits/{permit_id}/cancel")
    def permit_cancel(project_id: str, permit_id: str, role: str = Depends(identity)):
        project_allowed(project_id, role)
        return broker.cancel_permit(project_id, permit_id)

    @app.post("/v1/admin/permits/{permit_id}/reconcile")
    def permit_reconcile(permit_id: str, payload: Reconcile, _: str = Depends(admin)):
        return broker.reconcile_permit(permit_id, payload.evidence, payload.backend_confirmed_inactive)

    @app.post("/v1/projects/{project_id}/permits/{permit_id}/reconcile")
    def project_reconcile_permit(project_id: str, permit_id: str, payload: Reconcile,
                                 role: str = Depends(identity)):
        project_allowed(project_id, role)
        broker.get_permit(project_id, permit_id)
        return broker.reconcile_permit(permit_id, payload.evidence,
                                       payload.backend_confirmed_inactive)

    @app.post("/v1/admin/sessions/{session_id}/reconcile")
    def session_reconcile(session_id: str, payload: Reconcile, _: str = Depends(admin)):
        return broker.reconcile_session(session_id, payload.evidence, payload.backend_confirmed_inactive)

    @app.post("/v1/projects/{project_id}/sessions/{session_id}/reconcile")
    def project_reconcile_session(project_id: str, session_id: str, payload: Reconcile,
                                  role: str = Depends(identity)):
        project_allowed(project_id, role)
        broker.get_session(project_id, session_id)
        return broker.reconcile_session(session_id, payload.evidence,
                                        payload.backend_confirmed_inactive)

    return app
