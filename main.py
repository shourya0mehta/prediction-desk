#!/usr/bin/env python3
"""prediction-desk-v1 -- read-only sensor grid for prediction-market positions.

    load config from gist -> poll -> derive -> alert -> publish snapshot

This process holds exactly one credential: a GitHub PAT scoped to ``gist``. It
cannot place an order, move a dollar, or authenticate to an exchange. There is
no code path here that signs an exchange request, and there never should be.

    python main.py                 normal 30-minute poll
    python main.py --dry-run       full pipeline, prints alerts instead of pushing
    python main.py --universe      also refresh universe.json (brief cadence)
    python main.py --election-night  tighter thresholds, quiet hours bypassed
    python main.py --selftest      heartbeat: verify freshness and poller health
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from desk.core import alerts as alert_mod
from desk.core import compare, snapshot as snap_mod
from desk.core.books import D
from desk.core.state import (
    Gist, GistError, in_quiet_hours, last_brief_boundary, load_state, median,
    now_utc, save_state, stamp, trim_history,
)
from desk.pollers import feeds as feeds_mod
from desk.pollers import kalshi as kalshi_mod
from desk.pollers import polymarket as pm_mod

log = logging.getLogger("desk")

DEFAULT_THRESHOLDS = {
    "clip_size_default": 150,
    "single_poll_move_cents": 4,
    "single_poll_move_high_cents": 8,
    "cumulative_move_cents": 7,
    "volume_spike_multiple": 3,
    "gap_dislocation_cents": 3,
    "whale_notional_change": 500,
    "large_print_notional": 1000,
    "large_print_median_multiple": 10,
    "cooldown_minutes": 60,
    "quiet_hours_min_move_cents": 10,
    "stale_snapshot_minutes": 120,
    "election_night_move_cents": 2,
}


def load_thresholds(gist: Gist | None) -> dict:
    t = dict(DEFAULT_THRESHOLDS)
    local = os.path.join(os.path.dirname(__file__), "config", "thresholds.yaml")
    if os.path.exists(local):
        import yaml
        with open(local) as fh:
            t.update(yaml.safe_load(fh) or {})
    if gist:
        try:
            override = gist.read_yaml("thresholds.yaml")
            if isinstance(override, dict):
                t.update(override)
        except GistError as e:
            log.warning("gist thresholds unreadable, using defaults: %s", e)
    return t


def normalise_watchlist(rows: list) -> list:
    """Coerce YAML scalars into JSON-safe primitives.

    PyYAML turns an unquoted ``resolution_date: 2026-08-04`` into a
    ``datetime.date``, which json.dumps refuses. That would break publishing the
    snapshot on every run, so dates are flattened to ISO strings at the boundary
    rather than defended against at each use site.
    """
    import datetime as _dt

    out = []
    for row in rows or []:
        r = dict(row)
        for key, val in list(r.items()):
            if isinstance(val, (_dt.date, _dt.datetime)):
                r[key] = val.isoformat()
        out.append(r)
    return out


# Words too generic to identify a race. "Democratic party" is a real candidate
# label on the MI-07 party market, but matching it tagged an unrelated Roland
# Martin video as MI-07 news during build-time testing.
GENERIC_TERMS = {
    "democratic party", "republican party", "democrat", "republican",
    "democratic", "gop", "party", "yes", "no",
}


def race_keyword_map(watchlist: list) -> dict[str, list[str]]:
    """Distinctive terms per race, used to tag feed items.

    Only reasonably specific strings earn a place: a generic party name matches
    half the political internet and would mislabel unrelated news as belonging
    to a tracked race.
    """
    out: dict[str, list[str]] = {}
    for row in watchlist or []:
        tag = row.get("race_tag")
        if not tag:
            continue
        words = out.setdefault(tag, [])
        for w in list(row.get("keywords") or []) + [row.get("candidate")]:
            if not w:
                continue
            w = str(w).strip()
            if len(w) < 4 or w.lower() in GENERIC_TERMS:
                continue
            words.append(w)
    return {k: v for k, v in out.items() if v}


def _links(row: dict) -> list[str]:
    out = []
    if row.get("kalshi_ticker"):
        out.append(f"https://kalshi.com/markets/{row['kalshi_ticker']}")
    if row.get("polymarket_slug"):
        out.append(f"https://polymarket.com/market/{row['polymarket_slug']}")
    return out


def attach_context(feed_items: list, race_tag: str, within_hours: int = 2) -> tuple[str, list]:
    """Find a feed headline close enough in time to explain a move.

    An unexplained move is itself the signal -- it is the prompt to go look at X
    by hand -- so when nothing matches we say so rather than padding.
    """
    cutoff = now_utc() - timedelta(hours=within_hours)
    for item in feed_items:
        if item.get("race_tag") != race_tag:
            continue
        try:
            ts = datetime.fromisoformat(item["ts"])
        except (KeyError, ValueError):
            continue
        if ts >= cutoff:
            # The headline itself is the payload. Google News links are base64
            # redirect blobs that are unreadable in a notification, so they are
            # dropped and the publisher name recovered from the title instead.
            raw = item.get("title") or ""
            attribution = feeds_mod.publisher_of(raw) or item.get("source") or "feed"
            link = feeds_mod.readable_link(item.get("url"))
            return (f"Possibly related: \"{feeds_mod.clean_headline(raw)}\" "
                    f"({attribution}).", [link] if link else [])
    return ("No news attached -- unexplained move. Worth checking the race's X handles "
            "by hand; the pipeline cannot read them.", [])


def run(args) -> int:
    started = now_utc()
    errors: list[str] = []

    token = os.environ.get("GIST_TOKEN", "")
    gist_id = os.environ.get("GIST_ID", "")
    topic = os.environ.get("NTFY_TOPIC", "")

    if not token or not gist_id:
        print("GIST_TOKEN and GIST_ID must be set in the environment.", file=sys.stderr)
        return 2

    http = httpx.Client(timeout=30, follow_redirects=True,
                        headers={"User-Agent": "prediction-desk/1.0 (read-only)"})
    gist = Gist(gist_id, token, client=http)

    thresholds = load_thresholds(gist)
    if args.election_night:
        thresholds["single_poll_move_cents"] = thresholds.get("election_night_move_cents", 2)
        thresholds["single_poll_move_high_cents"] = max(
            4, int(thresholds["single_poll_move_cents"]) * 2)
        # Halve both large-print bars: on a settlement night a $500 print that is
        # 5x normal is worth knowing about, where on an ordinary Tuesday it is not.
        thresholds["large_print_notional"] = float(thresholds["large_print_notional"]) / 2
        thresholds["large_print_median_multiple"] = float(
            thresholds["large_print_median_multiple"]) / 2

    state = load_state(gist)
    engine = alert_mod.AlertEngine(thresholds, state, topic, client=http,
                                   dry_run=args.dry_run, election_night=args.election_night)

    # ---- config ---------------------------------------------------------
    try:
        watchlist = gist.read_yaml("watchlist.yaml", []) or []
        whales_cfg = gist.read_yaml("whales.yaml", []) or []
        feeds_cfg = gist.read_yaml("feeds.yaml", []) or []
        ledger_doc = gist.read_json("positions-ledger.json", {}) or {}
    except GistError as e:
        engine.pipeline_alert(f"Cannot read config from the gist: {e}")
        return 1

    watchlist = normalise_watchlist(watchlist)
    ledger = ledger_doc.get("positions", []) if isinstance(ledger_doc, dict) else (ledger_doc or [])
    active = [r for r in watchlist if r.get("active", True)]

    # Cold start: with no prior state every open whale position looks like a
    # brand-new entry and every recent print looks unprecedented. Firing all of
    # that at the phone would train the owner to ignore the channel on day one,
    # which is the worst possible outcome for an alerting system. So the first
    # run observes silently, records the baseline, and sends a single summary.
    cold_start = not state.get("initialised")
    if cold_start:
        log.info("cold start: seeding baselines, suppressing per-market alerts")
        engine.silent = True
    if not active:
        engine.pipeline_alert("watchlist.yaml has no active markets -- the desk is blind.")
        return 1

    kal = kalshi_mod.KalshiPoller(http)
    pm = pm_mod.PolymarketPoller(http)

    # ---- feeds ----------------------------------------------------------
    feed_items: list[dict] = []
    if not args.selftest:
        # Static feeds from the gist, plus one Google News feed derived per race
        # from that race's `candidates` list. The derived feeds replace the
        # manual Google Alerts setup, so adding a race brings its news coverage
        # with it and there is nothing to click.
        derived = feeds_mod.derive_feeds(active)
        all_feeds = list(feeds_cfg) + derived
        log.info("feeds: %d static + %d derived", len(feeds_cfg), len(derived))
        feed_items, feed_errors = feeds_mod.collect(http, all_feeds, race_keyword_map(watchlist))
        errors.extend(feed_errors)

    # ---- markets --------------------------------------------------------
    tickers = [r["kalshi_ticker"] for r in active if r.get("kalshi_ticker")]
    try:
        kmarkets = kal.markets(tickers)
    except kalshi_mod.KalshiError as e:
        errors.append(f"kalshi /markets: {e}")
        kmarkets = {}
    if tickers and not kmarkets:
        engine.pipeline_alert(f"Kalshi returned nothing for {len(tickers)} watchlist tickers.")

    markets_out: list[dict] = []
    cross_venue: list[dict] = []
    prev_markets = state.get("markets") or {}
    baseline = state.get("brief_baseline") or {}

    for row in active:
        mid_id = row["id"]
        clip = row.get("clip_size") or thresholds["clip_size_default"]
        label = row.get("label") or mid_id
        venue_data: dict = {}

        # --- Kalshi
        kticker = row.get("kalshi_ticker")
        if kticker and kticker in kmarkets:
            try:
                ob = kal.orderbook(kticker)
            except kalshi_mod.KalshiError as e:
                errors.append(f"kalshi orderbook {kticker}: {e}")
                ob = {}
            venue_data["kalshi"] = kalshi_mod.quote(kmarkets[kticker], ob, clip)
            time.sleep(1.0 / kalshi_mod.REQUESTS_PER_SECOND)

            try:
                prints = kalshi_mod.summarise_trades(kal.trades(kticker, limit=100))
            except kalshi_mod.KalshiError as e:
                errors.append(f"kalshi trades {kticker}: {e}")
                prints = []
            venue_data["kalshi"]["recent_prints"] = prints[:20]
            time.sleep(1.0 / kalshi_mod.REQUESTS_PER_SECOND)
        elif kticker:
            errors.append(f"watchlist ticker {kticker} not returned by Kalshi")

        # --- Polymarket (international; reference only)
        cond = row.get("polymarket_condition_id")
        if cond:
            try:
                pmm = pm.market_by_condition(cond)
                if pmm:
                    toks = pm_mod.token_ids(pmm)
                    bk = pm.book(toks[0]) if toks else {}
                    venue_data["polymarket"] = pm_mod.quote(pmm, bk, clip)
                else:
                    errors.append(f"gamma returned no market for {cond}")
            except pm_mod.PolymarketError as e:
                errors.append(f"polymarket {cond}: {e}")

        kblock = venue_data.get("kalshi")
        pblock = venue_data.get("polymarket")

        # Executable numbers come from Kalshi only. The international book is a
        # signal, never a quote the owner can act on.
        executable = (kblock or {}).get("executable") or {"thin": None}
        mid_price = (kblock or {}).get("mid") or (pblock or {}).get("mid")

        prev = prev_markets.get(mid_id) or {}
        d_poll = compare.delta_cents(mid_price, prev.get("mid"))
        d_brief = compare.delta_cents(mid_price, baseline.get(mid_id))

        entry = {
            "id": mid_id,
            "race_tag": row.get("race_tag"),
            "label": label,
            "resolution_date": row.get("resolution_date"),
            "rules_diff": row.get("rules_diff"),
            "clip_size": clip,
            "venue_data": venue_data,
            "mid": mid_price,
            "executable": executable,
            "book_top5": (kblock or {}).get("book_top5"),
            "volume_24h": (kblock or {}).get("volume_24h"),
            "open_interest": (kblock or {}).get("open_interest"),
            "delta_since_last_poll": d_poll,
            "delta_since_last_brief": d_brief,
        }
        markets_out.append(entry)

        ctx, ctx_links = attach_context(feed_items, row.get("race_tag"))
        alert_view = {
            "id": mid_id, "label": label, "mid": mid_price, "_prev_mid": prev.get("mid"),
            "_context": ctx, "_links": _links(row) + ctx_links,
            "rules_diff": row.get("rules_diff"),
        }

        if not args.selftest:
            if d_poll is not None:
                a = alert_mod.move_alert(alert_view, d_poll, thresholds)
                if a:
                    engine.emit(a)
            if d_brief is not None:
                a = alert_mod.move_alert(alert_view, d_brief, thresholds, cumulative=True)
                if a:
                    engine.emit(a)

            spike = compare.volume_spike((kblock or {}).get("volume_24h"), prev.get("volume_24h"))
            if spike:
                a = alert_mod.volume_alert(alert_view, spike, thresholds)
                if a:
                    engine.emit(a)

            # Large prints, against this market's own trailing 24h median.
            # Deduped by trade_id: the public tape returns the same last-100
            # trades every poll, so without this a single big print re-alerts
            # every time the cooldown lapses for as long as it stays in the
            # window.
            hist = trim_history(state.setdefault("print_history", {}).get(mid_id, []), 24)
            med = median([n for _, n in hist])
            seen_trades = set(state.setdefault("seen_trade_ids", {}).get(mid_id, []))
            fresh_ids = []
            fired = False
            for p in ((kblock or {}).get("recent_prints") or [])[:25]:
                tid = p.get("trade_id")
                if not tid or tid in seen_trades:
                    continue
                fresh_ids.append(tid)
                hist.append([p["ts"], float(p["notional"])])
                if fired:
                    continue
                a = alert_mod.large_print_alert(alert_view, p, med, thresholds)
                if a:
                    engine.emit(a)
                    fired = True  # one print alert per market per poll is enough
            state["print_history"][mid_id] = trim_history(hist, 24)
            # Keep the most recent 400 ids per market: comfortably more than the
            # 100-trade window the API returns, without growing without bound.
            state["seen_trade_ids"][mid_id] = (fresh_ids + list(seen_trades))[:400]

        # Cross-venue divergence
        gap = compare.cross_venue_gap(kblock, pblock)
        if gap:
            gh = trim_history(state.setdefault("gap_history", {}).get(mid_id, []), 24 * 7)
            disloc = compare.gap_dislocation(gap["gap_cents"], gh)
            gh.append([now_utc().isoformat(), gap["gap_cents"]])
            state["gap_history"][mid_id] = trim_history(gh, 24 * 7)

            cross_venue.append({
                "id": mid_id, "net_edge_cents": gap["gap_cents"],
                "median_gap_cents": disloc["median_gap_cents"],
                "dislocation_cents": disloc["dislocation_cents"],
                "rules_diff": row.get("rules_diff"),
                "executable": False,
                "note": gap["note"],
            })
            if not args.selftest:
                a = alert_mod.gap_alert(alert_view, gap, disloc, thresholds)
                if a:
                    engine.emit(a)

        prev_markets[mid_id] = {"mid": mid_price, "volume_24h": (kblock or {}).get("volume_24h")}

    state["markets"] = prev_markets

    # ---- whales ---------------------------------------------------------
    watch_conditions = {r["polymarket_condition_id"] for r in active
                        if r.get("polymarket_condition_id")}
    whales_out = []
    for w in whales_cfg:
        wallet, alias = w.get("wallet"), w.get("alias") or "whale"
        if not wallet or not w.get("active", True):
            continue
        try:
            rows = pm.positions(wallet)
        except pm_mod.PolymarketError as e:
            errors.append(f"whale {alias}: {e}")
            continue

        current = pm_mod.summarise_whale_positions(rows, watch_conditions)
        prior = (state.setdefault("whales", {}).get(wallet) or {})
        changes = []
        now_map = {}
        for p in current:
            cid = p["condition_id"]
            val = float(p["current_value"] or 0)
            now_map[cid] = val
            before = prior.get(cid)
            if before is None:
                changes.append({"kind": "entry", "title": p["title"], "value_change": val,
                                "on_watchlist": p["on_watchlist"]})
            elif abs(val - before) >= 1:
                changes.append({"kind": "size", "title": p["title"],
                                "value_change": val - before, "on_watchlist": p["on_watchlist"]})
        for cid, before in prior.items():
            if cid not in now_map:
                changes.append({"kind": "exit", "title": cid[:40], "value_change": -before,
                                "on_watchlist": cid in watch_conditions})

        state["whales"][wallet] = now_map
        whales_out.append({
            "alias": alias, "wallet": wallet, "note": w.get("note"),
            "positions_tracked": len(current), "changes_24h": changes[:20],
        })
        if changes and not args.selftest and w.get("alert", True):
            a = alert_mod.whale_alert(alias, wallet, changes, thresholds)
            if a:
                engine.emit(a)

    # ---- feed alerts ----------------------------------------------------
    # Deduped by item URL across runs. Items stay in the 36h window by design
    # (the analyst needs them in the snapshot), so without this the same
    # headline would re-alert on every poll for a day and a half.
    if not args.selftest:
        seen_feed = set(state.setdefault("seen_feed_guids", []))
        new_guids = []
        for item in feed_items:
            guid = item.get("url") or item.get("title")
            if not guid:
                continue
            if guid not in seen_feed:
                new_guids.append(guid)
                if item.get("keywords") and item.get("race_tag"):
                    engine.emit(alert_mod.feed_alert(item))
        state["seen_feed_guids"] = (new_guids + list(seen_feed))[:1000]

    # ---- positions ------------------------------------------------------
    by_id = {m["id"]: m for m in markets_out}
    marked, basis = compare.mark_positions(ledger, by_id)

    # ---- catalysts ------------------------------------------------------
    catalysts = snap_mod.catalysts_next_14d(watchlist)
    if not args.selftest:
        for c in catalysts:
            h = c.get("hours_out")
            if h is None:
                continue
            if h <= 2 or 23 <= h <= 25:
                engine.emit(alert_mod.catalyst_alert(c, h))

    # ---- universe -------------------------------------------------------
    universe = None
    if args.universe:
        universe = []
        universe_stats = {}
        try:
            krows, kstats = kal.open_political_markets()
            universe.extend(krows)
            universe_stats["kalshi"] = kstats
        except kalshi_mod.KalshiError as e:
            errors.append(f"kalshi universe: {e}")
        try:
            prows, pstats = pm.open_political_markets()
            universe.extend(prows)
            universe_stats["polymarket_intl"] = pstats
        except pm_mod.PolymarketError as e:
            errors.append(f"polymarket universe: {e}")
        log.info("universe: %s", universe_stats)

        known = set(state.get("seen_market_ids") or [])
        # Match new listings on the race's own distinctive keywords, with word
        # boundaries. The obvious shortcut -- comparing the first segment of the
        # race tag -- means "wi-gov-dem" matches the word "Will" and "mi-07"
        # matches any title containing "mi". On a live 1,777-market sweep that
        # produced 114 false listings, including a Peruvian presidency market
        # filed under the Wisconsin governor's race.
        kwmap = race_keyword_map(active)
        if known:
            for r in universe:
                if r["id"] in known:
                    continue
                blob = (r.get("title") or "").lower()
                hit = None
                for tag, words in kwmap.items():
                    if any(re.search(rf"\b{re.escape(w.lower())}\b", blob) for w in words):
                        hit = tag
                        break
                if hit and not args.selftest:
                    engine.emit(alert_mod.new_listing_alert(r, hit))
        state["seen_market_ids"] = [r["id"] for r in universe]

    # ---- brief baseline -------------------------------------------------
    boundary = last_brief_boundary(started)
    prior_boundary = state.get("brief_baseline_at")
    rolled = prior_boundary != boundary.isoformat()
    if rolled:
        state["brief_baseline"] = {m["id"]: m["mid"] for m in markets_out}
        state["brief_baseline_at"] = boundary.isoformat()
        state["alerts_since_last_brief"] = []

    # ---- assemble & publish --------------------------------------------
    snap = snap_mod.build(
        markets=markets_out,
        positions={"ledger": ledger, "marked_pnl": marked, "pnl_price_basis": basis,
                   "clusters": ledger_doc.get("clusters") if isinstance(ledger_doc, dict) else None,
                   "bankroll": (ledger_doc.get("bankroll_snapshot_2026_07_28")
                                if isinstance(ledger_doc, dict) else None)},
        cross_venue=cross_venue,
        whales=whales_out,
        feeds_36h=feed_items,
        alerts_since_last_brief=state.get("alerts_since_last_brief") or [],
        catalysts=catalysts,
        errors=errors,
        meta={
            "run_started": stamp(started),
            "mode": ("election_night" if args.election_night
                     else "selftest" if args.selftest else "poll"),
            "dry_run": args.dry_run,
            "brief_baseline_at": state.get("brief_baseline_at"),
            "quiet_hours": in_quiet_hours(),
            "kalshi_executable_only": True,
            "polymarket_price_basis": pm_mod.PRICE_BASIS,
            "universe_refreshed": bool(args.universe),
        },
    )

    problems = snap_mod.validate(snap)
    if problems:
        errors.extend(f"schema: {p}" for p in problems)
        snap["errors"] = errors
        engine.pipeline_alert("snapshot failed schema validation:\n" + "\n".join(problems[:6]))

    if cold_start:
        state["initialised"] = True
        engine.silent = False
        tracked = len(markets_out)
        engine.emit(alert_mod.Alert(
            market_id="_pipeline", trigger="pipeline",
            title="Prediction desk is live",
            body=(f"Baseline recorded for {tracked} markets across "
                  f"{len({m['race_tag'] for m in markets_out})} races, "
                  f"{len(whales_out)} whale wallets and {len(feed_items)} feed items "
                  f"from the last 36h.\n"
                  f"{len(engine.suppressed)} first-run alerts were suppressed on "
                  f"purpose -- with no prior state every open position looks new. "
                  f"Real alerts start from the next poll."),
            level="default", tags=["white_check_mark"],
        ))

    if args.dry_run:
        print(json.dumps(snap, indent=1, default=str)[:2500])
        print(f"\n[dry-run] {len(engine.sent)} alerts would fire, "
              f"{len(engine.suppressed)} suppressed, {len(errors)} errors")
        return 0

    try:
        snap_mod.publish(gist, snap, universe, locals().get("universe_stats"))
        save_state(gist, state)
    except GistError as e:
        engine.pipeline_alert(f"Could not publish snapshot to the gist: {e}")
        return 1

    if errors:
        engine.pipeline_alert(
            f"Run finished with {len(errors)} error(s):\n" + "\n".join(errors[:6]),
            level="default")

    log.info("published %d markets, %d alerts, %d errors",
             len(markets_out), len(engine.sent), len(errors))
    return 0


def selftest(args) -> int:
    """Heartbeat: is the published snapshot fresh and are the pollers alive?"""
    token, gist_id = os.environ.get("GIST_TOKEN", ""), os.environ.get("GIST_ID", "")
    topic = os.environ.get("NTFY_TOPIC", "")
    http = httpx.Client(timeout=30, follow_redirects=True)
    problems: list[str] = []   # broken: worth a push
    degraded: list[str] = []   # working but reduced: logged, never pushed

    try:
        gist = Gist(gist_id, token, client=http)
        snap = gist.read_json(snap_mod.SNAPSHOT_FILE)
        if not snap:
            problems.append("snapshot.json is missing or empty")
        else:
            age = snap_mod.age_minutes(snap)
            limit = float(DEFAULT_THRESHOLDS["stale_snapshot_minutes"])
            if age is None:
                problems.append("snapshot.json has no readable generated_at")
            elif age > limit:
                problems.append(f"snapshot is {age:.0f} minutes old (limit {limit:.0f})")
            # Not every recorded error is worth waking someone for. A blocked RSS
            # feed degrades the brief's context; a dead market poller blinds the
            # desk. Only the second kind is a heartbeat failure -- otherwise one
            # publisher that refuses GitHub's IP range would page every morning
            # at 08:05 forever, and the alert stops meaning anything.
            hard, soft = [], []
            for e in snap.get("errors") or []:
                (soft if str(e).startswith("feed ") else hard).append(str(e))
            if hard:
                problems.append(f"last run had {len(hard)} market-data error(s): "
                                + "; ".join(hard[:3]))
            if soft:
                degraded.extend(soft)
    except GistError as e:
        problems.append(f"gist unreachable: {e}")

    try:
        kalshi_mod.KalshiPoller(http)._get("/exchange/status")
    except kalshi_mod.KalshiError as e:
        problems.append(f"kalshi unreachable: {e}")

    try:
        pm_mod.PolymarketPoller(http)._get(f"{pm_mod.GAMMA}/markets", {"limit": 1})
    except pm_mod.PolymarketError as e:
        problems.append(f"polymarket gamma unreachable: {e}")

    if degraded:
        # Printed so it shows in the Actions log and in `--selftest` locally,
        # but deliberately never pushed.
        print("heartbeat DEGRADED (not pushed):\n" + "\n".join(f"- {d}" for d in degraded))

    if problems:
        engine = alert_mod.AlertEngine({}, {"alerts": {}}, topic, client=http,
                                       dry_run=args.dry_run)
        body = "Heartbeat FAILED:\n" + "\n".join(f"- {p}" for p in problems)
        if degraded:
            body += "\n\nAlso degraded (not the cause):\n" + "\n".join(f"- {d}" for d in degraded)
        engine.pipeline_alert(body)
        print("HEARTBEAT FAILED:\n" + "\n".join(problems), file=sys.stderr)
        return 1

    print("heartbeat OK" + (f" ({len(degraded)} degraded feed(s))" if degraded else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="prediction-desk-v1 (read-only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="run everything, print alerts instead of pushing, do not write the gist")
    ap.add_argument("--universe", action="store_true", help="also refresh universe.json")
    ap.add_argument("--election-night", action="store_true",
                    help="tighten move threshold and bypass quiet hours")
    ap.add_argument("--selftest", action="store_true", help="heartbeat check only")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs a line per request; a single poll makes ~40, which buries the
    # lines that matter in the Actions log. Raise it back with -v.
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.selftest:
        return selftest(args)
    try:
        return run(args)
    except Exception as e:  # never die silently -- spec 0.3
        log.exception("unhandled failure")
        topic = os.environ.get("NTFY_TOPIC", "")
        if topic and not args.dry_run:
            try:
                httpx.post(f"{alert_mod.NTFY_BASE}/{topic}",
                           content=f"Pipeline crashed: {type(e).__name__}: {e}".encode(),
                           headers={"Title": "Prediction desk: CRASH", "Priority": "5"},
                           timeout=15)
            except httpx.HTTPError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
