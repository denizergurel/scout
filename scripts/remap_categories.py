"""
One-off: rename legacy category labels in output/daily/items.json to match the
current `categories:` block in config.yaml.

Run from the project root:

    source .venv/bin/activate
    python scripts/remap_categories.py

Idempotent — running it again is a no-op once items are migrated.
"""

from __future__ import annotations

import json
from pathlib import Path

RENAMES = {
    "APPLE VISION PRO": "APPLE VISION PRO IN THE NEWS",
    "VISION PRO": "APPLE VISION PRO IN THE NEWS",
    "GAMING MARKETPLACE": "GAMING MARKETPLACE WATCH",
    "HANDS ON REVIEW": "HANDS-ON REVIEW",
}

STORE_PATH = Path(__file__).resolve().parent.parent / "output" / "daily" / "items.json"


def main() -> None:
    if not STORE_PATH.exists():
        print(f"No store at {STORE_PATH} — nothing to do.")
        return

    data = json.loads(STORE_PATH.read_text())
    items = data.get("items", [])

    changed = 0
    for it in items:
        old = (it.get("category") or "").strip().upper()
        new = RENAMES.get(old)
        if new and new != old:
            it["category"] = new
            changed += 1

    if changed == 0:
        print("No category renames needed — store already matches config.")
        return

    STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"Updated {changed} item{'s' if changed != 1 else ''} in {STORE_PATH}.")


if __name__ == "__main__":
    main()
