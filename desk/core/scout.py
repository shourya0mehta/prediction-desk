"""Scout layer: find every tradeable primary/special worth a human look.

Publishes ``scout-pack.json`` to the Pages mirror once a day. The analyst reads
it in date batches and writes a full brief-format-v2 workup per race.

Full coverage, on purpose
-------------------------
Every market that passes the scope filters gets an enrichment row. There is no
top-N cap, because a cap silently decides what the owner never sees. Markets
excluded *only* by the price band appear one line each in ``appendix_band_excluded``
so the exclusion is visible and reversible rather than invisible.

The date problem
----------------
Kalshi's ``close_time`` is not a resolution date on political boards -- the WA
boards read 2027-11-03 for a primary that settles 2026-08-04. A horizon filter
built on it would hide the races closest to settling, which is exactly backwards.
So resolution dates come, in order of trust:

  1. the watchlist, when the market is already tracked
  2. STATE_PRIMARY_2026, a calendar sourced from NCSL and independently
     confirmed on three states this session
  3. the venue's close_time, flagged unreliable and used only as a last resort

Every row carries ``resolution_date_source`` so the analyst knows which it got.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .books import D
from .state import median, now_utc, trim_history

log = logging.getLogger(__name__)

# 2026 statewide primary dates (NCSL). Independently cross-checked this session:
# Washington 08-04, Michigan 08-04, Missouri 08-04, Minnesota 08-11,
# Wisconsin 08-11, Massachusetts 09-01, Maine 06-09 (Platner won that primary).
STATE_PRIMARY_2026 = {
    "AL": "2026-05-19", "AK": "2026-08-18", "AZ": "2026-07-21", "AR": "2026-03-03",
    "CA": "2026-06-02", "CO": "2026-06-30", "CT": "2026-08-11", "DE": "2026-09-15",
    "FL": "2026-08-18", "GA": "2026-05-19", "HI": "2026-08-08", "ID": "2026-05-19",
    "IL": "2026-03-17", "IN": "2026-05-05", "IA": "2026-06-02", "KS": "2026-08-04",
    "KY": "2026-05-19", "LA": "2026-05-16", "ME": "2026-06-09", "MD": "2026-06-23",
    "MA": "2026-09-01", "MI": "2026-08-04", "MN": "2026-08-11", "MS": "2026-03-10",
    "MO": "2026-08-04", "MT": "2026-06-02", "NE": "2026-05-12", "NV": "2026-06-09",
    "NH": "2026-09-08", "NJ": "2026-06-02", "NM": "2026-06-02", "NY": "2026-06-23",
    "NC": "2026-03-03", "ND": "2026-06-09", "OH": "2026-05-05", "OK": "2026-06-16",
    "OR": "2026-05-19", "PA": "2026-05-19", "RI": "2026-09-09", "SC": "2026-06-09",
    "SD": "2026-06-02", "TN": "2026-08-06", "TX": "2026-03-03", "UT": "2026-06-23",
    "VT": "2026-08-11", "VA": "2026-08-04", "WA": "2026-08-04", "WV": "2026-05-12",
    "WI": "2026-08-11", "WY": "2026-08-18",
}

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

# --------------------------------------------------------------- defaults

DEFAULTS = {
    "horizon_days": 75,
    # AMENDED price band -- two tiers only. The buy-side ASK is what you would
    # actually pay, so the band is applied to it, not to the mid.
    "band_ask_standard": 0.77,        # anything resolving beyond 7 days
    "band_ask_near": 0.85,            # within 7 days; 80-85c is the intended zone
    "band_near_days": 7,
    "stale_hours": 24,
    "wide_spread_cents": 6,
    "thin_volume_24h": 50,
    "dead_tail_min_price": 0.05,      # a tail priced at/above this...
    "dead_tail_max_raised": 25_000,   # ...with about no money behind it
    "whale_consolidation_wallets": 2,
    "whale_consolidation_usd": 2000,
    "whale_consolidation_days": 7,
    "print_consolidation_count": 3,
    "print_consolidation_hours": 72,
    "print_consolidation_min_notional": 250,
    "max_trade_lookups": 250,         # per run; overflow is reported, never silent
}


def cfg(thresholds: dict, key: str):
    return (thresholds or {}).get(key, DEFAULTS[key])


# ----------------------------------------------------------- classification

# "primary" needs electoral language, not just the word "nomination". Federal
# APPOINTMENT markets -- "Will someone be nominated for a member of the Federal
# Reserve", judiciary and commission seats -- otherwise sail straight through
# and land in a batch of primaries as if they were races. Sixteen did on the
# first live sweep.
UNAMBIGUOUS_RE = re.compile(r"\bprimary\b|\bcaucus\b|\bwho will advance\b|top.?two", re.I)
NOMINEE_RE = re.compile(r"\bnominee\b|\bnomination\b", re.I)
ELECTED_OFFICE_RE = re.compile(
    r"\bgovernor\b|\bsenate\b|\bsenator\b|\bhouse\b|\bcongress|\bmayor\b|"
    r"\battorney general\b|\bsecretary of state\b|\blieutenant governor\b|"
    r"\bdistrict\b|[A-Z]{2}-\d{2}", re.I)
APPOINTMENT_RE = re.compile(
    r"federal reserve|\bfed\b|judiciary|judicial|commission|ambassador|"
    r"cabinet|chair(man)?\b|justice|court|board of governors|"
    r"secretary of (defense|treasury|state|energy|labor|commerce)|"
    r"confirmed|confirmation|appoint", re.I)
SPECIAL_RE = re.compile(r"\bspecial\b", re.I)
GENERAL_RE = re.compile(r"\bwin the (general|20\d\d)\b|general election", re.I)
PARTY_MARKET_RE = re.compile(
    r"^(SENATEPARTY|GOVPARTY|HOUSEPARTY|SENATE|GOVERNOR)[-A-Z0-9]*$|"
    r"(be sworn in|which party|party win|party control)", re.I)


def classify(market: dict, event: dict | None = None) -> str:
    """primary | special | general | other.

    Generals are excluded from the scope but still counted, so the pack can say
    how much of the board it deliberately ignored.
    """
    blob = " ".join(filter(None, [
        market.get("title"), market.get("yes_sub_title"),
        (event or {}).get("title"), (event or {}).get("sub_title"),
        market.get("rules_primary"),
    ]))
    ticker = market.get("ticker") or ""

    # An appointment or confirmation is never an election, whatever words it
    # shares with one.
    if APPOINTMENT_RE.search(blob) and not UNAMBIGUOUS_RE.search(blob):
        return "other"

    # A party-resolved market is a general-election bet even when its subtitle
    # carries a candidate's name -- the Maine trap, register item 4.
    if PARTY_MARKET_RE.search(ticker) or re.search(r"party.*(sworn in|win)", blob, re.I):
        return "general"

    is_special = bool(SPECIAL_RE.search(blob))
    electoral = bool(UNAMBIGUOUS_RE.search(blob)) or (
        NOMINEE_RE.search(blob) and ELECTED_OFFICE_RE.search(blob)
        and detect_state(blob) is not None)

    if is_special and electoral:
        return "special"
    if electoral:
        return "primary"
    if is_special:
        return "other"
    if GENERAL_RE.search(blob):
        return "general"
    return "other"


OFFICE_PATTERNS = (
    ("senate", re.compile(r"\bsenate\b|\bsenator\b", re.I)),
    ("house", re.compile(r"\bhouse\b|\bcongress|\b[A-Z]{2}-\d{2}\b", re.I)),
    ("governor", re.compile(r"\bgovernor\b|gubernatorial", re.I)),
    ("attorney_general", re.compile(r"\battorney general\b", re.I)),
    ("secretary_of_state", re.compile(r"\bsecretary of state\b", re.I)),
    ("mayor", re.compile(r"\bmayor\b", re.I)),
)


def detect_office(text: str) -> str | None:
    """The office a race is for. Only senate/house are covered by the FEC."""
    for name, pat in OFFICE_PATTERNS:
        if pat.search(text or ""):
            return name
    return None


def detect_state(text: str) -> str | None:
    low = (text or "").lower()
    for name, code in STATE_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", low):
            return code
    m = re.search(r"\b([A-Z]{2})-\d{2}\b", text or "")
    if m and m.group(1) in STATE_PRIMARY_2026:
        return m.group(1)
    return None


def infer_resolution(market: dict, event: dict | None,
                     watchlist_by_ticker: dict) -> tuple[date | None, str]:
    """(resolution_date, source). Never silently trusts close_time."""
    tk = market.get("ticker")
    row = watchlist_by_ticker.get(tk)
    if row and row.get("resolution_date"):
        try:
            return date.fromisoformat(str(row["resolution_date"])), "watchlist"
        except ValueError:
            pass

    blob = " ".join(filter(None, [market.get("title"), market.get("yes_sub_title"),
                                  (event or {}).get("title"), tk]))
    st = detect_state(blob)
    if st and st in STATE_PRIMARY_2026 and classify(market, event) in ("primary", "special"):
        return date.fromisoformat(STATE_PRIMARY_2026[st]), f"primary_calendar_2026:{st}"

    ct = market.get("close_time")
    if ct:
        try:
            return datetime.fromisoformat(ct.replace("Z", "+00:00")).date(), \
                "venue_close_time_UNRELIABLE"
        except ValueError:
            pass
    return None, "unknown"


# ------------------------------------------------------------- price band

def band_limit(days_to_resolution: int | None, thresholds: dict) -> float:
    """The highest ask we will look at, given time to resolution.

    Two tiers only. Beyond a week the bar is 77c: paying more than that for a
    binary needs an edge that rarely survives fees. Inside a week the bar rises
    to 85c, because a favourite at 80-85c settling in days is a different
    instrument -- short-dated, high-probability, and the intended zone for that
    window rather than an accident.
    """
    near = int(cfg(thresholds, "band_near_days"))
    if days_to_resolution is not None and days_to_resolution <= near:
        return float(cfg(thresholds, "band_ask_near"))
    return float(cfg(thresholds, "band_ask_standard"))


def in_band(ask, days_to_resolution: int | None, thresholds: dict) -> bool:
    a = D(ask)
    if a <= 0 or a >= 1:
        return False
    return float(a) <= band_limit(days_to_resolution, thresholds)


# ------------------------------------------------------------- enrichment

def _days(d: date | None, today: date) -> int | None:
    return (d - today).days if d else None


def return_profile(ask) -> dict:
    """What a winning buy pays, before fees. (1-p)/p."""
    a = D(ask)
    if a <= 0 or a >= 1:
        return {"ceiling_pct": None, "per_month_pct": None}
    return {"ceiling_pct": round(float((1 - a) / a) * 100, 1), "per_month_pct": None}


def structural_tags(row: dict, field: list[dict], fec: dict) -> list[str]:
    """Pattern tags that flag a shape worth a human look."""
    tags = []
    price = D(row.get("ask") or 0)

    # Dead-money tail: priced like a real chance, funded like nobody.
    money = (fec or {}).get("raised")
    if price >= D(str(DEFAULTS["dead_tail_min_price"])) and money is not None \
            and money < DEFAULTS["dead_tail_max_raised"]:
        tags.append("dead_money_tail")

    # Lone major-party candidate against a fragmented opposition: the WA top-two
    # shape, where one party's only entrant advances almost mechanically.
    priced = [m for m in field if D(m.get("yes_bid_dollars")) > D("0.02")]
    if len(field) >= 4 and len(priced) >= 3:
        tags.append("fragmented_field")
    if row.get("is_dominant_favourite"):
        tags.append("dominant_favourite_vs_fragmented_field")

    if row.get("flags", {}).get("wide_spread") and row.get("flags", {}).get("thin_volume"):
        tags.append("illiquid")
    return tags


def whale_consolidation(condition_id: str | None, whale_history: dict,
                        thresholds: dict, now: datetime | None = None) -> dict | None:
    """>=N tracked wallets net-adding the SAME side of one market within a week.

    Two wallets independently building the same side is a different signal from
    one wallet doubling down -- it is the closest thing to a consensus this data
    can show. Still only a prompt to investigate: fills are visible only after
    the price moved, and some of these wallets are market-makers.
    """
    if not condition_id:
        return None
    now = now or now_utc()
    window = timedelta(days=int(cfg(thresholds, "whale_consolidation_days")))
    need_wallets = int(cfg(thresholds, "whale_consolidation_wallets"))
    need_usd = float(cfg(thresholds, "whale_consolidation_usd"))

    adds: dict[str, float] = {}
    for wallet, markets in (whale_history or {}).items():
        rows = (markets or {}).get(condition_id) or []
        net = 0.0
        for ts, delta in rows:
            try:
                when = datetime.fromisoformat(ts)
            except (TypeError, ValueError):
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if now - when <= window:
                net += float(delta)
        if net > 0:
            adds[wallet] = net

    total = sum(adds.values())
    if len(adds) >= need_wallets and total >= need_usd:
        return {
            "kind": "whale_consolidation",
            "wallets": sorted(adds, key=adds.get, reverse=True),
            "wallet_count": len(adds),
            "combined_usd": round(total, 2),
            "window_days": window.days,
            "caveat": ("investigate, never copy: these fills are only visible after "
                       "the price moved, and some tracked wallets are market-makers"),
        }
    return None


def print_consolidation(prints: list[dict], thresholds: dict,
                        now: datetime | None = None) -> dict | None:
    """>=N same-side qualifying Kalshi prints inside the window.

    Kalshi accounts are anonymous, so repeated same-side size on the tape is the
    only accumulation signal this venue offers.
    """
    now = now or now_utc()
    window = timedelta(hours=int(cfg(thresholds, "print_consolidation_hours")))
    need = int(cfg(thresholds, "print_consolidation_count"))
    floor = float(cfg(thresholds, "print_consolidation_min_notional"))

    sides: dict[str, list[dict]] = {}
    for p in prints or []:
        try:
            when = datetime.fromisoformat((p.get("ts") or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if now - when > window:
            continue
        if float(p.get("notional") or 0) < floor:
            continue
        sides.setdefault(p.get("taker_side") or "?", []).append(p)

    for side, rows in sides.items():
        if len(rows) >= need:
            return {
                "kind": "print_consolidation",
                "side": side,
                "count": len(rows),
                "combined_usd": round(sum(float(r["notional"]) for r in rows), 2),
                "window_hours": window.total_seconds() / 3600,
                "caveat": "anonymous tape; size and side only, no identity",
            }
    return None
