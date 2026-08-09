#!/usr/bin/env bash
# Turn the existing mail VM into the public рупочта.рф service.
#
# Idempotent: safe to re-run after fixing a step. It renames the host, installs
# the application under /opt/rupochta, wires nginx and systemd, and leaves TLS
# and DNS to the operator — those need the domain to be delegated first.
#
#   sudo ./bootstrap-rupochta-rf.sh                 # install, keep host name
#   sudo ./bootstrap-rupochta-rf.sh --rename        # also rename the host
#   sudo ./bootstrap-rupochta-rf.sh --tls           # also request the certificate
#
# Without a certificate the site is served over plain HTTP, so that nginx comes
# up at all and the ACME challenge can be answered. Get a certificate as soon
# as the domain resolves here: --tls does it, or run certbot by hand and re-run.
#
# Run it on the mail VM (the one currently answering as mail.lets-mobile.ru),
# from a checkout of this repository.

set -euo pipefail

DOMAIN_PUNYCODE="xn--80a1acdmd4a.xn--p1ai"   # рупочта.рф
NEW_HOSTNAME="mail.${DOMAIN_PUNYCODE}"
APP_DIR="/opt/rupochta"
ENV_FILE="/etc/rupochta/rupochta.env"
STATE_DIR="/var/lib/rupochta"
SERVICE_USER="rupochta"
APP_PORT=18400
CERT_DIR="/etc/letsencrypt/live/$DOMAIN_PUNYCODE"
RENAME=0
OBTAIN_CERT=0
ACME_EMAIL="${ACME_EMAIL:-}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --rename) RENAME=1 ;;
    --hostname) shift; NEW_HOSTNAME="${1:?--hostname needs a value}"; RENAME=1 ;;
    --tls) OBTAIN_CERT=1 ;;
    --acme-email) shift; ACME_EMAIL="${1:?--acme-email needs a value}"; OBTAIN_CERT=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ "$(id -u)" -ne 0 ]; then
  echo "run this with sudo" >&2
  exit 1
fi

step() { printf '\n== %s\n' "$1"; }

# --------------------------------------------------------------- host name
if [ "$RENAME" -eq 1 ]; then
  step "renaming host to $NEW_HOSTNAME"
  old_hostname="$(hostname -f 2>/dev/null || hostname)"
  hostnamectl set-hostname "$NEW_HOSTNAME"
  if ! grep -q "$NEW_HOSTNAME" /etc/hosts; then
    printf '127.0.1.1\t%s %s\n' "$NEW_HOSTNAME" "${NEW_HOSTNAME%%.*}" >> /etc/hosts
  fi
  echo "was: $old_hostname"
  echo "now: $(hostname -f 2>/dev/null || hostname)"
  echo "note: the Proxmox VM name is separate — rename it on the hypervisor:"
  echo "      qm set <vmid> --name rupochta-rf"
fi

# ------------------------------------------------------------------ system
step "installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip nginx certbot python3-certbot-nginx git rsync

# ------------------------------------------------------------- application
step "installing the application into $APP_DIR"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
# Provisioning mailboxes shells into the mail container.
getent group docker >/dev/null 2>&1 && usermod -aG docker "$SERVICE_USER"

mkdir -p "$APP_DIR" "$STATE_DIR" "$(dirname "$ENV_FILE")" /var/www/certbot
rsync -a --delete \
  --exclude '.git' --exclude '.venv' --exclude 'node_modules' --exclude '*.db' \
  "$REPO_DIR"/ "$APP_DIR"/

if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$STATE_DIR"

# ------------------------------------------------------------------ config
step "preparing $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
  install -m 0600 "$REPO_DIR/deploy/rupochta-rf.env.example" "$ENV_FILE"
  secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  admin_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sed -i "s|^WEBMAIL_SECRET_KEY=.*|WEBMAIL_SECRET_KEY=$secret|" "$ENV_FILE"
  sed -i "s|^MAIL_ADMIN_KEY=.*|MAIL_ADMIN_KEY=$admin_key|" "$ENV_FILE"
  echo "generated fresh secrets in $ENV_FILE"
