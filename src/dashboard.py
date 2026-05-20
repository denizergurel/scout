"""
Scout — Dashboard

Local FastAPI dashboard for reviewing the weekly newsletter draft.
Native macOS-inspired design. Displays items by category, allows
approve/reject/edit, and exports final newsletter format.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

sys.path.insert(0, str(Path(__file__).parent))

from store import (
    by_status,
    by_status_for_publish,
    find_by_id,
    load_store,
    save_store,
    set_included,
    update_fields,
    update_status,
)
from curator import load_prompt as load_curator_prompt
from editor import categorize_items, load_prompt as load_editor_prompt
from llm import call_llm_json
from store import add_pending, make_id

app = FastAPI(title="Scout")

BASE_DIR = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output" / "daily"
EDITIONS_DIR = BASE_DIR / "output" / "editions"
SAMPLE_DATA = BASE_DIR / "output" / "sample_data.json"
BUCKET_STATE_FILE = OUTPUT_DIR / "bucket_state.json"
REFRESH_STATUS_FILE = OUTPUT_DIR / "refresh_status.json"
CONFIG_PATH = BASE_DIR / "config.yaml"
DAILY_SCRIPT = BASE_DIR / "src" / "daily.py"
VENV_PY = BASE_DIR / ".venv" / "bin" / "python"
PLIST_TEMPLATE = BASE_DIR / "com.scout.spatial-report.plist"
LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", "r") as f:
        return yaml.safe_load(f)


def load_weekly_draft() -> dict | None:
    draft_file = OUTPUT_DIR / "weekly_draft.json"
    if draft_file.exists():
        with open(draft_file, "r") as f:
            return json.load(f)
    # Fall back to sample data for demo
    if SAMPLE_DATA.exists():
        with open(SAMPLE_DATA, "r") as f:
            return json.load(f)
    return None


def load_pipeline_status() -> dict:
    config = load_config()
    status = {}
    labels = {
        "raw_file": ("Collector", "raw_items.json"),
        "filtered_file": ("Scout", "filtered_items.json"),
        "categorized_file": ("Editor", "categorized_items.json"),
        "weekly_file": ("Curator", "weekly_draft.json"),
    }
    for key, (label, _) in labels.items():
        filepath = OUTPUT_DIR / config["output"][key]
        if filepath.exists():
            stat = filepath.stat()
            status[key] = {
                "exists": True,
                "label": label,
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%b %d, %H:%M"),
                "size": f"{stat.st_size / 1024:.1f} KB",
            }
        else:
            status[key] = {"exists": False, "label": label}
    return status


# ─── Routes ───────────────────────────────────────────────────────────────────


def _newsletter_name(config: dict | None = None) -> str:
    cfg = config or load_config()
    nl = cfg.get("newsletter") or {}
    return nl.get("name") or "Newsletter"


def _hide_reasons(config: dict | None = None) -> list[dict]:
    """Hide reasons defined in config.yaml. Falls back to the universal three
    if the editorial block is missing, so behavior is preserved on older configs."""
    cfg = config or load_config()
    reasons = (cfg.get("editorial") or {}).get("hide_reasons") or []
    if not reasons:
        reasons = [
            {"id": "not_relevant", "label": "Not relevant"},
            {"id": "weak", "label": "Weak"},
            {"id": "already_covered", "label": "Already covered"},
        ]
    return reasons


def _section_order(config: dict) -> list[str]:
    """Section order from config; falls back to a sensible default."""
    sections = config.get("sections") or []
    if isinstance(sections, list) and sections:
        return [s.upper() if isinstance(s, str) else str(s).upper() for s in sections]
    return [
        "HIGHLIGHTS", "VISION PRO", "APP STORE HIGHLIGHTS", "GAMING MARKETPLACE",
        "INVESTMENT", "SOFTWARE", "HARDWARE", "AI AND SIMULATED WORLDS",
        "RESEARCH", "OPINION",
    ]


def group_by_category(items: list[dict], section_order: list[str]) -> "OrderedDict[str, list[dict]]":
    groups: "OrderedDict[str, list[dict]]" = OrderedDict((s, []) for s in section_order)
    for it in items:
        cat = (it.get("category") or "HIGHLIGHTS").upper()
        groups.setdefault(cat, []).append(it)
    # Drop empty sections for display
    return OrderedDict((k, v) for k, v in groups.items() if v)


def load_bucket_state() -> dict:
    if BUCKET_STATE_FILE.exists():
        with open(BUCKET_STATE_FILE, "r") as f:
            return json.load(f)
    return {"spotlight": ""}


def save_bucket_state(state: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(BUCKET_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _counts() -> dict:
    """Counts shown in the nav tabs. We surface counts only where they're
    actionable (Signals = inbound work to triage, Lineup = items waiting to
    publish). Editions and Archive are historical; their counts would just
    be noise."""
    return {
        "signals": len(by_status("pending")),
        "lineup": len(by_status("saved")),
    }


@app.get("/", response_class=HTMLResponse)
async def signals_page():
    config = load_config()
    items = by_status("pending")
    groups = group_by_category(items, _section_order(config))
    return HTMLResponse(content=render_page("signals", None, None, None, groups=groups, counts=_counts()))


@app.get("/lineup", response_class=HTMLResponse)
async def lineup_page():
    config = load_config()
    items = by_status("saved")
    groups = group_by_category(items, _section_order(config))
    state = load_bucket_state()
    return HTMLResponse(
        content=render_page("lineup", None, None, None, groups=groups, counts=_counts(), bucket_state=state)
    )


# Legacy aliases so any open tabs keep working.
@app.get("/saved", response_class=HTMLResponse)
async def saved_alias():
    return await lineup_page()


@app.get("/bucket", response_class=HTMLResponse)
async def bucket_alias():
    return await lineup_page()


@app.get("/archive", response_class=HTMLResponse)
async def archive_page():
    """Archive = items the editor set aside. Replaces the old `/hidden` page."""
    config = load_config()
    items = by_status("hidden")
    groups = group_by_category(items, _section_order(config))
    return HTMLResponse(
        content=render_page("archive", None, None, None, groups=groups, counts=_counts())
    )


@app.get("/hidden", response_class=HTMLResponse)
async def hidden_alias():
    return await archive_page()


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    return HTMLResponse(content=render_page("setup", None, None, None, counts=_counts()))


@app.get("/api/items")
async def get_items():
    return JSONResponse(load_store())


@app.post("/api/items/{item_id}/save")
async def save_item(item_id: str):
    it = update_status(item_id, "saved")
    if not it:
        return JSONResponse({"error": "Item not found"}, status_code=404)
    return JSONResponse({"ok": True, "status": "saved"})


@app.post("/api/items/{item_id}/hide")
async def hide_item(item_id: str):
    it = update_status(item_id, "hidden")
    if not it:
        return JSONResponse({"error": "Item not found"}, status_code=404)
    return JSONResponse({"ok": True, "status": "hidden"})


@app.post("/api/items/{item_id}/hide-reason")
async def set_hide_reason(item_id: str, request: Request):
    """Tag a hidden item with the editor's reason. Used later to train Scout."""
    body = await request.json()
    reason = (body.get("reason") or "").strip().lower()
    allowed = {r["id"] for r in _hide_reasons()}
    if reason and reason not in allowed:
        return JSONResponse({"error": f"reason must be one of {sorted(allowed)} or empty"}, status_code=400)
    it = update_fields(item_id, hide_reason=reason or None)
    if not it:
        return JSONResponse({"error": "Item not found"}, status_code=404)
    return JSONResponse({"ok": True, "hide_reason": reason or None})


@app.get("/api/hide-reasons")
async def get_hide_reasons():
    return JSONResponse({"reasons": _hide_reasons()})


@app.post("/api/items/{item_id}/remove")
async def remove_from_saved(item_id: str):
    """Lineup is the second-layer human decision. Removing here is a decisive 'no for now'
    rather than a re-triage — so it lands in Archive (restorable to Signals from there)."""
    it = update_status(item_id, "hidden")
    if not it:
        return JSONResponse({"error": "Item not found"}, status_code=404)
    return JSONResponse({"ok": True, "status": "hidden"})


@app.post("/api/items/{item_id}/restore")
async def restore_item(item_id: str):
    """Restore an Archive item back to Signals."""
    it = update_status(item_id, "pending")
    if not it:
        return JSONResponse({"error": "Item not found"}, status_code=404)
    return JSONResponse({"ok": True, "status": "pending"})


@app.post("/api/items/{item_id}/include")
async def include_item(item_id: str):
    """Mark a Lineup item as Including (ship in next edition)."""
    it = set_included(item_id, True)
    if not it:
        return JSONResponse({"error": "Item not found"}, status_code=404)
    return JSONResponse({"ok": True, "included_in_next": True})


@app.post("/api/items/{item_id}/hold")
async def hold_item(item_id: str):
    """Mark a Lineup item as Held (do not ship in next edition, keep in Lineup)."""
    it = set_included(item_id, False)
    if not it:
        return JSONResponse({"error": "Item not found"}, status_code=404)
    return JSONResponse({"ok": True, "included_in_next": False})


@app.post("/api/items/{item_id}/edit")
async def edit_item(item_id: str, request: Request):
    body = await request.json()
    summary = body.get("summary", "")
    it = update_fields(item_id, summary=summary)
    if not it:
        return JSONResponse({"error": "Item not found"}, status_code=404)
    return JSONResponse({"ok": True})


@app.post("/api/bucket/spotlight")
async def save_spotlight(request: Request):
    body = await request.json()
    state = load_bucket_state()
    state["spotlight"] = body.get("spotlight", "")
    save_bucket_state(state)
    return JSONResponse({"ok": True})


@app.post("/api/bucket/draft-spotlight")
async def draft_spotlight():
    """Ask the Curator agent to draft a SPOTLIGHT paragraph from the items currently
    flagged as Including in the Lineup (i.e., what would actually ship in the next edition)."""
    included = by_status_for_publish()
    if not included:
        return JSONResponse(
            {"error": "Nothing is Including right now — save and include some items first."},
            status_code=400,
        )

    system_prompt = load_curator_prompt()

    articles_text = ""
    for item in included:
        articles_text += (
            f"---\n"
            f"Title: {item.get('title', '')}\n"
            f"Source: {item.get('source', '')}\n"
            f"Category: {item.get('category', 'HIGHLIGHTS')}\n"
            f"Significance: {item.get('significance', 'medium')}\n"
            f"Summary: {item.get('summary', '')}\n"
            f"---\n"
        )

    newsletter_name = _newsletter_name()
    user_message = (
        f"These are the {len(included)} articles slated for the next edition of "
        f"{newsletter_name}. Draft only the SPOTLIGHT — a 2–3 sentence paragraph summarizing "
        f"the week's most significant stories across these items. Use the editorial tone "
        f"described in your system prompt (NYT / Bloomberg / Reuters / FT).\n\n"
        f"{articles_text}\n"
        f'Respond with ONLY this JSON: {{"spotlight": "<2-3 sentence paragraph>"}}'
    )

    try:
        result = call_llm_json(system_prompt, user_message, stage="curator")
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"error": f"Agent error: {e}"}, status_code=500)

    spotlight = (result.get("spotlight") or "").strip()
    if not spotlight:
        return JSONResponse(
            {"error": "Agent returned no spotlight text."}, status_code=500
        )

    state = load_bucket_state()
    state["spotlight"] = spotlight
    save_bucket_state(state)

    return JSONResponse({"ok": True, "spotlight": spotlight})


# ─── Refresh (run the daily pipeline from the UI) ─────────────────────────────


def _load_refresh_status() -> dict:
    if not REFRESH_STATUS_FILE.exists():
        return {"state": "idle"}
    try:
        with open(REFRESH_STATUS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"state": "idle"}


@app.get("/api/refresh/status")
async def refresh_status_endpoint():
    return JSONResponse(_load_refresh_status())


@app.post("/api/refresh")
async def trigger_refresh():
    """Spawn daily.py as a subprocess. Idempotent: returns 409 if one is already in flight."""
    status = _load_refresh_status()
    if status.get("state") == "running":
        return JSONResponse(
            {"error": "A refresh is already running.", "status": status},
            status_code=409,
        )

    if not VENV_PY.exists():
        return JSONResponse({"error": f"Python not found at {VENV_PY}"}, status_code=500)

    started = datetime.now(timezone.utc).isoformat()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(REFRESH_STATUS_FILE, "w") as f:
        json.dump({
            "state": "running",
            "stage": "starting",
            "message": "Starting refresh…",
            "started_at": started,
            "updated_at": started,
        }, f, indent=2)

    try:
        subprocess.Popen(
            [str(VENV_PY), str(DAILY_SCRIPT)],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except OSError as e:
        with open(REFRESH_STATUS_FILE, "w") as f:
            json.dump({
                "state": "error",
                "error": str(e),
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }, f, indent=2)
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"ok": True, "started_at": started}, status_code=202)


# ─── Automation (launchd schedule management from Settings) ──────────────────


def _automation_label() -> str:
    cfg = load_config()
    slug = (cfg.get("newsletter") or {}).get("slug") or "scout"
    return f"com.scout.{slug}"


def _automation_plist_path() -> Path:
    return LAUNCHD_DIR / f"{_automation_label()}.plist"


def _automation_supported() -> bool:
    return sys.platform == "darwin"


def _automation_loaded(label: str | None = None) -> bool:
    if not _automation_supported():
        return False
    label = label or _automation_label()
    try:
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=5
        )
        return any(label in line for line in result.stdout.splitlines())
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _automation_last_run() -> dict:
    """Parse the latest 'Daily Agent Run' header + 'Added N new pending' tail
    from agent.log so the UI can show when automation last fired."""
    log_file = OUTPUT_DIR / "agent.log"
    if not log_file.exists():
        return {"last_run_at": None, "last_run_added": None}
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()[-60:]
    except OSError:
        return {"last_run_at": None, "last_run_added": None}

    last_run_at = None
    last_added = None
    for line in lines:
        m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ", line)
        if not m:
            continue
        if "Daily Agent Run" in line:
            last_run_at = m.group(1)
        m2 = re.search(r"Added (\d+) new pending", line)
        if m2:
            last_added = int(m2.group(1))
    return {"last_run_at": last_run_at, "last_run_added": last_added}


def _automation_status_dict() -> dict:
    label = _automation_label()
    return {
        "platform_supported": _automation_supported(),
        "installed": _automation_plist_path().exists(),
        "loaded": _automation_loaded(label),
        "label": label,
        "schedule": "Daily at 8:00 AM",
        **_automation_last_run(),
    }


@app.get("/api/automation/status")
async def automation_status():
    return JSONResponse(_automation_status_dict())


