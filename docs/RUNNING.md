# Running Wassup

Phase 0 runs on one machine: Postgres in Docker, Ollama on the host for the GPU, and the Wassup app (pipeline, API and UI) either in Docker or straight from source.

## Everyday commands

Open Ubuntu and run one of these. They work from any folder.

| What | Command |
| --- | --- |
| Start (after a restart of the PC) | `bash ~/wassup/scripts/start.sh` |
| Stop (before a restart of the PC) | `bash ~/wassup/scripts/stop.sh` |
| Get the latest version and restart | `bash ~/wassup/scripts/update.sh` |
| Quick health check | `bash ~/wassup/scripts/status.sh` |
| Full report | `bash ~/wassup/scripts/status.sh report` |
| Back up the database now | `bash ~/wassup/scripts/backup.sh` |
| List backups, or restore one | `bash ~/wassup/scripts/restore.sh` |

`start.sh` waits for Docker Desktop, starts everything, and checks that Wassup, Ollama and the
Paperclip newsroom are all working, telling you what to do about anything that is not.

## Backups

The database is backed up every night at 3:30 (`BACKUP_AT`, in the time zone `TZ`, both in
`.env`), and the newest 7 backups are kept (`BACKUP_KEEP`). Wassup keeps running while it does.
A backup counts only once it is complete, and old ones are removed only after a new one works.
`status.sh` shows the last one.

They go to `~/wassup/backups` unless you set `BACKUP_DIR`. That folder is inside Ubuntu's own
virtual disk, so if that disk is lost the backups go with it. Better, point it at a Windows
drive, ideally a different physical disk, for example in `.env`:

```
BACKUP_DIR=/mnt/d/WassupBackups
TZ=America/Chicago
```

Then `bash ~/wassup/scripts/update.sh`. To restore, run `bash ~/wassup/scripts/restore.sh` to
list the backups and `bash ~/wassup/scripts/restore.sh wassup-20261009-0330.dump` to put one
back. It asks first, because everything collected after that backup is lost. Map files
(`data/maps`) and the Telegram login (`data/telegram`) are not in the database; they can be
downloaded or logged in again.

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

## The MAP view and tracks

The MAP tab is a zoomable map that looks like a globe when zoomed out and goes down to street
level. The map itself (OpenStreetMap data from Protomaps) lives on your PC in `data/maps`, so it
is free, works offline, and nobody sees what you look at. Download it once, from the wassup folder:

```
bash scripts/download_maps.sh
docker compose up -d app
```

That fetches the world down to city level plus street level detail for Ukraine, the Middle East,
and Mexico and Central America: roughly 10 to 20 GB, depending on the day's build. To fetch just
one part: `bash scripts/download_maps.sh ukraine`. To add a region, add a line to `REGIONS` at the
top of the script. Run it again any time to refresh the maps. Until the maps are downloaded, the
MAP tab shows country outlines only.

**Tracks** are things that move over time, drawn on the map. The first is the **Ukraine front
line** from DeepStateMap: the area Russia holds (red), contested areas (grey), directions of
attack (amber arrows), and what changed hands since the previous day, week or month (bright red
taken, blue retaken). The full history back to April 2022 downloads by itself over the first day.
In the tracks panel, drag the slider to any date or press PLAY to watch the front move.

The second kind of track follows a **migrant caravan**. When caravan stories appear, Wassup
starts a track by itself and the local model reads each caravan article for where the group was,
when, and how many people. The map shows the route day by day; the tracks panel lists every
report with the sentence it came from. Click **confirm** on a report you trust (it then wins its
day) or **reject** on a wrong one (it disappears for good). Reports tagged *approx* were placed
from the model's own estimate, *off route* ones disagree with the rest of the path, and
*undated* ones use the article's date.

