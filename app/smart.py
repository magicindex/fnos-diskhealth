"""smartctl 封装：读取并归一化 SMART 数据。"""

from __future__ import annotations

import os
import shutil
from typing import Any

from util import extract_json, normalize_text, run, strip, to_int

SMARTCTL = os.environ.get("SMARTCTL_PATH") or shutil.which("smartctl") or "/usr/sbin/smartctl"

# ATA 关键属性 ID
ATA_KEYS = {
    5: "reallocated",
    9: "power_on_hours",
    194: "temperature",
    197: "pending",
    198: "offline_uncorrectable",
    199: "udma_crc",
}


def available() -> bool:
    return os.access(SMARTCTL, os.X_OK)


def _parse(rc: int, payload: dict[str, Any] | None, stderr: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "passed": None,
        "power_mode": None,
        "temperature_c": None,
        "power_on_hours": None,
        "rotation_rate": None,
        "self_test": None,
        "attributes": [],
        "scsi": {},
        "sas": {},
        "nvme": {},
        "vendor": "",
        "product": "",
        "revision": "",
        "protocol_name": "",
        "scsi_version": "",
        "form_factor": "",
        "temperature_trip_c": None,
        "interface_speed": "",
        "messages": [],
        "error": None,
        "exit_code": rc,
    }

    if payload is None:
        if rc == 124:
            result["error"] = stderr or "SMART 读取超时"
        else:
            result["error"] = (stderr or "").strip() or "smartctl 未返回可解析的 JSON"
        return result

    result["available"] = True
    result["power_mode"] = payload.get("power_mode")
    device = payload.get("device") or {}
    result["protocol"] = strip(device.get("protocol"))
    result["device_type"] = strip(device.get("type"))
    result["vendor"] = normalize_text(payload.get("scsi_vendor") or payload.get("model_family"))
    result["product"] = normalize_text(payload.get("scsi_product") or payload.get("model_name"))
    result["revision"] = normalize_text(payload.get("scsi_revision") or payload.get("firmware_version"))
    transport = payload.get("scsi_transport_protocol") or {}
    result["protocol_name"] = normalize_text(transport.get("name") or device.get("protocol"))
    result["scsi_version"] = normalize_text(payload.get("scsi_version"))
    form_factor = payload.get("form_factor") or {}
    result["form_factor"] = normalize_text(form_factor.get("name"))
    interface = payload.get("interface_speed") or {}
    result["interface_speed"] = normalize_text(
        (interface.get("current") or {}).get("string") or (interface.get("max") or {}).get("string")
    )

    status = payload.get("smart_status") or {}
    if "passed" in status:
        result["passed"] = bool(status["passed"])

    temperature = payload.get("temperature") or {}
    result["temperature_c"] = to_int(temperature.get("current"))
    result["temperature_trip_c"] = to_int(temperature.get("drive_trip"))

    power_on = payload.get("power_on_time") or {}
    result["power_on_hours"] = to_int(power_on.get("hours"))
    if result["power_on_hours"] is None and power_on.get("minutes") is not None:
        result["power_on_hours"] = int(power_on["minutes"]) // 60

    result["rotation_rate"] = to_int(payload.get("rotation_rate"))

    ata = payload.get("ata_smart_data") or {}
    self_test = (ata.get("self_test") or {}).get("status") or {}
    if self_test.get("string"):
        result["self_test"] = strip(self_test.get("string"))

    for attr in (payload.get("ata_smart_attributes") or {}).get("table", []) or []:
        raw = attr.get("raw") or {}
        result["attributes"].append(
            {
                "id": to_int(attr.get("id")),
                "name": strip(attr.get("name")),
                "value": to_int(attr.get("value")),
                "worst": to_int(attr.get("worst")),
                "thresh": to_int(attr.get("thresh")),
                "when_failed": strip(attr.get("when_failed")) or "-",
                "raw": to_int(raw.get("value")),
                "raw_string": strip(raw.get("string")),
            }
        )

    log = payload.get("scsi_error_counter_log") or {}
    sas: dict[str, Any] = {
        "vendor": result["vendor"],
        "product": result["product"],
        "revision": result["revision"],
        "transport": result["protocol_name"],
        "scsi_version": result["scsi_version"],
        "drive_trip_c": result["temperature_trip_c"],
    }
    for op in ("read", "write", "verify"):
        entry = log.get(op) or {}
        corrected = to_int(entry.get("total_errors_corrected"))
        if corrected is None:
            corrected = sum(
                to_int(entry.get(key)) or 0
                for key in ("errors_corrected_by_eccfast", "errors_corrected_by_eccdelayed", "errors_corrected_by_rereads_rewrites")
            )
        counter = {
            "corrected": corrected,
            "uncorrected": to_int(entry.get("total_uncorrected_errors")) or 0,
            "gigabytes": normalize_text(entry.get("gigabytes_processed")),
        }
        result["scsi"][op] = {"corrected": counter["corrected"], "uncorrected": counter["uncorrected"]}
        sas[op] = counter
    grown = payload.get("scsi_grown_defect_list")
    if grown is not None:
        value = to_int(grown)
        result["scsi"]["grown_defects"] = value
        sas["grown_defects"] = value
    non_medium = payload.get("scsi_nonmedium_error_count")
    if non_medium is not None:
        value = to_int(non_medium)
        result["scsi"]["non_medium_errors"] = value
        sas["non_medium_errors"] = value
    cycles = payload.get("scsi_start_stop_cycle_counter")
    if isinstance(cycles, dict):
        start_stop = to_int(cycles.get("accumulated_start_stop_cycles"))
        load_unload = to_int(cycles.get("accumulated_load_unload_cycles"))
        result["scsi"]["start_stop_cycles"] = start_stop
        sas["start_stop_cycles"] = start_stop
        sas["load_unload_cycles"] = load_unload
        year = normalize_text(cycles.get("year_of_manufacture"))
        week = normalize_text(cycles.get("week_of_manufacture"))
        if year:
            sas["manufactured"] = f"{year}-W{week}" if week else year
    fmt = payload.get("scsi_format_status")
    if isinstance(fmt, dict):
        sas["format"] = {
            "grown_defects": to_int(fmt.get("grown_defects_during_cert")),
            "reassigned": to_int(fmt.get("blocks_reassigned_during_format")),
            "new_blocks": to_int(fmt.get("total_new_block_since_format")),
            "power_on_minutes": to_int(fmt.get("power_on_minutes_since_format")),
        }
    for key in ("scsi_self_test_0", "scsi_self_test_1", "scsi_self_test_2", "scsi_self_test_3"):
        test = payload.get(key)
        if isinstance(test, dict):
            text = normalize_text((test.get("result") or {}).get("string"))
            if text:
                sas["self_test"] = text
                if not result.get("self_test"):
                    result["self_test"] = text
                break
    if payload.get("scsi_transport_protocol") or log:
        result["sas"] = sas

    nvme = payload.get("nvme_smart_health_information_log") or {}
    if nvme:
        result["nvme"] = {
            "critical_warning": to_int(nvme.get("critical_warning")),
            "temperature_c": to_int(nvme.get("temperature")),
            "percentage_used": to_int(nvme.get("percentage_used")),
            "available_spare": to_int(nvme.get("available_spare")),
            "media_errors": to_int(nvme.get("media_errors")),
            "error_log_entries": to_int(nvme.get("num_err_log_entries")),
            "unsafe_shutdowns": to_int(nvme.get("unsafe_shutdowns")),
            "power_cycles": to_int(nvme.get("power_cycles")),
            "data_units_read": to_int(nvme.get("data_units_read")),
            "data_units_written": to_int(nvme.get("data_units_written")),
        }

    for msg in (payload.get("smartctl") or {}).get("messages", []) or []:
        text = strip(msg.get("string"))
        if text:
            result["messages"].append({"severity": strip(msg.get("severity")) or "info", "text": text})

    if result["power_mode"] == "STANDBY":
        result["available"] = False
        result["error"] = "硬盘处于休眠状态，已跳过唤醒"
    elif rc & 0x03 and not payload.get("smart_status") and not result["attributes"]:
        result["error"] = (stderr or "").strip() or "SMART 命令执行失败"

    return result