@app.post("/api/automation/enable")
async def automation_enable():
    if not _automation_supported():
        return JSONResponse(
            {"error": "Automation requires macOS (launchd). On other systems, schedule src/daily.py with cron or systemd."},
            status_code=400,
        )
    if not PLIST_TEMPLATE.exists():
        return JSONResponse(
            {"error": f"Plist template missing at {PLIST_TEMPLATE}"}, status_code=500
        )

    label = _automation_label()
    plist_path = _automation_plist_path()

    template = PLIST_TEMPLATE.read_text()
    rendered = (
        template.replace("__PROJECT_DIR__", str(BASE_DIR))
        .replace("__PLIST_LABEL__", label)
    )

    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    if _automation_loaded(label):
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    plist_path.write_text(rendered)
    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return JSONResponse(
            {"error": f"launchctl load failed: {result.stderr.strip() or 'unknown error'}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "label": label})


@app.post("/api/automation/disable")
async def automation_disable():
    if not _automation_supported():
        return JSONResponse({"error": "Automation requires macOS."}, status_code=400)
    plist_path = _automation_plist_path()
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
    return JSONResponse({"ok": True})


# ─── Manual URL ingest ────────────────────────────────────────────────────────


def _domain_label(url: str) -> str:
    """e.g. 'blogs.autodesk.com' → 'Autodesk Blogs'."""
    from urllib.parse import urlparse

    netloc = urlparse(url).netloc.lower()
    if not netloc:
        return "Web"
    netloc = netloc.removeprefix("www.")
    parts = netloc.split(".")
    if len(parts) >= 2:
        # Keep subdomain + sld so 'blogs.autodesk.com' → 'Autodesk Blogs'
        sld = parts[-2].capitalize()
        sub = parts[0].capitalize() if len(parts) > 2 and parts[0] != parts[-2] else ""
        return f"{sld} {sub}".strip() if sub else sld
    return parts[0].capitalize()


def _extract_url(url: str) -> dict | None:
    """Fetch and extract article content. Returns None if extraction fails."""
    try:
        import httpx
        import trafilatura
    except ImportError:
        return None

    try:
        with httpx.Client(follow_redirects=True, timeout=10) as client:
            resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 Scout/1.0"})
    except (httpx.HTTPError, OSError):
        return None

    if resp.status_code != 200:
        return None

    extracted = trafilatura.extract(
        resp.text,
        output_format="json",
        with_metadata=True,
        include_comments=False,
    )
    if not extracted:
        return None

    try:
        meta = json.loads(extracted)
    except json.JSONDecodeError:
        return None

    return {
        "title": (meta.get("title") or "").strip(),
        "text": (meta.get("text") or "").strip(),
        "date": (meta.get("date") or "").strip(),
        "sitename": (meta.get("sitename") or "").strip(),
    }


@app.post("/api/items/ingest-url")
async def ingest_url(request: Request):
    """Manually ingest a single article by URL. Skips the Scout relevance filter
    (the editor is the relevance filter here, by virtue of explicitly adding it)
    and runs the item through the Editor before landing it in Signals."""
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "URL is required."}, status_code=400)
    if not (url.startswith("http://") or url.startswith("https://")):
        return JSONResponse({"error": "URL must start with http:// or https://."}, status_code=400)

    # Dedup against the store before any LLM work.
    expected_id = make_id({"link": url})
    existing = find_by_id(expected_id)
    if existing:
        return JSONResponse(
            {
                "error": "already_present",
                "id": expected_id,
                "status": existing.get("status"),
                "title": existing.get("title"),
            },
            status_code=409,
        )

    # Manual paste fallback path: user provides title + content directly.
    title = (body.get("title") or "").strip()
    text = (body.get("content") or "").strip()
    date_str = (body.get("published") or "").strip()
    source = (body.get("source") or "").strip()

    if not title or not text:
        extracted = _extract_url(url)
        if extracted is None:
            return JSONResponse(
                {
                    "error": "Could not fetch or extract this URL. Paste the title and a snippet manually instead.",
                    "fallback": True,
                },
                status_code=422,
            )
        title = title or extracted["title"]
        text = text or extracted["text"]
        date_str = date_str or extracted["date"]
        source = source or extracted["sitename"]

    if not title:
        return JSONResponse(
            {"error": "Could not determine a title for this page.", "fallback": True},
            status_code=422,
        )
    if not text:
        return JSONResponse(
            {"error": "Could not extract any body content from this page.", "fallback": True},
            status_code=422,
        )

    raw_item = {
        "title": title,
        "link": url,
        "source": source or _domain_label(url),
        "description": text[:2000],
        "published": date_str,
        "category_hint": "",
    }

    try:
        editor_prompt = load_editor_prompt()
        categorized = categorize_items([raw_item], editor_prompt)
    except (ValueError, RuntimeError) as e:
        return JSONResponse({"error": f"Editor agent failed: {e}"}, status_code=500)

    if not categorized:
        return JSONResponse({"error": "Editor returned no result."}, status_code=500)

    added, _ = add_pending(categorized)
    if not added:
        # Caught by dedup-in-add_pending — unlikely after the find_by_id check above
        # but possible if the title matches an existing item from a different URL.
        return JSONResponse(
            {"error": "An item with this title or link already exists."},
            status_code=409,
        )

    item = categorized[0]
    return JSONResponse(
        {
            "ok": True,
            "id": item.get("id") or make_id(item),
            "title": item.get("title"),
            "category": item.get("category"),
        }
    )


# ─── Feeds (RSS sources editor) ───────────────────────────────────────────────


def _read_sources() -> list[dict]:
    cfg = load_config()
    return list(cfg.get("sources") or [])


def _write_sources(sources: list[dict]) -> None:
    """Text-based YAML edit: replace only the `sources:` block to preserve comments
    and ordering elsewhere in config.yaml. Loses any inline comments inside the
    sources block itself, which we accept as the trade-off."""
    text = CONFIG_PATH.read_text()
    # Match `sources:` plus all subsequent content up to the next top-level
    # key OR the next top-level comment (whichever comes first) or EOF.
    # `(?=^[#A-Za-z_])` stops at either a top-level letter (key) or `#` (comment),
    # so adjacent comment blocks aren't swallowed when we replace.
    pattern = re.compile(r"(?ms)^sources:.*?(?=^[#A-Za-z_]|\Z)")
    if not pattern.search(text):
        raise ValueError("Could not locate 'sources:' block in config.yaml")

    block = ["sources:\n"]
    for s in sources:
        name = (s.get("name") or "").strip()
        url = (s.get("url") or "").strip()
        hint = (s.get("category_hint") or "").strip()
        if not url:
            continue
        block.append(f'  - name: "{_yaml_escape(name or url)}"\n')
        block.append(f'    url: "{_yaml_escape(url)}"\n')
        if hint:
            block.append(f'    category_hint: "{_yaml_escape(hint)}"\n')
        block.append("\n")

    new_text = pattern.sub("".join(block), text, count=1)
    CONFIG_PATH.write_text(new_text)


def _yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


@app.get("/api/feeds")
async def get_feeds():
    return JSONResponse({"feeds": _read_sources()})


