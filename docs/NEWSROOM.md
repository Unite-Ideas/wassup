# The newsroom (Phase 1)

Phase 0 is the wire service: Wassup collects, groups, sorts and maps the news on its own. The
newsroom adds the staff. A team of AI agents, organized and scheduled by
[Paperclip](https://github.com/paperclipai/paperclip), reads what Wassup collects, decides what
matters, writes briefs, and draws the connections across desks.

## Who is on staff

| Agent | Runs on | What it does |
| --- | --- | --- |
| **Editor in Chief** | Claude (through Claude Code, inside Paperclip) | Writes the daily brief each morning and the standup write up each afternoon, draws connections across desks, assigns desks questions, and decides when a breaking story deserves a surge agent. |
| **Desk agents**, one per desk in `config/desks.yaml` | Your local model (Ollama, on your GPU) | Check in every hour, staggered so they take turns on the GPU. Each check in: review the stories that moved, pick what matters and what to follow, write briefs, draw reasoned connections to stories on other desks, and send misrouted stories to cold storage. Answer questions the Editor in Chief assigns. |
| **Surge agents** | Your local model | Hired for one fast moving story (an invasion, a coup, a summit happening now). Check in every 10 minutes, then retire on their own once the story goes quiet. At most 5 at a time. |

Everything they produce shows up in Wassup:

- the **NEWSROOM** tab: the daily brief, the latest standup, every agent and what it last did, and a live activity feed;
- the **story panel**: desk briefs and which agents follow the story;
- the **globe and the evidence board**: connections drawn by the newsroom are **gold**, with the reason on hover.

And in Paperclip (http://localhost:3100): the org chart, each check in as a task the agent
closed with its report, the Editor in Chief's work, budgets, and run history.

## How the pieces fit

```
 Wassup pipeline                Paperclip                          Wassup
 (no AI, nonstop)               (org chart, schedules)             (agent runtime, local model)
 ─────────────────              ─────────────────────              ─────────────────────────────
 collect, group, sort  ───────▶ "Check in: Russia desk" task ───▶  desk reviews its queue,
 breaking story found  ───────▶ "Breaking: ..." task to EiC        writes briefs and links,
                                 Editor in Chief (Claude) ◀──────  reports on the task, closes it
                                 asks Wassup for a surge  ───────▶ Wassup hires it, enforces caps,
                                                                   retires it when the story cools
```

Wassup owns the surge lifecycle, so the caps in `config/newsroom.yaml` hold no matter who asks.

## Setting it up

You need the Phase 0 setup running first (see [RUNNING.md](RUNNING.md)).

### 1. Settings

Add these to `~/wassup/.env`:

```bash
# A random secret for Paperclip logins:
BETTER_AUTH_SECRET=paste-the-output-of: openssl rand -hex 32

# The Editor in Chief runs on Claude. Pick ONE:
CLAUDE_CODE_OAUTH_TOKEN=...   # your Claude subscription: run `claude setup-token` on any machine with Claude Code
ANTHROPIC_API_KEY=...         # or pay per use, from console.anthropic.com
```

Generate the secret in Ubuntu with `openssl rand -hex 32` and paste the result.

### 2. Start Paperclip

```bash
cd ~/wassup
git pull
docker compose --profile newsroom up -d --build
```

The first build compiles Paperclip from source, which takes a while (often 10 to 20 minutes). Later
starts are quick. From now on, use `docker compose --profile newsroom up -d` to start everything.

### 3. Create your Paperclip account

Open http://localhost:3100 and sign up. The first account becomes the administrator.

### 4. Connect Wassup to Paperclip

```bash
docker compose exec app wassup newsroom connect
```

It prints a link. Open it in your browser (while signed in to Paperclip) and click **Approve**. This
gives Wassup operator access so it can build and manage the newsroom.

### 5. Build the newsroom

```bash
docker compose exec app wassup newsroom setup
```

This creates the Wassup company in Paperclip with the Editor in Chief, one agent per desk, the hourly
check ins, the daily brief (6:45) and the afternoon standup (4:50). It is safe to run again: after
editing `config/newsroom.yaml` or `config/desks.yaml`, run it again to apply the changes.

Within the hour every desk has checked in. To see it work right away, press **CALL STANDUP** on the
NEWSROOM tab: every desk reports, then the Editor in Chief writes it up.

## Day to day

- **Change times, caps, or models:** `config/newsroom.yaml`, then `docker compose restart app` and
  `docker compose exec app wassup newsroom setup`.
- **Add or remove a desk:** edit `config/desks.yaml`, then the same two commands. Removed desks are retired.
- **Ask a desk something:** in Paperclip, create a task assigned to that desk with your question. It
  answers from Wassup's coverage (cold storage included) on the spot and closes the task.
- **Pause the newsroom:** pause the agents in Paperclip, or stop it with `docker compose stop paperclip`.
  The Phase 0 pipeline keeps running either way.
- **Status from the command line:** `docker compose exec app wassup newsroom status`.

## Costs

- Desk and surge agents run on your local model: no API cost.
- The Editor in Chief runs on Claude a few times a day (the daily brief, the standup, and breaking
  stories, at most 8 a day by default). With a Claude subscription token that uses your plan; with an
  API key, set a monthly budget for the Editor in Chief in Paperclip (Agents, Editor in Chief, Budget).
- Jev, if configured, is used by triage, not by the newsroom, and is capped per day separately.

## Troubleshooting

- **`connect` says it cannot reach Paperclip:** check `docker compose --profile newsroom ps` shows
  `paperclip` running, and `docker compose logs paperclip` for errors (an empty `BETTER_AUTH_SECRET` is
  the usual one).
- **The approval link does not load:** make sure you are signed in to Paperclip at http://localhost:3100
  in the same browser, then open the link again. It expires after 10 minutes; run `connect` again.
- **Desks show "overdue" on the NEWSROOM tab:** check their recent runs in Paperclip. A desk's run fails
  if Ollama is not reachable; the error is posted on its check in task.
- **The Editor in Chief's runs fail:** in Paperclip open the Editor in Chief and use "Test environment";
  usually the Claude token or key is missing or expired.
