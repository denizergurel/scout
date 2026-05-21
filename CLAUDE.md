# Scout

## What This Is
A generic, retargetable newsletter production pipeline. **Scout** is the tool; the **newsletter** it produces is defined entirely in `config.yaml` — name, topics, sections, RSS sources, editorial behavior. Fork it, swap the config, and you have a fresh newsletter scout for any topic area.

This repository is currently configured for **Spatial Report**, a weekly newsletter on AR/VR/MR/XR/AI/Spatial Computing — but nothing in the code is hardcoded to that beat. Change `config.yaml` to retarget. The dashboard, prompts, and Edition output all read the newsletter identity from config at runtime.

> **Setting Scout up for a different newsletter?** See [NEW_NEWSLETTER.md](NEW_NEWSLETTER.md) for the step-by-step.

## Architecture

Scout uses a **per-item lifecycle model** backed by a single persistent store. The daily cron grows a pool of `pending` items; the editor moves items through states via the dashboard.

```
RSS feeds ─► Collector ─► Scout ─► Editor ─► store (status=pending)
                                                │
                                  Inbox view ◄──┤
                                       │
                                  Editor triages: approve / reject
                                       │
                                       ▼
                                   approved (the Bucket — persists across editions)
                                       │
                                  Bucket view: composition + export
                                       │
                                       ▼
                                   published (archived as an Edition)
```

### Item lifecycle (the core abstraction)

Four user-facing states, mapping to four nav tabs:

| Tab | Status (internal) | Meaning |
|---|---|---|
| **Signals** | `pending` | Incoming candidates — newly surfaced by the agents, awaiting editor triage |
| **Lineup** | `saved` | Editorially selected for some future edition |
| **Editions** | `published` | Shipped in a dated Edition; terminal |
| **Archive** | `hidden` | Inactive but retained; restorable to Signals |

(plus internal `aged_out` for the future background sweep)

Each Lineup item also has an `included_in_next: bool` flag (default `True` on Save):
- `True` = **Including** in the next edition
- `False` = **Held** for a future edition (stays in Lineup, not shipped this time)

**Lineup persists across editions.** Publishing only ships Including items; Held items remain in Lineup for a future edition.

### Pipeline stages
1. **Collector** (`src/collector.py`) — Pulls RSS feeds, extracts articles, dedups against existing store entries
2. **Scout** (`src/scout.py`) — Relevance filter via the configured LLM
3. **Editor** (`src/editor.py`) — Categorizes into newsletter sections, writes paragraph summaries
4. **Store** (`output/daily/items.json`) — Single source of truth; cron writes `pending`, dashboard mutates status
5. **Dashboard** (`src/dashboard.py`) — Four lifecycle views (plus Settings):
   - **Signals** (`/`) — `pending` items awaiting triage; Save · Hide · Edit. Header has a ✦ Refresh button to run the pipeline on demand.
   - **Lineup** (`/lineup`) — `saved` items with the per-item Including ⇄ Held toggle; Publish Edition ships only Including items
   - **Editions** (`/editions`) — published editions; auto-populated on Publish, also accepts manually pasted past editions via `/editions/new`
   - **Archive** (`/archive`) — `hidden` items with Restore action
   - **Settings** (`/setup`) — RSS Feeds editor (inline edit, add, remove — writes back to `config.yaml` while preserving comments), read-only Models panel showing the effective LLM per stage, an Editorial pipeline table, and first-time install instructions below.

### Daily Agent
- `src/daily.py` — Runs collect → scout → editor on each cron firing
- Appends new items to the store as `pending`
- Skips items already present in the store (dedup by link + title)
- No weekly reset — the store grows monotonically; aging happens per item, not per calendar week

### Curator's role
The original `src/curator.py` (LLM-based weekly compiler) is **not part of the auto-pipeline** in the new model. Composition is the editor's job in the Saved view. The Curator agent is retained as an **on-demand assistant** invoked from Saved (✦ Draft with agent → drafts the SPOTLIGHT from the items currently Including; future: suggest ordering, flag near-duplicates) — never auto-run.

