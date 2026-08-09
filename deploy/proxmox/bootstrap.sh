#!/usr/bin/env bash
# Установка почтового сервера RuПочты внутри гостевой Debian-машины,
# созданной provision-vm.sh. Запускать от root ВНУТРИ VM:
#
#   sudo MAIL_FQDN=mail.example.com MAIL_DOMAIN=example.com \
#        ACME_EMAIL=postmaster@example.com bash bootstrap.sh
#
# Ставит: docker-mailserver (Postfix + Dovecot + Rspamd), сертификат Let's
# Encrypt, RuПочту как systemd-сервис и nginx перед ней. Скрипт идемпотентен —
# повторный запуск обновляет конфиги, не трогая почту, ящики и секреты.
set -euo pipefail

MAIL_FQDN_INPUT="${MAIL_FQDN:?укажите MAIL_FQDN, например MAIL_FQDN=mail.example.com}"
MAIL_DOMAIN_INPUT="${MAIL_DOMAIN:-${MAIL_FQDN_INPUT#*.}}"

# Кириллические домены (рупочта.рф) переводим в punycode: Postfix, Dovecot,
# Let's Encrypt и /etc/hosts понимают только ASCII-имена.
to_punycode() {
  case "$1" in
    *[!\ -~]*)  # есть не-ASCII
      # Конвертация нужна раньше, чем блок установки пакетов ниже: имя хоста
      # участвует во всех последующих шагах. В cloud-образе Debian python3 уже
      # есть (его требует cloud-init), но на урезанном образе — доставим.
      # Вывод apt строго в stderr: stdout этой функции забирает подстановка,
      # и любая строка dpkg оттуда попала бы прямо в имя хоста.
      if ! command -v python3 >/dev/null; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >&2
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
          --no-install-recommends python3 >&2
      fi
      command -v python3 >/dev/null || {
        echo "Для IDN-домена нужен python3 — установите его или задайте имя в punycode" >&2
        exit 1
      }
      printf '%s' "$1" | python3 -c \
        'import sys; print(sys.stdin.read().strip().encode("idna").decode())'
      ;;
    *) printf '%s' "$1" ;;
  esac
}
MAIL_FQDN="$(to_punycode "$MAIL_FQDN_INPUT")"
MAIL_DOMAIN="$(to_punycode "$MAIL_DOMAIN_INPUT")"
# Дальше имя уходит в hostnamectl, /etc/hosts, конфиг nginx и certbot: если в
# него что-то затесалось, отказ здесь понятнее, чем поломка на третьем шаге.
for name in "$MAIL_FQDN" "$MAIL_DOMAIN"; do
  case "$name" in
    *[!a-zA-Z0-9.-]* | "" | .* | *.)
      echo "Не похоже на имя хоста: '$name'" >&2
      echo "Проверьте MAIL_FQDN и MAIL_DOMAIN." >&2
      exit 1 ;;
  esac
done
if [ "$MAIL_FQDN" != "$MAIL_FQDN_INPUT" ]; then
  echo "IDN-домен: $MAIL_FQDN_INPUT → $MAIL_FQDN (домен почты: $MAIL_DOMAIN)"
fi

ACME_EMAIL="${ACME_EMAIL:-postmaster@$MAIL_DOMAIN}"
RUPOCHTA_REPO="${RUPOCHTA_REPO:-https://github.com/lmcorp-it/RuPochta.git}"
RUPOCHTA_REF="${RUPOCHTA_REF:-main}"
# docker-mailserver 15.1.0, закреплён по digest: тег можно переписать в
# реестре, digest — нет. Обновление: сверьте новый релиз на
# github.com/docker-mailserver/docker-mailserver/releases и подставьте его
# digest (docker buildx imagetools inspect ghcr.io/...:<тег>).
DMS_IMAGE="${DMS_IMAGE:-ghcr.io/docker-mailserver/docker-mailserver@sha256:af51b15dd3fc72153c0e90eb7692bb5e3a463212d87959a80fa7aa89b617d44a}"
TZ_NAME="${TZ_NAME:-Europe/Moscow}"
SKIP_TLS="${SKIP_TLS:-0}"           # 1 — пропустить выпуск сертификата (нет DNS/наружного 80)

MAIL_ROOT=/srv/mail/mail-server
APP_ROOT=/opt/rupochta
STATE_DIR=/var/lib/rupochta
ENV_FILE=/etc/rupochta/rupochta.env
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Запускайте от root (sudo)." >&2; exit 1; }

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

subst() {  # subst <шаблон> <куда>
  sed -e "s|__MAIL_FQDN__|$MAIL_FQDN|g" \
      -e "s|__MAIL_DOMAIN__|$MAIL_DOMAIN|g" \
      -e "s|__TZ__|$TZ_NAME|g" \
      "$1" > "$2"
}

