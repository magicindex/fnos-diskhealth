"""SQLite 历史采样：按盘记录温度、错误计数与 SMART 指标，供趋势图查询。"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import sqlite3
import threading
import time
from typing import Any

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_CONFIG_PATH = os.path.join(DATA_DIR, "db_config.json")
DEFAULT_DB_DIR = DATA_DIR
_db_dir = DATA_DIR
DB_PATH = os.path.join(_db_dir, "history.db")
RETENTION_DAYS = max(30, int(os.environ.get("HISTORY_RETENTION_DAYS", "180")))
MAX_POINTS = 720

METRICS: dict[str, dict[str, Any]] = {
    "temperature_c": {"label": "温度", "unit": "°C", "agg": "avg", "precision": 1},
    "media_errors": {"label": "介质错误", "unit": "次", "agg": "max", "precision": 0},
    "other_errors": {"label": "其他错误", "unit": "次", "agg": "max", "precision": 0},
    "predictive_failures": {"label": "预测故障", "unit": "次", "agg": "max", "precision": 0},
    "reallocated": {"label": "重映射扇区", "unit": "个", "agg": "max", "precision": 0},
    "pending": {"label": "待映射扇区", "unit": "个", "agg": "max", "precision": 0},
    "offline_uncorrectable": {"label": "不可修正扇区", "unit": "个", "agg": "max", "precision": 0},
    "grown_defects": {"label": "SAS 增长缺陷", "unit": "个", "agg": "max", "precision": 0},
    "read_uncorrected": {"label": "读不可修正", "unit": "次", "agg": "max", "precision": 0},
    "write_uncorrected": {"label": "写不可修正", "unit": "次", "agg": "max", "precision": 0},
    "power_on_hours": {"label": "通电时间", "unit": "小时", "agg": "avg", "precision": 0},
}

_lock = threading.Lock()
_last_prune = 0.0
_initialized = False


def _clean_directory(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raw = DEFAULT_DB_DIR
    raw = os.path.expanduser(raw)
    if not os.path.isabs(raw):
        raise ValueError("数据库目录必须是绝对路径")
    if raw.lower().endswith(".db") or os.path.basename(raw).lower() == "history.db":
        raw = os.path.dirname(raw)
    raw = os.path.abspath(raw)
    if raw in ("/", "/proc", "/sys", "/dev"):
        raise ValueError("不能使用系统根目录或虚拟文件系统目录")
    return raw


def load_config() -> str:
    """读取持久化的数据库目录；无效或不存在时回退默认数据目录。"""
    global _db_dir, DB_PATH, _initialized
    directory = DEFAULT_DB_DIR
    try:
        with open(DB_CONFIG_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        candidate = _clean_directory(saved.get("directory") or "")
        if os.path.isdir(candidate):
            directory = candidate
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        directory = DEFAULT_DB_DIR
    _db_dir = directory
    DB_PATH = os.path.join(directory, "history.db")
    _initialized = False
    return directory


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def info() -> dict[str, Any]:
    """数据库路径、文件体积和历史记录量。"""
    path = DB_PATH
    sizes = {
        "db": _file_size(path),
        "wal": _file_size(path + "-wal"),
        "shm": _file_size(path + "-shm"),
    }
    exists = os.path.isfile(path)
    samples = 0
    disks = 0
    controllers = 0
    oldest_ts = None
    newest_ts = None
    error = ""
    if exists:
        try:
            conn = _connect()
            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*), COUNT(DISTINCT disk_id), COUNT(DISTINCT controller_id),
                           MIN(ts), MAX(ts)
                    FROM disk_samples
                    """
                ).fetchone()
                samples, disks, controllers, oldest_ts, newest_ts = row
            finally:
                conn.close()
        except Exception as exc:
            error = str(exc)
    return {
        "directory": _db_dir,
        "path": path,
        "default_directory": DEFAULT_DB_DIR,
        "config_path": DB_CONFIG_PATH,
        "exists": exists,
        "writable": os.access(os.path.dirname(path), os.W_OK),
        "db_bytes": sizes["db"],
        "wal_bytes": sizes["wal"],
        "shm_bytes": sizes["shm"],
        "total_bytes": sum(sizes.values()),
        "samples": int(samples or 0),
        "disks": int(disks or 0),
        "controllers": int(controllers or 0),
        "oldest_ts": oldest_ts,
        "newest_ts": newest_ts,
        "retention_days": RETENTION_DAYS,
        "error": error,
    }


