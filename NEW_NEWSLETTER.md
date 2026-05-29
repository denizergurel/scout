# Make a new newsletter with Scout

This is the recipe for pointing Scout at a new beat — say, an AI brief instead of a spatial-computing one — without touching code.

Two paths:

| Path | When |
|---|---|
| **Fork on GitHub, clone fresh** | The new newsletter is its own long-running project with its own remote. |
| **Local clone alongside an existing instance** | You want to run two newsletters from one Mac for now. |

Either way, each newsletter is an independent Scout instance with its own RSS sources, item store, dashboard, and launchd agent. They coexist because the install script derives the launchd label from `newsletter.slug` in your config.

---

## 1. Get a fresh copy

```bash
cd ~/your/projects
git clone https://github.com/denizergurel/scout.git scout-ai
cd scout-ai
```

Pick a folder name that matches your newsletter. Keep it lowercase — it becomes part of paths.

## 2. Set the newsletter identity

Open `config.yaml`. The top block is the only thing every newsletter must edit:

```yaml
newsletter:
  name: "AI Brief"                                              # Display name
  slug: "ai-brief"                                              # Used for launchd label + filenames; lowercase, no spaces
  cadence: "weekly"                                             # Or "daily", "biweekly", etc.
  topics: "frontier AI models, ML infrastructure, applied AI, and AI research"
```

`topics` flows directly into every agent's system prompt — make it a one-line description of what the newsletter covers. Be specific enough that the relevance filter can do its job.

## 3. Define your sections

Replace `newsletter_section_order` with the table of contents your edition will use, in publication order:

```yaml
newsletter_section_order:
  - "HIGHLIGHTS"
  - "FRONTIER MODELS"
  - "INFRASTRUCTURE"
  - "APPLIED AI"
  - "INVESTMENT"
  - "OPEN SOURCE"
  - "RESEARCH"
  - "POLICY & GOVERNANCE"
  - "OPINION"
```

Then update the `categories:` block to match — each section name needs a corresponding entry with a description and keyword hints. Copy the existing structure, swap the words:

```yaml
categories:
  - name: "FRONTIER MODELS"
    description: "Major releases and updates from frontier AI labs (OpenAI, Anthropic, Google DeepMind, xAI, Meta AI, Mistral)."
    keywords: ["GPT", "Claude", "Gemini", "Llama", "frontier model", "model release"]
  # …one entry per section
```

The keywords aren't strict rules — they're hints the Editor uses to bias categorization when the article itself is ambiguous.

## 4. Swap the RSS sources

Replace `sources:` with feeds for your beat:

```yaml
sources:
  - name: "MIT Tech Review - AI"
    url: "https://www.technologyreview.com/feed/"
    source_type: "Mainstream Press"
    category_hint: "Applied AI"

  - name: "The Decoder"
    url: "https://the-decoder.com/feed/"
    source_type: "Trade Press"
    category_hint: "Frontier models"

  - name: "arXiv cs.AI"
    url: "https://rss.arxiv.org/rss/cs.AI"
    source_type: "Research"
    category_hint: "Research"

  # …add more
```

You can also edit feeds from the dashboard's **Settings** page after installing — the editor writes back to `config.yaml` preserving comments.

### A note on `source_type` (the group label)

`source_type` is a free-form string. Whatever you type becomes a subheader in the Settings → RSS Feeds panel — there's no fixed taxonomy, no enum, nothing in the code that requires specific words. Same way you pick your own RSS feeds, you pick your own group names.

The defaults shipped with Scout (Platform Owners, Trade Press, Mainstream Press, Independent Voices, Research, Community) make sense for a tech/XR newsletter. For a different beat, your groups will look different:

| Beat | Groups that might make sense |
|---|---|
| **Music** | Streaming Platforms · Labels · Trade Press · Mainstream · Critics · Academic Journals · Forums |
| **Medicine** | Regulators (FDA, EMA) · Pharma · Trade Press · Mainstream · Practitioner Voices · Journals · Communities |
| **Climate policy** | Government Agencies · NGOs · Trade Press · Mainstream · Scientist Voices · Journals · Activism |
| **Finance** | Exchanges · Banks/Firms · Trade Press · Mainstream · Indie Analysts · Academic · Subreddits |

