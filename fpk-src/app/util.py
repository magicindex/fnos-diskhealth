"""通用工具：子进程、时间、JSON 提取与格式化。"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import threading
import time
import unicodedata
import urllib.parse
from typing import Any

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

TZ_NAME = os.environ.get("TZ", "Asia/Shanghai")


def now() -> _dt.datetime:
    if ZoneInfo is not None:
        try:
            return _dt.datetime.now(ZoneInfo(TZ_NAME))
        except Exception:
            pass
    return _dt.datetime.now()


def iso_now() -> str:
    return now().isoformat(timespec="seconds")


def run(cmd: list[str], timeout: int = 45) -> tuple[int, str, str]:
    """执行命令，永不抛异常；返回 (returncode, stdout, stderr)。

    returncode 约定：124 超时，127 命令不存在。
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"未找到命令：{cmd[0]}"
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return 124, out, f"命令超时（{timeout}s）：{' '.join(cmd)}"
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return 1, "", f"命令执行失败：{exc}"


_run_cache: dict[tuple[Any, ...], tuple[float, tuple[int, str, str]]] = {}
_run_cache_lock = threading.Lock()
RUN_CACHE_TTL = 5.0


def clear_run_cache() -> None:
    with _run_cache_lock:
        _run_cache.clear()


def run_cached(cmd: list[str], timeout: int = 45) -> tuple[int, str, str]:
    """同一次采集轮次内复用 lsblk / zpool 等只读命令结果。"""
    key = (tuple(cmd), timeout)
    now_ts = time.monotonic()
    with _run_cache_lock:
        cached = _run_cache.get(key)
        if cached and now_ts - cached[0] <= RUN_CACHE_TTL:
            return cached[1]
    result = run(cmd, timeout=timeout)
    with _run_cache_lock:
        _run_cache[key] = (now_ts, result)
    return result


def extract_json(text: str) -> dict[str, Any] | None:
    """从可能夹杂提示文本的输出中提取第一个 JSON 对象。"""
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError:
        try:
            return json.loads(text[start:])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1000 or unit == "PB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1000
    return f"{size:.2f} PB"


def natural_key(text: str) -> list[Any]:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(text))]


def strip(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def normalize_text(value: Any) -> str:
    """统一文本编码：百分号解码、Latin-1 乱码修复、NFC 归一化。

    同时用于 fnOS 用户名和设备厂商/型号：
    - fnOS 网关 Header 被 Python 按 ISO-8859-1 解码后，会得到 "å¼ ä¸‰"；
      重新按 Latin-1 编码再按 UTF-8 解码即可恢复。
    - 某些 perccli / sysfs 字段可能是百分号编码或 Latin-1。
    """
    if value is None:
        return ""
    text = str(value)
    for _ in range(2):
        if not re.search(r"%[0-9A-Fa-f]{2}", text):
            break
        decoded = urllib.parse.unquote(text, errors="replace")
        if decoded == text:
            break
        text = decoded
    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != text:
            text = repaired
            break
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in text)
    return " ".join(text.split())


def normalize_multiline(value: Any) -> str:
    """与 normalize_text 相同的编码修复，但保留换行和 Markdown 结构。"""
    if value is None:
        return ""
    text = str(value)
    for _ in range(2):
        if not re.search(r"%[0-9A-Fa-f]{2}", text):
            break
        decoded = urllib.parse.unquote(text, errors="replace")
        if decoded == text:
            break
        text = decoded
    for encoding in ("latin-1", "cp1252"):
        try:
            repaired = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if repaired != text:
            text = repaired
            break
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out: list[str] = []
    blanks = 0
    for line in lines:
        if not line.strip():
            blanks += 1
            if blanks <= 2:
                out.append("")
        else:
            blanks = 0
            out.append(line)
    return "\n".join(out)


def decode_header_text(value: Any) -> str:
    """修复 HTTP Header 的 Latin-1 解码（fnOS 中文昵称）。"""
    if value is None:
        return ""
    text = str(value)
    for encoding in ("latin-1", "cp1252"):
        try:
            decoded = text.encode(encoding).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if decoded != text:
            text = decoded
            break
    return normalize_text(text)


def to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"-?\d+", str(value).replace(",", ""))
    return int(match.group()) if match else None
