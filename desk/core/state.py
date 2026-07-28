"""Gist-backed config loading and state persistence. No database.

The only credential in this system is GIST_TOKEN: a PAT with the ``gist`` scope
and nothing else. It can read and write gists. It cannot touch a repo, an
exchange, or a dollar. That is the whole security model and it is deliberate.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import yaml

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")

# Brief boundaries in PT. delta_since_last_brief resets across these, which keeps
# Layer 2 fully read-only -- the analyst never has to write a marker file.
BRIEF_HOURS_PT = (7, 30), (17, 30)


class GistError(RuntimeError):
    pass


class Gist:
    def __init__(self, gist_id: str, token: str, client: httpx.Client | None = None):
        if not gist_id:
            raise GistError("GIST_ID is not set")
        if not token:
            raise GistError("GIST_TOKEN is not set")
        self.gist_id = gist_id
        self.client = client or httpx.Client(timeout=30)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._cache: dict[str, str] | None = None

    def _load(self, force: bool = False) -> dict[str, str]:
        if self._cache is not None and not force:
            return self._cache
        r = self.client.get(f"{GITHUB_API}/gists/{self.gist_id}", headers=self.headers)
        if r.status_code != 200:
            raise GistError(f"read gist {self.gist_id}: HTTP {r.status_code} {r.text[:200]}")
        files = r.json().get("files", {}) or {}
        out = {}
        for name, meta in files.items():
            if meta.get("truncated") and meta.get("raw_url"):
                rr = self.client.get(meta["raw_url"], headers=self.headers)
                out[name] = rr.text if rr.status_code == 200 else ""
            else:
                out[name] = meta.get("content") or ""
        self._cache = out
        return out

    def read(self, name: str, default: str | None = None) -> str | None:
        return self._load().get(name, default)

    def read_yaml(self, name: str, default=None):
        raw = self.read(name)
        if raw is None:
            return default
        try:
            return yaml.safe_load(raw) or default
        except yaml.YAMLError as e:
            raise GistError(f"{name} is not valid YAML: {e}") from e

    def read_json(self, name: str, default=None):
        raw = self.read(name)
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise GistError(f"{name} is not valid JSON: {e}") from e

    def write(self, files: dict[str, str]) -> None:
        """Patch one or more files. GitHub replaces content wholesale."""
        payload = {"files": {n: {"content": c} for n, c in files.items()}}
        r = self.client.patch(f"{GITHUB_API}/gists/{self.gist_id}",
                              headers=self.headers, json=payload)
        if r.status_code not in (200, 201):
            raise GistError(f"write gist: HTTP {r.status_code} {r.text[:300]}")
        if self._cache is not None:
            self._cache.update(files)


# --------------------------------------------------------------------- time

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(dt: datetime | None = None) -> dict:
    """Timestamps in both zones. Markets are ET-centric; the owner is on PT."""
    dt = dt or now_utc()
    return {
        "utc": dt.isoformat(),
        "pt": dt.astimezone(PT).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "et": dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


def last_brief_boundary(dt: datetime | None = None) -> datetime:
    """The most recent 07:30 or 17:30 PT boundary at or before ``dt``."""
    dt = (dt or now_utc()).astimezone(PT)
    today = dt.date()
    candidates = []
    for days in (0, 1):
        d = today - timedelta(days=days)
        for h, m in BRIEF_HOURS_PT:
            candidates.append(datetime(d.year, d.month, d.day, h, m, tzinfo=PT))
    past = [c for c in candidates if c <= dt]
    return max(past).astimezone(timezone.utc)


def in_quiet_hours(dt: datetime | None = None) -> bool:
    """23:00-07:00 PT."""
    h = (dt or now_utc()).astimezone(PT).hour
    return h >= 23 or h < 7


# -------------------------------------------------------------------- state

STATE_FILE = "pipeline_state.json"


def empty_state() -> dict:
    return {
        "version": 2,
        "last_run": None,
        "brief_baseline_at": None,
        "markets": {},        # id -> {mid, volume_24h, ...} at last poll
        "brief_baseline": {},  # id -> mid at last brief boundary
        "alerts": {},         # "market|trigger" -> {last_sent, level}
        "whales": {},         # wallet -> {condition_id -> size}
        "gap_history": {},    # id -> [[iso, gap_cents], ...] trailing 7d
        "print_history": {},  # ticker -> [[iso, notional], ...] trailing 24h
        "seen_market_ids": [],
        "alerts_since_last_brief": [],
    }


def load_state(gist: Gist) -> dict:
    st = gist.read_json(STATE_FILE)
    if not isinstance(st, dict) or st.get("version") != 2:
        log.warning("no usable prior state; starting fresh")
        return empty_state()
    base = empty_state()
    base.update(st)
    return base


def save_state(gist: Gist, state: dict) -> None:
    gist.write({STATE_FILE: json.dumps(state, indent=1, sort_keys=True)})


def trim_history(rows: list, hours: int, now: datetime | None = None) -> list:
    cutoff = (now or now_utc()) - timedelta(hours=hours)
    out = []
    for ts, val in rows or []:
        try:
            when = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            out.append([ts, val])
    return out


def median(values: list) -> float | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2
