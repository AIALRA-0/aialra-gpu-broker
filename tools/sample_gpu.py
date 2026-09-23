"""Record high-frequency, read-only GPU samples for a controlled task run.

The output is local evidence. Start this before submitting a task and stop it
after the backend has released its model; record task timestamps separately.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from gpu_broker.monitor import NvidiaMonitor


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Output contains GPU UUIDs, timestamps, task labels, and host memory readings. "
            "Save it outside the repository; do not commit measurement files."
        ),
    )
    parser.add_argument("--gpu-uuid", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--interval-ms", type=int, default=200)
    parser.add_argument("--label", default="controlled-gpu-task")
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 3600:
        parser.error("duration-seconds must be between 1 and 3600")
    if not 100 <= args.interval_ms <= 2000:
        parser.error("interval-ms must be between 100 and 2000")

    monitor = NvidiaMonitor()
    interval = args.interval_ms / 1000
    deadline = time.monotonic() + args.duration_seconds
    count = 0
    try:
        with args.output.open("x", encoding="utf-8", buffering=1) as output:
            while time.monotonic() < deadline:
                next_read = time.monotonic() + interval
                snapshot = monitor.read()
                if not snapshot["ok"]:
                    raise RuntimeError("GPU telemetry unavailable during measurement")
                cards = [card for card in snapshot["gpus"] if card["uuid"] == args.gpu_uuid]
                if len(cards) != 1:
                    raise RuntimeError("Managed GPU UUID missing or duplicated")
                card = cards[0]
                row = {
                    "timestamp": snapshot["timestamp"],
                    "label": args.label,
                    "gpu_uuid": args.gpu_uuid,
                    "used_mib": card["used_mib"],
                    "free_mib": card["free_mib"],
                    "utilization_pct": card["utilization_pct"],
                    "host_ram_available_mib": snapshot["host"]["ram_available_mib"],
                }
                output.write(json.dumps(row, separators=(",", ":")) + "\n")
                count += 1
                time.sleep(max(0, next_read - time.monotonic()))
    finally:
        monitor.close()
    print(f"Wrote {count} GPU samples to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
