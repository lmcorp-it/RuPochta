#!/usr/bin/env python3
"""Internal mail-control agent for the primary mail VM.

This service runs next to docker-mailserver and exposes a small authenticated
JSON API for mailbox inventory and password operations. The SOC proxy-panel can
call it directly instead of shelling into the VM over SSH.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import secrets
import subprocess
import threading
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rupochta-mail-control-agent")

MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "example.com").strip()
MAILSERVER_CONTAINER = os.environ.get("MAILSERVER_CONTAINER", "mailserver").strip()
INTERNAL_TOKEN = (
    os.environ.get("MAIL_CONTROL_AGENT_TOKEN", "").strip()
    or os.environ.get("RUPOCHTA_INTERNAL_TOKEN", "").strip()
)
ALLOWED_INTERNAL_NETWORKS: tuple[str, ...] = tuple(
    net.strip()
    for net in os.environ.get("MAIL_CONTROL_AGENT_ALLOWED_NETWORKS", "127.0.0.0/8,::1/128,10.0.0.0/8").split(",")
    if net.strip()
)
_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]*@[a-z0-9.\-]+\.[a-z]{2,}$")
_VALID_LOCAL_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]*$")
_mailserver_lock = threading.Lock()
_MAILSERVER_ACCOUNT_LOCK_PATH = "/var/lock/rupochta-mailcontrol-accounts.lock"

app = FastAPI(title="RuPochta Mail Control Agent", version="1.0.0")


class MailLoginBody(BaseModel):
    mail_login: str


class MailPasswordBody(BaseModel):
    mail_login: str
    password: str


def _tcp_client_ip(request: Request) -> str:
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "")


def _normalize_login(mail_login: str) -> str:
    candidate = str(mail_login or "").strip().lower()
    return candidate if _VALID_LOCAL_RE.match(candidate) else ""


def _email_for_login(mail_login: str) -> str:
    login = _normalize_login(mail_login)
    return f"{login}@{MAIL_DOMAIN}" if login else ""


def _ip_in_networks(ip_str: str, networks: tuple[str, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        for net in networks:
            try:
                if addr in ipaddress.ip_network(net, strict=False):
                    return True
            except ValueError:
                pass
    except ValueError:
        return False
    return False


def _require_internal_auth(request: Request, x_internal_token: str = Header(default="")) -> None:
    client_ip = _tcp_client_ip(request)
    if ALLOWED_INTERNAL_NETWORKS and not _ip_in_networks(client_ip, ALLOWED_INTERNAL_NETWORKS):
        raise HTTPException(status_code=403, detail="Forbidden (source IP)")
    if not INTERNAL_TOKEN:
        raise HTTPException(status_code=503, detail="MAIL_CONTROL_AGENT_TOKEN not configured")
    token_bytes = str(x_internal_token or "").strip().encode()
    expected_bytes = INTERNAL_TOKEN.encode()
    if not secrets.compare_digest(token_bytes, expected_bytes):
        raise HTTPException(status_code=403, detail="Forbidden (invalid token)")


def _docker_exec(args: list[str], timeout: int = 10, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["docker", "exec", "-i", MAILSERVER_CONTAINER] + list(args)
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _container_health() -> tuple[bool, str]:
    if not MAILSERVER_CONTAINER:
        return False, "mailserver container not configured"
    try:
        result = _docker_exec(["bash", "-lc", "printf '%s\\n' CONTROL_OK"], timeout=5)
    except Exception as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "docker exec failed"
    out = (result.stdout or "").strip()
    return ("CONTROL_OK" in out), (out or "local_mail_control_ok")


def _doveadm_hash_password(password: str) -> tuple[bool, str]:
    try:
        result = _docker_exec(["doveadm", "pw", "-s", "SHA512-CRYPT", "-p", password], timeout=8)
    except Exception as exc:
        return False, f"doveadm exec: {exc}"
    if result.returncode != 0:
        return False, f"doveadm pw failed: {(result.stderr or result.stdout).strip()}"
    pw_hash = (result.stdout or "").strip()
    if not pw_hash.startswith("{SHA512-CRYPT}"):
        return False, f"unexpected hash output: {pw_hash[:80]}"
    return True, pw_hash


def _mailserver_read_account_hash(email: str) -> tuple[bool, str]:
    script = (
        'set -eu; '
        'ACC=/tmp/docker-mailserver/postfix-accounts.cf; '
        '[ -f "$ACC" ] || { echo NOT_FOUND; exit 3; }; '
        'LINE=$(grep -i "^${EMAIL}|" "$ACC" | head -1); '
        '[ -n "$LINE" ] && echo "${LINE#*|}" || echo NOT_FOUND'
    )
    with _mailserver_lock:
        try:
            result = subprocess.run(
                ["docker", "exec", "-i", "-e", f"EMAIL={email}", MAILSERVER_CONTAINER, "bash", "-lc", script],
                capture_output=True, text=True, timeout=6,
            )
        except Exception as exc:
            return False, f"docker exec: {exc}"
    out = (result.stdout or "").strip()
    if result.returncode != 0 or out == "NOT_FOUND":
        return False, out or "account not found"
    return True, out


def _mailserver_write_account_hash(email: str, pw_hash: str) -> tuple[bool, str]:
    script = (
        'set -eu; '
        'ACC=/tmp/docker-mailserver/postfix-accounts.cf; '
        'TMP="$(mktemp)"; '
        'awk -F"|" -v email="$EMAIL" -v pwhash="$PWHASH" '
        '\'{ if ($1 == email) { print email "|" pwhash; found=1 } else { print $0 } } '
        'END { if (!found) exit 3 }\' "$ACC" > "$TMP" '
        '&& mv "$TMP" "$ACC"; echo UPDATED'
    )
    with _mailserver_lock:
        try:
            result = subprocess.run(
                [
                    "docker", "exec", "-i",
                    "-e", f"EMAIL={email}",
                    "-e", f"PWHASH={pw_hash}",
                    MAILSERVER_CONTAINER, "bash", "-lc", script,
                ],
                capture_output=True, text=True, timeout=6,
            )
        except Exception as exc:
            return False, f"docker exec: {exc}"
    out = (result.stdout or "").strip()
    if result.returncode != 0:
        return False, (result.stderr or out).strip() or "write failed"
    return True, out or "UPDATED"


def _mailserver_delete_account(mail_login: str) -> tuple[bool, str, str]:
    login = _normalize_login(mail_login)
    if not login or len(login) < 2:
        return False, "", "Invalid mail_login"
    email_addr = f"{login}@{MAIL_DOMAIN}"
    try:
        result = _docker_exec(["setup", "email", "del", "-y", email_addr], timeout=15)
    except Exception as exc:
        return False, email_addr, f"docker exec: {exc}"
    if result.returncode != 0:
        return False, email_addr, (result.stderr or result.stdout).strip() or "delete failed"
    return True, email_addr, "removed"


def _mailserver_get_distribution(alias_login: str) -> tuple[bool, str, list[str]]:
    login = _normalize_login(alias_login)
    if not login:
        return False, "", []
    alias_email = f"{login}@{MAIL_DOMAIN}"
    script = 'grep -i "^${ALIAS} " /tmp/docker-mailserver/postfix-virtual.cf 2>/dev/null || echo NOT_FOUND'
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", "-e", f"ALIAS={alias_email}", MAILSERVER_CONTAINER, "bash", "-lc", script],
            capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return False, alias_email, []
    out = (result.stdout or "").strip()
    if not out or out == "NOT_FOUND":
        return True, alias_email, []
    parts = out.split(None, 1)
    if len(parts) < 2:
        return True, alias_email, []
    recipients = [r.strip() for r in parts[1].split(",") if r.strip()]
    return True, alias_email, recipients


def _mailserver_set_distribution(alias_login: str, recipients: list[str]) -> tuple[bool, str, list[str]]:
    login = _normalize_login(alias_login)
    if not login:
        return False, "", []
    alias_email = f"{login}@{MAIL_DOMAIN}"
    recipients_str = ",".join(str(r) for r in recipients)
    script = r"""
