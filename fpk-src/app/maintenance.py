"""只读 RAID 维护检查：重建进度、外部配置、定位灯状态。

本模块只调用 perccli 的 show 子命令，不包含 set / add / delete / import / start
等写操作。结果由管理员接口读取并写入审计日志。
"""

from __future__ import annotations

import time
from typing import Any

import raid
from util import strip, to_int

READ_ONLY_COMMANDS = (
    ["show", "J"],
    ["/c{index}/vall", "show", "all", "J"],
    ["/c{index}/eall/sall", "show", "all", "J"],
    ["/c{index}/fall", "show", "J"],
)

REBUILD_STATES = {"Rbld", "Rebuild", "Cpyb", "Copyback"}


def _lookup_lower(data: dict[str, Any], *names: str) -> Any:
    wanted = {name.lower() for name in names}
    for key, value in data.items():
        if str(key).lower() in wanted:
            return value
    return None


def _find_value(obj: Any, needles: tuple[str, ...], depth: int = 0) -> str:
    if depth > 5:
        return ""
    if isinstance(obj, dict):
        for key, value in obj.items():
            name = str(key).lower()
            if any(needle in name for needle in needles) and not isinstance(value, (dict, list)):
                text = strip(value)
                if text:
                    return text
        for value in obj.values():
            found = _find_value(value, needles, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_value(value, needles, depth + 1)
            if found:
                return found
    return ""


def _progress(item: dict[str, Any], props: dict[str, Any]) -> str:
    for source in (item, props):
        value = _find_value(source, ("rebuild progress", "rebuildpercent", "progress"))
        if value:
            return value
    return ""


def _locate(item: dict[str, Any], detail: dict[str, Any]) -> str:
    for source in (item, detail):
        value = _find_value(source, ("locate",))
        if value:
            return value
    return ""


def _walk_foreign(obj: Any, path: str = "") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}".lower()
            if isinstance(value, list):
                if "foreign" in child:
                    found.extend(item for item in value if isinstance(item, dict))
                else:
                    for item in value:
                        found.extend(_walk_foreign(item, child))
            else:
                found.extend(_walk_foreign(value, child))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_foreign(item, path))
    return found


def _controller_index(controller: dict[str, Any]) -> int | None:
    bus = controller.get("smart_bus")
    if isinstance(bus, int):
        return bus
    digits = "".join(ch for ch in str(controller.get("id") or "") if ch.isdigit())
    return int(digits) if digits else None


def _foreign_disk(item: dict[str, Any]) -> dict[str, Any]:
    bay = strip(_lookup_lower(item, "EID:Slt", "EID:Slt", "Slot", "Bay"))
    return {
        "bay": bay,
        "state": strip(_lookup_lower(item, "State")),
        "model": strip(_lookup_lower(item, "Model")),
        "serial": strip(_lookup_lower(item, "SN", "Serial")),
        "size": strip(_lookup_lower(item, "Size")),
    }


