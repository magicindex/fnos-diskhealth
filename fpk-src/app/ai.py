"""AI 故障分析：供应商配置、Chat 调用、提示词构建与报告存储。"""

from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from util import iso_now, normalize_multiline, normalize_text, strip, to_int

DATA_DIR = os.environ.get("AI_DATA_DIR") or os.environ.get("DATA_DIR", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "ai_config.json")
REPORT_DIR = os.path.join(DATA_DIR, "ai_reports")
REPORT_INDEX = os.path.join(DATA_DIR, "ai_reports.jsonl")

PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "style": "openai",
        "key_required": True,
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "style": "openai",
        "key_required": True,
    },
    {
        "id": "moonshot",
        "label": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "style": "openai",
        "key_required": True,
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "style": "openai",
        "key_required": True,
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼 / 通义",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "style": "openai",
        "key_required": True,
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "style": "openai",
        "key_required": True,
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat",
        "style": "openai",
        "key_required": True,
    },
    {
        "id": "ollama",
        "label": "Ollama（本地）",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "style": "openai",
        "key_required": False,
    },
    {
        "id": "anthropic",
        "label": "Anthropic Claude",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-3-5-sonnet-20241022",
        "style": "anthropic",
        "key_required": True,
    },
    {
        "id": "custom",
        "label": "自定义 OpenAI 兼容接口",
        "base_url": "",
        "model": "",
        "style": "openai",
        "key_required": False,
    },
]
PROVIDER_MAP = {item["id"]: item for item in PROVIDERS}

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "deepseek",
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "api_key": "",
    "redact_serials": True,
    "temperature": 0.2,
    "max_tokens": 1800,
    "timeout": 300,
}

SYSTEM_PROMPT = """你是一名资深存储与 RAID 故障诊断工程师，正在分析一台 fnOS NAS 的物理硬盘健康数据。
请结合 RAID / HBA 控制器、背板、扩展器、SES 槽位与磁盘链路拓扑综合判断。
严格依据给定 JSON 数据，不要编造不存在的字段或结论；无法判断时明确说明。
使用简体中文，输出 Markdown，结构如下：
## 总体结论
用 1-3 句话说明系统是否健康，以及最需要关注的问题。

## 风险等级
只填写：正常 / 关注 / 高风险，并用一句话说明理由。

## 逐盘分析
仅针对异常盘或高风险盘展开；正常盘可一句话汇总。

## 建议动作
按优先级列出可执行动作（立即处理 / 计划处理 / 继续观察），说明涉及盘位和 RAID 影响。

## 需要持续观察的指标
列出温度、介质错误、预测故障、SAS 增长缺陷、NVMe 寿命等关键指标及阈值建议。"""

SCOPE_LABELS = {"issues": "异常磁盘", "all": "全部磁盘", "ids": "当前筛选"}


