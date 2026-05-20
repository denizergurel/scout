# Scout

**Scout compresses hours of newsletter curation into 30 minutes of editorial review.**

Newsletter editors spend most of their week on work that doesn't scale: scanning the RSS firehose, picking what matters, sorting items into sections, and writing summaries in a consistent voice. Scout's agents do the volume; the editor does the judgment. The pipeline pulls your sources daily, filters for relevance, categorizes, and drafts paragraph summaries — then surfaces a ranked lineup in a local dashboard. You triage, refine the spotlight, publish. Past editions feed back as training data for next week.

Built and tuned on a weekly XR / spatial-computing intelligence brief, Scout retargets to any beat through a single config file — sources, sections, editorial voice, even which LLM runs each stage. Runs locally. Free beyond your existing LLM subscription.

## What it does

```
RSS Feeds → Collector → Scout → Editor → Store ⇄ Dashboard → Editions
                                                  ↑
                                            Curator (on-demand)
```

| Stage | What it does | Implementation |
|---|---|---|
| **Collector** | Pulls articles from configured RSS feeds (dedupes against the store) | `src/collector.py` |
| **Scout** | Binary relevance filter — keep this article, or discard? | `src/scout.py` (LLM) |
| **Editor** | Categorizes survivors into newsletter sections and writes a paragraph summary for each | `src/editor.py` (LLM) |
| **Store** | Single persistent JSON store with per-item lifecycle status | `src/store.py` |
| **Dashboard** | Local FastAPI app: triage Signals, build a Lineup, draft a SPOTLIGHT, publish Editions | `src/dashboard.py` |
| **Curator** | On-demand assistant invoked from the Dashboard — drafts the SPOTLIGHT paragraph from the items currently slated | `src/curator.py` (LLM) |

A daily agent runs collect → scout → editor automatically via `launchd`. You can also trigger a refresh from the dashboard's Signals page.

## Why it exists

Manual newsletter curation takes hours of reading + ranking + summarizing. Scout compresses that to ~30 minutes of editorial review on top of pre-filtered, pre-summarized content. The agents handle volume; the human handles judgment.

## LLM providers — model freedom

Configure your model(s) in `config.yaml`'s `llm:` block. Default applies to every agent unless overridden per-stage; the dashboard's Settings page shows which model each stage actually uses.

| Provider | When to use | Auth |
|---|---|---|
| `claude` *(default)* | You have a Claude Pro/Max subscription. Zero cost per call beyond the subscription. | Local `claude` CLI, no API key |
| `openai` | OpenAI directly **or** any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, Together, Groq, DeepSeek, …) via `api_base` override | `OPENAI_API_KEY` env / `api_key` / `api_key_env` |
| `gemini` | Google Gemini | `GEMINI_API_KEY` |

```yaml
# Different models for different work — Scout is high-volume, Editor/Curator quality-critical
llm:
  default: { provider: "openai", model: "gpt-4o-mini" }
  editor:  { provider: "claude" }
  curator: { provider: "claude" }
```

```yaml
# Local Ollama
llm:
  default:
    provider: "openai"
    model: "llama3.1"
    api_base: "http://localhost:11434/v1"
    api_key: "ollama"
```

```yaml
# Groq (super-fast hosted inference)
llm:
  default:
    provider: "openai"
    model: "llama-3.1-70b-versatile"
    api_base: "https://api.groq.com/openai/v1"
    api_key_env: "GROQ_API_KEY"
```

## Stack

- **Python 3.9+**, FastAPI for the review dashboard
- **feedparser + httpx** for RSS ingestion
- **launchd** for daily scheduling on macOS
- **Claude CLI** (npm: `@anthropic-ai/claude-code`) as the default LLM backend, plus optional Gemini / OpenAI via SDKs

## Quick start

```bash
git clone <your-fork-url> scout
cd scout
$EDITOR config.yaml          # set newsletter.name / slug / topics / sources
./install.sh                 # creates venv, installs deps
```

Scout doesn't auto-schedule anything at install. You run the pipeline manually with the **✦ Refresh** button on the Signals page, or enable a daily 8 AM launchd agent from **Settings → Automation** when you're ready (macOS only). The launchd label derives from `newsletter.slug` so multiple newsletter instances coexist on the same machine.

See [SETUP.md](SETUP.md) for the full first-time install, and [NEW_NEWSLETTER.md](NEW_NEWSLETTER.md) for the recipe to point Scout at a different beat.

## Editorial workflow

```bash
.venv/bin/python -m uvicorn src.dashboard:app --reload
# Open http://localhost:8000
```

- **Signals** — fresh items surfaced by the agents. Save what's worth publishing, archive the rest. **+ Add by URL** lets you ingest a one-off story you saw outside your feeds.
- **Lineup** — items you've selected for the next edition. Each is Including or Held; only Including items ship on Publish.
- **Editions** — past published editions.
- **Archive** — items you've set aside; restorable.
- **Settings** — RSS feeds, models in use, editorial pipeline.

## Editorial policy

The default prompts enforce:
- No patent filings
- Always attribute source inline (e.g. *"According to CNET, …"*)
- Each item is a single paragraph (1–3 sentences), no markdown
- Neutral, authoritative tone — NYT, Bloomberg, Reuters, FT

Edit `prompts/*.md` or override via `config.yaml` to retarget for a different editorial voice.

## Project layout

```
scout/
├── src/
│   ├── collector.py            # RSS ingest
│   ├── scout.py                # relevance filter
│   ├── editor.py               # categorize + summarize
│   ├── curator.py              # on-demand spotlight drafter
│   ├── daily.py                # daily agent (collect→scout→editor)
│   ├── dashboard.py            # FastAPI dashboard
│   ├── store.py                # item store + lifecycle
│   ├── prompt_loader.py        # template substitution for prompts
│   └── llm.py                  # provider router (Claude/Gemini/OpenAI)
├── prompts/                    # system prompt templates per agent
├── config.yaml                 # the newsletter identity, sections, feeds
├── output/daily/items.json     # the persistent store (gitignored)
├── output/editions/            # published editions
├── install.sh                  # venv + dependency install
└── com.scout.spatial-report.plist   # launchd template (paths + label substituted at install)
```

## License

MIT. See [LICENSE](LICENSE) if present, otherwise treat as MIT.
