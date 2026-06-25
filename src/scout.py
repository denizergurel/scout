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
from pipeline_log import log
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
    """Send items to the LLM for relevance filtering.

    Fail-closed semantics: a batch that errors out is DROPPED, not kept.
    The previous behavior (keep-all-on-error) silently turned a transient
    LLM outage — e.g. an unauthenticated Claude CLI — into a 100% pass rate
    that flooded the dashboard with raw RSS items. Drop the batch and the
    items get re-collected on the next run.

    Raises RuntimeError if every batch fails so the daily runner can mark
    the refresh as errored instead of writing an empty pending pool.
    """
    batch_size = 20
    filtered = []
    # Report against items reviewed so the front-end can show "47/300" and a
    # percent rather than abstract batch numbers, which read better to a
    # non-engineer.
    total = len(items)
    total_batches = (total + batch_size - 1) // batch_size
    failed_batches = 0

    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        batch_num = i // batch_size + 1

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
            log(f"  Scout batch {batch_num}/{total_batches}: {kept}/{len(batch)} items kept")

        except (json.JSONDecodeError, KeyError, IndexError, ValueError, RuntimeError) as e:
            failed_batches += 1
            log(f"  ⚠ Scout batch {batch_num}/{total_batches} failed ({type(e).__name__}: {e}). Dropping batch.")

        progress_update(
            stage="scouting",
            current=min(i + batch_size, total),
            total=total,
            last_done=f"{len(filtered)} kept so far",
        )

    if total_batches and failed_batches == total_batches:
        raise RuntimeError(
            f"Scout failed on every batch ({failed_batches}/{total_batches}). "
            "LLM provider is likely unreachable or unauthenticated."
        )
    if failed_batches:
        log(
            f"  ⚠ Scout completed with {failed_batches}/{total_batches} failed batches "
            f"({total_batches - failed_batches} succeeded)."
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