@app.post("/api/feeds")
async def save_feeds(request: Request):
    body = await request.json()
    feeds = body.get("feeds")
    if not isinstance(feeds, list):
        return JSONResponse({"error": "feeds must be a list"}, status_code=400)

    cleaned: list[dict] = []
    for f in feeds:
        if not isinstance(f, dict):
            continue
        url = (f.get("url") or "").strip()
        name = (f.get("name") or "").strip()
        hint = (f.get("category_hint") or "").strip()
        if not url:
            continue
        if not (url.startswith("http://") or url.startswith("https://")):
            return JSONResponse(
                {"error": f"URL must start with http(s)://: {url}"}, status_code=400
            )
        cleaned.append({"name": name or url, "url": url, "category_hint": hint})

    try:
        _write_sources(cleaned)
    except (OSError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    return JSONResponse({"ok": True, "feeds": cleaned, "count": len(cleaned)})


@app.post("/api/export")
async def export_newsletter():
    """Publish the next edition.

    Ships only Lineup items flagged as Including. Held items stay in the Lineup for
    a future edition. Published items get stamped with the edition_id and an
    Edition record is written to output/archive/.
    """
    config = load_config()
    included = by_status_for_publish()
    if not included:
        return JSONResponse(
            {"error": "Nothing is Including right now — save and include some items first."},
            status_code=400,
        )

    bucket_state = load_bucket_state()
    spotlight = (bucket_state.get("spotlight") or "").strip()

    today = datetime.now().strftime("%Y-%m-%d")
    edition_number = _next_edition_number()
    edition_id = f"edition-{edition_number or today.replace('-', '')}"

    groups = group_by_category(included, _section_order(config))

    newsletter_name = _newsletter_name(config)
    # Markdown body in paragraph style — same shape regardless of which newsletter.
    lines = [newsletter_name, f"Week of {today}", "", "SPOTLIGHT", ""]
    if spotlight:
        lines.extend([spotlight, ""])

    for category, items in groups.items():
        lines.extend([category.upper(), ""])
        for it in items:
            summary = (it.get("summary") or "").strip()
            if summary:
                lines.extend([summary, ""])

    markdown = "\n".join(lines).rstrip() + "\n"

    def slim(it: dict) -> dict:
        return {
            "id": it["id"],
            "title": it.get("title"),
            "link": it.get("link"),
            "source": it.get("source"),
            "category": it.get("category"),
            "summary": it.get("summary"),
        }

    # Inline item dicts under each section so the existing archive view renders them.
    sections_payload: dict[str, list[dict]] = {
        category: [slim(it) for it in items] for category, items in groups.items()
    }

    edition = {
        "title": f"{newsletter_name} #{edition_number}" if edition_number else f"{newsletter_name} — {today}",
        "edition_number": edition_number,
        "date": today,
        "spotlight": spotlight,
        "item_ids": [it["id"] for it in included],
        "sections": sections_payload,
        "items": [slim(it) for it in included],
        "raw_content": markdown,
    }

    EDITIONS_DIR.mkdir(parents=True, exist_ok=True)
    with open(EDITIONS_DIR / f"{edition_id}.json", "w") as f:
        json.dump(edition, f, indent=2, ensure_ascii=False)
    md_path = EDITIONS_DIR / f"{edition_id}.md"
    with open(md_path, "w") as f:
        f.write(markdown)

    # Mark only the Including items published — Held items stay in Saved.
    for it in included:
        update_status(it["id"], "published", edition_id=edition_id, included_in_next=None)

    save_bucket_state({"spotlight": ""})

    return JSONResponse({
        "ok": True,
        "edition_id": edition_id,
        "edition_number": edition_number,
        "file": str(md_path),
        "published_count": len(included),
    })


def _next_edition_number() -> str:
    """Find the highest existing edition number in the archive and return next."""
    EDITIONS_DIR.mkdir(parents=True, exist_ok=True)
    highest = 0
    for f in EDITIONS_DIR.glob("edition-*.json"):
        try:
            with open(f, "r") as fh:
                data = json.load(fh)
            num = int(str(data.get("edition_number", "")).strip())
            if num > highest:
                highest = num
        except (ValueError, json.JSONDecodeError, KeyError, OSError):
            continue
    return str(highest + 1) if highest else ""


# ─── Editions Routes ─────────────────────────────────────────────────────────
# "Editions" is now distinct from "Archive": Editions = published artifacts,
# Archive = items the editor set aside. Old /archive/* edition URLs redirect.


@app.get("/editions", response_class=HTMLResponse)
async def editions_page():
    editions = load_editions_list()
    return HTMLResponse(content=render_page("editions_list", None, None, None, editions=editions))


@app.get("/editions/new", response_class=HTMLResponse)
async def editions_new_page():
    return HTMLResponse(content=render_page("editions_new", None, None, None))


@app.post("/editions/new", response_class=HTMLResponse)
async def editions_create(request: Request):
    form = await request.form()
    edition_number = form.get("edition_number", "").strip()
    date = form.get("date", "").strip()
    content = form.get("content", "").strip()

    if not content:
        return HTMLResponse(content=render_page("editions_new", None, None, None, error="Content is required."))

    if not edition_number:
        return HTMLResponse(content=render_page("editions_new", None, None, None, error="Edition number is required."))

    title = f"{_newsletter_name()} #{edition_number}"
    edition = parse_edition(title, edition_number, date, content)

    EDITIONS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"edition-{edition_number or datetime.now().strftime('%Y%m%d')}.json"
    filepath = EDITIONS_DIR / filename
    with open(filepath, "w") as f:
        json.dump(edition, f, indent=2, ensure_ascii=False)

    editions = load_editions_list()
    return HTMLResponse(
        content=render_page("edition_view", None, None, None, edition=edition, editions=editions)
    )


@app.get("/editions/{edition_id}", response_class=HTMLResponse)
async def edition_view(edition_id: str):
    filepath = EDITIONS_DIR / f"{edition_id}.json"
    if not filepath.exists():
        editions = load_editions_list()
        return HTMLResponse(content=render_page("editions_list", None, None, None, editions=editions))
    with open(filepath, "r") as f:
        edition = json.load(f)
    editions = load_editions_list()
    return HTMLResponse(content=render_page("edition_view", None, None, None, edition=edition, editions=editions))


@app.delete("/api/editions/{edition_id}")
async def edition_delete(edition_id: str):
    filepath = EDITIONS_DIR / f"{edition_id}.json"
    if filepath.exists():
        filepath.unlink()
    return JSONResponse({"ok": True})


# ─── Legacy /archive/* edition URLs redirect to /editions/* ──────────────────


@app.get("/archive/new", response_class=HTMLResponse)
async def archive_new_redirect():
    return RedirectResponse(url="/editions/new", status_code=301)


@app.get("/archive/{edition_id}", response_class=HTMLResponse)
async def archive_id_redirect(edition_id: str):
    return RedirectResponse(url=f"/editions/{edition_id}", status_code=301)


@app.delete("/api/archive/{edition_id}")
async def archive_delete_alias(edition_id: str):
    return await edition_delete(edition_id)


def load_editions_list() -> list:
    """Load list of published editions, sorted by date descending."""
    EDITIONS_DIR.mkdir(parents=True, exist_ok=True)
    editions = []
    for f in sorted(EDITIONS_DIR.glob("edition-*.json"), reverse=True):
        with open(f, "r") as fh:
            data = json.load(fh)
            editions.append({
                "id": f.stem,
                "title": data.get("title", "Untitled"),
                "edition_number": data.get("edition_number", ""),
                "date": data.get("date", ""),
                "item_count": len(data.get("items", [])),
                "section_count": len(data.get("sections", {})),
            })
    return editions


def parse_edition(title: str, edition_number: str, date: str, raw_content: str) -> dict:
    """Parse pasted newsletter content into structured format.
    Handles: markdown, HTML, plain text with ALL CAPS headers, and mixed formats.
    """
    import re

    edition = {
        "title": title or f"{_newsletter_name()} #{edition_number}",
        "edition_number": edition_number,
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "raw_content": raw_content,
        "items": [],
        "sections": {},
    }

    # Pre-process: convert HTML links to markdown-style for uniform handling
    # <a href="url">text</a> → [text](url)
    content = re.sub(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        r'[\2](\1)',
        raw_content,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Strip other HTML tags but preserve content
    content = re.sub(r'<br\s*/?>', '\n', content, flags=re.IGNORECASE)
    content = re.sub(r'</?(?:p|div|span|b|strong|i|em|u|li|ul|ol|h[1-6])[^>]*>', '', content, flags=re.IGNORECASE)

    # Detect section headers:
    # 1. Markdown: ## Section Name
    # 2. ALL CAPS lines (at least 3 chars, mostly uppercase/spaces)
    # 3. HTML h2/h3 (already stripped tags above, but content remains)
    lines = content.split("\n")
    current_section = "General"
    current_items = []
    paragraph_buffer = []

    def flush_paragraph():
        """Flush accumulated paragraph lines as a single item."""
        if paragraph_buffer:
            text = " ".join(paragraph_buffer).strip()
            if len(text) > 15:  # Skip very short fragments
                item = parse_item(text)
                current_items.append(item)
                edition["items"].append(item)
            paragraph_buffer.clear()

    def is_section_header(line: str) -> bool:
        """Detect ALL CAPS section headers or markdown ## headers."""
        stripped = line.strip()
        if not stripped or len(stripped) < 3:
            return False
        # Markdown header
        if stripped.startswith("## "):
            return True
        # ALL CAPS (allow emojis, spaces, &, /, parens)
        text_only = re.sub(r'[^\w\s]', '', stripped)
        if len(text_only) >= 3 and text_only == text_only.upper() and not text_only.isdigit():
            # Must have at least some letters
            if re.search(r'[A-Z]', text_only):
                return True
        return False

    def extract_header_text(line: str) -> str:
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped[3:].strip()
        # Clean up ALL CAPS — remove emojis and extra whitespace
        cleaned = re.sub(r'[^\w\s/&\'-]', '', stripped).strip()
        return cleaned

    for line in lines:
        stripped = line.strip()

        # Empty line — flush current paragraph
        if not stripped:
            flush_paragraph()
            continue

        # Check for section header
        if is_section_header(stripped):
            flush_paragraph()
            if current_items:
                edition["sections"][current_section] = current_items
            current_section = extract_header_text(stripped)
            current_items = []
            continue

        # Bullet point — flush paragraph first, then add as item
        if stripped.startswith("- ") or stripped.startswith("• ") or stripped.startswith("* "):
            flush_paragraph()
            item_text = stripped[2:].strip()
            item = parse_item(item_text)
            current_items.append(item)
            edition["items"].append(item)
            continue

        # Bold title line: **Title** rest (markdown item)
        if stripped.startswith("**") and "**" in stripped[2:]:
            flush_paragraph()
            item = parse_item(stripped)
            current_items.append(item)
            edition["items"].append(item)
            continue

        # Standalone URL line — attach to previous item if possible
        if re.match(r'^https?://', stripped):
            if current_items and not current_items[-1].get("link"):
                current_items[-1]["link"] = stripped
            continue

        # Regular text — accumulate as paragraph
        paragraph_buffer.append(stripped)

    # Flush final state
    flush_paragraph()
    if current_items:
        edition["sections"][current_section] = current_items

    return edition


def parse_item(text: str) -> dict:
    """Parse a single newsletter item into structured fields.
    Handles markdown links [text](url) and bold **title** patterns.
    """
    import re

    item = {"text": text, "title": "", "summary": "", "source": "", "link": ""}

    # Extract markdown links: [text](url)
    links = re.findall(r'\[([^\]]*)\]\((https?://[^)]+)\)', text)
    if links:
        item["link"] = links[0][1]  # First link URL
        # Remove link markup from text for cleaner display
        clean_text = re.sub(r'\[([^\]]*)\]\(https?://[^)]+\)', r'\1', text)
    else:
        clean_text = text

    # Extract bare URLs
    if not item["link"]:
        url_match = re.search(r'(https?://\S+)', clean_text)
        if url_match:
            item["link"] = url_match.group(1).rstrip('.,;)')
            clean_text = clean_text.replace(url_match.group(0), '').strip()

    # Try to extract bold title: **Title** rest
    bold_match = re.match(r"\*\*(.+?)\*\*[:\s—–-]*(.*)", clean_text)
    if bold_match:
        item["title"] = bold_match.group(1).strip()
        item["summary"] = bold_match.group(2).strip()
    else:
        # Use first sentence or first 80 chars as title
        sentences = clean_text.split(". ")
        if len(sentences) > 1 and len(sentences[0]) < 100:
            item["title"] = sentences[0].rstrip(".")
            item["summary"] = ". ".join(sentences[1:]).strip()
        else:
            item["title"] = clean_text[:80].rstrip()
            item["summary"] = clean_text

    # Try to extract source in parens at end: (Source Name)
    source_match = re.search(r'\(([^)]{2,30})\)\s*$', item["summary"])
    if source_match:
        potential_source = source_match.group(1)
        # Only treat as source if it looks like a publication name (no URLs, not too long)
        if not potential_source.startswith("http") and len(potential_source) < 30:
            item["source"] = potential_source
            item["summary"] = item["summary"][: source_match.start()].strip()

    return item


def update_item_status(item_id: int, status: str):
    draft_data = load_weekly_draft()
    if not draft_data or item_id >= len(draft_data["items"]):
        return JSONResponse({"error": "Item not found"}, status_code=404)
    draft_data["items"][item_id]["status"] = status
    save_draft(draft_data)
    return JSONResponse({"ok": True, "status": status})


def save_draft(draft_data: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    draft_file = OUTPUT_DIR / "weekly_draft.json"
    with open(draft_file, "w") as f:
        json.dump(draft_data, f, indent=2, ensure_ascii=False)


# ─── Rendering ────────────────────────────────────────────────────────────────


def render_page(mode: str, draft: dict | None, items: list | None, status: dict | None, **kwargs) -> str:
    counts = kwargs.get("counts") or _counts()
    # "active tab" mapping for sub-pages
    active = mode
    if mode in ("editions_list", "editions_new", "edition_view"):
        active = "editions"
    nav = render_nav(active, counts)

    if mode == "setup":
        content = render_setup()
    elif mode == "status":
        content = render_status(status)
    elif mode == "editions_list":
        content = render_editions_list(kwargs.get("editions", []))
    elif mode == "editions_new":
        content = render_editions_form(kwargs.get("error"))
    elif mode == "edition_view":
        content = render_edition_view(kwargs.get("edition"), kwargs.get("editions", []))
    elif mode == "signals":
        content = render_signals(kwargs.get("groups") or OrderedDict())
    elif mode == "lineup":
        content = render_lineup(
            kwargs.get("groups") or OrderedDict(),
            kwargs.get("bucket_state") or {},
        )
    elif mode == "archive":
        content = render_archive(kwargs.get("groups") or OrderedDict())
    else:
        content = render_signals(OrderedDict())

    config_blob = json.dumps({"hideReasons": _hide_reasons()})
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Scout</title>
{STYLES}
</head>
<body>
<div class="app">
{nav}
<main class="main">
{content}
</main>
</div>
<script>window.SCOUT_CONFIG = {config_blob};</script>
{SCRIPTS}
</body>
</html>"""


def render_nav(active: str, counts: dict | None = None) -> str:
    counts = counts or {}

    def tab(href: str, label: str, key: str, count_key: str | None) -> str:
        is_active = active == key
        cls = "nav-tab active" if is_active else "nav-tab"
        count = counts.get(count_key, 0) if count_key else None
        count_html = f'<span class="nav-tab-count">{count}</span>' if count_key else ""
        return (
            f'<a href="{href}" class="{cls}">'
            f'<span class="nav-tab-label">{label}</span>{count_html}</a>'
        )

    return f"""
<nav class="nav">
    <a href="/" class="nav-brand">
        <span class="nav-icon">◈</span>
        <span class="nav-title">Scout</span>
    </a>
    <div class="nav-tabs">
        {tab("/", "Signals", "signals", "signals")}
        {tab("/lineup", "Lineup", "lineup", "lineup")}
        {tab("/editions", "Editions", "editions", None)}
        {tab("/archive", "Archive", "archive", None)}
        <span class="nav-tab-divider"></span>
        {tab("/setup", "Settings", "setup", None)}
    </div>
</nav>"""


def _humanize_iso(iso_string: str) -> str:
    """e.g. '2 hours ago' from an ISO timestamp. Empty string if unparseable."""
    if not iso_string:
        return ""
    try:
        ts = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        m = secs // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if secs < 86400:
        h = secs // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = secs // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"


def _render_pipeline_panel() -> str:
    status = _load_refresh_status()
    state = status.get("state", "idle")
    last_run = ""
    last_finished = status.get("finished_at") or ""
    if last_finished:
        humanized = _humanize_iso(last_finished)
        added = status.get("added")
        msg = status.get("message") or ""
        bits = [f"Last run {humanized}"] if humanized else []
        if isinstance(added, int):
            bits.append(f"{added} new {'story' if added == 1 else 'stories'}")
        elif msg:
            bits.append(msg)
        last_run = " · ".join(bits)

    running = state == "running"
    btn_label = "Refreshing…" if running else "✦ Refresh now"
    btn_disabled = " disabled" if running else ""
    progress_msg = status.get("message", "") if running else ""

    return f"""
<section class="settings-panel">
    <div class="settings-panel-header">
        <div>
            <h2>Pipeline</h2>
            <p class="settings-panel-sub" id="pipeline-last-run">{last_run or 'No refresh runs yet.'}</p>
        </div>
        <button onclick="triggerRefresh()" class="btn-primary" id="refresh-btn"{btn_disabled}>{btn_label}</button>
    </div>
    <div class="pipeline-progress {'visible' if running else ''}" id="pipeline-progress">
        <span class="pipeline-progress-dot"></span>
        <span id="pipeline-progress-msg">{progress_msg}</span>
    </div>
</section>"""


def _render_feeds_editor() -> str:
    sources = _read_sources()
    rows = ""
    for i, s in enumerate(sources):
        rows += f"""
<div class="feed-row" data-idx="{i}">
    <input class="feed-input feed-name" placeholder="Name" value="{(s.get('name') or '').replace('"','&quot;')}">
    <input class="feed-input feed-url" placeholder="https://…" value="{(s.get('url') or '').replace('"','&quot;')}">
    <input class="feed-input feed-hint" placeholder="Category hint (optional)" value="{(s.get('category_hint') or '').replace('"','&quot;')}">
    <button class="feed-remove" onclick="removeFeedRow(this)" title="Remove">✕</button>
</div>"""

    return f"""
<section class="settings-panel">
    <div class="settings-panel-header">
        <div>
            <h2>RSS Feeds</h2>
            <p class="settings-panel-sub"><span id="feeds-count">{len(sources)}</span> feeds — edit, add, or remove. Changes apply on next refresh.</p>
        </div>
        <button onclick="saveFeeds()" class="btn-primary" id="feeds-save-btn">Save changes</button>
    </div>
    <div id="feeds-list" class="feeds-list">{rows}</div>
    <button onclick="addFeedRow()" class="btn-ghost" id="feeds-add-btn">+ Add feed</button>
    <div id="feeds-banner" class="export-banner hidden"></div>
</section>"""


def _render_models_panel() -> str:
    """Read-only summary of which model is in use for each stage.

    Read straight from llm.resolve_llm_settings so this reflects the *effective*
    config (including env-var fallbacks), not just what's literally typed in
    config.yaml. Useful for verifying your setup at a glance.
    """
    from llm import resolve_llm_settings  # local import to avoid circular at module load

    stages = [
        ("Scout", "scout", "Relevance filter on every collected article."),
        ("Editor", "editor", "Categorizes and writes the paragraph summary."),
        ("Curator", "curator", "Drafts the SPOTLIGHT when invoked from the Lineup."),
    ]
    rows = ""
    for label, stage, note in stages:
        s = resolve_llm_settings(stage)
        provider = s.get("provider", "—")
        model = s.get("model") or "<provider default>"
        api_base = s.get("api_base") or ""
        endpoint = f' · <span class="model-endpoint">{api_base}</span>' if api_base else ""
        rows += f"""
<tr>
    <td class="pipeline-table-stage"><span>{label}</span></td>
    <td>
        <span class="model-provider">{provider}</span>
        <span class="model-name">{model}</span>{endpoint}
    </td>
    <td class="pipeline-table-tune">{note}</td>
</tr>"""

    return f"""
<section class="settings-panel">
    <div class="settings-panel-header">
        <div>
            <h2>Models in use</h2>
            <p class="settings-panel-sub">Effective LLM settings per agent. Edit <code>llm:</code> in <code>config.yaml</code> to change.</p>
        </div>
    </div>
    <table class="pipeline-table">
        <thead>
            <tr>
                <th>Agent</th>
                <th>Provider · Model</th>
                <th>Role</th>
            </tr>
        </thead>
        <tbody>{rows}
        </tbody>
    </table>
</section>"""


def _render_automation_panel() -> str:
    status = _automation_status_dict()
    supported = status["platform_supported"]
    installed = status["installed"]
    loaded = status["loaded"]

    if not supported:
        return """
<section class="settings-panel">
    <div class="settings-panel-header">
        <div>
            <h2>Automation</h2>
            <p class="settings-panel-sub">Run the pipeline automatically each morning so Signals fills up overnight.</p>
        </div>
    </div>
    <p class="automation-note">
        Automation uses macOS launchd. On Linux or Windows, schedule
        <code>src/daily.py</code> with cron, systemd, or your scheduler of choice.
    </p>
</section>"""

    if installed and loaded:
        last_run = status.get("last_run_at") or "Not yet — first run will happen at the next scheduled time."
        added = status.get("last_run_added")
        added_str = (
            f" · {added} new {'story' if added == 1 else 'stories'} added"
            if isinstance(added, int)
            else ""
        )
        return f"""
<section class="settings-panel">
    <div class="settings-panel-header">
        <div>
            <h2>Automation</h2>
            <p class="settings-panel-sub">Scheduled daily at 8:00 AM · <code>{status['label']}</code></p>
        </div>
        <button onclick="disableAutomation()" class="btn-secondary" id="automation-btn">Disable</button>
    </div>
    <p class="automation-note"><span class="automation-dot automation-dot--on"></span> Last automatic run: {last_run}{added_str}</p>
</section>"""

    return """
<section class="settings-panel">
    <div class="settings-panel-header">
        <div>
            <h2>Automation</h2>
            <p class="settings-panel-sub">Run the pipeline automatically each morning so Signals fills up overnight. You can still click ✦ Refresh manually anytime.</p>
        </div>
        <button onclick="enableAutomation()" class="btn-primary" id="automation-btn">Enable daily refresh</button>
    </div>
    <p class="automation-note"><span class="automation-dot automation-dot--off"></span> Not scheduled — Scout only runs when you click ✦ Refresh.</p>
</section>"""


def _render_pipeline_table() -> str:
    rows = [
        ("Collector", "📡", "Pulls RSS feeds and dedups against your store.", "RSS Feeds — above"),
        ("Scout", "🔍", "Filters incoming items for relevance to your newsletter.", "Topics, focus, exclusions (config.yaml — UI coming soon)"),
        ("Editor", "✏️", "Categorizes each item into a section and writes the summary paragraph.", "Section list, tone (config.yaml — UI coming soon)"),
        ("Curator", "📋", "Drafts the SPOTLIGHT when invoked from the Lineup.", "Section order, voice (config.yaml — UI coming soon)"),
        ("Dashboard", "👁️", "Where you triage, compose, and publish.", "Newsletter name (config.yaml)"),
    ]
    body = ""
    for name, icon, what, change in rows:
        body += f"""
<tr>
    <td class="pipeline-table-stage"><span class="pipeline-table-icon">{icon}</span><span>{name}</span></td>
    <td>{what}</td>
    <td class="pipeline-table-tune">{change}</td>
</tr>"""
    return f"""
<section class="settings-panel">
    <div class="settings-panel-header">
        <div>
            <h2>Editorial pipeline</h2>
            <p class="settings-panel-sub">What each stage does — and what you can tune.</p>
        </div>
    </div>
    <table class="pipeline-table">
        <thead>
            <tr>
                <th>Stage</th>
                <th>What it does</th>
                <th>What you can change</th>
            </tr>
        </thead>
        <tbody>{body}
        </tbody>
    </table>
</section>"""


def render_setup() -> str:
    feeds_editor = _render_feeds_editor()
    automation_panel = _render_automation_panel()
    models_panel = _render_models_panel()
    pipeline_table = _render_pipeline_table()
    return f"""
<div class="page-header">
    <h1>Settings</h1>
    <p class="subtitle">Manage feeds, automation, and editorial behavior.</p>
</div>

{feeds_editor}

{automation_panel}

{models_panel}

{pipeline_table}

<details class="settings-details">
    <summary>First-time setup</summary>
    <p class="settings-details-hint">If you're setting up Scout on a new machine.</p>

<div class="timeline">
    <div class="timeline-step">
        <div class="timeline-marker">
            <span class="timeline-num">1</span>
            <div class="timeline-line"></div>
        </div>
        <div class="timeline-content">
            <h2>Clone the Repo</h2>
            <p>Clone your fork of Scout to your Mac.</p>
            <div class="code-block"><span class="code-comment"># Clone</span>
git clone &lt;your-fork-url&gt; scout
cd scout</div>
        </div>
    </div>

    <div class="timeline-step">
        <div class="timeline-marker">
            <span class="timeline-num">2</span>
            <div class="timeline-line"></div>
        </div>
        <div class="timeline-content">
            <h2>Create Environment</h2>
            <p>Set up a Python virtual environment and install dependencies.</p>
            <div class="code-block"><span class="code-comment"># Create virtual environment</span>
python3 -m venv .venv
source .venv/bin/activate

<span class="code-comment"># Install dependencies</span>
pip install -r requirements.txt</div>
        </div>
    </div>

    <div class="timeline-step">
        <div class="timeline-marker">
            <span class="timeline-num">3</span>
            <div class="timeline-line"></div>
        </div>
        <div class="timeline-content">
            <h2>Verify Claude CLI</h2>
            <p>The agents use the Claude CLI (npm package <code>@anthropic-ai/claude-code</code>) authed against a Claude Pro/Max subscription. Verify it's installed.</p>
            <div class="code-block"><span class="code-comment"># Check Claude is available</span>
claude --version</div>
            <div class="info-callout">
                No API key needed. The CLI bills against your Claude Pro/Max subscription.
            </div>
        </div>
    </div>

    <div class="timeline-step">
        <div class="timeline-marker">
            <span class="timeline-num">4</span>
            <div class="timeline-line"></div>
        </div>
        <div class="timeline-content">
            <h2>Run the Pipeline</h2>
            <p>Collect RSS feeds, filter for relevance, categorize, summarize, and compile your weekly draft — all in one command.</p>
            <div class="code-block"><span class="code-comment"># Activate environment</span>
source .venv/bin/activate

<span class="code-comment"># Run full pipeline (collect → scout → editor → curator)</span>
python src/pipeline.py</div>
            <p class="step-detail">Or run individual stages:</p>
            <div class="code-block"><span class="code-comment"># Individual stages</span>
python src/collector.py          <span class="code-comment"># Fetch RSS feeds</span>
python src/scout.py              <span class="code-comment"># Filter for relevance</span>
python src/editor.py             <span class="code-comment"># Categorize & summarize</span>
python src/curator.py            <span class="code-comment"># Compile weekly draft</span>

<span class="code-comment"># Or start from a specific stage</span>
python src/pipeline.py --from scout</div>
        </div>
    </div>

    <div class="timeline-step">
        <div class="timeline-marker">
            <span class="timeline-num">5</span>
            <div class="timeline-line timeline-line--last"></div>
        </div>
        <div class="timeline-content">
            <h2>Review & Export</h2>
            <p>Launch the dashboard to review this week's draft. Approve, reject, or edit items, then export.</p>
            <div class="code-block"><span class="code-comment"># Start the dashboard</span>
uvicorn src.dashboard:app --reload

<span class="code-comment"># Open in your browser</span>
open http://localhost:8000</div>
            <div class="info-callout">
                <strong>Editorial workflow:</strong> Triage Signals, build the Lineup, draft the Spotlight when ready, and Publish the Edition.
            </div>
        </div>
    </div>
</div>

</details>
"""


def render_status(status: dict) -> str:
    rows = ""
    for key, s in status.items():
        if s.get("exists"):
            rows += f"""
            <div class="status-row complete">
                <div class="status-indicator"></div>
                <div class="status-label">{s['label']}</div>
                <div class="status-detail">{s['modified']} · {s['size']}</div>
            </div>"""
        else:
            rows += f"""
            <div class="status-row pending">
                <div class="status-indicator"></div>
                <div class="status-label">{s['label']}</div>
                <div class="status-detail">Not yet run</div>
            </div>"""

    return f"""
<div class="page-header">
    <h1>Pipeline Status</h1>
    <p class="subtitle">Run the pipeline to generate your weekly newsletter draft.</p>
</div>
<div class="status-container">
    {rows}
</div>
<div class="code-block" style="margin-top: 2rem;">
    <span class="code-comment"># Run the full pipeline</span>
    source .venv/bin/activate
    python src/pipeline.py
</div>"""


def _date_range_label(week_of_str: str) -> str:
    try:
        end = datetime.strptime(week_of_str, "%Y-%m-%d")
        start = end - timedelta(days=end.weekday())  # Monday
        if start.month == end.month:
            return f"{start.strftime('%b %d')} – {end.strftime('%d, %Y')}"
        return f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    except (ValueError, TypeError):
        return week_of_str


def _process_notes(notes: str, items: list) -> str:
    if not notes:
        return ""

    def replace_id(m):
        idx = int(m.group(1))
        if 0 <= idx < len(items):
            return f"\"{items[idx].get('title', f'item {idx}')}\""
        return m.group(0)

    notes = re.sub(r'\bID\s+(\d+)\b', replace_id, notes)
    # Split on ". " before a capital letter to get bullet points
    bullets = re.split(r'\.\s+(?=[A-Z])', notes)
    bullets = [b.strip().rstrip('.') for b in bullets if b.strip()]
    items_html = "".join(f"<li>{b}</li>" for b in bullets)
    return f"""<details class="editor-notes-accordion">
    <summary class="editor-notes-summary">Editor notes <span class="notes-arrow">›</span></summary>
    <ul class="editor-notes-list">{items_html}</ul>
</details>"""


def render_review(draft: dict, items: list) -> str:
    intro = draft.get("intro", "")
    notes = draft.get("notes", "")
    sections = draft.get("sections", {})
    highlights = draft.get("highlights", [])

    total = len(items)
    approved = sum(1 for i in items if i.get("status") == "approved")
    rejected = sum(1 for i in items if i.get("status") == "rejected")
    pending = total - approved - rejected

    approved_pct = round(approved / total * 100) if total else 0
    rejected_pct = round(rejected / total * 100) if total else 0
    pending_pct = 100 - approved_pct - rejected_pct

    date_range = _date_range_label(draft.get("week_of", ""))

    header = f"""
<div class="page-header">
    <div class="header-top">
        <div>
            <h1>{_newsletter_name()}</h1>
            <p class="subtitle">{date_range}</p>
        </div>
        <button onclick="exportNewsletter()" class="btn-primary">Export Newsletter</button>
    </div>
    <div class="spotlight-block">
        <span class="spotlight-label">Spotlight</span>
        <p class="intro-text">{intro}</p>
    </div>
    <div class="stats-bar">
        <div class="stat-block">
            <span class="stat-num stat-num--total" id="stat-total">{total}</span>
            <span class="stat-label">total</span>
        </div>
        <div class="stat-divider"></div>
        <div class="stat-block">
            <span class="stat-num stat-num--approved" id="stat-approved">{approved}</span>
            <span class="stat-label">approved</span>
        </div>
        <div class="stat-block">
            <span class="stat-num stat-num--rejected" id="stat-rejected">{rejected}</span>
            <span class="stat-label">rejected</span>
        </div>
        <div class="stat-block">
            <span class="stat-num stat-num--pending" id="stat-pending">{pending}</span>
            <span class="stat-label">pending</span>
        </div>
        <div class="stat-progress">
            <div class="progress-bar">
                <div class="progress-approved" style="width:{approved_pct}%"></div>
                <div class="progress-rejected" style="width:{rejected_pct}%"></div>
                <div class="progress-pending" style="width:{pending_pct}%"></div>
            </div>
        </div>
    </div>
</div>
<div id="export-banner" class="export-banner hidden"></div>"""

    notes_html = _process_notes(notes, items)
    if notes_html:
        header += notes_html

    # Highlights section (formerly Spotlight)
    highlights_html = ""
    for sid in highlights:
        if isinstance(sid, int) and sid < len(items):
            highlights_html += render_card(sid, items[sid], is_spotlight=True)

    sections_html = ""
    if highlights_html:
        sections_html += f"""
<section class="section">
    <h2 class="section-title">Highlights <span class="section-count">{len(highlights)}</span></h2>
    <div class="cards">{highlights_html}</div>
</section>"""

    for category, article_ids in sections.items():
        if not article_ids:
            continue
        cards = ""
        for aid in article_ids:
            if isinstance(aid, int) and aid < len(items):
                cards += render_card(aid, items[aid])
        display_name = category.title()
        sections_html += f"""
<section class="section">
    <h2 class="section-title">{display_name} <span class="section-count">{len(article_ids)}</span></h2>
    <div class="cards">{cards}</div>
</section>"""

    return header + sections_html


_CHECK_ICON = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M13.5 4.5L6 12L2.5 8.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
_X_ICON = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M12 4L4 12M4 4l8 8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'
_EDIT_ICON = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M11.5 2.5l2 2-8 8H3.5v-2l8-8z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
_RESTORE_ICON = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 8a5 5 0 1 0 1.5-3.5M3 3v3.5h3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'
_PAUSE_ICON = '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><rect x="4" y="3" width="3" height="10" rx="1"/><rect x="9" y="3" width="3" height="10" rx="1"/></svg>'
_PLAY_ICON = '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M5 3.5v9a.5.5 0 0 0 .77.42l7-4.5a.5.5 0 0 0 0-.84l-7-4.5A.5.5 0 0 0 5 3.5z"/></svg>'


def render_card(item: dict, view: str = "signals") -> str:
    item_id = item.get("id", "")
    status = item.get("status", "pending")
    included = item.get("included_in_next", True)
    sig = item.get("significance", "medium")
    date_str = item.get("published", "")[:10] if item.get("published") else ""

    edit_btn = (
        f'<button onclick="editItem(\'{item_id}\')" class="action-btn action-btn--edit" '
        f'title="Edit summary">{_EDIT_ICON}</button>'
    )

    state_classes = [f"card--{status}"]

    if view == "lineup":
        # Including is the default for everything in the Lineup, so we don't
        # decorate it with a label. Held items are visually muted (via the
        # card--held class), which is itself the state signal.
        if included:
            toggle_btn = (
                f'<button onclick="holdItem(\'{item_id}\')" class="action-btn action-btn--toggle-include" '
                f'title="Hold for later (skip next edition)">{_PAUSE_ICON}</button>'
            )
        else:
            state_classes.append("card--held")
            toggle_btn = (
                f'<button onclick="includeItem(\'{item_id}\')" class="action-btn action-btn--toggle-held" '
                f'title="Include in next edition">{_PLAY_ICON}</button>'
            )
        actions = (
            f'{edit_btn}'
            f'{toggle_btn}'
            f'<button onclick="removeFromSaved(\'{item_id}\')" class="action-btn action-btn--reject" '
            f'title="Move to Archive">{_X_ICON}</button>'
        )
    elif view == "archive":
        actions = (
            f'<button onclick="restoreItem(\'{item_id}\')" class="action-btn action-btn--restore" '
            f'title="Restore to Signals">{_RESTORE_ICON}</button>'
        )
    else:  # signals
        actions = (
            f'<button onclick="saveItem(\'{item_id}\')" class="action-btn action-btn--approve" '
            f'title="Save → Lineup">{_CHECK_ICON}</button>'
            f'<button onclick="hideItem(\'{item_id}\')" class="action-btn action-btn--reject" '
            f'title="Send to Archive">{_X_ICON}</button>'
            f'{edit_btn}'
        )

    return f"""
<article class="card {' '.join(state_classes)}" id="item-{item_id}" data-status="{status}" data-included="{str(bool(included)).lower()}">
    <div class="card-content">
        <div class="card-meta">
            <span class="card-source">{item.get('source', '')}</span>
            <span class="card-date">{date_str}</span>
        </div>
        <h3 class="card-title">
            <a href="{item.get('link', '#')}" target="_blank" rel="noopener">{item.get('title', 'Untitled')}</a>
        </h3>
        <p class="card-summary" id="summary-{item_id}">{item.get('summary', '')}</p>
        <div class="card-footer">
            <span class="badge badge--category">{item.get('category', '')}</span>
            <span class="badge badge--sig-{sig}">{sig}</span>
        </div>
    </div>
    <div class="card-actions">
        {actions}
    </div>
</article>"""


def render_sections(groups: "OrderedDict[str, list[dict]]", view: str) -> str:
    html = ""
    for category, items in groups.items():
        cards = "".join(render_card(it, view=view) for it in items)
        html += f"""
<section class="section">
    <h2 class="section-title">{category.title()} <span class="section-count">{len(items)}</span></h2>
    <div class="cards">{cards}</div>
</section>"""
    return html


def render_signals(groups: "OrderedDict[str, list[dict]]") -> str:
    header = """
<div class="page-header">
    <div class="header-top">
        <div>
            <h1>Signals</h1>
            <p class="subtitle">Fresh items surfaced by the agents. Save what's worth publishing, archive the rest.</p>
        </div>
        <div class="header-actions">
            <button onclick="toggleAddUrl()" class="btn-ghost" id="add-url-btn" title="Add a single article by pasting its URL">+ Add by URL</button>
            <button onclick="triggerRefresh()" class="btn-ghost" id="refresh-stories-btn" title="Pull fresh items from RSS feeds">✦ Refresh</button>
        </div>
    </div>
    <div id="add-url-row" class="add-url-row hidden">
        <input type="url" id="add-url-input" class="add-url-input" placeholder="Paste a URL — https://example.com/article">
        <button onclick="ingestUrl()" class="btn-primary" id="add-url-submit">Add</button>
        <button onclick="toggleAddUrl()" class="btn-secondary">Cancel</button>
    </div>
    <div id="add-url-feedback" class="add-url-feedback hidden"></div>
</div>
<div id="refresh-banner" class="export-banner hidden"></div>
<div id="export-banner" class="export-banner hidden"></div>"""

    if not groups:
        return header + """
<div class="empty-state">
    <div class="empty-icon">✦</div>
    <h2>No new signals</h2>
    <p>The daily agent will surface more on its next run.</p>
</div>"""

    return header + render_sections(groups, view="signals")


def render_lineup(groups: "OrderedDict[str, list[dict]]", bucket_state: dict) -> str:
    spotlight = bucket_state.get("spotlight", "")

    header = f"""
<div class="page-header">
    <div class="header-top">
        <div>
            <h1>Lineup</h1>
            <p class="subtitle">Items you've selected for the next edition. Hold any you'd rather keep for later.</p>
        </div>
        <button onclick="exportNewsletter()" class="btn-primary" id="export-btn">Publish Edition</button>
    </div>
    <div class="spotlight-block">
        <div class="spotlight-header">
            <span class="spotlight-label">Spotlight</span>
            <button onclick="draftSpotlight()" class="btn-ghost" id="draft-spotlight-btn" title="Draft a spotlight from the items currently Including">✦ Draft with agent</button>
        </div>
        <textarea id="spotlight-input" class="spotlight-textarea" placeholder="2–3 sentences summarizing this week's biggest stories…" onblur="saveSpotlight()">{spotlight}</textarea>
    </div>
</div>
<div id="export-banner" class="export-banner hidden"></div>"""

    if not groups:
        return header + """
<div class="empty-state">
    <div class="empty-icon">✦</div>
    <h2>Lineup is empty</h2>
    <p>Save items from <a href="/">Signals</a> to start building the next edition.</p>
</div>"""

    return header + render_sections(groups, view="lineup")


def render_archive(groups: "OrderedDict[str, list[dict]]") -> str:
    header = """
<div class="page-header">
    <h1>Archive</h1>
    <p class="subtitle">Items you've set aside. Restore anytime.</p>
</div>"""

    if not groups:
        return header + """
<div class="empty-state">
    <div class="empty-icon">✦</div>
    <h2>Archive is empty</h2>
    <p>Items you archive from Signals or remove from the Lineup show up here.</p>
</div>"""

    return header + render_sections(groups, view="archive")


# ─── Archive Rendering ─────────────────────────────────────────────────────────


def render_editions_list(editions: list) -> str:
    if not editions:
        empty = """
<div class="empty-state">
    <div class="empty-icon">📚</div>
    <h2>No past editions yet</h2>
    <p>Add past editions to train the AI pipeline on your editorial style and preferences.</p>
    <a href="/editions/new" class="btn-primary">Add First Edition</a>
</div>"""
        return f"""
<div class="page-header">
    <div class="header-top">
        <div>
            <h1>Editions</h1>
            <p class="subtitle">Past editions.</p>
        </div>
        <a href="/editions/new" class="btn-primary">Add Edition</a>
    </div>
</div>
{empty}"""

    cards = ""
    for ed in editions:
        cards += f"""
<a href="/archive/{ed['id']}" class="archive-card">
    <div class="archive-card-header">
        <span class="archive-edition">Edition {ed['edition_number']}</span>
        <span class="archive-date">{ed['date']}</span>
    </div>
    <h3 class="archive-title">{ed['title']}</h3>
    <div class="archive-meta">
        <span>{ed['item_count']} items</span>
        <span>{ed['section_count']} sections</span>
    </div>
</a>"""

    return f"""
<div class="page-header">
    <div class="header-top">
        <div>
            <h1>Editions</h1>
            <p class="subtitle">{len(editions)} past edition{'s' if len(editions) != 1 else ''}.</p>
        </div>
        <a href="/editions/new" class="btn-primary">Add Edition</a>
    </div>
</div>
<div class="archive-grid">
    {cards}
</div>"""


def render_editions_form(error: str = None) -> str:
    error_html = ""
    if error:
        error_html = f'<div class="error-callout">{error}</div>'

    return f"""
<div class="page-header">
    <h1>Add Past Edition</h1>
    <p class="subtitle">Paste a past edition. It will be parsed and stored as reference material for the AI agents.</p>
</div>

{error_html}

<form method="POST" action="/archive/new" class="archive-form">
    <div class="form-row">
        <div class="form-group form-group--small">
            <label for="edition_number">Edition #</label>
            <input type="text" id="edition_number" name="edition_number" placeholder="281" class="form-input" required>
        </div>
        <div class="form-group form-group--small">
            <label for="date">Date</label>
            <input type="date" id="date" name="date" class="form-input">
        </div>
    </div>

    <div class="form-group">
        <label for="content">Newsletter Content</label>
        <p class="form-hint">Paste the full newsletter — plain text, HTML, or markdown. Section headers (ALL CAPS lines, ## headers, or &lt;h2&gt; tags) and links (HTML or markdown) are automatically detected.</p>
        <textarea id="content" name="content" class="form-textarea" rows="24"
            placeholder="SPOTLIGHT

This week, Meta said it will award nearly $2 million in new grants...

HIGHLIGHTS

Meta said it will award nearly $2 million in new grants to support research...
https://example.com/article

Speaking at the World Economic Forum in Davos, Meta CTO Andrew Bosworth said...

VISION PRO IN THE NEWS

The latest firmware update brings..."></textarea>
    </div>

    <div class="form-actions">
        <a href="/editions" class="btn-secondary">Cancel</a>
        <button type="submit" class="btn-primary">Save Edition</button>
    </div>
</form>"""


def render_edition_view(edition: dict, editions: list) -> str:
    if not edition:
        return "<p>Edition not found.</p>"

    # Render the edition beautifully
    title = edition.get("title", "Edition")
    date = edition.get("date", "")
    edition_num = edition.get("edition_number", "")
    spotlight = edition.get("spotlight") or ""
    sections = edition.get("sections", {})
    items = edition.get("items", [])

    spotlight_html = ""
    if spotlight.strip():
        spotlight_html = f"""
<section class="archive-section archive-section--spotlight">
    <h2 class="section-title">SPOTLIGHT</h2>
    <p class="archive-spotlight">{spotlight}</p>
</section>"""

    # Build sections HTML
    sections_html = ""
    for section_name, section_items in sections.items():
        items_html = ""
        for item in section_items:
            source = f' <span class="item-source">({item["source"]})</span>' if item.get("source") else ""
            link = (item.get("link") or "").strip()
            title_text = item.get("title") or ""
            if title_text:
                # Title itself is the link — drop the tiny ↗.
                title_html = (
                    f'<a href="{link}" target="_blank" rel="noopener">{title_text}</a>'
                    if link else title_text
                )
                items_html += f"""
<div class="archive-item">
    <h4 class="item-title">{title_html}</h4>
    <p class="item-summary">{item.get('summary', '')}{source}</p>
</div>"""
            else:
                items_html += f"""
<div class="archive-item">
    <p class="item-summary">{item.get('text', item.get('summary', ''))}{source}</p>
</div>"""

        sections_html += f"""
<section class="archive-section">
    <h2 class="section-title">{section_name} <span class="section-count">{len(section_items)}</span></h2>
    {items_html}
</section>"""

    # Stats
    stats_html = f"""
<div class="stats-bar">
    <span class="stat"><span class="stat-num">{len(items)}</span> items</span>
    <span class="stat"><span class="stat-num">{len(sections)}</span> sections</span>
</div>"""

    return f"""
<div class="page-header">
    <div class="header-top">
        <div>
            <div class="archive-view-meta">
                <span class="archive-edition-badge">Edition {edition_num}</span>
                <span class="archive-view-date">{date}</span>
            </div>
            <h1>{title}</h1>
        </div>
        <div class="header-actions">
            <a href="/archive" class="btn-secondary">All Editions</a>
        </div>
    </div>
    {stats_html}
</div>
{spotlight_html}
{sections_html}"""


# ─── Styles ───────────────────────────────────────────────────────────────────

STYLES = """<style>
:root {
    --font-sans: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'SF Pro Text', system-ui, sans-serif;
    --font-mono: 'SF Mono', SFMono-Regular, ui-monospace, Menlo, monospace;

    --color-bg: #f5f5f7;
    --color-surface: #ffffff;
    --color-text-primary: #1d1d1f;
    --color-text-secondary: #6e6e73;
    --color-text-tertiary: #86868b;
    --color-border: #d2d2d7;
    --color-border-light: #e8e8ed;

    --color-blue: #007aff;
    --color-green: #34c759;
    --color-red: #ff3b30;
    --color-orange: #ff9500;
    --color-purple: #af52de;
    --color-teal: #5ac8fa;

    --color-green-bg: #e8f8ed;
    --color-red-bg: #fef0ef;
    --color-blue-bg: #edf4ff;
    --color-orange-bg: #fff6e8;

    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-lg: 16px;
    --radius-xl: 20px;

    --shadow-sm: 0 1px 2px rgba(0,0,0,0.04);
    --shadow-md: 0 2px 8px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04);
    --shadow-lg: 0 4px 16px rgba(0,0,0,0.08), 0 2px 4px rgba(0,0,0,0.04);

    --transition: 200ms cubic-bezier(0.25, 0.1, 0.25, 1);
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: var(--font-sans);
    background: var(--color-bg);
    color: var(--color-text-primary);
    line-height: 1.47059;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}

.app {
    min-height: 100vh;
}

/* ─── Navigation ─── */

.nav {
    position: sticky;
    top: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 24px;
    background: rgba(255, 255, 255, 0.72);
    backdrop-filter: saturate(180%) blur(20px);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    border-bottom: 0.5px solid var(--color-border-light);
}

.nav-brand {
    display: flex;
    align-items: center;
    gap: 8px;
    text-decoration: none;
    color: inherit;
}

.nav-icon {
    font-size: 1.25rem;
    color: var(--color-blue);
}

.nav-title {
    font-size: 1.0625rem;
    font-weight: 600;
    letter-spacing: -0.022em;
}

.nav-tabs {
    display: flex;
    align-items: center;
    gap: 28px;
}

.nav-tab {
    display: inline-flex;
    align-items: baseline;
    gap: 6px;
    padding: 6px 0;
    font-size: 0.9375rem;
    font-weight: 500;
    color: var(--color-text-secondary);
    text-decoration: none;
    letter-spacing: -0.01em;
    border-bottom: 1.5px solid transparent;
    transition: color var(--transition), border-color var(--transition);
}

.nav-tab:hover {
    color: var(--color-text-primary);
}

.nav-tab.active {
    color: var(--color-text-primary);
    border-bottom-color: var(--color-blue);
}

.nav-tab-count {
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--color-text-tertiary, #999);
    font-variant-numeric: tabular-nums;
}

.nav-tab.active .nav-tab-count {
    color: var(--color-blue);
}

.nav-tab-divider {
    width: 1px;
    height: 18px;
    background: var(--color-border-light);
    margin: 0 4px;
}

/* ─── Main ─── */

.main {
    max-width: 960px;
    margin: 0 auto;
    padding: 40px 24px 80px;
}

/* ─── Page Header ─── */

.page-header {
    margin-bottom: 32px;
}

.header-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 12px;
}

h1 {
    font-size: 2rem;
    font-weight: 700;
    letter-spacing: -0.032em;
    line-height: 1.1;
}

.subtitle {
    font-size: 1.0625rem;
    color: var(--color-text-secondary);
    margin-top: 4px;
}


/* ─── Stats Bar ─── */

/* ─── Spotlight block ─── */

.spotlight-block {
    margin: 16px 0;
    padding: 16px 20px;
    background: var(--color-surface);
    border-radius: var(--radius-md);
    border: 0.5px solid var(--color-border-light);
}

.spotlight-label {
    display: block;
    font-size: 0.6875rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--color-blue);
    margin-bottom: 6px;
}

.intro-text {
    margin: 0;
    font-size: 0.9375rem;
    line-height: 1.6;
    color: var(--color-text-primary);
}

.spotlight-textarea {
    display: block;
    width: 100%;
    min-height: 84px;
    margin: 0;
    padding: 8px 10px;
    font: inherit;
    font-size: 0.9375rem;
    line-height: 1.6;
    color: var(--color-text-primary);
    background: var(--color-bg);
    border: 0.5px solid var(--color-border-light);
    border-radius: var(--radius-sm);
    resize: vertical;
    box-sizing: border-box;
}

.spotlight-textarea:focus {
    outline: none;
    border-color: var(--color-blue);
}

.spotlight-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
    gap: 12px;
}

