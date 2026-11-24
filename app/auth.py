"""fnOS 统一登录：复用 NAS 会话，不维护本地账号密码。

两条校验路径：
1. 浏览器已登录 fnOS 时，读取同域 Cookie（fnos-token / ost），
   由服务端带 Cookie 调用 fnOS /oauthapi/userinfo 或 /oauthapi/authorize 验证。
2. 没有有效 Cookie 时，302 到 fnOS /signin 完成登录，回调后换取身份。

本模块只保存 fnOS 返回的 uid / username / 管理员标记，不保存密码或原始 token。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from typing import Any

from util import normalize_text

CLIENT_ID = os.environ.get("FNOS_OAUTH_CLIENT_ID", "YJNMPJUGA9")
SCOPE = os.environ.get("FNOS_OAUTH_SCOPE", "trim.user.all")
FNOS_BASE = (os.environ.get("FNOS_BASE_URL", "http://host.docker.internal:5666")).rstrip("/")
FNOS_PUBLIC = (os.environ.get("FNOS_PUBLIC_URL", "")).rstrip("/")
INSECURE_TLS = os.environ.get("FNOS_INSECURE_TLS", "0") == "1"
DATA_DIR = os.environ.get("DATA_DIR", "/data")

SESSION_COOKIE = "dh_auth"
STATE_COOKIE = "dh_oauth"
SESSION_TTL = max(300, int(os.environ.get("SESSION_TTL", "43200")))
STATE_TTL = 600
HTTP_TIMEOUT = max(3, int(os.environ.get("FNOS_AUTH_TIMEOUT", "8")))
CLIENT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "disk-health/1.0",
    "X-Trim-Client": "disk-health",
    "X-Trim-Client-Version": "1.0.0",
}

# 允许 fnOS 登录态 Cookie 的名称（新版本用 ost，旧版本用 fnos-token）
TOKEN_COOKIES = ("fnos-token", "ost")
LONG_TOKEN_COOKIES = ("fnos-long-token", "osrt")

_secret_lock = threading.Lock()
_secret_cache: bytes | None = None
_cache_lock = threading.Lock()
_login_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class AuthError(RuntimeError):
    """认证流程中的可展示错误。"""

    def __init__(self, message: str, *, detail: str = "", status: int = 502, code: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status
        self.code = code


# --------------------------------------------------------------------------- #
# 基础 HTTP
# --------------------------------------------------------------------------- #
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _opener() -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_NoRedirect()]
    if INSECURE_TLS:
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    return urllib.request.build_opener(*handlers)


def _decode(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _http(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = HTTP_TIMEOUT,
) -> tuple[int, dict[str, Any]]:
    url = FNOS_BASE + path
    body = None
    merged = dict(CLIENT_HEADERS)
    if headers:
        merged.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=merged, method=method)
    try:
        with _opener().open(request, timeout=timeout) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AuthError("无法连接 fnOS 认证服务", detail=str(exc), status=502) from exc


# --------------------------------------------------------------------------- #
# Cookie / PKCE / 签名
# --------------------------------------------------------------------------- #
def parse_cookies(cookie_header: str | None) -> dict[str, str]:
    if not cookie_header:
        return {}
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except Exception:
        return {}
    return {name: morsel.value for name, morsel in jar.items()}


def fnos_tokens(cookie_header: str | None) -> dict[str, str]:
    cookies = parse_cookies(cookie_header)
    out: dict[str, str] = {}
    for name in TOKEN_COOKIES:
        if cookies.get(name):
            out["token"] = cookies[name]
            out["token_name"] = name
            break
    for name in LONG_TOKEN_COOKIES:
        if cookies.get(name):
            out["long_token"] = cookies[name]
            break
    return out


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload: dict[str, Any], secret: bytes) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body = _b64(raw)
    sig = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def _verify(value: str, secret: bytes) -> dict[str, Any] | None:
    if not value or value.count(".") != 1:
        return None
    body, signature = value.split(".", 1)
    expected = _b64(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp") or 0) <= int(time.time()):
        return None
    return payload


def secret() -> bytes:
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache
    with _secret_lock:
        if _secret_cache is not None:
            return _secret_cache
        path = os.path.join(DATA_DIR, "auth_secret")
        data = b""
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            pass
        if len(data) < 32:
            data = secrets.token_bytes(32)
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            except OSError:
                pass
        _secret_cache = data
        return data


def issue_session(user: dict[str, Any]) -> str:
    now = int(time.time())
    payload = {
        "p": "session",
        "uid": user.get("uid"),
        "username": user.get("username") or "",
        "admin": bool(user.get("admin")),
        "iat": now,
        "exp": now + SESSION_TTL,
    }
    return _sign(payload, secret())


def read_session(cookie_header: str | None) -> dict[str, Any] | None:
    value = parse_cookies(cookie_header).get(SESSION_COOKIE)
    if not value:
        return None
    payload = _verify(value, secret())
    if not payload or payload.get("p") != "session":
        return None
    return {
        "uid": payload.get("uid"),
        "username": payload.get("username") or "",
        "admin": bool(payload.get("admin")),
        "exp": payload.get("exp"),
    }


def issue_state(verifier: str, redirect_uri: str, state: str, next_path: str = "/") -> str:
    now = int(time.time())
    return _sign(
        {
            "p": "oauth",
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "state": state,
            "next": next_path,
            "exp": now + STATE_TTL,
        },
        secret(),
    )


def read_state(cookie_header: str | None) -> dict[str, Any] | None:
    value = parse_cookies(cookie_header).get(STATE_COOKIE)
    if not value:
        return None
    payload = _verify(value, secret())
    if not payload or payload.get("p") != "oauth":
        return None
    return payload


def cookie_header(name: str, value: str, max_age: int, *, secure: bool = False, http_only: bool = True) -> str:
    parts = [f"{name}={value}", "Path=/", f"Max-Age={max_age}", "SameSite=Lax"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# fnOS 身份校验
# --------------------------------------------------------------------------- #
def _parse_user(data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(data.get("user"), dict):
        data = data["user"]
    uid = data.get("uid")
    if uid is None:
        uid = data.get("userId")
    if uid is None:
        uid = data.get("id")
    username = (
        data.get("username")
        or data.get("userName")
        or data.get("user_name")
        or data.get("name")
        or ""
    )
    admin = False
    for key in ("admin", "isAdmin", "is_admin"):
        if key in data:
            admin = bool(data.get(key))
            break
    return {
        "uid": uid,
        "username": normalize_text(username),
        "admin": admin,
    }


def _userinfo_cookie(tokens: dict[str, str]) -> dict[str, Any] | None:
    pairs = []
    if tokens.get("token"):
        pairs.append(f"fnos-token={tokens['token']}")
        pairs.append(f"ost={tokens['token']}")
    if tokens.get("long_token"):
        pairs.append(f"fnos-long-token={tokens['long_token']}")
        pairs.append(f"osrt={tokens['long_token']}")
    if not pairs:
        return None
    status, payload = _http("POST", "/oauthapi/userinfo", headers={"Cookie": "; ".join(pairs)})
    if status == 200 and payload.get("code") == 0 and isinstance(payload.get("data"), dict):
        user = _parse_user(payload["data"])
        if user.get("uid") is not None or user.get("username"):
            return user
    return None


def _userinfo_bearer(access_token: str) -> dict[str, Any] | None:
    if not access_token:
        return None
    status, payload = _http(
        "POST",
        "/oauthapi/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if status == 200 and payload.get("code") == 0 and isinstance(payload.get("data"), dict):
        user = _parse_user(payload["data"])
        if user.get("uid") is not None or user.get("username"):
            return user
    return None


def _exchange(code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
    }
    if redirect_uri:
        payload["redirect_uri"] = redirect_uri
    status, response = _http("POST", "/oauthapi/third-part/token", payload=payload)
    code_value = response.get("code")
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    if status != 200 or code_value not in (0, None) or not data:
        raise AuthError(
            "fnOS 授权码兑换失败",
            detail=str(response.get("msg") or f"HTTP {status}"),
            status=502,
            code=code_value,
        )
    access = data.get("access_token") or data.get("accessToken") or data.get("token") or ""
    user = _parse_user(data)
    if not user.get("username") and access:
        extra = _userinfo_bearer(access)
        if extra:
            if not user.get("uid"):
                user["uid"] = extra.get("uid")
            user["username"] = extra.get("username") or user.get("username") or ""
            user["admin"] = bool(user.get("admin") or extra.get("admin"))
    if not user.get("username") and user.get("uid") is not None:
        user["username"] = f"用户 {user['uid']}"
    return user


def _authorize_with_token(token: str) -> dict[str, Any] | None:
    """用浏览器 Cookie 中的 fnOS 会话 token 直接换授权码（无需跳转）。

    返回用户信息；token 无效时返回 None（上层再跳登录页）。
    """
    verifier, challenge = _pkce()
    payload = {
        "client_id": CLIENT_ID,
        "redirect_uri": "",
        "response_type": "code",
        "state": "",
        "token": token,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": SCOPE,
        "client_type": "public",
        "device_id": f"disk-health-{secrets.token_hex(4)}",
        "device_name": "RAID 阵列健康",
        "device_model": "Web",
    }
    status, response = _http("POST", "/oauthapi/authorize", payload=payload)
    if status != 200:
        raise AuthError("fnOS 授权服务返回异常", detail=f"HTTP {status}", status=502)
    code_value = response.get("code")
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    if code_value == 0 and data.get("code"):
        return _exchange(str(data["code"]), verifier, "")
    if code_value in (10003, 11003, 12002):
        return None
    if code_value not in (0, None):
        raise AuthError(
            "fnOS 会话校验失败",
            detail=str(response.get("msg") or ""),
            status=502,
            code=code_value,
        )
    return None


def login_with_cookie(cookie_header: str | None) -> dict[str, Any] | None:
    """优先复用 fnOS 浏览器会话；失败返回 None。"""
    tokens = fnos_tokens(cookie_header)
    token = tokens.get("token")
    if not token:
        return None
    cache_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    with _cache_lock:
        cached = _login_cache.get(cache_key)
        if cached and cached[0] > now:
            return dict(cached[1])

    user = _userinfo_cookie(tokens)
    if user is None:
        user = _authorize_with_token(token)
    if user is None:
        return None
    with _cache_lock:
        if len(_login_cache) > 64:
            _login_cache.clear()
        _login_cache[cache_key] = (now + 120, dict(user))
    return user


def exchange_callback(code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
    return _exchange(code, verifier, redirect_uri)


# --------------------------------------------------------------------------- #
# URL / 请求上下文
# --------------------------------------------------------------------------- #
def request_origin(host_header: str | None, proto: str | None) -> str:
    scheme = "https" if (proto or "").lower() == "https" else "http"
    host = (host_header or "127.0.0.1").split(",")[0].strip()
    host = host.replace("\r", "").replace("\n", "")
    if "/" in host or " " in host:
        host = "127.0.0.1"
    return f"{scheme}://{host}"


def public_base(origin: str) -> str:
    if FNOS_PUBLIC:
        return FNOS_PUBLIC
    parsed = urllib.parse.urlparse(origin)
    host = parsed.hostname or "127.0.0.1"
    # 页面走标准端口时，认为 fnOS 也挂在同一个反向代理后面
    if parsed.port in (None, 80, 443):
        return f"{parsed.scheme}://{host}"
    if parsed.scheme == "https":
        return f"https://{host}:5667"
    return f"http://{host}:5666"


def callback_url(origin: str) -> str:
    return origin.rstrip("/") + "/auth/callback"


def signin_url(origin: str, callback: str, state: str, challenge: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": callback,
        "response_type": "code",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "scope": SCOPE,
        "device_id": f"disk-health-{secrets.token_hex(4)}",
        "device_name": "RAID 阵列健康",
        "device_model": "Web",
    }
    return public_base(origin) + "/signin?" + urllib.parse.urlencode(params)


def manual_signin_url(origin: str) -> tuple[str, str]:
    """无 redirect_uri 的公共客户端流程，用于排障/手动输入授权码。"""
    verifier, challenge = _pkce()
    params = {
        "client_id": CLIENT_ID,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "device_id": f"disk-health-{secrets.token_hex(4)}",
        "device_name": "RAID 阵列健康",
        "device_model": "Web",
    }
    return public_base(origin) + "/signin?" + urllib.parse.urlencode(params), verifier


def manual_page(signin_url: str) -> bytes:
    from html import escape

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>使用 fnOS 授权码登录</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:
    radial-gradient(900px 500px at 20% 0%, rgba(56,189,248,.12), transparent 60%), #060a12;
    color:#e8eef8; font-family:"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif; }}
  .card {{ width:min(560px, calc(100vw - 40px)); background:rgba(13,21,36,.92); border:1px solid #1c2a42;
    border-radius:16px; padding:28px; box-shadow:0 24px 80px rgba(0,0,0,.45); }}
  h1 {{ font-size:19px; margin:0 0 8px; }}
  p {{ color:#9aa9c0; font-size:13px; line-height:1.75; margin:0 0 10px; }}
  ol {{ color:#9aa9c0; font-size:13px; line-height:1.9; padding-left:20px; }}
  a.btn {{ display:inline-flex; padding:9px 14px; border-radius:9px; text-decoration:none; font-size:13px;
    background:linear-gradient(180deg,#1c9fd0,#0f8ab8); border:1px solid #2cb3e4; color:#03121c; font-weight:650; }}
  form {{ display:flex; gap:8px; margin-top:18px; }}
  input {{ flex:1; min-width:0; background:#0a1322; border:1px solid #1c2a42; border-radius:8px;
    height:38px; padding:0 11px; color:#e8eef8; outline:none; }}
  input:focus {{ border-color:#38bdf8; }}
  button {{ padding:0 16px; border-radius:8px; border:1px solid #2a405f; background:#101b2e; color:#cfe3ff; font-size:13px; }}
  .foot {{ margin-top:16px; font-size:11.5px; color:#5f6f88; }}
</style></head><body>
<div class="card">
  <h1>使用 fnOS 授权码登录</h1>
  <p>用于自动回跳不可用的场景。授权码由 fnOS 登录后显示，一次性使用，5 分钟内有效。</p>
  <ol>
    <li>点击下方按钮，在 fnOS 页面完成登录 / 授权，页面会显示一串授权码。</li>
    <li>复制授权码，粘贴到下面提交。</li>
  </ol>
  <p><a class="btn" href="{escape(signin_url)}" target="_blank" rel="noopener">打开 fnOS 授权页</a></p>
  <form method="post" action="/login/code">
    <input name="code" placeholder="粘贴授权码" autocomplete="off" required>
    <button type="submit">登录</button>
  </form>
  <p class="foot">授权码只在本次请求中使用，不会写入日志；登录成功后回到监控页。</p>
</div>
</body></html>""".encode("utf-8")


