"""OpenFEC client: raised and cash-on-hand per candidate.

``/v1/candidates/totals/?q=<name>`` returns the name, receipts and cash-on-hand
in a single call, which is why it is used instead of search-then-totals. Spot
checked against the 2026-07-28 brief: El-Sayed $2.55M cash and Stevens $2.79M
cash both reproduce exactly.

Rate limits are the binding constraint. **DEMO_KEY allows 10 requests per hour**
(measured: ``x-ratelimit-limit: 10``), which cannot cover a full board. So:

  * results are cached in gist state for CACHE_DAYS, since filings are periodic
    and these numbers move a few times a quarter, not hourly;
  * the run stops calling the moment it is throttled and reports how many
    candidates it could not price, rather than failing or silently blanking;
  * a real key (free, api.data.gov) lifts this to 1,000/hour.

Money is enrichment, never a gate: a race with no FEC data still gets a full
row, marked ``fec_status: "unavailable"``.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.open.fec.gov/v1"
CACHE_DAYS = 7

# A "raised" figure is not cash on hand -- register item 13 exists because a
# thesis leaned on a lifetime-raised number while the candidate held ~$37k.
CASH_FIELDS = ("cash_on_hand_end_period", "last_cash_on_hand_end_period")
RAISED_FIELDS = ("receipts", "last_receipts")


class FECClient:
    def __init__(self, client: httpx.Client, api_key: str = "DEMO_KEY",
                 cache: dict | None = None, cycle: int = 2026):
        self.client = client
        self.api_key = api_key or "DEMO_KEY"
        self.cache = cache if cache is not None else {}
        self.cycle = cycle
        self.throttled = False
        self.calls = 0
        self.misses: list[str] = []

    @property
    def using_demo_key(self) -> bool:
        return self.api_key == "DEMO_KEY"

    def _fresh(self, entry: dict) -> bool:
        try:
            when = datetime.fromisoformat(entry["fetched_at"])
        except (KeyError, TypeError, ValueError):
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - when < timedelta(days=CACHE_DAYS)

    def totals(self, name: str) -> dict:
        """{raised, cash_on_hand, candidate_id, source} for a candidate name."""
        key = re.sub(r"[^a-z ]", "", (name or "").lower()).strip()
        if not key:
            return {"fec_status": "no_name"}

        hit = self.cache.get(key)
        if hit and self._fresh(hit):
            return {**hit, "fec_status": "cached"}

        if self.throttled:
            self.misses.append(name)
            return {"fec_status": "rate_limited"}

        try:
            r = self.client.get(f"{BASE}/candidates/totals/", params={
                "api_key": self.api_key, "q": name, "election_year": self.cycle,
                "per_page": 5, "sort": "-receipts",
            })
            self.calls += 1
            if r.status_code == 429:
                self.throttled = True
                log.warning("FEC rate limit reached after %d call(s); "
                            "remaining candidates will be unpriced this run", self.calls)
                self.misses.append(name)
                return {"fec_status": "rate_limited"}
            r.raise_for_status()
            rows = (r.json() or {}).get("results") or []
        except httpx.HTTPError as e:
            log.info("FEC lookup failed for %r: %s", name, e)
            self.misses.append(name)
            return {"fec_status": f"error:{type(e).__name__}"}

        if not rows:
            entry = {"raised": None, "cash_on_hand": None, "candidate_id": None,
                     "fetched_at": datetime.now(timezone.utc).isoformat(),
                     "matched_name": None}
            self.cache[key] = entry
            return {**entry, "fec_status": "no_match"}

        best = rows[0]
        entry = {
            "raised": _first(best, RAISED_FIELDS),
            "cash_on_hand": _first(best, CASH_FIELDS),
            "candidate_id": best.get("candidate_id"),
            "matched_name": best.get("name"),
            "coverage_end": best.get("coverage_end_date"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        self.cache[key] = entry
        return {**entry, "fec_status": "fetched"}

    def status(self) -> dict:
        return {
            "api_key": "DEMO_KEY (10 requests/hour)" if self.using_demo_key
                       else "configured key",
            "calls_made": self.calls,
            "throttled": self.throttled,
            "candidates_unpriced": len(self.misses),
            "unpriced_names": self.misses[:25],
            "cache_entries": len(self.cache),
            "note": ("raised is NOT cash on hand -- a candidate can have raised "
                     "millions lifetime and hold very little today. Quote cash on "
                     "hand when the question is who can still buy turnout."),
        }


def _first(row: dict, fields: tuple[str, ...]):
    for f in fields:
        v = row.get(f)
        if v is not None:
            return v
    return None
