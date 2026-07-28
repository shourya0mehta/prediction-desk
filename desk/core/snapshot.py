"""snapshot.json assembly and gist publication -- the contract with Layer 2."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from .state import PT, now_utc, stamp

log = logging.getLogger(__name__)

SNAPSHOT_FILE = "snapshot.json"
UNIVERSE_FILE = "universe.json"
SCHEMA_VERSION = 2


def build(markets: list, positions: dict, cross_venue: list, whales: list,
          feeds_36h: list, alerts_since_last_brief: list, catalysts: list,
          errors: list, meta: dict | None = None) -> dict:
    ts = stamp()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": ts["utc"],
        "generated_at_pt": ts["pt"],
        "generated_at_et": ts["et"],
        "errors": errors,
        "meta": meta or {},
        "markets": markets,
        "positions": positions,
        "cross_venue": cross_venue,
        "whales": whales,
        "feeds_36h": feeds_36h,
        "alerts_since_last_brief": alerts_since_last_brief,
        "catalysts_next_14d": catalysts,
    }


REQUIRED_TOP_LEVEL = (
    "generated_at", "generated_at_pt", "errors", "markets", "positions",
    "cross_venue", "whales", "feeds_36h", "alerts_since_last_brief",
    "catalysts_next_14d",
)


def validate(snap: dict) -> list[str]:
    """Structural check against the spec 7 schema. Returns a list of problems."""
    problems = []
    for key in REQUIRED_TOP_LEVEL:
        if key not in snap:
            problems.append(f"missing top-level key: {key}")

    if not isinstance(snap.get("markets"), list):
        problems.append("markets must be a list")
    else:
        for i, m in enumerate(snap["markets"]):
            for key in ("id", "race_tag", "venue_data", "executable"):
                if key not in m:
                    problems.append(f"markets[{i}] missing {key}")
            ex = m.get("executable") or {}
            if "thin" not in ex:
                problems.append(f"markets[{i}].executable missing thin flag")

    pos = snap.get("positions") or {}
    if "ledger" not in pos or "marked_pnl" not in pos:
        problems.append("positions must carry ledger and marked_pnl")
    if not pos.get("pnl_price_basis"):
        problems.append("positions.pnl_price_basis must be stated explicitly")

    for i, cv in enumerate(snap.get("cross_venue") or []):
        if cv.get("executable") is not False:
            problems.append(f"cross_venue[{i}] must be marked executable:false")

    return problems


def catalysts_next_14d(watchlist: list, extra: list | None = None,
                       now: datetime | None = None) -> list:
    """Resolution dates and analyst-calendar entries inside the next 14 days.

    Driven by the watchlist's own ``resolution_date``. Kalshi's ``close_time``
    is deliberately ignored: on these boards it is a far-future placeholder
    (the WA markets read 2027-11-03 for an August 2026 primary), so a countdown
    built on it would never fire.
    """
    now = now or now_utc()
    horizon = now + timedelta(days=14)
    out = []
    seen = set()

    for row in watchlist or []:
        if not row.get("active", True):
            continue
        rd = row.get("resolution_date")
        if not rd:
            continue
        try:
            when = datetime.fromisoformat(str(rd)).replace(tzinfo=PT)
        except ValueError:
            continue
        if not (now <= when <= horizon):
            continue
        key = (row.get("race_tag"), rd)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "date": rd,
            "race_tag": row.get("race_tag"),
            "what": f"{row.get('race_tag')} settles ({row.get('market_title') or 'election day'})",
            "cancel_resting_orders": True,
            "hours_out": round((when - now).total_seconds() / 3600, 1),
        })

    for e in extra or []:
        out.append(e)

    out.sort(key=lambda c: c.get("date") or "")
    return out


def publish(gist, snap: dict, universe: list | None = None) -> None:
    files = {SNAPSHOT_FILE: json.dumps(snap, indent=1, sort_keys=False)}
    if universe is not None:
        files[UNIVERSE_FILE] = json.dumps({
            "generated_at": snap["generated_at"],
            "generated_at_pt": snap["generated_at_pt"],
            "count": len(universe),
            "note": ("Polymarket rows are the INTERNATIONAL book (reference only). "
                     "Kalshi rows are executable. close_date is the venue's own field "
                     "and is not a reliable resolution date on Kalshi political boards."),
            "markets": universe,
        }, indent=1)
    gist.write(files)


def age_minutes(snap: dict, now: datetime | None = None) -> float | None:
    try:
        gen = datetime.fromisoformat(snap["generated_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    return ((now or now_utc()) - gen).total_seconds() / 60