class AIError(RuntimeError):
    def __init__(self, message: str, status: int = 502, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


def _json_error(body: bytes, fallback: str) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        text = body.decode("utf-8", "replace").strip()
        return text[:500] or fallback
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return normalize_text(error.get("message")) or fallback
        if isinstance(error, str):
            return normalize_text(error)[:500]
        if data.get("message"):
            return normalize_text(data["message"])[:500]
    return fallback


def provider_list() -> list[dict[str, Any]]:
    return [
        {
            "id": item["id"],
            "label": item["label"],
            "base_url": item["base_url"],
            "model": item["model"],
            "style": item["style"],
            "key_required": item["key_required"],
        }
        for item in PROVIDERS
    ]


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in config:
                if key in data:
                    config[key] = data[key]
    except (OSError, json.JSONDecodeError):
        pass
    if config.get("provider") not in PROVIDER_MAP:
        config["provider"] = DEFAULT_CONFIG["provider"]
    return config


def status() -> dict[str, Any]:
    """普通用户可见的 AI 状态，不包含接口地址与 Key。"""
    config = load_config()
    preset = PROVIDER_MAP.get(config.get("provider", ""), PROVIDER_MAP["custom"])
    configured = bool(config.get("base_url") and config.get("model")) and (
        bool(config.get("api_key")) or not preset["key_required"]
    )
    return {
        "configured": configured,
        "provider": config.get("provider"),
        "provider_label": preset["label"],
        "model": config.get("model"),
        "redact_serials": bool(config.get("redact_serials", True)),
    }


def public_config() -> dict[str, Any]:
    config = load_config()
    return {
        "provider": config["provider"],
        "base_url": config["base_url"],
        "model": config["model"],
        "has_key": bool(config.get("api_key")),
        "redact_serials": bool(config.get("redact_serials", True)),
        "temperature": config.get("temperature", 0.2),
        "max_tokens": config.get("max_tokens", 1800),
        "timeout": config.get("timeout", 120),
        "providers": provider_list(),
    }


def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    current = load_config()
    provider = strip(payload.get("provider")) or current["provider"]
    if provider not in PROVIDER_MAP:
        raise AIError("未知的 AI 供应商", 400)
    preset = PROVIDER_MAP[provider]
    base_url = strip(payload.get("base_url")) or preset["base_url"]
    model = strip(payload.get("model")) or preset["model"]
    if not base_url:
        raise AIError("请填写接口地址", 400)
    if not re.match(r"^https?://", base_url, re.IGNORECASE):
        raise AIError("接口地址必须以 http:// 或 https:// 开头", 400)
    if not model:
        raise AIError("请填写模型名称", 400)

    if payload.get("clear_key"):
        api_key = ""
    elif "api_key" in payload and strip(payload.get("api_key")):
        api_key = strip(payload.get("api_key"))
    elif provider != current.get("provider"):
        # 换供应商时不把旧 Key 发给新接口
        api_key = ""
    else:
        api_key = current.get("api_key", "")

    try:
        temperature = float(payload.get("temperature", current.get("temperature", 0.2)))
    except (TypeError, ValueError):
        temperature = 0.2
    temperature = min(2.0, max(0.0, temperature))
    try:
        max_tokens = int(payload.get("max_tokens", current.get("max_tokens", 1800)))
    except (TypeError, ValueError):
        max_tokens = 1800
    max_tokens = min(8192, max(256, max_tokens))
    try:
        timeout = int(payload.get("timeout", current.get("timeout", 300)))
    except (TypeError, ValueError):
        timeout = 120
    timeout = min(300, max(10, timeout))

    config = {
        "provider": provider,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "api_key": api_key,
        "redact_serials": bool(payload.get("redact_serials", current.get("redact_serials", True))),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError as exc:
        raise AIError("保存 AI 配置失败", 500, str(exc)) from exc
    return public_config()


def _endpoint(base_url: str, style: str) -> str:
    base = base_url.rstrip("/")
    if style == "anthropic":
        return base if base.endswith("/messages") else base + "/messages"
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _http_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> tuple[int, dict[str, Any], bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    context = None
    if os.environ.get("AI_INSECURE_TLS") == "1" and url.lower().startswith("https://"):
        context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read()
            try:
                return response.status, json.loads(raw.decode("utf-8", "replace")), raw
            except (ValueError, UnicodeDecodeError):
                return response.status, {}, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, {}, raw
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIError("无法连接 AI 接口", 502, str(exc)) from exc


def _ollama_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    max_out: int,
    timeout: int,
) -> dict[str, Any]:
    """Ollama 原生 /api/chat，关闭 thinking，避免小 token 预算被思考消耗。"""
    base = strip(config.get("base_url")).rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if base.endswith("/api/chat"):
        url = base
    elif base.endswith("/api"):
        url = base + "/chat"
    else:
        url = base + "/api/chat"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    api_key = strip(config.get("api_key"))
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": config.get("model"),
        "messages": messages,
        "stream": False,
        "think": False,
        "options": {
            "temperature": config.get("temperature", 0.2),
            "num_predict": max_out,
        },
    }
    status, data, raw = _http_json(url, headers, payload, timeout)
    if status < 200 or status >= 300:
        raise AIError(f"Ollama 接口返回 HTTP {status}", 502, _json_error(raw, "请求失败"))
    message = data.get("message") or {}
    content = normalize_multiline(message.get("content"))
    if not content:
        if data.get("done_reason") == "length":
            raise AIError("模型输出达到 token 上限，请提高最大输出 token", 502)
        raise AIError("Ollama 返回内容为空", 502, str(data)[:300])
    usage = {
        "prompt_tokens": data.get("prompt_eval_count"),
        "completion_tokens": data.get("eval_count"),
    }
    return {
        "content": content,
        "usage": usage,
        "model": data.get("model") or config.get("model"),
        "endpoint": url,
    }


def chat(config: dict[str, Any], messages: list[dict[str, str]], *, max_tokens: int | None = None) -> dict[str, Any]:
    preset = PROVIDER_MAP.get(config.get("provider", ""), PROVIDER_MAP["custom"])
    style = preset["style"]
    api_key = strip(config.get("api_key"))
    if preset["key_required"] and not api_key:
        raise AIError("请先配置 API Key", 400)
    timeout = int(config.get("timeout", 300))
    max_out = int(max_tokens or config.get("max_tokens", 1800))
    if config.get("provider") == "ollama":
        return _ollama_chat(config, messages, max_out, timeout)
    url = _endpoint(strip(config.get("base_url")) or preset["base_url"], style)

    system = ""
    user_messages = []
    for message in messages:
        if message.get("role") == "system":
            system = message.get("content", "")
        else:
            user_messages.append(message)

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "disk-health/1.1",
    }
    if style == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
        payload = {
            "model": config.get("model"),
            "max_tokens": max_out,
            "temperature": config.get("temperature", 0.2),
            "system": system,
            "messages": user_messages,
        }
    else:
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": config.get("model"),
            "messages": ([{"role": "system", "content": system}] if system else []) + user_messages,
            "temperature": config.get("temperature", 0.2),
            "max_tokens": max_out,
            "stream": False,
        }

    status, data, raw = _http_json(url, headers, payload, timeout)
    if status < 200 or status >= 300:
        raise AIError(f"AI 接口返回 HTTP {status}", 502, _json_error(raw, "请求失败"))

    content = ""
    usage: dict[str, Any] = {}
    if style == "anthropic":
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                content += block.get("text") or ""
        usage = {
            "prompt_tokens": (data.get("usage") or {}).get("input_tokens"),
            "completion_tokens": (data.get("usage") or {}).get("output_tokens"),
        }
    else:
        choices = data.get("choices") or []
        finish_reason = ""
        if choices:
            finish_reason = str(choices[0].get("finish_reason") or "")
            message = choices[0].get("message") or {}
            value = message.get("content")
            if isinstance(value, list):
                content = "".join(str(part.get("text", "")) for part in value if isinstance(part, dict))
            else:
                content = str(value or "")
            if not content:
                reasoning = message.get("reasoning_content") or message.get("reasoning")
                if reasoning and finish_reason == "length":
                    raise AIError(
                        "模型把输出 token 用在了思考过程，未生成最终内容；请提高最大输出 token",
                        502,
                    )
        usage = data.get("usage") or {}
        content = content or str(data.get("response") or "")
    content = normalize_multiline(content)
    if not content:
        raise AIError("AI 返回内容为空", 502, str(data)[:300])
    return {"content": content, "usage": usage, "model": data.get("model") or config.get("model"), "endpoint": url}


