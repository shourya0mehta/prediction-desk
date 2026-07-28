"""Fixture-driven tests for book normalisation, clip walks and fee math.

Worked example (the fixture below is the real shape returned by
``GET /trade-api/v2/markets/KXSENATEMID-26-AELS/orderbook?depth=5`` on
2026-07-28, trimmed to five levels a side):

    yes_dollars (YES bids, ascending)   no_dollars (NO bids, ascending)
        0.70 x 1213.60                      0.21 x 10242.00
        0.71 x 17337.00                     0.22 x   374.00
        0.72 x 16570.00                     0.23 x  8820.82
        0.73 x  9914.00                     0.24 x  5644.43
        0.74 x  5884.20                     0.25 x    89.00

Kalshi publishes bids only. The NO bids imply YES asks at (1 - price):
best NO bid 0.25 -> best YES ask 0.75, which matched the market object's
``yes_ask_dollars`` field exactly when captured. So top of book is 0.74 / 0.75.

Buying a 150-contract clip walks the YES asks best-first: 89 contracts at 0.75,
then the remaining 61 at 0.76 (from the 0.24 NO bid). That is
(89*0.75 + 61*0.76) / 150 = (66.75 + 46.36) / 150 = 113.11 / 150 = 0.754066...,
which quantises to **0.7541**. The taker fee on that clip is
ceil(0.07 * 150 * 0.7541 * 0.2459 * 100) / 100 = ceil(194.68) / 100 = **$1.95**,
i.e. 1.3 cents a contract, so the all-in buy is about **0.7671**.
"""

from __future__ import annotations

import math
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk.core.books import (  # noqa: E402
    Book,
    Ladder,
    breakeven,
    executable,
    kalshi_fee,
    normalise_clob,
    normalise_kalshi,
    pm_us_fee,
    walk,
)

KALSHI_FIXTURE = {
    "orderbook_fp": {
        "yes_dollars": [
            ["0.7000", "1213.60"],
            ["0.7100", "17337.00"],
            ["0.7200", "16570.00"],
            ["0.7300", "9914.00"],
            ["0.7400", "5884.20"],
        ],
        "no_dollars": [
            ["0.2100", "10242.00"],
            ["0.2200", "374.00"],
            ["0.2300", "8820.82"],
            ["0.2400", "5644.43"],
            ["0.2500", "89.00"],
        ],
    }
}


# ---------------------------------------------------------------- normalisation

def test_kalshi_bids_only_normalises_to_two_sided_ladder():
    book = normalise_kalshi(KALSHI_FIXTURE)
    # Best YES bid is the highest yes_dollars level.
    assert book.bid == Decimal("0.74")
    # Best YES ask is implied by the highest NO bid: 1 - 0.25.
    assert book.ask == Decimal("0.75")
    assert book.mid == Decimal("0.7450")


def test_kalshi_ask_ladder_is_ascending_and_sized_from_no_bids():
    book = normalise_kalshi(KALSHI_FIXTURE)
    assert book.asks.levels[:3] == [
        (Decimal("0.75"), Decimal("89.00")),
        (Decimal("0.76"), Decimal("5644.43")),
        (Decimal("0.77"), Decimal("8820.82")),
    ]
    prices = [p for p, _ in book.asks.levels]
    assert prices == sorted(prices)


def test_kalshi_bid_ladder_is_descending():
    book = normalise_kalshi(KALSHI_FIXTURE)
    prices = [p for p, _ in book.bids.levels]
    assert prices == sorted(prices, reverse=True)


def test_zero_size_and_out_of_range_levels_are_dropped():
    book = normalise_kalshi(
        {"orderbook_fp": {"yes_dollars": [["0.50", "0"], ["0.00", "10"], ["0.60", "5"]],
                          "no_dollars": [["0.30", "7"]]}}
    )
    assert book.bids.levels == [(Decimal("0.60"), Decimal("5"))]


