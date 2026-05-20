"""
Scout — Editor Agent (Categorize & Summarize)

Reads filtered_items.json, calls the configured LLM to categorize and summarize,
outputs categorized_items.json.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from llm import call_llm_json

BASE_DIR = Path(__file__).parent.parent


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)


def load_prompt() -> str:
    from prompt_loader import render_prompt
    return render_prompt("editor")


def load_filtered_items(config: dict) -> dict:
    output_dir = BASE_DIR / config["output"]["daily_dir"]
    filtered_file = output_dir / config["output"]["filtered_file"]
    with open(filtered_file, "r") as f:
        return json.load(f)


def categorize_items(items: list[dict], system_prompt: str) -> list[dict]:
    """Send items to the LLM for categorization and summarization."""
    batch_size = 10
    categorized = []

    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]

        articles_text = ""
        for idx, item in enumerate(batch):
            articles_text += f"""
---
ID: {idx}
Title: {item['title']}
Source: {item['source']}
Link: {item['link']}
Date: {item.get('published', 'Unknown')}
Description: {item.get('description', 'No description')}
Category Hint: {item.get('category_hint', 'None')}
---
"""

        user_message = f"""Categorize and summarize the following {len(batch)} articles.

{articles_text}

Respond with ONLY a JSON array of objects, one per article, in order:
[{{"id": 0, "category": "SECTION NAME", "summary": "paragraph style summary with inline attribution", "significance": "high|medium|low", "flags": []}}]"""

        try:
            results = call_llm_json(system_prompt, user_message, stage="editor")

            for result in results:
                article_idx = result["id"]
                if article_idx < len(batch):
                    item = batch[article_idx].copy()
                    item["category"] = result.get("category", "HIGHLIGHTS")
                    item["summary"] = result.get("summary", item.get("description", ""))
                    item["significance"] = result.get("significance", "medium")
                    item["flags"] = result.get("flags", [])
                    categorized.append(item)

        except (json.JSONDecodeError, KeyError, IndexError, ValueError, RuntimeError) as e:
            print(f"  ⚠ Batch {i // batch_size + 1} error: {e}")
            for item in batch:
                item["category"] = item.get("category_hint", "HIGHLIGHTS")
                item["summary"] = item.get("description", "")
                item["significance"] = "medium"
                item["flags"] = ["parse_error"]
                categorized.append(item)

        print(f"  Batch {i // batch_size + 1}: {len(batch)} items categorized")

    return categorized


def save_categorized(items: list[dict], config: dict):
    output_dir = BASE_DIR / config["output"]["daily_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / config["output"]["categorized_file"]

    output = {
        "categorized_at": datetime.now(timezone.utc).isoformat(),
        "total_items": len(items),
        "items": items,
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(items)} categorized items to {output_file}")


def main():
    print("=" * 50)
    print("Scout — Editor Agent (Categorize & Summarize)")
    print("=" * 50 + "\n")

    config = load_config()
    system_prompt = load_prompt()
    filtered_data = load_filtered_items(config)
    items = filtered_data.get("items", [])

    if not items:
        print("No items to categorize. Run scout first.")
        return

    print(f"Categorizing {len(items)} items...\n")
    categorized = categorize_items(items, system_prompt)
    save_categorized(categorized, config)

    categories = {}
    for item in categorized:
        cat = item.get("category", "Uncategorized")
        categories[cat] = categories.get(cat, 0) + 1

    print("\nCategory breakdown:")
    for cat, count in sorted(categories.items()):
        print(f"  {cat}: {count}")

    print(f"\nDone! {len(categorized)} articles categorized.")


if __name__ == "__main__":
    main()
