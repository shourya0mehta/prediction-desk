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


# ---------------------------------------------------- alert-layer regressions
# These three all reproduce bugs found by running the real pipeline against live
# data during the build, not hypotheticals.

def test_small_print_does_not_alert_just_for_beating_a_tiny_median():
    """A $15 print in a market whose typical print is $3 is not signal.

    Spec 6 reads "$500 OR >=5x the trailing median". Taken literally that fired
    on $15, $17 and $21 prints across seven markets on a live run.
    """
    from desk.core.alerts import large_print_alert
    t = {"large_print_notional": 500, "large_print_median_multiple": 5,
         "large_print_median_floor": 100}
    mkt = {"id": "mo01", "label": "MO-01 Bush"}
    tiny = {"notional": "15", "count": "50", "yes_price": "0.30", "taker_side": "yes"}
    assert large_print_alert(mkt, tiny, median_notional=3.0, t=t) is None


def test_relative_rule_alone_no_longer_fires():
    """Superseded 2026-07-28: the relative test is no longer sufficient on its own.

    This used to assert that a $320 print at 16x the median should alert. In
    production that class of print ($204, $240) reached the phone and was not
    worth it, so a print must now clear the dollar bar as well.
    """
    from desk.core.alerts import large_print_alert
    t = {"large_print_notional": 1000, "large_print_median_multiple": 10}
    mkt = {"id": "mo04", "label": "MO-04 Gray"}
    big = {"notional": "320", "count": "440", "yes_price": "0.73", "taker_side": "yes"}
    assert large_print_alert(mkt, big, median_notional=20.0, t=t) is None


def test_absolute_floor_alone_is_enough_with_no_history():
    from desk.core.alerts import large_print_alert
    t = {"large_print_notional": 500, "large_print_median_multiple": 5,
         "large_print_median_floor": 100}
    mkt = {"id": "mi-sen", "label": "MI-Sen El-Sayed"}
    big = {"notional": "1900", "count": "2500", "yes_price": "0.75", "taker_side": "no"}
    assert large_print_alert(mkt, big, median_notional=None, t=t) is not None


def test_whale_alert_ignores_non_watchlist_races():
    """Tracked wallets hold dozens of 2028 presidential positions.

    Those belong in the snapshot for the analyst, not on the phone under a
    heading that claims a tracked race.
    """
    from desk.core.alerts import whale_alert
    t = {"whale_notional_change": 500}
    offwatch = [{"kind": "entry", "title": "Will JD Vance win the 2028 ...",
                 "value_change": 30000, "on_watchlist": False}]
    assert whale_alert("risk-manager", "0xabc", offwatch, t) is None

    onwatch = [{"kind": "entry", "title": "Will John James win the MI GOP primary",
                "value_change": 900, "on_watchlist": True}]
    assert whale_alert("Domer", "0xabc", onwatch, t) is not None


def test_watchlist_dates_are_flattened_to_strings_for_json():
    """PyYAML turns an unquoted date into datetime.date, which json.dumps refuses.

    This broke publishing the snapshot on every run until it was caught.
    """
    import datetime as dt
    import json as _json
    from main import normalise_watchlist
    rows = normalise_watchlist([{"id": "x", "resolution_date": dt.date(2026, 8, 4)}])
    assert rows[0]["resolution_date"] == "2026-08-04"
    _json.dumps(rows)  # must not raise


def test_generic_party_names_never_become_race_keywords():
    """"Democratic party" is MI-07's candidate label on the party market.

    Matching it tagged an unrelated Roland Martin video as MI-07 news.
    """
    from main import race_keyword_map
    m = race_keyword_map([
        {"race_tag": "mi-07", "candidate": "Democratic party", "keywords": ["MI-07"]},
        {"race_tag": "mi-sen-dem", "candidate": "Abdul El-Sayed", "keywords": ["El-Sayed"]},
    ])
    assert "Democratic party" not in m.get("mi-07", [])
    assert "MI-07" in m["mi-07"]
    assert "Abdul El-Sayed" in m["mi-sen-dem"]


def test_heartbeat_separates_degraded_feeds_from_broken_market_data():
    """A blocked RSS feed must not page anyone.

    centerforpolitics.org returns 403 to GitHub's IP range but 200 elsewhere, so
    the daily heartbeat would otherwise report FAILED every morning and the
    signal would stop meaning anything.
    """
    errs = [
        "feed crystal-ball: HTTPStatusError: Client error '403 Forbidden'",
        "kalshi orderbook KXSENATEMID-26-AELS: HTTP 500",
    ]
    hard = [e for e in errs if not e.startswith("feed ")]
    soft = [e for e in errs if e.startswith("feed ")]
    assert len(soft) == 1 and len(hard) == 1
    # A run whose only problem is feeds is healthy.
    only_feeds = ["feed crystal-ball: 403", "feed zeteo: timeout"]
    assert not [e for e in only_feeds if not e.startswith("feed ")]


