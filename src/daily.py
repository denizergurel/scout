"""
Scout — Daily Agent

Runs on each cron firing: collect → scout → editor → append to store as `pending`.
No weekly accumulator, no Monday reset — the store grows monotonically.
Dedup against the full store prevents re-processing items the editor already saw.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collector import collect, deduplicate, load_config
from scout import filter_items, load_prompt as load_scout_prompt
from editor import categorize_items, load_prompt as load_editor_prompt
from store import add_pending, known_links, load_store

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output" / "daily"
LOG_FILE = OUTPUT_DIR / "agent.log"
STATUS_FILE = OUTPUT_DIR / "refresh_status.json"


def log(msg: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _read_status() -> dict:
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_status(**fields) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATUS_FILE, "w") as f:
        json.dump(fields, f, indent=2)


def _set_progress(stage: str, message: str, started_at: str, **extra) -> None:
    _write_status(
        state="running",
        stage=stage,
        message=message,
        started_at=started_at,
        updated_at=datetime.now(timezone.utc).isoformat(),
        **extra,
    )


def dedup_against_store(items: list[dict]) -> list[dict]:
    """Drop items whose link or title already appears in the store."""
    seen = known_links()
    unique = []
    for item in items:
        link = (item.get("link") or "").strip()
        title = (item.get("title") or "").strip().lower()
        if link and link in seen:
            continue
        if title and title in seen:
            continue
        unique.append(item)
    return unique


def run_daily():
    # Pick up started_at from the dashboard if it pre-populated the status file,
    # otherwise stamp our own.
    prev = _read_status()
    started_at = prev.get("started_at") or datetime.now(timezone.utc).isoformat()

    def finish_idle(message: str, **extra):
        _write_status(
            state="done",
            stage="done",
            message=message,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            **extra,
        )

    try:
        log("=" * 50)
        log("Scout — Daily Agent Run")
        log("=" * 50)

        config = load_config()

        _set_progress("collecting", "Collecting from RSS feeds…", started_at)
        log("Stage 1: Collecting RSS feeds...")
        raw_items = collect(config)
        raw_items = deduplicate(raw_items)
        log(f"  Collected {len(raw_items)} unique articles from feeds")

        if not raw_items:
            log("  No items collected (feeds may be unreachable). Exiting.")
            finish_idle("No items collected.", added=0)
            return

        new_items = dedup_against_store(raw_items)
        log(f"  {len(new_items)} new items (not already in store)")

        if not new_items:
            log("  Nothing new today. Exiting.")
            finish_idle("No new stories — already up to date.", added=0)
            return

        _set_progress(
            "scouting",
            f"Scouting {len(new_items)} new items…",
            started_at,
            new_items=len(new_items),
        )
        log("Stage 2: Filtering for relevance (Scout agent)...")
        scout_prompt = load_scout_prompt()
        filtered = filter_items(new_items, scout_prompt)
        log(f"  {len(filtered)}/{len(new_items)} items passed relevance filter")

        if not filtered:
            log("  No relevant items today. Exiting.")
            finish_idle("No relevant stories in this batch.", added=0)
            return

        _set_progress(
            "editing",
            f"Categorizing {len(filtered)} items…",
            started_at,
            filtered=len(filtered),
        )
        log("Stage 3: Categorizing and summarizing (Editor agent)...")
        editor_prompt = load_editor_prompt()
        categorized = categorize_items(filtered, editor_prompt)
        log(f"  {len(categorized)} items categorized and summarized")

        added, skipped = add_pending(categorized)
        store = load_store()
        store_total = len(store.get("items", []))
        pending_total = sum(1 for it in store.get("items", []) if it.get("status") == "pending")
        log(f"  Added {added} new pending items (skipped {skipped} duplicates)")
        log(f"  Store totals — all: {store_total}, pending: {pending_total}")

        today_file = OUTPUT_DIR / f"daily_{datetime.now().strftime('%Y-%m-%d')}.json"
        with open(today_file, "w") as f:
            json.dump({"date": datetime.now().isoformat(), "items": categorized}, f, indent=2)

        finish_idle(
            f"Added {added} new {'story' if added == 1 else 'stories'}.",
            added=added,
            skipped=skipped,
        )
        log(f"\nDone! {added} items added to the pending pool.")
        log("Open the dashboard to triage: python -m uvicorn src.dashboard:app --reload")
    except Exception as e:  # noqa: BLE001 — we want every failure surfaced in the UI
        _write_status(
            state="error",
            stage="error",
            message=f"Refresh failed: {e}",
            error=str(e),
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        log(f"FAILED: {e}")
        raise


if __name__ == "__main__":
    run_daily()
