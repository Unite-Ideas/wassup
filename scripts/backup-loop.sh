#!/usr/bin/env bash
# Nightly database backups. Runs inside the backup service (docker-compose.yml), which uses the
# same image as the database so pg_dump always matches it. Writes /backups/wassup-<date>.dump
# (the folder BACKUP_DIR in .env, ./backups by default) at BACKUP_AT (TZ time) and keeps the
# newest BACKUP_KEEP. A backup counts only once it is complete; older ones are removed only
# after a new one succeeds.
#
#   backup now:   bash ~/wassup/scripts/backup.sh
#   restore:      bash ~/wassup/scripts/restore.sh
set -uo pipefail

KEEP=${BACKUP_KEEP:-7}
AT=${BACKUP_AT:-03:30}

dump() {
  local ts tmp out
  ts=$(date +%Y%m%d-%H%M)
  tmp=/backups/.wassup-$ts.dump.partial
  out=/backups/wassup-$ts.dump
  echo "backup: started $(date '+%F %T %Z')"
  if pg_dump -h db -U wassup -d wassup -Fc -Z 5 -f "$tmp"; then
    mv "$tmp" "$out"
    echo "backup: wrote $(basename "$out"), $(du -h "$out" | cut -f1), finished $(date '+%T')"
    ls -1t /backups/wassup-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f --
    echo "backup: keeping $(ls -1 /backups/wassup-*.dump | wc -l) backups, $(du -ch /backups/wassup-*.dump | tail -1 | cut -f1) in all"
  else
    rm -f "$tmp"
    echo "backup: FAILED, older backups kept"
    return 1
  fi
}

rm -f /backups/.wassup-*.partial  # left by a backup cut off by a shutdown
if [ "${1:-}" = once ]; then
  dump
  exit $?
fi
echo "backup: nightly at $AT ($(date +%Z)), keeping the newest $KEEP, in BACKUP_DIR"
while true; do
  now=$(date +%s)
  next=$(date -d "today $AT" +%s)
  [ "$next" -le "$now" ] && next=$(date -d "tomorrow $AT" +%s)
  sleep $((next - now))
  dump || true
done
