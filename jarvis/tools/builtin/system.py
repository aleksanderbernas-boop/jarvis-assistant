"""System telemetry tools. All values are measured; unavailable ones are reported as such."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from jarvis.telemetry.system import os_info, top_processes
from jarvis.tools.base import Tool, ToolContext, ToolResult


class SystemStatus(Tool):
    name = "system_status"
    description = "Current CPU, RAM, disk, GPU, battery, network and uptime."
    display_name = "Checking system status"
    category = "system"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        m = await asyncio.to_thread(ctx.services.telemetry.sample)
        parts = [f"CPU {m.cpu_percent:.0f}%", f"RAM {m.ram_percent:.0f}% ({m.ram_used_gb:.1f}/{m.ram_total_gb:.1f} GB)",
                 f"disk {m.disk_percent:.0f}% used"]
        if m.gpu and m.gpu.utilization_percent is not None:
            parts.append(f"GPU {m.gpu.utilization_percent:.0f}%")
        if m.battery_percent is not None:
            parts.append(f"battery {m.battery_percent:.0f}%{' charging' if m.battery_plugged else ''}")
        data = m.model_dump()
        if m.gpu is None:
            data["gpu"] = "not measurable on this system (no NVIDIA GPU / nvidia-smi)"
        return ToolResult.success(", ".join(parts), data)


class TopProcessesInput(BaseModel):
    sort_by: Literal["memory", "cpu"] = "memory"
    limit: int = Field(8, ge=1, le=30)


class TopProcesses(Tool):
    name = "top_processes"
    description = "Which applications/processes use the most memory or CPU (grouped by app)."
    display_name = "Inspecting processes"
    category = "system"
    Input = TopProcessesInput
    parallel_safe = True

    async def execute(self, args: TopProcessesInput, ctx: ToolContext) -> ToolResult:
        rows: list[dict[str, Any]]
        if ctx.platform.simulated:
            procs = sorted(ctx.platform.processes(), key=lambda p: p.memory_mb, reverse=True)[: args.limit]
            rows = [{"name": p.exe or p.name, "memory_mb": round(p.memory_mb), "cpu_percent": p.cpu_percent,
                     "processes": 1} for p in procs]
        else:
            rows = await asyncio.to_thread(top_processes, args.sort_by, args.limit)
        if not rows:
            return ToolResult.failure("could not read the process list")
        top = rows[0]
        metric = f"{float(top['memory_mb']) / 1024:.1f} GB" if args.sort_by == "memory" else f"{top['cpu_percent']}% CPU"
        return ToolResult.success(f"{top['name']} uses the most {args.sort_by} ({metric})", {"processes": rows},
                                  simulated=ctx.platform.simulated)


class OsInfo(Tool):
    name = "os_info"
    description = "Operating system, hardware and machine information."
    display_name = "Reading system information"
    category = "system"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        info = await asyncio.to_thread(os_info)
        return ToolResult.success(f"{info['system']} {info['release']} on {info['machine']}", info)


class NetworkStatus(Tool):
    name = "network_status"
    description = "Check internet connectivity and current network throughput."
    display_name = "Checking the network"
    category = "system"
    parallel_safe = True

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        metrics = await asyncio.to_thread(ctx.services.telemetry.sample)
        reachable, latency = False, None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                start = asyncio.get_running_loop().time()
                resp = await client.head("https://www.gstatic.com/generate_204")
                latency = round((asyncio.get_running_loop().time() - start) * 1000)
                reachable = resp.status_code < 500
        except httpx.HTTPError:
            reachable = False
        summary = f"Internet {'reachable' if reachable else 'NOT reachable'}"
        if latency is not None:
            summary += f" ({latency} ms)"
        return ToolResult.success(summary, {"internet": reachable, "latency_ms": latency,
                                            "interfaces_up": metrics.online, "up_kbps": metrics.net_up_kbps,
                                            "down_kbps": metrics.net_down_kbps})


TOOLS = [SystemStatus, TopProcesses, OsInfo, NetworkStatus]
