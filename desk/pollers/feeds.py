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
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

log = logging.getLogger(__name__)

WINDOW_HOURS = 36
UA = "prediction-desk/1.0 (+https://github.com/) read-only feed reader"

GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search"

# Politeness between feed fetches, applied PER HOST.
#
# The first Actions run of the derived feeds got HTTP 503 on all eleven Google
# News requests, while a single probe request from the same runner minutes later
# returned 200 with 100 items. So this is rate limiting, not a datacenter-IP
# block, and the fix is spacing rather than headers. A global 0.4-1.1s jitter is
# far too aggressive when eleven consecutive requests all hit news.google.com.
POLITE_DELAY_RANGE = (0.4, 1.1)          # between different hosts
SAME_HOST_DELAY_RANGE = (3.5, 6.0)       # between requests to the SAME host
MAX_RETRIES = 3

# 503 from a rate limiter needs a much longer wait than a transient 5xx.
RATE_LIMIT_BACKOFF = (8.0, 20.0, 35.0)

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
                # 429 and 503 mean "you are going too fast"; a plain 5xx is more
                # likely transient. Google News answers 503 when rate limiting,
                # and recovers only after a real pause.
                if r.status_code in (429, 503):
                    wait = RATE_LIMIT_BACKOFF[min(attempt, len(RATE_LIMIT_BACKOFF) - 1)]
                else:
                    wait = delay
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
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


# Famous-name collisions: if one of these FULL names appears in an item, the
# matching surname is suppressed for that item. Grown from live false
# positives -- "Jerome Powell leaving the Federal Reserve Board" tagged the
# WA-05 race on the bare surname "Powell", and "Jo Stevens, British MP" tagged
# the Michigan Senate primary.
FAMOUS_COLLISIONS = {
    "powell": ("jerome powell", "colin powell", "sidney powell", "jesse powell",
               "powell doctrine", "powell memo"),
    "stevens": ("jo stevens", "sufjan stevens", "cat stevens", "ted stevens",
                "john paul stevens", "stevens institute", "stevens point"),
    "james": ("lebron james", "james webb", "etta james", "rick james",
              "james brown", "james bond"),
    "kelly": ("megyn kelly", "r. kelly", "kelly clarkson", "mark kelly"),
    "smith": ("adam smith economist",),
    "jackson": ("michael jackson", "andrew jackson", "jackson hole",
                "ketanji brown jackson", "jesse jackson"),
    "collins": ("phil collins", "judy collins"),
    "gray": ("freddie gray", "gray zone", "dorian gray"),
    "hong": ("hong kong",),
}

_IDENTIFIER_RE = re.compile(
    r"\b[A-Z]{2}-(\d{2}|Sen|Gov)\b"                       # MO-04, MI-Sen, KS-Gov
    r"|\b(washington|michigan|missouri|wisconsin|minnesota|maine|kansas|"
    r"massachusetts|vermont|connecticut)\b.*"
    r"\b(senate|governor|house|district|\d+(st|nd|rd|th))\b", re.I)


def _word_hit(needle: str, low_text: str) -> bool:
    return re.search(rf"\b{re.escape(needle.lower())}\b", low_text) is not None


def build_race_matchers(watchlist: list) -> dict[str, dict]:
    """Per-race matcher structures for tag_for.

    names       full candidate names (watchlist `candidates` + `candidate`)
    surnames    their last tokens (suffix-stripped, >3 chars)
    identifiers race-specific keywords (an explicit race id, or state+office)
                -- sufficient ALONE to tag
    context     everything else in `keywords` (places, allied names) --
                supports a surname but never tags alone
    """
    suffixes = {"jr", "sr", "ii", "iii", "iv", "3rd", "2nd", "(dem)", "(gop)"}
    out: dict[str, dict] = {}
    for row in watchlist or []:
        tag = row.get("race_tag")
        if not tag:
            continue
        m = out.setdefault(tag, {"names": set(), "surnames": set(),
                                 "identifiers": set(), "context": set()})
        for n in list(row.get("candidates") or []) + [row.get("candidate")]:
            if not n:
                continue
            n = re.sub(r"\s*\(.*?\)\s*", " ", str(n)).strip()
            if len(n) < 4:
                continue
            m["names"].add(n.lower())
            parts = [p for p in n.lower().split() if p.strip(".") not in suffixes]
            if len(parts) >= 2 and len(parts[-1]) > 3:
                m["surnames"].add(parts[-1])
        for w in row.get("keywords") or []:
            w = str(w).strip()
            if len(w) < 4:
                continue
            if _IDENTIFIER_RE.search(w):
                m["identifiers"].add(w.lower())
            else:
                m["context"].add(w.lower())
    for m in out.values():
        # A keyword that merely repeats a candidate's surname ("Stevens" in the
        # MI-Sen keywords) must not double as context, or a bare surname becomes
        # its own supporting evidence and tags alone anyway.
        m["context"] -= m["surnames"]
        m["context"] -= m["names"]
    return {k: v for k, v in out.items() if v["names"] or v["identifiers"]}


