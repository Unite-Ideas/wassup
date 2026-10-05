# Running Wassup

Phase 0 runs on one machine: Postgres in Docker, Ollama on the host for the GPU, and the Wassup app (pipeline, API and UI) either in Docker or straight from source.

## Windows 11 setup (recommended path)

### 1. One time installs

1. **WSL2 with Ubuntu.** In an admin PowerShell: `wsl --install -d Ubuntu`, then reboot.
2. **Docker Desktop** with the WSL2 backend (Settings, General, "Use the WSL 2 based engine"). Under Settings, Resources, WSL integration, enable your Ubuntu distro.
3. **Give WSL2 room.** Create `C:\Users\<you>\.wslconfig`:
   ```ini
   [wsl2]
   memory=256GB
   processors=48
   ```
   Then `wsl --shutdown` in PowerShell so it takes effect.
4. **Ollama for Windows** from https://ollama.com/download. It runs natively and uses the RTX 6000 directly. Then pull the two models, in PowerShell:
   ```powershell
   ollama pull bge-m3      # embeddings, multilingual, about 1.2 GB
   ollama pull qwen3:8b    # triage second opinion, about 5 GB
   ```
   Optional but useful on this hardware: set the Windows environment variable `OLLAMA_NUM_PARALLEL=8` and restart Ollama, so embeddings and triage calls run side by side.

### 2. Get the code (inside Ubuntu)

Keep the repo in the Linux filesystem, not under `/mnt/c`, because file access across the boundary is slow.

```bash
cd ~
git clone https://github.com/Unite-Ideas/wassup.git
cd wassup
cp .env.example .env
```

Optional: get a free Congress.gov key at https://api.congress.gov/sign-up/ and put it in `.env` as `CONGRESS_API_KEY`. Without it Wassup uses the shared demo key, which is often rate limited. Senate roll call votes work either way.

### 3. Start it

```bash
docker compose up -d --build
```

Open **http://localhost:8000** in your Windows browser.

The first start backfills the last 4 hours of GDELT (about 15,000 articles) plus every RSS feed. With bge-m3 on the GPU that takes a few minutes. Watch progress with:

```bash
docker compose logs -f app
```

From then on new GDELT files arrive every 15 minutes, RSS every 10 minutes, Congress every 30, and the Federal Register hourly.

### Stopping, updating, resetting

```bash
docker compose down              # stop (data is kept in the wassup-db volume)
git pull && docker compose up -d --build   # update
docker compose down -v           # stop AND delete all collected data
```

## Running from source (for development)

Needs `uv` (https://docs.astral.sh/uv/) and Node 20 or newer inside WSL2 or Linux.

```bash
scripts/dev.sh        # Postgres in Docker, pipeline + API + built UI on :8000
scripts/dev.sh ui     # same, plus the Vite dev server with hot reload on :5173
```

Inside WSL2, Ollama on Windows is reachable at `localhost:11434` when WSL uses mirrored networking (the Windows 11 default for new installs). If it is not, set `OLLAMA_HOST=0.0.0.0` in Windows, restart Ollama, and put the Windows IP in `.env` as `OLLAMA_URL`.

Run the tests:

```bash
cd pipeline
.venv/bin/pytest                       # unit tests
WASSUP_TEST_ADMIN_URL=postgresql://wassup:wassup@localhost:5432/postgres .venv/bin/pytest   # plus end to end
```

## The `wassup` command

| Command | What it does |
| --- | --- |
| `wassup dev` | Pipeline and API together (what Docker runs) |
| `wassup run` | Pipeline only, forever |
| `wassup api` | API and UI only |
| `wassup once` | One full pass of every collector and step, then exit |
| `wassup collect gdelt` | One collector once (`gdelt`, `rss`, `congress`, `federal_register`) |
| `wassup retriage` | Re-run triage on every story after editing `config/desks.yaml` or `config/interests.yaml` |
| `wassup rebuild-stories` | Re-cluster everything after changing the embedding model |

In Docker, prefix with `docker compose exec app`, for example `docker compose exec app wassup retriage`.

## Tuning what you see

Everything you are likely to change lives in `config/` and is mounted into the container, so edits apply on restart (`docker compose restart app`), followed by `wassup retriage` for desk or interest changes.

- `config/sources.yaml`: RSS feeds (add as many as you like) and which GDELT themes to ingest.
- `config/desks.yaml`: the desks, their keywords, GDELT themes, countries and colors.
- `config/interests.yaml`: topics that go to cold storage (sports, celebrity, lifestyle) and extra topics that raise relevance.
- `config/outlets.yaml`: trust tiers per web domain, including state media.

Thumbs up and down in the story panel (MORE and LESS) teach the relevance model without editing anything.

## Settings (`.env`)

| Variable | Default | Notes |
| --- | --- | --- |
| `EMBED_BACKEND` | `ollama` | `hash` runs without a model (testing only; no cross language matching) |
| `EMBED_MODEL` | `bge-m3` | Must produce 1024 dimension vectors |
| `TRIAGE_BACKEND` | `hybrid` | `rules`, `hybrid`, or `ollama` |
| `TRIAGE_MODEL` | `qwen3:8b` | Any Ollama model that supports structured output |
| `TRANSLATE_BACKEND` | `ollama` | Translate non English headlines into English, or `off` |
| `TRANSLATE_MODEL` | (triage model) | Any Ollama chat model; qwen3 models translate well |
| `CLUSTER_THRESHOLD` | 0.80 | Cosine similarity for "same story". Lower merges more |
| `LINK_THRESHOLD` | 0.62 | Cosine similarity for "related story" strings |
| `RETENTION_DAYS` | 30 | After this many days, article and story vectors are dropped to save disk (articles themselves are kept). Minimum 15 |
| `GDELT_BACKFILL_FILES` | 16 | 15 minute files fetched on first start |
| `CONGRESS_API_KEY` | (demo key) | Free from api.congress.gov |

## Troubleshooting

- **"Ollama is not reachable"** in the logs: Ollama is not running, or the container cannot reach it. Check `curl http://localhost:11434/api/tags` on Windows. If that works but the container still fails, set `OLLAMA_HOST=0.0.0.0` for Ollama and restart it.
- **"does not have the embedding model"**: run `ollama pull bge-m3`.
- **"stories were built with the 'hash' embedder"**: you switched embedders. Run `docker compose exec app wassup rebuild-stories`.
- **A source shows as failing** (top bar, "Sources up"): see http://localhost:8000/api/sources for the error. Some outlets block certain networks or user agents. Failures never stop the pipeline.
- **The globe is empty**: give the first backfill a few minutes, and check that the time window at the bottom covers now.
