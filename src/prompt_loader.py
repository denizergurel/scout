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

    values = {
        "newsletter_name": identity.get("name") or _DEFAULTS["newsletter_name"],
        "cadence": identity.get("cadence") or _DEFAULTS["cadence"],
        "topics": identity.get("topics") or _DEFAULTS["topics"],
        "voice": (editorial.get("voice") or _DEFAULTS["voice"]).rstrip(),
        "include_criteria_block": _criteria_block(
            f"Additional KEEP rules for this newsletter",
            editorial.get("extra_include_criteria"),
        ),
        "exclude_criteria_block": _criteria_block(
            f"Additional DISCARD rules for this newsletter",
            editorial.get("extra_exclude_criteria"),
        ),
    }

    def replace(match: re.Match) -> str:
        key = match.group(1).strip()
        return values.get(key, match.group(0))

    rendered = re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", replace, text)

    # Collapse runs of 3+ blank lines that can appear when criteria blocks
    # are empty. Keeps the prompt tidy without affecting LLM behavior.
    return re.sub(r"\n{3,}", "\n\n", rendered)
