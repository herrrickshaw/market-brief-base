"""Offline tests for brief.py. No network: yfinance is replaced by a stub module.

The central invariant (design rule 5): a missing price is never a price. A
zero-filled close sits below every box bottom, so it must never reach darvas().
"""
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brief  # noqa: E402

NAN = float("nan")


# ── helpers ──────────────────────────────────────────────────────────────────
def box_series(n_cycles=3, extra_close=None):
    """A sideways 103-105 range; optional final bar."""
    box = [105, 104, 103, 104, 105, 104, 103, 104, 105, 104] * n_cycles
    highs = [x * 1.01 for x in box]
    lows = [x * 0.99 for x in box]
    closes = list(box)
    if extra_close is not None:
        highs.append(extra_close * 1.01)
        lows.append(extra_close * 0.99)
        closes.append(extra_close)
    return highs, lows, closes


def stub_yfinance(monkeypatch, frames):
    """Install a fake `yfinance` whose download() returns a group_by='ticker' frame."""
    def download(tickers, **kw):
        assert kw.get("auto_adjust") is False
        if len(tickers) == 1:
            return frames[tickers[0]]
        return pd.concat({t: frames[t] for t in tickers}, axis=1)
    mod = types.ModuleType("yfinance")
    mod.download = download
    monkeypatch.setitem(sys.modules, "yfinance", mod)