def test_empty_book_is_handled():
    book = normalise_kalshi({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    assert book.bid is None and book.ask is None and book.mid is None
    w = walk(book.asks, 150)
    assert w.filled == 0 and w.vwap is None and w.thin is True


def test_clob_normalises_two_sided():
    book = normalise_clob(
        {"bids": [{"price": "0.62", "size": "100"}, {"price": "0.63", "size": "50"}],
         "asks": [{"price": "0.66", "size": "80"}, {"price": "0.65", "size": "40"}]}
    )
    assert book.bid == Decimal("0.63")
    assert book.ask == Decimal("0.65")


# ------------------------------------------------------------------ clip walks

def test_buy_clip_vwap_matches_worked_example():
    book = normalise_kalshi(KALSHI_FIXTURE)
    w = walk(book.asks, 150)
    assert w.complete is True
    assert w.filled == Decimal("150")
    assert w.levels_used == 2
    assert w.vwap == Decimal("0.7541")  # (89*0.75 + 61*0.76) / 150
    assert w.notional == Decimal("113.11")


def test_sell_clip_vwap_walks_bids_best_first():
    book = normalise_kalshi(KALSHI_FIXTURE)
    w = walk(book.bids, 150)
    # 5884.20 available at 0.74 absorbs the whole clip.
    assert w.complete is True
    assert w.vwap == Decimal("0.74")
    assert w.levels_used == 1


def test_partial_fill_is_flagged_incomplete_and_thin():
    thin_book = Book(bids=Ladder([]), asks=Ladder([(Decimal("0.50"), Decimal("10"))]))
    w = walk(thin_book.asks, 150)
    assert w.complete is False
    assert w.filled == Decimal("10")
    assert w.thin is True


def test_thin_flag_trips_when_vwap_drifts_past_five_cents():
    # Top of book 0.50 but only 1 contract there; the rest fills at 0.60, so the
    # VWAP lands ~9.9c above top -> thin, even though the clip fills completely.
    book = Book(bids=Ladder([]), asks=Ladder([
        (Decimal("0.50"), Decimal("1")),
        (Decimal("0.60"), Decimal("149")),
    ]))
    w = walk(book.asks, 150)
    assert w.complete is True
    assert w.thin is True


def test_deep_book_within_five_cents_is_not_thin():
    book = normalise_kalshi(KALSHI_FIXTURE)
    assert walk(book.asks, 150).thin is False


# ------------------------------------------------------------------- Kalshi fees

def test_kalshi_fee_is_order_level_ceiling_not_per_contract():
    # The worked example: 150 contracts at 0.7541.
    fee = kalshi_fee(150, Decimal("0.7541"))
    assert fee == Decimal("1.95")
    # Per-contract rounding would charge ceil(0.07*0.7541*0.2459*100)/100 = 0.02
    # a contract = $3.00, over 50% more. Guard against regressing to that.
    assert fee < Decimal("3.00")


def test_kalshi_fee_peaks_at_fifty_cents():
    # 0.07 * 100 * 0.5 * 0.5 = 1.75 exactly, no rounding needed.
    assert kalshi_fee(100, Decimal("0.50")) == Decimal("1.75")


def test_kalshi_fee_rounds_up_to_the_next_cent():
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> ceil to 0.02.
    assert kalshi_fee(1, Decimal("0.50")) == Decimal("0.02")


def test_kalshi_fee_reproduces_owner_wa05_fill():
    """The WA-05 fill is the tightest real-world anchor we have.

    Ledger: 140 contracts of NO at 71.62c, all-in cost $102.27 against a notional
    of 140 * 0.7162 = $100.268, implying a fee of $2.00.
    """
    assert kalshi_fee(140, Decimal("0.7162")) == Decimal("2.00")


def test_kalshi_maker_is_quarter_of_taker():
    taker = kalshi_fee(1000, Decimal("0.40"))
    maker = kalshi_fee(1000, Decimal("0.40"), maker=True)
    assert taker == Decimal("16.80")
    assert maker == Decimal("4.20")


def test_kalshi_fee_zero_at_the_boundaries():
    assert kalshi_fee(100, Decimal("0")) == Decimal("0")
    assert kalshi_fee(100, Decimal("1")) == Decimal("0")
    assert kalshi_fee(0, Decimal("0.5")) == Decimal("0")


# ------------------------------------------------------------ Polymarket US fees

def test_pm_us_taker_cap_is_one_fifty_per_hundred_at_fifty_cents():
    # docs.polymarket.us/fees states the max taker fee is $1.50 per 100 lot.
    assert pm_us_fee(100, Decimal("0.50")) == Decimal("1.50")


def test_pm_us_maker_is_a_rebate_and_hits_its_documented_max():
    # Max maker rebate is -$0.31 per 100 lot at 50c.
    rebate = pm_us_fee(100, Decimal("0.50"), maker=True)
    assert rebate == Decimal("-0.31")
    assert rebate < 0, "makers are PAID on Polymarket US; the sign must stay negative"


def test_pm_us_uses_bankers_rounding_not_half_up():
    # 0.06 * 25 * 0.5 * 0.5 = 0.375 -> half-to-even gives 0.38 (8 is even).
    assert pm_us_fee(25, Decimal("0.50")) == Decimal("0.38")
    # 0.06 * 75 * 0.5 * 0.5 = 1.125 -> half-to-even gives 1.12, NOT 1.13.
    assert pm_us_fee(75, Decimal("0.50")) == Decimal("1.12")


def test_venue_fee_signs_differ_for_makers():
    """The comparison layer depends on this: Kalshi charges makers, PM-US pays."""
    assert kalshi_fee(100, Decimal("0.5"), maker=True) > 0
    assert pm_us_fee(100, Decimal("0.5"), maker=True) < 0


def test_intl_polymarket_has_no_executable_fee_model():
    from desk.core.books import fee_for
    with pytest.raises(ValueError):
        fee_for("polymarket-intl", 100, Decimal("0.5"))


# ------------------------------------------------------------------- executable

def test_executable_fees_move_both_sides_against_you():
    book = normalise_kalshi(KALSHI_FIXTURE)
    ex = executable(book, 150, venue="kalshi")
    buy_gross = Decimal(ex["buy_clip_vwap"]["vwap"])
    buy_net = Decimal(ex["buy_clip_vwap"]["net"])
    sell_gross = Decimal(ex["sell_clip_vwap"]["vwap"])
    sell_net = Decimal(ex["sell_clip_vwap"]["net"])
    assert buy_net > buy_gross, "buying should cost more after fees"
    assert sell_net < sell_gross, "selling should net less after fees"
    assert ex["thin"] is False


def test_settlement_breakeven_reproduces_owner_wi_gov_figure():
    """The WI-Gov row logs breakeven 48.08c on a 46.34c entry over 104 contracts.

    That is entry + entry fee, with no exit fee, because the position is held to
    settlement and neither venue charges a fee to settle.
    """
    be = breakeven(Decimal("0.4634"), venue="kalshi", contracts=104)
    assert be > Decimal("0.4634")
    assert abs(be - Decimal("0.4808")) < Decimal("0.001")


def test_round_trip_breakeven_is_strictly_higher_than_settlement():
    settle = breakeven(Decimal("0.4634"), venue="kalshi", contracts=104)
    rt = breakeven(Decimal("0.4634"), venue="kalshi", contracts=104, round_trip=True)
    assert rt > settle
    # Quoting the round-trip number against a held position would overstate the
    # bar for taking profit by roughly the entry fee again.
    assert abs((rt - settle) - Decimal("0.0176")) < Decimal("0.002")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
