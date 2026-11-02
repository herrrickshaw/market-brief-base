#!/usr/bin/env python3
"""
brief.py — the BASE daily market brief. One file, no local state, always fires.

WHY THIS EXISTS SEPARATELY FROM market-pipeline
-----------------------------------------------
The main pipeline is ~200 files and produces a far richer brief. It also has
~200 files' worth of ways to not run. Every one of these actually happened on
2026-07-21:

  * the Mac was asleep at 00:30, so the scheduled run never fired
  * `pipeline_lib.sh` resolved `python3` from the caller's PATH, so manual runs
    used a different interpreter with a different dependency set than scheduled
    ones, and died on a missing package
  * two copies of every data store existed (`~/Downloads/...` and the repo) and
    silently diverged, so a run could read week-old prices with no error
  * macOS TCC denies launchd all access to `~/Downloads`, where half the data
    lived
  * screener.in hard-blocked the machine for an hour, and the brief's validator
    depends on it

This file is the floor beneath all of that. It answers one question — "did
anything happen in the markets I follow?" — and is built so that the answer
arrives even when everything else is broken.

DESIGN RULES, each one earned
-----------------------------
1. NO LOCAL STATE. Every run fetches fresh and writes only its own output. There
   is no cache to go stale, no second copy to diverge from, no path outside this
   directory. The two-trees bug cannot occur because there is one tree.
2. NO EXTERNAL DATA SOURCE THAT CAN BLOCK YOU. yfinance only. screener.in,
   moneycontrol and the NSE bhavcopy are all better sources and all of them
   have refused this machine at least once.
3. RUNS IDENTICALLY IN BOTH PLACES. The same `python brief.py` on the laptop and
   in GitHub Actions, same pinned dependencies, no shell wrapper deciding which
   interpreter to use.
4. FAIL SOFT, NEVER SILENT. A market that fails to fetch is reported as failed
   in the output. The brief still goes. What it must never do is quietly drop a
   market and look complete.
5. A MISSING PRICE IS NOT A PRICE. No `.fillna(0)` anywhere — zero-filling a
   close is what made 2,472 of 2,480 Korean stocks report BREAKDOWN_SELL.
6. EVERY NUMBER CARRIES ITS DATE. A price without the bar date it came from is
   how a week-old close gets shipped as today's.

    python brief.py                 # print to stdout, write brief.html
    python brief.py --email         # also send (needs SMTP_* env)
    python brief.py --self-test     # no network; verify the logic

LAYOUT (top to bottom): constants → indicators (pure) → data (fetch + parse)
→ render (pure) → email (config + delivery) → self-test → CLI. Pure functions
take data in and return data out; the network and SMTP sit behind injectable
callables so every stage is testable without either.
"""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "brief.html"

# Universe is HARDCODED on purpose. Every "fetch the constituent list first"
# design adds a source that can be down at 00:30, and a base version that cannot
# run without a working index-scraper is not a base version. Small, liquid,
# recognisable — enough to answer "did anything happen".
UNIVERSE: dict[str, list[str]] = {
    "India": [
        "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
        "BHARTIARTL.NS", "SBIN.NS", "LT.NS", "ITC.NS", "AXISBANK.NS",
        "KOTAKBANK.NS", "HINDUNILVR.NS", "BAJFINANCE.NS", "MARUTI.NS", "TITAN.NS",
    ],
    "US": [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "JPM",
        "V", "UNH", "XOM", "JNJ", "WMT", "PG", "HD",
    ],
}
BENCH = {"India": "^NSEI", "US": "^GSPC"}

LOOKBACK = "1y"          # enough for a 200-day average
DARVAS_CONFIRM = 3       # bars a high must survive to define a box top
MIN_BARS_FOR_BOX = 20    # fewer settled bars than this → INSUFFICIENT_DATA
SMA_FAST = 50            # golden/death cross: fast leg
SMA_SLOW = 200           # golden/death cross: slow leg
ERROR_TEXT_LEN = 60      # per-ticker error messages are truncated to this

SMTP_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_TO")
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = "587"
SMTP_TIMEOUT_S = 45

Row = dict[str, Any]                      # one ticker's summary (or {"error": …})
MarketResults = dict[str, dict[str, Row]]  # market → ticker → Row
Downloader = Callable[[Sequence[str]], Any]


# ── indicators ────────────────────────────────────────────────────────────────
def sma(vals: Sequence[float], n: int) -> float | None:
    """Simple moving average of the last n values, or None if there aren't n."""
    v = [x for x in vals if x == x]          # drop NaN; never substitute zero
    return sum(v[-n:]) / n if len(v) >= n else None


