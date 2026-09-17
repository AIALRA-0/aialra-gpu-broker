from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


PROJECTS = {
    "minimax": "MiniMax H3",
    "live_translate": "Live Translate",
    "manga": "Manga / PanelTone",
}


def default_data_dir() -> Path:
    override = os.environ.get("AIALRA_GPU_BROKER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "AIALRA" / "GpuBroker"
    return Path.home() / ".aialra" / "gpu-broker"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    managed_gpu_uuid: str
    display_gpu_uuid: str | None
    admin_token: str
    project_tokens: dict[str, str]
    public_origin: str | None = None
    sample_interval_seconds: float = 2.0
    stale_sample_seconds: float = 10.0
    heartbeat_timeout_seconds: float = 15.0
    session_prepare_timeout_seconds: float = 180.0
    safety_floor_mib: int = 2048
    safety_ratio: float = 0.125

    @property
    def db_path(self) -> Path:
        return self.data_dir / "broker.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "broker.lock"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_settings(data_dir: Path | None = None) -> Settings:
    root = (data_dir or default_data_dir()).resolve()
    config_path = root / "config.json"
    tokens_path = root / "tokens.json"
    if not config_path.is_file() or not tokens_path.is_file():
        raise FileNotFoundError(
            f"Broker is not initialized in {root}; run 'gpu-broker init --data-dir ...'"
        )
    config = _read_json(config_path)
    tokens = _read_json(tokens_path)
    managed = str(config.get("managed_gpu_uuid") or "")
    if not managed.startswith("GPU-"):
        raise ValueError("config.json must set a real managed_gpu_uuid")
    if set(tokens.get("projects", {})) != set(PROJECTS):
        raise ValueError("tokens.json must contain exactly the three supported projects")
    return Settings(
        data_dir=root,
        managed_gpu_uuid=managed,
        display_gpu_uuid=config.get("display_gpu_uuid"),
        admin_token=str(tokens["admin"]),
        project_tokens={key: str(value) for key, value in tokens["projects"].items()},
        public_origin=str(config["public_origin"]).rstrip("/") if config.get("public_origin") else None,
        sample_interval_seconds=float(config.get("sample_interval_seconds", 2.0)),
        stale_sample_seconds=float(config.get("stale_sample_seconds", 10.0)),
        heartbeat_timeout_seconds=float(config.get("heartbeat_timeout_seconds", 15.0)),
        session_prepare_timeout_seconds=float(config.get("session_prepare_timeout_seconds", 180.0)),
        safety_floor_mib=int(config.get("safety_floor_mib", 2048)),
        safety_ratio=float(config.get("safety_ratio", 0.125)),
    )


def initialize(data_dir: Path, managed_gpu_uuid: str, display_gpu_uuid: str | None) -> Path:
    root = data_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.json"
    tokens_path = root / "tokens.json"
    if config_path.exists() or tokens_path.exists():
        raise FileExistsError("Broker configuration already exists; refusing to replace tokens")
    if not managed_gpu_uuid.startswith("GPU-"):
        raise ValueError("managed_gpu_uuid must be an NVIDIA GPU UUID")
    config = {
        "managed_gpu_uuid": managed_gpu_uuid,
        "display_gpu_uuid": display_gpu_uuid,
        "sample_interval_seconds": 2.0,
        "stale_sample_seconds": 10.0,
        "heartbeat_timeout_seconds": 15.0,
        "session_prepare_timeout_seconds": 180.0,
        "safety_floor_mib": 2048,
        "safety_ratio": 0.125,
        "public_origin": None,
    }
    tokens = {
        "admin": secrets.token_urlsafe(36),
        "projects": {project: secrets.token_urlsafe(36) for project in PROJECTS},
    }
    for path, payload in ((config_path, config), (tokens_path, tokens)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    return root
