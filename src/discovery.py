"""
Scout — Discovery (web search beyond the RSS feed list)

Finds candidate articles by *searching the web* for the newsletter's topics,
instead of only reading the fixed RSS feeds in config.yaml. Results are
returned in the same shape as collector items, so they flow through the
existing  dedup -> Scout -> Editor  pipeline unchanged — the learned relevance
filter and editorial voice apply to discovered items for free.

Honest scope (see the README/CLAUDE.md discussion):

- This does NOT fetch article bodies. It surfaces title + snippet + URL from
  the search provider; Scout judges relevance from that, exactly as it does an
  RSS blurb. Because we never fetch arbitrary result URLs, discovery adds no
  SSRF surface — the only host contacted is the configured search provider.
- "Intelligent" here means the queries ADAPT to your taste: static queries from
  config are augmented with queries the LLM generates from your recently
  saved / published stories. The search follows what you actually pick.
- Bounded on every axis: per-query result cap, total cap, a recency window,
  a hard cap on adaptive queries, and an off switch (`discovery.enabled`).
- Fully defensive: any provider/LLM/network failure yields fewer (or zero)
  results and logs a line — it never raises into the pipeline.

Providers (config `discovery.provider`):
  gdelt   — GDELT 2.0 Doc API. Free, no API key. Broad news coverage; noisier,
            but Scout filters it downstream. This is the zero-friction default.
  tavily  — Tavily Search API. Needs a key (free tier). Cleaner, LLM-oriented.
  brave   — Brave Search API. Needs a key (free tier). General web search.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from pipeline_log import log  # noqa: E402

_SEARCH_TIMEOUT_SECONDS = 20.0
_USER_AGENT = "Scout/1.0 (+https://github.com/denizergurel/scout)"


def _discovery_cfg(cfg: dict) -> dict:
    return cfg.get("discovery") or {}


def is_enabled(cfg: dict) -> bool:
    """Off by default unless `discovery.enabled: true` in config.yaml."""
    return bool(_discovery_cfg(cfg).get("enabled", False))


# ─── Query building ──────────────────────────────────────────────────────────


def _static_queries(cfg: dict) -> list[str]:
    """The hand-written queries in config.yaml's `search_queries:` block."""
    raw = cfg.get("search_queries") or []
    return [str(q).strip() for q in raw if str(q).strip()]


def _recent_pick_titles(limit: int) -> list[str]:
    """Titles the editor recently saved or published — the taste signal we
    generate adaptive queries from. Read defensively; any failure yields []."""
    try:
        from store import by_status

        picks = by_status("saved") + by_status("published")
    except Exception:  # noqa: BLE001 — store unavailable shouldn't break discovery
        return []

    def _key(it: dict) -> str:
        return it.get("decided_at") or it.get("added_at") or ""

    picks.sort(key=_key, reverse=True)
    titles: list[str] = []
    for it in picks:
        title = (it.get("title") or "").strip()
        if title:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def _adaptive_queries(cfg: dict, limit: int) -> list[str]:
    """Ask the LLM for fresh search queries grounded in the editor's recent
    picks. Returns [] on any failure (no LLM, bad JSON, etc.) so discovery
    degrades to the static queries rather than breaking."""
    if limit <= 0:
        return []

    titles = _recent_pick_titles(15)
    if not titles:
        # No taste signal yet — nothing to adapt from. The static queries carry
        # the first few runs; adaptive kicks in once the editor has picked items.
        return []

    topics = (cfg.get("newsletter") or {}).get("topics") or "the newsletter's topics"
    titles_block = "\n".join(f"- {t}" for t in titles)
    system_prompt = (
        "You generate concise web-search queries for a news scout. "
        "You output only a JSON array of strings."
    )
    user_message = (
        f"Newsletter topics: {topics}.\n\n"
        f"The editor recently selected these stories:\n{titles_block}\n\n"
        f"Generate up to {limit} short, specific web-search queries likely to "
        "surface NEW, recent developments related to these topics and the "
        "editor's evident interests. Prefer concrete entities (products, "
        "companies, people, emerging themes) over generic phrases. Do not just "
        "restate the story titles. Respond with ONLY a JSON array of query "
        'strings, e.g. ["query one", "query two"].'
    )

    try:
        from llm import call_llm_json

        result = call_llm_json(system_prompt, user_message, stage="scout")
    except Exception as e:  # noqa: BLE001 — adaptive queries are best-effort
        log(f"  ⚠ Discovery: adaptive query generation failed ({type(e).__name__}: {e}). Using static queries only.")
        return []

    if not isinstance(result, list):
        return []
    queries: list[str] = []
    for q in result:
        if isinstance(q, str) and q.strip():
            queries.append(q.strip())
        if len(queries) >= limit:
            break
    return queries