def darvas(highs: Sequence[float], lows: Sequence[float],
           closes: Sequence[float]) -> dict[str, Any]:
    """Darvas box on SETTLED bars, with the current bar excluded from formation.

    Including the current bar makes a breakdown impossible to detect — the box
    bottom moves down with the price it is meant to be measured against.
    Trailing NaNs are dropped, never zero-filled: zero sits below every box
    bottom, so a zero-filled close reports BREAKDOWN_SELL for the entire
    universe while the LTP column still shows a real price beside it.

    Returns {"signal", "top", "bottom"}; signal is one of INSUFFICIENT_DATA,
    NO_DATA, NO_BOX, BREAKOUT_BUY, BREAKDOWN_SELL, IN_BOX.
    """
    if len(closes) < MIN_BARS_FOR_BOX:
        return {"signal": "INSUFFICIENT_DATA", "top": None, "bottom": None}
    cur = closes[-1]
    if cur is None or cur != cur or cur <= 0:
        return {"signal": "NO_DATA", "top": None, "bottom": None}

    h, l = highs[:-1], lows[:-1]             # historical only
    top = top_i = None
    for i in range(len(h) - DARVAS_CONFIRM - 1, -1, -1):
        c = h[i]
        if c != c or c <= 0:
            continue
        w = h[i + 1: i + 1 + DARVAS_CONFIRM]
        if len(w) == DARVAS_CONFIRM and all(x < c for x in w):
            top, top_i = c, i
            break
    if top is None:
        return {"signal": "NO_BOX", "top": None, "bottom": None}

    seg = [x for x in l[top_i:] if x == x and x > 0]
    bottom = min(seg) if seg else None
    if bottom is None:
        return {"signal": "NO_BOX", "top": None, "bottom": None}

    sig = ("BREAKOUT_BUY" if cur > top
           else "BREAKDOWN_SELL" if cur < bottom
           else "IN_BOX")
    return {"signal": sig, "top": top, "bottom": bottom}


def trend(r: Mapping[str, Any]) -> str:
    """Golden/death cross from a row's sma50/sma200, or "—" if either is missing."""
    s50, s200 = r.get("sma50"), r.get("sma200")
    if not s50 or not s200:
        return "—"
    return "Golden Cross" if s50 > s200 else "Death Cross"


def day_change_pct(close: float, prev: float | None) -> float | None:
    """Percent change from the previous settled close, or None without one."""
    return (close / prev - 1) * 100 if prev else None


# ── data ──────────────────────────────────────────────────────────────────────
def _yf_download(tickers: Sequence[str]) -> Any:
    """The one network call: a yfinance frame for `tickers` over LOOKBACK."""
    import yfinance as yf
    return yf.download(list(tickers), period=LOOKBACK, auto_adjust=False,
                       progress=False, group_by="ticker", threads=True)


def summarise(closes: Sequence[float], highs: Sequence[float],
              lows: Sequence[float], bar_date: str) -> Row:
    """One ticker's row from aligned settled bars; `bar_date` is the last close's date."""
    return {
        "close": closes[-1],
        "date": bar_date,
        "sma50": sma(closes, SMA_FAST),
        "sma200": sma(closes, SMA_SLOW),
        "prev": closes[-2] if len(closes) > 1 else None,
        "darvas": darvas(highs, lows, closes),
    }


def _row_from_frame(frame: Any) -> Row:
    """Row for a single-ticker OHLC frame; {"error"} if it has no settled close."""
    c = frame["Close"].dropna()
    if c.empty:
        return {"error": "no settled bars"}
    closes = [float(x) for x in c.tolist()]
    hi = [float(x) for x in frame["High"].reindex(c.index).tolist()]
    lo = [float(x) for x in frame["Low"].reindex(c.index).tolist()]
    return summarise(closes, hi, lo, c.index[-1].date().isoformat())


def fetch(tickers: Sequence[str], downloader: Downloader | None = None) -> dict[str, Row]:
    """{ticker: {close, date, sma50, sma200, prev, darvas}} — or {} if yfinance fails.

    Returns the BAR DATE with every price. A price without its date is how a
    stale close gets presented as today's. A ticker that cannot be parsed gets
    {"error": "<ExcType>: <message>"} instead of being dropped.
    """
    out: dict[str, Row] = {}
    data = (downloader or _yf_download)(tickers)
    for t in tickers:
        try:
            d = data[t] if len(tickers) > 1 else data
            out[t] = _row_from_frame(d)
        except Exception as e:
            out[t] = {"error": f"{type(e).__name__}: {str(e)[:ERROR_TEXT_LEN]}"}
    return out


