#!/usr/bin/env bash
# Back up the database now (it also happens every night by itself):
#   bash ~/wassup/scripts/backup.sh
# Wassup keeps running while it does. The file lands in BACKUP_DIR (.env), ./backups by default.
set -uo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T backup bash /scripts/backup-loop.sh once