def _build_queries(cfg: dict) -> list[str]:
    """Static + adaptive queries, de-duplicated (case-insensitive), order-stable."""
    settings = _discovery_cfg(cfg)
    queries = list(_static_queries(cfg))
    if settings.get("adaptive_queries", True):
        max_adaptive = max(0, int(settings.get("max_adaptive_queries", 6)))
        adaptive = _adaptive_queries(cfg, max_adaptive)
        if adaptive:
            log(f"  Discovery: generated {len(adaptive)} adaptive queries from your recent picks")
        queries.extend(adaptive)

    seen: set[str] = set()
    unique: list[str] = []
    for q in queries:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique


# ─── Provider implementations ────────────────────────────────────────────────


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc.removeprefix("www.") if netloc else ""


def _resolve_api_key(settings: dict) -> str | None:
    if settings.get("api_key"):
        return str(settings["api_key"])
    env_name = settings.get("api_key_env")
    if env_name:
        return os.environ.get(env_name)
    return None


def _search_gdelt(query: str, *, timespan_days: int, max_results: int) -> list[dict]:
    """GDELT 2.0 Doc API. No key. Multi-word queries are phrase-quoted for
    precision (space is AND in GDELT, which is otherwise very broad)."""
    q = f'"{query}"' if (" " in query and '"' not in query) else query
    params = {
        "query": f"{q} sourcelang:english",
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max(1, min(max_results, 50))),
        "timespan": f"{max(1, timespan_days)}d",
        "sort": "DateDesc",
    }
    resp = httpx.get(
        "https://api.gdeltproject.org/api/v2/doc/doc",
        params=params,
        timeout=_SEARCH_TIMEOUT_SECONDS,
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    # GDELT sometimes returns HTML on malformed queries; guard the JSON parse.
    try:
        data = resp.json()
    except ValueError:
        return []

    items: list[dict] = []
    for art in data.get("articles", []) or []:
        url = (art.get("url") or "").strip()
        title = (art.get("title") or "").strip()
        if not url or not title:
            continue
        published = None
        seendate = (art.get("seendate") or "").strip()
        if seendate:
            try:
                published = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                ).isoformat()
            except ValueError:
                published = None
        items.append(
            {
                "title": title,
                "link": url,
                "published": published,
                # GDELT gives no snippet; the title is the relevance signal.
                "description": "",
                "source": (art.get("domain") or _domain(url) or "Web"),
            }
        )
    return items


