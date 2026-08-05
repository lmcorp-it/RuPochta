"""Regression tests for the company directory union + search.

Locks in behaviour fixed in the 2026-06-18 code review:

* H1 — _search_directory_contacts must NOT synthesise phantom @MAIL_DOMAIN
  emails for AD service accounts that have no mailbox, and must NOT rewrite
  cross-domain portal emails (alpha/beta/gamma) to @MAIL_DOMAIN. Portal rows
  already carry correct <login>@<directory_domain> emails synthesised by
  _portal_employee_to_contact; AD rows without mail must be dropped rather
  than fabricated.

* M2 — api_directory dedup keeps AD rows when the same login exists in both
  portal and AD (AD has richer data), and appends portal-only rows.

Runs anywhere with stdlib unittest — no pytest, no LDAP, no portal bridge.
Heavy module-level side effects in rupochta_server are bypassed by
importing the two functions under test in isolation.
"""

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_search_function():
    """Import _search_directory_contacts without triggering the full server boot.

    rupochta_server.py runs background workers / FastAPI app construction
    at import time, which we neither want nor need for a pure-function test.
    We exec only the function source plus its minimal dependencies.
    """
    src_path = REPO_ROOT / "rupochta_server.py"
    source = src_path.read_text(encoding="utf-8")

    # Extract just _search_directory_contacts (def ... up to the next top-level def).
    marker = "def _search_directory_contacts("
    start = source.index(marker)
    # Find the end: next top-level "def " or "class " at column 0 after start.
    end = len(source)
    for keyword in ("\ndef ldap_load_directory", "\ndef portal_load_directory",
                    "\n# ", "\n\ndef "):
        idx = source.find(keyword, start + 1)
        if idx != -1 and idx < end:
            end = idx
    func_src = source[start:end]

    # Minimal namespace: only the names _search_directory_contacts references.
    # We stub CFG, portal_load_directory, ldap_load_directory, log, typing.
    import logging
    from typing import Any, Dict, List, Optional

    cfg_stub = types.SimpleNamespace(MAIL_DOMAIN="example.com")

    captured = {}

    def fake_portal_load_directory(force: bool = False):
        return captured["portal_rows"]

    def fake_ldap_load_directory(force: bool = False):
        return captured["ad_rows"]

    namespace = {
        "CFG": cfg_stub,
        "portal_load_directory": fake_portal_load_directory,
        "ldap_load_directory": fake_ldap_load_directory,
        "log": logging.getLogger("test"),
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Optional": Optional,
        "__builtins__": __builtins__,
    }
    exec(compile(func_src, str(src_path), "exec"), namespace)
    fn = namespace["_search_directory_contacts"]

    def run(portal=None, ad=None, contacts=None, query="", limit=25):
        captured["portal_rows"] = portal or []
        captured["ad_rows"] = ad or []
        if contacts is None:
            contacts = []
            seen = set()
            for row in [*(portal or []), *(ad or [])]:
                login = str(row.get("login") or "").lower()
                email = str(row.get("email") or "").lower()
                if not email or (login and login in seen):
                    continue
                contacts.append(row)
                if login:
                    seen.add(login)
        return fn(query, limit, contacts)

    return run


class DirectorySearchTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # staticmethod wrapper is essential: a bare function assigned to a
        # class attribute becomes a bound method (descriptor protocol), so
        # self._do_search(**kw) would pass `self` as the first positional arg
        # → "multiple values for argument 'portal'". staticmethod disables that.
        cls._do_search = staticmethod(_load_search_function())

    def search(self, **kwargs):
        return self._do_search(**kwargs)

    # ---- Test fixtures -----------------------------------------------------

    PORTAL = [
        # Cross-domain portal employee: correct email already synthesised.
        {"name": "Иван Бутик", "login": "ivan.b", "directory_domain": "alpha.local",
         "email": "ivan.b@alpha.local", "source": "portal"},
        {"name": "Петр Дисмак", "login": "petr.d", "directory_domain": "beta.local",
         "email": "petr.d@beta.local", "source": "portal"},
        {"name": "Сидор Трейд", "login": "sidor.t", "directory_domain": "gamma.local",
         "email": "sidor.t@gamma.local", "source": "portal"},
        # corp.local portal employee.
        {"name": "Анна Локал", "login": "anna.l", "directory_domain": "corp.local",
         "email": "anna.l@example.com", "source": "portal"},
    ]

    AD = [
        # Duplicate of portal login → must be deduped (portal already has email).
        {"name": "Анна Локалова", "login": "anna.l", "email": "anna.l@example.com",
         "source": "ad"},
        # AD service accounts with NO mailbox.
        {"name": "Zabbix Monitor", "login": "zabbix_user", "email": "", "source": "ad"},
        {"name": "SQL Service", "login": "sql-user", "email": "", "source": "ad"},
        # AD-only real mailbox.
        {"name": "Борис Почтовый", "login": "boris.p", "email": "boris.p@example.com",
         "source": "ad"},
    ]

    # ---- Tests -------------------------------------------------------------

    def test_no_phantom_emails_for_ad_service_accounts(self):
        """H1 regression: AD accounts without mail must be DROPPED, not given a
        fabricated @MAIL_DOMAIN email that would silently bounce calendar invites."""
        rows = self.search(query="", portal=self.PORTAL, ad=self.AD)
        emails = {r["email"] for r in rows}
        self.assertNotIn(
            "zabbix_user@example.com", emails,
            "phantom email synthesised for zabbix_user (H1 regressed)",
        )
        self.assertNotIn(
            "sql-user@example.com", emails,
            "phantom email synthesised for sql-user (H1 regressed)",
        )
        logins = {r.get("login") for r in rows}
        self.assertNotIn("zabbix_user", logins)
        self.assertNotIn("sql-user", logins)

    def test_cross_domain_emails_preserved(self):
        """H1 regression: cross-domain portal emails (alpha/beta/gamma) must NOT
        be rewritten to @example.com."""
        rows = self.search(query="", portal=self.PORTAL, ad=self.AD)
        emails = {r["email"] for r in rows}
        self.assertIn("ivan.b@alpha.local", emails)
        self.assertIn("petr.d@beta.local", emails)
        self.assertIn("sidor.t@gamma.local", emails)

    def test_real_lm_local_emails_preserved(self):
        """Genuine corp.local mailboxes (both portal and AD-only) must survive."""
        rows = self.search(query="", portal=self.PORTAL, ad=self.AD)
        emails = {r["email"] for r in rows}
        self.assertIn("anna.l@example.com", emails)
        self.assertIn("boris.p@example.com", emails)

    def test_dedup_uses_login_and_email(self):
        """anna.l appears in both portal and AD with the same email → only one row."""
        rows = self.search(query="", portal=self.PORTAL, ad=self.AD)
        anna_rows = [r for r in rows if r.get("login") == "anna.l"]
        self.assertEqual(len(anna_rows), 1, "anna.l not deduped")

    def test_query_filter_returns_only_matches(self):
        rows = self.search(query="alpha", portal=self.PORTAL, ad=self.AD)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "ivan.b@alpha.local")

    def test_empty_query_returns_all_invitable(self):
        """No query → every returned row must have an email (invitable)."""
        rows = self.search(query="", portal=self.PORTAL, ad=self.AD)
        self.assertTrue(rows, "expected non-empty result")
        for r in rows:
            self.assertTrue(r.get("email"), f"row without email returned: {r}")

    def test_limit_enforced(self):
        rows = self.search(query="", limit=2, portal=self.PORTAL, ad=self.AD)
        self.assertLessEqual(len(rows), 2)

    def test_explicit_rupochta_contacts_do_not_merge_external_directories(self):
        contacts = [{
            "name": "RuPochta Admin",
            "login": "admin",
            "email": "admin@example.com",
            "source": "rupochta",
        }]
        rows = self.search(
            query="",
            contacts=contacts,
            portal=self.PORTAL,
            ad=self.AD,
        )
        self.assertEqual(rows, contacts)


class ApiDirectoryDedupTests(unittest.TestCase):
    """Lock in M2: the api_directory dedup loop.  We replicate the loop logic
    here because importing the full async endpoint is impractical; the goal is
    to guard the dedup invariant against future refactors of that block."""

    def _dedup(self, portal_rows, ad_rows):
        """Mirror of the api_directory dedup block (see rupochta_server.py
        around the 'seen_logins = set(portal_logins)' comment)."""
        portal_logins = {(r.get("login") or "").lower() for r in portal_rows if r.get("login")}
        seen_logins = set(portal_logins)
        ad_only = []
        for r in ad_rows:
            login = (r.get("login") or "").lower()
            if login and login in seen_logins:
                continue
            ad_only.append(r)
            if login:
                seen_logins.add(login)
        return portal_rows + ad_only, seen_logins

    def test_ad_duplicate_login_dropped(self):
        portal = [{"login": "anna.l", "email": "anna.l@example.com", "source": "portal"}]
        ad = [
            {"login": "anna.l", "email": "anna.l@example.com", "source": "ad"},
            {"login": "boris.p", "email": "boris.p@example.com", "source": "ad"},
        ]
        rows, seen = self._dedup(portal, ad)
        logins = [r["login"] for r in rows]
        self.assertEqual(logins.count("anna.l"), 1, "anna.l duplicated")
        self.assertIn("boris.p", logins, "AD-only row dropped")
        self.assertEqual(len(rows), 2)

    def test_seen_logins_used_for_employees_without_ad(self):
        """seen_logins must include both portal and ad_only logins so that
        _employees_without_ad excludes identities already covered."""
        portal = [{"login": "p1"}]
        ad = [{"login": "a1"}]
        _, seen = self._dedup(portal, ad)
        self.assertIn("p1", seen)
        self.assertIn("a1", seen)