ALIAS="$1"
RECIPIENTS="$2"
FILE=/tmp/docker-mailserver/postfix-virtual.cf
TMP=$(mktemp "${FILE}.tmp.XXXXXX")
(
  flock -x 9
  grep -v "^${ALIAS} " "$FILE" 2>/dev/null > "$TMP" || true
  if [ -n "${RECIPIENTS}" ]; then
    printf '%s %s\n' "${ALIAS}" "${RECIPIENTS}" >> "$TMP"
  fi
  mv "$TMP" "$FILE"
) 9>"${FILE}.lock"
printf '%s\n' APPLIED
""".strip()
    try:
        result = subprocess.run(
            ["docker", "exec", "-i", "-e", f"ALIAS={alias_email}", "-e", f"RECIPIENTS={recipients_str}",
             MAILSERVER_CONTAINER, "bash", "-s"],
            input=script,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return False, alias_email, []
    if result.returncode != 0:
        return False, alias_email, []
    return True, alias_email, list(recipients)


def _mailserver_delete_distribution(alias_login: str) -> tuple[bool, str, str]:
    ok, alias_email, _ = _mailserver_set_distribution(alias_login, [])
    if ok:
        return True, alias_email, "deleted"
    else:
        return False, f"{alias_login}@{MAIL_DOMAIN}", "delete failed"


def _mailserver_apply_account_changes() -> tuple[bool, str]:
    script = r"""
