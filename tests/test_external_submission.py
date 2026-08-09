"""The submission endpoint follows the mailbox, not the deployment.

An external mailbox (ADR-0071) must be sent through its own provider: relaying a
Mail.ru address through the local SMTP is refused by the receiving side. These
tests run the real helpers out of the server source against a temporary SQLite
file and a fake smtplib, so the host actually dialled is asserted.
"""

import sqlite3
import ssl
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")

EXTERNAL_TABLE = """
CREATE TABLE sso_external_mailbox_bindings (
    subject_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    imap_pass_enc TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL,
    provider TEXT NOT NULL,
    smtp_host TEXT,
    smtp_port INTEGER
)
"""


def _extract_function(name: str) -> str:
    start = SOURCE.index(f"def {name}(")
    ends = [
        SOURCE.find(marker, start + 1)
        for marker in ("\n\ndef ", "\n\nclass ", "\n\n@app", "\n\n# ")
    ]
    return SOURCE[start:min(position for position in ends if position != -1)]


def _load_presets() -> Dict[str, Dict[str, Any]]:
    start = SOURCE.index("MAIL_PROVIDER_PRESETS: Dict")
    end = SOURCE.index("\n\n# " + "-" * 75, start)
    namespace: Dict[str, Any] = {"Dict": Dict, "Any": Any}
    exec(SOURCE[start:end], namespace)  # noqa: S102 - trusted own source
    return namespace["MAIL_PROVIDER_PRESETS"]


def _load_endpoint_helpers(db_path: str):
    @contextmanager
    def _db_connection():
        con = sqlite3.connect(db_path)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    namespace: Dict[str, Any] = {
        "_db_connection": _db_connection,
        "Optional": Optional,
        "Tuple": Tuple,
        "Any": Any,
        "MAIL_PROVIDER_PRESETS": _load_presets(),
        # SEC-003 egress policy is exercised in test_custom_endpoint_egress.py
        # against the real implementation. These helpers resolve provider
        # presets, so the guard is stubbed out to keep them off the network.
        "_reject_ssrf_targets": lambda host, port: None,
    }
    for name in (
        "_resolve_provider_smtp",
        "_mailbox_external_row",
        "_mailbox_imap_endpoint",
        "_mailbox_submission_endpoint",
    ):
        exec(_extract_function(name), namespace)  # noqa: S102 - trusted own source
    return types.SimpleNamespace(**namespace)


class FakeMessage(dict):
    def as_bytes(self):
        return b"raw message"


class FakeSmtp:
    """Records the endpoint and whether the session was implicit TLS."""

    def __init__(self, log: List[Dict[str, Any]], implicit: bool):
        self.log = log
        self.implicit = implicit
        self.entry: Dict[str, Any] = {}

    def __call__(self, host, port, *args, **kwargs):
        self.entry = {
            "host": host,
            "port": port,
            "implicit_tls": self.implicit,
            "context": kwargs.get("context"),
            "starttls": False,
        }
        self.log.append(self.entry)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def ehlo(self):
        return "OK", []

    def starttls(self, context=None):
        self.entry["starttls"] = True
        self.entry["context"] = context

    def login(self, user, _password):
        self.entry["user"] = user

    def send_message(self, _msg, from_addr=None, to_addrs=None):
        self.entry["to"] = list(to_addrs or [])


def _load_smtp_send(endpoint, verify_tls: bool = True):
    calls: List[Dict[str, Any]] = []
    namespace: Dict[str, Any] = {
        "smtplib": types.SimpleNamespace(
            SMTP=FakeSmtp(calls, implicit=False),
            SMTP_SSL=FakeSmtp(calls, implicit=True),
        ),
        "ssl": ssl,
        "CFG": types.SimpleNamespace(
            SMTP_HOST="managed.test", SMTP_PORT=587, SMTP_VERIFY_TLS=verify_tls
        ),
        "_mailbox_submission_endpoint": endpoint,
        "_build_mail_message": lambda *args, **kwargs: FakeMessage(),
        "_imap_save_sent_copy": lambda *args, **kwargs: "Sent",
        "log": types.SimpleNamespace(warning=lambda *a, **k: None),
        "Optional": Optional,
        "List": List,
        "Dict": Dict,
        "Any": Any,
    }
    exec(_extract_function("smtp_send"), namespace)  # noqa: S102 - trusted own source
    return namespace["smtp_send"], calls


class ProviderSubmissionPresetTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db_path = str(Path(self._dir.name) / "bindings.db")
        with sqlite3.connect(self.db_path) as con:
            con.execute(EXTERNAL_TABLE)
        self.helpers = _load_endpoint_helpers(self.db_path)

    def _insert(self, email, provider, imap_host, smtp_host, smtp_port):
        with sqlite3.connect(self.db_path) as con:
            con.execute(
                "INSERT INTO sso_external_mailbox_bindings VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"hash-{email}",
                    email,
                    "enc",
                    "2026-08-05T00:00:00+00:00",
                    "2026-08-05T00:00:00+00:00",
                    imap_host,
                    993,
                    provider,
                    smtp_host,
                    smtp_port,
                ),
            )

    def test_known_provider_resolves_to_its_own_submission_endpoint(self):
        self.assertEqual(
            self.helpers._resolve_provider_smtp("mailru"), ("smtp.mail.ru", 465)
        )
        self.assertEqual(
            self.helpers._resolve_provider_smtp("yandex"), ("smtp.yandex.ru", 465)
        )

    def test_managed_mailbox_has_no_endpoint_and_unknown_provider_is_refused(self):
        self.assertEqual(self.helpers._resolve_provider_smtp(None), (None, None))
        with self.assertRaises(ValueError):
            self.helpers._resolve_provider_smtp("hotmail")

    def test_custom_provider_may_omit_submission_but_not_the_port(self):
        self.assertEqual(
            self.helpers._resolve_provider_smtp("custom", "smtp.corp.local", 587),
            ("smtp.corp.local", 587),
        )
        self.assertEqual(self.helpers._resolve_provider_smtp("custom"), (None, None))
        with self.assertRaises(ValueError):
            self.helpers._resolve_provider_smtp("custom", "smtp.corp.local", None)

    def test_endpoints_are_read_back_from_the_stored_binding(self):
        self._insert("person@mail.ru", "mailru", "imap.mail.ru", "smtp.mail.ru", 465)
        self.assertEqual(
            self.helpers._mailbox_submission_endpoint("person@mail.ru"),
            ("smtp.mail.ru", 465),
        )
        self.assertEqual(
            self.helpers._mailbox_imap_endpoint("PERSON@MAIL.RU"),
            ("imap.mail.ru", 993),
        )

    def test_a_row_written_before_the_columns_existed_falls_back_to_the_preset(self):
        self._insert("legacy@yandex.ru", "yandex", "imap.yandex.ru", None, None)
        self.assertEqual(
            self.helpers._mailbox_submission_endpoint("legacy@yandex.ru"),
            ("smtp.yandex.ru", 465),
        )

    def test_a_managed_mailbox_resolves_to_nothing(self):
        self.assertEqual(
            self.helpers._mailbox_submission_endpoint("staff@example.com"), (None, None)
        )
        self.assertEqual(self.helpers._mailbox_imap_endpoint(""), (None, None))


class SubmissionRoutingTests(unittest.TestCase):
    def test_managed_mailbox_keeps_the_deployment_smtp_with_starttls(self):
        send, calls = _load_smtp_send(lambda _email: (None, None))
        send("staff@example.com", "pw", "staff@example.com", ["a@b.test"], [], [],
             "s", "<p>h</p>", "t")
        self.assertEqual((calls[0]["host"], calls[0]["port"]), ("managed.test", 587))
        self.assertFalse(calls[0]["implicit_tls"])
        self.assertTrue(calls[0]["starttls"])

    def test_external_mailbox_is_submitted_through_its_own_provider(self):
        send, calls = _load_smtp_send(lambda _email: ("smtp.mail.ru", 465))
        send("person@mail.ru", "pw", "person@mail.ru", ["a@b.test"], [], [],
             "s", "<p>h</p>", "t")
        self.assertEqual((calls[0]["host"], calls[0]["port"]), ("smtp.mail.ru", 465))
        self.assertTrue(calls[0]["implicit_tls"], "port 465 is implicit TLS")

    def test_the_local_relay_opt_out_never_reaches_an_external_provider(self):
        send, calls = _load_smtp_send(lambda _email: ("smtp.mail.ru", 465), verify_tls=False)
        send("person@mail.ru", "pw", "person@mail.ru", ["a@b.test"], [], [],
             "s", "<p>h</p>", "t")
        context = calls[0]["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_the_opt_out_still_applies_to_the_deployments_own_relay(self):
        send, calls = _load_smtp_send(lambda _email: (None, None), verify_tls=False)
        send("staff@example.com", "pw", "staff@example.com", ["a@b.test"], [], [],
             "s", "<p>h</p>", "t")
        context = calls[0]["context"]
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)


if __name__ == "__main__":
    unittest.main()