def error_page(title: str, message: str, detail: str = "", retry: str = "/", fnos_base: str = "http://127.0.0.1:5666") -> bytes:
    from html import escape

    detail_html = f'<pre>{escape(detail)}</pre>' if detail else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:
    radial-gradient(900px 500px at 20% 0%, rgba(56,189,248,.12), transparent 60%), #060a12;
    color:#e8eef8; font-family:"Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif; }}
  .card {{ width:min(520px, calc(100vw - 40px)); background:rgba(13,21,36,.92); border:1px solid #1c2a42;
    border-radius:16px; padding:28px 28px 24px; box-shadow:0 24px 80px rgba(0,0,0,.45); }}
  .icon {{ width:44px;height:44px;border-radius:12px;display:grid;place-items:center;background:rgba(251,113,133,.12);
    border:1px solid rgba(251,113,133,.35);color:#fb7185;font-size:22px;margin-bottom:14px; }}
  h1 {{ font-size:19px; margin:0 0 8px; }}
  p {{ color:#9aa9c0; font-size:13px; line-height:1.7; margin:0 0 6px; }}
  pre {{ margin:14px 0 0; padding:10px 12px; background:#060b14; border:1px solid #162236; border-radius:8px;
    color:#8494ad; font-size:11px; white-space:pre-wrap; word-break:break-all; }}
  .actions {{ display:flex; gap:10px; margin-top:20px; flex-wrap:wrap; }}
  a {{ display:inline-flex; align-items:center; gap:6px; padding:9px 14px; border-radius:9px; text-decoration:none;
    font-size:13px; border:1px solid #2a405f; color:#cfe3ff; background:#101b2e; }}
  a.primary {{ background:linear-gradient(180deg,#1c9fd0,#0f8ab8); border-color:#2cb3e4; color:#03121c; font-weight:650; }}
</style></head><body>
<div class="card">
  <div class="icon">!</div>
  <h1>{escape(title)}</h1>
  <p>{escape(message)}</p>
  {detail_html}
  <div class="actions">
    <a class="primary" href="{escape(retry)}">重新登录</a>
    <a href="/login/code">使用授权码登录</a>
    <a href="{escape(fnos_base)}/" target="_blank" rel="noopener">打开 fnOS 首页</a>
  </div>
</div>
</body></html>""".encode("utf-8")
