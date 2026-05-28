"""
One-off: re-run the Editor on items that are currently `pending` and were added
in the last N hours, applying the latest prompt/config. Updates `category`,
`summary`, `significance`, `flags` in items.json in place. Other fields are
preserved.

Useful after a prompt or config change to bring fresh untriaged items in line
without losing editorial work on older saved/published items.

Usage from project root:

    source .venv/bin/activate
    python scripts/recategorize_pending.py            # default: last 24h
    python scripts/recategorize_pending.py --hours 6  # narrower window
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from editor import categorize_items  # noqa: E402
from prompt_loader import render_prompt  # noqa: E402

STORE_PATH = PROJECT_ROOT / "output" / "daily" / "items.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="age window in hours (default 24)")
    args = ap.parse_args()

    if not STORE_PATH.exists():
        print(f"No store at {STORE_PATH}")
        return

    data = json.loads(STORE_PATH.read_text())
    items = data.get("items", [])

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    pending_recent = []
    for it in items:
        if it.get("status") != "pending":
            continue
        added = it.get("added_at") or ""
        try:
            dt = datetime.fromisoformat(added.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt >= cutoff:
            pending_recent.append(it)

    if not pending_recent:
        print(f"No pending items added in the last {args.hours}h.")
        return

    print(f"Re-categorizing {len(pending_recent)} pending items added in last {args.hours}h…")
    prompt = render_prompt("editor")
    updated = categorize_items(pending_recent, prompt)

    by_id = {it["id"]: it for it in updated}
    changed = 0
    for it in items:
        if it["id"] in by_id:
            src = by_id[it["id"]]
            before = (it.get("category") or "").upper()
            it["category"] = src.get("category", it.get("category"))
            it["summary"] = src.get("summary", it.get("summary"))
            it["significance"] = src.get("significance", it.get("significance"))
            it["flags"] = src.get("flags", it.get("flags", []))
            if (it["category"] or "").upper() != before:
                changed += 1

    STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"\nDone. {changed} item{'s' if changed != 1 else ''} changed category.")


if __name__ == "__main__":
    main()
