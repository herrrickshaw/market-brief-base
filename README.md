# market-brief-base

<!-- how-to-use-with-ai -->
> **New here?** See [How to use this repo with AI (Claude, Gemini, ChatGPT)](HOW_TO_USE_WITH_AI.md) — clone it, paste it into an AI tool, and ask in plain English.

A daily market brief that always arrives. For 30 large-cap equities (15 India
NSE, 15 US) it reports the last **settled** close with its bar date, the day
change, the 50/200-day cross and a Darvas box signal, renders that to one
self-contained HTML file and optionally emails it. It is the deliberately
minimal floor beneath the owner's main `market-pipeline`: one file, no local
state, yfinance only, so it still runs when everything richer is broken.

> Status: active (scheduled daily via launchd and GitHub Actions).
> Prices from yfinance; educational / research only — **not investment advice**.

## What it does
- Fetches one year of daily OHLC for a hardcoded 30-name universe (`UNIVERSE` in `brief.py`) with a single `yfinance.download` call per market.
- Computes per ticker: last settled close + bar date, previous close, SMA-50, SMA-200, Darvas box (`BREAKOUT_BUY` / `BREAKDOWN_SELL` / `IN_BOX` / `NO_BOX` / `NO_DATA` / `INSUFFICIENT_DATA`).
- Renders `brief.html` with colours declared for both light and dark mail clients.
- Optionally emails the HTML over SMTP (STARTTLS) using `SMTP_*` environment variables; missing config is a logged skip, not a failure.
- Names a market that failed to fetch in a "Degraded" banner and the subject line instead of dropping it; exits non-zero only when *no* market returned data.
- Verifies its own signal logic offline with `--self-test` before every scheduled network call.

## Quick start
```bash
git clone https://github.com/herrrickshaw/market-brief-base.git
cd market-brief-base
./run.sh                 # creates .venv, installs pinned deps, writes brief.html
./run.sh --email         # also sends (needs .env, see Configuration)
./run.sh --self-test     # offline; verifies the signal logic
./run.sh --help
```

`run.sh` is the only local entry point. It resolves its own `.venv` rather than
inheriting `python3` from the caller's PATH, loads `.env` if present, then
`exec`s `./.venv/bin/python brief.py "$@"`. In CI the same `python brief.py`
runs directly against the same pinned `requirements.txt`.

## How it works

| Stage | Function (`brief.py`) | Output |
|---|---|---|
| download | `_yf_download` → `fetch` | per-ticker row `{close, date, prev, sma50, sma200, darvas}` or `{error}` |
| indicators | `sma`, `darvas`, `trend`, `day_change_pct` | pure functions; NaN is dropped, never zero-filled |
| collect | `collect(UNIVERSE)` | `results` per market + `failures` for markets that raised or had no usable row |
| render | `render`, `build_subject` | self-contained HTML, subject with "(N degraded)" when applicable |
| email | `smtp_config` → `deliver` → `send` | SMTP send, or a printed skip reason |
| CLI | `parse_args`, `main` | exit 0 brief written · 1 no market returned data · 2 brief written but delivery failed |

The network call and the SMTP client are injectable (`fetch(downloader=…)`,
`send(smtp_factory=…)`), which is how the test-suite covers every stage offline.

Design rules, each one earned by an incident in the main pipeline:

1. **No local state.** No cache to go stale, no second copy to diverge from; every run fetches fresh and writes only `brief.html`.
2. **No source that can block you.** yfinance only — screener.in, moneycontrol and NSE bhavcopy have all refused the owner's machine at least once.
3. **Identical in both places.** Same `python brief.py`, same pinned deps on the laptop and on GitHub Actions.
4. **Fail soft, never silent.** A failed market is named in the output; only an entirely empty result exits non-zero.
5. **A missing price is not a price.** No `.fillna(0)` anywhere — zero-filling once made 2,472 of 2,480 stocks report `BREAKDOWN_SELL`.
6. **Every number carries its date.** The Darvas box is formed on settled bars only, with the current bar excluded.
7. **Hardcoded universe.** Fetching a constituent list first would add a source that can be down at 00:30.

