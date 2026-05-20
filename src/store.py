"""
Scout — Item Store

Single persistent JSON store backing the per-item lifecycle model.
Every item has a stable id and a status: pending | saved | published | hidden | aged_out.

The daily cron writes new items as `pending`. The dashboard mutates status as the editor
triages (Signals) and composes the newsletter (Lineup). Published items reference the
edition they shipped in.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output" / "daily"
STORE_FILE = OUTPUT_DIR / "items.json"

STATUSES = {"pending", "saved", "published", "hidden", "aged_out"}
# Legacy aliases — older items may still carry these. Read paths accept them;
# write paths normalize via _normalize_status() below.
_LEGACY_STATUS_ALIASES = {"approved": "saved", "rejected": "hidden"}


def _normalize_status(status: str) -> str:
    return _LEGACY_STATUS_ALIASES.get(status, status)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id(item: dict) -> str:
    """Stable id from the article link; falls back to title."""
    key = (item.get("link") or item.get("title") or "").strip().lower()
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def load_store() -> dict:
    if not STORE_FILE.exists():
        return {"version": 1, "items": []}
    with open(STORE_FILE, "r") as f:
        return json.load(f)


def save_store(store: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(STORE_FILE, "w") as f:
        json.dump(store, f, indent=2, ensure_ascii=False)


def all_items(store: dict | None = None) -> list[dict]:
    return (store or load_store()).get("items", [])


def by_status(status: str, store: dict | None = None) -> list[dict]:
    target = _normalize_status(status)
    return [
        it for it in all_items(store)
        if _normalize_status(it.get("status", "")) == target
    ]


def by_status_for_publish(store: dict | None = None) -> list[dict]:
    """Saved items flagged for the next edition (included_in_next=True)."""
    return [it for it in by_status("saved", store) if it.get("included_in_next", True)]


def find_by_id(item_id: str, store: dict | None = None) -> dict | None:
    for it in all_items(store):
        if it.get("id") == item_id:
            return it
    return None


def add_pending(new_items: list[dict]) -> tuple[int, int]:
    """Append items as `pending`. Dedups against everything already in the store
    (by id, then by lowercased title). Returns (added, skipped)."""
    store = load_store()
    existing_ids = {it.get("id") for it in store["items"]}
    existing_titles = {(it.get("title") or "").strip().lower() for it in store["items"]}

    added = 0
    skipped = 0
    now = _now()
    for raw in new_items:
        item = dict(raw)
        item["id"] = item.get("id") or make_id(item)
        title_key = (item.get("title") or "").strip().lower()
        if item["id"] in existing_ids or (title_key and title_key in existing_titles):
            skipped += 1
            continue
        item.setdefault("status", "pending")
        item.setdefault("added_at", now)
        item.setdefault("decided_at", None)
        item.setdefault("edition_id", None)
        store["items"].append(item)
        existing_ids.add(item["id"])
        if title_key:
            existing_titles.add(title_key)
        added += 1

    save_store(store)
    return added, skipped


def update_status(item_id: str, status: str, **extra) -> dict | None:
    """Set status on an item, stamp decided_at, and apply any extra field updates.
    Returns the updated item or None if not found."""
    status = _normalize_status(status)
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    store = load_store()
    for it in store["items"]:
        if it.get("id") == item_id:
            it["status"] = status
            it["decided_at"] = _now()
            # When entering Saved, default the per-item include flag to True.
            if status == "saved" and "included_in_next" not in extra and "included_in_next" not in it:
                it["included_in_next"] = True
            for k, v in extra.items():
                it[k] = v
            save_store(store)
            return it
    return None


def set_included(item_id: str, included: bool) -> dict | None:
    """Flip the per-item include flag on a Saved item without changing status."""
    return update_fields(item_id, included_in_next=bool(included))


def normalize_existing() -> dict:
    """One-time pass: rewrite any legacy `approved`/`rejected` statuses to
    `saved`/`hidden`, and default `included_in_next=True` on Saved items."""
    store = load_store()
    renamed = 0
    flagged = 0
    for it in store["items"]:
        cur = it.get("status", "")
        new = _normalize_status(cur)
        if new != cur:
            it["status"] = new
            renamed += 1
        if it.get("status") == "saved" and "included_in_next" not in it:
            it["included_in_next"] = True
            flagged += 1
    save_store(store)
    return {"renamed_statuses": renamed, "flag_defaults_set": flagged}


def update_fields(item_id: str, **fields) -> dict | None:
    """Patch fields on an item without changing status."""
    store = load_store()
    for it in store["items"]:
        if it.get("id") == item_id:
            for k, v in fields.items():
                it[k] = v
            save_store(store)
            return it
    return None


def known_links() -> set[str]:
    """All links/titles already in the store. Used by the collector to skip
    re-processing items the editor has already seen, judged, or published."""
    keys = set()
    for it in all_items():
        link = (it.get("link") or "").strip()
        title = (it.get("title") or "").strip().lower()
        if link:
            keys.add(link)
        if title:
            keys.add(title)
    return keys


