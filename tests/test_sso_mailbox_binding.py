"""Regression tests for central-auth mailbox bindings (ADR-0041).

The helpers are loaded out of the server source and run against a real
temporary SQLite file, so the stored shape — hashed subject, encrypted
password — is asserted as it lands on disk, not as the code intends it.
"""

import hashlib
import hmac
import imaplib
import re
import sqlite3
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")

BINDING_TABLE = """
CREATE TABLE IF NOT EXISTS sso_mailbox_bindings (
    subject_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    imap_pass_enc TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    imap_host TEXT,
    imap_port INTEGER,
    provider TEXT
)
"""

EXTERNAL_BINDING_TABLE = """
CREATE TABLE IF NOT EXISTS sso_external_mailbox_bindings (
    subject_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    imap_pass_enc TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL,
    provider TEXT NOT NULL
)
"""


def _extract_function(name: str) -> str:
    start = SOURCE.index(f"def {name}(")
    ends = [
        SOURCE.find(marker, start + 1)
        for marker in ("\n\ndef ", "\n\nclass ", "\n\n@app", "\n\n# ")
    ]
    end = min(position for position in ends if position != -1)
    return SOURCE[start:end]


def _load_mail_provider_presets():
    """Load the MAIL_PROVIDER_PRESETS constant from the server source."""
    start = SOURCE.index("MAIL_PROVIDER_PRESETS: Dict")
    end = SOURCE.index("\n\n# ---------------------------------------------------------------------------", start)
    namespace = {"Dict": Dict, "Any": Any}
    exec(SOURCE[start:end], namespace)  # noqa: S102 - trusted own source
    return namespace["MAIL_PROVIDER_PRESETS"]


def _load_binding_helpers(db_path: str, key: bytes):
    """Load the binding helpers with a throwaway database and a reversible cipher.

    The real cipher is exercised elsewhere; here it is replaced by a marked,
    reversible transform so a plaintext password in the file would be visible
    as a failure rather than hidden behind encryption.
    """

    @contextmanager
    def _db_connection():
        con = sqlite3.connect(db_path)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    namespace = {
        "_SECRET_KEY": key,
        "_db_connection": _db_connection,
        "hashlib": hashlib,
        "hmac": hmac,
        "Optional": Optional,
        "Tuple": Tuple,
        "re": re,
        "encrypt_secret": lambda value: f"enc:{value[::-1]}" if value else "",
        "decrypt_secret": lambda value: (
            str(value)[4:][::-1] if str(value).startswith("enc:") else None
        ),
        "_utc_now_iso": lambda: datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "MAIL_PROVIDER_PRESETS": _load_mail_provider_presets(),
    }
    for name in (
        "_sso_binding_subject_hash",
        "_sso_bindings_put",
        "_sso_binding_put",
        "_sso_binding_drop",
        "_sso_binding_drop_requested",
        "_sso_bindings_drop_external",
        "_sso_binding_lookup",
        "_sso_bindings_status",
        "_resolve_provider_hosts",
    ):
        exec(_extract_function(name), namespace)  # noqa: S102 - trusted own source
    return types.SimpleNamespace(**namespace)


def _load_imap_connect():
    contexts = []

    class FakeContext:
        check_hostname = True
        verify_mode = "required"

    class FakeConnection:
        def __init__(self, _host, _port, *, ssl_context, timeout):
            contexts.append(ssl_context)
            self.timeout = timeout

        def login(self, _user, _password):
            return "OK", []

    fake_ssl = types.SimpleNamespace(
        CERT_NONE="none",
        CERT_REQUIRED="required",
        create_default_context=lambda: FakeContext(),
    )
    namespace = {
        "Optional": Optional,
        "contextmanager": contextmanager,
        "imaplib": types.SimpleNamespace(IMAP4_SSL=FakeConnection),
        "ssl": fake_ssl,
        "CFG": types.SimpleNamespace(IMAP_HOST="managed.test", IMAP_PORT=993),
        "_local_mailserver_enabled": lambda: False,
    }
    exec(_extract_function("_imap_connect"), namespace)  # noqa: S102 - trusted own source
    return namespace["_imap_connect"], contexts, fake_ssl


def _load_imap_validation_http_error():
    class FakeHTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code = status_code
            self.detail = detail

    namespace = {
        "HTTPException": FakeHTTPException,
        "contextmanager": contextmanager,
        "imaplib": imaplib,
    }
    helpers_start = SOURCE.index("_IMAP_AUTH_FAILURE_MARKERS =")
    helpers_end = SOURCE.index("def get_session_or_401", helpers_start)
    exec(SOURCE[helpers_start:helpers_end], namespace)  # noqa: S102 - trusted own source
    exec(  # noqa: S102 - trusted own source
        _extract_function("_imap_validation_http_error"),
        namespace,
    )
    return namespace["_imap_validation_http_error"]


class SsoMailboxBindingTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._dir.name) / "bindings.db")
        with sqlite3.connect(self.db_path) as con:
            con.execute(BINDING_TABLE)
            con.execute(EXTERNAL_BINDING_TABLE)
        self.helpers = _load_binding_helpers(self.db_path, b"secret-key-for-tests")

    def tearDown(self):
        self._dir.cleanup()

    def _rows(self):
        with sqlite3.connect(self.db_path) as con:
            return con.execute(
                "SELECT subject_hash, email, imap_pass_enc FROM sso_mailbox_bindings"
            ).fetchall()

    def _external_rows(self):
        with sqlite3.connect(self.db_path) as con:
            return con.execute(
                "SELECT subject_hash, email, imap_pass_enc "
                "FROM sso_external_mailbox_bindings"
            ).fetchall()

    def test_binding_round_trips_and_stores_no_plaintext(self):
        self.helpers._sso_binding_put(
            "social:yandex:4242", "Employee.One@example.com", "Mailbox-Secret_2026"
        )

        self.assertEqual(
            self.helpers._sso_binding_lookup("social:yandex:4242"),
            ("employee.one@example.com", "Mailbox-Secret_2026", None, None),
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        subject_hash, email, encrypted = rows[0]
        # Neither the subject nor the password may be readable in the file.
        self.assertNotIn("social:yandex:4242", subject_hash)
        self.assertNotIn("Mailbox-Secret_2026", encrypted)
        self.assertEqual(email, "employee.one@example.com")

    def test_startup_refuses_legacy_external_rows_that_may_have_overwritten_managed(self):
        with sqlite3.connect(":memory:") as con:
            con.execute(BINDING_TABLE)
            con.execute(
                "INSERT INTO sso_mailbox_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "external-hash",
                    "person@yandex.ru",
                    "encrypted-external",
                    "2026-08-04T00:00:00Z",
                    "2026-08-04T00:00:00Z",
                    "imap.yandex.ru",
                    993,
                    "yandex",
                ),
            )
            namespace = {"sqlite3": sqlite3}
            exec(  # noqa: S102 - trusted own source
                _extract_function("_sso_external_bindings_init"),
                namespace,
            )
            with self.assertRaisesRegex(RuntimeError, "manual recovery"):
                namespace["_sso_external_bindings_init"](con)

            self.assertEqual(
                con.execute(
                    "SELECT subject_hash, email FROM sso_mailbox_bindings"
                ).fetchall(),
                [("external-hash", "person@yandex.ru")],
            )
            self.assertEqual(
                con.execute(
                    "SELECT subject_hash, provider "
                    "FROM sso_external_mailbox_bindings"
                ).fetchall(),
                [],
            )

    def test_subject_hash_is_keyed_so_a_guess_cannot_be_confirmed(self):
        other = _load_binding_helpers(self.db_path, b"a-different-service-key")
        self.assertNotEqual(
            self.helpers._sso_binding_subject_hash("social:yandex:4242"),
            other._sso_binding_subject_hash("social:yandex:4242"),
        )
        self.assertEqual(self.helpers._sso_binding_subject_hash(" "), "")

    def test_external_imap_requires_normal_tls_certificate_verification(self):
        connect, contexts, fake_ssl = _load_imap_connect()

        connect(
            "person@yandex.ru",
            "application-password",
            host="imap.yandex.ru",
            port=993,
        )
        self.assertTrue(contexts[-1].check_hostname)
        self.assertEqual(contexts[-1].verify_mode, fake_ssl.CERT_REQUIRED)

        connect("person@example.com", "managed-password")
        self.assertFalse(contexts[-1].check_hostname)
        self.assertEqual(contexts[-1].verify_mode, fake_ssl.CERT_NONE)

    def test_imap_validation_distinguishes_credentials_from_transport_failure(self):
        classify = _load_imap_validation_http_error()

        rejected = classify(imaplib.IMAP4.error("AUTHENTICATIONFAILED"))
        unavailable = classify(TimeoutError("provider timeout"))
        aborted = classify(imaplib.IMAP4.abort("AUTHENTICATIONFAILED"))

        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.detail, "mailbox credentials rejected")
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.detail, "mailbox service unavailable")
        self.assertEqual(aborted.status_code, 503)
        self.assertEqual(aborted.detail, "mailbox service unavailable")

    def test_rebinding_replaces_the_previous_mailbox_for_that_subject(self):
        self.helpers._sso_binding_put(
            "social:vk:7", "first@example.com", "First-Secret_2026"
        )
        self.helpers._sso_binding_put(
            "social:vk:7", "second@example.com", "Second-Secret_2026"
        )

        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(
            self.helpers._sso_binding_lookup("social:vk:7"),
            ("second@example.com", "Second-Secret_2026", None, None),
        )

    def test_binding_status_exposes_provider_metadata_without_the_secret(self):
        self.helpers._sso_binding_put(
            "social:yandex:4242",
            "person@yandex.ru",
            "App-Secret_2026",
            imap_host="imap.yandex.ru",
            imap_port=993,
            provider="yandex",
        )

        status = self.helpers._sso_bindings_status(["social:yandex:4242"])

        self.assertEqual(
            status,
            {
                "bound": True,
                "email": "person@yandex.ru",
                "provider": "yandex",
                "external": True,
                "conflict": False,
            },
        )
        self.assertNotIn("password", status)

    def test_bulk_binding_is_one_state_for_every_login_identity(self):
        subjects = ["social:yandex:42", "social:vk:7"]
        self.assertEqual(
            self.helpers._sso_bindings_put(
                subjects,
                "person@yandex.ru",
                "App-Secret_2026",
                imap_host="imap.yandex.ru",
                imap_port=993,
                provider="yandex",
            ),
            2,
        )

        status = self.helpers._sso_bindings_status(subjects)

        self.assertTrue(status["bound"])
        self.assertFalse(status["conflict"])
        self.assertEqual(status["provider"], "yandex")

    def test_status_flags_missing_mismatched_and_unreadable_bindings(self):
        first = "social:yandex:42"
        second = "social:vk:7"
        self.helpers._sso_binding_put(
            first,
            "person@yandex.ru",
            "Yandex-Secret_2026",
            imap_host="imap.yandex.ru",
            imap_port=993,
            provider="yandex",
        )
        self.assertTrue(self.helpers._sso_bindings_status([first, second])["conflict"])

        self.helpers._sso_binding_put(
            second,
            "person@company.ru",
            "Vk-Secret_2026",
            imap_host="mail.vk.works",
            imap_port=993,
            provider="vk",
        )
        self.assertTrue(self.helpers._sso_bindings_status([first, second])["conflict"])

        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "UPDATE sso_external_mailbox_bindings "
                "SET imap_pass_enc = ? WHERE subject_hash = ?",
                ("cannot-decrypt", self.helpers._sso_binding_subject_hash(first)),
            )
        self.assertTrue(self.helpers._sso_bindings_status([first])["conflict"])

    def test_external_bulk_drop_is_provider_scoped_and_preserves_managed_binding(self):
        subjects = ["social:yandex:42", "social:vk:7"]
        self.helpers._sso_bindings_put(
            subjects,
            "person@example.com",
            "Managed-Secret_2026",
        )
        self.helpers._sso_bindings_put(
            subjects,
            "person@yandex.ru",
            "App-Secret_2026",
            imap_host="imap.yandex.ru",
            imap_port=993,
            provider="yandex",
        )
        self.assertEqual(len(self._rows()), 2)
        self.assertEqual(len(self._external_rows()), 2)
        self.assertEqual(
            self.helpers._sso_binding_lookup(subjects[0]),
            ("person@yandex.ru", "App-Secret_2026", "imap.yandex.ru", 993),
        )
        self.assertEqual(self.helpers._sso_bindings_drop_external(subjects, "vk"), 0)
        self.assertTrue(self.helpers._sso_bindings_status(subjects)["bound"])
        self.assertEqual(self.helpers._sso_bindings_drop_external(subjects, "yandex"), 2)
        self.assertFalse(self.helpers._sso_bindings_status(subjects)["bound"])
        self.assertEqual(len(self._rows()), 2)
        self.assertEqual(len(self._external_rows()), 0)
        self.assertEqual(
            self.helpers._sso_binding_lookup(subjects[0]),
            ("person@example.com", "Managed-Secret_2026", None, None),
        )

    def test_legacy_singular_disconnect_removes_only_external_binding(self):
        subject = "social:yandex:42"
        self.helpers._sso_bindings_put(
            [subject], "person@example.com", "Managed-Secret_2026"
        )
        self.helpers._sso_bindings_put(
            [subject],
            "person@yandex.ru",
            "External-Secret_2026",
            imap_host="imap.yandex.ru",
            imap_port=993,
            provider="yandex",
        )

        removed = self.helpers._sso_binding_drop_requested(
            subject,
            imap_host="imap.yandex.ru",
            provider="yandex",
        )

        self.assertEqual(removed, 1)
        self.assertEqual(len(self._external_rows()), 0)
        self.assertEqual(
            self.helpers._sso_binding_lookup(subject),
            ("person@example.com", "Managed-Secret_2026", None, None),
        )

    def test_known_provider_presets_resolve_to_correct_hosts(self):
        resolve = self.helpers._resolve_provider_hosts
        self.assertEqual(
            resolve("yandex"),
            ("imap.yandex.ru", 993),
        )
        self.assertEqual(
            resolve("yandex360"),
            ("imap.yandex.com", 993),
        )
        self.assertEqual(
            resolve("mailru"),
            ("imap.mail.ru", 993),
        )
        self.assertEqual(
            resolve("vk_workspace"),
            ("mail.vk.works", 993),
        )
        self.assertEqual(
            resolve("custom", "imap.example.com", 1993),
            ("imap.example.com", 1993),
        )
        # Unknown providers are rejected.
        with self.assertRaisesRegex(ValueError, "unknown provider"):
            resolve("unknown_provider")
        # Custom provider requires explicit host/port.
        with self.assertRaisesRegex(ValueError, "custom provider requires"):
            resolve("custom")
        with self.assertRaisesRegex(ValueError, "custom provider requires"):
            resolve("custom", "imap.example.com")

    def test_provider_lookup_is_case_and_whitespace_insensitive(self):
        resolve = self.helpers._resolve_provider_hosts
        self.assertEqual(resolve("Yandex"), ("imap.yandex.ru", 993))
        self.assertEqual(resolve("  MAILRU  "), ("imap.mail.ru", 993))
        self.assertEqual(resolve("Vk_Workspace"), ("mail.vk.works", 993))

    def test_unknown_subject_and_incomplete_input_are_refused(self):
        self.assertIsNone(self.helpers._sso_binding_lookup("social:vk:absent"))
        for subject, email, password in (
            ("", "a@example.com", "secret"),
            ("social:vk:7", "", "secret"),
            ("social:vk:7", "a@example.com", ""),
        ):
            with self.assertRaises(ValueError):
                self.helpers._sso_binding_put(subject, email, password)

    def test_dropping_a_binding_removes_the_login_path(self):
        self.helpers._sso_binding_put("social:vk:7", "a@example.com", "Secret_2026")

        self.assertEqual(self.helpers._sso_binding_drop("social:vk:7"), 1)
        self.assertIsNone(self.helpers._sso_binding_lookup("social:vk:7"))
        self.assertEqual(self.helpers._sso_binding_drop("social:vk:7"), 0)

    def test_a_binding_encrypted_under_a_rotated_key_reads_as_absent(self):
        self.helpers._sso_binding_put("social:vk:7", "a@example.com", "Secret_2026")
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "UPDATE sso_mailbox_bindings SET imap_pass_enc = ?",
                ("unreadable-ciphertext",),
            )

        # Not a crash and not a half-built session: the caller must fall back.
        self.assertIsNone(self.helpers._sso_binding_lookup("social:vk:7"))