.btn-ghost {
    appearance: none;
    background: transparent;
    color: var(--color-blue);
    border: 0.5px solid var(--color-border-light);
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s ease, border-color 0.15s ease;
    letter-spacing: 0.01em;
}

.btn-ghost:hover {
    background: rgba(0, 122, 255, 0.06);
    border-color: var(--color-blue);
}

.btn-ghost:disabled {
    opacity: 0.6;
    cursor: progress;
}

/* ─── Stats bar ─── */

.stats-bar {
    display: flex;
    align-items: center;
    gap: 20px;
    margin-top: 16px;
    padding: 14px 20px;
    background: var(--color-surface);
    border-radius: var(--radius-md);
    border: 0.5px solid var(--color-border-light);
}

.stat-block {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    min-width: 48px;
}

.stat-divider {
    width: 1px;
    height: 32px;
    background: var(--color-border-light);
}

.stat-num {
    font-size: 1.5rem;
    font-weight: 700;
    letter-spacing: -0.03em;
    line-height: 1;
    color: var(--color-text-primary);
}

.stat-num--total  { color: var(--color-text-secondary); font-size: 1.25rem; }
.stat-num--approved { color: var(--color-green); }
.stat-num--rejected { color: var(--color-red); }
.stat-num--pending  { color: var(--color-orange); }

