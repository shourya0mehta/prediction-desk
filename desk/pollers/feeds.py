"""RSS/Atom watcher. Public feeds only -- no login, no headless browser, no X.

Spec 0.2 forbids scraping anything behind a login. Everything configured in
feeds.yaml is an open RSS endpoint, verified to parse and return dated entries
at build time. X/Twitter is deliberately absent: the pipeline never touches it,
and the analyst is told to treat unexplained moves as a prompt for the owner to
check X by hand.
"""

from __future__ import annotations

import logging
import random
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

log = logging.getLogger(__name__)

WINDOW_HOURS = 36
UA = "prediction-desk/1.0 (+https://github.com/) read-only feed reader"

GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search"

# Politeness between feed fetches: a small randomised pause so a burst of
# derived feeds never looks like a scrape to Google News.
POLITE_DELAY_RANGE = (0.4, 1.1)
MAX_RETRIES = 3

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


def google_news_url(names: list[str]) -> str | None:
    """One Google News RSS search URL covering a race's candidates.

    The candidate names are OR'd as quoted phrases so the query matches the
    people rather than the loose words in their names -- an unquoted
    ``Nate Powell`` also matches every unrelated Powell in the news.
    """
    clean = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not clean:
        return None
    query = " OR ".join(f'"{n}"' for n in clean)
    params = urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    return f"{GOOGLE_NEWS_SEARCH}?{params}"


def derive_feeds(watchlist: list) -> list[dict]:
    """Build one Google News feed per active race from watchlist candidates.

    This replaces the manual Google Alerts setup: the same candidate names that
    would have been pasted into 62 alert queries now live in ``watchlist.yaml``
    as a ``candidates`` list, and the feed URL is derived from them. Adding a
    race therefore brings its news coverage with it, with nothing to click.

    Feeds are pre-tagged with their ``race_tag``, which is strictly better than
    inferring it: an article about a race's candidate belongs to that race even
    when the headline never names the state or district.
    """
    out = []
    for row in watchlist or []:
        if not row.get("active", True):
            continue
        tag = row.get("race_tag")
        url = google_news_url(row.get("candidates") or [])
        if not tag or not url:
            continue
        out.append({
            "source": f"gnews-{tag}",
            "tier": "core",
            "active": True,
            "url": url,
            "race_tag": tag,
            "derived": True,
        })
    return out


def fetch_feed(client: httpx.Client, url: str, retries: int = MAX_RETRIES) -> tuple[list, str | None]:
    """Return (entries, error). Never raises -- a dead feed must not kill a run.

    Honours 429 and 5xx with exponential backoff, respecting ``Retry-After``
    when the server sends one. Backing off is the difference between being
    rate-limited briefly and being blocked.
    """
    delay = 1.0
    last_err = None

    for attempt in range(retries):
        try:
            r = client.get(url, headers={"User-Agent": UA})
            if r.status_code == 429 or 500 <= r.status_code < 600:
                retry_after = r.headers.get("Retry-After")
                wait = delay
                if retry_after:
                    try:
                        wait = max(delay, float(retry_after))
                    except ValueError:
                        pass
                last_err = f"HTTP {r.status_code}"
                if attempt < retries - 1:
                    log.info("feed %s -> %s, backing off %.1fs", url[:60], last_err, wait)
                    time.sleep(wait + random.uniform(0, 0.5))
                    delay *= 2
                    continue
                return [], last_err
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            return list(parsed.entries or []), None
        except httpx.HTTPError as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < retries - 1:
                time.sleep(delay + random.uniform(0, 0.5))
                delay *= 2
                continue
            return [], last_err
        except Exception as e:  # feedparser is tolerant, but never trust it fully
            return [], f"parse error: {type(e).__name__}: {e}"

    return [], last_err or "exhausted retries"


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

    for i, f in enumerate(feeds):
        url, source = f.get("url"), f.get("source") or f.get("tag") or "feed"
        if not url or not f.get("active", True):
            continue

        # Jittered pause between fetches. Derived feeds all hit the same host,
        # so a tight loop would look like scraping.
        if i:
            time.sleep(random.uniform(*POLITE_DELAY_RANGE))

        entries, err = fetch_feed(client, url)
        if err:
            errors.append(f"feed {source}: {err}")
            continue
        if not entries:
            errors.append(f"feed {source}: returned no entries")
            continue

        tier = f.get("tier", "core")
        feed_tag = f.get("race_tag")
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
            # Keyword tagging is unchanged. A derived feed's own race_tag is
            # used only as the fallback, so an article that names no keyword
            # still lands in the race whose candidate feed produced it.
            race_tag = tag_for(blob, race_keywords) or feed_tag

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
