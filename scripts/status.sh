#!/usr/bin/env bash
# How is Wassup doing?   bash ~/wassup/scripts/status.sh          (quick)
#                        bash ~/wassup/scripts/status.sh report   (the full report)
set -uo pipefail
cd "$(dirname "$0")/.."
docker compose --profile newsroom ps
echo
echo "Problems in the last hour:"
docker compose logs --since 1h app 2>/dev/null | grep -E "ERROR|failed" | grep -v "could not embed" | tail -10 || true
echo
last=$(docker compose logs backup 2>/dev/null | grep -E "backup: (wrote|FAILED)" | tail -1 | sed 's/^[^|]*| //')
echo "Last backup: ${last:-none yet (the first runs tonight; bash scripts/backup.sh makes one now)}"
if [ "${1:-}" = report ]; then
  echo
  docker compose exec -T db psql -U wassup -d wassup < scripts/report.sql
fi