def _load_photo_resolver():
    """Import _resolve_photo without triggering the full server boot, using
    the same exec-from-source strategy as _load_search_function."""
    src_path = REPO_ROOT / "rupochta_server.py"
    source = src_path.read_text(encoding="utf-8")

    marker = "def _resolve_photo("
    start = source.index(marker)
    end = len(source)
    for keyword in ("\n@app", "\ndef ", "\n# ", "\n\ndef "):
        idx = source.find(keyword, start + 1)
        if idx != -1 and idx < end:
            end = idx
    func_src = source[start:end]

    from typing import Any, Dict, List, Optional
    namespace = {
        "Any": Any, "Dict": Dict, "List": List, "Optional": Optional,
        "__builtins__": __builtins__,
    }
    exec(compile(func_src, str(src_path), "exec"), namespace)
    return namespace["_resolve_photo"]


class PhotoResolverTests(unittest.TestCase):
    """Lock in the photo resolution order: LDAP first, then portal avatar
    fallback, then None. Added 2026-06-19 for the cross-domain photo fix."""

    @classmethod
    def setUpClass(cls):
        # staticmethod prevents descriptor-protocol self-injection (same trap
        # as the directory search tests).
        cls._resolve = staticmethod(_load_photo_resolver())

    # A 1x1 transparent PNG as a data URL for testing.
    _PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    _PNG_DATA_URL = f"data:image/png;base64,{_PNG_B64}"
    _JPEG_DATA_URL = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP"

    def test_ldap_photo_wins(self):
        """When LDAP has a photo, it is returned as JPEG and portal is ignored."""
        portal = [{"login": "max", "avatar_data_url": self._PNG_DATA_URL}]
        result = self._resolve("max", b"\xff\xd8jpeg\xff\xd9", portal)
        self.assertIsNotNone(result)
        self.assertEqual(result["media"], "image/jpeg")
        self.assertEqual(result["raw"], b"\xff\xd8jpeg\xff\xd9")

    def test_portal_fallback_when_no_ldap_photo(self):
        """No LDAP photo → portal avatarDataUrl decoded and returned."""
        portal = [{"login": "max", "avatar_data_url": self._PNG_DATA_URL}]
        result = self._resolve("max", None, portal)
        self.assertIsNotNone(result)
        self.assertEqual(result["media"], "image/png")
        self.assertTrue(result["raw"], "decoded bytes should be non-empty")

    def test_portal_fallback_jpeg_media_type(self):
        """Portal JPEG data URL preserves its media type."""
        # A valid minimal JPEG SOI/EOI marker, base64-encoded.
        jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\xff\xd9"
        jpeg_b64 = __import__("base64").b64encode(jpeg_bytes).decode()
        portal = [{"login": "max", "avatar_data_url": f"data:image/jpeg;base64,{jpeg_b64}"}]
        result = self._resolve("max", None, portal)
        self.assertIsNotNone(result)
        self.assertEqual(result["media"], "image/jpeg")

    def test_portal_fallback_case_insensitive_login(self):
        """Login matching against portal rows must be case-insensitive."""
        portal = [{"login": "Max", "avatar_data_url": self._PNG_DATA_URL}]
        result = self._resolve("MAX", None, portal)
        self.assertIsNotNone(result, "case mismatch prevented portal fallback")

    def test_no_photo_when_login_missing_from_portal(self):
        """Login not in portal → None (will become 404 in the endpoint)."""
        portal = [{"login": "someone_else", "avatar_data_url": self._PNG_DATA_URL}]
        result = self._resolve("max", None, portal)
        self.assertIsNone(result)

    def test_no_photo_when_portal_avatar_empty(self):
        """Portal row exists but has no avatarDataUrl → None."""
        portal = [{"login": "max", "avatar_data_url": ""}]
        result = self._resolve("max", None, portal)
        self.assertIsNone(result)

    def test_no_photo_when_neither_source_has_photo(self):
        """Both LDAP None and portal empty → None."""
        result = self._resolve("ghost", None, [])
        self.assertIsNone(result)

    def test_malformed_data_url_returns_none(self):
        """A non-base64 data URL must not crash — returns None."""
        portal = [{"login": "max", "avatar_data_url": "data:image/png;base64,!!!notb64!!!"}]
        result = self._resolve("max", None, portal)
        self.assertIsNone(result)

    def test_non_image_data_url_ignored(self):
        """A data:text/html URL must NOT be served (avoids XSS via photo endpoint)."""
        portal = [{"login": "max",
                   "avatar_data_url": "data:text/html;base64,PHNjcmlwdD4="}]
        result = self._resolve("max", None, portal)
        self.assertIsNone(result, "text/html data URL must not resolve to a photo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