.stat-label {
    font-size: 0.6875rem;
    font-weight: 500;
    color: var(--color-text-tertiary);
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

.stat-progress {
    flex: 1;
}

.progress-bar {
    display: flex;
    height: 6px;
    border-radius: 99px;
    overflow: hidden;
    background: var(--color-bg);
    gap: 1px;
}

.progress-approved { background: var(--color-green); border-radius: 99px 0 0 99px; transition: width 0.4s ease; }
.progress-rejected  { background: var(--color-red); transition: width 0.4s ease; }
.progress-pending   { background: var(--color-orange); flex: 1; border-radius: 0 99px 99px 0; transition: width 0.4s ease; }

/* ─── Buttons ─── */

.btn-primary {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 10px 20px;
    background: var(--color-blue);
    color: white;
    border: none;
    border-radius: var(--radius-xl);
    font-size: 0.9375rem;
    font-weight: 500;
    cursor: pointer;
    transition: all var(--transition);
}

.btn-primary:hover {
    background: #0066d6;
    transform: scale(1.02);
}

.btn-primary:active {
    transform: scale(0.98);
}

/* ─── Export Banner ─── */

.export-banner {
    margin-top: 12px;
    padding: 12px 16px;
    background: var(--color-green-bg);
    border-radius: var(--radius-md);
    font-size: 0.875rem;
    color: #065f46;
    transition: all var(--transition);
}

.export-banner.hidden { display: none; }

/* ─── Editor Notes Accordion ─── */

.editor-notes-accordion {
    margin-top: 12px;
    border-radius: var(--radius-md);
    border: 0.5px solid #fcd34d;
    background: var(--color-orange-bg);
    overflow: hidden;
}

.editor-notes-summary {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 16px;
    font-size: 0.8125rem;
    font-weight: 600;
    color: #92400e;
    cursor: pointer;
    list-style: none;
    user-select: none;
}

.editor-notes-summary::-webkit-details-marker { display: none; }

.notes-arrow {
    font-size: 1rem;
    transition: transform 0.2s ease;
    display: inline-block;
}

.editor-notes-accordion[open] .notes-arrow {
    transform: rotate(90deg);
}

.editor-notes-list {
    margin: 0;
    padding: 4px 16px 12px 32px;
    font-size: 0.8125rem;
    color: #92400e;
    line-height: 1.6;
    display: flex;
    flex-direction: column;
    gap: 6px;
}

/* ─── Sections ─── */

.section {
    margin-top: 32px;
}

.section-title {
    font-size: 1.25rem;
    font-weight: 600;
    letter-spacing: -0.022em;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
}

.section-count {
    font-size: 0.75rem;
    font-weight: 500;
    background: var(--color-border-light);
    color: var(--color-text-secondary);
    padding: 2px 8px;
    border-radius: 10px;
}

.cards {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

/* ─── Cards ─── */

.card {
    position: relative;
    display: flex;
    background: #ffffff;
    border-radius: var(--radius-md);
    border: 1px solid #e8e8ed;
    padding: 16px 20px;
    transition: background 0.2s ease, border-color 0.2s ease, opacity 0.2s ease;
}

.card:hover {
    border-color: #c7c7cc;
}

.card--spotlight {
    border-left: 3px solid var(--color-orange);
}

.card--saved {
    background: #f0faf3;
    border-color: rgba(52, 199, 89, 0.35);
    border-left: 3px solid #34c759;
}

.card--saved.card--held {
    background: #fafafa;
    border-left-color: rgba(52, 199, 89, 0.35);
    opacity: 0.72;
}

.card--hidden {
    background: #fafafa;
    border-color: rgba(0, 0, 0, 0.06);
    opacity: 0.72;
}

.card--published {
    background: var(--color-blue-bg);
    border-left: 3px solid var(--color-blue);
}

.card--edited {
    background: var(--color-blue-bg);
    border-color: rgba(0, 122, 255, 0.2);
}

.card-content {
    flex: 1;
    min-width: 0;
}

.card-meta {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
}

.card-source {
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--color-blue);
}

.card-date {
    font-size: 0.75rem;
    color: var(--color-text-tertiary);
}

.card-title {
    font-size: 0.9375rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    line-height: 1.3;
    margin-bottom: 6px;
}

.card-title a {
    color: var(--color-text-primary);
    text-decoration: none;
}

.card-title a:hover {
    color: var(--color-blue);
}

.card-summary {
    font-size: 0.8125rem;
    color: var(--color-text-secondary);
    line-height: 1.5;
    margin-bottom: 8px;
}

.card-footer {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}

.badge {
    font-size: 0.6875rem;
    font-weight: 500;
    padding: 2px 8px;
    border-radius: 6px;
}

.badge--category {
    background: var(--color-border-light);
    color: var(--color-text-secondary);
}

.badge--sig-high { background: #fee2e2; color: #b91c1c; }
.badge--sig-medium { background: #fef3c7; color: #92400e; }
.badge--sig-low { background: var(--color-border-light); color: var(--color-text-tertiary); }

/* ─── Card Actions ─── */

.card-actions {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 6px;
    margin-left: 12px;
    align-self: stretch;
    opacity: 0;
    transition: opacity var(--transition);
}

.card:hover .card-actions {
    opacity: 1;
}

.action-btn {
    width: 32px;
    height: 32px;
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all var(--transition);
}

.action-btn--approve { background: #dcfce7; color: #16a34a; }
.action-btn--approve:hover { background: #bbf7d0; transform: scale(1.1); }
.action-btn--reject { background: #fee2e2; color: #dc2626; }
.action-btn--reject:hover { background: #fecaca; transform: scale(1.1); }
.action-btn--edit { background: #dbeafe; color: #2563eb; }
.action-btn--edit:hover { background: #bfdbfe; transform: scale(1.1); }
.action-btn--include { background: #dcfce7; color: #16a34a; }
.action-btn--include:hover { background: #bbf7d0; transform: scale(1.1); }
.action-btn--held {
    background: transparent;
    color: var(--color-text-tertiary, #999);
    border: 1px dashed rgba(0, 0, 0, 0.18);
    font-size: 0.95rem;
    line-height: 1;
}
.action-btn--held:hover { background: rgba(0, 0, 0, 0.04); color: var(--color-text-primary); }
.action-btn--restore { background: #dbeafe; color: #2563eb; }
.action-btn--restore:hover { background: #bfdbfe; transform: scale(1.1); }
/* Pause = "hold" action on an Including card. The card is green (saying
   "in the next edition"); the button must read as the *toggle*, not as a
   match for the card. Neutral white + gray icon for calm intent. */
.action-btn--toggle-include {
    background: rgba(255, 255, 255, 0.92);
    color: var(--color-text-secondary);
    border: 0.5px solid rgba(0, 0, 0, 0.12);
}
.action-btn--toggle-include:hover {
    background: #ffffff;
    color: var(--color-text-primary);
    transform: scale(1.1);
}
/* Play = "include" action on a Held (muted) card. Bringing the item back
   to active is a positive move, so the brand green carries here. */
.action-btn--toggle-held {
    background: #dcfce7;
    color: #15803d;
    border: 0.5px solid rgba(52, 199, 89, 0.4);
}
.action-btn--toggle-held:hover { background: #bbf7d0; transform: scale(1.1); }

.card-status-badge {
    position: absolute;
    top: 8px;
    right: 8px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--color-text-tertiary);
    opacity: 0.7;
}

.card-state-label {
    position: absolute;
    bottom: 12px;
    right: 14px;
    font-size: 0.6875rem;
    font-weight: 500;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--color-text-tertiary, #999);
    pointer-events: none;
}

.card--held .card-state-label {
    color: var(--color-text-tertiary);
    font-style: italic;
}


/* ─── Settings page panels ─── */

.settings-panel {
    background: var(--color-surface);
    border: 0.5px solid var(--color-border-light);
    border-radius: var(--radius-md);
    padding: 20px 24px;
    margin-bottom: 20px;
}

.settings-panel-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 8px;
}

.settings-panel-header h2 {
    margin: 0 0 4px;
    font-size: 1.0625rem;
    font-weight: 600;
    letter-spacing: -0.022em;
}

.settings-panel-sub {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--color-text-secondary);
}

.pipeline-progress {
    display: none;
    align-items: center;
    gap: 8px;
    margin-top: 12px;
    padding: 8px 12px;
    background: var(--color-blue-bg);
    border-radius: var(--radius-sm);
    font-size: 0.8125rem;
    color: var(--color-blue);
}

.pipeline-progress.visible {
    display: inline-flex;
}

.pipeline-progress-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--color-blue);
    animation: pulse 1.4s ease-in-out infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 0.3; }
    50%      { opacity: 1.0; }
}

.feeds-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 12px 0;
}

.feed-row {
    display: grid;
    grid-template-columns: 1.2fr 2fr 1.6fr auto;
    gap: 8px;
    align-items: center;
    padding: 6px;
    border-radius: var(--radius-sm);
    border: 0.5px solid transparent;
}

.feed-row:hover {
    border-color: var(--color-border-light);
    background: var(--color-bg);
}

.feed-input {
    appearance: none;
    background: transparent;
    border: 0.5px solid transparent;
    border-radius: 6px;
    padding: 6px 8px;
    font: inherit;
    font-size: 0.8125rem;
    color: var(--color-text-primary);
    min-width: 0;
}

.feed-input:focus {
    outline: none;
    background: var(--color-bg);
    border-color: var(--color-blue);
}

.feed-input::placeholder {
    color: var(--color-text-tertiary, #999);
}

.feed-remove {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--color-text-tertiary, #999);
    cursor: pointer;
    padding: 4px 8px;
    border-radius: 999px;
    font-size: 0.875rem;
    transition: color var(--transition), background var(--transition);
}

.feed-remove:hover {
    color: #dc2626;
    background: #fee2e2;
}

/* ─── Automation panel ─── */

.automation-note {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 12px 0 0;
    padding: 10px 12px;
    background: var(--color-bg);
    border-radius: var(--radius-sm);
    font-size: 0.8125rem;
    color: var(--color-text-secondary);
}

.automation-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
}

.automation-dot--on { background: #34c759; }
.automation-dot--off { background: var(--color-text-tertiary, #999); }

/* ─── Signals header — add-url row ─── */

.header-actions {
    display: flex;
    gap: 8px;
}

.add-url-row {
    display: flex;
    gap: 8px;
    margin-top: 16px;
    align-items: center;
}

.add-url-row.hidden {
    display: none;
}

.add-url-input {
    flex: 1;
    padding: 8px 12px;
    font: inherit;
    font-size: 0.9375rem;
    color: var(--color-text-primary);
    background: var(--color-surface);
    border: 0.5px solid var(--color-border);
    border-radius: var(--radius-sm);
}

.add-url-input:focus {
    outline: none;
    border-color: var(--color-blue);
    box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.12);
}

.add-url-feedback {
    margin-top: 12px;
    padding: 10px 14px;
    border-radius: var(--radius-sm);
    font-size: 0.8125rem;
    background: var(--color-blue-bg);
    color: var(--color-blue);
    border: 0.5px solid rgba(0, 122, 255, 0.18);
}

.add-url-feedback.hidden {
    display: none;
}

.add-url-feedback.error {
    background: #fee2e2;
    color: #dc2626;
    border-color: rgba(220, 38, 38, 0.22);
}

.pipeline-table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 12px;
    font-size: 0.875rem;
}

.pipeline-table th {
    text-align: left;
    padding: 8px 12px;
    border-bottom: 0.5px solid var(--color-border-light);
    font-weight: 500;
    color: var(--color-text-secondary);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}

.pipeline-table td {
    padding: 12px;
    border-bottom: 0.5px solid var(--color-border-light);
    vertical-align: top;
}

.pipeline-table tr:last-child td {
    border-bottom: none;
}

.pipeline-table-stage {
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 600;
    white-space: nowrap;
}

.pipeline-table-icon {
    font-size: 1.1rem;
    width: 22px;
    display: inline-block;
    text-align: center;
}

.pipeline-table-tune {
    color: var(--color-text-secondary);
}

.model-provider {
    display: inline-block;
    background: var(--color-blue-bg);
    color: var(--color-blue);
    border: 0.5px solid rgba(0, 122, 255, 0.2);
    border-radius: 6px;
    padding: 1px 8px;
    font-size: 0.75rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    margin-right: 6px;
    text-transform: lowercase;
}

.model-name {
    font-family: var(--font-mono);
    font-size: 0.8125rem;
    color: var(--color-text-primary);
}

.model-endpoint {
    display: block;
    font-family: var(--font-mono);
    font-size: 0.6875rem;
    color: var(--color-text-tertiary, #999);
    margin-top: 2px;
}

.settings-details {
    margin-top: 32px;
    padding-top: 24px;
    border-top: 0.5px solid var(--color-border-light);
}

.settings-details summary {
    cursor: pointer;
    font-size: 0.9375rem;
    font-weight: 500;
    color: var(--color-text-secondary);
    padding: 6px 0;
}

.settings-details summary:hover {
    color: var(--color-text-primary);
}

.settings-details[open] summary {
    margin-bottom: 12px;
}

.settings-details-hint {
    margin: 0 0 16px;
    font-size: 0.8125rem;
    color: var(--color-text-secondary);
}

/* ─── Archive spotlight section ─── */

.archive-section--spotlight {
    background: var(--color-blue-bg);
    border-left: 3px solid var(--color-blue);
    padding: 16px 20px;
    border-radius: var(--radius-sm);
}

.archive-spotlight {
    margin: 8px 0 0;
    font-size: 1rem;
    line-height: 1.6;
    color: var(--color-text-primary);
}

/* ─── Hide-with-reason toast ─── */

.toast {
    position: fixed;
    left: 50%;
    bottom: 32px;
    transform: translateX(-50%) translateY(60px);
    background: rgba(28, 28, 30, 0.96);
    color: #fff;
    border-radius: 12px;
    padding: 10px 14px 10px 16px;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.8125rem;
    box-shadow: 0 12px 30px rgba(0, 0, 0, 0.18);
    backdrop-filter: saturate(180%) blur(20px);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    opacity: 0;
    pointer-events: none;
    transition: transform var(--transition), opacity var(--transition);
    z-index: 200;
}

.toast.visible {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
    pointer-events: auto;
}

.toast-label {
    color: rgba(255, 255, 255, 0.72);
    margin-right: 4px;
}

.toast-btn {
    appearance: none;
    background: rgba(255, 255, 255, 0.10);
    color: #fff;
    border: 0.5px solid rgba(255, 255, 255, 0.18);
    border-radius: 999px;
    padding: 4px 10px;
    font: inherit;
    font-size: 0.75rem;
    cursor: pointer;
    transition: background var(--transition);
}

.toast-btn:hover {
    background: rgba(255, 255, 255, 0.18);
}

.toast-btn--undo {
    background: transparent;
    border-color: transparent;
    color: var(--color-blue);
    margin-left: 4px;
}

.toast-btn--undo:hover {
    color: #4ea2ff;
    background: rgba(255, 255, 255, 0.05);
}

/* ─── Edit modal ─── */

.modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(28, 28, 30, 0.32);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 250;
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--transition);
}

.modal-backdrop.visible {
    opacity: 1;
    pointer-events: auto;
}

.modal-card {
    width: min(640px, 92vw);
    max-height: 86vh;
    background: var(--color-surface);
    border-radius: var(--radius-lg);
    box-shadow: 0 24px 60px rgba(0, 0, 0, 0.22), 0 4px 12px rgba(0, 0, 0, 0.08);
    padding: 24px 24px 20px;
    display: flex;
    flex-direction: column;
    transform: translateY(16px) scale(0.98);
    transition: transform var(--transition), opacity var(--transition);
    opacity: 0;
}

.modal-backdrop.visible .modal-card {
    transform: translateY(0) scale(1);
    opacity: 1;
}

.modal-header h2 {
    margin: 0 0 4px;
    font-size: 1.0625rem;
    font-weight: 600;
    letter-spacing: -0.022em;
}

.modal-hint {
    margin: 0 0 14px;
    font-size: 0.8125rem;
    color: var(--color-text-secondary);
}

.modal-textarea {
    appearance: none;
    width: 100%;
    min-height: 200px;
    padding: 12px 14px;
    font: inherit;
    font-size: 0.9375rem;
    line-height: 1.6;
    color: var(--color-text-primary);
    background: var(--color-bg);
    border: 0.5px solid var(--color-border-light);
    border-radius: var(--radius-sm);
    resize: vertical;
    box-sizing: border-box;
}

.modal-textarea:focus {
    outline: none;
    border-color: var(--color-blue);
    box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.12);
}

.modal-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 14px;
}

.modal-keyboard-hint {
    flex: 1;
    font-size: 0.75rem;
    color: var(--color-text-tertiary, #999);
    font-variant-numeric: tabular-nums;
}

/* ─── Status Page ─── */

.status-container {
    display: flex;
    flex-direction: column;
    gap: 2px;
    background: var(--color-surface);
    border-radius: var(--radius-lg);
    border: 0.5px solid var(--color-border-light);
    padding: 8px;
    box-shadow: var(--shadow-sm);
}

.status-row {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 14px 16px;
    border-radius: var(--radius-sm);
}

.status-row.complete { background: var(--color-green-bg); }
.status-row.pending { background: var(--color-bg); }

.status-indicator {
    width: 8px;
    height: 8px;
    border-radius: 50%;
}

.status-row.complete .status-indicator { background: var(--color-green); }
.status-row.pending .status-indicator { background: var(--color-border); }

.status-label {
    font-weight: 500;
    font-size: 0.9375rem;
    flex: 1;
}

.status-detail {
    font-size: 0.8125rem;
    color: var(--color-text-secondary);
}

/* ─── Setup Page — Timeline ─── */

.timeline {
    position: relative;
    margin-top: 32px;
}

.timeline-step {
    display: flex;
    gap: 24px;
    padding-bottom: 40px;
}

.timeline-step:last-child {
    padding-bottom: 0;
}

.timeline-marker {
    display: flex;
    flex-direction: column;
    align-items: center;
    flex-shrink: 0;
}

.timeline-num {
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--color-blue);
    color: white;
    font-size: 0.875rem;
    font-weight: 700;
    border-radius: 50%;
    flex-shrink: 0;
}

.timeline-line {
    width: 2px;
    flex: 1;
    background: var(--color-border-light);
    margin-top: 8px;
}

.timeline-line--last {
    background: transparent;
}

.timeline-content {
    flex: 1;
    min-width: 0;
    padding-top: 4px;
}

.timeline-content h2 {
    font-size: 1.1875rem;
    font-weight: 600;
    letter-spacing: -0.016em;
    margin-bottom: 8px;
}

.timeline-content p {
    font-size: 0.9375rem;
    color: var(--color-text-secondary);
    line-height: 1.5;
    margin-bottom: 14px;
}

.timeline-content code {
    font-family: var(--font-mono);
    font-size: 0.8125rem;
    background: var(--color-border-light);
    padding: 1px 6px;
    border-radius: 4px;
}

.step-detail {
    font-size: 0.8125rem !important;
    color: var(--color-text-tertiary) !important;
    margin-top: 14px !important;
    margin-bottom: 8px !important;
}

.code-block {
    font-family: var(--font-mono);
    font-size: 0.8125rem;
    line-height: 1.8;
    background: #1d1d1f;
    color: #f5f5f7;
    padding: 18px 22px;
    border-radius: var(--radius-md);
    white-space: pre;
    overflow-x: auto;
    margin-bottom: 4px;
}

.code-comment {
    color: #6e7681;
}

.info-callout {
    margin-top: 14px;
    padding: 12px 16px;
    background: var(--color-blue-bg);
    border-radius: var(--radius-sm);
    font-size: 0.8125rem;
    color: #1e40af;
    line-height: 1.5;
}

/* ─── Pipeline Diagram ─── */

.pipeline-section {
    margin-top: 48px;
    padding-top: 32px;
    border-top: 0.5px solid var(--color-border-light);
}

.pipeline-section h2 {
    font-size: 1.1875rem;
    font-weight: 600;
    letter-spacing: -0.016em;
    margin-bottom: 4px;
}

.pipeline-diagram {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 24px 0;
    flex-wrap: wrap;
}

.pipeline-stage {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    padding: 12px 16px;
    background: var(--color-bg);
    border-radius: var(--radius-md);
    min-width: 100px;
}

.stage-icon { font-size: 1.5rem; }
.stage-name { font-size: 0.8125rem; font-weight: 600; }
.stage-desc { font-size: 0.6875rem; color: var(--color-text-tertiary); }

.pipeline-arrow {
    font-size: 1.25rem;
    color: var(--color-text-tertiary);
    font-weight: 300;
}

/* ─── Responsive ─── */

@media (max-width: 640px) {
    .main { padding: 24px 16px 60px; }
    .nav { padding: 12px 16px; }
    .header-top { flex-direction: column; gap: 12px; }
    .stats-bar { flex-wrap: wrap; gap: 12px; }
    .timeline-step { gap: 16px; }
    .pipeline-diagram { flex-direction: column; }
    .pipeline-arrow { transform: rotate(90deg); }
    .form-row { flex-direction: column; }
    .archive-grid { grid-template-columns: 1fr; }
}

/* ─── Archive Styles ─── */

.archive-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
    margin-top: 8px;
}

.archive-card {
    display: block;
    background: var(--color-surface);
    border-radius: var(--radius-md);
    border: 0.5px solid var(--color-border-light);
    padding: 20px;
    text-decoration: none;
    color: inherit;
    transition: all var(--transition);
    box-shadow: var(--shadow-sm);
}

.archive-card:hover {
    box-shadow: var(--shadow-md);
    border-color: var(--color-border);
    transform: translateY(-1px);
}

.archive-card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.archive-edition {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--color-blue);
    background: var(--color-blue-bg);
    padding: 2px 8px;
    border-radius: 6px;
}

.archive-date {
    font-size: 0.75rem;
    color: var(--color-text-tertiary);
}

.archive-title {
    font-size: 1rem;
    font-weight: 600;
    letter-spacing: -0.01em;
    margin-bottom: 8px;
}

.archive-meta {
    display: flex;
    gap: 12px;
    font-size: 0.75rem;
    color: var(--color-text-tertiary);
}

/* Archive View */

.archive-view-meta {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 6px;
}

.archive-edition-badge {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--color-blue);
    background: var(--color-blue-bg);
    padding: 3px 10px;
    border-radius: 8px;
}

.archive-view-date {
    font-size: 0.8125rem;
    color: var(--color-text-tertiary);
}

.archive-section {
    margin-top: 28px;
}

.archive-item {
    padding: 12px 16px;
    background: var(--color-surface);
    border-radius: var(--radius-sm);
    border: 0.5px solid var(--color-border-light);
    margin-bottom: 6px;
}

.item-title {
    font-size: 0.875rem;
    font-weight: 600;
    margin-bottom: 4px;
    line-height: 1.3;
}

.item-summary {
    font-size: 0.8125rem;
    color: var(--color-text-secondary);
    line-height: 1.5;
}

.item-source {
    color: var(--color-text-tertiary);
    font-size: 0.75rem;
}

.item-link {
    color: var(--color-blue);
    text-decoration: none;
    font-size: 0.75rem;
}

/* Archive Form */

.archive-form {
    margin-top: 24px;
}

.form-row {
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
}

.form-group {
    flex: 1;
}

.form-group--small {
    flex: 0 0 120px;
}

.form-group label {
    display: block;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--color-text-primary);
    margin-bottom: 6px;
}

.form-hint {
    font-size: 0.75rem;
    color: var(--color-text-tertiary);
    margin-bottom: 8px;
    line-height: 1.4;
}

.form-input {
    width: 100%;
    padding: 10px 12px;
    font-family: var(--font-sans);
    font-size: 0.875rem;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-sm);
    outline: none;
    transition: border-color var(--transition);
}

.form-input:focus {
    border-color: var(--color-blue);
    box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.1);
}

