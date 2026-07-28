# market-brief-base

A daily market brief that always arrives.

This is deliberately **not** the main pipeline. `market-pipeline` produces a far
richer brief — five markets, fundamentals, news convergence, breakout quality,
forward signal tracking. It also has roughly two hundred files' worth of ways to
not run. This repo is the floor beneath it: one question — *did anything happen
in the markets I follow?* — answered even when everything else is broken.

## What it does

30 large-cap names (15 India, 15 US). For each: last **settled** close with its
bar date, day change, 50/200-day cross, and a Darvas box signal. Rendered to
self-contained HTML, optionally emailed.

## Why it exists

Every one of these happened to the main pipeline on 2026-07-21:

| failure | how this repo removes it |
|---|---|
| Mac asleep at 00:30, scheduled job never fired | GitHub Actions runs on GitHub's machines |
| `python3` resolved from the caller's PATH, so scheduled and manual runs used different interpreters and different packages | `run.sh` resolves its own venv; CI pins the same `requirements.txt` |
| Two copies of every data store silently diverged; a run could read week-old prices with no error | no cache at all — every run fetches fresh |
| macOS TCC denies launchd access to `~/Downloads`, where half the data lived | nothing outside this directory |
| screener.in hard-blocked the machine for an hour, and the validator depends on it | yfinance only |
| `.fillna(0)` on a close made 2,472 of 2,480 stocks report BREAKDOWN_SELL | no zero-filling anywhere; a missing bar is `NO_DATA` |
| Headings set to `#333` on an implicit white page vanished in dark-mode clients | colours declared for both schemes |

## Run it

```bash
./run.sh                 # writes brief.html
./run.sh --email         # also sends (needs .env)
./run.sh --self-test     # offline; verifies the signal logic
```

`run.sh` creates the venv and installs pinned deps on first use.

## Email

Copy `.env.example` to `.env` (gitignored) and fill it in.

> **Use a NEW Gmail app password.** The one in the main pipeline's history was
> committed to a public repo and reached six branches. Untracking it stopped
> further leakage but the exposure is permanent — that password needs revoking,
> and must not be reused here.

## GitHub Actions

`.github/workflows/daily-brief.yml` runs 01:00 UTC on weekdays, plus a manual
**Run workflow** button.

Set these under *Settings → Secrets and variables → Actions*:

`SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `SMTP_TO`

Without them the workflow still builds the brief and uploads it as an artifact;
it just skips the send. That is intentional — a missing secret should degrade
delivery, not fail the run.

**Keep this repo private if you set email secrets.** GitHub masks secrets in
logs, but a private repo is the right default for anything holding credentials.

## Local schedule

```bash
cp com.umashankar.basebrief.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.umashankar.basebrief.plist
launchctl print gui/$(id -u)/com.umashankar.basebrief | grep -E 'runs =|last exit'
```

Check `runs =`, not `launchctl list` — a job can be loaded and have never fired.
That distinction hid a dead job in the main pipeline for days.

**Both schedulers firing means two briefs a day.** That is the intended trade:
the point of a base version is that it always arrives, and de-duplicating across
two independent machines would reintroduce shared state — the thing this repo
exists to avoid.

## Design rules

1. **No local state.** No cache to go stale, no second copy to diverge from.
2. **No source that can block you.** yfinance only.
3. **Identical in both places.** Same `python brief.py`, same pinned deps.
4. **Fail soft, never silent.** A market that fails is named in the output. If
   *nothing* fetches, it exits non-zero rather than sending an empty brief that
   reads like a quiet market.
5. **A missing price is not a price.** No `.fillna(0)`, ever.
6. **Every number carries its date.**

## What it deliberately does not do

No fundamentals, no news, no watchlist, no forward tracking, no recommendations.
Those live in the main pipeline. Adding them here would recreate the fragility
this exists to be free of.

Educational / research only — **not investment advice.**

<!-- 
DATA LIBRARY LINK - Add this section to every repo README.md
This snippet provides discovery and documentation links.
-->

## 📊 Data Discovery

This repository is part of the **Global Data Library** — a unified catalog of 10,528 datasets across 40+ repositories.

### Quick Links

- **[Global Data Library README](.ruflo/DATA_LIBRARY_README.md)** — Full catalog, search API, and usage examples
- **[Data Library Python Interface](.ruflo/data-library/data_library.py)** — Query datasets programmatically
- **[Repository Scanner](.ruflo/data-library/repo_scanner.py)** — Reindex all repos to update the catalog

### Datasets in This Repository

The data catalog automatically inventories all datasets in this repo. To find your data:

```python
from data_library import DataLibrary

lib = DataLibrary()

# Search this repo's datasets
results = lib.search("", source="<repo-name>")

# Get dataset details
dataset = lib.get("<dataset_id>")
print(f"Rows: {dataset['row_count']}")
print(f"Freshness: {dataset['freshness_hours']} hours old")
print(f"Storage: {dataset['storage_tier']}")
```

### Browse the Full Catalog

**Market Coverage** (5 markets, 21,279 symbols):
- India (NSE/BSE): 2,364 instruments
- US (NASDAQ/NYSE): 7,442 instruments
- Europe (17 exchanges): 1,214 instruments
- Japan (TSE): 3,709 instruments
- Korea (KRX): 2,768 instruments

**Government Sources** (30+ ministries):
- MOSPI: 25 datasets (GDP, CPI, trade, agri, power)
- SEBI: 151,928 XBRL results + IPO pipeline
- PIB: 25+ ministry announcements
- DGFT: India trade data (monthly)
- Agmarknet: 300+ mandi prices (daily)
- NSE/MCX: Real-time derivatives chains

See [Global Data Library README](.ruflo/DATA_LIBRARY_README.md) for complete documentation.

### Finding Data Across All Repos

```python
# Find India OHLCV data (might be in multiple repos)
lib.search("india ohlcv", market="india")

# Get the fastest/freshest version
optimal = lib.get_optimal("india ohlcv", latency="<100ms", freshness="<1day")
# Returns: {"storage_tier": "cassandra", "path": "..."}

# Check data gaps
gaps = lib.gaps("india", date_from="2026-01-01")

# See which collectors are stale
status = lib.collectors_status()
```

---
