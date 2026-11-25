"""MegaRAID / PERC 控制器采集（perccli64 JSON 输出，含背板 / SES / 扩展器）。"""

from __future__ import annotations

import os
from typing import Any

from util import extract_json, natural_key, normalize_text, run, strip, to_int

PERCCLI = os.environ.get("PERCCLI_PATH") or "/usr/local/bin/perccli64"

STATE_LABELS = {
    "Onln": "在线",
    "Online": "在线",
    "Optl": "正常",
    "Dgrd": "降级",
    "Pdgd": "部分降级",
    "Rbld": "重建中",
    "Cpyb": "回拷中",
    "Offln": "离线",
    "Failed": "故障",
    "Missng": "丢失",
    "Msng": "丢失",
    "UGood": "未配置（正常）",
    "UBad": "未配置（故障）",
    "JBOD": "直通模式",
    "Good": "正常",
    "Hotsp": "热备",
    "DHS": "专用热备",
    "GHS": "全局热备",
    "Spn": "热备",
    "Rebuild": "重建中",
    "Degraded": "降级",
    "Optimal": "正常",
    "Foreign": "外部配置",
}

HEALTH_OK_STATES = {"Onln", "Online", "Optl", "Optimal", "JBOD", "UGood", "Good", "DHS", "GHS"}
HEALTH_WARN_STATES = {"Rbld", "Rebuild", "Cpyb", "Dgrd", "Pdgd", "Degraded", "Hotsp", "Spn"}
HEALTH_BAD_STATES = {"Offln", "Failed", "Msng", "Missng", "UBad", "Foreign"}


class PercError(RuntimeError):
    pass


def available() -> bool:
    return os.access(PERCCLI, os.X_OK)


def _json(args: list[str], timeout: int = 60) -> dict[str, Any]:
    rc, out, err = run([PERCCLI, *args], timeout=timeout)
    payload = extract_json(out)
    if payload is None:
        detail = strip(err) or strip(out)[-200:] or "无输出"
        raise PercError(f"perccli {' '.join(args)} 失败：{detail}")
    return payload