def tag_for(text: str, matchers: dict) -> str | None:
    """Best race tag for an item, or None.

    A race matches when the item carries:
      * a candidate's FULL name, or
      * a race identifier ("MO-04", "Michigan Senate"), or
      * two distinct candidate names of the same race (any form), or
      * one surname PLUS a same-item context token.
    A bare surname never tags on its own -- that is exactly how Jerome Powell
    became a WA-05 item -- and a famous-name collision suppresses the surname
    even when context is present.
    """
    low = (text or "").lower()
    if not low:
        return None

    # Backward compat: accept the old {tag: [words]} shape from older tests.
    if matchers and isinstance(next(iter(matchers.values())), (list, tuple)):
        matchers = build_race_matchers(
            [{"race_tag": t, "candidates": [], "keywords": list(ws)}
             for t, ws in matchers.items()])

    best, best_score = None, 0
    for tag, m in matchers.items():
        score = 0
        full_hits = {n for n in m["names"] if n in low}
        id_hits = {i for i in m["identifiers"] if _word_hit(i, low)}
        surname_hits = set()
        for sn in m["surnames"]:
            if not _word_hit(sn, low):
                continue
            famous = FAMOUS_COLLISIONS.get(sn, ())
            if any(f in low for f in famous):
                continue                      # collision: suppress this surname
            surname_hits.add(sn)
        ctx_hits = {c for c in m["context"] if c in low}

        name_hits = len(full_hits) + len(
            {s for s in surname_hits if not any(s in f for f in full_hits)})
        qualifies = (bool(full_hits) or bool(id_hits) or name_hits >= 2
                     or (bool(surname_hits) and bool(ctx_hits)))
        if not qualifies:
            continue
        score = 2 * len(full_hits) + 2 * len(id_hits) + len(surname_hits) + len(ctx_hits)
        if score > best_score:
            best, best_score = tag, score
    return best


def keyword_hits(text: str) -> list[str]:
    low = (text or "").lower()
    return [k for k in KEYWORDS if k in low]


def readable_link(url: str | None) -> str | None:
    """Drop links that are useless in a phone notification.

    Google News RSS items link to news.google.com/rss/articles/<long base64
    blob>, which redirects to the publisher. Pasted into an ntfy body it is an
    unreadable wall of characters that tells you nothing about where the story
    came from -- the headline itself is the useful payload, so these are dropped
    and the title is carried instead.
    """
    if not url:
        return None
    if "news.google.com/rss/" in url:
        return None
    return url


def publisher_of(title: str) -> str | None:
    """Google News suffixes each headline with ' - Publisher'.

    Recovering it gives the notification a source name even though the link
    itself is discarded.
    """
    if not title or " - " not in title:
        return None
    tail = title.rsplit(" - ", 1)[-1].strip()
    # Publisher names are short; a long tail is almost certainly headline prose.
    return tail if 0 < len(tail) <= 40 else None


def clean_headline(title: str) -> str:
    """The headline without its trailing ' - Publisher'.

    The publisher is shown on its own attribution line, so leaving the suffix in
    would print it twice and eat characters in a notification that truncates.
    """
    if not title:
        return ""
    pub = publisher_of(title)
    if pub and title.endswith(f" - {pub}"):
        return title[: -len(f" - {pub}")].strip()
    return title.strip()


def collect(client: httpx.Client, feeds: list[dict], race_keywords: dict[str, list[str]],
            window_hours: int = WINDOW_HOURS) -> tuple[list[dict], list[str]]:
    """Fetch every configured feed, keep recent items, tag and dedupe by GUID."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    seen: set[str] = set()
    items: list[dict] = []
    errors: list[str] = []
    last_host: str | None = None

    for i, f in enumerate(feeds):
        url, source = f.get("url"), f.get("source") or f.get("tag") or "feed"
        if not url or not f.get("active", True):
            continue

        # Jittered pause between fetches, longer when the previous request went
        # to the same host. Eleven derived feeds all hit news.google.com, and at
        # the short interval every one of them came back 503.
        host = urllib.parse.urlsplit(url).netloc.lower()
        if i:
            rng = SAME_HOST_DELAY_RANGE if host == last_host else POLITE_DELAY_RANGE
            time.sleep(random.uniform(*rng))
        last_host = host

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
