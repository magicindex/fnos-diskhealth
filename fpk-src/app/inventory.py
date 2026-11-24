"""直连磁盘（SAS HBA / 主板 SATA / NVMe）枚举。"""

from __future__ import annotations

import os
import re
from typing import Any

import sas
from util import extract_json, natural_key, normalize_text, read_text, run, strip, to_int

PCI_CHIPS = {
    "1000:0072": "LSI SAS2008",
    "1000:0070": "LSI SAS1068",
    "1000:0097": "LSI SAS3008",
    "1000:00e6": "LSI SAS3808",
    "1000:00e7": "LSI SAS3908",
    "1000:00aa": "LSI SAS3408",
    "1000:00af": "LSI SAS3508",
    "1000:00e5": "LSI SAS3608",
}


def _scsi_hosts() -> dict[int, dict[str, Any]]:
    hosts: dict[int, dict[str, Any]] = {}
    base = "/sys/class/scsi_host"
    try:
        names = os.listdir(base)
    except OSError:
        return hosts
    for name in names:
        if not name.startswith("host"):
            continue
        try:
            index = int(name[4:])
        except ValueError:
            continue
        path = os.path.join(base, name)
        proc_name = strip(read_text(os.path.join(path, "proc_name")))
        device_link = os.path.realpath(os.path.join(path, "device"))
        pci = os.path.basename(os.path.dirname(device_link))
        hosts[index] = {"index": index, "proc_name": proc_name, "pci": pci, "path": path}
    return hosts


def _pci_chip(pci: str) -> str:
    if not pci.startswith("0000:"):
        return ""
    vendor = strip(read_text(f"/sys/bus/pci/devices/{pci}/vendor")).replace("0x", "")
    device = strip(read_text(f"/sys/bus/pci/devices/{pci}/device")).replace("0x", "")
    key = f"{vendor}:{device}"
    return PCI_CHIPS.get(key, "")


def _clean(value: Any) -> str:
    """统一 sysfs / lsblk 文本编码并做 NFC 归一化。"""
    return normalize_text(value)