The third kind is **strikes**, one track for Ukraine and Russia and one for the Middle East.
The local model reads every Telegram post that mentions a strike, an explosion, a drone or a
missile, in any language, and lists each strike it reports: where, when, with what, by whom, what
was hit, and the sentence that says so. Places are looked up in a list of 76,000 villages and towns
in their Latin, Cyrillic, Arabic and Hebrew spellings; a village whose name several places share
is left off unless the post says which province. Reports of the same place within six hours are
one strike. On the map, orange is a hit, grey intercepted, yellow explosions only; bigger means
more reports, a white ring means two or more channels or news outlets reported it, and older
strikes fade. Click one to see who reported it, with links to the posts, and the photos from
those posts (click a photo to open it full size). Photos are saved only for posts that put a strike
on the map, in `data/telegram/media`; for a video only its preview image is kept. Use the 1 DAY, 7 DAYS
and 30 DAYS buttons to choose the span, and **reject** in the panel to hide a wrong one for good.
It reads only Telegram posts from the last 36 hours, two at a time (`STRIKES_PARALLEL` in `.env`;
`STRIKES=off` to stop it).

## Telegram channels and the SOURCES tab

Wassup reads public Telegram channels from their web previews (no account, no login). It starts
with the channels in `config/social.yaml`, about 25 for the Ukraine and Middle East desks, and
posts flow into stories like articles. The **SOURCES** tab lists every channel with its score:

- **confirmed**: share of its posts that two or more independent outlets also reported within 48 hours
- **early**: how many of those it posted at least 10 minutes before the first outlet, and the median lead
- **relevant**: share of its posts on stories your desks track

Every hour the scout re-scores the channels. Channels that the followed ones keep forwarding or
linking to become **candidates**, are watched for a week, then followed if they score well or
dropped if not. Followed channels that score badly for long are paused, then removed. **Pin**
keeps a channel whatever its score, **ban** removes it for good, and you can follow any channel
by typing its name. Scores mean little for the first two days, until posts are old enough to
judge.

**Optional: a logged in Telegram account.** With one, posts arrive the moment they are published,
channels with no public page can be read, and any channel you join on that account (private ones
too) is followed. Use a separate account with its own phone number, not your personal one.

1. Log into **my.telegram.org** with that account, open **API development tools**, and create an
   app (any name). Note the `api_id` and `api_hash`.
2. Add to `.env`: `TELEGRAM_API_ID=...`, `TELEGRAM_API_HASH=...`, `TELEGRAM_PHONE=+15551234567`
3. `bash ~/wassup/scripts/update.sh`, then log in once:
   `docker compose exec app wassup telegram login` and type the code Telegram sends to the account
   (and its two step password, if it has one).

Wassup then joins the followed channels gently, so Telegram does not take a new account for a
spam bot: 8 on the first day (pinned channels and seeds first), 8 more each day after, up to 40 a
day, never more than one every 6 minutes. If Telegram ever asks it to slow down, it stops joining
for at least a day. Joined channels show **live** in the SOURCES tab; the rest are still read from
their public pages meanwhile, so nothing is missed. To watch it:
`docker compose logs --since 1h app | grep -i telegram`. The pace is under `telegram: live:` in
`config/social.yaml`. Wassup only reads: it never posts, never opens files, and joins
channels only, never groups. The login is kept in `data/telegram`; delete that folder to log out.

## Investigations (the INVESTIGATE tab)

For a story you want to get to the bottom of. Press **+ NEW INVESTIGATION**, name it, write what
you want to know (your questions steer everything), paste the links you have (articles, YouTube
videos, Telegram, X and Facebook posts, one per line) and press START. From then on, for as long
as you chose to keep watching (two weeks by default), Wassup:

1. Reads every link: article text, a video's transcript and description, a post. Facebook shows
   only the start of a post without logging in; those are marked **partial**: use **paste its
   text** to give Wassup the whole thing. Use **PASTE TEXT** for anything else you have.
