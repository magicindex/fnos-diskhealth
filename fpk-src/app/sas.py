"""Linux SAS sysfs 拓扑：HBA → 扩展器 / 背板 → PHY → 端设备 → 磁盘。

数据来自 /sys/class/sas_*，只读；用于补全 HBA / JBOD 直通盘的背板、
扩展器、SES 槽位、SAS 地址和链路速率信息。
"""

from __future__ import annotations

import glob
import os
import re
from typing import Any

from util import normalize_text, read_text, strip, to_int

SYS = "/sys/class"


def _list(class_name: str) -> list[str]:
    try:
        return sorted(os.listdir(os.path.join(SYS, class_name)))
    except OSError:
        return []


def _real(class_name: str, name: str) -> str:
    return os.path.realpath(os.path.join(SYS, class_name, name))


def _attr(path: str) -> str:
    return normalize_text(read_text(path))


def _host_id(real: str) -> int | None:
    match = re.search(r"/host(\d+)/", real)
    return to_int(match.group(1)) if match else None


def _expander_name(real: str) -> str:
    match = re.search(r"/(expander-[^/]+)/", real)
    return match.group(1) if match else ""


def _blocks(real: str) -> list[str]:
    found: list[str] = []
    for pattern in (os.path.join(real, "target*", "*", "block", "*"), os.path.join(real, "target*", "block", "*")):
        for path in glob.glob(pattern):
            name = os.path.basename(path)
            if name and name not in found:
                found.append(name)
    return found


def _link_rate(text: str) -> str:
    value = strip(text)
    if not value:
        return ""
    if value.lower().endswith("gbit"):
        return f"{value[:-4].strip()}Gb/s"
    return value


def _sas_device_attr(name: str, key: str) -> str:
    return _attr(os.path.join(SYS, "sas_device", name, key))


def _phy_info(name: str) -> dict[str, Any]:
    path = os.path.join(SYS, "sas_phy", name)
    return {
        "id": name,
        "sas_address": _attr(os.path.join(path, "sas_address")),
        "phy_identifier": to_int(_attr(os.path.join(path, "phy_identifier"))),
        "negotiated_linkrate": _link_rate(_attr(os.path.join(path, "negotiated_linkrate"))),
        "maximum_linkrate": _link_rate(_attr(os.path.join(path, "maximum_linkrate"))),
        "minimum_linkrate": _link_rate(_attr(os.path.join(path, "minimum_linkrate"))),
        "invalid_dword_count": to_int(_attr(os.path.join(path, "invalid_dword_count"))),
        "loss_of_dword_sync_count": to_int(_attr(os.path.join(path, "loss_of_dword_sync_count"))),
        "phy_reset_problem_count": to_int(_attr(os.path.join(path, "phy_reset_problem_count"))),
        "running_disparity_error_count": to_int(_attr(os.path.join(path, "running_disparity_error_count"))),
        "initiator_protocols": _attr(os.path.join(path, "initiator_port_protocols")),
        "target_protocols": _attr(os.path.join(path, "target_port_protocols")),
    }


