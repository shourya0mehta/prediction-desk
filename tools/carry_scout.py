#!/usr/bin/env python3
"""Copy the last published scout pack from the gist onto the Pages mirror.

Pages is republished wholesale on every poll, so a file only written by the
daily scout run would 404 for the analyst on the other ~47 runs. universe.json
hit exactly this and was invisible for a day.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk.core.state import Gist, GistError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-site", required=True)
    args = ap.parse_args()

    prefix = os.environ.get("PAGES_PREFIX", "")
    if not prefix:
        print("PAGES_PREFIX unset", file=sys.stderr)
        return 1

    gist = Gist(os.environ.get("GIST_ID", ""), os.environ.get("GIST_TOKEN", ""),
                client=httpx.Client(timeout=30))
    try:
        raw = gist.read("scout-pack.json")
    except GistError as e:
        print(f"could not read the scout pack: {e}", file=sys.stderr)
        return 0  # never fail a healthy poll over this

    if not raw:
        print("no scout pack to carry forward yet")
        return 0

    out_dir = Path(args.emit_site) / "d" / prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "scout-pack.json").write_text(raw, encoding="utf-8")

    # Rebuild the per-date slices from the carried pack so those do not 404 either.
    try:
        pack = json.loads(raw)
    except json.JSONDecodeError:
        print("carried pack is not valid JSON; wrote it verbatim anyway")
        return 0

    races = pack.get("races") or []
    dates = sorted({r.get("resolution_date") for r in races if r.get("resolution_date")})
    for d in dates:
        rows = [r for r in races if r.get("resolution_date") == d]
        (out_dir / f"scout-pack-{d}.json").write_text(json.dumps({
            **{k: pack[k] for k in ("schema", "generated_at", "generated_at_pt",
                                    "bankroll_for_sizing_usd", "read_me", "scope", "fec")
               if k in pack},
            "batch_date": d, "race_count": len(rows),
            "tradeable_count": sum(1 for r in rows if r.get("tradeable")),
            "races": rows,
        }, indent=1), encoding="utf-8")
    print(f"carried scout pack forward ({len(races)} races, {len(dates)} slices)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
