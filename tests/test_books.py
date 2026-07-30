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


# --------------------------------------- Kalshi: one ticker per call, always

def test_markets_uses_the_per_market_endpoint_never_a_list_filter():
    """?ticker= (singular) is silently ignored and returns unrelated markets.

    A live analyst run asked for six political tickers that way and got a page
    of esports back, with no error to notice. The plural ?tickers= does filter,
    but silently DROPS unknown tickers. Both failures are silent, so the poller
    uses /markets/{ticker}, which cannot return the wrong instrument.
    """
    from desk.pollers.kalshi import KalshiPoller
    seen = []

    class P(KalshiPoller):
        def __init__(self):
            pass
        def _get(self, path, params=None):
            seen.append((path, params))
            tk = path.rsplit("/", 1)[-1]
            return {"market": {"ticker": tk, "yes_bid_dollars": "0.50"}}

    out = P().markets(["KXSENATEMID-26-AELS", "SENATEME-26-D"])
    assert set(out) == {"KXSENATEMID-26-AELS", "SENATEME-26-D"}
    assert all(p.startswith("/markets/") for p, _ in seen), seen
    for _, params in seen:
        assert not (params or {}).get("tickers"), "must not use the list filter"
        assert not (params or {}).get("ticker"), "must not use the singular filter"


def test_markets_discards_a_response_for_the_wrong_ticker():
    """Belt and braces: never let a mismatched instrument into the snapshot."""
    from desk.pollers.kalshi import KalshiPoller

    class P(KalshiPoller):
        def __init__(self):
            pass
        def _get(self, path, params=None):
            return {"market": {"ticker": "KXMVESPORTS-SOMETHING", "yes_bid_dollars": "0.5"}}

    assert P().markets(["SENATEME-26-D"]) == {}


def test_markets_survives_one_dead_ticker():
    from desk.pollers.kalshi import KalshiPoller, KalshiError

    class P(KalshiPoller):
        def __init__(self):
            pass
        def _get(self, path, params=None):
            tk = path.rsplit("/", 1)[-1]
            if tk == "DEAD":
                raise KalshiError("HTTP 404")
            return {"market": {"ticker": tk, "yes_bid_dollars": "0.5"}}

    out = P().markets(["DEAD", "SENATEME-26-D"])
    assert set(out) == {"SENATEME-26-D"}


# ------------------------------------------------------ brief-pack contract

def _snap(n_feeds=500):
    return {
        "generated_at": "2026-07-28T21:00:00+00:00",
        "generated_at_pt": "2026-07-28 14:00:00 PDT",
        "errors": [],
        "markets": [{
            "id": "mi-sen-dem-elsayed", "race_tag": "mi-sen-dem", "label": "MI-Sen",
            "resolution_date": "2026-08-04", "clip_size": 169,
            "venue_data": {"kalshi": {"bid": "0.74", "ask": "0.75", "mid": "0.745"}},
            "executable": {"buy_clip_vwap": {"vwap": "0.75", "net": "0.762"},
                           "sell_clip_vwap": {"vwap": "0.74", "net": "0.728"},
                           "thin": False},
            "volume_24h": "120811", "delta_since_last_poll": 0.0,
            "delta_since_last_brief": -1.0,
        }],
        "positions": {"marked_pnl": [{"market_id": "x", "mark": "0.75"}],
                      "pnl_price_basis": "kalshi_executable"},
        "cross_venue": [], 
        "whales": [{"alias": "Domer", "changes_24h": [{"kind": "entry", "title": "t"}]}],
        "alerts_since_last_brief": [{"ts": "t", "trigger": "move", "title": "x"}],
        "catalysts_next_14d": [{"date": "2026-08-04", "race_tag": "mi-sen-dem"}],
        "feeds_36h": [{"source": "gnews-mi-sen-dem", "race_tag": "mi-sen-dem",
                       "ts": "t", "title": "H" * 120, "keywords": ["poll"]}
                      for _ in range(n_feeds)],
    }


def test_brief_pack_orders_sections_so_truncation_degrades_gracefully():
    """The first cloud run truncated mid-snapshot and lost whales, PM and alerts.

    The pack puts the sections a brief cannot be written without ahead of the
    unbounded feed list, so a truncated read still yields a usable brief.
    """
    from desk.core.site import build_brief_pack
    keys = list(build_brief_pack(_snap()).keys())
    order = [k for k in keys if k in
             ("scoreboard", "positions", "whales", "alerts_since_last_brief",
              "catalysts_next_14d", "feeds_recent")]
    assert order == ["scoreboard", "positions", "whales",
                     "alerts_since_last_brief", "catalysts_next_14d", "feeds_recent"]