.form-textarea {
    width: 100%;
    padding: 14px 16px;
    font-family: var(--font-mono);
    font-size: 0.8125rem;
    line-height: 1.6;
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: var(--radius-md);
    outline: none;
    resize: vertical;
    transition: border-color var(--transition);
}

.form-textarea:focus {
    border-color: var(--color-blue);
    box-shadow: 0 0 0 3px rgba(0, 122, 255, 0.1);
}

.form-actions {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    margin-top: 20px;
}

.btn-secondary {
    display: inline-flex;
    align-items: center;
    padding: 10px 20px;
    background: var(--color-bg);
    color: var(--color-text-primary);
    border: 0.5px solid var(--color-border);
    border-radius: var(--radius-xl);
    font-size: 0.9375rem;
    font-weight: 500;
    text-decoration: none;
    cursor: pointer;
    transition: all var(--transition);
}

.btn-secondary:hover {
    background: var(--color-border-light);
}

.error-callout {
    padding: 12px 16px;
    background: var(--color-red-bg);
    border-radius: var(--radius-md);
    font-size: 0.875rem;
    color: #991b1b;
    margin-bottom: 16px;
}

.header-actions {
    display: flex;
    gap: 8px;
}

/* Empty State */

.empty-state {
    text-align: center;
    padding: 60px 20px;
    background: var(--color-surface);
    border-radius: var(--radius-lg);
    border: 0.5px solid var(--color-border-light);
    margin-top: 24px;
}

