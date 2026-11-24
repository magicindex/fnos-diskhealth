"""日志导出：CSV / JSON / TXT。"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from util import iso_now, now, strip

HEALTH_ORDER = {"critical": 0, "warning": 1, "unknown": 2, "ok": 3}


def _match(disk: dict[str, Any], controller: str, bay: str, status: str, query: str) -> bool:
    if controller and disk.get("controller_id") != controller:
        return False
    if bay and disk.get("bay") != bay:
        return False
    if status:
        wanted = {part.strip() for part in status.split(",") if part.strip()}
        if wanted and disk.get("health") not in wanted:
            return False
    if query:
        haystack = " ".join(
            strip(disk.get(key))
            for key in ("model", "serial", "bay", "bay_label", "device", "controller_name", "role")
        ).lower()
        if query.lower() not in haystack:
            return False
    return True


def collect_disks(
    snapshot: dict[str, Any],
    controller: str = "",
    bay: str = "",
    status: str = "",
    query: str = "",
) -> list[dict[str, Any]]:
    disks = [d for d in snapshot.get("disks") or [] if _match(d, controller, bay, status, query)]
    disks.sort(key=lambda d: (HEALTH_ORDER.get(d.get("health") or "unknown", 9), d.get("controller_id") or ""))
    return disks


def _metric(disk: dict[str, Any], key: str) -> Any:
    return (disk.get("smart_metrics") or {}).get(key)


def _filename(fmt: str) -> str:
    stamp = now().strftime("%Y%m%d-%H%M%S")
    ext = {"csv": "csv", "json": "json", "txt": "txt"}.get(fmt, "txt")
    return f"disk-health_{stamp}.{ext}"


def export(
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    fmt: str,
    controller: str = "",
    bay: str = "",
    status: str = "",
    query: str = "",
) -> tuple[bytes, str, str]:
    """返回 (内容, MIME, 文件名)。"""
    fmt = fmt.lower()
    filters = {"controller": controller, "bay": bay, "health": status, "q": query}
    disks = collect_disks(snapshot, controller, bay, status, query)
    if fmt == "json":
        payload = {
            "exported_at": iso_now(),
            "filters": {k: v for k, v in filters.items() if v},
            "host": snapshot.get("host"),
            "generated_at": snapshot.get("generated_at"),
            "summary": _summary(disks),
            "controllers": snapshot.get("controllers") or [],
            "disks": disks,
            "warnings": snapshot.get("warnings") or [],
            "events": events,
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return body, "application/json; charset=utf-8", _filename("json")
    if fmt == "csv":
        return _csv(disks, snapshot), "text/csv; charset=utf-8", _filename("csv")
    return _txt(disks, snapshot, events), "text/plain; charset=utf-8", _filename("txt")


def _summary(disks: list[dict[str, Any]]) -> dict[str, int]:
    out = {"total": len(disks), "ok": 0, "warning": 0, "critical": 0, "unknown": 0}
    for disk in disks:
        key = disk.get("health") if disk.get("health") in out else "unknown"
        out[key] += 1
    return out


COLUMNS = [
    "控制器",
    "类型",
    "盘位",
    "状态",
    "健康",
    "型号",
    "序列号",
    "容量",
    "接口",
    "介质",
    "固件",
    "温度(°C)",
    "通电小时",
    "介质错误",
    "其他错误",
    "预测故障",
    "SMART告警",
    "重映射扇区",
    "待映射扇区",
    "不可修正扇区",
    "增长缺陷",
    "RAID组",
    "虚拟磁盘",
    "角色",
    "告警说明",
    "采集时间",
]


def _csv(disks: list[dict[str, Any]], snapshot: dict[str, Any]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    generated = snapshot.get("generated_at") or ""
    for disk in disks:
        writer.writerow(
            [
                disk.get("controller_name") or "",
                disk.get("kind_label") or ("硬件 RAID 卡" if disk.get("kind") == "raid" else "直连"),
                disk.get("bay_label") or disk.get("bay") or "",
                disk.get("state_label") or disk.get("state") or "",
                disk.get("health_label") or "",
                disk.get("model") or "",
                disk.get("serial") or "",
                disk.get("size") or "",
                disk.get("interface") or "",
                disk.get("media") or "",
                disk.get("firmware") or "",
                disk.get("temperature_c") if disk.get("temperature_c") is not None else "",
                disk.get("power_on_hours") if disk.get("power_on_hours") is not None else "",
                disk.get("media_errors") if disk.get("media_errors") is not None else "",
                disk.get("other_errors") if disk.get("other_errors") is not None else "",
                disk.get("predictive_failures") if disk.get("predictive_failures") is not None else "",
                "是" if disk.get("smart_alert") else "否",
                _metric(disk, "reallocated") if _metric(disk, "reallocated") is not None else "",
                _metric(disk, "pending") if _metric(disk, "pending") is not None else "",
                _metric(disk, "offline_uncorrectable") if _metric(disk, "offline_uncorrectable") is not None else "",
                _metric(disk, "grown_defects") if _metric(disk, "grown_defects") is not None else "",
                disk.get("dg") if disk.get("dg") is not None else "",
                disk.get("vd_id") or "",
                disk.get("role") or "",
                "；".join(disk.get("reasons") or []),
                generated,
            ]
        )
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


def _txt(disks: list[dict[str, Any]], snapshot: dict[str, Any], events: list[dict[str, Any]]) -> bytes:
    lines: list[str] = []
    summary = _summary(disks)
    lines.append("=" * 78)
    lines.append("  RAID 阵列健康 / HARDWARE RAID・ARRAY MONITOR 日志")
    lines.append("=" * 78)
    lines.append(f"导出时间 : {iso_now()}")
    lines.append(f"采集时间 : {snapshot.get('generated_at') or '-'}")
    lines.append(f"主机名   : {snapshot.get('host') or '-'}")
    lines.append(
        "统计     : 共 {total} 块 | 正常 {ok} | 警告 {warning} | 严重 {critical} | 未知 {unknown}".format(**summary)
    )
    lines.append("")

    lines.append("-" * 78)
    lines.append("控制器与虚拟磁盘")
    lines.append("-" * 78)
    for controller in snapshot.get("controllers") or []:
        lines.append(
            f"[{controller.get('id')}] {controller.get('model') or '-'}  ({controller.get('kind_label') or '-'})"
        )
        details = []
        if controller.get("serial"):
            details.append(f"序列号 {controller['serial']}")
        if controller.get("firmware"):
            details.append(f"固件 {controller['firmware']}")
        if controller.get("pci"):
            details.append(f"PCI {controller['pci']}")
        if controller.get("driver"):
            details.append(f"驱动 {controller['driver']} {controller.get('driver_version') or ''}".strip())
        if details:
            lines.append("    " + " | ".join(details))
        for vd in controller.get("vds") or []:
            lines.append(
                f"    VD{vd.get('index')} {vd.get('level') or ''} {vd.get('size') or ''} "
                f"{vd.get('state_label') or ''} {vd.get('os_device') or ''} "
                f"成员 {', '.join(vd.get('members') or []) or '-'}"
            )
    lines.append("")

    lines.append("-" * 78)
    lines.append("物理硬盘明细")
    lines.append("-" * 78)
    for disk in disks:
        lines.append(
            f"[{disk.get('health_label') or '未知'}] {disk.get('controller_name') or '-'} / "
            f"{disk.get('bay_label') or disk.get('bay') or '-'} / {disk.get('model') or '-'}"
        )
        lines.append(
            f"    状态 {disk.get('state_label') or '-'} | 容量 {disk.get('size') or '-'} | "
            f"接口 {disk.get('interface') or '-'} | 介质 {disk.get('media') or '-'} | "
            f"固件 {disk.get('firmware') or '-'}"
        )
        lines.append(
            f"    序列号 {disk.get('serial') or '-'} | 温度 "
            f"{str(disk.get('temperature_c')) + '°C' if disk.get('temperature_c') is not None else '-'} | "
            f"通电 {str(disk.get('power_on_hours')) + ' h' if disk.get('power_on_hours') is not None else '-'}"
        )
        lines.append(
            f"    介质错误 {_val(disk.get('media_errors'))} | 其他错误 {_val(disk.get('other_errors'))} | "
            f"预测故障 {_val(disk.get('predictive_failures'))} | SMART告警 "
            f"{'是' if disk.get('smart_alert') else '否'}"
        )
        lines.append(f"    角色 {disk.get('role') or '-'} | 采集时间 {snapshot.get('generated_at') or '-'}")
        lines.append(f"    结论 {'；'.join(disk.get('reasons') or []) or '-'}")

        smart_info = disk.get("smart") or {}
        if smart_info.get("attributes"):
            lines.append("    SMART 属性:")
            lines.append("      ID  属性名                        当前 最差 阈值 原始值")
            for attr in smart_info["attributes"]:
                lines.append(
                    f"      {str(attr.get('id')):<3} {str(attr.get('name') or '')[:28]:<28} "
                    f"{str(attr.get('value')):>4} {str(attr.get('worst')):>4} {str(attr.get('thresh')):>4} "
                    f"{attr.get('raw_string') or attr.get('raw')}"
                )
        scsi = smart_info.get("scsi") or {}
        if scsi:
            lines.append(
                "    SAS 计数: 增长缺陷 {} | 读不可修正 {} | 写不可修正 {} | 非介质错误 {}".format(
                    _val(scsi.get("grown_defects")),
                    _val((scsi.get("read") or {}).get("uncorrected")),
                    _val((scsi.get("write") or {}).get("uncorrected")),
                    _val(scsi.get("non_medium_errors")),
                )
            )
        nvme = smart_info.get("nvme") or {}
        if nvme:
            lines.append(
                "    NVMe: 寿命已用 {}% | 可用备用 {}% | 介质错误 {} | 不安全关机 {}".format(
                    _val(nvme.get("percentage_used")),
                    _val(nvme.get("available_spare")),
                    _val(nvme.get("media_errors")),
                    _val(nvme.get("unsafe_shutdowns")),
                )
            )
        if smart_info.get("error"):
            lines.append(f"    SMART 提示: {smart_info.get('error')}")
        lines.append("")

    if snapshot.get("warnings"):
        lines.append("-" * 78)
        lines.append("采集警告")
        lines.append("-" * 78)
        for warning in snapshot["warnings"]:
            lines.append(f"  ! {warning}")
        lines.append("")

    if events:
        lines.append("-" * 78)
        lines.append(f"健康变化事件（最近 {min(len(events), 200)} 条）")
        lines.append("-" * 78)
        for event in events[-200:]:
            lines.append(
                f"  {event.get('ts')}  {event.get('controller')} / {event.get('bay')} / "
                f"{event.get('model')}  {event.get('from')} -> {event.get('to')}  "
                f"{event.get('message') or ''}"
            )
        lines.append("")

    return ("\n".join(lines) + "\n").encode("utf-8")


def _val(value: Any) -> str:
    return "-" if value is None else str(value)
