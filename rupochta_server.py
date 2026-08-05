"""
RuPochta Webmail Server
====================
FastAPI-based corporate webmail for example.com.

Features:
- IMAP/SMTP via stdlib (imaplib/smtplib) wrapped with asyncio executors
- Server-side sessions with cookie token (wmSID), 8h TTL
- AD LDAP contact search (svc-mail@corp.local)
- Custom mail aliases stored in SQLite (webmail_aliases.db)
- Static SPA served from ./static/

Runs on 127.0.0.1:18400 — proxied by nginx as mail.example.com
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.policy
import fcntl
import hashlib
import hmac
import html
import imaplib
import ipaddress
import json
import logging
import os
import re
import smtplib
import sqlite3
import ssl
import socket
import threading
import time
import secrets
import shlex
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, formataddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import urllib.parse
import urllib.request
import urllib.error

from fastapi import FastAPI, HTTPException, Request, Response, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from ldap3 import Server, Connection, ALL, SUBTREE, NTLM, SIMPLE
    from ldap3.core.exceptions import LDAPException
    LDAP_AVAILABLE = True
except Exception:  # pragma: no cover
    LDAP_AVAILABLE = False

# Import utility modules
from utils import normalization, errors, database, validation, config as config_utils, authentication, mail_protocol


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _mail_sso_default_endpoints(issuer: str) -> Dict[str, str]:
    issuer = str(issuer or "").rstrip("/")
    if issuer.endswith("/webman/sso"):
        return {
            "authorization": f"{issuer}/SSOOauth.cgi",
            "token": f"{issuer}/SSOAccessToken.cgi",
            "userinfo": f"{issuer}/SSOUserInfo.cgi",
        }
    oidc_base = f"{issuer}/protocol/openid-connect"
    return {
        "authorization": f"{oidc_base}/auth",
        "token": f"{oidc_base}/token",
        "userinfo": f"{oidc_base}/userinfo",
    }


DEFAULT_ADMIN_LDAP_GROUPS = [
    item.strip()
    for item in os.environ.get("MAILADMIN_LDAP_ADMIN_GROUPS", "").split(";")
    if item.strip()
]
PORTAL_PROFILE_AVATAR_MAX_CHARS = int(
    os.environ.get("PORTAL_PROFILE_AVATAR_MAX_CHARS", str(512 * 1024))
)
PORTAL_PROFILE_REQUEST_TIMEOUT = float(
    os.environ.get("PORTAL_PROFILE_REQUEST_TIMEOUT", "2")
)

# Sample multi-tenant directory profiles. Override with MAIL_DIRECTORY_PROFILES,
# a JSON object of {"Company": "ad.domain"}; aliases are derived from the keys.
DIRECTORY_PROFILES = json.loads(
    os.environ.get(
        "MAIL_DIRECTORY_PROFILES",
        '{"Corp": "corp.local", "Alpha": "alpha.local",'
        ' "Beta": "beta.local", "Gamma": "gamma.local"}',
    )
)
DIRECTORY_COMPANY_ALIASES = {
    alias: company
    for company in DIRECTORY_PROFILES
    for alias in (company.lower(), company.lower().replace("-", "").replace(" ", ""))
}


def _canonical_company(value: Any) -> str:
    clean = normalization.normalize_company_name(value)
    return DIRECTORY_COMPANY_ALIASES.get(clean) or DIRECTORY_COMPANY_ALIASES.get(
        clean.replace(" ", ""),
        "",
    )


def _directory_domain_for_company(value: Any) -> str:
    return DIRECTORY_PROFILES.get(_canonical_company(value), "")


def _company_for_directory_domain(value: Any) -> str:
    domain = normalization.normalize_domain(value)
    return next(
        (company for company, candidate in DIRECTORY_PROFILES.items() if candidate == domain),
        "",
    )



def _directory_domain_from_dn(value: Any) -> str:
    parts = re.findall(r"(?:^|,)\s*DC=([^,]+)", str(value or ""), flags=re.IGNORECASE)
    domain = ".".join(part.strip().lower() for part in parts if part.strip())
    return domain if domain in set(DIRECTORY_PROFILES.values()) else ""


class Config:
    HOST = "127.0.0.1"
    PORT = 18400

    # Mail server
    MAIL_HOST = os.environ.get("MAIL_HOST", "127.0.0.1")
    IMAP_HOST = os.environ.get("MAIL_IMAP_HOST", os.environ.get("IMAP_HOST", MAIL_HOST))
    IMAP_PORT = int(os.environ.get("MAIL_IMAP_PORT", os.environ.get("IMAP_PORT", "993")))
    SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", os.environ.get("SMTP_HOST", MAIL_HOST))
    SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", os.environ.get("SMTP_PORT", "587")))
    SMTP_VERIFY_TLS = os.environ.get("MAIL_SMTP_VERIFY_TLS", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }
    MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "example.com")
    MAIL_PUBLIC_HOST = os.environ.get("MAIL_PUBLIC_HOST", f"mail.{MAIL_DOMAIN}")
    MAIL_PUBLIC_IMAP_PORT = int(os.environ.get("MAIL_PUBLIC_IMAP_PORT", "993"))
    MAIL_PUBLIC_SMTP_PORT = int(os.environ.get("MAIL_PUBLIC_SMTP_PORT", "587"))
    MAIL_WEBMAIL_URL = os.environ.get("MAIL_WEBMAIL_URL", f"https://mail.{MAIL_DOMAIN}")
    TICKET_INTAKE_URL = os.environ.get(
        "MAIL_TICKET_INTAKE_URL",
        "http://127.0.0.1:3010/api/it/intake/mail",
    ).strip()
    TICKET_INTAKE_SECRET = os.environ.get("INTEGRATION_WEBHOOK_SECRET", "").strip()
    MANAGED_ADDRESS_WRITE_AUTHORITY = os.environ.get(
        "MANAGED_ADDRESS_WRITE_AUTHORITY",
        "0",
    ).strip().lower() in {"1", "true", "yes", "on"}
    MAIL_SSO_PUBLIC_URL = os.environ.get("MAIL_SSO_PUBLIC_URL", MAIL_WEBMAIL_URL).rstrip("/")
    MAIL_SSO_ISSUER = os.environ.get("MAIL_SSO_ISSUER", "").rstrip("/")
    MAIL_SSO_CLIENT_ID = os.environ.get("MAIL_SSO_CLIENT_ID", os.environ.get("IT_SSO_CLIENT_ID", "")).strip()
    MAIL_SSO_CLIENT_SECRET = os.environ.get("MAIL_SSO_CLIENT_SECRET", os.environ.get("IT_SSO_CLIENT_SECRET", "")).strip()
    MAIL_SSO_SCOPE = os.environ.get("MAIL_SSO_SCOPE", "openid email groups").strip()
    _MAIL_SSO_DEFAULT_ENDPOINTS = _mail_sso_default_endpoints(MAIL_SSO_ISSUER)
    MAIL_SSO_AUTHORIZATION_ENDPOINT = os.environ.get(
        "MAIL_SSO_AUTHORIZATION_ENDPOINT", _MAIL_SSO_DEFAULT_ENDPOINTS["authorization"]
    ).strip()
    MAIL_SSO_TOKEN_ENDPOINT = os.environ.get(
        "MAIL_SSO_TOKEN_ENDPOINT", _MAIL_SSO_DEFAULT_ENDPOINTS["token"]
    ).strip()
    MAIL_SSO_USERINFO_ENDPOINT = os.environ.get(
        "MAIL_SSO_USERINFO_ENDPOINT", _MAIL_SSO_DEFAULT_ENDPOINTS["userinfo"]
    ).strip()
    MAIL_SSO_STATE_COOKIE = "wmSSOState"
    MAIL_SSO_PKCE_ENABLED = os.environ.get("MAIL_SSO_PKCE_ENABLED", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }
    MAIL_SSO_PKCE_COOKIE = "__Host-wmSSOPkce"
    MAIL_SSO_TELEGRAM_IDP_ALIAS = os.environ.get(
        "MAIL_SSO_TELEGRAM_IDP_ALIAS", "telegram"
    ).strip()
    MAIL_SSO_TELEGRAM_ACCOUNT_STATE_COOKIE = "__Host-wmSSOTelegramAccountState"
    MAIL_SSO_TELEGRAM_ACCOUNT_PKCE_COOKIE = "__Host-wmSSOTelegramAccountPkce"
    MAIL_SSO_TELEGRAM_ACCOUNT_TTL = 300
    MAILADMIN_SSO_PUBLIC_URL = os.environ.get(
        "MAILADMIN_SSO_PUBLIC_URL", "http://127.0.0.1:18400"
    ).rstrip("/")
    MAILADMIN_SSO_PUBLIC_URLS = [
        item.strip().rstrip("/")
        for item in os.environ.get(
            "MAILADMIN_SSO_PUBLIC_URLS",
            MAILADMIN_SSO_PUBLIC_URL,
        ).split(",")
        if item.strip()
    ]
    MAILADMIN_SSO_CLIENT_ID = os.environ.get(
        "MAILADMIN_SSO_CLIENT_ID", MAIL_SSO_CLIENT_ID
    ).strip()
    MAILADMIN_SSO_CLIENT_SECRET = os.environ.get(
        "MAILADMIN_SSO_CLIENT_SECRET", MAIL_SSO_CLIENT_SECRET
    ).strip()
    MAILADMIN_SSO_SCOPE = os.environ.get("MAILADMIN_SSO_SCOPE", MAIL_SSO_SCOPE).strip()
    MAILADMIN_SSO_AUTHORIZATION_ENDPOINT = os.environ.get(
        "MAILADMIN_SSO_AUTHORIZATION_ENDPOINT", MAIL_SSO_AUTHORIZATION_ENDPOINT
    ).strip()
    MAILADMIN_SSO_TOKEN_ENDPOINT = os.environ.get(
        "MAILADMIN_SSO_TOKEN_ENDPOINT", MAIL_SSO_TOKEN_ENDPOINT
    ).strip()
    MAILADMIN_SSO_USERINFO_ENDPOINT = os.environ.get(
        "MAILADMIN_SSO_USERINFO_ENDPOINT", MAIL_SSO_USERINFO_ENDPOINT
    ).strip()
    MAILADMIN_SSO_ALLOWED_GROUP = os.environ.get(
        "MAILADMIN_SSO_ALLOWED_GROUP", ""
    ).strip()
    MAILADMIN_SSO_STATE_COOKIE = "jmAdminSSOState"

    # AD LDAP
    LDAP_SERVERS = [
        item.strip()
        for item in os.environ.get(
            "MAILADMIN_LDAPS_URLS",
            "",
        ).split(",")
        if item.strip()
    ]
    LDAP_BASE_DN = os.environ.get(
        "MAILADMIN_LDAPS_BASE_DN",
        "DC=corp,DC=local",
    ).strip()
    LDAP_BIND_USER = os.environ.get(
        "MAILADMIN_LDAPS_BIND_USER",
        "",
    ).strip()
    LDAP_BIND_PASS = os.environ.get("LDAP_BIND_PASS", "")

    # Proxy panel internal APIs.
    PROXY_PANEL_URL = os.environ.get("PROXY_PANEL_URL", "")
    INTERNAL_TOKEN = (
        os.environ.get("PROXY_PANEL_INTERNAL_TOKEN", "").strip()
        or os.environ.get("RUPOCHTA_INTERNAL_TOKEN", "").strip()
    )
    PORTAL_EMPLOYEE_DIRECTORY_URL = os.environ.get(
        "PORTAL_EMPLOYEE_DIRECTORY_URL",
        "",
    ).strip() or "http://127.0.0.1:3010/api/it/employees"
    PORTAL_DIRECTORY_BRIDGE_URL = os.environ.get(
        "PORTAL_DIRECTORY_BRIDGE_URL",
        "",
    ).strip()
    PORTAL_PERSONAL_PROFILE_URL = os.environ.get(
        "PORTAL_PERSONAL_PROFILE_URL",
        "",
    ).strip()
    MAIL_ACCOUNT_CONSOLE_URL = os.environ.get(
        "MAIL_ACCOUNT_CONSOLE_URL",
        "",
    ).strip()
    PORTAL_INTEGRATION_SECRET = os.environ.get(
        "PORTAL_INTEGRATION_SECRET",
        "",
    ).strip()
    PORTAL_MAIL_AUTH_URL = os.environ.get(
        "PORTAL_MAIL_AUTH_URL",
        "",
    ).strip()
    PORTAL_MAIL_AUTH_HOST = os.environ.get(
        "PORTAL_MAIL_AUTH_HOST",
        "",
    ).strip()
    MAIL_TELEGRAM_BROWSER_COOKIE = "wmTelegramBrowser"
    MAIL_TELEGRAM_BROWSER_TTL = int(
        os.environ.get("MAIL_TELEGRAM_BROWSER_TTL", str(15 * 60))
    )

    # Sessions
    SESSION_TTL = 8 * 3600
    SESSION_COOKIE = "wmSID"
    FOLDERS_CACHE_TTL = int(os.environ.get("WEBMAIL_FOLDERS_CACHE_TTL", "12"))
    MESSAGES_CACHE_TTL = int(os.environ.get("WEBMAIL_MESSAGES_CACHE_TTL", "6"))
    MESSAGE_DETAIL_CACHE_TTL = int(os.environ.get("WEBMAIL_MESSAGE_DETAIL_CACHE_TTL", "20"))

    # SQLite
    DB_PATH = os.environ.get(
        "WEBMAIL_DB",
        "/opt/Project-Tegridy/Project-RuPochta/webmail_aliases.db",
    )

    STATIC_DIR = str(Path(__file__).parent / "static")

    # Admin API key (for bot integration)
    ADMIN_KEY = os.environ.get("MAIL_ADMIN_KEY", "")

    # docker-mailserver setup script path on this host
    MAILSERVER_CONTAINER = os.environ.get("MAILSERVER_CONTAINER", "mailserver").strip()
    MAILSERVER_ACCOUNTS_FILE = os.environ.get(
        "MAILSERVER_ACCOUNTS_FILE",
        "/srv/mail/mail-server/config/postfix-accounts.cf",
    ).strip()
    MAILSERVER_MODE = os.environ.get("WEBMAIL_LOCAL_MAILSERVER", "auto").strip().lower()
    RUNTIME_MODE = os.environ.get("WEBMAIL_RUNTIME_MODE", "").strip().lower()


CFG = Config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("rupochta")


def _local_mailserver_enabled() -> bool:
    mode = str(getattr(CFG, "MAILSERVER_MODE", "auto") or "auto").strip().lower()
    if mode in {"0", "false", "off", "disabled", "no"}:
        return False
    if mode in {"1", "true", "on", "yes"}:
        return bool(getattr(CFG, "MAILSERVER_CONTAINER", ""))
    host = str(getattr(CFG, "IMAP_HOST", "") or "").strip().lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _mail_admin_available() -> bool:
    return _local_mailserver_enabled()


def _mail_runtime_mode() -> str:
    if _mail_admin_available():
        return "primary"
    if str(getattr(CFG, "RUNTIME_MODE", "") or "").strip().lower() == "relay":
        return "relay"
    return "backup"


def _ldap_bind_available() -> bool:
    return bool(LDAP_AVAILABLE and str(CFG.LDAP_BIND_PASS or '').strip())


def _raise_mail_admin_unavailable() -> None:
    raise HTTPException(
        status_code=503,
        detail="Mail admin operations are temporarily unavailable on this backup webmail node.",
    )


def _require_managed_address_write_authority() -> None:
    if not CFG.MANAGED_ADDRESS_WRITE_AUTHORITY:
        _raise_mail_admin_unavailable()


# ---------------------------------------------------------------------------
# SQLite alias storage
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


def _harden_sqlite_permissions(db_path: str) -> None:
    for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            continue


@contextmanager
def _db_connection():
    with _db_lock:
        previous_umask = os.umask(0o177)
        try:
            con = sqlite3.connect(CFG.DB_PATH)
        finally:
            os.umask(previous_umask)
        _harden_sqlite_permissions(CFG.DB_PATH)
        try:
            with con:
                yield con
        finally:
            con.close()
            _harden_sqlite_permissions(CFG.DB_PATH)


def _db_add_column_if_missing(
    con: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        str(row[1])
        for row in con.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _sso_external_bindings_init(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sso_external_mailbox_bindings (
            subject_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            imap_pass_enc TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            imap_host TEXT NOT NULL,
            imap_port INTEGER NOT NULL,
            provider TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_sso_external_mailbox_bindings_email "
        "ON sso_external_mailbox_bindings(email)"
    )
    # Submission endpoint of the external mailbox. Rows written before this
    # column existed keep NULL and fall back to the provider preset on read.
    _db_add_column_if_missing(con, "sso_external_mailbox_bindings", "smtp_host", "TEXT")
    _db_add_column_if_missing(con, "sso_external_mailbox_bindings", "smtp_port", "INTEGER")
    legacy_external = con.execute(
        "SELECT COUNT(*) FROM sso_mailbox_bindings "
        "WHERE imap_host IS NOT NULL "
        "OR imap_port IS NOT NULL "
        "OR provider IS NOT NULL"
    ).fetchone()[0]
    if legacy_external:
        raise RuntimeError(
            "legacy external SSO bindings require manual recovery before ADR-0072"
        )


def db_init() -> None:
    db_dir = Path(CFG.DB_PATH).parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Fallback to local file for dev
        CFG.DB_PATH = str(Path(__file__).parent / "webmail_aliases.db")
        log.warning("Falling back to local DB: %s", CFG.DB_PATH)
    with _db_connection() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_login TEXT NOT NULL UNIQUE,
                mail_login TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_aliases_mail ON mail_aliases(mail_login)")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                ad_login TEXT PRIMARY KEY,
                signature TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_login TEXT NOT NULL,
                name TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_mail_templates_owner "
            "ON mail_templates(ad_login, updated_at DESC)"
        )
        # Audit log for admin portal mutations
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                admin_user TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                details TEXT,
                ip TEXT,
                ok INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_admin ON audit_log(admin_user)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mailbox_lifecycle (
                email TEXT PRIMARY KEY,
                suspended INTEGER NOT NULL DEFAULT 0,
                suspended_hash TEXT,
                suspended_at TEXT,
                suspended_by TEXT,
                suspend_reason TEXT,
                restored_at TEXT,
                restored_by TEXT,
                restore_reason TEXT
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_mailbox_lifecycle_state "
            "ON mailbox_lifecycle(suspended, email)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mailbox_lifecycle_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                email TEXT NOT NULL,
                emp_id INTEGER NOT NULL,
                admin_user TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                password_enc TEXT NOT NULL,
                previous_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_mailbox_lifecycle_operations_status "
            "ON mailbox_lifecycle_operations(status, id)"
        )
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                uq_mailbox_lifecycle_operation_active
            ON mailbox_lifecycle_operations(email)
            WHERE status IN ('pending', 'reconciling')
            """
        )
        # Central-auth bindings: which SSO subject may open which local mailbox.
        # The portal registers a row when the user configures mail in the personal
        # cabinet; the mailbox password stays encrypted with this service's key and
        # never travels back out. Subjects are stored hashed — a leaked database
        # must not enumerate the identities of the central broker.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sso_mailbox_bindings (
                subject_hash TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                imap_pass_enc TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                imap_host TEXT,
                imap_port INTEGER,
                provider TEXT
            )
            """
        )
        # Optional columns added by ADR-0071 (BYOM external mailbox). Existing
        # rows keep NULL and fall back to the global SpaceWeb IMAP config, so a
        # pre-migration database keeps working without a data backfill.
        _db_add_column_if_missing(con, "sso_mailbox_bindings", "imap_host", "TEXT")
        _db_add_column_if_missing(con, "sso_mailbox_bindings", "imap_port", "INTEGER")
        _db_add_column_if_missing(con, "sso_mailbox_bindings", "provider", "TEXT")
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_sso_mailbox_bindings_email "
            "ON sso_mailbox_bindings(email)"
        )
        _sso_external_bindings_init(con)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_intake_mailboxes (
                email TEXT PRIMARY KEY,
                imap_pass_enc TEXT NOT NULL,
                last_uid INTEGER NOT NULL DEFAULT 0,
                initialized INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            INSERT OR IGNORE INTO ticket_intake_mailboxes (
                email, imap_pass_enc, last_uid, initialized, created_at, updated_at
            )
            SELECT email, imap_pass_enc, 0, 0, MIN(created_at), MAX(updated_at)
              FROM sso_mailbox_bindings
             GROUP BY email
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS employee_identity_cache (
                emp_id INTEGER PRIMARY KEY,
                derived_ad_login TEXT,
                derived_mail_login TEXT,
                ad_source TEXT,
                mail_source TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_employee_identity_cache_updated "
            "ON employee_identity_cache(updated_at DESC)"
        )
        # AI triage cache: one row per (user, message_id) with the predicted category.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_triage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imap_user TEXT NOT NULL,
                message_id TEXT NOT NULL,
                folder TEXT NOT NULL DEFAULT 'INBOX',
                category TEXT NOT NULL,
                confidence INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(imap_user, message_id)
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_triage_user_cat "
            "ON ai_triage(imap_user, category)"
        )
        # Scheduled / send-later queue
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imap_user TEXT NOT NULL,
                from_addr TEXT NOT NULL,
                to_list TEXT NOT NULL,
                cc_list TEXT NOT NULL DEFAULT '',
                bcc_list TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                body_html TEXT NOT NULL DEFAULT '',
                body_text TEXT NOT NULL DEFAULT '',
                in_reply_to TEXT,
                attachments_json TEXT,
                imap_pass_enc TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                sent_at TEXT
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_due "
            "ON scheduled_sends(status, scheduled_at)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_scheduled_user "
            "ON scheduled_sends(imap_user, status)"
        )
        scheduled_cols = {
            row[1]
            for row in con.execute("PRAGMA table_info(scheduled_sends)").fetchall()
        }
        if "origin" not in scheduled_cols:
            con.execute(
                "ALTER TABLE scheduled_sends "
                "ADD COLUMN origin TEXT NOT NULL DEFAULT 'scheduled'"
            )
        # Snooze queue
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS snoozed_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imap_user TEXT NOT NULL,
                imap_pass_enc TEXT NOT NULL,
                message_id TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                from_addr TEXT NOT NULL DEFAULT '',
                original_folder TEXT NOT NULL DEFAULT 'INBOX',
                snoozed_at TEXT NOT NULL DEFAULT (datetime('now')),
                wake_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT,
                woken_at TEXT
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_snooze_due "
            "ON snoozed_messages(status, wake_at)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_snooze_user "
            "ON snoozed_messages(imap_user, status)"
        )
        # Web Push subscriptions
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imap_user TEXT NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                user_agent TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_notified_at TEXT
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_push_user "
            "ON push_subscriptions(imap_user)"
        )
        # Sieve filter rules — client-side definition; when managesieve is
        # unavailable we fall back to applying them in post-fetch pass.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sieve_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imap_user TEXT NOT NULL,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 100,
                conditions_json TEXT NOT NULL,
                actions_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_sieve_user "
            "ON sieve_rules(imap_user, enabled, priority)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_forwarding_settings (
                imap_user TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                address TEXT NOT NULL DEFAULT '',
                keep_copy INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 0,
                sync_message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        _db_add_column_if_missing(
            con,
            "mail_forwarding_settings",
            "revision",
            "INTEGER NOT NULL DEFAULT 0",
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mail_autoreply_settings (
                imap_user TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                subject TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                start_date TEXT,
                end_date TEXT,
                repeat_days INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 0,
                sync_message TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        _db_add_column_if_missing(
            con,
            "mail_autoreply_settings",
            "revision",
            "INTEGER NOT NULL DEFAULT 0",
        )
        # Managed group, distribution, and service addresses.
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_mailboxes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                imap_pass_enc TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        _db_add_column_if_missing(
            con,
            "shared_mailboxes",
            "kind",
            "TEXT NOT NULL DEFAULT 'group'",
        )
        _db_add_column_if_missing(
            con,
            "shared_mailboxes",
            "technical_role",
            "TEXT NOT NULL DEFAULT ''",
        )
        _db_add_column_if_missing(
            con,
            "shared_mailboxes",
            "description",
            "TEXT NOT NULL DEFAULT ''",
        )
        _db_add_column_if_missing(
            con,
            "shared_mailboxes",
            "responsible_user",
            "TEXT NOT NULL DEFAULT ''",
        )
        _db_add_column_if_missing(
            con,
            "shared_mailboxes",
            "status",
            "TEXT NOT NULL DEFAULT 'active'",
        )
        _db_add_column_if_missing(
            con,
            "shared_mailboxes",
            "last_error",
            "TEXT NOT NULL DEFAULT ''",
        )
        _db_add_column_if_missing(
            con,
            "shared_mailboxes",
            "company",
            "TEXT NOT NULL DEFAULT 'Corp'",
        )
        _db_add_column_if_missing(
            con,
            "shared_mailboxes",
            "directory_domain",
            "TEXT NOT NULL DEFAULT 'corp.local'",
        )
        _db_add_column_if_missing(
            con,
            "shared_mailboxes",
            "revision",
            "INTEGER NOT NULL DEFAULT 1",
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_mailbox_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox_id INTEGER NOT NULL,
                member_user TEXT NOT NULL,
                can_send INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(mailbox_id, member_user),
                FOREIGN KEY(mailbox_id) REFERENCES shared_mailboxes(id) ON DELETE CASCADE
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_members_user "
            "ON shared_mailbox_members(member_user)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_mailbox_recipients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox_id INTEGER NOT NULL,
                recipient_email TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(mailbox_id, recipient_email),
                FOREIGN KEY(mailbox_id) REFERENCES shared_mailboxes(id) ON DELETE CASCADE
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_recipients_mailbox "
            "ON shared_mailbox_recipients(mailbox_id)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS shared_mailbox_ad_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox_id INTEGER NOT NULL,
                group_dn TEXT NOT NULL,
                group_name TEXT NOT NULL DEFAULT '',
                can_send INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(mailbox_id, group_dn),
                FOREIGN KEY(mailbox_id) REFERENCES shared_mailboxes(id) ON DELETE CASCADE
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_shared_ad_groups_mailbox "
            "ON shared_mailbox_ad_groups(mailbox_id)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS managed_address_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                mailbox_id INTEGER,
                email TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                secret_enc TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_managed_address_operations_status "
            "ON managed_address_operations(status, id)"
        )
        _db_add_column_if_missing(
            con,
            "managed_address_operations",
            "attempts",
            "INTEGER NOT NULL DEFAULT 0",
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS mailadmin_admin_groups (
                group_dn TEXT PRIMARY KEY,
                group_name TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        admin_group_count = int(
            con.execute(
                "SELECT COUNT(*) FROM mailadmin_admin_groups"
            ).fetchone()[0]
        )
        if admin_group_count == 0:
            for group_dn in DEFAULT_ADMIN_LDAP_GROUPS:
                con.execute(
                    """
                    INSERT INTO mailadmin_admin_groups (group_dn, group_name)
                    VALUES (?, ?)
                    """,
                    (group_dn, _ldap_dn_common_name(group_dn)),
                )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS web_sessions (
                token TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                expires REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_web_sessions_expires "
            "ON web_sessions(expires)"
        )
        if _PREVIOUS_SECRET_KEYS:
            migrated = _migrate_sqlite_secrets(
                con,
                current_key=_SECRET_KEY,
                previous_keys=_PREVIOUS_SECRET_KEYS,
            )
            if migrated:
                log.info(
                    "Migrated %d SQLite secret values to the active key",
                    migrated,
                )
        con.commit()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_audit_log(
    admin_user: str,
    action: str,
    target: str = "",
    details: Optional[Dict[str, Any]] = None,
    ip: str = "",
    ok: bool = True,
) -> None:
    """Insert an audit log row. Never raises (audit failures must not break ops)."""
    try:
        with _db_connection() as con:
            con.execute(
                "INSERT INTO audit_log (admin_user, action, target, details, ip, ok) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (admin_user, action, target,
                 json.dumps(details, ensure_ascii=False) if details else None,
                 ip, 1 if ok else 0),
            )
            con.commit()
    except Exception as e:
        log.warning("audit_log insert failed: %s", e)


def db_audit_query(
    limit: int = 100,
    offset: int = 0,
    admin_user: Optional[str] = None,
    action: Optional[str] = None,
    target_contains: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Query audit log with optional filters. Returns (rows, total_count)."""
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        where = []
        params: List[Any] = []
        if admin_user:
            where.append("admin_user = ?")
            params.append(admin_user)
        if action:
            where.append("action = ?")
            params.append(action)
        if target_contains:
            where.append("LOWER(target) LIKE ?")
            params.append(f"%{target_contains.lower()}%")
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        total = con.execute(
            f"SELECT COUNT(*) FROM audit_log {where_sql}", params
        ).fetchone()[0]
        rows = con.execute(
            f"SELECT id, ts, admin_user, action, target, details, ip, ok "
            f"FROM audit_log {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        result = []
        for r in rows:
            d = database.db_row_to_dict(r)
            d["details"] = database.db_parse_json_field(d.get("details"))
            d["ok"] = database.db_bool_from_int(d.get("ok"))
            result.append(d)
        return result, int(total)


def db_audit_query_mailbox(
    email_addr: str,
    hours: int = 72,
    limit: int = 250,
) -> List[Dict[str, Any]]:
    target = normalization.normalize_email(email_addr)
    if not target:
        return []
    local = validation.extract_email_local(target)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours or 72)))).replace(microsecond=0).isoformat()
    params: List[Any] = [
        cutoff,
        target,
        f"%{target}%",
        local,
        f"%->{local}%",
        limit,
    ]
    query = """
        SELECT id, ts, admin_user, action, target, details, ip, ok
        FROM audit_log
        WHERE julianday(ts) >= julianday(?)
          AND (
            LOWER(target) = ?
            OR LOWER(target) LIKE ?
            OR LOWER(target) = ?
            OR LOWER(target) LIKE ?
          )
        ORDER BY julianday(ts) DESC, id DESC
        LIMIT ?
    """
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(query, params).fetchall()
    result: List[Dict[str, Any]] = []
    for row in rows:
        data = database.db_row_to_dict(row)
        data["details"] = database.db_parse_json_field(data.get("details"))
        data["ok"] = database.db_bool_from_int(data.get("ok"))
        result.append(data)
    return result


def db_get_mailbox_lifecycle(email_addr: str) -> Optional[Dict[str, Any]]:
    target = normalization.normalize_email(email_addr)
    if not target:
        return None
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT email, suspended, suspended_hash, suspended_at, suspended_by, "
            "suspend_reason, restored_at, restored_by, restore_reason "
            "FROM mailbox_lifecycle WHERE email = ?",
            (target,),
        ).fetchone()
        if not row:
            return None
        data = database.db_row_to_dict(row)
    stored_hash = database.db_bool_from_int(data.get("suspended_hash"))
    data["suspended"] = database.db_bool_from_int(data.get("suspended"))
    data["state"] = "suspended" if data["suspended"] else "active"
    data["has_stored_hash"] = stored_hash
    data.pop("suspended_hash", None)
    return data


def db_list_mailbox_lifecycle_states() -> Dict[str, Dict[str, Any]]:
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT email, suspended, suspended_at, suspended_by, suspend_reason, "
            "restored_at, restored_by, restore_reason "
            "FROM mailbox_lifecycle"
        ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        data = database.db_row_to_dict(row)
        data["suspended"] = database.db_bool_from_int(data.get("suspended"))
        data["state"] = "suspended" if data["suspended"] else "active"
        out[normalization.normalize_email(data.get("email") or "")] = data
    return out


def db_mark_mailbox_suspended(
    email_addr: str,
    previous_hash: str,
    admin_user: str,
    reason: str = "",
) -> Dict[str, Any]:
    target = normalization.normalize_email(email_addr)
    ts = _utc_now_iso()
    with _db_connection() as con:
        con.execute(
            """
            INSERT INTO mailbox_lifecycle (
                email, suspended, suspended_hash, suspended_at, suspended_by,
                suspend_reason, restored_at, restored_by, restore_reason
            ) VALUES (?, 1, ?, ?, ?, ?, NULL, NULL, NULL)
            ON CONFLICT(email) DO UPDATE SET
                suspended = 1,
                suspended_hash = excluded.suspended_hash,
                suspended_at = excluded.suspended_at,
                suspended_by = excluded.suspended_by,
                suspend_reason = excluded.suspend_reason,
                restored_at = NULL,
                restored_by = NULL,
                restore_reason = NULL
            """,
            (target, previous_hash, ts, admin_user, reason.strip()),
        )
        con.commit()
    return db_get_mailbox_lifecycle(target) or {"email": target, "state": "suspended", "suspended": True}


def db_mark_mailbox_restored(email_addr: str, admin_user: str, reason: str = "") -> Dict[str, Any]:
    target = normalization.normalize_email(email_addr)
    ts = _utc_now_iso()
    with _db_connection() as con:
        con.execute(
            """
            UPDATE mailbox_lifecycle
            SET suspended = 0,
                restored_at = ?,
                restored_by = ?,
                restore_reason = ?
            WHERE email = ?
            """,
            (ts, admin_user, reason.strip(), target),
        )
        con.commit()
    return db_get_mailbox_lifecycle(target) or {"email": target, "state": "active", "suspended": False}


_mailbox_lifecycle_locks_guard = threading.Lock()
_mailbox_lifecycle_locks: Dict[str, threading.Lock] = {}


def _mailbox_lifecycle_lock(email_addr: str) -> threading.Lock:
    email = normalization.normalize_email(email_addr)
    with _mailbox_lifecycle_locks_guard:
        return _mailbox_lifecycle_locks.setdefault(
            email,
            threading.Lock(),
        )


def _mailbox_lifecycle_operation_start(
    action: str,
    email_addr: str,
    emp_id: int,
    admin_user: str,
    reason: str,
    password: str,
    previous_hash: str = "",
) -> int:
    with _db_connection() as con:
        try:
            cursor = con.execute(
                """
                INSERT INTO mailbox_lifecycle_operations (
                    action, email, emp_id, admin_user, reason,
                    password_enc, previous_hash, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    str(action or "").strip().lower(),
                    str(email_addr or "").strip().lower(),
                    int(emp_id),
                    str(admin_user or ""),
                    str(reason or ""),
                    encrypt_secret(str(password or "")),
                    str(previous_hash or ""),
                ),
            )
            con.commit()
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                "mailbox lifecycle operation already pending"
            ) from exc
    return int(cursor.lastrowid)


def _mailbox_lifecycle_operation_update(
    operation_id: int,
    status: str,
    error: str = "",
) -> None:
    with _db_connection() as con:
        con.execute(
            """
            UPDATE mailbox_lifecycle_operations
            SET status = ?,
                last_error = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (
                str(status or ""),
                str(error or "")[:1000],
                int(operation_id),
            ),
        )
        con.commit()


def _mailbox_lifecycle_operation_is_reconciling(
    operation_id: int,
) -> bool:
    with _db_connection() as con:
        row = con.execute(
            "SELECT status FROM mailbox_lifecycle_operations WHERE id = ?",
            (int(operation_id),),
        ).fetchone()
    return bool(row and str(row[0]) == "reconciling")


def _mailbox_lifecycle_operation_claim_stale(
    limit: int = 20,
) -> List[Dict[str, Any]]:
    stale_seconds = max(
        30,
        int(os.environ.get("MAILBOX_LIFECYCLE_STALE_SECONDS", "120")),
    )
    stale_modifier = f"-{stale_seconds} seconds"
    claimed: List[Dict[str, Any]] = []
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT id, action, email, emp_id, admin_user, reason,
                   password_enc, previous_hash, attempts
            FROM mailbox_lifecycle_operations
            WHERE status IN ('pending', 'reconciling')
              AND updated_at <= datetime('now', ?)
            ORDER BY id
            LIMIT ?
            """,
            (
                stale_modifier,
                max(1, min(int(limit), 100)),
            ),
        ).fetchall()
        for row in rows:
            cursor = con.execute(
                """
                UPDATE mailbox_lifecycle_operations
                SET status = 'reconciling',
                    attempts = attempts + 1,
                    updated_at = datetime('now')
                WHERE id = ?
                  AND status IN ('pending', 'reconciling')
                  AND updated_at <= datetime('now', ?)
                """,
                (int(row["id"]), stale_modifier),
            )
            if cursor.rowcount == 1:
                item = dict(row)
                item["attempts"] = int(row["attempts"] or 0) + 1
                claimed.append(item)
        con.commit()
    return claimed


def _reconcile_mailbox_lifecycle_operations() -> int:
    reconciled = 0
    for operation in _mailbox_lifecycle_operation_claim_stale():
        operation_id = int(operation["id"])
        action = str(operation["action"])
        email_addr = str(operation["email"])
        mail_login = email_addr.split("@", 1)[0]
        with _mailbox_lifecycle_lock(email_addr):
            if not _mailbox_lifecycle_operation_is_reconciling(
                operation_id
            ):
                continue
            try:
                password = decrypt_secret(
                    str(operation.get("password_enc") or "")
                )
                if not password:
                    raise RuntimeError(
                        "mailbox lifecycle password is unavailable"
                    )
                payload = {
                    "emp_id": int(operation["emp_id"]),
                    "mail_login": mail_login,
                    "password": password,
                }
                if action == "suspend":
                    payload["persist_credentials"] = False
                _mail_control_request(
                    "/api/mail/reset_password",
                    payload,
                    60,
                )
                if action == "suspend":
                    db_mark_mailbox_suspended(
                        email_addr,
                        str(operation.get("previous_hash") or ""),
                        str(operation.get("admin_user") or ""),
                        str(operation.get("reason") or ""),
                    )
                elif action == "restore":
                    db_mark_mailbox_restored(
                        email_addr,
                        str(operation.get("admin_user") or ""),
                        str(operation.get("reason") or ""),
                    )
                else:
                    raise RuntimeError(
                        f"unsupported mailbox lifecycle action: {action}"
                    )
                _mailbox_lifecycle_operation_update(
                    operation_id,
                    "completed",
                )
                reconciled += 1
            except Exception as exc:
                _mailbox_lifecycle_operation_update(
                    operation_id,
                    "pending",
                    str(exc),
                )
                attempts = int(operation.get("attempts") or 1)
                log_method = log.critical if attempts >= 5 else log.error
                log_method(
                    "mailbox lifecycle reconcile failed "
                    "id=%s action=%s: %s",
                    operation_id,
                    action,
                    exc,
                )
    return reconciled


def db_get_alias(ad_login: str) -> Optional[Dict[str, Any]]:
    emp = _panel_find_employee_by_exact_login(ad_login)
    if emp and emp.get("mail_login"):
        return {
            "id": emp.get("id"),
            "ad_login": emp.get("ad_login") or ad_login.lower(),
            "mail_login": emp.get("mail_login"),
            "created_at": emp.get("created_at"),
            "updated_at": emp.get("created_at"),
            "source": "proxy_employees",
        }
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM mail_aliases WHERE ad_login = ?", (ad_login.lower(),)
        ).fetchone()
        return dict(row) if row else None


def db_set_alias(ad_login: str, mail_login: str, emp_id: Optional[int] = None) -> Dict[str, Any]:
    ad_login = ad_login.strip().lower()
    mail_login = mail_login.strip().lower()
    if emp_id:
        update_fields: Dict[str, Any] = {"mail_login": mail_login}
        if ad_login:
            update_fields["ad_login"] = ad_login
        row = _panel_update_employee_identity(emp_id, **update_fields)
        if row and row.get("mail_login"):
            return {
                "id": row.get("id"),
                "ad_login": row.get("ad_login") or ad_login,
                "mail_login": row.get("mail_login"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("created_at"),
                "source": "proxy_employees",
            }
        raise ValueError("Не удалось сохранить привязку сотрудника")
    if not ad_login:
        raise ValueError("Логин AD обязателен для алиаса без сотрудника")
    now = datetime.now(timezone.utc).isoformat()
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        # Conflict check: someone else owns this mail_login?
        existing = con.execute(
            "SELECT ad_login FROM mail_aliases WHERE mail_login = ?",
            (mail_login,),
        ).fetchone()
        if existing and existing["ad_login"] != ad_login:
            raise ValueError("Этот email-логин уже занят")

        con.execute(
            """
            INSERT INTO mail_aliases (ad_login, mail_login, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ad_login) DO UPDATE SET
                mail_login = excluded.mail_login,
                updated_at = excluded.updated_at
            """,
            (ad_login, mail_login, now, now),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM mail_aliases WHERE ad_login = ?", (ad_login,)
        ).fetchone()
        return dict(row)


def db_get_user_signature(ad_login: str) -> str:
    with _db_connection() as con:
        row = con.execute(
            "SELECT signature FROM user_settings WHERE ad_login = ?",
            (ad_login.lower(),),
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else ""


def db_set_user_signature(ad_login: str, signature: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with _db_connection() as con:
        con.execute(
            """
            INSERT INTO user_settings (ad_login, signature, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(ad_login) DO UPDATE SET
                signature = excluded.signature,
                updated_at = excluded.updated_at
            """,
            (ad_login.lower(), signature or "", now),
        )
        con.commit()
    return {"ad_login": ad_login.lower(), "signature": signature or "", "updated_at": now}


def db_get_forwarding(imap_user: str) -> Dict[str, Any]:
    user = normalization.normalize_user(imap_user)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT imap_user, enabled, address, keep_copy, active,
                   sync_message, updated_at, revision
            FROM mail_forwarding_settings
            WHERE imap_user = ?
            """,
            (user,),
        ).fetchone()
    if not row:
        return {
            "imap_user": user,
            "enabled": False,
            "address": "",
            "keep_copy": True,
            "active": False,
            "sync_message": "",
            "updated_at": None,
            "revision": 0,
        }
    payload = database.db_row_to_dict(row)
    payload["enabled"] = database.db_bool_from_int(payload.get("enabled"))
    payload["keep_copy"] = database.db_bool_from_int(payload.get("keep_copy"))
    payload["active"] = database.db_bool_from_int(payload.get("active"))
    payload["revision"] = database.db_int_from_value(payload.get("revision"))
    return payload


def db_set_forwarding(
    imap_user: str,
    enabled: bool,
    address: str,
    keep_copy: bool,
) -> Dict[str, Any]:
    user = normalization.normalize_user(imap_user)
    now = datetime.now(timezone.utc).isoformat()
    with _db_connection() as con:
        con.execute(
            """
            INSERT INTO mail_forwarding_settings (
                imap_user, enabled, address, keep_copy, active,
                sync_message, updated_at, revision
            )
            VALUES (?, ?, ?, ?, 0, '', ?, 1)
            ON CONFLICT(imap_user) DO UPDATE SET
                enabled = excluded.enabled,
                address = excluded.address,
                keep_copy = excluded.keep_copy,
                active = 0,
                sync_message = '',
                updated_at = excluded.updated_at,
                revision = mail_forwarding_settings.revision + 1
            """,
            (user, 1 if enabled else 0, address or "", 1 if keep_copy else 0, now),
        )
        con.commit()
    return db_get_forwarding(user)


_FORWARDING_REVISION_UNSET = object()
_SIEVE_USER_LOCKS_MAX = 256
_sieve_user_locks_guard = threading.Lock()
_sieve_user_locks: Dict[str, Dict[str, Any]] = {}


@contextmanager
def _sieve_user_lock(imap_user: str):
    user = normalization.normalize_user(imap_user)
    with _sieve_user_locks_guard:
        entry = _sieve_user_locks.get(user)
        if entry is None:
            if len(_sieve_user_locks) >= _SIEVE_USER_LOCKS_MAX:
                for idle_user, idle_entry in tuple(_sieve_user_locks.items()):
                    if idle_entry["refs"] == 0:
                        _sieve_user_locks.pop(idle_user, None)
                        break
            entry = {"lock": threading.RLock(), "refs": 0}
            _sieve_user_locks[user] = entry
        entry["refs"] += 1
        user_lock = entry["lock"]
    try:
        with user_lock:
            yield
    finally:
        with _sieve_user_locks_guard:
            entry["refs"] -= 1
            if entry["refs"] == 0 and len(_sieve_user_locks) > _SIEVE_USER_LOCKS_MAX:
                _sieve_user_locks.pop(user, None)


def db_record_forwarding_sync(
    imap_user: str,
    active: bool,
    sync_message: str,
    expected_revision: Any = _FORWARDING_REVISION_UNSET,
) -> Dict[str, Any]:
    user = normalization.normalize_user(imap_user)
    message = normalization.normalize_multiline_string(sync_message, max_length=300)
    now = datetime.now(timezone.utc).isoformat()
    with _db_connection() as con:
        if expected_revision is _FORWARDING_REVISION_UNSET:
            cursor = con.execute(
                """
                INSERT INTO mail_forwarding_settings (
                    imap_user, enabled, address, keep_copy, active,
                    sync_message, updated_at
                )
                VALUES (?, 0, '', 1, ?, ?, ?)
                ON CONFLICT(imap_user) DO UPDATE SET
                    active = excluded.active,
                    sync_message = excluded.sync_message
                """,
                (user, 1 if active else 0, message, now),
            )
            stale = False
        elif expected_revision is None:
            cursor = con.execute(
                """
                INSERT OR IGNORE INTO mail_forwarding_settings (
                    imap_user, enabled, address, keep_copy, active,
                    sync_message, updated_at
                )
                VALUES (?, 0, '', 1, ?, ?, ?)
                """,
                (user, 1 if active else 0, message, now),
            )
            stale = cursor.rowcount == 0
        elif int(expected_revision) == 0:
            cursor = con.execute(
                """
                INSERT INTO mail_forwarding_settings (
                    imap_user, enabled, address, keep_copy, active,
                    sync_message, updated_at, revision
                )
                VALUES (?, 0, '', 1, ?, ?, ?, 0)
                ON CONFLICT(imap_user) DO UPDATE SET
                    active = excluded.active,
                    sync_message = excluded.sync_message
                WHERE mail_forwarding_settings.revision = 0
                """,
                (user, 1 if active else 0, message, now),
            )
            stale = cursor.rowcount == 0
        else:
            cursor = con.execute(
                """
                UPDATE mail_forwarding_settings
                SET active = ?, sync_message = ?
                WHERE imap_user = ? AND revision = ?
                """,
                (1 if active else 0, message, user, int(expected_revision)),
            )
            stale = cursor.rowcount == 0
        con.commit()
    result = db_get_forwarding(user)
    result["_sync_stale"] = stale
    return result


def _forwarding_public_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "enabled": bool(row.get("enabled")),
        "address": str(row.get("address") or ""),
        "keep_copy": bool(row.get("keep_copy", True)),
        "active": bool(row.get("active")),
        "sync_message": str(row.get("sync_message") or ""),
    }


def db_get_autoreply(imap_user: str) -> Dict[str, Any]:
    user = normalization.normalize_user(imap_user)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            """
            SELECT imap_user, enabled, subject, body, start_date, end_date,
                   repeat_days, active, sync_message, updated_at, revision
            FROM mail_autoreply_settings
            WHERE imap_user = ?
            """,
            (user,),
        ).fetchone()
    if not row:
        return {
            "imap_user": user,
            "enabled": False,
            "subject": "",
            "body": "",
            "start_date": None,
            "end_date": None,
            "repeat_days": 1,
            "active": False,
            "sync_message": "",
            "updated_at": None,
            "revision": 0,
        }
    payload = database.db_row_to_dict(row)
    payload["enabled"] = database.db_bool_from_int(payload.get("enabled"))
    payload["repeat_days"] = database.db_int_from_value(payload.get("repeat_days"))
    payload["active"] = database.db_bool_from_int(payload.get("active"))
    payload["revision"] = database.db_int_from_value(payload.get("revision"))
    return payload


def db_set_autoreply(
    imap_user: str,
    enabled: bool,
    subject: str,
    body: str,
    start_date: Optional[str],
    end_date: Optional[str],
    repeat_days: int,
) -> Dict[str, Any]:
    user = normalization.normalize_user(imap_user)
    now = datetime.now(timezone.utc).isoformat()
    with _db_connection() as con:
        con.execute(
            """
            INSERT INTO mail_autoreply_settings (
                imap_user, enabled, subject, body, start_date, end_date,
                repeat_days, active, sync_message, updated_at, revision
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, '', ?, 1)
            ON CONFLICT(imap_user) DO UPDATE SET
                enabled = excluded.enabled,
                subject = excluded.subject,
                body = excluded.body,
                start_date = excluded.start_date,
                end_date = excluded.end_date,
                repeat_days = excluded.repeat_days,
                active = 0,
                sync_message = '',
                updated_at = excluded.updated_at,
                revision = mail_autoreply_settings.revision + 1
            """,
            (
                user,
                1 if enabled else 0,
                subject,
                body,
                start_date,
                end_date,
                int(repeat_days),
                now,
            ),
        )
        con.commit()
    return db_get_autoreply(user)


_AUTOREPLY_REVISION_UNSET = object()


def db_record_autoreply_sync(
    imap_user: str,
    active: bool,
    sync_message: str,
    expected_revision: Any = _AUTOREPLY_REVISION_UNSET,
) -> Dict[str, Any]:
    user = str(imap_user or "").strip().lower()
    message = re.sub(r"[\r\n]+", " ", str(sync_message or "")).strip()[:300]
    now = datetime.now(timezone.utc).isoformat()
    with _db_connection() as con:
        if expected_revision is _AUTOREPLY_REVISION_UNSET:
            cursor = con.execute(
                """
                INSERT INTO mail_autoreply_settings (
                    imap_user, enabled, subject, body, start_date, end_date,
                    repeat_days, active, sync_message, updated_at
                )
                VALUES (?, 0, '', '', NULL, NULL, 1, ?, ?, ?)
                ON CONFLICT(imap_user) DO UPDATE SET
                    active = excluded.active,
                    sync_message = excluded.sync_message
                """,
                (user, 1 if active else 0, message, now),
            )
            stale = False
        elif expected_revision is None:
            cursor = con.execute(
                """
                INSERT OR IGNORE INTO mail_autoreply_settings (
                    imap_user, enabled, subject, body, start_date, end_date,
                    repeat_days, active, sync_message, updated_at
                )
                VALUES (?, 0, '', '', NULL, NULL, 1, ?, ?, ?)
                """,
                (user, 1 if active else 0, message, now),
            )
            stale = cursor.rowcount == 0
        elif int(expected_revision) == 0:
            cursor = con.execute(
                """
                INSERT INTO mail_autoreply_settings (
                    imap_user, enabled, subject, body, start_date, end_date,
                    repeat_days, active, sync_message, updated_at, revision
                )
                VALUES (?, 0, '', '', NULL, NULL, 1, ?, ?, ?, 0)
                ON CONFLICT(imap_user) DO UPDATE SET
                    active = excluded.active,
                    sync_message = excluded.sync_message
                WHERE mail_autoreply_settings.revision = 0
                """,
                (user, 1 if active else 0, message, now),
            )
            stale = cursor.rowcount == 0
        else:
            cursor = con.execute(
                """
                UPDATE mail_autoreply_settings
                SET active = ?, sync_message = ?
                WHERE imap_user = ? AND revision = ?
                """,
                (1 if active else 0, message, user, int(expected_revision)),
            )
            stale = cursor.rowcount == 0
        con.commit()
    result = db_get_autoreply(user)
    result["_sync_stale"] = stale
    return result


def _autoreply_is_effective(row: Dict[str, Any]) -> bool:
    if not row.get("enabled"):
        return False
    today = datetime.now().date().isoformat()
    start_date = row.get("start_date")
    end_date = row.get("end_date")
    return not (
        (start_date and today < str(start_date))
        or (end_date and today > str(end_date))
    )


def _autoreply_public_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "enabled": bool(row.get("enabled")),
        "subject": str(row.get("subject") or ""),
        "body": str(row.get("body") or ""),
        "start_date": row.get("start_date") or None,
        "end_date": row.get("end_date") or None,
        "repeat_days": int(row.get("repeat_days") or 1),
        "active": bool(row.get("active")),
        "effective": _autoreply_is_effective(row),
        "sync_message": str(row.get("sync_message") or ""),
    }


def db_list_user_templates(ad_login: str) -> List[Dict[str, Any]]:
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, name, subject, body, created_at, updated_at "
            "FROM mail_templates WHERE ad_login = ? ORDER BY updated_at DESC, id DESC",
            (ad_login.lower(),),
        ).fetchall()
        return [dict(r) for r in rows]


def db_upsert_user_template(
    ad_login: str,
    name: str,
    subject: str,
    body: str,
    template_id: Optional[int] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        if template_id:
            row = con.execute(
                "SELECT id FROM mail_templates WHERE id = ? AND ad_login = ?",
                (template_id, ad_login.lower()),
            ).fetchone()
            if not row:
                raise ValueError("Шаблон не найден")
            con.execute(
                "UPDATE mail_templates SET name = ?, subject = ?, body = ?, updated_at = ? "
                "WHERE id = ? AND ad_login = ?",
                (name, subject, body, now, template_id, ad_login.lower()),
            )
        else:
            cur = con.execute(
                "INSERT INTO mail_templates (ad_login, name, subject, body, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ad_login.lower(), name, subject, body, now, now),
            )
            template_id = int(cur.lastrowid)
        con.commit()
        row = con.execute(
            "SELECT id, name, subject, body, created_at, updated_at "
            "FROM mail_templates WHERE id = ? AND ad_login = ?",
            (template_id, ad_login.lower()),
        ).fetchone()
        return dict(row) if row else {}


def db_delete_user_template(ad_login: str, template_id: int) -> bool:
    with _db_connection() as con:
        cur = con.execute(
            "DELETE FROM mail_templates WHERE id = ? AND ad_login = ?",
            (template_id, ad_login.lower()),
        )
        con.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------

_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()
_profile_refresh_tasks: set[asyncio.Task[Any]] = set()
_mailserver_apply_thread: Optional[threading.Thread] = None
_mailserver_apply_thread_lock = threading.Lock()


def _session_db_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = deepcopy(data)
    payload.pop("_cache", None)
    imap_pass = payload.pop("imap_pass", None)
    if imap_pass:
        payload["imap_pass_enc"] = encrypt_secret(str(imap_pass))
    return payload


def _session_from_db_payload(payload: Dict[str, Any], expires: float) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    data = deepcopy(payload)
    imap_pass_enc = data.pop("imap_pass_enc", "")
    if imap_pass_enc:
        imap_pass = decrypt_secret(str(imap_pass_enc))
        if not imap_pass:
            return None
        data["imap_pass"] = imap_pass
    data["expires"] = float(expires)
    data.setdefault("_cache", {})
    return data


def _session_persist(token: str, data: Dict[str, Any]) -> None:
    payload = _session_db_payload(data)
    with _db_connection() as con:
        con.execute(
            "INSERT OR REPLACE INTO web_sessions (token, data_json, expires, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                token,
                json.dumps(payload, ensure_ascii=False),
                float(data["expires"]),
                _utc_now_iso(),
            ),
        )
        con.commit()


def _session_persist_if_active(token: str, data: Dict[str, Any]) -> bool:
    with _sessions_lock:
        if (
            _sessions.get(token) is not data
            or float(data.get("expires", 0) or 0) < time.time()
        ):
            return False
        _session_persist(token, data)
    return True


def _session_restore(token: str) -> Optional[Dict[str, Any]]:
    with _db_connection() as con:
        row = con.execute(
            "SELECT data_json, expires FROM web_sessions WHERE token = ?",
            (token,),
        ).fetchone()
    if not row:
        return None
    expires = float(row[1] or 0)
    if expires < time.time():
        with _db_connection() as con:
            con.execute("DELETE FROM web_sessions WHERE token = ?", (token,))
            con.commit()
        return None
    try:
        payload = json.loads(row[0] or "{}")
    except Exception:
        return None
    return _session_from_db_payload(payload, expires)


def session_create(data: Dict[str, Any]) -> str:
    token = secrets.token_urlsafe(32)
    data = deepcopy(data)
    data["expires"] = time.time() + CFG.SESSION_TTL
    data.setdefault("_cache", {})
    with _sessions_lock:
        _sessions[token] = data
    _session_persist(token, data)
    return token


def session_get(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    with _sessions_lock:
        sess = _sessions.get(token)
        if not sess:
            sess = _session_restore(token)
            if not sess:
                return None
            _sessions[token] = sess
        if sess["expires"] < time.time():
            _sessions.pop(token, None)
            with _db_connection() as con:
                con.execute("DELETE FROM web_sessions WHERE token = ?", (token,))
                con.commit()
            return None
        return sess


def session_delete(token: Optional[str]) -> None:
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)
    with _db_connection() as con:
        con.execute("DELETE FROM web_sessions WHERE token = ?", (token,))
        con.commit()


def _session_cache_get(sess: Dict[str, Any], key: Tuple[Any, ...]) -> Optional[Any]:
    cache = sess.get("_cache")
    if not isinstance(cache, dict):
        return None
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    if float(entry.get("expires", 0) or 0) < time.time():
        cache.pop(key, None)
        return None
    return deepcopy(entry.get("value"))


def _session_cache_put(sess: Dict[str, Any], key: Tuple[Any, ...], value: Any, ttl: int) -> Any:
    cache = sess.setdefault("_cache", {})
    if not isinstance(cache, dict):
        cache = {}
        sess["_cache"] = cache
    cache[key] = {
        "expires": time.time() + max(1, int(ttl or 1)),
        "value": deepcopy(value),
    }
    return value


def _session_cache_clear(sess: Dict[str, Any], prefix: Optional[str] = None) -> None:
    cache = sess.get("_cache")
    if not isinstance(cache, dict) or not cache:
        return
    if not prefix:
        cache.clear()
        return
    dead = [key for key in list(cache.keys()) if isinstance(key, tuple) and key and key[0] == prefix]
    for key in dead:
        cache.pop(key, None)


def _invalidate_mailbox_caches(sess: Dict[str, Any]) -> None:
    _session_cache_clear(sess, "bootstrap")
    _session_cache_clear(sess, "folders")
    _session_cache_clear(sess, "messages")
    _session_cache_clear(sess, "message")


def session_cleanup_loop() -> None:
    while True:
        time.sleep(600)
        now = time.time()
        with _sessions_lock:
            expired = [k for k, v in _sessions.items() if v["expires"] < now]
            for k in expired:
                _sessions.pop(k, None)
        with _db_connection() as con:
            con.execute("DELETE FROM web_sessions WHERE expires < ?", (now,))
            con.commit()
        if expired:
            log.info("Cleaned %d expired sessions", len(expired))


# ---------------------------------------------------------------------------
# Symmetric encryption for stored IMAP passwords (scheduled sends, snooze)
# ---------------------------------------------------------------------------
# We need to store the IMAP password briefly to let a background worker
# perform SMTP/IMAP operations on the user's behalf (Send Later, Snooze).
# Uses a derived key from SECRET_KEY env + a per-record salt. AES would be
# nicer but we want zero extra deps — use Fernet from `cryptography` if
# installed, else fall back to XOR+HMAC (still far better than plaintext).

_SECRET_KEY = os.environ.get(
    "WEBMAIL_SECRET_KEY",
    os.environ.get("RUPOCHTA_INTERNAL_TOKEN", ""),
).encode("utf-8")
_PREVIOUS_SECRET_KEYS = tuple(
    value.strip().encode("utf-8")
    for value in os.environ.get("WEBMAIL_SECRET_KEY_PREVIOUS", "").split(",")
    if value.strip()
)

try:
    from cryptography.fernet import Fernet as _Fernet  # type: ignore
    _FERNET_AVAILABLE = True
except Exception:
    _Fernet = None  # type: ignore
    _FERNET_AVAILABLE = False
    log.warning("cryptography.Fernet not available — using fallback XOR encryption")


def _fernet_for_key(key: bytes):
    if not _FERNET_AVAILABLE or _Fernet is None:
        return None
    import base64 as _b64f
    import hashlib as _hf
    return _Fernet(_b64f.urlsafe_b64encode(_hf.sha256(key).digest()))


def _encrypt_secret_with_key(
    plaintext: str,
    key: bytes,
    *,
    prefer_fernet: bool = True,
) -> str:
    if not plaintext:
        return ""
    fernet = _fernet_for_key(key) if prefer_fernet else None
    if fernet is not None:
        return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    # Fallback: XOR with SHA256-derived keystream + HMAC tag
    import base64 as _b64f
    import hashlib as _hf
    import hmac as _hm
    data = plaintext.encode("utf-8")
    # Generate keystream by hashing KEY + counter
    ks = b""
    counter = 0
    while len(ks) < len(data):
        ks += _hf.sha256(key + counter.to_bytes(4, "big")).digest()
        counter += 1
    cipher = bytes(a ^ b for a, b in zip(data, ks[:len(data)]))
    tag = _hm.new(key, cipher, _hf.sha256).digest()[:16]
    return "xor$" + _b64f.urlsafe_b64encode(cipher + tag).decode("ascii")


def _decrypt_secret_with_keys(
    ciphertext: str,
    keys: Tuple[bytes, ...],
) -> Tuple[Optional[str], Optional[int]]:
    if not ciphertext:
        return None, None
    if ciphertext.startswith("xor$"):
        import base64 as _b64f
        import hashlib as _hf
        import hmac as _hm
        try:
            raw = _b64f.urlsafe_b64decode(ciphertext[4:].encode("ascii"))
            if len(raw) < 16:
                return None, None
            cipher, tag = raw[:-16], raw[-16:]
            for index, key in enumerate(keys):
                want_tag = _hm.new(key, cipher, _hf.sha256).digest()[:16]
                if not _hm.compare_digest(want_tag, tag):
                    continue
                ks = b""
                counter = 0
                while len(ks) < len(cipher):
                    ks += _hf.sha256(
                        key + counter.to_bytes(4, "big")
                    ).digest()
                    counter += 1
                plaintext = bytes(
                    a ^ b for a, b in zip(cipher, ks[:len(cipher)])
                ).decode("utf-8")
                return plaintext, index
        except Exception:
            return None, None
        return None, None
    if _FERNET_AVAILABLE:
        for index, key in enumerate(keys):
            fernet = _fernet_for_key(key)
            if fernet is None:
                continue
            try:
                plaintext = fernet.decrypt(
                    ciphertext.encode("ascii")
                ).decode("utf-8")
                return plaintext, index
            except Exception:
                continue
    return None, None


_SQLITE_SECRET_COLUMNS = (
    ("mailbox_lifecycle_operations", "password_enc"),
    ("scheduled_sends", "imap_pass_enc"),
    ("snoozed_messages", "imap_pass_enc"),
    ("shared_mailboxes", "imap_pass_enc"),
    ("managed_address_operations", "secret_enc"),
)


def _migrate_sqlite_secrets(
    con: sqlite3.Connection,
    *,
    current_key: bytes,
    previous_keys: Tuple[bytes, ...],
) -> int:
    keys = (current_key, *previous_keys)
    migrated = 0
    con.execute("SAVEPOINT webmail_secret_migration")
    try:
        for table, column in _SQLITE_SECRET_COLUMNS:
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            columns = {
                str(row[1])
                for row in con.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in columns:
                continue
            rows = con.execute(
                f"SELECT rowid, {column} FROM {table} "
                f"WHERE {column} IS NOT NULL AND {column} != ''"
            ).fetchall()
            for rowid, ciphertext in rows:
                plaintext, key_index = _decrypt_secret_with_keys(
                    str(ciphertext),
                    keys,
                )
                if plaintext is None or key_index is None:
                    raise RuntimeError(
                        f"Cannot decrypt {table}.{column} row {rowid}; "
                        "secret migration rolled back"
                    )
                if key_index == 0 and (
                    not str(ciphertext).startswith("xor$")
                    or not _FERNET_AVAILABLE
                ):
                    continue
                replacement = _encrypt_secret_with_key(plaintext, current_key)
                con.execute(
                    f"UPDATE {table} SET {column}=? WHERE rowid=?",
                    (replacement, rowid),
                )
                migrated += 1
        session_table_exists = con.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='web_sessions'"
        ).fetchone()
        if session_table_exists:
            for rowid, data_json in con.execute(
                "SELECT rowid, data_json FROM web_sessions"
            ).fetchall():
                try:
                    payload = json.loads(str(data_json))
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                ciphertext = str(payload.get("imap_pass_enc") or "")
                if not ciphertext:
                    continue
                plaintext, key_index = _decrypt_secret_with_keys(
                    ciphertext,
                    keys,
                )
                if plaintext is None or key_index is None:
                    raise RuntimeError(
                        f"Cannot decrypt web_sessions.data_json row {rowid}; "
                        "secret migration rolled back"
                    )
                if key_index == 0 and (
                    not ciphertext.startswith("xor$")
                    or not _FERNET_AVAILABLE
                ):
                    continue
                payload["imap_pass_enc"] = _encrypt_secret_with_key(
                    plaintext,
                    current_key,
                )
                con.execute(
                    "UPDATE web_sessions SET data_json=? WHERE rowid=?",
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        rowid,
                    ),
                )
                migrated += 1
        con.execute("RELEASE SAVEPOINT webmail_secret_migration")
        return migrated
    except Exception:
        con.execute("ROLLBACK TO SAVEPOINT webmail_secret_migration")
        con.execute("RELEASE SAVEPOINT webmail_secret_migration")
        raise


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for at-rest storage. Returns base64 string."""
    return _encrypt_secret_with_key(plaintext, _SECRET_KEY)


def decrypt_secret(ciphertext: str) -> Optional[str]:
    """Decrypt a previously encrypted secret. Returns None on failure."""
    plaintext, _key_index = _decrypt_secret_with_keys(
        ciphertext,
        (_SECRET_KEY, *_PREVIOUS_SECRET_KEYS),
    )
    return plaintext


# ---------------------------------------------------------------------------
# Central-auth mailbox bindings (ADR-0041)
# ---------------------------------------------------------------------------

def _sso_binding_subject_hash(subject: str) -> str:
    """Hash a central-auth subject with the service key.

    Keyed, not plain: the binding table would otherwise let anyone who reads the
    file confirm a guessed subject offline.
    """
    normalized = str(subject or "").strip()
    if not normalized:
        return ""
    return hmac.new(
        _SECRET_KEY,
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _sso_bindings_put(
    subjects,
    email: str,
    password: str,
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
    provider: Optional[str] = None,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
) -> int:
    subject_hashes = list(dict.fromkeys(
        value for value in (_sso_binding_subject_hash(subject) for subject in subjects) if value
    ))
    normalized_email = str(email or "").strip().lower()
    if not subject_hashes or not normalized_email or not password:
        raise ValueError("sso binding requires subjects, email and password")
    encrypted = encrypt_secret(str(password))
    if not encrypted:
        raise RuntimeError("failed to protect the mailbox password")
    now = _utc_now_iso()
    host_value = str(imap_host or "").strip() or None
    port_value = int(imap_port) if imap_port else None
    provider_value = str(provider or "").strip() or None
    smtp_host_value = str(smtp_host or "").strip() or None
    smtp_port_value = int(smtp_port) if smtp_port else None
    external = bool(host_value)
    if external and (not port_value or not provider_value):
        raise ValueError("external sso binding requires endpoint and provider")
    if smtp_host_value and not smtp_port_value:
        raise ValueError("smtp_host requires smtp_port")
    table = (
        "sso_external_mailbox_bindings"
        if external
        else "sso_mailbox_bindings"
    )
    # The submission endpoint only exists on the external table: a managed
    # mailbox always submits through the deployment's own SMTP.
    smtp_columns = ", smtp_host, smtp_port" if external else ""
    smtp_values = ", ?, ?" if external else ""
    smtp_updates = (
        ",\n                smtp_host = excluded.smtp_host,"
        "\n                smtp_port = excluded.smtp_port"
        if external
        else ""
    )
    smtp_row = (smtp_host_value, smtp_port_value) if external else ()
    with _db_connection() as con:
        con.executemany(
            f"""
            INSERT INTO {table} (
                subject_hash, email, imap_pass_enc, created_at, updated_at,
                imap_host, imap_port, provider{smtp_columns}
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?{smtp_values})
            ON CONFLICT(subject_hash) DO UPDATE SET
                email = excluded.email,
                imap_pass_enc = excluded.imap_pass_enc,
                updated_at = excluded.updated_at,
                imap_host = excluded.imap_host,
                imap_port = excluded.imap_port,
                provider = excluded.provider{smtp_updates}
            """,
            [
                (
                    subject_hash,
                    normalized_email,
                    encrypted,
                    now,
                    now,
                    host_value,
                    port_value,
                    provider_value,
                ) + smtp_row
                for subject_hash in subject_hashes
            ],
        )
    return len(subject_hashes)


def _sso_binding_put(
    subject: str,
    email: str,
    password: str,
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
    provider: Optional[str] = None,
    smtp_host: Optional[str] = None,
    smtp_port: Optional[int] = None,
) -> None:
    _sso_bindings_put(
        [subject],
        email,
        password,
        imap_host=imap_host,
        imap_port=imap_port,
        provider=provider,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )


def _sso_binding_drop(subject: str) -> int:
    subject_hash = _sso_binding_subject_hash(subject)
    if not subject_hash:
        return 0
    with _db_connection() as con:
        cur = con.execute(
            "DELETE FROM sso_mailbox_bindings WHERE subject_hash = ?",
            (subject_hash,),
        )
        return int(cur.rowcount or 0)


def _sso_binding_drop_requested(
    subject: str,
    imap_host: Optional[str] = None,
    provider: Optional[str] = None,
) -> int:
    provider_value = str(provider or "").strip().lower()
    if str(imap_host or "").strip() or provider_value:
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", provider_value):
            raise ValueError("external provider is required")
        return _sso_bindings_drop_external([subject], provider_value)
    return _sso_binding_drop(subject)


def _resolve_provider_hosts(
    provider: Optional[str],
    request_imap_host: Optional[str] = None,
    request_imap_port: Optional[int] = None,
) -> Tuple[Optional[str], Optional[int]]:
    """Return the IMAP host/port for a provider preset.

    Known providers use the fixed values from MAIL_PROVIDER_PRESETS.
    The "custom" provider uses the host/port supplied by the caller.
    Unknown providers or incomplete custom endpoints raise ValueError.
    """
    provider_value = str(provider or "").strip().lower()
    if not provider_value:
        # Managed mailbox — no per-row host/port.
        return None, None
    preset = MAIL_PROVIDER_PRESETS.get(provider_value)
    if not preset:
        raise ValueError(f"unknown provider: {provider_value}")
    if provider_value == "custom":
        host = str(request_imap_host or "").strip() or None
        port = int(request_imap_port) if request_imap_port else None
        if not host or not port:
            raise ValueError("custom provider requires imap_host and imap_port")
        return host, port
    return str(preset["imap_host"]), int(preset["imap_port"])


def _resolve_provider_smtp(
    provider: Optional[str],
    request_smtp_host: Optional[str] = None,
    request_smtp_port: Optional[int] = None,
) -> Tuple[Optional[str], Optional[int]]:
    """Return the submission host/port for a provider preset.

    Mirrors _resolve_provider_hosts. A custom provider may omit the submission
    endpoint: without it the deployment's own SMTP is used, which is the old
    behaviour and stays valid for a mailbox relayed locally.
    """
    provider_value = str(provider or "").strip().lower()
    if not provider_value:
        return None, None
    preset = MAIL_PROVIDER_PRESETS.get(provider_value)
    if not preset:
        raise ValueError(f"unknown provider: {provider_value}")
    if provider_value == "custom":
        host = str(request_smtp_host or "").strip() or None
        port = int(request_smtp_port) if request_smtp_port else None
        if host and not port:
            raise ValueError("custom smtp_host requires smtp_port")
        return (host, port) if host else (None, None)
    return str(preset["smtp_host"]), int(preset["smtp_port"])


def _sso_bindings_drop_external(subjects, provider: str) -> int:
    subject_hashes = list(dict.fromkeys(
        value for value in (_sso_binding_subject_hash(subject) for subject in subjects) if value
    ))
    provider_value = str(provider or "").strip().lower()
    if not subject_hashes or not provider_value:
        return 0
    placeholders = ",".join("?" for _value in subject_hashes)
    with _db_connection() as con:
        cur = con.execute(
            f"DELETE FROM sso_external_mailbox_bindings "
            f"WHERE subject_hash IN ({placeholders}) "
            "AND provider = ?",
            (*subject_hashes, provider_value),
        )
        return int(cur.rowcount or 0)


def _mailbox_external_row(email: str) -> Optional[Tuple[Any, ...]]:
    """Return the external binding row for this mailbox address, or None."""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    with _db_connection() as con:
        return con.execute(
            "SELECT imap_host, imap_port, smtp_host, smtp_port, provider "
            "FROM sso_external_mailbox_bindings WHERE email = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (normalized,),
        ).fetchone()


def _mailbox_imap_endpoint(email: str) -> Tuple[Optional[str], Optional[int]]:
    """Return the IMAP host/port bound to this mailbox address, or (None, None).

    Every IMAP caller routes through _imap_connect, so resolving the endpoint
    there is what keeps browsing, search and the sent-copy append pointed at the
    mailbox the user actually signed in with.
    """
    row = _mailbox_external_row(email)
    if not row:
        return None, None
    host = str(row[0] or "").strip() or None
    port = int(row[1]) if row[1] else None
    return (host, port) if host and port else (None, None)


def _mailbox_submission_endpoint(email: str) -> Tuple[Optional[str], Optional[int]]:
    """Return the SMTP host/port bound to this mailbox address, or (None, None).

    A managed mailbox has no external binding and submits through the
    deployment's own SMTP. An external mailbox (ADR-0071) must submit through
    its own provider: sending a Mail.ru address through the local relay is
    refused by every receiving side, so the endpoint has to follow the mailbox.

    Rows written before the smtp_* columns existed fall back to the provider
    preset, which is where the values came from in the first place.
    """
    row = _mailbox_external_row(email)
    if not row:
        return None, None
    host = str(row[2] or "").strip() or None
    port = int(row[3]) if row[3] else None
    if host and port:
        return host, port
    try:
        return _resolve_provider_smtp(str(row[4] or "") or None)
    except ValueError:
        return None, None


def _sso_binding_lookup(
    subject: str,
) -> Optional[Tuple[str, str, Optional[str], Optional[int]]]:
    """Return (email, password, imap_host, imap_port) bound to this subject, or None.

    imap_host/imap_port are None for managed @example.com mailboxes, which fall
    back to the global SpaceWeb config (ADR-0071).
    """
    subject_hash = _sso_binding_subject_hash(subject)
    if not subject_hash:
        return None
    with _db_connection() as con:
        rows = con.execute(
            "SELECT email, imap_pass_enc, imap_host, imap_port, 0 AS priority "
            "FROM sso_external_mailbox_bindings WHERE subject_hash = ? "
            "UNION ALL "
            "SELECT email, imap_pass_enc, imap_host, imap_port, 1 AS priority "
            "FROM sso_mailbox_bindings WHERE subject_hash = ? "
            "ORDER BY priority",
            (subject_hash, subject_hash),
        ).fetchall()
    for row in rows:
        password = decrypt_secret(str(row[1] or ""))
        if not password:
            continue
        imap_host = str(row[2] or "").strip() or None
        imap_port_raw = row[3]
        imap_port = int(imap_port_raw) if imap_port_raw else None
        return str(row[0] or ""), password, imap_host, imap_port
    return None


def _sso_bindings_status(subjects) -> dict:
    """Return one usable non-secret binding state, or flag split identities."""
    subject_hashes = list(dict.fromkeys(
        value for value in (_sso_binding_subject_hash(subject) for subject in subjects) if value
    ))
    empty = {
        "bound": False,
        "external": False,
        "provider": "",
        "email": "",
        "conflict": False,
    }
    if not subject_hashes:
        return empty
    placeholders = ",".join("?" for _value in subject_hashes)
    with _db_connection() as con:
        rows = con.execute(
            "SELECT subject_hash, email, imap_pass_enc, imap_host, imap_port, provider "
            f"FROM sso_external_mailbox_bindings "
            f"WHERE subject_hash IN ({placeholders})",
            subject_hashes,
        ).fetchall()
    if not rows:
        return empty
    states = []
    for row in rows:
        password = decrypt_secret(str(row[2] or ""))
        if not password:
            return {**empty, "conflict": True}
        states.append((
            str(row[1] or "").strip().lower(),
            password,
            str(row[3] or "").strip().lower(),
            int(row[4]) if row[4] else None,
            str(row[5] or "").strip().lower(),
        ))
    if len(rows) != len(subject_hashes) or len(set(states)) != 1:
        return {**empty, "conflict": True}
    email, _password, imap_host, _imap_port, provider = states[0]
    external = bool(imap_host)
    if external and not provider:
        return {**empty, "conflict": True}
    return {
        "bound": True,
        "external": external,
        "provider": provider,
        "email": email,
        "conflict": False,
    }


def _ticket_intake_put(email_address: str, password: str) -> None:
    normalized = str(email_address or "").strip().lower()
    if not normalized or not password:
        raise ValueError("ticket intake requires email and password")
    encrypted = encrypt_secret(str(password))
    now = _utc_now_iso()
    with _db_connection() as con:
        con.execute(
            """
            INSERT INTO ticket_intake_mailboxes (
                email, imap_pass_enc, last_uid, initialized, created_at, updated_at
            ) VALUES (?, ?, 0, 1, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                imap_pass_enc = excluded.imap_pass_enc,
                updated_at = excluded.updated_at
            """,
            (normalized, encrypted, now, now),
        )


def _ticket_intake_drop(email_address: str) -> int:
    normalized = str(email_address or "").strip().lower()
    with _db_connection() as con:
        con.execute("DELETE FROM sso_mailbox_bindings WHERE email = ?", (normalized,))
        cur = con.execute(
            "DELETE FROM ticket_intake_mailboxes WHERE email = ?",
            (normalized,),
        )
        return int(cur.rowcount or 0)


def _ticket_intake_rows() -> List[sqlite3.Row]:
    with _db_connection() as con:
        return con.execute(
            "SELECT email, imap_pass_enc, last_uid, initialized "
            "FROM ticket_intake_mailboxes ORDER BY email"
        ).fetchall()


def _ticket_intake_set_cursor(email_address: str, uid: int) -> None:
    with _db_connection() as con:
        con.execute(
            "UPDATE ticket_intake_mailboxes "
            "SET last_uid = ?, initialized = 1, updated_at = ? WHERE email = ?",
            (max(0, int(uid)), _utc_now_iso(), str(email_address).lower()),
        )


# ---------------------------------------------------------------------------
# IMAP helpers (synchronous; called via run_in_executor)
# ---------------------------------------------------------------------------

# Standard folder names mapping (server-specific names → human labels)
FOLDER_LABELS = {
    "INBOX": "Входящие",
    "Sent": "Отправленные",
    "Drafts": "Черновики",
    "Trash": "Корзина",
    "Junk": "Спам",
    "Archive": "Архив",
}
PROTECTED_FOLDERS = {name.casefold() for name in FOLDER_LABELS}


def _decode_mime(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _strip_angle(value: Optional[str]) -> str:
    return str(value or "").strip().strip("<>").strip()


def _parse_references_header(value: Optional[str]) -> List[str]:
    raw = str(value or "")
    if not raw:
        return []
    refs = []
    for item in re.findall(r"<([^>]+)>", raw):
        clean = _strip_angle(item)
        if clean:
            refs.append(clean)
    if refs:
        return refs
    parts = [_strip_angle(part) for part in raw.split() if _strip_angle(part)]
    return parts


def _normalize_thread_subject(subject: Optional[str]) -> str:
    value = str(subject or "").strip()
    if not value:
        return ""
    while True:
        next_value = re.sub(r"^\s*((re|fw|fwd|aw|sv)\s*:\s*)+", "", value, flags=re.IGNORECASE).strip()
        if next_value == value:
            break
        value = next_value
    return value.lower()


def _imap_connect(
    user: str,
    password: str,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> imaplib.IMAP4_SSL:
    # An explicit host/port overrides the global config. BYOM external
    # mailboxes (ADR-0071) are on different IMAP servers; managed mailboxes
    # resolve to nothing here and hit the default.
    if not (host or port):
        host, port = _mailbox_imap_endpoint(user)
    imap_host = str(host or "").strip() or CFG.IMAP_HOST
    imap_port = int(port) if port else CFG.IMAP_PORT
    ctx = ssl.create_default_context()
    if not (host or port):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    direct_error: Optional[Exception] = None
    try:
        conn = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=ctx, timeout=20)
        conn.login(user, password)
        return conn
    except (TimeoutError, OSError) as e:
        direct_error = e
    # The local-mailserver fallback only applies to the default host, never to a
    # BYOM override - a foreign mailbox is not reachable through docker exec.
    if host or port:
        if direct_error:
            raise direct_error
        raise OSError("IMAP connection failed")
    if not _local_mailserver_enabled():
        if direct_error:
            raise direct_error
        raise OSError("IMAP connection failed")
    container = str(getattr(CFG, "MAILSERVER_CONTAINER", "") or "").strip()
    if not container:
        if direct_error:
            raise direct_error
        raise OSError("IMAP connection failed")
    cmd = (
        "docker exec -i "
        + shlex.quote(container)
        + " sh -lc "
        + shlex.quote("exec timeout 25 openssl s_client -quiet -connect 127.0.0.1:993 2>/dev/null")
    )
    conn = imaplib.IMAP4_stream(cmd)
    conn.login(user, password)
    return conn


def _imap_validation_http_error(exc: Exception) -> HTTPException:
    if (
        isinstance(exc, imaplib.IMAP4.error)
        and not isinstance(exc, imaplib.IMAP4.abort)
        and _imap_error_is_auth_failure(exc)
    ):
        return HTTPException(status_code=422, detail="mailbox credentials rejected")
    return HTTPException(status_code=503, detail="mailbox service unavailable")


@contextmanager
def imap_session(user: str, password: str):
    conn = _imap_connect(user, password)
    try:
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _imap_utf7_encode(value: str) -> str:
    """Encode an IMAP mailbox name using modified UTF-7."""
    import base64

    output: List[str] = []
    pending: List[str] = []

    def flush() -> None:
        if not pending:
            return
        raw = "".join(pending).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        output.append(f"&{encoded}-")
        pending.clear()

    for char in value:
        codepoint = ord(char)
        if 0x20 <= codepoint <= 0x7E:
            flush()
            output.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(output)


def _imap_utf7_decode(value: str) -> str:
    """Decode an IMAP modified UTF-7 mailbox name."""
    import base64

    output: List[str] = []
    index = 0
    while index < len(value):
        if value[index] != "&":
            output.append(value[index])
            index += 1
            continue
        end = value.find("-", index)
        if end < 0:
            output.append(value[index:])
            break
        encoded = value[index + 1:end]
        if not encoded:
            output.append("&")
        else:
            try:
                raw = encoded.replace(",", "/")
                raw += "=" * ((4 - len(raw) % 4) % 4)
                output.append(base64.b64decode(raw).decode("utf-16-be"))
            except Exception:
                output.append(value[index:end + 1])
        index = end + 1
    return "".join(output)


def _normalize_folder_name(value: str) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())
    if not name:
        raise ValueError("Укажите название папки")
    if len(name) > 120:
        raise ValueError("Название папки слишком длинное")
    if any(char in name for char in ("\x00", "\r", "\n", '"', "\\")):
        raise ValueError("Название папки содержит недопустимые символы")
    if name.casefold() in PROTECTED_FOLDERS:
        raise ValueError("Системную папку нельзя изменить")
    return name


def _parse_folder_line(line: bytes) -> Optional[str]:
    # Format: (\HasNoChildren) "/" "INBOX"
    try:
        s = line.decode("utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r'"([^"]+)"\s*$', s)
    if m:
        return _imap_utf7_decode(m.group(1))
    parts = s.split(" ")
    return _imap_utf7_decode(parts[-1].strip('"'))


def _imap_list_folders_conn(conn: imaplib.IMAP4_SSL) -> List[Dict[str, Any]]:
    folders = []
    typ, data = conn.list()
    if typ != "OK":
        return []
    for raw in data:
        if not raw:
            continue
        name = _parse_folder_line(raw)
        if not name:
            continue
        unread = 0
        total = 0
        try:
            mailbox = _imap_utf7_encode(name)
            typ2, st = conn.status(f'"{mailbox}"', "(MESSAGES UNSEEN)")
            if typ2 == "OK" and st and st[0]:
                text = st[0].decode("utf-8", errors="replace")
                m_total = re.search(r"MESSAGES\s+(\d+)", text)
                m_unread = re.search(r"UNSEEN\s+(\d+)", text)
                total = int(m_total.group(1)) if m_total else 0
                unread = int(m_unread.group(1)) if m_unread else 0
        except Exception:
            pass
        folders.append({
            "name": name,
            "label": FOLDER_LABELS.get(name, name),
            "unread": unread,
            "total": total,
        })
    # Sort: known folders first
    order = list(FOLDER_LABELS.keys())
    folders.sort(key=lambda f: (order.index(f["name"]) if f["name"] in order else 999, f["name"]))
    return folders


def imap_list_folders(user: str, password: str) -> List[Dict[str, Any]]:
    with imap_session(user, password) as conn:
        return _imap_list_folders_conn(conn)


def imap_create_folder(user: str, password: str, folder: str) -> str:
    name = _normalize_folder_name(folder)
    mailbox = _imap_utf7_encode(name)
    with imap_session(user, password) as conn:
        typ, data = conn.create(f'"{mailbox}"')
        if typ != "OK":
            detail = data[0].decode("utf-8", errors="replace") if data else ""
            raise RuntimeError(detail or "Не удалось создать папку")
    return name


def imap_delete_folder(user: str, password: str, folder: str) -> str:
    name = _normalize_folder_name(folder)
    mailbox = _imap_utf7_encode(name)
    with imap_session(user, password) as conn:
        typ, data = conn.delete(f'"{mailbox}"')
        if typ != "OK":
            detail = data[0].decode("utf-8", errors="replace") if data else ""
            raise RuntimeError(detail or "Не удалось удалить папку")
    return name


def _strip_text_for_snippet(payload: bytes) -> str:
    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:
        text = str(payload)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


def _message_has_attachments(msg: Optional[email.message.Message]) -> bool:
    if msg is None:
        return False
    if msg.is_multipart():
        for part in msg.walk():
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                return True
            filename = part.get_filename()
            if filename:
                return True
    ctype = (msg.get("Content-Type") or "").lower()
    return "name=" in ctype or "multipart/mixed" in ctype


def _parse_message_summary(uid: str, flags: List[str], header_blob: bytes, body_blob: bytes) -> Dict[str, Any]:
    try:
        msg = BytesParser(policy=email.policy.default).parsebytes(header_blob)
    except Exception:
        msg = None

    from_addr = ""
    to_addr = ""
    subject = ""
    date_str = ""
    message_id = ""
    in_reply_to = ""
    references: List[str] = []
    if msg is not None:
        from_addr = _decode_mime(msg.get("From", ""))
        to_addr = _decode_mime(msg.get("To", ""))
        subject = _decode_mime(msg.get("Subject", ""))
        date_str = msg.get("Date", "")
        message_id = _strip_angle(msg.get("Message-ID", ""))
        in_reply_to = _strip_angle(msg.get("In-Reply-To", ""))
        references = _parse_references_header(msg.get("References", ""))

    snippet = _strip_text_for_snippet(body_blob) if body_blob else ""
    has_attachments = _message_has_attachments(msg)

    return {
        "uid": uid,
        "from": from_addr,
        "to": to_addr,
        "subject": subject,
        "subject_base": _normalize_thread_subject(subject),
        "date": date_str,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "flags": flags,
        "unread": "\\Seen" not in flags,
        "flagged": "\\Flagged" in flags,
        "has_attachments": has_attachments,
        "snippet": snippet,
    }


def _fetch_message_summaries(
    conn: imaplib.IMAP4_SSL,
    uids: List[str],
) -> List[Dict[str, Any]]:
    if not uids:
        return []
    uid_set = ",".join(uids)
    typ, fetched = conn.uid(
        "FETCH", uid_set, "(FLAGS RFC822.HEADER BODY.PEEK[TEXT]<0.300>)"
    )
    if typ != "OK":
        return []
    parsed: Dict[str, Dict[str, Any]] = {}
    i = 0
    while i < len(fetched):
        item = fetched[i]
        if isinstance(item, tuple) and len(item) >= 2:
            meta = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
            m_uid = re.search(r"UID\s+(\d+)", meta)
            m_flags = re.search(r"FLAGS\s+\(([^)]*)\)", meta)
            if not m_uid:
                i += 1
                continue
            uid = m_uid.group(1)
            flags = m_flags.group(1).split() if m_flags else []
            header_blob = item[1] if isinstance(item[1], (bytes, bytearray)) else b""
            body_blob = b""
            if i + 1 < len(fetched) and isinstance(fetched[i + 1], tuple):
                body_blob = fetched[i + 1][1] if isinstance(fetched[i + 1][1], (bytes, bytearray)) else b""
                i += 1
            parsed[uid] = _parse_message_summary(uid, flags, header_blob, body_blob)
        i += 1
    return [parsed[uid] for uid in uids if uid in parsed]


def _fetch_message_ids(conn: imaplib.IMAP4_SSL, uids: List[str]) -> List[str]:
    if not uids:
        return []
    uid_set = ",".join(str(uid) for uid in uids if str(uid).strip())
    if not uid_set:
        return []
    typ, fetched = conn.uid("FETCH", uid_set, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    if typ != "OK":
        return []
    found: Dict[str, str] = {}
    for item in fetched:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta = item[0].decode("utf-8", errors="replace") if isinstance(item[0], bytes) else str(item[0])
        match_uid = re.search(r"UID\s+(\d+)", meta)
        if not match_uid:
            continue
        uid = match_uid.group(1)
        try:
            parsed = BytesParser(policy=email.policy.default).parsebytes(item[1] or b"")
            message_id = str(parsed.get("Message-ID") or "").strip().strip("<>")
        except Exception:
            message_id = ""
        if message_id:
            found[uid] = message_id
    return [found[uid] for uid in uids if uid in found]


def _move_uids(conn: imaplib.IMAP4_SSL, uid_set: str, to_folder: str) -> None:
    try:
        typ2, _ = conn.uid("MOVE", uid_set, f'"{to_folder}"')
        if typ2 == "OK":
            return
        raise RuntimeError("MOVE failed")
    except Exception:
        conn.uid("COPY", uid_set, f'"{to_folder}"')
        conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
        try:
            conn.expunge()
        except Exception:
            pass


def _build_undo_payload(source_folder: str, target_folder: str, message_ids: List[str]) -> Optional[Dict[str, Any]]:
    ids = [str(mid or "").strip().strip("<>") for mid in message_ids if str(mid or "").strip()]
    if not ids:
        return None
    return {
        "from_folder": target_folder,
        "to_folder": source_folder,
        "message_ids": ids,
    }


def _parse_search_query(query: str) -> Dict[str, Any]:
    raw = (query or "").strip()
    try:
        tokens = shlex.split(raw) if raw else []
    except ValueError:
        tokens = raw.split()
    flag_tokens = {
        "unread", "read", "seen", "starred", "flagged",
        "unstarred", "unflagged", "has:attachment", "attachment",
        "attachments", "has:no-attachment", "without:attachment",
    }
    field_prefixes = ("from:", "to:", "subject:")
    parsed: Dict[str, Any] = {
        "text": [],
        "from": None,
        "to": None,
        "subject": None,
        "unread": None,
        "flagged": None,
        "has_attachments": None,
    }
    i = 0
    while i < len(tokens):
        token = tokens[i]
        lower = token.lower()
        if lower == "unread":
            parsed["unread"] = True
        elif lower in {"read", "seen"}:
            parsed["unread"] = False
        elif lower in {"starred", "flagged"}:
            parsed["flagged"] = True
        elif lower in {"unstarred", "unflagged"}:
            parsed["flagged"] = False
        elif lower in {"has:attachment", "attachment", "attachments"}:
            parsed["has_attachments"] = True
        elif lower in {"has:no-attachment", "without:attachment"}:
            parsed["has_attachments"] = False
        elif lower.startswith("from:") and lower != "from:":
            values = [token.split(":", 1)[1]]
            while i + 1 < len(tokens):
                nxt = tokens[i + 1]
                nxt_lower = nxt.lower()
                if nxt_lower in flag_tokens or nxt_lower.startswith(field_prefixes):
                    break
                values.append(nxt)
                i += 1
            parsed["from"] = " ".join(v for v in values if v).strip()
        elif lower.startswith("to:") and lower != "to:":
            values = [token.split(":", 1)[1]]
            while i + 1 < len(tokens):
                nxt = tokens[i + 1]
                nxt_lower = nxt.lower()
                if nxt_lower in flag_tokens or nxt_lower.startswith(field_prefixes):
                    break
                values.append(nxt)
                i += 1
            parsed["to"] = " ".join(v for v in values if v).strip()
        elif lower.startswith("subject:") and lower != "subject:":
            values = [token.split(":", 1)[1]]
            while i + 1 < len(tokens):
                nxt = tokens[i + 1]
                nxt_lower = nxt.lower()
                if nxt_lower in flag_tokens or nxt_lower.startswith(field_prefixes):
                    break
                values.append(nxt)
                i += 1
            parsed["subject"] = " ".join(v for v in values if v).strip()
        else:
            parsed["text"].append(token)
        i += 1
    parsed["text"] = " ".join(parsed["text"]).strip()
    return parsed


def _search_all_uids(conn: imaplib.IMAP4_SSL) -> List[str]:
    typ, data = conn.uid("SEARCH", None, "ALL")
    if typ != "OK":
        return []
    uids = data[0].split() if data and data[0] else []
    out = [u.decode() for u in uids]
    out.reverse()
    return out


def _summary_matches_query(summary: Dict[str, Any], parsed: Dict[str, Any]) -> bool:
    if parsed.get("unread") is True and not summary.get("unread"):
        return False
    if parsed.get("unread") is False and summary.get("unread"):
        return False
    if parsed.get("flagged") is True and not summary.get("flagged"):
        return False
    if parsed.get("flagged") is False and summary.get("flagged"):
        return False
    if parsed.get("has_attachments") is True and not summary.get("has_attachments"):
        return False
    if parsed.get("has_attachments") is False and summary.get("has_attachments"):
        return False
    for field in ("from", "to", "subject"):
        needle = str(parsed.get(field) or "").strip().lower()
        if needle and needle not in str(summary.get(field) or "").lower():
            return False
    text = str(parsed.get("text") or "").strip().lower()
    if text:
        hay = " ".join([
            str(summary.get("from") or ""),
            str(summary.get("to") or ""),
            str(summary.get("subject") or ""),
            str(summary.get("snippet") or ""),
        ]).lower()
        if text not in hay:
            return False
    return True


def _filter_uids_by_query_local(
    conn: imaplib.IMAP4_SSL,
    uids: List[str],
    parsed: Dict[str, Any],
) -> List[str]:
    matched: List[str] = []
    for idx in range(0, len(uids), 50):
        batch = uids[idx:idx + 50]
        for summary in _fetch_message_summaries(conn, batch):
            if _summary_matches_query(summary, parsed):
                matched.append(str(summary.get("uid")))
    return matched


def _imap_quote_arg(value: str) -> bytes:
    """Return a properly quoted IMAP search argument."""
    raw = str(value or "")
    escaped = raw.replace("\\", "\\\\").replace('"', r'\"')
    return f'"{escaped}"'.encode("utf-8")


def _build_imap_search_args(query: str = "") -> Tuple[List[Any], Dict[str, Any]]:
    parsed = _parse_search_query(query)
    args: List[Any] = []
    if parsed["unread"] is True:
        args.append("UNSEEN")
    elif parsed["unread"] is False:
        args.append("SEEN")
    if parsed["flagged"] is True:
        args.append("FLAGGED")
    elif parsed["flagged"] is False:
        args.append("UNFLAGGED")
    if parsed["from"]:
        args.extend(["FROM", _imap_quote_arg(str(parsed["from"]))])
    if parsed["to"]:
        args.extend(["TO", _imap_quote_arg(str(parsed["to"]))])
    if parsed["subject"]:
        args.extend(["SUBJECT", _imap_quote_arg(str(parsed["subject"]))])
    if parsed["text"]:
        q_bytes = _imap_quote_arg(str(parsed["text"]))
        args.extend(["OR", "OR", "OR", "SUBJECT", q_bytes, "FROM", q_bytes, "TO", q_bytes, "TEXT", q_bytes])
    return args, parsed


def _imap_message_list_conn(
    conn: imaplib.IMAP4_SSL,
    folder: str,
    page: int,
    per_page: int,
    filter_query: str = "",
) -> Dict[str, Any]:
    typ, _ = conn.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        raise RuntimeError(f"Cannot select folder {folder}")
    search_args, parsed_query = _build_imap_search_args(filter_query)
    used_local_filter = False
    if search_args:
        try:
            typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *search_args)
        except Exception:
            try:
                typ, data = conn.uid("SEARCH", *search_args)
            except Exception as e:
                log.warning("imap_message_list search fallback to local filter for %r: %s", filter_query, e)
                all_uids = _search_all_uids(conn)
                uids = _filter_uids_by_query_local(conn, all_uids, parsed_query)
                used_local_filter = True
    else:
        typ, data = conn.uid("SEARCH", None, "ALL")
    if not used_local_filter and typ != "OK":
        return {"messages": [], "total": 0, "page": page, "per_page": per_page}
    if not used_local_filter:
        uids = data[0].split() if data and data[0] else []
        uids = [u.decode() for u in uids]
        uids.reverse()  # newest first
    if parsed_query.get("has_attachments") is not None and uids and not used_local_filter:
        filtered: List[str] = []
        for idx in range(0, len(uids), 50):
            batch = uids[idx:idx + 50]
            summaries = _fetch_message_summaries(conn, batch)
            filtered.extend(
                s["uid"] for s in summaries
                if bool(s.get("has_attachments")) == bool(parsed_query["has_attachments"])
            )
        uids = filtered
    total = len(uids)
    start = (page - 1) * per_page
    slice_uids = uids[start:start + per_page]

    if not slice_uids:
        return {"messages": [], "total": total, "page": page, "per_page": per_page}
    messages = _fetch_message_summaries(conn, slice_uids)
    return {
        "messages": messages,
        "total": total,
        "page": page,
        "per_page": per_page,
        "query": filter_query,
    }


def imap_message_list(
    user: str,
    password: str,
    folder: str,
    page: int,
    per_page: int,
    filter_query: str = "",
) -> Dict[str, Any]:
    with imap_session(user, password) as conn:
        return _imap_message_list_conn(conn, folder, page, per_page, filter_query)


def imap_search_messages(
    user: str,
    password: str,
    folder: str,
    query: str,
    limit: int = 50,
) -> Dict[str, Any]:
    """Search messages in a folder using IMAP SEARCH.

    Searches across SUBJECT, FROM, TO, and BODY (TEXT) for the query string.
    Returns the same envelope shape as imap_message_list (newest first).
    """
    q = (query or "").strip()
    parsed = _parse_search_query(q)
    if not q and not any(parsed.get(key) is not None for key in ("unread", "flagged", "has_attachments")):
        return {"messages": [], "total": 0, "query": query}

    # IMAP SEARCH supports OR but only as binary (a | b). Multi-criteria
    # OR requires nesting: OR (OR (SUBJECT q) (FROM q)) (OR (TO q) (TEXT q))
    # We use IMAP SEARCH literals to allow non-ASCII characters via UTF-8.
    with imap_session(user, password) as conn:
        typ, _ = conn.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Cannot select folder {folder}")

        search_args, parsed = _build_imap_search_args(q)
        if not search_args:
            search_args = ["ALL"]
        used_local_filter = False
        try:
            typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *search_args)
        except Exception:
            try:
                typ, data = conn.uid("SEARCH", *search_args)
            except Exception as e:
                log.warning("imap_search_messages fallback to local filter for %r: %s", query, e)
                all_uids = _search_all_uids(conn)
                uids = _filter_uids_by_query_local(conn, all_uids, parsed)
                used_local_filter = True
        if not used_local_filter and typ != "OK":
            return {"messages": [], "total": 0, "query": query}

        if not used_local_filter:
            uids = data[0].split() if data and data[0] else []
            uids = [u.decode() for u in uids]
            uids.reverse()  # newest first
        if parsed.get("has_attachments") is not None and uids and not used_local_filter:
            filtered: List[str] = []
            for idx in range(0, len(uids), 50):
                batch = uids[idx:idx + 50]
                summaries = _fetch_message_summaries(conn, batch)
                filtered.extend(
                    s["uid"] for s in summaries
                    if bool(s.get("has_attachments")) == bool(parsed["has_attachments"])
                )
            uids = filtered
        total = len(uids)
        slice_uids = uids[:limit]

        if not slice_uids:
            return {"messages": [], "total": total, "query": query}
        messages = _fetch_message_summaries(conn, slice_uids)
        return {"messages": messages, "total": total, "query": query, "limit": limit}


_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_EVENT_RE = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]*)", re.IGNORECASE)
_JS_HREF_RE = re.compile(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", re.IGNORECASE)


def sanitize_html(content: str) -> str:
    if not content:
        return ""
    content = _SCRIPT_RE.sub("", content)
    content = _EVENT_RE.sub("", content)
    content = _JS_HREF_RE.sub(r'\1=\2#\2', content)
    return content


def _ical_unescape(value: str) -> str:
    return (
        str(value or "")
        .replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    ).strip()


def _ical_unfold_lines(text: str) -> List[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    for line in lines:
        if out and (line.startswith(" ") or line.startswith("\t")):
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _ical_parse_line(line: str) -> Tuple[str, Dict[str, str], str]:
    if ":" not in line:
        return line.upper(), {}, ""
    name_blob, value = line.split(":", 1)
    parts = name_blob.split(";")
    name = parts[0].upper()
    params: Dict[str, str] = {}
    for item in parts[1:]:
        if "=" not in item:
            continue
        key, param_value = item.split("=", 1)
        params[key.upper()] = param_value.strip().strip('"')
    return name, params, value


def _ical_parse_datetime(value: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    raw = str(value or "").strip()
    params = params or {}
    if not raw:
        return {"raw": "", "iso": "", "all_day": False, "tzid": params.get("TZID", "")}
    all_day = params.get("VALUE", "").upper() == "DATE" or bool(re.fullmatch(r"\d{8}", raw))
    tzid = params.get("TZID", "")
    if all_day:
        try:
            dt = datetime.strptime(raw[:8], "%Y%m%d")
            return {"raw": raw, "iso": dt.date().isoformat(), "all_day": True, "tzid": tzid}
        except Exception:
            return {"raw": raw, "iso": raw, "all_day": True, "tzid": tzid}
    for fmt, is_utc in (("%Y%m%dT%H%M%SZ", True), ("%Y%m%dT%H%M%S", False), ("%Y%m%dT%H%M", False)):
        try:
            dt = datetime.strptime(raw, fmt)
            if is_utc:
                dt = dt.replace(tzinfo=timezone.utc)
            return {"raw": raw, "iso": dt.isoformat(), "all_day": False, "tzid": tzid}
        except Exception:
            continue
    return {"raw": raw, "iso": raw, "all_day": False, "tzid": tzid}


def _ical_extract_identity(value: str, params: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    params = params or {}
    raw = str(value or "").strip()
    email_value = raw[7:] if raw.lower().startswith("mailto:") else raw
    return {
        "name": _ical_unescape(params.get("CN", "")),
        "email": email_value,
        "role": params.get("ROLE", ""),
        "partstat": params.get("PARTSTAT", ""),
    }


def _parse_calendar_invite(text: str) -> Optional[Dict[str, Any]]:
    if not text or "BEGIN:VEVENT" not in text.upper():
        return None
    calendar_props: Dict[str, str] = {}
    event_props: Dict[str, str] = {}
    event_params: Dict[str, Dict[str, str]] = {}
    attendees: List[Dict[str, str]] = []
    in_event = False
    for line in _ical_unfold_lines(text):
        clean = line.strip()
        upper = clean.upper()
        if upper == "BEGIN:VEVENT":
            in_event = True
            continue
        if upper == "END:VEVENT":
            break
        if not clean:
            continue
        name, params, value = _ical_parse_line(clean)
        if in_event:
            if name == "ATTENDEE":
                attendees.append(_ical_extract_identity(value, params))
                continue
            if name not in event_props:
                event_props[name] = value
                event_params[name] = params
        else:
            if name not in calendar_props:
                calendar_props[name] = value
    if not in_event and not event_props:
        return None
    start = _ical_parse_datetime(event_props.get("DTSTART", ""), event_params.get("DTSTART"))
    end = _ical_parse_datetime(event_props.get("DTEND", ""), event_params.get("DTEND"))
    invite = {
        "method": calendar_props.get("METHOD", "").upper(),
        "uid": event_props.get("UID", ""),
        "summary": _ical_unescape(event_props.get("SUMMARY", "")),
        "description": _ical_unescape(event_props.get("DESCRIPTION", "")),
        "location": _ical_unescape(event_props.get("LOCATION", "")),
        "status": event_props.get("STATUS", "").upper(),
        "sequence": event_props.get("SEQUENCE", ""),
        "organizer": _ical_extract_identity(event_props.get("ORGANIZER", ""), event_params.get("ORGANIZER")),
        "attendees": attendees,
        "start": start,
        "end": end,
    }
    if not any([invite["summary"], invite["uid"], start.get("raw"), invite["organizer"].get("email")]):
        return None
    return invite


def imap_fetch_message(
    user: str,
    password: str,
    folder: str,
    uid: str,
    mark_seen: bool = True,
) -> Dict[str, Any]:
    with imap_session(user, password) as conn:
        typ, _ = conn.select(f'"{folder}"', readonly=not mark_seen)
        if typ != "OK":
            raise RuntimeError(f"Cannot select folder {folder}")
        typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data or not data[0]:
            raise RuntimeError("Message not found")

        raw = None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                raw = item[1]
                break
        if not raw:
            raise RuntimeError("Empty message body")

        msg = BytesParser(policy=email.policy.default).parsebytes(raw)

        body_html = ""
        body_text = ""
        attachments: List[Dict[str, Any]] = []
        invite: Optional[Dict[str, Any]] = None

        if msg.is_multipart():
            att_index = 0
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = (part.get("Content-Disposition") or "").lower()
                filename = _decode_mime(part.get_filename() or "")
                is_calendar = ctype == "text/calendar" or filename.lower().endswith(".ics")
                if is_calendar:
                    try:
                        invite_payload = part.get_content()
                    except Exception:
                        invite_payload = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
                    parsed_invite = _parse_calendar_invite(str(invite_payload or ""))
                    if parsed_invite:
                        is_attachment_invite = "attachment" in disp or (bool(filename) and not disp)
                        parsed_invite["source"] = "attachment" if is_attachment_invite else "inline"
                        parsed_invite["filename"] = filename or "invite.ics"
                        parsed_invite["attachment_index"] = att_index if is_attachment_invite else None
                        if invite is None or (invite.get("source") != "attachment" and is_attachment_invite):
                            invite = parsed_invite
                if "attachment" in disp:
                    attachments.append({
                        "index": att_index,  # stable index for /api/messages/{uid}/attachment/{index}
                        "filename": filename or "attachment",
                        "content_type": ctype,
                        "size": len(part.get_payload(decode=True) or b""),
                    })
                    att_index += 1
                elif ctype == "text/html" and not body_html:
                    try:
                        body_html = part.get_content()
                    except Exception:
                        payload = part.get_payload(decode=True) or b""
                        body_html = payload.decode("utf-8", errors="replace")
                elif ctype == "text/plain" and not body_text:
                    try:
                        body_text = part.get_content()
                    except Exception:
                        payload = part.get_payload(decode=True) or b""
                        body_text = payload.decode("utf-8", errors="replace")
        else:
            ctype = msg.get_content_type()
            try:
                content = msg.get_content()
            except Exception:
                content = (msg.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
            if ctype == "text/html":
                body_html = content
            elif ctype == "text/calendar":
                invite = _parse_calendar_invite(str(content or ""))
            else:
                body_text = content

        if mark_seen:
            try:
                conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            except Exception:
                pass

        return {
            "uid": uid,
            "folder": folder,
            "from": _decode_mime(msg.get("From", "")),
            "to": _decode_mime(msg.get("To", "")),
            "cc": _decode_mime(msg.get("Cc", "")),
            "subject": _decode_mime(msg.get("Subject", "")),
            "subject_base": _normalize_thread_subject(_decode_mime(msg.get("Subject", ""))),
            "date": msg.get("Date", ""),
            "message_id": _strip_angle(msg.get("Message-ID", "")),
            "in_reply_to": _strip_angle(msg.get("In-Reply-To", "")),
            "references": _parse_references_header(msg.get("References", "")),
            "body_html": sanitize_html(body_html) if body_html else "",
            "body_text": body_text,
            "attachments": attachments,
            "invite": invite,
        }


def imap_fetch_attachment(
    user: str, password: str, folder: str, uid: str, att_index: int
) -> Optional[Dict[str, Any]]:
    """Fetch a single attachment by index. Returns dict with filename,
    content_type, and binary content; or None if not found."""
    with imap_session(user, password) as conn:
        typ, _ = conn.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Cannot select folder {folder}")
        typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data or not data[0]:
            return None
        raw = None
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2:
                raw = item[1]
                break
        if not raw:
            return None

        msg = BytesParser(policy=email.policy.default).parsebytes(raw)
        if not msg.is_multipart():
            return None

        i = 0
        for part in msg.walk():
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" not in disp:
                continue
            if i == att_index:
                payload = part.get_payload(decode=True) or b""
                return {
                    "filename": _decode_mime(part.get_filename() or f"attachment-{i}"),
                    "content_type": part.get_content_type() or "application/octet-stream",
                    "content": payload,
                }
            i += 1
        return None


def imap_delete_message(user: str, password: str, folder: str, uid: str) -> Dict[str, Any]:
    with imap_session(user, password) as conn:
        typ, _ = conn.select(f'"{folder}"', readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Cannot select folder {folder}")
        message_ids = _fetch_message_ids(conn, [uid])
        # Try to move to Trash, fall back to flag
        if folder.lower() != "trash":
            _move_uids(conn, uid, "Trash")
            return {"ok": True, "undo": _build_undo_payload(folder, "Trash", message_ids)}
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        try:
            conn.expunge()
        except Exception:
            pass
    return {"ok": True}


def imap_update_flags(
    user: str,
    password: str,
    folder: str,
    uid: str,
    seen: Optional[bool] = None,
    flagged: Optional[bool] = None,
) -> Dict[str, Any]:
    with imap_session(user, password) as conn:
        typ, _ = conn.select(f'"{folder}"', readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Cannot select folder {folder}")
        if seen is not None:
            conn.uid("STORE", uid, "+FLAGS" if seen else "-FLAGS", "(\\Seen)")
        if flagged is not None:
            conn.uid("STORE", uid, "+FLAGS" if flagged else "-FLAGS", "(\\Flagged)")
        summaries = _fetch_message_summaries(conn, [uid])
        if summaries:
            return summaries[0]
        raise RuntimeError("Message not found")


def imap_bulk_action(
    user: str,
    password: str,
    from_folder: str,
    uids: List[str],
    action: str,
    to_folder: Optional[str] = None,
) -> Dict[str, Any]:
    clean_uids = [str(uid) for uid in uids if str(uid).strip()]
    if not clean_uids:
        raise ValueError("uids is required")
    action = action.strip().lower()
    uid_set = ",".join(clean_uids)
    with imap_session(user, password) as conn:
        typ, _ = conn.select(f'"{from_folder}"', readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Cannot select folder {from_folder}")
        message_ids = _fetch_message_ids(conn, clean_uids)
        if action == "move":
            if not to_folder:
                raise ValueError("to_folder is required")
            _move_uids(conn, uid_set, to_folder)
            return {
                "ok": True,
                "count": len(clean_uids),
                "action": action,
                "undo": _build_undo_payload(from_folder, to_folder, message_ids),
            }
        elif action == "delete":
            if from_folder.lower() != "trash":
                _move_uids(conn, uid_set, "Trash")
                return {
                    "ok": True,
                    "count": len(clean_uids),
                    "action": action,
                    "undo": _build_undo_payload(from_folder, "Trash", message_ids),
                }
            conn.uid("STORE", uid_set, "+FLAGS", "(\\Deleted)")
            try:
                conn.expunge()
            except Exception:
                pass
        elif action == "mark_read":
            conn.uid("STORE", uid_set, "+FLAGS", "(\\Seen)")
        elif action == "mark_unread":
            conn.uid("STORE", uid_set, "-FLAGS", "(\\Seen)")
        elif action == "star":
            conn.uid("STORE", uid_set, "+FLAGS", "(\\Flagged)")
        elif action == "unstar":
            conn.uid("STORE", uid_set, "-FLAGS", "(\\Flagged)")
        else:
            raise ValueError("Unsupported bulk action")
    return {"ok": True, "count": len(clean_uids), "action": action}


def imap_move_message(user: str, password: str, from_folder: str, to_folder: str, uid: str) -> Dict[str, Any]:
    with imap_session(user, password) as conn:
        typ, _ = conn.select(f'"{from_folder}"', readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Cannot select folder {from_folder}")
        message_ids = _fetch_message_ids(conn, [uid])
        _move_uids(conn, uid, to_folder)
        return {"ok": True, "undo": _build_undo_payload(from_folder, to_folder, message_ids)}


def imap_undo_move(
    user: str,
    password: str,
    from_folder: str,
    to_folder: str,
    message_ids: List[str],
) -> Dict[str, Any]:
    clean_ids = [str(mid or "").strip().strip("<>") for mid in message_ids if str(mid or "").strip()]
    if not clean_ids:
        raise ValueError("message_ids is required")
    with imap_session(user, password) as conn:
        typ, _ = conn.select(f'"{from_folder}"', readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Cannot select folder {from_folder}")
        found_uids: List[str] = []
        for message_id in clean_ids:
            typ2, data = conn.uid("SEARCH", "HEADER", "Message-ID", _imap_quote_arg(f"<{message_id}>"))
            if typ2 != "OK" or not data or not data[0]:
                continue
            current_uids = [
                item.decode() if isinstance(item, (bytes, bytearray)) else str(item)
                for item in data[0].split()
            ]
            if current_uids:
                found_uids.append(current_uids[-1])
        if not found_uids:
            return {"ok": True, "count": 0}
        _move_uids(conn, ",".join(found_uids), to_folder)
    return {"ok": True, "count": len(found_uids)}


def imap_save_draft(
    user: str,
    password: str,
    from_addr: str,
    to: List[str],
    cc: List[str],
    bcc: List[str],
    subject: str,
    body_html: str,
    body_text: str,
    in_reply_to: Optional[str] = None,
    replace_uid: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Save a message to the IMAP Drafts folder via APPEND with \\Draft flag.

    If replace_uid is given, the existing draft with that UID is deleted after
    the new one is appended (atomic-ish replacement).
    Returns {uid, folder} of the new draft.
    """
    msg = _build_mail_message(
        from_addr,
        to,
        cc,
        bcc,
        subject,
        body_html,
        body_text,
        in_reply_to,
        attachments,
    )
    raw = msg.as_bytes()

    with imap_session(user, password) as conn:
        # Some servers require literal length APPEND syntax — imaplib handles it.
        typ, data = conn.append(
            '"Drafts"',
            r"(\Draft)",
            imaplib.Time2Internaldate(time.time()),
            raw,
        )
        if typ != "OK":
            raise RuntimeError(f"APPEND failed: {data}")

        # Extract UID from APPENDUID response if server supports UIDPLUS
        new_uid: Optional[str] = None
        if data and isinstance(data[0], (bytes, bytearray)):
            text = data[0].decode("utf-8", errors="replace")
            m = re.search(r"APPENDUID\s+\d+\s+(\d+)", text)
            if m:
                new_uid = m.group(1)

        # Fallback: search for the message-id we just used
        if not new_uid:
            try:
                typ2, _ = conn.select('"Drafts"', readonly=False)
                if typ2 == "OK":
                    msgid = msg["Message-ID"].strip("<>")
                    typ3, search_data = conn.uid(
                        "SEARCH", "HEADER", "Message-ID", f"<{msgid}>"
                    )
                    if typ3 == "OK" and search_data and search_data[0]:
                        ids = search_data[0].split()
                        if ids:
                            new_uid = ids[-1].decode()
            except Exception:
                pass

        # Replace old draft if requested (delete from Drafts)
        if replace_uid:
            try:
                conn.select('"Drafts"', readonly=False)
                conn.uid("STORE", replace_uid, "+FLAGS", "(\\Deleted)")
                conn.expunge()
            except Exception as e:
                log.info("draft replace: failed to delete old uid %s: %s",
                         replace_uid, e)

    return {"uid": new_uid or "", "folder": "Drafts"}


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------

def _imap_pick_sent_folder(conn: imaplib.IMAP4_SSL) -> str:
    folders = _imap_list_folders_conn(conn)
    names = [str(item.get("name") or "") for item in folders if item.get("name")]
    preferred = [
        "Sent",
        "INBOX.Sent",
        "Sent Items",
        "INBOX.Sent Items",
        "Отправленные",
    ]
    lowered = {name.lower(): name for name in names}
    for candidate in preferred:
        found = lowered.get(candidate.lower())
        if found:
            return found
    for name in names:
        probe = name.lower()
        if "sent" in probe or "отправ" in probe:
            return name
    return "Sent"


def _imap_save_sent_copy(user: str, password: str, raw: bytes) -> str:
    with imap_session(user, password) as conn:
        sent_folder = _imap_pick_sent_folder(conn)
        typ, data = conn.append(
            f'"{sent_folder}"',
            r"(\Seen)",
            imaplib.Time2Internaldate(time.time()),
            raw,
        )
        if typ != "OK":
            raise RuntimeError(f"Sent APPEND failed: {data}")
        return sent_folder

def smtp_send(
    user: str,
    password: str,
    from_addr: str,
    to: List[str],
    cc: List[str],
    bcc: List[str],
    subject: str,
    body_html: str,
    body_text: str,
    in_reply_to: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    msg = _build_mail_message(
        from_addr,
        to,
        cc,
        bcc,
        subject,
        body_html,
        body_text,
        in_reply_to,
        attachments,
    )

    if "Bcc" in msg:
        del msg["Bcc"]

    recipients = [r for r in (to + cc + bcc) if r]
    raw = msg.as_bytes()

    # An external mailbox submits through its own provider; a managed one keeps
    # the deployment's SMTP.
    bound_host, bound_port = _mailbox_submission_endpoint(user)
    smtp_host = bound_host or CFG.SMTP_HOST
    smtp_port = bound_port or CFG.SMTP_PORT
    external = bool(bound_host)

    # Certificates are verified by default. Only a deployment whose own
    # submission host is a local relay with a self-signed certificate may opt
    # out — an unverified TLS session to a remote provider would expose the
    # mailbox password this call is about to send, so the opt-out never applies
    # to an external endpoint.
    ctx = ssl.create_default_context()
    if not CFG.SMTP_VERIFY_TLS and not external:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    # Port 465 is implicit TLS, everything else is STARTTLS on a plain session.
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=20) as s:
            s.login(user, password)
            s.send_message(msg, from_addr=from_addr, to_addrs=recipients)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(user, password)
            s.send_message(msg, from_addr=from_addr, to_addrs=recipients)
    result = {
        "smtp_ok": True,
        "smtp_status": "submitted",
        "recipient_count": len(recipients),
        "sent_copy_ok": False,
        "sent_folder": "",
        "sent_copy_status": "",
    }
    try:
        sent_folder = _imap_save_sent_copy(user, password, raw)
        result["sent_copy_ok"] = True
        result["sent_folder"] = sent_folder
        result["sent_copy_status"] = "saved"
    except Exception as exc:
        result["sent_copy_status"] = str(exc)
    return result


def _insert_scheduled_send(
    *,
    imap_user: str,
    imap_pass: str,
    from_addr: str,
    to: List[str],
    cc: List[str],
    bcc: List[str],
    subject: str,
    body_html: str,
    body_text: str,
    in_reply_to: Optional[str],
    attachments: Optional[List[Dict[str, Any]]],
    scheduled_at: str,
    status: str = "pending",
    origin: str = "scheduled",
    last_error: Optional[str] = None,
) -> int:
    imap_pass_enc = encrypt_secret(imap_pass)
    if not imap_pass_enc:
        raise RuntimeError("Failed to encrypt credentials")
    with _db_connection() as con:
        cur = con.execute(
            """
            INSERT INTO scheduled_sends
                (imap_user, from_addr, to_list, cc_list, bcc_list, subject,
                 body_html, body_text, in_reply_to, attachments_json,
                 imap_pass_enc, scheduled_at, status, last_error, origin)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                imap_user,
                from_addr,
                json.dumps(to, ensure_ascii=False),
                json.dumps(cc, ensure_ascii=False),
                json.dumps(bcc, ensure_ascii=False),
                subject,
                body_html,
                body_text,
                in_reply_to,
                json.dumps(attachments or [], ensure_ascii=False),
                imap_pass_enc,
                scheduled_at,
                status,
                (last_error or "")[:500] if last_error else None,
                origin,
            ),
        )
        con.commit()
        return int(cur.lastrowid or 0)


def _queue_failed_immediate_send(
    *,
    sess: Dict[str, Any],
    from_addr: str,
    to: List[str],
    cc: List[str],
    bcc: List[str],
    subject: str,
    body_html: str,
    body_text: str,
    in_reply_to: Optional[str],
    attachments: Optional[List[Dict[str, Any]]],
    error_text: str,
) -> int:
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return _insert_scheduled_send(
        imap_user=sess["imap_user"],
        imap_pass=sess["imap_pass"],
        from_addr=from_addr,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        in_reply_to=in_reply_to,
        attachments=attachments,
        scheduled_at=now_iso,
        status="failed",
        origin="immediate",
        last_error=error_text,
    )


def _scheduled_send_item(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "from": row["from_addr"],
        "to": json.loads(row["to_list"] or "[]"),
        "cc": json.loads(row["cc_list"] or "[]") if row["cc_list"] else [],
        "bcc": json.loads(row["bcc_list"] or "[]") if row["bcc_list"] else [],
        "subject": row["subject"],
        "scheduled_at": row["scheduled_at"],
        "status": row["status"],
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "sent_at": row["sent_at"],
        "origin": row["origin"] if "origin" in row.keys() else "scheduled",
    }


def _deliver_scheduled_send_row(
    row_id: int,
    *,
    allowed_statuses: Tuple[str, ...] = ("pending",),
    user_override: Optional[str] = None,
    password_override: Optional[str] = None,
) -> Dict[str, Any]:
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM scheduled_sends WHERE id=?",
            (row_id,),
        ).fetchone()
    if not row:
        raise RuntimeError("Queue item not found")
    if row["status"] not in allowed_statuses:
        raise RuntimeError(f"Queue item status={row['status']} is not retryable")
    if user_override and row["imap_user"] != user_override:
        raise RuntimeError("Queue item belongs to another user")
    with _db_connection() as con:
        cur = con.execute(
            "UPDATE scheduled_sends SET status='sending', attempts=attempts+1 "
            "WHERE id=? AND status=?",
            (row_id, row["status"]),
        )
        con.commit()
    if cur.rowcount == 0:
        raise RuntimeError("Queue item is already being processed")
    try:
        imap_pass = password_override or decrypt_secret(row["imap_pass_enc"])
        if not imap_pass:
            raise RuntimeError("Could not decrypt stored credentials")
        to_list = json.loads(row["to_list"] or "[]")
        cc_list = json.loads(row["cc_list"] or "[]") if row["cc_list"] else []
        bcc_list = json.loads(row["bcc_list"] or "[]") if row["bcc_list"] else []
        attachments = json.loads(row["attachments_json"] or "[]") if row["attachments_json"] else None
        send_result = smtp_send(
            row["imap_user"],
            imap_pass,
            row["from_addr"],
            to_list,
            cc_list,
            bcc_list,
            row["subject"] or "",
            row["body_html"] or "",
            row["body_text"] or "",
            row["in_reply_to"],
            attachments,
        )
    except Exception as e:
        err = str(e)[:500]
        with _db_connection() as con:
            con.execute(
                "UPDATE scheduled_sends SET status='failed', last_error=? WHERE id=?",
                (err, row_id),
            )
            con.commit()
        raise RuntimeError(err)
    with _db_connection() as con:
        con.execute(
            "UPDATE scheduled_sends SET status='sent', sent_at=?, "
            "imap_pass_enc=?, last_error=NULL WHERE id=?",
            (
                now_iso,
                encrypt_secret(imap_pass) or "",
                row_id,
            ),
        )
        con.commit()
    return send_result


# ---------------------------------------------------------------------------
# AI helpers (Smart Reply, Inbox Triage) — via proxy-panel Ollama proxy
# ---------------------------------------------------------------------------

AI_MODEL_DEFAULT = os.environ.get("WEBMAIL_AI_MODEL", "qwen2.5:7b")
AI_KEEP_ALIVE = os.environ.get("WEBMAIL_AI_KEEP_ALIVE", "30m")
AI_MAX_BODY_CHARS = 4000  # truncate email bodies to keep prompts short
AI_TRIAGE_TIMEOUT = min(
    15,
    max(1, int(os.environ.get("WEBMAIL_AI_TRIAGE_TIMEOUT", "15"))),
)
TRIAGE_CLASSIFY_ATTEMPTS_PER_REQUEST = 1


def _ai_generate(
    prompt: str,
    *,
    model: str = AI_MODEL_DEFAULT,
    format_json: bool = False,
    system: Optional[str] = None,
    timeout: int = 60,
) -> Optional[str]:
    """Call proxy panel AI proxy → Ollama. Returns raw response text or None."""
    if not CFG.INTERNAL_TOKEN:
        log.debug("_ai_generate: RUPOCHTA_INTERNAL_TOKEN not set, skipping")
        return None
    url = CFG.PROXY_PANEL_URL.rstrip("/") + "/api/ai/generate"
    body: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "keep_alive": AI_KEEP_ALIVE,
    }
    if format_json:
        body["format"] = "json"
    if system:
        body["system"] = system
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Internal-Token", CFG.INTERNAL_TOKEN)
    req.add_header("X-Internal-Service", "rupochta")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.warning("_ai_generate HTTP %s: %s", e.code, e.read()[:200] if hasattr(e, 'read') else '')
        return None
    except Exception as e:
        log.warning("_ai_generate error: %s", e)
        return None
    if not payload.get("ok"):
        log.warning("_ai_generate: panel not-ok: %s", payload)
        return None
    return (payload.get("data") or {}).get("response") or ""


def _ai_strip_html(text: str) -> str:
    """Basic HTML → plain text for AI prompts."""
    if not text:
        return ""
    # Drop scripts/styles/quoted blocks
    text = re.sub(r"<(script|style|blockquote)[^>]*>.*?</\1>",
                  " ", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert common block tags to newlines
    text = re.sub(r"<(br|/p|/div|/tr|/li)\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace but keep newlines
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return html.unescape(text).strip()


def _ai_extract_body(message: Dict[str, Any]) -> str:
    """Pull plain-text body from a fetched message dict."""
    body = str(message.get("body_text") or "").strip()
    if not body:
        body = _ai_strip_html(str(message.get("body_html") or ""))
    # Trim quoted replies (>>, "On ... wrote:")
    lines = body.splitlines()
    cut = len(lines)
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(">") or re.match(r"^(On .+wrote:|\d+.+(написал|wrote):)$", s):
            cut = i
            break
    body = "\n".join(lines[:cut]).strip()
    if len(body) > AI_MAX_BODY_CHARS:
        body = body[:AI_MAX_BODY_CHARS] + "…[обрезано]"
    return body


_AI_SMART_REPLY_SYSTEM = (
    "Ты — ассистент корпоративной почты. "
    "Даёшь краткие, вежливые, уместные варианты ответов на деловые письма. "
    "Всегда отвечаешь строго в формате JSON."
)

_AI_SMART_REPLY_PROMPT = """На основе письма ниже сгенерируй 3 КОРОТКИХ варианта ответа (1-3 предложения каждый) в разных тонах.
Тоны: "positive" (согласие/подтверждение), "neutral" (уточняющий вопрос), "decline" (вежливый отказ/невозможность).
Пиши на русском, обращайся на "вы", подпись не добавляй.

ОТ: {sender}
ТЕМА: {subject}
ТЕКСТ:
{body}

Верни СТРОГО JSON-объект без пояснений:
{{"suggestions": [
  {{"tone": "positive", "label": "Согласие", "text": "..."}},
  {{"tone": "neutral",  "label": "Уточнение", "text": "..."}},
  {{"tone": "decline",  "label": "Отказ", "text": "..."}}
]}}
"""


def ai_smart_reply_suggestions(message: Dict[str, Any]) -> List[Dict[str, str]]:
    """Generate 3 suggested replies for a message. Returns a list (possibly empty)."""
    body = _ai_extract_body(message)
    if not body:
        return []
    sender = str(message.get("from") or "").strip() or "неизвестный отправитель"
    subject = str(message.get("subject") or "").strip() or "(без темы)"
    prompt = _AI_SMART_REPLY_PROMPT.format(
        sender=sender, subject=subject, body=body
    )
    raw = _ai_generate(
        prompt,
        system=_AI_SMART_REPLY_SYSTEM,
        format_json=True,
        timeout=90,
    )
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        # Try to extract first JSON object from text
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            return []
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return []
    suggestions = parsed.get("suggestions") or []
    out: List[Dict[str, str]] = []
    for s in suggestions[:3]:
        if not isinstance(s, dict):
            continue
        text = str(s.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "tone": str(s.get("tone") or "neutral").lower()[:20],
            "label": str(s.get("label") or "Вариант").strip()[:40],
            "text": text[:2000],
        })
    return out


_AI_TRIAGE_SYSTEM = (
    "Ты — классификатор корпоративной почты. Отвечаешь строго в формате JSON "
    "на русском языке."
)

_AI_TRIAGE_PROMPT = """Отнеси письмо к ОДНОЙ из категорий:
- "important" — важное для работы (запросы, счета, договоры, инциденты, прямые обращения от коллег/начальства, дедлайны)
- "updates"  — уведомления от систем (CI/CD, мониторинг, коммиты, задачи в таск-трекере, автоматические отчёты)
- "newsletters" — подписки, рассылки отраслевых новостей, дайджесты
- "promo" — рекламные/маркетинговые письма

ОТ: {sender}
ТЕМА: {subject}
ТЕКСТ:
{body}

Верни СТРОГО JSON без пояснений:
{{"category": "important|updates|newsletters|promo", "confidence": 0-100, "reason": "короткое пояснение"}}
"""


def ai_triage_categorize(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Classify a message into one of 4 categories. Returns dict or None on failure."""
    body = _ai_extract_body(message)
    sender = str(message.get("from") or "").strip() or "неизвестный отправитель"
    subject = str(message.get("subject") or "").strip() or "(без темы)"
    prompt = _AI_TRIAGE_PROMPT.format(
        sender=sender, subject=subject, body=body[:2000] or "(пусто)"
    )
    raw = _ai_generate(
        prompt,
        system=_AI_TRIAGE_SYSTEM,
        format_json=True,
        timeout=AI_TRIAGE_TIMEOUT,
    )
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return None
    category = str(parsed.get("category") or "").strip().lower()
    if category not in ("important", "updates", "newsletters", "promo"):
        return None
    try:
        confidence = int(parsed.get("confidence") or 0)
    except Exception:
        confidence = 0
    return {
        "category": category,
        "confidence": max(0, min(100, confidence)),
        "reason": str(parsed.get("reason") or "").strip()[:300],
    }


# ---------------------------------------------------------------------------
# LDAP contacts
# ---------------------------------------------------------------------------

_LDAP_CONTACT_ATTRS = [
    "displayName", "mail", "sAMAccountName", "department",
    "telephoneNumber", "mobile", "title", "company",
    "givenName", "sn", "userAccountControl",
]


def _ldap_dn_common_name(group_dn: str) -> str:
    match = re.match(r"^\s*CN=((?:\\.|[^,])+)", str(group_dn or ""), re.IGNORECASE)
    if not match:
        return str(group_dn or "").strip()
    return match.group(1).replace("\\,", ",").replace("\\\\", "\\").strip()


def _ldap_attr_value(entry: Any, name: str, default: Any = "") -> Any:
    if isinstance(entry, dict):
        return entry.get(name, default)
    try:
        if name not in entry:
            return default
        attribute = getattr(entry, name)
        return getattr(attribute, "value", attribute)
    except Exception:
        return default


def _ldap_attr_values(entry: Any, name: str) -> List[str]:
    value = _ldap_attr_value(entry, name, [])
    if value in (None, "", "[]"):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _ldap_group_public(entry: Any) -> Dict[str, Any]:
    members = _ldap_attr_values(entry, "member")
    return {
        "dn": str(_ldap_attr_value(entry, "distinguishedName", "") or "").strip(),
        "name": str(_ldap_attr_value(entry, "cn", "") or "").strip(),
        "description": str(_ldap_attr_value(entry, "description", "") or "").strip(),
        "mail": str(_ldap_attr_value(entry, "mail", "") or "").strip().lower(),
        "members_count": len(members),
    }


def _ldap_entry_to_contact(entry, include_photo: bool = False) -> Dict[str, Any]:
    """Convert an ldap3 entry into a contact dict."""
    login = str(entry.sAMAccountName) if "sAMAccountName" in entry else ""
    if "mail" in entry and str(entry.mail) not in ("", "[]"):
        email_addr = str(entry.mail)
    elif login:
        email_addr = f"{login}@{CFG.MAIL_DOMAIN}"
    else:
        email_addr = ""
    # Derive display name: prefer displayName, then givenName+sn, then login
    if "displayName" in entry and str(entry.displayName) not in ("", "[]"):
        name = str(entry.displayName)
    elif ("givenName" in entry or "sn" in entry):
        gn = str(entry.givenName) if "givenName" in entry else ""
        sn = str(entry.sn) if "sn" in entry else ""
        name = f"{gn} {sn}".strip() or login
    else:
        name = login
    # Account disabled check (bit 2 of userAccountControl = ACCOUNTDISABLE)
    disabled = False
    if "userAccountControl" in entry:
        try:
            disabled = bool(int(str(entry.userAccountControl)) & 0x0002)
        except Exception:
            pass
    contact = {
        "name": name,
        "email": email_addr,
        "login": login,
        "department": str(entry.department) if "department" in entry else "",
        "phone": str(entry.telephoneNumber) if "telephoneNumber" in entry else "",
        "mobile": str(entry.mobile) if "mobile" in entry else "",
        "title": str(entry.title) if "title" in entry else "",
        "company": str(entry.company) if "company" in entry else "",
        "disabled": disabled,
    }
    if include_photo and "thumbnailPhoto" in entry:
        try:
            raw = entry.thumbnailPhoto.value
            if isinstance(raw, bytes) and raw:
                import base64 as _b64
                contact["photo"] = "data:image/jpeg;base64," + _b64.b64encode(raw).decode("ascii")
        except Exception:
            pass
    return contact


def _ldap_bind_and_search(
    flt: str,
    attrs: List[str],
    limit: int = 0,
    *,
    raise_on_failure: bool = False,
) -> List[Any]:
    """Bind to LDAP (iterating over servers) and return matching entries."""
    if not _ldap_bind_available():
        if raise_on_failure:
            raise RuntimeError("LDAPS service bind is not configured")
        return []
    last_error: Optional[Exception] = None
    for srv_url in CFG.LDAP_SERVERS:
        try:
            use_ssl = srv_url.startswith("ldaps://")
            host_part = srv_url.split("://", 1)[1]
            host, _, port = host_part.partition(":")
            srv = Server(host, port=int(port) if port else (636 if use_ssl else 389),
                         use_ssl=use_ssl, get_info=ALL)
            conn = Connection(
                srv,
                user=CFG.LDAP_BIND_USER,
                password=CFG.LDAP_BIND_PASS,
                authentication=SIMPLE,
                auto_bind=True,
                receive_timeout=10,
            )
            conn.search(
                search_base=CFG.LDAP_BASE_DN,
                search_filter=flt,
                search_scope=SUBTREE,
                attributes=attrs,
                size_limit=limit,
                paged_size=500,
            )
            entries = list(conn.entries)
            conn.unbind()
            return entries
        except LDAPException as e:
            last_error = e
            log.warning("LDAP server %s failed: %s", srv_url, e)
            continue
        except Exception as e:
            last_error = e
            log.warning("LDAP unexpected error on %s: %s", srv_url, e)
            continue
    if raise_on_failure:
        raise RuntimeError("All configured LDAPS servers are unavailable") from last_error
    return []


def ldap_search_contacts(query: str, limit: int = 50) -> List[Dict[str, Any]]:
    q = query.strip()
    safe = re.sub(r"[()\\*\x00]", "", q)
    if not safe:
        flt = "(&(objectClass=user)(objectCategory=person)(sAMAccountName=*))"
    else:
        flt = (
            f"(&(objectClass=user)(objectCategory=person)"
            f"(|(sAMAccountName=*{safe}*)(displayName=*{safe}*)"
            f"(mail=*{safe}*)(cn=*{safe}*)(sn=*{safe}*)(givenName=*{safe}*)))"
        )
    entries = _ldap_bind_and_search(flt, _LDAP_CONTACT_ATTRS, limit=limit)
    return [_ldap_entry_to_contact(e) for e in entries]


def _mail_session_contact(sess: Dict[str, Any]) -> Dict[str, Any]:
    """Return the current RuPochta mailbox as a contact without external HR/AD data."""
    email_addr = str(
        sess.get("from_address") or sess.get("email") or ""
    ).strip().lower()
    mail_login = str(
        sess.get("mail_login") or email_addr.split("@", 1)[0]
    ).strip().lower()
    return {
        "name": str(sess.get("display_name") or mail_login or email_addr).strip(),
        "email": email_addr,
        "login": mail_login,
        "mail_login": mail_login,
        "emp_id": None,
        "department": "",
        "phone": "",
        "mobile": "",
        "title": "",
        "company": "RuPochta",
        "directory_domain": "",
        "groups": [],
        "group_dns": [],
        "avatar_data_url": "",
        "disabled": False,
        "source": "rupochta",
    }


def _search_directory_contacts(
    query: str,
    limit: int = 25,
    contacts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Search an explicitly supplied RuPochta contact set."""
    q = query.strip().lower()
    rows = [row for row in (contacts or []) if row.get("email")]
    if not q:
        return rows[:limit]

    def _rank(row: Dict[str, Any]) -> int:
        email = str(row.get("email") or "").strip().lower()
        login = str(row.get("login") or row.get("mail_login") or "").strip().lower()
        name = str(row.get("name") or "").strip().lower()
        if email == q or login == q:
            return 100
        if email.startswith(q) or login.startswith(q):
            return 80
        if name.startswith(q):
            return 60
        return 40

    scored: List[tuple] = []
    for row in rows:
        hay = " ".join(
            str(row.get(f) or "")
            for f in ("name", "email", "login", "mail_login",
                      "department", "title", "company")
        ).lower()
        if q not in hay:
            continue
        scored.append((_rank(row), row))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("name") or "")))
    return [row for _, row in scored[:limit]]


# Cached full directory dump. Loading all AD users is expensive (LDAP paging)
# so we keep a 5-minute memoisation. Refreshes on demand via /api/directory?refresh=1.
_directory_cache: Dict[str, Any] = {"ts": 0.0, "rows": []}
_directory_lock = threading.Lock()
DIRECTORY_TTL = 300  # seconds
_portal_directory_cache: Dict[str, Any] = {"ts": 0.0, "rows": [], "groups": []}
_portal_directory_lock = threading.Lock()
_directory_photo_cache: Dict[str, Dict[str, Any]] = {}
_directory_photo_lock = threading.Lock()
DIRECTORY_PHOTO_TTL = 3600
_directory_photo_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ldap-photo")
_mailadmin_read_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mailadmin-read")
_mailserver_stats_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mailserver-stats")
_ldap_group_cache: Dict[str, Any] = {"ts": 0.0, "rows": []}
_ldap_group_lock = threading.Lock()
_ldap_user_group_cache: Dict[str, Dict[str, Any]] = {}
_ldap_user_group_lock = threading.Lock()
LDAP_GROUP_TTL = 300
LDAP_USER_GROUP_TTL = 60


def ldap_load_groups(force: bool = False) -> List[Dict[str, Any]]:
    with _ldap_group_lock:
        now = time.time()
        if (
            not force
            and _ldap_group_cache["rows"]
            and now - float(_ldap_group_cache["ts"] or 0) < LDAP_GROUP_TTL
        ):
            return deepcopy(_ldap_group_cache["rows"])
        entries = _ldap_bind_and_search(
            "(&(objectClass=group)(cn=*)(distinguishedName=*))",
            ["cn", "distinguishedName", "description", "mail", "member"],
            limit=0,
            raise_on_failure=True,
        )
        rows = [
            _ldap_group_public(entry)
            for entry in entries
        ]
        rows = [row for row in rows if row["dn"] and row["name"]]
        deduplicated = {
            row["dn"].casefold(): row
            for row in rows
        }
        result = sorted(
            deduplicated.values(),
            key=lambda row: (row["name"].casefold(), row["dn"].casefold()),
        )
        _ldap_group_cache["rows"] = result
        _ldap_group_cache["ts"] = now
        log.info("LDAP group catalog refreshed: %d groups", len(result))
        return deepcopy(result)


def ldap_get_user_group_dns(login: str) -> List[str]:
    clean_login = str(login or "").strip().lower().split("@", 1)[0]
    safe_login = re.sub(r"[()\\*\x00]", "", clean_login)
    if not safe_login:
        return []
    with _ldap_user_group_lock:
        cached = _ldap_user_group_cache.get(safe_login)
        if cached and time.time() - float(cached.get("ts") or 0) < LDAP_USER_GROUP_TTL:
            return list(cached.get("groups") or [])
    entries = _ldap_bind_and_search(
        f"(&(objectClass=user)(sAMAccountName={safe_login}))",
        ["memberOf"],
        limit=1,
        raise_on_failure=True,
    )
    groups = _ldap_attr_values(entries[0], "memberOf") if entries else []
    with _ldap_user_group_lock:
        _ldap_user_group_cache[safe_login] = {
            "ts": time.time(),
            "groups": list(groups),
        }
    return groups


def db_list_mailadmin_admin_groups() -> List[Dict[str, str]]:
    try:
        with _db_connection() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT group_dn, group_name
                FROM mailadmin_admin_groups
                ORDER BY group_name COLLATE NOCASE, group_dn COLLATE NOCASE
                """
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if rows:
        return [
            {"dn": str(row["group_dn"]), "name": str(row["group_name"])}
            for row in rows
        ]
    return [
        {"dn": group_dn, "name": _ldap_dn_common_name(group_dn)}
        for group_dn in DEFAULT_ADMIN_LDAP_GROUPS
    ]


def db_replace_mailadmin_admin_groups(groups: List[Dict[str, Any]]) -> None:
    with _db_connection() as con:
        con.execute("DELETE FROM mailadmin_admin_groups")
        for group in groups:
            con.execute(
                """
                INSERT INTO mailadmin_admin_groups (
                    group_dn, group_name, updated_at
                )
                VALUES (?, ?, datetime('now'))
                """,
                (str(group["dn"]), str(group["name"])),
            )
        con.commit()


def _admin_group_claims_allowed(
    claims: List[Any],
    policy: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    allowed = policy if policy is not None else db_list_mailadmin_admin_groups()
    allowed_dns = {
        str(group.get("dn") or "").strip().casefold()
        for group in allowed
        if str(group.get("dn") or "").strip()
    }
    allowed_names = {
        str(group.get("name") or _ldap_dn_common_name(group.get("dn") or ""))
        .strip()
        .casefold()
        for group in allowed
        if str(group.get("name") or group.get("dn") or "").strip()
    }
    for claim in claims:
        text = str(claim or "").strip()
        if not text:
            continue
        normalized = text.casefold()
        if "=" in text:
            if normalized in allowed_dns:
                return True
            continue
        if normalized in allowed_names:
            return True
    return False


def _validate_admin_group_selection(group_dns: List[str]) -> List[Dict[str, Any]]:
    selected = [
        str(group_dn or "").strip()
        for group_dn in group_dns
        if str(group_dn or "").strip()
    ]
    if not selected:
        raise HTTPException(400, "Выберите хотя бы одну группу администраторов")
    try:
        catalog = directory_load_groups()
    except Exception as exc:
        raise HTTPException(503, "Каталог групп AD временно недоступен") from exc
    by_dn = {str(group["dn"]).casefold(): group for group in catalog}
    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for group_dn in selected:
        key = group_dn.casefold()
        if key in seen:
            continue
        group = by_dn.get(key)
        if not group:
            raise HTTPException(400, f"Группа AD не найдена: {group_dn}")
        result.append({"dn": group["dn"], "name": group["name"]})
        seen.add(key)
    return result


def ldap_load_directory(force: bool = False) -> List[Dict[str, Any]]:
    """Return a cached list of all enabled AD users, minus duplicates and
    accounts without a displayName (service/system accounts)."""
    with _directory_lock:
        now = time.time()
        if not force and _directory_cache["rows"] and (now - _directory_cache["ts"]) < DIRECTORY_TTL:
            return _directory_cache["rows"]

        flt = ("(&(objectClass=user)(objectCategory=person)"
               "(displayName=*)(sAMAccountName=*)"
               "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))")  # exclude disabled
        entries = _ldap_bind_and_search(flt, _LDAP_CONTACT_ATTRS, limit=0)
        rows = [_ldap_entry_to_contact(e) for e in entries]
        # Filter out obvious system / service accounts.
        # Prefix matches catch svc-*, iis-*, admin_*, _* (existing).
        # Exact-login matches catch confirmed non-person technical accounts that
        # have a displayName (so they pass the LDAP filter) but no mailbox and
        # no business use in the company address book. Verified against AD:
        # each has no mail, no thumbnailPhoto, and a technical displayName.
        # Bots WITH a mailbox (Randy@, RuPochta@) are intentionally kept.
        sys_prefixes = ("svc-", "svc_", "iis-", "admin_", "_")
        sys_logins = {
            "zabbix_user", "veeam-backup", "sync_user", "sql-user", "scan",
            "ldapadmin", "grafana", "ai", "1ceffectorsaver", "1c-user",
            "codexsandboxonline", "codexsandboxoffline",
        }
        rows = [
            r for r in rows
            if r.get("login")
            and not r["login"].lower().startswith(sys_prefixes)
            and r["login"].lower() not in sys_logins
        ]
        # Stable sort: department then name
        rows.sort(key=lambda r: (r.get("department") or "zzz", r.get("name") or ""))
        _directory_cache["rows"] = rows
        _directory_cache["ts"] = now
        log.info("LDAP directory refreshed: %d contacts", len(rows))
        return rows


def _portal_employee_to_contact(employee: Dict[str, Any]) -> Dict[str, Any]:
    accounts = employee.get("accounts") or {}
    mail = accounts.get("mail") or {}
    bitrix = accounts.get("bitrix") or {}
    email_addr = str(mail.get("email") or bitrix.get("email") or "").strip()
    company = _canonical_company(employee.get("company")) or str(
        employee.get("company") or ""
    ).strip()
    directory_domain = str(
        employee.get("directoryDomain")
        or employee.get("directory_domain")
        or employee.get("adDomain")
        or employee.get("ad_domain")
        or ""
    ).strip().lower() or _directory_domain_for_company(company)
    ad_login = str(accounts.get("ad") or "").strip()
    # Synthesise email from AD login when the employee has no configured
    # mailbox — gives every AD account a reachable identity in the directory
    # and makes cross-domain employees (alpha, beta, gamma) visible.
    if not email_addr and ad_login and directory_domain:
        email_addr = f"{ad_login}@{directory_domain}"
    directory_groups = (
        employee.get("directoryGroups")
        or employee.get("directory_groups")
        or []
    )
    return {
        "name": str(employee.get("fullName") or "").strip(),
        "email": email_addr,
        "login": ad_login,
        "mail_login": str(mail.get("login") or "").strip(),
        "emp_id": employee.get("id"),
        "department": str(employee.get("department") or "").strip(),
        "phone": "",
        "mobile": "",
        "title": str(employee.get("title") or "").strip(),
        "company": company,
        "directory_domain": directory_domain,
        "groups": [
            str(group.get("name") or "").strip()
            for group in directory_groups
            if isinstance(group, dict) and str(group.get("name") or "").strip()
        ],
        "group_dns": [
            str(group.get("dn") or group.get("distinguishedName") or "").strip()
            for group in directory_groups
            if isinstance(group, dict)
            and str(group.get("dn") or group.get("distinguishedName") or "").strip()
        ],
        "avatar_data_url": str(employee.get("avatarDataUrl") or "").strip(),
        "disabled": employee.get("active") is False,
        "source": "portal",
    }


def _portal_directory_payload() -> Dict[str, Any]:
    if CFG.PORTAL_EMPLOYEE_DIRECTORY_URL and CFG.PORTAL_INTEGRATION_SECRET:
        req = urllib.request.Request(
            CFG.PORTAL_EMPLOYEE_DIRECTORY_URL,
            method="GET",
            headers={
                "Accept": "application/json",
                "Host": CFG.PORTAL_MAIL_AUTH_HOST,
                "X-Integration-Secret": CFG.PORTAL_INTEGRATION_SECRET,
                "X-Internal-Service": "rupochta",
            },
        )
    elif CFG.PORTAL_DIRECTORY_BRIDGE_URL and CFG.INTERNAL_TOKEN:
        req = urllib.request.Request(
            CFG.PORTAL_DIRECTORY_BRIDGE_URL,
            method="GET",
            headers={
                "Accept": "application/json",
                "X-Internal-Token": CFG.INTERNAL_TOKEN,
                "X-Internal-Service": "rupochta",
            },
        )
    elif CFG.INTERNAL_TOKEN:
        req = urllib.request.Request(
            CFG.PROXY_PANEL_URL.rstrip("/") + "/api/directory/portal",
            method="GET",
            headers={
                "Accept": "application/json",
                "X-Internal-Token": CFG.INTERNAL_TOKEN,
                "X-Internal-Service": "rupochta",
            },
        )
    else:
        return {}
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("ok") is False:
        raise RuntimeError(payload.get("error") or "portal directory failed")
    return (
        (payload.get("data") or {}).get("directory")
        if isinstance(payload.get("data"), dict)
        else payload
    ) or {}


def portal_load_directory(force: bool = False) -> List[Dict[str, Any]]:
    with _portal_directory_lock:
        now = time.time()
        if (
            not force
            and _portal_directory_cache["rows"]
            and (now - _portal_directory_cache["ts"]) < DIRECTORY_TTL
        ):
            return list(_portal_directory_cache["rows"])

        try:
            payload = _portal_directory_payload()
            if not payload:
                return []
            employees = payload.get("employees") or []
            if not isinstance(employees, list):
                raise RuntimeError("portal directory returned an invalid employee list")
            rows = [
                _portal_employee_to_contact(employee)
                for employee in employees
                if isinstance(employee, dict)
            ]
            rows = [row for row in rows if row.get("name") or row.get("login")]
            rows.sort(
                key=lambda row: (
                    row.get("department") or "zzz",
                    row.get("name") or "",
                )
            )
            _portal_directory_cache["rows"] = rows
            groups = (payload.get("options") or {}).get("adGroups") or []
            _portal_directory_cache["groups"] = [
                {
                    "dn": str(group.get("dn") or "").strip(),
                    "name": str(group.get("name") or group.get("dn") or "").strip(),
                    "description": "",
                    "mail": "",
                    "member_count": int(group.get("memberCount") or 0),
                    "company": _canonical_company(group.get("company"))
                    or str(group.get("company") or "").strip(),
                    "directory_domain": str(group.get("domain") or "").strip().lower(),
                    "source": "portal",
                }
                for group in groups
                if isinstance(group, dict) and str(group.get("dn") or "").strip()
            ]
            _portal_directory_cache["ts"] = now
            log.info("Portal employee directory refreshed: %d contacts", len(rows))
            return list(rows)
        except Exception as exc:
            log.warning("Portal employee directory unavailable: %s", exc)
            return []


def portal_load_groups(force: bool = False) -> List[Dict[str, Any]]:
    portal_load_directory(force=force)
    return deepcopy(_portal_directory_cache.get("groups") or [])


def directory_load_groups(force: bool = False) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    errors: List[str] = []
    try:
        groups.extend(ldap_load_groups(force=force))
    except Exception as exc:
        errors.append(str(exc))
    try:
        groups.extend(portal_load_groups(force=force))
    except Exception as exc:
        errors.append(str(exc))
    deduplicated = {
        str(group.get("dn") or "").strip().casefold(): group
        for group in groups
        if str(group.get("dn") or "").strip()
    }
    if not deduplicated and errors:
        raise RuntimeError("; ".join(errors))
    return sorted(
        deduplicated.values(),
        key=lambda group: (
            str(group.get("company") or ""),
            str(group.get("name") or group.get("dn") or "").casefold(),
        ),
    )


def portal_get_user_group_dns(login: str, directory_domain: str = "") -> List[str]:
    clean_login = str(login or "").strip().lower().split("@", 1)[0]
    clean_domain = str(directory_domain or "").strip().lower()
    matches = [
        row
        for row in portal_load_directory()
        if str(row.get("login") or "").strip().lower() == clean_login
        and (
            not clean_domain
            or str(row.get("directory_domain") or "").strip().lower() == clean_domain
        )
    ]
    domains = {
        str(row.get("directory_domain") or "").strip().lower()
        for row in matches
        if str(row.get("directory_domain") or "").strip()
    }
    if not clean_domain and len(domains) > 1:
        return []
    return list(
        dict.fromkeys(
            str(group_dn).strip()
            for row in matches
            for group_dn in row.get("group_dns") or []
            if str(group_dn or "").strip()
        )
    )


def ldap_get_user_by_login(login: str, with_photo: bool = True) -> Optional[Dict[str, Any]]:
    """Fetch a single AD user by sAMAccountName, including thumbnailPhoto."""
    safe = re.sub(r"[()\\*\x00]", "", login)
    if not safe:
        return None
    attrs = _LDAP_CONTACT_ATTRS + ["thumbnailPhoto"]
    flt = f"(&(objectClass=user)(sAMAccountName={safe}))"
    entries = _ldap_bind_and_search(flt, attrs, limit=1)
    if not entries:
        return None
    return _ldap_entry_to_contact(entries[0], include_photo=with_photo)


def ldap_get_user_photo(login: str) -> Optional[bytes]:
    """Return raw JPEG bytes of a user's thumbnailPhoto, or None."""
    safe = re.sub(r"[()\\*\x00]", "", login)
    if not safe:
        return None
    cache_key = safe.lower()
    now = time.time()
    with _directory_photo_lock:
        cached = _directory_photo_cache.get(cache_key)
        if cached and float(cached.get("expires") or 0) > now:
            return cached.get("raw") or None
        if cached:
            _directory_photo_cache.pop(cache_key, None)
    entries = _ldap_bind_and_search(
        f"(&(objectClass=user)(sAMAccountName={safe}))",
        ["thumbnailPhoto"],
        limit=1,
    )
    raw: Optional[bytes] = None
    if not entries:
        with _directory_photo_lock:
            _directory_photo_cache[cache_key] = {"expires": now + DIRECTORY_PHOTO_TTL, "raw": None}
        return None
    entry = entries[0]
    if "thumbnailPhoto" in entry:
        try:
            candidate = entry.thumbnailPhoto.value
            if isinstance(candidate, bytes) and candidate:
                raw = candidate
        except Exception:
            pass
    with _directory_photo_lock:
        _directory_photo_cache[cache_key] = {"expires": now + DIRECTORY_PHOTO_TTL, "raw": raw}
    return raw


def ldap_get_user_displayname(login: str) -> str:
    if not _ldap_bind_available():
        return login
    safe = re.sub(r"[()\\*\x00]", "", login)
    flt = f"(&(objectClass=user)(sAMAccountName={safe}))"
    for srv_url in CFG.LDAP_SERVERS:
        try:
            host_part = srv_url.split("://", 1)[1]
            host, _, port = host_part.partition(":")
            use_ssl = srv_url.startswith("ldaps://")
            srv = Server(host, port=int(port) if port else (636 if use_ssl else 389), use_ssl=use_ssl)
            conn = Connection(
                srv,
                user=CFG.LDAP_BIND_USER,
                password=CFG.LDAP_BIND_PASS,
                authentication=SIMPLE,
                auto_bind=True,
                receive_timeout=8,
            )
            conn.search(
                search_base=CFG.LDAP_BASE_DN,
                search_filter=flt,
                search_scope=SUBTREE,
                attributes=["displayName"],
                size_limit=1,
            )
            if conn.entries:
                name = str(conn.entries[0].displayName)
                conn.unbind()
                return name or login
            conn.unbind()
        except Exception:
            continue
    return login


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="RuPochta Webmail", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost", "http://127.0.0.1", "http://localhost:18400"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic models
class LoginBody(BaseModel):
    email: str
    password: str
    remember_me: bool = False


class TelegramChallengeStartBody(BaseModel):
    action: str


class TelegramChallengeTokenBody(BaseModel):
    action: str
    token: str


class TelegramResetPasswordBody(BaseModel):
    token: str
    password: str = Field(..., min_length=12, max_length=256)


class SendBody(BaseModel):
    to: str
    cc: Optional[str] = ""
    bcc: Optional[str] = ""
    subject: str
    body: str
    body_text: Optional[str] = None
    reply_to_uid: Optional[str] = None
    folder: Optional[str] = None
    in_reply_to: Optional[str] = None
    draft_uid: Optional[str] = None


class MoveBody(BaseModel):
    from_folder: str
    to_folder: str


class DraftBody(BaseModel):
    to: Optional[str] = ""
    cc: Optional[str] = ""
    bcc: Optional[str] = ""
    subject: Optional[str] = ""
    body: Optional[str] = ""
    body_text: Optional[str] = None
    in_reply_to: Optional[str] = None
    replace_uid: Optional[str] = None  # delete this draft after appending the new one


class AliasBody(BaseModel):
    mail_login: str = Field(..., min_length=3, max_length=30)


class MailAccountProfileBody(BaseModel):
    lastName: Optional[str] = Field(default=None, max_length=80)
    firstName: Optional[str] = Field(default=None, max_length=80)
    middleName: Optional[str] = Field(default=None, max_length=80)
    avatarDataUrl: Optional[str] = Field(default=None, max_length=4 * 1024 * 1024)


class MailAccountPasswordBody(BaseModel):
    currentPassword: Optional[str] = Field(default=None, max_length=256)
    newPassword: str = Field(..., min_length=8, max_length=256)


class MessageFlagsBody(BaseModel):
    folder: str = "INBOX"
    seen: Optional[bool] = None
    flagged: Optional[bool] = None


class InviteRsvpBody(BaseModel):
    folder: str = "INBOX"
    response: str = Field(..., pattern="^(accepted|tentative|declined)$")


class BulkActionBody(BaseModel):
    from_folder: str = "INBOX"
    uids: List[str]
    action: str
    to_folder: Optional[str] = None


class UndoMoveBody(BaseModel):
    from_folder: str
    to_folder: str
    message_ids: List[str]


class SignatureBody(BaseModel):
    signature: str = ""


class ForwardingBody(BaseModel):
    enabled: bool = False
    address: str = ""
    keep_copy: bool = True


class AutoReplyBody(BaseModel):
    enabled: bool = False
    subject: str = ""
    body: str = ""
    start_date: str | None = None
    end_date: str | None = None
    repeat_days: int = 1


class NotificationSettingsBody(BaseModel):
    telegram_enabled: bool


class FolderBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class TemplateBody(BaseModel):
    id: Optional[int] = None
    name: str = Field(..., min_length=1, max_length=100)
    subject: str = ""
    body: str = ""


# Helpers

_IMAP_AUTH_FAILURE_MARKERS = (
    "authentication failed",
    "authenticationfailed",
    "authenticationfailure",
    "auth failed",
    "auth failure",
    "login failed",
    "login failure",
    "invalid credentials",
    "invalid password",
    "login incorrect",
    "[authenticationfailed]",
)


def _imap_error_text(exc: BaseException) -> str:
    text = str(exc or "").strip()
    return text or exc.__class__.__name__


def _imap_error_is_auth_failure(exc: BaseException) -> bool:
    text = _imap_error_text(exc).lower()
    return any(marker in text for marker in _IMAP_AUTH_FAILURE_MARKERS)

def get_session_or_401(request: Request) -> Dict[str, Any]:
    token = request.cookies.get(CFG.SESSION_COOKIE)
    sess = session_get(token)
    if not sess:
        raise HTTPException(status_code=401, detail="Не авторизован")
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and not _is_same_origin_admin_request(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return sess


# The providers the central broker offers. Mail mirrors the set auth/my/help
# use — one authorize endpoint, one identity, no AD (ADR-0043).
MAIL_SSO_PROVIDERS = frozenset({"yandex", "yandex/mail", "vk", "telegram"})


def _mail_sso_enabled() -> bool:
    return bool(
        CFG.MAIL_SSO_CLIENT_ID
        and CFG.MAIL_SSO_AUTHORIZATION_ENDPOINT
        and CFG.MAIL_SSO_TOKEN_ENDPOINT
        and CFG.MAIL_SSO_USERINFO_ENDPOINT
    )


def _mail_sso_origin(request: Request) -> str:
    return CFG.MAIL_SSO_PUBLIC_URL


def _mail_sso_redirect_uri(request: Request) -> str:
    return f"{_mail_sso_origin(request)}/sso/callback"


def _mail_sso_cookie_secure() -> bool:
    return urllib.parse.urlparse(CFG.MAIL_SSO_PUBLIC_URL).scheme == "https"


def _mail_sso_pkce_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")


def _mail_sso_pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _mail_sso_pkce_cookie_value(state: str, code_verifier: str) -> str:
    return f"{state}.{code_verifier}"


def _mail_sso_pkce_cookie_parts(value: str) -> Tuple[str, str]:
    state, separator, code_verifier = str(value or "").partition(".")
    if not state or not separator or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", code_verifier):
        return "", ""
    return state, code_verifier


def _mail_sso_authorize_url(
    request: Request,
    state: str,
    nonce: str,
    code_verifier: str = "",
    idp_hint: str = "",
    prompt: str = "",
    provider: str = "",
) -> str:
    params = {
        "response_type": "code",
        "client_id": CFG.MAIL_SSO_CLIENT_ID,
        "redirect_uri": _mail_sso_redirect_uri(request),
        "scope": CFG.MAIL_SSO_SCOPE or "openid email groups",
        "state": state,
        "nonce": nonce,
    }
    if code_verifier:
        params["code_challenge"] = _mail_sso_pkce_challenge(code_verifier)
        params["code_challenge_method"] = "S256"
    if idp_hint:
        params["kc_idp_hint"] = idp_hint
    if prompt == "none":
        params["prompt"] = prompt
    if provider in MAIL_SSO_PROVIDERS:
        params["provider"] = provider
    return f"{CFG.MAIL_SSO_AUTHORIZATION_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _mail_sso_clear_transaction(response: Response) -> Response:
    response.delete_cookie(
        CFG.MAIL_SSO_STATE_COOKIE,
        path="/",
        secure=_mail_sso_cookie_secure(),
    )
    response.delete_cookie(
        CFG.MAIL_SSO_PKCE_COOKIE,
        path="/",
        secure=_mail_sso_cookie_secure(),
    )
    return response


def _mail_sso_telegram_enabled() -> bool:
    # Telegram is a provider of the central broker, not a separate upstream IdP
    # reached by alias, so it is available exactly when SSO is (ADR-0043).
    return _mail_sso_enabled()


def _mail_password_login_enabled() -> bool:
    # Mailbox password login stays available as a fallback: the optional
    # directory-auth bridge may be unreachable from this host.
    return True


def _mail_sso_account_console_url() -> str:
    """Where a signed-in user configures their mailbox.

    Points at the personal cabinet, not at the broker's own account page: the
    binding is made in «Интеграции», and the old link led to a Keycloak-era
    console that has nothing to do with mail (ADR-0043).
    """
    return CFG.MAIL_ACCOUNT_CONSOLE_URL


def _mail_sso_subject(value: Any) -> str:
    subject = str(value or "").strip()
    return subject if 1 <= len(subject) <= 255 else ""


def _mail_sso_telegram_account_handoff_allowed(sess: Dict[str, Any]) -> bool:
    return bool(
        _mail_sso_telegram_enabled()
        and sess.get("auth_source") == "sso"
        and _mail_sso_subject(sess.get("sso_subject"))
    )


def _mail_sso_session_hash(session_token: str) -> str:
    return hashlib.sha256(str(session_token or "").encode("utf-8")).hexdigest()


def _mail_sso_telegram_account_transaction(
    sess: Dict[str, Any], session_token: str, state: str
) -> Optional[Dict[str, Any]]:
    transaction = sess.get("telegram_sso_account")
    if not isinstance(transaction, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", state):
        return None
    try:
        expires = float(transaction.get("expires") or 0)
    except (TypeError, ValueError):
        return None
    expected_state = str(transaction.get("state") or "")
    session_hash = str(transaction.get("session_hash") or "")
    subject = _mail_sso_subject(transaction.get("subject"))
    if (
        expires < time.time()
        or not subject
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", expected_state)
        or not re.fullmatch(r"[0-9a-f]{64}", session_hash)
        or not _ct_eq(state, expected_state)
        or not _ct_eq(_mail_sso_session_hash(session_token), session_hash)
    ):
        return None
    return transaction


def _mail_sso_clear_telegram_account_transaction(
    response: Response,
    *,
    session_token: str = "",
    sess: Optional[Dict[str, Any]] = None,
) -> Response:
    if sess is not None:
        sess.pop("telegram_sso_account", None)
        if session_token:
            _session_persist_if_active(session_token, sess)
    for cookie_name in (
        CFG.MAIL_SSO_TELEGRAM_ACCOUNT_STATE_COOKIE,
        CFG.MAIL_SSO_TELEGRAM_ACCOUNT_PKCE_COOKIE,
    ):
        response.delete_cookie(
            cookie_name,
            path="/",
            secure=_mail_sso_cookie_secure(),
        )
    return response


def _mail_sso_telegram_account_result_redirect(result: str) -> JSONResponse:
    response = JSONResponse({"ok": False}, status_code=302)
    response.headers["Location"] = f"/?telegram_sso_account={result}"
    response.headers["Cache-Control"] = "no-store"
    return response


def _json_http_request(
    url: str,
    *,
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 15,
) -> Dict[str, Any]:
    data = None
    req_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw or "{}")


def _portal_mail_auth_request(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    if not CFG.PORTAL_MAIL_AUTH_URL or not CFG.PORTAL_INTEGRATION_SECRET:
        raise RuntimeError("portal mail auth is not configured")
    try:
        return _json_http_request(
            f"{CFG.PORTAL_MAIL_AUTH_URL.rstrip('/')}/{path.lstrip('/')}",
            method="POST",
            body=body,
            headers={
                "Accept": "application/json",
                "X-Integration-Secret": CFG.PORTAL_INTEGRATION_SECRET,
                "X-Internal-Service": "rupochta",
            },
            timeout=15,
        )
    except urllib.error.HTTPError as exc:
        # The portal returns a structured {ok, error} JSON body even on
        # 401/403/409 (bad credentials, unknown user) -- that is a normal
        # auth rejection, not a broker outage, so parse it instead of
        # letting it surface as a generic 503 to the caller.
        try:
            return json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
        except Exception:
            raise exc


def _mail_account_available(sess: Dict[str, Any]) -> bool:
    return bool(
        str(sess.get("auth_source") or "").strip().lower() == "sso"
        and (
            str(sess.get("sso_username") or "").strip()
            or _mail_sso_subject(sess.get("sso_subject"))
        )
    )


def _portal_mail_account_request(
    sess: Dict[str, Any],
    path: str,
    *,
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
    source: str = "",
) -> Tuple[int, Dict[str, Any]]:
    if not CFG.PORTAL_MAIL_AUTH_URL or not CFG.PORTAL_INTEGRATION_SECRET:
        raise RuntimeError("portal mail account is not configured")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{CFG.PORTAL_MAIL_AUTH_URL.rstrip('/')}/{path.lstrip('/')}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Host": CFG.PORTAL_MAIL_AUTH_HOST,
            "X-Integration-Secret": CFG.PORTAL_INTEGRATION_SECRET,
            "X-Internal-Service": "rupochta",
            "X-Account-Username": str(sess.get("sso_username") or "").strip(),
            "X-Account-Subject": _mail_sso_subject(sess.get("sso_subject")),
            "X-Account-Source": str(source or "").strip(),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
            return response.status, payload
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
        except Exception:
            payload = {"ok": False, "error": "account_service_unavailable"}
        return exc.code, payload


def _mail_telegram_browser_binding(request: Request, response: Response) -> str:
    current = str(request.cookies.get(CFG.MAIL_TELEGRAM_BROWSER_COOKIE) or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", current):
        return current
    binding = secrets.token_urlsafe(32)
    response.set_cookie(
        CFG.MAIL_TELEGRAM_BROWSER_COOKIE,
        binding,
        max_age=max(60, CFG.MAIL_TELEGRAM_BROWSER_TTL),
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
        path="/",
    )
    return binding


def _mail_telegram_browser_binding_from_request(request: Request) -> str:
    binding = str(request.cookies.get(CFG.MAIL_TELEGRAM_BROWSER_COOKIE) or "").strip()
    return binding if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", binding) else ""


def _portal_mail_employee(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    employee = payload.get("employee") if isinstance(payload, dict) else None
    if not isinstance(employee, dict):
        return None
    try:
        employee_id = int(employee.get("employeeId"))
    except (TypeError, ValueError):
        return None
    ad_login = str(employee.get("adLogin") or "").strip().lower()
    mail_login = str(employee.get("mailLogin") or "").strip().lower()
    directory_domain = str(employee.get("directoryDomain") or "").strip().lower()
    if employee_id <= 0 or not ad_login or not mail_login:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,62}", mail_login):
        return None
    return {
        "id": employee_id,
        "ad_login": ad_login,
        "mail_login": mail_login,
        "ad_domain": directory_domain,
        "full_name": str(employee.get("displayName") or "").strip(),
    }


def _sanitize_portal_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = deepcopy(profile)
    employee = sanitized.get("employee")
    if not isinstance(employee, dict):
        return sanitized
    avatar = employee.get("avatarDataUrl")
    if (
        isinstance(avatar, str)
        and len(avatar) > PORTAL_PROFILE_AVATAR_MAX_CHARS
    ):
        employee.pop("avatarDataUrl", None)
        log.warning(
            "Portal avatar omitted because encoded payload exceeds %d characters",
            PORTAL_PROFILE_AVATAR_MAX_CHARS,
        )
    return sanitized


def _mail_profile_snapshot(profile: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(profile, dict):
        return None
    employee = _sanitize_portal_profile(profile).get("employee")
    if not isinstance(employee, dict):
        return None
    snapshot: Dict[str, Any] = {}
    full_name = str(employee.get("fullName") or "").strip()
    avatar = employee.get("avatarDataUrl")
    if full_name:
        snapshot["fullName"] = full_name
    if isinstance(avatar, str) and avatar:
        snapshot["avatarDataUrl"] = avatar
    return {"employee": snapshot} if snapshot else None


def _portal_personal_profile(identity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not CFG.PORTAL_PERSONAL_PROFILE_URL or not CFG.PORTAL_INTEGRATION_SECRET:
        return None
    query = urllib.parse.urlencode({
        key: str(identity.get(key) or "").strip()
        for key in ("username", "email", "employeeId")
        if str(identity.get(key) or "").strip()
    })
    url = CFG.PORTAL_PERSONAL_PROFILE_URL
    if query:
        url += ("&" if "?" in url else "?") + query
    try:
        payload = _json_http_request(
            url,
            headers={
                "Accept": "application/json",
                "X-Integration-Secret": CFG.PORTAL_INTEGRATION_SECRET,
                "X-Internal-Service": "rupochta",
            },
            timeout=max(0.5, PORTAL_PROFILE_REQUEST_TIMEOUT),
        )
        profile = payload.get("profile")
        return _sanitize_portal_profile(profile) if isinstance(profile, dict) else None
    except Exception as exc:
        log.warning("Portal personal profile unavailable: %s", exc)
        return None


def _mail_sso_post_form(url: str, body: Dict[str, str]) -> Dict[str, Any]:
    request_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (compatible; RuPochta/1.0; +https://mail.example.com)",
    }
    if CFG.MAIL_SSO_CLIENT_SECRET:
        auth_raw = f"{CFG.MAIL_SSO_CLIENT_ID}:{CFG.MAIL_SSO_CLIENT_SECRET}".encode("utf-8")
        import base64
        request_headers["Authorization"] = f"Basic {base64.b64encode(auth_raw).decode('ascii')}"
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(body).encode("utf-8"),
        method="POST",
        headers=request_headers,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")


def _mail_sso_userinfo(access_token: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        CFG.MAIL_SSO_USERINFO_ENDPOINT,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "Mozilla/5.0 (compatible; RuPochta/1.0; +https://mail.example.com)",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")


def _admin_sso_enabled() -> bool:
    return bool(CFG.MAILADMIN_SSO_CLIENT_ID and CFG.MAILADMIN_SSO_CLIENT_SECRET)


def _admin_sso_uses_mail_callback(request: Request) -> bool:
    if not _mail_sso_enabled():
        return False
    public_url = urllib.parse.urlparse(CFG.MAIL_SSO_PUBLIC_URL)
    return bool(
        public_url.netloc
        and _request_host(request).lower().rstrip(".")
        == public_url.netloc.lower().rstrip(".")
    )


def _admin_sso_origin(request: Optional[Request] = None) -> str:
    origins: List[str] = []
    for raw_origin in CFG.MAILADMIN_SSO_PUBLIC_URLS:
        parsed = urllib.parse.urlparse(str(raw_origin or "").rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in origins:
            origins.append(origin)
    if not origins:
        return ""
    if request is not None:
        request_host = _request_host(request).lower().rstrip(".")
        for origin in origins:
            if urllib.parse.urlparse(origin).netloc.lower().rstrip(".") == request_host:
                return origin
    return origins[0]


def _admin_sso_redirect_uri(request: Optional[Request] = None) -> str:
    if request is not None and _admin_sso_uses_mail_callback(request):
        return _mail_sso_redirect_uri(request)
    return f"{_admin_sso_origin(request)}/admin/sso/callback"


def _admin_sso_authorize_url(request: Request, state: str, nonce: str) -> str:
    use_mail_callback = _admin_sso_uses_mail_callback(request)
    params = {
        "response_type": "code",
        "client_id": (
            CFG.MAIL_SSO_CLIENT_ID
            if use_mail_callback
            else CFG.MAILADMIN_SSO_CLIENT_ID
        ),
        "redirect_uri": _admin_sso_redirect_uri(request),
        "scope": (
            CFG.MAIL_SSO_SCOPE
            if use_mail_callback
            else CFG.MAILADMIN_SSO_SCOPE
        ) or "openid email groups",
        "state": state,
        "nonce": nonce,
    }
    authorization_endpoint = (
        CFG.MAIL_SSO_AUTHORIZATION_ENDPOINT
        if use_mail_callback
        else CFG.MAILADMIN_SSO_AUTHORIZATION_ENDPOINT
    )
    return (
        f"{authorization_endpoint}?"
        f"{urllib.parse.urlencode(params)}"
    )


def _admin_sso_post_form(body: Dict[str, str]) -> Dict[str, Any]:
    auth_raw = (
        f"{CFG.MAILADMIN_SSO_CLIENT_ID}:{CFG.MAILADMIN_SSO_CLIENT_SECRET}"
    ).encode("utf-8")
    import base64
    req = urllib.request.Request(
        CFG.MAILADMIN_SSO_TOKEN_ENDPOINT,
        data=urllib.parse.urlencode(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {base64.b64encode(auth_raw).decode('ascii')}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")


def _admin_sso_userinfo(access_token: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        CFG.MAILADMIN_SSO_USERINFO_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")


def _admin_sso_user_is_allowed(user: Dict[str, Any]) -> bool:
    groups = user.get("groups")
    if not isinstance(groups, list):
        return False
    return _admin_group_claims_allowed(groups)


def _admin_sso_username(user: Dict[str, Any]) -> str:
    for key in ("username", "preferred_username", "email"):
        value = str(user.get(key) or "").strip().lower()
        if not value:
            continue
        local = value.split("@", 1)[0]
        local = re.sub(r"[^a-z0-9._-]", "", local)
        if local:
            return local
    return ""


def _sso_candidate_logins(user: Dict[str, Any]) -> List[str]:
    raw_values: List[str] = []
    for key in ("username", "preferred_username"):
        value = user.get(key)
        if value:
            raw_values.append(str(value))
    out: List[str] = []
    for value in raw_values:
        local = value.strip().lower()
        if "@" in local:
            local = local.split("@", 1)[0]
        local = re.sub(r"[^a-z0-9._-]", "", local)
        if local and local not in out:
            out.append(local)
    return out


def _sso_identity_domain(user: Dict[str, Any]) -> str:
    candidates: List[str] = []
    for key in (
        "ad_domain",
        "directory_domain",
        "domain",
        "identity_provider",
        "idp",
        "idp_alias",
        "ldap_entry_dn",
        "upn",
        "userPrincipalName",
        "preferred_username",
        "username",
        "email",
    ):
        value = user.get(key)
        if value:
            candidates.append(str(value).strip().lower())
    for value in candidates:
        dn_domain = _directory_domain_from_dn(value)
        if dn_domain:
            return dn_domain
        for domain in DIRECTORY_PROFILES.values():
            if value == domain or value.endswith("@" + domain):
                return domain
            if re.search(rf"(?:^|[^a-z0-9.-]){re.escape(domain)}(?:$|[^a-z0-9.-])", value):
                return domain
    for claim in user.get("groups") or []:
        domain = _directory_domain_from_dn(claim)
        if domain:
            return domain
    return ""


async def _build_authenticated_session_payload(
    entered_local: str,
    full_email: str,
    password: str,
    identity_employee: Optional[Dict[str, Any]] = None,
    identity_domain: str = "",
    directory_lookup: bool = True,
) -> Dict[str, Any]:
    """Build a mail session.

    `directory_lookup=False` builds it from the mailbox alone — no AD and no
    employee directory. That is the shape central-auth logins use: the identity
    is already established by auth.example.com and this deployment has no AD to
    consult (ADR-0043).
    """
    loop = asyncio.get_event_loop()
    auth_local = full_email.split("@", 1)[0].lower()
    ad_domain = str(
        identity_domain
        or (identity_employee or {}).get("ad_domain")
        or _directory_domain_for_company((identity_employee or {}).get("company"))
        or ""
    ).strip().lower()
    entered_ldap = None
    if directory_lookup and (not ad_domain or ad_domain == "corp.local"):
        entered_ldap = await loop.run_in_executor(
            None,
            ldap_get_user_by_login,
            entered_local,
            False,
        )
    matched_emp = identity_employee or (
        None if not directory_lookup else (
            await loop.run_in_executor(
                None,
                _panel_find_employee_by_exact_mail_login,
                auth_local,
            )
            or await loop.run_in_executor(None, _find_employee_match, entered_local, "")
            or await loop.run_in_executor(None, _find_employee_match, auth_local, "")
        )
    )
    ad_login = (matched_emp.get("ad_login") or "").lower() if matched_emp else ""
    if not ad_login:
        ad_login = entered_local

    display_name = " ".join(
        part
        for part in (
            str((matched_emp or {}).get("last_name") or "").strip(),
            str((matched_emp or {}).get("first_name") or "").strip(),
        )
        if part
    )
    if not display_name and (not ad_domain or ad_domain == "corp.local"):
        display_name = await loop.run_in_executor(
            None,
            ldap_get_user_displayname,
            ad_login,
        )
    if not display_name:
        portal_match = next(
            (
                row
                for row in portal_load_directory()
                if str(row.get("login") or "").strip().lower() == ad_login
                and (
                    not ad_domain
                    or str(row.get("directory_domain") or "").strip().lower()
                    == ad_domain
                )
            ),
            None,
        )
        display_name = str((portal_match or {}).get("name") or ad_login)
    if not matched_emp:
        matched_emp = (
            await loop.run_in_executor(None, _find_employee_by_display_name, display_name)
            or await loop.run_in_executor(None, _find_employee_match, entered_local, display_name)
            or await loop.run_in_executor(None, _find_employee_match, auth_local, display_name)
        )
        if matched_emp and matched_emp.get("ad_login"):
            ad_login = (matched_emp.get("ad_login") or "").lower()
            if not ad_domain or ad_domain == "corp.local":
                display_name = await loop.run_in_executor(
                    None,
                    ldap_get_user_displayname,
                    ad_login,
                )

    emp_id = int(matched_emp["id"]) if matched_emp and matched_emp.get("id") else None
    if emp_id and entered_ldap and not (matched_emp.get("ad_login") or "").strip():
        try:
            synced = await loop.run_in_executor(
                None,
                lambda: _panel_update_employee_identity(
                    emp_id,
                    ad_login=entered_local,
                    ad_domain=ad_domain or "corp.local",
                ),
            )
            if synced:
                matched_emp = synced
        except EmployeeDirectoryUnavailable as exc:
            log.warning("employee identity sync unavailable during login: %s", exc)

    if matched_emp and matched_emp.get("mail_login"):
        mail_login = str(matched_emp["mail_login"]).lower()
    else:
        alias_row = db_get_alias(ad_login)
        mail_login = alias_row["mail_login"] if alias_row else auth_local
    if matched_emp and not (matched_emp.get("mail_login") or "").strip() and emp_id and auth_local:
        try:
            synced = await loop.run_in_executor(
                None,
                lambda: _panel_update_employee_identity(emp_id, mail_login=auth_local),
            )
            if synced:
                matched_emp = synced
        except (ValueError, EmployeeDirectoryUnavailable):
            pass

    if entered_ldap and entered_local != auth_local:
        try:
            await loop.run_in_executor(None, lambda: db_set_alias(entered_local, auth_local))
        except Exception:
            pass

    return {
        "emp_id": emp_id,
        "ad_login": ad_login,
        "ad_domain": ad_domain or _directory_domain_for_company(
            (matched_emp or {}).get("company")
        ),
        "company": _canonical_company((matched_emp or {}).get("company"))
        or _company_for_directory_domain(ad_domain),
        "email": full_email,
        "display_name": display_name,
        "mail_login": mail_login,
        "from_address": f"{mail_login}@{CFG.MAIL_DOMAIN}",
        "imap_user": full_email,
        "imap_pass": password,
        "profile_card": None,
    }


def _set_webmail_session_cookie(
    response: Response,
    token: str,
    request: Request,
    remember_me: bool = False,
) -> None:
    response.set_cookie(
        CFG.SESSION_COOKIE,
        token,
        max_age=CFG.SESSION_TTL if remember_me else None,
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
        path="/",
    )


def _webmail_login_payload(sess_data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ok": True,
        "email": sess_data["email"],
        "display_name": sess_data["display_name"],
        "mail_login": sess_data["mail_login"],
        "from_address": sess_data["from_address"],
        "profile_card": _mail_profile_snapshot(sess_data.get("profile_card")),
    }


def _settings_owner_key(sess: Dict[str, Any]) -> str:
    login = str(sess.get("ad_login") or "").strip().lower()
    domain = str(sess.get("ad_domain") or "").strip().lower()
    if domain and domain != "corp.local" and login:
        return f"{domain}\\{login}"
    if login:
        return login
    return str(sess.get("imap_user") or sess.get("email") or "").strip().lower()


def _profile_payload_from_session(sess: Dict[str, Any]) -> Dict[str, Any]:
    owner_key = _settings_owner_key(sess)
    signature = db_get_user_signature(owner_key)
    templates = db_list_user_templates(owner_key)
    return {
        "ok": True,
        "ad_login": sess["ad_login"],
        "ad_domain": sess.get("ad_domain", ""),
        "company": sess.get("company", ""),
        "email": sess["email"],
        "display_name": sess["display_name"],
        "mail_login": sess["mail_login"],
        "from_address": sess["from_address"],
        "signature": signature,
        "templates": templates,
        "profile_card": _mail_profile_snapshot(sess.get("profile_card")),
    }


async def _refresh_portal_profile_card(
    sess: Dict[str, Any],
    *,
    token: str = "",
    username_key: str = "ad_login",
    mail_snapshot: bool = True,
) -> Optional[Dict[str, Any]]:
    identity = {
        "username": sess.get(username_key),
        "email": sess.get("email"),
        "employeeId": sess.get("emp_id"),
    }
    profile_card = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: _portal_personal_profile(identity),
    )
    if profile_card is not None:
        if mail_snapshot:
            profile_card = _mail_profile_snapshot(profile_card)
        sess["profile_card"] = profile_card
        _session_cache_clear(sess, "bootstrap")
        if token:
            await asyncio.get_event_loop().run_in_executor(
                None,
                _session_persist_if_active,
                token,
                sess,
            )
    return sess.get("profile_card")


def _profile_refresh_done(task: asyncio.Task[Any]) -> None:
    _profile_refresh_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("Portal profile background refresh failed")


def _schedule_portal_profile_refresh(
    sess: Dict[str, Any],
    token: str = "",
    *,
    username_key: str = "ad_login",
    mail_snapshot: bool = True,
) -> None:
    refresh_session = session_get(token) if token else sess
    task = asyncio.create_task(
        _refresh_portal_profile_card(
            refresh_session or sess,
            token=token,
            username_key=username_key,
            mail_snapshot=mail_snapshot,
        )
    )
    _profile_refresh_tasks.add(task)
    task.add_done_callback(_profile_refresh_done)


def _imap_bootstrap_mailbox(
    user: str,
    password: str,
    folder: str,
    page: int,
    per_page: int,
    filter_query: str = "",
) -> Dict[str, Any]:
    with imap_session(user, password) as conn:
        folders = _imap_list_folders_conn(conn)
        messages = _imap_message_list_conn(conn, folder, page, per_page, filter_query)
    return {"folders": folders, "messages": messages}


def parse_address_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    parts = [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
    out = []
    for p in parts:
        name, addr = parseaddr(p)
        if addr:
            out.append(addr)
        else:
            out.append(p)
    return out


def normalize_email_login(value: str) -> Tuple[str, str]:
    """Returns (ad_login, full_email). Accepts 'user' or 'user@domain'."""
    value = value.strip().lower()
    if "@" in value:
        local, _, _ = value.partition("@")
    else:
        local = value
    return local, f"{local}@{CFG.MAIL_DOMAIN}"


def _mailbox_local_parts() -> set[str]:
    return {
        str(m.get("email") or "").split("@", 1)[0].strip().lower()
        for m in _mailserver_stats().get("mailboxes", [])
        if str(m.get("email") or "").strip()
    }


def _mail_login_candidates_from_display_name(display_name: str) -> List[str]:
    parts = re.sub(r"[^a-zа-яё ]", "", (display_name or "").lower()).split()
    if len(parts) < 2:
        return []
    mailbox_locals = _mailbox_local_parts()
    if not mailbox_locals:
        return []
    candidates: List[str] = []
    p0 = _translit(parts[0])
    p1 = _translit(parts[1])
    for fn, ln in ((p0, p1), (p1, p0)):
        if not (fn and ln):
            continue
        for local in (f"{ln}.{fn}", f"{fn}.{ln}", f"{fn[:1]}.{ln}", f"{ln}.{fn[:1]}"):
            if local in mailbox_locals and local not in candidates:
                candidates.append(local)
    return candidates


def resolve_imap_login_candidates(value: str) -> List[str]:
    """Return possible mailbox usernames for a webmail login.

    Users often type an AD login even when the real mailbox uses a different
    mail_login. Try the entered local-part first, then any mapped mail_login
    we can resolve from proxy_employees or the local alias DB.
    """
    local, full_email = normalize_email_login(value)
    candidates: List[str] = []

    def _add(candidate: str) -> None:
        clean = str(candidate or "").strip().lower()
        if clean and clean not in candidates:
            candidates.append(clean)

    _add(full_email)

    exact = _panel_find_employee_by_exact_login(local)
    if exact and exact.get("mail_login"):
        _add(f"{str(exact['mail_login']).strip().lower()}@{CFG.MAIL_DOMAIN}")

    alias = db_get_alias(local)
    if alias and alias.get("mail_login"):
        _add(f"{str(alias['mail_login']).strip().lower()}@{CFG.MAIL_DOMAIN}")

    matched = _find_employee_match(local, "")
    if matched and matched.get("mail_login"):
        _add(f"{str(matched['mail_login']).strip().lower()}@{CFG.MAIL_DOMAIN}")
    elif local:
        # If proxy_employees has no exact login binding yet, try the AD
        # displayName for this login and map that to an employee by name.
        display_name = ldap_get_user_displayname(local)
        if display_name and display_name.strip().lower() != local:
            matched = _find_employee_by_display_name(display_name)
            if matched and matched.get("mail_login"):
                _add(f"{str(matched['mail_login']).strip().lower()}@{CFG.MAIL_DOMAIN}")
            for guessed_local in _mail_login_candidates_from_display_name(display_name):
                _add(f"{guessed_local}@{CFG.MAIL_DOMAIN}")

    return candidates


def _html_to_plain_text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _plain_text_to_html(value: str) -> str:
    safe = html.escape(value or "")
    return f"<div style=\"font-family:Segoe UI,Arial,sans-serif;white-space:pre-wrap\">{safe.replace(chr(10), '<br>')}</div>"


def _looks_like_html(value: str) -> bool:
    return bool(re.search(r"<[a-zA-Z][^>]*>", value or ""))


def _compose_body_variants(html_body: str, body_text: Optional[str]) -> Tuple[str, str]:
    clean_html = (html_body or "").strip()
    clean_text = (body_text or "").strip()
    if clean_html and not _looks_like_html(clean_html):
        if not clean_text:
            clean_text = clean_html
        clean_html = _plain_text_to_html(clean_text or clean_html)
    if not clean_html and clean_text:
        clean_html = _plain_text_to_html(clean_text)
    if clean_html and not clean_text:
        clean_text = _html_to_plain_text(clean_html)
    return clean_html or "", clean_text or ""


_INLINE_IMAGE_RE = re.compile(
    r"data:image/(?P<subtype>png|jpeg|webp|gif);base64,(?P<data>[^\"'\s<>]+)",
    re.IGNORECASE,
)


def _extract_inline_images(body_html: str) -> Tuple[str, List[Dict[str, Any]]]:
    images: List[Dict[str, Any]] = []

    def replace(match: re.Match[str]) -> str:
        if len(images) >= 10:
            return ""
        try:
            content = base64.b64decode(match.group("data"), validate=True)
        except (ValueError, TypeError):
            return ""
        if (
            not content
            or len(content) > 4 * 1024 * 1024
            or sum(len(image["content"]) for image in images) + len(content) > 12 * 1024 * 1024
        ):
            return ""
        subtype = match.group("subtype").lower().replace("jpg", "jpeg")
        cid = email.utils.make_msgid(domain=CFG.MAIL_DOMAIN)[1:-1]
        images.append({"cid": cid, "subtype": subtype, "content": content})
        return f"cid:{cid}"

    return _INLINE_IMAGE_RE.sub(replace, body_html or ""), images


def _calendar_escape(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", r"\n")
    )


def _parse_content_type_header(raw: str) -> tuple[str, Dict[str, str]]:
    parts = [p.strip() for p in str(raw or "").split(";") if p.strip()]
    base = parts[0].lower() if parts else ""
    params: Dict[str, str] = {}
    for item in parts[1:]:
        key, sep, value = item.partition("=")
        if not sep:
            continue
        params[key.strip().lower()] = value.strip().strip('"')
    return base, params


def _extract_calendar_payload(
    attachments: Optional[List[Dict[str, Any]]],
) -> tuple[Optional[str], str, Optional[str], Optional[int]]:
    for idx, attachment in enumerate(attachments or []):
        filename = str(attachment.get("filename") or "")
        ctype = str(attachment.get("content_type") or "application/octet-stream")
        base_ctype, params = _parse_content_type_header(ctype)
        if base_ctype != "text/calendar" and not filename.lower().endswith(".ics"):
            continue
        content = attachment.get("content") or b""
        if isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        else:
            text = str(content)
        return text, params.get("method") or "REQUEST", filename or "invite.ics", idx
    return None, "REQUEST", None, None


def _invite_find_attendee(invite: Dict[str, Any], emails: List[str]) -> Optional[Dict[str, Any]]:
    email_set = {str(e or "").strip().lower() for e in emails if str(e or "").strip()}
    if not email_set:
        return None
    for attendee in invite.get("attendees") or []:
        attendee_email = str((attendee or {}).get("email") or "").strip().lower()
        if attendee_email and attendee_email in email_set:
            return attendee
    return None


def _build_invite_reply_attachment(
    invite: Dict[str, Any],
    attendee_name: str,
    attendee_email: str,
    response: str,
) -> Dict[str, Any]:
    response_key = str(response or "").strip().lower()
    partstat = {
        "accepted": "ACCEPTED",
        "tentative": "TENTATIVE",
        "declined": "DECLINED",
    }.get(response_key, "NEEDS-ACTION")
    organizer = invite.get("organizer") or {}
    organizer_email = str(organizer.get("email") or "").strip()
    organizer_name = str(organizer.get("name") or organizer_email).strip()
    summary = str(invite.get("summary") or "Meeting").strip()
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//RuPochta Webmail//Invite Reply//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REPLY",
        "BEGIN:VEVENT",
        f"UID:{invite.get('uid') or ''}",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{_calendar_escape(summary)}",
    ]
    if invite.get("sequence"):
        lines.append(f"SEQUENCE:{invite.get('sequence')}")
    start_raw = str(((invite.get("start") or {}).get("raw") or "")).strip()
    end_raw = str(((invite.get("end") or {}).get("raw") or "")).strip()
    if start_raw:
        lines.append(f"DTSTART:{start_raw}")
    if end_raw:
        lines.append(f"DTEND:{end_raw}")
    if invite.get("location"):
        lines.append(f"LOCATION:{_calendar_escape(str(invite.get('location') or ''))}")
    if organizer_email:
        lines.append(
            f"ORGANIZER;CN={_calendar_escape(organizer_name)}:mailto:{organizer_email}"
        )
    lines.append(
        "ATTENDEE;CN="
        f"{_calendar_escape(attendee_name or attendee_email)}"
        f";ROLE=REQ-PARTICIPANT;PARTSTAT={partstat};RSVP=FALSE:mailto:{attendee_email}"
    )
    lines.extend([
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])
    return {
        "filename": "invite-reply.ics",
        "content_type": "text/calendar; method=REPLY; charset=UTF-8",
        "content": "\r\n".join(lines).encode("utf-8"),
    }


def _build_mail_message(
    from_addr: str,
    to: List[str],
    cc: List[str],
    bcc: Optional[List[str]],
    subject: str,
    body_html: str,
    body_text: str,
    in_reply_to: Optional[str] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    msg["Subject"] = subject or "(без темы)"
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain=CFG.MAIL_DOMAIN)
    msg["X-Mailer"] = "RuPochta Webmail"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body_text or "")
    if body_html:
        body_html, inline_images = _extract_inline_images(body_html)
        msg.add_alternative(body_html, subtype="html")
        html_part = msg.get_payload()[-1]
        for index, image in enumerate(inline_images, 1):
            extension = "jpg" if image["subtype"] == "jpeg" else image["subtype"]
            html_part.add_related(
                image["content"],
                maintype="image",
                subtype=image["subtype"],
                cid=f'<{image["cid"]}>',
                filename=f"inline-image-{index}.{extension}",
                disposition="inline",
            )
    calendar_text, calendar_method, calendar_name, calendar_index = _extract_calendar_payload(attachments)
    if calendar_text:
        msg.add_alternative(
            calendar_text,
            subtype="calendar",
            charset="utf-8",
            params={"method": calendar_method},
        )
        calendar_part = msg.get_payload()[-1]
        calendar_part["Content-Class"] = "urn:content-classes:calendarmessage"
        calendar_part["Content-Disposition"] = f'inline; filename="{calendar_name or "invite.ics"}"'
    for idx, attachment in enumerate(attachments or []):
        if calendar_index is not None and idx == calendar_index:
            continue
        filename = str(attachment.get("filename") or "attachment")
        content = attachment.get("content") or b""
        ctype = str(attachment.get("content_type") or "application/octet-stream")
        base_ctype, params = _parse_content_type_header(ctype)
        maintype, _, subtype = base_ctype.partition("/")
        maintype = maintype or "application"
        subtype = subtype or "octet-stream"
        add_kwargs: Dict[str, Any] = {}
        if params:
            add_kwargs["params"] = params
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename, **add_kwargs)
    return msg


async def _parse_compose_request(request: Request, draft_mode: bool = False) -> Dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" not in content_type:
        raw = await request.json()
        model = DraftBody.model_validate(raw) if draft_mode else SendBody.model_validate(raw)
        payload = model.model_dump()
        payload["attachments"] = []
        return payload

    form = await request.form()
    payload: Dict[str, Any] = {
        "to": str(form.get("to") or ""),
        "cc": str(form.get("cc") or ""),
        "bcc": str(form.get("bcc") or ""),
        "subject": str(form.get("subject") or ""),
        "body": str(form.get("body") or ""),
        "body_text": str(form.get("body_text") or ""),
        "in_reply_to": form.get("in_reply_to") or None,
        "replace_uid": form.get("replace_uid") or None,
        "draft_uid": form.get("draft_uid") or None,
        "attachments": [],
    }
    for _, item in form.multi_items():
        if not hasattr(item, "filename") or not getattr(item, "filename", ""):
            continue
        payload["attachments"].append(
            {
                "filename": item.filename,
                "content_type": getattr(item, "content_type", None) or "application/octet-stream",
                "content": await item.read(),
            }
        )
    if not draft_mode:
        payload.pop("replace_uid", None)
    else:
        payload.pop("draft_uid", None)
    return payload


# ---------------------------------------------------------------------------
# Background worker: scheduled sends + snooze wake-ups
# ---------------------------------------------------------------------------

WORKER_POLL_INTERVAL = int(os.environ.get("WEBMAIL_WORKER_POLL", "30"))  # seconds


def _worker_process_scheduled_sends() -> int:
    """Process due scheduled_sends. Returns number of messages sent."""
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    sent_count = 0
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        due = con.execute(
            "SELECT * FROM scheduled_sends WHERE status='pending' "
            "AND scheduled_at <= ? ORDER BY scheduled_at LIMIT 20",
            (now_iso,),
        ).fetchall()
    for row in due:
        row_id = row["id"]
        try:
            _deliver_scheduled_send_row(row_id, allowed_statuses=("pending",))
        except Exception as e:
            log.warning("scheduled send id=%s failed: %s", row_id, e)
            with _db_connection() as con:
                current = con.execute(
                    "SELECT attempts FROM scheduled_sends WHERE id=?",
                    (row_id,),
                ).fetchone()
                attempts = int(current[0]) if current else 0
                status = "failed" if attempts >= 3 else "pending"
                con.execute(
                    "UPDATE scheduled_sends SET status=?, last_error=? WHERE id=?",
                    (status, str(e)[:500], row_id),
                )
                con.commit()
            continue
        log.info("scheduled send id=%s sent to %s", row_id, row["to_list"][:200])
        sent_count += 1
    return sent_count


def _worker_wake_snoozed() -> int:
    """Move due snoozed messages back to their original folder. Returns wake count."""
    now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    woken = 0
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        due = con.execute(
            "SELECT * FROM snoozed_messages WHERE status='pending' "
            "AND wake_at <= ? ORDER BY wake_at LIMIT 20",
            (now_iso,),
        ).fetchall()
    for row in due:
        row_id = row["id"]
        with _db_connection() as con:
            cur = con.execute(
                "UPDATE snoozed_messages SET status='waking' WHERE id=? AND status='pending'",
                (row_id,),
            )
            con.commit()
            if cur.rowcount == 0:
                continue
        try:
            imap_pass = decrypt_secret(row["imap_pass_enc"])
            if not imap_pass:
                raise RuntimeError("Could not decrypt stored credentials")
            # Find the UID in the Snoozed folder by Message-ID, move back
            with imap_session(row["imap_user"], imap_pass) as conn:
                typ, _ = conn.select('"Snoozed"', readonly=False)
                if typ != "OK":
                    raise RuntimeError("Cannot select Snoozed folder")
                search_id = f"<{row['message_id']}>" if not row["message_id"].startswith("<") else row["message_id"]
                typ, data = conn.uid("SEARCH", "HEADER", "Message-ID", search_id)
                if typ != "OK" or not data or not data[0]:
                    raise RuntimeError(f"Message not found in Snoozed: {row['message_id']}")
                found_uids = [
                    item.decode() if isinstance(item, (bytes, bytearray)) else str(item)
                    for item in data[0].split()
                ]
                if not found_uids:
                    raise RuntimeError("Zero UIDs matched")
                target_uid = found_uids[-1]
                _move_uids(conn, target_uid, row["original_folder"] or "INBOX")
                # Mark as unread again in the destination folder
                try:
                    conn.select(f'"{row["original_folder"] or "INBOX"}"', readonly=False)
                    typ2, data2 = conn.uid("SEARCH", "HEADER", "Message-ID", search_id)
                    if typ2 == "OK" and data2 and data2[0]:
                        new_ids = data2[0].split()
                        if new_ids:
                            conn.uid("STORE", new_ids[-1].decode(), "-FLAGS", "(\\Seen)")
                except Exception as e:
                    log.debug("snooze: could not clear seen flag for %s: %s", row["message_id"], e)
        except Exception as e:
            log.warning("snooze wake id=%s failed: %s", row_id, e)
            with _db_connection() as con:
                con.execute(
                    "UPDATE snoozed_messages SET status='failed', last_error=? WHERE id=?",
                    (str(e)[:500], row_id),
                )
                con.commit()
            continue
        with _db_connection() as con:
            con.execute(
                "UPDATE snoozed_messages SET status='woken', woken_at=?, "
                "imap_pass_enc='' WHERE id=?",
                (now_iso, row_id),
            )
            con.commit()
        log.info("snooze wake id=%s message_id=%s", row_id, row["message_id"])
        woken += 1
    return woken


def _ticket_intake_uids(email_address: str, password: str, last_uid: int) -> List[int]:
    with imap_session(email_address, password) as conn:
        typ, _ = conn.select('"INBOX"', readonly=True)
        if typ != "OK":
            raise RuntimeError("Cannot select INBOX")
        typ, data = conn.uid("SEARCH", None, "UID", f"{max(1, last_uid + 1)}:*")
        if typ != "OK":
            raise RuntimeError("Cannot search INBOX")
        return [int(value) for value in (data[0] or b"").split() if value.isdigit()]


def _ticket_intake_post(email_address: str, uid: int, message: Dict[str, Any]) -> None:
    if not CFG.TICKET_INTAKE_URL or not CFG.TICKET_INTAKE_SECRET:
        raise RuntimeError("ticket intake is not configured")
    sender_name, sender_email = parseaddr(str(message.get("from") or ""))
    message_id = str(message.get("message_id") or "").strip()
    body_text = str(message.get("body_text") or "").strip()
    if not body_text and message.get("body_html"):
        body_text = _html_to_plain_text(str(message["body_html"]))
    subject = str(message.get("subject") or "").strip()
    payload = {
        "externalId": f"mail:{email_address}:{message_id or uid}",
        "title": subject or f"Письмо от {sender_name or sender_email or 'неизвестного отправителя'}",
        "description": body_text or "Письмо без текстового содержимого",
        "requesterName": sender_name,
        "requesterEmail": sender_email,
        "requesterContact": sender_email,
        "mailbox": email_address,
        "messageUid": str(uid),
        "attachmentCount": len(message.get("attachments") or []),
    }
    request = urllib.request.Request(
        CFG.TICKET_INTAKE_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-Integration-Secret": CFG.TICKET_INTAKE_SECRET,
            "Host": "it.example.com",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"ticket intake returned {response.status}")


def _worker_process_ticket_intake() -> int:
    created = 0
    for row in _ticket_intake_rows():
        email_address = str(row["email"] or "")
        password = decrypt_secret(str(row["imap_pass_enc"] or ""))
        if not password:
            log.warning("ticket intake password cannot be decrypted for %s", email_address)
            continue
        try:
            last_uid = int(row["last_uid"] or 0)
            uids = _ticket_intake_uids(email_address, password, last_uid)
            if not int(row["initialized"] or 0):
                _ticket_intake_set_cursor(email_address, max(uids, default=last_uid))
                continue
            for uid in uids[:20]:
                message = imap_fetch_message(email_address, password, "INBOX", str(uid), mark_seen=False)
                _ticket_intake_post(email_address, uid, message)
                _ticket_intake_set_cursor(email_address, uid)
                created += 1
        except Exception as exc:
            log.warning("ticket intake failed for %s: %s", email_address, exc)
    return created


def background_worker_loop() -> None:
    """Infinite loop for scheduled sends, snooze wake-ups and ticket intake."""
    log.info("background worker starting (poll=%ds)", WORKER_POLL_INTERVAL)
    while True:
        try:
            _maybe_refresh_identity_cache()
            owners_synced = _sync_managed_address_owners()
            reconciled = _reconcile_managed_address_operations()
            lifecycle_reconciled = (
                _reconcile_mailbox_lifecycle_operations()
            )
            sent = _worker_process_scheduled_sends()
            woken = _worker_wake_snoozed()
            tickets = _worker_process_ticket_intake()
            if (
                owners_synced
                or reconciled
                or lifecycle_reconciled
                or sent
                or woken
                or tickets
            ):
                log.info(
                    "worker tick: owners_synced=%d reconciled=%d "
                    "lifecycle_reconciled=%d sent=%d woken=%d tickets=%d",
                    owners_synced,
                    reconciled,
                    lifecycle_reconciled,
                    sent,
                    woken,
                    tickets,
                )
        except Exception as e:
            log.warning("worker tick error: %s", e)
        time.sleep(WORKER_POLL_INTERVAL)


# ---- Endpoints ----

@app.on_event("startup")
async def on_startup():
    if not _ldap_bind_available():
        log.warning("LDAP bind is unavailable — contact search and LDAP-backed admin checks will stay disabled on this node.")
    if not CFG.INTERNAL_TOKEN:
        log.warning("RUPOCHTA_INTERNAL_TOKEN not set — internal proxy integrations will be unavailable.")
    db_init()
    t = threading.Thread(target=session_cleanup_loop, daemon=True)
    t.start()
    worker = threading.Thread(target=background_worker_loop, daemon=True)
    worker.start()
    log.info("RuPochta Webmail started on %s:%d", CFG.HOST, CFG.PORT)


@app.post("/api/auth/login")
async def api_login(body: LoginBody, request: Request, response: Response):
    if not _mail_password_login_enabled():
        return JSONResponse(
            {"error": "Вход по паролю почты отключён. Используйте вход через AD."},
            status_code=503,
        )
    ip = _client_ip(request)
    if not rate_check("webmail_login", ip):
        return JSONResponse(
            {"error": "Слишком много неудачных попыток. Попробуйте через несколько минут."},
            status_code=429,
        )
    entered_local, full_email = normalize_email_login(body.email)
    loop = asyncio.get_event_loop()
    login_candidates = resolve_imap_login_candidates(body.email)

    # Verify IMAP creds. Users may type an AD login even when the actual
    # mailbox uses a different mail_login, so try the mapped mailbox too.
    login_ok = False
    transport_error: Optional[str] = None
    for candidate_email in login_candidates:
        try:
            await loop.run_in_executor(None, lambda c=candidate_email: _imap_connect(c, body.password).logout())
            full_email = candidate_email
            login_ok = True
            break
        except imaplib.IMAP4.error as e:
            err_text = _imap_error_text(e)
            if _imap_error_is_auth_failure(e):
                continue
            transport_error = transport_error or err_text
            continue
        except imaplib.IMAP4.abort as e:
            transport_error = transport_error or _imap_error_text(e)
            continue
        except Exception as e:
            transport_error = transport_error or _imap_error_text(e)
            continue
    if not login_ok:
        hint_email = ""
        if len(login_candidates) >= 2:
            hinted = str(login_candidates[1] or "").strip().lower()
            if hinted and hinted != full_email.lower():
                hint_email = hinted
        if transport_error:
            payload: Dict[str, Any] = {
                "error": "Почтовый сервер временно недоступен. Это не похоже на неверный пароль.",
                "hint": "IMAP-путь сейчас не отвечает, поэтому webmail не может проверить вход. Попробуйте позже.",
                "imap_error": transport_error[:300],
                "retryable": True,
            }
            if hint_email:
                payload["resolved_login_hint"] = hint_email
            return JSONResponse(payload, status_code=503)
        rate_record_fail("webmail_login", ip)
        payload: Dict[str, Any] = {
            "error": "Неверный логин или пароль почтового ящика.",
        }
        if hint_email:
            payload["resolved_login_hint"] = hint_email
            payload["hint"] = f"Попробуйте войти как {hint_email} с паролем почтового ящика."
        return JSONResponse(payload, status_code=401)
    rate_reset("webmail_login", ip)

    sess_data = await _build_authenticated_session_payload(entered_local, full_email, body.password)
    token = session_create(sess_data)
    _schedule_portal_profile_refresh(sess_data, token)
    response.set_cookie(
        CFG.SESSION_COOKIE,
        token,
        max_age=CFG.SESSION_TTL if body.remember_me else None,
        httponly=True,
        samesite="lax",
        secure=urllib.parse.urlparse(CFG.MAIL_SSO_PUBLIC_URL).scheme == "https",
        path="/",
    )
    return {
        "ok": True,
        "email": full_email,
        "display_name": sess_data["display_name"],
        "mail_login": sess_data["mail_login"],
        "from_address": sess_data["from_address"],
        "profile_card": _mail_profile_snapshot(sess_data.get("profile_card")),
    }


@app.post("/api/sso/telegram-account/start")
async def api_sso_telegram_account_start(request: Request):
    sess = get_session_or_401(request)
    if not _mail_sso_telegram_account_handoff_allowed(sess):
        return JSONResponse(
            {"error": "Подключение Telegram доступно после нового входа через корпоративный SSO."},
            status_code=409,
        )

    session_token = str(request.cookies.get(CFG.SESSION_COOKIE) or "")
    subject = _mail_sso_subject(sess.get("sso_subject"))
    if not session_token or not subject:
        return JSONResponse({"error": "Сеанс устарел. Войдите повторно."}, status_code=401)

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = _mail_sso_pkce_verifier()
    sess["telegram_sso_account"] = {
        "state": state,
        "subject": subject,
        "session_hash": _mail_sso_session_hash(session_token),
        "expires": time.time() + CFG.MAIL_SSO_TELEGRAM_ACCOUNT_TTL,
    }
    if not _session_persist_if_active(session_token, sess):
        return JSONResponse({"error": "Сеанс устарел. Войдите повторно."}, status_code=401)

    response = JSONResponse(
        {
            "ok": True,
            "authorize_url": _mail_sso_authorize_url(
                request,
                state,
                nonce,
                code_verifier,
                prompt="none",
            ),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    for cookie_name, cookie_value in (
        (CFG.MAIL_SSO_TELEGRAM_ACCOUNT_STATE_COOKIE, state),
        (
            CFG.MAIL_SSO_TELEGRAM_ACCOUNT_PKCE_COOKIE,
            _mail_sso_pkce_cookie_value(state, code_verifier),
        ),
    ):
        response.set_cookie(
            cookie_name,
            cookie_value,
            max_age=CFG.MAIL_SSO_TELEGRAM_ACCOUNT_TTL,
            httponly=True,
            samesite="lax",
            secure=_mail_sso_cookie_secure(),
            path="/",
        )
    return response


async def _complete_mail_sso_telegram_account_callback(
    request: Request,
    code: str,
    state: str,
) -> Response:
    cookie_state = str(request.cookies.get(CFG.MAIL_SSO_TELEGRAM_ACCOUNT_STATE_COOKIE) or "")
    pkce_state, code_verifier = _mail_sso_pkce_cookie_parts(
        str(request.cookies.get(CFG.MAIL_SSO_TELEGRAM_ACCOUNT_PKCE_COOKIE) or "")
    )
    session_token = str(request.cookies.get(CFG.SESSION_COOKIE) or "")
    sess = session_get(session_token)
    transaction = (
        _mail_sso_telegram_account_transaction(sess, session_token, state)
        if sess is not None
        else None
    )
    if (
        not cookie_state
        or not _ct_eq(state, cookie_state)
        or not sess
        or not transaction
        or not code_verifier
        or not _ct_eq(state, pkce_state)
    ):
        return _mail_sso_clear_telegram_account_transaction(
            JSONResponse({"error": "SSO state mismatch"}, status_code=400),
            session_token=session_token,
            sess=sess,
        )

    if str(request.query_params.get("error") or "") or not code:
        return _mail_sso_clear_telegram_account_transaction(
            _mail_sso_telegram_account_result_redirect("reauth"),
            session_token=session_token,
            sess=sess,
        )

    loop = asyncio.get_event_loop()
    try:
        token_payload = await loop.run_in_executor(
            None,
            lambda: _mail_sso_post_form(
                CFG.MAIL_SSO_TOKEN_ENDPOINT,
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _mail_sso_redirect_uri(request),
                    "code_verifier": code_verifier,
                },
            ),
        )
        access_token = str(token_payload.get("access_token") or "")
        if not access_token:
            raise RuntimeError("sso access token missing")
        user = await loop.run_in_executor(None, lambda: _mail_sso_userinfo(access_token))
        returned_subject = _mail_sso_subject(user.get("sub"))
    except Exception:
        log.warning("mail Telegram SSO account preflight failed")
        return _mail_sso_clear_telegram_account_transaction(
            _mail_sso_telegram_account_result_redirect("error"),
            session_token=session_token,
            sess=sess,
        )

    if not returned_subject or not _ct_eq(returned_subject, str(transaction["subject"])):
        return _mail_sso_clear_telegram_account_transaction(
            _mail_sso_telegram_account_result_redirect("mismatch"),
            session_token=session_token,
            sess=sess,
        )

    response = JSONResponse({"ok": True}, status_code=302)
    response.headers["Location"] = _mail_sso_account_console_url()
    response.headers["Cache-Control"] = "no-store"
    return _mail_sso_clear_telegram_account_transaction(
        response,
        session_token=session_token,
        sess=sess,
    )


@app.get("/sso/login")
async def mail_sso_login(request: Request, response: Response):
    if not _mail_sso_enabled():
        return JSONResponse({"error": "SSO не настроен для webmail"}, status_code=503)
    provider = str(request.query_params.get("provider") or "").strip().lower()
    # `telegram=1` was the old Keycloak-style entry point that picked an upstream
    # IdP by alias. The central broker takes Telegram as a provider like any
    # other, so the legacy form is still accepted and mapped onto it.
    if not provider and str(request.query_params.get("telegram") or "") == "1":
        provider = "telegram"
    if provider and provider not in MAIL_SSO_PROVIDERS:
        return JSONResponse({"error": "Неизвестный провайдер входа."}, status_code=400)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = _mail_sso_pkce_verifier() if CFG.MAIL_SSO_PKCE_ENABLED else ""
    response = JSONResponse({"ok": False}, status_code=302)
    account_session_token = str(request.cookies.get(CFG.SESSION_COOKIE) or "")
    response = _mail_sso_clear_telegram_account_transaction(
        response,
        session_token=account_session_token,
        sess=session_get(account_session_token),
    )
    response.headers["Location"] = _mail_sso_authorize_url(
        request,
        state,
        nonce,
        code_verifier,
        provider=provider,
    )
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        CFG.MAIL_SSO_STATE_COOKIE,
        state,
        max_age=900,
        httponly=True,
        samesite="lax",
        secure=_mail_sso_cookie_secure(),
        path="/",
    )
    if code_verifier:
        response.set_cookie(
            CFG.MAIL_SSO_PKCE_COOKIE,
            _mail_sso_pkce_cookie_value(state, code_verifier),
            max_age=900,
            httponly=True,
            samesite="lax",
            secure=_mail_sso_cookie_secure(),
            path="/",
        )
    else:
        response.delete_cookie(
            CFG.MAIL_SSO_PKCE_COOKIE,
            path="/",
            secure=_mail_sso_cookie_secure(),
        )
    return response


@app.get("/sso/callback")
async def mail_sso_callback(request: Request, response: Response):
    code = str(request.query_params.get("code") or "")
    state = str(request.query_params.get("state") or "")
    telegram_account_state = str(
        request.cookies.get(CFG.MAIL_SSO_TELEGRAM_ACCOUNT_STATE_COOKIE) or ""
    )
    if telegram_account_state and _ct_eq(state, telegram_account_state):
        return await _complete_mail_sso_telegram_account_callback(request, code, state)
    cookie_state = str(request.cookies.get(CFG.MAIL_SSO_STATE_COOKIE) or "")
    pkce_state, code_verifier = _mail_sso_pkce_cookie_parts(
        str(request.cookies.get(CFG.MAIL_SSO_PKCE_COOKIE) or "")
    )
    if not code or not state or not cookie_state or not _ct_eq(state, cookie_state):
        return JSONResponse({"error": "SSO state mismatch"}, status_code=400)
    if CFG.MAIL_SSO_PKCE_ENABLED and (
        not code_verifier or not _ct_eq(state, pkce_state)
    ):
        return JSONResponse({"error": "SSO state mismatch"}, status_code=400)
    if not _mail_sso_enabled():
        return _mail_sso_clear_transaction(
            JSONResponse({"error": "SSO не настроен для webmail"}, status_code=503)
        )

    loop = asyncio.get_event_loop()
    try:
        token_request = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CFG.MAIL_SSO_CLIENT_ID,
            "redirect_uri": _mail_sso_redirect_uri(request),
        }
        if code_verifier and _ct_eq(state, pkce_state):
            token_request["code_verifier"] = code_verifier
        token_payload = await loop.run_in_executor(
            None,
            lambda: _mail_sso_post_form(
                CFG.MAIL_SSO_TOKEN_ENDPOINT,
                token_request,
            ),
        )
        access_token = str(token_payload.get("access_token") or "")
        if not access_token:
            raise RuntimeError("sso access token missing")
        user = await loop.run_in_executor(None, lambda: _mail_sso_userinfo(access_token))
        sso_subject = _mail_sso_subject(user.get("sub"))
        if not sso_subject:
            raise RuntimeError("sso user subject missing")
        # The binding registered from the personal cabinet is the only source of
        # truth for who may open which mailbox. No AD, no employee directory and
        # no foreign control plane on this path: mail signs in with the same
        # central identity as auth/my/help (ADR-0043).
        binding = await loop.run_in_executor(None, lambda: _sso_binding_lookup(sso_subject))
        if not binding:
            return _mail_sso_clear_transaction(
                JSONResponse(
                    {
                        "error": "Почта не настроена для этого аккаунта",
                        "hint": "Настройте почту в личном кабинете, раздел «Интеграции»",
                        "account_console": _mail_sso_account_console_url(),
                    },
                    status_code=403,
                )
            )
        full_email, password, imap_host, imap_port = binding
        sso_login = full_email.split("@", 1)[0]
        await loop.run_in_executor(
            None,
            lambda: _imap_connect(
                full_email, password, host=imap_host, port=imap_port
            ).logout(),
        )
        sess_data = await _build_authenticated_session_payload(
            sso_login,
            full_email,
            password,
            directory_lookup=False,
        )
        sess_data["auth_source"] = "sso"
        sess_data["sso_subject"] = sso_subject
        sess_data["sso_username"] = str(user.get("username") or "").strip().lower()
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("mail SSO callback failed: %s", exc)
        return _mail_sso_clear_transaction(
            JSONResponse({"error": "SSO вход не удался"}, status_code=502)
        )

    sid = session_create(sess_data)
    _schedule_portal_profile_refresh(sess_data, sid)
    redirect = JSONResponse({"ok": True}, status_code=302)
    redirect.headers["Location"] = "/"
    redirect.headers["Cache-Control"] = "no-store"
    redirect.set_cookie(
        CFG.SESSION_COOKIE,
        sid,
        max_age=CFG.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
        path="/",
    )
    return _mail_sso_clear_transaction(redirect)


@app.get("/admin/sso/login")
async def admin_sso_login(request: Request, response: Response):
    raise HTTPException(status_code=404, detail="Not Found")


async def _complete_admin_sso_callback(
    request: Request,
    code: str,
    *,
    use_mail_client: bool,
    state_cookie_path: str,
) -> Response:
    loop = asyncio.get_event_loop()
    try:
        if use_mail_client:
            token_payload = await loop.run_in_executor(
                None,
                lambda: _mail_sso_post_form(
                    CFG.MAIL_SSO_TOKEN_ENDPOINT,
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": _mail_sso_redirect_uri(request),
                    },
                ),
            )
        else:
            token_payload = await loop.run_in_executor(
                None,
                lambda: _admin_sso_post_form(
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": _admin_sso_redirect_uri(request),
                    }
                ),
            )
        access_token = str(token_payload.get("access_token") or "")
        if not access_token:
            raise RuntimeError("sso access token missing")
        if use_mail_client:
            user = await loop.run_in_executor(
                None, lambda: _mail_sso_userinfo(access_token)
            )
        else:
            user = await loop.run_in_executor(
                None, lambda: _admin_sso_userinfo(access_token)
            )
        if not _admin_sso_user_is_allowed(user):
            raise HTTPException(status_code=403, detail="Нет прав mail administrator")
        username = _admin_sso_username(user)
        if not username:
            raise HTTPException(status_code=403, detail="SSO login отсутствует")
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Mail admin SSO callback failed: %s", exc)
        return JSONResponse(
            {"error": "SSO вход в mail admin не удался"},
            status_code=502,
        )

    token = secrets.token_hex(32)
    admin_session = {
        "username": username,
        "ts": time.time(),
        "auth_source": "sso",
        "email": str(user.get("email") or ""),
        "profile_card": None,
    }
    _admin_sessions[token] = admin_session
    _schedule_portal_profile_refresh(
        admin_session,
        username_key="username",
        mail_snapshot=False,
    )
    redirect = JSONResponse({"ok": True}, status_code=302)
    redirect.headers["Location"] = "/admin"
    redirect.headers["Cache-Control"] = "no-store"
    redirect.set_cookie(
        ADMIN_PORTAL_COOKIE,
        token,
        max_age=ADMIN_PORTAL_TTL,
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
        path="/",
    )
    redirect.delete_cookie(
        CFG.MAILADMIN_SSO_STATE_COOKIE,
        path=state_cookie_path,
    )
    db_audit_log(
        username,
        "auth.login",
        "",
        {"source": "sso"},
        _client_ip(request),
        ok=True,
    )
    return redirect


@app.get("/admin/sso/callback")
async def admin_sso_callback(request: Request, response: Response):
    raise HTTPException(status_code=404, detail="Not Found")


@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    token = request.cookies.get(CFG.SESSION_COOKIE)
    sess = await asyncio.to_thread(session_get, token)
    return {"authenticated": bool(sess)}


@app.post("/api/auth/logout")
async def api_logout(request: Request, response: Response):
    if not _is_same_origin_admin_request(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    token = request.cookies.get(CFG.SESSION_COOKIE)
    session_delete(token)
    response.delete_cookie(CFG.SESSION_COOKIE, path="/")
    return {"ok": True}


def _mailbox_context_for_request(
    request: Request,
    sess: Dict[str, Any],
    *,
    require_send: bool = False,
    require_write: bool = False,
) -> Dict[str, Any]:
    raw_context = str(
        request.headers.get("X-Mailbox-Context")
        or request.query_params.get("mailbox_context")
        or "personal"
    ).strip().lower()
    if raw_context in {"", "personal"}:
        email_addr = str(
            sess.get("email")
            or sess.get("from_address")
            or sess.get("imap_user")
            or ""
        ).strip().lower()
        return {
            "key": "personal",
            "is_shared": False,
            "mailbox_id": None,
            "imap_user": sess["imap_user"],
            "imap_pass": sess["imap_pass"],
            "email": email_addr,
            "from_address": str(sess.get("from_address") or email_addr),
            "display_name": str(
                sess.get("display_name")
                or sess.get("mail_login")
                or email_addr
            ),
            "can_send": True,
        }
    if not raw_context.startswith("shared:"):
        raise HTTPException(status_code=400, detail="Некорректный контекст ящика")
    try:
        mailbox_id = int(raw_context.split(":", 1)[1])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректный контекст ящика")
    if mailbox_id <= 0:
        raise HTTPException(status_code=400, detail="Некорректный контекст ящика")
    creds = _shared_get_creds_for_user(
        mailbox_id,
        sess["imap_user"],
        str(sess.get("ad_login") or ""),
        str(sess.get("ad_domain") or ""),
    )
    if not creds:
        raise HTTPException(status_code=403, detail="Нет доступа к общему ящику")
    row, password = creds
    can_send = bool(row.get("can_send"))
    if (require_send or require_write) and not can_send:
        raise HTTPException(
            status_code=403,
            detail=(
                "Для этого общего ящика отправка запрещена"
                if require_send
                else "Общий ящик доступен только для чтения"
            ),
        )
    email_addr = str(row.get("email") or "").strip().lower()
    return {
        "key": f"shared:{mailbox_id}",
        "is_shared": True,
        "mailbox_id": mailbox_id,
        "imap_user": email_addr,
        "imap_pass": password,
        "email": email_addr,
        "from_address": email_addr,
        "display_name": str(row.get("display_name") or email_addr),
        "can_send": can_send,
    }


async def _mailbox_context_for_request_async(
    request: Request,
    sess: Dict[str, Any],
    *,
    require_send: bool = False,
    require_write: bool = False,
) -> Dict[str, Any]:
    requirements: Dict[str, bool] = {}
    if require_send:
        requirements["require_send"] = True
    if require_write:
        requirements["require_write"] = True
    return await asyncio.to_thread(
        _mailbox_context_for_request,
        request,
        sess,
        **requirements,
    )


def _mailbox_context_public(context: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": context["key"],
        "is_shared": bool(context["is_shared"]),
        "mailbox_id": context.get("mailbox_id"),
        "email": context["email"],
        "from_address": context["from_address"],
        "display_name": context["display_name"],
        "can_send": bool(context["can_send"]),
    }


@app.get("/api/me")
async def api_me(request: Request):
    sess = get_session_or_401(request)
    return {
        "ad_login": sess["ad_login"],
        "email": sess["email"],
        "display_name": sess["display_name"],
        "mail_login": sess["mail_login"],
        "from_address": sess["from_address"],
        "profile_card": _mail_profile_snapshot(sess.get("profile_card")),
        "telegram_sso_account_available": _mail_sso_telegram_account_handoff_allowed(sess),
    }


def _mail_account_unavailable() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "account_session_required",
            "detail": "Войдите через RuPochta ID, чтобы управлять личным кабинетом.",
        },
        status_code=409,
        headers={"Cache-Control": "no-store"},
    )


async def _mail_account_proxy(
    request: Request,
    path: str,
    *,
    method: str = "GET",
    body: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]] | JSONResponse:
    sess = get_session_or_401(request)
    if not _mail_account_available(sess):
        return _mail_account_unavailable()
    try:
        return await asyncio.to_thread(
            _portal_mail_account_request,
            sess,
            path,
            method=method,
            body=body,
            source=_client_ip(request),
        )
    except Exception as exc:
        log.warning("mail account proxy failed: %s", exc)
        return JSONResponse(
            {"ok": False, "error": "account_service_unavailable"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )


def _mail_account_response(result: Tuple[int, Dict[str, Any]]) -> JSONResponse:
    status, payload = result
    return JSONResponse(
        payload,
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/account")
async def api_account(request: Request):
    account_result = await _mail_account_proxy(request, "account")
    if isinstance(account_result, JSONResponse):
        return account_result
    if account_result[0] != 200:
        return _mail_account_response(account_result)
    profile_result = await _mail_account_proxy(request, "profile")
    if isinstance(profile_result, JSONResponse):
        return profile_result
    if profile_result[0] != 200:
        return _mail_account_response(profile_result)
    return JSONResponse(
        {
            "ok": True,
            "account": account_result[1].get("account") or {},
            "profile": profile_result[1].get("profile") or {},
        },
        headers={"Cache-Control": "no-store"},
    )


@app.patch("/api/account/profile")
async def api_account_profile(
    body: MailAccountProfileBody,
    request: Request,
):
    result = await _mail_account_proxy(
        request,
        "profile",
        method="PATCH",
        body=body.model_dump(exclude_none=True),
    )
    return result if isinstance(result, JSONResponse) else _mail_account_response(result)


@app.post("/api/account/password")
async def api_account_password(
    body: MailAccountPasswordBody,
    request: Request,
):
    result = await _mail_account_proxy(
        request,
        "account/password",
        method="POST",
        body=body.model_dump(exclude_none=True),
    )
    return result if isinstance(result, JSONResponse) else _mail_account_response(result)


@app.post("/api/account/identities/{provider}/unlink")
async def api_account_unlink(provider: str, request: Request):
    normalized = str(provider or "").strip().lower()
    if normalized == "yandex/mail":
        normalized = "yandexMail"
    if normalized not in {"telegram", "vk", "yandex", "yandexMail", "bitrix24"}:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
    result = await _mail_account_proxy(
        request,
        f"account/identities/{normalized}/unlink",
        method="POST",
        body={},
    )
    return result if isinstance(result, JSONResponse) else _mail_account_response(result)


@app.get("/api/bootstrap")
async def api_bootstrap(
    request: Request,
    folder: str = Query("INBOX"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    sess = get_session_or_401(request)
    if not _mail_profile_snapshot(sess.get("profile_card")):
        await _refresh_portal_profile_card(
            sess,
            token=str(request.cookies.get(CFG.SESSION_COOKIE) or ""),
        )
    context = await _mailbox_context_for_request_async(request, sess)
    cache_key = ("bootstrap", context["key"], folder, page, per_page)
    cached = _session_cache_get(sess, cache_key)
    if cached is not None:
        return cached
    loop = asyncio.get_event_loop()
    try:
        mailbox = await loop.run_in_executor(
            None,
            _imap_bootstrap_mailbox,
            context["imap_user"],
            context["imap_pass"],
            folder,
            page,
            per_page,
            "",
        )
        payload = {
            "me": {
                "ad_login": sess["ad_login"],
                "email": sess["email"],
                "display_name": sess["display_name"],
                "mail_login": sess["mail_login"],
                "from_address": sess["from_address"],
                "telegram_sso_account_available": _mail_sso_telegram_account_handoff_allowed(sess),
            },
            "profile": _profile_payload_from_session(sess),
            "active_mailbox": _mailbox_context_public(context),
            "folders": mailbox["folders"],
            "messages": mailbox["messages"],
        }
        _session_cache_put(sess, cache_key, payload, CFG.MESSAGES_CACHE_TTL)
        _session_cache_put(
            sess,
            ("folders", context["key"]),
            mailbox["folders"],
            CFG.FOLDERS_CACHE_TTL,
        )
        _session_cache_put(
            sess,
            (
                "messages",
                context["key"],
                folder,
                page,
                per_page,
                "",
                None,
                None,
                None,
            ),
            mailbox["messages"],
            CFG.MESSAGES_CACHE_TTL,
        )
        return payload
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/folders")
async def api_folders(request: Request):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(request, sess)
    cache_key = ("folders", context["key"])
    cached = _session_cache_get(sess, cache_key)
    if cached is not None:
        return {"folders": cached}
    loop = asyncio.get_event_loop()
    try:
        folders = await loop.run_in_executor(
            None,
            imap_list_folders,
            context["imap_user"],
            context["imap_pass"],
        )
        _session_cache_put(sess, cache_key, folders, CFG.FOLDERS_CACHE_TTL)
        return {"folders": folders}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


def _folder_is_used_by_enabled_rule(imap_user: str, folder: str) -> bool:
    target = str(folder or "").strip().casefold()
    if not target:
        return False
    with _db_connection() as con:
        rows = con.execute(
            "SELECT actions_json FROM sieve_rules "
            "WHERE imap_user = ? AND enabled = 1",
            (imap_user,),
        ).fetchall()
    for row in rows:
        try:
            actions = json.loads(row[0] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for action in actions:
            if (
                str(action.get("type") or "").lower() in {"move", "copy"}
                and str(action.get("value") or "").strip().casefold() == target
            ):
                return True
    return False


@app.post("/api/folders")
async def api_folder_create(body: FolderBody, request: Request):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_write=True,
    )
    try:
        name = _normalize_folder_name(body.name)
        folder = await asyncio.get_event_loop().run_in_executor(
            None,
            imap_create_folder,
            context["imap_user"],
            context["imap_pass"],
            name,
        )
        _session_cache_clear(sess, "folders")
        _session_cache_clear(sess, "bootstrap")
        return {"ok": True, "folder": folder}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.delete("/api/folders/{folder:path}")
async def api_folder_delete(folder: str, request: Request):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_write=True,
    )
    try:
        name = _normalize_folder_name(folder)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if (
        not context["is_shared"]
        and _folder_is_used_by_enabled_rule(context["imap_user"], name)
    ):
        return JSONResponse(
            {"error": "Папка используется включённым правилом"},
            status_code=409,
        )
    try:
        deleted = await asyncio.get_event_loop().run_in_executor(
            None,
            imap_delete_folder,
            context["imap_user"],
            context["imap_pass"],
            name,
        )
        _invalidate_mailbox_caches(sess)
        return {"ok": True, "folder": deleted}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


@app.get("/api/messages")
async def api_messages(
    request: Request,
    folder: str = Query("INBOX"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    q: str = Query("", max_length=300),
    unread: Optional[bool] = Query(None),
    flagged: Optional[bool] = Query(None),
    has_attachments: Optional[bool] = Query(None),
):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(request, sess)
    cache_key = (
        "messages",
        context["key"],
        folder,
        page,
        per_page,
        q or "",
        unread,
        flagged,
        has_attachments,
    )
    cached = _session_cache_get(sess, cache_key)
    if cached is not None:
        return cached
    loop = asyncio.get_event_loop()
    filter_tokens: List[str] = []
    if q.strip():
        filter_tokens.append(q.strip())
    if unread is True:
        filter_tokens.append("unread")
    elif unread is False:
        filter_tokens.append("read")
    if flagged is True:
        filter_tokens.append("flagged")
    elif flagged is False:
        filter_tokens.append("unflagged")
    if has_attachments is True:
        filter_tokens.append("has:attachment")
    elif has_attachments is False:
        filter_tokens.append("has:no-attachment")
    try:
        result = await loop.run_in_executor(
            None,
            imap_message_list,
            context["imap_user"],
            context["imap_pass"],
            folder,
            page,
            per_page,
            " ".join(filter_tokens).strip(),
        )
        _session_cache_put(sess, cache_key, result, CFG.MESSAGES_CACHE_TTL)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/messages/search")
async def api_messages_search(
    request: Request,
    folder: str = Query("INBOX"),
    q: str = Query("", max_length=300),
    limit: int = Query(50, ge=1, le=200),
):
    """Search messages in `folder` matching query `q` (SUBJECT/FROM/TO/TEXT)."""
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(request, sess)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            imap_search_messages,
            context["imap_user"],
            context["imap_pass"],
            folder,
            q,
            limit,
        )
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/messages/bulk")
async def api_messages_bulk(body: BulkActionBody, request: Request):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_write=True,
    )
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            imap_bulk_action,
            context["imap_user"],
            context["imap_pass"],
            body.from_folder,
            body.uids,
            body.action,
            body.to_folder,
        )
        _invalidate_mailbox_caches(sess)
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/messages/undo")
async def api_messages_undo(body: UndoMoveBody, request: Request):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_write=True,
    )
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            imap_undo_move,
            context["imap_user"],
            context["imap_pass"],
            body.from_folder,
            body.to_folder,
            body.message_ids,
        )
        _invalidate_mailbox_caches(sess)
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/messages/{uid}")
async def api_message(uid: str, request: Request, folder: str = Query("INBOX")):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(request, sess)
    cache_key = ("message", context["key"], folder, uid)
    cached = _session_cache_get(sess, cache_key)
    if cached is not None:
        return cached
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            imap_fetch_message,
            context["imap_user"],
            context["imap_pass"],
            folder,
            uid,
            bool(context["can_send"]),
        )
        _session_cache_put(sess, cache_key, result, CFG.MESSAGE_DETAIL_CACHE_TTL)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/messages/{uid}/attachment/{idx}")
async def api_message_attachment(
    uid: str,
    idx: int,
    request: Request,
    folder: str = Query("INBOX"),
):
    """Download an attachment by its index (from the attachments[] list in
    /api/messages/{uid})."""
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(request, sess)
    loop = asyncio.get_event_loop()
    try:
        att = await loop.run_in_executor(
            None,
            imap_fetch_attachment,
            context["imap_user"],
            context["imap_pass"],
            folder,
            uid,
            idx,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    if not att:
        return JSONResponse({"error": "Attachment not found"}, status_code=404)
    # Build filename header — RFC 5987 for non-ASCII
    filename = att["filename"] or f"attachment-{idx}"
    safe_ascii = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    quoted_utf8 = urllib.parse.quote(filename, safe="")
    disp = f'attachment; filename="{safe_ascii}"; filename*=UTF-8\'\'{quoted_utf8}'
    return Response(
        content=att["content"],
        media_type=att["content_type"] or "application/octet-stream",
        headers={"Content-Disposition": disp},
    )


@app.post("/api/messages/{uid}/invite/rsvp")
async def api_message_invite_rsvp(
    uid: str,
    body: InviteRsvpBody,
    request: Request,
):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_send=True,
    )
    if context["is_shared"]:
        return JSONResponse(
            {"error": "Ответ на приглашение доступен только в личном ящике"},
            status_code=409,
        )
    loop = asyncio.get_event_loop()
    try:
        message = await loop.run_in_executor(
            None,
            imap_fetch_message,
            context["imap_user"],
            context["imap_pass"],
            body.folder,
            uid,
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    invite = message.get("invite") or {}
    if not invite or not invite.get("uid"):
        return JSONResponse({"error": "Invite not found in this message"}, status_code=400)
    if str(invite.get("method") or "").upper() == "CANCEL":
        return JSONResponse({"error": "Cannot RSVP to cancelled invite"}, status_code=400)

    organizer_email = parseaddr(str((invite.get("organizer") or {}).get("email") or ""))[1]
    if not organizer_email:
        organizer_email = parseaddr(str(message.get("from") or ""))[1]
    if not organizer_email:
        return JSONResponse({"error": "Organizer email is missing"}, status_code=400)

    attendee = _invite_find_attendee(
        invite,
        [context.get("from_address"), context.get("email")],
    )
    attendee_email = str(
        (attendee or {}).get("email") or context.get("from_address") or ""
    ).strip()
    attendee_name = str(
        (attendee or {}).get("name")
        or context.get("display_name")
        or attendee_email
    ).strip()
    response_key = body.response.lower()
    response_title = {
        "accepted": "Accepted",
        "tentative": "Tentative",
        "declined": "Declined",
    }.get(response_key, "Replied")
    summary = str(invite.get("summary") or message.get("subject") or "Meeting").strip() or "Meeting"
    body_text = f'{attendee_name} {response_title.lower()} invite "{summary}".'
    body_html = _plain_text_to_html(body_text)
    attachment = _build_invite_reply_attachment(invite, attendee_name, attendee_email, response_key)
    from_addr = formataddr((context["display_name"], context["from_address"]))

    try:
        await loop.run_in_executor(
            None,
            lambda: smtp_send(
                context["imap_user"],
                context["imap_pass"],
                from_addr,
                [organizer_email],
                [],
                [],
                f"{response_title}: {summary}",
                body_html,
                body_text,
                message.get("message_id"),
                [attachment],
            ),
        )
    except Exception as e:
        return JSONResponse({"error": f"SMTP: {e}"}, status_code=502)

    # On Accept/Tentative: also save the event to the user's CalDAV calendar
    # so it shows up in the calendar view. Decline skips this.
    calendar_saved = False
    if response_key in ("accepted", "tentative"):
        try:
            event_to_save = {
                "uid": invite.get("uid") or "",
                "summary": invite.get("summary") or message.get("subject") or "Meeting",
                "description": invite.get("description") or "",
                "location": invite.get("location") or "",
                "start": (invite.get("start") or {}).get("iso") or "",
                "end": (invite.get("end") or {}).get("iso") or "",
                "attendees": [
                    {"email": a.get("email"), "name": a.get("name", "")}
                    for a in (invite.get("attendees") or [])
                    if a.get("email")
                ],
            }
            if event_to_save["start"] and event_to_save["end"]:
                ok, _ = await loop.run_in_executor(
                    None, _caldav_put_event, sess, event_to_save
                )
                calendar_saved = ok
        except Exception as e:
            log.warning("Could not save invite to calendar: %s", e)

    return {
        "ok": True,
        "response": response_key,
        "organizer": organizer_email,
        "attendee": attendee_email,
        "calendar_saved": calendar_saved,
    }


@app.post("/api/messages/send")
async def api_send(request: Request):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_send=True,
    )
    payload = await _parse_compose_request(request, draft_mode=False)
    to = parse_address_list(payload.get("to"))
    cc = parse_address_list(payload.get("cc") or "")
    bcc = parse_address_list(payload.get("bcc") or "")
    if not to:
        return JSONResponse({"error": "Поле 'Кому' не должно быть пустым"}, status_code=400)
    subject = str(payload.get("subject") or "").strip() or "(без темы)"
    body_html, body_text = _compose_body_variants(
        str(payload.get("body") or ""),
        payload.get("body_text"),
    )

    from_addr = formataddr((context["display_name"], context["from_address"]))
    loop = asyncio.get_event_loop()
    try:
        send_result = await loop.run_in_executor(
            None,
            lambda: smtp_send(
                context["imap_user"],
                context["imap_pass"],
                from_addr,
                to,
                cc,
                bcc,
                subject,
                body_html,
                body_text,
                payload.get("in_reply_to"),
                payload.get("attachments"),
            ),
        )
        draft_deleted = None
        warnings: List[str] = []
        if payload.get("draft_uid"):
            try:
                await loop.run_in_executor(
                    None,
                    imap_delete_message,
                    context["imap_user"],
                    context["imap_pass"],
                    "Drafts",
                    str(payload["draft_uid"]),
                )
                draft_deleted = True
            except Exception as e:
                log.info("send cleanup draft %s failed: %s", payload["draft_uid"], e)
                draft_deleted = False
                warnings.append("Черновик не удалось удалить автоматически")
        if not send_result.get("sent_copy_ok"):
            warnings.append("SMTP ok, но копия в «Отправленные» не сохранена")
        if context["is_shared"]:
            db_audit_log(
                sess["imap_user"],
                "shared.send",
                context["email"],
                details={
                    "to": to,
                    "subject": subject,
                    "mailbox_id": context["mailbox_id"],
                },
                ip=_client_ip(request),
            )
        _invalidate_mailbox_caches(sess)
        return {
            "ok": True,
            "smtp_ok": bool(send_result.get("smtp_ok")),
            "smtp_status": send_result.get("smtp_status") or "submitted",
            "recipient_count": int(send_result.get("recipient_count") or 0),
            "sent_copy_ok": bool(send_result.get("sent_copy_ok")),
            "sent_folder": send_result.get("sent_folder") or "",
            "sent_copy_status": send_result.get("sent_copy_status") or "",
            "draft_deleted": draft_deleted,
            "warnings": warnings,
        }
    except Exception as e:
        queue_id = 0
        mail_sess = {
            **sess,
            "imap_user": context["imap_user"],
            "imap_pass": context["imap_pass"],
        }
        if not context["is_shared"]:
            try:
                queue_id = _queue_failed_immediate_send(
                    sess=mail_sess,
                    from_addr=from_addr,
                    to=to,
                    cc=cc,
                    bcc=bcc,
                    subject=subject,
                    body_html=body_html,
                    body_text=body_text,
                    in_reply_to=payload.get("in_reply_to"),
                    attachments=payload.get("attachments"),
                    error_text=f"SMTP: {e}",
                )
            except Exception as queue_exc:
                log.warning(
                    "failed to persist immediate send error for %s: %s",
                    context["imap_user"],
                    queue_exc,
                )
        return JSONResponse(
            {
                "error": f"SMTP: {e}",
                "queued_failed_send": bool(queue_id),
                "queue_id": queue_id,
                "retry_available": bool(queue_id),
            },
            status_code=502,
        )


@app.post("/api/messages/draft")
async def api_save_draft(request: Request):
    """Save a draft message to the IMAP Drafts folder.

    If `replace_uid` is provided, the previous draft is removed atomically
    after the new one is appended (used for autosave updates).
    """
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_send=True,
    )
    payload = await _parse_compose_request(request, draft_mode=True)
    to = parse_address_list(payload.get("to") or "")
    cc = parse_address_list(payload.get("cc") or "")
    bcc = parse_address_list(payload.get("bcc") or "")
    from_addr = formataddr((context["display_name"], context["from_address"]))
    body_html, body_text = _compose_body_variants(
        str(payload.get("body") or ""),
        payload.get("body_text"),
    )
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: imap_save_draft(
                context["imap_user"],
                context["imap_pass"],
                from_addr,
                to,
                cc,
                bcc,
                str(payload.get("subject") or ""),
                body_html,
                body_text,
                payload.get("in_reply_to"),
                payload.get("replace_uid"),
                payload.get("attachments"),
            ),
        )
        _invalidate_mailbox_caches(sess)
        return {"ok": True, **result}
    except Exception as e:
        return JSONResponse({"error": f"Drafts: {e}"}, status_code=502)


@app.post("/api/messages/{uid}/flags")
async def api_message_flags(uid: str, body: MessageFlagsBody, request: Request):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_write=True,
    )
    loop = asyncio.get_event_loop()
    if body.seen is None and body.flagged is None:
        return JSONResponse({"error": "Nothing to update"}, status_code=400)
    try:
        result = await loop.run_in_executor(
            None,
            imap_update_flags,
            context["imap_user"],
            context["imap_pass"],
            body.folder,
            uid,
            body.seen,
            body.flagged,
        )
        _invalidate_mailbox_caches(sess)
        return {"ok": True, "message": result}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.delete("/api/messages/{uid}")
async def api_delete(uid: str, request: Request, folder: str = Query("INBOX")):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_write=True,
    )
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            imap_delete_message,
            context["imap_user"],
            context["imap_pass"],
            folder,
            uid,
        )
        _invalidate_mailbox_caches(sess)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/messages/{uid}/move")
async def api_move(uid: str, body: MoveBody, request: Request):
    sess = get_session_or_401(request)
    context = await _mailbox_context_for_request_async(
        request,
        sess,
        require_write=True,
    )
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            imap_move_message,
            context["imap_user"],
            context["imap_pass"],
            body.from_folder,
            body.to_folder,
            uid,
        )
        _invalidate_mailbox_caches(sess)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


# ---------------------------------------------------------------------------
# External mailbox provider presets (BYOM, ADR-0071)
# ---------------------------------------------------------------------------

MAIL_PROVIDER_PRESETS: Dict[str, Dict[str, Any]] = {
    "yandex": {
        "name": "Яндекс",
        "imap_host": "imap.yandex.ru",
        "imap_port": 993,
        "smtp_host": "smtp.yandex.ru",
        "smtp_port": 465,
    },
    "yandex360": {
        "name": "Яндекс 360",
        "imap_host": "imap.yandex.com",
        "imap_port": 993,
        "smtp_host": "smtp.yandex.com",
        "smtp_port": 465,
    },
    "mailru": {
        "name": "Mail.ru",
        "imap_host": "imap.mail.ru",
        "imap_port": 993,
        "smtp_host": "smtp.mail.ru",
        "smtp_port": 465,
    },
    "vk_workspace": {
        "name": "VK WorkSpace",
        "imap_host": "mail.vk.works",
        "imap_port": 993,
        "smtp_host": "mail.vk.works",
        "smtp_port": 465,
    },
    "custom": {
        "name": "Свой IMAP",
        "imap_host": None,
        "imap_port": None,
        "smtp_host": None,
        "smtp_port": None,
    },
}


# ---------------------------------------------------------------------------
# Managed group, distribution, service, and technical addresses
# ---------------------------------------------------------------------------

TECHNICAL_MAILBOX_PRESETS: Dict[str, Dict[str, str]] = {
    "bounce": {
        "email": f"bounces@{CFG.MAIL_DOMAIN}",
        "display_name": "Сборщик недоставленных сообщений",
        "description": "Приём и разбор отчётов о недоставке и возвратов SMTP.",
    },
    "noreply": {
        "email": f"noreply@{CFG.MAIL_DOMAIN}",
        "display_name": "No Reply",
        "description": "Исходящие автоматические уведомления без ручной переписки.",
    },
    "support": {
        "email": f"support@{CFG.MAIL_DOMAIN}",
        "display_name": "Техническая поддержка",
        "description": "Входящие обращения и исходящие ответы технической поддержки.",
    },
    "alerts": {
        "email": f"alerts@{CFG.MAIL_DOMAIN}",
        "display_name": "Системные алерты",
        "description": "Приём и отправка инфраструктурных и сервисных оповещений.",
    },
}


class ManagedAddressMember(BaseModel):
    email: str
    can_send: bool = True


class ManagedAddressGroup(BaseModel):
    dn: str
    name: str = ""
    can_send: bool = True


class ManagedAddressBody(BaseModel):
    kind: str
    technical_role: str = ""
    email: str
    revision: Optional[int] = None
    display_name: str = ""
    company: str = "Corp"
    directory_domain: str = ""
    description: str = ""
    responsible_user: str = ""
    members: list[ManagedAddressMember] = Field(default_factory=list)
    ad_groups: list[ManagedAddressGroup] = Field(default_factory=list)
    recipients: list[str] = Field(default_factory=list)


_managed_address_locks_guard = threading.Lock()
_managed_address_locks: Dict[Tuple[int, str], asyncio.Lock] = {}


def _managed_address_operation_lock(address_key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    key = (id(loop), str(address_key or "").strip().lower())
    with _managed_address_locks_guard:
        lock = _managed_address_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _managed_address_locks[key] = lock
        return lock


if hasattr(ManagedAddressBody, "model_rebuild"):
    ManagedAddressBody.model_rebuild(
        _types_namespace={
            "ManagedAddressMember": ManagedAddressMember,
            "ManagedAddressGroup": ManagedAddressGroup,
            "Optional": Optional,
        }
    )
else:  # pragma: no cover - Pydantic v1 compatibility
    ManagedAddressBody.update_forward_refs(
        ManagedAddressMember=ManagedAddressMember,
        ManagedAddressGroup=ManagedAddressGroup,
    )


def _is_mail_domain_address(email_addr: str) -> bool:
    if not _EMAIL_RE.match(email_addr):
        return False
    return email_addr.rsplit("@", 1)[1].lower() == CFG.MAIL_DOMAIN.lower()


def _validate_managed_address_body(body: ManagedAddressBody) -> Dict[str, Any]:
    kind = body.kind.strip().lower()
    if kind not in {"group", "distribution", "service", "technical"}:
        raise HTTPException(400, "Неизвестный тип управляемого адреса")

    email_addr = body.email.strip().lower()
    if not _is_mail_domain_address(email_addr):
        raise HTTPException(
            400,
            f"Адрес должен принадлежать {CFG.MAIL_DOMAIN}",
        )
    company = _canonical_company(body.company)
    if not company:
        raise HTTPException(400, "Неизвестная компания")
    directory_domain = _directory_domain_for_company(company)
    requested_domain = str(body.directory_domain or "").strip().lower()
    if requested_domain and requested_domain != directory_domain:
        raise HTTPException(400, "Компания не соответствует домену каталога")

    members_by_email: Dict[str, Dict[str, Any]] = {}
    for member in body.members:
        member_email = member.email.strip().lower()
        if not _is_mail_domain_address(member_email):
            raise HTTPException(
                400,
                "Участник должен иметь корпоративный email",
            )
        members_by_email[member_email] = {
            "email": member_email,
            "can_send": bool(member.can_send),
        }

    groups_by_dn: Dict[str, Dict[str, Any]] = {}
    for group in body.ad_groups:
        group_dn = group.dn.strip()
        if not group_dn:
            raise HTTPException(400, "Некорректная группа AD")
        group_domain = _directory_domain_from_dn(group_dn)
        if group_domain and group_domain != directory_domain:
            raise HTTPException(400, "Группа AD относится к другой компании")
        groups_by_dn[group_dn.casefold()] = {
            "dn": group_dn,
            "name": group.name.strip() or _ldap_dn_common_name(group_dn),
            "can_send": bool(group.can_send),
        }

    recipients = list(
        dict.fromkeys(
            item.strip().lower()
            for item in body.recipients
            if item.strip()
        )
    )
    if any(not _EMAIL_RE.match(item) for item in recipients):
        raise HTTPException(400, "Некорректный получатель рассылки")

    technical_role = body.technical_role.strip().lower()
    description = body.description.strip()
    responsible_user = body.responsible_user.strip().lower()
    if kind == "technical":
        preset = TECHNICAL_MAILBOX_PRESETS.get(technical_role)
        if not preset:
            raise HTTPException(400, "Неизвестная роль технического ящика")
        if email_addr != preset["email"]:
            raise HTTPException(
                400,
                "Email не соответствует роли технического ящика",
            )
    elif technical_role:
        raise HTTPException(
            400,
            "Техническая роль допустима только для технического ящика",
        )
    if kind == "distribution":
        if members_by_email or groups_by_dn or not recipients:
            raise HTTPException(
                400,
                "Рассылке нужны получатели, но не участники или группы AD",
            )
    elif recipients:
        raise HTTPException(
            400,
            "Получатели допустимы только для рассылки",
        )
    if kind in {"service", "technical"} and (
        not responsible_user or not description
    ):
        raise HTTPException(
            400,
            "Сервисному или техническому ящику нужны ответственный и описание",
        )

    return {
        "kind": kind,
        "technical_role": technical_role,
        "email": email_addr,
        "revision": body.revision,
        "display_name": body.display_name.strip(),
        "company": company,
        "directory_domain": directory_domain,
        "description": description,
        "responsible_user": responsible_user,
        "members": list(members_by_email.values()),
        "ad_groups": list(groups_by_dn.values()),
        "recipients": recipients,
    }


def _validate_managed_address_ad_groups(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    selected = payload.get("ad_groups") or []
    if not selected:
        return payload
    try:
        catalog = directory_load_groups()
    except Exception as exc:
        raise HTTPException(503, "Каталог групп AD временно недоступен") from exc
    by_dn = {
        str(group.get("dn") or "").casefold(): group
        for group in catalog
        if str(group.get("dn") or "").strip()
    }
    normalized: List[Dict[str, Any]] = []
    for selected_group in selected:
        group = by_dn.get(str(selected_group.get("dn") or "").casefold())
        if not group:
            raise HTTPException(
                400,
                f"Группа AD не найдена: {selected_group.get('dn') or ''}",
            )
        normalized.append(
            {
                "dn": group["dn"],
                "name": group["name"],
                "can_send": bool(selected_group.get("can_send")),
            }
        )
    return {**payload, "ad_groups": normalized}


def _managed_address_row_public(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "revision": int(row["revision"] or 1),
        "kind": str(row["kind"] or "group"),
        "technical_role": str(row["technical_role"] or ""),
        "email": str(row["email"] or ""),
        "display_name": str(row["display_name"] or ""),
        "company": str(row["company"] or "Corp"),
        "directory_domain": str(row["directory_domain"] or "corp.local"),
        "description": str(row["description"] or ""),
        "responsible_user": str(row["responsible_user"] or ""),
        "status": str(row["status"] or "active"),
        "last_error": str(row["last_error"] or ""),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def _managed_address_select_projection(con: sqlite3.Connection) -> str:
    columns = {
        str(row[1])
        for row in con.execute("PRAGMA table_info(shared_mailboxes)").fetchall()
    }
    company = "company" if "company" in columns else "'Corp' AS company"
    directory_domain = (
        "directory_domain"
        if "directory_domain" in columns
        else "'corp.local' AS directory_domain"
    )
    revision = "revision" if "revision" in columns else "1 AS revision"
    technical_role = (
        "technical_role"
        if "technical_role" in columns
        else "'' AS technical_role"
    )
    return (
        f"id, kind, {technical_role}, email, display_name, "
        f"{company}, {directory_domain}, {revision}, "
        "description, responsible_user, status, last_error, "
        "created_at, updated_at"
    )


def _managed_address_attach_relations(
    con: sqlite3.Connection,
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not items:
        return items
    by_id = {int(item["id"]): item for item in items}
    placeholders = ",".join("?" for _ in by_id)
    for item in items:
        item["members"] = []
        item["ad_groups"] = []
        item["recipients"] = []
    member_rows = con.execute(
        "SELECT mailbox_id, member_user, can_send "
        f"FROM shared_mailbox_members WHERE mailbox_id IN ({placeholders}) "
        "ORDER BY member_user",
        tuple(by_id),
    ).fetchall()
    for row in member_rows:
        item = by_id.get(int(row["mailbox_id"]))
        if item is not None:
            item["members"].append(
                {
                    "email": str(row["member_user"]),
                    "can_send": bool(row["can_send"]),
                }
            )
    group_rows = con.execute(
        "SELECT mailbox_id, group_dn, group_name, can_send "
        f"FROM shared_mailbox_ad_groups WHERE mailbox_id IN ({placeholders}) "
        "ORDER BY group_name COLLATE NOCASE, group_dn COLLATE NOCASE",
        tuple(by_id),
    ).fetchall()
    for row in group_rows:
        item = by_id.get(int(row["mailbox_id"]))
        if item is not None:
            item["ad_groups"].append(
                {
                    "dn": str(row["group_dn"]),
                    "name": str(row["group_name"]),
                    "can_send": bool(row["can_send"]),
                }
            )
    recipient_rows = con.execute(
        "SELECT mailbox_id, recipient_email "
        f"FROM shared_mailbox_recipients WHERE mailbox_id IN ({placeholders}) "
        "ORDER BY recipient_email",
        tuple(by_id),
    ).fetchall()
    for row in recipient_rows:
        item = by_id.get(int(row["mailbox_id"]))
        if item is not None:
            item["recipients"].append(str(row["recipient_email"]))
    return items


def _managed_address_get(mailbox_id: int) -> Optional[Dict[str, Any]]:
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT "
            + _managed_address_select_projection(con)
            + " FROM shared_mailboxes WHERE id = ?",
            (mailbox_id,),
        ).fetchone()
        if not row:
            return None
        return _managed_address_attach_relations(
            con,
            [_managed_address_row_public(row)],
        )[0]


def _managed_address_list() -> List[Dict[str, Any]]:
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT "
            + _managed_address_select_projection(con)
            + " FROM shared_mailboxes ORDER BY email"
        ).fetchall()
        return _managed_address_attach_relations(
            con,
            [_managed_address_row_public(row) for row in rows],
        )


def _managed_address_find_by_email(
    email_addr: str,
) -> Optional[Dict[str, Any]]:
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT "
            + _managed_address_select_projection(con)
            + " FROM shared_mailboxes WHERE lower(email) = lower(?)",
            (email_addr,),
        ).fetchone()
        if not row:
            return None
        return _managed_address_attach_relations(
            con,
            [_managed_address_row_public(row)],
        )[0]


def _reject_generic_managed_address_operation(
    email_addr: str,
    operation: str,
) -> None:
    managed = _managed_address_find_by_email(email_addr)
    if managed:
        raise HTTPException(
            409,
            f"{operation} выполняется в разделе «Общие адреса»",
        )


def _managed_address_insert(
    payload: Dict[str, Any],
    imap_pass_enc: str,
) -> int:
    with _db_connection() as con:
        cur = con.execute(
            """
            INSERT INTO shared_mailboxes (
                kind, technical_role, email, display_name,
                company, directory_domain,
                description, responsible_user,
                status, last_error, imap_pass_enc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', '', ?)
            """,
            (
                payload["kind"],
                payload.get("technical_role") or "",
                payload["email"],
                payload["display_name"],
                payload.get("company") or "Corp",
                payload.get("directory_domain") or "corp.local",
                payload["description"],
                payload["responsible_user"],
                imap_pass_enc,
            ),
        )
        mailbox_id = int(cur.lastrowid)
        for member in payload.get("members") or []:
            con.execute(
                """
                INSERT INTO shared_mailbox_members (
                    mailbox_id, member_user, can_send
                )
                VALUES (?, ?, ?)
                """,
                (
                    mailbox_id,
                    member["email"],
                    1 if member["can_send"] else 0,
                ),
            )
        for group in payload.get("ad_groups") or []:
            con.execute(
                """
                INSERT INTO shared_mailbox_ad_groups (
                    mailbox_id, group_dn, group_name, can_send
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    mailbox_id,
                    group["dn"],
                    group["name"],
                    1 if group["can_send"] else 0,
                ),
            )
        for recipient in payload.get("recipients") or []:
            con.execute(
                """
                INSERT INTO shared_mailbox_recipients (
                    mailbox_id, recipient_email
                )
                VALUES (?, ?)
                """,
                (mailbox_id, recipient),
            )
        con.commit()
        return mailbox_id


def _managed_address_update_metadata(
    mailbox_id: int,
    payload: Dict[str, Any],
) -> None:
    try:
        expected_revision = int(payload.get("revision") or 0)
    except (TypeError, ValueError):
        expected_revision = 0
    if expected_revision <= 0:
        raise HTTPException(
            409,
            "Данные общего адреса устарели. Обновите страницу и повторите.",
        )
    with _db_connection() as con:
        cur = con.execute(
            """
            UPDATE shared_mailboxes
            SET display_name = ?,
                technical_role = ?,
                company = ?,
                directory_domain = ?,
                description = ?,
                responsible_user = ?,
                status = 'active',
                last_error = '',
                updated_at = datetime('now'),
                revision = revision + 1
            WHERE id = ? AND revision = ?
            """,
            (
                payload["display_name"],
                payload.get("technical_role") or "",
                payload.get("company") or "Corp",
                payload.get("directory_domain") or "corp.local",
                payload["description"],
                payload["responsible_user"],
                mailbox_id,
                expected_revision,
            ),
        )
        if cur.rowcount != 1:
            exists = con.execute(
                "SELECT 1 FROM shared_mailboxes WHERE id = ?",
                (mailbox_id,),
            ).fetchone()
            if not exists:
                raise KeyError(f"managed address {mailbox_id} not found")
            raise HTTPException(
                409,
                "Состав общего адреса уже изменён. Обновите страницу и повторите.",
            )
        con.execute(
            "DELETE FROM shared_mailbox_members WHERE mailbox_id = ?",
            (mailbox_id,),
        )
        con.execute(
            "DELETE FROM shared_mailbox_ad_groups WHERE mailbox_id = ?",
            (mailbox_id,),
        )
        con.execute(
            "DELETE FROM shared_mailbox_recipients WHERE mailbox_id = ?",
            (mailbox_id,),
        )
        for member in payload.get("members") or []:
            con.execute(
                """
                INSERT INTO shared_mailbox_members (
                    mailbox_id, member_user, can_send
                )
                VALUES (?, ?, ?)
                """,
                (
                    mailbox_id,
                    member["email"],
                    1 if member["can_send"] else 0,
                ),
            )
        for group in payload.get("ad_groups") or []:
            con.execute(
                """
                INSERT INTO shared_mailbox_ad_groups (
                    mailbox_id, group_dn, group_name, can_send
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    mailbox_id,
                    group["dn"],
                    group["name"],
                    1 if group["can_send"] else 0,
                ),
            )
        for recipient in payload.get("recipients") or []:
            con.execute(
                """
                INSERT INTO shared_mailbox_recipients (
                    mailbox_id, recipient_email
                )
                VALUES (?, ?)
                """,
                (mailbox_id, recipient),
            )
        con.commit()


def _managed_address_get_encrypted_password(mailbox_id: int) -> str:
    with _db_connection() as con:
        row = con.execute(
            "SELECT imap_pass_enc FROM shared_mailboxes WHERE id = ?",
            (mailbox_id,),
        ).fetchone()
    return str(row[0] or "") if row else ""


def _managed_address_set_encrypted_password(
    mailbox_id: int,
    encrypted_password: str,
) -> None:
    with _db_connection() as con:
        cur = con.execute(
            """
            UPDATE shared_mailboxes
            SET imap_pass_enc = ?,
                status = 'active',
                last_error = '',
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (encrypted_password, mailbox_id),
        )
        if cur.rowcount != 1:
            raise KeyError(f"managed address {mailbox_id} not found")
        con.commit()


def _managed_address_mark_error(mailbox_id: int, error: str) -> None:
    with _db_connection() as con:
        con.execute(
            """
            UPDATE shared_mailboxes
            SET status = 'error',
                last_error = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (str(error or "")[:1000], mailbox_id),
        )
        con.commit()


def _managed_address_clear_error(mailbox_id: int) -> None:
    with _db_connection() as con:
        cur = con.execute(
            """
            UPDATE shared_mailboxes
            SET status = 'active',
                last_error = '',
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (mailbox_id,),
        )
        if cur.rowcount != 1:
            raise KeyError(f"managed address {mailbox_id} not found")
        con.commit()


def _managed_address_delete_metadata(mailbox_id: int) -> None:
    with _db_connection() as con:
        cur = con.execute(
            "DELETE FROM shared_mailboxes WHERE id = ?",
            (mailbox_id,),
        )
        if cur.rowcount != 1:
            raise KeyError(f"managed address {mailbox_id} not found")
        con.execute(
            "DELETE FROM shared_mailbox_members WHERE mailbox_id = ?",
            (mailbox_id,),
        )
        con.execute(
            "DELETE FROM shared_mailbox_ad_groups WHERE mailbox_id = ?",
            (mailbox_id,),
        )
        con.execute(
            "DELETE FROM shared_mailbox_recipients WHERE mailbox_id = ?",
            (mailbox_id,),
        )
        con.commit()


def _ensure_managed_operation_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_address_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            mailbox_id INTEGER,
            email TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            secret_enc TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_managed_address_operations_status "
        "ON managed_address_operations(status, id)"
    )
    columns = {
        str(row[1])
        for row in con.execute(
            "PRAGMA table_info(managed_address_operations)"
        ).fetchall()
    }
    if "attempts" not in columns:
        con.execute(
            "ALTER TABLE managed_address_operations "
            "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )


def _managed_operation_start(
    action: str,
    email: str,
    payload: Dict[str, Any],
    *,
    mailbox_id: Optional[int] = None,
    secret_enc: str = "",
) -> int:
    with _db_connection() as con:
        _ensure_managed_operation_table(con)
        cur = con.execute(
            """
            INSERT INTO managed_address_operations (
                action, mailbox_id, email, payload_json, secret_enc, status
            )
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (
                action,
                mailbox_id,
                str(email or "").strip().lower(),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                secret_enc,
            ),
        )
        con.commit()
        return int(cur.lastrowid)


def _managed_owner_release_or_queue(payload: Dict[str, Any]) -> None:
    try:
        _panel_manage_mail_login_owner(
            payload["email"].split("@", 1)[0],
            payload["email"],
            action="release",
        )
    except Exception as exc:
        _managed_operation_start(
            "release",
            payload["email"],
            payload,
        )
        log.error(
            "managed address owner release queued for %s: %s",
            payload["email"],
            exc,
        )


def _managed_operation_update(
    operation_id: int,
    status: str,
    *,
    error: str = "",
    mailbox_id: Optional[int] = None,
    secret_enc: Optional[str] = None,
) -> None:
    fields = [
        "status = ?",
        "last_error = ?",
        "updated_at = datetime('now')",
    ]
    values: List[Any] = [status, str(error or "")[:1000]]
    if mailbox_id is not None:
        fields.append("mailbox_id = ?")
        values.append(int(mailbox_id))
    if secret_enc is not None:
        fields.append("secret_enc = ?")
        values.append(secret_enc)
    values.append(int(operation_id))
    with _db_connection() as con:
        _ensure_managed_operation_table(con)
        con.execute(
            f"UPDATE managed_address_operations SET {', '.join(fields)} "
            "WHERE id = ?",
            values,
        )
        con.commit()


def _managed_operation_claim_stale(limit: int = 20) -> List[Dict[str, Any]]:
    stale_seconds = max(
        120,
        int(os.environ.get("MANAGED_OPERATION_STALE_SECONDS", "180")),
    )
    stale_modifier = f"-{stale_seconds} seconds"
    claimed: List[Dict[str, Any]] = []
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        _ensure_managed_operation_table(con)
        rows = con.execute(
            """
            SELECT id, action, mailbox_id, email, payload_json, secret_enc,
                   attempts
            FROM managed_address_operations
            WHERE status IN ('pending', 'reconciling')
              AND updated_at <= datetime('now', ?)
            ORDER BY id
            LIMIT ?
            """,
            (
                stale_modifier,
                max(1, min(int(limit), 100)),
            ),
        ).fetchall()
        for row in rows:
            cur = con.execute(
                """
                UPDATE managed_address_operations
                SET status = 'reconciling',
                    attempts = attempts + 1,
                    updated_at = datetime('now')
                WHERE id = ?
                  AND status IN ('pending', 'reconciling')
                  AND updated_at <= datetime('now', ?)
                """,
                (int(row["id"]), stale_modifier),
            )
            if cur.rowcount == 1:
                item = dict(row)
                item["attempts"] = int(row["attempts"] or 0) + 1
                claimed.append(item)
        con.commit()
    return claimed


def _managed_operation_is_superseded(
    operation_id: int,
    email: str,
    action: str,
) -> bool:
    with _db_connection() as con:
        rows = con.execute(
            """
            SELECT action
            FROM managed_address_operations
            WHERE email = ?
              AND id > ?
              AND status IN ('pending', 'reconciling', 'completed')
            ORDER BY id
            """,
            (
                str(email or "").strip().lower(),
                int(operation_id),
            ),
        ).fetchall()
    return bool(rows)


def _reconcile_managed_address_operations() -> int:
    if not CFG.MANAGED_ADDRESS_WRITE_AUTHORITY:
        return 0
    reconciled = 0
    for operation in _managed_operation_claim_stale():
        operation_id = int(operation["id"])
        action = str(operation["action"])
        mailbox_id = operation.get("mailbox_id")
        try:
            if _managed_operation_is_superseded(
                operation_id,
                operation["email"],
                action,
            ):
                _managed_operation_update(
                    operation_id,
                    "completed",
                    error="superseded by newer operation",
                    mailbox_id=int(mailbox_id) if mailbox_id else None,
                )
                continue
            payload = json.loads(str(operation["payload_json"] or "{}"))
            secret_enc = str(operation.get("secret_enc") or "")
            current = (
                _managed_address_get(int(mailbox_id))
                if mailbox_id
                else _managed_address_find_by_email(operation["email"])
            )
            if action == "create":
                _panel_manage_mail_login_owner(
                    payload["email"].split("@", 1)[0],
                    payload["email"],
                    action="claim",
                )
                password = decrypt_secret(secret_enc) if secret_enc else ""
                _mail_entity_resync(payload, password)
                if current:
                    _managed_address_update_metadata(
                        int(current["id"]),
                        {
                            **payload,
                            "revision": int(current.get("revision") or 1),
                        },
                    )
                    mailbox_id = int(current["id"])
                else:
                    mailbox_id = _managed_address_insert(
                        payload,
                        secret_enc,
                    )
            elif action == "update":
                if payload.get("kind") == "distribution":
                    _mail_entity_update_distribution(payload)
                if not mailbox_id:
                    raise RuntimeError("managed address update is missing mailbox_id")
                if not current:
                    raise RuntimeError("managed address update target is missing")
                _managed_address_update_metadata(
                    int(mailbox_id),
                    {
                        **payload,
                        "revision": (
                            payload.get("revision")
                            if payload.get("revision") is not None
                            else int(current.get("revision") or 1)
                        ),
                    },
                )
            elif action == "password":
                if not mailbox_id or not current:
                    raise RuntimeError("managed address password target is missing")
                password = decrypt_secret(secret_enc) if secret_enc else ""
                if not password:
                    raise RuntimeError("managed address password is unavailable")
                _mail_entity_reset_password(current, password)
                _managed_address_set_encrypted_password(
                    int(mailbox_id),
                    secret_enc,
                )
            elif action == "delete":
                _mail_entity_delete(payload)
                if mailbox_id and _managed_address_get(int(mailbox_id)):
                    _managed_address_delete_metadata(int(mailbox_id))
                _panel_manage_mail_login_owner(
                    payload["email"].split("@", 1)[0],
                    payload["email"],
                    action="release",
                )
            elif action == "release":
                _panel_manage_mail_login_owner(
                    payload["email"].split("@", 1)[0],
                    payload["email"],
                    action="release",
                )
            else:
                raise RuntimeError(f"unsupported managed address action: {action}")
            _managed_operation_update(
                operation_id,
                "completed",
                mailbox_id=int(mailbox_id) if mailbox_id else None,
            )
            reconciled += 1
        except Exception as exc:
            attempts = int(operation.get("attempts") or 1)
            _managed_operation_update(
                operation_id,
                "pending",
                error=str(exc),
                mailbox_id=int(mailbox_id) if mailbox_id else None,
            )
            log_method = log.critical if attempts >= 5 else log.error
            log_method(
                "managed address operation reconcile failed id=%s action=%s: %s",
                operation_id,
                action,
                exc,
            )
    return reconciled


_managed_owner_sync_ts = 0.0


def _sync_managed_address_owners(force: bool = False) -> int:
    global _managed_owner_sync_ts
    if not CFG.MANAGED_ADDRESS_WRITE_AUTHORITY:
        return 0
    now = time.time()
    if not force and _managed_owner_sync_ts and now - _managed_owner_sync_ts < 300:
        return 0
    synced = 0
    for address in _managed_address_list():
        try:
            _panel_manage_mail_login_owner(
                address["email"].split("@", 1)[0],
                address["email"],
                action="claim",
            )
            synced += 1
        except ValueError as exc:
            _managed_address_mark_error(
                int(address["id"]),
                f"mail login ownership conflict: {exc}",
            )
        except EmployeeDirectoryUnavailable as exc:
            log.warning("managed address owner sync unavailable: %s", exc)
            break
    _managed_owner_sync_ts = now
    return synced


class ManagedAddressReservationBody(BaseModel):
    mail_login: str = Field(..., min_length=1, max_length=64)


@app.post("/api/internal/managed-address/reservation")
async def internal_managed_address_reservation(
    body: ManagedAddressReservationBody,
    request: Request,
):
    _require_internal_service_access(request)
    mail_login = _sanitize_mail_login(body.mail_login)
    if not mail_login:
        raise HTTPException(status_code=400, detail="Invalid mail_login")
    managed = _managed_address_find_by_email(_mail_login_to_email(mail_login))
    return {
        "ok": True,
        "reserved": managed is not None,
        "address": managed,
    }


@app.get("/api/internal/managed-addresses")
async def internal_managed_addresses(request: Request):
    _require_internal_service_access(request)
    return {
        "ok": True,
        "addresses": _managed_address_list(),
    }


def _shared_mailbox_row_public(row: sqlite3.Row) -> Dict[str, Any]:
    payload = {
        "id": row["id"],
        "email": row["email"],
        "display_name": row["display_name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if "kind" in row.keys():
        payload["kind"] = row["kind"] or "group"
    if "technical_role" in row.keys():
        payload["technical_role"] = row["technical_role"] or ""
    if "company" in row.keys():
        payload["company"] = row["company"] or "Corp"
    if "directory_domain" in row.keys():
        payload["directory_domain"] = row["directory_domain"] or "corp.local"
    return payload


@app.get("/api/shared_mailboxes")
async def api_shared_list(request: Request):
    """List shared mailboxes the current user is a member of."""
    sess = get_session_or_401(request)
    rows = await asyncio.to_thread(
        _shared_access_rows_for_user,
        sess["imap_user"],
        str(sess.get("ad_login") or ""),
        str(sess.get("ad_domain") or ""),
    )
    out = []
    for row in rows:
        public = _shared_mailbox_row_public(row)
        public["can_send"] = bool(row["can_send"])
        out.append(public)
    return {"ok": True, "mailboxes": out}


def _shared_access_rows_for_user(
    imap_user: str,
    ad_login: str = "",
    ad_domain: str = "",
) -> List[Dict[str, Any]]:
    return [
        row
        for row in _shared_access_catalog_for_identity(imap_user, ad_login, ad_domain)
        if row["connected"]
    ]


def _shared_access_catalog_for_identity(
    member_email: str,
    ad_login: str = "",
    ad_domain: str = "",
) -> List[Dict[str, Any]]:
    normalized_email = str(member_email or "").strip().lower()
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        mailbox_columns = {
            str(row[1])
            for row in con.execute("PRAGMA table_info(shared_mailboxes)").fetchall()
        }
        company_projection = (
            "company" if "company" in mailbox_columns else "'Corp' AS company"
        )
        directory_projection = (
            "directory_domain"
            if "directory_domain" in mailbox_columns
            else "'corp.local' AS directory_domain"
        )
        mailboxes = [
            dict(row)
            for row in con.execute(
                "SELECT id, kind, technical_role, email, "
                f"{company_projection}, {directory_projection}, "
                "display_name, imap_pass_enc, created_at, updated_at "
                "FROM shared_mailboxes "
                "WHERE kind IN ('group', 'service', 'technical') "
                "ORDER BY email"
            ).fetchall()
        ]
        explicit = {
            int(row["mailbox_id"]): bool(row["can_send"])
            for row in con.execute(
                """
                SELECT mailbox_id, can_send
                FROM shared_mailbox_members
                WHERE LOWER(member_user) = LOWER(?)
                """,
                (normalized_email,),
            ).fetchall()
        }
        group_rows = con.execute(
            """
            SELECT mailbox_id, group_dn, group_name, can_send
            FROM shared_mailbox_ad_groups
            ORDER BY mailbox_id
            """
        ).fetchall()

    member_group_dns: set[str] = set()
    if group_rows and str(ad_login or "").strip():
        clean_domain = str(ad_domain or "").strip().lower()
        if not clean_domain or clean_domain == "corp.local":
            try:
                member_group_dns.update(
                    str(group_dn).casefold()
                    for group_dn in ldap_get_user_group_dns(ad_login)
                    if str(group_dn or "").strip()
                )
            except Exception as exc:
                log.warning(
                    "LDAPS group access lookup failed for %s: %s",
                    str(ad_login or "").strip(),
                    exc,
                )
        else:
            try:
                member_group_dns.update(
                    str(group_dn).casefold()
                    for group_dn in portal_get_user_group_dns(ad_login, clean_domain)
                    if str(group_dn or "").strip()
                )
            except Exception as exc:
                log.warning(
                    "Portal group access lookup failed for %s@%s: %s",
                    str(ad_login or "").strip(),
                    clean_domain or "?",
                    exc,
                )

    group_access: Dict[int, Dict[str, Any]] = {}
    for row in group_rows:
        if str(row["group_dn"] or "").casefold() not in member_group_dns:
            continue
        mailbox_id = int(row["mailbox_id"])
        access = group_access.setdefault(
            mailbox_id,
            {"can_send": False, "groups": []},
        )
        access["can_send"] = bool(access["can_send"] or row["can_send"])
        group_name = str(row["group_name"] or "").strip()
        if group_name and group_name not in access["groups"]:
            access["groups"].append(group_name)

    result: List[Dict[str, Any]] = []
    for mailbox in mailboxes:
        mailbox_id = int(mailbox["id"])
        direct = mailbox_id in explicit
        inherited = mailbox_id in group_access
        direct_can_send = bool(explicit.get(mailbox_id, False))
        inherited_can_send = bool(
            (group_access.get(mailbox_id) or {}).get("can_send")
        )
        mailbox.update(
            {
                "connected": direct or inherited,
                "direct": direct,
                "inherited": inherited,
                "direct_can_send": direct_can_send,
                "inherited_can_send": inherited_can_send,
                "can_send": direct_can_send or inherited_can_send,
                "inherited_groups": list(
                    (group_access.get(mailbox_id) or {}).get("groups") or []
                ),
            }
        )
        result.append(mailbox)
    return result


def _shared_access_public_item(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": int(row["id"]),
        "kind": str(row.get("kind") or "group"),
        "email": str(row.get("email") or ""),
        "display_name": str(row.get("display_name") or ""),
        "connected": bool(row.get("connected")),
        "direct": bool(row.get("direct")),
        "inherited": bool(row.get("inherited")),
        "direct_can_send": bool(row.get("direct_can_send")),
        "inherited_can_send": bool(row.get("inherited_can_send")),
        "can_send": bool(row.get("can_send")),
        "inherited_groups": list(row.get("inherited_groups") or []),
    }


def _shared_member_set(
    mailbox_id: int,
    member_email: str,
    *,
    connected: bool,
    can_send: bool,
) -> Dict[str, Any]:
    normalized_email = str(member_email or "").strip().lower()
    if not _is_mail_domain_address(normalized_email):
        raise HTTPException(
            status_code=400,
            detail="Участник должен иметь корпоративный email",
        )
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        mailbox = con.execute(
            """
            SELECT id, email, display_name, kind
            FROM shared_mailboxes
            WHERE id = ? AND kind IN ('group', 'service')
            """,
            (mailbox_id,),
        ).fetchone()
        if not mailbox:
            raise HTTPException(status_code=404, detail="Общий ящик не найден")
        if connected:
            existing = con.execute(
                """
                SELECT id
                FROM shared_mailbox_members
                WHERE mailbox_id = ? AND LOWER(member_user) = LOWER(?)
                """,
                (mailbox_id, normalized_email),
            ).fetchone()
            if existing:
                con.execute(
                    """
                    UPDATE shared_mailbox_members
                    SET member_user = ?,
                        can_send = ?,
                        added_at = datetime('now')
                    WHERE id = ?
                    """,
                    (
                        normalized_email,
                        1 if can_send else 0,
                        int(existing["id"]),
                    ),
                )
            else:
                con.execute(
                    """
                    INSERT INTO shared_mailbox_members (
                        mailbox_id, member_user, can_send
                    )
                    VALUES (?, ?, ?)
                    """,
                    (mailbox_id, normalized_email, 1 if can_send else 0),
                )
        else:
            con.execute(
                """
                DELETE FROM shared_mailbox_members
                WHERE mailbox_id = ? AND LOWER(member_user) = LOWER(?)
                """,
                (mailbox_id, normalized_email),
            )
        con.execute(
            """
            UPDATE shared_mailboxes
            SET revision = revision + 1,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (mailbox_id,),
        )
        con.commit()
    return {
        "id": int(mailbox["id"]),
        "email": str(mailbox["email"] or ""),
        "display_name": str(mailbox["display_name"] or ""),
        "kind": str(mailbox["kind"] or "group"),
        "member_email": normalized_email,
        "connected": bool(connected),
        "can_send": bool(can_send) if connected else False,
    }


def _shared_members_replace(
    mailbox_id: int,
    members: List[str],
) -> Dict[str, Any]:
    normalized_members: List[str] = []
    seen = set()
    for member in members:
        normalized = str(member or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        if not _is_mail_domain_address(normalized):
            raise HTTPException(
                status_code=400,
                detail="Участник должен иметь корпоративный email",
            )
        seen.add(normalized)
        normalized_members.append(normalized)

    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        mailbox = con.execute(
            "SELECT email FROM shared_mailboxes "
            "WHERE id = ? AND kind IN ('group', 'service')",
            (mailbox_id,),
        ).fetchone()
        if not mailbox:
            raise HTTPException(status_code=404, detail="Общий ящик не найден")
        existing_permissions = {
            str(row["member_user"]).strip().lower(): bool(row["can_send"])
            for row in con.execute(
                "SELECT member_user, can_send FROM shared_mailbox_members "
                "WHERE mailbox_id = ?",
                (mailbox_id,),
            ).fetchall()
        }
        con.execute(
            "DELETE FROM shared_mailbox_members WHERE mailbox_id = ?",
            (mailbox_id,),
        )
        for member in normalized_members:
            con.execute(
                "INSERT INTO shared_mailbox_members "
                "(mailbox_id, member_user, can_send) VALUES (?, ?, ?)",
                (
                    mailbox_id,
                    member,
                    1 if existing_permissions.get(member, True) else 0,
                ),
            )
        con.execute(
            """
            UPDATE shared_mailboxes
            SET revision = revision + 1,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (mailbox_id,),
        )
        con.commit()
    return {
        "email": str(mailbox["email"]),
        "members": normalized_members,
        "count": len(normalized_members),
    }


def _shared_get_creds_for_user(
    mailbox_id: int,
    imap_user: str,
    ad_login: str = "",
    ad_domain: str = "",
) -> Optional[Tuple[Dict[str, Any], str]]:
    """Return (row_dict, password) if user is a member, else None."""
    row = next(
        (
            item
            for item in _shared_access_rows_for_user(imap_user, ad_login, ad_domain)
            if int(item["id"]) == int(mailbox_id)
        ),
        None,
    )
    if not row:
        return None
    pwd = decrypt_secret(row["imap_pass_enc"])
    if not pwd:
        return None
    return (dict(row), pwd)


@app.get("/api/shared_mailboxes/{mailbox_id}/messages")
async def api_shared_messages(
    mailbox_id: int,
    request: Request,
    folder: str = Query("INBOX"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    sess = get_session_or_401(request)
    creds = await asyncio.to_thread(
        _shared_get_creds_for_user,
        mailbox_id,
        sess["imap_user"],
        str(sess.get("ad_login") or ""),
        str(sess.get("ad_domain") or ""),
    )
    if not creds:
        return JSONResponse({"error": "Not a member of this mailbox"}, status_code=403)
    row, pwd = creds
    loop = asyncio.get_event_loop()
    try:
        listing = await loop.run_in_executor(
            None, imap_message_list, row["email"], pwd, folder, page, per_page, ""
        )
    except Exception as e:
        return JSONResponse({"error": f"IMAP: {e}"}, status_code=502)
    return {
        "ok": True,
        "mailbox": {"id": mailbox_id, "email": row["email"], "display_name": row["display_name"]},
        "folder": folder,
        **listing,
    }


@app.get("/api/shared_mailboxes/{mailbox_id}/message/{uid}")
async def api_shared_message(
    mailbox_id: int,
    uid: str,
    request: Request,
    folder: str = Query("INBOX"),
):
    sess = get_session_or_401(request)
    creds = await asyncio.to_thread(
        _shared_get_creds_for_user,
        mailbox_id,
        sess["imap_user"],
        str(sess.get("ad_login") or ""),
        str(sess.get("ad_domain") or ""),
    )
    if not creds:
        return JSONResponse({"error": "Not a member"}, status_code=403)
    row, pwd = creds
    loop = asyncio.get_event_loop()
    try:
        msg = await loop.run_in_executor(
            None,
            imap_fetch_message,
            row["email"],
            pwd,
            folder,
            uid,
            bool(row.get("can_send")),
        )
    except Exception as e:
        return JSONResponse({"error": f"IMAP: {e}"}, status_code=502)
    return msg


class SharedSendBody(BaseModel):
    to: list[str]
    cc: list[str] | None = None
    bcc: list[str] | None = None
    subject: str
    body_html: str = ""
    body_text: str = ""
    in_reply_to: str | None = None


@app.post("/api/shared_mailboxes/{mailbox_id}/send")
async def api_shared_send(
    mailbox_id: int, body: SharedSendBody, request: Request
):
    sess = get_session_or_401(request)
    creds = await asyncio.to_thread(
        _shared_get_creds_for_user,
        mailbox_id,
        sess["imap_user"],
        str(sess.get("ad_login") or ""),
        str(sess.get("ad_domain") or ""),
    )
    if not creds:
        return JSONResponse({"error": "Not a member"}, status_code=403)
    row, pwd = creds
    if not row.get("can_send"):
        return JSONResponse({"error": "No send permission"}, status_code=403)
    # "From" is the shared address; reply-to carries the acting member for accountability
    from_addr = formataddr((row["display_name"] or row["email"], row["email"]))
    body_html, body_text = _compose_body_variants(body.body_html, body.body_text)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: smtp_send(
                row["email"],
                pwd,
                from_addr,
                body.to,
                body.cc or [],
                body.bcc or [],
                body.subject,
                body_html,
                body_text,
                body.in_reply_to,
                None,
            ),
        )
    except Exception as e:
        return JSONResponse({"error": f"SMTP: {e}"}, status_code=502)
    # Audit: so admins can see who sent from the shared mailbox
    db_audit_log(
        sess["imap_user"],
        "shared.send",
        row["email"],
        details={"to": body.to, "subject": body.subject, "mailbox_id": mailbox_id},
        ip=_client_ip(request),
    )
    return {"ok": True}


# Admin endpoints — manage shared mailboxes and membership
class SharedMailboxCreateBody(BaseModel):
    email: str
    display_name: str = ""
    password: str  # IMAP/SMTP password for the shared account
    members: list[str] = Field(default_factory=list)


@app.post("/api/adminportal/shared_mailboxes/create")
async def api_admin_shared_create(body: SharedMailboxCreateBody, request: Request):
    _require_admin(request)
    _require_managed_address_write_authority()
    managed_body = ManagedAddressBody(
        kind="group",
        email=body.email,
        display_name=body.display_name,
        members=[
            ManagedAddressMember(email=member, can_send=True)
            for member in body.members
        ],
    )
    address_key = managed_body.email.strip().lower()
    async with _managed_address_operation_lock(address_key):
        return await _adminportal_managed_address_create_locked(
            managed_body,
            request,
            requested_password=body.password,
        )


@app.get("/api/adminportal/shared_mailboxes")
async def api_admin_shared_list(request: Request):
    admin_sess = _require_admin(request)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, kind, technical_role, email, company, directory_domain, "
            "display_name, created_at, updated_at "
            "FROM shared_mailboxes "
            "WHERE kind IN ('group', 'service', 'technical') "
            "ORDER BY email"
        ).fetchall()
        members: Dict[int, List[str]] = {}
        for mr in con.execute(
            "SELECT mailbox_id, member_user FROM shared_mailbox_members"
        ).fetchall():
            members.setdefault(mr["mailbox_id"], []).append(mr["member_user"])
    out = []
    for r in rows:
        d = _shared_mailbox_row_public(r)
        d["members"] = members.get(r["id"], [])
        out.append(d)
    return {"ok": True, "mailboxes": out}


class SharedMembersBody(BaseModel):
    members: list[str]


class SharedMembershipBody(BaseModel):
    mailbox_id: int
    email: str
    connected: bool
    can_send: bool = True
    ad_login: str = ""
    ad_domain: str = ""


@app.put("/api/adminportal/shared-mailboxes/membership")
async def api_admin_shared_membership(
    body: SharedMembershipBody,
    request: Request,
):
    admin_sess = _require_admin(request)
    _require_managed_address_write_authority()
    member_email = body.email.strip().lower()
    loop = asyncio.get_event_loop()
    async with _managed_address_operation_lock(f"id:{body.mailbox_id}"):
        result = await loop.run_in_executor(
            None,
            lambda: _shared_member_set(
                body.mailbox_id,
                member_email,
                connected=body.connected,
                can_send=body.can_send,
            ),
        )
        catalog = await loop.run_in_executor(
            None,
            _shared_access_catalog_for_identity,
            member_email,
            body.ad_login.strip().lower(),
            body.ad_domain.strip().lower(),
        )
    access = next(
        (
            _shared_access_public_item(row)
            for row in catalog
            if int(row["id"]) == int(body.mailbox_id)
        ),
        None,
    )
    db_audit_log(
        admin_sess.get("username", "admin"),
        "shared_mailbox.member_update",
        result["email"],
        details={
            "member": member_email,
            "connected": bool(body.connected),
            "can_send": bool(body.can_send) if body.connected else False,
            "mailbox_id": int(body.mailbox_id),
        },
        ip=_client_ip(request),
    )
    return {"ok": True, "membership": result, "access": access}


@app.put("/api/adminportal/shared_mailboxes/{mailbox_id}/members")
async def api_admin_shared_members(
    mailbox_id: int, body: SharedMembersBody, request: Request
):
    admin_sess = _require_admin(request)
    _require_managed_address_write_authority()
    async with _managed_address_operation_lock(f"id:{mailbox_id}"):
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            _shared_members_replace,
            mailbox_id,
            body.members,
        )
    db_audit_log(
        admin_sess.get("username", "admin"),
        "shared_mailbox.members_update",
        result["email"],
        details={"members": result["members"]},
        ip=_client_ip(request),
    )
    return {"ok": True, "count": result["count"]}


@app.delete("/api/adminportal/shared_mailboxes/{mailbox_id}")
async def api_admin_shared_delete(mailbox_id: int, request: Request):
    admin_sess = _require_admin(request)
    _require_managed_address_write_authority()
    return await _managed_address_delete(
        mailbox_id,
        admin_sess.get("username", "admin"),
        _client_ip(request),
    )


def _managed_address_assert_available(payload: Dict[str, Any]) -> None:
    email_addr = payload["email"]
    if _managed_address_find_by_email(email_addr):
        raise HTTPException(409, "Управляемый адрес уже существует")

    if _mail_admin_available():
        ok, accounts, status_text = _collect_mail_accounts_result()
        if not ok:
            raise HTTPException(
                502,
                f"Не удалось проверить почтовые аккаунты: {status_text}",
            )
    else:
        try:
            account_data = _mail_control_request(
                "/api/mail/list_accounts",
                {},
                timeout=_MAIL_CONTROL_LIST_TIMEOUT,
            )
        except Exception as exc:
            raise HTTPException(
                502,
                f"Не удалось проверить почтовые аккаунты: {exc}",
            ) from exc
        accounts = account_data.get("accounts") or []
    if email_addr in {
        str(item or "").strip().lower()
        for item in accounts
    }:
        raise HTTPException(409, "Почтовый аккаунт уже существует")

    mail_login = email_addr.split("@", 1)[0]
    for alias in db_list_all_aliases():
        if str(alias.get("mail_login") or "").strip().lower() == mail_login:
            raise HTTPException(
                409,
                "Адрес уже закреплён за сотрудником",
            )

    try:
        distribution_data = _mail_control_request(
            "/api/mail/distribution/get",
            {"mail_login": mail_login},
            timeout=15,
        )
    except Exception as exc:
        raise HTTPException(
            502,
            f"Не удалось проверить рассылки: {exc}",
        ) from exc
    if distribution_data.get("recipients"):
        raise HTTPException(409, "Адрес рассылки уже существует")


def _mail_entity_create(
    payload: Dict[str, Any],
    password: str,
) -> Dict[str, Any]:
    mail_login = payload["email"].split("@", 1)[0]
    if payload["kind"] == "distribution":
        data = _mail_control_request(
            "/api/mail/distribution/set",
            {
                "mail_login": mail_login,
                "recipients": payload["recipients"],
            },
            timeout=60,
        )
        return {
            "password": "",
            "source": str(data.get("source") or "soc-control-plane"),
        }

    if _mail_admin_available():
        ok, status_text = _mailserver_add_user_direct(
            payload["email"],
            password,
        )
        if ok and status_text == "already exists":
            raise HTTPException(409, "Почтовый аккаунт уже существует")
        if not ok:
            raise RuntimeError(status_text)
        return {
            "password": password,
            "source": "local-mailserver",
        }

    data = _mail_control_request(
        "/api/mail/create_account",
        {
            "mail_login": mail_login,
            "password": password,
        },
        timeout=60,
    )
    if data.get("status") == "already exists":
        raise HTTPException(409, "Почтовый аккаунт уже существует")
    confirmed_password = str(data.get("password") or "")
    if not confirmed_password:
        raise RuntimeError(
            "mail control-plane did not confirm the mailbox password"
        )
    return {
        "password": confirmed_password,
        "source": str(data.get("source") or "soc-control-plane"),
    }


def _mail_entity_delete(payload: Dict[str, Any]) -> Dict[str, Any]:
    mail_login = payload["email"].split("@", 1)[0]
    if payload["kind"] == "distribution":
        data = _mail_control_request(
            "/api/mail/distribution/delete",
            {"mail_login": mail_login},
            timeout=60,
        )
        return {
            "status": str(data.get("status") or "removed"),
            "source": str(data.get("source") or "soc-control-plane"),
        }

    if _mail_admin_available():
        ok, status_text = _mailserver_delete_user_direct(payload["email"])
        if not ok:
            raise RuntimeError(status_text)
        return {
            "status": status_text,
            "source": "local-mailserver",
        }

    data = _mail_control_request(
        "/api/mail/delete_account",
        {"mail_login": mail_login},
        timeout=60,
    )
    return {
        "status": str(data.get("status") or "removed"),
        "source": str(data.get("source") or "soc-control-plane"),
    }


@app.get("/api/adminportal/managed-addresses")
async def adminportal_managed_address_list(request: Request):
    _require_admin(request)
    return {
        "ok": True,
        "addresses": _managed_address_list(),
        "technical_presets": TECHNICAL_MAILBOX_PRESETS,
    }


@app.get("/api/adminportal/managed-addresses/{mailbox_id}")
async def adminportal_managed_address_detail(
    mailbox_id: int,
    request: Request,
):
    _require_admin(request)
    address = _managed_address_get(mailbox_id)
    if not address:
        raise HTTPException(404, "Управляемый адрес не найден")
    return {"ok": True, "address": address}


@app.post("/api/adminportal/managed-addresses")
async def adminportal_managed_address_create(
    body: ManagedAddressBody,
    request: Request,
):
    _require_admin(request)
    _require_managed_address_write_authority()
    address_key = body.email.strip().lower()
    async with _managed_address_operation_lock(address_key):
        return await _adminportal_managed_address_create_locked(body, request)


async def _adminportal_managed_address_create_locked(
    body: ManagedAddressBody,
    request: Request,
    *,
    requested_password: str = "",
):
    admin_sess = _require_admin(request)
    admin_user = admin_sess.get("username", "admin")
    ip = _client_ip(request)
    payload = _validate_managed_address_body(body)
    loop = asyncio.get_event_loop()
    if payload["ad_groups"]:
        payload = await loop.run_in_executor(
            _mailadmin_read_executor,
            _validate_managed_address_ad_groups,
            payload,
        )
    audit_details = {
        "kind": payload["kind"],
        "members_count": len(payload["members"]),
        "ad_groups_count": len(payload["ad_groups"]),
        "recipients_count": len(payload["recipients"]),
    }
    operation_id: Optional[int] = None
    owner_claimed = False

    try:
        await loop.run_in_executor(
            None,
            _managed_address_assert_available,
            payload,
        )
        effective_password = (
            ""
            if payload["kind"] == "distribution"
            else (requested_password or _generate_password())
        )
        encrypted_password = (
            ""
            if payload["kind"] == "distribution"
            else encrypt_secret(effective_password)
        )
        if payload["kind"] != "distribution" and not encrypted_password:
            raise HTTPException(500, "Не удалось защитить пароль ящика")
        operation_id = await loop.run_in_executor(
            None,
            lambda: _managed_operation_start(
                "create",
                payload["email"],
                payload,
                secret_enc=encrypted_password,
            ),
        )
        await loop.run_in_executor(
            None,
            lambda: _panel_manage_mail_login_owner(
                payload["email"].split("@", 1)[0],
                payload["email"],
                action="claim",
            ),
        )
        owner_claimed = True
        entity = await loop.run_in_executor(
            None,
            _mail_entity_create,
            payload,
            effective_password,
        )
    except ValueError as exc:
        if operation_id is not None:
            _managed_operation_update(
                operation_id,
                "failed",
                error=str(exc),
            )
        db_audit_log(
            admin_user,
            "managed_address.create",
            payload["email"],
            {**audit_details, "error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(409, "Этот mail login уже занят") from exc
    except HTTPException as exc:
        if owner_claimed and operation_id is None:
            await loop.run_in_executor(
                None,
                _managed_owner_release_or_queue,
                payload,
            )
        if operation_id is not None:
            _managed_operation_update(
                operation_id,
                "pending",
                error=str(exc.detail),
            )
        db_audit_log(
            admin_user,
            "managed_address.create",
            payload["email"],
            {**audit_details, "error": str(exc.detail)},
            ip,
            ok=False,
        )
        raise
    except Exception as exc:
        if owner_claimed and operation_id is None:
            await loop.run_in_executor(
                None,
                _managed_owner_release_or_queue,
                payload,
            )
        if operation_id is not None:
            _managed_operation_update(
                operation_id,
                "pending",
                error=str(exc),
            )
        db_audit_log(
            admin_user,
            "managed_address.create",
            payload["email"],
            {**audit_details, "error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(
            502,
            f"Ошибка почтового сервера: {exc}",
        ) from exc

    final_password = str(entity.get("password") or effective_password)
    if payload["kind"] != "distribution" and final_password != effective_password:
        encrypted_password = encrypt_secret(final_password)
        if not encrypted_password:
            compensation_ok = False
            try:
                await loop.run_in_executor(None, _mail_entity_delete, payload)
                compensation_ok = True
            except Exception as compensation_exc:
                log.error(
                    "managed address credential compensation failed for %s: %s",
                    payload["email"],
                    compensation_exc,
                )
            _managed_operation_update(
                operation_id,
                "completed" if compensation_ok else "pending",
                error="" if compensation_ok else "credential encryption failed",
            )
            if compensation_ok:
                await loop.run_in_executor(
                    None,
                    _managed_owner_release_or_queue,
                    payload,
                )
            raise HTTPException(500, "Не удалось защитить пароль ящика")
        _managed_operation_update(
            operation_id,
            "pending",
            secret_enc=encrypted_password,
        )

    try:
        mailbox_id = await loop.run_in_executor(
            None,
            _managed_address_insert,
            payload,
            encrypted_password,
        )
    except Exception as exc:
        compensation_ok = False
        try:
            await loop.run_in_executor(None, _mail_entity_delete, payload)
            compensation_ok = True
        except Exception as compensation_exc:
            log.error(
                "managed address create compensation failed for %s: %s",
                payload["email"],
                compensation_exc,
            )
        _managed_operation_update(
            operation_id,
            "completed" if compensation_ok else "pending",
            error="" if compensation_ok else str(exc),
        )
        if compensation_ok:
            await loop.run_in_executor(
                None,
                _managed_owner_release_or_queue,
                payload,
            )
        db_audit_log(
            admin_user,
            "managed_address.create",
            payload["email"],
            {**audit_details, "error": str(exc)},
            ip,
            ok=False,
        )
        if isinstance(exc, sqlite3.IntegrityError):
            raise HTTPException(409, "Управляемый адрес уже существует") from exc
        raise HTTPException(
            500,
            "Почтовая сущность создана, но метаданные не сохранены",
        ) from exc

    _managed_operation_update(
        operation_id,
        "completed",
        mailbox_id=mailbox_id,
    )
    db_audit_log(
        admin_user,
        "managed_address.create",
        payload["email"],
        {
            **audit_details,
            "source": entity.get("source") or "",
        },
        ip,
        ok=True,
    )
    response = {
        "ok": True,
        "id": mailbox_id,
        "kind": payload["kind"],
        "email": payload["email"],
        "source": entity.get("source") or "",
    }
    if payload["kind"] != "distribution":
        response["password"] = final_password
        response["credential_pack"] = _build_credential_pack(
            payload["email"],
            final_password,
            payload["display_name"] or payload["email"],
        )
    return response


def _mail_entity_update_distribution(
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    mail_login = payload["email"].split("@", 1)[0]
    data = _mail_control_request(
        "/api/mail/distribution/set",
        {
            "mail_login": mail_login,
            "recipients": payload["recipients"],
        },
        timeout=60,
    )
    return {
        "source": str(data.get("source") or "soc-control-plane"),
        "recipients": data.get("recipients") or payload["recipients"],
    }


def _mail_entity_reset_password(
    payload: Dict[str, Any],
    password: str,
) -> Dict[str, Any]:
    if payload["kind"] == "distribution":
        raise HTTPException(400, "У рассылки нет пароля")
    mail_login = payload["email"].split("@", 1)[0]
    if _mail_admin_available():
        ok, status_text = _mailserver_set_password_direct(
            payload["email"],
            password,
        )
        if not ok:
            raise RuntimeError(status_text)
        return {
            "status": status_text,
            "source": "local-mailserver",
        }
    data = _mail_control_request(
        "/api/mail/reset_password",
        {
            "mail_login": mail_login,
            "password": password,
        },
        timeout=60,
    )
    return {
        "status": str(data.get("status") or "updated"),
        "source": str(data.get("source") or "soc-control-plane"),
    }


def _mail_entity_resync(
    payload: Dict[str, Any],
    password: str,
) -> Dict[str, Any]:
    if payload["kind"] == "distribution":
        return _mail_entity_update_distribution(payload)
    try:
        return _mail_entity_reset_password(payload, password)
    except Exception as reset_exc:
        try:
            return _mail_entity_create(payload, password)
        except Exception:
            raise reset_exc


async def _managed_address_update(
    mailbox_id: int,
    payload: Dict[str, Any],
    admin_user: str,
    ip: str,
) -> Dict[str, Any]:
    async with _managed_address_operation_lock(f"id:{mailbox_id}"):
        return await _managed_address_update_locked(
            mailbox_id,
            payload,
            admin_user,
            ip,
        )


async def _managed_address_update_locked(
    mailbox_id: int,
    payload: Dict[str, Any],
    admin_user: str,
    ip: str,
) -> Dict[str, Any]:
    current = _managed_address_get(mailbox_id)
    if not current:
        raise HTTPException(404, "Управляемый адрес не найден")
    if (
        payload["kind"] != current["kind"]
        or payload["email"] != current["email"]
    ):
        raise HTTPException(
            409,
            "Тип и email управляемого адреса нельзя изменить",
        )
    current_revision = int(current.get("revision") or 1)
    requested_revision = payload.get("revision")
    if (
        requested_revision is not None
        and int(requested_revision) != current_revision
    ):
        raise HTTPException(
            409,
            "Состав общего адреса уже изменён. Обновите страницу и повторите.",
        )
    payload = {
        **payload,
        "revision": (
            current_revision
            if requested_revision is None
            else int(requested_revision)
        ),
    }

    loop = asyncio.get_event_loop()
    operation_id = await loop.run_in_executor(
        None,
        lambda: _managed_operation_start(
            "update",
            current["email"],
            payload,
            mailbox_id=mailbox_id,
        ),
    )
    distribution_applied = False
    source = ""
    if current["kind"] == "distribution":
        try:
            result = await loop.run_in_executor(
                None,
                _mail_entity_update_distribution,
                payload,
            )
            source = str(result.get("source") or "")
            distribution_applied = True
        except Exception as exc:
            _managed_operation_update(
                operation_id,
                "pending",
                error=str(exc),
                mailbox_id=mailbox_id,
            )
            _managed_address_mark_error(mailbox_id, str(exc))
            db_audit_log(
                admin_user,
                "managed_address.update",
                current["email"],
                {"kind": current["kind"], "error": str(exc)},
                ip,
                ok=False,
            )
            raise HTTPException(
                502,
                f"Не удалось обновить рассылку: {exc}",
            ) from exc

    try:
        await loop.run_in_executor(
            None,
            _managed_address_update_metadata,
            mailbox_id,
            payload,
        )
    except Exception as exc:
        compensation_error = ""
        if distribution_applied:
            rollback_payload = {
                **current,
                "members": [],
                "recipients": current.get("recipients") or [],
            }
            try:
                await loop.run_in_executor(
                    None,
                    _mail_entity_update_distribution,
                    rollback_payload,
                )
                _managed_operation_update(
                    operation_id,
                    "completed",
                    mailbox_id=mailbox_id,
                )
            except Exception as compensation_exc:
                compensation_error = str(compensation_exc)
                _managed_address_mark_error(
                    mailbox_id,
                    f"{exc}; rollback: {compensation_exc}",
                )
        if not distribution_applied:
            _managed_operation_update(
                operation_id,
                "pending",
                error=str(exc),
                mailbox_id=mailbox_id,
            )
        elif compensation_error:
            _managed_operation_update(
                operation_id,
                "pending",
                error=f"{exc}; rollback: {compensation_error}",
                mailbox_id=mailbox_id,
            )
        db_audit_log(
            admin_user,
            "managed_address.update",
            current["email"],
            {
                "kind": current["kind"],
                "error": str(exc),
                "rollback_error": compensation_error,
            },
            ip,
            ok=False,
        )
        raise HTTPException(
            500,
            "Почтовая сущность обновлена, но метаданные не сохранены",
        ) from exc

    _managed_operation_update(
        operation_id,
        "completed",
        mailbox_id=mailbox_id,
    )
    db_audit_log(
        admin_user,
        "managed_address.update",
        current["email"],
        {
            "kind": current["kind"],
            "members_count": len(payload["members"]),
            "recipients_count": len(payload["recipients"]),
            "source": source,
        },
        ip,
        ok=True,
    )
    return {
        "ok": True,
        "address": _managed_address_get(mailbox_id),
    }


async def _managed_address_reset_password(
    mailbox_id: int,
    admin_user: str,
    ip: str,
) -> Dict[str, Any]:
    async with _managed_address_operation_lock(f"id:{mailbox_id}"):
        return await _managed_address_reset_password_locked(
            mailbox_id,
            admin_user,
            ip,
        )


async def _managed_address_reset_password_locked(
    mailbox_id: int,
    admin_user: str,
    ip: str,
) -> Dict[str, Any]:
    current = _managed_address_get(mailbox_id)
    if not current:
        raise HTTPException(404, "Управляемый адрес не найден")
    if current["kind"] == "distribution":
        raise HTTPException(400, "У рассылки нет пароля")

    old_encrypted = _managed_address_get_encrypted_password(mailbox_id)
    old_password = decrypt_secret(old_encrypted) if old_encrypted else ""
    new_password = _generate_password()
    encrypted = encrypt_secret(new_password)
    if not encrypted:
        raise HTTPException(500, "Не удалось защитить новый пароль")
    loop = asyncio.get_event_loop()
    operation_id = await loop.run_in_executor(
        None,
        lambda: _managed_operation_start(
            "password",
            current["email"],
            current,
            mailbox_id=mailbox_id,
            secret_enc=encrypted,
        ),
    )
    try:
        result = await loop.run_in_executor(
            None,
            _mail_entity_reset_password,
            current,
            new_password,
        )
    except Exception as exc:
        _managed_operation_update(
            operation_id,
            "pending",
            error=str(exc),
            mailbox_id=mailbox_id,
        )
        _managed_address_mark_error(mailbox_id, str(exc))
        db_audit_log(
            admin_user,
            "managed_address.password",
            current["email"],
            {"kind": current["kind"], "error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(
            502,
            f"Не удалось изменить пароль: {exc}",
        ) from exc

    try:
        await loop.run_in_executor(
            None,
            _managed_address_set_encrypted_password,
            mailbox_id,
            encrypted,
        )
    except Exception as exc:
        rollback_error = ""
        rollback_ok = False
        if old_password:
            try:
                await loop.run_in_executor(
                    None,
                    _mail_entity_reset_password,
                    current,
                    old_password,
                )
                rollback_ok = True
            except Exception as compensation_exc:
                rollback_error = str(compensation_exc)
        _managed_operation_update(
            operation_id,
            "completed" if rollback_ok else "pending",
            error="" if rollback_ok else f"{exc}; rollback: {rollback_error}",
            mailbox_id=mailbox_id,
        )
        _managed_address_mark_error(
            mailbox_id,
            f"{exc}; rollback: {rollback_error}",
        )
        db_audit_log(
            admin_user,
            "managed_address.password",
            current["email"],
            {"kind": current["kind"], "error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(
            500,
            "Пароль изменён, но защищённая копия не сохранена",
        ) from exc

    _managed_operation_update(
        operation_id,
        "completed",
        mailbox_id=mailbox_id,
    )
    db_audit_log(
        admin_user,
        "managed_address.password",
        current["email"],
        {
            "kind": current["kind"],
            "source": result.get("source") or "",
        },
        ip,
        ok=True,
    )
    return {
        "ok": True,
        "id": mailbox_id,
        "email": current["email"],
        "password": new_password,
        "credential_pack": _build_credential_pack(
            current["email"],
            new_password,
            current.get("display_name") or current["email"],
        ),
    }


async def _managed_address_resync(
    mailbox_id: int,
    admin_user: str,
    ip: str,
) -> Dict[str, Any]:
    async with _managed_address_operation_lock(f"id:{mailbox_id}"):
        return await _managed_address_resync_locked(
            mailbox_id,
            admin_user,
            ip,
        )


async def _managed_address_resync_locked(
    mailbox_id: int,
    admin_user: str,
    ip: str,
) -> Dict[str, Any]:
    current = _managed_address_get(mailbox_id)
    if not current:
        raise HTTPException(404, "Управляемый адрес не найден")
    password = ""
    if current["kind"] != "distribution":
        encrypted = _managed_address_get_encrypted_password(mailbox_id)
        password = decrypt_secret(encrypted) if encrypted else ""
        if not password:
            raise HTTPException(
                409,
                "Для повторной синхронизации нужен сохранённый пароль",
            )
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None,
            _mail_entity_resync,
            current,
            password,
        )
        await loop.run_in_executor(
            None,
            _managed_address_clear_error,
            mailbox_id,
        )
    except Exception as exc:
        _managed_address_mark_error(mailbox_id, str(exc))
        db_audit_log(
            admin_user,
            "managed_address.resync",
            current["email"],
            {"kind": current["kind"], "error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(
            502,
            f"Повторная синхронизация не выполнена: {exc}",
        ) from exc
    db_audit_log(
        admin_user,
        "managed_address.resync",
        current["email"],
        {
            "kind": current["kind"],
            "source": result.get("source") or "",
        },
        ip,
        ok=True,
    )
    return {
        "ok": True,
        "id": mailbox_id,
        "email": current["email"],
        "status": "active",
    }


async def _managed_address_delete(
    mailbox_id: int,
    admin_user: str,
    ip: str,
) -> Dict[str, Any]:
    async with _managed_address_operation_lock(f"id:{mailbox_id}"):
        return await _managed_address_delete_locked(
            mailbox_id,
            admin_user,
            ip,
        )


async def _managed_address_delete_locked(
    mailbox_id: int,
    admin_user: str,
    ip: str,
) -> Dict[str, Any]:
    current = _managed_address_get(mailbox_id)
    if not current:
        raise HTTPException(404, "Управляемый адрес не найден")
    loop = asyncio.get_event_loop()
    encrypted_password = _managed_address_get_encrypted_password(mailbox_id)
    operation_id = await loop.run_in_executor(
        None,
        lambda: _managed_operation_start(
            "delete",
            current["email"],
            current,
            mailbox_id=mailbox_id,
            secret_enc=encrypted_password,
        ),
    )
    try:
        result = await loop.run_in_executor(
            None,
            _mail_entity_delete,
            current,
        )
    except Exception as exc:
        _managed_operation_update(
            operation_id,
            "pending",
            error=str(exc),
            mailbox_id=mailbox_id,
        )
        _managed_address_mark_error(mailbox_id, str(exc))
        db_audit_log(
            admin_user,
            "managed_address.delete",
            current["email"],
            {"kind": current["kind"], "error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(
            502,
            f"Не удалось удалить почтовую сущность: {exc}",
        ) from exc
    try:
        await loop.run_in_executor(
            None,
            _managed_address_delete_metadata,
            mailbox_id,
        )
    except Exception as exc:
        _managed_operation_update(
            operation_id,
            "pending",
            error=str(exc),
            mailbox_id=mailbox_id,
        )
        _managed_address_mark_error(mailbox_id, str(exc))
        db_audit_log(
            admin_user,
            "managed_address.delete",
            current["email"],
            {"kind": current["kind"], "error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(
            500,
            "Почтовая сущность удалена, но метаданные не очищены",
        ) from exc
    await loop.run_in_executor(
        None,
        _managed_owner_release_or_queue,
        current,
    )
    _managed_operation_update(
        operation_id,
        "completed",
        mailbox_id=mailbox_id,
    )
    db_audit_log(
        admin_user,
        "managed_address.delete",
        current["email"],
        {
            "kind": current["kind"],
            "source": result.get("source") or "",
        },
        ip,
        ok=True,
    )
    return {
        "ok": True,
        "id": mailbox_id,
        "email": current["email"],
    }


@app.put("/api/adminportal/managed-addresses/{mailbox_id}")
async def adminportal_managed_address_update(
    mailbox_id: int,
    body: ManagedAddressBody,
    request: Request,
):
    admin_sess = _require_admin(request)
    _require_managed_address_write_authority()
    if body.revision is None:
        raise HTTPException(
            409,
            "Обновите карточку общего адреса перед сохранением.",
        )
    payload = _validate_managed_address_body(body)
    if payload["ad_groups"]:
        payload = await asyncio.get_event_loop().run_in_executor(
            _mailadmin_read_executor,
            _validate_managed_address_ad_groups,
            payload,
        )
    return await _managed_address_update(
        mailbox_id,
        payload,
        admin_sess.get("username", "admin"),
        _client_ip(request),
    )


@app.post("/api/adminportal/managed-addresses/{mailbox_id}/password")
async def adminportal_managed_address_password(
    mailbox_id: int,
    request: Request,
):
    admin_sess = _require_admin(request)
    _require_managed_address_write_authority()
    return await _managed_address_reset_password(
        mailbox_id,
        admin_sess.get("username", "admin"),
        _client_ip(request),
    )


@app.post("/api/adminportal/managed-addresses/{mailbox_id}/resync")
async def adminportal_managed_address_resync(
    mailbox_id: int,
    request: Request,
):
    admin_sess = _require_admin(request)
    _require_managed_address_write_authority()
    return await _managed_address_resync(
        mailbox_id,
        admin_sess.get("username", "admin"),
        _client_ip(request),
    )


@app.delete("/api/adminportal/managed-addresses/{mailbox_id}")
async def adminportal_managed_address_delete(
    mailbox_id: int,
    request: Request,
):
    admin_sess = _require_admin(request)
    _require_managed_address_write_authority()
    return await _managed_address_delete(
        mailbox_id,
        admin_sess.get("username", "admin"),
        _client_ip(request),
    )


# ---------------------------------------------------------------------------
# Web Push (PWA notifications)
# ---------------------------------------------------------------------------
# VAPID keys live in env — public one is exposed to the client, private is
# never shipped. Actual push delivery is done by proxy-panel on SOC
# (where pywebpush is installed). Webmail just stores subscriptions + forwards.

VAPID_PUBLIC = os.environ.get("JMAIL_VAPID_PUBLIC", "").strip()
VAPID_SUBJECT = os.environ.get("JMAIL_VAPID_SUBJECT", "mailto:admin@localhost")


def _push_send_via_panel(sub: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Forward a push notification to the proxy panel which has pywebpush."""
    if not CFG.INTERNAL_TOKEN:
        return False, "internal token not configured"
    url = CFG.PROXY_PANEL_URL.rstrip("/") + "/api/push/send"
    body = json.dumps({"subscription": sub, "payload": payload, "ttl": 86400}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Internal-Token", CFG.INTERNAL_TOKEN)
    req.add_header("X-Internal-Service", "rupochta")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload_resp = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return False, str(e)
    if not payload_resp.get("ok"):
        return False, str(payload_resp.get("error") or "push delivery failed")
    data = payload_resp.get("data") or {}
    if not data.get("ok"):
        return False, str(data.get("error") or "")
    return True, "ok"


class PushSubscribeBody(BaseModel):
    endpoint: str
    keys: Dict[str, str]  # {p256dh, auth}
    user_agent: Optional[str] = None


@app.get("/api/push/vapid_key")
async def api_push_vapid(request: Request):
    get_session_or_401(request)
    if not VAPID_PUBLIC:
        return JSONResponse({"error": "VAPID not configured"}, status_code=503)
    return {"ok": True, "public_key": VAPID_PUBLIC}


@app.post("/api/push/subscribe")
async def api_push_subscribe(body: PushSubscribeBody, request: Request):
    sess = get_session_or_401(request)
    endpoint = str(body.endpoint or "").strip()
    p256dh = str(body.keys.get("p256dh") or "")
    auth_key = str(body.keys.get("auth") or "")
    if not endpoint or not p256dh or not auth_key:
        return JSONResponse({"error": "endpoint + keys required"}, status_code=400)
    with _db_connection() as con:
        con.execute(
            """
            INSERT INTO push_subscriptions (imap_user, endpoint, p256dh, auth, user_agent)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
                imap_user=excluded.imap_user,
                p256dh=excluded.p256dh,
                auth=excluded.auth,
                user_agent=excluded.user_agent
            """,
            (sess["imap_user"], endpoint, p256dh, auth_key, body.user_agent or ""),
        )
        con.commit()
    try:
        subscription = {
            "endpoint": endpoint,
            "keys": {"p256dh": p256dh, "auth": auth_key},
        }
        await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/push/subscriptions",
            {
                "operation": "upsert",
                "mail_login": sess["mail_login"],
                "subscription": subscription,
                "user_agent": body.user_agent or "",
            },
        )
        await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/notification-settings",
            {
                "operation": "set",
                "mail_login": sess["mail_login"],
                "mail_password": sess["imap_pass"],
            },
        )
    except Exception as exc:
        log.warning(
            "push subscription credential sync failed for %s: %s",
            sess["imap_user"],
            exc,
        )
    return {"ok": True}


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(request: Request):
    sess = get_session_or_401(request)
    payload = await request.json()
    endpoint = str(payload.get("endpoint") or "").strip()
    with _db_connection() as con:
        if endpoint:
            con.execute(
                "DELETE FROM push_subscriptions WHERE imap_user = ? AND endpoint = ?",
                (sess["imap_user"], endpoint),
            )
        else:
            con.execute(
                "DELETE FROM push_subscriptions WHERE imap_user = ?",
                (sess["imap_user"],),
            )
        con.commit()
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/push/subscriptions",
            {
                "operation": "delete",
                "mail_login": sess["mail_login"],
                "endpoint": endpoint,
            },
        )
    except Exception as exc:
        log.warning(
            "push subscription registry delete failed for %s: %s",
            sess["imap_user"],
            exc,
        )
    return {"ok": True}


@app.post("/api/push/test")
async def api_push_test(request: Request):
    """Send a test push notification to all the user's subscribed endpoints."""
    sess = get_session_or_401(request)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE imap_user = ?",
            (sess["imap_user"],),
        ).fetchall()
    if not rows:
        return JSONResponse({"error": "No subscriptions"}, status_code=404)
    payload = {
        "title": "RuPochta Mail",
        "body": "Тестовое уведомление работает ✓",
        "icon": "/static/brand/icon-192.png",
        "tag": "rupochta-mail-test",
        "url": "/",
    }
    sent = 0
    failed = 0
    for r in rows:
        sub = {
            "endpoint": r["endpoint"],
            "keys": {"p256dh": r["p256dh"], "auth": r["auth"]},
        }
        ok, _ = await asyncio.get_event_loop().run_in_executor(
            None, _push_send_via_panel, sub, payload
        )
        if ok:
            sent += 1
        else:
            failed += 1
    return {"ok": True, "sent": sent, "failed": failed, "total": len(rows)}


# Serve PWA manifest + service worker from static/ (they're plain files)
@app.get("/manifest.webmanifest")
async def serve_manifest():
    p = Path(CFG.STATIC_DIR) / "manifest.webmanifest"
    if p.exists():
        return FileResponse(p, media_type="application/manifest+json")
    return JSONResponse({"error": "manifest missing"}, status_code=404)


@app.get("/sw.js")
async def serve_sw():
    p = Path(CFG.STATIC_DIR) / "sw.js"
    if p.exists():
        # Service worker must be served with this header to be allowed
        # at the root scope ("/")
        return FileResponse(
            p,
            media_type="application/javascript",
            headers={
                "Service-Worker-Allowed": "/",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )
    return JSONResponse({"error": "sw missing"}, status_code=404)


# ---------------------------------------------------------------------------
# Sieve filter rules — server-side email rules via managesieve (port 4190)
# ---------------------------------------------------------------------------
# Storage: SQLite `sieve_rules` table keeps a canonical JSON representation
# per user. On save, we compile the rules into a single Sieve script and
# upload it to the user's managesieve server (Dovecot pigeonhole). If
# managesieve is unavailable, we still persist the rules — and they will
# be applied best-effort on message fetch via a lightweight matcher.

MANAGESIEVE_HOST = os.environ.get("MANAGESIEVE_HOST", CFG.IMAP_HOST)
MANAGESIEVE_PORT = int(os.environ.get("MANAGESIEVE_PORT", "4190"))
SIEVE_SCRIPT_NAME = "jmail-rules"


def _sieve_escape(value: str) -> str:
    """Escape a string literal for Sieve (RFC 5228)."""
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )


def _normalize_forwarding_address(
    address: str,
    enabled: bool,
    own_address: str,
) -> str:
    raw_value = str(address or "")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_value):
        raise HTTPException(400, "Укажите один почтовый адрес без имени получателя")
    value = raw_value.strip()
    if not value:
        if enabled:
            raise HTTPException(400, "Укажите один адрес для пересылки")
        return ""
    if len(value) > 254:
        raise HTTPException(400, "Адрес пересылки слишком длинный")
    if any(char in value for char in ",;<>"):
        raise HTTPException(400, "Укажите один почтовый адрес без имени получателя")
    if value.count("@") != 1:
        raise HTTPException(400, "Укажите корректный почтовый адрес")

    local, domain = value.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
    ):
        raise HTTPException(400, "Укажите корректный почтовый адрес")
    if (
        not domain
        or len(domain) > 253
        or domain.startswith(".")
        or domain.endswith(".")
    ):
        raise HTTPException(400, "Укажите корректный почтовый адрес")
    labels = domain.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[A-Za-z0-9-]+", label)
        for label in labels
    ):
        raise HTTPException(400, "Укажите корректный почтовый адрес")

    normalized = f"{local}@{domain.lower()}"
    if len(normalized) > 254:
        raise HTTPException(400, "Адрес пересылки слишком длинный")
    if normalized.lower() == str(own_address or "").strip().lower():
        raise HTTPException(400, "Нельзя пересылать почту на этот же ящик")
    return normalized


def _normalize_autoreply_settings(
    enabled: bool,
    subject: str,
    body: str,
    start_date: Optional[str],
    end_date: Optional[str],
    repeat_days: int,
) -> Dict[str, Any]:
    normalized_subject = str(subject or "").strip()
    normalized_body = str(body or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized_subject) > 200:
        raise HTTPException(400, "Тема автоответа слишком длинная")
    if len(normalized_body) > 5000:
        raise HTTPException(400, "Текст автоответа слишком длинный")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized_subject):
        raise HTTPException(400, "Тема автоответа содержит недопустимые символы")
    if any(
        (ord(char) < 32 and char not in "\n\t") or ord(char) == 127
        for char in normalized_body
    ):
        raise HTTPException(400, "Текст автоответа содержит недопустимые символы")
    if enabled and not normalized_subject:
        raise HTTPException(400, "Укажите тему автоответа")
    if enabled and not normalized_body.strip():
        raise HTTPException(400, "Укажите текст автоответа")
    try:
        normalized_repeat_days = int(repeat_days)
    except (TypeError, ValueError):
        raise HTTPException(400, "Интервал повторного ответа должен быть от 1 до 30 дней")
    if not 1 <= normalized_repeat_days <= 30:
        raise HTTPException(400, "Интервал повторного ответа должен быть от 1 до 30 дней")

    normalized_dates: Dict[str, Optional[str]] = {}
    for field, raw_value, label in (
        ("start_date", start_date, "даты начала"),
        ("end_date", end_date, "даты окончания"),
    ):
        value = str(raw_value or "").strip()
        if not value:
            normalized_dates[field] = None
            continue
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(400, f"Укажите корректное значение {label}")
        normalized_dates[field] = parsed.date().isoformat()
    if (
        normalized_dates["start_date"]
        and normalized_dates["end_date"]
        and normalized_dates["start_date"] > normalized_dates["end_date"]
    ):
        raise HTTPException(400, "Дата начала не может быть позже даты окончания")
    return {
        "enabled": bool(enabled),
        "subject": normalized_subject,
        "body": normalized_body,
        "start_date": normalized_dates["start_date"],
        "end_date": normalized_dates["end_date"],
        "repeat_days": normalized_repeat_days,
    }


def _sieve_multiline_text(value: str) -> str:
    normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    escaped = "\n".join(
        f".{line}" if line.startswith(".") else line
        for line in normalized.split("\n")
    )
    return f"text:\n{escaped}\n.\n;"


def _compile_sieve_script(
    rules: List[Dict[str, Any]],
    forwarding: Optional[Dict[str, Any]] = None,
    autoreply: Optional[Dict[str, Any]] = None,
) -> str:
    """Convert a list of rule dicts into a Sieve script.

    Rule shape: {
      name, enabled, priority,
      conditions: [{field: "from"|"to"|"subject"|"body", op: "contains"|"is", value: str}],
      match_all: bool,     # AND vs OR
      actions: [{type: "move"|"copy"|"redirect"|"mark_read"|"flag"|"discard", value: str}]
    }
    """
    autoreply = autoreply or {}
    extensions = ["fileinto", "imap4flags", "copy"]
    if autoreply.get("enabled"):
        extensions.append("vacation")
        if autoreply.get("start_date") or autoreply.get("end_date"):
            extensions.extend(["date", "relational"])
    require = ", ".join(f'"{extension}"' for extension in extensions)
    lines: List[str] = [
        '# Auto-generated by jmail — do not edit manually',
        f"require [{require}];",
        '',
    ]
    if autoreply.get("enabled"):
        subject = _sieve_escape(str(autoreply.get("subject") or ""))
        repeat_days = int(autoreply.get("repeat_days") or 1)
        vacation = (
            f'vacation :days {repeat_days} :subject "{subject}" '
            f'{_sieve_multiline_text(str(autoreply.get("body") or ""))}'
        )
        date_tests = []
        if autoreply.get("start_date"):
            date_tests.append(
                'currentdate :value "ge" "date" '
                f'"{_sieve_escape(str(autoreply["start_date"]))}"'
            )
        if autoreply.get("end_date"):
            date_tests.append(
                'currentdate :value "le" "date" '
                f'"{_sieve_escape(str(autoreply["end_date"]))}"'
            )
        lines.append("# Managed auto-reply")
        if date_tests:
            test = (
                date_tests[0]
                if len(date_tests) == 1
                else "allof (\n  " + ",\n  ".join(date_tests) + "\n)"
            )
            lines.append(f"if {test} {{")
            vacation_lines = vacation.splitlines()
            lines.append(f"  {vacation_lines[0]}")
            lines.extend(vacation_lines[1:])
            lines.append("}")
        else:
            lines.extend(vacation.splitlines())
        lines.append("")
    forwarding = forwarding or {}
    if forwarding.get("enabled") and forwarding.get("address"):
        target = _sieve_escape(str(forwarding["address"]))
        lines.append("# Managed forwarding")
        if forwarding.get("keep_copy", True):
            lines.append(f'redirect :copy "{target}";')
        else:
            lines.append(f'redirect "{target}";')
            lines.append("stop;")
        lines.append("")
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        conds = rule.get("conditions") or []
        acts = rule.get("actions") or []
        if not conds or not acts:
            continue
        match_all = bool(rule.get("match_all", True))
        test_parts: List[str] = []
        for c in conds:
            field = (c.get("field") or "").lower()
            op = (c.get("op") or "contains").lower()
            value = _sieve_escape(str(c.get("value") or ""))
            if not field or not value:
                continue
            comparator = "i;ascii-casemap"
            match_type = ":contains" if op == "contains" else ":is"
            if field in ("from", "to", "cc", "bcc"):
                header_name = field.capitalize()
                test_parts.append(
                    f'address :comparator "{comparator}" {match_type} '
                    f'"{header_name}" "{value}"'
                )
            elif field == "subject":
                test_parts.append(
                    f'header :comparator "{comparator}" {match_type} '
                    f'"Subject" "{value}"'
                )
            elif field == "body":
                test_parts.append(
                    f'body :comparator "{comparator}" :text {match_type} "{value}"'
                )
        if not test_parts:
            continue
        if len(test_parts) == 1:
            test = test_parts[0]
        else:
            joiner = "allof" if match_all else "anyof"
            test = f"{joiner} (\n    " + ",\n    ".join(test_parts) + "\n  )"
        # Build actions block
        action_lines: List[str] = []
        for a in acts:
            t = (a.get("type") or "").lower()
            v = _sieve_escape(str(a.get("value") or ""))
            if t == "move":
                action_lines.append(f'  fileinto "{v}";')
                action_lines.append('  stop;')
            elif t == "copy":
                action_lines.append(f'  fileinto :copy "{v}";')
            elif t == "redirect":
                action_lines.append(f'  redirect "{v}";')
            elif t == "mark_read":
                action_lines.append('  setflag "\\\\Seen";')
            elif t == "flag":
                action_lines.append('  setflag "\\\\Flagged";')
            elif t == "discard":
                action_lines.append('  discard;')
                action_lines.append('  stop;')
        if not action_lines:
            continue
        lines.append(f'# Rule: {rule.get("name", "unnamed")}')
        lines.append(f'if {test} {{')
        lines.extend(action_lines)
        lines.append('}')
        lines.append('')
    return "\n".join(lines)


def _managesieve_put_script(user: str, password: str, script: str) -> Tuple[bool, str]:
    """Upload a script via managesieve protocol. Returns (ok, message).

    managesieve is a simple text protocol over SSL/STARTTLS. We implement
    a minimal client that logs in and issues PUTSCRIPT + SETACTIVE.
    If the managesieve port is unreachable, we return (False, reason).
    """
    import socket
    import ssl as _ssl

    try:
        sock = socket.create_connection((MANAGESIEVE_HOST, MANAGESIEVE_PORT), timeout=10)
    except Exception as e:
        return False, f"connect failed: {e}"
    try:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE

        def _recv_until(buf_state: Dict[str, bytes]) -> List[str]:
            """Read response lines until we see OK/NO/BYE."""
            lines: List[str] = []
            while True:
                # Read line by line from the accumulated buffer
                while b"\r\n" not in buf_state["buf"]:
                    chunk = sock.recv(4096)
                    if not chunk:
                        return lines
                    buf_state["buf"] += chunk
                line, _, rest = buf_state["buf"].partition(b"\r\n")
                buf_state["buf"] = rest
                decoded = line.decode("utf-8", errors="replace")
                lines.append(decoded)
                upper = decoded.upper().lstrip()
                if upper.startswith("OK") or upper.startswith("NO") or upper.startswith("BYE"):
                    return lines

        def _send(line: str):
            sock.sendall((line + "\r\n").encode("utf-8"))

        buf_state = {"buf": b""}
        # Read banner
        banner = _recv_until(buf_state)
        caps = " ".join(banner).upper()
        if "STARTTLS" in caps:
            _send("STARTTLS")
            _recv_until(buf_state)
            sock = ctx.wrap_socket(sock, server_hostname=MANAGESIEVE_HOST)
            buf_state = {"buf": b""}
            _recv_until(buf_state)  # post-TLS capabilities
        # Login — SASL PLAIN
        import base64 as _b64ms
        sasl = _b64ms.b64encode(
            b"\x00" + user.encode("utf-8") + b"\x00" + password.encode("utf-8")
        ).decode("ascii")
        _send(f'AUTHENTICATE "PLAIN" "{sasl}"')
        auth_resp = _recv_until(buf_state)
        if not any(l.upper().startswith("OK") for l in auth_resp):
            return False, f"auth failed: {auth_resp}"
        # PUTSCRIPT
        script_bytes = script.encode("utf-8")
        _send(f'PUTSCRIPT "{SIEVE_SCRIPT_NAME}" {{{len(script_bytes)}+}}')
        sock.sendall(script_bytes + b"\r\n")
        put_resp = _recv_until(buf_state)
        if not any(l.upper().lstrip().startswith("OK") for l in put_resp):
            return False, f"putscript failed: {put_resp}"
        _send(f'SETACTIVE "{SIEVE_SCRIPT_NAME}"')
        act_resp = _recv_until(buf_state)
        if not any(l.upper().lstrip().startswith("OK") for l in act_resp):
            return False, f"setactive failed: {act_resp}"
        _send("LOGOUT")
        return True, "ok"
    finally:
        try:
            sock.close()
        except Exception:
            pass


class SieveRuleBody(BaseModel):
    name: str
    enabled: bool = True
    priority: int = 100
    match_all: bool = True
    conditions: List[Dict[str, Any]]
    actions: List[Dict[str, Any]]


async def _validate_sieve_rule_targets(
    body: SieveRuleBody,
    sess: Dict[str, Any],
) -> None:
    targets = {
        str(action.get("value") or "").strip()
        for action in body.actions
        if str(action.get("type") or "").lower() in {"move", "copy"}
    }
    targets.discard("")
    if not targets:
        return
    folders = await asyncio.get_event_loop().run_in_executor(
        None,
        imap_list_folders,
        sess["imap_user"],
        sess["imap_pass"],
    )
    available = {
        str(folder.get("name") or "").strip().casefold()
        for folder in folders
    }
    missing = sorted(
        target for target in targets if target.casefold() not in available
    )
    if missing:
        raise HTTPException(
            400,
            f"Папка для правила не существует: {', '.join(missing)}",
        )


@app.get("/api/sieve/rules")
async def api_sieve_list(request: Request):
    sess = get_session_or_401(request)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, name, enabled, priority, conditions_json, actions_json, "
            "created_at, updated_at FROM sieve_rules "
            "WHERE imap_user = ? ORDER BY priority ASC, id ASC",
            (sess["imap_user"],),
        ).fetchall()
    items = []
    for r in rows:
        items.append({
            "id": r["id"],
            "name": r["name"],
            "enabled": bool(r["enabled"]),
            "priority": r["priority"],
            "conditions": json.loads(r["conditions_json"] or "[]"),
            "actions": json.loads(r["actions_json"] or "[]"),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return {"ok": True, "rules": items, "count": len(items)}


def _sieve_sync_to_server(sess: Dict[str, Any]) -> Dict[str, Any]:
    """Re-upload the user's rules to managesieve. Returns status dict."""
    imap_user = sess["imap_user"]
    with _sieve_user_lock(imap_user):
        forwarding = db_get_forwarding(imap_user)
        forwarding_revision = forwarding["revision"]
        autoreply = db_get_autoreply(imap_user)
        autoreply_revision = autoreply["revision"]
        with _db_connection() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT name, enabled, priority, conditions_json, actions_json "
                "FROM sieve_rules WHERE imap_user = ? AND enabled = 1 "
                "ORDER BY priority ASC, id ASC",
                (imap_user,),
            ).fetchall()
        rules = []
        for r in rows:
            rules.append({
                "name": r["name"],
                "enabled": True,
                "priority": r["priority"],
                "match_all": True,
                "conditions": json.loads(r["conditions_json"] or "[]"),
                "actions": json.loads(r["actions_json"] or "[]"),
            })
        script = _compile_sieve_script(rules, forwarding, autoreply)
        try:
            ok, _ = _managesieve_put_script(
                imap_user, sess["imap_pass"], script
            )
        except Exception:
            ok = False

        if ok:
            active = bool(forwarding["enabled"])
            safe_message = (
                "Применено"
                if active
                else "Пересылка отключена"
            )
        else:
            active = False
            safe_message = "Почтовый сервер временно недоступен"
            log.warning("ManageSieve sync failed for %s", imap_user)
        forwarding = db_record_forwarding_sync(
            imap_user,
            active,
            safe_message,
            forwarding_revision,
        )
        stale = bool(forwarding.pop("_sync_stale", False))
        result_message = forwarding["sync_message"] if stale else safe_message
        if ok:
            autoreply_active = bool(autoreply["enabled"])
            autoreply_message = (
                "Применено"
                if autoreply_active
                else "Автоответчик отключен"
            )
        else:
            autoreply_active = False
            autoreply_message = "Почтовый сервер временно недоступен"
        autoreply = db_record_autoreply_sync(
            imap_user,
            autoreply_active,
            autoreply_message,
            autoreply_revision,
        )
        autoreply.pop("_sync_stale", None)
    return {
        "managesieve_ok": ok,
        "managesieve_msg": result_message,
        "script_size": len(script),
        "rules_compiled": len(rules),
        "forwarding": _forwarding_public_payload(forwarding),
        "autoreply": _autoreply_public_payload(autoreply),
    }


def _set_and_sync_forwarding(
    sess: Dict[str, Any],
    enabled: bool,
    address: str,
    keep_copy: bool,
) -> Dict[str, Any]:
    imap_user = sess["imap_user"]
    with _sieve_user_lock(imap_user):
        db_set_forwarding(imap_user, enabled, address, keep_copy)
        _sieve_sync_to_server(sess)
        return db_get_forwarding(imap_user)


def _set_and_sync_autoreply(
    sess: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    imap_user = sess["imap_user"]
    with _sieve_user_lock(imap_user):
        db_set_autoreply(
            imap_user,
            settings["enabled"],
            settings["subject"],
            settings["body"],
            settings["start_date"],
            settings["end_date"],
            settings["repeat_days"],
        )
        _sieve_sync_to_server(sess)
        return db_get_autoreply(imap_user)


@app.post("/api/sieve/rules")
async def api_sieve_create(body: SieveRuleBody, request: Request):
    sess = get_session_or_401(request)
    await _validate_sieve_rule_targets(body, sess)
    with _db_connection() as con:
        cur = con.execute(
            """
            INSERT INTO sieve_rules
                (imap_user, name, enabled, priority, conditions_json, actions_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                sess["imap_user"],
                body.name[:200],
                1 if body.enabled else 0,
                body.priority,
                json.dumps(body.conditions, ensure_ascii=False),
                json.dumps(body.actions, ensure_ascii=False),
            ),
        )
        con.commit()
        row_id = cur.lastrowid
    sync = await asyncio.get_event_loop().run_in_executor(
        None, _sieve_sync_to_server, sess
    )
    return {"ok": True, "id": row_id, "sync": sync}


@app.put("/api/sieve/rules/{rule_id}")
async def api_sieve_update(rule_id: int, body: SieveRuleBody, request: Request):
    sess = get_session_or_401(request)
    await _validate_sieve_rule_targets(body, sess)
    with _db_connection() as con:
        cur = con.execute(
            """
            UPDATE sieve_rules
            SET name=?, enabled=?, priority=?, conditions_json=?, actions_json=?,
                updated_at=datetime('now')
            WHERE id=? AND imap_user=?
            """,
            (
                body.name[:200],
                1 if body.enabled else 0,
                body.priority,
                json.dumps(body.conditions, ensure_ascii=False),
                json.dumps(body.actions, ensure_ascii=False),
                rule_id,
                sess["imap_user"],
            ),
        )
        con.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "Not found"}, status_code=404)
    sync = await asyncio.get_event_loop().run_in_executor(
        None, _sieve_sync_to_server, sess
    )
    return {"ok": True, "id": rule_id, "sync": sync}


@app.delete("/api/sieve/rules/{rule_id}")
async def api_sieve_delete(rule_id: int, request: Request):
    sess = get_session_or_401(request)
    with _db_connection() as con:
        cur = con.execute(
            "DELETE FROM sieve_rules WHERE id=? AND imap_user=?",
            (rule_id, sess["imap_user"]),
        )
        con.commit()
    if cur.rowcount == 0:
        return JSONResponse({"error": "Not found"}, status_code=404)
    sync = await asyncio.get_event_loop().run_in_executor(
        None, _sieve_sync_to_server, sess
    )
    return {"ok": True, "deleted": rule_id, "sync": sync}


@app.post("/api/sieve/sync")
async def api_sieve_sync(request: Request):
    """Force re-upload of the current rule set to managesieve."""
    sess = get_session_or_401(request)
    sync = await asyncio.get_event_loop().run_in_executor(
        None, _sieve_sync_to_server, sess
    )
    return {"ok": True, "sync": sync}


# ---------------------------------------------------------------------------
# Send Later / Scheduled sends
# ---------------------------------------------------------------------------

def _parse_iso_future(iso_str: str) -> Optional[datetime]:
    """Parse ISO datetime and validate it's in the future. Returns aware UTC datetime."""
    if not iso_str:
        return None
    try:
        # Accept both with/without timezone
        s = iso_str.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            # Assume UTC if no tz
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


@app.post("/api/scheduled/send")
async def api_schedule_send(request: Request):
    """Queue a message for future delivery.

    Body (JSON or multipart): same as /api/messages/send + scheduled_at (ISO).
    """
    sess = get_session_or_401(request)
    payload = await _parse_compose_request(request, draft_mode=False)
    scheduled_at_raw = (
        payload.get("scheduled_at")
        or payload.get("scheduledAt")
        or ""
    )
    dt = _parse_iso_future(str(scheduled_at_raw))
    if not dt:
        return JSONResponse({"error": "scheduled_at must be a valid ISO datetime"}, status_code=400)
    now = datetime.now(timezone.utc)
    if dt <= now + timedelta(seconds=10):
        return JSONResponse({"error": "scheduled_at must be at least 10 seconds in the future"}, status_code=400)
    if dt > now + timedelta(days=365):
        return JSONResponse({"error": "scheduled_at cannot be more than 1 year ahead"}, status_code=400)

    to = parse_address_list(payload.get("to"))
    cc = parse_address_list(payload.get("cc") or "")
    bcc = parse_address_list(payload.get("bcc") or "")
    if not to:
        return JSONResponse({"error": "Поле 'Кому' не должно быть пустым"}, status_code=400)
    subject = str(payload.get("subject") or "").strip() or "(без темы)"
    body_html, body_text = _compose_body_variants(
        str(payload.get("body") or ""),
        payload.get("body_text"),
    )
    from_addr = formataddr((sess["display_name"], sess["from_address"]))

    try:
        row_id = _insert_scheduled_send(
            imap_user=sess["imap_user"],
            imap_pass=sess["imap_pass"],
            from_addr=from_addr,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            in_reply_to=payload.get("in_reply_to"),
            attachments=payload.get("attachments"),
            scheduled_at=dt.replace(microsecond=0).isoformat(),
            origin="scheduled",
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    # Delete draft if this schedule came from a draft compose
    if payload.get("draft_uid"):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                imap_delete_message,
                sess["imap_user"],
                sess["imap_pass"],
                "Drafts",
                str(payload["draft_uid"]),
            )
        except Exception as e:
            log.info("schedule: cleanup draft %s failed: %s", payload["draft_uid"], e)

    return {
        "ok": True,
        "id": row_id,
        "scheduled_at": dt.isoformat(),
        "to": to,
        "subject": subject,
    }


@app.get("/api/scheduled")
async def api_scheduled_list(request: Request):
    """List the current user's scheduled (or recently sent) send-laters."""
    sess = get_session_or_401(request)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, from_addr, to_list, cc_list, bcc_list, subject, "
            "scheduled_at, status, attempts, last_error, created_at, sent_at, origin "
            "FROM scheduled_sends WHERE imap_user = ? "
            "ORDER BY CASE status "
            "         WHEN 'failed' THEN 0 "
            "         WHEN 'pending' THEN 1 "
            "         WHEN 'sending' THEN 2 "
            "         WHEN 'sent' THEN 3 "
            "         ELSE 4 END, scheduled_at DESC LIMIT 100",
            (sess["imap_user"],),
        ).fetchall()
    result = [_scheduled_send_item(r) for r in rows]
    return {"ok": True, "items": result, "count": len(result)}


@app.delete("/api/scheduled/{row_id}")
async def api_scheduled_cancel(row_id: int, request: Request):
    """Cancel a pending scheduled send."""
    sess = get_session_or_401(request)
    with _db_connection() as con:
        cur = con.execute(
            "UPDATE scheduled_sends SET status='cancelled', imap_pass_enc='' "
            "WHERE id = ? AND imap_user = ? AND status='pending'",
            (row_id, sess["imap_user"]),
        )
        con.commit()
    if cur.rowcount == 0:
        return JSONResponse(
            {"error": "Not found or already sent/cancelled"},
            status_code=404,
        )
    return {"ok": True, "cancelled": row_id}


@app.post("/api/scheduled/{row_id}/retry")
async def api_scheduled_retry(row_id: int, request: Request):
    """Retry a failed queued send immediately using the current session password."""
    sess = get_session_or_401(request)
    loop = asyncio.get_event_loop()
    try:
        send_result = await loop.run_in_executor(
            None,
            lambda: _deliver_scheduled_send_row(
                row_id,
                allowed_statuses=("failed",),
                user_override=sess["imap_user"],
                password_override=sess["imap_pass"],
            ),
        )
        _invalidate_mailbox_caches(sess)
        return {
            "ok": True,
            "id": row_id,
            "smtp_ok": bool(send_result.get("smtp_ok")),
            "sent_copy_ok": bool(send_result.get("sent_copy_ok")),
            "sent_folder": send_result.get("sent_folder") or "",
            "warnings": [] if send_result.get("sent_copy_ok") else ["SMTP ok, но копия в «Отправленные» не сохранена"],
        }
    except Exception as e:
        return JSONResponse({"error": f"Retry: {e}"}, status_code=502)


# ---------------------------------------------------------------------------
# Snooze Inbox
# ---------------------------------------------------------------------------

class SnoozeBody(BaseModel):
    folder: str = "INBOX"
    uids: List[str]
    wake_at: str  # ISO


@app.post("/api/snooze")
async def api_snooze(body: SnoozeBody, request: Request):
    """Move messages to Snoozed folder and schedule wake-up."""
    sess = get_session_or_401(request)
    dt = _parse_iso_future(body.wake_at)
    if not dt:
        return JSONResponse({"error": "wake_at must be a valid future ISO datetime"}, status_code=400)
    now = datetime.now(timezone.utc)
    if dt <= now + timedelta(minutes=1):
        return JSONResponse({"error": "wake_at must be at least 1 minute in the future"}, status_code=400)
    uids = [u for u in (body.uids or []) if u]
    if not uids:
        return JSONResponse({"error": "uids is required"}, status_code=400)

    loop = asyncio.get_event_loop()
    # Fetch message metadata for each UID (needed for unique Message-ID + subject/from)
    metas: List[Dict[str, Any]] = []
    try:
        for uid in uids:
            msg = await loop.run_in_executor(
                None,
                imap_fetch_message,
                sess["imap_user"],
                sess["imap_pass"],
                body.folder,
                uid,
            )
            mid = _strip_angle(msg.get("message_id") or "")
            if not mid:
                continue
            metas.append({
                "uid": uid,
                "message_id": mid,
                "subject": msg.get("subject") or "",
                "from": msg.get("from") or "",
            })
    except Exception as e:
        return JSONResponse({"error": f"IMAP fetch: {e}"}, status_code=502)

    if not metas:
        return JSONResponse({"error": "No messages with valid Message-ID"}, status_code=400)

    # Ensure Snoozed folder exists, then move
    def _ensure_and_move():
        with imap_session(sess["imap_user"], sess["imap_pass"]) as conn:
            # CREATE is idempotent-ish: ignore error if already exists
            try:
                conn.create('"Snoozed"')
            except Exception:
                pass
            try:
                conn.subscribe('"Snoozed"')
            except Exception:
                pass
            typ, _ = conn.select(f'"{body.folder}"', readonly=False)
            if typ != "OK":
                raise RuntimeError(f"Cannot select {body.folder}")
            _move_uids(conn, ",".join(str(m["uid"]) for m in metas), "Snoozed")

    try:
        await loop.run_in_executor(None, _ensure_and_move)
    except Exception as e:
        return JSONResponse({"error": f"IMAP move: {e}"}, status_code=502)

    imap_pass_enc = encrypt_secret(sess["imap_pass"])
    wake_iso = dt.replace(microsecond=0).isoformat()
    with _db_connection() as con:
        for m in metas:
            con.execute(
                """
                INSERT INTO snoozed_messages
                    (imap_user, imap_pass_enc, message_id, subject, from_addr,
                     original_folder, wake_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sess["imap_user"],
                    imap_pass_enc,
                    m["message_id"],
                    m["subject"][:500],
                    m["from"][:200],
                    body.folder,
                    wake_iso,
                ),
            )
        con.commit()
    return {"ok": True, "count": len(metas), "wake_at": wake_iso}


@app.get("/api/snoozed")
async def api_snoozed_list(request: Request):
    """List pending snoozed messages for current user."""
    sess = get_session_or_401(request)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, message_id, subject, from_addr, original_folder, "
            "snoozed_at, wake_at, status, last_error, woken_at "
            "FROM snoozed_messages WHERE imap_user = ? "
            "ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'waking' THEN 1 ELSE 2 END, "
            "wake_at LIMIT 100",
            (sess["imap_user"],),
        ).fetchall()
    return {
        "ok": True,
        "items": [dict(r) for r in rows],
        "count": len(rows),
    }


@app.delete("/api/snoozed/{row_id}")
async def api_snoozed_cancel(row_id: int, request: Request):
    """Un-snooze immediately: move back to original folder, mark cancelled."""
    sess = get_session_or_401(request)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM snoozed_messages WHERE id=? AND imap_user=? AND status='pending'",
            (row_id, sess["imap_user"]),
        ).fetchone()
    if not row:
        return JSONResponse({"error": "Not found or already woken"}, status_code=404)

    loop = asyncio.get_event_loop()

    def _unsnooze():
        with imap_session(sess["imap_user"], sess["imap_pass"]) as conn:
            typ, _ = conn.select('"Snoozed"', readonly=False)
            if typ != "OK":
                raise RuntimeError("Cannot select Snoozed")
            mid = row["message_id"]
            search_id = f"<{mid}>" if not mid.startswith("<") else mid
            typ, data = conn.uid("SEARCH", "HEADER", "Message-ID", search_id)
            if typ != "OK" or not data or not data[0]:
                raise RuntimeError("Message not in Snoozed")
            found = [
                item.decode() if isinstance(item, (bytes, bytearray)) else str(item)
                for item in data[0].split()
            ]
            if not found:
                raise RuntimeError("Not found")
            _move_uids(conn, found[-1], row["original_folder"] or "INBOX")

    try:
        await loop.run_in_executor(None, _unsnooze)
    except Exception as e:
        return JSONResponse({"error": f"IMAP: {e}"}, status_code=502)

    with _db_connection() as con:
        con.execute(
            "UPDATE snoozed_messages SET status='cancelled', imap_pass_enc='', "
            "woken_at=datetime('now') WHERE id=?",
            (row_id,),
        )
        con.commit()
    return {"ok": True, "cancelled": row_id}


# ---------------------------------------------------------------------------
# AI features: Smart Reply + Inbox Triage
# ---------------------------------------------------------------------------

@app.post("/api/messages/{uid}/smart_reply")
async def api_smart_reply(uid: str, request: Request, folder: str = Query("INBOX")):
    """Generate 3 suggested replies for a message via Ollama.
    Returns {suggestions: [{tone, label, text}, ...]} — empty if AI unavailable.
    """
    sess = get_session_or_401(request)
    if not CFG.INTERNAL_TOKEN:
        return JSONResponse(
            {"error": "AI features are not configured on this server"},
            status_code=503,
        )
    loop = asyncio.get_event_loop()
    try:
        message = await loop.run_in_executor(
            None,
            imap_fetch_message,
            sess["imap_user"],
            sess["imap_pass"],
            folder,
            uid,
        )
    except Exception as e:
        return JSONResponse({"error": f"IMAP: {e}"}, status_code=502)
    suggestions = await loop.run_in_executor(
        None, ai_smart_reply_suggestions, message
    )
    if not suggestions:
        return JSONResponse(
            {"error": "AI service returned no suggestions", "suggestions": []},
            status_code=502,
        )
    return {"ok": True, "suggestions": suggestions, "uid": uid, "folder": folder}


@app.get("/api/triage")
async def api_triage_list(
    request: Request,
    folder: str = Query("INBOX"),
    category: Optional[str] = Query(None),
):
    """Return triage categories for messages in folder.

    Lightweight: reads from SQLite cache (populated by background classifier or
    on-demand POST /api/messages/triage/classify). Frontend uses this to filter
    message list tabs.

    Returns {categories: {message_id: {category, confidence}}, totals: {...}}.
    """
    sess = get_session_or_401(request)
    valid_cats = {"important", "updates", "newsletters", "promo"}
    if category and category not in valid_cats:
        return JSONResponse({"error": f"category must be one of {valid_cats}"}, status_code=400)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        params: List[Any] = [sess["imap_user"]]
        where = "imap_user = ?"
        if folder:
            where += " AND folder = ?"
            params.append(folder)
        if category:
            where += " AND category = ?"
            params.append(category)
        rows = con.execute(
            f"SELECT message_id, category, confidence FROM ai_triage WHERE {where}",
            params,
        ).fetchall()
        # totals regardless of category filter
        total_rows = con.execute(
            "SELECT category, COUNT(*) AS cnt FROM ai_triage "
            "WHERE imap_user = ? AND folder = ? GROUP BY category",
            (sess["imap_user"], folder),
        ).fetchall()
    categories: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        categories[r["message_id"]] = {
            "category": r["category"],
            "confidence": int(r["confidence"] or 0),
        }
    totals = {c: 0 for c in valid_cats}
    for r in total_rows:
        totals[r["category"]] = int(r["cnt"])
    return {"ok": True, "folder": folder, "categories": categories, "totals": totals}


class TriageClassifyBody(BaseModel):
    folder: str = "INBOX"
    limit: int = Field(20, ge=1, le=50)
    force: bool = False  # re-classify even if cached


@app.post("/api/triage/classify")
async def api_triage_classify(body: TriageClassifyBody, request: Request):
    """Classify the N most recent messages in folder that don't have a cached category yet.

    Intended to be called from the frontend on folder open (debounced).
    """
    sess = get_session_or_401(request)
    if not CFG.INTERNAL_TOKEN:
        return JSONResponse(
            {"error": "AI features are not configured on this server"},
            status_code=503,
        )
    loop = asyncio.get_event_loop()
    try:
        listing = await loop.run_in_executor(
            None,
            imap_message_list,
            sess["imap_user"],
            sess["imap_pass"],
            body.folder,
            1,
            body.limit,
            "",
        )
    except Exception as e:
        return JSONResponse({"error": f"IMAP: {e}"}, status_code=502)
    messages = listing.get("messages") or []
    if not messages:
        return {"ok": True, "classified": 0, "skipped": 0}

    # Filter to only unclassified
    msg_ids = [str(m.get("message_id") or "") for m in messages if m.get("message_id")]
    already: set = set()
    if not body.force and msg_ids:
        with _db_connection() as con:
            rows = con.execute(
                f"SELECT message_id FROM ai_triage WHERE imap_user = ? "
                f"AND message_id IN ({','.join('?' * len(msg_ids))})",
                [sess["imap_user"]] + msg_ids,
            ).fetchall()
            already = {r[0] for r in rows}

    classified = 0
    skipped = 0
    attempted = 0
    for meta in messages:
        message_id = str(meta.get("message_id") or "").strip()
        if not message_id:
            skipped += 1
            continue
        if message_id in already:
            skipped += 1
            continue
        if attempted >= TRIAGE_CLASSIFY_ATTEMPTS_PER_REQUEST:
            break
        attempted += 1
        # Fetch full body
        try:
            full = await loop.run_in_executor(
                None,
                imap_fetch_message,
                sess["imap_user"],
                sess["imap_pass"],
                body.folder,
                str(meta.get("uid")),
            )
        except Exception as e:
            log.debug("triage fetch %s failed: %s", meta.get("uid"), e)
            skipped += 1
            continue
        result = await loop.run_in_executor(None, ai_triage_categorize, full)
        if not result:
            skipped += 1
            continue
        with _db_connection() as con:
            con.execute(
                """
                INSERT INTO ai_triage (imap_user, message_id, folder, category, confidence, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(imap_user, message_id) DO UPDATE SET
                    category = excluded.category,
                    confidence = excluded.confidence,
                    reason = excluded.reason,
                    folder = excluded.folder
                """,
                (
                    sess["imap_user"],
                    message_id,
                    body.folder,
                    result["category"],
                    result["confidence"],
                    result.get("reason", ""),
                ),
            )
            con.commit()
        classified += 1
    return {"ok": True, "classified": classified, "skipped": skipped}


@app.get("/api/contacts")
async def api_contacts(request: Request, q: str = Query("", min_length=0)):
    sess = get_session_or_401(request)
    if len(q.strip()) < 2:
        return {"contacts": []}
    loop = asyncio.get_event_loop()
    directory_hits = await loop.run_in_executor(
        None, _search_directory_contacts, q, 25, [_mail_session_contact(sess)],
    )
    return {"contacts": directory_hits}


# ---------------------------------------------------------------------------
# Company directory (employee address book)
# ---------------------------------------------------------------------------

_employee_directory_status: Dict[str, str] = {"error": ""}


def _fetch_employees_raw_from_panel() -> List[Dict[str, Any]]:
    """Fetch employee identities from the portal, then use SOC as fallback."""
    portal_rows = _fetch_employees_raw_from_portal()
    if portal_rows is not None:
        return portal_rows
    if not CFG.INTERNAL_TOKEN:
        _employee_directory_status["error"] = "internal token is not configured"
        return []
    url = CFG.PROXY_PANEL_URL.rstrip("/") + "/api/directory/employees"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Internal-Token", CFG.INTERNAL_TOKEN)
    req.add_header("X-Internal-Service", "rupochta")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not payload.get("ok"):
            error = str(payload.get("error") or "directory request failed")
            _employee_directory_status["error"] = error
            log.warning("proxy panel employees: %s", error)
            return []
        _employee_directory_status["error"] = ""
        return (payload.get("data") or {}).get("employees", [])
    except Exception as e:
        _employee_directory_status["error"] = str(e)
        log.warning("_fetch_employees_raw_from_panel: %s", e)
        return []


def _fetch_employees_raw_from_portal() -> Optional[List[Dict[str, Any]]]:
    try:
        payload = _portal_directory_payload()
        employees = payload.get("employees") or []
        if not isinstance(employees, list):
            raise RuntimeError("portal directory returned an invalid employee list")
        rows: List[Dict[str, Any]] = []
        for employee in employees:
            if not isinstance(employee, dict):
                continue
            employee_id = employee.get("id")
            if not str(employee_id or "").isdigit():
                continue
            accounts = employee.get("accounts") or {}
            mail = accounts.get("mail") or {}
            telegram = accounts.get("telegram") or {}
            rows.append({
                "id": int(employee_id),
                "company": employee.get("company"),
                "first_name": employee.get("firstName"),
                "last_name": employee.get("lastName"),
                "full_name": employee.get("fullName"),
                "mail_login": mail.get("login"),
                "ad_login": accounts.get("ad"),
                "ad_domain": employee.get("directoryDomain"),
                "telegram_user_id": telegram.get("id"),
                "telegram_username": telegram.get("username"),
                "department": employee.get("department"),
                "title": employee.get("title"),
                "directory_groups": employee.get("directoryGroups") or [],
                "is_active": employee.get("active") is not False,
                "created_at": employee.get("createdAt"),
            })
        _employee_directory_status["error"] = ""
        return rows
    except Exception as e:
        log.warning("_fetch_employees_raw_from_portal: %s", e)
        return None


def _directory_mail_accounts_from_panel() -> List[str]:
    accounts = {
        str(row.get("mail_login") or "").strip().lower()
        for row in _fetch_employees_raw_from_panel()
        if str(row.get("mail_login") or "").strip()
    }
    return [f"{login}@{CFG.MAIL_DOMAIN}" for login in sorted(accounts)]


def _mail_control_request(path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
    """Call the SOC-side mail control-plane on proxy-panel.

    Used by backup webmail for real mailbox operations when the local mailserver
    is not available on this node.
    """
    if not CFG.INTERNAL_TOKEN:
        raise RuntimeError("RUPOCHTA_INTERNAL_TOKEN not configured")
    url = CFG.PROXY_PANEL_URL.rstrip("/") + path
    raw = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Internal-Token", CFG.INTERNAL_TOKEN)
    req.add_header("X-Internal-Service", "rupochta")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        message = str(e)
        try:
            error_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            error_body = {}
        if isinstance(error_body, dict):
            message = str(
                error_body.get("error")
                or error_body.get("detail")
                or message
            )
        raise RuntimeError(message)
    except Exception as e:
        raise RuntimeError(str(e))
    if not body.get("ok"):
        raise RuntimeError(body.get("error") or body.get("detail") or "mail control-plane error")
    return body.get("data") or {}


def _mail_control_error_is_uncertain(error: Any) -> bool:
    normalized = str(error or "").strip().lower()
    return (
        "runtime state uncertain" in normalized
        or "mail control agent unavailable:" in normalized
        or "timed out" in normalized
    )


_mail_control_health_cache_lock = threading.Lock()
_mail_control_health_cache = {
    "ts": 0.0,
    "data": {},
    "error": "",
}
_MAIL_CONTROL_LIST_TIMEOUT = 6


def _mail_control_health_cached(timeout: int = 3, max_age: float = 20.0) -> tuple[Dict[str, Any], str]:
    now = time.time()
    with _mail_control_health_cache_lock:
        cached_ts = float(_mail_control_health_cache.get("ts") or 0.0)
        cached_data = dict(_mail_control_health_cache.get("data") or {})
        cached_error = str(_mail_control_health_cache.get("error") or "")
        if (now - cached_ts) < max(1.0, float(max_age or 0.0)):
            return cached_data, cached_error
    data: Dict[str, Any] = {}
    error = ""
    try:
        data = _mail_control_request("/api/mail/health", {}, timeout=timeout)
    except Exception as exc:
        error = str(exc)
    with _mail_control_health_cache_lock:
        _mail_control_health_cache.update({
            "ts": time.time(),
            "data": dict(data or {}),
            "error": error,
        })
    return dict(data or {}), error


_CYR_TRANS_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _translit(s: str) -> str:
    if not s:
        return ""
    return "".join(_CYR_TRANS_MAP.get(c, c) for c in s.lower())


def _person_name_keys(first_name: str, last_name: str) -> List[str]:
    keys: List[str] = []
    a = re.sub(r"\s+", " ", (first_name or "").strip().lower())
    b = re.sub(r"\s+", " ", (last_name or "").strip().lower())
    if a and b:
        keys.append(f"{a} {b}")
        keys.append(f"{b} {a}")
    return keys


def _display_name_keys(name: str) -> List[str]:
    parts = [p for p in re.sub(r"\s+", " ", (name or "").strip().lower()).split(" ") if p]
    if len(parts) < 2:
        return []
    keys: List[str] = []
    keys.append(" ".join(parts[:2]))
    keys.append(" ".join((parts[1], parts[0])))
    return keys


def _unique_name_login_map() -> Dict[str, str]:
    """Return unique human-name -> LDAP login matches.

    Keys are normalized two-token names in both direct and reversed order so we
    can handle rows where first/last names were imported in the wrong columns.
    """
    result: Dict[str, str] = {}
    collisions: set[str] = set()
    try:
        rows = ldap_load_directory()
    except Exception:
        return {}
    for row in rows:
        login = str(row.get("login") or "").strip().lower()
        if not login:
            continue
        for key in _display_name_keys(str(row.get("name") or "")):
            if not key:
                continue
            existing = result.get(key)
            if existing and existing != login:
                collisions.add(key)
            else:
                result[key] = login
    for key in collisions:
        result.pop(key, None)
    return result


def _enrich_employee_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    mailbox_locals = _mailbox_local_parts()
    name_map = _unique_name_login_map()
    out: List[Dict[str, Any]] = []
    derived_ad = 0
    derived_mail = 0
    for raw in rows:
        row = dict(raw or {})
        ad_login = str(row.get("ad_login") or "").strip().lower()
        mail_login = str(row.get("mail_login") or "").strip().lower()

        if not ad_login:
            for key in _person_name_keys(str(row.get("first_name") or ""), str(row.get("last_name") or "")):
                login = name_map.get(key)
                if login:
                    ad_login = login
                    row["ad_login"] = login
                    row["ad_login_source"] = "ldap_name"
                    derived_ad += 1
                    break

        if not mail_login:
            if ad_login and ad_login in mailbox_locals:
                row["mail_login"] = ad_login
                row["mail_login_source"] = "mailbox_same_as_ad"
                derived_mail += 1
            else:
                full_name = " ".join(part for part in [row.get("first_name"), row.get("last_name")] if part)
                guesses = _mail_login_candidates_from_display_name(full_name)
                if len(guesses) == 1:
                    row["mail_login"] = guesses[0]
                    row["mail_login_source"] = "mailbox_name_guess"
                    derived_mail += 1
        out.append(row)
    if derived_ad or derived_mail:
        log.info("employee enrich derived_ad=%d derived_mail=%d rows=%d", derived_ad, derived_mail, len(rows))
    return out


def _read_employee_identity_cache() -> Dict[int, Dict[str, Any]]:
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT emp_id, derived_ad_login, derived_mail_login, ad_source, mail_source, updated_at "
            "FROM employee_identity_cache"
        ).fetchall()
    return {
        int(row["emp_id"]): dict(row)
        for row in rows
        if row["emp_id"] is not None
    }


def _merge_employee_identity_cache(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cache = _read_employee_identity_cache()
    if not cache:
        return [dict(row or {}) for row in rows]
    out: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw or {})
        emp_id = row.get("id")
        cached = cache.get(int(emp_id)) if emp_id is not None else None
        if cached:
            if not str(row.get("ad_login") or "").strip() and cached.get("derived_ad_login"):
                row["ad_login"] = cached["derived_ad_login"]
                row["ad_login_source"] = cached.get("ad_source") or "cached"
            if not str(row.get("mail_login") or "").strip() and cached.get("derived_mail_login"):
                row["mail_login"] = cached["derived_mail_login"]
                row["mail_login_source"] = cached.get("mail_source") or "cached"
        out.append(row)
    return out


def _fetch_employees_from_panel() -> List[Dict[str, Any]]:
    return _merge_employee_identity_cache(_fetch_employees_raw_from_panel())


def refresh_employee_identity_cache() -> Dict[str, Any]:
    rows = _fetch_employees_raw_from_panel()
    enriched = _enrich_employee_rows(rows)
    now = _utc_now_iso()
    inserts = 0
    updates = 0
    deletes = 0
    cached_emp_ids: set[int] = set()
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        existing = {
            int(row["emp_id"]): dict(row)
            for row in con.execute(
                "SELECT emp_id, derived_ad_login, derived_mail_login, ad_source, mail_source "
                "FROM employee_identity_cache"
            ).fetchall()
            if row["emp_id"] is not None
        }
        for raw, row in zip(rows, enriched):
            emp_id = row.get("id")
            if emp_id is None:
                continue
            emp_id = int(emp_id)
            cached_emp_ids.add(emp_id)
            raw_ad = str(raw.get("ad_login") or "").strip().lower()
            raw_mail = str(raw.get("mail_login") or "").strip().lower()
            derived_ad = str(row.get("ad_login") or "").strip().lower() if not raw_ad else ""
            derived_mail = str(row.get("mail_login") or "").strip().lower() if not raw_mail else ""
            ad_source = str(row.get("ad_login_source") or "") if derived_ad else ""
            mail_source = str(row.get("mail_login_source") or "") if derived_mail else ""
            prev = existing.get(emp_id)
            if not derived_ad and not derived_mail:
                if prev:
                    con.execute("DELETE FROM employee_identity_cache WHERE emp_id = ?", (emp_id,))
                    deletes += 1
                continue
            if prev and prev.get("derived_ad_login") == derived_ad and prev.get("derived_mail_login") == derived_mail and prev.get("ad_source") == ad_source and prev.get("mail_source") == mail_source:
                continue
            con.execute(
                """
                INSERT INTO employee_identity_cache (emp_id, derived_ad_login, derived_mail_login, ad_source, mail_source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(emp_id) DO UPDATE SET
                    derived_ad_login = excluded.derived_ad_login,
                    derived_mail_login = excluded.derived_mail_login,
                    ad_source = excluded.ad_source,
                    mail_source = excluded.mail_source,
                    updated_at = excluded.updated_at
                """,
                (emp_id, derived_ad or None, derived_mail or None, ad_source or None, mail_source or None, now),
            )
            if prev:
                updates += 1
            else:
                inserts += 1
        stale_ids = [emp_id for emp_id in existing.keys() if emp_id not in cached_emp_ids]
        for emp_id in stale_ids:
            con.execute("DELETE FROM employee_identity_cache WHERE emp_id = ?", (emp_id,))
            deletes += 1
        con.commit()
    _invalidate_employees_cache()
    report = {
        "employees": len(rows),
        "cached_rows": len(_read_employee_identity_cache()),
        "inserted": inserts,
        "updated": updates,
        "deleted": deletes,
    }
    log.info("employee identity cache refresh: %s", report)
    return report


def _employees_by_slug() -> Dict[str, Dict[str, Any]]:
    """Map slugified display names to proxy_employees rows so we can join AD
    users with their mail login. Keys:
      - ad_login (best, explicit link)
      - transliterated 'firstname.lastname' and 'lastname.firstname'
    Data is fetched over HTTP from proxy-panel (cached 5 min).

    When multiple employees share a key, prefer the one with more identifiers
    so AD matching surfaces richer data rather than a stripped-down duplicate.
    """
    result: Dict[str, Dict[str, Any]] = {}
    def _slug(s: str) -> str:
        return re.sub(r"[^a-z0-9.]", "", _translit(s or ""))

    def _score(emp: dict) -> int:
        s = 0
        if emp.get("mail_login"):      s += 2
        if emp.get("ad_login"):        s += 3
        if emp.get("telegram_user_id"):s += 1
        return s

    def _put(key: str, entry: dict) -> None:
        """Store entry for key, but only overwrite a weaker existing one."""
        existing = result.get(key)
        if not existing or _score(entry["_src"]) > _score(existing["_src"]):
            result[key] = entry

    # Sort by score desc so better candidates are inserted first; setdefault-like
    # logic via _put still chooses the best on collision.
    emps = sorted(_fetch_employees_cached(), key=_score, reverse=True)
    for emp in emps:
        ad = (emp.get("ad_login") or "").lower()
        fn = emp.get("first_name") or ""
        ln = emp.get("last_name") or ""
        entry = {
            "emp_id": emp.get("id"),
            "mail_login": emp.get("mail_login"),
            "tg_id": emp.get("telegram_user_id"),
            "tg_username": emp.get("telegram_username"),
            "company": emp.get("company"),
            "first_name": fn,
            "last_name": ln,
            "_src": emp,
        }
        if ad:
            _put(ad, entry)
        fn_s = _slug(fn)
        ln_s = _slug(ln)
        if fn_s and ln_s:
            _put(f"{ln_s}.{fn_s}", entry)
            _put(f"{fn_s}.{ln_s}", entry)
            _put(f"{fn_s[0]}.{ln_s}", entry)
            _put(f"{ln_s}.{fn_s[0]}", entry)
    # Strip the debugging key before returning
    for v in result.values():
        v.pop("_src", None)
    return result


# Cache employees list for 5 min so we don't hammer proxy panel
_employees_cache: Dict[str, Any] = {"ts": 0.0, "rows": []}
_employees_lock = threading.Lock()
EMPLOYEES_TTL = 300
IDENTITY_CACHE_REFRESH_INTERVAL = 900
_identity_cache_refresh_ts = 0.0


def _invalidate_employees_cache() -> None:
    with _employees_lock:
        _employees_cache["ts"] = 0.0
        _employees_cache["rows"] = []


def _maybe_refresh_identity_cache() -> None:
    global _identity_cache_refresh_ts
    now = time.time()
    if _identity_cache_refresh_ts and (now - _identity_cache_refresh_ts) < IDENTITY_CACHE_REFRESH_INTERVAL:
        return
    refresh_employee_identity_cache()
    _identity_cache_refresh_ts = now


def _fetch_employees_cached(force: bool = False) -> List[Dict[str, Any]]:
    with _employees_lock:
        now = time.time()
        if (not force and _employees_cache["rows"]
                and (now - _employees_cache["ts"]) < EMPLOYEES_TTL):
            return _employees_cache["rows"]
        rows = _fetch_employees_from_panel()
        if rows:
            _employees_cache["rows"] = rows
            _employees_cache["ts"] = now
        return rows


def _panel_find_employee_by_id(emp_id: int, force: bool = False) -> Optional[Dict[str, Any]]:
    for emp in _fetch_employees_cached(force=force):
        if emp.get("id") == emp_id:
            return emp
    return None


def _panel_find_employee_by_exact_login(
    login: str,
    force: bool = False,
    ad_domain: str = "",
) -> Optional[Dict[str, Any]]:
    login = (login or "").strip().lower()
    ad_domain = (ad_domain or "").strip().lower()
    if not login:
        return None
    ad_hits: List[Dict[str, Any]] = []
    mail_hits: List[Dict[str, Any]] = []
    for emp in _fetch_employees_cached(force=force):
        ad = (emp.get("ad_login") or "").lower()
        domain = (
            str(emp.get("ad_domain") or "").strip().lower()
            or _directory_domain_for_company(emp.get("company"))
        )
        ml = (emp.get("mail_login") or "").lower()
        if ad == login and (not ad_domain or domain == ad_domain):
            ad_hits.append(emp)
        if ml == login:
            mail_hits.append(emp)
    if len(ad_hits) == 1:
        return ad_hits[0]
    if len(ad_hits) > 1:
        return None
    return mail_hits[0] if len(mail_hits) == 1 else None


def _panel_find_employee_by_exact_mail_login(
    login: str,
    force: bool = False,
) -> Optional[Dict[str, Any]]:
    login = str(login or "").strip().lower()
    if not login:
        return None
    hits = [
        emp
        for emp in _fetch_employees_cached(force=force)
        if str(emp.get("mail_login") or "").strip().lower() == login
    ]
    return hits[0] if len(hits) == 1 else None


class EmployeeDirectoryUnavailable(RuntimeError):
    pass


def _panel_manage_mail_login_owner(
    mail_login: str,
    owner_key: str,
    *,
    action: str,
) -> None:
    if not CFG.INTERNAL_TOKEN:
        raise EmployeeDirectoryUnavailable("internal token is not configured")
    payload = {
        "action": action,
        "mail_login": str(mail_login or "").strip().lower(),
        "owner_type": "managed",
        "owner_key": str(owner_key or "").strip().lower(),
    }
    req = urllib.request.Request(
        CFG.PROXY_PANEL_URL.rstrip("/") + "/api/directory/mail-login-owner",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Internal-Token", CFG.INTERNAL_TOKEN)
    req.add_header("X-Internal-Service", "rupochta")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            response = json.loads(resp.read().decode("utf-8"))
        if not response.get("ok"):
            raise EmployeeDirectoryUnavailable(
                str(response.get("error") or "invalid owner registry response")
            )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = body
        try:
            parsed = json.loads(body)
            message = str(
                parsed.get("error")
                or parsed.get("detail")
                or body
            )
        except Exception:
            pass
        if exc.code == 409:
            raise ValueError(message) from exc
        raise EmployeeDirectoryUnavailable(
            f"mail login owner registry HTTP {exc.code}: {message}"
        ) from exc
    except (ValueError, EmployeeDirectoryUnavailable):
        raise
    except Exception as exc:
        raise EmployeeDirectoryUnavailable(str(exc)) from exc


def _panel_update_employee_identity(
    emp_id: int,
    *,
    ad_login: Any = None,
    ad_domain: Any = None,
    mail_login: Any = None,
    clear_mail_login: bool = False,
) -> Optional[Dict[str, Any]]:
    """Persist identity fields into proxy_employees via proxy-panel internal API."""
    if not emp_id:
        return None
    if not CFG.INTERNAL_TOKEN:
        raise EmployeeDirectoryUnavailable("internal token is not configured")
    payload: Dict[str, Any] = {"id": int(emp_id)}
    if ad_login is not None:
        payload["ad_login"] = str(ad_login).strip().lower() if ad_login else None
    if ad_domain is not None:
        payload["ad_domain"] = str(ad_domain).strip().lower() if ad_domain else None
    if clear_mail_login:
        payload["mail_login"] = None
    elif mail_login is not None:
        payload["mail_login"] = str(mail_login).strip().lower() if mail_login else None
    req = urllib.request.Request(
        CFG.PROXY_PANEL_URL.rstrip("/") + "/api/directory/employee-update",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Internal-Token", CFG.INTERNAL_TOKEN)
    req.add_header("X-Internal-Service", "rupochta")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if not payload.get("ok"):
            error = str(payload.get("error") or "invalid employee-update response")
            log.warning("employee-update failed emp=%s: %s", emp_id, error)
            raise EmployeeDirectoryUnavailable(error)
        row = (payload.get("data") or {}).get("employee")
        if row:
            _invalidate_employees_cache()
        return row
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(e)
        msg = body
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and parsed.get("error"):
                msg = str(parsed["error"])
        except Exception:
            pass
        log.warning("employee-update HTTP %s emp=%s: %s", e.code, emp_id, body)
        if e.code == 409:
            raise ValueError(msg)
        raise EmployeeDirectoryUnavailable(
            f"employee directory HTTP {e.code}: {msg}"
        ) from e
    except Exception as e:
        if isinstance(e, (ValueError, EmployeeDirectoryUnavailable)):
            raise
        log.warning("employee-update emp=%s: %s", emp_id, e)
        raise EmployeeDirectoryUnavailable(str(e)) from e


def _find_employee_match(login_local: str, display_name: str = "") -> Optional[Dict[str, Any]]:
    """Resolve a webmail login to proxy_employees.

    Prefers exact `ad_login`, then exact `mail_login`, then name-based slug match.
    """
    exact = _panel_find_employee_by_exact_login(login_local)
    if exact:
        return exact

    slug_map = _employees_by_slug()
    if not slug_map:
        return None

    def _row_from_entry(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not entry:
            return None
        emp_id = entry.get("emp_id") or entry.get("id")
        if not emp_id:
            return None
        return _panel_find_employee_by_id(int(emp_id))

    hit = _row_from_entry(slug_map.get((login_local or "").strip().lower()))
    if hit:
        return hit

    parts = re.sub(r"[^a-zа-яё ]", "", (display_name or "").lower()).split()
    if len(parts) >= 2:
        p0 = _translit(parts[0])
        p1 = _translit(parts[1])
        for fn, ln in ((p0, p1), (p1, p0)):
            if not (fn and ln):
                continue
            for key in (f"{ln}.{fn}", f"{fn}.{ln}", f"{fn[:1]}.{ln}", f"{ln}.{fn[:1]}"):
                hit = _row_from_entry(slug_map.get(key))
                if hit:
                    return hit
    return None


def _find_employee_by_display_name(display_name: str) -> Optional[Dict[str, Any]]:
    """Resolve an employee by human name only.

    This is safer than feeding a login-shaped string back into the slug map:
    an AD login like `ivanov.i` can collide with another employee's
    `surname.initial` slug even when the LDAP displayName points to a mailbox
    owner with a populated mail_login.
    """
    slug_map = _employees_by_slug()
    if not slug_map:
        return None

    def _row_from_entry(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not entry:
            return None
        emp_id = entry.get("emp_id") or entry.get("id")
        if not emp_id:
            return None
        return _panel_find_employee_by_id(int(emp_id))

    def _is_useful(entry: Optional[Dict[str, Any]]) -> bool:
        return bool(entry and entry.get("mail_login"))

    parts = re.sub(r"[^a-zа-яё ]", "", (display_name or "").lower()).split()
    if len(parts) < 2:
        return None

    candidates: List[Dict[str, Any]] = []
    p0 = _translit(parts[0])
    p1 = _translit(parts[1])
    for fn, ln in ((p0, p1), (p1, p0)):
        if not (fn and ln):
            continue
        for key in (f"{ln}.{fn}", f"{fn}.{ln}", f"{fn[:1]}.{ln}", f"{ln}.{fn[:1]}"):
            hit = slug_map.get(key)
            if hit and hit not in candidates:
                candidates.append(hit)

    chosen = next((entry for entry in candidates if _is_useful(entry)),
                  candidates[0] if candidates else None)
    return _row_from_entry(chosen)


def _enrich_with_employees(contacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach employee and mail identifiers to matched directory contacts."""
    slug_map = _employees_by_slug()
    if not slug_map:
        return contacts

    def _is_useful(m: Optional[Dict[str, Any]]) -> bool:
        return bool(m and m.get("mail_login"))

    for c in contacts:
        login = (c.get("login") or "").lower()
        name = (c.get("name") or "").lower()
        candidates: List[Dict[str, Any]] = []
        if login:
            hit = slug_map.get(login)
            if hit:
                candidates.append(hit)
        # Always also try name-based variants so we can pick the best match
        parts = re.sub(r"[^a-zа-яё ]", "", name).split()
        if len(parts) >= 2:
            p0 = _translit(parts[0])
            p1 = _translit(parts[1])
            for fn, ln in ((p0, p1), (p1, p0)):
                if not (fn and ln):
                    continue
                for key in (f"{ln}.{fn}", f"{fn}.{ln}",
                            f"{fn[:1]}.{ln}", f"{ln}.{fn[:1]}"):
                    hit = slug_map.get(key)
                    if hit and hit not in candidates:
                        candidates.append(hit)
        # Pick the first useful candidate; fall back to first if none are useful
        match = next((m for m in candidates if _is_useful(m)),
                     candidates[0] if candidates else None)
        if match:
            if match.get("emp_id"):
                c["emp_id"] = match["emp_id"]
            if match.get("mail_login"):
                c["mail_login"] = match["mail_login"]
    return contacts


def _employees_without_ad(ad_logins_seen: set) -> List[Dict[str, Any]]:
    """Return proxy_employees rows that aren't already represented in the AD
    directory results. Mail-only employees remain visible as lightweight records."""
    result: List[Dict[str, Any]] = []
    for emp in _fetch_employees_cached():
        ad = (emp.get("ad_login") or "").lower()
        ml = emp.get("mail_login") or ""
        if ad and ad in ad_logins_seen:
            continue
        fn = emp.get("first_name") or ""
        ln = emp.get("last_name") or ""
        name = f"{fn} {ln}".strip() or (ml or "(?)")
        email = f"{ml}@{CFG.MAIL_DOMAIN}" if ml else ""
        result.append({
            "name": name,
            "email": email,
            "login": emp.get("ad_login") or "",
            "mail_login": ml,
            "emp_id": emp.get("id"),
            "department": emp.get("company") or "",
            "phone": "",
            "mobile": "",
            "title": "",
            "company": emp.get("company") or "",
            "disabled": False,
            "source": "employees",
        })
    return result


def reconcile_employee_identities(*, apply: bool = False) -> Dict[str, Any]:
    """Safely backfill missing ad_login/mail_login into proxy_employees.

    Only applies facts that can be verified without ambiguous name heuristics:
    - if an employee has mail_login but no ad_login, and the same login exists
      in LDAP, set ad_login = mail_login
    - if an employee has ad_login but no mail_login, and a mailbox with the
      same local-part exists on the mailserver, set mail_login = ad_login
    """
    rows = _fetch_employees_cached(force=True)
    mailbox_locals = _mailbox_local_parts()
    report: Dict[str, Any] = {
        "employees": len(rows),
        "mailboxes": len(mailbox_locals),
        "planned": [],
        "applied": [],
        "errors": [],
    }

    for row in rows:
        emp_id = row.get("id")
        if not emp_id:
            continue
        ad_login = str(row.get("ad_login") or "").strip().lower()
        mail_login = str(row.get("mail_login") or "").strip().lower()

        if mail_login and not ad_login:
            if not ldap_get_user_by_login(mail_login, with_photo=False):
                continue
            change = {
                "id": int(emp_id),
                "field": "ad_login",
                "value": mail_login,
                "reason": "mail_login_exists_in_ldap",
            }
        elif ad_login and not mail_login:
            if ad_login not in mailbox_locals:
                continue
            change = {
                "id": int(emp_id),
                "field": "mail_login",
                "value": ad_login,
                "reason": "ad_login_mailbox_exists",
            }
        else:
            continue

        report["planned"].append(change)
        if not apply:
            continue
        try:
            kwargs = {change["field"]: change["value"]}
            updated = _panel_update_employee_identity(int(emp_id), **kwargs)
            if updated:
                report["applied"].append(change)
            else:
                report["errors"].append({**change, "error": "employee_update_failed"})
        except Exception as exc:
            report["errors"].append({**change, "error": str(exc)})

    if apply and report["applied"]:
        _invalidate_employees_cache()
    report["planned_count"] = len(report["planned"])
    report["applied_count"] = len(report["applied"])
    report["error_count"] = len(report["errors"])
    return report


@app.get("/api/directory")
async def api_directory(request: Request, refresh: int = Query(0)):
    """RuPochta-native contacts; external corporate directories stay isolated."""
    sess = get_session_or_401(request)
    rows = [_mail_session_contact(sess)]
    return {
        "total": len(rows),
        "contacts": rows,
        "departments": [],
        "sources": {"rupochta": len(rows)},
    }


@app.get("/api/directory/{login}")
async def api_directory_profile(login: str, request: Request):
    """Return the current RuPochta mailbox contact only."""
    row = _mail_session_contact(get_session_or_401(request))
    aliases = {
        str(row.get("login") or "").lower(),
        str(row.get("mail_login") or "").lower(),
        str(row.get("email") or "").lower(),
    }
    if login.strip().lower() not in aliases:
        raise HTTPException(404, "Contact not found")
    return row


def _resolve_photo(login: str, ldap_photo: Optional[bytes],
                   portal_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Resolve a contact photo to binary bytes + media type.

    Order: LDAP thumbnailPhoto first, then portal avatarDataUrl (covers
    cross-domain employees from alpha/beta/gamma whose photo only exists in
    the portal bridge cache). Returns {"raw": bytes, "media": str} or None.

    Pure data function — no I/O, no framework deps — so it can be unit-tested
    in isolation without booting the server.
    """
    if ldap_photo:
        return {"raw": ldap_photo, "media": "image/jpeg"}
    login_lower = login.lower()
    for row in portal_rows:
        if (row.get("login") or "").lower() != login_lower:
            continue
        data_url = (row.get("avatar_data_url") or "").strip()
        if data_url.startswith("data:image/"):
            try:
                import base64
                header, b64payload = data_url.split(",", 1)
                media = header.split(":", 1)[1].split(";", 1)[0]
                raw = base64.b64decode(b64payload)
                if raw:
                    return {"raw": raw, "media": media}
            except Exception:
                pass
        break
    return None


@app.get("/api/directory/{login}/photo")
async def api_directory_photo(login: str, request: Request):
    """RuPochta contacts do not expose external directory photos."""
    row = _mail_session_contact(get_session_or_401(request))
    aliases = {
        str(row.get("login") or "").lower(),
        str(row.get("mail_login") or "").lower(),
        str(row.get("email") or "").lower(),
    }
    if login.strip().lower() not in aliases:
        raise HTTPException(404, "Contact not found")
    raise HTTPException(404, "No photo")


@app.get("/api/alias")
async def api_alias_get(request: Request):
    sess = get_session_or_401(request)
    row = db_get_alias(sess["ad_login"])
    if row:
        return {
            "mail_login": row["mail_login"],
            "from_address": f"{row['mail_login']}@{CFG.MAIL_DOMAIN}",
            "updated_at": row["updated_at"],
        }
    return {
        "mail_login": sess["mail_login"],
        "from_address": sess["from_address"],
        "updated_at": None,
    }


@app.put("/api/alias")
async def api_alias_set(body: AliasBody, request: Request):
    get_session_or_401(request)
    return JSONResponse(
        {
            "error": (
                "Адрес корпоративной почты управляется личным кабинетом "
                "и не изменяется в RuPochta Mail"
            )
        },
        status_code=403,
    )


@app.get("/api/settings/profile")
async def api_settings_profile(request: Request):
    sess = get_session_or_401(request)
    await _refresh_portal_profile_card(
        sess,
        token=str(request.cookies.get(CFG.SESSION_COOKIE) or ""),
    )
    owner_key = _settings_owner_key(sess)
    signature = await asyncio.get_event_loop().run_in_executor(
        None, db_get_user_signature, owner_key
    )
    templates = await asyncio.get_event_loop().run_in_executor(
        None, db_list_user_templates, owner_key
    )
    return {
        "ok": True,
        "ad_login": sess["ad_login"],
        "email": sess["email"],
        "display_name": sess["display_name"],
        "mail_login": sess["mail_login"],
        "from_address": sess["from_address"],
        "signature": signature,
        "templates": templates,
        "profile_card": _mail_profile_snapshot(sess.get("profile_card")),
    }


def _push_subscription_count(imap_user: str) -> int:
    with _db_connection() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM push_subscriptions WHERE imap_user = ?",
            (imap_user,),
        ).fetchone()
    return int(row[0] if row else 0)


@app.get("/api/settings/notifications")
async def api_settings_notifications_get(request: Request):
    sess = get_session_or_401(request)
    push_count = _push_subscription_count(sess["imap_user"])
    telegram_state: Dict[str, Any] = {
        "telegram_linked": False,
        "telegram_username": "",
        "telegram_enabled": False,
        "stored_password": False,
    }
    try:
        telegram_state.update(
            await asyncio.get_event_loop().run_in_executor(
                None,
                _mail_control_request,
                "/api/mail/notification-settings",
                {
                    "operation": "get",
                    "mail_login": sess["mail_login"],
                },
            )
        )
    except Exception as exc:
        telegram_state["telegram_error"] = str(exc)
    return {
        "ok": True,
        "push_enabled": push_count > 0,
        "push_subscriptions": push_count,
        **telegram_state,
    }


@app.put("/api/settings/notifications")
async def api_settings_notifications_put(
    body: NotificationSettingsBody,
    request: Request,
):
    sess = get_session_or_401(request)
    payload: Dict[str, Any] = {
        "operation": "set",
        "mail_login": sess["mail_login"],
        "mail_password": sess["imap_pass"],
    }
    if body.telegram_enabled is not None:
        payload["telegram_enabled"] = body.telegram_enabled
    try:
        telegram_state = await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/notification-settings",
            payload,
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    push_count = _push_subscription_count(sess["imap_user"])
    return {
        "ok": True,
        "push_enabled": push_count > 0,
        "push_subscriptions": push_count,
        **telegram_state,
    }


@app.get("/api/settings/forwarding")
async def api_settings_forwarding_get(request: Request):
    sess = get_session_or_401(request)
    row = await asyncio.get_event_loop().run_in_executor(
        None,
        db_get_forwarding,
        sess["imap_user"],
    )
    return _forwarding_public_payload(row)


@app.put("/api/settings/forwarding")
async def api_settings_forwarding_put(body: ForwardingBody, request: Request):
    sess = get_session_or_401(request)
    address = _normalize_forwarding_address(
        body.address,
        body.enabled,
        sess["from_address"],
    )
    loop = asyncio.get_event_loop()
    row = await loop.run_in_executor(
        None,
        _set_and_sync_forwarding,
        sess,
        body.enabled,
        address,
        body.keep_copy,
    )
    return _forwarding_public_payload(row)


@app.get("/api/settings/autoreply")
async def api_settings_autoreply_get(request: Request):
    sess = get_session_or_401(request)
    row = await asyncio.get_event_loop().run_in_executor(
        None,
        db_get_autoreply,
        sess["imap_user"],
    )
    return _autoreply_public_payload(row)


@app.put("/api/settings/autoreply")
async def api_settings_autoreply_put(body: AutoReplyBody, request: Request):
    sess = get_session_or_401(request)
    settings = _normalize_autoreply_settings(
        body.enabled,
        body.subject,
        body.body,
        body.start_date,
        body.end_date,
        body.repeat_days,
    )
    row = await asyncio.get_event_loop().run_in_executor(
        None,
        _set_and_sync_autoreply,
        sess,
        settings,
    )
    return _autoreply_public_payload(row)


@app.get("/api/settings/signature")
async def api_settings_signature_get(request: Request):
    sess = get_session_or_401(request)
    signature = await asyncio.get_event_loop().run_in_executor(
        None, db_get_user_signature, _settings_owner_key(sess)
    )
    return {"ok": True, "signature": signature}


@app.put("/api/settings/signature")
async def api_settings_signature_put(body: SignatureBody, request: Request):
    sess = get_session_or_401(request)
    row = await asyncio.get_event_loop().run_in_executor(
        None,
        db_set_user_signature,
        _settings_owner_key(sess),
        body.signature or "",
    )
    return {"ok": True, **row}


@app.get("/api/settings/templates")
async def api_settings_templates_get(request: Request):
    sess = get_session_or_401(request)
    templates = await asyncio.get_event_loop().run_in_executor(
        None, db_list_user_templates, _settings_owner_key(sess)
    )
    return {"ok": True, "templates": templates, "total": len(templates)}


@app.post("/api/settings/templates")
async def api_settings_templates_post(body: TemplateBody, request: Request):
    sess = get_session_or_401(request)
    try:
        row = await asyncio.get_event_loop().run_in_executor(
            None,
            db_upsert_user_template,
            _settings_owner_key(sess),
            body.name.strip(),
            body.subject or "",
            body.body or "",
            body.id,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"ok": True, "template": row}


@app.delete("/api/settings/templates/{template_id}")
async def api_settings_templates_delete(template_id: int, request: Request):
    sess = get_session_or_401(request)
    deleted = await asyncio.get_event_loop().run_in_executor(
        None,
        db_delete_user_template,
        _settings_owner_key(sess),
        template_id,
    )
    if not deleted:
        return JSONResponse({"error": "Шаблон не найден"}, status_code=404)
    return {"ok": True}


# Static files / SPA
# ---------------------------------------------------------------------------
# Admin API (bot integration)
# ---------------------------------------------------------------------------

class AdminCreateBody(BaseModel):
    mail_login: str
    emp_id: int
    password: str = ""  # if empty — generate random


class AdminDeleteBody(BaseModel):
    mail_login: str


class AdminTicketIntakeBody(BaseModel):
    email: str
    password: str


class AdminSsoBindingBody(BaseModel):
    subject: str
    email: str = ""
    password: str = ""
    # BYOM external mailbox (ADR-0071): when set, the binding stores a per-row
    # IMAP endpoint instead of falling back to the global SpaceWeb config.
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    provider: Optional[str] = None
    # Submission endpoint of the same mailbox: an external address has to be
    # sent through its own provider, not through the local relay.
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    # For known providers the host/port are taken from MAIL_PROVIDER_PRESETS.
    # For "custom" the caller must supply imap_host and imap_port; the
    # submission endpoint is optional and falls back to the deployment's SMTP.


class AdminSsoBindingsBody(BaseModel):
    subjects: List[str] = Field(default_factory=list)
    email: str = ""
    password: str = ""
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    provider: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None


class AdminSsoBindingStatusBody(BaseModel):
    subjects: List[str] = Field(default_factory=list)


def _run_setup_email(action: str, *args: str) -> tuple[bool, str]:
    """Run docker exec mailserver setup email <action> <args> via subprocess.

    Used for read-only ops (list) and as a fallback. Slow for mutations because
    docker-mailserver's `setup` script triggers postfix/dovecot reload synchronously.
    Prefer the direct-write helpers below for add/del/password ops.
    """
    import subprocess
    cmd = ["docker", "exec", CFG.MAILSERVER_CONTAINER, "setup", "email", action] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, (result.stderr or result.stdout).strip()
    except Exception as e:
        return False, str(e)


def _parse_setup_email_accounts(output: str) -> List[str]:
    accounts: List[str] = []
    seen = set()
    for line in str(output or "").splitlines():
        match = re.search(
            r"([a-z0-9][a-z0-9._+\-]*@[a-z0-9][a-z0-9.\-]*\.[a-z]{2,})",
            line.lower(),
        )
        if not match:
            continue
        email_addr = match.group(1)
        if email_addr not in seen:
            seen.add(email_addr)
            accounts.append(email_addr)
    return accounts


def _read_mail_accounts_file() -> Optional[List[str]]:
    raw_path = str(getattr(CFG, "MAILSERVER_ACCOUNTS_FILE", "") or "").strip()
    if not raw_path:
        return None
    path = Path(raw_path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    accounts: List[str] = []
    seen = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        email_addr, separator, password_hash = line.partition("|")
        email_addr = email_addr.strip().lower()
        if (
            not separator
            or not password_hash.strip()
            or not re.fullmatch(
                r"[a-z0-9][a-z0-9._+\-]*@[a-z0-9][a-z0-9.\-]*\.[a-z]{2,}",
                email_addr,
            )
        ):
            return None
        if email_addr not in seen:
            seen.add(email_addr)
            accounts.append(email_addr)
    return accounts


def _collect_mail_accounts_result() -> tuple[bool, List[str], str]:
    direct_accounts = _read_mail_accounts_file()
    if direct_accounts is not None:
        return True, direct_accounts, "host_config"
    ok, out = _run_setup_email("list")
    return ok, _parse_setup_email_accounts(out) if ok else [], out


def _generate_password(length: int = 16) -> str:
    import secrets, string
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _sanitize_mail_login(value: str) -> str:
    return re.sub(r"[^a-z0-9.\-]", "", str(value or "").strip().lower())


def _mail_login_to_email(mail_login: str) -> str:
    clean = _sanitize_mail_login(mail_login)
    return f"{clean}@{CFG.MAIL_DOMAIN}" if clean else ""


def _employee_display_name(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return ""
    for key in ("display_name", "name"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    first = str(row.get("first_name") or "").strip()
    last = str(row.get("last_name") or "").strip()
    return " ".join(part for part in (first, last) if part).strip()


def _build_dclogin_url(email_addr: str, password: str) -> str:
    qp = (
        f"v=1"
        f"&p={urllib.parse.quote(password, safe='')}"
        f"&i={urllib.parse.quote(CFG.MAIL_PUBLIC_HOST, safe='')}&ip={CFG.IMAP_PORT}&is=ssl"
        f"&sp={urllib.parse.quote(CFG.MAIL_PUBLIC_HOST, safe='')}&spo={CFG.SMTP_PORT}&ss=starttls"
    )
    return f"dclogin:{email_addr}?{qp}"


def _build_thunderbird_mobile_qr_payload(
    email_addr: str,
    password: str,
    display_name: str = "",
) -> str:
    identity_name = (display_name or "").strip() or email_addr
    payload = [
        1,
        [1, 1],
        [0, CFG.MAIL_PUBLIC_HOST, CFG.IMAP_PORT, 3, 1, email_addr, email_addr, password],
        [
            [
                [0, CFG.MAIL_PUBLIC_HOST, CFG.SMTP_PORT, 2, 1, email_addr, password],
                [email_addr, identity_name],
            ]
        ],
    ]
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _build_credential_pack(email_addr: str, password: str, display_name: str = "") -> Dict[str, Any]:
    mail_login = email_addr.split("@", 1)[0].lower()
    thunderbird_payload = _build_thunderbird_mobile_qr_payload(email_addr, password, display_name)
    delta_url = _build_dclogin_url(email_addr, password)
    summary_lines = [
        f"Email: {email_addr}",
        f"Password: {password}",
        f"Webmail: {CFG.MAIL_WEBMAIL_URL}",
        f"IMAP: {CFG.MAIL_PUBLIC_HOST}:{CFG.IMAP_PORT} SSL/TLS",
        f"SMTP: {CFG.MAIL_PUBLIC_HOST}:{CFG.SMTP_PORT} STARTTLS",
    ]
    return {
        "email": email_addr,
        "mail_login": mail_login,
        "display_name": display_name or email_addr,
        "password": password,
        "webmail_url": CFG.MAIL_WEBMAIL_URL,
        "imap": {
            "host": CFG.MAIL_PUBLIC_HOST,
            "port": CFG.IMAP_PORT,
            "security": "SSL/TLS",
            "username": email_addr,
        },
        "smtp": {
            "host": CFG.MAIL_PUBLIC_HOST,
            "port": CFG.SMTP_PORT,
            "security": "STARTTLS",
            "username": email_addr,
        },
        "delta_chat": {
            "setup_url": delta_url,
            "qr_text": delta_url,
        },
        "thunderbird_mobile": {
            "payload": thunderbird_payload,
            "qr_text": thunderbird_payload,
        },
        "summary_text": "\n".join(summary_lines),
    }


def _collect_mail_accounts() -> List[str]:
    _ok, accounts, _status = _collect_mail_accounts_result()
    return accounts


def _collect_queue_index(entries: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, int]]:
    queue_entries = entries if entries is not None else _postfix_queue_list()
    index: Dict[str, Dict[str, int]] = {}
    for entry in queue_entries:
        touched = set()
        sender = str(entry.get("sender") or "").strip().lower()
        if sender and "@" in sender:
            touched.add(sender)
        for rcpt in entry.get("recipients") or []:
            value = str(rcpt or "").strip().lower()
            if value and "@" in value:
                touched.add(value)
        for email_addr in touched:
            bucket = index.setdefault(email_addr, {"total": 0, "active": 0, "deferred": 0, "hold": 0})
            bucket["total"] += 1
            if entry.get("hold"):
                bucket["hold"] += 1
            elif entry.get("active"):
                bucket["active"] += 1
            else:
                bucket["deferred"] += 1
    return index


def _mailbox_local_identities(email_addr: str) -> set[str]:
    target = str(email_addr or "").strip().lower()
    if not target or "@" not in target:
        return set()
    local = target.split("@", 1)[0]
    return {target, local, f"{local}@{CFG.MAIL_DOMAIN.lower()}"}


def _parse_queue_event_ts(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return _utc_now_iso()
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S").replace(
            year=datetime.now(timezone.utc).year,
            tzinfo=timezone.utc,
        )
        return dt.isoformat()
    except Exception:
        return _utc_now_iso()


def _docker_logs_capture(container: str, hours: int) -> tuple[bool, str]:
    import subprocess
    since_hours = max(1, min(int(hours or 72), 24 * 14))
    try:
        r = subprocess.run(
            ["docker", "logs", "--since", f"{since_hours}h", container],
            capture_output=True,
            text=True,
            timeout=min(60, 8 + since_hours // 2),
        )
    except Exception as e:
        return False, str(e)
    output = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    if r.returncode != 0 and not output.strip():
        return False, "docker logs failed"
    return True, output


def _log_line_matches_mailbox(line: str, email_addr: str) -> bool:
    lowered = str(line or "").lower()
    identities = _mailbox_local_identities(email_addr)
    if not identities:
        return False
    full_email = next((value for value in identities if "@" in value), "")
    local = full_email.split("@", 1)[0] if full_email else ""
    if full_email and full_email in lowered:
        return True
    if not local:
        return False
    patterns = [
        rf"user=<{re.escape(local)}>",
        rf"user=<{re.escape(full_email)}>",
        rf"passwd-file\({re.escape(local)},",
        rf"sasl_username={re.escape(local)}(?:@{re.escape(CFG.MAIL_DOMAIN.lower())})?",
        rf"rip=.* lip=.* mpid=.* session=<.*>.* user=<{re.escape(local)}>",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def _parse_mailserver_log_event(line: str, email_addr: str) -> Optional[Dict[str, Any]]:
    raw = str(line or "").strip()
    if not raw or not _log_line_matches_mailbox(raw, email_addr):
        return None
    lowered = raw.lower()
    ts = _utc_now_iso()
    remainder = raw
    if " " in raw:
        first, rest = raw.split(" ", 1)
        try:
            ts = datetime.fromisoformat(first.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            remainder = rest.strip()
        except Exception:
            remainder = raw

    source = "system"
    if "dovecot" in lowered:
        source = "dovecot"
    elif "rspamd" in lowered:
        source = "rspamd"
    elif "postfix" in lowered:
        source = "postfix"

    category = "delivery"
    if any(token in lowered for token in ("auth failed", "imap-login", "pop3-login", "passwd-file(", "sasl_username=", "auth:")):
        category = "auth"
    if "account.suspend" in lowered or "account.restore" in lowered:
        category = "lifecycle"

    severity = "info"
    if any(token in lowered for token in ("auth failed", "authentication failure", "warning", "reject", "status=bounced", "fatal", "error", "timed out")):
        severity = "error"
    elif any(token in lowered for token in ("status=deferred", "disconnect", "lost connection", "temporary failure")):
        severity = "warn"

    summary = remainder
    if source == "dovecot" and "auth failed" in lowered:
        summary = "Authentication failed"
    elif source == "dovecot" and "imap-login" in lowered:
        summary = "IMAP login activity"
    elif "status=sent" in lowered:
        summary = "Delivery sent"
    elif "status=deferred" in lowered:
        summary = "Delivery deferred"
    elif "status=bounced" in lowered:
        summary = "Delivery bounced"
    elif "reject" in lowered:
        summary = "Delivery rejected"

    return {
        "ts": ts,
        "source": source,
        "category": category,
        "severity": severity,
        "summary": summary,
        "details": remainder,
        "raw": raw,
    }


def _mailbox_audit_events(email_addr: str, hours: int = 72) -> List[Dict[str, Any]]:
    rows = db_audit_query_mailbox(email_addr, hours=hours, limit=500)
    events: List[Dict[str, Any]] = []
    for row in rows:
        action = str(row.get("action") or "")
        if action in {"account.suspend", "account.restore"}:
            category = "lifecycle"
        elif action.startswith("auth."):
            category = "auth"
        elif action.startswith("queue."):
            category = "delivery"
        else:
            category = "admin"
        severity = "info" if row.get("ok") else "error"
        details_text = ""
        if row.get("details") is not None:
            try:
                details_text = json.dumps(row.get("details"), ensure_ascii=False)
            except Exception:
                details_text = str(row.get("details"))
        events.append({
            "ts": row.get("ts") or _utc_now_iso(),
            "source": "audit",
            "category": category,
            "severity": severity,
            "summary": f"{action} by {row.get('admin_user') or '?'}",
            "details": details_text,
            "raw": details_text,
        })
    return events


def _mailbox_queue_events(
    email_addr: str,
    entries: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    target = str(email_addr or "").strip().lower()
    events: List[Dict[str, Any]] = []
    for entry in entries if entries is not None else _postfix_queue_list():
        sender = str(entry.get("sender") or "").lower()
        recipients = [str(value or "").lower() for value in (entry.get("recipients") or [])]
        if sender != target and target not in recipients:
            continue
        summary = "Queue hold" if entry.get("hold") else "Queue active" if entry.get("active") else "Queue deferred"
        severity = "warn" if entry.get("deferred") or entry.get("hold") else "info"
        events.append({
            "ts": _parse_queue_event_ts(str(entry.get("time") or "")),
            "source": "queue",
            "category": "delivery",
            "severity": severity,
            "summary": summary,
            "details": f"{entry.get('queue_id') or ''} · from {entry.get('sender') or '—'} · rcpt {', '.join(entry.get('recipients') or []) or '—'}",
            "raw": entry.get("error") or "",
        })
    return events


def _mailbox_log_events(email_addr: str, hours: int = 72) -> List[Dict[str, Any]]:
    ok, output = _docker_logs_capture(CFG.MAILSERVER_CONTAINER, hours)
    if not ok or not output:
        return []
    events: List[Dict[str, Any]] = []
    for line in output.splitlines():
        event = _parse_mailserver_log_event(line, email_addr)
        if event:
            events.append(event)
    return events


def _timeline_apply_filter(events: List[Dict[str, Any]], filter_name: str) -> List[Dict[str, Any]]:
    if filter_name not in {"delivery", "auth", "lifecycle"}:
        return events
    return [event for event in events if event.get("category") == filter_name]


def _timeline_summary(events: List[Dict[str, Any]], queue_summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    latest_issue = next(
        (
            event.get("summary") or event.get("details") or ""
            for event in events
            if event.get("severity") in {"warn", "error"}
        ),
        "",
    )
    return {
        "total": len(events),
        "queue_total": int((queue_summary or {}).get("total", 0)),
        "auth_failures": sum(1 for event in events if event.get("category") == "auth" and event.get("severity") == "error"),
        "delivery_failures": sum(1 for event in events if event.get("category") == "delivery" and event.get("severity") in {"warn", "error"}),
        "lifecycle_events": sum(1 for event in events if event.get("category") == "lifecycle"),
        "latest_issue": latest_issue,
    }


def _build_mailbox_timeline(email_addr: str, hours: int = 72, filter_name: str = "all") -> Dict[str, Any]:
    target = str(email_addr or "").strip().lower()
    queue_future = _mailadmin_read_executor.submit(_postfix_queue_list)
    logs_future = _mailadmin_read_executor.submit(_mailbox_log_events, target, hours)
    audit_events = _mailbox_audit_events(target, hours)
    queue_snapshot = queue_future.result()
    queue_entries = [
        entry for entry in queue_snapshot
        if (str(entry.get("sender") or "").lower() == target)
        or target in [str(value or "").lower() for value in (entry.get("recipients") or [])]
    ]
    queue_summary = {
        "total": len(queue_entries),
        "active": sum(1 for entry in queue_entries if entry.get("active")),
        "deferred": sum(1 for entry in queue_entries if entry.get("deferred") and not entry.get("active") and not entry.get("hold")),
        "hold": sum(1 for entry in queue_entries if entry.get("hold")),
    }
    all_events = _mailbox_queue_events(target, queue_snapshot) + audit_events + logs_future.result()
    all_events.sort(key=lambda event: str(event.get("ts") or ""), reverse=True)
    filtered_events = _timeline_apply_filter(all_events, filter_name)
    summary = _timeline_summary(all_events, queue_summary)
    return {
        "ok": True,
        "email": target,
        "hours": max(1, min(int(hours or 72), 24 * 14)),
        "filter": filter_name if filter_name in {"all", "delivery", "auth", "lifecycle"} else "all",
        "summary": summary,
        "events": filtered_events,
    }


def _onboarding_check(mail_login: str) -> Dict[str, Any]:
    clean = _sanitize_mail_login(mail_login)
    if not clean:
        return {"mail_login": "", "email": "", "available": False, "reason": "empty"}
    email_addr = _mail_login_to_email(clean)
    aliases = db_list_all_aliases()
    owner = next((row for row in aliases if (row.get("mail_login") or "").lower() == clean), None)
    exists = email_addr in set(_collect_mail_accounts())
    managed = _managed_address_find_by_email(email_addr)
    return {
        "mail_login": clean,
        "email": email_addr,
        "available": not exists and owner is None and managed is None,
        "mailbox_exists": exists,
        "alias_exists": owner is not None,
        "managed_address": managed,
        "owner": {
            "ad_login": owner.get("ad_login"),
            "employee_name": owner.get("employee_name"),
            "source": owner.get("source"),
        } if owner else None,
        "reason": (
            "mailbox_exists"
            if exists
            else "alias_exists"
            if owner
            else "managed_address"
            if managed
            else ""
        ),
    }


def _support_search(query: str, limit: int = 25) -> Dict[str, Any]:
    q = str(query or "").strip().lower()
    accounts_future = _mailadmin_read_executor.submit(_collect_mail_accounts)
    queue_future = _mailadmin_read_executor.submit(_postfix_queue_list)
    employees = _fetch_employees_cached()
    aliases = db_list_all_aliases()
    lifecycle_index = db_list_mailbox_lifecycle_states()
    alias_by_ad = {
        str(row.get("ad_login") or "").lower(): row
        for row in aliases
        if str(row.get("ad_login") or "").strip()
    }
    alias_by_mail = {
        str(row.get("mail_login") or "").lower(): row
        for row in aliases
        if str(row.get("mail_login") or "").strip()
    }
    accounts = set(accounts_future.result())
    queue_index = _collect_queue_index(queue_future.result())
    results: List[Dict[str, Any]] = []
    seen_keys = set()

    def add_item(item: Dict[str, Any], haystack: List[str], exact_candidates: List[str]) -> None:
        hay = " ".join(part for part in haystack if part).lower()
        if q and q not in hay:
            return
        if item["key"] in seen_keys:
            return
        score = 0
        if not q:
            score = 10 if item.get("mailbox_exists") else 5
        elif any(q == str(candidate or "").lower() for candidate in exact_candidates):
            score = 300
        elif any(str(candidate or "").lower().startswith(q) for candidate in exact_candidates):
            score = 220
        elif q in hay:
            score = 120
        score += 40 if item.get("mailbox_exists") else 0
        score += 10 if item.get("emp_id") else 0
        score += min((item.get("queue_summary") or {}).get("total", 0), 9)
        item["score"] = score
        seen_keys.add(item["key"])
        results.append(item)

    for emp in employees:
        emp_id = emp.get("id")
        ad_login = str(emp.get("ad_login") or "").strip().lower()
        mail_login = str(emp.get("mail_login") or "").strip().lower()
        alias = alias_by_ad.get(ad_login) or alias_by_mail.get(mail_login)
        if alias and not mail_login:
            mail_login = str(alias.get("mail_login") or "").strip().lower()
        email_addr = _mail_login_to_email(mail_login) if mail_login else ""
        queue_summary = queue_index.get(email_addr, {"total": 0, "active": 0, "deferred": 0, "hold": 0})
        display_name = _employee_display_name(emp)
        item = {
            "kind": "employee",
            "key": f"emp:{emp_id}",
            "emp_id": emp_id,
            "display_name": display_name,
            "email": email_addr,
            "mail_login": mail_login,
            "ad_login": ad_login,
            "department": emp.get("department"),
            "title": emp.get("job_title") or emp.get("title"),
            "mailbox_exists": email_addr in accounts if email_addr else False,
            "alias_source": alias.get("source") if alias else ("proxy_employees" if mail_login else ""),
            "queue_summary": queue_summary,
            "suggested_mail_login": mail_login or ad_login,
            "lifecycle_state": (lifecycle_index.get(email_addr or "") or {}).get("state", "active"),
        }
        add_item(
            item,
            [
                display_name,
                ad_login,
                mail_login,
                email_addr,
                str(emp.get("department") or ""),
            ],
            [email_addr, mail_login, ad_login, display_name],
        )

    for email_addr in sorted(accounts):
        local = email_addr.split("@", 1)[0].lower()
        alias = alias_by_mail.get(local)
        if alias:
            continue
        queue_summary = queue_index.get(email_addr, {"total": 0, "active": 0, "deferred": 0, "hold": 0})
        item = {
            "kind": "mailbox",
            "key": f"box:{email_addr}",
            "email": email_addr,
            "mail_login": local,
            "mailbox_exists": True,
            "display_name": email_addr,
            "ad_login": "",
            "department": "",
            "title": "",
            "alias_source": "",
            "queue_summary": queue_summary,
            "suggested_mail_login": local,
            "lifecycle_state": (lifecycle_index.get(email_addr) or {}).get("state", "active"),
        }
        add_item(item, [email_addr, local], [email_addr, local])

    results.sort(
        key=lambda item: (
            -int(item.get("score", 0)),
            0 if item.get("mailbox_exists") else 1,
            str(item.get("display_name") or item.get("email") or ""),
        )
    )
    return {
        "query": q,
        "results": results[:limit],
        "total": len(results),
    }


# ---------------------------------------------------------------------------
# Direct write to docker-mailserver postfix-accounts.cf
# ---------------------------------------------------------------------------
# docker-mailserver's `setup email add` is slow because it triggers a synchronous
# postfix/dovecot reload via the change-detection daemon (which uses 5-second
# lock retry sleeps). We bypass that by:
#   1. Generating the SHA512-CRYPT hash via `doveadm pw` (~0.2s)
#   2. Appending the line directly to /tmp/docker-mailserver/postfix-accounts.cf
#   3. Returning immediately (the daemon will pick up the change within ~5s)
#
# This brings creation time from 15-30s down to ~1s.

_mailserver_lock = threading.RLock()
_MAILSERVER_ACCOUNT_LOCK_PATH = os.environ.get(
    "MAILSERVER_ACCOUNT_LOCK_PATH",
    "/tmp/rupochta-mail-accounts.lock",
)

_EMAIL_RE = re.compile(r'^[a-z0-9][a-z0-9._\-]*@[a-z0-9.\-]+\.[a-z]{2,}$')


@contextmanager
def _mailserver_account_transaction():
    with _mailserver_lock:
        with open(_MAILSERVER_ACCOUNT_LOCK_PATH, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _doveadm_hash_password(password: str) -> tuple[bool, str]:
    """Generate SHA512-CRYPT hash via `doveadm pw` inside the mailserver container."""
    import subprocess
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", CFG.MAILSERVER_CONTAINER,
             "doveadm", "pw", "-s", "SHA512-CRYPT", "-p", password],
            capture_output=True, text=True, timeout=8,
        )
        if r.returncode != 0:
            return False, f"doveadm pw failed: {(r.stderr or r.stdout).strip()}"
        line = r.stdout.strip()
        if not line.startswith("{SHA512-CRYPT}"):
            return False, f"unexpected hash output: {line[:60]}"
        return True, line
    except Exception as e:
        return False, f"doveadm exec: {e}"


def _mailserver_apply_account_changes() -> tuple[bool, str]:
    """Force docker-mailserver to rebuild account maps and reload services.

    Direct writes update only the source files under /tmp/docker-mailserver.
    To make new accounts/passwords visible immediately to IMAP/SMTP, we mirror
    the essential changedetector path synchronously.
    """
    import subprocess
    script = r'''
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
'''
    with _mailserver_lock:
        try:
            r = subprocess.run(
                ["docker", "exec", "-i", CFG.MAILSERVER_CONTAINER, "bash", "-s"],
                input=script,
                capture_output=True,
                text=True,
                timeout=25,
            )
        except Exception as e:
            return False, f"apply exec: {e}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, (r.stdout or "").strip() or "applied"


def _queue_mailserver_apply_account_changes() -> None:
    global _mailserver_apply_thread

    def _runner() -> None:
        global _mailserver_apply_thread
        try:
            ok, status = _mailserver_apply_account_changes()
            if not ok:
                log.warning("mailserver deferred apply failed: %s", status)
            else:
                log.info("mailserver deferred apply complete: %s", status)
        finally:
            with _mailserver_apply_thread_lock:
                _mailserver_apply_thread = None

    with _mailserver_apply_thread_lock:
        if _mailserver_apply_thread and _mailserver_apply_thread.is_alive():
            return
        _mailserver_apply_thread = threading.Thread(target=_runner, daemon=True)
        _mailserver_apply_thread.start()


def _mailserver_add_user_direct_unlocked(email: str, password: str) -> tuple[bool, str]:
    """Atomically append email|hash to postfix-accounts.cf via docker exec.

    Returns (ok, status) where status is one of: 'added', 'already exists',
    or an error message.
    """
    import subprocess
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        return False, "Invalid email format"

    ok, hash_or_err = _doveadm_hash_password(password)
    if not ok:
        return False, hash_or_err
    pw_hash = hash_or_err

    # Use a small bash script that:
    #   - Checks if email already exists in the file (idempotent)
    #   - Appends new line atomically
    # We pass email and hash via env vars (avoids shell escaping nightmares).
    script = (
        'set -eu; '
        'ACC=/tmp/docker-mailserver/postfix-accounts.cf; '
        'touch "$ACC"; '
        'if grep -qE "^${EMAIL}\\|" "$ACC" 2>/dev/null; then '
        '  echo EXISTS; '
        'else '
        '  printf "%s|%s\\n" "$EMAIL" "$PWHASH" >> "$ACC"; '
        '  echo ADDED; '
        'fi'
    )
    with _mailserver_lock:
        try:
            r = subprocess.run(
                ["docker", "exec", "-i",
                 "-e", f"EMAIL={email}",
                 "-e", f"PWHASH={pw_hash}",
                 CFG.MAILSERVER_CONTAINER, "bash", "-c", script],
                capture_output=True, text=True, timeout=6,
            )
        except Exception as e:
            return False, f"docker exec: {e}"

    if r.returncode != 0:
        return False, f"append failed: {(r.stderr or r.stdout).strip()}"
    out = r.stdout.strip()
    if out == "EXISTS":
        return True, "already exists"
    if out == "ADDED":
        ok_apply, apply_status = _mailserver_apply_account_changes()
        if not ok_apply:
            log.warning("mailserver apply after add failed for %s: %s", email, apply_status)
            rollback_script = (
                'set -eu; '
                'ACC=/tmp/docker-mailserver/postfix-accounts.cf; '
                '[ -f "$ACC" ] || { echo REMOVED; exit 0; }; '
                'TMP="$(mktemp)"; '
                'awk -F"|" -v email="$EMAIL" \'$1 != email { print $0 }\' '
                '"$ACC" > "$TMP"; '
                'cat "$TMP" > "$ACC"; rm -f "$TMP"; echo REMOVED'
            )
            try:
                rollback = subprocess.run(
                    [
                        "docker", "exec", "-i",
                        "-e", f"EMAIL={email}",
                        CFG.MAILSERVER_CONTAINER, "bash", "-c", rollback_script,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=6,
                )
            except Exception as exc:
                return False, f"apply failed: {apply_status}; rollback failed: {exc}"
            if rollback.returncode != 0:
                rollback_error = (rollback.stderr or rollback.stdout).strip()
                return False, (
                    f"apply failed: {apply_status}; rollback failed: {rollback_error}"
                )
            rollback_apply_ok, rollback_apply_status = _mailserver_apply_account_changes()
            if not rollback_apply_ok:
                log.error(
                    "mailserver apply after add rollback failed for %s: %s",
                    email,
                    rollback_apply_status,
                )
                return False, (
                    f"apply failed: {apply_status}; staged account removed, "
                    f"rollback apply failed: {rollback_apply_status}; "
                    "runtime state uncertain"
                )
            return False, f"apply failed: {apply_status}; staged account removed"
        return True, "added"
    return False, f"unexpected: {out!r}"


def _mailserver_add_user_direct(email: str, password: str) -> tuple[bool, str]:
    with _mailserver_account_transaction():
        return _mailserver_add_user_direct_unlocked(email, password)


def _mailserver_delete_user_direct_unlocked(email: str) -> tuple[bool, str]:
    """Delete an account and all associated docker-mailserver data.

    Returns (ok, status) where status is 'removed', 'not found', or error.
    """
    import subprocess
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        return False, "Invalid email format"
    with _mailserver_lock:
        try:
            r = subprocess.run(
                [
                    "docker",
                    "exec",
                    CFG.MAILSERVER_CONTAINER,
                    "setup",
                    "email",
                    "del",
                    "-y",
                    email,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as e:
            return False, f"docker exec: {e}"
    output = (r.stderr or r.stdout or "").strip()
    if r.returncode == 0:
        return True, "removed"
    if "does not exist" in output.lower() or "not found" in output.lower():
        return True, "not found"
    return False, f"delete failed: {output or 'unknown error'}"


def _mailserver_delete_user_direct(email: str) -> tuple[bool, str]:
    with _mailserver_account_transaction():
        return _mailserver_delete_user_direct_unlocked(email)


def _docker_exec_capture(cmd: List[str], timeout: int = 8) -> tuple[bool, str]:
    """Run `docker exec mailserver <cmd>` and capture stdout. Returns (ok, output)."""
    import subprocess
    if not _local_mailserver_enabled() or not CFG.MAILSERVER_CONTAINER:
        return False, "local mailserver unavailable"
    full = ["docker", "exec", CFG.MAILSERVER_CONTAINER] + cmd
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return True, r.stdout
        return False, (r.stderr or r.stdout).strip()
    except Exception as e:
        return False, str(e)


def _mailserver_stats() -> Dict[str, Any]:
    """Compute mailbox count, per-mailbox sizes, postfix queue stats.

    Returns dict with: total_mailboxes, mailboxes (list of {email, size_bytes,
    size_human, last_modified}), queue_kb, queue_count, deferred_count.
    """
    out: Dict[str, Any] = {
        "total_mailboxes": 0,
        "mailboxes": [],
        "queue_kb": 0,
        "queue_count": 0,
        "deferred_count": 0,
        "domain": CFG.MAIL_DOMAIN,
    }

    du_future = _mailserver_stats_executor.submit(
        _docker_exec_capture,
        ["bash", "-c", f"du -sb /var/mail/{CFG.MAIL_DOMAIN}/*/ 2>/dev/null || true"],
        10,
    )
    mailq_future = _mailserver_stats_executor.submit(
        _docker_exec_capture,
        ["bash", "-c", "mailq 2>/dev/null"],
        8,
    )

    # 1. Per-mailbox sizes via du -s
    ok, du_out = du_future.result()
    mailboxes = []
    if ok and du_out:
        for line in du_out.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                size_b = int(parts[0])
            except ValueError:
                continue
            path = parts[1].strip()
            local = Path(path).name
            email = f"{local}@{CFG.MAIL_DOMAIN}"
            mailboxes.append({
                "email": email,
                "size_bytes": size_b,
                "size_human": _human_size(size_b),
            })
    mailboxes.sort(key=lambda m: m["size_bytes"], reverse=True)
    out["mailboxes"] = mailboxes
    out["total_mailboxes"] = len(mailboxes)

    # 2. Postfix queue summary via mailq
    ok, mailq_out = mailq_future.result()
    if ok and mailq_out:
        # Last line: "-- N Kbytes in M Requests."
        for line in reversed(mailq_out.strip().splitlines()):
            m = re.search(r"--\s+(\d+)\s+Kbytes\s+in\s+(\d+)\s+Request", line)
            if m:
                out["queue_kb"] = int(m.group(1))
                out["queue_count"] = int(m.group(2))
                break
        # Count deferred entries
        out["deferred_count"] = sum(1 for ln in mailq_out.splitlines()
                                     if re.match(r"^[A-F0-9]{8,}\*?\s", ln) and "*" not in ln.split()[0])

    return out


def _human_size(n: int) -> str:
    """Format bytes as human-readable string (KB/MB/GB)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}PB"


def _parse_postqueue(out: str) -> List[Dict[str, Any]]:
    """Parse `postqueue -p` output into structured queue entries.

    Format example:
        -Queue ID-  --Size-- ----Arrival Time---- -Sender/Recipient-------
        ABCD12345*    1234 Mon Apr  8 12:00:00  sender@example.com
                                                 (host[ip] said: connection timed out)
                                                 rcpt@target.com

    Returns list of {queue_id, size, time, sender, recipients[], error, deferred}
    """
    entries: List[Dict[str, Any]] = []
    if not out:
        return entries
    lines = out.splitlines()
    current: Optional[Dict[str, Any]] = None
    header_re = re.compile(
        r"^(?P<id>[A-F0-9]{6,})(?P<flag>[*!]?)\s+(?P<size>\d+)\s+"
        r"(?P<time>\w+\s+\w+\s+\d+\s+\d+:\d+:\d+)\s+(?P<sender>\S.*)$"
    )
    for line in lines:
        m = header_re.match(line)
        if m:
            if current:
                entries.append(current)
            current = {
                "queue_id": m.group("id"),
                "deferred": m.group("flag") == "",  # no flag = active or default
                "active": m.group("flag") == "*",
                "hold": m.group("flag") == "!",
                "size": int(m.group("size")),
                "time": m.group("time").strip(),
                "sender": m.group("sender").strip(),
                "recipients": [],
                "error": "",
            }
        elif current:
            stripped = line.strip()
            if stripped.startswith("(") and stripped.endswith(")"):
                current["error"] = stripped[1:-1]
            elif stripped and "@" in stripped:
                current["recipients"].append(stripped)
    if current:
        entries.append(current)
    return entries


def _mailserver_get_account_hash(email: str) -> tuple[bool, str]:
    import subprocess
    target = email.strip().lower()
    if not _EMAIL_RE.match(target):
        return False, "Invalid email format"
    script = (
        'set -eu; '
        'ACC=/tmp/docker-mailserver/postfix-accounts.cf; '
        '[ -f "$ACC" ] || { echo NOT_FOUND; exit 3; }; '
        'awk -F"|" -v email="$EMAIL" '
        '\'$1==email { print substr($0, length($1) + 2); found=1; exit } '
        'END { if (!found) exit 3 }\' "$ACC"'
    )
    with _mailserver_lock:
        try:
            r = subprocess.run(
                ["docker", "exec", "-i", "-e", f"EMAIL={target}", CFG.MAILSERVER_CONTAINER, "bash", "-c", script],
                capture_output=True,
                text=True,
                timeout=6,
            )
        except Exception as e:
            return False, f"docker exec: {e}"
    if r.returncode != 0:
        out = (r.stderr or r.stdout).strip()
        if "NOT_FOUND" in out or r.returncode == 3:
            return False, "not found"
        return False, f"read failed: {out}"
    value = (r.stdout or "").strip()
    return (True, value) if value else (False, "not found")


def _mailserver_set_account_hash_direct_unlocked(
    email: str,
    pw_hash: str,
    wait_apply: bool = True,
    queue_deferred_apply: bool = True,
) -> tuple[bool, str]:
    import subprocess
    target = email.strip().lower()
    if not _EMAIL_RE.match(target):
        return False, "Invalid email format"
    if not pw_hash.strip():
        return False, "Empty password hash"
    previous_hash = ""
    if wait_apply:
        previous_ok, previous_hash = _mailserver_get_account_hash(target)
        if not previous_ok:
            return False, previous_hash
    script = (
        'set -eu; '
        'ACC=/tmp/docker-mailserver/postfix-accounts.cf; '
        '[ -f "$ACC" ] || { echo NOT_FOUND; exit 3; }; '
        'TMP="$(mktemp)"; '
        'awk -F"|" -v email="$EMAIL" -v pwhash="$PWHASH" '
        '\'{ if ($1 == email) { print email "|" pwhash; found=1 } else { print $0 } } '
        'END { if (!found) exit 3 }\' "$ACC" > "$TMP"; '
        'cat "$TMP" > "$ACC"; '
        'rm -f "$TMP"; '
        'echo UPDATED'
    )
    with _mailserver_lock:
        try:
            r = subprocess.run(
                [
                    "docker", "exec", "-i",
                    "-e", f"EMAIL={target}",
                    "-e", f"PWHASH={pw_hash}",
                    CFG.MAILSERVER_CONTAINER, "bash", "-c", script,
                ],
                capture_output=True,
                text=True,
                timeout=6,
            )
        except Exception as e:
            return False, f"docker exec: {e}"
    if r.returncode != 0:
        out = (r.stderr or r.stdout).strip()
        if "NOT_FOUND" in out or r.returncode == 3:
            return False, "not found"
        return False, f"update failed: {out}"
    if wait_apply:
        ok_apply, apply_status = _mailserver_apply_account_changes()
        if not ok_apply:
            log.warning("mailserver apply after hash update failed for %s: %s", target, apply_status)
            rollback_ok, rollback_status = _mailserver_set_account_hash_direct_unlocked(
                target,
                previous_hash,
                wait_apply=False,
                queue_deferred_apply=False,
            )
            if rollback_ok:
                restore_ok, restore_status = _mailserver_apply_account_changes()
                if not restore_ok:
                    log.error(
                        "mailserver apply after password rollback failed for %s: %s",
                        target,
                        restore_status,
                    )
                    return False, (
                        f"apply failed: {apply_status}; old hash restored, "
                        f"rollback apply failed: {restore_status}; "
                        "runtime state uncertain"
                    )
            else:
                log.error(
                    "mailserver password rollback failed for %s: %s",
                    target,
                    rollback_status,
                )
                return False, (
                    f"apply failed: {apply_status}; rollback failed: "
                    f"{rollback_status}; runtime state uncertain"
                )
            return False, f"apply failed: {apply_status}; password change rolled back"
        return True, "updated"
    if queue_deferred_apply:
        _queue_mailserver_apply_account_changes()
    return True, "updated_deferred"


def _mailserver_set_account_hash_direct(
    email: str,
    pw_hash: str,
    wait_apply: bool = True,
    queue_deferred_apply: bool = True,
) -> tuple[bool, str]:
    with _mailserver_account_transaction():
        return _mailserver_set_account_hash_direct_unlocked(
            email,
            pw_hash,
            wait_apply=wait_apply,
            queue_deferred_apply=queue_deferred_apply,
        )


def _mailserver_set_password_direct(email: str, new_password: str) -> tuple[bool, str]:
    """Update password for an existing user by replacing the line atomically."""
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        return False, "Invalid email format"

    ok, hash_or_err = _doveadm_hash_password(new_password)
    if not ok:
        return False, hash_or_err
    new_hash = hash_or_err
    return _mailserver_set_account_hash_direct(email, new_hash, wait_apply=True)


def _probe_tcp(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "ok"
    except Exception as exc:
        return False, str(exc)


def _probe_imap_ready(host: Optional[str] = None, port: Optional[int] = None) -> tuple[bool, str]:
    target_host = str(host or CFG.IMAP_HOST or "").strip() or CFG.IMAP_HOST
    target_port = int(port or CFG.IMAP_PORT)
    ok, status = _probe_tcp(target_host, target_port, timeout=4.0)
    return ok, f"{target_host}:{target_port} {status}"


def _control_imap_probe_target(control: Optional[Dict[str, Any]] = None) -> tuple[Optional[str], Optional[int]]:
    payload = dict(control or {})
    cfg = dict(payload.get("config") or {})
    imap = dict(cfg.get("imap") or {})
    host = str(imap.get("host") or "").strip()
    port_raw = imap.get("port")
    try:
        port = int(port_raw) if port_raw is not None else None
    except Exception:
        port = None
    if not host:
        return None, None
    return host, port or CFG.IMAP_PORT


def _use_control_imap_probe_target(target_host: Optional[str], target_port: Optional[int]) -> bool:
    host = str(target_host or "").strip()
    if not host:
        return False
    configured_host = str(CFG.IMAP_HOST or "").strip().lower()
    # In backup mode we may intentionally pin IMAP to a local loopback tunnel.
    # Don't overwrite that healthy path with the public control-plane hint.
    if configured_host in {"127.0.0.1", "localhost", "::1"}:
        return False
    return host != CFG.IMAP_HOST or int(target_port or 0) != int(CFG.IMAP_PORT)


def _probe_smtp_ready() -> tuple[bool, str]:
    try:
        with smtplib.SMTP(CFG.SMTP_HOST, CFG.SMTP_PORT, timeout=5) as s:
            code, banner = s.docmd("NOOP")
        if 200 <= int(code) < 400:
            return True, str(banner or b"OK")
        return False, f"bad_smtp_response:{code}"
    except Exception as exc:
        return False, str(exc)


def _probe_mail_control_ready() -> tuple[bool, str]:
    try:
        data, control_error = _mail_control_health_cached(timeout=2, max_age=20)
        panel_ok = bool(data.get("panel_ok"))
        control_ok = bool(data.get("mail_control_ok")) if "mail_control_ok" in data else bool(data.get("mail_vm_ssh_ok"))
        control_status = str(data.get("mail_control_status") or data.get("mail_vm_ssh_status") or control_error or "").strip() or "unknown"
        smtp_proxy_ok = bool(data.get("smtp_proxy_ok"))
        smtp_proxy_status = str(data.get("smtp_proxy_status") or "").strip() or "unknown"
        if panel_ok and control_ok:
            return True, f"panel_ok mail_control_ok smtp_proxy_{'ok' if smtp_proxy_ok else 'fail'}:{smtp_proxy_status}"
        if panel_ok:
            return False, f"panel_ok mail_control_fail:{control_status} smtp_proxy_{'ok' if smtp_proxy_ok else 'fail'}:{smtp_proxy_status}"
        return False, "panel_fail"
    except Exception as exc:
        return False, str(exc)


def _mail_control_snapshot(control: Optional[Dict[str, Any]] = None, control_error: str = "") -> Dict[str, Any]:
    payload = dict(control or {})
    panel_ok = bool(payload.get("panel_ok"))
    control_ok = bool(payload.get("mail_control_ok")) if "mail_control_ok" in payload else bool(payload.get("mail_vm_ssh_ok"))
    smtp_proxy_ok = bool(payload.get("smtp_proxy_ok"))
    control_status = str(payload.get("mail_control_status") or payload.get("mail_vm_ssh_status") or control_error or "unknown").strip() or "unknown"
    smtp_proxy_status = str(payload.get("smtp_proxy_status") or "unknown").strip() or "unknown"
    if payload:
        if panel_ok and control_ok:
            status = f"panel_ok mail_control_ok smtp_proxy_{'ok' if smtp_proxy_ok else 'fail'}:{smtp_proxy_status}"
            ok = True
        elif panel_ok:
            status = f"panel_ok mail_control_fail:{control_status} smtp_proxy_{'ok' if smtp_proxy_ok else 'fail'}:{smtp_proxy_status}"
            ok = False
        else:
            status = "panel_fail"
            ok = False
    else:
        status = control_error or "unavailable"
        ok = False
    return {
        "ok": ok,
        "status": status,
        "panel_ok": panel_ok,
        "mail_control_ok": control_ok,
        "mail_control_status": control_status,
        "mail_control_source": payload.get("mail_control_source") or "",
        "mail_vm_ssh_ok": bool(payload.get("mail_vm_ssh_ok")),
        "mail_vm_ssh_status": str(payload.get("mail_vm_ssh_status") or "unknown").strip() or "unknown",
        "smtp_proxy_ok": smtp_proxy_ok,
        "smtp_proxy_status": smtp_proxy_status,
    }


def _build_ready_payload(
    imap_ok: bool,
    imap_status: str,
    smtp_ok: bool,
    smtp_status: str,
    control: Optional[Dict[str, Any]] = None,
    control_error: str = "",
) -> Dict[str, Any]:
    runtime_mode = _mail_runtime_mode()
    backup_mode = runtime_mode == "backup"
    control_snapshot = _mail_control_snapshot(control, control_error) if backup_mode else {
        "ok": False,
        "status": "not_applicable",
        "smtp_proxy_ok": False,
        "smtp_proxy_status": "not_applicable",
    }
    mail_control_ok = bool(control_snapshot.get("ok"))
    mail_control_status = str(control_snapshot.get("status") or "not_applicable")
    smtp_client_ok = bool(control_snapshot.get("smtp_proxy_ok")) if backup_mode else False
    smtp_client_status = (
        str(control_snapshot.get("smtp_proxy_status") or "unknown")
        if backup_mode else "not_applicable"
    )
    ready_ok = bool(smtp_ok and (imap_ok or (backup_mode and mail_control_ok)))
    return {
        "ok": ready_ok,
        "mode": runtime_mode,
        "mail_admin_available": _mail_admin_available(),
        "checks": {
            "imap_public": {"ok": imap_ok, "status": imap_status},
            "smtp": {"ok": smtp_ok, "status": smtp_status},
            "smtp_client": {"ok": smtp_client_ok, "status": smtp_client_status},
            "mail_control": {"ok": mail_control_ok, "status": mail_control_status},
        },
        "degraded": bool(
            backup_mode and (
                (not imap_ok and mail_control_ok)
                or not smtp_client_ok
            )
        ),
    }


def _mail_delivery_global_snapshot() -> Dict[str, Any]:
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT status, COUNT(*) AS c FROM scheduled_sends GROUP BY status"
        ).fetchall()
        counts = {str(row["status"] or ""): int(row["c"] or 0) for row in rows}
        recent_sent_24h = int(
            con.execute(
                "SELECT COUNT(*) FROM scheduled_sends "
                "WHERE status='sent' "
                "AND datetime(COALESCE(sent_at, created_at)) >= datetime('now', '-1 day')"
            ).fetchone()[0]
            or 0
        )
    failed = int(counts.get("failed", 0))
    pending = int(counts.get("pending", 0))
    sending = int(counts.get("sending", 0))
    sent = int(counts.get("sent", 0))
    if failed:
        status = "failed"
    elif pending or sending:
        status = "queued"
    elif recent_sent_24h:
        status = "recent_sent"
    else:
        status = "idle"
    return {
        "pending": pending,
        "sending": sending,
        "failed": failed,
        "sent": sent,
        "recent_sent_24h": recent_sent_24h,
        "status": status,
    }


def _mail_health_watcher_counts() -> Dict[str, Any]:
    rows = _fetch_employees_cached()
    total = 0
    enabled = 0
    available = False
    for row in rows:
        mail_login = str(row.get("mail_login") or "").strip().lower()
        if not mail_login:
            continue
        total += 1
        if "mail_notify_enabled" in row:
            available = True
            if bool(row.get("mail_notify_enabled")):
                enabled += 1
    return {
        "enabled": enabled if available else None,
        "total": total,
        "available": available,
    }


def _mail_health_severity(control: Dict[str, Any], ready: Dict[str, Any], delivery: Dict[str, Any]) -> tuple[str, str]:
    primary_mode = ready.get("mode") == "primary"
    panel_ok = bool(control.get("panel_ok"))
    control_ok = bool(control.get("mail_control_ok")) if "mail_control_ok" in control else bool(control.get("mail_vm_ssh_ok"))
    smtp_proxy_ok = bool(control.get("smtp_proxy_ok"))
    ready_ok = bool(ready.get("ok"))
    delivery_failed = int(delivery.get("failed") or 0)
    delivery_queued = int(delivery.get("pending") or 0) + int(delivery.get("sending") or 0)
    control_failed = not primary_mode and (
        not panel_ok or not control_ok or not smtp_proxy_ok
    )
    if control_failed or not ready_ok or delivery_failed >= 3:
        severity = "fail"
    elif delivery_failed > 0 or delivery_queued >= 5 or bool(ready.get("degraded")):
        severity = "warn"
    else:
        severity = "ok"
    key = "|".join([
        severity,
        str(ready.get("mode") or ""),
        str(int(panel_ok)),
        str(int(control_ok)),
        str(int(smtp_proxy_ok)),
        str(int(ready_ok)),
        str(int(ready.get("degraded") is True)),
        str(delivery_failed),
        str(delivery_queued),
    ])
    return severity, key


def _mail_health_issue_list(
    control: Dict[str, Any],
    ready: Dict[str, Any],
    delivery: Dict[str, Any],
    control_error: str = "",
    delivery_error: str = "",
    watcher_error: str = "",
) -> List[str]:
    issues: List[str] = []
    primary_mode = ready.get("mode") == "primary"
    if not primary_mode:
        if control_error:
            issues.append(f"control-plane error: {control_error}")
        if not bool(control.get("panel_ok")):
            issues.append("SOC mail control-plane unavailable")
        elif not bool(control.get("mail_control_ok")) if "mail_control_ok" in control else not bool(control.get("mail_vm_ssh_ok")):
            issues.append(f"mail control: {control.get('mail_control_status') or control.get('mail_vm_ssh_status') or 'down'}")
        elif not bool(control.get("smtp_proxy_ok")):
            issues.append(f"SMTP proxy: {control.get('smtp_proxy_status') or 'down'}")
    if not bool(ready.get("ok")):
        issues.append("webmail is not ready" if primary_mode else "backup webmail is not ready")
    elif bool(ready.get("degraded")):
        issues.append("backup webmail degraded")
    failed = int(delivery.get("failed") or 0)
    queued = int(delivery.get("pending") or 0) + int(delivery.get("sending") or 0)
    if failed:
        issues.append(f"outbox failed: {failed}")
    if queued:
        issues.append(f"outbox queued: {queued}")
    if delivery_error:
        issues.append(f"delivery snapshot error: {delivery_error}")
    if watcher_error:
        issues.append(f"watcher snapshot error: {watcher_error}")
    return issues


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "mode": _mail_runtime_mode(),
        "mail_admin_available": _mail_admin_available(),
    }


@app.get("/ready")
async def ready() -> JSONResponse:
    loop = asyncio.get_event_loop()
    imap_task = loop.run_in_executor(None, _probe_imap_ready)
    smtp_task = loop.run_in_executor(None, _probe_smtp_ready)
    backup_mode = _mail_runtime_mode() == "backup"
    control: Dict[str, Any] = {}
    control_error = ""
    control_task = loop.run_in_executor(None, _mail_control_health_cached, 2, 30.0) if backup_mode else None
    imap_ok, imap_status = await imap_task
    smtp_ok, smtp_status = await smtp_task
    if backup_mode:
        try:
            control, control_error = await control_task
        except Exception as exc:
            control_error = str(exc)
        target_host, target_port = _control_imap_probe_target(control)
        if _use_control_imap_probe_target(target_host, target_port):
            imap_ok, imap_status = await loop.run_in_executor(None, _probe_imap_ready, target_host, target_port)
    payload = _build_ready_payload(imap_ok, imap_status, smtp_ok, smtp_status, control, control_error)
    return JSONResponse(payload, status_code=200 if payload["ok"] else 503)


@app.post("/api/admin/create_account")
async def api_admin_create_account(body: AdminCreateBody, request: Request):
    """Create an employee mailbox through the owner-aware control plane."""
    _require_mail_admin_api_access(request)
    mail_login = re.sub(r"[^a-z0-9.\-]", "", body.mail_login.lower())
    if not mail_login or len(mail_login) < 2:
        raise HTTPException(status_code=400, detail="Invalid mail_login")
    email = f"{mail_login}@{CFG.MAIL_DOMAIN}"
    _reject_generic_managed_address_operation(
        email,
        "Создание почтового аккаунта",
    )
    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/create_account",
            {
                "mail_login": mail_login,
                "emp_id": body.emp_id,
                "password": body.password,
            },
            45,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"mail control-plane error: {exc}",
        ) from exc
    return {
        "ok": True,
        "mail_login": data.get("mail_login") or mail_login,
        "email": data.get("email") or email,
        "created": bool(data.get("created")),
        "password": data.get("password") or "",
        "status": data.get("status") or "",
    }


@app.post("/api/admin/ticket_intake")
async def api_admin_ticket_intake(body: AdminTicketIntakeBody, request: Request):
    """Register mailbox credentials used only for mail-to-ticket intake."""
    _require_mail_admin_api_access(request)
    local, email_address = normalize_email_login(body.email)
    if not local or not body.password:
        raise HTTPException(status_code=400, detail="valid mailbox credentials are required")
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: _imap_connect(email_address, body.password).logout(),
        )
        await loop.run_in_executor(
            None,
            lambda: _ticket_intake_put(email_address, body.password),
        )
    except Exception as exc:
        log.warning("ticket intake registration rejected for %s: %s", email_address, exc)
        raise _imap_validation_http_error(exc) from exc
    return {"ok": True, "email": email_address}


@app.get("/api/admin/ticket_intake")
async def api_admin_ticket_intake_status(email: str, request: Request):
    _require_mail_admin_api_access(request)
    _, email_address = normalize_email_login(email)
    with _db_connection() as con:
        row = con.execute(
            "SELECT 1 FROM ticket_intake_mailboxes WHERE email = ?",
            (email_address,),
        ).fetchone()
    return {"ok": True, "email": email_address, "active": bool(row)}


@app.post("/api/admin/delete_account")
async def api_admin_delete_account(body: AdminDeleteBody, request: Request):
    """Delete a personal mailbox and its portal-owned runtime state."""
    _require_mail_admin_api_access(request)
    mail_login = re.sub(r"[^a-z0-9.\-]", "", body.mail_login.lower())
    if not mail_login or len(mail_login) < 2:
        raise HTTPException(status_code=400, detail="Invalid mail_login")
    email_address = f"{mail_login}@{CFG.MAIL_DOMAIN}"
    _reject_generic_managed_address_operation(email_address, "Удаление почтового аккаунта")
    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/delete_account",
            {"mail_login": mail_login},
            60,
        )
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: _ticket_intake_drop(email_address),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mail control-plane error: {exc}") from exc
    return {"ok": True, "email": email_address, "status": data.get("status") or "removed"}


@app.post("/api/admin/sso_binding")
async def api_admin_sso_binding(body: AdminSsoBindingBody, request: Request):
    """Bind a central-auth subject to a local mailbox, or drop the binding.

    Called by the portal when the user configures (or disconnects) mail in the
    personal cabinet. An empty password means "remove", which is what keeps the
    binding revocable from the same place it was created (ADR-0041).
    """
    _require_mail_admin_api_access(request)
    subject = str(body.subject or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")
    loop = asyncio.get_event_loop()
    if not str(body.password or ""):
        try:
            removed = await loop.run_in_executor(
                None,
                lambda: _sso_binding_drop_requested(
                    subject,
                    imap_host=body.imap_host,
                    provider=body.provider,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "bound": False, "removed": bool(removed)}
    # A BYOM binding (ADR-0071) carries its own IMAP endpoint, so the mailbox
    # may be on a foreign domain. A managed @example.com binding keeps the
    # local-part/domain validation that the old behaviour enforced.
    provider = str(body.provider or "").strip().lower() or None
    try:
        imap_host, imap_port = _resolve_provider_hosts(
            provider, body.imap_host, body.imap_port
        )
        smtp_host, smtp_port = _resolve_provider_smtp(
            provider, body.smtp_host, body.smtp_port
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if imap_host:
        email = str(body.email or "").strip().lower()
        if not email or "@" not in email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(status_code=400, detail="email must be a valid address")
    else:
        local, email = normalize_email_login(str(body.email or ""))
        if not local or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", local):
            raise HTTPException(status_code=400, detail="email must belong to the mail domain")
    # Prove the credentials work before storing them: a binding that cannot log
    # in would turn a working password login into a broken SSO login.
    try:
        await loop.run_in_executor(
            None,
            lambda: _imap_connect(
                email, str(body.password), host=imap_host, port=imap_port
            ).logout(),
        )
    except Exception as exc:
        log.warning("sso binding rejected for %s: %s", email, exc)
        raise _imap_validation_http_error(exc) from exc
    try:
        await loop.run_in_executor(
            None,
            lambda: _sso_binding_put(
                subject,
                email,
                str(body.password),
                imap_host=imap_host,
                imap_port=imap_port,
                provider=provider,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "bound": True, "email": email}


@app.post("/api/admin/sso_binding/bulk")
async def api_admin_sso_binding_bulk(
    body: AdminSsoBindingsBody,
    request: Request,
):
    """Atomically replace or remove one external binding for all account subjects."""
    _require_mail_admin_api_access(request)
    subjects = list(dict.fromkeys(
        str(subject or "").strip() for subject in body.subjects if str(subject or "").strip()
    ))[:16]
    if not subjects:
        raise HTTPException(status_code=400, detail="subjects are required")
    provider = str(body.provider or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", provider):
        raise HTTPException(status_code=400, detail="provider is required")
    loop = asyncio.get_event_loop()
    if not str(body.password or ""):
        removed = await loop.run_in_executor(
            None,
            lambda: _sso_bindings_drop_external(subjects, provider),
        )
        binding = await loop.run_in_executor(None, lambda: _sso_bindings_status(subjects))
        return {"ok": True, "removed": removed, **binding}
    email = str(body.email or "").strip().lower()
    try:
        imap_host, imap_port = _resolve_provider_hosts(
            provider, body.imap_host, body.imap_port
        )
        smtp_host, smtp_port = _resolve_provider_smtp(
            provider, body.smtp_host, body.smtp_port
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not imap_host or not imap_port or not 1 <= imap_port <= 65535:
        raise HTTPException(status_code=400, detail="external IMAP endpoint is required")
    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="email must be a valid address")
    try:
        await loop.run_in_executor(
            None,
            lambda: _imap_connect(
                email,
                str(body.password),
                host=imap_host,
                port=imap_port,
            ).logout(),
        )
    except Exception as exc:
        log.warning("bulk sso binding rejected for %s: %s", email, exc)
        raise _imap_validation_http_error(exc) from exc
    updated = await loop.run_in_executor(
        None,
        lambda: _sso_bindings_put(
            subjects,
            email,
            str(body.password),
            imap_host=imap_host,
            imap_port=imap_port,
            provider=provider,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
        ),
    )
    return {
        "ok": True,
        "updated": updated,
        "bound": True,
        "external": True,
        "provider": provider,
        "email": email,
        "conflict": False,
    }


@app.get("/api/admin/sso_binding/providers")
async def api_admin_sso_binding_providers(request: Request):
    """Return external mailbox provider presets for the binding UI."""
    _require_mail_admin_api_access(request)
    return {
        "ok": True,
        "providers": [
            {"id": key, **value}
            for key, value in MAIL_PROVIDER_PRESETS.items()
        ],
    }


class ExternalMailboxBody(BaseModel):
    """A signed-in user connecting their own external mailbox."""

    provider: str = ""
    email: str = ""
    password: str = ""
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None


def _external_mailbox_subject(request: Request) -> Tuple[Dict[str, Any], str]:
    """Return the session and its login subject, or refuse.

    The binding is keyed by the central-auth subject, so a mailbox connected
    with a password login would have nothing to attach to.
    """
    sess = get_session_or_401(request)
    subject = _mail_sso_subject(sess.get("sso_subject"))
    if not subject:
        raise HTTPException(
            status_code=409,
            detail="подключение внешнего ящика доступно после входа через SSO",
        )
    return sess, subject


@app.get("/api/mailbox/external")
async def api_external_mailbox_state(request: Request):
    """Return the caller's own external mailbox state and the provider presets."""
    sess = get_session_or_401(request)
    subject = _mail_sso_subject(sess.get("sso_subject"))
    loop = asyncio.get_event_loop()
    state = (
        await loop.run_in_executor(None, lambda: _sso_bindings_status([subject]))
        if subject
        else {"bound": False, "external": False, "provider": "", "email": "", "conflict": False}
    )
    return JSONResponse(
        {
            "ok": True,
            "available": bool(subject),
            **state,
            "providers": [
                {
                    "id": key,
                    "name": value["name"],
                    "imap_host": value["imap_host"],
                    "imap_port": value["imap_port"],
                    "smtp_host": value["smtp_host"],
                    "smtp_port": value["smtp_port"],
                }
                for key, value in MAIL_PROVIDER_PRESETS.items()
            ],
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/mailbox/external")
async def api_external_mailbox_connect(body: ExternalMailboxBody, request: Request):
    """Connect an external mailbox to the caller's own login."""
    _sess, subject = _external_mailbox_subject(request)
    provider = str(body.provider or "").strip().lower()
    if not provider or provider not in MAIL_PROVIDER_PRESETS:
        raise HTTPException(status_code=400, detail="неизвестный почтовый сервис")
    email = str(body.email or "").strip().lower()
    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="укажите адрес ящика целиком")
    if not str(body.password or ""):
        raise HTTPException(status_code=400, detail="укажите пароль приложения")
    try:
        imap_host, imap_port = _resolve_provider_hosts(
            provider, body.imap_host, body.imap_port
        )
        smtp_host, smtp_port = _resolve_provider_smtp(
            provider, body.smtp_host, body.smtp_port
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    loop = asyncio.get_event_loop()
    # Prove the credentials work before storing them, exactly as the admin path
    # does: a binding that cannot log in is worse than no binding at all.
    try:
        await loop.run_in_executor(
            None,
            lambda: _imap_connect(
                email, str(body.password), host=imap_host, port=imap_port
            ).logout(),
        )
    except Exception as exc:
        log.warning("external mailbox rejected for %s: %s", email, exc)
        raise _imap_validation_http_error(exc) from exc
    try:
        await loop.run_in_executor(
            None,
            lambda: _sso_binding_put(
                subject,
                email,
                str(body.password),
                imap_host=imap_host,
                imap_port=imap_port,
                provider=provider,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "bound": True, "external": True, "provider": provider, "email": email}


@app.delete("/api/mailbox/external")
async def api_external_mailbox_disconnect(request: Request):
    """Disconnect the caller's external mailbox, keeping the managed one."""
    _sess, subject = _external_mailbox_subject(request)
    loop = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, lambda: _sso_bindings_status([subject]))
    provider = str(state.get("provider") or "").strip().lower()
    if not provider:
        return {"ok": True, "bound": False, "removed": False}
    # The drop stays provider-scoped: disconnecting an external mailbox must
    # leave the managed binding alone (ADR-0072).
    removed = await loop.run_in_executor(
        None, lambda: _sso_bindings_drop_external([subject], provider)
    )
    return {"ok": True, "bound": False, "removed": bool(removed)}


@app.post("/api/admin/sso_binding/status")
async def api_admin_sso_binding_status(
    body: AdminSsoBindingStatusBody,
    request: Request,
):
    """Return one safe aggregate state for every supplied login subject."""
    _require_mail_admin_api_access(request)
    subjects = list(dict.fromkeys(
        str(subject or "").strip() for subject in body.subjects if str(subject or "").strip()
    ))[:16]
    loop = asyncio.get_event_loop()
    binding = await loop.run_in_executor(None, lambda: _sso_bindings_status(subjects))
    return {"ok": True, **binding}


@app.get("/api/admin/list_accounts")
async def api_admin_list_accounts(request: Request):
    """List all mail accounts."""
    _require_mail_admin_api_access(request)
    if not _mail_admin_available():
        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, _mail_control_request, "/api/mail/list_accounts", {}, _MAIL_CONTROL_LIST_TIMEOUT
            )
            accounts = data.get("accounts") or []
            payload = {
                "ok": True,
                "output": "\n".join(accounts),
                "accounts": accounts,
                "count": len(accounts),
                "source": data.get("source") or "soc-control-plane",
            }
            if data.get("warning"):
                payload["warning"] = data.get("warning")
            return payload
        except Exception as e:
            accounts = await asyncio.get_event_loop().run_in_executor(None, _directory_mail_accounts_from_panel)
            if accounts:
                return {
                    "ok": True,
                    "output": "\n".join(accounts),
                    "accounts": accounts,
                    "count": len(accounts),
                    "source": "directory_fallback",
                    "warning": f"mail control-plane list_accounts failed: {e}",
                }
            raise HTTPException(status_code=502, detail=f"mail control-plane error: {e}")
    ok, accounts, status = await asyncio.get_event_loop().run_in_executor(
        None, _collect_mail_accounts_result
    )
    output = "\n".join(accounts) if status == "host_config" else status
    return {
        "ok": ok,
        "output": output,
        "accounts": accounts,
        "count": len(accounts),
        "source": "local_mailserver_config" if status == "host_config" else "local_mailserver",
    }


class AdminResetPasswordBody(BaseModel):
    mail_login: str
    emp_id: int | None = None
    password: str = ""  # if empty — generate random


@app.post("/api/admin/reset_password")
async def api_admin_reset_password(body: AdminResetPasswordBody, request: Request):
    """Reset a mailbox password. Returns the new password so the bot can show it
    to the user (e.g. for Delta Chat QR code generation)."""
    _require_mail_admin_api_access(request)
    mail_login = re.sub(r"[^a-z0-9.\-]", "", body.mail_login.lower())
    if not mail_login or len(mail_login) < 2:
        raise HTTPException(status_code=400, detail="Invalid mail_login")
    email = f"{mail_login}@{CFG.MAIL_DOMAIN}"
    _reject_generic_managed_address_operation(
        email,
        "Сброс пароля почтового аккаунта",
    )
    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/reset_password",
            {
                "mail_login": mail_login,
                "emp_id": body.emp_id,
                "password": body.password,
            },
            60,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"mail control-plane error: {exc}",
        ) from exc
    return {
        "ok": True,
        "mail_login": data.get("mail_login") or mail_login,
        "email": data.get("email") or email,
        "password": data.get("password") or "",
        "status": data.get("status") or "",
    }


# ---------------------------------------------------------------------------
# Admin Portal — LDAP-authenticated IT-staff interface
# ---------------------------------------------------------------------------

ADMIN_PORTAL_COOKIE = "jmAdminSID"
ADMIN_PORTAL_TTL = 4 * 3600
ADMIN_LDAP_GROUPS = DEFAULT_ADMIN_LDAP_GROUPS

_admin_sessions: Dict[str, Dict[str, Any]] = {}


def _admin_session_get(token: str) -> Optional[Dict[str, Any]]:
    s = _admin_sessions.get(token)
    if s and time.time() - s["ts"] < ADMIN_PORTAL_TTL:
        return s
    _admin_sessions.pop(token, None)
    return None


def _client_ip(request: Request) -> str:
    """Real client IP as seen by the trusted reverse proxy."""
    return authentication.get_client_ip(request)


def _request_is_https(request: Request) -> bool:
    """Check if request was made over HTTPS."""
    return authentication.is_request_https(request)


def _request_host(request: Request) -> str:
    """Extract Host from request headers, handling proxies."""
    return authentication.get_request_host(request)


def _is_internal_client_ip(client_ip: str) -> bool:
    """Check if IP is in the internal/private range."""
    return validation.is_internal_client_ip(client_ip)


def _ct_eq(provided: str, expected: str) -> bool:
    """Constant-time compare that tolerates non-ASCII header bytes.

    secrets.compare_digest raises TypeError on non-ASCII *str* inputs, and HTTP
    header values reach us latin-1-decoded, so a hostile X-Admin-Key /
    X-Internal-Token would otherwise turn a rejected request into a 500.
    Comparing the UTF-8 byte encodings stays constant-time and crash-free.
    """
    return secrets.compare_digest(
        provided.encode("utf-8", "surrogatepass"),
        expected.encode("utf-8", "surrogatepass"),
    )


def _has_valid_internal_token(request: Request) -> bool:
    configured = str(CFG.INTERNAL_TOKEN or "").strip()
    provided = str(request.headers.get("x-internal-token") or "").strip()
    return bool(configured and provided and _ct_eq(provided, configured))


def _require_internal_service_access(request: Request) -> None:
    if not _has_valid_internal_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    if not _is_internal_client_ip(_client_ip(request)):
        raise HTTPException(status_code=403, detail="Forbidden")


def _require_mail_admin_api_access(request: Request) -> None:
    key = str(request.headers.get("x-admin-key") or "")
    if not _ct_eq(key, str(CFG.ADMIN_KEY or "")):
        raise HTTPException(status_code=403, detail="Forbidden")
    if _has_valid_internal_token(request):
        return
    if not _is_internal_client_ip(_client_ip(request)):
        raise HTTPException(status_code=403, detail="Forbidden")


def _is_same_origin_admin_request(request: Request) -> bool:
    """Check if request is same-origin (with fallback to internal IP check)."""
    expected_host = authentication.get_request_host(request)
    if not expected_host:
        return _is_internal_client_ip(authentication.get_client_ip(request))
    expected_origin = authentication.build_expected_origin(request)
    origin = authentication.get_request_origin(request)
    if origin:
        return origin == expected_origin
    referer = authentication.get_request_referer(request)
    if referer:
        return referer == expected_origin or referer.startswith(expected_origin + "/")
    # Keep internal ops/curl flows working even when browsers omit origin headers.
    return _is_internal_client_ip(authentication.get_client_ip(request))


# ---------------------------------------------------------------------------
# Rate limiting (per-IP failed-auth tracking)
# ---------------------------------------------------------------------------
# Simple in-process bucket. Tracks failed auth attempts per IP per bucket name.
# Default: 5 failures per 5 minutes = lockout for the rest of the window.

_RATE_BUCKETS: Dict[str, Dict[str, List[float]]] = {}
_rate_lock = threading.Lock()
RATE_WINDOW = int(os.environ.get("WEBMAIL_RATE_WINDOW", "300"))  # 5 minutes
RATE_MAX_FAILS = int(os.environ.get("WEBMAIL_RATE_MAX_FAILS", "5"))
TELEGRAM_ISSUE_WINDOW = int(os.environ.get("WEBMAIL_TELEGRAM_ISSUE_WINDOW", "300"))
TELEGRAM_ISSUE_MAX = int(os.environ.get("WEBMAIL_TELEGRAM_ISSUE_MAX", "10"))


def rate_check(bucket: str, ip: str) -> bool:
    """Return True if the IP is currently allowed (under the failure limit)."""
    if not ip:
        return True
    max_fails = RATE_MAX_FAILS
    if bucket == "webmail_login":
        max_fails = int(os.environ.get("WEBMAIL_LOGIN_RATE_MAX_FAILS", str(RATE_MAX_FAILS)))
    now = time.time()
    cutoff = now - RATE_WINDOW
    with _rate_lock:
        b = _RATE_BUCKETS.setdefault(bucket, {})
        fails = [t for t in b.get(ip, []) if t > cutoff]
        b[ip] = fails
        return len(fails) < max_fails


def rate_record_fail(bucket: str, ip: str) -> None:
    """Record a failed attempt for this IP/bucket."""
    if not ip:
        return
    now = time.time()
    cutoff = now - RATE_WINDOW
    with _rate_lock:
        b = _RATE_BUCKETS.setdefault(bucket, {})
        fails = [t for t in b.get(ip, []) if t > cutoff]
        fails.append(now)
        b[ip] = fails


def rate_reset(bucket: str, ip: str) -> None:
    """Clear failures for this IP after a successful attempt."""
    if not ip:
        return
    with _rate_lock:
        b = _RATE_BUCKETS.get(bucket)
        if b and ip in b:
            b.pop(ip, None)


def telegram_issue_allowed(ip: str) -> bool:
    """Bound Telegram challenge issuance without treating it as an auth failure."""
    if not ip:
        return True
    now = time.time()
    cutoff = now - max(1, TELEGRAM_ISSUE_WINDOW)
    with _rate_lock:
        bucket = _RATE_BUCKETS.setdefault("webmail_telegram_issue", {})
        requests = [at for at in bucket.get(ip, []) if at > cutoff]
        if len(requests) >= max(1, TELEGRAM_ISSUE_MAX):
            bucket[ip] = requests
            return False
        requests.append(now)
        bucket[ip] = requests
        return True


def _ldap_check_user(username: str, password: str) -> bool:
    """Bind as user only — no group-membership gate. Used by regular
    webmail AD login (added 2026-07-25): unlike the admin panel, any valid
    AD account should be able to open its own mailbox, not just IT admins.
    Mirrors _ldap_check_admin's bind step, direct against AD — this
    deliberately bypasses the portal's /api/internal/mail-auth/ad broker,
    which requires the caller to be on the portal's own loopback and can
    never be satisfied from this host (see mail-example memory)."""
    if not _ldap_bind_available():
        return False
    plain = username.split("@")[0]
    user_dn = f"{plain}@corp.local"
    for srv_url in CFG.LDAP_SERVERS:
        try:
            use_ssl = srv_url.startswith("ldaps://")
            host_part = srv_url.split("://", 1)[1]
            host, _, port = host_part.partition(":")
            srv = Server(host, port=int(port) if port else (636 if use_ssl else 389),
                         use_ssl=use_ssl, get_info=ALL)
            user_conn = Connection(srv, user=user_dn, password=password,
                                   authentication=SIMPLE, auto_bind=True, receive_timeout=8)
            user_conn.unbind()
            return True
        except LDAPException as e:
            log.warning("Webmail AD LDAP %s: %s", srv_url, e)
            continue
        except Exception as e:
            log.warning("Webmail AD LDAP unexpected %s: %s", srv_url, e)
            continue
    return False


def _ldap_check_admin(username: str, password: str) -> bool:
    """Bind as user, then verify membership in IT LDAP groups."""
    if not _ldap_bind_available():
        return False
    plain = username.split("@")[0]
    safe_plain = re.sub(r"[()\\*\x00]", "", plain)
    user_dn = f"{plain}@corp.local"
    for srv_url in CFG.LDAP_SERVERS:
        try:
            use_ssl = srv_url.startswith("ldaps://")
            host_part = srv_url.split("://", 1)[1]
            host, _, port = host_part.partition(":")
            srv = Server(host, port=int(port) if port else (636 if use_ssl else 389),
                         use_ssl=use_ssl, get_info=ALL)
            # Step 1: bind as user (auth check)
            user_conn = Connection(srv, user=user_dn, password=password,
                                   authentication=SIMPLE, auto_bind=True, receive_timeout=8)
            # Step 2: service account for group lookup
            svc_conn = Connection(srv, user=CFG.LDAP_BIND_USER, password=CFG.LDAP_BIND_PASS,
                                  authentication=SIMPLE, auto_bind=True, receive_timeout=8)
            svc_conn.search(
                search_base=CFG.LDAP_BASE_DN,
                search_filter=f"(&(objectClass=user)(sAMAccountName={safe_plain}))",
                search_scope=SUBTREE,
                attributes=["memberOf"],
                size_limit=1,
            )
            if not svc_conn.entries:
                user_conn.unbind()
                svc_conn.unbind()
                return False
            member_of = [str(g) for g in svc_conn.entries[0].memberOf] \
                if "memberOf" in svc_conn.entries[0] else []
            is_member = _admin_group_claims_allowed(member_of)
            user_conn.unbind()
            svc_conn.unbind()
            return is_member
        except LDAPException as e:
            log.warning("Admin LDAP %s: %s", srv_url, e)
            continue
        except Exception as e:
            log.warning("Admin LDAP unexpected %s: %s", srv_url, e)
            continue
    return False


def _require_admin(request: Request) -> Dict[str, Any]:
    token = request.cookies.get(ADMIN_PORTAL_COOKIE, "")
    sess = _admin_session_get(token) if token else None
    if not sess:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"} and not _is_same_origin_admin_request(request):
        raise HTTPException(status_code=403, detail="Forbidden")
    return sess


class AdminPortalLoginBody(BaseModel):
    username: str
    password: str


class AdminPortalCreateBody(BaseModel):
    email: str
    password: str = ""


class AdminPortalEmailBody(BaseModel):
    email: str


class AdminPortalPasswordBody(BaseModel):
    email: str
    password: str = ""


class AdminPortalProvisionBody(BaseModel):
    emp_id: Optional[int] = None
    mail_login: Optional[str] = ""
    email: Optional[str] = ""
    password: str = ""


class AdminPortalMailboxLifecycleBody(BaseModel):
    email: str
    reason: str = ""


@app.post("/api/adminportal/login")
async def adminportal_login(body: AdminPortalLoginBody, request: Request, response: Response):
    ip = _client_ip(request)
    if not rate_check("admin_login", ip):
        raise HTTPException(429, "Слишком много неудачных попыток. Попробуйте через несколько минут.")
    username = body.username.strip()
    if not username or not body.password:
        raise HTTPException(400, "Username and password required")
    ok = await asyncio.get_event_loop().run_in_executor(
        None, _ldap_check_admin, username, body.password
    )
    plain_user = username.split("@")[0]
    if not ok:
        rate_record_fail("admin_login", ip)
        log.warning("Admin portal auth failed: %s", username)
        db_audit_log(plain_user, "auth.login", "", {"error": "auth_failed"}, ip, ok=False)
        raise HTTPException(401, "Неверные учётные данные или нет прав доступа")
    rate_reset("admin_login", ip)
    token = secrets.token_hex(32)
    admin_session = {
        "username": plain_user,
        "ts": time.time(),
        "profile_card": None,
    }
    _admin_sessions[token] = admin_session
    _schedule_portal_profile_refresh(
        admin_session,
        username_key="username",
        mail_snapshot=False,
    )
    response.set_cookie(
        ADMIN_PORTAL_COOKIE,
        token,
        max_age=ADMIN_PORTAL_TTL,
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request),
        path="/",
    )
    log.info("Admin portal login: %s", username)
    db_audit_log(plain_user, "auth.login", "", None, ip, ok=True)
    return {
        "ok": True,
        "username": plain_user,
        "profile_card": None,
    }


@app.post("/api/adminportal/logout")
async def adminportal_logout(request: Request, response: Response):
    _require_admin(request)
    token = request.cookies.get(ADMIN_PORTAL_COOKIE, "")
    sess = _admin_sessions.pop(token, None)
    response.delete_cookie(ADMIN_PORTAL_COOKIE)
    if sess:
        db_audit_log(sess.get("username", "?"), "auth.logout", "", None, _client_ip(request))
    return {"ok": True}


@app.get("/api/adminportal/me")
async def adminportal_me(request: Request):
    sess = _require_admin(request)
    await _refresh_portal_profile_card(
        sess,
        username_key="username",
        mail_snapshot=False,
    )
    return {
        "ok": True,
        "username": sess["username"],
        "profile_card": sess.get("profile_card"),
    }


@app.get("/api/adminportal/auth/status")
async def adminportal_auth_status(request: Request):
    token = request.cookies.get(ADMIN_PORTAL_COOKIE)
    sess = _admin_session_get(token) if token else None
    return {"authenticated": bool(sess)}


@app.get("/api/adminportal/support/search")
async def adminportal_support_search(
    request: Request,
    q: str = Query(""),
    limit: int = Query(25, ge=1, le=100),
):
    _require_admin(request)
    payload = await asyncio.get_event_loop().run_in_executor(None, _support_search, q, limit)
    return {"ok": True, **payload}


@app.get("/api/adminportal/onboarding/check")
async def adminportal_onboarding_check(
    request: Request,
    mail_login: str = Query(...),
):
    _require_admin(request)
    check = await asyncio.get_event_loop().run_in_executor(None, _onboarding_check, mail_login)
    return {"ok": True, "check": check}


@app.post("/api/adminportal/onboarding/provision")
async def adminportal_onboarding_provision(body: AdminPortalProvisionBody, request: Request):
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    loop = asyncio.get_event_loop()

    emp = None
    if body.emp_id:
        emp = await loop.run_in_executor(None, _panel_find_employee_by_id, int(body.emp_id), True)
        if not emp:
            raise HTTPException(404, "Сотрудник не найден")

    raw_mail_login = body.mail_login or ""
    raw_email = (body.email or "").strip().lower()
    if raw_email and "@" in raw_email and not raw_mail_login:
        raw_mail_login = raw_email.split("@", 1)[0]
    suggested_mail_login = _sanitize_mail_login(raw_mail_login or (emp.get("mail_login") if emp else "") or (emp.get("ad_login") if emp else ""))
    if not suggested_mail_login:
        raise HTTPException(400, "Нужен mail login")
    if not re.match(r"^[a-z0-9][a-z0-9.\-]{1,28}[a-z0-9]$", suggested_mail_login):
        raise HTTPException(400, "Invalid mail_login format (3-30 chars, a-z 0-9 . -)")
    if not emp:
        emp = await loop.run_in_executor(
            None,
            _panel_find_employee_by_exact_mail_login,
            suggested_mail_login,
            True,
        )
    if not emp or not isinstance(emp.get("id"), int):
        raise HTTPException(
            409,
            "Для создания почтового аккаунта нужен точный сотрудник",
        )

    email_addr = _mail_login_to_email(suggested_mail_login)
    _reject_generic_managed_address_operation(
        email_addr,
        "Создание почтового аккаунта",
    )
    check = await loop.run_in_executor(None, _onboarding_check, suggested_mail_login)
    owner = check.get("owner") or {}
    owner_ad = str(owner.get("ad_login") or "").strip().lower()
    employee_ad = str(emp.get("ad_login") or "").strip().lower()
    employee_mail_login = str(emp.get("mail_login") or "").strip().lower()
    recovering_bound_mailbox = bool(
        check.get("mailbox_exists")
        and check.get("alias_exists")
        and owner_ad
        and owner_ad == employee_ad
        and employee_mail_login == suggested_mail_login
    )
    if check.get("mailbox_exists") and not recovering_bound_mailbox:
        db_audit_log(admin_user, "account.provision", email_addr, {"error": "already_exists"}, ip, ok=False)
        raise HTTPException(409, "Почтовый ящик уже существует")
    if check.get("alias_exists") and (not emp or owner_ad != str(emp.get("ad_login") or "").lower()):
        db_audit_log(admin_user, "account.provision", email_addr, {"error": "alias_conflict"}, ip, ok=False)
        raise HTTPException(409, "Этот mail login уже связан с другим сотрудником")

    password = body.password or _generate_password()
    binding = None
    ad_login = ""
    should_create_alias = False
    if emp:
        ad_login = str(emp.get("ad_login") or "").strip().lower()
        if not ad_login:
            db_audit_log(admin_user, "account.provision", email_addr, {"error": "missing_ad_login"}, ip, ok=False)
            raise HTTPException(409, "У сотрудника нет AD login для привязки")
        should_create_alias = not bool(check.get("alias_exists"))

    if should_create_alias:
        try:
            binding = await loop.run_in_executor(
                None,
                lambda: db_set_alias(
                    ad_login,
                    suggested_mail_login,
                    emp_id=int(emp["id"]),
                ),
            )
        except ValueError as e:
            db_audit_log(
                admin_user,
                "account.provision",
                email_addr,
                {"error": str(e)},
                ip,
                ok=False,
            )
            raise HTTPException(409, str(e))
    elif ad_login:
        binding = await loop.run_in_executor(None, db_get_alias, ad_login)
    try:
        data = await loop.run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/create_account",
            {
                "mail_login": suggested_mail_login,
                "emp_id": int(emp["id"]),
                "password": password,
            },
            60,
        )
    except Exception as e:
        if (
            should_create_alias
            and ad_login
            and not _mail_control_error_is_uncertain(e)
        ):
            await loop.run_in_executor(None, db_delete_alias, ad_login)
        text = str(e)
        db_audit_log(
            admin_user,
            "account.provision",
            email_addr,
            {"error": text},
            ip,
            ok=False,
        )
        if "already exists" in text.lower():
            raise HTTPException(409, "Почтовый ящик уже существует")
        raise HTTPException(502, f"mail control-plane error: {e}")
    confirmed_password = str(data.get("password") or "")
    if not confirmed_password:
        status = str(data.get("status") or "").strip().lower()
        db_audit_log(
            admin_user,
            "account.provision",
            email_addr,
            {"error": "password_not_confirmed", "status": status},
            ip,
            ok=False,
        )
        if status == "already exists":
            raise HTTPException(409, "Почтовый ящик уже существует")
        raise HTTPException(
            502,
            "mail control-plane did not confirm the mailbox password",
        )
    password = confirmed_password

    display_name = _employee_display_name(emp) if emp else email_addr
    credential_pack = _build_credential_pack(email_addr, password, display_name)
    audit_details = {
        "emp_id": emp.get("id") if emp else None,
        "mail_login": suggested_mail_login,
        "bound": bool(binding),
    }
    db_audit_log(admin_user, "account.provision", email_addr, audit_details, ip, ok=True)
    return {
        "ok": True,
        "email": email_addr,
        "mail_login": suggested_mail_login,
        "created": True,
        "binding_applied": bool(binding),
        "binding": binding,
        "employee": {
            "id": emp.get("id"),
            "display_name": _employee_display_name(emp),
            "ad_login": emp.get("ad_login"),
            "department": emp.get("department"),
        } if emp else None,
        "password": password,
        "credential_pack": credential_pack,
    }


def _build_account_detail(target_email: str) -> Dict[str, Any]:
    local = target_email.split("@", 1)[0]
    queue_future = _mailadmin_read_executor.submit(_postfix_queue_list)
    stats_future = _mailadmin_read_executor.submit(_mailserver_stats)
    employee = None
    employees = _fetch_employees_cached()
    owner = next(
        (
            row
            for row in employees
            if str(row.get("mail_login") or "").lower() == local
        ),
        None,
    )
    if owner is None:
        owner = next(
            (
                row
                for row in employees
                if str(row.get("ad_login") or "").lower() == local
            ),
            None,
    )
    if owner is not None:
        row = owner
        employee = {
            "id": row.get("id"),
            "name": row.get("display_name") or row.get("name") or "",
            "ad_login": row.get("ad_login"),
            "mail_login": row.get("mail_login"),
            "ad_domain": row.get("ad_domain"),
            "company": row.get("company"),
            "department": row.get("department"),
            "title": row.get("job_title") or row.get("title"),
            "created_at": row.get("created_at"),
        }
    alias = db_get_alias(employee["ad_login"] if employee and employee.get("ad_login") else local)
    queue_snapshot = queue_future.result()
    queue_entries = [
        entry for entry in queue_snapshot
        if (entry.get("sender") or "").lower() == target_email
        or target_email in [str(rcpt).lower() for rcpt in entry.get("recipients") or []]
    ]
    queue_summary = {
        "total": len(queue_entries),
        "active": sum(1 for entry in queue_entries if entry.get("active")),
        "deferred": sum(1 for entry in queue_entries if entry.get("deferred") and not entry.get("active") and not entry.get("hold")),
        "hold": sum(1 for entry in queue_entries if entry.get("hold")),
    }
    mailbox_info = next(
        (m for m in stats_future.result().get("mailboxes", []) if (m.get("email") or "").lower() == target_email),
        None,
    )
    recent_audit = db_audit_query_mailbox(target_email, hours=72, limit=10)
    lifecycle = db_get_mailbox_lifecycle(target_email) or {}
    managed_address = _managed_address_find_by_email(target_email)
    identity_ad_login = str(
        (employee or {}).get("ad_login")
        or (alias or {}).get("ad_login")
        or local
    ).strip().lower()
    identity_ad_domain = str(
        (employee or {}).get("ad_domain")
        or _directory_domain_for_company((employee or {}).get("company"))
        or ""
    ).strip().lower()
    shared_access = []
    if not managed_address:
        shared_access = [
            _shared_access_public_item(row)
            for row in _shared_access_catalog_for_identity(
                target_email,
                identity_ad_login,
                identity_ad_domain,
            )
        ]
    incident_summary = {
        "total": 0,
        "queue_total": queue_summary["total"],
        "auth_failures": 0,
        "delivery_failures": int(queue_summary["deferred"]) + int(queue_summary["hold"]),
        "lifecycle_events": 1 if lifecycle.get("state") == "suspended" else 0,
        "latest_issue": "Mailbox suspended" if lifecycle.get("state") == "suspended" else "",
    }
    return {
        "email": target_email,
        "mail_login": local,
        "employee": employee,
        "alias": alias,
        "managed_address": managed_address,
        "shared_access": shared_access,
        "mailbox": mailbox_info,
        "queue_summary": queue_summary,
        "queue_entries": queue_entries,
        "recent_audit": recent_audit,
        "lifecycle_state": lifecycle.get("state", "active"),
        "suspended_at": lifecycle.get("suspended_at"),
        "suspended_by": lifecycle.get("suspended_by"),
        "suspension_reason": lifecycle.get("suspend_reason"),
        "restored_at": lifecycle.get("restored_at"),
        "restored_by": lifecycle.get("restored_by"),
        "incident_summary": incident_summary,
    }


def _build_mailbox_delivery_doctor(target_email: str) -> Dict[str, Any]:
    mailbox = str(target_email or "").strip().lower()
    if not mailbox:
        return {
            "pending": 0,
            "sending": 0,
            "failed": 0,
            "sent": 0,
            "cancelled": 0,
            "recent_sent_24h": 0,
            "status": "idle",
            "latest": None,
        }
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        count_rows = con.execute(
            "SELECT status, COUNT(*) AS c "
            "FROM scheduled_sends WHERE lower(imap_user) = ? GROUP BY status",
            (mailbox,),
        ).fetchall()
        counts = {str(row["status"] or ""): int(row["c"] or 0) for row in count_rows}
        latest = con.execute(
            "SELECT id, status, attempts, last_error, created_at, scheduled_at, sent_at, origin, subject, to_list "
            "FROM scheduled_sends WHERE lower(imap_user) = ? "
            "ORDER BY CASE status "
            "         WHEN 'failed' THEN 0 "
            "         WHEN 'pending' THEN 1 "
            "         WHEN 'sending' THEN 2 "
            "         WHEN 'sent' THEN 3 "
            "         ELSE 4 END, "
            "COALESCE(sent_at, scheduled_at, created_at) DESC LIMIT 1",
            (mailbox,),
        ).fetchone()
        recent_sent_24h = int(
            con.execute(
                "SELECT COUNT(*) FROM scheduled_sends "
                "WHERE lower(imap_user) = ? AND status = 'sent' "
                "AND datetime(COALESCE(sent_at, created_at)) >= datetime('now', '-1 day')",
                (mailbox,),
            ).fetchone()[0]
            or 0
        )
    failed = int(counts.get("failed", 0))
    pending = int(counts.get("pending", 0))
    sending = int(counts.get("sending", 0))
    sent = int(counts.get("sent", 0))
    cancelled = int(counts.get("cancelled", 0))
    if failed:
        status = "failed"
    elif pending or sending:
        status = "queued"
    elif recent_sent_24h:
        status = "recent_sent"
    else:
        status = "idle"
    latest_payload = None
    if latest:
        latest_payload = {
            "id": latest["id"],
            "status": latest["status"] or "",
            "attempts": int(latest["attempts"] or 0),
            "last_error": latest["last_error"] or "",
            "created_at": latest["created_at"] or "",
            "scheduled_at": latest["scheduled_at"] or "",
            "sent_at": latest["sent_at"] or "",
            "origin": latest["origin"] or "",
            "subject": latest["subject"] or "",
            "to_list": latest["to_list"] or "",
        }
    return {
        "pending": pending,
        "sending": sending,
        "failed": failed,
        "sent": sent,
        "cancelled": cancelled,
        "recent_sent_24h": recent_sent_24h,
        "status": status,
        "latest": latest_payload,
    }


@app.get("/api/adminportal/accounts")
async def adminportal_list_accounts(request: Request):
    _require_admin(request)
    if not _mail_admin_available():
        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, _mail_control_request, "/api/mail/list_accounts", {}, _MAIL_CONTROL_LIST_TIMEOUT
            )
            accounts = data.get("accounts") or []
            payload = {
                "ok": True,
                "accounts": accounts,
                "count": len(accounts),
                "source": data.get("source") or "soc-control-plane",
            }
            if data.get("warning"):
                payload["warning"] = data.get("warning")
            return payload
        except Exception as e:
            accounts = await asyncio.get_event_loop().run_in_executor(None, _directory_mail_accounts_from_panel)
            if accounts:
                return {
                    "ok": True,
                    "accounts": accounts,
                    "count": len(accounts),
                    "source": "directory_fallback",
                    "warning": f"mail control-plane list_accounts failed: {e}",
                }
            return {"ok": False, "accounts": [], "error": f"mail control-plane error: {e}"}
    ok, accounts, status = await asyncio.get_event_loop().run_in_executor(
        None, _collect_mail_accounts_result
    )
    if not ok:
        return {"ok": False, "accounts": [], "error": status}
    return {
        "ok": True,
        "accounts": accounts,
        "count": len(accounts),
        "source": "local_mailserver_config" if status == "host_config" else "local_mailserver",
    }


@app.get("/api/adminportal/accounts/detail")
async def adminportal_account_detail(request: Request, email: str = Query(...)):
    _require_admin(request)
    target_email = email.strip().lower()
    if not _EMAIL_RE.match(target_email):
        raise HTTPException(400, "Invalid email")
    detail = await asyncio.get_event_loop().run_in_executor(None, _build_account_detail, target_email)
    return {"ok": True, "detail": detail}


@app.get("/api/adminportal/mailboxes/timeline")
async def adminportal_mailbox_timeline(
    request: Request,
    email: str = Query(...),
    hours: int = Query(72, ge=1, le=24 * 14),
    filter: str = Query("all"),
):
    _require_admin(request)
    target_email = email.strip().lower()
    if not _EMAIL_RE.match(target_email):
        raise HTTPException(400, "Invalid email")
    filter_name = filter if filter in {"all", "delivery", "auth", "lifecycle"} else "all"
    payload = await asyncio.get_event_loop().run_in_executor(
        None, _build_mailbox_timeline, target_email, hours, filter_name
    )
    return payload


@app.get("/api/adminportal/mailboxes/doctor")
async def adminportal_mailbox_doctor(request: Request, email: str = Query(...)):
    _require_admin(request)
    target_email = email.strip().lower()
    if not _EMAIL_RE.match(target_email):
        raise HTTPException(400, "Invalid email")
    local = target_email.split("@", 1)[0].lower()
    emp_id = None
    for row in _fetch_employees_cached():
        mail_login = str(row.get("mail_login") or "").strip().lower()
        ad_login = str(row.get("ad_login") or "").strip().lower()
        if mail_login == local or ad_login == local:
            try:
                emp_id = int(row.get("id"))
            except Exception:
                emp_id = None
            break
    payload = {"mail_login": local}
    if emp_id is not None:
        payload["emp_id"] = emp_id
    try:
        doctor = await asyncio.get_event_loop().run_in_executor(
            None, _mail_control_request, "/api/mail/doctor", payload, 15
        )
    except Exception as exc:
        raise HTTPException(
            502,
            f"mail control-plane error: {exc}",
        ) from exc
    doctor["delivery"] = await asyncio.get_event_loop().run_in_executor(
        None, _build_mailbox_delivery_doctor, target_email
    )
    return {"ok": True, "doctor": doctor}


async def _adminportal_mailbox_suspend_locked(body: AdminPortalMailboxLifecycleBody, request: Request):
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    target_email = body.email.strip().lower()
    reason = body.reason.strip()
    if not _EMAIL_RE.match(target_email):
        db_audit_log(admin_user, "account.suspend", target_email, {"error": "invalid_format"}, ip, ok=False)
        raise HTTPException(400, "Invalid email")
    _reject_generic_managed_address_operation(
        target_email,
        "Приостановка управляемого ящика",
    )
    lifecycle = db_get_mailbox_lifecycle(target_email)
    if lifecycle and lifecycle.get("state") == "suspended":
        db_audit_log(admin_user, "account.suspend", target_email, {"error": "already_suspended"}, ip, ok=False)
        raise HTTPException(409, "Mailbox already suspended")
    loop = asyncio.get_event_loop()
    mail_login = target_email.split("@", 1)[0]
    employee = await loop.run_in_executor(
        None,
        _panel_find_employee_by_exact_mail_login,
        mail_login,
        True,
    )
    if not employee or not isinstance(employee.get("id"), int):
        raise HTTPException(409, "Employee mailbox binding not found")
    emp_id = int(employee["id"])
    try:
        recovery = await loop.run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/recovery_password",
            {"emp_id": emp_id, "mail_login": mail_login},
            30,
        )
    except Exception as exc:
        raise HTTPException(
            502,
            f"mail control-plane error: {exc}",
        ) from exc
    recovery_password = str(recovery.get("password") or "")
    if not recovery_password:
        raise HTTPException(409, "Stored mailbox password is unavailable")
    ok_hash, current_hash = await loop.run_in_executor(None, _mailserver_get_account_hash, target_email)
    if not ok_hash:
        db_audit_log(admin_user, "account.suspend", target_email, {"error": current_hash}, ip, ok=False)
        raise HTTPException(404 if current_hash == "not found" else 500, f"Mailserver error: {current_hash}")
    block_password = secrets.token_urlsafe(24)
    try:
        lifecycle_operation_id = await loop.run_in_executor(
            None,
            _mailbox_lifecycle_operation_start,
            "suspend",
            target_email,
            emp_id,
            admin_user,
            reason,
            block_password,
            current_hash,
        )
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        await loop.run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/reset_password",
            {
                "emp_id": emp_id,
                "mail_login": mail_login,
                "password": block_password,
                "persist_credentials": False,
            },
            60,
        )
    except Exception as exc:
        compensated = False
        try:
            await loop.run_in_executor(
                None,
                _mail_control_request,
                "/api/mail/reset_password",
                {
                    "emp_id": emp_id,
                    "mail_login": mail_login,
                    "password": recovery_password,
                },
                60,
            )
            compensated = True
        except Exception:
            log.critical(
                "mailbox suspend compensation failed: %s",
                target_email,
            )
        await loop.run_in_executor(
            None,
            _mailbox_lifecycle_operation_update,
            lifecycle_operation_id,
            "completed" if compensated else "pending",
            f"compensated after failure: {exc}" if compensated else str(exc),
        )
        db_audit_log(admin_user, "account.suspend", target_email, {"error": str(exc)}, ip, ok=False)
        raise HTTPException(502, f"mail control-plane error: {exc}") from exc
    try:
        lifecycle_row = await loop.run_in_executor(None, db_mark_mailbox_suspended, target_email, current_hash, admin_user, reason)
    except Exception as e:
        compensated = False
        try:
            await loop.run_in_executor(
                None,
                _mail_control_request,
                "/api/mail/reset_password",
                {
                    "emp_id": emp_id,
                    "mail_login": mail_login,
                    "password": recovery_password,
                },
                60,
            )
            compensated = True
        except Exception:
            log.critical(
                "mailbox suspend lifecycle compensation failed: %s",
                target_email,
            )
        await loop.run_in_executor(
            None,
            _mailbox_lifecycle_operation_update,
            lifecycle_operation_id,
            "completed" if compensated else "pending",
            f"compensated after lifecycle failure: {e}"
            if compensated
            else str(e),
        )
        db_audit_log(admin_user, "account.suspend", target_email, {"error": str(e)}, ip, ok=False)
        raise HTTPException(500, "Failed to persist lifecycle state")
    await loop.run_in_executor(
        None,
        _mailbox_lifecycle_operation_update,
        lifecycle_operation_id,
        "completed",
    )
    db_audit_log(admin_user, "account.suspend", target_email, {"reason": reason}, ip, ok=True)
    return {
        "ok": True,
        "email": target_email,
        "state": "suspended",
        "suspended_at": lifecycle_row.get("suspended_at"),
        "reason": lifecycle_row.get("suspend_reason") or reason,
    }


@app.post("/api/adminportal/mailboxes/suspend")
async def adminportal_mailbox_suspend(
    body: AdminPortalMailboxLifecycleBody,
    request: Request,
):
    lock = _mailbox_lifecycle_lock(body.email)
    await asyncio.get_event_loop().run_in_executor(None, lock.acquire)
    try:
        return await _adminportal_mailbox_suspend_locked(body, request)
    finally:
        lock.release()


async def _adminportal_mailbox_restore_locked(body: AdminPortalMailboxLifecycleBody, request: Request):
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    target_email = body.email.strip().lower()
    reason = body.reason.strip()
    if not _EMAIL_RE.match(target_email):
        db_audit_log(admin_user, "account.restore", target_email, {"error": "invalid_format"}, ip, ok=False)
        raise HTTPException(400, "Invalid email")
    _reject_generic_managed_address_operation(
        target_email,
        "Восстановление управляемого ящика",
    )
    lifecycle = db_get_mailbox_lifecycle(target_email)
    if not lifecycle or lifecycle.get("state") != "suspended" or not lifecycle.get("has_stored_hash"):
        db_audit_log(admin_user, "account.restore", target_email, {"error": "not_suspended"}, ip, ok=False)
        raise HTTPException(409, "Mailbox is not suspended")
    loop = asyncio.get_event_loop()
    mail_login = target_email.split("@", 1)[0]
    employee = await loop.run_in_executor(
        None,
        _panel_find_employee_by_exact_mail_login,
        mail_login,
        True,
    )
    if not employee or not isinstance(employee.get("id"), int):
        raise HTTPException(409, "Employee mailbox binding not found")
    emp_id = int(employee["id"])
    try:
        recovery = await loop.run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/recovery_password",
            {"emp_id": emp_id, "mail_login": mail_login},
            30,
        )
        recovery_password = str(recovery.get("password") or "")
        if not recovery_password:
            raise RuntimeError("stored mailbox password is unavailable")
    except Exception as exc:
        db_audit_log(admin_user, "account.restore", target_email, {"error": str(exc)}, ip, ok=False)
        raise HTTPException(502, f"mail control-plane error: {exc}") from exc
    try:
        lifecycle_operation_id = await loop.run_in_executor(
            None,
            _mailbox_lifecycle_operation_start,
            "restore",
            target_email,
            emp_id,
            admin_user,
            reason,
            recovery_password,
            "",
        )
    except Exception as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        await loop.run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/reset_password",
            {
                "emp_id": emp_id,
                "mail_login": mail_login,
                "password": recovery_password,
            },
            60,
        )
    except Exception as exc:
        await loop.run_in_executor(
            None,
            _mailbox_lifecycle_operation_update,
            lifecycle_operation_id,
            "pending",
            str(exc),
        )
        db_audit_log(admin_user, "account.restore", target_email, {"error": str(exc)}, ip, ok=False)
        raise HTTPException(502, f"mail control-plane error: {exc}") from exc
    try:
        lifecycle_row = await loop.run_in_executor(
            None,
            db_mark_mailbox_restored,
            target_email,
            admin_user,
            reason,
        )
    except Exception as exc:
        compensated = False
        try:
            await loop.run_in_executor(
                None,
                _mail_control_request,
                "/api/mail/reset_password",
                {
                    "emp_id": emp_id,
                    "mail_login": mail_login,
                    "password": secrets.token_urlsafe(24),
                    "persist_credentials": False,
                },
                60,
            )
            compensated = True
        except Exception:
            log.critical(
                "mailbox restore lifecycle compensation failed: %s",
                target_email,
            )
        await loop.run_in_executor(
            None,
            _mailbox_lifecycle_operation_update,
            lifecycle_operation_id,
            "completed" if compensated else "pending",
            f"compensated after lifecycle failure: {exc}"
            if compensated
            else str(exc),
        )
        db_audit_log(admin_user, "account.restore", target_email, {"error": str(exc)}, ip, ok=False)
        raise HTTPException(500, "Failed to persist lifecycle state") from exc
    await loop.run_in_executor(
        None,
        _mailbox_lifecycle_operation_update,
        lifecycle_operation_id,
        "completed",
    )
    db_audit_log(admin_user, "account.restore", target_email, {"reason": reason}, ip, ok=True)
    return {
        "ok": True,
        "email": target_email,
        "state": "active",
        "restored_at": lifecycle_row.get("restored_at"),
    }


@app.post("/api/adminportal/mailboxes/restore")
async def adminportal_mailbox_restore(
    body: AdminPortalMailboxLifecycleBody,
    request: Request,
):
    lock = _mailbox_lifecycle_lock(body.email)
    await asyncio.get_event_loop().run_in_executor(None, lock.acquire)
    try:
        return await _adminportal_mailbox_restore_locked(body, request)
    finally:
        lock.release()


@app.post("/api/adminportal/accounts/create")
async def adminportal_create_account(body: AdminPortalCreateBody, request: Request):
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        db_audit_log(admin_user, "account.create", email, {"error": "invalid_format"}, ip, ok=False)
        raise HTTPException(400, "Invalid email format")
    _reject_generic_managed_address_operation(
        email,
        "Создание управляемого адреса",
    )
    mail_login = email.split("@", 1)[0]
    employee = await asyncio.get_event_loop().run_in_executor(
        None,
        _panel_find_employee_by_exact_mail_login,
        mail_login,
        True,
    )
    if not employee or not isinstance(employee.get("id"), int):
        raise HTTPException(
            409,
            "Создание доступно только для привязанного сотрудника "
            "или через раздел «Общие адреса»",
        )
    password = body.password or _generate_password()
    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/create_account",
            {
                "mail_login": mail_login,
                "emp_id": int(employee["id"]),
                "password": password,
            },
            60,
        )
    except Exception as exc:
        db_audit_log(
            admin_user,
            "account.create",
            email,
            {"error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(
            502,
            f"mail control-plane error: {exc}",
        ) from exc
    actual_password = data.get("password") or ""
    db_audit_log(
        admin_user,
        "account.create",
        email,
        {"status": data.get("status"), "source": "soc-control-plane"},
        ip,
        ok=True,
    )
    return {
        "ok": True,
        "email": data.get("email") or email,
        "password": actual_password,
        "credential_pack": _build_credential_pack(
            data.get("email") or email,
            actual_password,
            data.get("email") or email,
        ),
    }


@app.post("/api/adminportal/accounts/delete")
async def adminportal_delete_account(body: AdminPortalEmailBody, request: Request):
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        db_audit_log(admin_user, "account.delete", email, {"error": "invalid_format"}, ip, ok=False)
        raise HTTPException(400, "Invalid email")
    _reject_generic_managed_address_operation(
        email,
        "Удаление управляемого адреса",
    )
    mail_login = email.split("@", 1)[0]
    employee = await asyncio.get_event_loop().run_in_executor(
        None,
        _panel_find_employee_by_exact_mail_login,
        mail_login,
        True,
    )
    if not employee or not isinstance(employee.get("id"), int):
        raise HTTPException(409, "Почтовый аккаунт не привязан к сотруднику")
    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/delete_account",
            {
                "mail_login": mail_login,
                "emp_id": int(employee["id"]),
            },
            60,
        )
    except Exception as exc:
        db_audit_log(
            admin_user,
            "account.delete",
            email,
            {"error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(
            502,
            f"mail control-plane error: {exc}",
        ) from exc
    with _db_connection() as con:
        con.execute("DELETE FROM mailbox_lifecycle WHERE email = ?", (email,))
        con.commit()
    status = str(data.get("status") or "removed")
    log.info("Admin portal deleted account: %s by %s (%s)", email, admin_user, status)
    db_audit_log(
        admin_user,
        "account.delete",
        email,
        {"status": status, "source": "soc-control-plane"},
        ip,
        ok=True,
    )
    return {"ok": True, "email": email}


@app.post("/api/adminportal/accounts/password")
async def adminportal_change_password(body: AdminPortalPasswordBody, request: Request):
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    email = body.email.strip().lower()
    if not _EMAIL_RE.match(email):
        db_audit_log(admin_user, "account.password", email, {"error": "invalid_format"}, ip, ok=False)
        raise HTTPException(400, "Invalid email")
    _reject_generic_managed_address_operation(
        email,
        "Смена пароля управляемого ящика",
    )
    mail_login = email.split("@", 1)[0]
    employee = await asyncio.get_event_loop().run_in_executor(
        None,
        _panel_find_employee_by_exact_mail_login,
        mail_login,
        True,
    )
    if not employee or not isinstance(employee.get("id"), int):
        raise HTTPException(409, "Почтовый аккаунт не привязан к сотруднику")
    lifecycle = db_get_mailbox_lifecycle(email)
    if lifecycle and lifecycle.get("state") == "suspended":
        db_audit_log(admin_user, "account.password", email, {"error": "suspended"}, ip, ok=False)
        raise HTTPException(409, "Mailbox suspended. Restore it before resetting password.")
    new_pass = body.password or _generate_password()
    try:
        data = await asyncio.get_event_loop().run_in_executor(
            None,
            _mail_control_request,
            "/api/mail/reset_password",
            {
                "mail_login": mail_login,
                "emp_id": int(employee["id"]),
                "password": new_pass,
            },
            60,
        )
    except Exception as exc:
        db_audit_log(
            admin_user,
            "account.password",
            email,
            {"error": str(exc)},
            ip,
            ok=False,
        )
        raise HTTPException(
            502,
            f"mail control-plane error: {exc}",
        ) from exc
    actual_password = str(data.get("password") or "")
    if not actual_password:
        db_audit_log(
            admin_user,
            "account.password",
            email,
            {
                "error": "password_not_confirmed",
                "status": str(data.get("status") or ""),
            },
            ip,
            ok=False,
        )
        raise HTTPException(
            502,
            "mail control-plane did not confirm the mailbox password",
        )
    status = str(data.get("status") or "updated")
    log.info("Admin portal password changed: %s by %s", email, admin_user)
    db_audit_log(
        admin_user,
        "account.password",
        email,
        {"status": status, "source": "soc-control-plane"},
        ip,
        ok=True,
    )
    return {
        "ok": True,
        "email": data.get("email") or email,
        "password": actual_password,
        "status": status,
        "credential_pack": _build_credential_pack(
            data.get("email") or email,
            actual_password,
            data.get("email") or email,
        ),
    }


@app.get("/api/adminportal/mail-health")
async def adminportal_mail_health(request: Request):
    _require_admin(request)
    loop = asyncio.get_event_loop()
    control_task = loop.run_in_executor(None, _mail_control_health_cached, 3, 15.0)
    imap_task = loop.run_in_executor(None, _probe_imap_ready)
    smtp_task = loop.run_in_executor(None, _probe_smtp_ready)
    delivery_task = loop.run_in_executor(None, _mail_delivery_global_snapshot)
    watchers_task = loop.run_in_executor(None, _mail_health_watcher_counts)

    control: Dict[str, Any] = {}
    control_error = ""
    delivery: Dict[str, Any] = {}
    delivery_error = ""
    watchers: Dict[str, Any] = {"enabled": None, "total": 0, "available": False}
    watcher_error = ""

    try:
        control, control_error = await control_task
    except Exception as exc:
        control_error = str(exc)
    imap_ok, imap_status = await imap_task
    smtp_ok, smtp_status = await smtp_task
    target_host, target_port = _control_imap_probe_target(control)
    if _use_control_imap_probe_target(target_host, target_port):
        imap_ok, imap_status = await loop.run_in_executor(None, _probe_imap_ready, target_host, target_port)
    try:
        delivery = await delivery_task
    except Exception as exc:
        delivery_error = str(exc)
        delivery = {
            "pending": 0,
            "sending": 0,
            "failed": 0,
            "sent": 0,
            "recent_sent_24h": 0,
            "status": "error",
        }
    try:
        watchers = await watchers_task
    except Exception as exc:
        watcher_error = str(exc)

    ready_payload = _build_ready_payload(imap_ok, imap_status, smtp_ok, smtp_status, control, control_error)
    control_snapshot = _mail_control_snapshot(control, control_error)
    severity, key = _mail_health_severity(control_snapshot, ready_payload, delivery)
    issues = _mail_health_issue_list(control_snapshot, ready_payload, delivery, control_error, delivery_error, watcher_error)
    queued = int(delivery.get("pending") or 0) + int(delivery.get("sending") or 0)
    headline = issues[0] if issues else (f"outbox queued: {queued}" if queued else "mail path healthy")
    return {
        "ok": True,
        "health": {
            "checked_at": _utc_now_iso(),
            "severity": severity,
            "key": key,
            "headline": headline,
            "issues": issues,
            "control": control_snapshot,
            "control_error": control_error,
            "ready": ready_payload,
            "delivery": delivery,
            "delivery_error": delivery_error,
            "watchers": watchers,
            "watcher_error": watcher_error,
        },
    }


# ---------------------------------------------------------------------------
# Admin Portal — Audit log query endpoint
# ---------------------------------------------------------------------------

@app.get("/api/adminportal/audit")
async def adminportal_audit(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin_user: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    target: Optional[str] = Query(None),
):
    """List audit log rows with optional filters."""
    _require_admin(request)
    rows, total = db_audit_query(limit, offset, admin_user, action, target)
    return {"ok": True, "total": total, "limit": limit, "offset": offset, "rows": rows}


# ---------------------------------------------------------------------------
# Admin Portal — Mail statistics
# ---------------------------------------------------------------------------

@app.get("/api/adminportal/stats")
async def adminportal_stats(request: Request):
    """Mailbox count, sizes, postfix queue stats."""
    _require_admin(request)
    stats = await asyncio.get_event_loop().run_in_executor(None, _mailserver_stats)
    return {"ok": True, **stats}


@app.get("/api/adminportal/settings")
async def adminportal_settings(request: Request):
    """Public connection parameters for the authenticated admin console."""
    _require_admin(request)
    return {
        "ok": True,
        "mail": {
            "domain": CFG.MAIL_DOMAIN,
            "webmail_url": CFG.MAIL_WEBMAIL_URL,
            "admin_url": _admin_sso_origin(request),
            "imap": {
                "host": CFG.MAIL_PUBLIC_HOST,
                "port": CFG.MAIL_PUBLIC_IMAP_PORT,
                "security": "SSL/TLS",
            },
            "smtp": {
                "host": CFG.MAIL_PUBLIC_HOST,
                "port": CFG.MAIL_PUBLIC_SMTP_PORT,
                "security": "STARTTLS",
            },
        },
        "identity": {
            "sso": {
                "configured": bool(
                    CFG.MAILADMIN_SSO_CLIENT_ID
                    and CFG.MAILADMIN_SSO_CLIENT_SECRET
                    and CFG.MAILADMIN_SSO_AUTHORIZATION_ENDPOINT
                ),
                "issuer": CFG.MAIL_SSO_ISSUER,
                "redirect_uri": _admin_sso_redirect_uri(request),
            },
            "ldaps": {
                "configured": _ldap_bind_available(),
                "servers": list(CFG.LDAP_SERVERS),
                "base_dn": CFG.LDAP_BASE_DN,
                "bind_user": CFG.LDAP_BIND_USER,
            },
            "allowed_admin_groups": db_list_mailadmin_admin_groups(),
        },
    }


@app.get("/api/adminportal/directory/groups")
async def adminportal_directory_groups(
    request: Request,
    q: str = Query("", max_length=200),
    refresh: int = Query(0, ge=0, le=1),
):
    _require_admin(request)
    try:
        groups = await asyncio.get_event_loop().run_in_executor(
            _mailadmin_read_executor,
            directory_load_groups,
            bool(refresh),
        )
    except Exception as exc:
        raise HTTPException(503, "Каталог групп AD временно недоступен") from exc
    needle = str(q or "").strip().casefold()
    if needle:
        groups = [
            group
            for group in groups
            if needle
            in " ".join(
                str(group.get(key) or "")
                for key in ("name", "description", "mail", "dn")
            ).casefold()
        ]
    return {"ok": True, "groups": groups, "total": len(groups)}


class AdminIdentitySettingsBody(BaseModel):
    allowed_group_dns: list[str] = Field(default_factory=list)


@app.put("/api/adminportal/settings/identity")
async def adminportal_identity_settings_update(
    body: AdminIdentitySettingsBody,
    request: Request,
):
    sess = _require_admin(request)
    groups = await asyncio.get_event_loop().run_in_executor(
        _mailadmin_read_executor,
        _validate_admin_group_selection,
        body.allowed_group_dns,
    )
    await asyncio.get_event_loop().run_in_executor(
        None,
        db_replace_mailadmin_admin_groups,
        groups,
    )
    db_audit_log(
        sess.get("username", "admin"),
        "identity.admin_groups_update",
        "mailadmin",
        details={
            "groups": [group["name"] for group in groups],
            "count": len(groups),
        },
        ip=_client_ip(request),
    )
    _admin_sessions.clear()
    return {
        "ok": True,
        "allowed_admin_groups": groups,
        "sessions_revoked": True,
    }


@app.post("/api/adminportal/settings/identity/check")
async def adminportal_identity_settings_check(request: Request):
    _require_admin(request)
    try:
        groups = await asyncio.get_event_loop().run_in_executor(
            _mailadmin_read_executor,
            directory_load_groups,
            True,
        )
        ldaps = {"ok": True, "groups_count": len(groups)}
    except Exception as exc:
        ldaps = {"ok": False, "message": str(exc)[:200]}
    sso_ok = bool(
        CFG.MAILADMIN_SSO_CLIENT_ID
        and CFG.MAILADMIN_SSO_CLIENT_SECRET
        and CFG.MAILADMIN_SSO_AUTHORIZATION_ENDPOINT
        and CFG.MAILADMIN_SSO_TOKEN_ENDPOINT
        and CFG.MAILADMIN_SSO_USERINFO_ENDPOINT
    )
    return {
        "ok": bool(ldaps["ok"] and sso_ok),
        "ldaps": ldaps,
        "sso": {"ok": sso_ok},
    }


# ---------------------------------------------------------------------------
# Admin Portal — Postfix queue management
# ---------------------------------------------------------------------------

def _postfix_queue_list() -> List[Dict[str, Any]]:
    ok, out = _docker_exec_capture(["postqueue", "-p"], timeout=8)
    if not ok:
        return []
    return _parse_postqueue(out)


def _postfix_flush() -> tuple[bool, str]:
    return _docker_exec_capture(["postqueue", "-f"], timeout=8)


def _postfix_delete(queue_id: str) -> tuple[bool, str]:
    if queue_id == "ALL":
        return _docker_exec_capture(["postsuper", "-d", "ALL"], timeout=10)
    if not re.match(r"^[A-F0-9]{6,}\*?$", queue_id):
        return False, "Invalid queue id"
    return _docker_exec_capture(["postsuper", "-d", queue_id], timeout=8)


@app.get("/api/adminportal/queue")
async def adminportal_queue_list(request: Request):
    """List Postfix queue contents (parsed JSON)."""
    _require_admin(request)
    entries = await asyncio.get_event_loop().run_in_executor(None, _postfix_queue_list)
    deferred = sum(1 for e in entries if not e.get("active"))
    return {"ok": True, "entries": entries, "total": len(entries), "deferred": deferred}


class QueueDeleteBody(BaseModel):
    queue_id: str  # specific id or "ALL"


@app.post("/api/adminportal/queue/flush")
async def adminportal_queue_flush(request: Request):
    """Trigger Postfix to attempt delivery of all queued messages."""
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    ok, out = await asyncio.get_event_loop().run_in_executor(None, _postfix_flush)
    db_audit_log(admin_user, "queue.flush", "", {"output": out[:200]}, ip, ok=ok)
    if not ok:
        raise HTTPException(500, f"postqueue -f failed: {out}")
    return {"ok": True, "output": out}


@app.post("/api/adminportal/queue/delete")
async def adminportal_queue_delete(body: QueueDeleteBody, request: Request):
    """Delete a single queue entry, or all (queue_id='ALL')."""
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    ok, out = await asyncio.get_event_loop().run_in_executor(
        None, _postfix_delete, body.queue_id
    )
    db_audit_log(admin_user, "queue.delete", body.queue_id, {"output": out[:200]}, ip, ok=ok)
    if not ok:
        raise HTTPException(500, f"postsuper -d failed: {out}")
    return {"ok": True, "output": out}


# ---------------------------------------------------------------------------
# Admin Portal — Alias management
# ---------------------------------------------------------------------------

def db_list_all_aliases() -> List[Dict[str, Any]]:
    employees = _fetch_employees_cached()
    by_ad: Dict[str, Dict[str, Any]] = {}
    by_mail: Dict[str, Dict[str, Any]] = {}
    for emp in employees:
        ad = (emp.get("ad_login") or "").lower()
        ml = (emp.get("mail_login") or "").lower()
        if ad:
            by_ad[ad] = emp
        if ml:
            by_mail[ml] = emp
    panel_rows: List[Dict[str, Any]] = []
    seen_ads = set()
    seen_mails = set()
    for emp in employees:
        ad = (emp.get("ad_login") or "").lower()
        ml = (emp.get("mail_login") or "").lower()
        if not ml:
            continue
        panel_rows.append({
            "id": emp.get("id"),
            "ad_login": ad,
            "mail_login": ml,
            "created_at": emp.get("created_at"),
            "updated_at": emp.get("created_at"),
            "source": "proxy_employees",
            "employee_name": emp.get("display_name") or emp.get("name"),
            "department": emp.get("department"),
        })
        if ad:
            seen_ads.add(ad)
        seen_mails.add(ml)
    with _db_connection() as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, ad_login, mail_login, created_at, updated_at "
            "FROM mail_aliases ORDER BY ad_login"
        ).fetchall()
        legacy_rows = []
        for row in rows:
            ad = (row["ad_login"] or "").lower()
            ml = (row["mail_login"] or "").lower()
            if ad in seen_ads or ml in seen_mails:
                continue
            emp = by_ad.get(ad) or by_mail.get(ml)
            legacy_rows.append({
                **dict(row),
                "source": "legacy_sqlite",
                "employee_name": emp.get("display_name") or emp.get("name") if emp else None,
                "department": emp.get("department") if emp else None,
            })
    return sorted(
        panel_rows + legacy_rows,
        key=lambda row: (
            str(row.get("ad_login") or "").lower(),
            str(row.get("employee_name") or "").casefold(),
            str(row.get("mail_login") or "").lower(),
        ),
    )


def db_delete_alias(ad_login: str) -> bool:
    emp = _panel_find_employee_by_exact_login(ad_login, force=True)
    deleted = False
    if emp and emp.get("id") and (emp.get("ad_login") or "").lower() == ad_login.lower():
        row = _panel_update_employee_identity(int(emp["id"]), clear_mail_login=True)
        deleted = row is not None
    with _db_connection() as con:
        cur = con.execute("DELETE FROM mail_aliases WHERE ad_login = ?", (ad_login.lower(),))
        con.commit()
        return deleted or cur.rowcount > 0


def db_delete_alias_by_employee(emp_id: int) -> bool:
    emp = _panel_find_employee_by_id(int(emp_id), force=True)
    if not emp:
        return False
    ad_login = str(emp.get("ad_login") or "").strip().lower()
    row = _panel_update_employee_identity(int(emp_id), clear_mail_login=True)
    if row is None:
        return False
    if ad_login:
        with _db_connection() as con:
            con.execute(
                "DELETE FROM mail_aliases WHERE ad_login = ?",
                (ad_login,),
            )
            con.commit()
    return True


@app.get("/api/adminportal/aliases")
async def adminportal_aliases_list(request: Request):
    """List all mail_aliases rows."""
    _require_admin(request)
    rows = await asyncio.get_event_loop().run_in_executor(None, db_list_all_aliases)
    return {"ok": True, "aliases": rows, "total": len(rows)}


@app.get("/api/adminportal/employees")
async def adminportal_employee_options(request: Request):
    """Return the minimal employee directory needed by mailbox selectors."""
    _require_admin(request)
    rows = await asyncio.get_event_loop().run_in_executor(
        None,
        _fetch_employees_cached,
    )
    if not rows and _employee_directory_status.get("error"):
        raise HTTPException(502, "Каталог сотрудников недоступен")
    employees = []
    for row in rows:
        mail_login = str(row.get("mail_login") or "").strip().lower()
        employees.append(
            {
                "id": row.get("id"),
                "name": _employee_display_name(row),
                "ad_login": str(row.get("ad_login") or "").strip().lower(),
                "mail_login": mail_login,
                "email": _mail_login_to_email(mail_login) if mail_login else "",
                "department": row.get("department") or "",
                "title": row.get("job_title") or row.get("title") or "",
            }
        )
    employees.sort(
        key=lambda row: (
            str(row.get("name") or "").casefold(),
            str(row.get("ad_login") or ""),
        )
    )
    return {"ok": True, "employees": employees, "total": len(employees)}


class AliasUpsertBody(BaseModel):
    ad_login: str
    mail_login: str = Field(..., min_length=3, max_length=30)
    emp_id: int | None = None


@app.put("/api/adminportal/aliases")
async def adminportal_alias_upsert(body: AliasUpsertBody, request: Request):
    """Set or update an alias for an AD login."""
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    ad = body.ad_login.strip().lower()
    ml = body.mail_login.strip().lower()
    if body.emp_id is None and not re.match(r"^[a-z0-9._-]+$", ad):
        raise HTTPException(400, "Invalid ad_login format")
    if not re.match(r"^[a-z0-9][a-z0-9.\-]{1,28}[a-z0-9]$", ml):
        raise HTTPException(400, "Invalid mail_login format (3-30 chars, a-z 0-9 . -)")
    _reject_generic_managed_address_operation(
        _mail_login_to_email(ml),
        "Привязка управляемого адреса к сотруднику",
    )
    try:
        if body.emp_id is not None:
            emp = await asyncio.get_event_loop().run_in_executor(
                None,
                _panel_find_employee_by_id,
                body.emp_id,
                True,
            )
            if not emp:
                raise HTTPException(404, "Employee not found")
            selected_ad = str(emp.get("ad_login") or "").strip().lower()
            if ad and selected_ad and selected_ad != ad:
                raise HTTPException(409, "Employee selection does not match ad_login")
            ad = selected_ad
            emp_id = int(body.emp_id)
        else:
            emp = await asyncio.get_event_loop().run_in_executor(
                None,
                _panel_find_employee_by_exact_login,
                ad,
                True,
            )
            emp_id = int(emp["id"]) if emp and emp.get("id") else None
        row = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: db_set_alias(ad, ml, emp_id=emp_id),
        )
    except ValueError as e:
        db_audit_log(admin_user, "alias.set", f"{ad}->{ml}",
                     {"error": str(e)}, ip, ok=False)
        raise HTTPException(409, str(e))
    except EmployeeDirectoryUnavailable as e:
        db_audit_log(
            admin_user,
            "alias.set",
            f"{ad}->{ml}",
            {"error": str(e)},
            ip,
            ok=False,
        )
        raise HTTPException(502, "Каталог сотрудников недоступен") from e
    db_audit_log(admin_user, "alias.set", f"{ad}->{ml}", None, ip, ok=True)
    return {"ok": True, "alias": row}


class AliasDeleteBody(BaseModel):
    ad_login: str = ""
    emp_id: int | None = None


@app.post("/api/adminportal/aliases/delete")
async def adminportal_alias_delete(body: AliasDeleteBody, request: Request):
    """Delete an alias by AD login."""
    sess = _require_admin(request)
    admin_user = sess.get("username", "?")
    ip = _client_ip(request)
    ad = body.ad_login.strip().lower()
    if body.emp_id is not None:
        target = f"employee:{body.emp_id}"
        try:
            deleted = await asyncio.get_event_loop().run_in_executor(
                None,
                db_delete_alias_by_employee,
                body.emp_id,
            )
        except EmployeeDirectoryUnavailable as exc:
            raise HTTPException(502, "Каталог сотрудников недоступен") from exc
    else:
        if not re.match(r"^[a-z0-9._-]+$", ad):
            raise HTTPException(400, "Invalid ad_login format")
        target = ad
        try:
            deleted = await asyncio.get_event_loop().run_in_executor(
                None,
                db_delete_alias,
                ad,
            )
        except EmployeeDirectoryUnavailable as exc:
            raise HTTPException(502, "Каталог сотрудников недоступен") from exc
    db_audit_log(admin_user, "alias.delete", target, {"deleted": deleted}, ip, ok=deleted)
    if not deleted:
        raise HTTPException(404, "Alias not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Calendar (CalDAV via Radicale on 127.0.0.1:5232)
# ---------------------------------------------------------------------------
#
# Radicale is reachable both externally at mail.example.com/caldav/
# (for external clients like Outlook/Thunderbird/iOS) and locally from the
# webmail FastAPI backend at http://127.0.0.1:5232/ (direct, bypassing nginx).
# We authenticate with HTTP Basic using the session's stored IMAP password —
# Radicale validates via the same Dovecot IMAP that the user logged in with.

CALDAV_BASE = os.environ.get("CALDAV_BASE", "http://127.0.0.1:5232")
CALDAV_DEFAULT_COLLECTION = "jmail"  # URL segment for the default calendar

_ICAL_NL = "\r\n"
_ICAL_FOLD_LINE = 73


def _ical_fold(line: str) -> str:
    """Fold long lines per RFC 5545 §3.1 (max 75 octets)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    parts = []
    chunk = ""
    for ch in line:
        if len((chunk + ch).encode("utf-8")) > _ICAL_FOLD_LINE:
            parts.append(chunk)
            chunk = " " + ch
        else:
            chunk += ch
    if chunk:
        parts.append(chunk)
    return _ICAL_NL.join(parts)


def _ical_escape(s: str) -> str:
    if s is None:
        return ""
    return (s.replace("\\", "\\\\").replace(",", "\\,")
             .replace(";", "\\;").replace("\n", "\\n").replace("\r", ""))


def _ical_unescape(s: str) -> str:
    if s is None:
        return ""
    return (s.replace("\\n", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def _dt_to_ical(dt_str: str) -> tuple[str, bool]:
    """Convert ISO-8601 or YYYY-MM-DD to iCal DATE or DATE-TIME (UTC).
    Returns (value, is_all_day)."""
    s = (dt_str or "").strip()
    if not s:
        return "", False
    # All-day: YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s.replace("-", ""), True
    # Try parsing ISO with timezone
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_utc = dt.astimezone(timezone.utc)
        return dt_utc.strftime("%Y%m%dT%H%M%SZ"), False
    except Exception:
        return s, False


def _ical_to_iso(value: str) -> str:
    """Convert iCal DATE/DATE-TIME to ISO-8601 string for the browser."""
    v = (value or "").strip()
    if not v:
        return ""
    # UTC timestamp: 20260408T120000Z
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$", v)
    if m:
        y, mo, d, h, mi, s = m.groups()
        return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"
    # All-day: 20260408
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", v)
    if m:
        return "-".join(m.groups())
    return v


def _build_ics(event: Dict[str, Any]) -> str:
    """Build a minimal RFC 5545 VCALENDAR containing one VEVENT."""
    uid = event.get("uid") or (secrets.token_hex(12) + f"@{CFG.MAIL_DOMAIN}")
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary = _ical_escape(event.get("summary") or "Событие")
    description = _ical_escape(event.get("description") or "")
    location = _ical_escape(event.get("location") or "")

    dtstart_val, start_all_day = _dt_to_ical(event.get("start") or "")
    dtend_val, end_all_day = _dt_to_ical(event.get("end") or "")
    dtstart_prop = (f"DTSTART;VALUE=DATE:{dtstart_val}" if start_all_day
                    else f"DTSTART:{dtstart_val}")
    dtend_prop = (f"DTEND;VALUE=DATE:{dtend_val}" if end_all_day
                  else f"DTEND:{dtend_val}")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RuPochta Webmail//Calendar//RU",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now_stamp}",
        dtstart_prop,
        dtend_prop,
        _ical_fold(f"SUMMARY:{summary}"),
    ]
    if description:
        lines.append(_ical_fold(f"DESCRIPTION:{description}"))
    if location:
        lines.append(_ical_fold(f"LOCATION:{location}"))
    # Attendees as ATTENDEE;CN="...":mailto:...
    for att in (event.get("attendees") or []):
        email_a = att.get("email") if isinstance(att, dict) else str(att)
        if not email_a:
            continue
        name_a = att.get("name", "") if isinstance(att, dict) else ""
        a_line = (f'ATTENDEE;CN="{_ical_escape(name_a)}";'
                  f'PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{email_a}'
                  if name_a else
                  f'ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{email_a}')
        lines.append(_ical_fold(a_line))
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return _ICAL_NL.join(lines) + _ICAL_NL


def _parse_ics(data: str) -> List[Dict[str, Any]]:
    """Parse an iCalendar document and return a list of event dicts.
    Handles line folding and basic properties."""
    # Unfold lines: per RFC 5545, a CRLF followed by a space/tab is continuation
    unfolded = re.sub(r"\r?\n[ \t]", "", data or "")
    events: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in unfolded.splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            current = {"uid": "", "summary": "", "description": "",
                       "location": "", "start": "", "end": "",
                       "all_day": False, "attendees": [], "organizer": ""}
        elif line == "END:VEVENT":
            if current:
                events.append(current)
            current = None
        elif current is not None:
            # Split at first unescaped colon, but property may have params after ';'
            m = re.match(r"^([A-Za-z\-]+)(?:;([^:]*))?:(.*)$", line)
            if not m:
                continue
            prop = m.group(1).upper()
            params = m.group(2) or ""
            val = m.group(3)
            if prop == "UID":
                current["uid"] = val
            elif prop == "SUMMARY":
                current["summary"] = _ical_unescape(val)
            elif prop == "DESCRIPTION":
                current["description"] = _ical_unescape(val)
            elif prop == "LOCATION":
                current["location"] = _ical_unescape(val)
            elif prop == "DTSTART":
                current["start"] = _ical_to_iso(val)
                if "VALUE=DATE" in params.upper():
                    current["all_day"] = True
            elif prop == "DTEND":
                current["end"] = _ical_to_iso(val)
            elif prop == "ATTENDEE":
                m2 = re.search(r"(?:mailto:)(\S+)", val, re.IGNORECASE)
                email_a = m2.group(1) if m2 else val
                cn_m = re.search(r'CN="?([^;"]+)"?', params)
                current["attendees"].append({
                    "email": email_a,
                    "name": cn_m.group(1) if cn_m else "",
                })
            elif prop == "ORGANIZER":
                m2 = re.search(r"(?:mailto:)(\S+)", val, re.IGNORECASE)
                current["organizer"] = m2.group(1) if m2 else val
    return events


def _caldav_request(sess: Dict[str, Any],
                    method: str,
                    path: str,
                    body: Optional[str] = None,
                    headers: Optional[Dict[str, str]] = None) -> tuple[int, bytes, dict]:
    """Authenticated HTTP call to Radicale using the session's IMAP password."""
    import base64 as _b64
    user = sess["imap_user"]
    pwd = sess["imap_pass"]
    auth = _b64.b64encode(f"{user}:{pwd}".encode()).decode()
    url = CALDAV_BASE.rstrip("/") + path
    req_headers = {
        "Authorization": f"Basic {auth}",
        "User-Agent": "rupochta/1.0",
    }
    if body is not None:
        req_headers["Content-Type"] = "text/calendar; charset=utf-8"
    if headers:
        req_headers.update(headers)
    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b""), dict(e.headers or {})
    except Exception as e:
        log.warning("caldav request failed: %s %s %s", method, url, e)
        return 0, str(e).encode(), {}


def _caldav_user_path(sess: Dict[str, Any]) -> str:
    """URL path segment for the user's principal on Radicale."""
    return f"/{urllib.parse.quote(sess['imap_user'], safe='')}"


def _caldav_default_collection_path(sess: Dict[str, Any]) -> str:
    return f"{_caldav_user_path(sess)}/{CALDAV_DEFAULT_COLLECTION}/"


def _caldav_ensure_default_collection(sess: Dict[str, Any]) -> bool:
    """MKCALENDAR the user's default collection if it doesn't exist yet."""
    path = _caldav_default_collection_path(sess)
    # HEAD-like check via PROPFIND depth 0
    status, _, _ = _caldav_request(sess, "PROPFIND", path,
                                    body="",
                                    headers={"Depth": "0"})
    if status == 207:
        return True
    if status not in (404, 401):
        log.debug("caldav collection check %s -> %s", path, status)
    # Create it
    mk_body = """<?xml version="1.0" encoding="utf-8" ?>
<mkcalendar xmlns="urn:ietf:params:xml:ns:caldav" xmlns:D="DAV:">
  <D:set><D:prop>
    <D:displayname>Мой календарь</D:displayname>
    <calendar-description>Личный календарь jmail</calendar-description>
  </D:prop></D:set>
</mkcalendar>
"""
    status, body, _ = _caldav_request(sess, "MKCALENDAR", path,
                                       body=mk_body,
                                       headers={"Content-Type": "application/xml; charset=utf-8"})
    if status not in (201, 207):
        log.warning("MKCALENDAR %s failed: %s %s", path, status, body[:200])
        return False
    log.info("Created default CalDAV collection %s", path)
    return True


def _caldav_list_events(sess: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch all .ics files from the user's default calendar and parse them."""
    _caldav_ensure_default_collection(sess)
    path = _caldav_default_collection_path(sess)
    # PROPFIND depth 1 to list all .ics hrefs
    pf_body = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:getetag/>
    <D:resourcetype/>
  </D:prop>
</D:propfind>
"""
    status, body, _ = _caldav_request(sess, "PROPFIND", path, body=pf_body,
                                       headers={"Depth": "1",
                                                "Content-Type": "application/xml"})
    if status != 207:
        log.warning("PROPFIND %s -> %s", path, status)
        return []
    text = body.decode("utf-8", errors="replace")
    hrefs = re.findall(r"<href[^>]*>([^<]+\.ics)</href>", text, flags=re.IGNORECASE)
    events = []
    for href in hrefs:
        # href is path-absolute; strip CALDAV_BASE prefix if present
        if href.startswith("/caldav/"):
            href = href[len("/caldav"):]
        status2, body2, _ = _caldav_request(sess, "GET", href)
        if status2 != 200:
            continue
        parsed = _parse_ics(body2.decode("utf-8", errors="replace"))
        for ev in parsed:
            ev["href"] = href
            events.append(ev)
    return events


def _caldav_put_event(sess: Dict[str, Any], event: Dict[str, Any]) -> tuple[bool, str]:
    """Create or update an event. Uses event['uid'] or generates one."""
    _caldav_ensure_default_collection(sess)
    if not event.get("uid"):
        event["uid"] = secrets.token_hex(12) + f"@{CFG.MAIL_DOMAIN}"
    ics = _build_ics(event)
    path = f"{_caldav_default_collection_path(sess)}{urllib.parse.quote(event['uid'])}.ics"
    status, body, _ = _caldav_request(sess, "PUT", path, body=ics)
    if status not in (200, 201, 204):
        return False, f"PUT failed: {status} {body[:200]}"
    return True, event["uid"]


def _caldav_delete_event(sess: Dict[str, Any], uid: str) -> tuple[bool, str]:
    path = f"{_caldav_default_collection_path(sess)}{urllib.parse.quote(uid)}.ics"
    status, body, _ = _caldav_request(sess, "DELETE", path)
    if status not in (200, 204, 404):
        return False, f"DELETE failed: {status}"
    return True, ""


class CalendarEventBody(BaseModel):
    uid: Optional[str] = None
    summary: str
    description: Optional[str] = ""
    location: Optional[str] = ""
    start: str              # ISO-8601 or YYYY-MM-DD
    end: str                # ISO-8601 or YYYY-MM-DD
    attendees: Optional[List[Dict[str, str]]] = None


@app.get("/api/calendar/events")
async def api_calendar_events(request: Request):
    """List all events in the user's default calendar."""
    sess = get_session_or_401(request)
    loop = asyncio.get_event_loop()
    try:
        events = await loop.run_in_executor(None, _caldav_list_events, sess)
        return {"ok": True, "events": events, "count": len(events)}
    except Exception as e:
        log.warning("calendar list error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/api/calendar/event")
async def api_calendar_create(body: CalendarEventBody, request: Request):
    """Create a new event."""
    sess = get_session_or_401(request)
    event = body.model_dump()
    loop = asyncio.get_event_loop()
    ok, uid_or_err = await loop.run_in_executor(None, _caldav_put_event, sess, event)
    if not ok:
        return JSONResponse({"error": uid_or_err}, status_code=502)
    return {"ok": True, "uid": uid_or_err}


@app.put("/api/calendar/event/{uid}")
async def api_calendar_update(uid: str, body: CalendarEventBody, request: Request):
    sess = get_session_or_401(request)
    event = body.model_dump()
    event["uid"] = uid
    loop = asyncio.get_event_loop()
    ok, err = await loop.run_in_executor(None, _caldav_put_event, sess, event)
    if not ok:
        return JSONResponse({"error": err}, status_code=502)
    return {"ok": True, "uid": uid}


@app.delete("/api/calendar/event/{uid}")
async def api_calendar_delete(uid: str, request: Request):
    sess = get_session_or_401(request)
    loop = asyncio.get_event_loop()
    ok, err = await loop.run_in_executor(None, _caldav_delete_event, sess, uid)
    if not ok:
        return JSONResponse({"error": err}, status_code=502)
    return {"ok": True}


@app.get("/api/calendar/config")
async def api_calendar_config(request: Request):
    """Return connection params so external clients (Outlook, Thunderbird,
    iOS Calendar) can be bootstrapped from webmail settings."""
    sess = get_session_or_401(request)
    base_url = str(CFG.MAIL_WEBMAIL_URL or f"https://mail.{CFG.MAIL_DOMAIN}").rstrip("/")
    return {
        "caldav_url": f"{base_url}/caldav{_caldav_user_path(sess)}/",
        "calendar_url": f"{base_url}/caldav{_caldav_default_collection_path(sess)}",
        "username": sess["imap_user"],
        "server_display": f"{CFG.MAIL_PUBLIC_HOST} (CalDAV)",
        "port": 443,
        "requires_auth": True,
        "auth_hint": "Используйте тот же пароль, что и для почты.",
    }


@app.get("/admin")
@app.get("/admin/{rest:path}")
async def serve_admin(rest: str = ""):
    p = Path(CFG.STATIC_DIR) / "admin.html"
    if p.exists():
        return FileResponse(p)
    return JSONResponse({"error": "Admin panel not available"}, status_code=503)


@app.get("/")
async def serve_index():
    index_path = Path(CFG.STATIC_DIR) / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse({"error": "static/index.html missing"}, status_code=500)


if Path(CFG.STATIC_DIR).exists():
    app.mount("/static", StaticFiles(directory=CFG.STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="RuPochta Webmail runtime helpers")
    parser.add_argument("--reconcile-identities", action="store_true",
                        help="Safely backfill missing ad_login/mail_login into proxy_employees")
    parser.add_argument("--refresh-identity-cache", action="store_true",
                        help="Refresh local employee identity cache from LDAP + mailbox inventory")
    parser.add_argument("--apply", action="store_true",
                        help="Apply changes for helper commands instead of dry-run")
    args = parser.parse_args()
    if args.refresh_identity_cache:
        print(json.dumps(refresh_employee_identity_cache(), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if args.reconcile_identities:
        print(json.dumps(reconcile_employee_identities(apply=args.apply), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    import uvicorn
    uvicorn.run("rupochta_server:app", host=CFG.HOST, port=CFG.PORT, reload=False)
