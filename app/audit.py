"""只读操作审计日志：JSONL，按数据目录保存，权限 0600。"""

from __future__ import annotations

import json
import datetime
import re
import os
import threading
import time
from typing import Any

DATA_DIR = os.environ.get("DATA_DIR", "/data")
AUDIT_PATH = os.path.join(DATA_DIR, "audit.jsonl")
NAMES_PATH = os.path.join(DATA_DIR, "user_names.json")
MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()
_names: dict[str, str] = {}
_names_loaded = False


def _generic_name(name: str) -> bool:
    value = str(name or "").strip()
    return not value or bool(re.match(r"^(用户\s*\d+|unknown|未知用户|—|-)$", value, re.I))


def _load_names() -> None:
    global _names, _names_loaded
    if _names_loaded:
        return
    _names_loaded = True
    try:
        with open(NAMES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            _names.update({str(k): str(v) for k, v in data.items() if v})
    except (OSError, ValueError, TypeError):
        pass
    # Backfill from audit history: latest non-generic name per uid wins.
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                uid = str(item.get("uid") or "")
                name = str(item.get("user") or "")
                if uid and not _generic_name(name):
                    _names[uid] = name
    except OSError:
        pass


def _save_names() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        temp = NAMES_PATH + ".tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(_names, fh, ensure_ascii=False)
        os.chmod(temp, 0o600)
        os.replace(temp, NAMES_PATH)
    except OSError:
        pass


def resolve_user(uid: Any, username: str = "") -> str:
    """优先真实 Header 用户名，缺失时按 uid 复用已见用户名。"""
    _load_names()
    uid_text = str(uid or "")
    name = str(username or "").strip()
    if name and not _generic_name(name):
        with _lock:
            if _names.get(uid_text) != name:
                _names[uid_text] = name
                _save_names()
        return name
    return _names.get(uid_text) or (f"用户 {uid_text}" if uid_text else "未知用户")


def _user_fields(user: dict[str, Any] | None) -> tuple[str, str]:
    user = user or {}
    uid = str(user.get("uid") or "")
    raw = str(user.get("name") or user.get("username") or "")
    return uid, resolve_user(uid, raw)


def record(
    user: dict[str, Any] | None,
    action: str,
    *,
    target: str = "",
    result: str = "ok",
    detail: str = "",
) -> None:
    """记录一次操作；写日志失败不影响主流程。"""
    uid, name = _user_fields(user)
    entry = {
        "ts": int(time.time()),
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "uid": uid,
        "user": name,
        "action": action,
        "target": target,
        "result": result,
        "read_only": True,
        "detail": detail[:300],
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with _lock:
            try:
                if os.path.getsize(AUDIT_PATH) > MAX_BYTES:
                    os.replace(AUDIT_PATH, AUDIT_PATH + ".1")
            except OSError:
                pass
            fd = os.open(AUDIT_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError:
        return


def recent(limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(100, int(limit or 20)))
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            item = json.loads(line)
        except (ValueError, TypeError):
            continue
        entries.append(
            {
                "time": item.get("time") or "",
                "user": item.get("user") or "",
                "action": item.get("action") or "",
                "target": item.get("target") or "",
                "result": item.get("result") or "",
                "read_only": bool(item.get("read_only")),
            }
        )
    return entries


def query(
    limit: int = 50,
    offset: int = 0,
    user: str = "",
    action: str = "",
    result: str = "",
    keyword: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict[str, Any]:
    """分页筛选审计记录；仅管理员接口调用。"""
    limit = max(1, min(200, int(limit or 50)))
    offset = max(0, int(offset or 0))
    user = str(user or "").strip().lower()
    action = str(action or "").strip().lower()
    result = str(result or "").strip().lower()
    keyword = str(keyword or "").strip().lower()

    def day_ts(value: str, end: bool = False) -> float | None:
        value = str(value or "").strip()
        if not value:
            return None
        try:
            day = datetime.datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
        if end:
            day += datetime.timedelta(days=1)
        return day.timestamp()

    start = day_ts(date_from)
    end = day_ts(date_to, end=True)

    entries: list[dict[str, Any]] = []
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except (ValueError, TypeError):
                    continue
                entries.append(item)
    except OSError:
        entries = []

    # Resolve legacy entries where the gateway omitted X-Trim-Username.
    _load_names()
    seen_names = dict(_names)
    for item in entries:
        uid = str(item.get("uid") or "")
        current_name = str(item.get("user") or "")
        if uid and not _generic_name(current_name):
            seen_names[uid] = current_name
    for item in entries:
        uid = str(item.get("uid") or "")
        current_name = str(item.get("user") or "")
        if uid and seen_names.get(uid):
            item["user"] = seen_names[uid]
        elif _generic_name(current_name):
            item["user"] = f"用户 {uid}" if uid else "未知用户"

    filtered: list[dict[str, Any]] = []
    for item in entries:
        ts = item.get("ts")
        if start is not None and (ts is None or float(ts) < start):
            continue
        if end is not None and (ts is None or float(ts) >= end):
            continue
        if user and user not in str(item.get("user") or "").lower():
            continue
        if action and action not in str(item.get("action") or "").lower():
            continue
        if result and result != str(item.get("result") or "").lower():
            continue
        if keyword:
            haystack = " ".join(str(item.get(k) or "") for k in ("user", "action", "target", "detail", "result")).lower()
            if keyword not in haystack:
                continue
        filtered.append(item)
    entries = filtered
    entries.reverse()
    total = len(entries)
    items = [
        {
            "time": item.get("time") or "",
            "uid": item.get("uid") or "",
            "user": item.get("user") or "",
            "action": item.get("action") or "",
            "target": item.get("target") or "",
            "result": item.get("result") or "",
            "read_only": bool(item.get("read_only")),
            "detail": item.get("detail") or "",
        }
        for item in entries[offset : offset + limit]
    ]
    users = sorted({str(item.get("user") or "") for item in entries if item.get("user")})
    actions = sorted({str(item.get("action") or "") for item in entries if item.get("action")})
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {"user": user, "action": action, "result": result, "keyword": keyword, "from": date_from, "to": date_to},
        "users": users,
        "actions": actions,
    }
