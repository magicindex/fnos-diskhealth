"""采集编排与后台刷新。"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from typing import Any

import health
import history
import inventory
import raid
import smart
from util import iso_now, natural_key, now, read_text, strip, to_int

DATA_DIR = os.environ.get("DATA_DIR", "/data")
REFRESH_INTERVAL = max(30, to_int(os.environ.get("REFRESH_INTERVAL")) or 120)
SMART_WORKERS = max(2, min(8, to_int(os.environ.get("SMART_WORKERS")) or 6))


def ensure_bus_nodes() -> list[str]:
    """为 smartctl 的 MegaRAID 直通模式创建 /dev/bus/N 字符设备。"""
    messages: list[str] = []
    major = 240
    dev_text = strip(read_text("/sys/class/misc/megaraid_sas_ioctl_node/dev"))
    if ":" in dev_text:
        major = to_int(dev_text.split(":")[0]) or 240
    try:
        os.makedirs("/dev/bus", exist_ok=True)
    except OSError as exc:
        return [f"无法创建 /dev/bus：{exc}"]
    for index in range(8):
        node = f"/dev/bus/{index}"
        if os.path.exists(node):
            continue
        try:
            os.mknod(node, 0o600 | stat.S_IFCHR, os.makedev(major, index))
        except OSError as exc:
            messages.append(f"无法创建 {node}：{exc}")
    return messages


def _fmt_size(size: int | float | None) -> str:
    if not size:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1000 or unit == "PB":
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1000
    return f"{value:.2f} PB"


def collect_all() -> tuple[dict[str, Any], list[str]]:
    started = time.time()
    warnings: list[str] = []
    seen: set[str] = set()

    def log(message: str) -> None:
        message = strip(message)
        if message and message not in seen:
            seen.add(message)
            warnings.append(message)

    warnings.extend(ensure_bus_nodes())

    raid_controllers, raid_warnings = raid.enumerate_controllers(log)
    for warning in raid_warnings:
        log(warning)
    direct_controllers, direct_warnings = inventory.collect(log)
    for warning in direct_warnings:
        log(warning)

    disks: list[dict[str, Any]] = []

    for controller in raid_controllers:
        for pd in controller.get("pds") or []:
            disk = dict(pd)
            disk["kind"] = "raid"
            if disk.get("did") is not None:
                disk["device"] = f"megaraid,{disk['did']}"
            disk["controller_id"] = controller["id"]
            disk["controller_name"] = controller["model"]
            disk["controller_kind"] = "raid"
            disk["kind_label"] = "RAID 物理盘"
            if disk.get("vd_id"):
                disk["role"] = f"阵列成员 · {disk.get('vd_level') or 'RAID'}"
            elif disk.get("state") == "UGood":
                disk["role"] = "未配置 / 空闲"
            else:
                disk["role"] = disk.get("state_label") or "阵列磁盘"
            disks.append(disk)

    for controller in direct_controllers:
        for disk in controller.get("disks") or []:
            disk["kind"] = disk.get("kind") or "direct"
            disk["controller_id"] = controller["id"]
            disk["controller_name"] = controller["model"]
            disk["controller_kind"] = controller["kind"]
            disk["kind_label"] = (
                "HBA 直通盘" if controller["kind"] == "hba" else "直连磁盘"
            )
            disks.append(disk)

    # 并行读取 SMART（不同控制器/磁盘互不阻塞）
    jobs: list[tuple[dict[str, Any], Any]] = []
    for disk in disks:
        if disk["kind"] == "raid":
            did = disk.get("did")
            if did is None:
                continue
            bus = 0
            for controller in raid_controllers:
                if controller["id"] == disk["controller_id"]:
                    bus = controller.get("smart_bus") or 0
                    break
            func = partial(
                smart.read_smart,
                f"/dev/bus/{bus}",
                device_type=f"megaraid,{did}",
                guard_standby=False,
                timeout=45,
            )
        else:
            device = disk.get("device") or ""
            if not device:
                continue
            func = partial(
                smart.read_smart,
                device,
                device_type=disk.get("device_type") or None,
                guard_standby=disk.get("device_type") == "sat",
                timeout=35,
            )
        jobs.append((disk, func))

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=SMART_WORKERS, thread_name_prefix="smart") as pool:
        future_map = {pool.submit(func): disk for disk, func in jobs}
        for future in as_completed(future_map):
            disk = future_map[future]
            try:
                results[disk["id"]] = future.result()
            except Exception as exc:  # pragma: no cover - 防御
                results[disk["id"]] = {
                    "available": False,
                    "passed": None,
                    "power_mode": None,
                    "temperature_c": None,
                    "power_on_hours": None,
                    "rotation_rate": None,
                    "self_test": None,
                    "attributes": [],
                    "scsi": {},
                    "nvme": {},
                    "messages": [],
                    "error": f"SMART 读取异常：{exc}",
                    "exit_code": 1,
                }

    for disk in disks:
        info = results.get(disk["id"]) or {}
        disk["smart"] = info
        disk["smart_metrics"] = smart.metrics(info)
        protocol = str(info.get("protocol") or "").upper()
        if protocol:
            disk["interface"] = {"ATA": "SATA", "SAS": "SAS", "NVME": "NVMe"}.get(protocol, disk.get("interface"))
        if info.get("vendor"):
            disk["vendor"] = info["vendor"]
        if not disk.get("model") and info.get("product"):
            disk["model"] = info["product"]
        if info.get("protocol_name"):
            detail = info["protocol_name"]
            disk["protocol_detail"] = "SATA" if detail.upper() == "ATA" else detail
        if info.get("scsi_version"):
            disk["scsi_version"] = info["scsi_version"]
        if info.get("form_factor"):
            disk["form_factor"] = info["form_factor"]
        if disk.get("temperature_trip_c") is None:
            disk["temperature_trip_c"] = info.get("temperature_trip_c")
        if not disk.get("link_speed") and info.get("interface_speed"):
            disk["link_speed"] = info["interface_speed"]
        rotation = info.get("rotation_rate")
        if rotation == 0:
            disk["media"] = "SSD"
        if disk.get("temperature_c") is None:
            disk["temperature_c"] = info.get("temperature_c")
        if disk.get("temperature_c") is None:
            disk["temperature_c"] = (info.get("nvme") or {}).get("temperature_c")
        disk["power_on_hours"] = info.get("power_on_hours")
        if info.get("power_mode"):
            disk["power_mode"] = info["power_mode"]

        if disk["kind"] == "direct":
            nvme = info.get("nvme") or {}
            if nvme.get("media_errors") is not None:
                disk["media_errors"] = nvme.get("media_errors")
            elif info.get("passed") is False:
                disk["media_errors"] = None
            disk["smart_alert"] = info.get("passed") is False

        verdict = health.evaluate(disk)
        disk.update(verdict)

    for controller in raid_controllers + direct_controllers:
        controller_disks = [d for d in disks if d.get("controller_id") == controller["id"]]
        controller["disks"] = controller_disks
        controller["disk_count"] = len(controller_disks)
        controller["health"] = health.controller_health(controller)
        controller["health_label"] = health.LEVEL_LABELS.get(controller["health"], "未知")
        if not controller_disks:
            log(f"{controller['model']}（{controller['id']}）未返回物理磁盘")

    summaries = {"ok": 0, "warning": 0, "critical": 0, "unknown": 0}
    for disk in disks:
        key = disk.get("health") if disk.get("health") in summaries else "unknown"
        summaries[key] += 1

    vds = [vd for controller in raid_controllers for vd in controller.get("vds") or []]
    vd_optimal = sum(1 for vd in vds if vd.get("health") == "ok")
    topology = _build_topology(raid_controllers + direct_controllers, disks)

    snapshot = {
        "schema": 1,
        "generated_at": iso_now(),
        "generated_ts": int(now().timestamp()),
        "host": socket.gethostname(),
        "refresh_interval": REFRESH_INTERVAL,
        "duration_ms": int((time.time() - started) * 1000),
        "summary": {
            "controllers": len(raid_controllers) + len(direct_controllers),
            "raid_controllers": len(raid_controllers),
            "disks": len(disks),
            "ok": summaries["ok"],
            "warning": summaries["warning"],
            "critical": summaries["critical"],
            "unknown": summaries["unknown"],
            "vds": len(vds),
            "vds_optimal": vd_optimal,
            "enclosures": topology["enclosures"],
            "expanders": topology["expanders"],
        },
        "controllers": [_public_controller(c) for c in raid_controllers + direct_controllers],
        "topology": topology,
        "disks": [_sort_disk(d) for d in disks],
        "warnings": warnings,
    }
    snapshot["disks"].sort(key=lambda d: (_controller_order(d.get("controller_id")), natural_key(d.get("bay") or "")))
    return snapshot, warnings


def _controller_order(controller_id: str | None) -> tuple[int, list[Any]]:
    cid = controller_id or ""
    if cid.startswith("c"):
        return (0, natural_key(cid))
    if cid.startswith("hba"):
        return (1, natural_key(cid))
    if cid.startswith("sata"):
        return (2, natural_key(cid))
    return (3, natural_key(cid))


def _sort_disk(disk: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "kind",
        "controller_id",
        "controller_name",
        "controller_kind",
        "kind_label",
        "bay",
        "bay_label",
        "eid",
        "slot",
        "did",
        "state",
        "state_label",
        "role",
        "size",
        "size_bytes",
        "interface",
        "media",
        "model",
        "serial",
        "firmware",
        "wwn",
        "vendor",
        "protocol_detail",
        "scsi_version",
        "form_factor",
        "temperature_trip_c",
        "device",
        "dg",
        "vd_id",
        "vd_level",
        "temperature_c",
        "power_on_hours",
        "media_errors",
        "other_errors",
        "predictive_failures",
        "smart_alert",
        "link_speed",
        "sas_address",
        "ses_enclosure",
        "ses_bay",
        "expander_id",
        "expander_product",
        "expander_vendor",
        "phy_id",
        "link_rate",
        "end_device_id",
        "enclosure_id",
        "enclosure_product",
        "enclosure_device_type",
        "device_speed",
        "raw_size",
        "coerced_size",
        "non_coerced_size",
        "physical_sector_size",
        "connected_port",
        "port_status",
        "port_linkspeed",
        "multipath",
        "certified",
        "sed_capable",
        "health",
        "health_label",
        "reasons",
        "smart_metrics",
        "smart",
    )
    return {key: disk.get(key) for key in keys if key in disk}


def _public_controller(controller: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "kind",
        "kind_label",
        "model",
        "serial",
        "firmware",
        "firmware_package",
        "bios",
        "driver",
        "driver_version",
        "pci",
        "sas_address",
        "ports",
        "health",
        "health_label",
        "temperature_c",
        "bbu",
        "memory_correctable",
        "memory_uncorrectable",
        "controller_status",
        "host_interface",
        "enclosure_count",
        "expander_count",
        "details",
        "disk_count",
        "vds",
        "warnings",
    )
    data = {key: controller.get(key) for key in keys if key in controller}
    data["vds"] = [
        {k: v for k, v in vd.items() if k not in ("member_dids",)}
        for vd in controller.get("vds") or []
    ]
    return data


def _topology_disk(disk: dict[str, Any]) -> dict[str, Any]:
    return {
        key: disk.get(key)
        for key in (
            "id", "bay", "bay_label", "model", "serial", "state", "state_label",
            "health", "health_label", "temperature_c", "link_speed", "link_rate",
            "sas_address", "ses_bay", "ses_enclosure", "expander_id",
            "expander_product", "enclosure_id", "enclosure_product",
            "controller_name", "controller_id", "kind_label",
        )
        if disk.get(key) not in (None, "", [], {})
    }


def _build_topology(controllers: list[dict[str, Any]], disks: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {disk.get("id"): disk for disk in disks if disk.get("id")}
    items: list[dict[str, Any]] = []
    enclosure_total = 0
    expander_total = 0
    for controller in controllers:
        details = controller.get("details") or {}
        enclosures = []
        covered: set[str] = set()
        for enclosure in controller.get("enclosures") or []:
            drive_ids = list(enclosure.get("drive_ids") or [])
            drives = [_topology_disk(by_id[did]) for did in drive_ids if did in by_id]
            covered.update(drive_ids)
            item = {
                key: enclosure.get(key)
                for key in (
                    "id", "eid", "kind", "kind_label", "device_type", "enclosure_type",
                    "status", "health", "vendor", "product", "revision", "fru", "serial",
                    "zoning", "partner", "position", "connector", "slots", "drives",
                    "power_supplies", "fans", "temperature_sensors", "alarms", "sim",
                    "port", "expander", "ses", "expander_id", "enclosure_identifier",
                )
                if enclosure.get(key) not in (None, "", [], {})
            }
            item["drives"] = drives
            item["drive_count"] = len(drives)
            enclosures.append(item)
            enclosure_total += 1
        expanders = []
        for expander in controller.get("expanders") or []:
            expanders.append(
                {
                    key: expander.get(key)
                    for key in (
                        "id", "kind", "kind_label", "host", "sas_address", "component_id",
                        "component_vendor", "component_revision", "vendor", "product",
                        "revision", "level", "status", "ses", "phy_count", "link_rates",
                        "invalid_dword_count", "loss_of_dword_sync_count",
                        "phy_reset_problem_count", "running_disparity_error_count",
                    )
                    if expander.get(key) not in (None, "", [], {})
                }
            )
            expander_total += 1
        controller_disks = [d for d in disks if d.get("controller_id") == controller.get("id")]
        direct = [_topology_disk(d) for d in controller_disks if d.get("id") not in covered]
        items.append(
            {
                "id": controller.get("id"),
                "model": controller.get("model"),
                "kind": controller.get("kind"),
                "kind_label": controller.get("kind_label"),
                "health": controller.get("health"),
                "pci": controller.get("pci"),
                "sas_address": controller.get("sas_address"),
                "firmware": controller.get("firmware"),
                "bios": controller.get("bios"),
                "driver": controller.get("driver"),
                "driver_version": controller.get("driver_version"),
                "temperature_c": controller.get("temperature_c"),
                "bbu": controller.get("bbu"),
                "controller_status": controller.get("controller_status"),
                "memory_correctable": controller.get("memory_correctable"),
                "memory_uncorrectable": controller.get("memory_uncorrectable"),
                "host_interface": controller.get("host_interface"),
                "details": {
                    "bios": details.get("bios"),
                    "ctrl_r": details.get("ctrl_r"),
                    "nvdata": details.get("nvdata"),
                    "bootloader": details.get("bootloader"),
                    "onboard_memory": details.get("onboard_memory"),
                    "flash_size": details.get("flash_size"),
                    "nvram_size": details.get("nvram_size"),
                    "cachevault_flash": details.get("cachevault_flash"),
                    "front_end_ports": details.get("front_end_ports"),
                    "backend_ports": details.get("backend_ports"),
                    "onboard_expander": details.get("onboard_expander"),
                    "alarm": details.get("alarm"),
                    "roc_temp_c": details.get("roc_temp_c"),
                    "ctrl_temp_c": details.get("ctrl_temp_c"),
                    "policies": details.get("policies"),
                    "scheduled": details.get("scheduled"),
                },
                "vds": [
                    {k: v for k, v in vd.items() if k not in ("member_dids",)}
                    for vd in controller.get("vds") or []
                ],
                "enclosures": enclosures,
                "expanders": expanders,
                "direct_disks": direct,
                "disk_count": len(controller_disks),
            }
        )
    return {
        "controllers": items,
        "enclosures": enclosure_total,
        "expanders": expander_total,
        "disks": len(disks),
    }


class Monitor:
    """后台采集线程 + 快照缓存 + 健康变化事件。"""

    def __init__(self) -> None:
        self.interval = REFRESH_INTERVAL
        self.snapshot: dict[str, Any] | None = None
        self.collecting = True
        self.last_error: str | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=800)
        self._last_health: dict[str, str] = {}
        self._collect_lock = threading.Lock()
        self._wake = threading.Event()
        self._started = threading.Event()
        self._load_events()
        self._load_snapshot()

    # ---------- 持久化 ----------
    def _data_path(self, name: str) -> str:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
        except OSError:
            pass
        return os.path.join(DATA_DIR, name)

    def _load_events(self) -> None:
        path = os.path.join(DATA_DIR, "events.jsonl")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()[-800:]
            for line in lines:
                try:
                    self.events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return

    def _load_snapshot(self) -> None:
        path = os.path.join(DATA_DIR, "last_snapshot.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                snapshot = json.load(fh)
            if isinstance(snapshot, dict) and snapshot.get("disks"):
                self.snapshot = snapshot
                self.collecting = False
                for disk in snapshot["disks"]:
                    self._last_health[disk["id"]] = disk.get("health") or "unknown"
        except (OSError, json.JSONDecodeError):
            return

    def _save_snapshot(self, snapshot: dict[str, Any]) -> None:
        path = self._data_path("last_snapshot.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            return

    def _append_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        path = self._data_path("events.jsonl")
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            return

    # ---------- 采集 ----------
    def start(self) -> None:
        if self._started.is_set():
            return
        self._started.set()
        threading.Thread(target=self._loop, name="collector", daemon=True).start()

    def request_refresh(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while True:
            self.collect_once()
            self._wake.wait(self.interval)
            self._wake.clear()

    def collect_once(self) -> None:
        if not self._collect_lock.acquire(blocking=False):
            return
        self.collecting = True
        try:
            snapshot, _warnings = collect_all()
            self._record_events(snapshot)
            snap = dict(snapshot)
            snap["events_total"] = len(self.events)
            self.snapshot = snap
            self.last_error = None
            self._save_snapshot(snap)
            try:
                history.record_snapshot(snap)
            except Exception:
                pass
        except Exception as exc:  # pragma: no cover - 防御
            self.last_error = f"采集失败：{exc}"
            if self.snapshot is not None:
                snapshot = dict(self.snapshot)
                snapshot["collect_error"] = self.last_error
                self.snapshot = snapshot
        finally:
            self.collecting = False
            self._collect_lock.release()

    def _record_events(self, snapshot: dict[str, Any]) -> None:
        current = {d["id"]: d for d in snapshot.get("disks") or []}
        for disk_id, disk in current.items():
            new = disk.get("health") or "unknown"
            old = self._last_health.get(disk_id)
            if old is not None and old != new:
                self._append_event(
                    {
                        "ts": snapshot["generated_at"],
                        "disk_id": disk_id,
                        "controller": disk.get("controller_name"),
                        "bay": disk.get("bay_label") or disk.get("bay"),
                        "model": disk.get("model"),
                        "serial": disk.get("serial"),
                        "from": old,
                        "to": new,
                        "message": "；".join(disk.get("reasons") or []),
                    }
                )
            self._last_health[disk_id] = new

    # ---------- 查询 ----------
    def get_snapshot(self) -> dict[str, Any] | None:
        return self.snapshot

    def status(self) -> dict[str, Any]:
        return {
            "collecting": self.collecting,
            "last_error": self.last_error,
            "generated_at": (self.snapshot or {}).get("generated_at"),
            "interval": self.interval,
            "events_total": len(self.events),
        }