## Repository layout
| Path | Purpose |
|---|---|
| `brief.py` | The whole brief: constants → indicators → fetch → render → email → self-test → CLI |
| `run.sh` | Local entry point: fd-limit, venv bootstrap, `.env`, exec `brief.py` |
| `requirements.txt` | Runtime pins (`yfinance`, `pandas`) — installed by `run.sh` and both workflows |
| `requirements-dev.txt` | `pytest`, `ruff` on top of the runtime pins |
| `pyproject.toml` | ruff and pytest configuration only (no packaging) |
| `tests/test_brief.py` | Unit tests, no network or SMTP |
| `tests/smoke.sh` | `bash -n`, `--help`, `py_compile`, offline `--self-test` |
| `.github/workflows/daily-brief.yml` | Scheduled production run (01:00 UTC weekdays + manual) |
| `.github/workflows/ci.yml` | Lint + smoke + tests on push/PR |
| `com.umashankar.basebrief.plist` | launchd job: `run.sh --email` at 06:35 IST weekdays |
| `.env.example` | SMTP variable names; copy to `.env` (gitignored) |
| `brief.html`, `run.log`, `run.err`, `.venv/` | Generated, gitignored |
| `graphify-out/` | Generated knowledge-graph output, not hand-edited |

## Data sources
| Source | What is taken | Access route | Licence / caveat |
|---|---|---|---|
| Yahoo Finance via [`yfinance`](https://github.com/ranaroussi/yfinance) | 1y daily OHLC, `auto_adjust=False` | `yf.download(…, group_by="ticker")` | Unofficial API; terms are Yahoo's. Can rate-limit or return empty frames. |

## Configuration
All via environment variables (locally from `.env`, in CI from repository secrets). Empty and whitespace-only values are treated as unset.

| Variable | Default | Purpose |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | SMTP server |
| `SMTP_PORT` | `587` | STARTTLS port; a non-numeric value skips the send |
| `SMTP_USER` | — | login and `From:` |
| `SMTP_PASS` | — | password / app password |
| `SMTP_TO` | `SMTP_USER` | recipient(s), comma-separated |

Without `SMTP_USER`/`SMTP_PASS` the brief is still built; `--email` prints
`email: not configured (…) — skipping send` and exits 0. No secret is ever
committed: `.env` is gitignored and `.env.example` holds placeholders only.

> **Use a NEW Gmail app password.** The one in the main pipeline's history was
> committed to a public repo and reached six branches. Untracking it stopped
> further leakage but the exposure is permanent — that password needs revoking,
> and must not be reused here. Keep this repo private if you set email secrets.

### GitHub Actions
`daily-brief.yml` runs `python brief.py --self-test` then `python brief.py --email`
at 01:00 UTC on weekdays, plus a manual **Run workflow** button, and always
uploads `brief.html` as an artifact (14 days). Set `SMTP_HOST SMTP_PORT SMTP_USER
SMTP_PASS SMTP_TO` under *Settings → Secrets and variables → Actions*. Without
them the workflow still builds and uploads the brief.

### Local schedule (launchd)
```bash
cp com.umashankar.basebrief.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.umashankar.basebrief.plist
launchctl print gui/$(id -u)/com.umashankar.basebrief | grep -E 'runs =|last exit'
```
Check `runs =`, not `launchctl list` — a job can be loaded and have never fired.
The plist deliberately sets no PATH; `run.sh` raises the launchd 256-descriptor
soft limit itself because yfinance holds a socket per in-flight request.

## Testing
```bash
pip install -r requirements-dev.txt
ruff check .
bash tests/smoke.sh        # bash -n, --help paths, py_compile, offline self-test
pytest -q                  # 43 tests, no network
```

## Limitations and known issues
- 🔴 **No already-sent guard.** `--email` sends every time it finds SMTP config. Both schedulers (launchd 06:35 IST and Actions 01:00 UTC) firing the same morning means two briefs — accepted on purpose, because de-duplicating across two machines would reintroduce the shared state this repo exists to avoid. Do not add a third trigger expecting it to be idempotent.
- GitHub cron is UTC-only with no DST, so the Actions run drifts against IST by an hour twice a year and can queue 10–20 minutes late.
- The universe is a fixed 30 names; there is no index membership, fundamentals, news, watchlist or forward tracking by design — those live in the main pipeline.
- yfinance is an unofficial API: a rate-limit or an empty frame shows up as a degraded market, not as a retry.
- `BENCH` (`^NSEI`, `^GSPC`) is defined but not yet used in the output.
- Exit code 1 (no data at all) and 2 (delivery failed) are the only non-zero paths; a partially degraded brief exits 0 and relies on the banner being read.

## Related repositories
- `market-pipeline` — the owner's full multi-market pipeline; this repo is its independent fallback and shares no code or data with it.

## Licence
No licence file yet — all rights reserved.