### Editions on publish
When the editor clicks Publish Edition from the Lineup:
1. Lineup items with `included_in_next=True` (Including) flip to `published` and get stamped with `edition_id`
2. Lineup items with `included_in_next=False` (Held) **stay in the Lineup** for a future edition
3. A new Edition record (`{ date, title, item_ids, markdown }`) is written to `output/editions/`
4. `output/editions/` becomes the source of truth for "what we've already published" — feeding future dedup and (eventually) Scout's feedback loop

> Internal note: items hidden by the editor live as `status=hidden` in `items.json`. They surface in the **Archive** tab in the UI. There is no `output/archive/` directory — that path was renamed to `output/editions/` to remove the naming collision.

### Key Files
- `config.yaml` — **The newsletter identity, sections, editorial policy, and RSS sources.** This is the only file you edit to retarget Scout to a new topic. Top-level `newsletter:` block defines `name`, `slug`, `cadence`, `topics` — all flow into the agents via prompt templates.
- `src/prompt_loader.py` — Reads `prompts/*.md` and substitutes `{{newsletter_name}}`, `{{cadence}}`, `{{topics}}` from config. Prompts ship as templates.
- `src/llm.py` — LLM provider router (Claude / Gemini / OpenAI), selected via `LLM_PROVIDER` env var
- `src/claude_cli.py`, `src/gemini_client.py`, `src/openai_client.py` — provider implementations
- `src/store.py` — Persistent item store (read/write/status mutations)
- `prompts/` — Agent system prompts (scout.md, editor.md, curator.md) — templated with `{{placeholders}}`
- `output/daily/items.json` — The single store (gitignored)
- `output/daily/` — Intermediate per-stage outputs for debugging (gitignored, regenerated)
- `output/editions/` — Published editions (kept in git; training data + dedup source)
- `com.scout.spatial-report.plist` — launchd template; the dashboard's Automation panel (Settings) substitutes `__PROJECT_DIR__`, `__PLIST_LABEL__` (derived from `newsletter.slug`), and `__HOUR__` / `__MINUTE__` (from `automation.schedule_time` in `config.yaml`) when the user clicks Enable or changes the run time

### LLM Providers
The agents (Scout, Editor, Curator) call `llm.call_llm_json()`, which resolves settings from the `llm:` block in `config.yaml`. Each call passes a `stage` hint (`scout` / `editor` / `curator`) so different stages can use different models.

```yaml
# Simplest — one provider for everything (current default)
llm:
  default:
    provider: "claude"            # claude | openai | gemini

# Per-stage routing — cheap model on high-volume Scout, quality on Editor/Curator
llm:
  default:
    provider: "openai"
    model: "gpt-4o-mini"
  editor:
    provider: "claude"            # Claude Pro/Max via local CLI
  curator:
    provider: "claude"

# Ollama (local), via OpenAI-compatible /v1 endpoint
llm:
  default:
    provider: "openai"
    model: "llama3.1"
    api_base: "http://localhost:11434/v1"
    api_key: "ollama"

# Groq (fast hosted inference)
llm:
  default:
    provider: "openai"
    model: "llama-3.1-70b-versatile"
    api_base: "https://api.groq.com/openai/v1"
    api_key_env: "GROQ_API_KEY"
```

Provider client modules: `src/claude_cli.py`, `src/openai_client.py` (accepts `api_base`/`api_key` for any OpenAI-compatible endpoint), `src/gemini_client.py`. The `LLM_PROVIDER` env var is honored as a back-compat fallback when `config.yaml` has no `llm:` block.

The Settings page in the dashboard shows a read-only "Models in use" panel reflecting the effective provider/model for each stage.

## Newsletter Sections
- HIGHLIGHTS
- VISION PRO
- APP STORE HIGHLIGHTS
- GAMING MARKETPLACE
- INVESTMENT
- SOFTWARE
- HARDWARE
- AI AND SIMULATED WORLDS
- RESEARCH
- OPINION

SPOTLIGHT is a 2-3 sentence summary paragraph, not a section with items.

## Editorial Policy (STRICT)
- **NEVER** include patent filings
- VISION PRO section: usage, apps, workflows, and notable reports
- **Always** attribute source inline ("According to CNET,", "Reuters reports")
- **Format**: Each item is a self-contained paragraph (1-3 sentences), no markdown
- **Tone**: Neutral, authoritative — NYT, Bloomberg, Reuters, FT. No hype.

