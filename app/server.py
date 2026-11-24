"""HTTP 服务：fnOS 登录鉴权 + 静态页面 + JSON API + 日志导出。"""

from __future__ import annotations

import json
import os
import posixpath
import socketserver
import secrets
import ssl
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import ai
import auth
import export
import history
import smart
from collector import Monitor
from util import decode_header_text

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
LICENSE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "LICENSE")
PORT = int(os.environ.get("PORT", "8080"))
GATEWAY_SOCKET = os.environ.get("DISKHEALTH_GATEWAY_SOCKET", "").strip()
APP_BASE_PATH = os.environ.get("APP_BASE_PATH", "").strip().rstrip("/")
AUTH_MODE = os.environ.get("AUTH_MODE", "cookie").strip().lower()
GATEWAY_MODE = AUTH_MODE == "gateway" or bool(GATEWAY_SOCKET)
APP_VERSION = os.environ.get("APP_VERSION", "1.10.12").strip() or "1.10.12"
APP_BUILD = os.environ.get("APP_BUILD", "2026-11-23").strip() or "2026-11-23"
APP_NAME = "RAID 阵列健康"
APP_SUBTITLE = "HARDWARE RAID・ARRAY MONITOR"
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; base-uri 'self'; frame-ancestors "
    + ("'self'" if GATEWAY_MODE else "'none'")
)
FRAME_OPTIONS = "SAMEORIGIN" if GATEWAY_MODE else "DENY"

monitor = Monitor()


