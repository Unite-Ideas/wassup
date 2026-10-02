#!/usr/bin/env bash
# Run Wassup from source (Linux or WSL2). Postgres runs in Docker, everything else runs here.
#   scripts/dev.sh          pipeline + API + built UI on http://localhost:8000
#   scripts/dev.sh ui       also start the Vite dev server with hot reload on http://localhost:5173
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then set -a; . ./.env; set +a; fi
# Outside Docker, Ollama is on localhost unless told otherwise.
if [[ "${OLLAMA_URL:-}" == *host.docker.internal* ]]; then export OLLAMA_URL=http://localhost:11434; fi

docker compose up -d db
echo "waiting for postgres..."
until docker compose exec -T db pg_isready -U wassup -d wassup >/dev/null 2>&1; do sleep 1; done

if ! command -v uv >/dev/null; then echo "install uv first: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; fi
(cd pipeline && [ -d .venv ] || uv venv -q .venv) && (cd pipeline && uv pip install -q -e ".[dev]")

(cd ui && [ -d node_modules ] || npm ci --no-audit --no-fund)
if [ "${1:-}" = "ui" ]; then
  (cd ui && npm run dev) &
else
  (cd ui && npm run build >/dev/null)
fi

exec pipeline/.venv/bin/wassup dev --host 127.0.0.1 --port 8000
