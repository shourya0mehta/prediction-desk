"""Order-book normalisation, executable-price walks, and venue fee math.

Everything here works in Decimal dollars, never float and never integer cents.
Kalshi markets carry a ``price_level_structure`` that is sometimes ``linear_cent``
but is ``tapered_deci_cent`` on the bigger political boards (0.001 ticks below
$0.10 and above $0.90) -- verified 2026-07-28 against KXSENATEMID-26-AELS -- so an
int-cents representation would silently truncate real levels.

Kalshi's public orderbook returns **bids only for both sides** (``orderbook_fp``
with ``yes_dollars`` / ``no_dollars``, ascending price, best bid last). YES asks
are implied from NO bids: a NO bid at q is a YES ask at 1-q for the same size.
Verified against the live API: best no_bid 0.2500 -> yes_ask 0.7500, matching the
market object's ``yes_ask_dollars``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Iterable, Sequence

ONE = Decimal("1")
CENT = Decimal("0.01")
ZERO = Decimal("0")

# Kalshi taker fee coefficient, and the maker fee as a fraction of taker.
KALSHI_THETA = Decimal("0.07")
KALSHI_MAKER_FRACTION = Decimal("0.25")

# Polymarket US, per docs.polymarket.us/fees effective 2026-07-01 (re-verified at
# build time 2026-07-28). Makers are PAID; the sign flip matters when comparing
# venues, because Kalshi charges makers.
PM_US_TAKER_THETA = Decimal("0.06")
PM_US_MAKER_THETA = Decimal("-0.0125")


def D(value) -> Decimal:
    """Coerce an API value (usually a decimal string like "0.7400") to Decimal."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    return Decimal(str(value))


# --------------------------------------------------------------------------
# Fees
# --------------------------------------------------------------------------

def kalshi_fee(contracts, price, maker: bool = False) -> Decimal:
    """Kalshi fee in dollars for an order of ``contracts`` at ``price``.

    fee = ceil(0.07 * C * P * (1-P) * 100) / 100  -- rounded UP to the next cent
    at the ORDER level, not per contract. Maker is 25% of taker, same rounding.

    The order-level rounding was verified at build time against the owner's own
    fill costs: it reproduces the WA-05 fill exactly ($2.00) and lands within a
    few cents on the rest, whereas per-contract rounding overshoots every row by
    30-40%. See README "Fee verification".
    """
    c, p = D(contracts), D(price)
    if c <= 0 or p <= 0 or p >= ONE:
        return ZERO
    raw = KALSHI_THETA * c * p * (ONE - p)
    if maker:
        raw *= KALSHI_MAKER_FRACTION
    # ceil to the next cent
    return (Decimal(math.ceil(raw * 100)) / 100).quantize(CENT)


def pm_us_fee(contracts, price, maker: bool = False) -> Decimal:
    """Polymarket US fee in dollars. Negative when maker (it is a rebate).

    taker = 0.06 * C * p * (1-p); maker = -0.0125 * C * p * (1-p).
    Rounded to the nearest cent with banker's rounding (round half to even).
    """
    c, p = D(contracts), D(price)
    if c <= 0 or p <= 0 or p >= ONE:
        return ZERO
    theta = PM_US_MAKER_THETA if maker else PM_US_TAKER_THETA
    raw = theta * c * p * (ONE - p)
    return raw.quantize(CENT, rounding=ROUND_HALF_EVEN)


def fee_for(venue: str, contracts, price, maker: bool = False) -> Decimal:
    if venue == "kalshi":
        return kalshi_fee(contracts, price, maker)
    if venue in ("pm-us", "polymarket-us"):
        return pm_us_fee(contracts, price, maker)
    # International Polymarket is reference-only; we never quote an executable
    # fee for it, so returning zero here would be a lie. Callers must not ask.
    raise ValueError(f"no executable fee model for venue {venue!r}")


# --------------------------------------------------------------------------
# Book normalisation
# --------------------------------------------------------------------------

@dataclass
class Ladder:
    """A one-sided price ladder, always ordered best-price-first for the taker."""

    levels: list[tuple[Decimal, Decimal]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.levels)

    @property
    def top(self) -> Decimal | None:
        return self.levels[0][0] if self.levels else None

    @property
    def depth(self) -> Decimal:
        return sum((s for _, s in self.levels), ZERO)


@dataclass
class Book:
    """A normalised two-sided book for one outcome token (the YES side)."""

    bids: Ladder  # what you can SELL into, best (highest) first
    asks: Ladder  # what you can BUY from, best (lowest) first

    @property
    def bid(self) -> Decimal | None:
        return self.bids.top

    @property
    def ask(self) -> Decimal | None:
        return self.asks.top

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return ((self.bid + self.ask) / 2).quantize(Decimal("0.0001"))


def _pairs(raw: Iterable) -> list[tuple[Decimal, Decimal]]:
    out = []
    for lvl in raw or []:
        if isinstance(lvl, dict):
            price, size = lvl.get("price"), lvl.get("size")
        else:
            price, size = lvl[0], lvl[1]
        p, s = D(price), D(size)
        if s > 0 and ZERO < p < ONE:
            out.append((p, s))
    return out


