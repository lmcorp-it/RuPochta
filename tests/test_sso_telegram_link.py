"""Regression contracts for the Keycloak-owned Mail Telegram pairing handoff."""

import base64
import hashlib
from pathlib import Path
import re
import secrets
import time
import types
import unittest
import urllib.parse
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")


def _extract_function(name: str) -> str:
    start = SOURCE.index(f"def {name}(")
    ends = [
        SOURCE.find(marker, start + 1)
        for marker in ("\n\ndef ", "\n\nclass ", "\n\n@app", "\n\n# ", "\n\nasync def ")
    ]
    end = min(position for position in ends if position != -1)
    return SOURCE[start:end]


def _load_helpers():
    namespace = {
        "Request": object,
        "Any": Any,
        "Dict": Dict,
        "Optional": Optional,
        "base64": base64,
        "hashlib": hashlib,
        "re": re,
        "secrets": secrets,
        "time": time,
        "urllib": urllib,
        "CFG": types.SimpleNamespace(
            MAIL_SSO_CLIENT_ID="rupochta",
            MAIL_SSO_ISSUER="https://sso.example.com/realms/corp",
            MAIL_ACCOUNT_CONSOLE_URL="https://my.example.com/dashboard#integrations",
            MAIL_SSO_PUBLIC_URL="https://mail.example.com",
            MAIL_SSO_SCOPE="openid email groups",
            MAIL_SSO_AUTHORIZATION_ENDPOINT="https://sso.example.com/authorize",
            MAIL_SSO_TELEGRAM_IDP_ALIAS="ssosukebot",
        ),
        "_mail_sso_enabled": lambda: True,
        "_mail_sso_redirect_uri": lambda _request: "https://mail.example.com/sso/callback",
        "MAIL_SSO_PROVIDERS": frozenset({"yandex", "vk", "telegram"}),
        "_ct_eq": lambda provided, expected: secrets.compare_digest(provided, expected),
    }
    for name in (
        "_mail_sso_pkce_challenge",
        "_mail_sso_cookie_secure",
        "_mail_sso_session_hash",
        "_mail_sso_telegram_enabled",
        "_mail_sso_account_console_url",
        "_mail_sso_subject",
        "_mail_sso_telegram_account_handoff_allowed",
        "_mail_sso_telegram_account_transaction",
        "_mail_sso_authorize_url",
    ):
        exec(compile(_extract_function(name), str(ROOT / "rupochta_server.py"), "exec"), namespace)
    return namespace


class MailSsoTelegramAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_helpers()

    def test_account_console_points_at_the_personal_cabinet(self):
        """The mailbox is configured in «Интеграции» of the cabinet, not on the
        broker's own account screen (ADR-0043)."""
        self.assertEqual(
            self.helpers["_mail_sso_account_console_url"](),
            "https://my.example.com/dashboard#integrations",
        )

    def test_handoff_preflight_uses_pkce_and_prompt_none_without_link_action(self):
        verifier = "A" * 43
        url = self.helpers["_mail_sso_authorize_url"](
            object(),
            "state-token-123456",
            "nonce-token-123456",
            verifier,
            prompt="none",
        )
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["prompt"], ["none"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertNotIn("kc_action", params)
        self.assertNotIn("kc_idp_hint", params)
        self.assertNotIn("code_verifier", params)
        self.assertNotIn("wmSID", params)
        self.assertNotIn("emp_id", params)

    def test_handoff_requires_the_subject_saved_by_the_original_sso_login(self):
        allowed = self.helpers["_mail_sso_telegram_account_handoff_allowed"]
        session = {"auth_source": "sso"}
        self.assertFalse(allowed(session))
        session["sso_subject"] = "subject-123456"
        self.assertTrue(allowed(session))
        session["auth_source"] = "password"
        self.assertFalse(allowed(session))

    def test_handoff_transaction_is_bound_to_state_session_subject_and_expiry(self):
        session_token = "session-A"
        state = "state-token-123456"
        transaction = {
            "state": state,
            "subject": "subject-123456",
            "session_hash": self.helpers["_mail_sso_session_hash"](session_token),
            "expires": time.time() + 60,
        }
        session = {"telegram_sso_account": transaction}
        resolve = self.helpers["_mail_sso_telegram_account_transaction"]
        self.assertEqual(resolve(session, session_token, state), transaction)
        self.assertIsNone(resolve(session, "session-B", state))
        self.assertIsNone(resolve(session, session_token, "wrong-state-token"))
        transaction["expires"] = time.time() - 1
        self.assertIsNone(resolve(session, session_token, state))

    def test_handoff_callback_never_links_or_replaces_the_mail_session(self):
        start = SOURCE.index('@app.post("/api/sso/telegram-account/start")')
        callback = SOURCE.index("async def _complete_mail_sso_telegram_account_callback", start)
        login = SOURCE.index('@app.get("/sso/login")', callback)
        start_source = SOURCE[start:callback]
        callback_source = SOURCE[callback:login]
        self.assertIn("get_session_or_401(request)", start_source)
        self.assertIn('prompt="none"', start_source)
        self.assertIn("_session_persist_if_active", start_source)
        self.assertNotIn("kc_action", start_source)
        self.assertNotIn("broker/", start_source)
        self.assertIn("_mail_sso_post_form", callback_source)
        self.assertIn("_mail_sso_userinfo", callback_source)
        self.assertIn("_mail_sso_account_console_url()", callback_source)
        self.assertNotIn("session_create(", callback_source)
        self.assertNotIn("set_cookie(\n        CFG.SESSION_COOKIE", callback_source)
        callback_route = SOURCE.index('@app.get("/sso/callback")')
        callback_end = SOURCE.index('@app.get("/admin/sso/login")', callback_route)
        normal_callback = SOURCE[callback_route:callback_end]
        self.assertIn("telegram_account_state", normal_callback)
        self.assertIn("_ct_eq(state, telegram_account_state)", normal_callback)
        self.assertIn('sess_data["sso_subject"] = sso_subject', normal_callback)
        self.assertIn('sess_data["sso_username"] = str(user.get("username")', normal_callback)

    def test_direct_telegram_login_goes_through_the_central_broker(self):
        """Telegram is selected as a provider, never as an upstream IdP alias.

        The alias form belonged to the Keycloak-era contour this deployment no
        longer uses (ADR-0043).
        """
        url = self.helpers["_mail_sso_authorize_url"](
            object(), "state-token-123456", "nonce-token", provider="telegram"
        )
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["provider"], ["telegram"])
        self.assertNotIn("kc_idp_hint", params)
        self.assertNotIn("kc_action", params)

    def test_a_new_sso_login_clears_an_interrupted_account_handoff(self):
        start = SOURCE.index('@app.get("/sso/login")')
        end = SOURCE.index('@app.get("/sso/callback")', start)
        login_source = SOURCE[start:end]
        self.assertIn("_mail_sso_clear_telegram_account_transaction(", login_source)
        self.assertIn("account_session_token", login_source)

    def test_legacy_mail_link_endpoint_and_callback_result_are_removed(self):
        self.assertNotIn('/api/sso/telegram-link/start', SOURCE)
        self.assertNotIn("telegram_link=", SOURCE)
        self.assertNotIn("/broker/", SOURCE)
        self.assertIn("telegram_sso_account_available", SOURCE)

    def test_host_only_handoff_cookies_are_secure(self):
        self.assertTrue(self.helpers["_mail_sso_cookie_secure"]())
        self.assertIn('MAIL_SSO_TELEGRAM_ACCOUNT_STATE_COOKIE = "__Host-wmSSOTelegramAccountState"', SOURCE)
        self.assertIn('MAIL_SSO_TELEGRAM_ACCOUNT_PKCE_COOKIE = "__Host-wmSSOTelegramAccountPkce"', SOURCE)
        self.assertIn("secure=_mail_sso_cookie_secure()", SOURCE)


if __name__ == "__main__":
    unittest.main()
