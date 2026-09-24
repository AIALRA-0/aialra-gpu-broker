"""Canonical HMAC messages for the standalone Owner API."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Encode an Owner authentication object in its one canonical form."""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sign_request(
    credential: str,
    *,
    project: str,
    nonce: str,
    timestamp: float,
    method: str,
    path: str,
    body: dict[str, Any],
) -> str:
    """Sign the identity, freshness challenge, route, and parsed JSON body."""

    return _sign(credential, {
        "project": project,
        "nonce": nonce,
        "timestamp": timestamp,
        "method": method.upper(),
        "path": path,
        "body": body,
    })


def sign_response(credential: str, *, nonce: str, result: dict[str, Any]) -> str:
    """Sign one successful API result while binding it to its request nonce."""

    return _sign(credential, {"nonce": nonce, "result": result})


def verify_response(
    credential: str,
    *,
    nonce: str,
    result: dict[str, Any],
    signature: str,
) -> bool:
    """Return whether a response envelope has a valid Owner HMAC."""

    if not isinstance(signature, str):
        return False
    return hmac.compare_digest(
        signature,
        sign_response(credential, nonce=nonce, result=result),
    )


def _sign(credential: str, payload: dict[str, Any]) -> str:
    return hmac.new(
        credential.encode("utf-8"),
        canonical_json(payload),
        hashlib.sha256,
    ).hexdigest()
