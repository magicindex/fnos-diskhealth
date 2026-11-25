"""唯一的硬件写操作：定位灯 start/stop locate。

- 仅管理员调用
- 30 秒自动停止
- 只接受 PD / 硬件 RAID VD / md 阵列目标
- 不做创建、删除、重建、导入等操作
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

import audit
import raid
from util import read_text, strip, to_int

AUTO_STOP_SECONDS = 30
_lock = threading.Lock()
_active: dict[str, dict[str, Any]] = {}


class LocateError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _matches_pd(controller: dict[str, Any], eid: str, slot: str) -> dict[str, Any] | None:
    for pd in controller.get("pds") or []:
        if str(pd.get("eid")) == str(eid) and str(pd.get("slot")) == str(slot):
            return pd
    return None


def _pd_target(snapshot: dict[str, Any], disk_id: str) -> tuple[str, list[tuple[str, str]]]:
    disk = next((item for item in snapshot.get("disks") or [] if item.get("id") == disk_id), None)
    if not disk or disk.get("kind") != "raid":
        raise LocateError("目标不是可定位的 RAID 物理盘", 404)
    cid = str(disk.get("controller_id") or "")
    index = "".join(ch for ch in cid if ch.isdigit())
    eid = str(disk.get("eid") or "")
    slot = str(disk.get("slot") or "")
    if not (index and eid and slot):
        raise LocateError("物理盘缺少控制器 / EID / Slot，无法定位", 400)
    path = f"/c{index}/e{eid}/s{slot}"
    label = f"{disk_id} {disk.get('bay_label') or disk.get('bay') or ''}".strip()
    return label, [(path, label)]


def _vd_target(snapshot: dict[str, Any], target: str) -> tuple[str, list[tuple[str, str]]]:
    controller = next((c for c in snapshot.get("controllers") or [] if c.get("id") == target.split("/", 1)[0]), None)
    if not controller:
        raise LocateError("未找到对应控制器", 404)
    vd = next((v for v in controller.get("vds") or [] if v.get("id") == target), None)
    if not vd:
        raise LocateError("未找到对应虚拟盘 / 阵列组", 404)
    index = "".join(ch for ch in str(controller.get("id") or "") if ch.isdigit())
    members = []
    for member in vd.get("members") or []:
        if isinstance(member, dict):
            eid, slot = member.get("eid"), member.get("slot")
        else:
            parts = str(member).split(":")
            eid, slot = (parts[0], parts[1]) if len(parts) >= 2 else (None, None)
        if eid is not None and slot is not None:
            members.append((str(eid), str(slot)))
    # Fall back to disks associated with the VD.
    if not members:
        for disk in controller.get("disks") or []:
            if disk.get("vd_id") == target:
                members.append((str(disk.get("eid")), str(disk.get("slot"))))
    commands = []
    for eid, slot in members:
        if index:
            commands.append((f"/c{index}/e{eid}/s{slot}", f"EID {eid}:{slot}"))
    if not commands:
        raise LocateError("该阵列组没有可定位的成员盘", 404)
    return f"{target} {vd.get('level') or ''} ({len(commands)} 块成员盘)", commands


def _md_target(snapshot: dict[str, Any], target: str) -> tuple[str, list[tuple[str, str]]]:
    text = read_text("/proc/mdstat")
    match = re.search(rf"^{re.escape(target)}\s*:.*$", text, re.M)
    if not match:
        raise LocateError("未找到对应 md 阵列", 404)
    # mdstat members are on following lines; use sysfs for reliability.
    sys_md = f"/sys/block/{target}/md"
    try:
        devices = [name[4:] for name in os.listdir(sys_md) if name.startswith("dev-")]
    except OSError:
        devices = []
    commands: list[tuple[str, str]] = []
    seen: set[str] = set()
    for device in devices:
        base = re.sub(r"p\d+$", "", device) if (device.startswith("nvme") or device.startswith("mmcblk")) else re.sub(r"\d+$", "", device)
        if base in seen:
            continue
        seen.add(base)
        for controller in snapshot.get("controllers") or []:
            if controller.get("kind") != "raid":
                continue
            index = "".join(ch for ch in str(controller.get("id") or "") if ch.isdigit())
            for vd in controller.get("vds") or []:
                if os.path.basename(str(vd.get("os_device") or "")) != base:
                    continue
                for member in vd.get("members") or []:
                    parts = str(member).split(":")
                    if len(parts) >= 2 and index:
                        command = f"/c{index}/e{parts[0]}/s{parts[1]}"
                        if command not in [item[0] for item in commands]:
                            commands.append((command, f"EID {parts[0]}:{parts[1]}"))
    if not commands:
        raise LocateError("md 阵列成员没有可控制的硬件定位灯", 422)
    return f"{target} 软 RAID ({len(commands)} 块硬件成员盘)", commands


def resolve(snapshot: dict[str, Any], target: str) -> tuple[str, list[tuple[str, str]]]:
    target = strip(target)
    if not target:
        raise LocateError("缺少定位目标", 400)
    if target.startswith("md") and target[2:].isdigit():
        return _md_target(snapshot, target)
    if "/" in target and target.startswith("c"):
        return _vd_target(snapshot, target)
    return _pd_target(snapshot, target)


def _run(commands: list[tuple[str, str]], action: str) -> None:
    failures = []
    for command, label in commands:
        rc, out, err = raid.run([raid.PERCCLI, command, "start" if action == "on" else "stop", "locate"], timeout=30)
        if rc != 0:
            failures.append(f"{label}: {(err or out).strip()[-160:]}")
    if failures:
        raise LocateError("定位灯切换失败：" + "；".join(failures), 502)


def _auto_stop(target: str) -> None:
    with _lock:
        item = _active.pop(target, None)
    if not item:
        return
    try:
        _run(item["commands"], "off")
        audit.record(item.get("user"), "raid.locate.auto_stop", target=item.get("label") or target, result="ok", detail="30s timeout")
    except LocateError as exc:
        audit.record(item.get("user"), "raid.locate.auto_stop", target=item.get("label") or target, result="failed", detail=exc.message)
    except Exception as exc:  # pragma: no cover - defensive
        audit.record(item.get("user"), "raid.locate.auto_stop", target=item.get("label") or target, result="failed", detail=str(exc))


def start(snapshot: dict[str, Any], target: str, user: dict[str, Any] | None) -> dict[str, Any]:
    label, commands = resolve(snapshot, target)
    with _lock:
        if target in _active:
            raise LocateError("该目标正在定位，请先停止", 409)
        _run(commands, "on")
        until = int(time.time()) + AUTO_STOP_SECONDS
        timer = threading.Timer(AUTO_STOP_SECONDS, _auto_stop, args=(target,))
        timer.daemon = True
        _active[target] = {
            "target": target,
            "label": label,
            "commands": commands,
            "until": until,
            "user": user or {},
            "timer": timer,
        }
        timer.start()
    return {"target": target, "label": label, "until": until, "seconds": AUTO_STOP_SECONDS}


def stop(target: str, user: dict[str, Any] | None, *, auto: bool = False) -> dict[str, Any]:
    with _lock:
        item = _active.pop(target, None)
    if not item:
        raise LocateError("该目标当前未在定位", 404)
    item["timer"].cancel()
    _run(item["commands"], "off")
    return {"target": target, "label": item.get("label") or target}


def status() -> list[dict[str, Any]]:
    now = int(time.time())
    with _lock:
        expired = [key for key, item in _active.items() if item["until"] <= now]
    for key in expired:
        _auto_stop(key)
    with _lock:
        return [
            {
                "target": target,
                "label": item.get("label") or target,
                "until": item["until"],
                "seconds_left": max(0, item["until"] - now),
                "user": (item.get("user") or {}).get("username") or "",
            }
            for target, item in _active.items()
        ]
