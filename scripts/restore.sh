#!/usr/bin/env bash
# Put the database back the way it was at a backup:
#   bash ~/wassup/scripts/restore.sh                         (lists the backups)
#   bash ~/wassup/scripts/restore.sh wassup-20261009-0330.dump
# Everything Wassup collected after that backup is lost, so it asks first.
set -uo pipefail
cd "$(dirname "$0")/.."

name=${1:-}
if [ -z "$name" ]; then
  echo "Backups (newest first):"
  docker compose exec -T backup sh -c 'ls -1t /backups/wassup-*.dump 2>/dev/null | while read f; do echo "  $(basename "$f")  $(du -h "$f" | cut -f1)"; done'
  echo
  echo "Restore one with:  bash scripts/restore.sh <name>"
  exit 0
fi
name=$(basename "$name")
if ! docker compose exec -T backup test -f "/backups/$name" </dev/null; then
  echo "No backup called $name. Run without a name to list them."
  exit 1
fi

read -r -p "Replace the current database with $name? Anything newer is lost. Type yes: " answer
[ "$answer" = yes ] || { echo "Nothing changed."; exit 0; }

echo "Stopping the app..."
docker compose stop app
echo "Replacing the database (this can take a while for a big backup)..."
docker compose exec -T db psql -U wassup -d postgres -q \
  -c "DROP DATABASE IF EXISTS wassup WITH (FORCE)" -c "CREATE DATABASE wassup"
if docker compose exec -T backup pg_restore -h db -U wassup -d wassup -j 8 --no-owner "/backups/$name"; then
  echo "  [ok]  Restored $name."
else
  echo "  [!!]  pg_restore reported problems (above). Most warnings are harmless; check Wassup works."
fi
echo "Starting the app..."
docker compose up -d app
