#!/usr/bin/env python3
"""Build scout-pack.json: every in-scope primary/special, enriched.

    python tools/scout.py --emit-site ./site        # write to the Pages mirror
    python tools/scout.py --dry-run                 # print a summary, write nothing

Scope, all knobs in thresholds.yaml:
  * category      primaries + special elections (generals excluded but counted)
  * horizon       resolution within HORIZON_DAYS (75)
  * price band    buy-side ask <= 77c, or <= 85c inside 7 days

Full coverage: everything passing the filters gets an enrichment row. Markets
excluded only by the band land in ``appendix_band_excluded``, one line each, so
nothing the scope touched is invisible.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk.core import scout as S                                  # noqa: E402
from desk.core.books import D, kalshi_fee                          # noqa: E402
from desk.core.state import Gist, GistError, now_utc, stamp, trim_history  # noqa: E402
from desk.pollers.fec import FECClient                             # noqa: E402
from desk.pollers.kalshi import KalshiPoller, KalshiError, summarise_trades  # noqa: E402

log = logging.getLogger("scout")

PACK = "scout-pack.json"
FEC_CACHE = "fec_cache.json"


def load_thresholds(gist: Gist | None) -> dict:
    t = {}
    local = Path(__file__).resolve().parents[1] / "config" / "thresholds.yaml"
    if local.exists():
        t.update(yaml.safe_load(local.read_text()) or {})
    if gist:
        try:
            t.update(gist.read_yaml("thresholds.yaml") or {})
        except GistError:
            pass
    return t


def sweep(poller: KalshiPoller, max_pages: int = 60) -> tuple[list, dict]:
    """Every open political market with its full object and parent event.

    Uses the nested-markets events endpoint so ask, spread, volume and open
    interest all arrive with zero extra requests -- the band filter needs the
    ASK, which the light universe rows do not carry.
    """
    rows, cursor, pages, seen = [], None, 0, 0
    wanted = {"elections", "politics"}
    while pages < max_pages:
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            data = poller._get("/events", params)
        except KalshiError as e:
            log.warning("sweep stopped early: %s", e)
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
                rows.append((m, ev))
        cursor = data.get("cursor")
        if not cursor or not events:
            break
        time.sleep(0.2)
    return rows, {"pages": pages, "markets_seen": seen,
                  "truncated": bool(cursor) and pages >= max_pages}


def main() -> int:
    ap = argparse.ArgumentParser(description="scout: enrich every in-scope primary/special")
    ap.add_argument("--emit-site", metavar="DIR")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-trade-lookups", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    http = httpx.Client(timeout=30, follow_redirects=True,
                        headers={"User-Agent": "prediction-desk/1.0 (read-only)"})
    gist = Gist(os.environ.get("GIST_ID", ""), os.environ.get("GIST_TOKEN", ""), client=http)
    t = load_thresholds(gist)
    today = now_utc().date()

    try:
        watchlist = gist.read_yaml("watchlist.yaml", []) or []
        state = gist.read_json("pipeline_state.json", {}) or {}
        fec_cache = gist.read_json(FEC_CACHE, {}) or {}
    except GistError as e:
        print(f"cannot read config: {e}", file=sys.stderr)
        return 1

    wl_by_ticker = {r.get("kalshi_ticker"): r for r in watchlist if r.get("kalshi_ticker")}
    fec = FECClient(http, os.environ.get("FEC_API_KEY", "DEMO_KEY"), cache=fec_cache)

    poller = KalshiPoller(http)
    pairs, sweep_stats = sweep(poller)
    log.info("swept %d political markets across %d pages",
             sweep_stats["markets_seen"], sweep_stats["pages"])

    horizon = int(S.cfg(t, "horizon_days"))
    counts = {"primary": 0, "special": 0, "general_excluded": 0, "other": 0,
              "beyond_horizon": 0, "no_date": 0, "band_excluded": 0, "in_scope": 0}

    candidates_rows, appendix = [], []
    for m, ev in pairs:
        kind = S.classify(m, ev)
        if kind == "general":
            counts["general_excluded"] += 1
            continue
        if kind == "other":
            counts["other"] += 1
            continue
        counts[kind] += 1

        rdate, rsource = S.infer_resolution(m, ev, wl_by_ticker)
        days = (rdate - today).days if rdate else None
        if days is None:
            counts["no_date"] += 1
            continue
        if days < 0 or days > horizon:
            counts["beyond_horizon"] += 1
            continue

        ask = D(m.get("yes_ask_dollars"))
        bid = D(m.get("yes_bid_dollars"))
        base = {
            "id": m.get("ticker"), "venue": "kalshi",
            "event_ticker": m.get("event_ticker"),
            "title": (m.get("title") or "")[:150],
            "candidate": m.get("yes_sub_title") or "",
            "kind": kind,
            "resolution_date": rdate.isoformat(),
            "resolution_date_source": rsource,
            "days_to_resolution": days,
            "bid": str(bid), "ask": str(ask),
            "spread_cents": round(float((ask - bid) * 100), 2) if ask and bid else None,
            "volume_24h": str(D(m.get("volume_24h_fp"))),
            "open_interest": str(D(m.get("open_interest_fp"))),
            "url": f"https://kalshi.com/markets/{m.get('ticker','')}",
        }

        if not S.in_band(ask, days, t):
            counts["band_excluded"] += 1
            appendix.append({
                **{k: base[k] for k in ("id", "title", "candidate", "ask",
                                        "resolution_date", "days_to_resolution")},
                "excluded_because": (
                    f"ask {ask} above the {S.band_limit(days, t):.2f} band for "
                    f"{days} days out"),
            })
            continue

        counts["in_scope"] += 1
        candidates_rows.append((base, m, ev))

    log.info("in scope: %d | band-excluded: %d | generals counted: %d",
             counts["in_scope"], counts["band_excluded"], counts["general_excluded"])

    # ---- enrichment -----------------------------------------------------
    by_event: dict[str, list] = {}
    for _, m, ev in candidates_rows:
        by_event.setdefault(m.get("event_ticker"), []).append(m)

    max_lookups = args.max_trade_lookups or int(S.cfg(t, "max_trade_lookups"))
    whale_hist = state.get("whale_history") or {}
    consolidations, trade_lookups, skipped_trades = [], 0, 0
    races = []

    # Spend the trades budget where consolidation would actually mean something:
    # soonest-resolving and most-traded first, rather than whatever order the
    # sweep happened to return.
    candidates_rows.sort(key=lambda t3: (t3[0]["days_to_resolution"],
                                         -float(D(t3[1].get("volume_24h_fp")))))

    for base, m, ev in candidates_rows:
        field = by_event.get(m.get("event_ticker")) or [m]
        stale_h = float(S.cfg(t, "stale_hours"))
        flags = {
            "wide_spread": (base["spread_cents"] or 0) >= float(S.cfg(t, "wide_spread_cents")),
            "thin_volume": float(D(m.get("volume_24h_fp"))) < float(S.cfg(t, "thin_volume_24h")),
            "no_open_interest": float(D(m.get("open_interest_fp"))) <= 0,
            "date_unverified": base["resolution_date_source"].endswith("UNRELIABLE"),
        }
        base["flags"] = flags

        ask = D(base["ask"])
        prof = S.return_profile(ask)
        months = max(base["days_to_resolution"], 1) / 30.44
        if prof["ceiling_pct"] is not None:
            prof["per_month_pct"] = round(prof["ceiling_pct"] / months, 1)
        base["return"] = prof

        # Fee-aware breakeven on the buy side, at a nominal 100-contract clip.
        if 0 < ask < 1:
            fee = kalshi_fee(100, ask)
            base["fee_100_contracts_usd"] = str(fee)
            base["breakeven_with_fee"] = str((ask + fee / 100).quantize(Decimal("0.0001")))

        # FEC money, with the race's office so the client can skip non-federal
        # races (governors file with the state) and reject surname mismatches.
        office = S.detect_office(f"{base['title']} {m.get('yes_sub_title') or ''}")
        base["office"] = office
        money = (fec.totals(base["candidate"], office) if base["candidate"]
                 else {"fec_status": "no_name"})
        base["fec"] = money

        # Dominant favourite against a fragmented field -- the WA top-two shape,
        # where one candidate advances almost mechanically while the rest split.
        # NOTE this measures PRICE dominance, not party: Kalshi exposes no party
        # field, so a true "lone Republican among five Democrats" read needs the
        # Secretary of State list, which is exactly what the
        # official_candidate_list_checked guardrail is for.
        above_half = [x for x in field if D(x.get("yes_bid_dollars")) > D("0.5")]
        base["is_dominant_favourite"] = (
            len(field) >= 4 and len(above_half) == 1
            and above_half[0].get("ticker") == base["id"])

        base["structural_tags"] = S.structural_tags(base, field, money)

        # Full coverage is the point, so nothing is dropped -- but most of a
        # primary board is dust: on the first live sweep 694 of 928 in-scope
        # rows had zero 24h volume. `tradeable` says whether there is anything
        # to actually trade, so the analyst can prioritise without the pack
        # having quietly deleted anything.
        base["tradeable"] = bool(
            D(base["ask"]) > 0
            and (float(D(m.get("volume_24h_fp"))) > 0
                 or float(D(m.get("open_interest_fp"))) > 0))

        # Whale consolidation needs a Polymarket condition id; only tracked
        # markets have one, so this fires on the watchlist subset.
        wl = wl_by_ticker.get(base["id"]) or {}
        cons = S.whale_consolidation(wl.get("polymarket_condition_id"), whale_hist, t)

        # Print consolidation, budgeted. Overflow is reported, never silent.
        pcons = None
        if trade_lookups < max_lookups:
            try:
                prints = summarise_trades(poller.trades(base["id"], limit=100))
                trade_lookups += 1
                pcons = S.print_consolidation(prints, t)
                time.sleep(0.5)
            except KalshiError as e:
                log.debug("trades %s: %s", base["id"], e)
        else:
            skipped_trades += 1

        base["consolidation"] = [c for c in (cons, pcons) if c]
        if base["consolidation"]:
            consolidations.append(base)

        # Guardrails: ALWAYS false from code. Only the analyst may flip these,
        # in writing, after actually doing the check. A machine cannot verify a
        # Secretary of State candidate list or read a rulebook, and pretending
        # otherwise is how a ghost candidate becomes a position.
        base["guardrails"] = {
            "official_candidate_list_checked": False,
            "rules_read": False,
            "_note": ("both must be flipped by the analyst in its written workup "
                      "before any entry is recommended; code never sets these true"),
        }
        races.append(base)

    # Soonest first, tradeable ahead of dust, then by volume. Nothing is
    # removed; ordering just puts the rows a human would actually act on first.
    races.sort(key=lambda r: (r["days_to_resolution"],
                              not r.get("tradeable", False),
                              -float(r.get("volume_24h") or 0)))

    batches: dict[str, dict] = {}
    for r in races:
        b = batches.setdefault(r["resolution_date"], {"races": 0, "tradeable": 0})
        b["races"] += 1
        b["tradeable"] += 1 if r.get("tradeable") else 0

    pack = {
        "schema": "scout-pack/1",
        "generated_at": stamp()["utc"],
        "generated_at_pt": stamp()["pt"],
        "bankroll_for_sizing_usd": 1000,
        "read_me": (
            "Every open primary/special resolving within the horizon and inside the "
            "price band, one row each -- no top-N cap. Generals are excluded by "
            "design and counted below. Markets excluded ONLY by the price band are "
            "listed in appendix_band_excluded so nothing is invisible. "
            "guardrails.* are always false here: code cannot verify a Secretary of "
            "State list or read a rulebook, and the analyst must flip them in "
            "writing before recommending an entry."
        ),
        "scope": {
            "horizon_days": horizon,
            "band_ask_standard": S.cfg(t, "band_ask_standard"),
            "band_ask_near": S.cfg(t, "band_ask_near"),
            "band_near_days": S.cfg(t, "band_near_days"),
            "band_rule": ("buy-side ask <= 0.77 beyond 7 days; <= 0.85 within 7 days "
                          "(80-85c is the intended zone for that short window)"),
            "categories": ["primary", "special"],
        },
        "counts": {**counts, "sweep": sweep_stats,
                   "trade_lookups": trade_lookups,
                   "trade_lookups_skipped_by_cap": skipped_trades},
        "date_batches": [{"date": d, **n} for d, n in sorted(batches.items())],
        "fec": fec.status(),
        "consolidation_alerts": [
            {"id": r["id"], "title": r["title"], "candidate": r["candidate"],
             "signals": r["consolidation"]} for r in consolidations
        ],
        "races": races,
        "appendix_band_excluded": appendix,
    }

    if skipped_trades:
        log.warning("%d markets skipped the trades lookup at the %d cap",
                    skipped_trades, max_lookups)

    print(f"in scope        {counts['in_scope']}")
    print(f"band-excluded   {counts['band_excluded']} (appendix)")
    print(f"generals count  {counts['general_excluded']} (excluded by design)")
    print(f"date batches    {pack['date_batches']}")
    print(f"consolidation   {len(consolidations)}")
    print(f"FEC             {fec.status()['calls_made']} calls, "
          f"{fec.status()['candidates_unpriced']} unpriced, "
          f"throttled={fec.throttled}")

    if args.dry_run:
        return 0

    try:
        gist.write({PACK: json.dumps(pack, indent=1),
                    FEC_CACHE: json.dumps(fec.cache, indent=1)})
    except GistError as e:
        print(f"could not write the gist: {e}", file=sys.stderr)
        return 1

    if args.emit_site:
        prefix = os.environ.get("PAGES_PREFIX", "")
        if not prefix:
            print("PAGES_PREFIX unset; refusing to write a guessable path", file=sys.stderr)
            return 1
        base_dir = Path(args.emit_site) / "d" / prefix
        base_dir.mkdir(parents=True, exist_ok=True)
        out = base_dir / PACK
        out.write_text(json.dumps(pack, indent=1), encoding="utf-8")
        print(f"wrote {out.name} ({out.stat().st_size/1000:.0f} KB, {len(races)} races)")

        # Per-date slices. The full pack is >1MB because full coverage means
        # ~900 rows; the analyst works one settlement date at a time, so give it
        # a file the size of its actual job. Coverage is unchanged -- these are
        # views of the same rows, and the full pack stays published.
        for d_iso in sorted(batches):
            slice_rows = [r for r in races if r["resolution_date"] == d_iso]
            sp = base_dir / f"scout-pack-{d_iso}.json"
            sp.write_text(json.dumps({
                **{k: pack[k] for k in ("schema", "generated_at", "generated_at_pt",
                                        "bankroll_for_sizing_usd", "read_me", "scope", "fec")},
                "batch_date": d_iso,
                "race_count": len(slice_rows),
                "tradeable_count": sum(1 for r in slice_rows if r.get("tradeable")),
                "consolidation_alerts": [c for c in pack["consolidation_alerts"]
                                         if any(r["id"] == c["id"] and
                                                r["resolution_date"] == d_iso
                                                for r in slice_rows)],
                "races": slice_rows,
            }, indent=1), encoding="utf-8")
        print(f"wrote {len(batches)} per-date slices "
              f"(largest {max(len(json.dumps([r for r in races if r['resolution_date']==d])) for d in batches)/1000:.0f} KB)")

    topic = os.environ.get("NTFY_TOPIC", "")
    if consolidations and topic:
        lines = [f"- {r['candidate'] or r['title'][:40]}: "
                 f"{', '.join(c['kind'] for c in r['consolidation'])}"
                 for r in consolidations[:6]]
        try:
            httpx.post(f"https://ntfy.sh/{topic}",
                       content=("Tracked money is building the same side in "
                                f"{len(consolidations)} market(s):\n" + "\n".join(lines)
                                + "\n\nInvestigate, do not copy -- these fills are only "
                                  "visible after the price moved.").encode(),
                       headers={"Title": f"Scout: consolidation in {len(consolidations)} market(s)",
                                "Priority": "4", "Tags": "whale"}, timeout=20)
        except httpx.HTTPError as e:
            log.error("ntfy failed: %s", e)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