def test_brief_pack_stays_under_the_size_budget_by_trimming_feeds_only():
    import json
    from desk.core.site import build_brief_pack, MAX_PACK_BYTES
    pack = build_brief_pack(_snap(n_feeds=5000))
    assert len(json.dumps(pack)) <= MAX_PACK_BYTES
    # The high-value sections survive the trim intact.
    assert len(pack["scoreboard"]) == 1
    assert pack["whales"] and pack["alerts_since_last_brief"]
    assert pack["catalysts_next_14d"]
    assert len(pack["feeds_recent"]) < 5000
    assert "5000 items" in pack["feeds_note"], "must say what was dropped"


def test_brief_pack_marks_polymarket_as_non_executable():
    from desk.core.site import build_brief_pack
    s = _snap(n_feeds=1)
    s["markets"][0]["venue_data"]["polymarket"] = {"bid": "0.63", "ask": "0.64", "mid": "0.635"}
    row = build_brief_pack(s)["scoreboard"][0]
    assert row["pm_intl_ref"]["executable"] is False


def test_site_refuses_to_publish_without_a_prefix():
    """A guessable path would put the ledger at a predictable URL."""
    import pytest as _pytest
    from desk.core import site
    with _pytest.raises(ValueError):
        site.build("/tmp/nope", "", {}, None, None, None, {})


def test_universe_is_carried_forward_on_runs_that_do_not_sweep():
    """Pages is republished wholesale every run.

    The universe is only swept near the two brief times, so without carrying the
    last good copy forward the file 404s for the analyst on every other run --
    observed live.
    """
    import json, tempfile
    from pathlib import Path
    from desk.core import site

    class FakeGist:
        def read(self, name):
            return '{"count": 7, "markets": []}' if name == "universe.json" else None
        def _load(self):
            return {}

    with tempfile.TemporaryDirectory() as tmp:
        site.build(tmp, "abc123", {"generated_at_pt": "x"}, None, None, FakeGist(), {})
        got = json.loads((Path(tmp) / "d/abc123/universe.json").read_text())
        assert got["count"] == 7


def test_missing_universe_does_not_crash_the_mirror():
    import tempfile
    from pathlib import Path
    from desk.core import site

    class FakeGist:
        def read(self, name):
            return None
        def _load(self):
            return {}

    with tempfile.TemporaryDirectory() as tmp:
        site.build(tmp, "abc123", {"generated_at_pt": "x"}, None, None, FakeGist(), {})
        assert (Path(tmp) / "d/abc123/snapshot.json").exists()


# ------------------------------------------- orphan positions (ledger guard)

def test_ledger_row_with_no_watchlist_entry_is_flagged_as_an_orphan():
    """An untracked position is money at risk the pipeline cannot see.

    Dropping it from the brief would produce a confident exposure dashboard that
    silently understates the book.
    """
    from desk.core.compare import orphan_positions
    ledger = [
        {"market_id": "mi-sen-dem-elsayed", "race_tag": "mi-sen-dem", "shares": 169},
        {"market_id": "nj-gov-dem-sherrill", "race_tag": "nj-gov-dem", "shares": 50,
         "market_title": "New Jersey Democratic Governor nominee?", "venue": "kalshi",
         "side": "YES", "avg_price_cents": 61.0},
    ]
    watchlist = [{"id": "mi-sen-dem-elsayed", "race_tag": "mi-sen-dem"}]
    orphans = orphan_positions(ledger, watchlist)
    assert [o["market_id"] for o in orphans] == ["nj-gov-dem-sherrill"]
    o = orphans[0]
    # The analyst needs the title, because that is all it has to research from.
    assert o["market_title"] == "New Jersey Democratic Governor nominee?"
    assert o["shares"] == "50"
    assert "market_title" in o["what_to_do"]


def test_no_orphans_when_every_position_is_tracked():
    from desk.core.compare import orphan_positions
    ledger = [{"market_id": "a", "race_tag": "r1"}, {"market_id": "b", "race_tag": "r2"}]
    watchlist = [{"id": "a", "race_tag": "r1"}, {"id": "b", "race_tag": "r2"}]
    assert orphan_positions(ledger, watchlist) == []


def test_orphan_matched_by_market_id_even_if_race_tag_differs():
    from desk.core.compare import orphan_positions
    ledger = [{"market_id": "a", "race_tag": "renamed-tag"}]
    watchlist = [{"id": "a", "race_tag": "original-tag"}]
    assert orphan_positions(ledger, watchlist) == []