class Handler(BaseHTTPRequestHandler):
    server_version = "DiskHealth/1.1"
    protocol_version = "HTTP/1.1"

    # ---------- 基础输出 ----------
    def _init_response(self) -> None:
        self._cookies: list[str] = []
        self.user: dict | None = None

    def _queue_cookie(self, value: str) -> None:
        self._cookies.append(value)

    def _headers(self, status: int, content_type: str, length: int, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", FRAME_OPTIONS)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        for cookie in self._cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _bytes(self, body: bytes, content_type: str, status: int = 200, extra: dict[str, str] | None = None) -> None:
        self._headers(status, content_type, len(body), extra)
        self.wfile.write(body)

    def _send_html(self, body: bytes, status: int = 200) -> None:
        self._bytes(body, "text/html; charset=utf-8", status)

    def _redirect(self, location: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", FRAME_OPTIONS)
        self.send_header("Referrer-Policy", "no-referrer")
        for cookie in self._cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    # ---------- 登录鉴权 ----------
    def _origin(self) -> str:
        proto = self.headers.get("X-Forwarded-Proto")
        if not proto:
            proto = "https" if isinstance(self.connection, ssl.SSLSocket) else "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
        return auth.request_origin(host, proto)

    def _secure(self) -> bool:
        return self._origin().startswith("https://")

    def _error_html(self, title: str, message: str, detail: str = "", status: int = 502) -> None:
        self._send_html(
            auth.error_page(
                title,
                message,
                detail=detail,
                retry="/",
                fnos_base=auth.public_base(self._origin()),
            ),
            status,
        )

    def _route_path(self, parsed) -> str | None:
        path = unquote(parsed.path)
        if APP_BASE_PATH:
            if path == APP_BASE_PATH:
                suffix = ("?" + parsed.query) if parsed.query else ""
                self._redirect(APP_BASE_PATH + "/" + suffix)
                return None
            if path.startswith(APP_BASE_PATH + "/"):
                path = path[len(APP_BASE_PATH):]
        return path

    def _gateway_user(self) -> dict | None:
        uid = (self.headers.get("X-Trim-Userid") or "").strip()
        if not uid:
            return None
        username = decode_header_text(self.headers.get("X-Trim-Username"))
        return {
            "uid": uid,
            "username": username or f"用户 {uid}",
            "admin": (self.headers.get("X-Trim-Isadmin") or "").strip().lower() == "true",
            "exp": None,
        }

    def _require_admin(self) -> dict | None:
        user = self.user or {}
        if not bool(user.get("admin")):
            self._json({"error": "需要 fnOS 管理员权限"}, HTTPStatus.FORBIDDEN)
            return None
        return user

    def _can_access_report(self, report: dict | None) -> bool:
        if not report:
            return False
        user = self.user or {}
        if bool(user.get("admin")):
            return True
        return str(report.get("uid")) == str(user.get("uid"))

    def _require_user(self, api: bool) -> dict | None:
        if GATEWAY_MODE:
            user = self._gateway_user()
            if user:
                self.user = user
                return user
            if api:
                self._json({"error": "unauthorized", "message": "统一网关未提供登录身份"}, HTTPStatus.UNAUTHORIZED)
            else:
                self._error_html("未获取到 fnOS 登录身份", "请求未经过 fnOS 统一网关，或登录态已失效。", status=401)
            return None
        cookie = self.headers.get("Cookie")
        session = auth.read_session(cookie)
        if session:
            self.user = session
            return session
        try:
            user = auth.login_with_cookie(cookie)
        except auth.AuthError as exc:
            if api:
                self._json({"error": exc.message, "detail": exc.detail}, exc.status)
            else:
                self._error_html("登录校验失败", exc.message, exc.detail, exc.status)
            return None
        if user:
            token = auth.issue_session(user)
            self._queue_cookie(
                auth.cookie_header(auth.SESSION_COOKIE, token, auth.SESSION_TTL, secure=self._secure())
            )
            self.user = auth.read_session(
                f"{auth.SESSION_COOKIE}={token}"
            ) or {"uid": user.get("uid"), "username": user.get("username"), "admin": user.get("admin")}
            return self.user
        if api:
            self._json(
                {"error": "unauthorized", "message": "登录已过期，请重新登录", "login_url": "/"},
                HTTPStatus.UNAUTHORIZED,
            )
            return None
        self._start_login()
        return None

    def _start_login(self) -> None:
        origin = self._origin()
        callback = auth.callback_url(origin)
        verifier, challenge = auth._pkce()
        state = secrets.token_urlsafe(24)
        state_cookie = auth.issue_state(verifier, callback, state)
        self._queue_cookie(
            auth.cookie_header(auth.STATE_COOKIE, state_cookie, 600, secure=self._secure())
        )
        self._redirect(auth.signin_url(origin, callback, state, challenge))

    def _manual_login_page(self) -> None:
        if auth.read_session(self.headers.get("Cookie")):
            return self._redirect("/")
        origin = self._origin()
        url, verifier = auth.manual_signin_url(origin)
        state = secrets.token_urlsafe(24)
        state_cookie = auth.issue_state(verifier, "", state)
        self._queue_cookie(auth.cookie_header(auth.STATE_COOKIE, state_cookie, 600, secure=self._secure()))
        self._send_html(auth.manual_page(url))

    def _manual_login_submit(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            return self._error_html("提交内容无效", "请返回重新输入授权码。", status=400)
        body = self.rfile.read(length).decode("utf-8", "replace")
        code = (parse_qs(body).get("code") or [""])[0].strip()
        if not code:
            return self._error_html("缺少授权码", "请返回粘贴 fnOS 显示的授权码。", status=400)
        state_cookie = auth.read_state(self.headers.get("Cookie"))
        if not state_cookie:
            return self._error_html("登录会话已过期", "请重新打开授权码登录页。", status=400)
        try:
            user = auth.exchange_callback(code, str(state_cookie.get("verifier") or ""), "")
        except auth.AuthError as exc:
            return self._error_html("授权码登录失败", exc.message, exc.detail, exc.status)
        token = auth.issue_session(user)
        self._queue_cookie(auth.cookie_header(auth.SESSION_COOKIE, token, auth.SESSION_TTL, secure=self._secure()))
        self._queue_cookie(auth.cookie_header(auth.STATE_COOKIE, "", 0, secure=self._secure()))
        self._redirect("/")

    def _clear_cookies(self) -> None:
        self._queue_cookie(auth.cookie_header(auth.SESSION_COOKIE, "", 0, secure=self._secure()))
        self._queue_cookie(auth.cookie_header(auth.STATE_COOKIE, "", 0, secure=self._secure()))

    def _callback(self, query: dict[str, list[str]]) -> None:
        if query.get("error"):
            self._error_html(
                "fnOS 登录未完成",
                "授权过程返回错误。",
                detail=str(query.get("error_description") or query["error"][0]),
                status=400,
            )
            return
        state_cookie = auth.read_state(self.headers.get("Cookie"))
        if not state_cookie:
            self._error_html("登录会话已过期", "请重新发起登录。", status=400)
            return
        state = (query.get("state") or [""])[0]
        if not secrets.compare_digest(str(state_cookie.get("state") or ""), str(state)):
            self._error_html("登录状态校验失败", "state 不匹配，请重新发起登录。", status=400)
            return
        code = (query.get("code") or [""])[0]
        if not code:
            self._error_html("缺少授权码", "fnOS 未返回授权码，请重新登录。", status=400)
            return
        try:
            user = auth.exchange_callback(code, str(state_cookie.get("verifier") or ""), str(state_cookie.get("redirect_uri") or ""))
        except auth.AuthError as exc:
            self._error_html("登录失败", exc.message, exc.detail, exc.status)
            return
        token = auth.issue_session(user)
        self._queue_cookie(auth.cookie_header(auth.SESSION_COOKIE, token, auth.SESSION_TTL, secure=self._secure()))
        self._queue_cookie(auth.cookie_header(auth.STATE_COOKIE, "", 0, secure=self._secure()))
        self._redirect("/")

    def _json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 1_000_000:
            raise ai.AIError("请求体为空或过大", 400)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ai.AIError("请求 JSON 格式错误", 400, str(exc)) from exc
        if not isinstance(data, dict):
            raise ai.AIError("请求 JSON 必须是对象", 400)
        return data

    # ---------- 路由 ----------
    def do_GET(self) -> None:  # noqa: N802
        self._init_response()
        parsed = urlparse(self.path)
        path = self._route_path(parsed)
        if path is None:
            return
        query = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                return self._json({"status": "ok", "service": "disk-health"})
            if path == "/auth/callback":
                return self._callback(query)
            if path == "/login/code":
                return self._manual_login_page()
            if path.startswith("/static/"):
                return self._static(posixpath.basename(path))
            if path in ("/", "/index.html"):
                if self._require_user(api=False) is None:
                    return
                return self._static("index.html")
            if path in ("/donate", "/donation", "/donate.html", "/pricing", "/pricing.html"):
                if self._require_user(api=False) is None:
                    return
                return self._static("donate.html")

            if self._require_user(api=path.startswith("/api/")) is None:
                return

            if path == "/api/status":
                return self._json(monitor.status())
            if path == "/api/license":
                try:
                    with open(LICENSE_PATH, "rb") as fh:
                        body = fh.read()
                except OSError:
                    return self._error_html("许可证文件缺失", "未找到 LICENSE 文件", "", 404)
                return self._bytes(
                    body,
                    "text/plain; charset=utf-8",
                    extra={"Content-Disposition": 'inline; filename="LICENSE"'},
                )
            if path == "/api/session":
                user = self.user or {}
                return self._json(
                    {
                        "user": {
                            "uid": user.get("uid"),
                            "username": user.get("username") or "",
                            "admin": bool(user.get("admin")),
                        },
                        "mode": "gateway" if GATEWAY_MODE else "standalone",
                        "can_logout": not GATEWAY_MODE,
                        "expires_at": user.get("exp"),
                        "app": {
                            "name": APP_NAME,
                            "subtitle": APP_SUBTITLE,
                            "version": APP_VERSION,
                            "build": APP_BUILD,
                            "copyright": "© 2026 cyberzero · 保留所有权利",
                            "license": "GPL-3.0",
                            "tagline": "洞察每一块磁盘 / Every Disk, Clearly Seen",
                        },
                    }
                )
            if path == "/api/topology":
                snapshot = monitor.get_snapshot() or {}
                return self._json(snapshot.get("topology") or {"controllers": [], "enclosures": 0, "expanders": 0, "disks": 0})
            if path == "/api/history/disks":
                days = int((query.get("days") or ["30"])[0] or 30)
                return self._json(
                    {
                        "days": max(1, min(365, days)),
                        "disks": history.disks(days),
                        "metrics": history.metrics(),
                    }
                )
            if path == "/api/history/series":
                disk_id = (query.get("disk_id") or [""])[0]
                if not disk_id:
                    return self._json({"error": "缺少 disk_id"}, 400)
                days = int((query.get("days") or ["30"])[0] or 30)
                metric = (query.get("metric") or ["temperature_c"])[0]
                max_points = int((query.get("max_points") or ["720"])[0] or 720)
                return self._json(history.series(disk_id, metric, days, max_points))
            if path == "/api/history/compare":
                try:
                    days = int((query.get("days") or ["30"])[0] or 30)
                except ValueError:
                    days = 30
                return self._json(history.compare(days))
            if path == "/api/history/export":
                disk_id = (query.get("disk_id") or [""])[0]
                if not disk_id:
                    return self._json({"error": "缺少 disk_id"}, 400)
                days = int((query.get("days") or ["30"])[0] or 30)
                metric = (query.get("metric") or ["temperature_c"])[0]
                body, filename = history.export_csv(disk_id, metric, days)
                return self._bytes(
                    body,
                    "text/csv; charset=utf-8",
                    extra={"Content-Disposition": f'attachment; filename="{filename}"'},
                )
            if path == "/api/ai/status":
                return self._json(ai.status())
            if path == "/api/admin/ai":
                if self._require_admin() is None:
                    return
                return self._json(ai.public_config())
            if path == "/api/admin/db":
                if self._require_admin() is None:
                    return
                return self._json(history.info())
            if path == "/api/ai/reports":
                user = self.user or {}
                reports = ai.list_reports(uid=str(user.get("uid")), is_admin=bool(user.get("admin")))
                return self._json({"reports": reports})
            if path == "/api/ai/report":
                report = ai.get_report((query.get("id") or [""])[0])
                if not report:
                    return self._json({"error": "报告不存在"}, 404)
                if not self._can_access_report(report):
                    return self._json({"error": "无权查看该报告"}, 403)
                return self._json({"report": report})
            if path == "/api/ai/export":
                return self._api_ai_export(query)
            if path == "/api/disks":
                return self._api_disks(query)
            if path == "/api/refresh":
                monitor.request_refresh()
                return self._json({"ok": True, "message": "已触发刷新"})
            if path == "/api/events":
                limit = min(800, int((query.get("limit") or ["200"])[0] or 200))
                return self._json({"events": list(monitor.events)[-limit:]})
            if path == "/api/export":
                return self._api_export(query)
            if path.startswith("/api/disk"):
                return self._api_disk(query)
            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            return
        except Exception as exc:  # pragma: no cover - 防御
            sys.stderr.write(f"[http] {path} 处理失败: {exc}\n")
            try:
                self._json({"error": "internal error"}, 500)
            except Exception:
                pass

    def do_POST(self) -> None:  # noqa: N802
        self._init_response()
        parsed = urlparse(self.path)
        path = self._route_path(parsed)
        if path is None:
            return
        try:
            if path == "/login/code":
                return self._manual_login_submit()
            if path == "/api/admin/db":
                if self._require_user(api=True) is None:
                    return
                if self._require_admin() is None:
                    return
                try:
                    body = self._json_body()
                    return self._json(history.set_directory(str(body.get("directory") or "")))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if path.startswith("/api/admin/ai"):
                if self._require_user(api=True) is None:
                    return
                if self._require_admin() is None:
                    return
                try:
                    if path == "/api/admin/ai":
                        return self._json(ai.save_config(self._json_body()))
                    if path == "/api/admin/ai/test":
                        return self._json(ai.test_connection())
                except ai.AIError as exc:
                    return self._json({"error": exc.message, "detail": exc.detail}, exc.status)
            if path == "/api/ai/analyze":
                if self._require_user(api=True) is None:
                    return
                try:
                    snapshot = monitor.get_snapshot()
                    if not snapshot:
                        return self._json({"error": "数据尚未就绪"}, 503)
                    body = self._json_body()
                    scope = str(body.get("scope") or "issues")
                    ids = body.get("ids") if isinstance(body.get("ids"), list) else []
                    report = ai.analyze(
                        snapshot,
                        ai.load_config(),
                        scope,
                        [str(item) for item in ids],
                        list(monitor.events),
                        user=self.user,
                    )
                    return self._json({"ok": True, "report": report})
                except ai.AIError as exc:
                    return self._json({"error": exc.message, "detail": exc.detail}, exc.status)
            if path == "/api/logout":
                self._clear_cookies()
                return self._json({"ok": True})
            if path != "/api/refresh":
                return self._json({"error": "not found"}, 404)
            if self._require_user(api=True) is None:
                return
            monitor.request_refresh()
            return self._json({"ok": True, "message": "已触发刷新"})
        except BrokenPipeError:
            return
        except Exception as exc:  # pragma: no cover
            sys.stderr.write(f"[http] {path} 处理失败: {exc}\n")
            try:
                self._json({"error": "internal error"}, 500)
            except Exception:
                pass

    # ---------- API ----------
    def _api_ai_export(self, query: dict[str, list[str]]) -> None:
        report = ai.get_report((query.get("id") or [""])[0])
        if not report:
            return self._json({"error": "报告不存在"}, 404)
        if not self._can_access_report(report):
            return self._json({"error": "无权导出该报告"}, 403)
        body, content_type, filename = ai.export_report(report, (query.get("format") or ["md"])[0])
        self._bytes(body, content_type, extra={"Content-Disposition": f'attachment; filename="{filename}"'})

    def _api_disks(self, query: dict[str, list[str]]) -> None:
        snapshot = monitor.get_snapshot()
        if snapshot is None:
            if monitor.last_error:
                return self._json({"error": monitor.last_error, "collecting": False}, 503)
            return self._json(
                {"collecting": True, "message": "正在首次采集物理磁盘与 SMART 数据，请稍候…"},
                HTTPStatus.ACCEPTED,
            )
        if query.get("refresh") == ["1"]:
            monitor.request_refresh()
        data = dict(snapshot)
        data["collecting"] = monitor.collecting
        data["last_error"] = monitor.last_error
        self._json(data)

    def _api_export(self, query: dict[str, list[str]]) -> None:
        snapshot = monitor.get_snapshot()
        if snapshot is None:
            return self._json({"error": "数据尚未就绪"}, 503)
        fmt = (query.get("format") or ["txt"])[0]
        if fmt not in ("csv", "json", "txt"):
            return self._json({"error": "format 仅支持 csv/json/txt"}, 400)
        body, content_type, filename = export.export(
            snapshot,
            list(monitor.events),
            fmt,
            controller=(query.get("controller") or [""])[0],
            bay=(query.get("bay") or [""])[0],
            status=(query.get("health") or [""])[0],
            query=(query.get("q") or [""])[0],
        )
        self._bytes(body, content_type, extra={"Content-Disposition": f'attachment; filename="{filename}"'})

    def _api_disk(self, query: dict[str, list[str]]) -> None:
        disk_id = (query.get("id") or [""])[0]
        snapshot = monitor.get_snapshot()
        if not snapshot:
            return self._json({"error": "数据尚未就绪"}, 503)
        disk = next((d for d in snapshot.get("disks") or [] if d.get("id") == disk_id), None)
        if disk is None:
            return self._json({"error": "未找到该磁盘"}, 404)
        if disk.get("kind") == "raid":
            index = int("".join(ch for ch in str(disk.get("controller_id")) if ch.isdigit()) or 0)
            raw = smart.read_raw(f"/dev/bus/{index}", f"megaraid,{disk.get('did')}")
        else:
            raw = smart.read_raw(disk.get("device") or "", disk.get("device_type") or None)
        self._json({"disk": disk, "raw": raw})

    # ---------- 静态文件 ----------
    def _static(self, name: str) -> None:
        safe = posixpath.basename(name)
        path = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        with open(path, "rb") as fh:
            body = fh.read()
        if safe in ("index.html", "donate.html") and APP_BASE_PATH:
            body = body.replace(b'<base href="/">', f'<base href="{APP_BASE_PATH}/">'.encode("utf-8"), 1)
        ext = os.path.splitext(safe)[1].lower()
        self._bytes(body, MIME.get(ext, "application/octet-stream"))

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        line = fmt % args
        if "/api/" in line or "/auth/" in line or "code=" in line:
            return
        sys.stderr.write("[http] %s\n" % line)


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    history.load_config()
    monitor.start()
    if GATEWAY_SOCKET:
        os.makedirs(os.path.dirname(os.path.abspath(GATEWAY_SOCKET)), exist_ok=True)
        try:
            os.unlink(GATEWAY_SOCKET)
        except FileNotFoundError:
            pass
        server = ThreadingUnixServer(GATEWAY_SOCKET, Handler)
        try:
            os.chmod(GATEWAY_SOCKET, 0o666)
        except OSError:
            pass
        sys.stderr.write(f"[disk-health] listening on unix:{GATEWAY_SOCKET}\n")
    else:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        sys.stderr.write(f"[disk-health] listening on 0.0.0.0:{PORT}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if GATEWAY_SOCKET:
            try:
                os.unlink(GATEWAY_SOCKET)
            except OSError:
                pass


if __name__ == "__main__":
    main()
