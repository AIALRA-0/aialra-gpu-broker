"""HMAC-authenticated HTTP adapter for the independent 4080 Owner coordinator.

Mount this app only in the dedicated local Owner process. The public dashboard
may read a separate snapshot, but must never proxy these mutation endpoints.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import math
import re
import threading
import time
from dataclasses import asdict
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .owner import AuthorizationError, OwnerCoordinator, OwnerResult
from .owner_auth import sign_request, sign_response


_MAX_CLOCK_SKEW_SECONDS = 10.0
_MAX_REQUEST_BODY_BYTES = 4096
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_instance: str = Field(min_length=16, max_length=256)


def create_owner_app(
    coordinator: OwnerCoordinator,
    project_tokens: Mapping[str, str],
    admin_token: str,
) -> FastAPI:
    """Expose HMAC-authenticated ownership operations and a read-only view.

    Request signatures authenticate project identity, timestamp, nonce, route,
    and body without disclosing a reusable credential to a loopback listener.
    Successful responses are signed and bound to the request nonce.
    """

    if set(project_tokens) != {"h3", "live", "manga"}:
        raise ValueError("Owner API needs h3, live, and manga credentials")
    if not isinstance(admin_token, str) or not admin_token:
        raise ValueError("Owner API needs an administrator credential")
    credentials = {**project_tokens, "admin": admin_token}
    if any(not isinstance(token, str) or not token for token in credentials.values()):
        raise ValueError("Owner API credentials must be non-empty strings")
    if len(set(credentials.values())) != len(credentials):
        raise ValueError("Owner API credentials must be distinct")

    app = FastAPI(
        title="AIALRA GPU Owner",
        description="Local 4080 ownership, without task or model control",
    )

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(AuthorizationError)
    async def unauthorized(_: Request, __: AuthorizationError):
        return JSONResponse({"detail": "Owner request authentication failed"}, status_code=401)

    @app.exception_handler(ValueError)
    async def invalid_request(_: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    used_nonces: dict[str, float] = {}
    nonce_lock = threading.Lock()

    async def authenticate(request: Request) -> tuple[str, str, str, dict[str, Any]]:
        # Never accept the legacy reusable Bearer token on this new API.
        if "authorization" in request.headers:
            raise HTTPException(401, "Owner request authentication failed")
        project = request.headers.get("x-owner-project", "")
        nonce = request.headers.get("x-owner-nonce", "")
        raw_timestamp = request.headers.get("x-owner-timestamp", "")
        signature = request.headers.get("x-owner-signature", "")
        if project not in credentials or not _NONCE_RE.fullmatch(nonce):
            raise HTTPException(401, "Owner request authentication failed")
        if not raw_timestamp or len(raw_timestamp) > 64:
            raise HTTPException(401, "Owner request authentication failed")
        try:
            timestamp = float(raw_timestamp)
        except ValueError:
            raise HTTPException(401, "Owner request authentication failed") from None
        now = time.time()
        if not math.isfinite(timestamp) or abs(now - timestamp) > _MAX_CLOCK_SKEW_SECONDS:
            raise HTTPException(401, "Owner request authentication failed")

        try:
            # Owner messages carry at most one opaque instance ID. Bound the
            # body before JSON parsing so a local malformed request cannot
            # consume arbitrary memory in the single-writer Owner process.
            parts = []
            total_bytes = 0
            async for part in request.stream():
                total_bytes += len(part)
                if total_bytes > _MAX_REQUEST_BODY_BYTES:
                    raise HTTPException(413, "Owner request body is too large")
                parts.append(part)
            raw_body = b"".join(parts)
            body = json.loads(raw_body) if raw_body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(422, "Owner request body must be JSON") from None
        if not isinstance(body, dict):
            raise HTTPException(422, "Owner request body must be an object")
        if request.method == "GET" and body:
            raise HTTPException(422, "Owner GET requests must have an empty body")

        token = credentials[project]
        expected = sign_request(
            token,
            project=project,
            nonce=nonce,
            timestamp=timestamp,
            method=request.method,
            path=request.url.path,
            body=body,
        )
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise HTTPException(401, "Owner request authentication failed")

        # Retain each challenge through the end of its accepted timestamp
        # window, including requests stamped slightly into the future.
        expiry = timestamp + _MAX_CLOCK_SKEW_SECONDS
        with nonce_lock:
            for previous, previous_expiry in tuple(used_nonces.items()):
                if previous_expiry < now:
                    del used_nonces[previous]
            if nonce in used_nonces:
                raise HTTPException(401, "Owner request authentication failed")
            used_nonces[nonce] = expiry
        return project, token, nonce, body

    def signed_response(result: OwnerResult, token: str, nonce: str) -> dict[str, Any]:
        value = asdict(result)
        return {
            "nonce": nonce,
            "result": value,
            "signature": sign_response(token, nonce=nonce, result=value),
        }

    @app.get("/v1/health")
    def health() -> dict[str, bool]:
        # Liveness is not an ownership or admission assertion.
        return {"running": True}

    @app.get("/v1/owner")
    async def snapshot(request: Request) -> dict[str, Any]:
        _, token, nonce, _ = await authenticate(request)
        result = await asyncio.to_thread(coordinator.snapshot, token)
        return signed_response(result, token, nonce)

    @app.post("/v1/owner/observe")
    async def observe(request: Request) -> dict[str, Any]:
        project, token, nonce, body = await authenticate(request)
        if project == "admin":
            raise HTTPException(401, "Owner request authentication failed")
        if body:
            raise HTTPException(422, "Owner observe requests must have an empty body")
        # Direct project probes and nvidia-smi can block for seconds. Keep the
        # API loop responsive to health and other signed requests while they
        # run; Coordinator's SQLite transaction remains the acquire authority.
        result = await asyncio.to_thread(coordinator.observe, token)
        return signed_response(result, token, nonce)

    @app.post("/v1/owner/acquire")
    async def acquire(request: Request) -> dict[str, Any]:
        project, token, nonce, value = await authenticate(request)
        if project == "admin":
            raise HTTPException(401, "Owner request authentication failed")
        try:
            body = ClaimRequest.model_validate(value)
        except ValidationError:
            raise HTTPException(422, "Owner acquire body is invalid") from None
        result = await asyncio.to_thread(coordinator.acquire, token, body.owner_instance)
        return signed_response(result, token, nonce)

    @app.post("/v1/owner/release")
    async def release(request: Request) -> dict[str, Any]:
        project, token, nonce, value = await authenticate(request)
        if project == "admin":
            raise HTTPException(401, "Owner request authentication failed")
        try:
            body = ClaimRequest.model_validate(value)
        except ValidationError:
            raise HTTPException(422, "Owner release body is invalid") from None
        result = await asyncio.to_thread(coordinator.release, token, body.owner_instance)
        return signed_response(result, token, nonce)

    return app
