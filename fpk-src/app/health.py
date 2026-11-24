"""健康评级：把控制器状态、SMART 指标归一为 正常/警告/严重/未知。"""

from __future__ import annotations

from typing import Any

from raid import HEALTH_BAD_STATES, HEALTH_WARN_STATES

LEVELS = {"ok": 0, "info": 0, "unknown": 1, "warning": 2, "critical": 3}

LEVEL_LABELS = {
    "ok": "正常",
    "warning": "警告",
    "critical": "严重",
    "unknown": "未知",
}

TEMP_LIMITS = {
    "HDD": (50, 58),
    "SSD": (65, 75),
    "NVME": (70, 80),
}


def _temperature_limits(media: str, interface: str) -> tuple[int, int]:
    if interface.upper().startswith("NVME"):
        return TEMP_LIMITS["NVME"]
    if (media or "").upper().startswith("SSD"):
        return TEMP_LIMITS["SSD"]
    return TEMP_LIMITS["HDD"]


def evaluate(disk: dict[str, Any]) -> dict[str, Any]:
    """返回 {health, health_label, reasons}。"""
    reasons: list[str] = []
    level = "ok"

    def raise_level(target: str) -> None:
        nonlocal level
        if LEVELS[target] > LEVELS[level]:
            level = target

    def crit(text: str) -> None:
        reasons.append(text)
        raise_level("critical")

    def warn(text: str) -> None:
        reasons.append(text)
        raise_level("warning")

    def info(text: str) -> None:
        reasons.append(text)

    state = str(disk.get("state") or "")
    if disk.get("kind") == "raid":
        if state in HEALTH_BAD_STATES:
            crit(f"RAID 卡状态：{disk.get('state_label') or state}")
        elif state in HEALTH_WARN_STATES:
            warn(f"RAID 卡状态：{disk.get('state_label') or state}")

    if disk.get("smart_alert"):
        crit("RAID 卡标记 SMART 告警")

    predictive = disk.get("predictive_failures")
    if predictive:
        crit(f"预测性故障计数 {predictive}")

    media_errors = disk.get("media_errors")
    if media_errors:
        crit(f"介质错误 {media_errors} 次")

    other_errors = disk.get("other_errors")
    if other_errors:
        # MegaRAID 的 Other Error Count 会被 SMART 直通查询累加，
        # 不作为健康告警，只作为提示展示，避免误报。
        info(
            f"非介质错误计数 {other_errors}：通常由 SMART 直通轮询 / SAS 链路累计，"
            "不代表介质故障，不计入健康告警"
        )

    smart = disk.get("smart") or {}
    if not smart.get("available"):
        if disk.get("kind") == "raid":
            info(smart.get("error") or "SMART 暂不可用")
        else:
            raise_level("unknown")
            reasons.append(smart.get("error") or "SMART 不可用")
    else:
        if smart.get("passed") is False:
            crit("SMART 自检未通过")
        metrics = disk.get("smart_metrics") or {}
        pending = metrics.get("pending")
        offline = metrics.get("offline_uncorrectable")
        reallocated = metrics.get("reallocated")
        grown = metrics.get("grown_defects")
        if pending:
            warn(f"待映射扇区 {pending}")
        if offline:
            warn(f"不可修正扇区 {offline}")
        if reallocated:
            warn(f"已重映射扇区 {reallocated}")
        if grown:
            warn(f"增长缺陷 {grown}")
        read_unc = metrics.get("read_uncorrected")
        write_unc = metrics.get("write_uncorrected")
        if read_unc:
            warn(f"读不可修正错误 {read_unc}")
        if write_unc:
            warn(f"写不可修正错误 {write_unc}")

        for attr in smart.get("attributes") or []:
            if attr.get("when_failed") not in (None, "", "-"):
                warn(f"SMART 属性 {attr.get('name')} 曾低于阈值")
                break

        nvme = smart.get("nvme") or {}
        if nvme:
            if nvme.get("critical_warning"):
                warn("NVMe 关键警告已触发")
            used = nvme.get("percentage_used")
            if used is not None and used >= 90:
                warn(f"NVMe 寿命已使用 {used}%")
            if nvme.get("media_errors"):
                crit(f"NVMe 介质错误 {nvme.get('media_errors')} 次")
            if nvme.get("available_spare") is not None and nvme.get("available_spare") < 10:
                warn(f"NVMe 可用备用块仅 {nvme.get('available_spare')}%")

    temp = disk.get("temperature_c")
    if temp is not None:
        warn_temp, crit_temp = _temperature_limits(str(disk.get("media") or ""), str(disk.get("interface") or ""))
        if temp >= crit_temp:
            crit(f"温度过高 {temp}°C（阈值 {crit_temp}°C）")
        elif temp >= warn_temp:
            warn(f"温度偏高 {temp}°C（阈值 {warn_temp}°C）")

    if level == "ok":
        reasons.insert(0, "运行正常")

    return {
        "health": level,
        "health_label": LEVEL_LABELS.get(level, "未知"),
        "reasons": reasons,
    }


def controller_health(controller: dict[str, Any]) -> str:
    level = "ok"
    for vd in controller.get("vds") or []:
        vd_health = vd.get("health") or "unknown"
        if LEVELS.get(vd_health, 0) > LEVELS.get(level, 0):
            level = vd_health
    for disk in controller.get("disks") or []:
        disk_health = disk.get("health") or "unknown"
        if LEVELS.get(disk_health, 0) > LEVELS.get(level, 0):
            level = disk_health
    return level