def _vpd_serial(path: str) -> str:
    """从 VPD page 0x80 原始字节中提取序列号，避免把二进制当文本。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read(255)
    except OSError:
        return ""
    if len(data) > 4 and data[1] == 0x80:
        length = data[3]
        text = data[4 : 4 + length].decode("ascii", "ignore")
        text = _clean(text)
        if text:
            return text
    printable = "".join(chr(byte) if 32 <= byte < 127 else " " for byte in data)
    candidates = [part for part in printable.split() if len(part) >= 4]
    return candidates[0] if candidates else ""


def _ata_port(realpath: str) -> str:
    match = re.search(r"/ata(\d+)/", realpath)
    return match.group(1) if match else ""


def collect(log) -> tuple[list[dict[str, Any]], list[str]]:
    """返回 (控制器列表, 警告列表)。每个控制器内包含 disks。"""
    rc, out, err = run(
        ["lsblk", "-J", "-b", "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,HCTL"],
        timeout=20,
    )
    payload = extract_json(out)
    if rc != 0 or not payload:
        detail = strip(err) or "lsblk 输出无法解析"
        log(f"直连磁盘枚举失败：{detail}")
        return [], [f"直连磁盘枚举失败：{detail}"]

    hosts = _scsi_hosts()
    sas_data = sas.collect()
    controllers: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []

    for device in payload.get("blockdevices") or []:
        if strip(device.get("type")) != "disk":
            continue
        name = strip(device.get("name"))
        hctl = strip(device.get("hctl"))
        host_index = None
        target = None
        if hctl:
            parts = hctl.split(":")
            if len(parts) >= 4:
                host_index = to_int(parts[0])
                target = to_int(parts[2])
        host = hosts.get(host_index) if host_index is not None else None
        proc_name = strip((host or {}).get("proc_name"))
        transport = strip(device.get("tran"))

        if proc_name == "megaraid_sas":
            continue  # 虚拟磁盘，由 RAID 采集提供
        if transport in ("usb",) or proc_name == "usb-storage":
            continue

        realpath = os.path.realpath(f"/sys/block/{name}/device")
        if name.startswith("nvme"):
            cid = "nvme"
            controller = controllers.setdefault(
                cid,
                {
                    "id": cid,
                    "kind": "direct",
                    "kind_label": "NVMe 直连",
                    "model": "NVMe 控制器",
                    "pci": "",
                    "disks": [],
                },
            )
            bay = "M.2"
            bay_label = "M.2 插槽"
            interface = "NVMe"
            role = "系统盘 / 直连"
            device_type = "nvme"
            chip = ""
        elif proc_name in ("mpt2sas", "mpt3sas"):
            cid = f"hba{host_index}"
            chip = _pci_chip(strip(host.get("pci")))
            controller = controllers.setdefault(
                cid,
                {
                    "id": cid,
                    "kind": "hba",
                    "kind_label": "SAS 直通卡（HBA）",
                    "model": (f"{chip} 直通卡" if chip else "SAS HBA 直通卡"),
                    "driver": proc_name,
                    "pci": strip(host.get("pci")),
                    "disks": [],
                },
            )
            channel = target if target is not None else 0
            bay = f"CH{channel}"
            bay_label = f"直连通道 CH{channel}"
            interface = "SAS"
            role = "直通盘"
            device_type = None
        elif proc_name in ("ahci", "ata_piix", "sata_sil24"):
            cid = "sata"
            port = _ata_port(realpath)
            controller = controllers.setdefault(
                cid,
                {
                    "id": cid,
                    "kind": "direct",
                    "kind_label": "主板 SATA",
                    "model": "主板 SATA 控制器",
                    "driver": proc_name,
                    "pci": "",
                    "disks": [],
                },
            )
            bay = f"SATA{port}" if port else f"SATA{host_index}"
            bay_label = f"SATA 端口 {port}" if port else f"SATA 通道 {host_index}"
            interface = "SATA"
            role = "系统盘 / 直连"
            device_type = "sat"
        else:
            if transport == "sas":
                cid = f"hba{host_index}"
                controller = controllers.setdefault(
                    cid,
                    {
                        "id": cid,
                        "kind": "hba",
                        "kind_label": "SAS 直通卡（HBA）",
                        "model": "SAS HBA 直通卡",
                        "driver": proc_name,
                        "pci": strip((host or {}).get("pci")),
                        "disks": [],
                    },
                )
                bay = f"CH{target or 0}"
                bay_label = f"直连通道 CH{target or 0}"
                interface = "SAS"
                role = "直通盘"
                device_type = "scsi"
            else:
                continue

        model = _clean(device.get("model"))
        if not model:
            model = _clean(read_text(f"/sys/block/{name}/device/model"))
        serial = _clean(device.get("serial"))
        if not serial:
            serial = _vpd_serial(f"/sys/block/{name}/device/vpd_pg80") or _clean(
                read_text(f"/sys/block/{name}/device/serial")
            )
        size = to_int(device.get("size")) or 0
        rota = to_int(device.get("rota"))
        media = "HDD" if rota == 1 else "SSD"
        firmware = _clean(read_text(f"/sys/block/{name}/device/rev"))
        if not firmware:
            firmware = _clean(read_text(f"/sys/block/{name}/device/firmware_rev"))

        disk = {
            "id": f"{cid}.{bay}",
            "did": None,
            "eid": "",
            "slot": bay,
            "bay": bay,
            "bay_label": bay_label,
            "device": f"/dev/{name}",
            "device_name": name,
            "state": "Online",
            "state_label": "在线",
            "size": _fmt_size(size),
            "size_bytes": size,
            "interface": interface,
            "media": media,
            "model": model,
            "serial": serial,
            "firmware": firmware,
            "wwn": "",
            "temperature_c": None,
            "media_errors": None,
            "other_errors": None,
            "predictive_failures": None,
            "smart_alert": False,
            "dg": None,
            "vd_id": None,
            "vd_level": "",
            "link_speed": "",
            "role": role,
            "device_type": device_type,
        }
        controller["disks"].append(disk)

    end_by_device = {
        device.get("device_name"): device
        for device in sas_data.get("end_devices") or []
        if device.get("device_name")
    }
    expander_by_host: dict[int, list[dict[str, Any]]] = {}
    for expander in sas_data.get("expanders") or []:
        host = expander.get("host")
        if host is None:
            continue
        summary = {
            key: expander.get(key)
            for key in (
                "id", "kind", "kind_label", "host", "sas_address", "component_id",
                "component_vendor", "component_revision", "vendor", "product",
                "revision", "level", "status", "ses", "enclosure_id",
            )
        }
        phys = expander.get("phys") or []
        summary["phy_count"] = len(phys)
        summary["link_rates"] = sorted({p.get("negotiated_linkrate") for p in phys if p.get("negotiated_linkrate") and p.get("negotiated_linkrate") != "Unknown"})
        summary["invalid_dword_count"] = sum((p.get("invalid_dword_count") or 0) for p in phys)
        summary["loss_of_dword_sync_count"] = sum((p.get("loss_of_dword_sync_count") or 0) for p in phys)
        summary["phy_reset_problem_count"] = sum((p.get("phy_reset_problem_count") or 0) for p in phys)
        summary["running_disparity_error_count"] = sum((p.get("running_disparity_error_count") or 0) for p in phys)
        expander_by_host.setdefault(host, []).append(summary)
    enclosure_by_host: dict[int, list[dict[str, Any]]] = {}
    for enclosure in sas_data.get("enclosures") or []:
        host = enclosure.get("host")
        if host is None:
            continue
        enclosure["drive_ids"] = []
        enclosure["bays"] = sorted(enclosure.get("bays") or [])
        enclosure_by_host.setdefault(host, []).append(enclosure)

    for controller in controllers.values():
        controller["disks"].sort(key=lambda d: natural_key(d["bay"]))
        host_match = re.match(r"^hba(\d+)$", controller["id"])
        if host_match:
            host_index = int(host_match.group(1))
            host_info = sas_data.get("hosts", {}).get(host_index) or {}
            controller["sas_address"] = host_info.get("sas_address", "")
            controller["expanders"] = expander_by_host.get(host_index, [])
            controller["enclosures"] = enclosure_by_host.get(host_index, [])
            controller["expander_count"] = len(controller["expanders"])
            controller["enclosure_count"] = len(controller["enclosures"])
            if controller["expanders"] or controller["enclosures"]:
                controller["kind_label"] = "SAS HBA / JBOD"
            for disk in controller["disks"]:
                end = end_by_device.get(disk.get("device_name"))
                if not end:
                    continue
                disk["sas_address"] = end.get("sas_address", "")
                disk["ses_enclosure"] = end.get("enclosure_identifier", "")
                disk["ses_bay"] = end.get("bay_identifier")
                disk["expander_id"] = end.get("expander_id", "")
                disk["expander_product"] = end.get("expander_product", "")
                disk["expander_vendor"] = end.get("expander_vendor", "")
                disk["phy_id"] = end.get("phy_identifier")
                disk["link_rate"] = end.get("link_rate", "")
                disk["end_device_id"] = end.get("id", "")
                if not disk.get("link_speed") and disk.get("link_rate"):
                    disk["link_speed"] = disk["link_rate"]
                physically = end.get("expander_product") or "SES"
                bay = end.get("bay_identifier")
                if bay is not None:
                    disk["bay"] = f"{physically}:{bay}"
                    disk["bay_label"] = f"{physically} · 槽位 {bay}"
                for enclosure in controller["enclosures"]:
                    if enclosure.get("expander_id") == end.get("expander_id") and disk["id"] not in enclosure["drive_ids"]:
                        enclosure["drive_ids"].append(disk["id"])
        else:
            controller.setdefault("expanders", [])
            controller.setdefault("enclosures", [])
            controller["expander_count"] = 0
            controller["enclosure_count"] = 0
        controller["disks"].sort(key=lambda d: natural_key(d["bay"]))
        controllers[controller["id"]] = controller

    order = {"raid": 0, "hba": 1, "direct": 2}
    result = sorted(controllers.values(), key=lambda c: (order.get(c["kind"], 9), natural_key(c["id"])))
    for controller in result:
        for enclosure in controller.get("enclosures") or []:
            enclosure["drive_count"] = len(enclosure.get("drive_ids") or [])
        if not controller["disks"]:
            warnings.append(f"{controller['model']} 未发现可用磁盘")
    return result, warnings


def _fmt_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1000 or unit == "PB":
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1000
    return f"{value:.2f} PB"
