"""
Scout — Curator Agent

The Curator's role in Scout's lifecycle model is narrow: it's an on-demand
assistant invoked from the Lineup view to draft the SPOTLIGHT paragraph
from the items currently slated for the next edition. The dashboard calls
`load_prompt()` to fetch the system prompt; the LLM call happens in
`src/dashboard.py::draft_spotlight()`.

The agent is intentionally NOT in the auto-pipeline — the editor composes
the lineup; the agent assists.
"""

from __future__ import annotations

from pathlib import Path

import yaml

BASE_DIR = Path(__file__).parent.parent


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)


def load_prompt() -> str:
    from prompt_loader import render_prompt
    return render_prompt("curator")
