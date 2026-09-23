from __future__ import annotations

import csv
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Protocol

import psutil


MIB = 1024 * 1024


class Monitor(Protocol):
    def read(self) -> dict: ...


@dataclass
class NvidiaMonitor:
    """Read whole-card usage; WDDM process memory is intentionally not used."""

    def __post_init__(self) -> None:
        # Keep telemetry outside the Broker process.  Some NVIDIA driver/NVML
        # calls can block while holding the Python GIL under sustained GPU and
        # host-memory pressure.  asyncio.to_thread cannot isolate that failure:
        # the API loop and every heartbeat handler stop with it.  nvidia-smi is
        # a separate process with a hard timeout, so a wedged driver query makes
        # telemetry stale and admission fail closed without starving leases.
        self._nvml = None
        self._nvml_error = "in_process_nvml_disabled"

    def close(self) -> None:
        if self._nvml is not None:
            self._nvml.nvmlShutdown()
            self._nvml = None

    def _read_nvml(self) -> list[dict]:
        nvml = self._nvml
        assert nvml is not None
        output = []
        driver = nvml.nvmlSystemGetDriverVersion()
        for index in range(nvml.nvmlDeviceGetCount()):
            handle = nvml.nvmlDeviceGetHandleByIndex(index)
            memory = nvml.nvmlDeviceGetMemoryInfo(handle)
            utilization = nvml.nvmlDeviceGetUtilizationRates(handle)

            def optional(call, divisor=1):
                try:
                    return round(call() / divisor, 1)
                except Exception:
                    return None

            output.append(
                {
                    "index": index,
                    "uuid": str(nvml.nvmlDeviceGetUUID(handle)),
                    "name": str(nvml.nvmlDeviceGetName(handle)),
                    "total_mib": int(memory.total / MIB),
                    "used_mib": int(memory.used / MIB),
                    "free_mib": int(memory.free / MIB),
                    "utilization_pct": int(utilization.gpu),
                    "temperature_c": optional(lambda: nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU)),
                    "power_w": optional(lambda: nvml.nvmlDeviceGetPowerUsage(handle), 1000),
                    "display_active": optional(lambda: nvml.nvmlDeviceGetDisplayActive(handle)),
                    "driver": str(driver),
                }
            )
        return output

    def _read_smi(self) -> list[dict]:
        executable = shutil.which("nvidia-smi")
        if not executable:
            raise RuntimeError("NVML and nvidia-smi are unavailable")
        query = (
            "index,uuid,name,memory.total,memory.used,memory.free,"
            "utilization.gpu,temperature.gpu,driver_version,display_active"
        )
        result = subprocess.run(
            [executable, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        output = []
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) != 10:
                continue
            index, uuid, name, total, used, free, util, temp, driver, display = [x.strip() for x in row]
            output.append(
                {
                    "index": int(index),
                    "uuid": uuid,
                    "name": name,
                    "total_mib": int(total),
                    "used_mib": int(used),
                    "free_mib": int(free),
                    "utilization_pct": int(util),
                    "temperature_c": None if temp in ("N/A", "-") else float(temp),
                    "power_w": None,
                    "display_active": display.lower() in ("enabled", "active", "1"),
                    "driver": driver,
                }
            )
        if not output:
            raise RuntimeError("nvidia-smi returned no GPU rows")
        return output

    def read(self) -> dict:
        now = time.time()
        source = "nvml" if self._nvml is not None else "nvidia-smi"
        try:
            if self._nvml is not None:
                try:
                    cards = self._read_nvml()
                    if not cards:
                        raise RuntimeError("NVML returned no GPU rows")
                except Exception:
                    cards = self._read_smi()
                    source = "nvidia-smi"
            else:
                cards = self._read_smi()
            error = None
        except Exception as exc:
            cards = []
            error = f"{type(exc).__name__}: {exc}"
        memory = psutil.virtual_memory()
        return {
            "ok": bool(cards),
            "error": error,
            "source": source,
            "timestamp": now,
            "gpus": cards,
            "host": {
                "cpu_pct": psutil.cpu_percent(interval=None),
                "ram_total_mib": round(memory.total / MIB),
                "ram_used_mib": round(memory.used / MIB),
                "ram_available_mib": round(memory.available / MIB),
            },
        }