def collect(universe: Mapping[str, Sequence[str]],
            fetcher: Callable[[Sequence[str]], dict[str, Row]] | None = None,
            ) -> tuple[MarketResults, dict[str, str]]:
    """Fetch every market; a market that raises is named in `failures`, not dropped."""
    fetcher = fetcher or fetch
    results: MarketResults = {}
    failures: dict[str, str] = {}
    for market, tickers in universe.items():
        try:
            r = fetcher(tickers)
            good = {k: v for k, v in r.items() if "error" not in v}
            if not good:
                failures[market] = "no usable data"
            results[market] = r
            print(f"  {market}: {len(good)}/{len(tickers)} priced")
        except Exception as e:
            failures[market] = f"{type(e).__name__}"
            print(f"  {market}: FAILED {type(e).__name__}: {str(e)[:70]}")
    return results, failures


# ── render ────────────────────────────────────────────────────────────────────
# Colours are declared for BOTH schemes. The main pipeline's brief set every
# heading to #333 on an implicit white page; a dark-theme client darkens the
# background and keeps the grey, so the title and every section heading
# rendered invisible with no error. The semantic green/red is left intact —
# the point of the colour is the signal.
CSS = """
    body{margin:0;padding:16px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;
         background:#fff;color:#1a1a1a}
    h1{font-size:20px;margin:0 0 2px} h2{font-size:15px;margin:20px 0 6px;
       border-bottom:2px solid #1a73e8;padding-bottom:3px}
    .sub{color:#666;font-size:12px;margin:0 0 14px}
    .wrap{overflow-x:auto}
    table{border-collapse:collapse;width:100%;font-size:13px;min-width:480px}
    th{background:#eef;text-align:left;padding:5px 8px}
    td{padding:4px 8px;border-bottom:1px solid #f0f0f0}
    .up{color:#1b7f37;font-weight:600} .dn{color:#b00;font-weight:600}
    .mut{color:#666;font-size:11px}
    .warn{background:#fffbe6;border-left:3px solid #f9a825;padding:8px 11px;
          font-size:12px;margin:10px 0}
    @media (prefers-color-scheme:dark){
      body{background:#151515;color:#e8e8e8}
      h1,h2{color:#f3f3f3} .mut{color:#adadad}
      th{background:#22304a;color:#f3f3f3} td{border-bottom-color:#2e2e2e}
      .warn{background:#221f14;color:#e0dcc8;border-color:#5a4b1e}
    }
    """

DISCLAIMER = ("Educational / research only — NOT investment advice. Mechanical "
              "signals on a fixed 30-name universe; they do not account for your "
              "objectives or risk. Consult a SEBI-registered adviser before acting.")


def _row_html(ticker: str, r: Row) -> str:
    """One <tr> for a ticker: an error row spans the table, a priced row has six cells."""
    if "error" in r:
        return (f"<tr><td><b>{ticker}</b></td><td colspan='5' class='mut'>"
                f"{r['error']}</td></tr>")
    chg = day_change_pct(r["close"], r.get("prev"))
    cls = "up" if (chg or 0) > 0 else "dn"
    sig = r["darvas"]["signal"]
    sc = "up" if sig == "BREAKOUT_BUY" else "dn" if sig == "BREAKDOWN_SELL" else "mut"
    return (f"<tr><td><b>{ticker}</b></td><td>{r['close']:,.2f}</td>"
            f"<td class='mut'>{r['date']}</td>"
            f"<td class='{cls}'>{f'{chg:+.2f}%' if chg is not None else '—'}</td>"
            f"<td class='mut'>{trend(r)}</td>"
            f"<td class='{sc}'>{sig}</td></tr>")


def _market_html(market: str, rows: Mapping[str, Row]) -> list[str]:
    """Heading plus one table for a market, tickers in sorted order."""
    p = [f"<h2>{market}</h2><div class='wrap'><table>"
         "<tr><th>Symbol</th><th>Close</th><th>As of</th><th>Day</th>"
         "<th>Trend</th><th>Darvas</th></tr>"]
    p.extend(_row_html(t, r) for t, r in sorted(rows.items()))
    p.append("</table></div>")
    return p


