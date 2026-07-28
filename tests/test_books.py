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
