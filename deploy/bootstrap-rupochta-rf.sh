#!/usr/bin/env bash
# Turn the existing mail VM into the public рупочта.рф service.
#
# Idempotent: safe to re-run after fixing a step. It renames the host, installs
# the application under /opt/rupochta, wires nginx and systemd, and leaves TLS
# and DNS to the operator — those need the domain to be delegated first.
#
#   sudo ./bootstrap-rupochta-rf.sh                 # install, keep host name
#   sudo ./bootstrap-rupochta-rf.sh --rename        # also rename the host
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
RENAME=0

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --rename) RENAME=1 ;;
    --hostname) shift; NEW_HOSTNAME="${1:?--hostname needs a value}"; RENAME=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
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
ln -sf /etc/nginx/sites-available/rupochta-rf.conf /etc/nginx/sites-enabled/rupochta-rf.conf
rm -f /etc/nginx/sites-enabled/default

if [ ! -d "/etc/letsencrypt/live/$DOMAIN_PUNYCODE" ]; then
  cat <<EOF

TLS certificate for $DOMAIN_PUNYCODE is not present yet, so nginx will not
start with this config. Point the DNS at this host, then run:

  certbot certonly --webroot -w /var/www/certbot \\
    -d $DOMAIN_PUNYCODE -d www.$DOMAIN_PUNYCODE -d mail.$DOMAIN_PUNYCODE

and re-run this script (or just: nginx -t && systemctl reload nginx).
EOF
else
  nginx -t
  systemctl reload nginx
fi

# ------------------------------------------------------------------- start
step "starting RuPochta"
systemctl restart rupochta.service
sleep 2
if curl -fsS http://127.0.0.1:18400/health >/dev/null; then
  echo "health: ok"
else
  echo "health check failed — journalctl -u rupochta -n 50" >&2
  exit 1
fi
curl -fsS http://127.0.0.1:18400/ready >/dev/null \
  && echo "ready:  ok (IMAP and SMTP reachable)" \
  || echo "ready:  NOT ok — the mail path is not answering yet"

cat <<EOF

Installed. Remaining manual steps are in docs/deploy-rupochta-rf.md:
  1. DNS for $DOMAIN_PUNYCODE (A, MX, SPF, DKIM, DMARC, PTR)
  2. TLS certificate (certbot command above)
  3. verify registration: https://$DOMAIN_PUNYCODE/signup
EOF
