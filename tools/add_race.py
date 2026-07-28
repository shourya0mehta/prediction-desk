#!/usr/bin/env python3
"""Add a race to the watchlist from one Kalshi ticker or URL.

Run from the Actions tab on a phone: paste a ticker, get a complete watchlist
block, a primer stub and a derived candidates list appended to the gist, plus an
ntfy confirmation saying exactly what it did and what still needs a human.

    python tools/add_race.py --input KXSENATEMID-26-AELS
    python tools/add_race.py --input https://kalshi.com/markets/kxgovwinomd/...
    python tools/add_race.py --input KXGOVMNNOMR-26-MLIN \
        --polymarket-condition 0xbbd6... --resolution-date 2026-08-11

Accepts a market ticker, an event ticker, a series ticker, or a kalshi.com URL
containing any of those.

Two things it deliberately will not guess
-----------------------------------------
**resolution_date.** Kalshi's ``close_time`` is a far-future placeholder on
political boards -- the WA markets read 2027-11-03 for an August 2026 primary --
so it is never a resolution date. If ``--resolution-date`` is not supplied the
block is written with ``resolution_date: null`` and ``active: false``, so the
race is staged but cannot silently join the alert path with a wrong countdown.
The ntfy message says so.

**Which sibling is the tracked market.** If given an event or series it picks the
highest-priced market and says so, because that is the favourite and the usual
intent; if that is wrong, the block is one edit away.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk.core.books import D                      # noqa: E402
from desk.core.state import Gist, GistError        # noqa: E402
from desk.pollers.kalshi import KalshiPoller, KalshiError  # noqa: E402

WATCHLIST = "watchlist.yaml"
INDEX = "primers-index.json"

# Sibling markets below this price are noise for a candidates list: on a
# fragmented primary board most names sit at a tenth of a cent.
CANDIDATE_MIN_PRICE = D("0.005")
MAX_CANDIDATES = 8


def parse_input(raw: str) -> str:
    """Pull a ticker out of a raw ticker or a kalshi.com URL."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("no input given")
    if "kalshi.com" in raw.lower():
        path = re.sub(r"[?#].*$", "", raw).rstrip("/").split("/")
        # URLs look like /markets/<series>/<slug> or /markets/<TICKER>; the
        # ticker-ish segment is the one with no lowercase words in it, else the
        # segment right after /markets/.
        segs = [s for s in path if s and s.lower() not in ("https:", "http:", "")]
        for s in reversed(segs):
            if re.fullmatch(r"[A-Za-z0-9]+(-[A-Za-z0-9]+)*", s) and any(c.isdigit() for c in s):
                return s.upper()
        if "markets" in segs:
            i = segs.index("markets")
            if i + 1 < len(segs):
                return segs[i + 1].upper()
        raise ValueError(f"could not find a ticker in URL: {raw}")
    return raw.upper()


def resolve(poller: KalshiPoller, token: str) -> tuple[dict, list[dict], str]:
    """Return (target_market, siblings, how_target_was_chosen)."""
    # 1) a full market ticker -- track exactly what was asked for
    try:
        m = poller._get(f"/markets/{token}").get("market")
        if m:
            ev = m.get("event_ticker")
            sibs = poller._get("/markets", {"event_ticker": ev, "limit": 200}).get("markets", [])
            return m, sibs or [m], "exact"
    except KalshiError:
        pass

    # 2) an event ticker
    try:
        sibs = poller._get("/markets", {"event_ticker": token, "limit": 200}).get("markets", [])
        if sibs:
            return pick_target(sibs), sibs, "favourite"
    except KalshiError:
        pass

    # 3) a series ticker -> its soonest open event
    events = poller._get("/events", {"series_ticker": token, "status": "open",
                                     "limit": 50}).get("events", []) or []
    if not events:
        raise ValueError(f"{token} is not a market, event or open series on Kalshi")
    ev = sorted(events, key=lambda e: e.get("event_ticker") or "")[-1]
    sibs = poller._get("/markets", {"event_ticker": ev["event_ticker"],
                                    "limit": 200}).get("markets", []) or []
    if not sibs:
        raise ValueError(f"series {token} has an open event with no markets")
    return pick_target(sibs), sibs, "favourite"


def pick_target(markets: list[dict]) -> dict:
    """The favourite. Stated in the confirmation so a wrong pick is obvious."""
    return max(markets, key=lambda m: D(m.get("yes_bid_dollars")))