def read_smart(
    device: str,
    device_type: str | None = None,
    *,
    guard_standby: bool = False,
    timeout: int = 50,
) -> dict[str, Any]:
    """读取一块设备的 SMART。

    guard_standby=True 时使用 -n standby，避免把休眠盘唤醒。
    """
    if not available():
        return {
            "available": False,
            "passed": None,
            "power_mode": None,
            "temperature_c": None,
            "power_on_hours": None,
            "rotation_rate": None,
            "self_test": None,
            "attributes": [],
            "scsi": {},
            "sas": {},
            "nvme": {},
            "vendor": "",
            "product": "",
            "revision": "",
            "protocol_name": "",
            "scsi_version": "",
            "form_factor": "",
            "temperature_trip_c": None,
            "interface_speed": "",
            "messages": [],
            "error": "容器内未找到 smartctl",
            "exit_code": 127,
        }

    cmd = [SMARTCTL, "-a", "-j"]
    if device_type:
        cmd.extend(["-d", device_type])
    if guard_standby:
        cmd.extend(["-n", "standby"])
    cmd.append(device)

    rc, out, err = run(cmd, timeout=timeout)
    return _parse(rc, extract_json(out), err)


def metrics(smart: dict[str, Any]) -> dict[str, int | None]:
    """从归一化 SMART 中提取用于健康判断的关键指标。"""
    out: dict[str, int | None] = {
        "reallocated": None,
        "pending": None,
        "offline_uncorrectable": None,
        "grown_defects": None,
        "read_uncorrected": None,
        "write_uncorrected": None,
    }
    for attr in smart.get("attributes") or []:
        key = ATA_KEYS.get(attr.get("id") or -1)
        if key in ("reallocated", "pending", "offline_uncorrectable"):
            out[key] = attr.get("raw")
    scsi = smart.get("scsi") or {}
    if scsi.get("grown_defects") is not None:
        out["grown_defects"] = scsi.get("grown_defects")
    out["read_uncorrected"] = (scsi.get("read") or {}).get("uncorrected")
    out["write_uncorrected"] = (scsi.get("write") or {}).get("uncorrected")
    return out


def read_raw(
    device: str,
    device_type: str | None = None,
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    """读取原始 smartctl JSON，供详情页展示/排障。"""
    if not available():
        return {"command": "", "exit_code": 127, "json": None, "stderr": "未找到 smartctl"}
    cmd = [SMARTCTL, "-a", "-j"]
    if device_type:
        cmd.extend(["-d", device_type])
    cmd.append(device)
    rc, out, err = run(cmd, timeout=timeout)
    return {
        "command": " ".join(cmd),
        "exit_code": rc,
        "json": extract_json(out),
        "stderr": strip(err),
    }
