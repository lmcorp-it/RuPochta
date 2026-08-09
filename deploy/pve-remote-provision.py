#!/usr/bin/env python3
"""Drive the рупочта.рф deployment through the Proxmox API.

Renames the guest, then runs the bootstrap inside it through the QEMU guest
agent, then checks that the service answers. Everything happens over the
Proxmox HTTPS API, so this works from anywhere that can reach port 8006 — no
SSH access to either the hypervisor or the guest is required.

Standard library only, so it runs on the hypervisor, on a laptop, or from CI.

    export PVE_PASSWORD='...'
    ./deploy/pve-remote-provision.py \
        --host pve.example.com --user claude@lm.local \
        --vm mail --new-name rupochta-rf

`--user` also accepts an API token id, which is what to use in CI:

    export PVE_PASSWORD='<token secret>'
    ./deploy/pve-remote-provision.py --user 'claude@lm.local!deploy' ...

A token can be scoped to this job and revoked on its own, so no account
password has to live in a secret store.

Nothing is changed without --apply: the default run reports what it found and
what it would do. Mailbox deletion is separate again and needs
--purge-mailboxes on top of --apply, because it destroys mail.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

for _stream in (sys.stdout, sys.stderr):
    # Guest output round-trips through JSON as UTF-8 and can contain
    # non-ASCII (the domain is Cyrillic). Windows consoles default to a
    # legacy codepage that can't encode it, which crashes print() mid-run
    # after side effects have already happened. Force UTF-8 with a lossy
    # fallback instead of failing on an incidental encoding mismatch.
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_REPO = "https://github.com/lmcorp-it/RuPochta.git"
CHECKOUT_DIR = "/opt/rupochta-src"
GUEST_EXEC_POLL_SECONDS = 3
GUEST_EXEC_MAX_WAIT = 1800  # bootstrap installs packages and a venv


class ProxmoxError(RuntimeError):
    pass


class Proxmox:
    """The slice of the Proxmox API this deployment needs."""

    def __init__(self, host: str, verify_tls: bool = True) -> None:
        if "://" in host:
            host = urllib.parse.urlsplit(host).netloc or host
        self.base = f"https://{host if ':' in host else host + ':8006'}/api2/json"
        self.ticket: Optional[str] = None
        self.csrf: Optional[str] = None
        self.token_header: Optional[str] = None
        if verify_tls:
            self.ssl_context = ssl.create_default_context()
        else:
            # Proxmox ships a self-signed certificate by default; opting out is
            # explicit and loud rather than silent.
            self.ssl_context = ssl._create_unverified_context()

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
    ) -> Any:
        url = f"{self.base}{path}"
        body = urllib.parse.urlencode(data, doseq=True).encode() if data else None
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Accept", "application/json")
        if self.token_header:
            # API tokens authenticate per request and need no CSRF token.
            request.add_header("Authorization", self.token_header)
        elif self.ticket:
            request.add_header("Cookie", f"PVEAuthCookie={self.ticket}")
            if self.csrf and method != "GET":
                request.add_header("CSRFPreventionToken", self.csrf)
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.ssl_context) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise ProxmoxError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ProxmoxError(
                f"{method} {path} -> cannot reach the API: {exc.reason}. "
                "Check that port 8006 is reachable from here."
            ) from exc
        return payload.get("data")

    def login(self, username: str, password: str) -> None:
        """Authenticate with an API token when one is given, otherwise a ticket.

        A token id carries a '!' — user@realm!tokenid — and the secret is the
        token's UUID. Prefer it: the token can be scoped to just this job and
        revoked on its own, instead of putting an account password in CI.
        """
        if "!" in username:
            self.token_header = f"PVEAPIToken={username}={password}"
            try:
                self._request("GET", "/version")
            except ProxmoxError as exc:
                self.token_header = None
                raise ProxmoxError(
                    f"API token rejected: {exc}. Check the token id (user@realm!tokenid), "
                    "its secret, and that the token has the permissions this job needs "
                    "(VM.Config, VM.Monitor, VM.GuestAgent.* on the guest)."
                ) from exc
            return

        data = self._request(
            "POST", "/access/ticket", {"username": username, "password": password}
        )
        if not data or not data.get("ticket"):
            raise ProxmoxError("authentication failed: no ticket returned")
        self.ticket = data["ticket"]
        self.csrf = data.get("CSRFPreventionToken")

    def find_guest(self, needle: str) -> Dict[str, Any]:
        """Locate a guest by vmid, exact name, or unique name fragment."""
        resources = self._request("GET", "/cluster/resources?type=vm") or []
        if needle.isdigit():
            matches = [r for r in resources if str(r.get("vmid")) == needle]
        else:
            lowered = needle.lower()
            exact = [r for r in resources if str(r.get("name", "")).lower() == lowered]
            matches = exact or [
                r for r in resources if lowered in str(r.get("name", "")).lower()
            ]
        if not matches:
            known = ", ".join(f"{r.get('vmid')}:{r.get('name')}" for r in resources) or "none"
            raise ProxmoxError(f"no guest matches {needle!r}. Known guests: {known}")
        if len(matches) > 1:
            found = ", ".join(f"{r.get('vmid')}:{r.get('name')}" for r in matches)
            raise ProxmoxError(f"{needle!r} is ambiguous, matches: {found}")
        return matches[0]

    def rename(self, node: str, vmid: int, kind: str, new_name: str) -> None:
        field = "hostname" if kind == "lxc" else "name"
        self._request("PUT", f"/nodes/{node}/{kind}/{vmid}/config", {field: new_name})

    def agent_ping(self, node: str, vmid: int) -> bool:
        try:
            self._request("POST", f"/nodes/{node}/qemu/{vmid}/agent/ping", {})
            return True
        except ProxmoxError:
            return False

    def agent_exec(
        self,
        node: str,
        vmid: int,
        command: List[str],
        max_wait: int = GUEST_EXEC_MAX_WAIT,
    ) -> Tuple[int, str, str]:
        """Run a command in the guest and wait for it, returning (rc, out, err)."""
        started = self._request(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/agent/exec",
            {"command": command},
        )
        pid = (started or {}).get("pid")
        if pid is None:
            raise ProxmoxError("guest agent did not return a pid")

        deadline = time.time() + max_wait
        while time.time() < deadline:
            status = self._request(
                "GET", f"/nodes/{node}/qemu/{vmid}/agent/exec-status?pid={pid}"
            ) or {}
            if status.get("exited"):
                return (
                    int(status.get("exitcode", -1)),
                    str(status.get("out-data") or ""),
                    str(status.get("err-data") or ""),
                )
            time.sleep(GUEST_EXEC_POLL_SECONDS)
        raise ProxmoxError(f"command did not finish within {max_wait}s: {' '.join(command)}")


def shell(api: Proxmox, node: str, vmid: int, script: str, label: str, apply: bool) -> bool:
    """Run a shell snippet in the guest, printing what it did."""
    print(f"\n-- {label}")
    if not apply:
        print("   (dry run) would run:")
        for line in script.strip().splitlines():
            print(f"     {line}")
        return True
    rc, out, err = api.agent_exec(node, vmid, ["/bin/bash", "-lc", script])
    for stream in (out, err):
        for line in stream.strip().splitlines()[-40:]:
            print(f"   {line}")
    if rc != 0:
        print(f"   FAILED (exit {rc})")
        return False
    print("   ok")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="Proxmox host, e.g. pve.example.com or pve.example.com:8006")
    parser.add_argument(
        "--user",
        required=True,
        help=(
            "Proxmox login (root@pam, claude@lm.local) with the account password in "
            "PVE_PASSWORD, or an API token id (claude@lm.local!deploy) with the token "
            "secret in PVE_PASSWORD — the token is preferred"
        ),
    )
    parser.add_argument("--vm", required=True, help="Guest vmid, exact name, or unique name fragment")
    parser.add_argument("--new-name", default="rupochta-rf", help="New guest name (default: rupochta-rf)")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Repository to deploy from")
    parser.add_argument("--branch", default="main", help="Branch to deploy (default: main)")
    parser.add_argument("--apply", action="store_true", help="Actually make changes (default: report only)")
    parser.add_argument("--skip-rename", action="store_true", help="Leave the guest name alone")
    parser.add_argument(
        "--purge-mailboxes",
        action="store_true",
        help="Also delete the lets-mobile mailboxes. Destroys mail; needs --apply",
    )
    parser.add_argument("--insecure", action="store_true", help="Skip TLS verification (self-signed Proxmox cert)")
    args = parser.parse_args()

    password = os.environ.get("PVE_PASSWORD", "")
    if not password:
        print("PVE_PASSWORD is not set — export it rather than passing it on the command line", file=sys.stderr)
        return 2

    api = Proxmox(args.host, verify_tls=not args.insecure)
    try:
        api.login(args.user, password)
        print(f"authenticated to {args.host} as {args.user}")

        guest = api.find_guest(args.vm)
        node = str(guest["node"])
        vmid = int(guest["vmid"])
        kind = "lxc" if str(guest.get("type")) == "lxc" else "qemu"
        print(f"guest: {guest.get('name')} (vmid {vmid}, node {node}, {kind}, status {guest.get('status')})")

        if guest.get("status") != "running":
            print("guest is not running — start it first", file=sys.stderr)
            return 1

        if kind == "lxc":
            print(
                "\nThis is an LXC container. Proxmox has no exec API for containers, so the\n"
                "rename below is all that can be done remotely; run deploy/bootstrap-rupochta-rf.sh\n"
                "inside the container (pct enter %d) for the rest." % vmid,
                file=sys.stderr,
            )

        if not args.skip_rename:
            print(f"\n-- renaming guest to {args.new_name}")
            if args.apply:
                api.rename(node, vmid, kind, args.new_name)
                print("   ok")
            else:
                print("   (dry run) would rename")

        if kind == "lxc":
            return 0

        if not api.agent_ping(node, vmid):
            print(
                "\nQEMU guest agent is not answering, so nothing can be run inside the guest.\n"
                "Install it there (apt install qemu-guest-agent) and enable the agent on the VM\n"
                "(qm set %d --agent enabled=1), or run deploy/bootstrap-rupochta-rf.sh over SSH."
                % vmid,
                file=sys.stderr,
            )
            return 1
        print("guest agent: responding")

        checkout = (
            f"set -euo pipefail\n"
            f"if [ -d {CHECKOUT_DIR}/.git ]; then\n"
            f"  git -C {CHECKOUT_DIR} fetch --depth 1 origin {args.branch}\n"
            f"  git -C {CHECKOUT_DIR} checkout -B {args.branch} origin/{args.branch}\n"
            f"else\n"
            f"  git clone --depth 1 --branch {args.branch} {args.repo} {CHECKOUT_DIR}\n"
            f"fi\n"
            f"git -C {CHECKOUT_DIR} log --oneline -1"
        )
        if not shell(api, node, vmid, checkout, f"fetching {args.repo} ({args.branch})", args.apply):
            return 1

        rename_flag = "" if args.skip_rename else "--rename"
        bootstrap = f"set -euo pipefail\ncd {CHECKOUT_DIR}\nbash deploy/bootstrap-rupochta-rf.sh {rename_flag}"
        if not shell(api, node, vmid, bootstrap, "running bootstrap-rupochta-rf.sh", args.apply):
            return 1

        if args.purge_mailboxes:
            purge = (
                f"set -euo pipefail\ncd {CHECKOUT_DIR}\n"
                f"bash deploy/purge-lets-mobile-mailboxes.sh --apply"
            )
            if not shell(api, node, vmid, purge, "removing lets-mobile mailboxes", args.apply):
                return 1
        else:
            preview = f"set -euo pipefail\ncd {CHECKOUT_DIR}\nbash deploy/purge-lets-mobile-mailboxes.sh"
            shell(api, node, vmid, preview, "lets-mobile mailboxes (listing only)", args.apply)

        verify = (
            "set -euo pipefail\n"
            "curl -fsS http://127.0.0.1:18400/health && echo\n"
            "curl -fsS http://127.0.0.1:18400/api/signup/config && echo\n"
            "curl -fsS http://127.0.0.1:18400/ready && echo || echo 'ready: mail path not answering yet'"
        )
        shell(api, node, vmid, verify, "verifying the service inside the guest", args.apply)

        if not args.apply:
            print("\nDry run only — nothing was changed. Re-run with --apply.")
        else:
            print(
                "\nDone inside the guest. Still to do from outside, per "
                "docs/deploy-rupochta-rf.md: DNS for рупочта.рф, the TLS certificate, "
                "and the deliverability check."
            )
        return 0

    except ProxmoxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
