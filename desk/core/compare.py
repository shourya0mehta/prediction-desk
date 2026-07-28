"""Derived metrics: cross-venue gaps, volume spikes, deltas, marked P&L.

The cross-venue number here is explicitly NOT an arbitrage. The owner cannot
trade the international Polymarket book. Measured at build time, his Polymarket
US fills sat ~10 cents from the international book on MN-Gov. So the gap is a
divergence signal -- one book repricing on information the other has not
absorbed -- and the alert fires on movement away from the market's own trailing
median gap, not on absolute level, because segregated books carry persistent
structural spreads that would otherwise alert forever.
"""

from __future__ import annotations

from decimal import Decimal

from .books import D, breakeven, fee_for
from .state import median

CENT = Decimal("0.01")


def _cents(x: Decimal | None) -> float | None:
    return float(x * 100) if x is not None else None


def cross_venue_gap(kalshi_block: dict | None, pm_block: dict | None) -> dict | None:
    """Signed gap in cents between the two books' mids (Kalshi minus intl PM)."""
    if not kalshi_block or not pm_block:
        return None
    k_mid, p_mid = kalshi_block.get("mid"), pm_block.get("mid")
    if k_mid is None or p_mid is None:
        return None
    gap = (D(k_mid) - D(p_mid)) * 100
    return {
        "kalshi_mid": k_mid,
        "intl_mid": p_mid,
        "gap_cents": float(gap.quantize(Decimal("0.01"))),
        "executable": False,
        "note": "signal only -- the international book is not tradeable by the owner",
    }


def gap_dislocation(gap_cents: float, history: list) -> dict:
    """How far the current gap sits from its own trailing median."""
    med = median([g for _, g in history or []])
    if med is None:
        return {"median_gap_cents": None, "dislocation_cents": None, "samples": 0}
    return {
        "median_gap_cents": round(med, 2),
        "dislocation_cents": round(gap_cents - med, 2),
        "samples": len(history or []),
    }


def volume_spike(volume_24h, last_volume_24h) -> dict | None:
    """Interval volume against the trailing hourly median implied by 24h volume.

    Kalshi exposes cumulative volume_24h, so the increment between polls is the
    interval volume. Comparing that to the 24h average per half-hour interval
    gives a usable multiple without storing a full tape.
    """
    v_now, v_prev = D(volume_24h), D(last_volume_24h)
    if v_prev <= 0 or v_now <= 0:
        return None
    interval = v_now - v_prev
    if interval <= 0:
        return None
    # 48 half-hour intervals in 24h.
    baseline = v_now / 48
    if baseline <= 0:
        return None
    return {
        "interval_volume": str(interval.quantize(Decimal("0.01"))),
        "baseline_per_interval": str(baseline.quantize(Decimal("0.01"))),
        "multiple": round(float(interval / baseline), 2),
    }


def delta_cents(current, previous) -> float | None:
    if current is None or previous is None:
        return None
    return float(((D(current) - D(previous)) * 100).quantize(Decimal("0.01")))


def orphan_positions(ledger: list, watchlist: list) -> list[dict]:
    """Ledger rows whose race has no watchlist entry.

    An orphan is a real position the pipeline is blind to: no quote, no book, no
    executable price, no news feed, no catalyst countdown. Silently dropping it
    from the brief is the worst outcome, because the analyst would produce a
    confident-looking exposure dashboard that is missing money the owner
    actually has at risk.

    So the position is still carried in the snapshot (marked to nothing), and
    this list tells the analyst exactly which rows to research from the ledger's
    own ``market_title`` instead of from market data.
    """
    tracked_tags = {r.get("race_tag") for r in watchlist or [] if r.get("race_tag")}
    tracked_ids = {r.get("id") for r in watchlist or [] if r.get("id")}

    out = []
    for pos in ledger or []:
        tag, mid_ = pos.get("race_tag"), pos.get("market_id")
        if (tag and tag in tracked_tags) or (mid_ and mid_ in tracked_ids):
            continue
        out.append({
            "market_id": mid_,
            "race_tag": tag,
            "market_title": pos.get("market_title"),
            "venue": pos.get("venue"),
            "side": pos.get("side"),
            "shares": str(D(pos.get("shares"))),
            "avg_price_cents": pos.get("avg_price_cents"),
            "resolution_date": pos.get("resolution_date"),
            "why": ("no watchlist entry for this race, so the pipeline has no quote, "
                    "no executable price, no news feed and no catalyst countdown "
                    "for it"),
            "what_to_do": ("research this position from its market_title and the "
                           "ledger's thesis; add a watchlist block via the add-race "
                           "workflow to bring it back into the pipeline"),
        })
    return out


