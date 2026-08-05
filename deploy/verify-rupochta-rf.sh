#!/usr/bin/env bash
# Prove that рупочта.рф is actually live, from outside the machine.
#
# Checks the things that make a public mail service real, in the order they
# break: DNS, TLS, the app, mail-path readiness, open registration, and the
# records that decide whether outgoing mail lands in the inbox or in spam.
#
#   ./verify-rupochta-rf.sh                       # check the public service
#   ./verify-rupochta-rf.sh --host 127.0.0.1:18400 --no-tls   # check locally
#
# Exit status is non-zero if any required check fails, so CI or a cron job can
# use it as a health gate.

set -uo pipefail

DOMAIN="xn--80a1acdmd4a.xn--p1ai"     # рупочта.рф
HOST=""
SCHEME="https"
FAILED=0
WARNED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) shift; DOMAIN="${1:?--domain needs a value}" ;;
    --host) shift; HOST="${1:?--host needs a value}" ;;
    --no-tls) SCHEME="http" ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

BASE="$SCHEME://${HOST:-$DOMAIN}"

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; WARNED=$((WARNED + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILED=$((FAILED + 1)); }

echo "checking $BASE (domain $DOMAIN)"

# ------------------------------------------------------------------- DNS
echo
echo "DNS"
if command -v dig >/dev/null 2>&1; then
  a_record="$(dig +short A "$DOMAIN" | head -1)"
  [ -n "$a_record" ] && ok "A $DOMAIN -> $a_record" || bad "A $DOMAIN missing — the domain is not delegated"
  mx_record="$(dig +short MX "$DOMAIN" | head -1)"
  [ -n "$mx_record" ] && ok "MX $DOMAIN -> $mx_record" || bad "MX $DOMAIN missing — nobody can send mail here"
  spf="$(dig +short TXT "$DOMAIN" | grep -i 'v=spf1' | head -1)"
  [ -n "$spf" ] && ok "SPF $spf" || warn "no SPF record — outgoing mail will be treated as suspicious"
  dmarc="$(dig +short TXT "_dmarc.$DOMAIN" | head -1)"
  [ -n "$dmarc" ] && ok "DMARC $dmarc" || warn "no DMARC record"
  dkim="$(dig +short TXT "mail._domainkey.$DOMAIN" | head -1)"
  [ -n "$dkim" ] && ok "DKIM key published" || warn "no DKIM record — mail will be filtered"
  if [ -n "$a_record" ]; then
    ptr="$(dig +short -x "$a_record" | head -1)"
    [ -n "$ptr" ] && ok "PTR $a_record -> $ptr" || warn "no PTR record for $a_record"
  fi
else
  warn "dig not installed — skipping DNS checks"
fi

# ------------------------------------------------------------------- TLS
echo
echo "TLS"
if [ "$SCHEME" = "https" ]; then
  if cert="$(echo | timeout 10 openssl s_client -servername "$DOMAIN" -connect "${HOST:-$DOMAIN:443}" 2>/dev/null \
      | openssl x509 -noout -subject -enddate 2>/dev/null)"; then
    ok "$(echo "$cert" | tr '\n' ' ')"
  else
    bad "no usable certificate for $DOMAIN"
  fi
else
  ok "skipped (plain http requested)"
fi

# ----------------------------------------------------------- application
echo
echo "application"
health="$(timeout 10 curl -fsS "$BASE/health" 2>/dev/null)"
if [ -n "$health" ]; then
  ok "/health $health"
else
  bad "/health did not answer — the service is not running"
fi

ready="$(timeout 15 curl -fsS "$BASE/ready" 2>/dev/null)"
if [ -n "$ready" ]; then
  ok "/ready $ready"
else
  bad "/ready did not answer — IMAP or SMTP is unreachable"
fi

signup="$(timeout 10 curl -fsS "$BASE/api/signup/config" 2>/dev/null)"
case "$signup" in
  *'"enabled":true'*)
    ok "registration enabled"
    case "$signup" in
      *'"provisioning_ready":true'*) ok "mailbox provisioning ready" ;;
      *) bad "provisioning not ready — the app cannot reach the local mail server" ;;
    esac
    case "$signup" in
      *"$DOMAIN"*) ok "registration domain is $DOMAIN" ;;
      *) warn "registration domain is not $DOMAIN: $signup" ;;
    esac
    ;;
  *'"enabled":false'*) bad "registration is disabled — set MAIL_PUBLIC_SIGNUP=1" ;;
  *) bad "/api/signup/config did not answer" ;;
esac

status="$(timeout 10 curl -sS -o /dev/null -w '%{http_code}' "$BASE/signup" 2>/dev/null)"
[ "$status" = "200" ] && ok "/signup page served" || bad "/signup returned $status"

# -------------------------------------------------------------- summary
echo
if [ "$FAILED" -gt 0 ]; then
  echo "$FAILED check(s) failed, $WARNED warning(s) — the service is not live yet."
  exit 1
fi
if [ "$WARNED" -gt 0 ]; then
  echo "all required checks passed, $WARNED warning(s) — mail deliverability will suffer until those are fixed."
  exit 0
fi
echo "all checks passed — рупочта.рф is live."
