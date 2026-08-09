"""Behavioral regression tests for the production bootstrap helpers.

The real helper functions are sourced by Bash and exercised with deterministic
fake commands.  Nothing here reads the bootstrap source or touches host nginx,
systemd, DNS, or Let's Encrypt state.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASH = Path(r"C:\Program Files\Git\bin\bash.exe") if os.name == "nt" else Path("/bin/bash")


def _bash_path(path: Path) -> str:
    return path.resolve().as_posix()


def _run_bash(script: str, *, test_root: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["TEST_ROOT"] = _bash_path(test_root)
    return subprocess.run(
        [str(BASH), "--noprofile", "--norc", "-c", "set -uo pipefail\n" + textwrap.dedent(script)],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )


class BootstrapHelperTests(unittest.TestCase):
    def run_helper(self, script: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="rupochta-bootstrap-test-") as temp_dir:
            return _run_bash(script, test_root=Path(temp_dir))

    def assert_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(
            result.returncode,
            0,
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_certificate_names_use_public_dns_not_hosts_database(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            DOMAIN_PUNYCODE=xn--80a1acdmd4a.xn--p1ai
            TEST_LOG="$TEST_ROOT/commands.log"

            dig() {
              local name="${@: -1}"
              printf 'dig %s\n' "$*" >> "$TEST_LOG"
              if [ "$name" = "www.$DOMAIN_PUNYCODE" ] && [ "$2" = "A" ]; then
                printf '203.0.113.10\n'
              fi
            }
            getent() {
              printf 'getent %s\n' "$*" >> "$TEST_LOG"
              printf '127.0.1.1 %s\n' "$2"
            }

            build_certificate_names
            printf 'SAN=%s\n' "${CERT_NAMES[@]}"
            printf '%s' "$(cat "$TEST_LOG")"
            """
        )
        self.assert_ok(result)
        san_lines = [line for line in result.stdout.splitlines() if line.startswith("SAN=")]
        self.assertEqual(
            san_lines,
            [
                "SAN=xn--80a1acdmd4a.xn--p1ai",
                "SAN=www.xn--80a1acdmd4a.xn--p1ai",
            ],
        )
        self.assertNotIn("getent ", result.stdout)
        self.assertIn("dig +short A mail.xn--80a1acdmd4a.xn--p1ai", result.stdout)

    def test_explicit_tls_failure_is_nonzero_and_keeps_bootstrap_site(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            DOMAIN_PUNYCODE=xn--80a1acdmd4a.xn--p1ai
            CERT_DIR="$TEST_ROOT/live/$DOMAIN_PUNYCODE"
            SITE_AVAILABLE_DIR="$TEST_ROOT/sites-available"
            SITE_LINK="$TEST_ROOT/sites-enabled/rupochta-rf.conf"
            ACME_WEBROOT="$TEST_ROOT/acme"
            TEST_LOG="$TEST_ROOT/commands.log"
            mkdir -p "$SITE_AVAILABLE_DIR" "$(dirname "$SITE_LINK")" "$ACME_WEBROOT"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf.conf"
            printf '%s\n' "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf" > "$SITE_LINK"

            dig() { return 0; }
            certbot() { printf 'certbot %s\n' "$*" >> "$TEST_LOG"; return 17; }
            nginx() { printf 'nginx %s\n' "$*" >> "$TEST_LOG"; return 0; }
            systemctl() { printf 'systemctl %s\n' "$*" >> "$TEST_LOG"; return 0; }
            ln() { printf '%s\n' "${@: -2:1}" > "${@: -1}"; }
            readlink() { cat "$1"; }

            if request_tls_certificate; then
              printf 'unexpected success\n' >&2
              exit 90
            fi
            printf 'TARGET=%s\n' "$(readlink "$SITE_LINK")"
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        self.assertIn("TARGET=", result.stdout)
        self.assertIn("rupochta-rf-bootstrap.conf", result.stdout)
        self.assertIn("certbot ", result.stdout)
        self.assertNotIn("systemctl reload nginx", result.stdout)

    def test_existing_certificate_is_checked_and_expanded_idempotently(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            DOMAIN_PUNYCODE=xn--80a1acdmd4a.xn--p1ai
            CERT_DIR="$TEST_ROOT/live/$DOMAIN_PUNYCODE"
            SITE_AVAILABLE_DIR="$TEST_ROOT/sites-available"
            SITE_LINK="$TEST_ROOT/sites-enabled/rupochta-rf.conf"
            ACME_WEBROOT="$TEST_ROOT/acme"
            ACME_EMAIL=postmaster@example.test
            TEST_LOG="$TEST_ROOT/commands.log"
            mkdir -p "$CERT_DIR" "$SITE_AVAILABLE_DIR" "$(dirname "$SITE_LINK")" "$ACME_WEBROOT"
            printf 'certificate\n' > "$CERT_DIR/fullchain.pem"
            printf 'private key\n' > "$CERT_DIR/privkey.pem"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf.conf"
            printf '%s\n' "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf" > "$SITE_LINK"

            dig() { printf '203.0.113.10\n'; }
            certbot() { printf 'CERTBOT %s\n' "$*" >> "$TEST_LOG"; return 0; }
            nginx() { printf 'NGINX %s\n' "$*" >> "$TEST_LOG"; return 0; }
            systemctl() { printf 'SYSTEMCTL %s\n' "$*" >> "$TEST_LOG"; return 0; }
            ln() { printf '%s\n' "${@: -2:1}" > "${@: -1}"; }
            readlink() { cat "$1"; }

            request_tls_certificate
            request_tls_certificate
            printf 'TARGET=%s\n' "$(readlink "$SITE_LINK")"
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        certbot_lines = [line for line in result.stdout.splitlines() if line.startswith("CERTBOT ")]
        self.assertEqual(len(certbot_lines), 2)
        self.assertEqual(certbot_lines[0], certbot_lines[1])
        for expected in (
            "--cert-name xn--80a1acdmd4a.xn--p1ai",
            "--expand",
            "--keep-until-expiring",
            "-d xn--80a1acdmd4a.xn--p1ai",
            "-d www.xn--80a1acdmd4a.xn--p1ai",
            "-d mail.xn--80a1acdmd4a.xn--p1ai",
        ):
            self.assertIn(expected, certbot_lines[0])
        target_lines = [line for line in result.stdout.splitlines() if line.startswith("TARGET=")]
        self.assertEqual(len(target_lines), 1)
        self.assertTrue(target_lines[0].endswith("/rupochta-rf.conf"), target_lines[0])

    def test_unrelated_uvicorn_unit_is_not_taken_over(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            TEST_LOG="$TEST_ROOT/commands.log"
            systemctl() {
              printf 'systemctl %s\n' "$*" >> "$TEST_LOG"
              if [ "$1" = show ]; then
                printf 'ExecStart={ path=/usr/bin/python ; argv[]=/usr/bin/python -m uvicorn other_app:app; }\n'
              fi
            }

            if take_over_port_owner other-web.service '/usr/bin/python -m uvicorn other_app:app'; then
              printf 'unexpected takeover\n' >&2
              exit 91
            fi
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        self.assertIn("systemctl show", result.stdout)
        self.assertNotIn("disable --now", result.stdout)

    def test_proven_rupochta_unit_can_be_taken_over(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            TEST_LOG="$TEST_ROOT/commands.log"
            systemctl() {
              printf 'systemctl %s\n' "$*" >> "$TEST_LOG"
              if [ "$1" = show ]; then
                printf 'ExecStart={ path=/usr/bin/python ; argv[]=/usr/bin/python -m uvicorn rupochta_server:app; }\n'
              fi
            }

            take_over_port_owner legacy-rupochta.service \
              '/opt/old/.venv/bin/python -m uvicorn rupochta_server:app --port 18400'
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        self.assertIn("systemctl disable --now legacy-rupochta.service", result.stdout)

    def test_nginx_test_failure_restores_previous_site(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            SITE_AVAILABLE_DIR="$TEST_ROOT/sites-available"
            SITE_LINK="$TEST_ROOT/sites-enabled/rupochta-rf.conf"
            TEST_LOG="$TEST_ROOT/commands.log"
            mkdir -p "$SITE_AVAILABLE_DIR" "$(dirname "$SITE_LINK")"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf.conf"
            printf '%s\n' "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf" > "$SITE_LINK"
            NGINX_CALLS=0

            nginx() {
              NGINX_CALLS=$((NGINX_CALLS + 1))
              printf 'nginx %s\n' "$*" >> "$TEST_LOG"
              [ "$NGINX_CALLS" -gt 1 ]
            }
            systemctl() { printf 'systemctl %s\n' "$*" >> "$TEST_LOG"; return 0; }
            ln() { printf '%s\n' "${@: -2:1}" > "${@: -1}"; }
            readlink() { cat "$1"; }

            if activate_site_checked rupochta-rf.conf; then
              printf 'unexpected activation\n' >&2
              exit 92
            fi
            printf 'TARGET=%s\n' "$(readlink "$SITE_LINK")"
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        self.assertIn("TARGET=", result.stdout)
        self.assertIn("rupochta-rf-bootstrap.conf", result.stdout)

    def test_nginx_activation_stops_if_site_link_cannot_be_changed(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            SITE_AVAILABLE_DIR="$TEST_ROOT/sites-available"
            SITE_LINK="$TEST_ROOT/sites-enabled/rupochta-rf.conf"
            TEST_LOG="$TEST_ROOT/commands.log"
            mkdir -p "$SITE_AVAILABLE_DIR" "$(dirname "$SITE_LINK")"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf.conf"
            printf '%s\n' "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf" > "$SITE_LINK"

            ln() { printf 'ln %s\n' "$*" >> "$TEST_LOG"; return 1; }
            readlink() { cat "$1"; }
            nginx() { printf 'nginx %s\n' "$*" >> "$TEST_LOG"; return 0; }
            systemctl() { printf 'systemctl %s\n' "$*" >> "$TEST_LOG"; return 0; }

            if activate_site_checked rupochta-rf.conf; then
              printf 'unexpected activation\n' >&2
              exit 97
            fi
            printf 'TARGET=%s\n' "$(readlink "$SITE_LINK")"
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        self.assertIn("rupochta-rf-bootstrap.conf", result.stdout)
        self.assertIn("ln -sfn", result.stdout)
        self.assertNotIn("nginx -t", result.stdout)
        self.assertNotIn("systemctl reload", result.stdout)

    def test_nginx_reload_failure_restores_previous_site(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            SITE_AVAILABLE_DIR="$TEST_ROOT/sites-available"
            SITE_LINK="$TEST_ROOT/sites-enabled/rupochta-rf.conf"
            TEST_LOG="$TEST_ROOT/commands.log"
            mkdir -p "$SITE_AVAILABLE_DIR" "$(dirname "$SITE_LINK")"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf.conf"
            printf '%s\n' "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf" > "$SITE_LINK"
            RELOAD_CALLS=0

            nginx() { printf 'nginx %s\n' "$*" >> "$TEST_LOG"; return 0; }
            systemctl() {
              printf 'systemctl %s\n' "$*" >> "$TEST_LOG"
              if [ "$1" = reload ]; then
                RELOAD_CALLS=$((RELOAD_CALLS + 1))
                [ "$RELOAD_CALLS" -gt 1 ]
              fi
            }
            ln() { printf '%s\n' "${@: -2:1}" > "${@: -1}"; }
            readlink() { cat "$1"; }

            if activate_site_checked rupochta-rf.conf; then
              printf 'unexpected activation\n' >&2
              exit 93
            fi
            printf 'TARGET=%s\n' "$(readlink "$SITE_LINK")"
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        self.assertIn("TARGET=", result.stdout)
        self.assertIn("rupochta-rf-bootstrap.conf", result.stdout)

    def test_certbot_deploy_hook_tests_config_before_reload(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            CERTBOT_DEPLOY_HOOK="$TEST_ROOT/hooks/deploy/rupochta-nginx"
            TEST_LOG="$TEST_ROOT/commands.log"

            install_certbot_deploy_hook
            nginx() { printf 'nginx %s\n' "$*" >> "$TEST_LOG"; return 0; }
            systemctl() { printf 'systemctl %s\n' "$*" >> "$TEST_LOG"; return 0; }
            export TEST_LOG
            export -f nginx systemctl

            test -x "$CERTBOT_DEPLOY_HOOK"
            "$CERTBOT_DEPLOY_HOOK"
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        self.assertEqual(
            result.stdout.splitlines(),
            ["nginx -t", "systemctl reload nginx"],
        )

    def test_certbot_deploy_hook_does_not_reload_invalid_config(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            CERTBOT_DEPLOY_HOOK="$TEST_ROOT/hooks/deploy/rupochta-nginx"
            TEST_LOG="$TEST_ROOT/commands.log"

            install_certbot_deploy_hook
            nginx() { printf 'nginx %s\n' "$*" >> "$TEST_LOG"; return 1; }
            systemctl() { printf 'systemctl %s\n' "$*" >> "$TEST_LOG"; return 0; }
            export TEST_LOG
            export -f nginx systemctl

            if "$CERTBOT_DEPLOY_HOOK"; then
              printf 'unexpected hook success\n' >&2
              exit 94
            fi
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines(), ["nginx -t"])

    def test_invalid_existing_certificate_falls_back_then_reaches_certbot(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            DOMAIN_PUNYCODE=xn--80a1acdmd4a.xn--p1ai
            CERT_DIR="$TEST_ROOT/live/$DOMAIN_PUNYCODE"
            SITE_AVAILABLE_DIR="$TEST_ROOT/sites-available"
            SITE_LINK="$TEST_ROOT/sites-enabled/rupochta-rf.conf"
            ACME_WEBROOT="$TEST_ROOT/acme"
            ACME_EMAIL=postmaster@example.test
            OBTAIN_CERT=1
            TEST_LOG="$TEST_ROOT/commands.log"
            REPAIRED=0
            mkdir -p "$CERT_DIR" "$SITE_AVAILABLE_DIR" "$(dirname "$SITE_LINK")" "$ACME_WEBROOT"
            printf 'certificate\n' > "$CERT_DIR/fullchain.pem"
            printf 'private key\n' > "$CERT_DIR/privkey.pem"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf.conf"
            printf '%s\n' "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf" > "$SITE_LINK"

            ln() { printf '%s\n' "${@: -2:1}" > "${@: -1}"; }
            readlink() { cat "$1"; }
            dig() { printf '203.0.113.10\n'; }
            nginx() {
              local target
              target="$(readlink "$SITE_LINK")"
              printf 'NGINX repaired=%s target=%s %s\n' "$REPAIRED" "$target" "$*" >> "$TEST_LOG"
              if [[ "$target" = */rupochta-rf.conf ]] && [ "$REPAIRED" -eq 0 ]; then
                return 1
              fi
            }
            systemctl() { printf 'SYSTEMCTL %s\n' "$*" >> "$TEST_LOG"; return 0; }
            certbot() {
              printf 'CERTBOT %s\n' "$*" >> "$TEST_LOG"
              REPAIRED=1
              return 0
            }

            if ! prepare_nginx_site; then
              printf 'failed to prepare a repairable nginx site\n' >&2
              exit 95
            fi
            printf 'AFTER_PREP=%s\n' "$(readlink "$SITE_LINK")"
            request_tls_certificate
            printf 'FINAL=%s\n' "$(readlink "$SITE_LINK")"
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        prep = [line for line in result.stdout.splitlines() if line.startswith("AFTER_PREP=")]
        final = [line for line in result.stdout.splitlines() if line.startswith("FINAL=")]
        self.assertEqual(len(prep), 1)
        self.assertTrue(prep[0].endswith("/rupochta-rf-bootstrap.conf"), prep[0])
        self.assertEqual(len(final), 1)
        self.assertTrue(final[0].endswith("/rupochta-rf.conf"), final[0])
        self.assertIn("CERTBOT ", result.stdout)
        self.assertIn("--force-renewal", result.stdout)
        self.assertNotIn("--keep-until-expiring", result.stdout)
        self.assertIn("NGINX repaired=0 target=", result.stdout)

    def test_invalid_existing_certificate_without_tls_fails_closed(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            DOMAIN_PUNYCODE=xn--80a1acdmd4a.xn--p1ai
            CERT_DIR="$TEST_ROOT/live/$DOMAIN_PUNYCODE"
            SITE_AVAILABLE_DIR="$TEST_ROOT/sites-available"
            SITE_LINK="$TEST_ROOT/sites-enabled/rupochta-rf.conf"
            OBTAIN_CERT=0
            TEST_LOG="$TEST_ROOT/commands.log"
            mkdir -p "$CERT_DIR" "$SITE_AVAILABLE_DIR" "$(dirname "$SITE_LINK")"
            printf 'certificate\n' > "$CERT_DIR/fullchain.pem"
            printf 'private key\n' > "$CERT_DIR/privkey.pem"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf.conf"
            printf '%s\n' "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf" > "$SITE_LINK"

            ln() { printf '%s\n' "${@: -2:1}" > "${@: -1}"; }
            readlink() { cat "$1"; }
            nginx() { printf 'NGINX %s\n' "$*" >> "$TEST_LOG"; return 1; }

            if prepare_nginx_site; then
              printf 'unexpected fallback without explicit TLS repair\n' >&2
              exit 96
            fi
            printf 'TARGET=%s\n' "$(readlink "$SITE_LINK")"
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        target = [line for line in result.stdout.splitlines() if line.startswith("TARGET=")]
        self.assertEqual(len(target), 1)
        self.assertTrue(target[0].endswith("/rupochta-rf-bootstrap.conf"), target[0])
        self.assertEqual(result.stdout.count("NGINX -t"), 1)

    def test_incomplete_existing_lineage_forces_explicit_tls_repair(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            DOMAIN_PUNYCODE=xn--80a1acdmd4a.xn--p1ai
            CERT_DIR="$TEST_ROOT/live/$DOMAIN_PUNYCODE"
            SITE_AVAILABLE_DIR="$TEST_ROOT/sites-available"
            SITE_LINK="$TEST_ROOT/sites-enabled/rupochta-rf.conf"
            ACME_WEBROOT="$TEST_ROOT/acme"
            ACME_EMAIL=postmaster@example.test
            OBTAIN_CERT=1
            TEST_LOG="$TEST_ROOT/commands.log"
            mkdir -p "$CERT_DIR" "$SITE_AVAILABLE_DIR" "$(dirname "$SITE_LINK")" "$ACME_WEBROOT"
            : > "$CERT_DIR/fullchain.pem"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf.conf"
            printf '%s\n' "$SITE_AVAILABLE_DIR/rupochta-rf.conf" > "$SITE_LINK"

            ln() { printf '%s\n' "${@: -2:1}" > "${@: -1}"; }
            readlink() { cat "$1"; }
            dig() { return 0; }
            nginx() { printf 'NGINX %s\n' "$*" >> "$TEST_LOG"; return 0; }
            systemctl() { printf 'SYSTEMCTL %s\n' "$*" >> "$TEST_LOG"; return 0; }
            certbot() {
              printf 'CERTBOT %s\n' "$*" >> "$TEST_LOG"
              printf 'certificate\n' > "$CERT_DIR/fullchain.pem"
              printf 'private key\n' > "$CERT_DIR/privkey.pem"
            }

            prepare_nginx_site
            request_tls_certificate
            cat "$TEST_LOG"
            """
        )
        self.assert_ok(result)
        certbot = [line for line in result.stdout.splitlines() if line.startswith("CERTBOT ")]
        self.assertEqual(len(certbot), 1)
        self.assertIn("--force-renewal", certbot[0])
        self.assertNotIn("--keep-until-expiring", certbot[0])

    def test_incomplete_existing_lineage_without_tls_fails_closed(self) -> None:
        result = self.run_helper(
            r"""
            source deploy/bootstrap-rupochta-rf-lib.sh
            DOMAIN_PUNYCODE=xn--80a1acdmd4a.xn--p1ai
            CERT_DIR="$TEST_ROOT/live/$DOMAIN_PUNYCODE"
            SITE_AVAILABLE_DIR="$TEST_ROOT/sites-available"
            SITE_LINK="$TEST_ROOT/sites-enabled/rupochta-rf.conf"
            OBTAIN_CERT=0
            mkdir -p "$CERT_DIR" "$SITE_AVAILABLE_DIR" "$(dirname "$SITE_LINK")"
            : > "$CERT_DIR/fullchain.pem"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf-bootstrap.conf"
            : > "$SITE_AVAILABLE_DIR/rupochta-rf.conf"
            printf '%s\n' "$SITE_AVAILABLE_DIR/rupochta-rf.conf" > "$SITE_LINK"

            nginx() { printf 'unexpected nginx validation\n' >&2; return 0; }
            ln() { printf '%s\n' "${@: -2:1}" > "${@: -1}"; }
            readlink() { cat "$1"; }

            if prepare_nginx_site; then
              printf 'unexpected HTTP downgrade\n' >&2
              exit 98
            fi
            printf 'TARGET=%s\n' "$(readlink "$SITE_LINK")"
            """
        )
        self.assert_ok(result)
        target = [line for line in result.stdout.splitlines() if line.startswith("TARGET=")]
        self.assertEqual(len(target), 1)
        self.assertTrue(target[0].endswith("/rupochta-rf.conf"), target[0])
        self.assertNotIn("unexpected nginx validation", result.stderr)


if __name__ == "__main__":
    unittest.main()