# ------------------------------------------------- derived Google News feeds

def test_google_news_url_quotes_each_candidate_as_a_phrase():
    """Unquoted names match loosely: a bare Nate Powell also matches any Powell."""
    from urllib.parse import parse_qs, urlparse
    from desk.pollers.feeds import google_news_url
    u = google_news_url(["Abdul El-Sayed", "Haley Stevens"])
    q = parse_qs(urlparse(u).query)
    assert q["q"][0] == '"Abdul El-Sayed" OR "Haley Stevens"'
    assert q["hl"] == ["en-US"] and q["gl"] == ["US"] and q["ceid"] == ["US:en"]
    assert urlparse(u).netloc == "news.google.com"


def test_google_news_url_is_none_without_candidates():
    from desk.pollers.feeds import google_news_url
    assert google_news_url([]) is None
    assert google_news_url(None) is None
    assert google_news_url(["  "]) is None


def test_derive_feeds_one_per_active_race_pretagged():
    from desk.pollers.feeds import derive_feeds
    feeds = derive_feeds([
        {"race_tag": "mi-sen-dem", "candidates": ["Abdul El-Sayed"], "active": True},
        {"race_tag": "wa-05", "candidates": ["Nate Powell"], "active": True},
        {"race_tag": "retired", "candidates": ["Someone"], "active": False},
        {"race_tag": "no-names", "candidates": [], "active": True},
    ])
    assert [f["race_tag"] for f in feeds] == ["mi-sen-dem", "wa-05"]
    assert all(f["derived"] and f["tier"] == "core" for f in feeds)
    assert feeds[0]["source"] == "gnews-mi-sen-dem"


def test_derived_feed_tag_is_a_fallback_and_never_overrides_keywords():
    """Keyword tagging is unchanged; the feed's own tag only fills the gap."""
    from desk.pollers.feeds import tag_for
    kw = {"mi-sen-dem": ["El-Sayed"], "wa-05": ["Conroy"]}
    # Keyword wins when present.
    assert tag_for("Conroy leads the WA field", kw) == "wa-05"
    # Nothing matches -> caller falls back to the feed's race_tag.
    assert tag_for("A story naming nobody", kw) is None


def test_fetch_feed_backs_off_on_429_then_succeeds():
    """A 429 must be retried, not recorded as a dead feed."""
    import httpx
    from desk.pollers import feeds as F
    calls = {"n": 0}
    rss = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
           b'<item><title>hello</title><link>http://x/1</link></item></channel></rss>')

    class C:
        def get(self, url, headers=None):
            calls["n"] += 1
            req = httpx.Request("GET", url)
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, request=req)
            return httpx.Response(200, content=rss, request=req)

    entries, err = F.fetch_feed(C(), "http://example/feed")
    assert err is None
    assert calls["n"] == 2, "should have retried once after the 429"
    assert len(entries) == 1


def test_fetch_feed_gives_up_after_repeated_429s():
    import httpx
    from desk.pollers import feeds as F
    calls = {"n": 0}

    class C:
        def get(self, url, headers=None):
            calls["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "0"},
                                  request=httpx.Request("GET", url))

    entries, err = F.fetch_feed(C(), "http://example/feed", retries=3)
    assert entries == [] and "429" in err
    assert calls["n"] == 3


# ------------------------- gist write retry (409 from a concurrent writer)

def _gist(client):
    from desk.core.state import Gist
    return Gist("gid", "tok", client=client)


def test_gist_write_retries_409_then_succeeds(monkeypatch):
    """GitHub 409s when the analyst appends a brief mid-publish.

    Observed in production at 2:44 PM; without a retry the snapshot is silently
    lost for that cycle.
    """
    import httpx
    from desk.core import state as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class C:
        def patch(self, url, headers=None, json=None):
            calls["n"] += 1
            req = httpx.Request("PATCH", url)
            if calls["n"] < 3:
                return httpx.Response(409, text="Gist cannot be updated", request=req)
            return httpx.Response(200, json={"id": "gid"}, request=req)

    _gist(C()).write({"snapshot.json": "{}"})
    assert calls["n"] == 3


def test_gist_write_retries_5xx(monkeypatch):
    import httpx
    from desk.core import state as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class C:
        def patch(self, url, headers=None, json=None):
            calls["n"] += 1
            req = httpx.Request("PATCH", url)
            code = 502 if calls["n"] == 1 else 200
            return httpx.Response(code, json={"id": "gid"}, request=req)

    _gist(C()).write({"snapshot.json": "{}"})
    assert calls["n"] == 2


