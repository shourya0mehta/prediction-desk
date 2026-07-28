"""Polymarket pollers: Gamma metadata, CLOB depth, Data API positions.

CRITICAL VENUE NOTE (spec 4, re-verified at build time 2026-07-28)
------------------------------------------------------------------
The owner trades **Polymarket US** (QCX LLC, CFTC-regulated, iOS-app-only). The
APIs reachable from here belong to the **separate international exchange**. The
two books diverge materially -- measured at build time on the watchlist:

    market            Kalshi        PM-intl      owner's PM-US (ledger)
    MN-Gov Lindell    0.61/0.62     0.635        0.53
    MI-Gov James      0.91/0.929    0.935        0.89

A 10-cent gap on MN. So every price this module produces is tagged
``intl_reference`` and must never be presented as executable. It is polled as a
leading indicator -- it is the deeper, older book carrying the tracked sharps --
never as a quote the owner can hit.

The decoy wallet 0x9c2dfba5885f5602b9339f8e4ee862ea8537350b is an empty
international-site shell created by a desktop login. It is refused explicitly
below so it can never re-enter the config by copy-paste.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Iterable

import httpx

from ..core.books import D, normalise_clob

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

# Never poll this address. It is the empty shell account auto-created when the
# owner logged into the international site with the same Google account.
DECOY_WALLET = "0x9c2dfba5885f5602b9339f8e4ee862ea8537350b"

PRICE_BASIS = "intl_reference"


class PolymarketError(RuntimeError):
    pass


class PolymarketPoller:
    def __init__(self, client: httpx.Client):
        self.client = client

    def _get(self, url: str, params: dict | None = None):
        try:
            r = self.client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise PolymarketError(f"{url} -> HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise PolymarketError(f"{url} -> {type(e).__name__}: {e}") from e
        except ValueError as e:
            raise PolymarketError(f"{url} -> bad JSON: {e}") from e

    # ----------------------------------------------------------------- gamma
    def market_by_condition(self, condition_id: str) -> dict | None:
        rows = self._get(f"{GAMMA}/markets", {"condition_ids": condition_id})
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    def event_markets(self, slug: str) -> list[dict]:
        events = self._get(f"{GAMMA}/events", {"slug": slug})
        out = []
        for ev in events or []:
            out.extend(ev.get("markets", []) or [])
        return out

    def open_political_markets(self, tag_slug: str = "politics", limit: int = 500) -> list[dict]:
        """Universe sweep over Gamma. Filters out the phantom rows described
        below before they reach the analyst."""
        rows = self._get(f"{GAMMA}/markets", {
            "closed": "false", "active": "true", "limit": limit,
            "order": "volume24hr", "ascending": "false", "tag": tag_slug,
        })
        return [summarise_for_universe(m) for m in (rows or []) if is_real_market(m)]

    # ------------------------------------------------------------------ clob
    def book(self, token_id: str) -> dict:
        return self._get(f"{CLOB}/book", {"token_id": token_id})

    # -------------------------------------------------------------- data api
    def positions(self, wallet: str, limit: int = 200) -> list[dict]:
        w = (wallet or "").strip().lower()
        if not w:
            return []
        if w == DECOY_WALLET.lower():
            raise PolymarketError(
                "refusing to poll the known decoy wallet 0x9c2d...350b -- it is an "
                "empty international shell, not the owner's account"
            )
        rows = self._get(f"{DATA}/positions", {
            "user": w, "limit": limit, "sortBy": "CURRENT", "sortDirection": "DESC",
        })
        return rows if isinstance(rows, list) else []


# ------------------------------------------------------------------ shaping

def is_real_market(m: dict) -> bool:
    """Reject Gamma's placeholder rows.

    The MI-Gov GOP event alone carries ~25 markets titled "Will Candidate D/E/F
    ... win", all with ``active: false`` and no ``outcomePrices``. They are
    scaffolding for candidates who may never file, and letting them into
    universe.json would hand the analyst two dozen fake races to grep.
    """
    if not m.get("active") or m.get("closed"):
        return False
    prices = m.get("outcomePrices")
    if not prices:
        return False
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except ValueError:
            return False
    return bool(prices)


def _first_price(m: dict) -> Decimal | None:
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except ValueError:
            return None
    if prices:
        return D(prices[0])
    return None


def summarise_for_universe(m: dict) -> dict:
    px = _first_price(m)
    return {
        "venue": "polymarket-intl",
        "id": m.get("conditionId"),
        "title": (m.get("question") or "")[:160],
        "close_date": m.get("endDate"),
        "mid": str(px) if px is not None else None,
        "volume_24h": str(D(m.get("volume24hr"))),
        "url": f"https://polymarket.com/market/{m.get('slug','')}",
        "price_basis": PRICE_BASIS,
    }


def token_ids(market: dict) -> list[str]:
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return list(raw or [])


def quote(market: dict, book_json: dict | None, clip) -> dict:
    """venue_data.polymarket block. Reference only -- no executable net price.

    We deliberately do NOT attach fee-adjusted numbers here. The fees that would
    apply are Polymarket US's, but the book is the international one; combining
    them would manufacture a price that exists on neither venue.
    """
    from ..core.books import walk

    book = normalise_clob(book_json or {})
    buy, sell = walk(book.asks, clip), walk(book.bids, clip)
    return {
        "condition_id": market.get("conditionId"),
        "question": market.get("question"),
        "price_basis": PRICE_BASIS,
        "executable": False,
        "bid": str(book.bid) if book.bid is not None else None,
        "ask": str(book.ask) if book.ask is not None else None,
        "mid": str(book.mid) if book.mid is not None else None,
        "gamma_price": str(_first_price(market)) if _first_price(market) is not None else None,
        "volume_24h": str(D(market.get("volume24hr"))),
        "book_top5": {
            "bids": [[str(p), str(s)] for p, s in book.bids.levels[:5]],
            "asks": [[str(p), str(s)] for p, s in book.asks.levels[:5]],
        },
        "clip_walk": {"buy": buy.as_dict(), "sell": sell.as_dict()},
        "note": "international book; owner trades Polymarket US -- check the app before acting",
    }


POLITICAL_HINT = (
    "primary", "senate", "governor", "nominee", "election", "president", "house",
    "congress", "midterm", "mayor", "caucus", "republican", "democrat",
)


def is_political(title: str) -> bool:
    t = (title or "").lower()
    return any(h in t for h in POLITICAL_HINT)


def summarise_whale_positions(rows: Iterable[dict], watch_condition_ids: set[str]) -> list[dict]:
    """Filter a wallet's positions to political / watchlist markets."""
    out = []
    for p in rows or []:
        title = p.get("title") or ""
        cond = p.get("conditionId")
        if not (is_political(title) or (cond and cond in watch_condition_ids)):
            continue
        out.append({
            "condition_id": cond,
            "title": title[:120],
            "outcome": p.get("outcome"),
            "size": str(D(p.get("size"))),
            "avg_price": str(D(p.get("avgPrice"))),
            "current_value": str(D(p.get("currentValue"))),
            "on_watchlist": bool(cond and cond in watch_condition_ids),
        })
    return out