def _search_tavily(query: str, *, max_results: int, api_key: str | None) -> list[dict]:
    """Tavily Search API (news topic). Returns clean title/snippet/url."""
    if not api_key:
        raise RuntimeError("Tavily requires an API key. Set discovery.api_key_env in config.yaml.")
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "topic": "news",
            "max_results": max(1, min(max_results, 20)),
            "search_depth": "basic",
        },
        timeout=_SEARCH_TIMEOUT_SECONDS,
        headers={"User-Agent": _USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    items: list[dict] = []
    for r in data.get("results", []) or []:
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        if not url or not title:
            continue
        items.append(
            {
                "title": title,
                "link": url,
                "published": (r.get("published_date") or "").strip() or None,
                "description": (r.get("content") or "").strip()[:500],
                "source": _domain(url) or "Web",
            }
        )
    return items


def _search_brave(query: str, *, max_results: int, api_key: str | None) -> list[dict]:
    """Brave Search API (web). Returns title/description/url."""
    if not api_key:
        raise RuntimeError("Brave requires an API key. Set discovery.api_key_env in config.yaml.")
    resp = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": str(max(1, min(max_results, 20)))},
        timeout=_SEARCH_TIMEOUT_SECONDS,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    items: list[dict] = []
    for r in (data.get("web", {}) or {}).get("results", []) or []:
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        if not url or not title:
            continue
        items.append(
            {
                "title": title,
                "link": url,
                "published": (r.get("age") or "").strip() or None,
                "description": (r.get("description") or "").strip()[:500],
                "source": _domain(url) or "Web",
            }
        )
    return items


def _run_search(provider: str, query: str, settings: dict) -> list[dict]:
    timespan_days = max(1, int(settings.get("timespan_days", 7)))
    max_results = max(1, int(settings.get("max_results_per_query", 10)))
    if provider == "gdelt":
        return _search_gdelt(query, timespan_days=timespan_days, max_results=max_results)
    if provider == "tavily":
        return _search_tavily(query, max_results=max_results, api_key=_resolve_api_key(settings))
    if provider == "brave":
        return _search_brave(query, max_results=max_results, api_key=_resolve_api_key(settings))
    raise ValueError(f"Unknown discovery provider {provider!r} (use gdelt, tavily, or brave).")


# ─── Public entry point ──────────────────────────────────────────────────────


def discover(cfg: dict) -> list[dict]:
    """Run web-search discovery and return candidate items in collector shape.

    Items carry `via="discovery:<provider>"` so their provenance is visible in
    the store. Returns [] (never raises) if discovery is disabled or every
    query fails."""
    if not is_enabled(cfg):
        return []

    settings = _discovery_cfg(cfg)
    provider = str(settings.get("provider") or "gdelt").lower()
    total_cap = max(1, int(settings.get("max_total_results", 60)))

    queries = _build_queries(cfg)
    if not queries:
        log("  Discovery: no queries configured (add search_queries or enable adaptive_queries).")
        return []

    log(f"  Discovery: searching {len(queries)} queries via {provider!r}…")

    seen_links: set[str] = set()
    results: list[dict] = []
    failed = 0
    for query in queries:
        try:
            found = _run_search(provider, query, settings)
        except Exception as e:  # noqa: BLE001 — one bad query shouldn't sink discovery
            failed += 1
            log(f"  ⚠ Discovery query {query!r} failed ({type(e).__name__}: {e}).")
            continue
        for item in found:
            link = (item.get("link") or "").strip()
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            item.setdefault("category_hint", "")
            item["via"] = f"discovery:{provider}"
            results.append(item)
            if len(results) >= total_cap:
                break
        if len(results) >= total_cap:
            log(f"  Discovery: hit total cap of {total_cap} results; stopping.")
            break

    if failed and failed == len(queries):
        log(f"  ⚠ Discovery: all {failed} queries failed — provider likely unreachable or misconfigured.")
    log(f"  Discovery: {len(results)} unique candidate articles from web search")
    return results


# ─── Source promotion (suggest new feeds from your behavior) ──────────────────


def suggest_new_sources(cfg: dict, *, min_count: int = 2, limit: int = 8) -> list[dict]:
    """Domains you've repeatedly saved/published that aren't in your RSS feeds.

    This is the 'grow your own source list' signal: if the editor keeps picking
    items from a domain not in config.yaml's `sources:`, it's a candidate to add
    as a permanent feed. Read-only — returns suggestions, changes nothing."""
    try:
        from store import by_status
    except Exception:  # noqa: BLE001
        return []

    configured = set()
    for s in cfg.get("sources") or []:
        url = (s.get("url") or "") if isinstance(s, dict) else ""
        dom = _domain(url)
        if dom:
            configured.add(dom)

    counts: dict[str, int] = {}
    for it in by_status("saved") + by_status("published"):
        dom = _domain((it.get("link") or ""))
        if not dom or dom in configured:
            continue
        counts[dom] = counts.get(dom, 0) + 1

    ranked = sorted(
        ((dom, n) for dom, n in counts.items() if n >= min_count),
        key=lambda x: x[1],
        reverse=True,
    )[:limit]
    return [{"domain": dom, "picked_count": n} for dom, n in ranked]