def mask_serial(value: str) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    if len(text) <= 4:
        return "***"
    return text[:4] + "***"


def _compact_disk(disk: dict[str, Any], redact: bool) -> dict[str, Any]:
    smart = disk.get("smart") or {}
    attributes = {}
    for attr in smart.get("attributes") or []:
        if attr.get("id") in (5, 9, 187, 188, 194, 196, 197, 198, 199):
            attributes[attr.get("name")] = attr.get("raw_string") or attr.get("raw")
    sas = smart.get("sas") or {}
    result = {
        "id": disk.get("id"),
        "controller": disk.get("controller_name"),
        "bay": disk.get("bay_label") or disk.get("bay"),
        "vendor": disk.get("vendor") or smart.get("vendor"),
        "model": disk.get("model"),
        "serial": mask_serial(disk.get("serial")) if redact else disk.get("serial"),
        "firmware": disk.get("firmware"),
        "interface": disk.get("interface"),
        "protocol": disk.get("protocol_detail"),
        "media": disk.get("media"),
        "capacity": disk.get("size"),
        "state": disk.get("state_label") or disk.get("state"),
        "health": disk.get("health"),
        "reasons": disk.get("reasons") or [],
        "temperature_c": disk.get("temperature_c"),
        "temperature_trip_c": disk.get("temperature_trip_c") or smart.get("temperature_trip_c"),
        "power_on_hours": disk.get("power_on_hours"),
        "rotation_rate": smart.get("rotation_rate"),
        "media_errors": disk.get("media_errors"),
        "other_errors": disk.get("other_errors"),
        "predictive_failures": disk.get("predictive_failures"),
        "smart_alert": disk.get("smart_alert"),
        "smart_passed": smart.get("passed"),
        "raid": disk.get("vd_id"),
        "raid_level": disk.get("vd_level"),
        "link_speed": disk.get("link_speed"),
        "link_rate": disk.get("link_rate"),
        "sas_address": disk.get("sas_address"),
        "ses_bay": disk.get("ses_bay"),
        "ses_enclosure": disk.get("ses_enclosure"),
        "expander": disk.get("expander_product"),
        "enclosure": disk.get("enclosure_product") or disk.get("enclosure_id"),
        "phy_id": disk.get("phy_id"),
        "port": disk.get("connected_port"),
        "multipath": disk.get("multipath"),
        "certified": disk.get("certified"),
        "sed": disk.get("sed_capable"),
        "raw_size": disk.get("raw_size"),
        "sector_size": disk.get("sector_size") or disk.get("physical_sector_size"),
    }
    if attributes:
        result["ata_smart"] = attributes
    if sas:
        result["sas"] = {
            "transport": sas.get("transport"),
            "read": sas.get("read"),
            "write": sas.get("write"),
            "verify": sas.get("verify"),
            "grown_defects": sas.get("grown_defects"),
            "non_medium_errors": sas.get("non_medium_errors"),
            "start_stop_cycles": sas.get("start_stop_cycles"),
            "load_unload_cycles": sas.get("load_unload_cycles"),
            "manufactured": sas.get("manufactured"),
            "self_test": sas.get("self_test"),
        }
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


