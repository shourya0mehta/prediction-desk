#!/usr/bin/env python3
"""Build whale-book.json: full political books, daily deltas, consensus grades.

    python tools/whale_book.py            # build, push to gist, alert on consensus
    python tools/whale_book.py --dry-run  # print, write nothing, alert nothing

Runs daily from the poll workflow. The mirror picks whale-book.json up from the
gist on every poll, so the file never 404s between builds.

Consensus grades, recalibrated 2026-07-29 to the approved 10-wallet roster
(5 alerting) -- the original bars were written for a ~25-wallet book and could
near-never fire on this one. All knobs in thresholds.yaml:
  WATCH   2 roster wallets aligned on one side               -> pack only
  STRONG  >= 2 of the ALERTING set aligned, >= $5k combined  -> ntfy push,
          mandatory next-brief coverage
  HEAVY   >= 3 alerting aligned, OR both cores plus any      -> high-priority
          third roster wallet regardless of dollars             push + queue

"Aligned" means net-ADDED dollar value to the same side inside the window --
holding still is conviction, but only new money is a signal. Alert bodies name
wallets, sides, combined dollars and before->after position sizes, never bare
share counts.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desk.core.state import Gist, GistError, now_utc, stamp, trim_history  # noqa: E402
from desk.pollers.polymarket import is_political  # noqa: E402

log = logging.getLogger("whale-book")

BOOK_FILE = "whale-book.json"
STATE_FILE = "whale-book-state.json"
QUEUE_FILE = "standing-queue.md"
DATA = "https://data-api.polymarket.com"

DEFAULTS = {
    "consensus_watch_wallets": 2,
    "consensus_strong_alerting": 2,
    "consensus_strong_usd": 5_000,
    "consensus_heavy_alerting": 3,
    "consensus_heavy_core_min": 2,   # both cores + any third fires HEAVY
    "consensus_window_days": 7,
}


def cfg(t, k):
    return (t or {}).get(k, DEFAULTS[k])


def positions(client, wallet):
    try:
        r = client.get(f"{DATA}/positions", params={
            "user": wallet, "limit": 500, "sortBy": "CURRENT", "sortDirection": "DESC"})
        r.raise_for_status()
        return r.json() or []
    except httpx.HTTPError as e:
        log.warning("positions %s: %s", wallet[:10], e)
        return None


def grade(aligned: list, t: dict, core_aliases: set,
          alerting_aliases: set) -> str | None:
    """WATCH | STRONG | HEAVY for one (market, side) group of net-adders.

    Alignment among the ALERTING set is what escalates -- context wallets
    (polled, alert:false) count toward WATCH and toward the "any third" leg of
    HEAVY, but cannot form a STRONG on their own.
    """
    n = len(aligned)
    alerting = [a for a in aligned if a["alias"] in alerting_aliases]
    usd = sum(a["added_usd"] for a in alerting)
    cores = sum(1 for a in aligned if a["alias"] in core_aliases)

    if len(alerting) >= int(cfg(t, "consensus_heavy_alerting")):
        return "HEAVY"
    if cores >= int(cfg(t, "consensus_heavy_core_min")) and n >= cores + 1:
        return "HEAVY"          # both cores plus any third, dollars irrelevant
    if (len(alerting) >= int(cfg(t, "consensus_strong_alerting"))
            and usd >= float(cfg(t, "consensus_strong_usd"))):
        return "STRONG"
    if n >= int(cfg(t, "consensus_watch_wallets")):
        return "WATCH"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    http = httpx.Client(timeout=30, follow_redirects=True)
    gist = Gist(os.environ.get("GIST_ID", ""), os.environ.get("GIST_TOKEN", ""), client=http)
    topic = os.environ.get("NTFY_TOPIC", "")

    try:
        roster = gist.read_yaml("whales.yaml", []) or []
        t = gist.read_yaml("thresholds.yaml", {}) or {}
        st = gist.read_json(STATE_FILE, {}) or {}
    except GistError as e:
        print(f"cannot read config: {e}", file=sys.stderr)
        return 1

    core_aliases = {w.get("alias") for w in roster if w.get("tier") == "core"}
    alerting_aliases = {w.get("alias") for w in roster
                        if w.get("alert", True) and w.get("active", True)}
    window_days = int(cfg(t, "consensus_window_days"))
    now_iso = now_utc().isoformat()

    books, adds_by_key = [], {}
    hist = st.setdefault("value_history", {})   # wallet -> cond|side -> [[ts, value]]

    for w in roster:
        wallet, alias = w.get("wallet"), w.get("alias") or "?"
        if not wallet or not w.get("active", True):
            continue
        rows = positions(http, wallet)
        if rows is None:
            books.append({"alias": alias, "wallet": wallet, "error": "unreachable"})
            continue

        pol = [p for p in rows if is_political(p.get("title") or "")]
        wh = hist.setdefault(wallet, {})
        book_rows, deltas = [], []

        seen_keys = set()
        for p in pol:
            key = f"{p.get('conditionId')}|{p.get('outcome')}"
            seen_keys.add(key)
            val = float(p.get("currentValue") or 0)
            size = float(p.get("size") or 0)
            series = trim_history(wh.get(key, []), 24 * window_days)
            prev_val = series[-1][1] if series else None
            series.append([now_iso, val])
            wh[key] = trim_history(series, 24 * window_days)

            row = {
                "market": (p.get("title") or "")[:110],
                "condition_id": p.get("conditionId"),
                "side": p.get("outcome"),
                "size": round(size, 2),
                "avg_price": p.get("avgPrice"),
                "value_usd": round(val, 2),
            }
            book_rows.append(row)

            if prev_val is not None and abs(val - prev_val) >= 1:
                deltas.append({**row, "value_before": round(prev_val, 2),
                               "value_after": round(val, 2),
                               "delta_usd": round(val - prev_val, 2)})
            if prev_val is not None and val - prev_val > 0:
                adds_by_key.setdefault(key, {"market": row["market"],
                                             "condition_id": row["condition_id"],
                                             "side": row["side"], "adders": []})
                adds_by_key[key]["adders"].append({
                    "alias": alias, "added_usd": round(val - prev_val, 2),
                    "before": round(prev_val, 2), "after": round(val, 2)})

        # Exits: keys we held last time but not now.
        for key in list(wh.keys()):
            if key not in seen_keys and wh[key]:
                last_val = wh[key][-1][1]
                if last_val and last_val > 1:
                    deltas.append({"market": key, "side": key.rsplit("|", 1)[-1],
                                   "value_before": round(last_val, 2),
                                   "value_after": 0.0, "delta_usd": round(-last_val, 2),
                                   "exit": True})
                wh[key] = trim_history(wh[key] + [[now_iso, 0.0]], 24 * window_days)

        books.append({
            "alias": alias, "wallet": wallet,
            "tier": w.get("tier") or "standard",
            "political_value_usd": round(sum(r["value_usd"] for r in book_rows), 2),
            "positions": sorted(book_rows, key=lambda r: -r["value_usd"]),
            "deltas_today": sorted(deltas, key=lambda d: -abs(d.get("delta_usd", 0)))[:40],
        })
        time.sleep(0.5)

    # ---- consensus over the trailing window --------------------------------
    consensus = []
    for key, grp in adds_by_key.items():
        # Merge with prior window adds recorded in state so consensus builds
        # across days, not just today's diff.
        prior = (st.get("window_adds") or {}).get(key, [])
        cutoff = now_utc() - timedelta(days=window_days)
        merged: dict[str, dict] = {}
        for a in prior:
            try:
                if datetime.fromisoformat(a["ts"]) >= cutoff:
                    merged[a["alias"]] = {**a}
            except (KeyError, ValueError):
                continue
        for a in grp["adders"]:
            m = merged.setdefault(a["alias"], {"alias": a["alias"], "added_usd": 0,
                                               "before": a["before"], "ts": now_iso})
            m["added_usd"] = round(m.get("added_usd", 0) + a["added_usd"], 2)
            m["after"] = a["after"]
            m["ts"] = now_iso
        st.setdefault("window_adds", {})[key] = list(merged.values())

        aligned = [m for m in merged.values() if m["added_usd"] > 0]
        g = grade(aligned, t, core_aliases, alerting_aliases)
        if g:
            consensus.append({
                "grade": g, "market": grp["market"], "side": grp["side"],
                "condition_id": grp["condition_id"],
                "wallets": len(aligned),
                "combined_usd": round(sum(a["added_usd"] for a in aligned), 2),
                "core_wallets": sum(1 for a in aligned if a["alias"] in core_aliases),
                "adders": sorted(aligned, key=lambda a: -a["added_usd"]),
                "window_days": window_days,
            })
    consensus.sort(key=lambda c: ({"HEAVY": 0, "STRONG": 1, "WATCH": 2}[c["grade"]],
                                  -c["combined_usd"]))

    book = {
        "schema": "whale-book/1",
        "generated_at": stamp()["utc"],
        "generated_at_pt": stamp()["pt"],
        "read_me": ("Full political books for the tracked roster, daily deltas, and a "
                    "consensus table. Consensus means multiple tracked wallets NET-ADDED "
                    "the same side of the same market inside the window. It is a prompt "
                    "to investigate and never a reason to copy: fills are visible only "
                    "after the price has moved, and adds are dollar-value based so a "
                    "price rally alone can look like an add -- check the size column."),
        "grades": {"WATCH": "pack only", "STRONG": "pushed + mandatory next-brief coverage",
                   "HEAVY": "high-priority push + standing-queue"},
        "consensus": consensus,
        "wallets": books,
    }

    if args.dry_run:
        print(json.dumps({k: book[k] for k in ("generated_at_pt", "consensus")}, indent=1))
        print(f"wallets: {len(books)}; consensus rows: {len(consensus)}")
        return 0

    files = {BOOK_FILE: json.dumps(book, indent=1),
             STATE_FILE: json.dumps(st, indent=1)}

    # HEAVY consensus auto-joins the standing queue, clearly machine-stamped.
    heavy = [c for c in consensus if c["grade"] == "HEAVY"]
    if heavy:
        try:
            queue = gist.read(QUEUE_FILE) or "# Standing queue\n"
            for c in heavy:
                line = (f"\n- [AUTO {stamp()['pt'][:10]}] HEAVY whale consensus: "
                        f"{c['wallets']} wallets net-added {c['side']} "
                        f"\"{c['market'][:70]}\" (${c['combined_usd']:,.0f} combined, "
                        f"{c['core_wallets']} core) -- investigate before it settles.")
                if line.strip() not in queue:
                    queue += line
            files[QUEUE_FILE] = queue
        except GistError as e:
            log.error("standing queue update failed: %s", e)

    try:
        gist.write(files)
    except GistError as e:
        print(f"gist write failed: {e}", file=sys.stderr)
        return 1

    # ---- pushes: STRONG and HEAVY only (WATCH stays in the pack) -----------
    for c in consensus:
        if c["grade"] == "WATCH" or not topic:
            continue
        adders = "\n".join(
            f"- {a['alias']}: ${a['before']:,.0f} -> ${a['after']:,.0f} "
            f"(+${a['added_usd']:,.0f})" for a in c["adders"][:6])
        body = (f"{c['market']}\nside: {c['side']} | {c['wallets']} wallets | "
                f"${c['combined_usd']:,.0f} combined over {c['window_days']}d\n{adders}"
                f"\n\nInvestigate, never copy -- fills show up after the price moved.")
        try:
            httpx.post(f"https://ntfy.sh/{topic}", content=body.encode(),
                       headers={"Title": f"{c['grade']} whale consensus: {c['side']} "
                                         f"{c['market'][:50]}".encode(),
                                "Priority": "5" if c["grade"] == "HEAVY" else "4",
                                "Tags": "whale"}, timeout=20)
        except httpx.HTTPError as e:
            log.error("ntfy failed: %s", e)

    print(f"whale-book: {len(books)} wallets, {len(consensus)} consensus rows "
          f"({sum(1 for c in consensus if c['grade'] != 'WATCH')} pushed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
