#!/usr/bin/env bash
# Печатает DNS-записи, которые нужно завести для почтового домена.
# Запускать ВНУТРИ VM после bootstrap.sh (нужен сгенерированный ключ DKIM):
#
#   MAIL_FQDN=mail.example.com MAIL_DOMAIN=example.com ./dns-records.sh
set -euo pipefail

MAIL_FQDN_INPUT="${MAIL_FQDN:?укажите MAIL_FQDN}"
MAIL_DOMAIN_INPUT="${MAIL_DOMAIN:-${MAIL_FQDN_INPUT#*.}}"
MAIL_ROOT="${MAIL_ROOT:-/srv/mail/mail-server}"

# Записи печатаем в punycode: панели некоторых регистраторов принимают
# кириллицу, но ASCII-вид работает везде и совпадает с сертификатом.
to_punycode() {
  case "$1" in
    *[!\ -~]*) printf '%s' "$1" | python3 -c \
        'import sys; print(sys.stdin.read().strip().encode("idna").decode())' ;;
    *) printf '%s' "$1" ;;
  esac
}
MAIL_FQDN="$(to_punycode "$MAIL_FQDN_INPUT")"
MAIL_DOMAIN="$(to_punycode "$MAIL_DOMAIN_INPUT")"
DMARC_RUA="${DMARC_RUA:-postmaster@$MAIL_DOMAIN}"

# При отправке через smarthost письма уходят с чужих адресов, и «v=spf1 mx -all»
# заворачивает собственную же почту в SPF-fail. Смотрим, настроен ли релей.
# `|| true`: при set -o pipefail отсутствующий mailserver.env иначе обрывает скрипт.
relay_host="$(sed -n 's/^RELAY_HOST=//p' "$MAIL_ROOT/mailserver.env" 2>/dev/null | tail -n1 || true)"
relay_host="${RELAY_HOST:-$relay_host}"
if [ -n "$relay_host" ]; then
  spf_mechanisms="mx include:<spf-домен-релея>"
  spf_comment="# SPF: отправка идёт через релей $relay_host — подставьте его механизм
#      вместо <spf-домен-релея> (обычно include:, что именно — смотрите в
#      документации провайдера). Без этого своя же почта получит SPF-fail."
else
  spf_mechanisms="mx"
  spf_comment="# SPF: отправляет только этот сервер, остальное — отклонять.
#      Если позже настроите RELAY_HOST — добавьте сюда механизм релея."
fi

ipv4="$(curl -4 -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo '<внешний-IPv4>')"
ipv6="$(curl -6 -fsS --max-time 5 https://api64.ipify.org 2>/dev/null || true)"

if [ "$MAIL_DOMAIN" != "$MAIL_DOMAIN_INPUT" ]; then
  cat <<EOF
Домен $MAIL_DOMAIN_INPUT записан в punycode как $MAIL_DOMAIN,
сервер — $MAIL_FQDN_INPUT → $MAIL_FQDN.
EOF
fi

cat <<EOF
DNS для $MAIL_DOMAIN (TTL 3600)

# Адрес сервера
$MAIL_FQDN.            A      $ipv4
EOF
[ -n "$ipv6" ] && printf '%-30s AAAA   %s\n' "$MAIL_FQDN." "$ipv6"

cat <<EOF

# Куда доставлять почту домена
$MAIL_DOMAIN.          MX 10  $MAIL_FQDN.

$spf_comment
$MAIL_DOMAIN.          TXT    "v=spf1 $spf_mechanisms -all"

# DMARC: карантин для несовпадений, отчёты на $DMARC_RUA
_dmarc.$MAIL_DOMAIN.   TXT    "v=DMARC1; p=quarantine; rua=mailto:$DMARC_RUA; adkim=s; aspf=s"

# Автонастройка почтовых клиентов (необязательно)
_imaps._tcp.$MAIL_DOMAIN.      SRV 0 1 993 $MAIL_FQDN.
_submission._tcp.$MAIL_DOMAIN. SRV 0 1 587 $MAIL_FQDN.

# Отчёты о TLS-ошибках доставки (необязательно)
_smtp._tls.$MAIL_DOMAIN. TXT  "v=TLSRPTv1; rua=mailto:$DMARC_RUA"
# MTA-STS здесь не публикуется: кроме TXT-записи он требует политику на
# https://mta-sts.$MAIL_DOMAIN/.well-known/mta-sts.txt — включайте осознанно.

# DKIM
EOF

dkim_file="$(find "$MAIL_ROOT/config/rspamd/dkim" "$MAIL_ROOT/config/opendkim/keys/$MAIL_DOMAIN" \
  -maxdepth 1 -type f \( -name '*.public.dns.txt' -o -name '*.public.txt' -o -name 'mail.txt' \) \
  2>/dev/null | grep -F "$MAIL_DOMAIN" | head -n1 || true)"
[ -z "$dkim_file" ] && dkim_file="$(find "$MAIL_ROOT/config/rspamd/dkim" -maxdepth 1 -type f -name '*.txt' 2>/dev/null | head -n1 || true)"

if [ -n "$dkim_file" ]; then
  cat "$dkim_file"
else
  cat <<EOF
Ключ DKIM не найден в $MAIL_ROOT/config. Сгенерируйте его и повторите:
  docker exec -ti mailserver setup config dkim domain $MAIL_DOMAIN
EOF
fi

cat <<EOF

# PTR (обратная зона) — настраивается у провайдера/хостера, не в этой зоне:
$ipv4  ->  $MAIL_FQDN

Проверить после публикации:
  dig +short MX $MAIL_DOMAIN
  dig +short TXT $MAIL_DOMAIN
  dig +short TXT mail._domainkey.$MAIL_DOMAIN
  dig +short -x $ipv4
EOF