def _compact_topology(topology: dict[str, Any], selected_ids: set[str], redact: bool) -> dict[str, Any]:
    controllers = []
    for controller in topology.get("controllers") or []:
        enclosures = []
        for enclosure in controller.get("enclosures") or []:
            drives = []
            for disk in enclosure.get("drives") or []:
                if disk.get("id") not in selected_ids:
                    continue
                disk = dict(disk)
                if redact:
                    disk["serial"] = mask_serial(disk.get("serial"))
                drives.append(disk)
            item = {k: v for k, v in enclosure.items() if k != "drives"}
            if redact and item.get("serial"):
                item["serial"] = mask_serial(item.get("serial"))
            item["drives"] = drives
            item["drive_count"] = len(drives)
            enclosures.append(item)
        direct = []
        for disk in controller.get("direct_disks") or []:
            if disk.get("id") not in selected_ids:
                continue
            disk = dict(disk)
            if redact:
                disk["serial"] = mask_serial(disk.get("serial"))
            direct.append(disk)
        controllers.append(
            {
                key: controller.get(key)
                for key in (
                    "id", "model", "kind", "kind_label", "health", "pci", "sas_address",
                    "firmware", "bios", "driver", "temperature_c", "bbu", "details",
                    "vds", "expanders", "expander_count", "enclosure_count",
                )
                if controller.get(key) not in (None, "", [], {})
            }
            | {"enclosures": enclosures, "direct_disks": direct}
        )
    return {"controllers": controllers, "enclosures": topology.get("enclosures"), "expanders": topology.get("expanders")}


