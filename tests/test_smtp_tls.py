from pathlib import Path
import re
import unittest


SERVER = (Path(__file__).resolve().parents[1] / "rupochta_server.py").read_text(encoding="utf-8")


class SmtpTlsVerificationTests(unittest.TestCase):
    """Submission carries the mailbox password, so TLS must be verified unless
    the operator explicitly opts out for a local relay."""

    def test_verification_is_on_unless_explicitly_disabled(self):
        self.assertIn('MAIL_SMTP_VERIFY_TLS", "1"', SERVER)
        send = SERVER.split("def smtp_send(", 1)[1].split("\ndef ", 1)[0]
        relaxations = re.findall(r"ctx\.(?:check_hostname|verify_mode)\s*=", send)
        self.assertEqual(len(relaxations), 2, "expected exactly the opt-out branch")
        guard = send.split("ctx = ssl.create_default_context()", 1)[1]
        self.assertTrue(
            guard.lstrip().startswith("if not CFG.SMTP_VERIFY_TLS and not external:"),
            "the relaxation must sit behind the opt-out flag and never apply "
            "to an external provider",
        )

    def test_external_imap_hosts_are_always_verified(self):
        connect = SERVER.split("def _imap_connect(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("not (host or port)", connect)
        self.assertIn("IMAP_VERIFY_TLS", connect)
        self.assertIn("_host_resolves_to_loopback_only", connect)


if __name__ == "__main__":
    unittest.main()
