#!/usr/bin/env bash
# Stop Wassup cleanly, for example before restarting the PC:   bash ~/wassup/scripts/stop.sh
# Your data is kept. Start again with:   bash ~/wassup/scripts/start.sh
set -uo pipefail
cd "$(dirname "$0")/.."
echo "Stopping Wassup (the database may take up to a minute to finish writing)..."
docker compose --profile newsroom stop
if docker compose --profile newsroom ps --status running -q | grep -q .; then
  echo "  [!!]  Something is still running:"
  docker compose --profile newsroom ps
else
  echo "  [ok]  Everything is stopped. Safe to restart or shut down the PC."
fi