def mark_positions(ledger: list, markets_by_id: dict) -> tuple[list, str]:
    """Mark the ledger to market and state the price basis honestly.

    Kalshi rows mark against the executable sell-clip price. Polymarket US rows
    have no readable venue -- his account is intermediated and exposes no wallet
    -- so they are marked against the international book and clearly labelled as
    a reference mark, not a real one.
    """
    marked = []
    used_reference = False

    for pos in ledger or []:
        mid_ = pos.get("market_id")
        snap = markets_by_id.get(mid_) or {}
        venue = pos.get("venue")
        shares = D(pos.get("shares"))
        entry = D(pos.get("avg_price_cents")) / 100
        side = (pos.get("side") or "YES").upper()

        kal = (snap.get("venue_data") or {}).get("kalshi")
        pm = (snap.get("venue_data") or {}).get("polymarket")

        mark, basis, thin = None, None, None
        if venue == "kalshi" and kal:
            ex = kal.get("executable") or {}
            sell = ex.get("sell_clip_vwap") or {}
            raw = sell.get("vwap") or kal.get("mid")
            if raw is not None:
                mark = D(raw) if side == "YES" else (Decimal("1") - D(raw))
                basis = "kalshi_executable"
                thin = bool(ex.get("thin"))
        elif venue in ("pm-us", "polymarket-us"):
            used_reference = True
            src = pm or kal
            if src and src.get("mid") is not None:
                mark = D(src["mid"]) if side == "YES" else (Decimal("1") - D(src["mid"]))
                basis = "intl_reference" if src is pm else "kalshi_cross_reference"

        row = {
            "market_id": mid_,
            "venue": venue,
            "side": side,
            "shares": str(shares),
            "avg_price": str(entry),
            "mark": str(mark.quantize(Decimal("0.0001"))) if mark is not None else None,
            "price_basis": basis,
            "thin_book": thin,
        }

        if mark is not None and shares > 0:
            cost = shares * entry
            value = shares * mark
            row["cost_basis"] = str(cost.quantize(CENT))
            row["market_value"] = str(value.quantize(CENT))
            row["unrealised_pnl"] = str((value - cost).quantize(CENT))
            row["unrealised_pct"] = round(float((value - cost) / cost * 100), 2) if cost else None

        if venue == "kalshi":
            be = breakeven(entry, venue="kalshi", contracts=int(shares) or 1)
            if be is not None:
                row["settlement_breakeven"] = str(be)
        marked.append(row)

    basis = "mixed:kalshi_executable+intl_reference" if used_reference else "kalshi_executable"
    return marked, basis


def worked_fee_line(venue: str, contracts, price) -> str | None:
    """One human-readable fee sentence, for alert bodies and the brief."""
    try:
        fee = fee_for(venue, contracts, price)
    except ValueError:
        return None
    p = D(price)
    per = (fee / D(contracts)) if D(contracts) else Decimal("0")
    if venue == "kalshi":
        formula = f"ceil(0.07 x {contracts} x {p} x {1 - p:.4f})"
    else:
        formula = f"0.06 x {contracts} x {p} x {1 - p:.4f}"
    return (f"{formula} = ${fee} total, about {per * 100:.2f}c per contract")
