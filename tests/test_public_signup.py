"""Public registration must stay closed by default and picky when open.

рупочта.рф is the open service where anyone may take a mailbox; every other
deployment of this code base is a company mail server that must never grow open
signup by upgrading. These tests run the real validators and the enable switch
out of the server source, so a regression in either is caught without booting
the app or touching a mail server.
"""

import re
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")


def _extract(pattern: str) -> str:
    match = re.search(pattern, SOURCE, re.S | re.M)
    if not match:
        raise AssertionError(f"could not find {pattern!r} in rupochta_server.py")
    return match.group(0)


class _Cfg:
    """The handful of settings the signup helpers read."""

    PUBLIC_SIGNUP = ""
    PUBLIC_SIGNUP_DOMAIN = ""
    PUBLIC_SIGNUP_MIN_PASSWORD = 10
    PUBLIC_SIGNUP_PER_HOUR = 3
    MAIL_DOMAIN = "example.com"


def _load_namespace() -> Dict[str, Any]:
    import threading

    namespace: Dict[str, Any] = {
        "re": re,
        "os": __import__("os"),
        "time": time,
        "Tuple": Tuple,
        "Optional": Optional,
        "List": List,
        "Dict": Dict,
        "Any": Any,
        "CFG": _Cfg(),
        "_rate_lock": threading.Lock(),
        "_RATE_BUCKETS": {},
    }
    block = _extract(
        r"^_PUBLIC_SIGNUP_LOCAL_RE = .*?(?=^class PublicSignupBody)"
    )
    exec(block, namespace)
    return namespace


class PublicSignupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ns = _load_namespace()
        self.cfg = self.ns["CFG"]

    # -- the switch ---------------------------------------------------------

    def test_disabled_by_default(self) -> None:
        self.assertFalse(self.ns["_public_signup_enabled"]())

    def test_enabled_only_by_explicit_truthy_values(self) -> None:
        for value in ("1", "true", "yes", "on", "TRUE"):
            self.cfg.PUBLIC_SIGNUP = value
            self.assertTrue(self.ns["_public_signup_enabled"](), value)
        for value in ("", "0", "false", "off", "no", "maybe"):
            self.cfg.PUBLIC_SIGNUP = value
            self.assertFalse(self.ns["_public_signup_enabled"](), value)

    def test_signup_domain_falls_back_to_mail_domain(self) -> None:
        self.assertEqual(self.ns["_public_signup_domain"](), "example.com")
        self.cfg.PUBLIC_SIGNUP_DOMAIN = "XN--80A1ACDMD4A.XN--P1AI"
        self.assertEqual(self.ns["_public_signup_domain"](), "xn--80a1acdmd4a.xn--p1ai")

    # -- login rules --------------------------------------------------------

    def test_accepts_ordinary_logins(self) -> None:
        for login in ("ivan", "ivan.petrov", "ivan-petrov", "ivan_petrov", "ivan2026", "a1b"):
            value, error = self.ns["validate_public_signup_login"](login)
            self.assertEqual(error, "", f"{login} should be accepted")
            self.assertEqual(value, login)

    def test_accepts_a_pasted_address_on_our_own_domain(self) -> None:
        self.cfg.PUBLIC_SIGNUP_DOMAIN = "xn--80a1acdmd4a.xn--p1ai"
        value, error = self.ns["validate_public_signup_login"](
            "Ivan.Petrov@XN--80A1ACDMD4A.XN--P1AI"
        )
        self.assertEqual(error, "")
        self.assertEqual(value, "ivan.petrov")

    def test_rejects_a_pasted_address_on_someone_elses_domain(self) -> None:
        """ivan@gmail.com must not quietly become ivan@<our domain>."""
        _, error = self.ns["validate_public_signup_login"]("ivan@gmail.com")
        self.assertIn("только на домене @example.com", error)

    def test_rejects_malformed_logins(self) -> None:
        for login in ("ab", "-ivan", ".ivan", "ivan petrov", "иван", "x" * 31):
            _, error = self.ns["validate_public_signup_login"](login)
            self.assertTrue(error, f"{login!r} should be rejected")

    def test_rejects_trailing_separators_and_double_dots(self) -> None:
        for login in ("ivan.", "ivan-", "ivan_", "ivan..petrov"):
            _, error = self.ns["validate_public_signup_login"](login)
            self.assertTrue(error, f"{login!r} should be rejected")

    def test_reserves_role_addresses(self) -> None:
        for login in ("postmaster", "abuse", "admin", "support", "noreply", "rupochta", "www"):
            _, error = self.ns["validate_public_signup_login"](login)
            self.assertIn("зарезервировано", error, login)

    def test_empty_login_is_reported_separately(self) -> None:
        _, error = self.ns["validate_public_signup_login"]("   ")
        self.assertEqual(error, "Укажите имя ящика.")

    # -- password rules -----------------------------------------------------

    def test_password_length_follows_configuration(self) -> None:
        self.assertTrue(self.ns["validate_public_signup_password"]("short", "ivan"))
        self.assertEqual(self.ns["validate_public_signup_password"]("correct-horse", "ivan"), "")
        self.cfg.PUBLIC_SIGNUP_MIN_PASSWORD = 20
        self.assertTrue(self.ns["validate_public_signup_password"]("correct-horse", "ivan"))

    def test_password_floor_cannot_be_configured_below_eight(self) -> None:
        self.cfg.PUBLIC_SIGNUP_MIN_PASSWORD = 3
        self.assertTrue(self.ns["validate_public_signup_password"]("abc", "ivan"))
        self.assertEqual(self.ns["validate_public_signup_password"]("abcdefgh", "ivan"), "")

    def test_password_may_not_repeat_the_login(self) -> None:
        error = self.ns["validate_public_signup_password"]("ivan.petrov", "ivan.petrov")
        self.assertIn("совпадать", error)

    # -- rate limiting ------------------------------------------------------

    def test_rate_limit_counts_every_attempt(self) -> None:
        allow = self.ns["_public_signup_rate_allow"]
        record = self.ns["_public_signup_rate_record"]
        for _ in range(self.cfg.PUBLIC_SIGNUP_PER_HOUR):
            self.assertTrue(allow("203.0.113.5"))
            record("203.0.113.5")
        self.assertFalse(allow("203.0.113.5"))
        # A different client is unaffected.
        self.assertTrue(allow("203.0.113.6"))

    def test_rate_limit_window_expires(self) -> None:
        record = self.ns["_public_signup_rate_record"]
        allow = self.ns["_public_signup_rate_allow"]
        for _ in range(self.cfg.PUBLIC_SIGNUP_PER_HOUR):
            record("198.51.100.7")
        self.assertFalse(allow("198.51.100.7"))
        stale = time.time() - self.ns["_PUBLIC_SIGNUP_WINDOW"] - 1
        self.ns["_RATE_BUCKETS"]["public_signup"]["198.51.100.7"] = [stale] * 10
        self.assertTrue(allow("198.51.100.7"))

    def test_missing_client_ip_is_not_rate_limited(self) -> None:
        self.assertTrue(self.ns["_public_signup_rate_allow"](""))


class SignupSurfaceTests(unittest.TestCase):
    """The routes and the page have to stay wired to each other."""

    def test_endpoints_exist(self) -> None:
        self.assertIn('@app.post("/api/signup")', SOURCE)
        self.assertIn('@app.get("/api/signup/config")', SOURCE)
        self.assertIn('@app.get("/signup")', SOURCE)

    def test_signup_requires_same_origin(self) -> None:
        handler = _extract(r"async def api_public_signup\(.*?(?=^@app\.)")
        self.assertIn("_is_same_origin_admin_request", handler)

    def test_signup_page_is_shipped(self) -> None:
        page = (ROOT / "static" / "signup.html").read_text(encoding="utf-8")
        self.assertIn("/api/signup/config", page)
        self.assertIn("/api/signup", page)

    def test_login_page_hides_the_invite_until_signup_is_open(self) -> None:
        page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="login-signup-invite" hidden', page)
        self.assertIn("provisioning_ready", page)


if __name__ == "__main__":
    unittest.main()
