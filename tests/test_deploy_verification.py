from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PVE_SCRIPT = ROOT / "deploy" / "pve-remote-provision.py"
PUBLIC_VERIFY_SCRIPT = ROOT / "deploy" / "verify-rupochta-rf.sh"
WINDOWS_GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(WINDOWS_GIT_BASH) if WINDOWS_GIT_BASH.exists() else (shutil.which("bash") or "bash")


def _load_pve_module():
    spec = importlib.util.spec_from_file_location("pve_remote_provision", PVE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {PVE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PVE = _load_pve_module()


def _bash_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    if len(value) >= 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class FakeCommandEnvironment:
    def __init__(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.curl_log = self.root / "curl.log"
        self._install_commands()

    def close(self) -> None:
        self.tempdir.cleanup()

    def _install_commands(self) -> None:
        _write_executable(
            self.bin / "systemctl",
            r'''#!/usr/bin/env bash
if [ "${1:-}" = "is-active" ]; then
  case "${2:-}" in
    nginx) state="${FAKE_NGINX_STATE:-active}" ;;
    rupochta.service) state="${FAKE_RUPOCHTA_STATE:-active}" ;;
    cloudflared) state="${FAKE_CLOUDFLARED_STATE:-inactive}" ;;
    *) state="inactive" ;;
  esac
  printf '%s\n' "$state"
  [ "$state" = "active" ]
  exit
fi
exit 0
''',
        )
        _write_executable(
            self.bin / "curl",
            r'''#!/usr/bin/env bash
output=""
write_format=""
fail_http=0
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) output="$2"; shift 2 ;;
    -w) write_format="$2"; shift 2 ;;
    --max-time) shift 2 ;;
    -*f*) fail_http=1; shift ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done

[ -n "${FAKE_CURL_LOG:-}" ] && printf '%s\n' "$url" >> "$FAKE_CURL_LOG"

case "$url" in
  http://*/health)
    code="${FAKE_PUBLIC_HTTP_HEALTH_CODE:-${FAKE_HEALTH_CODE:-200}}"
    body='{"ok":true}'
    ;;
  */health)
    code="${FAKE_HEALTH_CODE:-200}"
    body='{"ok":true}'
    ;;
  */ready)
    code="${FAKE_READY_CODE:-200}"
    body='{"ok":true}'
    ;;
  */api/signup/config)
    code="${FAKE_SIGNUP_CONFIG_CODE:-200}"
    body='{"enabled":true,"provisioning_ready":true,"domain":"xn--80a1acdmd4a.xn--p1ai"}'
    ;;
  */signup)
    code="${FAKE_SIGNUP_PAGE_CODE:-200}"
    body='<!doctype html><title>signup</title>'
    ;;
  http://127.0.0.1/)
    code="${FAKE_NGINX_HTTP_CODE:-200}"
    body='nginx'
    ;;
  *)
    code="${FAKE_DEFAULT_CODE:-200}"
    body='ok'
    ;;
esac

if [ "$code" = "000" ]; then
  [ -n "$write_format" ] && printf '000'
  exit 7
fi
if [ "$fail_http" -eq 1 ] && [ "$code" -ge 400 ] 2>/dev/null; then
  [ -n "$write_format" ] && printf '%s' "$code"
  exit 22
fi
if [ -n "$output" ] && [ "$output" != "/dev/null" ]; then
  printf '%s' "$body" > "$output"
fi
if [ -n "$write_format" ]; then
  printf '%s' "$code"
elif [ -z "$output" ]; then
  printf '%s' "$body"
fi
exit 0
''',
        )
        _write_executable(
            self.bin / "git",
            "#!/usr/bin/env bash\nprintf '%s\\n' 'abc123 deployed revision'\n",
        )
        _write_executable(
            self.bin / "ip",
            "#!/usr/bin/env bash\nprintf '%s\\n' '1.1.1.1 via 10.10.0.1 dev eth0 src 10.10.0.150'\n",
        )
        _write_executable(
            self.bin / "ss",
            "#!/usr/bin/env bash\nprintf '%s\\n' 'State Local Address:Port' 'LISTEN 0.0.0.0:80'\n",
        )
        _write_executable(
            self.bin / "timeout",
            "#!/usr/bin/env bash\nshift\nexec \"$@\"\n",
        )
        _write_executable(
            self.bin / "dig",
            r'''#!/usr/bin/env bash
case "$*" in
  *"+short A "*) echo 203.0.113.10 ;;
  *"+short MX "*) echo '10 mail.xn--80a1acdmd4a.xn--p1ai.' ;;
  *"_dmarc."*) echo '"v=DMARC1; p=quarantine"' ;;
  *"mail._domainkey."*) echo '"v=DKIM1; p=test"' ;;
  *"+short TXT "*) echo '"v=spf1 mx -all"' ;;
  *"+short -x "*) echo mail.xn--80a1acdmd4a.xn--p1ai. ;;
esac
''',
        )
        _write_executable(
            self.bin / "openssl",
            r'''#!/usr/bin/env bash
if [ "${1:-}" = "s_client" ]; then
  echo certificate
else
  cat >/dev/null
  echo 'subject=CN = xn--80a1acdmd4a.xn--p1ai'
  echo 'notAfter=Jan  1 00:00:00 2030 GMT'
fi
''',
        )

    def run(self, script: str, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(overrides)
        env["FAKE_CURL_LOG"] = _bash_path(self.curl_log)
        command = f'export PATH="{_bash_path(self.bin)}:$PATH"\n{script}'
        return subprocess.run(
            [BASH, "-c", command],
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    def curl_urls(self) -> list[str]:
        if not self.curl_log.exists():
            return []
        return self.curl_log.read_text(encoding="utf-8").splitlines()


class GuestVerificationScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.commands = FakeCommandEnvironment()

    def tearDown(self) -> None:
        self.commands.close()

    def run_verify(self, **environment: str) -> subprocess.CompletedProcess[str]:
        script = PVE.build_guest_verify_script()
        return self.commands.run(script, **environment)

    def assert_verification_fails(self, **environment: str) -> None:
        result = self.run_verify(**environment)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)

    def test_inactive_nginx_fails(self) -> None:
        self.assert_verification_fails(FAKE_NGINX_STATE="inactive")

    def test_inactive_rupochta_fails(self) -> None:
        self.assert_verification_fails(FAKE_RUPOCHTA_STATE="failed")

    def test_health_status_must_be_200(self) -> None:
        self.assert_verification_fails(FAKE_HEALTH_CODE="503")

    def test_ready_status_must_be_200(self) -> None:
        self.assert_verification_fails(FAKE_READY_CODE="503")

    def test_signup_config_status_must_be_200(self) -> None:
        self.assert_verification_fails(FAKE_SIGNUP_CONFIG_CODE="404")

    def test_signup_page_status_must_be_200(self) -> None:
        self.assert_verification_fails(FAKE_SIGNUP_PAGE_CODE="502")

    def test_all_required_guest_checks_pass(self) -> None:
        result = self.run_verify()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('{"ok":true}', result.stdout + result.stderr)
        self.assertNotIn("<!doctype html>", result.stdout + result.stderr)
        self.assertNotIn("10.10.0.1", result.stdout + result.stderr)
        self.assertNotIn("10.10.0.150", result.stdout + result.stderr)
        self.assertNotIn("0.0.0.0", result.stdout + result.stderr)
        self.assertIn("listener :80 -> present", result.stdout)
        self.assertIn("listener :443 -> absent", result.stdout)


class ProvisionOutcomeTests(unittest.TestCase):
    class FakeProxmox:
        verification_result = (0, "verified", "")
        verification_error = None

        def __init__(self, _host: str, verify_tls: bool = True) -> None:
            self.exec_calls = 0
            self.verify_tls = verify_tls

        def login(self, _username: str, _password: str) -> None:
            return None

        def find_guest(self, _needle: str):
            return {"node": "pve", "vmid": 106, "type": "qemu", "name": "rupochta-rf", "status": "running"}

        def rename(self, _node: str, _vmid: int, _kind: str, _new_name: str) -> None:
            return None

        def agent_ping(self, _node: str, _vmid: int) -> bool:
            return True

        def agent_exec(self, _node: str, _vmid: int, _command):
            self.exec_calls += 1
            if self.exec_calls == 4:
                if self.verification_error is not None:
                    raise self.verification_error
                return self.verification_result
            return (0, "ok", "")

    def run_main(self, fake: "ProvisionOutcomeTests.FakeProxmox", *, apply: bool = True):
        argv = [
            str(PVE_SCRIPT),
            "--host",
            "pve.example.test",
            "--user",
            "deploy@example.test!ci",
            "--vm",
            "rupochta-rf",
        ]
        if apply:
            argv.append("--apply")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(PVE, "Proxmox", return_value=fake),
            mock.patch.object(sys, "argv", argv),
            mock.patch.dict(os.environ, {"PVE_PASSWORD": "test-only"}, clear=False),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = PVE.main()
        return result, stdout.getvalue(), stderr.getvalue()

    def test_false_guest_verification_fails_deployment(self) -> None:
        fake = self.FakeProxmox("unused")
        fake.verification_result = (1, "required check failed", "")
        result, stdout, stderr = self.run_main(fake)
        self.assertEqual(1, result, stdout + stderr)
        self.assertNotIn("Done inside the guest", stdout)
        self.assertNotIn("test-only", stdout + stderr)

    def test_guest_verification_exception_fails_deployment(self) -> None:
        fake = self.FakeProxmox("unused")
        fake.verification_error = PVE.ProxmoxError("guest verification unavailable")
        result, stdout, stderr = self.run_main(fake)
        self.assertEqual(1, result, stdout + stderr)
        self.assertNotIn("Done inside the guest", stdout)
        self.assertIn("guest verification unavailable", stderr)

    def test_dry_run_remains_non_mutating(self) -> None:
        fake = self.FakeProxmox("unused")
        result, stdout, stderr = self.run_main(fake, apply=False)
        self.assertEqual(0, result, stdout + stderr)
        self.assertEqual(0, fake.exec_calls)
        self.assertIn("Dry run only", stdout)


class PublicVerificationTests(unittest.TestCase):
    def test_public_https_check_does_not_claim_to_probe_origin_http(self) -> None:
        commands = FakeCommandEnvironment()
        try:
            result = commands.run(
                f'bash "{_bash_path(PUBLIC_VERIFY_SCRIPT)}"',
                FAKE_HEALTH_CODE="502",
                FAKE_PUBLIC_HTTP_HEALTH_CODE="200",
            )
            self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse(
                any(url.startswith("http://") for url in commands.curl_urls()),
                f"public verification made an HTTP request and could mislabel it as origin:\n{result.stdout}",
            )
            self.assertNotIn("the origin answers", result.stdout + result.stderr)
        finally:
            commands.close()


if __name__ == "__main__":
    unittest.main()
