#!/usr/bin/env bash
# Shared, side-effect-free-until-called helpers for bootstrap-rupochta-rf.sh.
# Kept separate so deployment decisions can be exercised with fake commands.

public_dns_resolves() {
  local name="$1"

  # `getent hosts` is intentionally not used here: NSS reads /etc/hosts, and
  # the bootstrap itself puts mail.<domain> there for local mail delivery.  ACME
  # validates through public DNS, so only actual A/AAAA answers count.
  if dig +short A "$name" 2>/dev/null \
      | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
    return 0
  fi
  dig +short AAAA "$name" 2>/dev/null \
    | grep -Eq '^[0-9A-Fa-f]*:[0-9A-Fa-f:]+$'
}

build_certificate_names() {
  local name

  CERT_NAMES=("$DOMAIN_PUNYCODE")
  for name in "www.$DOMAIN_PUNYCODE" "mail.$DOMAIN_PUNYCODE"; do
    if public_dns_resolves "$name"; then
      CERT_NAMES+=("$name")
    else
      echo "skipping $name — it has no public A/AAAA record yet" >&2
    fi
  done
}

certificate_files_present() {
  [ -s "$CERT_DIR/fullchain.pem" ] && [ -s "$CERT_DIR/privkey.pem" ]
}

install_certbot_deploy_hook() {
  local hook_dir temp_hook

  hook_dir="$(dirname -- "$CERTBOT_DEPLOY_HOOK")"
  temp_hook="${CERTBOT_DEPLOY_HOOK}.tmp.$$"
  if ! install -d -m 0755 "$hook_dir"; then
    echo "could not create the certbot deploy-hook directory: $hook_dir" >&2
    return 1
  fi

  # Certbot runs deploy hooks only after a successful renewal.  Validate the
  # complete nginx configuration first so a bad renewal or unrelated config
  # edit cannot trigger a reload of an invalid configuration.
  if ! (
    umask 022
    printf '%s\n' \
      '#!/usr/bin/env bash' \
      'set -euo pipefail' \
      'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
      'export PATH' \
      'nginx -t' \
      'systemctl reload nginx' > "$temp_hook"
  ); then
    rm -f -- "$temp_hook"
    echo "could not write certbot deploy hook: $CERTBOT_DEPLOY_HOOK" >&2
    return 1
  fi
  if ! chmod 0755 "$temp_hook" || ! mv -f -- "$temp_hook" "$CERTBOT_DEPLOY_HOOK"; then
    rm -f -- "$temp_hook"
    echo "could not install certbot deploy hook: $CERTBOT_DEPLOY_HOOK" >&2
    return 1
  fi
}

set_site_link() {
  local site="$1"

  ln -sfn "$SITE_AVAILABLE_DIR/$site" "$SITE_LINK"
}

restore_site_link() {
  local previous_target="$1"

  if [ -n "$previous_target" ]; then
    ln -sfn "$previous_target" "$SITE_LINK"
  else
    rm -f "$SITE_LINK"
  fi
}

prepare_nginx_site() {
  local previous_target

  previous_target="$(readlink "$SITE_LINK" 2>/dev/null || true)"
  TLS_REPAIR_REQUIRED=0

  if certificate_files_present; then
    if ! set_site_link rupochta-rf.conf; then
      echo "could not enable the production nginx site" >&2
      return 1
    fi
    if nginx -t; then
      echo "certificate present — serving HTTPS"
      return 0
    fi

    if [ "${OBTAIN_CERT:-0}" -ne 1 ]; then
      echo "the existing certificate/configuration does not pass nginx -t." >&2
      echo "Re-run with --tls to attempt certificate repair; refusing an implicit HTTP downgrade." >&2
      restore_site_link "$previous_target" || true
      return 1
    fi

    echo "the existing certificate/configuration does not pass nginx -t;" >&2
    echo "temporarily enabling the bootstrap site for explicit TLS repair." >&2
    if ! set_site_link rupochta-rf-bootstrap.conf || ! nginx -t; then
      echo "the bootstrap nginx site is also invalid; restoring the previous site link." >&2
      restore_site_link "$previous_target" || true
      return 1
    fi
    TLS_REPAIR_REQUIRED=1
    echo "serving bootstrap HTTP until certbot repairs the certificate"
    return 0
  fi

  # A lineage directory with missing/empty live files is not a fresh install:
  # certbot must replace it when the operator explicitly requested TLS rather
  # than treating the incomplete lineage as an unexpired no-op.
  if [ -d "$CERT_DIR" ]; then
    if [ "${OBTAIN_CERT:-0}" -ne 1 ]; then
      echo "the existing certificate lineage is incomplete." >&2
      echo "Re-run with --tls to repair it; refusing an implicit HTTP downgrade." >&2
      return 1
    fi
    TLS_REPAIR_REQUIRED=1
    echo "the existing certificate lineage is incomplete; certbot will repair it" >&2
  fi
  if ! set_site_link rupochta-rf-bootstrap.conf || ! nginx -t; then
    echo "nginx rejected the bootstrap site; restoring the previous enabled site" >&2
    restore_site_link "$previous_target" || true
    return 1
  fi
  echo "no certificate for $DOMAIN_PUNYCODE yet — serving plain HTTP for now."
  echo "Anything in front of this host must terminate TLS, or mailbox passwords"
  echo "travel in the clear. Re-run with --tls once the DNS points here."
}

