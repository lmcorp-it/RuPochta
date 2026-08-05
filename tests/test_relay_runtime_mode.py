import ast
from pathlib import Path
import types
import typing
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "rupochta_server.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))


def load_function(name, namespace):
    node = next(
        item
        for item in TREE.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


class RelayRuntimeModeTests(unittest.TestCase):
    def test_explicit_external_relay_is_not_reported_as_backup(self):
        namespace = {
            "CFG": types.SimpleNamespace(RUNTIME_MODE="relay"),
            "_mail_admin_available": lambda: False,
        }
        runtime_mode = load_function("_mail_runtime_mode", namespace)
        self.assertEqual(runtime_mode(), "relay")

    def test_local_mailserver_stays_primary(self):
        namespace = {
            "CFG": types.SimpleNamespace(RUNTIME_MODE="relay"),
            "_mail_admin_available": lambda: True,
        }
        runtime_mode = load_function("_mail_runtime_mode", namespace)
        self.assertEqual(runtime_mode(), "primary")

    def test_unconfigured_remote_mode_keeps_backup_compatibility(self):
        namespace = {
            "CFG": types.SimpleNamespace(RUNTIME_MODE=""),
            "_mail_admin_available": lambda: False,
        }
        runtime_mode = load_function("_mail_runtime_mode", namespace)
        self.assertEqual(runtime_mode(), "backup")

    def test_relay_readiness_uses_direct_imap_and_smtp_without_degradation(self):
        namespace = {
            "Any": typing.Any,
            "Dict": typing.Dict,
            "Optional": typing.Optional,
            "_mail_runtime_mode": lambda: "relay",
            "_mail_admin_available": lambda: False,
            "_mail_control_snapshot": lambda *_args: self.fail(
                "relay mode must not call the backup control plane"
            ),
        }
        build_payload = load_function("_build_ready_payload", namespace)
        payload = build_payload(True, "imap ok", True, "smtp ok")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "relay")
        self.assertFalse(payload["degraded"])
        self.assertEqual(payload["checks"]["mail_control"]["status"], "not_applicable")
        self.assertEqual(payload["checks"]["smtp_client"]["status"], "not_applicable")


if __name__ == "__main__":
    unittest.main()