def _response(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    controllers = payload.get("Controllers") or []
    if not controllers:
        raise PercError("perccli 返回中没有控制器信息")
    entry = controllers[0]
    return entry.get("Response Data") or {}, entry.get("Command Status") or {}


def _ok(status: dict[str, Any]) -> bool:
    return strip(status.get("Status")).lower() == "success"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _value(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if data.get(key) not in (None, ""):
            return strip(data.get(key))
    return ""


def enumerate_controllers(log) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (控制器列表, 警告)。支持多张 MegaRAID / PERC 卡。"""
    if not available():
        log("未找到 perccli64，跳过 MegaRAID 采集")
        return [], ["未找到 perccli64，RAID 卡数据不可用"]

    try:
        payload = _json(["show", "J"])
        data, _status = _response(payload)
    except PercError as exc:
        log(str(exc))
        return [], [str(exc)]

    count = to_int(data.get("Number of Controllers")) or 0
    overview = {to_int(item.get("Ctl")): item for item in (data.get("System Overview") or []) if isinstance(item, dict)}
    if count <= 0:
        return [], ["服务器未检测到 MegaRAID / PERC 控制器"]

    controllers: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index in range(count):
        try:
            try:
                info, _ = _response(_json([f"/c{index}", "show", "all", "J"]))
            except PercError:
                info, _ = _response(_json([f"/c{index}", "show", "J"]))
            try:
                enclosures, _ = _response(_json([f"/c{index}/eall", "show", "all", "J"]))
            except PercError:
                enclosures = {}
            pds, pds_status = _response(_json([f"/c{index}/eall/sall", "show", "all", "J"], timeout=90))
            vds, vds_status = _response(_json([f"/c{index}/vall", "show", "all", "J"], timeout=90))
            try:
                locate_data, _ = _response(_json([f"/c{index}", "show", "activityforlocate", "J"], timeout=60))
            except PercError:
                locate_data = None
        except PercError as exc:
            warnings.append(str(exc))
            log(str(exc))
            continue
        if not _ok(pds_status):
            warnings.append(f"控制器 {index} 物理盘列表读取失败：{strip(pds_status.get('Description'))}")
        if not _ok(vds_status):
            warnings.append(f"控制器 {index} 虚拟盘列表读取失败：{strip(vds_status.get('Description'))}")
        controllers.append(_parse_controller(index, info, pds, vds, overview.get(index, {}), enclosures, locate_data))
    return controllers, warnings


def _pci_from_basics(text: str) -> str:
    parts = text.strip().split(":")
    if len(parts) == 4:
        domain, bus, dev, fn = parts
        try:
            fn = format(int(fn, 16), "x")
        except ValueError:
            fn = fn.lstrip("0") or "0"
        return f"{domain.zfill(4)}:{bus}:{dev}.{fn}"
    return ""


def _controller_details(info: dict[str, Any]) -> dict[str, Any]:
    basics = _as_dict(info.get("Basics"))
    version = _as_dict(info.get("Version"))
    bus = _as_dict(info.get("Bus"))
    status = _as_dict(info.get("Status"))
    hw = _as_dict(info.get("HwCfg"))
    policies = _as_dict(info.get("Policies"))
    caps = _as_dict(info.get("Capabilities"))
    scheduled = _as_dict(info.get("Scheduled Tasks"))
    bbu = (_as_list(info.get("BBU_Info")) or [{}])[0]
    bbu = bbu if isinstance(bbu, dict) else {}
    roc_temp = to_int(hw.get("ROC temperature(Degree Celsius)"))
    ctrl_temp = to_int(hw.get("Ctrl temperature(Degree Celsius)"))
    return {
        "basics": basics,
        "version": version,
        "bus": bus,
        "bios": _value(version, "Bios Version") or _value(info, "BIOS Version"),
        "ctrl_r": _value(version, "Ctrl-R Version"),
        "nvdata": _value(version, "NVDATA Version"),
        "boot_block": _value(version, "Boot Block Version"),
        "bootloader": _value(version, "Bootloader Version"),
        "controller_status": _value(status, "Controller Status"),
        "memory_correctable": to_int(status.get("Memory Correctable Errors")),
        "memory_uncorrectable": to_int(status.get("Memory Uncorrectable Errors")),
        "ecc_bucket": to_int(status.get("ECC Bucket Count")),
        "bbu_status": _value(status, "BBU Status"),
        "cache_preserved": _value(status, "Any Offline VD Cache Preserved"),
        "safe_mode": _value(status, "Controller has booted into safe mode"),
        "host_interface": _value(bus, "Host Interface") or _value(info, "Host Interface"),
        "device_interface": _value(bus, "Device Interface") or _value(info, "Device Interface"),
        "domain_id": to_int(bus.get("Domain ID")),
        "front_end_ports": to_int(hw.get("Front End Port Count")),
        "backend_ports": to_int(hw.get("Backend Port Count")),
        "onboard_memory": _value(hw, "On Board Memory Size"),
        "flash_size": _value(hw, "Flash Size"),
        "nvram_size": _value(hw, "NVRAM Size"),
        "cachevault_flash": _value(hw, "CacheVault Flash Size"),
        "onboard_expander": _value(hw, "On Board Expander"),
        "tpm": _value(hw, "TPM"),
        "alarm": _value(hw, "Alarm"),
        "roc_temp_c": roc_temp,
        "ctrl_temp_c": ctrl_temp,
        "temperature_c": roc_temp if roc_temp is not None else ctrl_temp,
        "bbu": {
            "model": _value(bbu, "Model"),
            "state": _value(bbu, "State"),
            "temperature_c": to_int(strip(bbu.get("Temp")).split("C")[0]),
            "retention": _value(bbu, "RetentionTime"),
            "mode": _value(bbu, "Mode"),
        }
        if bbu
        else {},
        "auto_rebuild": _value(policies, "Auto Rebuild"),
        "battery_warning": _value(policies, "Battery Warning"),
        "coercion_mode": _value(policies, "Drive Coercion Mode"),
        "raid_levels": _value(caps, "RAID Level Supported"),
        "supported_drives": _value(caps, "Supported Drives"),
        "max_arrays": to_int(caps.get("Max Arrays")),
        "max_vds": to_int(caps.get("Max Number of VDs")),
        "scheduled": {
            "next_cc": _value(scheduled, "Next Consistency check launch"),
            "next_patrol": _value(scheduled, "Next Patrol Read launch"),
            "cc_reoccurrence": _value(scheduled, "Consistency Check Reoccurrence"),
            "patrol_reoccurrence": _value(scheduled, "Patrol Read Reoccurrence"),
        },
        "policies": {k: strip(v) for k, v in policies.items() if k in (
            "Auto Rebuild", "Battery Warning", "Drive Coercion Mode", "Auto detect BackPlane",
            "Expose Enclosure Devices", "Maintain PD Fail History", "Load Balance Mode",
        ) and v not in (None, "")},
    }


def _parse_enclosures(data: dict[str, Any], cid: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip().lower().startswith("enclosure"):
            continue
        block = _as_dict(value)
        info = _as_dict(block.get("Information"))
        inquiry = _as_dict(block.get("Inquiry Data"))
        props = (_as_list(block.get("Properties")) or [{}])[0]
        props = props if isinstance(props, dict) else {}
        eid = to_int(info.get("Device ID"))
        product = _value(inquiry, "Product Identification") or _value(props, "ProdID")
        vendor = _value(inquiry, "Vendor Identification")
        revision = _value(inquiry, "Product Revision Level")
        device_type = _value(info, "Device Type")
        enc_type = _value(info, "Enclosure Type")
        status = _value(info, "Status")
        health = "ok" if status.lower() in ("ok", "optimal") else "warning" if status else "unknown"
        result.append(
            {
                "id": f"{cid}/e{eid}" if eid is not None else key.strip(),
                "eid": eid,
                "kind": "backplane",
                "kind_label": "背板 / SES 扩展器",
                "device_type": device_type,
                "enclosure_type": enc_type,
                "status": status,
                "health": health,
                "vendor": vendor,
                "product": product,
                "revision": revision,
                "fru": _value(info, "FRU Part Number"),
                "serial": _value(info, "Enclosure Serial Number"),
                "zoning": _value(info, "Enclosure Zoning Mode"),
                "partner": _value(info, "Partner Device ID"),
                "position": _value(info, "Position"),
                "connector": _value(info, "Connector Name") or _value(props, "Port#"),
                "slots": to_int(props.get("Slots")),
                "drives": to_int(props.get("PD")),
                "power_supplies": to_int(props.get("PS")),
                "fans": to_int(props.get("Fans")),
                "temperature_sensors": to_int(props.get("TSs")),
                "alarms": to_int(props.get("Alms")),
                "sim": to_int(props.get("SIM")),
                "port": _value(props, "Port#"),
                "expander": "EXP" in product.upper() or enc_type.upper() == "SES",
                "ses": enc_type.upper() == "SES",
                "drive_ids": [],
            }
        )
    return result


def _progress_text(*sources: dict[str, Any]) -> str:
    for source in sources:
        for key, value in source.items():
            if "progress" in str(key).lower() and not isinstance(value, (dict, list)):
                text = strip(value)
                if text and text not in ("0", "0%", "0.0%"):
                    return text
    return ""


def _locate_states(data: dict[str, Any]) -> dict[str, str]:
    """从 activityforlocate 响应中提取 EID:Slt / DID 的定位灯状态。"""
    result: dict[str, str] = {}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            eid_slot = _value(obj, "EID:Slt", "EID:Slot")
            did = obj.get("DID")
            state = _value(obj, "Locate", "State", "Activity")
            if eid_slot:
                result[eid_slot] = state or "On"
            if did is not None:
                result[str(did)] = state or "On"
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(data)
    return result


def _parse_controller(
    index: int,
    info: dict[str, Any],
    pds: dict[str, Any],
    vds: dict[str, Any],
    overview: dict[str, Any],
    enclosures: dict[str, Any] | None = None,
    locate_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cid = f"c{index}"
    details = _controller_details(info)
    basics = details["basics"]
    bus = details["bus"]
    bus_no = to_int(bus.get("Bus Number")) if bus else None
    dev_no = to_int(bus.get("Device Number")) if bus else None
    fn_no = to_int(bus.get("Function Number")) if bus else None
    pci = f"0000:{bus_no:02x}:{dev_no:02x}.{fn_no}" if None not in (bus_no, dev_no, fn_no) else _pci_from_basics(
        _value(info, "PCI Address") or _value(basics, "PCI Address")
    )
    model = _value(basics, "Model") or _value(info, "Product Name") or "MegaRAID"
    controller: dict[str, Any] = {
        "id": cid,
        "index": index,
        "kind": "raid",
        "kind_label": "硬件 RAID 卡",
        "model": normalize_text(model),
        "serial": normalize_text(_value(basics, "Serial Number") or _value(info, "Serial Number")),
        "firmware": normalize_text(details["version"].get("Firmware Version") or _value(info, "FW Version")),
        "firmware_package": normalize_text(details["version"].get("Firmware Package Build") or _value(info, "FW Package Build")),
        "bios": normalize_text(details["bios"]),
        "driver": normalize_text(_value(details["version"], "Driver Name") or _value(info, "Driver Name")),
        "driver_version": normalize_text(_value(details["version"], "Driver Version") or _value(info, "Driver Version")),
        "pci": pci,
        "sas_address": _value(basics, "SAS Address") or _value(info, "SAS Address"),
        "host_interface": details["host_interface"],
        "device_interface": details["device_interface"],
        "temperature_c": details["temperature_c"],
        "bbu": details["bbu"],
        "memory_correctable": details["memory_correctable"],
        "memory_uncorrectable": details["memory_uncorrectable"],
        "controller_status": details["controller_status"],
        "ports": to_int(overview.get("Ports")),
        "health": _overview_health(overview),
        "vds": [],
        "pds": [],
        "enclosures": _parse_enclosures(enclosures or {}, cid),
        "details": details,
        "smart_bus": index,
        "warnings": [],
    }
    controller["enclosure_count"] = len(controller["enclosures"])
    controller["expander_count"] = sum(1 for e in controller["enclosures"] if e.get("expander"))

    # 虚拟磁盘
    vd_keys = [key for key in vds if key.startswith(f"/{cid}/v") and key.count("/") == 2]
    for key in sorted(vd_keys, key=natural_key):
        items = _as_list(vds.get(key))
        if not items or not isinstance(items[0], dict):
            continue
        item = items[0]
        vd_index = to_int(key.rsplit("v", 1)[-1])
        props = _as_dict(vds.get(f"VD{vd_index} Properties"))
        members = _as_list(vds.get(f"PDs for VD {vd_index}"))
        controller["vds"].append(
            {
                "id": f"{cid}/v{vd_index}",
                "index": vd_index,
                "dg": to_int(item.get("DG/VD")),
                "level": strip(item.get("TYPE")),
                "state": strip(item.get("State")),
                "state_label": STATE_LABELS.get(strip(item.get("State")), strip(item.get("State"))),
                "size": strip(item.get("Size")),
                "name": strip(item.get("Name")),
                "os_device": strip(props.get("OS Drive Name")),
                "strip_size": strip(props.get("Strip Size")),
                "exposed": strip(props.get("Exposed to OS")),
                "consistency": strip(item.get("Consist")),
                "members": [strip(m.get("EID:Slt")) for m in members if isinstance(m, dict)],
                "member_dids": [to_int(m.get("DID")) for m in members if isinstance(m, dict)],
                "health": _vd_health(strip(item.get("State"))),
                "rebuild_progress": _progress_text(item, props),
                "scsi_naa": strip(props.get("SCSI NAA Id")),
                "span_depth": to_int(props.get("Span Depth")),
                "drives_per_span": to_int(props.get("Number of Drives Per Span")),
                "creation": f"{strip(props.get('Creation Date'))} {strip(props.get('Creation Time'))}".strip(),
            }
        )

    vd_by_did: dict[int, dict[str, Any]] = {}
    for vd in controller["vds"]:
        for did in vd["member_dids"]:
            if did is not None:
                vd_by_did[did] = vd

    enclosure_by_eid = {item.get("eid"): item for item in controller["enclosures"]}
    locate_map = _locate_states(locate_data or {})
    locate_available = locate_data is not None

    # 物理磁盘
    drive_keys = [
        key
        for key in pds
        if key.startswith(f"Drive /{cid}/e")
        and "Detailed Information" not in key
        and key.count("/") == 3
    ]
    for key in drive_keys:
        items = _as_list(pds.get(key))
        if not items or not isinstance(items[0], dict):
            continue
        item = items[0]
        detail = _as_dict(pds.get(key + " - Detailed Information"))
        state_block = _as_dict(detail.get(f"{key} State"))
        attrs = _as_dict(detail.get(f"{key} Device attributes"))
        policies = _as_dict(detail.get(f"{key} Policies/Settings"))
        eid_slot = strip(item.get("EID:Slt"))
        eid, _, slot = eid_slot.partition(":")
        did = to_int(item.get("DID"))
        vd = vd_by_did.get(did) if did is not None else None
        model = normalize_text(item.get("Model")) or normalize_text(attrs.get("Model Number"))
        smart_alert = _value(state_block, "S.M.A.R.T alert flagged by drive").lower() in ("yes", "true", "1")
        ports = [
            p for p in _as_list(policies.get("Port Information")) if isinstance(p, dict)
        ]
        port0 = ports[0] if ports else {}
        enclosure = enclosure_by_eid.get(to_int(eid))
        disk_id = f"{cid}.e{eid}.s{slot}"
        controller["pds"].append(
            {
                "id": disk_id,
                "did": did,
                "eid": eid,
                "slot": slot,
                "bay": eid_slot,
                "bay_label": f"EID {eid} · 槽位 {slot}",
                "state": strip(item.get("State")),
                "state_label": STATE_LABELS.get(strip(item.get("State")), strip(item.get("State"))),
                "size": strip(item.get("Size")),
                "interface": strip(item.get("Intf")),
                "media": strip(item.get("Med")),
                "model": model,
                "manufacturer": normalize_text(attrs.get("Manufacturer Id")),
                "serial": normalize_text(attrs.get("SN")),
                "firmware": normalize_text(attrs.get("Firmware Revision")),
                "wwn": normalize_text(attrs.get("WWN")),
                "sas_address": normalize_text(port0.get("SAS address")),
                "temperature_c": _temperature(strip(state_block.get("Drive Temperature"))),
                "media_errors": to_int(state_block.get("Media Error Count")),
                "other_errors": to_int(state_block.get("Other Error Count")),
                "predictive_failures": to_int(state_block.get("Predictive Failure Count")),
                "last_predictive_event": to_int(policies.get("Last Predictive Failure Event Sequence Number")),
                "smart_alert": smart_alert,
                "dg": to_int(item.get("DG")),
                "vd_id": vd["id"] if vd else None,
                "vd_level": vd["level"] if vd else "",
                "rebuild_progress": vd.get("rebuild_progress", "") if vd else "",
                "locate": (
                    locate_map.get(eid_slot)
                    or (locate_map.get(str(did)) if did is not None else "")
                    or ("Off" if locate_available else "")
                ),
                "link_speed": normalize_text(attrs.get("Link Speed")),
                "device_speed": normalize_text(attrs.get("Device Speed")),
                "connector": normalize_text(attrs.get("Connector Name")),
                "position": strip(policies.get("Drive position")),
                "enclosure_position": strip(policies.get("Enclosure position")),
                "connected_port": strip(policies.get("Connected Port Number")),
                "port_status": strip(port0.get("Status")),
                "port_linkspeed": strip(port0.get("Linkspeed")),
                "multipath": strip(policies.get("Multipath")),
                "sector_size": strip(attrs.get("Logical Sector Size")),
                "physical_sector_size": strip(attrs.get("Physical Sector Size")),
                "raw_size": strip(attrs.get("Raw size")),
                "coerced_size": strip(attrs.get("Coerced size")),
                "non_coerced_size": strip(attrs.get("Non Coerced size")),
                "write_cache": strip(attrs.get("Write Cache")),
                "certified": strip(policies.get("Certified")),
                "sed_capable": strip(policies.get("SED Capable")) or strip(item.get("SED")),
                "sed_enabled": strip(policies.get("SED Enabled")),
                "shield_counter": to_int(state_block.get("Shield Counter")),
                "enclosure_id": enclosure["id"] if enclosure else (f"{cid}/e{eid}" if eid else ""),
                "enclosure_product": enclosure.get("product", "") if enclosure else "",
                "enclosure_device_type": enclosure.get("device_type", "") if enclosure else "",
                "expander_id": (enclosure["id"] if enclosure and enclosure.get("expander") else ""),
            }
        )
        if enclosure and disk_id not in enclosure["drive_ids"]:
            enclosure["drive_ids"].append(disk_id)

    controller["pds"].sort(key=lambda d: natural_key(d["bay"]))
    for enclosure in controller["enclosures"]:
        enclosure["drive_count"] = len(enclosure["drive_ids"])
    return controller


def _temperature(text: str) -> int | None:
    return to_int(text.split("C")[0]) if "C" in text else to_int(text)


def _overview_health(overview: dict[str, Any]) -> str:
    health = strip(overview.get("Hlth")).lower()
    if health in ("opt", "optimal", "ok"):
        return "ok"
    if health in ("warn", "warning", "nd"):
        return "warning"
    if health in ("crit", "critical", "dgd", "degraded"):
        return "critical"
    return "unknown"


def _vd_health(state: str) -> str:
    if state in ("Optl", "Optimal", "Good"):
        return "ok"
    if state in HEALTH_BAD_STATES:
        return "critical"
    if state in HEALTH_WARN_STATES:
        return "warning"
    return "unknown"
