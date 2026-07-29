"""OpenFEC client: raised and cash-on-hand per candidate.

``/v1/candidates/totals/?q=<name>`` returns the name, receipts and cash-on-hand
in a single call, which is why it is used instead of search-then-totals. Spot
checked against the 2026-07-28 brief: El-Sayed $2.55M cash and Stevens $2.79M
cash both reproduce exactly.

Rate limits are the binding constraint, and smaller than advertised. Measured
from ``x-ratelimit-limit``: **DEMO_KEY = 10/hour, a registered key = 60/hour**
(not the 1,000 the signup page implies). Against a ~900-race board that is still
the scarce resource, so:

  * results are cached in gist state for CACHE_DAYS, since filings are periodic
    and these numbers move a few times a quarter, not hourly;
  * non-federal races are skipped before a call is spent (see GUARD 1);
  * the run stops calling the moment it is throttled and reports how many
    candidates it could not price, rather than failing or silently blanking.

Money is enrichment, never a gate: a race with no FEC data still gets a full
row, with an ``fec_status`` that says which kind of nothing it is.

Two failure modes here produce WRONG numbers rather than missing ones, and both
were caught on live data -- see GUARD 1 and GUARD 2 in ``totals``.
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

    def totals(self, name: str, office: str | None = None) -> dict:
        """{raised, cash_on_hand, candidate_id, ...} for a candidate name.

        ``office`` is the race's office as detected from its title: one of
        "senate", "house", or a non-federal value. Passing it is strongly
        recommended -- see the two guards below, both of which exist because the
        naive version produced wrong numbers on live data.
        """
        key = re.sub(r"[^a-z ]", "", (name or "").lower()).strip()
        if not key:
            return {"fec_status": "no_name"}

        # GUARD 1: the FEC covers FEDERAL candidates only. Governors, attorneys
        # general, mayors and state legislators file with state agencies, so a
        # lookup for Mike Lindell in the Minnesota governor's race returns
        # nothing -- and "nothing" reads like "no money raised" if it is not
        # labelled. Three of the owner's own positions are gubernatorial.
        if office and office not in ("senate", "house"):
            return {"fec_status": "not_federal_race",
                    "note": f"{office} candidates file with the state, not the FEC"}

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

        # GUARD 2: the q= search is fuzzy and will happily return a different
        # person. Live example: q="John James" returned "JOHNSON, JAMES MICHAEL",
        # a House candidate with $20,976,864 raised -- which, reported against
        # the Michigan governor's race, would have been a $21M fabrication
        # attached to the wrong human. Verify the surname before believing it.
        best = _pick_match(name, rows, office)
        if best is None:
            entry = {"raised": None, "cash_on_hand": None, "candidate_id": None,
                     "matched_name": None,
                     "fetched_at": datetime.now(timezone.utc).isoformat()}
            self.cache[key] = entry
            return {**entry, "fec_status": "no_confident_match",
                    "note": (f"FEC returned {len(rows)} row(s) for {name!r} but none "
                             f"matched on surname"
                             + (f" and {office} office" if office else "")
                             + "; refusing to attach another candidate's money"),
                    "rejected": [r.get("name") for r in rows[:3]]}

        mismatch = best.pop("_office_mismatch", False)
        entry = {
            "raised": _first(best, RAISED_FIELDS),
            "cash_on_hand": _first(best, CASH_FIELDS),
            "candidate_id": best.get("candidate_id"),
            "matched_name": best.get("name"),
            "office": best.get("office_full"),
            "coverage_end": best.get("coverage_end_date"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        self.cache[key] = entry
        if mismatch:
            return {**entry, "fec_status": "office_mismatch",
                    "note": (f"this is {best.get('name')}'s {best.get('office_full')} "
                             f"committee, but the race is for {office}. A sitting "
                             f"member running for another office really does have "
                             f"both -- treat as context, not as this race's money.")}
        return {**entry, "fec_status": "fetched"}

    def status(self) -> dict:
        return {
            "api_key": "DEMO_KEY (10 requests/hour)" if self.using_demo_key
                       else "registered key (60 requests/hour, measured)",
            "calls_made": self.calls,
            "throttled": self.throttled,
            "candidates_unpriced": len(self.misses),
            "unpriced_names": self.misses[:25],
            "cache_entries": len(self.cache),
            "note": ("raised is NOT cash on hand -- a candidate can have raised "
                     "millions lifetime and hold very little today. Quote cash on "
                     "hand when the question is who can still buy turnout. "
                     "fec_status 'not_federal_race' means the FEC does not cover "
                     "that office at all (governor, AG, mayor, state legislature "
                     "file with the state) -- it does NOT mean no money was raised. "
                     "'no_confident_match' means the FEC returned someone whose "
                     "surname did not match, and it was refused."),
        }


def _first(row: dict, fields: tuple[str, ...]):
    for f in fields:
        v = row.get(f)
        if v is not None:
            return v
    return None


# Kalshi writes ordinals as digits -- its MO-04 subtitle is literally
# "Hartzell Gray 3rd", which a naive parse reads as the surname "3rd".
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "esq",
            "1st", "2nd", "3rd", "4th", "5th"}


def surname_of(name: str) -> str:
    """Best-effort surname from either 'First Last' or FEC's 'LAST, FIRST'."""
    n = (name or "").strip()
    if not n:
        return ""
    if "," in n:                       # FEC style: "EL-SAYED, ABDUL"
        return n.split(",")[0].strip().lower()
    parts = [p for p in re.split(r"\s+", n.lower())
             if p.strip(".") not in SUFFIXES and p]
    return parts[-1] if parts else ""


def _surnames_agree(a: str, b: str) -> bool:
    """Surnames match, allowing for compound names.

    The FEC keeps the whole compound -- "GLUESENKAMP PEREZ, MARIE" -- while
    Kalshi writes "Marie Gluesenkamp Perez", whose last token alone is "perez".
    A strict equality check refused three genuine matches on the first live run.
    Containment accepts those while still refusing "james" against "johnson".
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return a in b or b in a


def _pick_match(query: str, rows: list[dict], office: str | None) -> dict | None:
    """The best row whose surname matches, preferring the right office.

    Returns None rather than a guess. A wrong candidate's money is worse than no
    money: an absent figure prompts a lookup, a confident wrong one gets quoted
    in a brief and sized against.

    An office mismatch does NOT discard the row -- a sitting House member running
    for the Senate really does have a House committee, and that money is context
    rather than an error. It is returned flagged, so the analyst decides.
    """
    want = surname_of(query)
    if not want:
        return None
    # A bare first name cannot be verified: "Wayne" and "Virginia" each matched
    # three unrelated filers on the live run.
    if len(re.split(r"\s+", (query or "").strip())) < 2:
        return None

    fallback = None
    for r in rows:
        if not _surnames_agree(want, surname_of(r.get("name") or "")):
            continue
        if not office:
            return r
        got = (r.get("office_full") or r.get("office") or "").lower()
        if (office == "senate" and "senate" in got) or \
           (office == "house" and "house" in got):
            return r
        fallback = fallback or dict(r, _office_mismatch=True)
    return fallback
