from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .config import default_data_dir, initialize, load_settings
from .monitor import NvidiaMonitor


def main() -> None:
    parser = argparse.ArgumentParser(description="AIALRA local GPU broker")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create local config and project tokens")
    init.add_argument("--data-dir", type=Path, default=default_data_dir())
    init.add_argument("--managed-gpu-uuid", required=True)
    init.add_argument("--display-gpu-uuid")
    serve = sub.add_parser("serve", help="Run one local API and scheduler process")
    serve.add_argument("--data-dir", type=Path, default=default_data_dir())
    serve.add_argument("--port", type=int, default=18765)
    doctor = sub.add_parser("doctor", help="Read GPU and database readiness")
    doctor.add_argument("--data-dir", type=Path, default=default_data_dir())
    backup = sub.add_parser("backup", help="Create and verify a SQLite backup")
    backup.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args()

    if args.command == "init":
        root = initialize(args.data_dir, args.managed_gpu_uuid, args.display_gpu_uuid)
        print(f"Initialized: {root}")
        print(f"Tokens: {root / 'tokens.json'} (keep private)")
        return
    settings = load_settings(args.data_dir)
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            parser.error("port must be 1..65535")
        # Do not enable reload or multiple workers: one instance owns all grants.
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, workers=1, reload=False, access_log=False)
        return
    from .core import Broker

    broker = Broker(settings, NvidiaMonitor())
    try:
        if args.command == "doctor":
            print(broker.integrity_check())
            print(broker.monitor.read())
            return
        if args.command == "backup":
            print(broker.backup())
            return
    finally:
        broker.stop()
    sys.exit(2)


if __name__ == "__main__":
    main()