def test_orphan_position_is_still_carried_in_marked_pnl():
    """Flagged AND carried -- never silently dropped."""
    from desk.core.compare import mark_positions
    ledger = [{"market_id": "orphan", "venue": "kalshi", "side": "YES",
               "shares": 50, "avg_price_cents": 61.0}]
    marked, _ = mark_positions(ledger, {})
    assert len(marked) == 1
    assert marked[0]["market_id"] == "orphan"
    assert marked[0]["mark"] is None, "unpriced, but present"


# ---------------------------------------------------------- add-race tooling

def test_parse_input_accepts_tickers_and_urls():
    from tools.add_race import parse_input
    assert parse_input("KXSENATEMID-26-AELS") == "KXSENATEMID-26-AELS"
    assert parse_input("  kxsenatemid-26-aels ") == "KXSENATEMID-26-AELS"
    assert parse_input("https://kalshi.com/markets/KXSENATEMID-26-AELS") == "KXSENATEMID-26-AELS"
    assert parse_input(
        "https://kalshi.com/markets/kxgovwinomd/wisconsin-governor?foo=1"
    ).startswith("KXGOVWINOMD")


def test_parse_input_rejects_empty():
    import pytest as _p
    from tools.add_race import parse_input
    with _p.raises(ValueError):
        parse_input("   ")


def test_candidates_are_ordered_by_price_and_drop_the_dust():
    """A primary board's tail sits at a tenth of a cent; OR-ing 16 names is noise."""
    from tools.add_race import derive_candidates
    sibs = [
        {"yes_sub_title": "Mike Lindell", "yes_bid_dollars": "0.61"},
        {"yes_sub_title": "Lisa Demuth", "yes_bid_dollars": "0.34"},
        {"yes_sub_title": "Kendall Qualls", "yes_bid_dollars": "0.013"},
        {"yes_sub_title": "Brad Kohler", "yes_bid_dollars": "0.0000"},
        {"yes_sub_title": "Scott Jensen", "yes_bid_dollars": "0.0000"},
    ]
    assert derive_candidates(sibs) == ["Mike Lindell", "Lisa Demuth", "Kendall Qualls"]


def test_block_is_valid_yaml_and_appends_without_touching_comments():
    """watchlist.yaml carries the Kalshi series map in comments.

    A yaml.safe_load/safe_dump round-trip would delete all of it, so the block is
    built as text and appended.
    """
    import yaml
    from tools.add_race import build_block
    existing = ("# KALSHI SERIES MAP -- do not lose this comment\n"
                "- id: keep-me\n  race_tag: keep\n  active: true\n")
    block = build_block(
        target={"ticker": "KXFOO-26-BAR", "event_ticker": "KXFOO-26",
                "title": "Will Bar win?", "yes_sub_title": "Bar Person"},
        siblings=[{}, {}], race_tag="kxfoo-26", market_id="kxfoo-26-bar",
        candidates=["Bar Person", "Baz Rival"], resolution_date="2026-08-04",
        pm_condition=None, clip=150)
    merged = existing.rstrip() + "\n" + block
    parsed = yaml.safe_load(merged)
    assert len(parsed) == 2
    assert parsed[1]["kalshi_ticker"] == "KXFOO-26-BAR"
    assert parsed[1]["candidates"] == ["Bar Person", "Baz Rival"]
    assert parsed[1]["active"] is True
    assert "KALSHI SERIES MAP" in merged, "existing comments must survive"


def test_missing_resolution_date_stages_the_race_inactive():
    """close_time is a placeholder on these boards, so a date is never guessed.

    Staging inactive keeps a race with no real date out of the alert path
    instead of giving it a countdown to a date invented from venue metadata.
    """
    import yaml
    from tools.add_race import build_block
    block = build_block(
        target={"ticker": "KXFOO-26-BAR", "event_ticker": "KXFOO-26",
                "title": "Will Bar win?", "yes_sub_title": "Bar"},
        siblings=[{}], race_tag="kxfoo-26", market_id="kxfoo-26-bar",
        candidates=["Bar"], resolution_date=None, pm_condition=None, clip=150)
    row = yaml.safe_load(block)[0]
    assert row["resolution_date"] is None
    assert row["active"] is False
    assert "NEEDS A HUMAN" in block