def build_dataset(snapshot: dict[str, Any], scope: str, ids: list[str] | None, redact: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    disks = snapshot.get("disks") or []
    selected = list(disks)
    if scope == "issues":
        selected = [d for d in disks if d.get("health") in ("warning", "critical", "unknown")]
    elif scope == "ids":
        wanted = set(ids or [])
        selected = [d for d in disks if d.get("id") in wanted]
    if not selected:
        raise AIError("没有符合范围的磁盘可分析", 400)
    controllers = []
    for controller in snapshot.get("controllers") or []:
        controllers.append(
            {
                "id": controller.get("id"),
                "model": controller.get("model"),
                "kind": controller.get("kind"),
                "firmware": controller.get("firmware"),
                "bios": controller.get("bios"),
                "driver": controller.get("driver"),
                "pci": controller.get("pci"),
                "sas_address": controller.get("sas_address"),
                "temperature_c": controller.get("temperature_c"),
                "bbu": controller.get("bbu"),
                "memory_correctable": controller.get("memory_correctable"),
                "memory_uncorrectable": controller.get("memory_uncorrectable"),
                "controller_status": controller.get("controller_status"),
                "enclosure_count": controller.get("enclosure_count"),
                "expander_count": controller.get("expander_count"),
                "details": {
                    key: (controller.get("details") or {}).get(key)
                    for key in (
                        "bios", "ctrl_r", "nvdata", "bootloader", "onboard_memory",
                        "flash_size", "nvram_size", "cachevault_flash", "front_end_ports",
                        "backend_ports", "onboard_expander", "alarm", "roc_temp_c",
                        "ctrl_temp_c", "policies", "scheduled",
                    )
                    if (controller.get("details") or {}).get(key) not in (None, "", [], {})
                },
                "health": controller.get("health"),
                "vds": [
                    {
                        "level": vd.get("level"),
                        "state": vd.get("state_label"),
                        "size": vd.get("size"),
                        "os_device": vd.get("os_device"),
                        "members": vd.get("members"),
                    }
                    for vd in controller.get("vds") or []
                ],
            }
        )
    dataset = {
        "host": snapshot.get("host"),
        "collected_at": snapshot.get("generated_at"),
        "summary": snapshot.get("summary"),
        "controllers": controllers,
        "scope": SCOPE_LABELS.get(scope, scope),
        "disks": [_compact_disk(disk, redact) for disk in selected],
        "topology": _compact_topology(snapshot.get("topology") or {}, {d.get("id") for d in selected}, redact),
    }
    return dataset, selected


def analyze(
    snapshot: dict[str, Any],
    config: dict[str, Any],
    scope: str,
    ids: list[str] | None,
    events: list[dict[str, Any]] | None = None,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dataset, selected = build_dataset(snapshot, scope, ids, bool(config.get("redact_serials", True)))
    redact = bool(config.get("redact_serials", True))
    if events:
        recent = []
        for event in events[-8:]:
            item = dict(event)
            if redact:
                item["serial"] = mask_serial(item.get("serial"))
            recent.append(item)
        dataset["recent_health_events"] = recent
    user_prompt = (
        f"请分析以下 fnOS 物理硬盘快照（范围：{SCOPE_LABELS.get(scope, scope)}）。\n"
        "重点关注 RAID 状态、介质错误、预测故障、SAS 增长缺陷、温度与 SMART 趋势。\n\n"
        + json.dumps(dataset, ensure_ascii=False, indent=1)
    )
    if len(user_prompt) > 48000:
        dataset["disks"] = [
            {key: value for key, value in disk.items() if key not in ("ata_smart", "sas")}
            for disk in dataset["disks"]
        ]
        user_prompt = (
            f"请分析以下 fnOS 物理硬盘快照（范围：{SCOPE_LABELS.get(scope, scope)}）。\n"
            "数据已裁剪 SMART 细节，请基于健康状态与关键计数判断。\n\n"
            + json.dumps(dataset, ensure_ascii=False, indent=1)
        )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt[:50000]},
    ]
    started = time.time()
    result = chat(config, messages)
    report = {
        "id": time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex(),
        "created_at": iso_now(),
        "provider": config.get("provider"),
        "provider_label": PROVIDER_MAP.get(config.get("provider", ""), {}).get("label", config.get("provider")),
        "model": result.get("model") or config.get("model"),
        "scope": scope,
        "scope_label": SCOPE_LABELS.get(scope, scope),
        "disk_count": len(selected),
        "disk_ids": [disk.get("id") for disk in selected],
        "serial_redacted": bool(config.get("redact_serials", True)),
        "duration_ms": int((time.time() - started) * 1000),
        "usage": result.get("usage") or {},
        "prompt_chars": len(user_prompt),
        "uid": str(user.get("uid")) if user and user.get("uid") is not None else None,
        "username": (user or {}).get("username") or "",
        "content": result["content"],
    }
    _save_report(report)
    return report


