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
import httpx
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from progress import update as progress_update  # noqa: E402

# Per-feed network timeout. Long enough for slow but live servers; short
# enough that a dead host can't wedge the whole collection stage. (We hit
# this when arpost.co and vrscout.com went offline — without a timeout,
# feedparser.parse(url) hung the pipeline for >4 minutes on a single feed.)
_FEED_TIMEOUT_SECONDS = 15.0
_FEED_USER_AGENT = "Scout/1.0 (+https://github.com/denizergurel/scout)"


def load_config(config_path: str = None) -> dict:
    """Load configuration from config.yaml."""
    if config_path is None:
        config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def fetch_feed(source: dict) -> list[dict]:
    """Fetch and parse a single RSS feed, returning extracted items.

    Fetches bytes with httpx (timeout-bounded) before handing them to
    feedparser. feedparser.parse(url) does its own network I/O with no
    timeout, so a dead host hangs the pipeline. By fetching first we
    keep failure cost bounded to _FEED_TIMEOUT_SECONDS per feed.
    """
    url = source["url"]
    name = source["name"]
    items = []

    try:
        try:
            response = httpx.get(
                url,
                timeout=_FEED_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": _FEED_USER_AGENT},
            )
            response.raise_for_status()
        except httpx.TimeoutException:
            print(f"  ⚠ {name}: timed out after {_FEED_TIMEOUT_SECONDS:.0f}s")
            return items
        except httpx.HTTPError as e:
            print(f"  ⚠ {name}: {e}")
            return items

        feed = feedparser.parse(response.content)
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
    total = len(sources)

    print(f"Collecting from {total} sources...\n")

    for idx, source in enumerate(sources, start=1):
        items = fetch_feed(source)
        all_items.extend(items)
        # Update the dashboard progress pill — per feed, since each feed is
        # the natural unit of "something just happened."
        progress_update(
            stage="collecting",
            current=idx,
            total=total,
            last_done=f"{source.get('name', '?')} ({len(items)} new)",
        )

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