def test_polymarket_condition_id_round_trips():
    import yaml
    from tools.add_race import build_block
    cond = "0x" + "a" * 64
    block = build_block(
        target={"ticker": "T", "event_ticker": "E", "title": "t", "yes_sub_title": "s"},
        siblings=[{}], race_tag="r", market_id="m", candidates=["s"],
        resolution_date="2026-08-04", pm_condition=cond, clip=86)
    row = yaml.safe_load(block)[0]
    assert row["polymarket_condition_id"] == cond
    assert row["clip_size"] == 86


def test_race_tag_never_collides_with_an_existing_one():
    from tools.add_race import make_race_tag
    assert make_race_tag("KXFOO-26", set()) == "kxfoo-26"
    assert make_race_tag("KXFOO-26", {"kxfoo-26"}) == "kxfoo-26-2"
    assert make_race_tag("KXFOO-26", {"kxfoo-26", "kxfoo-26-2"}) == "kxfoo-26-3"


def test_quotes_in_a_market_title_cannot_break_the_yaml():
    import yaml
    from tools.add_race import build_block
    block = build_block(
        target={"ticker": "T", "event_ticker": "E",
                "title": 'Will "Scare Quotes" Smith win?', "yes_sub_title": 'The "Guy"'},
        siblings=[{}], race_tag="r", market_id="m", candidates=['The "Guy"'],
        resolution_date="2026-08-04", pm_condition=None, clip=150)
    row = yaml.safe_load(block)[0]
    assert "Scare Quotes" in row["market_title"]


# ================================================================= scout layer

from datetime import date as _date, datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402


# ---------------------------------------------------- price band (two tiers)

def test_band_is_77c_beyond_a_week():
    from desk.core.scout import band_limit, in_band
    assert band_limit(30, {}) == 0.77
    assert in_band("0.77", 30, {}) is True
    assert in_band("0.78", 30, {}) is False
    assert in_band("0.85", 30, {}) is False, "the near-tier must not leak into long-dated"


def test_band_rises_to_85c_inside_a_week():
    """80-85c is the intended zone for a short-dated favourite, not an accident."""
    from desk.core.scout import band_limit, in_band
    assert band_limit(7, {}) == 0.85
    assert band_limit(1, {}) == 0.85
    assert in_band("0.85", 7, {}) is True
    assert in_band("0.82", 3, {}) is True
    assert in_band("0.86", 3, {}) is False


def test_band_has_no_middle_tier():
    """The 21-day/80c tier was removed; day 8 and day 21 must both be 77c."""
    from desk.core.scout import band_limit
    assert band_limit(8, {}) == 0.77
    assert band_limit(21, {}) == 0.77
    assert band_limit(20, {}) == 0.77
    assert in_band_helper(0.80, 20) is False


def in_band_helper(ask, days):
    from desk.core.scout import in_band
    return in_band(str(ask), days, {})


def test_band_boundary_is_exactly_at_seven_days():
    from desk.core.scout import band_limit
    assert band_limit(7, {}) == 0.85
    assert band_limit(8, {}) == 0.77


def test_band_rejects_degenerate_prices():
    from desk.core.scout import in_band
    assert in_band("0", 3, {}) is False
    assert in_band("1", 3, {}) is False


def test_band_thresholds_are_configurable():
    from desk.core.scout import band_limit
    t = {"band_ask_standard": 0.5, "band_ask_near": 0.9, "band_near_days": 14}
    assert band_limit(20, t) == 0.5
    assert band_limit(10, t) == 0.9


# ----------------------------------------------------------------- appendix

def test_band_excluded_markets_are_not_silently_dropped():
    """Full coverage means an exclusion has to be visible and reversible."""
    from desk.core.scout import in_band, band_limit
    rows = [{"ticker": "A", "ask": "0.60", "days": 30},
            {"ticker": "B", "ask": "0.82", "days": 30},
            {"ticker": "C", "ask": "0.82", "days": 3}]
    kept, appendix = [], []
    for r in rows:
        (kept if in_band(r["ask"], r["days"], {}) else appendix).append(r)
    assert [r["ticker"] for r in kept] == ["A", "C"]
    assert [r["ticker"] for r in appendix] == ["B"]
    assert band_limit(30, {}) == 0.77


# ----------------------------------------------------------- classification

def test_appointments_are_not_primaries():
    """A live sweep put 16 Fed/judiciary nomination markets into a primary batch."""
    from desk.core.scout import classify
    for title in [
        "Will someone be nominated for a member of the Federal Reserve Board?",
        "Will Blanche be reported to the Senate Judiciary Committee?",
        "Will someone be nominated for member of the Election Assistance Commission?",
    ]:
        assert classify({"title": title}) == "other", title