def derive_candidates(siblings: list[dict]) -> list[str]:
    """Candidate names for the news feed, from the sibling markets' subtitles.

    Ordered by price so the real contenders lead, and capped -- a 16-name OR
    query returns noise, and the tail of a primary board is people polling at a
    tenth of a cent.
    """
    rows = []
    for m in siblings:
        name = (m.get("yes_sub_title") or "").strip()
        if not name or name.lower() in ("yes", "no"):
            continue
        rows.append((D(m.get("yes_bid_dollars")), name))
    rows.sort(reverse=True, key=lambda r: r[0])

    out, seen = [], set()
    for price, name in rows:
        if name.lower() in seen:
            continue
        if price < CANDIDATE_MIN_PRICE and len(out) >= 2:
            break
        seen.add(name.lower())
        out.append(name)
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def make_race_tag(event_ticker: str, existing: set[str]) -> str:
    base = slugify(event_ticker) or "race"
    tag = base
    n = 2
    while tag in existing:
        tag = f"{base}-{n}"
        n += 1
    return tag


def build_block(target: dict, siblings: list[dict], race_tag: str, market_id: str,
                candidates: list[str], resolution_date: str | None,
                pm_condition: str | None, clip: int) -> str:
    """The watchlist.yaml block, as TEXT.

    Deliberately assembled as a string rather than by re-dumping parsed YAML:
    watchlist.yaml carries the Kalshi series map and a lot of hard-won trap
    documentation in comments, and a yaml.safe_load/safe_dump round-trip would
    silently delete every one of them.
    """
    # Every string scalar goes through json.dumps. YAML 1.2 is a superset of
    # JSON, so a JSON-quoted string is always valid YAML, and this is the only
    # thing that survives a candidate named  Robert "Bob" Smith  -- naive
    # quoting produces  - "Robert "Bob" Smith"  and corrupts the whole file,
    # which would blind the desk rather than merely add a bad row.
    q = json.dumps
    title = (target.get("title") or "")[:110]
    subtitle = target.get("yes_sub_title") or ""
    lines = [
        "",
        f"# --- added by the add-race workflow from {target.get('ticker')}",
        f"# event {target.get('event_ticker')} carried {len(siblings)} markets;"
        f" candidates below are its top {len(candidates)} by price.",
        f"- id: {market_id}",
        f"  race_tag: {race_tag}",
        f"  label: {q((subtitle or race_tag)[:40])}",
        f"  market_title: {q(title)}",
        f"  candidate: {q(subtitle)}",
        "  candidates:",
    ]
    lines += [f"    - {q(c)}" for c in candidates]
    kw = sorted({c.split()[-1] for c in candidates if len(c.split()[-1]) > 3})[:6]
    lines.append("  keywords: [" + ", ".join(q(k) for k in kw) + "]")
    lines.append(f"  kalshi_ticker: {target.get('ticker')}")
    lines.append(f'  polymarket_condition_id: {json.dumps(pm_condition) if pm_condition else "null"}')

    if resolution_date:
        lines.append(f"  resolution_date: {resolution_date}")
    else:
        lines.append("  # NEEDS A HUMAN: Kalshi's close_time is a far-future placeholder on")
        lines.append("  # political boards, so it is not a resolution date and was not used.")
        lines.append("  # Set this, then flip active: true.")
        lines.append("  resolution_date: null")

    lines.append("  rules_diff: null")
    lines.append(f"  clip_size: {clip}")
    lines.append(f"  active: {'true' if resolution_date else 'false'}")
    return "\n".join(lines) + "\n"


PRIMER_STUB = """# Primer: {title}

> **STATUS: NOT YET WRITTEN.** Stub created by the add-race workflow on request.
> The analyst drafts the real primer on its next run and puts it in the brief's
> "proposed primer edits" section for one-word approval. Nothing below is fact.

- **Race tag:** `{tag}`
- **Kalshi ticker:** `{ticker}`
- **Resolves:** {resolves}
- **Our position:** none recorded at creation
- **Last updated:** never

## The situation in a paragraph
## Who's running
## How this election works
## Why we're in the trade
## Bull case
## Bear case
## Correlated legs
## Sources for this race
"""


