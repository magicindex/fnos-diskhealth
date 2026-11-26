"""受二次安全码保护的硬件 RAID 控制台。

安全边界：
- 只接受 fnOS 管理员
- 首次设置 3 组单词安全码，PBKDF2 哈希后保存 0600
- 登录后发放 30 分钟内存 Token
- 所有操作使用白名单 argv，不接受原始 shell 命令
- 每个破坏性操作要求输入精确确认短语
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from typing import Any

import raid
from util import strip

DATA_DIR = os.environ.get("DATA_DIR", "/data")
# FPK 的 @appconf 目录在应用升级/重装时保留；data 目录仅作为 Docker/兼容回退。
CONFIG_DIR = os.environ.get("TRIM_APPCONF") or "/usr/local/apps/@appconf/diskhealth"
LEGACY_SECRET_PATH = os.path.join(DATA_DIR, "console_security.json")
SECRET_PATH = os.path.join(CONFIG_DIR, "console_security.json") if os.path.isdir(CONFIG_DIR) else LEGACY_SECRET_PATH
ITERATIONS = 200_000
TOKEN_TTL = 1800
_lock = threading.Lock()
_tokens: dict[str, dict[str, Any]] = {}
_attempts: dict[str, list[float]] = {}


class ConsoleError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _words(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = re.split(r"[\s,，]+", value.strip())
    else:
        raw = [str(item) for item in (value or [])]
    words = [strip(item).lower() for item in raw if strip(item)]
    if len(words) != 3:
        raise ConsoleError("安全码必须是 3 组单词", 400)
    for word in words:
        if not re.fullmatch(r"[\w\u4e00-\u9fff-]{2,32}", word):
            raise ConsoleError("每个安全词需为 2-32 个字母、数字、下划线或中文", 400)
    return words


def _hash(words: list[str], salt: bytes, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", " ".join(words).encode("utf-8"), salt, iterations)
    return base64.b64encode(digest).decode("ascii")


def _load_secret_file(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("hash") and data.get("salt"):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _read_secret() -> dict[str, Any] | None:
    primary = _load_secret_file(SECRET_PATH)
    if primary:
        return primary
    if LEGACY_SECRET_PATH != SECRET_PATH:
        legacy = _load_secret_file(LEGACY_SECRET_PATH)
        if legacy:
            # 从旧 data 位置迁移到升级后仍保留的 @appconf。
            try:
                os.makedirs(os.path.dirname(SECRET_PATH), exist_ok=True)
                temp = SECRET_PATH + ".tmp"
                with open(temp, "w", encoding="utf-8") as fh:
                    json.dump(legacy, fh)
                os.chmod(temp, 0o600)
                os.replace(temp, SECRET_PATH)
            except OSError:
                pass
            return legacy
    return None


def configured() -> bool:
    return _read_secret() is not None


def setup(words: Any) -> None:
    parsed = _words(words)
    with _lock:
        if _read_secret():
            raise ConsoleError("安全码已设置，不能重复创建", 409)
        salt = os.urandom(16)
        data = {
            "salt": base64.b64encode(salt).decode("ascii"),
            "hash": _hash(parsed, salt, ITERATIONS),
            "iterations": ITERATIONS,
            "created_at": int(time.time()),
        }
        for path in (SECRET_PATH, LEGACY_SECRET_PATH):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                temp = path + ".tmp"
                with open(temp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.chmod(temp, 0o600)
                os.replace(temp, path)
            except OSError:
                if path == SECRET_PATH:
                    raise ConsoleError("安全码保存失败", 500)


def _rate_limit(uid: str) -> None:
    now = time.time()
    attempts = [item for item in _attempts.get(uid, []) if now - item < 600]
    _attempts[uid] = attempts
    if len(attempts) >= 5:
        raise ConsoleError("安全码尝试次数过多，请 10 分钟后再试", 429)


def login(words: Any, user: dict[str, Any] | None) -> str:
    uid = str((user or {}).get("uid") or "")
    _rate_limit(uid)
    parsed = _words(words)
    secret = _read_secret()
    if not secret:
        raise ConsoleError("尚未创建安全码", 409)
    try:
        salt = base64.b64decode(secret["salt"])
        iterations = int(secret.get("iterations") or ITERATIONS)
        expected = str(secret.get("hash") or "")
    except (ValueError, TypeError):
        raise ConsoleError("安全码配置损坏", 500)
    if not secrets.compare_digest(_hash(parsed, salt, iterations), expected):
        _attempts.setdefault(uid, []).append(time.time())
        raise ConsoleError("安全码错误", 403)
    _attempts.pop(uid, None)
    token = secrets.token_urlsafe(32)
    with _lock:
        _tokens[token] = {"uid": uid, "expires": int(time.time()) + TOKEN_TTL}
    return token


def check(token: str, user: dict[str, Any] | None) -> bool:
    token = strip(token)
    if not token:
        return False
    with _lock:
        item = _tokens.get(token)
        if not item:
            return False
        if item["expires"] <= int(time.time()):
            _tokens.pop(token, None)
            return False
        if str(item.get("uid")) != str((user or {}).get("uid")):
            return False
        return True


def logout(token: str) -> None:
    with _lock:
        _tokens.pop(strip(token), None)


def state(token: str, user: dict[str, Any] | None) -> dict[str, Any]:
    return {"configured": configured(), "authenticated": check(token, user), "token_ttl": TOKEN_TTL}


def _size_sectors(value: Any) -> int:
    text = str(value or "")
    match = re.search(r"\[0x([0-9a-fA-F]+)\s+Sectors\]", text)
    if match:
        try:
            return int(match.group(1), 16)
        except ValueError:
            return 0
    return 0


def _vd_disks(snapshot: dict[str, Any], vd_id: str) -> list[dict[str, Any]]:
    return [
        disk for disk in snapshot.get("disks") or []
        if disk.get("kind") == "raid" and disk.get("vd_id") == vd_id
    ]


def _min_member_sectors(disks: list[dict[str, Any]]) -> int:
    sizes = [_size_sectors(disk.get("raw_size")) for disk in disks]
    sizes = [size for size in sizes if size > 0]
    return min(sizes) if sizes else 0


def _disk_free(disk: dict[str, Any]) -> tuple[bool, str]:
    if disk.get("vd_id"):
        return False, "已加入阵列"
    if str(disk.get("state") or "") not in ("UGood", "UG", "Good", "Unconfigured Good"):
        return False, f"状态 {disk.get('state_label') or disk.get('state') or '未知'} 不是 Unconfigured Good"
    return True, ""


def _eligible_new_disks(snapshot: dict[str, Any], ctrl: dict[str, Any], vd: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    level = str(vd.get("level") or "").upper().replace("RAID", "")
    members = _vd_disks(snapshot, str(vd.get("id")))
    min_size = _min_member_sectors(members)
    if level == "10" and len(ids) % 2 != 0:
        raise ConsoleError("RAID10 扩容必须选择偶数块新盘", 400)
    if level in ("50", "60") and len(ids) < 2:
        raise ConsoleError(f"RAID{level} 扩容至少选择 2 块新盘", 400)
    result = []
    for disk_id in ids:
        disk = _pd(snapshot, disk_id)
        if disk.get("controller_id") != ctrl.get("id"):
            raise ConsoleError(f"{disk_id} 不属于所选控制器", 400)
        free, reason = _disk_free(disk)
        if not free:
            raise ConsoleError(f"{disk_id} 不可用于扩容：{reason}", 400)
        size = _size_sectors(disk.get("raw_size"))
        if min_size and size < min_size:
            raise ConsoleError(f"{disk_id} 容量小于现有成员盘，不能加入该阵列", 400)
        result.append(disk)
    return result


def _controller(snapshot: dict[str, Any], controller_id: str) -> dict[str, Any]:
    ctrl = next((c for c in snapshot.get("controllers") or [] if c.get("id") == controller_id), None)
    if not ctrl or ctrl.get("kind") != "raid":
        raise ConsoleError("只有硬件 RAID 控制器支持该操作", 404)
    return ctrl


def _controller_index(ctrl: dict[str, Any]) -> str:
    index = "".join(ch for ch in str(ctrl.get("id") or "") if ch.isdigit())
    if not index:
        raise ConsoleError("控制器编号无效", 400)
    return index


def _vd(snapshot: dict[str, Any], vd_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    ctrl = next((c for c in snapshot.get("controllers") or [] if any(v.get("id") == vd_id for v in c.get("vds") or [])), None)
    if not ctrl or ctrl.get("kind") != "raid":
        raise ConsoleError("未找到硬件 RAID 虚拟盘", 404)
    vd = next((v for v in ctrl.get("vds") or [] if v.get("id") == vd_id), None)
    if not vd:
        raise ConsoleError("未找到虚拟盘", 404)
    return ctrl, vd


def _pd(snapshot: dict[str, Any], disk_id: str) -> dict[str, Any]:
    disk = next((d for d in snapshot.get("disks") or [] if d.get("id") == disk_id), None)
    if not disk or disk.get("kind") != "raid":
        raise ConsoleError("未找到硬件 RAID 物理盘", 404)
    return disk


def run_op(snapshot: dict[str, Any], op: str, payload: dict[str, Any]) -> dict[str, Any]:
    confirm = str(payload.get("confirm") or "").strip()
    op = strip(op)
    if op == "create_vd":
        ctrl = _controller(snapshot, str(payload.get("controller") or ""))
        level = str(payload.get("level") or "").replace("raid", "").replace("RAID", "")
        if level not in ("0", "1", "5", "6", "10", "50", "60"):
            raise ConsoleError("RAID 级别必须是 0/1/5/6/10/50/60", 400)
        drive_ids = [str(item) for item in payload.get("drives") or [] if str(item)]
        if len(drive_ids) < 2:
            raise ConsoleError("至少选择 2 块物理盘", 400)
        drives = []
        for disk_id in drive_ids:
            disk = _pd(snapshot, disk_id)
            if disk.get("controller_id") != ctrl.get("id"):
                raise ConsoleError("物理盘不属于所选控制器", 400)
            drives.append(f"{int(disk['eid'])}:{int(disk['slot'])}")
        if confirm != "CREATE VD":
            raise ConsoleError("确认短语不匹配：CREATE VD", 400)
        index = _controller_index(ctrl)
        argv = [raid.PERCCLI, f"/c{index}", "add", "vd", f"r{level}", f"drives={','.join(drives)}"]
        strip_size = str(payload.get("strip") or "256")
        if strip_size in ("64", "128", "256", "512", "1024"):
            argv.append(f"Strip={strip_size}")
        name = re.sub(r"[^A-Za-z0-9_-]", "", str(payload.get("name") or ""))[:15]
        if name:
            argv.append(f"name={name}")
        argv.append("force")
        command_label = f"create c{index} RAID{level} drives={','.join(drives)}"
    elif op == "delete_vd":
        ctrl, vd = _vd(snapshot, str(payload.get("vd_id") or ""))
        if confirm != f"DELETE {vd.get('id')}":
            raise ConsoleError(f"确认短语不匹配：DELETE {vd.get('id')}", 400)
        argv = [raid.PERCCLI, f"/{vd.get('id')}", "del", "force"]
        command_label = f"delete {vd.get('id')}"
    elif op == "expand_vd":
        ctrl, vd = _vd(snapshot, str(payload.get("vd_id") or ""))
        drive_ids = [str(item) for item in payload.get("drives") or [] if str(item)]
        if not drive_ids:
            raise ConsoleError("没有选择可扩容的新盘；请先选择符合条件的新盘", 400)
        if confirm != f"EXPAND {vd.get('id')}":
            raise ConsoleError(f"确认短语不匹配：EXPAND {vd.get('id')}", 400)
        disks = _eligible_new_disks(snapshot, ctrl, vd, drive_ids)
        drives = [f"{int(disk['eid'])}:{int(disk['slot'])}" for disk in disks]
        level = str(vd.get("level") or "").upper().replace("RAID", "")
        if not level.isdigit():
            raise ConsoleError("无法解析虚拟盘 RAID 级别", 400)
        argv = [raid.PERCCLI, f"/{vd.get('id')}", "start", "migrate", f"type=raid{level}", "option=add", f"drives={','.join(drives)}", "Force"]
        command_label = f"expand {vd.get('id')} add {','.join(drives)}"
    elif op == "init_vd":
        ctrl, vd = _vd(snapshot, str(payload.get("vd_id") or ""))
        full = bool(payload.get("full"))
        if confirm != f"INIT {vd.get('id')}":
            raise ConsoleError(f"确认短语不匹配：INIT {vd.get('id')}", 400)
        argv = [raid.PERCCLI, f"/{vd.get('id')}", "start", "init"]
        if full:
            argv.append("Full")
        argv.append("Force")
        command_label = f"init {vd.get('id')} {'full' if full else 'fast'}"
    elif op == "rebuild":
        disk = _pd(snapshot, str(payload.get("disk_id") or ""))
        ctrl = _controller(snapshot, str(disk.get("controller_id") or ""))
        index = _controller_index(ctrl)
        eid, slot = int(disk["eid"]), int(disk["slot"])
        if confirm != f"REBUILD {eid}:{slot}":
            raise ConsoleError(f"确认短语不匹配：REBUILD {eid}:{slot}", 400)
        argv = [raid.PERCCLI, f"/c{index}/e{eid}/s{slot}", "start", "rebuild"]
        command_label = f"rebuild e{eid}/s{slot}"
    elif op == "import_foreign":
        ctrl = _controller(snapshot, str(payload.get("controller") or ""))
        if confirm != f"IMPORT {ctrl.get('id')}":
            raise ConsoleError(f"确认短语不匹配：IMPORT {ctrl.get('id')}", 400)
        index = _controller_index(ctrl)
        argv = [raid.PERCCLI, f"/c{index}/fall", "import"]
        command_label = f"import foreign {ctrl.get('id')}"
    else:
        raise ConsoleError("不支持的操作", 400)
    rc, out, err = raid.run(argv, timeout=120)
    if rc != 0:
        raise ConsoleError(f"操作失败：{(err or out).strip()[-500:]}", 502)
    return {
        "ok": True,
        "op": op,
        "command": command_label,
        "output": (out or err or "").strip()[-4000:],
    }


def public_inventory(snapshot: dict[str, Any]) -> dict[str, Any]:
    controllers = []
    for ctrl in snapshot.get("controllers") or []:
        if ctrl.get("kind") != "raid":
            continue
        controllers.append({
            "id": ctrl.get("id"),
            "model": ctrl.get("model"),
            "health": ctrl.get("health"),
            "vds": [
                {
                    "id": vd.get("id"),
                    "level": vd.get("level"),
                    "size": vd.get("size"),
                    "state": vd.get("state_label") or vd.get("state"),
                    "health": vd.get("health"),
                    "members": [disk.get("id") for disk in snapshot.get("disks") or [] if disk.get("vd_id") == vd.get("id")],
                    "min_size_sectors": _min_member_sectors(_vd_disks(snapshot, str(vd.get("id")))),
                }
                for vd in ctrl.get("vds") or []
            ],
            "pds": [
                {
                    "id": disk.get("id"),
                    "bay": disk.get("bay_label") or disk.get("bay"),
                    "model": disk.get("model"),
                    "serial": disk.get("serial"),
                    "state": disk.get("state_label") or disk.get("state"),
                    "vd_id": disk.get("vd_id"),
                    "eid": disk.get("eid"),
                    "slot": disk.get("slot"),
                    "size": disk.get("size"),
                    "size_sectors": _size_sectors(disk.get("raw_size")),
                    "free": _disk_free(disk)[0],
                    "free_reason": _disk_free(disk)[1],
                }
                for disk in snapshot.get("disks") or []
                if disk.get("controller_id") == ctrl.get("id") and disk.get("kind") == "raid"
            ],
        })
    return {"controllers": controllers}
