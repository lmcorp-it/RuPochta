"""Focused checks for the Mail to canonical-account trust boundary."""

import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch
import urllib
from typing import Any, Dict, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")


def _extract_function(name: str) -> str:
    start = SOURCE.index(f"def {name}(")
    ends = [
        SOURCE.find(marker, start + 1)
        for marker in ("\n\ndef ", "\n\nclass ", "\n\n@app", "\n\n# ")
    ]
    end = min(position for position in ends if position != -1)
    return SOURCE[start:end]


def _load_helpers():
    namespace = {
        "Any": Any,
        "Dict": Dict,
        "Optional": Optional,
        "Tuple": Tuple,
        "json": json,
        "urllib": urllib,
        "CFG": types.SimpleNamespace(
            PORTAL_MAIL_AUTH_URL="https://it.example.com/api/internal/mail-auth",
            PORTAL_INTEGRATION_SECRET="integration-secret",
            PORTAL_MAIL_AUTH_HOST="it.example.com",
        ),
    }
    for name in (
        "_mail_sso_subject",
        "_mail_account_available",
        "_portal_mail_account_request",
    ):
        exec(compile(_extract_function(name), str(ROOT / "rupochta_server.py"), "exec"), namespace)
    return namespace


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b'{"ok":true}'


class MailAccountProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_helpers()

    def test_only_an_sso_session_can_reach_the_canonical_account(self):
        available = self.helpers["_mail_account_available"]
        self.assertTrue(available({"auth_source": "sso", "sso_subject": "local:owner"}))
        self.assertTrue(available({"auth_source": "sso", "sso_username": "owner"}))
        self.assertFalse(available({"auth_source": "password", "sso_username": "owner"}))
        self.assertFalse(available({"auth_source": "sso"}))

    def test_proxy_keeps_the_secret_server_side_and_forwards_the_sso_actor(self):
        call = self.helpers["_portal_mail_account_request"]
        with patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            status, body = call(
                {
                    "sso_username": "owner@example.test",
                    "sso_subject": "social:telegram:42",
                },
                "account/password",
                method="POST",
                body={"newPassword": "not-a-real-secret"},
                source="203.0.113.7",
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        request = urlopen.call_args.args[0]
        headers = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(headers["x-integration-secret"], "integration-secret")
        self.assertEqual(headers["x-account-username"], "owner@example.test")
        self.assertEqual(headers["x-account-subject"], "social:telegram:42")
        self.assertEqual(headers["x-account-source"], "203.0.113.7")


if __name__ == "__main__":
    unittest.main()
