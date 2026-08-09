#!/usr/bin/env bash
# Remove the old lets-mobile mailboxes from docker-mailserver.
#
# Deleting a mailbox destroys its mail. This script therefore lists what it
# would do and stops; it only deletes when run with --apply, and it refuses to
# touch anything that is not on a domain you named.
#
#   ./purge-lets-mobile-mailboxes.sh                       # dry run
#   ./purge-lets-mobile-mailboxes.sh --apply               # delete
#   ./purge-lets-mobile-mailboxes.sh --domain lm.local --apply
#
# Run it on the mail host, as a user in the docker group.

set -euo pipefail

CONTAINER="${MAILSERVER_CONTAINER:-mailserver}"
DOMAINS=()
APPLY=0
BACKUP_DIR="${BACKUP_DIR:-/var/backups/rupochta}"

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --domain) shift; DOMAINS+=("${1:?--domain needs a value}") ;;
    --container) shift; CONTAINER="${1:?--container needs a value}" ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ ${#DOMAINS[@]} -eq 0 ]; then
  DOMAINS=(lets-mobile.ru lets-mobile.online lm.local)
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "mail container '$CONTAINER' not found — set MAILSERVER_CONTAINER" >&2
  exit 1
fi

echo "container: $CONTAINER"
echo "domains:   ${DOMAINS[*]}"

accounts="$(docker exec "$CONTAINER" setup email list 2>/dev/null \
  | sed -n 's/^\* \([^ ]*\).*/\1/p' || true)"

if [ -z "$accounts" ]; then
  echo "no accounts reported by 'setup email list'; nothing to do"
  exit 0
fi

targets=()
for account in $accounts; do
  for domain in "${DOMAINS[@]}"; do
    case "$account" in
      *@"$domain") targets+=("$account") ;;
    esac
  done
done

if [ ${#targets[@]} -eq 0 ]; then
  echo "no mailboxes match those domains; nothing to do"
  exit 0
fi

echo
echo "${#targets[@]} mailbox(es) selected:"
printf '  %s\n' "${targets[@]}"

if [ "$APPLY" -ne 1 ]; then
  echo
  echo "dry run — nothing was deleted. Re-run with --apply to remove these."
  exit 0
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$BACKUP_DIR/lets-mobile-mail-$stamp.tar.gz"
echo
echo "archiving maildirs to $archive before deleting"
for domain in "${DOMAINS[@]}"; do
  docker exec "$CONTAINER" sh -c "[ -d /var/mail/$domain ] && tar -cz -C /var/mail $domain || true"
done > "$archive"
echo "archive written ($(du -h "$archive" | cut -f1))"

for account in "${targets[@]}"; do
  echo "deleting $account"
  docker exec "$CONTAINER" setup email del -y "$account"
done

echo
echo "done. Remaining accounts:"
docker exec "$CONTAINER" setup email list