The pattern is the same — **what kind of source is this?** — even though the labels change. Group by source *type*, not topic. Topic routing happens via `category_hint` (which feeds into the editor's section assignment).

Practical tip: when in doubt, three buckets cover most of what you'll start with — **first-party** (the people/orgs being covered), **press** (anyone writing news about the beat), and **research** (papers, journals). You can split further as your feed list grows.

## 5. (Optional) Tune the relevance filter

Open `prompts/scout.md`. The defaults are written for general newsletter editorial; if your beat has specific things to exclude, add them under the DISCARD section:

```markdown
## Relevance Criteria — DISCARD if:
- Funding rounds under $50M (too noisy for our readership)
- Crypto / blockchain news (unless directly AI-related)
- Product launches with no third-party verification
```

Same for `prompts/editor.md` (tone) and `prompts/curator.md` (SPOTLIGHT voice) if your editorial style differs from the defaults.

The `{{newsletter_name}}`, `{{cadence}}`, `{{topics}}` placeholders in the prompts get filled in from `config.yaml` at runtime — don't change those.

## 6. (Optional) Route different stages to different LLMs

The default `llm:` block uses Claude for everything. If your beat is high-volume (many feeds, many items per day), routing Scout to a cheaper model saves cost without hurting editorial quality:

```yaml
llm:
  default:
    provider: "claude"                # Editor + Curator stay on quality model
  scout:
    provider: "openai"
    model: "gpt-4o-mini"              # Cheap, fast filter for high volume
```

Or use a local model via Ollama (no API key needed):

```yaml
llm:
  default:
    provider: "openai"
    model: "llama3.1"
    api_base: "http://localhost:11434/v1"
    api_key: "ollama"
```

See README for more LLM examples.

## 7. Install Scout

```bash
./install.sh
```

This builds the venv and installs dependencies — no scheduling yet. Daily automation is opt-in from the dashboard's **Settings → Automation** panel; until you enable it, you'll refresh manually using the **✦ Refresh** button on Signals (or **+ Add by URL** for one-off stories you saw outside your feeds).

When you do enable automation, it registers a launchd agent labeled `com.scout.<slug>` — so multiple Scout newsletters coexist on the same Mac with separate schedules.

## 8. Run the dashboard on a free port

**If this is your only Scout instance**, the default works:

```bash
.venv/bin/python -m uvicorn src.dashboard:app --reload
# → http://localhost:8000
```

**If you already have another Scout instance running on 8000**, pick a different port:

```bash
.venv/bin/python -m uvicorn src.dashboard:app --host 127.0.0.1 --port 8001 --reload
# → http://localhost:8001
```

Each newsletter needs its own port if you want both dashboards open at once.

## 9. Triage and publish

Click **✦ Refresh** on Signals to pull a first batch immediately (or wait for tomorrow's 8am cron). Save what's worth publishing, hide the rest. Move to **Lineup** when ready, ✦ Draft the Spotlight, **Publish Edition**. The first edition lands in `output/editions/`.

Past editions feed back as training data — Scout's collector dedups against them, so once you've covered a story, it won't resurface on the next refresh.

---

## Your taste stays yours

Hide a story, save one, or promote it to HIGHLIGHTS — Scout quietly remembers. On the next run, it shows the LLM a handful of your most recent decisions as soft hints for what you do and don't like. The more you triage, the better it gets at matching your judgment.

All of that memory lives in `output/daily/items.json`, which never gets committed to git. So if someone else clones this repo, they start with a blank notebook. Their own hides and saves train their own Scout — completely separate from yours. Nothing leaks either way.

If you'd rather Scout not learn at all, flip `learning.enabled: false` in `config.yaml` and it goes back to running purely on the rules you wrote.

---

## Practical notes when running multiple newsletters

**LLM rate limits.** Two newsletters means roughly 2× the daily LLM calls. If both hit Claude Pro at the same 8am cron firing, you may hit subscription limits. Easy fix: stagger the cron in each newsletter's plist (one at 8:00, the other at 8:30), or route the higher-volume newsletter's Scout stage to `gpt-4o-mini`.

**Per-newsletter venv.** Each clone has its own `.venv/` with independent dependency state. No conflict between instances.

**Independent stores.** Each clone has its own `output/daily/items.json`, its own `output/editions/`, its own bucket state. You can hide a story in one newsletter without affecting the other.

**Upstream updates.** If Scout itself gains new features, you'd pull them in manually per clone (`git pull` upstream into each). Fine for two or three instances; consider a single-fork-with-branches workflow if you ever run more.

**Stopping an agent.** `launchctl unload ~/Library/LaunchAgents/com.scout.<your-slug>.plist`

---

That's the whole recipe. The biggest unlock is realizing that *everything Scout-specific is in `config.yaml` and `prompts/`* — the code itself doesn't know what newsletter it's serving.