## Tech Stack
- Python 3.9+
- feedparser, httpx — RSS collection
- LLM: Claude CLI by default; Gemini or OpenAI optional (see LLM Providers above)
- FastAPI + uvicorn — dashboard
- pyyaml — config
- launchd — daily scheduling on macOS

## Setup

```bash
# 1. Clone the repo
git clone <your-fork-url> scout
cd scout

# 2. Edit config.yaml — set newsletter.name / slug / topics / sources to taste

# 3. Install Scout
./install.sh
```

`install.sh` creates the venv and installs deps. Daily automation is opt-in from **Settings → Automation** in the dashboard — when enabled, it registers a launchd agent labeled `com.scout.<slug>` (from `newsletter.slug`) so multiple Scout newsletters coexist on the same Mac.

### Manual setup (if you prefer)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Commands

```bash
# Activate environment
source .venv/bin/activate

# Run daily agent manually (collect → scout → editor → store as pending)
python src/daily.py

# Run individual stages (write to per-stage debug files in output/daily/)
python src/collector.py
python src/scout.py
python src/editor.py

# Start dashboard for editorial review
python -m uvicorn src.dashboard:app --reload
# Open http://localhost:8000
```

## Editorial Workflow

The daily agent runs automatically at 8am, growing the `pending` pool.

**Triage (whenever, ideally daily):**
1. Open dashboard at http://localhost:8000 — the Inbox shows all `pending` items
2. Approve newsletter-worthy items → they move to the Bucket
3. Reject the rest

**Newsletter production (your usual day):**
1. Open `/bucket` — every `approved` item across all sections, all visible at once
2. Final review: drop, reorder, edit summaries, write SPOTLIGHT
3. Click "Export Newsletter" → exported items flip to `published` and land in `output/archive/` as an Edition record
4. Unselected approved items stay in the Bucket for next time

## Dashboard Pages
- **/** — Signals: triage `pending` items (Save / Hide / Edit), ✦ Refresh button
- **/lineup** — Lineup: `saved` items with Including ⇄ Held toggle, SPOTLIGHT editor, Publish Edition (alias: `/saved`, `/bucket`)
- **/editions** — Editions: browse published editions
- **/editions/new** — Manually paste a past edition (mostly legacy)
- **/editions/{id}** — View a specific edition
- **/archive** — Archive: `hidden` items with Restore action (alias: `/hidden`)
- **/setup** — Settings: RSS Feeds editor, Models panel, Editorial pipeline table, first-time install guide

Legacy URLs `/archive/new` and `/archive/{id}` 301 redirect to the `/editions/*` equivalents.

## Key API Endpoints
- `GET /api/refresh/status` · `POST /api/refresh` — kick off `daily.py` as subprocess, polled by the UI
- `GET /api/feeds` · `POST /api/feeds` — read/write the `sources:` block in `config.yaml`
- `POST /api/items/{id}/{save,hide,remove,restore,include,hold,edit}` — lifecycle mutations
- `POST /api/bucket/spotlight` · `POST /api/bucket/draft-spotlight` — manual edit / agent-draft of the SPOTLIGHT
- `POST /api/export` — publish the next Edition (ships only Including items)

## Useful Commands
```bash
# View agent logs
cat output/daily/agent.log

# Stop the daily agent
launchctl unload ~/Library/LaunchAgents/com.scout.<your-slug>.plist

# Restart the daily agent
launchctl unload ~/Library/LaunchAgents/com.scout.<your-slug>.plist
launchctl load ~/Library/LaunchAgents/com.scout.<your-slug>.plist

# Test a run manually
python src/daily.py
```

## Development Notes
- This project doubles as a documented learning journey (see JOURNEY.md)
- Keep code simple and readable
- Default provider: install Claude CLI with `npm install -g @anthropic-ai/claude-code` and auth against your Claude Pro/Max subscription
- To switch providers, set `LLM_PROVIDER` in `.env` and `pip install` the SDK you need (`google-generativeai` for gemini, `openai` for openai)
