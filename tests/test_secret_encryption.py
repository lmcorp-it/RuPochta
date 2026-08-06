from pathlib import Path
import unittest


SERVER = (Path(__file__).resolve().parents[1] / "rupochta_server.py").read_text(encoding="utf-8")


class SecretEncryptionFailClosedTests(unittest.TestCase):
    """SEC-008: stored-secret encryption must fail closed. There must be no
    XOR/HMAC fallback cipher and the encryption key must never silently
    reuse RUPOCHTA_INTERNAL_TOKEN."""

    def test_no_xor_fallback_cipher_is_used_to_encrypt(self):
        encrypt_fn = SERVER.split(
            "def _encrypt_secret_with_key(", 1
        )[1].split("\ndef ", 1)[0]
        self.assertNotIn("xor$", encrypt_fn)
        self.assertIn("raise RuntimeError", encrypt_fn)

    def test_secret_key_does_not_fall_back_to_internal_token(self):
        key_setup = SERVER.split('_SECRET_KEY = os.environ.get(', 1)[1].split(")", 1)[0]
        self.assertIn('"WEBMAIL_SECRET_KEY"', key_setup)
        self.assertNotIn("RUPOCHTA_INTERNAL_TOKEN", key_setup)

    def test_decrypt_rejects_legacy_xor_ciphertext(self):
        decrypt_fn = SERVER.split(
            "def _decrypt_secret_with_keys(", 1
        )[1].split("\ndef ", 1)[0]
        self.assertIn('ciphertext.startswith("xor$")', decrypt_fn)
        # The legacy branch must return None (unrecoverable), not attempt to
        # decrypt with the removed XOR cipher.
        legacy_branch = decrypt_fn.split('ciphertext.startswith("xor$")', 1)[1]
        legacy_branch = legacy_branch.split("if _FERNET_AVAILABLE", 1)[0]
        self.assertIn("return None, None", legacy_branch)
        self.assertNotIn("_hm.new", legacy_branch)


if __name__ == "__main__":
    unittest.main()
