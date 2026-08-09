"""The desktop client is a shell over the user's own deployment (issue #8).

These checks are cheap on purpose: the real proof is the build job, which
produces installers on each system's own runner. What is asserted here is the
part a broken edit would silently change — the identity of the app, the bundle
targets each platform needs, and the rule that the client never sends a mailbox
password over plain http.
"""

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
CONFIG = json.loads((DESKTOP / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
CONNECT_UI = (DESKTOP / "ui" / "index.html").read_text(encoding="utf-8")


class DesktopBundleTests(unittest.TestCase):
    def test_the_identity_is_stable(self):
        # The identifier is what upgrades key off; renaming it strands installs.
        self.assertEqual(CONFIG["identifier"], "ru.rupochta.desktop")
        self.assertEqual(CONFIG["productName"], "RuPochta")

    def test_every_platform_has_its_installer_target(self):
        targets = set(CONFIG["bundle"]["targets"])
        for required in ("deb", "rpm", "appimage", "msi", "nsis", "dmg"):
            self.assertIn(required, targets, required)

    def test_icons_referenced_by_the_bundle_exist(self):
        for icon in CONFIG["bundle"]["icon"]:
            self.assertTrue((DESKTOP / "src-tauri" / icon).is_file(), icon)

    def test_the_window_loads_the_bundled_connect_screen(self):
        self.assertEqual(CONFIG["build"]["frontendDist"], "../ui")
        self.assertTrue((DESKTOP / "ui" / "index.html").is_file())


class ConnectScreenTests(unittest.TestCase):
    def test_plain_http_is_refused_for_a_remote_server(self):
        # The next screen asks for a mailbox password, so the transport has to
        # be encrypted; loopback stays allowed for development.
        self.assertIn('url.protocol !== "https:"', CONNECT_UI)
        self.assertIn('url.hostname !== "localhost"', CONNECT_UI)
        self.assertIn('url.hostname !== "127.0.0.1"', CONNECT_UI)

    def test_the_address_is_remembered_between_runs(self):
        self.assertIn('localStorage.getItem("rupochta.server")', CONNECT_UI)
        self.assertIn('localStorage.setItem("rupochta.server"', CONNECT_UI)


if __name__ == "__main__":
    unittest.main()
