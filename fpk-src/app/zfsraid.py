"""ZFS 池只读枚举：zpool list + zpool status -P。"""

from __future__ import annotations

import os
import re
from typing import Any

from util import run_cached, strip


def _size(bytes_value: int | None) -> str:
    value = float(bytes_value or 0)
    if value <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            return f"{value:.3f} {unit}" if unit in ("GB", "TB", "PB") else f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def _base(device: str) -> str:
    name = strip(device).split("/")[-1]
    if name.startswith("nvme") or name.startswith("mmcblk"):
        return re.sub(r"p\d+$", "", name)
    return re.sub(r"\d+$", "", name)


def _state_health(state: str) -> str:
    value = strip(state).upper()
    if value in ("ONLINE",):
        return "ok"
    if value in ("DEGRADED", "FAULTED", "OFFLINE", "UNAVAIL", "REMOVED"):
        return "critical" if value in ("FAULTED", "UNAVAIL", "REMOVED") else "warning"
    return "unknown"


def collect(log) -> tuple[list[dict[str, Any]], list[str]]:
    rc, out, err = run_cached(["/usr/sbin/zpool", "list", "-Hp", "-o", "name,size,alloc,free,health"], timeout=20)
    if rc != 0:
        log(f"ZFS 池枚举失败：{strip(err) or 'zpool 返回错误'}")
        return [], []
    out = out.replace("\\n", "\n").replace("\\t", "\t")
    pools: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        fields = line.split("\\t") if "\\t" in line else line.split()
        if len(fields) < 5:
            continue
        name, size, alloc, free, health = [strip(item) for item in fields[:5]]
        if not name:
            continue
        try:
            size_n = int(size)
        except ValueError:
            size_n = 0
        pools[name] = {
            "name": name,
            "size": _size(size_n),
            "health": _state_health(health),
            "state": health,
            "level": "",
            "members": [],
        }

    rc, out, err = run_cached(["/usr/sbin/zpool", "status", "-P"], timeout=20)
    if rc != 0:
        log(f"ZFS 状态读取失败：{strip(err) or 'zpool status 返回错误'}")
        return [], []
    out = out.replace("\\n", "\n").replace("\\t", "\t")
    current = ""
    in_config = False
    for raw in out.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("pool:"):
            current = stripped.split(":", 1)[1].strip()
            in_config = False
            continue
        if stripped.startswith("state:") and current in pools:
            state = stripped.split(":", 1)[1].strip()
            pools[current]["state"] = state
            pools[current]["health"] = _state_health(state)
            continue
        if stripped.startswith("config:"):
            in_config = True
            continue
        if not in_config or not current or not stripped:
            continue
        if stripped.startswith("NAME") or stripped.startswith("errors:"):
            continue
        tokens = stripped.split()
        first = tokens[0]
        if first.startswith("/dev/"):
            if first.startswith("/dev/disk/by-partuuid/"):
                first = os.path.realpath(first)
            member = _base(first)
            if member and member not in pools[current]["members"]:
                pools[current]["members"].append(member)
            continue
        if first == current:
            continue
        if re.match(r"^(raidz[123]?|mirror|draid|spare|log|cache|special|dedup|stripe)", first, re.I):
            if current in pools and not pools[current].get("level"):
                pools[current]["level"] = re.sub(r"-\d+$", "", first.upper())
    for item in pools.values():
        if not item["members"]:
            continue
        item["level"] = item["level"] or "ZFS"
        item["id"] = item["name"]
        item["os_device"] = item["name"]
    return [pool for pool in pools.values() if pool.get("members")], []
