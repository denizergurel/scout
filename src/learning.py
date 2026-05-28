"""
Adaptive learning for Scout and Editor.

Produces few-shot example blocks injected into the relevant agent prompts.
Two signals, both fully automatic — the user's normal triage feeds these
without any extra step.

- Scout: recent user-tagged rejections (`hide_reason` set, status=hidden)
- Editor: recent user-confirmed HIGHLIGHTS picks (category=HIGHLIGHTS,
  status in {saved, published})

Risk mitigations are baked in:

1. **Sliding window.** Decisions older than `window_days` are ignored, so
   stale tastes don't anchor the model.
2. **User-authored signals only.** We only look at items where the user
   tagged a hide_reason / saved / promoted — never at Scout's own past
   `relevant` flags or the Editor's own LLM-assigned categories. No
   self-feedback, no runaway loop.
3. **Hard cap.** `max_examples` keeps the prompt size bounded so we don't
   drift into a memory-explosion failure mode.
4. **Off-switch.** `learning.enabled: false` in config.yaml is byte-
   equivalent to the previous prompt — easy to disable without surprises.
5. **Config criteria stay the stable baseline.** Learning *augments*
   `extra_include_criteria` / `extra_exclude_criteria`; it never replaces
   them.

The module is intentionally read-only: it never writes to items.json or
mutates state. All it does is look at the store and render a block of
text. Failures here should never break a pipeline run, so all I/O is
defensive.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STORE_PATH = BASE_DIR / "output" / "daily" / "items.json"


def _learning_cfg(cfg: dict) -> dict:
    return cfg.get("learning") or {}


def is_enabled(cfg: dict) -> bool:
    """Off by default unless `learning.enabled: true` in config.yaml."""
    return bool(_learning_cfg(cfg).get("enabled", False))


def _load_items() -> list[dict]:
    """Read items.json defensively. A missing or unreadable store yields
    an empty list — learning blocks become empty strings, prompt is
    unchanged."""
    if not STORE_PATH.exists():
        return []
    try:
        with open(STORE_PATH, "r") as f:
            return json.load(f).get("items", [])
    except (json.JSONDecodeError, OSError):
        return []


def _within_window(added_at: str, days: int) -> bool:
    if not added_at:
        return False
    try:
        dt = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return dt >= cutoff


def _reason_label(reason_id: str, reasons: list[dict]) -> str:
    for r in reasons:
        if isinstance(r, dict) and r.get("id") == reason_id:
            return r.get("label") or reason_id
    return reason_id


def _log(stage: str, count: int, window_days: int, kind: str) -> None:
    """One-line marker so the user can see the loop working in pipeline
    output. Quiet when nothing is fed back, to avoid log noise."""
    if count <= 0:
        return
    print(f"[learning] {stage}: feeding back {count} recent {kind} from last {window_days}d", file=sys.stderr)


def scout_hides_block(cfg: dict) -> str:
    """Render a markdown block of recent user-tagged rejections for Scout."""
    if not is_enabled(cfg):
        return ""

    settings = _learning_cfg(cfg).get("scout") or {}
    window_days = max(1, int(settings.get("window_days", 30)))
    max_examples = max(0, int(settings.get("max_examples", 15)))
    if max_examples == 0:
        return ""

    reasons = (cfg.get("editorial") or {}).get("hide_reasons") or []

    candidates: list[dict] = []
    for it in _load_items():
        if it.get("status") != "hidden":
            continue
        if not it.get("hide_reason"):
            continue
        if not _within_window(it.get("added_at") or "", window_days):
            continue
        candidates.append(it)

    candidates.sort(key=lambda x: x.get("added_at") or "", reverse=True)
    picks = candidates[:max_examples]
    if not picks:
        return ""

    bullets: list[str] = []
    for it in picks:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        label = _reason_label(it.get("hide_reason") or "", reasons)
        if label:
            bullets.append(f'- "{title}" — marked as {label}')
        else:
            bullets.append(f'- "{title}"')
    if not bullets:
        return ""

    _log("scout", len(bullets), window_days, "rejections")

    return (
        "## Recent rejections (your past decisions)\n\n"
        "These items were judged not-fit for the newsletter in the last "
        f"{window_days} days, with the editor's reason in parentheses. "
        "Treat them as soft examples of the kinds of items to reject "
        "going forward — but the universal and topic-specific rules above "
        "still take priority.\n\n"
        + "\n".join(bullets)
    )


def editor_highlights_block(cfg: dict) -> str:
    """Render a markdown block of recent user-confirmed HIGHLIGHTS for Editor."""
    if not is_enabled(cfg):
        return ""

    settings = _learning_cfg(cfg).get("editor") or {}
    window_days = max(1, int(settings.get("window_days", 30)))
    max_examples = max(0, int(settings.get("max_highlights_examples", 6)))
    if max_examples == 0:
        return ""

    candidates: list[dict] = []
    for it in _load_items():
        if (it.get("category") or "").upper() != "HIGHLIGHTS":
            continue
        if it.get("status") not in ("saved", "published"):
            continue
        if not _within_window(it.get("added_at") or "", window_days):
            continue
        candidates.append(it)

    candidates.sort(key=lambda x: x.get("added_at") or "", reverse=True)
    picks = candidates[:max_examples]
    if not picks:
        return ""

    bullets: list[str] = []
    for it in picks:
        title = (it.get("title") or "").strip()
        if title:
            bullets.append(f'- "{title}"')
    if not bullets:
        return ""

    _log("editor", len(bullets), window_days, "HIGHLIGHTS examples")

    return (
        "## Recent HIGHLIGHTS examples (the editor's bar)\n\n"
        "These items were placed into HIGHLIGHTS by the editor in the "
        f"last {window_days} days. Use them as positive examples of the "
        "bar a story must clear to qualify — the most significant 4–6 "
        "stories of the period across all topics.\n\n"
        + "\n".join(bullets)
    )