def collect(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回只读维护信息；任何 perccli 错误只记录为 warning。"""
    snapshot = snapshot or {}
    warnings: list[str] = []
    rebuilds: list[dict[str, Any]] = []
    foreign: list[dict[str, Any]] = []
    locate: list[dict[str, Any]] = []
    controllers: list[dict[str, Any]] = []

    if not raid.available():
        return {
            "read_only": True,
            "generated_ts": int(time.time()),
            "rebuilds": [],
            "foreign": [],
            "locate": [],
            "warnings": ["未找到 perccli64，无法读取 RAID 维护状态"],
            "controllers": [],
        }

    for controller in snapshot.get("controllers") or []:
        if controller.get("kind") != "raid":
            continue
        index = _controller_index(controller)
        if index is None:
            continue
        cid = str(controller.get("id") or f"c{index}")
        controller_name = controller.get("model") or cid
        item = {
            "id": cid,
            "name": controller_name,
            "rebuilds": 0,
            "foreign": 0,
            "locate_on": 0,
        }
        try:
            vds, _ = raid._response(raid._json([f"/c{index}/vall", "show", "all", "J"], timeout=90))
            pds, _ = raid._response(raid._json([f"/c{index}/eall/sall", "show", "all", "J"], timeout=90))
            foreign_data, _ = raid._response(raid._json([f"/c{index}/fall", "show", "J"], timeout=60))
        except Exception as exc:
            warnings.append(f"{cid}: 只读维护检查失败：{exc}")
            controllers.append(item)
            continue

        # Virtual drive rebuild / copyback progress
        vd_items: dict[str, dict[str, Any]] = {}
        for key in [k for k in vds if k.startswith(f"/{cid}/v") and k.count("/") == 2]:
            values = raid._as_list(vds.get(key))
            if values and isinstance(values[0], dict):
                vd_items[key] = values[0]
        for key, raw_vd in vd_items.items():
            vd_index = to_int(str(key).rsplit("v", 1)[-1])
            props = raid._as_dict(vds.get(f"VD{vd_index} Properties"))
            state = strip(raw_vd.get("State"))
            progress = _progress(raw_vd, props)
            if state not in REBUILD_STATES:
                continue
            members = raid._as_list(vds.get(f"PDs for VD {vd_index}"))
            bays = [strip(m.get("EID:Slt")) for m in members if isinstance(m, dict)]
            rebuilds.append({
                "controller_id": cid,
                "controller": controller_name,
                "vd_id": f"{cid}/v{vd_index}" if vd_index is not None else key,
                "level": strip(raw_vd.get("TYPE")),
                "state": state,
                "state_label": raid.STATE_LABELS.get(state, state or "重建中"),
                "progress": progress,
                "disks": bays,
            })
            item["rebuilds"] += 1

        # Physical drive states: rebuild without a mapped VD, and locate LED state
        for key in [k for k in pds if k.startswith(f"Drive /{cid}/e") and "Detailed Information" not in k and k.count("/") == 3]:
            values = raid._as_list(pds.get(key))
            if not values or not isinstance(values[0], dict):
                continue
            raw_pd = values[0]
            detail = raid._as_dict(pds.get(key + " - Detailed Information"))
            bay = strip(raw_pd.get("EID:Slt")) or key.rsplit("/", 1)[-1]
            state = strip(raw_pd.get("State"))
            if state in REBUILD_STATES:
                mapped = any(bay in entry["disks"] for entry in rebuilds if entry["controller_id"] == cid)
                if not mapped:
                    rebuilds.append({
                        "controller_id": cid,
                        "controller": controller_name,
                        "vd_id": "",
                        "level": "",
                        "state": state,
                        "state_label": raid.STATE_LABELS.get(state, state),
                        "progress": _progress(raw_pd, detail),
                        "disks": [bay],
                    })
                    item["rebuilds"] += 1
            locate_state = _locate(raw_pd, detail)
            if locate_state:
                locate.append({
                    "controller_id": cid,
                    "controller": controller_name,
                    "bay": bay,
                    "model": strip(raw_pd.get("Model")),
                    "state": locate_state,
                })
                if locate_state.lower() in ("on", "yes", "1", "true"):
                    item["locate_on"] += 1

        # Foreign configuration (read-only detection)
        for raw in _walk_foreign(foreign_data):
            disk = _foreign_disk(raw)
            if not disk["bay"] and not disk["serial"] and not disk["model"]:
                continue
            disk["controller_id"] = cid
            disk["controller"] = controller_name
            foreign.append(disk)
            item["foreign"] += 1

        controllers.append(item)

    # Fallback when raw foreign output is unavailable: snapshot PD Foreign state
    if not foreign:
        for disk in snapshot.get("disks") or []:
            if str(disk.get("state") or "") == "Foreign":
                foreign.append({
                    "controller_id": disk.get("controller_id") or "",
                    "controller": disk.get("controller_name") or "",
                    "bay": disk.get("bay_label") or disk.get("bay") or "",
                    "state": "Foreign",
                    "model": disk.get("model") or "",
                    "serial": disk.get("serial") or "",
                    "size": disk.get("size") or "",
                })

    return {
        "read_only": True,
        "generated_ts": int(time.time()),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "rebuilds": rebuilds,
        "foreign": foreign,
        "locate": locate,
        "warnings": warnings,
        "controllers": controllers,
        "writes_enabled": False,
    }