set -u
source /usr/local/bin/helpers/index.sh
source /etc/dms-settings
_obtain_hostname_and_domainname
_rspamd_get_envs
_create_postfix_vhost || true
_create_accounts
postmap /etc/postfix/vmailbox >/dev/null 2>&1 || true
_reload_postfix
if [[ ${SMTP_ONLY:-0} -ne 1 ]]; then
  dovecot reload
fi
printf '%s\n' APPLIED
""".strip()
    with _mailserver_lock:
        try:
            result = _docker_exec(["bash", "-s"], timeout=25, stdin=script)
        except Exception as exc:
            return False, f"apply exec: {exc}"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "apply failed"
    return True, (result.stdout or "").strip() or "applied"


def _mailserver_list_accounts() -> tuple[bool, list[str] | str]:
    try:
        result = _docker_exec(["setup", "email", "list"], timeout=20)
    except Exception as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "setup email list failed"
    emails: list[str] = []
    for line in (result.stdout or "").splitlines():
        match = re.search(r"([a-z0-9][a-z0-9._\-]*@" + re.escape(MAIL_DOMAIN) + r")", line.lower())
        if match:
            emails.append(match.group(1))
    return True, sorted(set(emails))


def _mailserver_account_exists(mail_login: str) -> tuple[bool, str]:
    email_addr = _email_for_login(mail_login)
    if not _EMAIL_RE.match(email_addr):
        return False, "invalid email"
    script = (
        'set -eu; '
        'ACC=/tmp/docker-mailserver/postfix-accounts.cf; '
        '[ -f "$ACC" ] || { echo MISSING_FILE; exit 4; }; '
        'grep -qiE "^${EMAIL}\\|" "$ACC" && echo EXISTS || echo NOT_FOUND'
    )
    with _mailserver_lock:
        try:
            result = subprocess.run(
                ["docker", "exec", "-i", "-e", f"EMAIL={email_addr}", MAILSERVER_CONTAINER, "bash", "-lc", script],
                capture_output=True,
                text=True,
                timeout=6,
            )
        except Exception as exc:
            return False, f"docker exec: {exc}"
    if result.returncode != 0:
        out = (result.stderr or result.stdout).strip()
        return False, out or "mailbox lookup failed"
    status = (result.stdout or "").strip()
    if status == "EXISTS":
        return True, "exists"
    if status == "NOT_FOUND":
        return True, "missing"
    return False, status or "unknown"


def _is_hard_mail_auth_failure(text: str) -> bool:
    lowered = str(text or "").lower()
    hard_markers = (
        "authentication failed",
        "auth failed",
        "password mismatch",
        "invalid credentials",
        "login failed",
        "unknown user",
    )
    soft_markers = (
        "socket error",
        "eof",
        "timed out",
        "timeout",
        "connection reset",
        "connection closed",
        "temporarily unavailable",
        "try again",
        "server unavailable",
        "abort",
    )
    if any(marker in lowered for marker in soft_markers):
        return False
    return any(marker in lowered for marker in hard_markers)


def _mailserver_verify_password(mail_login: str, password: str) -> tuple[bool, str, bool]:
    email_addr = _email_for_login(mail_login)
    if not _EMAIL_RE.match(email_addr):
        return False, "invalid email", False
    if not password:
        return False, "missing password", False
    try:
        result = _docker_exec(["doveadm", "auth", "test", email_addr, password], timeout=12)
    except Exception as exc:
        return False, str(exc), False
    output = (result.stdout or result.stderr or "").strip()
    lowered = output.lower()
    if "auth succeeded" in lowered:
        return True, output or "auth succeeded", False
    if result.returncode == 0 or "auth failed" in lowered or "password mismatch" in lowered:
        return False, output or "auth failed", _is_hard_mail_auth_failure(output)
    if "no such container" in lowered or "is not running" in lowered or "docker" in lowered:
        return False, output or "docker exec failed", False
    return False, output or "auth failed", _is_hard_mail_auth_failure(output)


def _mailserver_create_account(mail_login: str, password: str) -> tuple[bool, str, str]:
    login = _normalize_login(mail_login)
    if not login or len(login) < 2:
        return False, "", "Invalid mail_login"
    email_addr = f"{login}@{MAIL_DOMAIN}"
    ok_hash, pw_hash = _doveadm_hash_password(password)
    if not ok_hash:
        return False, email_addr, pw_hash
    add_script = (
        'set -eu; '
        'ACC=/tmp/docker-mailserver/postfix-accounts.cf; '
        'touch "$ACC"; '
        'if grep -qE "^${EMAIL}\\|" "$ACC" 2>/dev/null; then echo EXISTS; '
        'else printf "%s|%s\\n" "$EMAIL" "$PWHASH" >> "$ACC"; echo ADDED; fi'
    )
    with _mailserver_lock:
        try:
            result = subprocess.run(
                [
                    "docker", "exec", "-i",
                    "-e", f"EMAIL={email_addr}",
                    "-e", f"PWHASH={pw_hash}",
                    MAILSERVER_CONTAINER, "bash", "-lc", add_script,
                ],
                capture_output=True,
                text=True,
                timeout=6,
            )
        except Exception as exc:
            return False, email_addr, f"docker exec: {exc}"
    if result.returncode != 0:
        return False, email_addr, (result.stderr or result.stdout).strip() or "append failed"
    out = (result.stdout or "").strip()
    if out == "EXISTS":
        return True, email_addr, "already exists"
    if out == "ADDED":
        ok_apply, apply_status = _mailserver_apply_account_changes()
        if not ok_apply:
            log.warning("mail-control apply after add failed for %s: %s", email_addr, apply_status)
            rollback_script = (
                'set -eu; '
                'ACC=/tmp/docker-mailserver/postfix-accounts.cf; '
                'TMP="$(mktemp)"; '
                'grep -v "^${EMAIL}\\|" "$ACC" > "$TMP" || true; '
                'mv "$TMP" "$ACC"; echo REMOVED'
            )
            with _mailserver_lock:
                result = subprocess.run(
                    ["docker", "exec", "-i", "-e", f"EMAIL={email_addr}", MAILSERVER_CONTAINER, "bash", "-lc", rollback_script],
                    capture_output=True, text=True, timeout=6,
                )
                if result.returncode != 0:
                    error_detail = (result.stderr or result.stdout).strip() or "rollback subprocess failed"
                    log.error("Rollback failed for %s: %s", email_addr, error_detail)
                    return False, email_addr, f"apply failed, rollback also failed: {error_detail}"
            return False, email_addr, apply_status
        return True, email_addr, "added"
    return False, email_addr, out or "unexpected create result"


def _mailserver_reset_password(mail_login: str, password: str) -> tuple[bool, str, str]:
    login = _normalize_login(mail_login)
    if not login or len(login) < 2:
        return False, "", "Invalid mail_login"
    email_addr = f"{login}@{MAIL_DOMAIN}"
    ok_hash, new_hash = _doveadm_hash_password(password)
    if not ok_hash:
        return False, email_addr, new_hash
    ok_read, old_hash = _mailserver_read_account_hash(email_addr)
    if not ok_read:
        old_hash = ""
    ok_write, write_status = _mailserver_write_account_hash(email_addr, new_hash)
    if not ok_write:
        return False, email_addr, write_status
    ok_apply, apply_status = _mailserver_apply_account_changes()
    if not ok_apply:
        log.warning("mail-control apply after reset failed for %s: %s", email_addr, apply_status)
        if old_hash:
            ok_rollback, rollback_status = _mailserver_write_account_hash(email_addr, old_hash)
            if not ok_rollback:
                return False, email_addr, f"runtime state uncertain: apply={apply_status}, rollback={rollback_status}"
        return False, email_addr, apply_status
    return True, email_addr, "updated"


@app.get("/health")
@app.post("/health")
async def health(request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    ok, status = _container_health()
    return {
        "ok": bool(ok),
        "status": "local_mail_control_ok" if ok else status,
        "container": MAILSERVER_CONTAINER,
        "domain": MAIL_DOMAIN,
    }


@app.post("/list_accounts")
async def list_accounts(request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    ok, data = _mailserver_list_accounts()
    if not ok:
        raise HTTPException(status_code=502, detail=str(data))
    return {
        "ok": True,
        "accounts": data,
        "count": len(data),
        "source": "mail_control_agent",
    }


@app.post("/account_exists")
async def account_exists(body: MailLoginBody, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    ok, status = _mailserver_account_exists(body.mail_login)
    if not ok:
        raise HTTPException(status_code=502, detail=status)
    return {
        "ok": True,
        "mailbox_exists": status == "exists",
        "status": status,
        "source": "mail_control_agent",
    }


@app.post("/verify_password")
async def verify_password(body: MailPasswordBody, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    verified, status, hard_failure = _mailserver_verify_password(body.mail_login, body.password)
    return {
        "ok": True,
        "verified": bool(verified),
        "status": status,
        "hard_failure": bool(hard_failure),
        "source": "mail_control_agent",
    }


@app.post("/status")
async def status(body: MailLoginBody, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    health_ok, health_status = _container_health()
    exists_ok, mailbox_status = _mailserver_account_exists(body.mail_login)
    if not exists_ok:
        raise HTTPException(status_code=502, detail=mailbox_status)
    return {
        "ok": bool(health_ok),
        "status": "mailbox_exists" if mailbox_status == "exists" else "mailbox_missing",
        "mailbox_exists": mailbox_status == "exists",
        "mailbox_status": mailbox_status,
        "mail_control_status": "local_mail_control_ok" if health_ok else health_status,
        "source": "mail_control_agent",
    }


@app.post("/doctor")
async def doctor(body: MailPasswordBody, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    health_ok, health_status = _container_health()
    exists_ok, mailbox_status = _mailserver_account_exists(body.mail_login)
    if not exists_ok:
        raise HTTPException(status_code=502, detail=mailbox_status)
    verified, verify_status, hard_failure = _mailserver_verify_password(body.mail_login, body.password)
    return {
        "ok": bool(health_ok),
        "status": "mailbox_exists" if mailbox_status == "exists" else "mailbox_missing",
        "mailbox_exists": mailbox_status == "exists",
        "mailbox_status": mailbox_status,
        "verified": bool(verified),
        "verify_status": verify_status,
        "hard_failure": bool(hard_failure),
        "mail_control_status": "local_mail_control_ok" if health_ok else health_status,
        "source": "mail_control_agent",
    }


@app.post("/create_account")
async def create_account(body: MailPasswordBody, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    ok, email_addr, status = _mailserver_create_account(body.mail_login, body.password)
    if not ok:
        raise HTTPException(status_code=502, detail=status)
    return {
        "ok": True,
        "mail_login": _normalize_login(body.mail_login),
        "email": email_addr,
        "created": status == "added",
        "status": status,
        "source": "mail_control_agent",
    }


@app.post("/reset_password")
async def reset_password(body: MailPasswordBody, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    ok, email_addr, status = _mailserver_reset_password(body.mail_login, body.password)
    if not ok:
        raise HTTPException(status_code=502, detail=status)
    return {
        "ok": True,
        "mail_login": _normalize_login(body.mail_login),
        "email": email_addr,
        "status": status,
        "source": "mail_control_agent",
    }


class DistributionSetBody(BaseModel):
    alias_login: str
    recipients: list[str]


class DistributionGetBody(BaseModel):
    alias_login: str


@app.post("/delete_account")
async def delete_account(body: MailLoginBody, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    ok, email_addr, status = _mailserver_delete_account(body.mail_login)
    if not ok:
        raise HTTPException(status_code=502, detail=status)
    return {
        "ok": True,
        "email": email_addr,
        "status": status,
        "source": "mail_control_agent",
    }


@app.post("/distribution/set")
async def distribution_set(body: DistributionSetBody, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    ok, alias_email, recipients = _mailserver_set_distribution(body.alias_login, body.recipients)
    if not ok:
        raise HTTPException(status_code=502, detail="distribution set failed")
    return {"ok": True, "alias": alias_email, "recipients": recipients, "source": "mail_control_agent"}


@app.post("/distribution/delete")
async def distribution_delete(body: DistributionGetBody, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    ok, alias_email, _ = _mailserver_set_distribution(body.alias_login, [])
    if not ok:
        raise HTTPException(status_code=502, detail="distribution delete failed")
    return {"ok": True, "alias": alias_email, "status": "deleted", "source": "mail_control_agent"}


@app.get("/distribution/get")
async def distribution_get(alias_login: str, request: Request, x_internal_token: str = Header(default="")) -> dict[str, Any]:
    _require_internal_auth(request, x_internal_token)
    ok, alias_email, recipients = _mailserver_get_distribution(alias_login)
    if not ok:
        raise HTTPException(status_code=502, detail="distribution get failed")
    return {"ok": True, "alias": alias_email, "recipients": recipients, "source": "mail_control_agent"}