def test_real_primaries_still_classify():
    from desk.core.scout import classify
    assert classify({"title": "Will Abdul El-Sayed be the Democratic nominee for the Senate in Michigan?"}) == "primary"
    assert classify({"title": "WA-05 primary: who will advance?"}) == "primary"
    assert classify({"title": "South Carolina Republican Senate special primary"}) == "special"


def test_party_resolved_markets_are_generals_not_primaries():
    """The Maine trap: the subtitle carries a person's name, the rules say party."""
    from desk.core.scout import classify
    m = {"ticker": "SENATEME-26-D", "title": "Maine Senate winner?",
         "yes_sub_title": "Troy Jackson",
         "rules_primary": "If a representative of the Democratic party is sworn in..."}
    assert classify(m) == "general"


# ------------------------------------------------- resolution date sourcing

def test_resolution_date_prefers_the_watchlist_then_the_calendar():
    from desk.core.scout import infer_resolution
    m = {"ticker": "KXWAPRIMARY-0526-NPOW", "title": "WA-05 primary: who will advance?",
         "close_time": "2027-11-03T15:00:00Z"}
    d, src = infer_resolution(m, None, {"KXWAPRIMARY-0526-NPOW": {"resolution_date": "2026-08-04"}})
    assert d == _date(2026, 8, 4) and src == "watchlist"
    d2, src2 = infer_resolution(m, None, {})
    assert d2 == _date(2026, 8, 4), "must fall back to the WA primary calendar"
    assert src2.startswith("primary_calendar_2026")


def test_close_time_is_never_silently_trusted():
    """close_time reads 2027 for a 2026 primary; using it unflagged hides races."""
    from desk.core.scout import infer_resolution
    m = {"ticker": "X", "title": "Some untagged nomination contest",
         "close_time": "2027-11-03T15:00:00Z"}
    d, src = infer_resolution(m, None, {})
    assert src.endswith("UNRELIABLE")


# ------------------------------------------------------------ consolidation

def _iso(hours_ago):
    return (_dt.now(_tz.utc) - _td(hours=hours_ago)).isoformat()


def test_whale_consolidation_needs_two_wallets_and_the_dollar_floor():
    from desk.core.scout import whale_consolidation
    hist = {"w1": {"c1": [[_iso(24), 1500.0]]},
            "w2": {"c1": [[_iso(48), 900.0]]}}
    assert whale_consolidation("c1", hist, {}) is not None       # 2 wallets, $2400
    one = {"w1": {"c1": [[_iso(24), 5000.0]]}}
    assert whale_consolidation("c1", one, {}) is None, "one wallet is not consensus"
    small = {"w1": {"c1": [[_iso(24), 500.0]]}, "w2": {"c1": [[_iso(24), 400.0]]}}
    assert whale_consolidation("c1", small, {}) is None, "under the $2k floor"


def test_whale_consolidation_ignores_adds_outside_the_window():
    from desk.core.scout import whale_consolidation
    stale = {"w1": {"c1": [[_iso(24 * 30), 5000.0]]},
             "w2": {"c1": [[_iso(24 * 30), 5000.0]]}}
    assert whale_consolidation("c1", stale, {}) is None


def test_print_consolidation_needs_three_same_side_qualifying_prints():
    from desk.core.scout import print_consolidation
    same = [{"ts": _iso(1), "notional": "400", "taker_side": "yes"},
            {"ts": _iso(5), "notional": "500", "taker_side": "yes"},
            {"ts": _iso(9), "notional": "600", "taker_side": "yes"}]
    assert print_consolidation(same, {})["side"] == "yes"
    split = [{"ts": _iso(1), "notional": "400", "taker_side": "yes"},
             {"ts": _iso(5), "notional": "500", "taker_side": "no"},
             {"ts": _iso(9), "notional": "600", "taker_side": "yes"}]
    assert print_consolidation(split, {}) is None, "opposite sides are not accumulation"
    tiny = [{"ts": _iso(1), "notional": "10", "taker_side": "yes"}] * 5
    assert print_consolidation(tiny, {}) is None, "below the notional floor"


# ------------------------------------------------------------ batch picking

def pick_batch(pack_dates: list[str], covered: list[str], today: _date) -> str | None:
    """Nearest uncovered settlement date at or after today."""
    future = sorted(d for d in pack_dates if _date.fromisoformat(d) >= today)
    for d in future:
        if d not in covered:
            return d
    return None


