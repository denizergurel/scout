# Scout — Setup Instructions

## Prerequisites

- macOS with Homebrew installed
- Python 3.9+
- One LLM provider configured (defaults to the Claude CLI via Claude Pro/Max):
  - `claude` (default) — `npm install -g @anthropic-ai/claude-code`, then `claude login`
  - `gemini` — set `LLM_PROVIDER=gemini` + `GEMINI_API_KEY` in `.env`
  - `openai` — set `LLM_PROVIDER=openai` + `OPENAI_API_KEY` in `.env`

## Quick Start

1. Clone your fork and edit `config.yaml` — set `newsletter.name`, `slug`, `topics`, sections, and feed sources to match your newsletter:

```bash
git clone <your-fork-url> scout
cd scout
$EDITOR config.yaml
```

2. Run the installer:

```bash
chmod +x install.sh
./install.sh
```

This will:
- Verify Python is available
- Create a Python virtual environment
- Install dependencies
- Sanity-check your LLM provider

Daily 8 AM automation is **opt-in** — enable it from **Settings → Automation** in the dashboard once you're comfortable with how Scout runs. Until then, click **✦ Refresh** on the Signals page to pull fresh items on demand.

## Daily Use

Start the dashboard:

```bash
.venv/bin/python -m uvicorn src.dashboard:app --reload
```

Then open: http://localhost:8000

## Refreshing Signals

Three ways:

| When | How |
|---|---|
| You're in the dashboard | Hit **✦ Refresh** on Signals |
| You saw a specific story outside your feeds | Click **+ Add by URL** on Signals, paste the link |
| You want a CLI run | `.venv/bin/python src/daily.py` |

To turn on automatic daily runs, open **Settings → Automation** and click *Enable daily refresh*.

## Useful Commands

| Action | Command |
|--------|---------|
| Run agent now | `.venv/bin/python src/daily.py` |
| Start dashboard | `.venv/bin/python -m uvicorn src.dashboard:app --reload` |
| View agent log | `cat output/daily/agent.log` |
| Stop daily agent | `launchctl unload ~/Library/LaunchAgents/com.scout.<your-slug>.plist` |
| Restart daily agent | `launchctl load ~/Library/LaunchAgents/com.scout.<your-slug>.plist` |

(Substitute `<your-slug>` with the `newsletter.slug` value from your `config.yaml`.)

## Your Workflow

1. Agent collects, filters, and categorizes news automatically every morning → items land in **Signals** as `pending`
2. Open the dashboard, triage Signals: **Save** what's worth publishing, **Hide** the rest (they go to **Archive**)
3. In the **Lineup**, fine-tune which items are **Including** in the next edition vs **Held** for a future one
4. Click **✦ Draft with agent** to have the Curator draft your SPOTLIGHT
5. **Publish Edition** → markdown + JSON land in `output/editions/`

## Troubleshooting

- **Agent didn't run?** Check: `launchctl list | grep com.scout`
- **No items showing?** Run manually: `.venv/bin/python src/daily.py` and check `output/daily/agent.log`
- **Claude CLI not found?** Install: `npm install -g @anthropic-ai/claude-code`, then `claude login`
- **Dashboard won't start?** Use the venv's Python directly: `.venv/bin/python -m uvicorn src.dashboard:app`
