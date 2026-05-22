"""
Prompt template loader.

Reads a prompt file from prompts/ and substitutes `{{placeholder}}` tokens
from config.yaml. This is what makes Scout a generic tool: change config.yaml
to retarget the system to a different newsletter without touching code or
prompts.

Two layers feed the prompts:

  newsletter:   identity (name, cadence, topics) — per-user defaults
  editorial:    voice, include criteria, exclude criteria — Scout's
                editorial DNA (voice) + per-newsletter overlay (extras)
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"

# Universal fallbacks applied when config.yaml is missing or partial. The
# values that ship in config.yaml are the canonical defaults; these exist
# only so an empty / malformed config never crashes prompt rendering.
_DEFAULTS = {
    "newsletter_name": "Scout Newsletter",
    "cadence": "weekly",
    "topics": "the topics defined in your config",
    "voice": (
        "- **Format**: Single paragraph, 1-3 sentences. Self-contained and readable alone.\n"
        "- **Tone**: Neutral, authoritative. Think NYT, Bloomberg, Reuters, FT.\n"
        "- **Voice**: Active voice. Precise language. No hype words.\n"
        "- **Attribution**: Name the source publication INLINE.\n"
        "- **NO markdown formatting**: Plain prose paragraph."
    ),
}

# Frozen baseline used when config.yaml omits `editorial.universal_discards`.
# The strings here match Scout's shipped prompt verbatim; the renderer joins
# them under the "## Universal DISCARD rules (always apply)" heading.
_DEFAULT_UNIVERSAL_DISCARDS = [
    {"id": "patents",
     "rule": "**Patent filings** (ALWAYS discard)",
     "enabled": True},
    {"id": "opinion_unanchored",
     "rule": "Opinion or commentary without a news anchor",
     "enabled": True},
    {"id": "stale_7d",
     "rule": "Older than 7 days",
     "enabled": True},
    {"id": "off_topic",
     "rule": "General news unrelated to {{topics}}",
     "enabled": True},
    {"id": "duplicates",
     "rule": "Duplicate of another article already marked YES (keep the better source)",
     "enabled": True},
]

# Frozen baseline used when config.yaml omits `editorial.sections`. Matches
# Spatial Report's shipped Categories table verbatim.
_DEFAULT_SECTIONS = [
    ("HIGHLIGHTS", "Major stories of the week — significant announcements, strategy shifts, market moves"),
    ("APPLE VISION PRO", "Apple Vision Pro coverage: usage, apps, workflows, and notable reports."),
    ("APP STORE HIGHLIGHTS", "Notable new or updated apps/games for Apple Vision Pro"),
    ("GAMING MARKETPLACE", "VR/AR/XR gaming industry — studios, releases, market shifts"),
    ("INVESTMENT", "Funding rounds, acquisitions, M&A in XR/AI/spatial"),
    ("HARDWARE", "New devices, displays, optics, chips, sensors — anything physical"),
    ("SOFTWARE", "Platform updates, OS releases, SDKs, developer tools, runtime changes"),
    ("AI AND SIMULATED WORLDS", "AI models, world models, simulation, compute — when relevant to spatial/XR or seismic"),
    ("HANDS ON REVIEW", "First-hand reviews of headsets, glasses, apps, or experiences"),
    ("RESEARCH", "Academic studies, clinical trials, published papers using XR/VR/AR"),
    ("OPINION", "Analysis pieces, editorials, commentary — attributed to author/publication"),
]


def _load_config() -> dict:
    config_path = BASE_DIR / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {}


def _criteria_block(heading: str, body: str | None) -> str:
    """Wrap a user-provided criteria body in a labeled section, or return
    an empty string if there's nothing to render. Keeps the prompt clean
    when extras are empty."""
    text = (body or "").strip()
    if not text:
        return ""
    return f"## {heading}\n\n{text}\n"


def _render_universal_discards(editorial: dict, topics: str) -> str:
    """Render the Universal DISCARD rules section. The shape (`## heading`
    + blank line + bullets) and the rule strings match Scout's shipped
    prompt verbatim when every rule is enabled (the default), so toggling
    nothing in the UI produces a byte-identical prompt."""
    items = editorial.get("universal_discards")
    if not isinstance(items, list) or not items:
        items = _DEFAULT_UNIVERSAL_DISCARDS

    bullets = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", True):
            continue
        rule = (item.get("rule") or "").strip()
        if not rule:
            continue
        # The rule string may itself reference {{topics}} (e.g. "General news
        # unrelated to {{topics}}"). Substitute here so the rendered block
        # doesn't leave dangling placeholders — outer regex pass is single-
        # depth and won't recurse into our substitution.
        rule = rule.replace("{{topics}}", topics)
        bullets.append(f"- {rule}")

    if not bullets:
        # All toggles off — emit the heading with no bullets rather than an
        # orphan heading, to keep the prompt structurally clean. (We still
        # render the heading so adjacent sections don't visually merge.)
        return "## Universal DISCARD rules (always apply)\n"

    return "## Universal DISCARD rules (always apply)\n\n" + "\n".join(bullets)


def _render_categories_table(editorial: dict) -> str:
    """Render the Editor's Categories markdown table. Output is byte-
    identical to Scout's shipped editor.md when sections match the default
    list."""
    sections = editorial.get("sections")
    pairs: list[tuple[str, str]]
    if isinstance(sections, list) and sections:
        pairs = []
        for s in sections:
            if not isinstance(s, dict):
                continue
            name = (s.get("name") or "").strip()
            desc = (s.get("description") or "").strip()
            if not name:
                continue
            pairs.append((name, desc))
    else:
        pairs = list(_DEFAULT_SECTIONS)

    if not pairs:
        return ""

    lines = ["| Category | Description |", "|----------|-------------|"]
    for name, desc in pairs:
        lines.append(f"| {name} | {desc} |")
    return "\n".join(lines)


def render_prompt(name: str) -> str:
    """Read prompts/{name}.md and substitute `{{placeholder}}` tokens.

    Unknown placeholders are left unchanged (visible as `{{x}}` in the
    rendered output) so missing config is loud rather than silent.
    """
    path = PROMPTS_DIR / f"{name}.md"
    text = path.read_text()

    cfg = _load_config()
    identity = cfg.get("newsletter") or {}
    editorial = cfg.get("editorial") or {}

    topics = identity.get("topics") or _DEFAULTS["topics"]
    values = {
        "newsletter_name": identity.get("name") or _DEFAULTS["newsletter_name"],
        "cadence": identity.get("cadence") or _DEFAULTS["cadence"],
        "topics": topics,
        "voice": (editorial.get("voice") or _DEFAULTS["voice"]).rstrip(),
        "include_criteria_block": _criteria_block(
            f"Additional KEEP rules for this newsletter",
            editorial.get("extra_include_criteria"),
        ),
        "exclude_criteria_block": _criteria_block(
            f"Additional DISCARD rules for this newsletter",
            editorial.get("extra_exclude_criteria"),
        ),
        "universal_discard_block": _render_universal_discards(editorial, topics),
        "categories_table": _render_categories_table(editorial),
    }

    def replace(match: re.Match) -> str:
        key = match.group(1).strip()
        return values.get(key, match.group(0))

    rendered = re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", replace, text)

    # Collapse runs of 3+ blank lines that can appear when criteria blocks
    # are empty. Keeps the prompt tidy without affecting LLM behavior.
    return re.sub(r"\n{3,}", "\n\n", rendered)