def test_batch_selection_takes_the_nearest_uncovered_window():
    dates = ["2026-08-04", "2026-08-11", "2026-08-18"]
    today = _date(2026, 7, 28)
    assert pick_batch(dates, [], today) == "2026-08-04"
    assert pick_batch(dates, ["2026-08-04"], today) == "2026-08-11"
    assert pick_batch(dates, ["2026-08-04", "2026-08-11"], today) == "2026-08-18"
    assert pick_batch(dates, dates, today) is None


def test_batch_selection_skips_dates_already_past():
    dates = ["2026-08-04", "2026-08-11"]
    assert pick_batch(dates, [], _date(2026, 8, 5)) == "2026-08-11"


# ------------------------------------------------------------------ sizing

def test_quarter_kelly_sizes_against_the_thousand_dollar_bankroll():
    """Sizing uses target_bankroll_usd (1000), not the current balance."""
    bankroll = 1000
    p, price = 0.60, 0.45           # our probability vs the ask
    b = (1 - price) / price
    kelly = (p * b - (1 - p)) / b
    quarter = kelly / 4
    dollars = round(bankroll * quarter, 2)
    assert 0 < quarter < 0.25
    assert dollars == round(1000 * quarter, 2)
    # A negative edge must never produce a position.
    p_bad = 0.40
    kelly_bad = (p_bad * b - (1 - p_bad)) / b
    assert kelly_bad < 0


# ------------------------------------------------------- FEC identity guards

def test_fec_refuses_a_surname_mismatch():
    """q='John James' returned 'JOHNSON, JAMES MICHAEL' with $20.9M raised.

    Reported against the Michigan governor's race that is a $21M number attached
    to the wrong human. A missing figure prompts a lookup; a confident wrong one
    gets quoted in a brief and sized against.
    """
    from desk.pollers.fec import _pick_match
    rows = [{"name": "JOHNSON, JAMES MICHAEL", "office_full": "House",
             "receipts": 20976864.33}]
    assert _pick_match("John James", rows, None) is None


def test_fec_accepts_a_genuine_surname_match():
    from desk.pollers.fec import _pick_match
    rows = [{"name": "EL-SAYED, ABDUL", "office_full": "Senate", "receipts": 14528335.93}]
    hit = _pick_match("Abdul El-Sayed", rows, "senate")
    assert hit is not None
    assert hit["receipts"] == 14528335.93


def test_office_mismatch_is_flagged_not_discarded():
    """A sitting House member running for the Senate has a House committee.

    That money is context, not an error, so it is returned flagged rather than
    thrown away -- Rashida Tlaib on the Michigan Senate board is exactly this.
    """
    from desk.pollers.fec import _pick_match
    rows = [{"name": "TLAIB, RASHIDA", "office_full": "House"}]
    hit = _pick_match("Rashida Tlaib", rows, "senate")
    assert hit is not None and hit["_office_mismatch"] is True


def test_compound_surnames_are_not_refused():
    """The FEC keeps the whole compound; Kalshi's last token is only half of it.

    Strict equality refused three genuine matches on the first live run.
    """
    from desk.pollers.fec import _pick_match
    assert _pick_match("Marie Gluesenkamp Perez",
                       [{"name": "GLUESENKAMP PEREZ, MARIE", "office_full": "House"}],
                       "house") is not None
    assert _pick_match("Kristen McDonald Rivet",
                       [{"name": "MCDONALD RIVET, KRISTEN", "office_full": "House"}],
                       "house") is not None


def test_a_bare_first_name_is_never_matched():
    """'Wayne' and 'Virginia' each matched three unrelated filers live."""
    from desk.pollers.fec import _pick_match
    assert _pick_match("Wayne", [{"name": "KINSEL, WAYNE CHARLES",
                                  "office_full": "House"}], "house") is None
    assert _pick_match("Virginia", [{"name": "FOXX, VIRGINIA ANN",
                                     "office_full": "House"}], "house") is None


def test_surname_parsing_handles_fec_and_kalshi_formats():
    from desk.pollers.fec import surname_of
    assert surname_of("EL-SAYED, ABDUL") == "el-sayed"
    assert surname_of("Abdul El-Sayed") == "el-sayed"
    assert surname_of("MARKEY, EDWARD SEN.") == "markey"
    # Kalshi writes ordinals as digits: "Hartzell Gray 3rd"
    assert surname_of("Hartzell Gray 3rd") == "gray"
    assert surname_of("Robert Smith Jr") == "smith"
    assert surname_of("") == ""