def notify(topic: str, title: str, body: str, priority: str = "3") -> None:
    if not topic:
        print(f"[no NTFY_TOPIC] {title}\n{body}")
        return
    try:
        httpx.post(f"https://ntfy.sh/{topic}", content=body.encode("utf-8"),
                   headers={"Title": title.encode("utf-8"), "Priority": priority,
                            "Tags": "heavy_plus_sign"}, timeout=20)
    except httpx.HTTPError as e:
        print(f"ntfy failed: {e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description="Add a race to the desk watchlist")
    ap.add_argument("--input", required=True, help="Kalshi market/event/series ticker, or a kalshi.com URL")
    ap.add_argument("--polymarket-condition", default="", help="optional 0x… condition id")
    ap.add_argument("--resolution-date", default="", help="YYYY-MM-DD; without it the race is staged inactive")
    ap.add_argument("--clip-size", type=int, default=150)
    ap.add_argument("--dry-run", action="store_true", help="print what would change, write nothing")
    args = ap.parse_args()

    topic = os.environ.get("NTFY_TOPIC", "")
    http = httpx.Client(timeout=30, follow_redirects=True,
                        headers={"User-Agent": "prediction-desk/1.0 (read-only)"})

    try:
        token = parse_input(args.input)
        target, siblings, how = resolve(KalshiPoller(http), token)
    except (ValueError, KalshiError) as e:
        notify(topic, "Add race FAILED", f"{args.input}\n\n{e}", "4")
        print(f"error: {e}", file=sys.stderr)
        return 1

    pm = (args.polymarket_condition or "").strip() or None
    if pm and not re.fullmatch(r"0x[0-9a-fA-F]{64}", pm):
        msg = f"polymarket condition id looks wrong: {pm!r} (expected 0x + 64 hex)"
        notify(topic, "Add race FAILED", msg, "4")
        print(f"error: {msg}", file=sys.stderr)
        return 1

    rd = (args.resolution_date or "").strip() or None
    if rd and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rd):
        msg = f"resolution date must be YYYY-MM-DD, got {rd!r}"
        notify(topic, "Add race FAILED", msg, "4")
        print(f"error: {msg}", file=sys.stderr)
        return 1

    gist = Gist(os.environ.get("GIST_ID", ""), os.environ.get("GIST_TOKEN", ""), client=http)
    try:
        raw = gist.read(WATCHLIST) or ""
        existing = yaml.safe_load(raw) or []
    except GistError as e:
        notify(topic, "Add race FAILED", f"cannot read the watchlist: {e}", "4")
        return 1

    ticker = target.get("ticker")
    if any((r.get("kalshi_ticker") or "").upper() == (ticker or "").upper() for r in existing):
        msg = f"{ticker} is already on the watchlist -- nothing to do."
        notify(topic, "Add race: already tracked", msg)
        print(msg)
        return 0

    candidates = derive_candidates(siblings)
    race_tag = make_race_tag(target.get("event_ticker") or ticker,
                             {r.get("race_tag") for r in existing})
    market_id = f"{race_tag}-{slugify(target.get('yes_sub_title') or 'yes')}"[:60]
    block = build_block(target, siblings, race_tag, market_id, candidates,
                        rd, pm, args.clip_size)

    new_watchlist = raw.rstrip() + "\n" + block
    # Never write YAML we cannot read back.
    try:
        parsed = yaml.safe_load(new_watchlist)
        assert isinstance(parsed, list) and len(parsed) == len(existing) + 1
    except Exception as e:
        notify(topic, "Add race FAILED", f"generated block did not parse: {e}", "4")
        print(f"error: generated YAML invalid: {e}", file=sys.stderr)
        return 1

    primer_name = f"primer-{race_tag}.md"
    primer = PRIMER_STUB.format(title=target.get("title") or race_tag, tag=race_tag,
                                ticker=ticker, resolves=rd or "UNSET -- see watchlist")

    try:
        idx = json.loads(gist.read(INDEX) or "{}")
    except json.JSONDecodeError:
        idx = {}
    idx.setdefault("races", []).append(
        {"race_tag": race_tag, "file": primer_name,
         "resolution_date": rd, "title": target.get("title")})

    summary = (
        f"{target.get('title')}\n"
        f"ticker    {ticker}\n"
        f"race_tag  {race_tag}\n"
        f"clip      {args.clip_size}\n"
        + (f"tracking  {target.get('yes_sub_title')} (exactly the ticker you gave; "
           f"{len(siblings)} markets on this board)\n"
           if how == "exact" else
           f"tracking  {target.get('yes_sub_title')} -- the FAVOURITE, picked from "
           f"{len(siblings)} markets on this board because you gave an event or "
           f"series rather than a market. If you meant a different one, edit "
           f"kalshi_ticker in the block.\n")
        + f"candidates {', '.join(candidates) or '(none derived)'}\n"
        + (f"resolves  {rd} -- ACTIVE\n"
           if rd else
           "resolves  NOT SET -- staged as active:false. Kalshi's close_time is a\n"
           "          placeholder, not a resolution date, so it was not guessed.\n"
           "          Set resolution_date and flip active:true to start alerting.\n")
        + (f"polymarket {pm}\n" if pm else "")
    )

    if args.dry_run:
        print(summary)
        print("--- watchlist block ---")
        print(block)
        return 0

    try:
        gist.write({
            WATCHLIST: new_watchlist,
            primer_name: primer,
            INDEX: json.dumps(idx, indent=1),
        })
    except GistError as e:
        notify(topic, "Add race FAILED", f"could not write the gist: {e}", "4")
        print(f"error: {e}", file=sys.stderr)
        return 1

    notify(topic,
           f"Race added: {race_tag}" + ("" if rd else " (needs a date)"),
           summary + "\nNext poll picks it up. Primer stub created; the analyst "
                     "will propose the real primer on its next run.",
           "3" if rd else "4")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