def normalise_kalshi(orderbook: dict) -> Book:
    """Turn Kalshi's bids-only ``orderbook_fp`` into a standard YES book.

    ``yes_dollars`` are genuine YES bids. ``no_dollars`` are NO bids, each of
    which is an equal-size YES ask at (1 - price).
    """
    ob = (orderbook or {}).get("orderbook_fp") or (orderbook or {}).get("orderbook") or {}
    yes_bids = _pairs(ob.get("yes_dollars") or ob.get("yes"))
    no_bids = _pairs(ob.get("no_dollars") or ob.get("no"))

    # Best YES bid is the highest price.
    yes_bids.sort(key=lambda x: x[0], reverse=True)
    # Each NO bid at q implies a YES ask at 1-q; best ask is the lowest price,
    # which comes from the highest NO bid.
    yes_asks = sorted(((ONE - q, s) for q, s in no_bids), key=lambda x: x[0])

    return Book(bids=Ladder(yes_bids), asks=Ladder(yes_asks))


def normalise_clob(book: dict) -> Book:
    """Normalise a Polymarket CLOB ``/book`` response for one token.

    The CLOB returns real two-sided depth: ``bids`` and ``asks``, each
    ``{price, size}``. Polymarket returns bids ascending, so we re-sort rather
    than trusting order.
    """
    bids = _pairs((book or {}).get("bids"))
    asks = _pairs((book or {}).get("asks"))
    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])
    return Book(bids=Ladder(bids), asks=Ladder(asks))


# --------------------------------------------------------------------------
# Executable prices
# --------------------------------------------------------------------------

@dataclass
class Walk:
    """The result of walking a ladder to fill a clip."""

    filled: Decimal
    requested: Decimal
    notional: Decimal
    vwap: Decimal | None
    top: Decimal | None
    complete: bool
    thin: bool
    levels_used: int

    def as_dict(self) -> dict:
        return {
            "vwap": str(self.vwap) if self.vwap is not None else None,
            "top": str(self.top) if self.top is not None else None,
            "filled": str(self.filled),
            "requested": str(self.requested),
            "complete": self.complete,
            "thin": self.thin,
            "levels_used": self.levels_used,
        }


# A book is "thin" if filling the clip drags the average more than this far from
# the top of book (spec 5).
THIN_SLIPPAGE = Decimal("0.05")


def walk(ladder: Ladder, clip, thin_slippage: Decimal = THIN_SLIPPAGE) -> Walk:
    """Walk ``ladder`` to fill ``clip`` contracts, returning VWAP and thinness.

    ``thin`` is True when the clip cannot be filled at all, or when the VWAP ends
    up more than ``thin_slippage`` away from the top of book.
    """
    clip = D(clip)
    remaining, notional, used = clip, ZERO, 0
    for price, size in ladder.levels:
        if remaining <= 0:
            break
        take = size if size < remaining else remaining
        notional += take * price
        remaining -= take
        used += 1

    filled = clip - remaining
    complete = remaining <= 0
    top = ladder.top
    vwap = (notional / filled).quantize(Decimal("0.0001")) if filled > 0 else None

    thin = not complete
    if vwap is not None and top is not None and abs(vwap - top) > thin_slippage:
        thin = True

    return Walk(
        filled=filled,
        requested=clip,
        notional=notional.quantize(CENT),
        vwap=vwap,
        top=top,
        complete=complete,
        thin=thin,
        levels_used=used,
    )


def executable(book: Book, clip, venue: str = "kalshi") -> dict:
    """Executable buy/sell prices for a clip, gross and net of taker fees.

    ``buy_clip_vwap`` is what it costs per contract to BUY the clip (walking the
    asks); ``sell_clip_vwap`` is what you net per contract to SELL it (walking
    the bids). ``*_net`` fold in the venue's taker fee per contract, so buys get
    more expensive and sells net less -- both move against you, which is the
    point.
    """
    buy, sell = walk(book.asks, clip), walk(book.bids, clip)
    out = {
        "buy_clip_vwap": buy.as_dict(),
        "sell_clip_vwap": sell.as_dict(),
        "thin": buy.thin or sell.thin,
    }

    for key, w, sign in (("buy_clip_vwap", buy, 1), ("sell_clip_vwap", sell, -1)):
        if w.vwap is None or w.filled <= 0:
            out[key]["net"] = None
            continue
        try:
            fee = fee_for(venue, w.filled, w.vwap)
        except ValueError:
            out[key]["net"] = None
            continue
        per_contract = fee / w.filled
        out[key]["fee_total"] = str(fee)
        out[key]["net"] = str((w.vwap + sign * per_contract).quantize(Decimal("0.0001")))

    return out


def breakeven(entry_price, venue: str = "kalshi", contracts=1, round_trip: bool = False) -> Decimal | None:
    """All-in cost per contract -- the price the market must reach to break even.

    By default this is the **settlement** breakeven: entry price plus the entry
    fee only. Neither venue charges a trading fee when a contract settles, and
    the standing rule on nearly every position in this book is hold-to-
    settlement, so this is the number that actually decides "edge intact" versus
    "trim zone". It reproduces the owner's own logged figures -- a 46.34c entry
    on 104 WI-Gov contracts gives 48.08c, exactly as recorded in the ledger.

    Pass ``round_trip=True`` for the sell-before-settlement case, which also
    charges an exit taker fee. That number is roughly 1.8c higher on a 50c
    contract, so quoting it against a position being held would overstate the
    bar for taking profit.
    """
    p = D(entry_price)
    if not (ZERO < p < ONE):
        return None
    try:
        entry_fee = fee_for(venue, contracts, p) / D(contracts)
    except ValueError:
        return None
    total = p + entry_fee
    if round_trip:
        # Approximate the exit fee at the entry price; it is within a cent for
        # any realistic move and avoids solving a fixed point for a display-only
        # number.
        total += entry_fee
    return total.quantize(Decimal("0.0001"))