def render(results: MarketResults, failures: Mapping[str, str],
           generated: datetime) -> str:
    """Self-contained HTML that survives a dark-mode client (see CSS)."""
    p = ["<!doctype html><html><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<meta name='color-scheme' content='light dark'>",
         f"<style>{CSS}</style></head><body>",
         f"<h1>Daily Market Brief — {generated:%d %b %Y}</h1>",
         "<p class='sub'>Base version · yfinance only · prices are the last "
         "SETTLED close, never live</p>"]

    if failures:
        p.append("<div class='warn'><b>Degraded:</b> " +
                 "; ".join(f"{k} ({v})" for k, v in failures.items()) +
                 ". Those markets are missing below — this is not an empty result."
                 "</div>")

    for market, rows in results.items():
        p.extend(_market_html(market, rows))

    p.append(f"<p class='mut' style='margin-top:22px'>{DISCLAIMER}</p>")
    p.append(f"<p class='mut'>Generated {generated:%Y-%m-%d %H:%M} UTC · "
             "base brief, independent of the main pipeline</p>")
    p.append("</body></html>")
    return "\n".join(p)


def build_subject(generated: datetime, failures: Mapping[str, str]) -> str:
    """Email subject; names how many markets degraded so the inbox shows it."""
    subject = f"Daily Market Brief — {generated:%d %b %Y}"
    if failures:
        subject += f" ({len(failures)} degraded)"
    return subject


# ── email ─────────────────────────────────────────────────────────────────────
def _env(key: str, default: str | None = None,
         env: Mapping[str, str] | None = None) -> str | None:
    """Env var, treating EMPTY as absent.

    GitHub Actions sets an env var for every `secrets.X` reference, so an
    unconfigured secret arrives as "" rather than being unset. os.environ.get's
    default only fires when the key is MISSING, so `int(os.environ.get(
    "SMTP_PORT", "587"))` became int("") and raised — the 2026-07-21 test run
    fetched all 30 prices, wrote the brief, then exited 2 on a config read.
    "Absent" and "empty" are different states and only one of them takes a
    default.
    """
    v = (os.environ if env is None else env).get(key)
    return v.strip() if v and v.strip() else default


@dataclass(frozen=True)
class SmtpConfig:
    """Everything deliver() needs; `to` may be a comma-separated list."""
    host: str
    port: int
    user: str
    password: str
    to: str


def smtp_config(env: Mapping[str, str] | None = None) -> tuple[SmtpConfig | None, str]:
    """(config, "") when SMTP_* is complete, else (None, reason-to-skip)."""
    host = _env("SMTP_HOST", DEFAULT_SMTP_HOST, env)
    try:
        port = int(_env("SMTP_PORT", DEFAULT_SMTP_PORT, env))
    except ValueError:
        shown = (os.environ if env is None else env).get("SMTP_PORT")
        return None, f"SMTP_PORT={shown!r} is not a number"
    user, pw = _env("SMTP_USER", env=env), _env("SMTP_PASS", env=env)
    to = _env("SMTP_TO", env=env) or user
    if not (user and pw and to):
        missing = [k for k in ("SMTP_USER", "SMTP_PASS", "SMTP_TO") if not _env(k, env=env)]
        return None, f"not configured ({', '.join(missing)})"
    return SmtpConfig(host, port, user, pw, to), ""


def build_message(cfg: SmtpConfig, html: str, subject: str) -> MIMEMultipart:
    """Multipart/alternative: a one-line plain part plus the HTML brief."""
    m = MIMEMultipart("alternative")
    m["Subject"], m["From"], m["To"] = subject, cfg.user, cfg.to
    m.attach(MIMEText("HTML brief attached inline.", "plain"))
    m.attach(MIMEText(html, "html"))
    return m


def deliver(cfg: SmtpConfig, html: str, subject: str,
            smtp_factory: Callable[..., Any] = smtplib.SMTP) -> None:
    """STARTTLS + login + sendmail. Raises on any delivery error — the caller decides."""
    m = build_message(cfg, html, subject)
    with smtp_factory(cfg.host, cfg.port, timeout=SMTP_TIMEOUT_S) as s:
        s.starttls()
        s.login(cfg.user, cfg.password)
        s.sendmail(cfg.user, [x.strip() for x in cfg.to.split(",")], m.as_string())


def send(html: str, subject: str, env: Mapping[str, str] | None = None,
         smtp_factory: Callable[..., Any] = smtplib.SMTP) -> bool:
    """SMTP from env only. Missing config is a clear skip (False), never a crash.

    There is no already-sent guard: every call that finds SMTP_* configured
    sends. Two schedulers firing the same morning means two briefs, by design.
    """
    cfg, reason = smtp_config(env)
    if cfg is None:
        print(f"  email: {reason} — skipping send")
        return False
    deliver(cfg, html, subject, smtp_factory)
    print(f"  email: sent to {cfg.to}")
    return True


