"""Regression tests for the browser request-origin guard.

The production module has heavyweight startup side effects, so these tests
extract only the small request-security helpers from the active source file.
"""

import ipaddress
from pathlib import Path
import types
import unittest
from copy import deepcopy
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")


class HTTPException(Exception):
    def __init__(self, *, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Request:
    pass


class FakeRequest:
    def __init__(self, *, method="POST", headers=None, client_ip="203.0.113.44", cookies=None):
        self.method = method
        self.headers = dict(headers or {})
        self.cookies = dict(cookies or {"mail-session": "active"})
        self.client = types.SimpleNamespace(host=client_ip)
        self.url = types.SimpleNamespace(scheme="https", netloc="mail.example.com")


def _extract_function(name: str) -> str:
    start = SOURCE.index(f"def {name}(")
    ends = [
        SOURCE.find(marker, start + 1)
        for marker in ("\n\ndef ", "\n\nclass ", "\n\n@app", "\n\n# ")
    ]
    end = min(position for position in ends if position != -1)
    return SOURCE[start:end]


def _load_security_helpers():
    namespace = {
        "Any": Any,
        "Dict": Dict,
        "Optional": Optional,
        "HTTPException": HTTPException,
        "Request": Request,
        "PORTAL_PROFILE_AVATAR_MAX_CHARS": 1024,
        "deepcopy": deepcopy,
        "ipaddress": ipaddress,
        "log": types.SimpleNamespace(warning=lambda *_args: None),
        "types": types,
        "CFG": types.SimpleNamespace(SESSION_COOKIE="mail-session"),
        "session_get": lambda _token: {"email": "operator@example.com"},
    }
    for name in (
        "_sanitize_portal_profile",
        "_mail_profile_snapshot",
        "_client_ip",
        "_request_is_https",
        "_request_host",
        "_is_internal_client_ip",
        "_is_same_origin_admin_request",
        "get_session_or_401",
    ):
        exec(compile(_extract_function(name), str(ROOT / "rupochta_server.py"), "exec"), namespace)
    return namespace


class RequestSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = _load_security_helpers()
        cls._mail_profile_snapshot = staticmethod(cls.namespace["_mail_profile_snapshot"])
        cls._same_origin = staticmethod(cls.namespace["_is_same_origin_admin_request"])
        cls._session_guard = staticmethod(cls.namespace["get_session_or_401"])

    @staticmethod
    def request(*, method="POST", origin=None, referer=None, client_ip="203.0.113.44"):
        headers = {
            "host": "mail.example.com",
            "x-forwarded-proto": "https",
        }
        if origin is not None:
            headers["origin"] = origin
        if referer is not None:
            headers["referer"] = referer
        return FakeRequest(method=method, headers=headers, client_ip=client_ip)

    def test_same_origin_origin_and_referer_are_allowed(self):
        self.assertTrue(self._same_origin(self.request(origin="https://mail.example.com")))
        self.assertTrue(self._same_origin(self.request(referer="https://mail.example.com/inbox")))

    def test_cross_site_and_headerless_public_requests_are_rejected(self):
        self.assertFalse(self._same_origin(self.request(origin="https://example.invalid")))
        self.assertFalse(self._same_origin(self.request()))

    def test_headerless_internal_maintenance_request_is_allowed(self):
        self.assertTrue(self._same_origin(self.request(client_ip="10.40.50.5")))

    def test_session_guard_blocks_cross_site_mutations(self):
        request = self.request(origin="https://example.invalid")
        with self.assertRaises(HTTPException) as raised:
            self._session_guard(request)
        self.assertEqual(raised.exception.status_code, 403)

    def test_session_guard_keeps_same_origin_and_reads_working(self):
        same_origin = self.request(origin="https://mail.example.com")
        self.assertEqual(self._session_guard(same_origin)["email"], "operator@example.com")
        cross_origin_read = self.request(method="GET", origin="https://example.invalid")
        self.assertEqual(self._session_guard(cross_origin_read)["email"], "operator@example.com")

    def test_session_guard_rejects_missing_session_before_origin_check(self):
        self.namespace["session_get"] = lambda _token: None
        try:
            with self.assertRaises(HTTPException) as raised:
                self._session_guard(self.request(origin="https://mail.example.com"))
            self.assertEqual(raised.exception.status_code, 401)
        finally:
            self.namespace["session_get"] = lambda _token: {"email": "operator@example.com"}

    def test_logout_has_an_explicit_same_origin_guard(self):
        marker = '@app.post("/api/auth/logout")'
        start = SOURCE.index(marker)
        end = SOURCE.index("\n\ndef _mailbox_context_for_request", start)
        self.assertIn("if not _is_same_origin_admin_request(request):", SOURCE[start:end])

    def test_mail_profile_snapshot_exposes_only_name_and_avatar(self):
        profile = {
            "employee": {
                "employeeId": 17,
                "adLogin": "operator",
                "fullName": "  Иван Иванов  ",
                "avatarDataUrl": "data:image/webp;base64,AAAA",
                "role": "administrator",
            },
            "integrations": {"telegram": {"linked": True}},
        }
        self.assertEqual(
            self._mail_profile_snapshot(profile),
            {
                "employee": {
                    "fullName": "Иван Иванов",
                    "avatarDataUrl": "data:image/webp;base64,AAAA",
                }
            },
        )
        self.assertEqual(profile["employee"]["adLogin"], "operator")


if __name__ == "__main__":
    unittest.main()
