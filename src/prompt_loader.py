"""
Prompt template loader.

Reads a prompt file from prompts/ and substitutes `{{placeholder}}` tokens
from the newsletter identity defined in config.yaml. This is what makes
Scout a generic tool: change config.yaml to retarget the system to a
different newsletter without touching code or prompts.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent.parent
PROMPTS_DIR = BASE_DIR / "prompts"

# Defaults applied when a config value is missing — preserve current behavior
# for installations that don't yet have a `newsletter:` block.
_DEFAULTS = {
    "newsletter_name": "Scout Newsletter",
    "cadence": "weekly",
    "topics": "the topics defined in your config",
}


def _load_newsletter_identity() -> dict:
    config_path = BASE_DIR / "config.yaml"
    if not config_path.exists():
        return {}
    with open(config_path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("newsletter") or {}


def render_prompt(name: str) -> str:
    """Read prompts/{name}.md and substitute `{{placeholder}}` tokens.

    Unknown placeholders are left unchanged (visible as `{{x}}` in the
    rendered output) so missing config is loud rather than silent.
    """
    path = PROMPTS_DIR / f"{name}.md"
    text = path.read_text()

    identity = _load_newsletter_identity()
    values = {
        "newsletter_name": identity.get("name") or _DEFAULTS["newsletter_name"],
        "cadence": identity.get("cadence") or _DEFAULTS["cadence"],
        "topics": identity.get("topics") or _DEFAULTS["topics"],
    }

    def replace(match: re.Match) -> str:
        key = match.group(1).strip()
        return values.get(key, match.group(0))

    return re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", replace, text)
