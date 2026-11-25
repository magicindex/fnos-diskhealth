"""Linux 软 RAID（mdadm）只读枚举：/proc/mdstat + /sys/block/md*。"""

from __future__ import annotations

import os
import re
from typing import Any

from util import read_text, strip, to_int

MDSTAT = "/proc/mdstat"
SYS_BLOCK = "/sys/block"


def _size(bytes_value: int) -> str:
    value = float(bytes_value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.3f} {unit}" if unit in ("GB", "TB") else f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def _base_device(name: str) -> str:
    name = strip(name)
    if not name:
        return ""
    if name.startswith("nvme") or name.startswith("mmcblk"):
        return re.sub(r"p\d+$", "", name)
    return re.sub(r"\d+$", "", name)


def _mdname(name: str) -> str:
    return strip(name).split("/")[-1]


def _array_health(array_state: str, sync_action: str, degraded: int) -> str:
    state = array_state.lower()
    action = sync_action.lower()
    if state in ("inactive", "broken", "failed") or degraded > 0:
        return "critical"
    if action in ("resync", "recover", "reshape", "check", "repair"):
        return "warning"
    return "ok"


def _member_states(md: str) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    base = os.path.join(SYS_BLOCK, md, "md")
    try:
        names = os.listdir(base)
    except OSError:
        return result
    for name in names:
        if not name.startswith("dev-"):
            continue
        device = name[4:]
        path = os.path.join(base, name)
        result[device] = {
            "state": strip(read_text(os.path.join(path, "state"))),
            "slot": strip(read_text(os.path.join(path, "slot"))),
            "errors": strip(read_text(os.path.join(path, "errors"))),
        }
    return result


def _parse_mdstat() -> dict[str, dict[str, Any]]:
    arrays: dict[str, dict[str, Any]] = {}
    try:
        text = read_text(MDSTAT)
    except OSError:
        return arrays
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(md\d+)\s*:\s*(active|inactive)(?:\s+\([^)]*\))?\s+(\w+)\s*(.*)$", line.strip())
        if not match:
            continue
        name, state, level, members_text = match.groups()
        members = re.findall(r"([A-Za-z0-9:_.-]+)\[\d+\](?:\([A-Z]\))?", members_text)
        arrays[name] = {
            "name": name,
            "state": state,
            "level": level,
            "members": members,
            "raw": line.strip(),
        }
    return arrays


def collect(log) -> tuple[list[dict[str, Any]], list[str]]:
    arrays = _parse_mdstat()
    if not arrays:
        return [], []
    warnings: list[str] = []
    vds: list[dict[str, Any]] = []
    member_map: dict[str, list[dict[str, str]]] = {}
    all_members: set[str] = set()
    for name in sorted(arrays, key=lambda value: int(value[2:]) if value[2:].isdigit() else value):
        item = arrays[name]
        path = os.path.join(SYS_BLOCK, name)
        level = strip(read_text(os.path.join(path, "md", "level"))) or strip(item.get("level")) or "raid"
        array_state = strip(read_text(os.path.join(path, "md", "array_state"))) or strip(item.get("state"))
        sync_action = strip(read_text(os.path.join(path, "md", "sync_action"))) or "idle"
        sync_completed = strip(read_text(os.path.join(path, "md", "sync_completed")))
        degraded = to_int(read_text(os.path.join(path, "md", "degraded"))) or 0
        raid_disks = to_int(read_text(os.path.join(path, "md", "raid_disks"))) or 0
        component_size = to_int(read_text(os.path.join(path, "md", "component_size"))) or 0
        chunk_size = to_int(read_text(os.path.join(path, "md", "chunk_size"))) or 0
        sys_size = to_int(read_text(os.path.join(path, "size"))) or 0
        uuid = strip(read_text(os.path.join(path, "md", "uuid")))
        metadata = strip(read_text(os.path.join(path, "md", "metadata_version")))
        states = _member_states(name)
        members = []
        for device in item.get("members") or []:
            info = states.get(device, {})
            members.append({
                "device": device,
                "base": _base_device(device),
                "state": info.get("state") or "",
                "slot": info.get("slot") or "",
                "errors": info.get("errors") or "",
            })
            all_members.add(_base_device(device))
            member_map.setdefault(_base_device(device), []).append({
                "array": name,
                "level": level.upper() if level.startswith("raid") else level.upper(),
                "device": device,
                "state": info.get("state") or "",
            })
        health = _array_health(array_state, sync_action, degraded)
        progress = ""
        if sync_action in ("resync", "recover", "reshape", "check", "repair") and sync_completed:
            if "/" in sync_completed:
                done, total = sync_completed.split("/", 1)
                if to_int(total):
                    progress = f"{round(100 * (to_int(done) or 0) / (to_int(total) or 1))}%"
            if progress:
                progress = f"{sync_action} {progress}"
            else:
                progress = sync_action
        state_label = {"active": "正常", "inactive": "未激活", "clean": "正常"}.get(array_state.lower(), array_state or "—")
        vds.append({
            "id": name,
            "index": name,
            "dg": None,
            "level": level.upper() if level.startswith("raid") else level.upper(),
            "state": array_state,
            "state_label": state_label,
            "size": _size(sys_size * 512) if sys_size else "—",
            "name": name,
            "os_device": f"/dev/{name}",
            "members": [m["device"] for m in members],
            "member_details": members,
            "health": health,
            "raid_disks": raid_disks,
            "degraded": degraded,
            "sync_action": sync_action,
            "sync_progress": progress,
            "component_size": component_size,
            "chunk_size": chunk_size,
            "metadata_version": metadata,
            "uuid": uuid,
        })
    if not vds:
        return [], warnings
    controller_health = "ok"
    for vd in vds:
        if vd["health"] == "critical":
            controller_health = "critical"
        elif vd["health"] == "warning" and controller_health == "ok":
            controller_health = "warning"
    controller = {
        "id": "md",
        "index": -1,
        "kind": "soft",
        "kind_label": "Linux 软 RAID（mdadm）",
        "model": "Linux MD RAID",
        "driver": "md",
        "firmware": "",
        "serial": "",
        "pci": "",
        "sas_address": "",
        "host_interface": "",
        "device_interface": "",
        "temperature_c": None,
        "bbu": {},
        "memory_correctable": None,
        "memory_uncorrectable": None,
        "controller_status": "Optimal" if controller_health == "ok" else "Degraded",
        "ports": 0,
        "health": controller_health,
        "vds": vds,
        "pds": [],
        "enclosures": [],
        "details": {},
        "smart_bus": -1,
        "warnings": warnings,
        "enclosure_count": 0,
        "expander_count": 0,
        "disk_count": len(all_members),
        "filterable": False,
        "topology": False,
    }
    # Return member map via a private key consumed by the collector.
    controller["_member_map"] = member_map
    return [controller], warnings
