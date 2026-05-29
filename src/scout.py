"""
Scout — Scout Agent (Relevance Filter)

Reads raw_items.json, calls the configured LLM to filter for relevance,
outputs filtered_items.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from llm import call_llm_json
from progress import update as progress_update

BASE_DIR = Path(__file__).parent.parent


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)


def load_prompt() -> str:
    from prompt_loader import render_prompt
    return render_prompt("scout")


def load_raw_items(config: dict) -> dict:
    output_dir = BASE_DIR / config["output"]["daily_dir"]
    raw_file = output_dir / config["output"]["raw_file"]
    with open(raw_file, "r") as f:
        return json.load(f)


def filter_items(items: list[dict], system_prompt: str) -> list[dict]:
    """Send items to the LLM for relevance filtering."""
    batch_size = 20
    filtered = []
    # Report against items reviewed so the front-end can show "47/300" and a
    # percent rather than abstract batch numbers, which read better to a
    # non-engineer.
    total = len(items)

    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]

        articles_text = ""
        for idx, item in enumerate(batch):
            articles_text += f"""
---
ID: {idx}
Title: {item['title']}
Source: {item['source']}
Date: {item.get('published', 'Unknown')}
Description: {item.get('description', 'No description')}
---
"""

        user_message = f"""Evaluate the following {len(batch)} articles for relevance. For each, respond with a JSON object.

{articles_text}

Respond with ONLY a JSON array of objects, one per article, in order:
[{{"id": 0, "relevant": true/false, "reason": "..."}}]"""

        try:
            results = call_llm_json(system_prompt, user_message, stage="scout")

            for result in results:
                article_idx = result["id"]
                if article_idx < len(batch) and result.get("relevant", False):
                    item = batch[article_idx].copy()
                    item["scout_reason"] = result.get("reason", "")
                    filtered.append(item)

            kept = sum(1 for r in results if r.get("relevant", False))
            print(f"  Batch {i // batch_size + 1}: {kept}/{len(batch)} items kept")

        except (json.JSONDecodeError, KeyError, IndexError, ValueError, RuntimeError) as e:
            print(f"  ⚠ Batch {i // batch_size + 1} error: {e}, keeping all items")
            filtered.extend(batch)

        progress_update(
            stage="scouting",
            current=min(i + batch_size, total),
            total=total,
            last_done=f"{len(filtered)} kept so far",
        )

    return filtered


def save_filtered(items: list[dict], config: dict):
    output_dir = BASE_DIR / config["output"]["daily_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / config["output"]["filtered_file"]

    output = {
        "filtered_at": datetime.now(timezone.utc).isoformat(),
        "total_items": len(items),
        "items": items,
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(items)} filtered items to {output_file}")


def main():
    print("=" * 50)
    print("Scout — Scout Agent (Relevance Filter)")
    print("=" * 50 + "\n")

    config = load_config()
    system_prompt = load_prompt()
    raw_data = load_raw_items(config)
    items = raw_data.get("items", [])

    if not items:
        print("No items to filter. Run collector first.")
        return

    print(f"Filtering {len(items)} items for relevance...\n")
    filtered = filter_items(items, system_prompt)
    save_filtered(filtered, config)

    print(f"\nDone! {len(filtered)}/{len(items)} articles passed relevance filter.")


if __name__ == "__main__":
    main()
