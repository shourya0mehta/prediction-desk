#!/usr/bin/env python3
"""Screen whale-roster candidates against the B2 admission criteria.

    python tools/screen_whales.py            # screen the round-7 candidate list
    python tools/screen_whales.py 0xabc...   # screen specific wallets

Criteria (all verified via the public Data API, no keys):
  * politics >= ~40% of the open book by $ value
  * positive P&L -- verified as realized + unrealized on the CURRENT book, and
    lifetime leaderboard presence where it exists. A true 30-day P&L is not
    exposed by any public endpoint; what Polycopy shows comes from their own
    indexing. The evidence column says exactly which proxy was used.
  * >= 20 resolved trades (REDEEM events on the activity feed, counted to 100)
  * >= 6 months tenure (deep-offset probe of the activity feed, which is
    newest-first: a trade at a deep offset older than 6 months proves tenure
    without paging a whale's entire history)
  * not a two-sided market-maker book (holding BOTH outcomes of the same
    market across a meaningful share of the book)

Output: one evidence row per wallet plus a proposed core|standard|grinder tag,
printed for the owner's one-word approval. Nothing is written anywhere.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk.pollers.polymarket import is_political  # noqa: E402

DATA = "https://data-api.polymarket.com"

CANDIDATES = [
    # --- core seeds (named, owner-specified) ---
    ("Domer",         "0x9d84ce0306f8551e02efef1680475fc0f1dc1344", "core-seed"),
    ("aenews2",       "0x44c1dfe43260c94ed4f1d00de2e1f80fb113ebc1", "core-seed"),
    # --- Polycopy politics-30d leaderboard, named ---
    ("qwrasdz",       "0xd43cf929909540b714c054b97d7c53d9f31004e8", "polycopy"),
    ("chachakk",      "0x88be0553c9de90db290d925c51a8f1062be2652d", "polycopy"),
    ("wan123",        "0xde7be6d489bce070a959e0cb813128ae659b5f4b", "polycopy"),
    ("Noble-Factory", "0xdf17f4a8dd01a4cfa6fc3da323a2baee5f8697d1", "polycopy"),
    # --- Polycopy anonymous rows ---
    ("anon-23d8", "0x23d81ba9371e576015c1e562db09c689f56b0288", "polycopy-anon"),
    ("anon-36ca", "0x36cacb8b6626e8cb275e45612d60e0c814531067", "polycopy-anon"),
    ("anon-c12b", "0xc12bb7ca6d79a3cfce0c8686ed5ecb82f418af60", "polycopy-anon"),
    ("anon-9bc6", "0x9bc60ced1a6542ed577b17f28398d7e454764f74", "polycopy-anon"),
    ("anon-3029", "0x3029039cbeb17bdfe366020dcf086e4feb200805", "polycopy-anon"),
    ("anon-6ba3", "0x6ba31e0627b6bb015ef1c8533e33f25a43c017bf", "polycopy-anon"),
    # --- volume leaders needing a political-share cross-check ---
    ("swisstony",     "0x204f72f35326db932158cba6adff0b9a1da95e14", "crosscheck"),
    ("DEEDDIT",       "0x09b428f7c2b469786286214aa5c90dd9015f7320", "crosscheck"),
    ("asparagus2012", "0x476e1322d1a412fa0325527b8c3bc5e707b1396d", "crosscheck"),
    # --- kept from the build-time screen (already on the roster) ---
    ("debased",       "0x24c8cf69a0e0a17eee21f69d29752bfa32e823e1", "build-roster"),
    ("gloriafoster",  "0x5d189e816b4149be00977c1a3c8840374aec4972", "build-roster"),
    ("Q96s3kwozynxpau", "0x2663daca3cecf3767ca1c3b126002a8578a8ed1f", "build-roster"),
    ("risk-manager",  "0xa61ef8773ec2e821962306ca87d4b57e39ff0abd", "build-roster"),
    ("cigarettes",    "0xd218e474776403a330142299f7796e8ba32eb5c9", "build-roster"),
]

SIX_MONTHS = timedelta(days=183)


def get(client, path, **params):
    try:
        r = client.get(f"{DATA}{path}", params=params)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError:
        return None


def tenure_months(client, wallet) -> float | None:
    """First activity, directly: the feed supports sortDirection=ASC.

    (The first version probed fixed deep offsets on the newest-first feed and
    called Domer -- trading since January 2022 -- one month old, because a
    heavy trader's offset 3000 is still last week.)"""
    rows = get(client, "/activity", user=wallet, limit=1, sortDirection="ASC")
    if not rows or not rows[0].get("timestamp"):
        return None
    oldest = datetime.fromtimestamp(int(rows[0]["timestamp"]), tz=timezone.utc)
    return (datetime.now(timezone.utc) - oldest).days / 30.44


def pnl(client, wallet) -> tuple[float | None, float | None]:
    """Exact per-wallet P&L: lb-api /profit takes an address parameter."""
    out = []
    for window in ("30d", "all"):
        rows = None
        try:
            r = client.get("https://lb-api.polymarket.com/profit",
                           params={"window": window, "address": wallet})
            r.raise_for_status()
            rows = r.json()
        except httpx.HTTPError:
            pass
        out.append(float(rows[0]["amount"]) if rows else None)
        time.sleep(0.2)
    return out[0], out[1]


def recent_politics_share(client, wallet) -> float | None:
    """Politics share of the last 100 trades -- covers burst traders whose
    current book is empty between races."""
    rows = get(client, "/activity", user=wallet, limit=100, type="TRADE")
    if not rows:
        return None
    pol = sum(1 for r in rows if is_political(r.get("title") or ""))
    return pol / len(rows)


def screen(client, alias, wallet, source) -> dict:
    pos = get(client, "/positions", user=wallet, limit=500,
              sortBy="CURRENT", sortDirection="DESC") or []
    total = sum(float(p.get("currentValue") or 0) for p in pos)
    pol = [p for p in pos if is_political(p.get("title") or "")]
    pol_val = sum(float(p.get("currentValue") or 0) for p in pol)
    pol_share = (pol_val / total) if total > 0 else 0.0

    pnl_30d, pnl_life = pnl(client, wallet)
    act_share = recent_politics_share(client, wallet)

    # Two-sided MM check: both outcomes of one market held simultaneously.
    by_cond = {}
    for p in pos:
        by_cond.setdefault(p.get("conditionId"), set()).add(p.get("outcome"))
    two_sided = sum(1 for outs in by_cond.values() if len(outs) > 1)
    mm_share = two_sided / max(len(by_cond), 1)

    redeems = get(client, "/activity", user=wallet, limit=100, type="REDEEM") or []
    months = tenure_months(client, wallet)

    # Politics share: the better of open-book share and recent-trade share --
    # a burst trader between races has an empty book but a political tape.
    eff_pol = max(pol_share, act_share or 0.0)
    passes = {
        "politics>=40%": eff_pol >= 0.40,
        "pnl30d>0": pnl_30d is not None and pnl_30d > 0,
        "pnl_life>0": pnl_life is not None and pnl_life > 0,
        ">=20_resolved": len(redeems) >= 20,
        ">=6mo": months is not None and months >= 6,
        "not_MM": mm_share < 0.25,
    }
    verdict = all(passes.values())

    # Tier: core = the two named seeds; grinder = passing book under $25k
    # political value; standard = the rest that pass.
    if source == "core-seed":
        tier = "core"
    elif verdict and pol_val < 25_000:
        tier = "grinder"
    elif verdict:
        tier = "standard"
    else:
        tier = "-"

    return {
        "alias": alias, "wallet": wallet, "source": source,
        "book_usd": round(total), "political_usd": round(pol_val),
        "political_share_book": round(pol_share * 100),
        "political_share_recent_trades": round((act_share or 0) * 100),
        "political_positions": len(pol),
        "pnl_30d": round(pnl_30d) if pnl_30d is not None else None,
        "pnl_lifetime": round(pnl_life) if pnl_life is not None else None,
        "resolved_trades": f"{len(redeems)}{'+' if len(redeems) == 100 else ''}",
        "tenure_months": round(months, 1) if months is not None else None,
        "two_sided_share": round(mm_share * 100),
        "passes": passes, "admit": verdict, "tier": tier,
    }


def fresh_leaderboard_candidates(client, known: set) -> list:
    """New wallets from the current profit leaderboards worth screening."""
    out, seen = [], set()
    for window in ("30d", "all"):
        try:
            rows = client.get("https://lb-api.polymarket.com/profit",
                              params={"window": window, "limit": 50}).json()
        except httpx.HTTPError:
            continue
        for r in rows or []:
            w = (r.get("proxyWallet") or "").lower()
            if not w or w in known or w in seen:
                continue
            seen.add(w)
            out.append((r.get("name") or w[:10], w, f"leaderboard-{window}"))
        time.sleep(0.3)
    return out


def rescreen() -> int:
    """Monthly re-admission pass: current roster + fresh leaderboard blood.

    Prints proposed changes and writes whale-rescreen-YYYY-MM.md to the gist
    (mirrored to Pages). NEVER applies anything -- roster changes remain a
    one-word owner approval, exactly like the original screen.
    """
    import yaml
    from desk.core.state import Gist, stamp

    client = httpx.Client(timeout=30, follow_redirects=True)
    gist = Gist(os.environ.get("GIST_ID", ""), os.environ.get("GIST_TOKEN", ""))
    roster = yaml.safe_load(gist.read("whales.yaml") or "[]") or []
    known = {(w.get("wallet") or "").lower() for w in roster}

    lines = [f"# Whale rescreen — {stamp()['pt'][:10]}", "",
             "Admission criteria re-run against live data. Proposals only — "
             "nothing here is applied without owner approval.", ""]

    lines.append("## Current roster drift")
    proposals = 0
    for w in roster:
        r = screen(client, w.get("alias") or "?", w.get("wallet"), "roster")
        fails = [k for k, v in r["passes"].items() if not v]
        drift = ""
        if w.get("alert") and fails and w.get("tier") != "core":
            drift = f" -> PROPOSE demote to non-alerting (fails: {', '.join(fails)})"
            proposals += 1
        if not w.get("alert") and not fails:
            drift = " -> PROPOSE promote to alerting (all criteria pass)"
            proposals += 1
        lines.append(f"- {r['alias']}: pol {max(r['political_share_book'], r['political_share_recent_trades'])}%, "
                     f"30d {r['pnl_30d']}, life {r['pnl_lifetime']}, "
                     f"res {r['resolved_trades']}, ten {r['tenure_months']}mo{drift}")
        time.sleep(0.5)

    lines.append("")
    lines.append("## Fresh leaderboard candidates (screened, political tape >= 40%)")
    fresh = fresh_leaderboard_candidates(client, known)
    admits = 0
    for alias, wallet, source in fresh[:25]:
        r = screen(client, alias, wallet, source)
        if max(r["political_share_book"], r["political_share_recent_trades"]) < 40:
            continue
        verdict = "PROPOSE ADMIT" if r["admit"] else                   f"reject ({', '.join(k for k, v in r['passes'].items() if not v)})"
        if r["admit"]:
            admits += 1
            proposals += 1
        lines.append(f"- {alias} `{wallet}`: pol {max(r['political_share_book'], r['political_share_recent_trades'])}%, "
                     f"30d {r['pnl_30d']}, life {r['pnl_lifetime']}, res {r['resolved_trades']}, "
                     f"ten {r['tenure_months']}mo -> **{verdict}**")
        time.sleep(0.5)
    if not any(l.startswith("- ") for l in lines[lines.index("## Fresh leaderboard candidates (screened, political tape >= 40%)"):]):
        lines.append("- none met the political-share screen this month")

    report = "\n".join(lines) + "\n"
    name = f"whale-rescreen-{stamp()['pt'][:7]}.md"
    gist.write({name: report})
    print(report)
    print(f"written to gist as {name} ({proposals} proposal(s))")

    topic = os.environ.get("NTFY_TOPIC", "")
    if topic:
        httpx.post(f"https://ntfy.sh/{topic}",
                   content=(f"{proposals} proposed roster change(s), {admits} new admit(s). "
                            f"Full evidence: {name} on the mirror. Reply 'approve rescreen' "
                            f"to apply.").encode(),
                   headers={"Title": "Monthly whale rescreen ready", "Priority": "3",
                            "Tags": "whale"}, timeout=20)
    return 0


def main() -> int:
    if "--rescreen" in sys.argv:
        return rescreen()
    wallets = [a for a in sys.argv[1:] if not a.startswith("--")]
    todo = ([(w[:10], w, "cli") for w in wallets] if wallets else CANDIDATES)
    client = httpx.Client(timeout=30, follow_redirects=True)

    rows = []
    for alias, wallet, source in todo:
        r = screen(client, alias, wallet, source)
        rows.append(r)
        f = r["passes"]
        flags = "".join("Y" if v else "n" for v in f.values())
        p30 = f"{r['pnl_30d']:+,}" if r['pnl_30d'] is not None else "?"
        plf = f"{r['pnl_lifetime']:+,}" if r['pnl_lifetime'] is not None else "?"
        print(f"{r['alias'][:15]:16} {r['tier']:9} pol book {r['political_share_book']:3}%/"
              f"tape {r['political_share_recent_trades']:3}% (${r['political_usd']:>9,}) "
              f"30d {p30:>11} life {plf:>12} res {r['resolved_trades']:>4} "
              f"ten {str(r['tenure_months']):>6}mo 2s {r['two_sided_share']:2}% "
              f"[{flags}] {'ADMIT' if r['admit'] else 'reject'}")
        time.sleep(0.6)

    print()
    admitted = [r for r in rows if r["admit"] or r["source"] == "core-seed"]
    print(f"admitted: {len(admitted)} of {len(rows)} screened")
    Path("/tmp/whale_screen.json").write_text(json.dumps(rows, indent=1))
    print("full evidence written to /tmp/whale_screen.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
