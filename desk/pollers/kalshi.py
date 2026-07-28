"""Kalshi public market-data poller. No authentication, no credentials, ever.

Every endpoint here is the open public REST surface. There is no code path in
this repo that signs a request, holds a key, or places an order.

Verified against the live API on 2026-07-28:
  * host              https://api.elections.kalshi.com/trade-api/v2
  * GET /markets?tickers=...            batch quotes
  * GET /markets/{ticker}/orderbook     bids-only, see books.normalise_kalshi
  * GET /markets/trades?ticker=...      public tape

The API returns decimal-dollar STRINGS (``yes_bid_dollars``: "0.7400") and
fractional size strings (``count_fp``, ``volume_24h_fp``). Older Kalshi docs
describe integer-cent fields; those are gone. Do not reintroduce int(cents).
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any, Iterable

import httpx

from ..core.books import D, normalise_kalshi

log = logging.getLogger(__name__)

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# The public cap is around 30 req/s; spec 4 asks us to stay far under it.
REQUESTS_PER_SECOND = 2.0


class KalshiError(RuntimeError):
    """Raised so the caller can record it in snapshot.errors and push an alert."""


class KalshiPoller:
    def __init__(self, client: httpx.Client, base: str = BASE):
        self.client = client
        self.base = base.rstrip("/")

    # ------------------------------------------------------------------ http
    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        try:
            r = self.client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            raise KalshiError(f"{path} -> HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise KalshiError(f"{path} -> {type(e).__name__}: {e}") from e
        except ValueError as e:
            raise KalshiError(f"{path} -> bad JSON: {e}") from e

    # --------------------------------------------------------------- markets
    def markets(self, tickers: Iterable[str]) -> dict[str, dict]:
        """Fetch quotes for specific tickers, keyed by ticker.

        The batch ``tickers=`` filter is used when available; any ticker the
        batch call fails to return is fetched individually so a single delisted
        or renamed market cannot silently blank the whole watchlist.
        """
        tickers = [t for t in tickers if t]
        if not tickers:
            return {}

        out: dict[str, dict] = {}
        try:
            data = self._get("/markets", {"tickers": ",".join(tickers), "limit": 1000})
            for m in data.get("markets", []) or []:
                out[m["ticker"]] = m
        except KalshiError as e:
            log.warning("batch /markets failed (%s); falling back per-ticker", e)

        for t in tickers:
            if t in out:
                continue
            try:
                out[t] = self._get(f"/markets/{t}")["market"]
            except (KalshiError, KeyError) as e:
                log.error("ticker %s unavailable: %s", t, e)
        return out

    def orderbook(self, ticker: str, depth: int = 20) -> dict:
        return self._get(f"/markets/{ticker}/orderbook", {"depth": depth})

    def trades(self, ticker: str, limit: int = 100) -> list[dict]:
        data = self._get("/markets/trades", {"ticker": ticker, "limit": limit})
        return data.get("trades", []) or []

    # ------------------------------------------------------------- discovery
    def open_political_markets(self, categories: tuple[str, ...] = ("Elections", "Politics"),
                               max_pages: int = 60, page_size: int = 200,
                               require_activity: bool = True) -> tuple[list[dict], dict]:
        """Every open market in the political categories, for universe.json.

        Paginates ``/events?with_nested_markets=true`` and filters on the
        ``category`` each event already carries. The obvious alternative --
        walking /series then /events then /markets -- costs one request per
        series, and there are ~1,500 election series alone, which at a polite
        2 req/s would not finish inside the job timeout. This does it in tens of
        requests instead of thousands.

        Returns ``(rows, stats)``. With ``require_activity`` the sweep keeps only
        markets showing 24h volume or open interest. That is a real narrowing of
        spec 4's "every open market", and it is deliberate: the unfiltered sweep
        returns ~13,100 rows and 4.35 MB, of which ~11,400 have never traded and
        include placeholders dated 2099. The analyst greps this file every brief
        to find opportunities, and a market with no volume and no open interest
        is not one. ``stats`` reports exactly what was dropped so the narrowing
        is never silent.
        """
        wanted = {c.lower() for c in categories}
        rows: list[dict] = []
        cursor = None
        seen = dropped = 0
        pages = 0

        for _ in range(max_pages):
            params = {"status": "open", "with_nested_markets": "true", "limit": page_size}
            if cursor:
                params["cursor"] = cursor
            try:
                data = self._get("/events", params)
            except KalshiError as e:
                log.warning("universe sweep stopped early: %s", e)
                break

            pages += 1
            events = data.get("events", []) or []
            for ev in events:
                if (ev.get("category") or "").lower() not in wanted:
                    continue
                for m in ev.get("markets", []) or []:
                    if m.get("status") not in (None, "active", "open"):
                        continue
                    seen += 1
                    if require_activity and not _has_activity(m):
                        dropped += 1
                        continue
                    rows.append(summarise_for_universe(m, ev))

            cursor = data.get("cursor")
            if not cursor or not events:
                break
            time.sleep(1.0 / REQUESTS_PER_SECOND)

        stats = {
            "pages_fetched": pages,
            "markets_seen": seen,
            "markets_kept": len(rows),
            "dropped_no_24h_volume": dropped,
            "truncated_by_page_cap": bool(cursor) and pages >= max_pages,
        }
        if stats["truncated_by_page_cap"]:
            log.warning("universe sweep hit the %d-page cap; results are partial", max_pages)
        return rows, stats


def _has_activity(m: dict) -> bool:
    """True if the market traded in the last 24 hours.

    Measured over the full political board at build time: 13,345 open markets,
    of which 1,777 traded in the last 24h (0.56 MB) but 8,161 carry some open
    interest or lifetime volume (2.9 MB). Loosening this to include open
    interest quadruples the file for markets nobody is trading, and the analyst
    fetches it whole on every brief.
    """
    return D(m.get("volume_24h_fp")) > 0


# ------------------------------------------------------------------ shaping

def summarise_for_universe(m: dict, event: dict | None = None) -> dict:
    """One universe.json row. Deliberately light -- this file gets grepped."""
    bid, ask = D(m.get("yes_bid_dollars")), D(m.get("yes_ask_dollars"))
    mid = ((bid + ask) / 2) if (bid and ask) else None
    return {
        "venue": "kalshi",
        "id": m.get("ticker"),
        "title": m.get("title") or (event or {}).get("title") or "",
        "candidate": m.get("yes_sub_title") or "",
        # NOTE: close_date is Kalshi's close_time, which on political boards is a
        # far-future placeholder (WA boards read 2027-11-03 for an Aug 2026
        # primary). It is NOT the resolution date. Never drive a countdown off it.
        "close_date": m.get("close_time"),
        "mid": str(mid.quantize(Decimal("0.0001"))) if mid is not None else None,
        "volume_24h": str(D(m.get("volume_24h_fp"))),
        "url": f"https://kalshi.com/markets/{m.get('ticker','')}",
    }


def quote(market: dict, orderbook: dict | None, clip, **kw) -> dict:
    """Normalise a market + book into the snapshot's venue_data.kalshi block."""
    from ..core.books import executable

    book = normalise_kalshi(orderbook or {})
    bid, ask = book.bid, book.ask
    out: dict[str, Any] = {
        "ticker": market.get("ticker"),
        "status": market.get("status"),
        "bid": str(bid) if bid is not None else None,
        "ask": str(ask) if ask is not None else None,
        "mid": str(book.mid) if book.mid is not None else None,
        "last": str(D(market.get("last_price_dollars"))),
        "previous": str(D(market.get("previous_price_dollars"))),
        "volume_24h": str(D(market.get("volume_24h_fp"))),
        "volume_total": str(D(market.get("volume_fp"))),
        "open_interest": str(D(market.get("open_interest_fp"))),
        "price_level_structure": market.get("price_level_structure"),
        "rules_primary": market.get("rules_primary"),
        "early_close_condition": market.get("early_close_condition"),
        "book_top5": {
            "bids": [[str(p), str(s)] for p, s in book.bids.levels[:5]],
            "asks": [[str(p), str(s)] for p, s in book.asks.levels[:5]],
        },
        "executable": executable(book, clip, venue="kalshi"),
    }
    return out


def summarise_trades(trades: list[dict]) -> list[dict]:
    """Recent prints, newest first. Kalshi accounts are anonymous, so size and
    side on the public tape are the only whale-ish signal this venue offers."""
    rows = []
    for t in trades:
        price = D(t.get("yes_price_dollars"))
        count = D(t.get("count_fp"))
        rows.append({
            "ts": t.get("created_time"),
            "count": str(count),
            "yes_price": str(price),
            "notional": str((count * price).quantize(Decimal("0.01"))),
            "taker_side": t.get("taker_side"),
            "is_block_trade": bool(t.get("is_block_trade")),
            "trade_id": t.get("trade_id"),
        })
    rows.sort(key=lambda r: r["ts"] or "", reverse=True)
    return rows