else
  echo "$ENV_FILE already exists — left untouched"
fi
chmod 0600 "$ENV_FILE"

# ------------------------------------------------------------------ systemd
step "installing the systemd unit"
install -m 0644 "$REPO_DIR/deploy/systemd/rupochta.service" /etc/systemd/system/rupochta.service
systemctl daemon-reload
systemctl enable rupochta.service

# -------------------------------------------------------------------- nginx
step "installing the nginx front end"
install -d /etc/nginx/snippets /etc/nginx/conf.d
install -m 0644 "$REPO_DIR/deploy/nginx/snippets/rupochta-proxy.conf" /etc/nginx/snippets/rupochta-proxy.conf
install -m 0644 "$REPO_DIR/deploy/nginx/conf.d/rupochta-limits.conf" /etc/nginx/conf.d/rupochta-limits.conf
install -m 0644 "$REPO_DIR/deploy/nginx/rupochta-rf.conf" /etc/nginx/sites-available/rupochta-rf.conf
install -m 0644 "$REPO_DIR/deploy/nginx/rupochta-rf-bootstrap.conf" /etc/nginx/sites-available/rupochta-rf-bootstrap.conf
rm -f /etc/nginx/sites-enabled/default

# The production config names files under /etc/letsencrypt/live/, so nginx
# refuses to load it before certbot has run — and a refusing nginx serves
# nothing at all, including the ACME challenge that would produce the
# certificate. Serve HTTP only until the certificate exists, then switch.
enable_site() {
  ln -sf "/etc/nginx/sites-available/$1" /etc/nginx/sites-enabled/rupochta-rf.conf
}

if [ -d "$CERT_DIR" ]; then
  enable_site rupochta-rf.conf
  echo "certificate present — serving HTTPS"
else
  enable_site rupochta-rf-bootstrap.conf
  echo "no certificate for $DOMAIN_PUNYCODE yet — serving plain HTTP for now."
  echo "Anything in front of this host must terminate TLS, or mailbox passwords"
  echo "travel in the clear. Re-run with --tls once the DNS points here."
fi

# Always prove the config loads and that nginx is actually up: a front end that
# fails to start is exactly the failure this script used to report as success.
nginx -t
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx

# ------------------------------------------------------------------- start
step "starting RuPochta"

# Anything listening on the application port is, by definition, an older
# RuPochta — usually an earlier install under a different unit name. uvicorn
# would exit with "address already in use" while `systemctl restart` still
# reported success, leaving the stale build serving the public. Hand the port
# over first, and name what was stopped.
port_owner_pid() {
  ss -ltnp 2>/dev/null | awk -v p=":$APP_PORT\$" '$4 ~ p' \
    | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -1
}
unit_of_pid() {   # 0::/system.slice/whatever.service -> whatever.service
  sed -n 's|.*/\([^/]*\.service\)$|\1|p' "/proc/$1/cgroup" 2>/dev/null | head -1
}

squatter="$(port_owner_pid || true)"
if [ -n "${squatter:-}" ]; then
  squatter_cmd="$(tr '\0' ' ' < "/proc/$squatter/cmdline" 2>/dev/null || true)"
  owner_unit="$(unit_of_pid "$squatter" || true)"

  # Stop only something that is demonstrably another copy of this application.
  # A container would publish the port through docker-proxy, and disabling
  # docker.service on this box would take the mail server down with it.
  case "$squatter_cmd" in
    *rupochta_server*|*uvicorn*) mine=1 ;;
    *)                           mine=0 ;;
  esac

  if [ "${owner_unit:-}" = "rupochta.service" ]; then
    :   # our own unit — the restart below replaces it
  elif [ "$mine" -eq 0 ] || [ -z "${owner_unit:-}" ]; then
    echo "port $APP_PORT is held by pid $squatter and this script will not stop it:" >&2
    echo "  unit: ${owner_unit:-none (not managed by systemd)}" >&2
    echo "  cmd:  $squatter_cmd" >&2
    echo "Only a RuPochta instance owned by a unit is taken over automatically." >&2
    exit 1
  else
    echo "port $APP_PORT is held by $owner_unit — an earlier install of this service"
    echo "  cmd: $squatter_cmd"
    echo "stopping and disabling it so this deployment can take the port"
    systemctl disable --now "$owner_unit"
  fi
