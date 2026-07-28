# prediction-desk-v1

An always-on, cloud-hosted, **read-only** monitor for prediction-market positions
on Kalshi and Polymarket. It pushes a phone alert when something tradeable
happens, and publishes a machine-readable `snapshot.json` that a twice-daily
Claude analyst consumes.

Runs entirely on GitHub Actions against a secret gist. **$0/month**, and it does
not depend on anyone's laptop being awake.

---

## The one thing to understand before reading the code

**This system cannot trade.** Not "is configured not to" — cannot.

The only credential it holds is a GitHub personal access token scoped to `gist`.
That token can edit a gist and do nothing else. There are no Kalshi API keys, no
wallet private keys, no exchange authentication of any kind, and no code path
that signs an exchange request. Every market endpoint used is public and
unauthenticated.

A CI check (`.github/workflows/tests.yml`) fails the build if a
trading-credential-shaped string ever appears in the repo.

Two further standing rules:

- **No scraping behind logins.** RSS, public APIs and public pages only. X/Twitter
  is never read; when a market moves with no news attached, the alert says so and
  tells you to go look yourself.
- **Fail loud.** Every failure path ends in an ntfy push. A silent stall is a bug.

---

## Layout

```
main.py                     load config -> poll -> derive -> alert -> publish
desk/pollers/kalshi.py      public REST market data
desk/pollers/polymarket.py  gamma + CLOB + data-api (international = reference only)
desk/pollers/feeds.py       RSS/Atom watchers
desk/core/books.py          book walking, VWAP at clip, fee math
desk/core/compare.py        cross-venue divergence, spikes, deltas, marked P&L
desk/core/alerts.py         trigger evaluation, cooldowns, quiet hours, ntfy
desk/core/snapshot.py       snapshot.json assembly, validation, gist publish
desk/core/state.py          gist I/O, prior-snapshot diffing, brief baselines
config/thresholds.yaml      non-sensitive defaults (gist copy overrides)
tests/test_books.py         fixture book -> VWAP + fee assertions
```

Python 3.12. Dependencies: `httpx`, `feedparser`, `PyYAML`. No database — state
is a JSON file in the gist.

---

## Setup

1. **Secrets.** Repo → Settings → Secrets and variables → Actions:
   - `GIST_TOKEN` — a classic PAT with **only** the `gist` scope.
   - `GIST_ID` — the secret gist's ID.
   - `NTFY_TOPIC` — an unguessable string, e.g. `desk-<random12>`.
2. **Phone.** Install ntfy, subscribe to that topic.
3. **Analyst tasks.** Two scheduled tasks (07:30 and 17:30 PT) using
   `analyst-task-prompt.md` with the gist raw URLs filled in.

**News coverage needs no setup.** Each active race in `watchlist.yaml` carries a
`candidates` list, and the pipeline derives one Google News RSS search per race
from it — the names OR'd as quoted phrases — merged with the static feeds in
`feeds.yaml`. `GOOGLE-ALERTS.md` in the gist is now an **optional extra**: the
derived feeds cover the same ground automatically, so only add Google Alerts if
you want a second, differently-ranked source for a specific candidate.

Nothing needs redeploying when you edit the gist — the next poll picks it up.

---

## Running it locally

```bash
pip install -r requirements.txt
GIST_TOKEN=... GIST_ID=... python main.py --dry-run
```

`--dry-run` executes the whole pipeline, prints the alerts it *would* send, and
writes nothing to the gist. Other flags: `--universe` (also refresh
`universe.json`), `--election-night` (2¢ threshold, quiet hours bypassed),
`--selftest` (heartbeat).

---

## Ops runbook

### Add a race
Add one block to `watchlist.yaml` and a `primer-<race_tag>.md` stub. The analyst
writes the primer on its next run and flags it for approval. `resolution_date` is
required — see the warning below.

**News feeds are automatic.** Fill the block's `candidates` list and the race
gets its own Google News feed on the next poll, pre-tagged with the race tag —
there is nothing to click and no Google Alert to create. Include the main
rivals, not just our candidate: news about the opponent moves the price just as
much. Prefer distinctive names — the WA-09 incumbent is Adam Smith, and
including him returned stories about the economist and a UK footballer, so he is
deliberately left out of that race's list.

### Update the ledger (either venue)
Send one line:

```
LEDGER: bought 150 mi-sen-dem-elsayed YES @ 46.3 on Kalshi, 2026-07-28
LEDGER: sold 70 mn-gov-gop-lindell YES @ 62 on PM-US, 2026-07-28
```

applied to `positions-ledger.json` in about thirty seconds. The ledger is the
single source of truth for positions on both venues: Kalshi has no read-only API
scope, and Polymarket US accounts are intermediated with no user-visible wallet,
so **neither book is publicly readable**. Nothing auto-syncs.

### Retire a race
Set `active: false`. The primer stays archived.

### Execution surface
Kalshi Pro is the standing recommendation for acting on alerts: batch-cancel
resting orders in one click when a catalyst countdown fires, keep a saved
election-night layout per settlement date (Aug 4, Aug 11), and read the
per-market tape to tell a dump from a rotation before reacting to any price move.

### Election nights
Fire `election_night.yml` manually on the evenings of **Tue Aug 4** and **Tue Aug
11, 2026**. One job loops internally rather than relying on cron, which cannot go
below five minutes.

---

## Things that will bite you

**Kalshi's `close_time` is not the resolution date.** The WA boards report
`2027-11-03` for markets settling at the Aug 4, 2026 primary. Countdowns run off
`watchlist.yaml`'s `resolution_date` and must keep doing so.