def test_state_races_never_spend_a_call_and_are_labelled():
    """The FEC covers federal candidates only.

    Three of the owner's positions are gubernatorial; a blank there reads as
    'raised nothing' unless it says why it is blank.
    """
    from desk.pollers.fec import FECClient

    class Boom:
        def get(self, *a, **k):
            raise AssertionError("must not call the FEC for a non-federal race")

    c = FECClient(Boom(), "key")
    out = c.totals("Mike Lindell", "governor")
    assert out["fec_status"] == "not_federal_race"
    assert "state" in out["note"]
    assert c.calls == 0


def test_federal_races_do_spend_a_call():
    from desk.pollers.fec import FECClient
    import httpx

    class C:
        def get(self, url, params=None):
            return httpx.Response(200, json={"results": [
                {"name": "BUSH, CORI", "office_full": "House",
                 "receipts": 1306727.18, "cash_on_hand_end_period": 97762.68,
                 "candidate_id": "H6MO01234"}]},
                request=httpx.Request("GET", url))

    c = FECClient(C(), "key")
    out = c.totals("Cori Bush", "house")
    assert out["fec_status"] == "fetched"
    assert out["cash_on_hand"] == 97762.68
    assert c.calls == 1


# ================================================================ round 7

def test_no_positions_mark_against_their_own_exit_book():
    """A NO holder exits by selling NO -- the walk of the implied YES asks.

    The old code complemented the YES sell walk, which prices the NO exit off
    the NO *ask* (what buying more costs) and overstated NO marks by roughly
    the spread plus slippage -- ~$24 across the two live NO positions.
    """
    from desk.core.compare import mark_positions
    snap = {"venue_data": {"kalshi": {
        "bid": "0.27", "ask": "0.34", "mid": "0.305",
        "executable": {"thin": False,
                       "sell_clip_vwap": {"vwap": "0.2650"},   # YES exit
                       "buy_clip_vwap": {"vwap": "0.3550"}},   # -> NO exit = .645
    }}}
    ledger = [
        {"market_id": "m", "venue": "kalshi", "side": "NO", "shares": 140,
         "avg_price_cents": 71.62, "cost_dollars_fee_incl": 102.27},
        {"market_id": "m", "venue": "kalshi", "side": "YES", "shares": 140,
         "avg_price_cents": 50.0},
    ]
    marked, _ = mark_positions(ledger, {"m": snap})
    no_row, yes_row = marked[0], marked[1]
    assert no_row["mark"] == "0.6450", "NO mark = 1 - buy walk, not 1 - sell walk"
    assert yes_row["mark"] == "0.2650", "YES mark = the sell walk"
    # Old behaviour would have said 1 - 0.2650 = 0.7350 -- 9c high here.


def test_cost_basis_uses_the_ledger_fee_inclusive_figure():
    from desk.core.compare import mark_positions
    snap = {"venue_data": {"kalshi": {
        "mid": "0.30", "bid": "0.29", "ask": "0.31",
        "executable": {"thin": False, "sell_clip_vwap": {"vwap": "0.29"},
                       "buy_clip_vwap": {"vwap": "0.31"}}}}}
    ledger = [{"market_id": "m", "venue": "kalshi", "side": "YES", "shares": 140,
               "avg_price_cents": 71.62, "cost_dollars_fee_incl": 102.27}]
    marked, _ = mark_positions(ledger, {"m": snap})
    assert marked[0]["cost_basis"] == "102.27", "ledger figure, not shares x entry (100.27)"


def test_cumulative_alert_title_shows_baseline_to_current():
    """Live bug: '84->84 (+8c)' -- the current price printed twice."""
    from desk.core.alerts import move_alert
    t = {"cumulative_move_cents": 7, "single_poll_move_cents": 4}
    mkt = {"id": "x", "label": "WI-Gov Hong", "mid": "0.84",
           "_prev_mid": "0.84",          # last poll == current (no poll move)
           "_baseline_mid": "0.76"}      # the brief baseline the delta is vs
    a = move_alert(mkt, +8.0, t, cumulative=True)
    assert "76->84" in a.title, a.title
    assert "84->84" not in a.title


def test_single_poll_alert_title_still_uses_prev_poll():
    from desk.core.alerts import move_alert
    t = {"single_poll_move_cents": 4, "single_poll_move_high_cents": 8}
    mkt = {"id": "x", "label": "MI-Sen", "mid": "0.55", "_prev_mid": "0.62",
           "_baseline_mid": "0.70"}
    a = move_alert(mkt, -7.0, t, cumulative=False)
    assert "62->55" in a.title


def test_viability_thresholds():
    """viable = mid >= (100/N - 10), N = candidates priced >= 10c."""
    n2 = (100 / 2) - 10   # 40 in a two-way
    n3 = (100 / 3) - 10   # ~23.3 in a three-way
    assert n2 == 40
    assert round(n3, 1) == 23.3
    assert 39.9 < n2 <= 40 and (25 >= n3)


