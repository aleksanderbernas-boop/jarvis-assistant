"""Real system telemetry. Values that can't be measured are reported as None — never invented."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import psutil

from jarvis.api.schemas import GpuMetrics, SystemMetrics


def _system_drive() -> str:
    if os.name == "nt":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


class Telemetry:
    GPU_TTL_S = 3.0

    def __init__(self) -> None:
        psutil.cpu_percent(interval=None)  # prime the CPU counter
        self._last_net = psutil.net_io_counters()
        self._last_net_at = time.monotonic()
        self._gpu_cache: tuple[float, GpuMetrics | None] = (0.0, None)
        self._nvidia_smi = shutil.which("nvidia-smi")

    def _gpu(self) -> GpuMetrics | None:
        if not self._nvidia_smi:
            return None
        cached_at, cached = self._gpu_cache
        if time.monotonic() - cached_at < self.GPU_TTL_S:
            return cached
        gpu = None
        try:
            out = subprocess.run(
                [self._nvidia_smi, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip().splitlines()
            if out:
                name, util, used, total, temp = [p.strip() for p in out[0].split(",")]
                gpu = GpuMetrics(name=name, utilization_percent=float(util), memory_used_mb=float(used),
                                 memory_total_mb=float(total), temperature_c=float(temp))
        except (subprocess.SubprocessError, OSError, ValueError):
            gpu = None
        self._gpu_cache = (time.monotonic(), gpu)
        return gpu

    def sample(self) -> SystemMetrics:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(_system_drive())
        net = psutil.net_io_counters()
        now = time.monotonic()
        elapsed = max(0.001, now - self._last_net_at)
        up = (net.bytes_sent - self._last_net.bytes_sent) / elapsed / 1024
        down = (net.bytes_recv - self._last_net.bytes_recv) / elapsed / 1024
        self._last_net, self._last_net_at = net, now
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        online = any(
            stats.isup for name, stats in psutil.net_if_stats().items()
            if not name.lower().startswith(("lo", "loopback"))
        )
        return SystemMetrics(
            cpu_percent=psutil.cpu_percent(interval=None),
            ram_percent=vm.percent,
            ram_used_gb=round((vm.total - vm.available) / 1024**3, 2),
            ram_total_gb=round(vm.total / 1024**3, 2),
            disk_percent=disk.percent,
            disk_used_gb=round(disk.used / 1024**3, 1),
            disk_total_gb=round(disk.total / 1024**3, 1),
            gpu=self._gpu(),
            battery_percent=round(battery.percent, 0) if battery else None,
            battery_plugged=battery.power_plugged if battery else None,
            net_up_kbps=round(max(0.0, up), 1),
            net_down_kbps=round(max(0.0, down), 1),
            online=online,
            uptime_s=round(time.time() - psutil.boot_time()),
            process_count=len(psutil.pids()),
        )


def top_processes(sort_by: str = "memory", limit: int = 8, group: bool = True) -> list[dict[str, Any]]:
    """Heaviest processes, optionally grouped by name (Chrome's many processes → one entry)."""
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"memory_mb": 0.0, "cpu_percent": 0.0, "count": 0})
    procs = list(psutil.process_iter(["pid", "name", "memory_info"]))
    for proc in procs:
        try:
            proc.cpu_percent(None)
        except psutil.Error:
            continue
    if sort_by == "cpu":
        time.sleep(0.4)  # CPU % needs a short measurement window
    for proc in procs:
        try:
            name = (proc.info.get("name") or "unknown").removesuffix(".exe")
            memory = proc.info["memory_info"].rss / 1_048_576 if proc.info.get("memory_info") else 0.0
            cpu = proc.cpu_percent(None) / max(1, psutil.cpu_count() or 1)
        except psutil.Error:
            continue
        key = name if group else f"{name} ({proc.pid})"
        totals[key]["memory_mb"] += memory
        totals[key]["cpu_percent"] += cpu
        totals[key]["count"] += 1
    field = "cpu_percent" if sort_by == "cpu" else "memory_mb"
    ranked = sorted(totals.items(), key=lambda kv: kv[1][field], reverse=True)[:limit]
    return [
        {"name": name, "memory_mb": round(v["memory_mb"]), "cpu_percent": round(v["cpu_percent"], 1),
         "processes": int(v["count"])}
        for name, v in ranked
    ]


def os_info() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "hostname": platform.node(),
        "python": platform.python_version(),
        "cpu_cores_logical": psutil.cpu_count(),
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "home": str(Path.home()),
    }