fi

systemctl restart rupochta.service
sleep 2

# Type=simple means `systemctl restart` returns as soon as the process is
# forked, so a unit that dies immediately — most often because something else
# already holds 18400 — still looks like a clean restart, and the health check
# below happily answers from whatever is really on that port.
if ! systemctl is-active --quiet rupochta.service; then
  echo "rupochta.service is not running after restart:" >&2
  systemctl --no-pager --lines=20 status rupochta.service >&2 || true
  ss -ltnp 2>/dev/null | grep ":$APP_PORT" >&2 || true
  exit 1
fi

if curl -fsS http://127.0.0.1:$APP_PORT/health >/dev/null; then
  echo "health: ok"
else
  echo "health check failed — journalctl -u rupochta -n 50" >&2
  exit 1
fi
curl -fsS http://127.0.0.1:$APP_PORT/ready >/dev/null \
  && echo "ready:  ok (IMAP and SMTP reachable)" \
  || echo "ready:  NOT ok — the mail path is not answering yet"

# /health has answered for every version this service has ever had, so it
# cannot tell a fresh deploy from a stale process still holding the port. Ask
# for a route only current code serves.
if curl -fsS http://127.0.0.1:$APP_PORT/api/signup/config >/dev/null 2>&1; then
  echo "signup: /api/signup/config present"
else
  echo "signup: /api/signup/config is missing — whatever answers on 18400 is" >&2
  echo "        not the build just installed. Open registration would 404." >&2
  ss -ltnp 2>/dev/null | grep ":$APP_PORT" >&2 || true
  exit 1
fi

# --------------------------------------------------------------------- TLS
# Deliberately last: the challenge is served by the nginx started above, which
# is why this could never have worked from the old ordering.
if [ "$OBTAIN_CERT" -eq 1 ] && [ ! -d "$CERT_DIR" ]; then
  step "requesting a TLS certificate"
  cert_names=("$DOMAIN_PUNYCODE")
  for name in "www.$DOMAIN_PUNYCODE" "mail.$DOMAIN_PUNYCODE"; do
    # A name that does not resolve fails the whole order, so ask only for the
    # ones that are actually delegated here.
    if getent hosts "$name" >/dev/null 2>&1; then
      cert_names+=("$name")
    else
      echo "skipping $name — it does not resolve yet"
    fi
  done
  certbot_args=(certonly --webroot -w /var/www/certbot --non-interactive --agree-tos)
  if [ -n "$ACME_EMAIL" ]; then
    certbot_args+=(-m "$ACME_EMAIL")
  else
    certbot_args+=(--register-unsafely-without-email)
  fi
  for name in "${cert_names[@]}"; do certbot_args+=(-d "$name"); done

  if certbot "${certbot_args[@]}"; then
    enable_site rupochta-rf.conf
    nginx -t
    systemctl reload nginx
    echo "certificate installed — now serving HTTPS"
  else
    echo "certbot failed; the site stays on plain HTTP until it succeeds." >&2
    echo "A CDN in front of the origin must forward /.well-known/acme-challenge/" >&2
    echo "to this host without redirecting it to HTTPS." >&2
  fi
fi

echo
if [ -d "$CERT_DIR" ]; then
  echo "Installed, serving HTTPS from this host."
else
  echo "Installed, serving plain HTTP — TLS is still terminated by whatever sits"
  echo "in front of this host. Re-run with --tls to hold the certificate here."
fi
cat <<EOF
Remaining steps are in docs/deploy-rupochta-rf.md:
  1. DNS for $DOMAIN_PUNYCODE (A, MX, SPF, DKIM, DMARC, PTR)
  2. verify registration: https://$DOMAIN_PUNYCODE/signup
EOF