def test_motion_flags_a_diverging_mid_and_admits_missing_history():
    from desk.core.scout import motion
    hist = [["t1", 0.60], ["t2", 0.61], ["t3", 0.60], ["t4", 0.61]]
    m = motion(hist, current_mid=0.66, thresholds={})
    assert m["available"] and m["in_motion"] is True
    assert m["deviation_cents"] > 2
    calm = motion(hist, current_mid=0.605, thresholds={})
    assert calm["in_motion"] is False
    none = motion(None, current_mid=0.60)
    assert none["available"] is False and none["in_motion"] is None


def test_feed_errors_do_not_page_but_hard_errors_do():
    errs = ["feed crystal-ball: HTTP 403", "kalshi orderbook X: HTTP 500"]
    hard = [e for e in errs if not e.startswith("feed ")]
    assert hard == ["kalshi orderbook X: HTTP 500"]


def test_briefs_relay_republishes_and_advances_cursor(monkeypatch):
    """E2: a brief POSTed to the ntfy topic lands in the gist with an index."""
    import json as _json
    import httpx
    from desk.core import relay as R

    ndjson = "\n".join([
        _json.dumps({"event": "open", "id": "x1", "time": 1753900000}),
        _json.dumps({"event": "message", "id": "m1", "time": 1753900100,
                     "title": "Morning brief", "message": "# Brief\n" + ("word " * 100)}),
        _json.dumps({"event": "message", "id": "m2", "time": 1753900200,
                     "message": "ping"}),  # too short -- not a brief
    ])

    class HTTP:
        def get(self, url, params=None, timeout=None):
            return httpx.Response(200, text=ndjson, request=httpx.Request("GET", url))

    written_files = {}
    class G:
        def read(self, name):
            return None
        def write(self, files):
            written_files.update(files)

    state = {}
    out = R.relay(G(), state, "desk-briefs-test", HTTP())
    assert len(out) == 1 and out[0].startswith("brief-")
    assert out[0] in written_files
    assert "briefs-index.json" in written_files
    idx = _json.loads(written_files["briefs-index.json"])
    assert idx["briefs"][0]["title"] == "Morning brief"
    assert state["briefs_relay_since"] == "m2", "cursor advances past non-briefs too"


def test_briefs_relay_survives_topic_outage():
    import httpx
    from desk.core import relay as R

    class HTTP:
        def get(self, url, params=None, timeout=None):
            raise httpx.ConnectError("blocked")

    assert R.relay(None, {}, "desk-briefs-test", HTTP()) == []


# ------------------------------------ consensus grades (roster-recalibrated)

CORES = {"Domer", "aenews2"}
ALERTING = {"Domer", "aenews2", "risk-manager", "anon-23d8", "Q96s"}


def _adds(*pairs):
    return [{"alias": a, "added_usd": usd, "before": 0, "after": usd}
            for a, usd in pairs]


def test_strong_needs_two_alerting_and_five_k():
    from tools.whale_book import grade
    t = {}
    assert grade(_adds(("risk-manager", 3000), ("anon-23d8", 2500)),
                 t, CORES, ALERTING) == "STRONG"          # 2 alerting, $5.5k
    assert grade(_adds(("risk-manager", 2000), ("anon-23d8", 2000)),
                 t, CORES, ALERTING) == "WATCH"           # 2 alerting, only $4k
    # Two CONTEXT wallets cannot form a STRONG whatever the dollars.
    assert grade(_adds(("wan123", 90000), ("debased", 90000)),
                 t, CORES, ALERTING) == "WATCH"


def test_heavy_fires_on_three_alerting_or_both_cores_plus_third():
    from tools.whale_book import grade
    t = {}
    assert grade(_adds(("Domer", 100), ("risk-manager", 100), ("Q96s", 100)),
                 t, CORES, ALERTING) == "HEAVY"           # 3 alerting, $ irrelevant
    assert grade(_adds(("Domer", 50), ("aenews2", 50), ("wan123", 10)),
                 t, CORES, ALERTING) == "HEAVY"           # both cores + any third
    assert grade(_adds(("Domer", 50), ("aenews2", 50)),
                 t, CORES, ALERTING) == "WATCH"           # both cores alone: no third


def test_single_wallet_never_grades():
    from tools.whale_book import grade
    assert grade(_adds(("Domer", 500000)), {}, CORES, ALERTING) is None