if __name__ == "__main__":
    unittest.main()


class SsoCallbackContractTest(unittest.TestCase):
    """The callback source must offer exactly one way in: the cabinet binding."""

    @classmethod
    def setUpClass(cls):
        start = SOURCE.index('@app.get("/sso/callback")')
        end = SOURCE.index('@app.get("/admin/sso/login")', start)
        cls.callback = SOURCE[start:end]

    def test_the_binding_is_the_only_source_of_the_mailbox(self):
        self.assertIn("_sso_binding_lookup(sso_subject)", self.callback)
        self.assertIn("if not binding:", self.callback)
        # No AD, no employee directory, no foreign control plane on this path.
        for retired in (
            "_resolve_sso_employee",
            "_mail_recovery_password",
            "identity_employee=",
        ):
            self.assertNotIn(retired, self.callback)

    def test_a_missing_binding_points_at_the_cabinet_instead_of_failing(self):
        self.assertIn("status_code=403", self.callback)
        self.assertIn("личном кабинете", self.callback)

    def test_the_session_is_built_without_a_directory_lookup(self):
        self.assertIn("directory_lookup=False", self.callback)

    def test_the_retired_resolvers_are_gone_from_the_service(self):
        for retired in (
            "def _resolve_sso_employee(",
            "def _mail_recovery_password(",
            "async def _session_payload_from_portal_mail_employee(",
            '@app.post("/api/auth/ad/login")',
            '@app.post("/api/auth/telegram/start")',
        ):
            self.assertNotIn(retired, SOURCE)