.empty-icon {
    font-size: 3rem;
    margin-bottom: 16px;
}

.empty-state h2 {
    font-size: 1.25rem;
    font-weight: 600;
    margin-bottom: 8px;
}

.empty-state p {
    font-size: 0.9375rem;
    color: var(--color-text-secondary);
    margin-bottom: 20px;
    max-width: 400px;
    margin-left: auto;
    margin-right: auto;
}
</style>"""


# ─── Scripts ──────────────────────────────────────────────────────────────────

SCRIPTS = """<script>
async function saveItem(id) {
    await fetch(`/api/items/${id}/save`, {method: 'POST'});
    removeCard(id);
    bumpNavCount('lineup', +1);
    bumpNavCount('signals', -1);
}

async function hideItem(id) {
    await fetch(`/api/items/${id}/hide`, {method: 'POST'});
    removeCard(id);
    bumpNavCount('signals', -1);
    showHideToast(id);
}

// ─── Toast for tag-on-hide (no-friction reason capture) ──────────────────────

let _toastEl = null;
let _toastTimer = null;

function ensureToast() {
    if (_toastEl) return _toastEl;
    const t = document.createElement('div');
    t.className = 'toast';
    t.id = 'hide-toast';
    document.body.appendChild(t);
    _toastEl = t;
    return t;
}

function showHideToast(id) {
    const t = ensureToast();
    const reasons = (window.SCOUT_CONFIG && window.SCOUT_CONFIG.hideReasons) || [
        {id: 'not_relevant', label: 'Not relevant'},
        {id: 'weak', label: 'Weak'},
        {id: 'already_covered', label: 'Already covered'},
    ];
    const reasonButtons = reasons.map(r =>
        `<button class="toast-btn" onclick="tagHideReason('${id}', '${r.id}')">${r.label}</button>`
    ).join('');
    t.innerHTML = `
        <span class="toast-label">Sent to Archive · Tag reason?</span>
        ${reasonButtons}
        <button class="toast-btn toast-btn--undo" onclick="undoHide('${id}')">Undo</button>
    `;
    t.classList.add('visible');
    if (_toastTimer) clearTimeout(_toastTimer);
    _toastTimer = setTimeout(dismissToast, 6000);
}

function dismissToast() {
    if (_toastEl) _toastEl.classList.remove('visible');
    if (_toastTimer) { clearTimeout(_toastTimer); _toastTimer = null; }
}

async function tagHideReason(id, reason) {
    await fetch(`/api/items/${id}/hide-reason`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({reason})
    });
    dismissToast();
}

async function undoHide(id) {
    // Move the item back to Signals
    await fetch(`/api/items/${id}/restore`, {method: 'POST'});
    bumpNavCount('signals', +1);
    dismissToast();
}

async function removeFromSaved(id) {
    await fetch(`/api/items/${id}/remove`, {method: 'POST'});
    removeCard(id);
    bumpNavCount('lineup', -1);
}

async function restoreItem(id) {
    await fetch(`/api/items/${id}/restore`, {method: 'POST'});
    removeCard(id);
    bumpNavCount('signals', +1);
}

async function holdItem(id) {
    const res = await fetch(`/api/items/${id}/hold`, {method: 'POST'});
    if (res.ok) updateSavedCardState(id, false);
}

async function includeItem(id) {
    const res = await fetch(`/api/items/${id}/include`, {method: 'POST'});
    if (res.ok) updateSavedCardState(id, true);
}

function updateSavedCardState(id, included) {
    // Re-render the card by reloading the page section; simpler than swapping inline
    // since the toggle button + label + classes all change together.
    window.location.reload();
}

function editItem(id) {
    openEditModal(id);
}

// ─── Edit modal ──────────────────────────────────────────────────────────────

let _editModalEl = null;
let _editingId = null;

function ensureEditModal() {
    if (_editModalEl) return _editModalEl;
    const wrap = document.createElement('div');
    wrap.className = 'modal-backdrop';
    wrap.id = 'edit-modal';
    wrap.innerHTML = `
        <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="edit-modal-title">
            <div class="modal-header">
                <h2 id="edit-modal-title">Edit summary</h2>
                <p class="modal-hint">Refine the paragraph that will appear in the edition.</p>
            </div>
            <textarea id="edit-modal-textarea" class="modal-textarea" rows="10"></textarea>
            <div class="modal-actions">
                <span class="modal-keyboard-hint">⌘ + Enter to save · Esc to cancel</span>
                <button class="btn-secondary" onclick="closeEditModal()">Cancel</button>
                <button class="btn-primary" onclick="saveEditModal()">Save</button>
            </div>
        </div>`;
    document.body.appendChild(wrap);
    wrap.addEventListener('click', (e) => {
        if (e.target === wrap) closeEditModal();
    });
    document.addEventListener('keydown', (e) => {
        if (!wrap.classList.contains('visible')) return;
        if (e.key === 'Escape') closeEditModal();
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) saveEditModal();
    });
    _editModalEl = wrap;
    return wrap;
}

