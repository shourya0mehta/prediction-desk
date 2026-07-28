"""RSS/Atom watcher. Public feeds only -- no login, no headless browser, no X.

Spec 0.2 forbids scraping anything behind a login. Everything configured in
feeds.yaml is an open RSS endpoint, verified to parse and return dated entries
at build time. X/Twitter is deliberately absent: the pipeline never touches it,
and the analyst is told to treat unexplained moves as a prompt for the owner to
check X by hand.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

log = logging.getLogger(__name__)

WINDOW_HOURS = 36
UA = "prediction-desk/1.0 (+https://github.com/) read-only feed reader"

# Words that mean a race just changed shape. Kept blunt on purpose -- a false
# positive costs one glance at a headline; a false negative costs a position.
KEYWORDS = (
    "endorse", "endorsement", "withdraw", "withdraws", "drops out", "drop out",
    "suspends campaign", "scandal", "allegation", "indict", "indicted",
    "poll", "polling", "resign", "arrested", "charged", "lawsuit", "ballot",
    "disqualif", "recount", "concede", "concedes",
)


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def fetch_feed(client: httpx.Client, url: str) -> tuple[list, str | None]:
    """Return (entries, error). Never raises -- a dead feed must not kill a run."""
    try:
        r = client.get(url, headers={"User-Agent": UA})
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        return list(parsed.entries or []), None
    except httpx.HTTPError as e:
        return [], f"{type(e).__name__}: {e}"
    except Exception as e:  # feedparser is tolerant, but never trust it fully
        return [], f"parse error: {type(e).__name__}: {e}"


def tag_for(text: str, race_keywords: dict[str, list[str]]) -> str | None:
    low = (text or "").lower()
    best, best_hits = None, 0
    for tag, words in race_keywords.items():
        hits = sum(1 for w in words if w.lower() in low)
        if hits > best_hits:
            best, best_hits = tag, hits
    return best


def keyword_hits(text: str) -> list[str]:
    low = (text or "").lower()
    return [k for k in KEYWORDS if k in low]


def collect(client: httpx.Client, feeds: list[dict], race_keywords: dict[str, list[str]],
            window_hours: int = WINDOW_HOURS) -> tuple[list[dict], list[str]]:
    """Fetch every configured feed, keep recent items, tag and dedupe by GUID."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    seen: set[str] = set()
    items: list[dict] = []
    errors: list[str] = []

    for f in feeds:
        url, source = f.get("url"), f.get("source") or f.get("tag") or "feed"
        if not url or not f.get("active", True):
            continue

        entries, err = fetch_feed(client, url)
        if err:
            errors.append(f"feed {source}: {err}")
            continue
        if not entries:
            errors.append(f"feed {source}: returned no entries")
            continue

        tier = f.get("tier", "core")
        for e in entries:
            ts = _entry_time(e)
            if ts is None or ts < cutoff:
                continue
            guid = e.get("id") or e.get("link") or e.get("title")
            if not guid or guid in seen:
                continue

            title = e.get("title") or ""
            summary = e.get("summary") or ""
            blob = f"{title} {summary}"
            hits = keyword_hits(blob)
            race_tag = tag_for(blob, race_keywords)

            # Secondary feeds are pure noise unless a keyword or a race name
            # fires -- a generic upload from a big channel is not news.
            if tier == "secondary" and not (hits and race_tag):
                continue

            seen.add(guid)
            items.append({
                "source": source,
                "tier": tier,
                "ts": ts.isoformat(),
                "title": title[:220],
                "url": e.get("link"),
                "race_tag": race_tag,
                "keywords": hits,
            })

    items.sort(key=lambda i: i["ts"], reverse=True)
    return items, errors
