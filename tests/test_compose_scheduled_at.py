"""Send-later has to survive the compose parser.

`/api/scheduled/send` reuses `_parse_compose_request`, which validates JSON
bodies against `SendBody` and rebuilds multipart bodies field by field. A
`scheduled_at` that either model drops never reaches `_parse_iso_future`, and
every scheduled send answers 400 — which is what happened until `SendBody`
gained the field and the multipart branch started reading it.

These tests run the real definitions out of the server source, so they fail if
the field is removed from either path again.
"""

import asyncio
import re
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")


def _extract(pattern: str) -> str:
    match = re.search(pattern, SOURCE, re.S | re.M)
    if not match:
        raise AssertionError(f"could not find {pattern!r} in rupochta_server.py")
    return match.group(0)


def _load_namespace() -> Dict[str, Any]:
    namespace: Dict[str, Any] = {
        "BaseModel": BaseModel,
        "Field": Field,
        "Optional": Optional,
        "List": List,
        "Dict": Dict,
        "Any": Any,
        # The parser only annotates with Request; the fakes below stand in for it.
        "Request": object,
    }
    exec(_extract(r"^class SendBody\(BaseModel\):.*?(?=^class )"), namespace)
    exec(_extract(r"^class DraftBody\(BaseModel\):.*?(?=^class )"), namespace)
    exec(_extract(r"^async def _parse_compose_request.*?(?=^# ---)"), namespace)
    return namespace


class FakeUploadFile:
    """Just enough of starlette's UploadFile for the parser's attachment loop."""

    def __init__(self, filename: str, content: bytes, content_type: str) -> None:
        self.filename = filename
        self.content_type = content_type
        self._content = content

    async def read(self) -> bytes:
        return self._content


class FakeForm:
    def __init__(self, values: Dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def multi_items(self):
        return list(self._values.items())


class FakeRequest:
    def __init__(self, *, headers: Dict[str, str], json_body=None, form=None) -> None:
        self.headers = headers
        self._json_body = json_body
        self._form = form

    async def json(self):
        return self._json_body

    async def form(self):
        return self._form


class ComposeScheduledAtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ns = _load_namespace()

    def parse(self, request, *, draft_mode: bool):
        return asyncio.run(self.ns["_parse_compose_request"](request, draft_mode=draft_mode))

    def test_json_send_body_keeps_scheduled_at(self) -> None:
        request = FakeRequest(
            headers={"content-type": "application/json"},
            json_body={
                "to": "alice@example.com",
                "subject": "Later",
                "body": "text",
                "scheduled_at": "2099-01-01T09:00:00Z",
            },
        )
        payload = self.parse(request, draft_mode=False)
        self.assertEqual(payload["scheduled_at"], "2099-01-01T09:00:00Z")

    def test_multipart_send_reads_scheduled_at(self) -> None:
        form = FakeForm(
            {
                "to": "alice@example.com",
                "subject": "Later",
                "body": "text",
                "scheduled_at": "2099-01-01T09:00:00Z",
                "attachment": FakeUploadFile("a.txt", b"payload", "text/plain"),
            }
        )
        request = FakeRequest(
            headers={"content-type": "multipart/form-data; boundary=x"},
            form=form,
        )
        payload = self.parse(request, draft_mode=False)
        self.assertEqual(payload["scheduled_at"], "2099-01-01T09:00:00Z")
        self.assertEqual(payload["attachments"][0]["filename"], "a.txt")

    def test_scheduled_at_is_absent_from_drafts(self) -> None:
        """Drafts are never scheduled; the key must not leak into that path."""
        form = FakeForm(
            {
                "to": "alice@example.com",
                "subject": "Draft",
                "body": "text",
                "scheduled_at": "2099-01-01T09:00:00Z",
            }
        )
        request = FakeRequest(
            headers={"content-type": "multipart/form-data; boundary=x"},
            form=form,
        )
        payload = self.parse(request, draft_mode=True)
        self.assertNotIn("scheduled_at", payload)

    def test_missing_scheduled_at_stays_falsy(self) -> None:
        request = FakeRequest(
            headers={"content-type": "application/json"},
            json_body={"to": "alice@example.com", "subject": "Now", "body": "text"},
        )
        payload = self.parse(request, draft_mode=False)
        self.assertFalse(payload.get("scheduled_at"))


if __name__ == "__main__":
    unittest.main()