function openEditModal(id) {
    const modal = ensureEditModal();
    const el = document.getElementById(`summary-${id}`);
    if (!el) return;
    _editingId = id;
    const ta = document.getElementById('edit-modal-textarea');
    ta.value = el.textContent.trim();
    modal.classList.add('visible');
    setTimeout(() => { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }, 50);
}

function closeEditModal() {
    if (_editModalEl) _editModalEl.classList.remove('visible');
    _editingId = null;
}

async function saveEditModal() {
    if (!_editingId) return;
    const id = _editingId;
    const ta = document.getElementById('edit-modal-textarea');
    const newText = ta.value.trim();
    const summaryEl = document.getElementById(`summary-${id}`);
    const current = summaryEl ? summaryEl.textContent.trim() : '';
    if (newText && newText !== current) {
        await fetch(`/api/items/${id}/edit`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({summary: newText})
        });
        if (summaryEl) summaryEl.textContent = newText;
    }
    closeEditModal();
}

let spotlightSaveTimer = null;
async function saveSpotlight() {
    const el = document.getElementById('spotlight-input');
    if (!el) return;
    await fetch('/api/bucket/spotlight', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({spotlight: el.value})
    });
}

async function draftSpotlight() {
    const btn = document.getElementById('draft-spotlight-btn');
    const ta = document.getElementById('spotlight-input');
    if (!btn || !ta) return;
    const originalLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = '✦ Drafting…';
    try {
        const res = await fetch('/api/bucket/draft-spotlight', {method: 'POST'});
        const data = await res.json();
        if (res.ok && data.ok) {
            ta.value = data.spotlight;
            btn.textContent = '✦ Redraft';
        } else {
            alert(data.error || 'Drafting failed.');
            btn.textContent = originalLabel;
        }
    } catch (e) {
        alert('Network error: ' + e.message);
        btn.textContent = originalLabel;
    } finally {
        btn.disabled = false;
    }
}

function removeCard(id) {
    const card = document.getElementById(`item-${id}`);
    if (!card) return;
    const section = card.closest('.section');
    card.remove();
    if (section && !section.querySelector('.card')) {
        section.remove();
    }
}

function bumpNavCount(key, delta) {
    document.querySelectorAll(`.nav-tab[href]`).forEach(tab => {
        const label = tab.querySelector('.nav-tab-label');
        const countEl = tab.querySelector('.nav-tab-count');
        if (!label || !countEl) return;
        const labelText = label.textContent.trim().toLowerCase();
        if (labelText === key) {
            const cur = parseInt(countEl.textContent || '0', 10) || 0;
            countEl.textContent = Math.max(0, cur + delta);
        }
    });
}

async function exportNewsletter() {
    const btn = document.getElementById('export-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Publishing…'; }
    const res = await fetch('/api/export', {method: 'POST'});
    const data = await res.json();
    const banner = document.getElementById('export-banner');
    if (data.ok) {
        banner.textContent = `Published edition ${data.edition_id} (${data.published_count} items) → ${data.file}`;
        banner.classList.remove('hidden');
        setTimeout(() => { window.location.href = `/archive/${data.edition_id}`; }, 1200);
    } else {
        banner.textContent = data.error || 'Export failed.';
        banner.classList.remove('hidden');
        if (btn) { btn.disabled = false; btn.textContent = 'Publish Edition'; }
    }
}

// ─── Refresh (run the daily pipeline from the UI) ────────────────────────────

let refreshPollTimer = null;

async function triggerRefresh() {
    const storyBtn = document.getElementById('refresh-stories-btn');
    const settingsBtn = document.getElementById('refresh-btn');
    [storyBtn, settingsBtn].forEach(b => { if (b) b.disabled = true; });

    const banner = document.getElementById('refresh-banner');
    if (banner) {
        banner.textContent = 'Starting refresh…';
        banner.classList.remove('hidden');
    }

    const res = await fetch('/api/refresh', {method: 'POST'});
    const data = await res.json();
    if (!res.ok && res.status !== 202) {
        if (banner) banner.textContent = data.error || 'Could not start refresh.';
        [storyBtn, settingsBtn].forEach(b => { if (b) b.disabled = false; });
        return;
    }
    startRefreshPolling();
}

function startRefreshPolling() {
    if (refreshPollTimer) clearInterval(refreshPollTimer);
    pollRefreshOnce();
    refreshPollTimer = setInterval(pollRefreshOnce, 2500);
}

async function pollRefreshOnce() {
    const res = await fetch('/api/refresh/status');
    const s = await res.json();
    updateRefreshUI(s);
    if (s.state === 'done' || s.state === 'error' || s.state === 'idle') {
        clearInterval(refreshPollTimer);
        refreshPollTimer = null;
        // Re-enable buttons
        const storyBtn = document.getElementById('refresh-stories-btn');
        const settingsBtn = document.getElementById('refresh-btn');
        if (storyBtn) { storyBtn.disabled = false; storyBtn.textContent = '✦ Refresh'; }
        if (settingsBtn) { settingsBtn.disabled = false; settingsBtn.textContent = '✦ Refresh now'; }
        if (s.state === 'done') {
            const added = s.added || 0;
            const banner = document.getElementById('refresh-banner');
            if (banner) {
                banner.textContent = s.message || `Refresh complete — ${added} new stories.`;
                banner.classList.remove('hidden');
            }
            // Reload to surface the newly added Stories
            if (added > 0) setTimeout(() => window.location.reload(), 1200);
        } else if (s.state === 'error') {
            const banner = document.getElementById('refresh-banner');
            if (banner) {
                banner.textContent = 'Refresh failed: ' + (s.error || s.message || 'unknown error');
                banner.classList.remove('hidden');
            }
        }
    }
}

function updateRefreshUI(s) {
    const storyBtn = document.getElementById('refresh-stories-btn');
    const settingsBtn = document.getElementById('refresh-btn');
    if (s.state === 'running') {
        if (storyBtn) { storyBtn.disabled = true; storyBtn.textContent = '✦ Refreshing…'; }
        if (settingsBtn) { settingsBtn.disabled = true; settingsBtn.textContent = 'Refreshing…'; }
    }
    const banner = document.getElementById('refresh-banner');
    if (banner && s.state === 'running') {
        banner.textContent = s.message || 'Refreshing…';
        banner.classList.remove('hidden');
    }
    const progress = document.getElementById('pipeline-progress');
    const progressMsg = document.getElementById('pipeline-progress-msg');
    if (progress) {
        progress.classList.toggle('visible', s.state === 'running');
    }
    if (progressMsg) progressMsg.textContent = s.message || '';
}

// If we land on a page while a refresh is in flight, start polling so the UI stays in sync.
document.addEventListener('DOMContentLoaded', () => {
    fetch('/api/refresh/status').then(r => r.json()).then(s => {
        if (s.state === 'running') startRefreshPolling();
    }).catch(() => {});
});

// ─── Automation toggle (Settings) ─────────────────────────────────────────────

async function enableAutomation() {
    const btn = document.getElementById('automation-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Enabling…'; }
    try {
        const res = await fetch('/api/automation/enable', {method: 'POST'});
        const data = await res.json();
        if (res.ok && data.ok) {
            window.location.reload();
        } else {
            alert(data.error || 'Could not enable automation.');
            if (btn) { btn.disabled = false; btn.textContent = 'Enable daily refresh'; }
        }
    } catch (e) {
        alert('Network error: ' + e.message);
        if (btn) { btn.disabled = false; btn.textContent = 'Enable daily refresh'; }
    }
}

async function disableAutomation() {
    if (!confirm('Disable daily automatic refresh? You can still trigger refreshes manually with the ✦ Refresh button.')) return;
    const btn = document.getElementById('automation-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Disabling…'; }
    try {
        const res = await fetch('/api/automation/disable', {method: 'POST'});
        const data = await res.json();
        if (res.ok && data.ok) {
            window.location.reload();
        } else {
            alert(data.error || 'Could not disable automation.');
            if (btn) { btn.disabled = false; btn.textContent = 'Disable'; }
        }
    } catch (e) {
        alert('Network error: ' + e.message);
        if (btn) { btn.disabled = false; btn.textContent = 'Disable'; }
    }
}

// ─── Manual URL ingest (Signals) ──────────────────────────────────────────────

function toggleAddUrl() {
    const row = document.getElementById('add-url-row');
    const fb = document.getElementById('add-url-feedback');
    if (!row) return;
    row.classList.toggle('hidden');
    if (fb) fb.classList.add('hidden');
    if (!row.classList.contains('hidden')) {
        document.getElementById('add-url-input').focus();
    }
}

function showAddUrlFeedback(message, kind = 'info') {
    const fb = document.getElementById('add-url-feedback');
    if (!fb) return;
    fb.textContent = message;
    fb.classList.remove('hidden', 'error');
    if (kind === 'error') fb.classList.add('error');
}

async function ingestUrl() {
    const input = document.getElementById('add-url-input');
    const btn = document.getElementById('add-url-submit');
    if (!input || !btn) return;
    const url = input.value.trim();
    if (!url) {
        showAddUrlFeedback('Please paste a URL first.', 'error');
        return;
    }

    btn.disabled = true;
    btn.textContent = 'Adding…';
    showAddUrlFeedback('Fetching and summarizing…');

    try {
        const res = await fetch('/api/items/ingest-url', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url})
        });
        const data = await res.json();

        if (res.ok && data.ok) {
            showAddUrlFeedback(`Added: ${data.title}`);
            input.value = '';
            setTimeout(() => window.location.reload(), 900);
        } else if (res.status === 409) {
            showAddUrlFeedback(
                `Already in your store (status: ${data.status || 'unknown'}).`,
                'error'
            );
            btn.disabled = false;
            btn.textContent = 'Add';
        } else if (data && data.fallback) {
            openManualAddModal(url, data.error);
            btn.disabled = false;
            btn.textContent = 'Add';
        } else {
            showAddUrlFeedback(`Error: ${data.error || 'unknown error'}`, 'error');
            btn.disabled = false;
            btn.textContent = 'Add';
        }
    } catch (e) {
        showAddUrlFeedback(`Network error: ${e.message}`, 'error');
        btn.disabled = false;
        btn.textContent = 'Add';
    }
}

// ─── Manual paste fallback modal ──────────────────────────────────────────────

let _manualModalEl = null;

function ensureManualModal() {
    if (_manualModalEl) return _manualModalEl;
    const wrap = document.createElement('div');
    wrap.className = 'modal-backdrop';
    wrap.id = 'manual-add-modal';
    wrap.innerHTML = `
        <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="manual-modal-title">
            <div class="modal-header">
                <h2 id="manual-modal-title">Add manually</h2>
                <p class="modal-hint" id="manual-modal-hint">We couldn't auto-extract this page. Paste the title and a snippet of the article body — the Editor will summarize from there.</p>
            </div>
            <input type="text" id="manual-modal-title-input" class="add-url-input" placeholder="Article title" style="margin-bottom: 12px;">
            <textarea id="manual-modal-content" class="modal-textarea" rows="8" placeholder="Paste the article body or a representative excerpt (a paragraph or two is enough)"></textarea>
            <div class="modal-actions">
                <span class="modal-keyboard-hint">⌘ + Enter to add · Esc to cancel</span>
                <button class="btn-secondary" onclick="closeManualModal()">Cancel</button>
                <button class="btn-primary" onclick="submitManualAdd()">Add</button>
            </div>
        </div>`;
    document.body.appendChild(wrap);
    wrap.addEventListener('click', (e) => { if (e.target === wrap) closeManualModal(); });
    document.addEventListener('keydown', (e) => {
        if (!wrap.classList.contains('visible')) return;
        if (e.key === 'Escape') closeManualModal();
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) submitManualAdd();
    });
    _manualModalEl = wrap;
    return wrap;
}

let _manualUrl = null;
function openManualAddModal(url, reason) {
    const modal = ensureManualModal();
    _manualUrl = url;
    const hint = document.getElementById('manual-modal-hint');
    if (hint && reason) {
        hint.textContent = `${reason} Paste the title and body manually instead.`;
    }
    document.getElementById('manual-modal-title-input').value = '';
    document.getElementById('manual-modal-content').value = '';
    modal.classList.add('visible');
    setTimeout(() => document.getElementById('manual-modal-title-input').focus(), 50);
}

function closeManualModal() {
    if (_manualModalEl) _manualModalEl.classList.remove('visible');
    _manualUrl = null;
}

async function submitManualAdd() {
    if (!_manualUrl) return;
    const title = document.getElementById('manual-modal-title-input').value.trim();
    const content = document.getElementById('manual-modal-content').value.trim();
    if (!title || !content) {
        alert('Title and content are both required.');
        return;
    }
    try {
        const res = await fetch('/api/items/ingest-url', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({url: _manualUrl, title, content})
        });
        const data = await res.json();
        if (res.ok && data.ok) {
            closeManualModal();
            showAddUrlFeedback(`Added: ${data.title}`);
            setTimeout(() => window.location.reload(), 900);
        } else {
            alert(data.error || 'Could not add.');
        }
    } catch (e) {
        alert('Network error: ' + e.message);
    }
}

// ─── Feeds editor ────────────────────────────────────────────────────────────

function addFeedRow() {
    const list = document.getElementById('feeds-list');
    if (!list) return;
    const div = document.createElement('div');
    div.className = 'feed-row';
    div.innerHTML = `
        <input class="feed-input feed-name" placeholder="Name" value="">
        <input class="feed-input feed-url" placeholder="https://…" value="">
        <input class="feed-input feed-hint" placeholder="Category hint (optional)" value="">
        <button class="feed-remove" onclick="removeFeedRow(this)" title="Remove">✕</button>`;
    list.appendChild(div);
    div.querySelector('.feed-name').focus();
}

function removeFeedRow(btn) {
    const row = btn.closest('.feed-row');
    if (row) row.remove();
    const count = document.querySelectorAll('.feed-row').length;
    const countEl = document.getElementById('feeds-count');
    if (countEl) countEl.textContent = count;
}

async function saveFeeds() {
    const rows = document.querySelectorAll('.feed-row');
    const feeds = [];
    rows.forEach(r => {
        const name = r.querySelector('.feed-name').value.trim();
        const url = r.querySelector('.feed-url').value.trim();
        const hint = r.querySelector('.feed-hint').value.trim();
        if (url) feeds.push({name, url, category_hint: hint});
    });

    const btn = document.getElementById('feeds-save-btn');
    const banner = document.getElementById('feeds-banner');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }

    const res = await fetch('/api/feeds', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({feeds})
    });
    const data = await res.json();
    if (banner) {
        banner.textContent = data.ok
            ? `Saved ${data.count} feeds.`
            : `Save failed: ${data.error || 'unknown error'}`;
        banner.classList.remove('hidden');
        setTimeout(() => banner.classList.add('hidden'), 3500);
    }
    if (btn) { btn.disabled = false; btn.textContent = 'Save changes'; }

    if (data.ok) {
        const countEl = document.getElementById('feeds-count');
        if (countEl) countEl.textContent = data.count;
    }
}
</script>"""
