"""Regression checks for personal mailbox ticket intake (ADR-0044)."""

import json
import sqlite3
import tempfile
import types
import unittest
import urllib.request
from contextlib import contextmanager
from email.utils import parseaddr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")


def _extract_function(name):
    start = SOURCE.index(f"def {name}(")
    ends = [SOURCE.find(marker, start + 1) for marker in ("\n\ndef ", "\n\nclass ", "\n\n@app", "\n\n# ")]
    return SOURCE[start:min(position for position in ends if position != -1)]


class TicketIntakeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "intake.db")
        with sqlite3.connect(self.db_path) as con:
            con.execute("CREATE TABLE sso_mailbox_bindings (subject_hash TEXT PRIMARY KEY, email TEXT, imap_pass_enc TEXT, created_at TEXT, updated_at TEXT, imap_host TEXT, imap_port INTEGER, provider TEXT)")
            con.execute("CREATE TABLE ticket_intake_mailboxes (email TEXT PRIMARY KEY, imap_pass_enc TEXT, last_uid INTEGER DEFAULT 0, initialized INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)")

        @contextmanager
        def db_connection():
            con = sqlite3.connect(self.db_path)
            con.row_factory = sqlite3.Row
            try:
                yield con
                con.commit()
            finally:
                con.close()

        self.namespace = {
            "_db_connection": db_connection,
            "_utc_now_iso": lambda: "2026-07-31T00:00:00+00:00",
            "encrypt_secret": lambda value: f"enc:{value[::-1]}",
            "decrypt_secret": lambda value: value[4:][::-1],
            "sqlite3": sqlite3,
            "List": list,
        }
        for name in ("_ticket_intake_put", "_ticket_intake_drop", "_ticket_intake_rows", "_ticket_intake_set_cursor"):
            exec(_extract_function(name), self.namespace)  # noqa: S102 - trusted own source

    def tearDown(self):
        self.tempdir.cleanup()

    def test_credentials_are_encrypted_and_delete_clears_intake_and_sso(self):
        self.namespace["_ticket_intake_put"]("User@example.com", "Secret_2026")
        row = self.namespace["_ticket_intake_rows"]()[0]
        self.assertEqual(row["email"], "user@example.com")
        self.assertNotIn("Secret_2026", row["imap_pass_enc"])
        self.namespace["_ticket_intake_set_cursor"]("user@example.com", 42)
        self.assertEqual(self.namespace["_ticket_intake_rows"]()[0]["last_uid"], 42)
        self.assertEqual(self.namespace["_ticket_intake_drop"]("user@example.com"), 1)
        self.assertEqual(self.namespace["_ticket_intake_rows"](), [])

    def test_message_payload_uses_stable_dedup_key(self):
        captured = {}

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        def urlopen(request, timeout):
            captured.update(json.loads(request.data))
            self.assertEqual(request.get_header("X-integration-secret"), "secret")
            self.assertEqual(timeout, 15)
            return Response()

        namespace = {
            "CFG": types.SimpleNamespace(TICKET_INTAKE_URL="http://portal/intake", TICKET_INTAKE_SECRET="secret"),
            "Any": object,
            "Dict": dict,
            "json": json,
            "parseaddr": parseaddr,
            "urllib": types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=urlopen)),
            "_html_to_plain_text": lambda value: value,
        }
        exec(_extract_function("_ticket_intake_post"), namespace)  # noqa: S102 - trusted own source
        namespace["_ticket_intake_post"]("box@example.com", 7, {
            "from": "Иван <ivan@example.ru>",
            "subject": "Не работает VPN",
            "message_id": "message-42",
            "body_text": "Помогите",
            "attachments": [],
        })
        self.assertEqual(captured["externalId"], "mail:box@example.com:message-42")
        self.assertEqual(captured["requesterEmail"], "ivan@example.ru")
        self.assertEqual(captured["title"], "Не работает VPN")

    def test_admin_api_exposes_registration_status_without_credentials(self):
        self.assertIn('@app.get("/api/admin/ticket_intake")', SOURCE)
        self.assertIn('"active": bool(row)', SOURCE)


if __name__ == "__main__":
    unittest.main()
