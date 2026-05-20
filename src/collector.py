"""
Scout — RSS Collector

Pulls RSS feeds from config.yaml, extracts articles, and writes raw_items.json.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import yaml


def load_config(config_path: str = None) -> dict:
    """Load configuration from config.yaml."""
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def fetch_feed(source: dict) -> list[dict]:
    """Fetch and parse a single RSS feed, returning extracted items."""
    url = source["url"]
    name = source["name"]
    items = []

    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            print(f"  ⚠ {name}: Feed error — {feed.bozo_exception}")
            return items

        cutoff = datetime.now(timezone.utc) - timedelta(days=8)

        for entry in feed.entries:
            # Extract published date
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc).isoformat()

            # Skip articles older than 8 days (allow undated items through)
            if published and datetime.fromisoformat(published) < cutoff:
                continue

            # Extract description (prefer summary, fall back to content)
            description = ""
            if hasattr(entry, "summary"):
                description = entry.summary
            elif hasattr(entry, "content") and entry.content:
                description = entry.content[0].get("value", "")

            # Strip HTML tags from description (basic)
            description = re.sub(r"<[^>]+>", "", description).strip()
            # Truncate long descriptions
            if len(description) > 500:
                description = description[:497] + "..."

            item = {
                "title": entry.get("title", "Untitled"),
                "link": entry.get("link", ""),
                "published": published,
                "description": description,
                "source": name,
                "category_hint": source.get("category_hint", ""),
            }
            items.append(item)

        print(f"  ✓ {name}: {len(items)} items")

    except Exception as e:
        print(f"  ✗ {name}: {e}")

    return items


def collect(config: dict) -> list[dict]:
    """Collect articles from all RSS sources."""
    all_items = []
    sources = config.get("sources", [])

    print(f"Collecting from {len(sources)} sources...\n")

    for source in sources:
        items = fetch_feed(source)
        all_items.extend(items)

    return all_items


def deduplicate(items: list[dict]) -> list[dict]:
    """Remove duplicate articles based on URL."""
    seen_links = set()
    unique = []
    for item in items:
        link = item.get("link", "")
        if link and link not in seen_links:
            seen_links.add(link)
            unique.append(item)
        elif not link:
            unique.append(item)
    return unique


def save_output(items: list[dict], config: dict):
    """Save collected items to raw_items.json."""
    output_dir = Path(__file__).parent.parent / config["output"]["daily_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    output_file = output_dir / config["output"]["raw_file"]

    output = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "total_items": len(items),
        "items": items,
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(items)} items to {output_file}")


def main():
    """Main entry point."""
    print("=" * 50)
    print("Scout — RSS Collector")
    print("=" * 50 + "\n")

    config = load_config()
    items = collect(config)
    items = deduplicate(items)
    save_output(items, config)

    print(f"\nDone! {len(items)} unique articles collected.")


if __name__ == "__main__":
    main()
