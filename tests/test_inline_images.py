"""Inline compose image MIME regression without starting the web server."""

import base64
import email
import email.utils
from pathlib import Path
import re
import types
import unittest
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")


def _load_helper():
    start = SOURCE.index("_INLINE_IMAGE_RE =")
    end = SOURCE.index("\n\ndef _calendar_escape", start)
    namespace = {
        "Any": Any,
        "Dict": Dict,
        "List": List,
        "Tuple": Tuple,
        "base64": base64,
        "email": email,
        "re": re,
        "CFG": types.SimpleNamespace(MAIL_DOMAIN="example.com"),
    }
    exec(compile(SOURCE[start:end], str(ROOT / "rupochta_server.py"), "exec"), namespace)
    return namespace["_extract_inline_images"]


class InlineImageTests(unittest.TestCase):
    def test_data_image_becomes_a_bounded_cid_part(self):
        image = b"small-png"
        encoded = base64.b64encode(image).decode("ascii")
        html, parts = _load_helper()(f'<p>До<img src="data:image/png;base64,{encoded}">После</p>')
        self.assertRegex(html, r'src="cid:[^\"]+"')
        self.assertEqual(parts[0]["content"], image)
        self.assertEqual(parts[0]["subtype"], "png")

    def test_invalid_data_image_is_removed(self):
        html, parts = _load_helper()('<img src="data:image/png;base64,not-valid***">')
        self.assertNotIn("data:image", html)
        self.assertEqual(parts, [])


if __name__ == "__main__":
    unittest.main()
