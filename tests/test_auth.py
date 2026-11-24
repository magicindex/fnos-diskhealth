"""fnOS 登录鉴权单元测试（不发真实网络请求）。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

import auth  # noqa: E402
from util import decode_header_text, normalize_text  # noqa: E402


class AuthBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        auth.DATA_DIR = self.tmp.name
        auth._secret_cache = None
        auth._login_cache.clear()

    def tearDown(self) -> None:
        auth._login_cache.clear()
        self.tmp.cleanup()


class TextEncodingTest(AuthBase):
    def test_latin1_mojibake(self) -> None:
        raw = "张三".encode("utf-8").decode("latin-1")
        self.assertEqual(normalize_text(raw), "张三")
        self.assertEqual(decode_header_text(raw), "张三")

    def test_percent_encoded(self) -> None:
        self.assertEqual(normalize_text("%E5%BC%A0%E4%B8%89"), "张三")

    def test_nfc(self) -> None:
        self.assertEqual(normalize_text("e\u0301"), "é")

    def test_ascii_untouched(self) -> None:
        self.assertEqual(normalize_text("SEAGATE ST2000NX0463"), "SEAGATE ST2000NX0463")


class CookieTest(AuthBase):
    def test_token_cookie_prefers_legacy_name(self) -> None:
        tokens = auth.fnos_tokens("fnos-token=abc; ost=xyz; osrt=long")
        self.assertEqual(tokens["token"], "abc")
        self.assertEqual(tokens["long_token"], "long")

    def test_token_cookie_new_name(self) -> None:
        tokens = auth.fnos_tokens("ost=xyz")
        self.assertEqual(tokens["token"], "xyz")
        self.assertEqual(tokens["token_name"], "ost")

    def test_no_token(self) -> None:
        self.assertEqual(auth.fnos_tokens("other=1"), {})

    def test_parse_cookie_quoted(self) -> None:
        cookies = auth.parse_cookies('a="b c"; d=1')
        self.assertEqual(cookies["a"], "b c")
        self.assertEqual(cookies["d"], "1")


class PkceTest(AuthBase):
    def test_challenge_matches_verifier(self) -> None:
        verifier, challenge = auth._pkce()
        import base64, hashlib

        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(challenge, expected)
        self.assertGreaterEqual(len(verifier), 43)


class SessionTest(AuthBase):
    def test_roundtrip(self) -> None:
        token = auth.issue_session({"uid": 1000, "username": "admin", "admin": True})
        user = auth.read_session(f"dh_auth={token}")
        self.assertEqual(user["uid"], 1000)
        self.assertEqual(user["username"], "admin")
        self.assertTrue(user["admin"])

    def test_tampered(self) -> None:
        token = auth.issue_session({"uid": 1000, "username": "admin", "admin": False})
        body, sig = token.split(".")
        tampered = body[:-2] + ("AA" if not body.endswith("AA") else "BB") + "." + sig
        self.assertIsNone(auth.read_session(f"dh_auth={tampered}"))

    def test_expired(self) -> None:
        payload = {"p": "session", "uid": 1, "username": "x", "admin": False,
                   "iat": int(time.time()) - 100, "exp": int(time.time()) - 1}
        token = auth._sign(payload, auth.secret())
        self.assertIsNone(auth.read_session(f"dh_auth={token}"))

    def test_state_roundtrip(self) -> None:
        value = auth.issue_state("verifier", "http://host/cb", "state123")
        state = auth.read_state(f"dh_oauth={value}")
        self.assertEqual(state["verifier"], "verifier")
        self.assertEqual(state["state"], "state123")
        self.assertEqual(state["redirect_uri"], "http://host/cb")

    def test_state_wrong_purpose(self) -> None:
        token = auth.issue_session({"uid": 1, "username": "x", "admin": False})
        self.assertIsNone(auth.read_state(f"dh_oauth={token}"))


class UserParseTest(AuthBase):
    def test_variants(self) -> None:
        self.assertEqual(auth._parse_user({"uid": 7, "username": "bob", "isAdmin": True}),
                         {"uid": 7, "username": "bob", "admin": True})
        nested = auth._parse_user({"user": {"userId": 8, "userName": "alice", "admin": False}})
        self.assertEqual(nested["uid"], 8)
        self.assertEqual(nested["username"], "alice")


class LoginFlowTest(AuthBase):
    def test_userinfo_cookie_success(self) -> None:
        with mock.patch.object(auth, "_http", return_value=(200, {"code": 0, "data": {"uid": 1000, "username": "admin"}})) as http:
            user = auth.login_with_cookie("fnos-token=good")
        self.assertEqual(user["uid"], 1000)
        self.assertEqual(user["username"], "admin")
        self.assertEqual(http.call_count, 1)

    def test_authorize_fallback(self) -> None:
        responses = [
            (200, {"code": 12002, "msg": "authentication failed"}),  # userinfo cookie
            (200, {"code": 0, "data": {"code": "CODE"}}),             # authorize
            (200, {"code": 0, "data": {"access_token": "AT", "uid": 1001, "admin": True}}),  # exchange
            (200, {"code": 0, "data": {"uid": 1001, "username": "bob"}}),  # userinfo bearer
        ]
        with mock.patch.object(auth, "_http", side_effect=responses):
            user = auth.login_with_cookie("fnos-token=good")
        self.assertEqual(user["uid"], 1001)
        self.assertEqual(user["username"], "bob")
        self.assertTrue(user["admin"])

    def test_invalid_token_returns_none(self) -> None:
        responses = [
            (200, {"code": 12002, "msg": "authentication failed"}),
            (200, {"code": 10003, "msg": "user authorization not obtained"}),
        ]
        with mock.patch.object(auth, "_http", side_effect=responses):
            self.assertIsNone(auth.login_with_cookie("fnos-token=bad"))

    def test_exchange_failure_raises(self) -> None:
        responses = [
            (200, {"code": 12002, "msg": "authentication failed"}),
            (200, {"code": 0, "data": {"code": "CODE"}}),
            (200, {"code": 11004, "msg": "invalid code"}),
        ]
        with mock.patch.object(auth, "_http", side_effect=responses):
            with self.assertRaises(auth.AuthError):
                auth.login_with_cookie("fnos-token=good")

    def test_no_token_skips_network(self) -> None:
        with mock.patch.object(auth, "_http") as http:
            self.assertIsNone(auth.login_with_cookie("other=1"))
        http.assert_not_called()


class UrlTest(AuthBase):
    def test_signin_url(self) -> None:
        url = auth.signin_url("http://192.168.1.100:18089", "http://192.168.1.100:18089/auth/callback", "st", "ch")
        self.assertIn("http://192.168.1.100:5666/signin?", url)
        self.assertIn("client_id=YJNMPJUGA9", url)
        self.assertIn("redirect_uri=http%3A%2F%2F192.168.1.100%3A18089%2Fauth%2Fcallback", url)
        self.assertIn("code_challenge_method=S256", url)

    def test_origin(self) -> None:
        self.assertEqual(auth.request_origin("nas.local:18089", "https"), "https://nas.local:18089")
        self.assertEqual(auth.request_origin("bad/header", None), "http://127.0.0.1")

    def test_error_page_escapes(self) -> None:
        body = auth.error_page("<x>", "msg", "<script>alert(1)</script>").decode()
        self.assertIn("&lt;x&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)


if __name__ == "__main__":
    unittest.main()