def ohlc(closes, highs=None, lows=None, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    highs = highs if highs is not None else [c * 1.01 if c == c else NAN for c in closes]
    lows = lows if lows is not None else [c * 0.99 if c == c else NAN for c in closes]
    return pd.DataFrame({"Open": closes, "High": highs, "Low": lows, "Close": closes},
                        index=idx)


# ── sma ──────────────────────────────────────────────────────────────────────
def test_sma_happy_path_uses_last_n_values():
    assert brief.sma([1.0, 2.0, 3.0, 4.0], 2) == pytest.approx(3.5)


def test_sma_exactly_n_values():
    assert brief.sma([2.0, 4.0, 6.0], 3) == pytest.approx(4.0)


def test_sma_empty_is_none():
    assert brief.sma([], 5) is None


def test_sma_nan_never_counts_toward_window():
    # three real values, n=3 -> mean of the real ones, not (x+0)/3
    assert brief.sma([NAN, 3.0, NAN, 6.0, 9.0], 3) == pytest.approx(6.0)
    assert brief.sma([NAN, NAN, 1.0], 2) is None


# ── darvas ───────────────────────────────────────────────────────────────────
def test_darvas_fewer_than_20_bars_is_insufficient():
    h, l, c = [10.0] * 19, [9.0] * 19, [9.5] * 19
    assert brief.darvas(h, l, c)["signal"] == "INSUFFICIENT_DATA"


@pytest.mark.parametrize("last", [NAN, 0.0, -1.0])
def test_darvas_unusable_current_close_is_no_data(last):
    h, l, c = box_series(extra_close=104)
    c[-1] = last
    assert brief.darvas(h, l, c)["signal"] == "NO_DATA"


def test_darvas_in_box_reports_top_and_bottom():
    h, l, c = box_series(extra_close=104)
    d = brief.darvas(h, l, c)
    assert d["signal"] == "IN_BOX"
    # top = last high that survived 3 lower highs; bottom = min low since it
    assert d["top"] == pytest.approx(105 * 1.01)
    assert d["bottom"] == pytest.approx(103 * 0.99)


def test_darvas_current_bar_excluded_from_formation():
    # If the current bar's low were part of the box, a collapse to 80 would
    # drag the bottom to 79.2 and never register as a breakdown.
    h, l, c = box_series(extra_close=80)
    d = brief.darvas(h, l, c)
    assert d["signal"] == "BREAKDOWN_SELL"
    assert d["bottom"] == pytest.approx(103 * 0.99)


def test_darvas_breakout_above_top():
    h, l, c = box_series(extra_close=120)
    assert brief.darvas(h, l, c)["signal"] == "BREAKOUT_BUY"


def test_darvas_nan_lows_are_skipped_not_zero_filled():
    h, l, c = box_series(extra_close=104)
    for i in range(20, 29):           # missing lows inside the box segment
        l[i] = NAN
    d = brief.darvas(h, l, c)
    assert d["bottom"] is not None and d["bottom"] > 0
    assert d["signal"] != "BREAKDOWN_SELL"


def test_darvas_zero_lows_are_skipped_not_used_as_bottom():
    h, l, c = box_series(extra_close=104)
    l[25] = 0.0                       # a zero-filled bar in history
    d = brief.darvas(h, l, c)
    assert d["bottom"] == pytest.approx(103 * 0.99)
    assert d["signal"] == "IN_BOX"


def test_darvas_no_confirmed_high_is_no_box():
    # strictly rising highs: no high is ever followed by 3 lower highs
    n = 40
    h = [100.0 + i for i in range(n)]
    l = [x - 2 for x in h]
    c = [x - 1 for x in h]
    assert brief.darvas(h, l, c)["signal"] == "NO_BOX"


# ── fetch (yfinance stubbed) ─────────────────────────────────────────────────
def test_fetch_drops_unsettled_trailing_bar_and_reports_its_date(monkeypatch):
    _, _, closes = box_series(extra_close=104)
    df = ohlc(closes + [NAN])         # today's bar: no settled close yet
    df.iloc[-1, df.columns.get_loc("High")] = 200.0
    stub_yfinance(monkeypatch, {"AAA": df, "BBB": ohlc(closes)})
    out = brief.fetch(["AAA", "BBB"])
    r = out["AAA"]
    assert r["close"] == pytest.approx(104)
    assert r["date"] == df.index[-2].date().isoformat()
    assert r["prev"] == pytest.approx(closes[-2])
    assert r["darvas"]["signal"] == "IN_BOX"


def test_fetch_missing_close_mid_series_is_never_zero_filled(monkeypatch):
    _, _, closes = box_series(extra_close=104)
    closes[-2] = NAN                  # a hole right before the current bar
    stub_yfinance(monkeypatch, {"AAA": ohlc(closes), "BBB": ohlc(closes)})
    r = brief.fetch(["AAA", "BBB"])["AAA"]
    assert r["prev"] == pytest.approx(closes[-3])
    assert r["darvas"]["signal"] != "BREAKDOWN_SELL"


def test_fetch_all_nan_ticker_reports_error_not_a_price(monkeypatch):
    _, _, closes = box_series(extra_close=104)
    stub_yfinance(monkeypatch, {"AAA": ohlc([NAN] * len(closes)), "BBB": ohlc(closes)})
    out = brief.fetch(["AAA", "BBB"])
    assert out["AAA"] == {"error": "no settled bars"}
    assert "close" in out["BBB"]


def test_fetch_zero_close_from_source_is_treated_as_missing(monkeypatch):
    # A zero close is not a price (rule 5; darvas() already treats <=0 as
    # NO_DATA). It must not be shipped as the close or drag the averages down.
    closes = [100.0 + 0.1 * i for i in range(60)]
    closes_z = closes + [0.0]
    stub_yfinance(monkeypatch, {"AAA": ohlc(closes_z), "BBB": ohlc(closes)})
    r = brief.fetch(["AAA", "BBB"])["AAA"]
    assert r["close"] == pytest.approx(closes[-1])
    assert r["sma50"] == pytest.approx(sum(closes[-50:]) / 50)


def test_fetch_per_ticker_exception_is_reported_not_raised(monkeypatch):
    _, _, closes = box_series(extra_close=104)
    stub_yfinance(monkeypatch, {"AAA": ohlc(closes), "BBB": ohlc(closes)})
    monkeypatch.setattr(brief, "darvas",
                        lambda *a: (_ for _ in ()).throw(ValueError("boom")))
    out = brief.fetch(["AAA", "BBB"])
    assert out["AAA"]["error"].startswith("ValueError")


# ── trend ────────────────────────────────────────────────────────────────────
def test_trend_cross_labels():
    assert brief.trend({"sma50": 110, "sma200": 100}) == "Golden Cross"
    assert brief.trend({"sma50": 90, "sma200": 100}) == "Death Cross"
    assert brief.trend({"sma50": 90, "sma200": None}) == "—"


# ── render ───────────────────────────────────────────────────────────────────
GEN = datetime(2026, 10, 24, 1, 0, tzinfo=timezone.utc)


def test_render_shows_degraded_banner_for_failed_market():
    html = brief.render({}, {"India": "no usable data"}, GEN)
    assert "Degraded" in html and "India (no usable data)" in html


def test_render_every_price_carries_its_bar_date():
    row = {"close": 104.0, "prev": 100.0, "date": "2026-10-23",
           "sma50": None, "sma200": None,
           "darvas": {"signal": "IN_BOX", "top": 1, "bottom": 1}}
    html = brief.render({"US": {"AAA": row}}, {}, GEN)
    assert "104.00" in html and "2026-10-23" in html and "+4.00%" in html


def test_render_error_row_is_reported_not_dropped():
    html = brief.render({"US": {"AAA": {"error": "no settled bars"}}}, {}, GEN)
    assert "AAA" in html and "no settled bars" in html


def test_render_declares_dark_mode_colours():
    html = brief.render({}, {}, GEN)
    assert "prefers-color-scheme:dark" in html
    assert "color-scheme' content='light dark'" in html


# ── env / send ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("val,expected", [("", "d"), ("  ", "d"), (" 25 ", "25")])
def test_env_empty_is_absent(monkeypatch, val, expected):
    monkeypatch.setenv("_T_X", val)
    assert brief._env("_T_X", "d") == expected


def _clear_smtp(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_TO"):
        monkeypatch.delenv(k, raising=False)


def test_send_unconfigured_skips_without_connecting(monkeypatch):
    _clear_smtp(monkeypatch)
    monkeypatch.setattr(brief.smtplib, "SMTP",
                        lambda *a, **k: pytest.fail("must not connect"))
    assert brief.send("<p/>", "s") is False


def test_send_bad_port_skips(monkeypatch):
    _clear_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_PORT", "abc")
    assert brief.send("<p/>", "s") is False


def test_send_configured_uses_starttls_and_splits_recipients(monkeypatch):
    _clear_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_USER", "u@example.test")
    monkeypatch.setenv("SMTP_PASS", "x")
    monkeypatch.setenv("SMTP_TO", "a@example.test, b@example.test")
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("init", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): calls.append(("tls",))
        def login(self, u, p): calls.append(("login", u))
        def sendmail(self, frm, to, msg): calls.append(("send", frm, to))

    monkeypatch.setattr(brief.smtplib, "SMTP", FakeSMTP)
    assert brief.send("<p/>", "s") is True
    assert ("init", "smtp.gmail.com", 587) in calls
    assert ("tls",) in calls
    assert ("send", "u@example.test", ["a@example.test", "b@example.test"]) in calls


# ── main ─────────────────────────────────────────────────────────────────────
def test_main_refuses_empty_brief_when_every_market_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(brief, "fetch", lambda t: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(sys, "argv", ["brief.py", "--out", str(tmp_path / "b.html")])
    assert brief.main() == 1
    assert not (tmp_path / "b.html").exists()


def test_main_partial_failure_still_writes_brief_with_banner(monkeypatch, tmp_path):
    good = {"close": 1.0, "prev": 1.0, "date": "2026-10-23", "sma50": None,
            "sma200": None, "darvas": {"signal": "NO_BOX", "top": None, "bottom": None}}

    def fake_fetch(tickers):
        if "AAPL" in tickers:
            raise RuntimeError("down")
        return {t: dict(good) for t in tickers}

    monkeypatch.setattr(brief, "fetch", fake_fetch)
    out = tmp_path / "b.html"
    monkeypatch.setattr(sys, "argv", ["brief.py", "--out", str(out)])
    assert brief.main() == 0
    html = out.read_text()
    assert "Degraded" in html and "US (RuntimeError)" in html


def test_self_test_passes():
    assert brief.self_test() == 0