**Kalshi returns bids only, for both sides.** YES asks are implied from NO bids
(`yes_ask = 1 − best_no_bid`). `books.normalise_kalshi` handles it; verified
against the live API.

**Prices are decimal-dollar strings, not integer cents.** Fields are
`yes_bid_dollars: "0.7400"`, `count_fp`, `volume_24h_fp`. Political boards use
`tapered_deci_cent` ticks — 0.001 steps below 10¢ and above 90¢ — so an
int-cents representation silently truncates real levels. Everything here is
`Decimal`.

**The Polymarket book in this feed is not the one you trade.** You are on
Polymarket US; these APIs serve the *international* exchange. Measured at build
time: MN-Gov Lindell was 0.61/0.62 on Kalshi, 0.635 international, and **0.53**
on your PM-US ledger — a ten-cent gap. Every Polymarket price here is tagged
`intl_reference` and is a leading indicator, never a quote you can hit. The decoy
wallet `0x9c2d…350b` is refused in code.

**`universe.json` lists markets that traded in the last 24 hours,** not every
open market. The full political board is ~13,300 markets and 4.35 MB, of which
~11,500 have never traded and include placeholders dated 2099. The sweep keeps
the ~1,800 with real 24h volume, which is 0.70 MB and greppable in one fetch.
`sweep_stats` in the file reports exactly what was dropped. If you are hunting a
specific dormant market, query the Kalshi API directly rather than concluding it
does not exist.

**The analyst reads GitHub Pages, not the gist.** The scheduled-task sandbox
cannot reach `gist.githubusercontent.com` at all, and its fallback route
truncates large files mid-document — the first cloud brief lost everything after
the WA-05 block. Every poll republishes the analyst-facing files to Pages under
an unguessable `/d/<random32>/` prefix (the `PAGES_PREFIX` secret). The gist
remains internal state and config; the token model is unchanged. `robots.txt`
disallows everything and HTML carries `noindex` — note that GitHub Pages cannot
set custom HTTP headers, so `X-Robots-Tag` is not available and the prefix is
the real control, exactly as with the secret gist.

**Google News rate-limits bursts.** Eleven derived feeds fired 0.4–1.1s apart
returned HTTP 503 on every one from an Actions runner, while a single request
from the same runner got 200 — rate limiting, not an IP block. Fetches are now
spaced 3.5–6s when consecutive requests hit the same host, and 429/503 back off
8/20/35s. Election-night mode skips the derived news feeds entirely: they need
~60s of spacing and the poll interval is 150s.

**Gist writes can collide.** The analyst task appends its brief to the same
gist the poll publishes into, and GitHub answers `409 Gist cannot be updated`
when two writers overlap — seen in production at 2:44 PM. `Gist.write` retries
three times with a 5–15s jittered backoff, which is long on purpose: two writers
retrying in lockstep would just collide again. The two writers touch different
filenames, so a PATCH merges cleanly and there is no clobbering risk — the 409 is
lock contention, not a data conflict. If 409s start surviving all three attempts,
that is the signal to move briefs into their own gist.

**The cross-venue number is not an arbitrage.** You cannot trade the
international book. It fires on movement away from that market's own 7-day median
gap, because segregated books carry persistent structural spreads that would
otherwise alert forever.

---

## Fee verification

Both venues' formulas were checked against primary sources and live data on
2026-07-28, because both had changed recently.

**Kalshi:** `fee = ceil(0.07 × C × P × (1−P) × 100) / 100` — rounded **up to the
next cent at the order level**, not per contract. Maker is 25% of taker.

The order-level detail matters and is easy to get wrong, so it was tested against
real fills from the ledger:

| Position | Notional | Paid | Implied fee | Order-level ceil | Per-contract ceil |
|---|---|---|---|---|---|
| WA-05 NO Powell | $100.27 | $102.27 | $2.00 | **$2.00 exact** | $2.80 |
| WI-Gov Hong | $48.19 | $50.00 | $1.81 | $1.82 | $2.08 |
| MI-Sen El-Sayed | $148.72 | $150.00 | $1.28 | $1.25 | $1.69 |

Per-contract rounding overshoots every row by 30–40%. Order-level reproduces the
WA-05 fill to the cent.

**Polymarket US** (docs.polymarket.us/fees, effective 2026-07-01): taker
`0.06 × C × p × (1−p)` capped at $1.50 per 100 at 50¢; maker is a **rebate**,
`−0.0125 × C × p × (1−p)`. Rounded to the nearest cent with **banker's rounding**.
The sign flip is real and the comparison layer accounts for it: Polymarket US
pays makers, Kalshi charges them.

**Breakevens are settlement breakevens** — entry price plus the entry fee only.
Neither venue charges a fee at settlement, and almost every position here is
held to settlement, so a round-trip breakeven would overstate the bar for taking
profit by roughly another entry fee (~1.8¢ on a 50¢ contract). `books.breakeven`
takes `round_trip=True` when you really are selling early.

---

## Testing

```bash
python -m pytest tests/ -v
```

31 tests covering book normalisation (including the bids-only inversion and
empty/partial books), clip walks against a real captured fixture, the thin-book
flag, both venues' fee schedules — including the banker's-rounding cases and the
maker sign flip — and a regression for each bug found by running the pipeline
against live data. `tests/test_books.py`'s module docstring carries the fully
worked 150-contract example.

CI additionally fails the build if a trading-credential-shaped string, or the
decoy wallet, ever appears in a config file.
