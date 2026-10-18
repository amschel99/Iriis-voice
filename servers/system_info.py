"""system_info MCP server (read-only): CPU, RAM, disk, battery, processes, uptime, network."""
from __future__ import annotations

import datetime as dt
import socket
import time

import psutil

from servers._common import make_server

mcp = make_server("system_info")


def _gb(n: float) -> str:
    return f"{n / 1024**3:.1f} GB"


@mcp.tool()
def system_overview() -> str:
    """CPU load, memory use, main disk use and battery, in one summary."""
    vm = psutil.virtual_memory()
    parts = [
        f"CPU {psutil.cpu_percent(interval=0.3):.0f}% across {psutil.cpu_count()} threads",
        f"RAM {vm.percent:.0f}% used ({_gb(vm.used)} of {_gb(vm.total)}, {_gb(vm.available)} free)",
    ]
    for d in disk_list():
        parts.append(d)
    b = _battery()
    if b:
        parts.append(b)
    return "; ".join(parts)


def disk_list() -> list[str]:
    out = []
    for p in psutil.disk_partitions(all=False):
        if "cdrom" in p.opts or not p.fstype:
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except (PermissionError, OSError):
            continue
        out.append(f"disk {p.mountpoint} {u.percent:.0f}% used, {_gb(u.free)} free of {_gb(u.total)}")
    return out


@mcp.tool()
def disk_usage() -> str:
    """Used and free space on each disk."""
    return "; ".join(disk_list()) or "No disks found"


def _battery() -> str:
    b = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if not b:
        return ""
    state = "charging" if b.power_plugged else "on battery"
    left = ""
    if not b.power_plugged and b.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
        left = f", about {b.secsleft // 3600}h {(b.secsleft % 3600) // 60}m left"
    return f"battery {b.percent:.0f}% {state}{left}"


@mcp.tool()
def battery() -> str:
    """Battery percentage and charging state."""
    return _battery() or "No battery detected (desktop or unsupported)."


@mcp.tool()
def uptime() -> str:
    """How long the computer has been running since last boot."""
    secs = int(time.time() - psutil.boot_time())
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    booted = dt.datetime.fromtimestamp(psutil.boot_time()).strftime("%A %H:%M")
    return f"Up {d} days {h} hours {rem // 60} minutes (booted {booted})"


@mcp.tool()
def top_processes(sort_by: str = "memory", limit: int = 5) -> str:
    """Top running processes by 'memory' or 'cpu'."""
    limit = max(1, min(limit, 20))
    procs = []
    for p in psutil.process_iter(["name", "memory_info", "pid"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if sort_by == "cpu":
        time.sleep(0.5)
        rows = []
        for p in procs:
            try:
                rows.append((p.cpu_percent(None) / psutil.cpu_count(), p.info["name"], p.info["pid"]))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        rows.sort(reverse=True)
        return "; ".join(f"{n} (pid {pid}) {c:.0f}% CPU" for c, n, pid in rows[:limit])
    agg: dict[str, int] = {}
    for p in procs:
        mi = p.info.get("memory_info")
        if mi:
            agg[p.info["name"]] = agg.get(p.info["name"], 0) + mi.rss
    top = sorted(agg.items(), key=lambda kv: -kv[1])[:limit]
    return "; ".join(f"{n} {_gb(r)}" if r > 1024**3 else f"{n} {r / 1024**2:.0f} MB" for n, r in top)


@mcp.tool()
def find_process(name: str) -> str:
    """Check whether a program is running, by (partial) process name."""
    hits = [p.info for p in psutil.process_iter(["name", "pid"]) if name.lower() in (p.info["name"] or "").lower()]
    if not hits:
        return f"No running process matches '{name}'."
    return f"{len(hits)} matching: " + ", ".join(f"{h['name']} (pid {h['pid']})" for h in hits[:10])


@mcp.tool()
def network_status() -> str:
    """Network interfaces that are up, local IP addresses, and whether the internet is reachable."""
    up = [n for n, s in psutil.net_if_stats().items() if s.isup and "loopback" not in n.lower()]
    addrs = []
    for n in up:
        for a in psutil.net_if_addrs().get(n, []):
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                addrs.append(f"{n} {a.address}")
    online = False
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=1.5):
            online = True
    except OSError:
        pass
    io = psutil.net_io_counters()
    return (f"Internet {'reachable' if online else 'NOT reachable'}. Interfaces: {', '.join(addrs) or 'none with IPv4'}. "
            f"Since boot: {_gb(io.bytes_recv)} received, {_gb(io.bytes_sent)} sent.")


if __name__ == "__main__":
    mcp.run()