activate_site_checked() {
  local site="$1"
  local previous_target

  previous_target="$(readlink "$SITE_LINK" 2>/dev/null || true)"
  if ! set_site_link "$site"; then
    echo "could not enable nginx site $site; leaving the previous site unchanged" >&2
    return 1
  fi

  if ! nginx -t; then
    echo "nginx rejected $site; restoring the previous enabled site" >&2
    restore_site_link "$previous_target"
    if [ -n "$previous_target" ] && nginx -t; then
      systemctl reload nginx >/dev/null 2>&1 || true
    fi
    return 1
  fi

  if ! systemctl reload nginx; then
    echo "nginx could not reload $site; restoring the previous enabled site" >&2
    restore_site_link "$previous_target"
    if [ -n "$previous_target" ] && nginx -t; then
      systemctl reload nginx >/dev/null 2>&1 || true
    fi
    return 1
  fi
}

request_tls_certificate() {
  local -a certbot_args
  local name

  build_certificate_names
  certbot_args=(
    certonly
    --webroot -w "$ACME_WEBROOT"
    --non-interactive
    --agree-tos
  )

  # Re-running --tls must let certbot compare and expand the named lineage;
  # merely seeing /etc/letsencrypt/live/<name>/ is not proof that every newly
  # delegated hostname is already covered.
  if [ -d "$CERT_DIR" ]; then
    certbot_args+=(
      --cert-name "$DOMAIN_PUNYCODE"
      --expand
    )
    if [ "${TLS_REPAIR_REQUIRED:-0}" -eq 1 ]; then
      certbot_args+=(--force-renewal)
    else
      certbot_args+=(--keep-until-expiring)
    fi
  fi

  if [ -n "${ACME_EMAIL:-}" ]; then
    certbot_args+=(-m "$ACME_EMAIL")
  else
    certbot_args+=(--register-unsafely-without-email)
  fi
  for name in "${CERT_NAMES[@]}"; do
    certbot_args+=(-d "$name")
  done

  if ! certbot "${certbot_args[@]}"; then
    echo "certbot failed; the currently enabled nginx site was left unchanged." >&2
    echo "A CDN in front of the origin must forward /.well-known/acme-challenge/" >&2
    echo "to this host without redirecting it to HTTPS." >&2
    return 1
  fi
  if ! certificate_files_present; then
    echo "certbot returned success but the certificate files are missing in $CERT_DIR" >&2
    return 1
  fi
  if ! activate_site_checked rupochta-rf.conf; then
    echo "the certificate exists, but nginx stayed on its previous configuration" >&2
    return 1
  fi

  echo "certificate installed — now serving HTTPS"
}

command_runs_rupochta() {
  local command_line="$1"

  [[ " $command_line " =~ [[:space:]]rupochta_server:app([[:space:];]|$) ]]
}

take_over_port_owner() {
  local owner_unit="$1"
  local process_command="$2"
  local unit_exec

  if [ "$owner_unit" = "rupochta.service" ]; then
    return 0
  fi
  case "$owner_unit" in
    *.service) ;;
    *) return 2 ;;
  esac

  unit_exec="$(systemctl show --property=ExecStart --value "$owner_unit" 2>/dev/null || true)"
  if ! command_runs_rupochta "$process_command" || ! command_runs_rupochta "$unit_exec"; then
    return 2
  fi

  systemctl disable --now "$owner_unit"
}