_report_lock = threading.Lock()


def _save_report(report: dict[str, Any]) -> None:
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError:
        return
    with _report_lock:
        try:
            with open(os.path.join(REPORT_DIR, report["id"] + ".md"), "w", encoding="utf-8") as fh:
                fh.write(report["content"])
            with open(REPORT_INDEX, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(report, ensure_ascii=False) + "\n")
            os.chmod(REPORT_INDEX, 0o600)
        except OSError:
            return


def list_reports(limit: int = 30, uid: str | None = None, is_admin: bool = False) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    try:
        with open(REPORT_INDEX, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines[-limit:]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            if not is_admin and uid is not None and str(data.get("uid")) != str(uid):
                continue
            reports.append(
                {
                    "id": data.get("id"),
                    "created_at": data.get("created_at"),
                    "provider_label": data.get("provider_label"),
                    "model": data.get("model"),
                    "scope_label": data.get("scope_label"),
                    "disk_count": data.get("disk_count"),
                }
            )
    return list(reversed(reports))


def get_report(report_id: str) -> dict[str, Any] | None:
    if not report_id:
        return None
    try:
        with open(REPORT_INDEX, "r", encoding="utf-8") as fh:
            for line in reversed(fh.readlines()):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("id") == report_id:
                    return data
    except OSError:
        return None
    return None


def export_report(report: dict[str, Any], fmt: str) -> tuple[bytes, str, str]:
    fmt = fmt if fmt in ("md", "txt", "json") else "md"
    if fmt == "json":
        body = json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8")
        mime = "application/json; charset=utf-8"
    else:
        content = report.get("content") or ""
        header = (
            f"# RAID 阵列健康 AI 诊断报告\n\n"
            f"- 生成时间：{report.get('created_at')}\n"
            f"- 供应商 / 模型：{report.get('provider_label')} / {report.get('model')}\n"
            f"- 分析范围：{report.get('scope_label')}（{report.get('disk_count')} 块）\n"
            f"- 序列号：{'已脱敏' if report.get('serial_redacted') else '完整'}\n"
            f"- 耗时：{report.get('duration_ms')} ms\n\n---\n\n"
        )
        body = (header + content).encode("utf-8")
        mime = "text/markdown; charset=utf-8" if fmt == "md" else "text/plain; charset=utf-8"
    ext = "md" if fmt == "md" else fmt
    filename = f"disk-health-ai-{report.get('id', 'report')}.{ext}"
    return body, mime, filename


def test_connection(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or load_config()
    result = chat(
        config,
        [
            {"role": "system", "content": "你是连接测试助手。只回复 OK。"},
            {"role": "user", "content": "请只回复 OK"},
        ],
        max_tokens=512,
    )
    return {"ok": True, "reply": result["content"][:200], "model": result.get("model")}