2. Finds related reports it already collected, in any language.
3. Has the local model read each source: how it knows what it says (an eyewitness, someone
   involved, an official statement, a primary document, a reporter's own reporting, or a rewrite
   of someone else's), the evidence it mentions and whether the author saw it, the people and what
   they are said to gain or lose, the claims, the denials, and which links it relies on.
4. Follows those links toward the originals (the **CITED SOURCES** tab): the first report, the
   documents, the post or video it all came from.
5. Searches news worldwide, YouTube and your Telegram channels for more, every six hours.
6. Writes a summary that answers your questions with numbered references to the sources, keeps
   apart what was shown from what was only said, lists the evidence and who holds it, the people
   and their interests as reported, a timeline, how the story came out, where accounts disagree,
   and what is still unknown. It says "not reported" rather than guessing.

**The Investigator** is a newsroom agent of its own (an investigative journalist on Claude Code,
your subscription), separate from the Editor in Chief. Keyword search finds what is already being
said; the Investigator thinks about where the truth would be recorded and goes to look: the
organisations' own sites and statements, their video archives, old versions of pages (the
Internet Archive), nonprofit tax filings, court records, and the open web. It writes **leads**
(what to check and why), follows them, adds what it finds as sources (Wassup then reads and traces
them like yours), marks leads that need a person (a records request, a phone call, a site that
forbids automated searching), and leaves you **notes** answering your questions. It uses public
material only, never logs in or contacts anyone, and looks as hard for what clears someone as for
what accuses them.

It starts by itself once an investigation's first summary is ready, looks again every 12 hours
while new material comes in, and starts now when you press **ASK THE INVESTIGATOR** (add a note to
point it somewhere). You can add your own leads for it. At most 6 runs a day in all
(`investigator:` in `config/newsroom.yaml`). To hire it the first time, after updating:
`docker compose exec app wassup newsroom setup`.

Sources open on **FIRST-HAND**; **ALL, BY DATE** shows how the story spread. **pin** a source to
make sure the summary uses it, **hide** one to leave it out, and **LOOK AGAIN** to search and
summarise again now. The summary is the local model's reading of the sources: check the sources
it cites before relying on it, especially about real people.

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
| `CLUSTER_THRESHOLD` | 0.75 | Cosine similarity for an article to join a story. Lower merges more |
| `MERGE_THRESHOLD` | 0.78 | Cosine similarity at which two whole stories are merged (checked every 5 minutes) |
| `LINK_THRESHOLD` | 0.62 | Cosine similarity for "related story" strings |
| `RETENTION_DAYS` | 30 | After this many days, article and story vectors are dropped to save disk (articles themselves are kept). Minimum 15 |
| `READ_SCOPE` | `tracked` | Read the full text of articles in tracked stories, `all` articles, or `off` |
| `READ_CONCURRENCY` | 32 | Article pages downloading at once (never more than two per website) |
| `READ_WORKERS` | 16 | CPU cores used to pull article text out of pages |
| `NEWSROOM_WORKERS` | 4 | Desk check ins running at once |
| `TRANSLATE_PARALLEL` | 3 | Translation batches sent to the model at once |
| `GDELT_BACKFILL_FILES` | 16 | 15 minute files fetched on first start |
| `CONGRESS_API_KEY` | (demo key) | Free from api.congress.gov |

## Troubleshooting

- **"Ollama is not reachable"** in the logs: Ollama is not running, or the container cannot reach it. Check `curl http://localhost:11434/api/tags` on Windows. If that works but the container still fails, set `OLLAMA_HOST=0.0.0.0` for Ollama and restart it.
- **"does not have the embedding model"**: run `ollama pull bge-m3`.
- **"stories were built with the 'hash' embedder"**: you switched embedders. Run `docker compose exec app wassup rebuild-stories`.
- **A source shows as failing** (top bar, "Sources up"): see http://localhost:8000/api/sources for the error. Some outlets block certain networks or user agents. Failures never stop the pipeline.
- **The globe is empty**: give the first backfill a few minutes, and check that the time window at the bottom covers now.
