"""The user-facing screen for connecting an external mailbox (issue #3).

The endpoints are session-scoped: a signed-in user connects their own mailbox,
so nothing here may reach the admin API key or another person's binding.
"""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")
STATIC = ROOT / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")


def _endpoint(decorator: str) -> str:
    start = SERVER.index(decorator)
    return SERVER[start:SERVER.index("\n\n\n", start)]


class ExternalMailboxEndpointTests(unittest.TestCase):
    def test_the_endpoints_are_session_scoped_not_admin_scoped(self):
        for decorator in (
            '@app.get("/api/mailbox/external")',
            '@app.post("/api/mailbox/external")',
            '@app.delete("/api/mailbox/external")',
        ):
            handler = _endpoint(decorator)
            self.assertNotIn("_require_mail_admin_api_access", handler, decorator)
            self.assertIn("get_session_or_401", handler + SERVER.split(
                "def _external_mailbox_subject", 1)[1][:400], decorator)

    def test_connecting_binds_the_callers_own_subject(self):
        handler = _endpoint('@app.post("/api/mailbox/external")')
        # The subject comes from the session, never from the request body.
        self.assertIn("_external_mailbox_subject(request)", handler)
        self.assertNotIn("body.subject", handler)

    def test_credentials_are_verified_before_they_are_stored(self):
        handler = _endpoint('@app.post("/api/mailbox/external")')
        self.assertLess(
            handler.index("_imap_connect("),
            handler.index("_sso_binding_put("),
            "the mailbox must be dialled before the password is written",
        )

    def test_disconnect_stays_provider_scoped(self):
        handler = _endpoint('@app.delete("/api/mailbox/external")')
        self.assertIn("_sso_bindings_drop_external", handler)
        self.assertIn("provider", handler)

    def test_the_state_response_carries_no_secret(self):
        handler = _endpoint('@app.get("/api/mailbox/external")')
        for leak in ("imap_pass", "password", "decrypt_secret"):
            self.assertNotIn(leak, handler)


class ExternalMailboxSurfaceTests(unittest.TestCase):
    def test_settings_expose_the_external_mailbox_tab(self):
        self.assertIn('data-settings-tab="external"', INDEX)
        self.assertIn('data-settings-panel="external"', INDEX)
        for element_id in (
            "external-mailbox-form",
            "external-mailbox-provider",
            "external-mailbox-email",
            "external-mailbox-password",
            "external-mailbox-disconnect",
        ):
            self.assertIn(f'id="{element_id}"', INDEX)

    def test_custom_endpoint_fields_exist_and_start_hidden(self):
        self.assertIn('id="external-mailbox-custom" class="settings-form hidden"', INDEX)
        for element_id in (
            "external-mailbox-imap-host",
            "external-mailbox-imap-port",
            "external-mailbox-smtp-host",
            "external-mailbox-smtp-port",
        ):
            self.assertIn(f'id="{element_id}"', INDEX)

    def test_the_password_field_is_masked_and_cleared_after_success(self):
        self.assertIn('type="password" id="external-mailbox-password"', INDEX)
        connect = APP.split("async function connectExternalMailbox", 1)[1].split(
            "\nasync function ", 1
        )[0]
        self.assertIn('$("#external-mailbox-password").value = "";', connect)

    def test_the_screen_talks_to_the_session_scoped_endpoint(self):
        self.assertIn('apiFetch("/mailbox/external"', APP)
        self.assertIn('method: "DELETE"', APP.split("disconnectExternalMailbox", 1)[1][:400])
        self.assertNotIn("/admin/sso_binding", APP)


if __name__ == "__main__":
    unittest.main()
