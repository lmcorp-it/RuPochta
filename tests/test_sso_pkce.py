"""Regression tests for Mail SSO PKCE helpers without booting the web server."""

import base64
import hashlib
from pathlib import Path
import re
import secrets
import types
import unittest
import urllib.parse
from typing import Tuple


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


def _load_pkce_helpers():
    namespace = {
        "Request": object,
        "base64": base64,
        "hashlib": hashlib,
        "re": re,
        "secrets": secrets,
        "Tuple": Tuple,
        "urllib": urllib,
        "CFG": types.SimpleNamespace(
            MAIL_SSO_CLIENT_ID="rupochta",
            MAIL_SSO_SCOPE="openid email groups",
            MAIL_SSO_AUTHORIZATION_ENDPOINT="https://sso.example.com/authorize",
        ),
        "_mail_sso_redirect_uri": lambda _request: "https://mail.example.com/sso/callback",
        # Mail offers exactly the providers the central broker offers (ADR-0043).
        "MAIL_SSO_PROVIDERS": frozenset({"yandex", "yandex/mail", "vk", "telegram"}),
    }
    for name in (
        "_mail_sso_pkce_verifier",
        "_mail_sso_pkce_challenge",
        "_mail_sso_pkce_cookie_value",
        "_mail_sso_pkce_cookie_parts",
        "_mail_sso_authorize_url",
    ):
        exec(compile(_extract_function(name), str(ROOT / "rupochta_server.py"), "exec"), namespace)
    return namespace


class MailSsoPkceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = _load_pkce_helpers()

    def test_s256_challenge_matches_rfc_example(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(
            self.helpers["_mail_sso_pkce_challenge"](verifier),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        )

    def test_pkce_cookie_keeps_only_a_valid_state_verifier_pair(self):
        state = "state-token"
        verifier = self.helpers["_mail_sso_pkce_verifier"]()
        self.assertRegex(verifier, r"^[A-Za-z0-9_-]{43}$")
        packed = self.helpers["_mail_sso_pkce_cookie_value"](state, verifier)
        self.assertEqual(self.helpers["_mail_sso_pkce_cookie_parts"](packed), (state, verifier))
        self.assertEqual(self.helpers["_mail_sso_pkce_cookie_parts"](state), ("", ""))
        self.assertEqual(self.helpers["_mail_sso_pkce_cookie_parts"](""), ("", ""))

    def test_authorize_url_includes_s256_without_leaking_verifier(self):
        verifier = self.helpers["_mail_sso_pkce_verifier"]()
        url = self.helpers["_mail_sso_authorize_url"](object(), "state-token", "nonce-token", verifier)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["code_challenge"], [self.helpers["_mail_sso_pkce_challenge"](verifier)])
        self.assertNotIn("code_verifier", params)
        no_pkce_params = urllib.parse.parse_qs(
            urllib.parse.urlparse(
                self.helpers["_mail_sso_authorize_url"](object(), "state-token", "nonce-token")
            ).query
        )
        self.assertNotIn("code_challenge", no_pkce_params)

    def test_authorize_url_can_select_only_supported_central_providers(self):
        yandex = self.helpers["_mail_sso_authorize_url"](
            object(), "state-token", "nonce-token", provider="yandex"
        )
        yandex_mail = self.helpers["_mail_sso_authorize_url"](
            object(), "state-token", "nonce-token", provider="yandex/mail"
        )
        unknown = self.helpers["_mail_sso_authorize_url"](
            object(), "state-token", "nonce-token", provider="unknown"
        )
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(yandex).query)["provider"], ["yandex"])
        self.assertEqual(
            urllib.parse.parse_qs(urllib.parse.urlparse(yandex_mail).query)["provider"],
            ["yandex/mail"],
        )
        self.assertNotIn("provider", urllib.parse.parse_qs(urllib.parse.urlparse(unknown).query))
        # Telegram is a central provider like the others, not an upstream IdP
        # selected by alias (ADR-0043).
        telegram = self.helpers["_mail_sso_authorize_url"](
            object(), "state-token", "nonce-token", provider="telegram"
        )
        query = urllib.parse.parse_qs(urllib.parse.urlparse(telegram).query)
        self.assertEqual(query["provider"], ["telegram"])
        self.assertNotIn("kc_idp_hint", query)

    def test_callback_posts_verifier_for_new_sso_state(self):
        marker = '@app.get("/sso/callback")'
        start = SOURCE.index(marker)
        end = SOURCE.index("\n\n@app.get(\"/admin/sso/login\")", start)
        self.assertIn('token_request["code_verifier"] = code_verifier', SOURCE[start:end])
        self.assertIn('MAIL_SSO_PKCE_COOKIE = "__Host-wmSSOPkce"', SOURCE)
        self.assertIn("def _mail_sso_clear_transaction", SOURCE)
        self.assertIn('"client_id": CFG.MAIL_SSO_CLIENT_ID', SOURCE[start:end])

    def test_sso_requests_identify_the_mail_client(self):
        self.assertEqual(SOURCE.count('"User-Agent": "Mozilla/5.0 (compatible; RuPochta/1.0; +https://mail.example.com)"'), 2)


if __name__ == "__main__":
    unittest.main()