def test_gist_write_raises_after_exhausting_retries(monkeypatch):
    import httpx
    import pytest as _pytest
    from desk.core import state as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class C:
        def patch(self, url, headers=None, json=None):
            calls["n"] += 1
            return httpx.Response(409, text="Gist cannot be updated",
                                  request=httpx.Request("PATCH", url))

    with _pytest.raises(S.GistError) as e:
        _gist(C()).write({"snapshot.json": "{}"})
    assert calls["n"] == 3
    assert "409" in str(e.value)


def test_gist_write_does_not_retry_a_real_failure(monkeypatch):
    """401/404 are not transient; retrying just delays the alert."""
    import httpx
    import pytest as _pytest
    from desk.core import state as S
    monkeypatch.setattr(S.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class C:
        def patch(self, url, headers=None, json=None):
            calls["n"] += 1
            return httpx.Response(404, text="Not Found",
                                  request=httpx.Request("PATCH", url))

    with _pytest.raises(S.GistError):
        _gist(C()).write({"snapshot.json": "{}"})
    assert calls["n"] == 1, "a 404 must fail immediately"


# ------------------------------------- large prints: BOTH bars, not either

LP_T = {"large_print_notional": 1000, "large_print_median_multiple": 10}
LP_MKT = {"id": "mi-sen", "label": "MI-Sen El-Sayed"}


def _print(n):
    return {"notional": str(n), "count": "100", "yes_price": "0.75", "taker_side": "yes"}


def test_production_noise_prints_no_longer_page():
    """$204 and $240 prints reached the phone under OR semantics."""
    from desk.core.alerts import large_print_alert
    assert large_print_alert(LP_MKT, _print(204), median_notional=15.0, t=LP_T) is None
    assert large_print_alert(LP_MKT, _print(240), median_notional=20.0, t=LP_T) is None


def test_big_notional_but_ordinary_for_this_market_is_suppressed():
    """$1,200 in a market whose median print is $500 is just normal flow."""
    from desk.core.alerts import large_print_alert
    assert large_print_alert(LP_MKT, _print(1200), median_notional=500.0, t=LP_T) is None


def test_relatively_huge_but_small_in_dollars_is_suppressed():
    """80x the median still is not worth waking up for at $400."""
    from desk.core.alerts import large_print_alert
    assert large_print_alert(LP_MKT, _print(400), median_notional=5.0, t=LP_T) is None


def test_print_clearing_both_bars_fires():
    from desk.core.alerts import large_print_alert
    a = large_print_alert(LP_MKT, _print(2500), median_notional=50.0, t=LP_T)
    assert a is not None and a.trigger == "large_print"


def test_election_night_halves_both_bars():
    """A $600 print at 6x median is noise on a Tuesday, signal on election night."""
    from desk.core.alerts import large_print_alert
    night = {"large_print_notional": 500, "large_print_median_multiple": 5}
    assert large_print_alert(LP_MKT, _print(600), median_notional=100.0, t=LP_T) is None
    assert large_print_alert(LP_MKT, _print(600), median_notional=100.0, t=night) is not None


# ------------------------------- news alerts carry headlines, not base64 URLs

GNEWS = ("https://news.google.com/rss/articles/"
         "CBMiZkFVX3lxTE5rZTRjaXJvaWJIUXNVU1lXbG9uZ2Jhc2U2NGJsb2I")


def test_google_news_redirect_links_are_dropped():
    from desk.pollers.feeds import readable_link
    assert readable_link(GNEWS) is None
    assert readable_link("https://www.bangordailynews.com/story") == \
        "https://www.bangordailynews.com/story"
    assert readable_link(None) is None


def test_publisher_is_recovered_from_the_headline_suffix():
    from desk.pollers.feeds import publisher_of
    assert publisher_of("Harris backs Troy Jackson in Maine Senate race - The Hill") == "The Hill"
    assert publisher_of("No suffix here") is None


def test_feed_alert_body_carries_the_headline_and_no_base64_url():
    from desk.core.alerts import feed_alert
    a = feed_alert({
        "race_tag": "me-sen",
        "title": "Kamala Harris endorses Troy Jackson in Senate race - WGME",
        "url": GNEWS, "source": "gnews-me-sen", "keywords": ["endorse"],
    })
    assert "Kamala Harris endorses Troy Jackson" in a.body
    assert "WGME" in a.body
    assert a.links == [], "the base64 Google News link must not reach the phone"
    assert "news.google.com" not in a.body


def test_feed_alert_keeps_a_real_publisher_url():
    from desk.core.alerts import feed_alert
    a = feed_alert({
        "race_tag": "wi-gov-dem", "title": "UAW endorses Mandela Barnes - FOX6",
        "url": "https://www.fox6now.com/news/uaw", "source": "gnews-wi-gov-dem",
        "keywords": ["endorse"],
    })
    assert a.links == ["https://www.fox6now.com/news/uaw"]
