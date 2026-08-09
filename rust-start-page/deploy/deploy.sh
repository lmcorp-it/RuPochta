#!/usr/bin/env bash
# Deploy script: build → install → (re)start systemd service.
# Usage: sudo ./deploy.sh [DOMAIN]   (default domain: example.com)
set -euo pipefail

DOMAIN="${1:-example.com}"
APP_DIR="/opt/rupochta-rust"
SERVICE="rupochta-web"

echo "==> Building release binary"
cargo build --release --locked

echo "==> Installing to ${APP_DIR}"
install -d "${APP_DIR}"
install -m 0755 target/release/rupochta-web "${APP_DIR}/rupochta-web"

echo "==> Installing systemd unit"
install -m 0644 deploy/rupochta-web.service /etc/systemd/system/${SERVICE}.service
sed -i "s/^Environment=RUPOCHTA_DOMAIN=.*/Environment=RUPOCHTA_DOMAIN=${DOMAIN}/" /etc/systemd/system/${SERVICE}.service

echo "==> (Re)starting service"
systemctl daemon-reload
systemctl enable --now ${SERVICE} || systemctl restart ${SERVICE}
systemctl status ${SERVICE} --no-pager

echo "==> Done. Page:  http://$(hostname -I | awk '{print $1}'):8080  Canvas: /canvas"
