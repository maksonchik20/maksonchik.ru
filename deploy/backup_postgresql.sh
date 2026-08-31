#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR=/srv/maksonchik/backups/postgresql
CONTAINER=who-update-postgresql
RETENTION_DAYS=14
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TMP_PATH="${BACKUP_DIR}/maksonchik-${STAMP}.dump.tmp"
FINAL_PATH="${BACKUP_DIR}/maksonchik-${STAMP}.dump"

install -d -o root -g root -m 0700 "${BACKUP_DIR}"

cleanup() {
    rm -f "${TMP_PATH}"
}
trap cleanup EXIT

docker exec "${CONTAINER}" bash -lc \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump --host 127.0.0.1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --format=custom --compress=zstd:6 --no-owner --no-privileges' \
    > "${TMP_PATH}"

test -s "${TMP_PATH}"
docker exec -i "${CONTAINER}" pg_restore --list < "${TMP_PATH}" > /dev/null
mv "${TMP_PATH}" "${FINAL_PATH}"
chmod 0600 "${FINAL_PATH}"
find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'maksonchik-*.dump' -mtime "+${RETENTION_DAYS}" -delete

trap - EXIT
printf '%s\n' "${FINAL_PATH}"