# ---------------------------------------------------------------------------
log "Базовые пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl gnupg git rsync jq \
  python3 python3-venv python3-dev build-essential \
  nginx certbot ufw qemu-guest-agent unattended-upgrades
systemctl enable --now qemu-guest-agent >/dev/null 2>&1 || true
timedatectl set-timezone "$TZ_NAME" || true

# ---------------------------------------------------------------------------
log "Имя хоста: $MAIL_FQDN"
hostnamectl set-hostname "$MAIL_FQDN"
# Собственный FQDN резолвим в петлю: веб-интерфейс ходит в IMAP/SMTP по имени
# из сертификата, не выходя в сеть и не завися от hairpin-NAT.
if ! grep -qE "^127\.0\.0\.1[[:space:]]+.*\b${MAIL_FQDN}\b" /etc/hosts; then
  printf '127.0.0.1 %s %s\n' "$MAIL_FQDN" "${MAIL_FQDN%%.*}" >> /etc/hosts
fi

# ---------------------------------------------------------------------------
log "Docker Engine"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.list <<EOF
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable
EOF
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

# ---------------------------------------------------------------------------
log "Сертификат Let's Encrypt для $MAIL_FQDN"
mkdir -p /var/www/html
# Временный сервер только под ACME-челлендж: полный конфиг требует уже
# существующего сертификата, поэтому его подключаем ниже.
cat > /etc/nginx/conf.d/acme-bootstrap.conf <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 404; }
}
EOF
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

if [ "$SKIP_TLS" != "1" ] && [ ! -s "/etc/letsencrypt/live/$MAIL_FQDN/fullchain.pem" ]; then
  certbot certonly --webroot -w /var/www/html \
    -d "$MAIL_FQDN" -m "$ACME_EMAIL" --agree-tos --no-eff-email -n
fi

# Обновление сертификата должно доехать и до nginx, и до Postfix/Dovecot.
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/rupochta.sh <<'EOF'
#!/bin/sh
set -e
systemctl reload nginx || true
docker restart mailserver >/dev/null 2>&1 || true
EOF
chmod +x /etc/letsencrypt/renewal-hooks/deploy/rupochta.sh

# ---------------------------------------------------------------------------
log "docker-mailserver в $MAIL_ROOT"
mkdir -p "$MAIL_ROOT"/{config,mail-data,mail-state,mail-logs}
subst "$SCRIPT_DIR/env/mailserver.env.example" "$MAIL_ROOT/mailserver.env"
cp "$SCRIPT_DIR/docker-compose.mail.yml" "$MAIL_ROOT/compose.yml"
cat > "$MAIL_ROOT/.env" <<EOF
MAIL_FQDN=$MAIL_FQDN
DMS_IMAGE=$DMS_IMAGE
EOF
if [ "$SKIP_TLS" = "1" ]; then
  # Без сертификата DMS не поднимется с SSL_TYPE=letsencrypt.
  sed -i 's|^SSL_TYPE=letsencrypt|SSL_TYPE=|' "$MAIL_ROOT/mailserver.env"
fi
chmod 600 "$MAIL_ROOT/mailserver.env"

cd "$MAIL_ROOT"
docker compose pull
docker compose up -d

log "Жду готовности контейнера mailserver"
for _ in $(seq 1 60); do
  state="$(docker inspect -f '{{.State.Health.Status}}' mailserver 2>/dev/null || echo starting)"
  [ "$state" = "healthy" ] && break
  sleep 5
done
if [ "${state:-}" != "healthy" ]; then
  # Ниже всё опирается на работающий SMTP/IMAP, а нулевой код возврата
  # автоматика примет за успешную установку почтового сервера, которого нет.
  echo "mailserver не перешёл в healthy за 5 минут (состояние: ${state:-неизвестно})." >&2
  echo "Смотрите: docker logs mailserver — и запустите bootstrap.sh снова." >&2
  exit 1
fi

log "Ключ DKIM для $MAIL_DOMAIN"
if ! compgen -G "$MAIL_ROOT/config/rspamd/dkim/*$MAIL_DOMAIN*" >/dev/null; then
  docker exec mailserver setup config dkim domain "$MAIL_DOMAIN" || \
    echo "DKIM не сгенерирован — выполните вручную: docker exec -ti mailserver setup config dkim domain $MAIL_DOMAIN" >&2
fi

# ---------------------------------------------------------------------------
log "RuПочта в $APP_ROOT"
id -u rupochta >/dev/null 2>&1 || useradd --system --home-dir "$APP_ROOT" --shell /usr/sbin/nologin rupochta
usermod -aG docker rupochta
install -d -o rupochta -g rupochta -m 0750 "$STATE_DIR" "$APP_ROOT"

# Каталог принадлежит rupochta, а скрипт работает от root: без safe.directory
# git отказывается с «detected dubious ownership» и повторный запуск ломается.
# Флаг задаётся на вызов, а не глобально в конфиге root.
app_git() { git -c safe.directory="$APP_ROOT/app" -C "$APP_ROOT/app" "$@"; }

