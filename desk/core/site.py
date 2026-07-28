"""Build the GitHub Pages mirror that the cloud analyst actually reads.

Why this exists
---------------
The scheduled-task sandbox that runs the analyst cannot reach
gist.githubusercontent.com at all -- the request is refused -- and its fallback
route renders the gist's *web* page, which truncates large files. On the first
cloud run snapshot.json died partway through the WA-05 block, so whales,
Polymarket data, alerts and catalysts never reached the analyst, and
watchlist.yaml, universe.json and sources.yaml never loaded at all.

So the read path moves to GitHub Pages, which is plain static hosting the
sandbox can fetch. The gist stays exactly as it was for internal state and
config -- the token model is unchanged, and nothing here can write to a gist.

Obscurity, not security
-----------------------
Files are published under /d/<random32>/, an unguessable prefix, with
robots.txt Disallow and noindex on every page. This is the same bargain the
secret gist already makes: unlisted, not access-controlled. The repo is public,
so treat the prefix as the only thing standing between this data and a reader,
and rotate it (PAGES_PREFIX secret) if it ever leaks.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Files the analyst is told to fetch. Everything here is mirrored verbatim from
# the gist except snapshot.json/universe.json, which are written fresh.
ANALYST_FILES = (
    "watchlist.yaml",
    "sources.yaml",
    "primers-index.json",
    "brief-format-v2.md",
    "positions-ledger.json",
    "DATA-QUALITY.md",
)

NOINDEX = '<meta name="robots" content="noindex, nofollow">'


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build(root: str, prefix: str, snapshot: dict, universe: list | None,
          universe_stats: dict | None, gist, brief_pack: dict) -> list[str]:
    """Materialise the Pages site under ``root``. Returns the paths written."""
    if not prefix:
        raise ValueError("PAGES_PREFIX is required; refusing to publish at a guessable path")

    site = Path(root)
    d = site / "d" / prefix
    written: list[str] = []

    def put(name: str, content: str) -> None:
        _write(d / name, content)
        written.append(f"d/{prefix}/{name}")

    # Order matters for the analyst's own fetch order, not for correctness.
    put("brief-pack.json", json.dumps(brief_pack, indent=1))
    put("snapshot.json", json.dumps(snapshot, indent=1))

    if universe is not None:
        put("universe.json", json.dumps({
            "generated_at": snapshot.get("generated_at"),
            "generated_at_pt": snapshot.get("generated_at_pt"),
            "count": len(universe),
            "sweep_stats": universe_stats or {},
            "note": ("Polymarket rows are the INTERNATIONAL book (reference only). "
                     "Kalshi rows are executable. SCOPE: open political markets that "
                     "traded in the last 24h."),
            "markets": universe,
        }, indent=1))
    else:
        # The universe is only swept on the runs nearest the two brief times, but
        # Pages is republished wholesale every run -- so without this the file
        # 404s for the analyst on all the other runs. Carry the gist's last good
        # copy forward instead of dropping it.
        try:
            carried = gist.read("universe.json")
        except Exception as e:
            log.error("could not carry universe.json forward: %s", e)
            carried = None
        if carried:
            put("universe.json", carried)
        else:
            log.warning("no universe.json to carry forward; the analyst will 404 on it")

    # Mirror the static config straight off the gist so there is one source of
    # truth and no chance of the two drifting.
    for name in ANALYST_FILES:
        try:
            content = gist.read(name)
        except Exception as e:  # a mirror failure must not kill the poll
            log.error("could not mirror %s: %s", name, e)
            continue
        if content is None:
            log.warning("mirror: %s not present in the gist", name)
            continue
        put(name, content)

    # Every primer, plus the index that names them.
    for name, content in (gist._load() or {}).items():
        if name.startswith("primer-") and name.endswith(".md"):
            put(name, content)

    # An index page, so a stray visitor to the prefix sees something honest
    # rather than a 404 that invites poking.
    put("index.html",
        f"<!doctype html><html><head>{NOINDEX}"
        "<title>desk</title></head><body>"
        "<p>Private data mirror. Nothing here is an offer, a quote, or advice.</p>"
        f"<p>Generated {snapshot.get('generated_at_pt')}</p>"
        "</body></html>")

    # Root: refuse every crawler, and give the site root no links at all.
    _write(site / "robots.txt", "User-agent: *\nDisallow: /\n")
    written.append("robots.txt")
    _write(site / "index.html",
           f"<!doctype html><html><head>{NOINDEX}<title>.</title></head>"
           "<body></body></html>")
    written.append("index.html")
    # Pages serves .nojekyll-aware; without it, files starting with _ are dropped.
    _write(site / ".nojekyll", "")
    written.append(".nojekyll")

    return written


# --------------------------------------------------------------- brief pack

MAX_PACK_BYTES = 100_000


def build_brief_pack(snapshot: dict, max_bytes: int = MAX_PACK_BYTES) -> dict:
    """A compact, ordered digest of the snapshot for a truncation-prone reader.

    Sections are emitted in decreasing order of how badly the brief needs them,
    so that if the transport truncates, what survives is what matters most:

        1. scoreboard   -- quotes and executable clip prices
        2. positions    -- the ledger marked to market
        3. whales       -- tracked-wallet changes
        4. alerts       -- what fired since the last brief
        5. catalysts    -- what is coming
        6. feeds        -- headlines, trimmed last

    The first cloud run lost everything after the WA-05 block, which is exactly
    the whales/Polymarket/alerts tail. Putting the cheap, high-value sections
    first means a truncated pack still produces a usable brief.
    """
    def market_row(m: dict) -> dict:
        k = (m.get("venue_data") or {}).get("kalshi") or {}
        p = (m.get("venue_data") or {}).get("polymarket") or {}
        ex = m.get("executable") or {}
        buy, sell = ex.get("buy_clip_vwap") or {}, ex.get("sell_clip_vwap") or {}
        row = {
            "id": m.get("id"),
            "race": m.get("race_tag"),
            "label": m.get("label"),
            "resolves": m.get("resolution_date"),
            "clip": m.get("clip_size"),
            "kalshi": {"bid": k.get("bid"), "ask": k.get("ask"), "mid": k.get("mid")},
            "exec": {
                "buy_vwap": buy.get("vwap"), "buy_net": buy.get("net"),
                "sell_vwap": sell.get("vwap"), "sell_net": sell.get("net"),
                "thin": ex.get("thin"),
            },
            "vol_24h": m.get("volume_24h"),
            "d_poll": m.get("delta_since_last_poll"),
            "d_brief": m.get("delta_since_last_brief"),
        }
        if p:
            row["pm_intl_ref"] = {"bid": p.get("bid"), "ask": p.get("ask"),
                                  "mid": p.get("mid"), "executable": False}
        if m.get("rules_diff"):
            row["rules_diff"] = m["rules_diff"]
        return row

    pack = {
        "generated_at": snapshot.get("generated_at"),
        "generated_at_pt": snapshot.get("generated_at_pt"),
        "generated_at_et": snapshot.get("generated_at_et"),
        "schema": "brief-pack/1",
        "read_me": (
            "Compact digest of snapshot.json for readers that truncate. Sections are "
            "ordered most-important-first: scoreboard, positions, whales, alerts, "
            "catalysts, feeds. Kalshi is the only executable venue; any polymarket "
            "figure is the INTERNATIONAL book, reference only. Prices are decimal "
            "dollars."
        ),
        "errors": snapshot.get("errors") or [],
        "scoreboard": [market_row(m) for m in snapshot.get("markets") or []],
        "positions": {
            "marked": (snapshot.get("positions") or {}).get("marked_pnl") or [],
            "pnl_price_basis": (snapshot.get("positions") or {}).get("pnl_price_basis"),
        },
        "cross_venue": snapshot.get("cross_venue") or [],
        "whales": [
            {"alias": w.get("alias"), "changes": (w.get("changes_24h") or [])[:6]}
            for w in snapshot.get("whales") or [] if w.get("changes_24h")
        ],
        "alerts_since_last_brief": [
            {"ts": a.get("ts"), "trigger": a.get("trigger"), "title": a.get("title")}
            for a in snapshot.get("alerts_since_last_brief") or []
        ],
        "catalysts_next_14d": snapshot.get("catalysts_next_14d") or [],
    }

    # Feeds go last and absorb the whole size budget, because they are the only
    # unbounded section. Trim by count until the pack fits.
    feeds = [
        {"src": f.get("source"), "race": f.get("race_tag"), "ts": f.get("ts"),
         "title": f.get("title"), "kw": f.get("keywords")}
        for f in snapshot.get("feeds_36h") or []
    ]
    for limit in (120, 80, 50, 30, 15, 5, 0):
        pack["feeds_recent"] = feeds[:limit]
        pack["feeds_note"] = (
            f"{len(feeds)} items in the last 36h; {min(limit, len(feeds))} carried here. "
            "Full list is in snapshot.json."
        )
        if len(json.dumps(pack)) <= max_bytes:
            break

    return pack