def set_directory(directory: str) -> dict[str, Any]:
    """更新 history.db 所在目录并持久化，立即切换到新数据库。"""
    global _db_dir, DB_PATH, _initialized, _last_prune
    target = _clean_directory(directory)
    try:
        os.makedirs(target, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"无法创建数据库目录：{exc}") from exc
    if not os.path.isdir(target):
        raise ValueError("数据库路径不是目录")
    if not os.access(target, os.W_OK | os.X_OK):
        raise ValueError("数据库目录不可写")
    probe = os.path.join(target, ".diskhealth-write-test")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.unlink(probe)
    except OSError as exc:
        raise ValueError(f"数据库目录不可写：{exc}") from exc

    with _lock:
        previous = (_db_dir, DB_PATH, _initialized, _last_prune)
        _db_dir = target
        DB_PATH = os.path.join(target, "history.db")
        _initialized = False
        _last_prune = 0.0
    try:
        init()
        os.makedirs(os.path.dirname(DB_CONFIG_PATH), exist_ok=True)
        temp = DB_CONFIG_PATH + ".tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump({"directory": target}, fh, ensure_ascii=False)
        os.chmod(temp, 0o600)
        os.replace(temp, DB_CONFIG_PATH)
    except Exception as exc:
        with _lock:
            _db_dir, DB_PATH, _initialized, _last_prune = previous
        raise ValueError(f"数据库路径保存失败：{exc}") from exc
    return info()


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init() -> None:
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        conn = _connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS disk_samples (
                    ts INTEGER NOT NULL,
                    disk_id TEXT NOT NULL,
                    controller_id TEXT,
                    bay_label TEXT,
                    model TEXT,
                    serial TEXT,
                    health TEXT,
                    state TEXT,
                    temperature_c REAL,
                    temperature_trip_c REAL,
                    power_on_hours REAL,
                    media_errors INTEGER,
                    other_errors INTEGER,
                    predictive_failures INTEGER,
                    reallocated INTEGER,
                    pending INTEGER,
                    offline_uncorrectable INTEGER,
                    grown_defects INTEGER,
                    read_uncorrected INTEGER,
                    write_uncorrected INTEGER,
                    smart_passed INTEGER,
                    PRIMARY KEY (ts, disk_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_disk_ts ON disk_samples(disk_id, ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON disk_samples(ts)")
            conn.commit()
            try:
                os.chmod(DB_PATH, 0o600)
            except OSError:
                pass
            _initialized = True
        finally:
            conn.close()


def _num(value: Any) -> float | int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def record_snapshot(snapshot: dict[str, Any]) -> int:
    """把一次快照写入 SQLite；失败不影响采集主流程。"""
    global _last_prune
    try:
        init()
        ts = int(snapshot.get("generated_ts") or time.time())
        rows = []
        for disk in snapshot.get("disks") or []:
            disk_id = disk.get("id")
            if not disk_id:
                continue
            metrics = disk.get("smart_metrics") or {}
            smart = disk.get("smart") or {}
            passed = smart.get("passed")
            rows.append(
                (
                    ts,
                    disk_id,
                    disk.get("controller_id"),
                    disk.get("bay_label") or disk.get("bay"),
                    disk.get("model"),
                    disk.get("serial"),
                    disk.get("health"),
                    disk.get("state"),
                    _num(disk.get("temperature_c")),
                    _num(disk.get("temperature_trip_c")),
                    _num(disk.get("power_on_hours")),
                    _num(disk.get("media_errors")),
                    _num(disk.get("other_errors")),
                    _num(disk.get("predictive_failures")),
                    _num(metrics.get("reallocated")),
                    _num(metrics.get("pending")),
                    _num(metrics.get("offline_uncorrectable")),
                    _num(metrics.get("grown_defects")),
                    _num(metrics.get("read_uncorrected")),
                    _num(metrics.get("write_uncorrected")),
                    None if passed is None else (1 if passed else 0),
                )
            )
        if not rows:
            return 0
        with _lock:
            conn = _connect()
            try:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO disk_samples (
                        ts, disk_id, controller_id, bay_label, model, serial, health, state,
                        temperature_c, temperature_trip_c, power_on_hours,
                        media_errors, other_errors, predictive_failures,
                        reallocated, pending, offline_uncorrectable, grown_defects,
                        read_uncorrected, write_uncorrected, smart_passed
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )
                now = time.time()
                if now - _last_prune > 3600:
                    conn.execute("DELETE FROM disk_samples WHERE ts < ?", (int(now - RETENTION_DAYS * 86400),))
                    _last_prune = now
                conn.commit()
            finally:
                conn.close()
        return len(rows)
    except Exception:
        return 0


def _since(days: int) -> int:
    days = max(1, min(365, int(days or 30)))
    return int(time.time() - days * 86400)


def metrics() -> list[dict[str, Any]]:
    return [{"key": key, "label": value["label"], "unit": value["unit"]} for key, value in METRICS.items()]


def disks(days: int = 30) -> list[dict[str, Any]]:
    try:
        init()
        since = _since(days)
        conn = _connect()
        try:
            cursor = conn.execute(
                """
                SELECT s.disk_id, COUNT(*) AS samples, MAX(s.ts) AS last_ts,
                       (SELECT model FROM disk_samples x WHERE x.disk_id = s.disk_id ORDER BY x.ts DESC LIMIT 1),
                       (SELECT bay_label FROM disk_samples x WHERE x.disk_id = s.disk_id ORDER BY x.ts DESC LIMIT 1),
                       (SELECT controller_id FROM disk_samples x WHERE x.disk_id = s.disk_id ORDER BY x.ts DESC LIMIT 1),
                       (SELECT health FROM disk_samples x WHERE x.disk_id = s.disk_id ORDER BY x.ts DESC LIMIT 1),
                       (SELECT temperature_c FROM disk_samples x WHERE x.disk_id = s.disk_id ORDER BY x.ts DESC LIMIT 1),
                       (SELECT serial FROM disk_samples x WHERE x.disk_id = s.disk_id ORDER BY x.ts DESC LIMIT 1)
                FROM disk_samples s
                WHERE s.ts >= ?
                GROUP BY s.disk_id
                ORDER BY s.disk_id
                """,
                (since,),
            )
            result = []
            for row in cursor.fetchall():
                result.append(
                    {
                        "disk_id": row[0],
                        "samples": row[1],
                        "last_ts": row[2],
                        "model": row[3] or "",
                        "bay": row[4] or "",
                        "controller_id": row[5] or "",
                        "health": row[6] or "",
                        "temperature_c": row[7],
                        "serial": row[8] or "",
                    }
                )
            return result
        finally:
            conn.close()
    except Exception:
        return []


def _downsample(rows: list[tuple[int, float]], max_points: int, agg: str) -> list[dict[str, Any]]:
    if len(rows) <= max_points:
        return [{"ts": int(ts), "value": value} for ts, value in rows]
    start, end = rows[0][0], rows[-1][0]
    if end <= start:
        return [{"ts": int(start), "value": rows[-1][1]}]
    bucket = max(1, math.ceil((end - start + 1) / max_points))
    buckets: dict[int, list[float]] = {}
    for ts, value in rows:
        key = (ts - start) // bucket
        buckets.setdefault(key, []).append(value)
    result = []
    for key in sorted(buckets):
        values = buckets[key]
        value = sum(values) / len(values) if agg == "avg" else max(values)
        result.append({"ts": int(start + key * bucket + bucket // 2), "value": value})
    return result


def series(disk_id: str, metric: str = "temperature_c", days: int = 30, max_points: int = MAX_POINTS) -> dict[str, Any]:
    metric = metric if metric in METRICS else "temperature_c"
    max_points = max(100, min(2000, int(max_points or MAX_POINTS)))
    info = METRICS[metric]
    since = _since(days)
    try:
        init()
        conn = _connect()
        try:
            rows = [
                (int(ts), float(value))
                for ts, value in conn.execute(
                    f"SELECT ts, {metric} FROM disk_samples WHERE disk_id = ? AND ts >= ? AND {metric} IS NOT NULL ORDER BY ts",
                    (disk_id, since),
                ).fetchall()
            ]
            disk_row = conn.execute(
                """
                SELECT model, bay_label, controller_id, serial, health
                FROM disk_samples WHERE disk_id = ? ORDER BY ts DESC LIMIT 1
                """,
                (disk_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        rows = []
        disk_row = None

    values = [value for _, value in rows]
    stats = {
        "samples": len(rows),
        "latest": rows[-1][1] if rows else None,
        "latest_ts": rows[-1][0] if rows else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "avg": (sum(values) / len(values)) if values else None,
    }
    return {
        "disk_id": disk_id,
        "metric": metric,
        "label": info["label"],
        "unit": info["unit"],
        "days": max(1, min(365, int(days or 30))),
        "points": _downsample(rows, max_points, info["agg"]),
        "stats": stats,
        "disk": {
            "model": (disk_row[0] if disk_row else "") or "",
            "bay": (disk_row[1] if disk_row else "") or "",
            "controller_id": (disk_row[2] if disk_row else "") or "",
            "serial": (disk_row[3] if disk_row else "") or "",
            "health": (disk_row[4] if disk_row else "") or "",
        },
    }


def export_csv(disk_id: str, metric: str = "temperature_c", days: int = 30) -> tuple[bytes, str]:
    data = series(disk_id, metric, days, 5000)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(["时间", f"{data['label']}({data['unit']})"])
    for point in data["points"]:
        writer.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(point["ts"])), point["value"]])
    body = ("\ufeff" + buffer.getvalue()).encode("utf-8")
    return body, f"disk-health-trend-{disk_id.replace('.', '_')}-{metric}-{days}d.csv"


def compare(days: int = 30) -> dict[str, Any]:
    """按盘返回所选范围内最新值与 min/max/avg，供同型号对比视图使用。"""
    days = max(1, min(365, int(days or 30)))
    since = _since(days)
    keys = list(METRICS)
    try:
        init()
        conn = _connect()
        try:
            agg_select = [
                "disk_id", "controller_id", "model", "bay_label", "serial", "health",
                "COUNT(*)", "MAX(ts)",
            ]
            for key in keys:
                agg_select.extend([f"MIN({key})", f"MAX({key})", f"AVG({key})"])
            agg_rows = conn.execute(
                f"SELECT {', '.join(agg_select)} FROM disk_samples WHERE ts >= ? GROUP BY disk_id",
                (since,),
            ).fetchall()
            latest_select = ["s.disk_id", "s.health", "s.state"] + [f"s.{key}" for key in keys]
            latest_rows = conn.execute(
                f"""
                SELECT {', '.join(latest_select)}
                FROM disk_samples s
                JOIN (
                    SELECT disk_id, MAX(ts) AS ts
                    FROM disk_samples WHERE ts >= ? GROUP BY disk_id
                ) latest ON s.disk_id = latest.disk_id AND s.ts = latest.ts
                """,
                (since,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return {"days": days, "metrics": metrics(), "disks": []}

    latest: dict[str, dict[str, Any]] = {}
    for row in latest_rows:
        disk_id = row[0]
        latest[disk_id] = {
            "health": row[1] or "",
            "state": row[2] or "",
            "values": {key: row[index + 3] for index, key in enumerate(keys)},
        }

    result = []
    for row in agg_rows:
        disk_id, controller_id, model, bay, serial, health, samples, last_ts = row[:8]
        stats: dict[str, dict[str, Any]] = {}
        offset = 8
        for key in keys:
            minimum, maximum, average = row[offset : offset + 3]
            stats[key] = {"min": minimum, "max": maximum, "avg": average}
            offset += 3
        item = latest.get(disk_id, {})
        result.append(
            {
                "disk_id": disk_id,
                "controller_id": controller_id or "",
                "model": model or "",
                "bay": bay or "",
                "serial": serial or "",
                "health": item.get("health") or health or "",
                "state": item.get("state") or "",
                "samples": samples,
                "last_ts": last_ts,
                "latest": item.get("values") or {},
                "stats": stats,
            }
        )
    result.sort(key=lambda item: (item["controller_id"], item["model"], item["bay"]))
    return {"days": days, "metrics": metrics(), "disks": result}


load_config()