if [ -d "$APP_ROOT/app/.git" ]; then
  app_git fetch --depth 1 origin "$RUPOCHTA_REF"
  # -B, а не `checkout FETCH_HEAD`: иначе рабочая копия остаётся в detached HEAD
  # и обещанное в README `git pull` после первого же обновления перестаёт работать.
  app_git checkout -B "$RUPOCHTA_REF" FETCH_HEAD
  app_git branch --set-upstream-to="origin/$RUPOCHTA_REF" \
    "$RUPOCHTA_REF" >/dev/null 2>&1 || true
else
  git clone --depth 1 --branch "$RUPOCHTA_REF" "$RUPOCHTA_REPO" "$APP_ROOT/app"
fi
chown -R rupochta:rupochta "$APP_ROOT/app"

python3 -m venv "$APP_ROOT/venv"
"$APP_ROOT/venv/bin/pip" install --quiet --upgrade pip
"$APP_ROOT/venv/bin/pip" install --quiet -r "$APP_ROOT/app/requirements.txt"
chown -R rupochta:rupochta "$APP_ROOT/venv"

log "Конфигурация RuПочты"
mkdir -p /etc/rupochta
if [ ! -s "$ENV_FILE" ]; then
  tmp_env="$(mktemp)"
  subst "$SCRIPT_DIR/env/rupochta.env.example" "$tmp_env"
  secret="$(openssl rand -base64 48 | tr -d '\n=+/' | cut -c1-48)"
  admin_key="$(openssl rand -hex 24)"
  sed -i -e "s|__WEBMAIL_SECRET_KEY__|$secret|" -e "s|__MAIL_ADMIN_KEY__|$admin_key|" "$tmp_env"
  install -o root -g rupochta -m 0640 "$tmp_env" "$ENV_FILE"
  rm -f "$tmp_env"
else
  echo "$ENV_FILE уже есть — оставляю как есть (секреты не перегенерируются)"
fi

install -m 0644 "$SCRIPT_DIR/systemd/rupochta.service" /etc/systemd/system/rupochta.service
systemctl daemon-reload
systemctl enable --now rupochta
systemctl restart rupochta

# ---------------------------------------------------------------------------
log "nginx перед RuПочтой"
if [ "$SKIP_TLS" = "1" ]; then
  # Без сертификата почта не работает совсем: приложение ходит в IMAP только по
  # TLS (imaplib.IMAP4_SSL), а docker-mailserver поднят без SSL_TYPE. Отдаём
  # интерфейс по HTTP, чтобы установку можно было довести до конца и увидеть,
  # но /ready будет красным, пока не выпущен сертификат.
  cat > /etc/nginx/conf.d/acme-bootstrap.conf <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / {
        proxy_pass http://127.0.0.1:18400;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
  nginx -t && systemctl reload nginx
  echo "SKIP_TLS=1 — интерфейс отдаётся по HTTP без сертификата; почта не заработает,"
  echo "пока домен не делегирован. Делегируйте DNS и перезапустите bootstrap.sh без SKIP_TLS."
else
  rm -f /etc/nginx/conf.d/acme-bootstrap.conf
  subst "$SCRIPT_DIR/nginx/rupochta.conf" /etc/nginx/conf.d/rupochta.conf
  nginx -t && systemctl reload nginx
fi

# ---------------------------------------------------------------------------
log "Файрвол"
# ufw закрывает то, что слушает сам хост (ssh, nginx). Порты контейнера Docker
# публикует в обход ufw — их доступность задана привязками в compose.yml.
ufw allow 22/tcp    comment 'ssh'          >/dev/null
ufw allow 80/tcp    comment 'http/acme'    >/dev/null
ufw allow 443/tcp   comment 'https'        >/dev/null
ufw --force enable  >/dev/null

# ---------------------------------------------------------------------------
log "Готово"
cat <<EOF

Веб-интерфейс:  https://$MAIL_FQDN
Ключ админ-API: grep MAIL_ADMIN_KEY $ENV_FILE

Ящики заводятся с сервера:
  docker exec -ti mailserver setup email add admin@$MAIL_DOMAIN

Админ-панель $MAIL_FQDN/admin просит учётные данные LDAP/AD, и на этой
установке войти в неё пока нельзя: задайте MAILADMIN_LDAPS_* в $ENV_FILE
и перезапустите rupochta.

DNS-записи для домена — выведет:
  MAIL_FQDN=$MAIL_FQDN MAIL_DOMAIN=$MAIL_DOMAIN $SCRIPT_DIR/dns-records.sh

Проверка:
  systemctl status rupochta
  curl -sf http://127.0.0.1:18400/ready && echo IMAP/SMTP OK
  docker exec mailserver setup debug show-mail-logs
EOF
