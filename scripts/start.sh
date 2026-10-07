#!/usr/bin/env bash
# Start Wassup, for example after restarting the PC:   bash ~/wassup/scripts/start.sh
# Waits for Docker Desktop, starts everything, then checks that each part is working.
set -uo pipefail
cd "$(dirname "$0")/.."

ok()   { echo "  [ok]  $*"; }
bad()  { echo "  [!!]  $*"; }

echo "Starting Wassup..."

# 1. Docker Desktop has to be running on Windows.
if ! docker info >/dev/null 2>&1; then
  echo "  Waiting for Docker Desktop. If it is not open, open it from the Windows Start menu."
  for _ in $(seq 1 60); do
    sleep 5
    docker info >/dev/null 2>&1 && break
  done
fi
if ! docker info >/dev/null 2>&1; then
  bad "Docker Desktop is not running. Open it, wait until it says 'Engine running', then run this again."
  exit 1
fi
ok "Docker Desktop is running"

# 2. Start everything: database, Wassup, and the Paperclip newsroom.
docker compose --profile newsroom up -d ${BUILD:+--build} || { bad "Could not start the containers (see the messages above)."; exit 1; }

# 3. Wait for Wassup to answer (the database may need a minute after a forced stop).
echo "  Waiting for Wassup to come up..."
up=0
for _ in $(seq 1 60); do
  if docker compose exec -T app python -c "import httpx; httpx.get('http://localhost:8000/api/stats', timeout=5).raise_for_status()" >/dev/null 2>&1; then
    up=1; break
  fi
  sleep 5
done
[ $up = 1 ] && ok "Wassup is up" || bad "Wassup did not answer after 5 minutes. Show me: docker compose logs --tail 50 app"

# 4. Ollama runs on Windows, outside Docker. Everything AI needs it.
if docker compose exec -T app python -c "import os, httpx; httpx.get(os.environ.get('OLLAMA_URL', 'http://host.docker.internal:11434') + '/api/tags', timeout=5).raise_for_status()" >/dev/null 2>&1; then
  ok "Ollama is reachable"
else
  bad "Wassup cannot reach Ollama. Start Ollama on Windows (Start menu), then it reconnects on its own."
fi

# 5. Paperclip (the newsroom).
if docker compose --profile newsroom ps --status running paperclip 2>/dev/null | grep -q paperclip; then
  ok "Paperclip newsroom is running"
else
  bad "Paperclip is not running. Show me: docker compose logs --tail 50 paperclip"
fi

echo
echo "Open Wassup:     http://localhost:8000"
echo "Open Paperclip:  http://localhost:3100"
echo "Stop it all:     bash ~/wassup/scripts/stop.sh"