def collect() -> dict[str, Any]:
    """返回 expanders / end_devices / enclosures / hosts。"""
    expanders: list[dict[str, Any]] = []
    expander_by_id: dict[str, dict[str, Any]] = {}
    for name in _list("sas_expander"):
        class_path = os.path.join(SYS, "sas_expander", name)
        real = os.path.realpath(os.path.join(class_path, "device"))
        address = _sas_device_attr(name, "sas_address")
        exp = {
            "id": name,
            "kind": "expander",
            "kind_label": "SAS 扩展器 / 背板",
            "host": _host_id(real),
            "sas_address": address,
            "component_id": to_int(_attr(os.path.join(SYS, "sas_expander", name, "component_id"))),
            "component_vendor": _attr(os.path.join(SYS, "sas_expander", name, "component_vendor_id")),
            "component_revision": _attr(os.path.join(SYS, "sas_expander", name, "component_revision_id")),
            "vendor": _attr(os.path.join(SYS, "sas_expander", name, "vendor_id")),
            "product": _attr(os.path.join(SYS, "sas_expander", name, "product_id")),
            "revision": _attr(os.path.join(SYS, "sas_expander", name, "product_rev")),
            "level": to_int(_attr(os.path.join(SYS, "sas_expander", name, "level"))),
            "status": "OK",
            "ses": True,
            "phys": [],
            "end_devices": [],
            "enclosure_id": "",
        }
        # 挂在该扩展器下的 PHY
        for phy_name in _list("sas_phy"):
            phy_real = _real("sas_phy", phy_name)
            if f"/{name}/" in phy_real:
                exp["phys"].append(_phy_info(phy_name))
        expanders.append(exp)
        expander_by_id[name] = exp

    end_devices: list[dict[str, Any]] = []
    for name in _list("sas_end_device"):
        class_path = os.path.join(SYS, "sas_end_device", name)
        real = os.path.realpath(os.path.join(class_path, "device"))
        address = _sas_device_attr(name, "sas_address")
        enclosure = _sas_device_attr(name, "enclosure_identifier")
        bay = to_int(_sas_device_attr(name, "bay_identifier"))
        phy_id = to_int(_sas_device_attr(name, "phy_identifier"))
        exp_name = _expander_name(real)
        exp = expander_by_id.get(exp_name)
        link = ""
        if exp:
            for phy in exp["phys"]:
                if phy.get("phy_identifier") == phy_id:
                    link = phy.get("negotiated_linkrate") or ""
                    break
        blocks = _blocks(real)
        device = {
            "id": name,
            "sas_address": address,
            "enclosure_identifier": enclosure,
            "bay_identifier": bay,
            "phy_identifier": phy_id,
            "scsi_target_id": to_int(_sas_device_attr(name, "scsi_target_id")),
            "target_protocols": _sas_device_attr(name, "target_port_protocols"),
            "host": _host_id(real),
            "expander_id": exp_name,
            "expander_product": exp.get("product", "") if exp else "",
            "expander_vendor": exp.get("vendor", "") if exp else "",
            "link_rate": link,
            "blocks": blocks,
            "device_name": blocks[0] if blocks else "",
        }
        end_devices.append(device)
        if exp:
            exp["end_devices"].append(device["id"])

    # 按 SES enclosure_identifier 聚合成背板 / JBOD 外壳
    enclosures: list[dict[str, Any]] = []
    by_enclosure: dict[str, dict[str, Any]] = {}
    for device in end_devices:
        if not device.get("blocks"):
            continue
        enclosure_id = device.get("enclosure_identifier") or ""
        if not enclosure_id:
            continue
        exp = expander_by_id.get(device.get("expander_id", ""))
        key = enclosure_id
        enclosure = by_enclosure.get(key)
        if enclosure is None:
            short = re.sub(r"[^0-9a-fA-F]", "", enclosure_id)[-8:] or "ses"
            enclosure = {
                "id": f"ses-{short}",
                "kind": "enclosure",
                "kind_label": "SES 背板 / JBOD",
                "enclosure_identifier": enclosure_id,
                "vendor": exp.get("vendor", "") if exp else "",
                "product": exp.get("product", "") if exp else "",
                "revision": exp.get("revision", "") if exp else "",
                "expander_id": exp.get("id", "") if exp else "",
                "host": device.get("host"),
                "status": "OK",
                "health": "ok",
                "ses": True,
                "slots": 0,
                "drives": 0,
                "bays": [],
                "drive_ids": [],
            }
            by_enclosure[key] = enclosure
            enclosures.append(enclosure)
        bay = device.get("bay_identifier")
        if bay is not None and bay not in enclosure["bays"]:
            enclosure["bays"].append(bay)
        enclosure["slots"] = max(enclosure["slots"], (max(enclosure["bays"]) + 1) if enclosure["bays"] else 0)
        enclosure["drives"] = len(enclosure["bays"])
        if exp:
            exp["enclosure_id"] = enclosure["id"]

    # 标准 enclosure class（部分 SAS 背板提供）
    for name in _list("enclosure"):
        real = _real("enclosure", name)
        components = []
        for slot in _list(os.path.join("enclosure", name, "components")):
            path = os.path.join(SYS, "enclosure", name, "components", slot)
            components.append(
                {
                    "slot": to_int(_attr(os.path.join(path, "slot"))) or to_int(slot),
                    "status": _attr(os.path.join(path, "status")),
                    "component": _attr(os.path.join(path, "component")),
                }
            )
        if not components:
            continue
        enclosures.append(
            {
                "id": name,
                "kind": "enclosure",
                "kind_label": "SES 背板",
                "vendor": _attr(os.path.join(real, "vendor")),
                "product": _attr(os.path.join(real, "model")),
                "revision": _attr(os.path.join(real, "firmware")),
                "status": "OK",
                "ses": True,
                "slots": len(components),
                "drives": sum(1 for c in components if c.get("status", "").lower() in ("ok", "active")),
                "components": components,
                "host": _host_id(real),
                "drive_ids": [],
            }
        )

    hosts: dict[int, dict[str, Any]] = {}
    for name in _list("sas_phy"):
        if not re.match(r"^phy-\d+:\d+$", name):
            continue
        real = _real("sas_phy", name)
        host = _host_id(real)
        if host is None or host in hosts:
            continue
        hosts[host] = {
            "host": host,
            "sas_address": _attr(os.path.join(SYS, "sas_phy", name, "sas_address")),
            "link_rate": _link_rate(_attr(os.path.join(SYS, "sas_phy", name, "negotiated_linkrate"))),
        }

    return {
        "expanders": expanders,
        "end_devices": end_devices,
        "enclosures": enclosures,
        "hosts": hosts,
    }
