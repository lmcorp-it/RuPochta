import json
from pathlib import Path
import re
import unittest


MAIL_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_TYPOGRAPHY_MEMBERS = {
    "fontFamily",
    "fontSize",
    "fontWeight",
    "letterSpacing",
    "lineHeight",
}


def normative_typography():
    front_matter = (MAIL_ROOT / "DESIGN.md").read_text(encoding="utf-8").split("---", 2)[1]
    typography = {}
    current = None
    in_typography = False
    for line in front_matter.splitlines():
        if line == "typography:":
            in_typography = True
            continue
        if in_typography and line and not line.startswith(" "):
            break
        style_match = re.fullmatch(r"  ([A-Za-z]+):", line)
        if in_typography and style_match:
            current = style_match.group(1)
            typography[current] = {}
            continue
        member_match = re.fullmatch(r"    ([A-Za-z]+): (.+)", line)
        if current and member_match:
            key, raw_value = member_match.groups()
            typography[current][key] = raw_value.strip('"')
    return typography


def dimension(raw_value):
    match = re.fullmatch(r"(-?\d+(?:\.\d+)?)([a-z]+)", raw_value)
    if not match:
        raise AssertionError(f"invalid dimension: {raw_value}")
    return {"value": float(match.group(1)), "unit": match.group(2)}


class DesignTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = normative_typography()
        cls.export = json.loads(
            (MAIL_ROOT / "tokens.json").read_text(encoding="utf-8")
        )["typography"]

    def test_typography_values_have_all_dtcg_members(self):
        self.assertEqual(set(self.export), set(self.source))
        for name, token in self.export.items():
            self.assertEqual(token["$type"], "typography", name)
            self.assertEqual(set(token["$value"]), REQUIRED_TYPOGRAPHY_MEMBERS, name)
            self.assertIn(token["$value"]["fontSize"]["unit"], {"px", "rem"}, name)
            self.assertIn(
                token["$value"]["letterSpacing"]["unit"],
                {"px", "rem"},
                name,
            )

    def test_typography_export_matches_normative_design(self):
        for name, source_value in self.source.items():
            exported = self.export[name]["$value"]
            self.assertEqual(exported["fontFamily"], source_value["fontFamily"], name)
            self.assertEqual(exported["fontSize"], dimension(source_value["fontSize"]), name)
            self.assertEqual(exported["fontWeight"], int(source_value["fontWeight"]), name)
            self.assertEqual(
                exported["letterSpacing"],
                dimension(source_value["letterSpacing"]),
                name,
            )
            self.assertEqual(exported["lineHeight"], float(source_value["lineHeight"]), name)


if __name__ == "__main__":
    unittest.main()