# ── self-test ─────────────────────────────────────────────────────────────────
def self_test() -> int:
    """Verify the logic offline. Runs in CI before any network call."""
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond

    base = [100 + i * 0.1 for i in range(60)]
    box = [105, 104, 103, 104, 105, 104, 103, 104, 105, 104] * 3
    check("rising series is not a breakdown",
          darvas([x * 1.01 for x in base], [x * .99 for x in base], base)["signal"]
          != "BREAKDOWN_SELL")
    d = darvas([x * 1.01 for x in box] + [120], [x * .99 for x in box] + [119],
               box + [120])
    check("decisive break above the box is BREAKOUT_BUY", d["signal"] == "BREAKOUT_BUY")
    d2 = darvas([x * 1.01 for x in box] + [80], [x * .99 for x in box] + [79],
                box + [80])
    check("decisive break below the box is BREAKDOWN_SELL", d2["signal"] == "BREAKDOWN_SELL")
    nan = float("nan")
    check("a NaN final close is NO_DATA, not a fabricated zero",
          darvas([nan] * 30, [nan] * 30, [100.0] * 29 + [nan])["signal"] == "NO_DATA")
    check("sma returns None rather than inventing a short average",
          sma([1.0, 2.0, 3.0], 50) is None)
    check("sma ignores NaN instead of treating it as zero",
          abs(sma([2.0, nan, 4.0], 2) - 3.0) < 1e-9)

    # GitHub sets an env var for every referenced secret, so an unconfigured one
    # arrives as "" rather than unset. This is asserted, not remembered.
    os.environ["_T_EMPTY"] = ""
    os.environ["_T_SPACES"] = "   "
    os.environ["_T_SET"] = "587"
    check("empty env var falls back to the default", _env("_T_EMPTY", "587") == "587")
    check("whitespace-only env var falls back too", _env("_T_SPACES", "587") == "587")
    check("a real value is returned", _env("_T_SET", "1") == "587")
    check("absent env var falls back", _env("_T_MISSING", "587") == "587")
    for k in ("_T_EMPTY", "_T_SPACES", "_T_SET"):
        os.environ.pop(k, None)

    # An unconfigured send is a SKIP, not a failure — otherwise a CI run with no
    # secrets reports broken when it worked.
    saved = {k: os.environ.pop(k, None) for k in SMTP_KEYS}
    os.environ["SMTP_PORT"] = ""          # reproduce the exact CI condition
    try:
        check("unconfigured send skips cleanly instead of raising",
              send("<p>x</p>", "test") is False)
    except Exception as e:
        check(f"unconfigured send raised {type(e).__name__}", False)
    finally:
        os.environ.pop("SMTP_PORT", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    print("\n  self-test:", "OK" if ok else "FAILED")
    return 0 if ok else 1


# ── CLI ───────────────────────────────────────────────────────────────────────
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """CLI flags. Names are load-bearing: launchd and CI invoke them by name."""
    ap = argparse.ArgumentParser(description="Base daily market brief")
    ap.add_argument("--email", action="store_true",
                    help="also send the brief via SMTP_* env (skips if unset)")
    ap.add_argument("--out", default=str(OUT_FILE),
                    help="where to write the HTML (default: %(default)s)")
    ap.add_argument("--self-test", action="store_true",
                    help="verify the signal logic offline and exit")
    return ap.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Exit 0 on a brief, 1 if no market returned data, 2 if only delivery failed."""
    a = parse_args(argv)

    if a.self_test:
        return self_test()

    generated = datetime.now(timezone.utc)
    results, failures = collect(UNIVERSE)

    if not results:
        # Nothing at all: exit non-zero so the scheduler surfaces it. Sending an
        # empty brief would look like a quiet market rather than a broken run.
        print("  no market returned data — refusing to emit an empty brief")
        return 1

    html = render(results, failures, generated)
    Path(a.out).write_text(html)
    print(f"  wrote {a.out} ({len(html):,} bytes)")

    if a.email:
        try:
            # send() returns False for "not configured" — a legitimate state, not
            # a failure. Only a genuine delivery error is worth a non-zero exit;
            # conflating the two made an unconfigured CI run look broken.
            send(html, build_subject(generated, failures))
        except Exception as e:
            print(f"  email FAILED: {type(e).__name__}: {str(e)[:90]}")
            return 2          # brief was produced; only delivery failed
    return 0


if __name__ == "__main__":
    sys.exit(main())
