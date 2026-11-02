"""Unit tests for brief.py's pure functions and its injectable I/O seams.

Nothing here touches the network or an SMTP server: fetch() gets a fake
downloader, send() gets a fake smtplib.SMTP.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import brief

NAN = float("nan")
GENERATED = datetime(2026, 11, 2, 1, 5, tzinfo=timezone.utc)


# ── indicators ────────────────────────────────────────────────────────────────
def test_sma_needs_n_values():
    assert brief.sma([1.0, 2.0, 3.0], 50) is None


def test_sma_ignores_nan_rather_than_zero_filling():
    assert brief.sma([2.0, NAN, 4.0], 2) == pytest.approx(3.0)


def test_sma_uses_last_n():
    assert brief.sma([10.0, 1.0, 2.0, 3.0], 3) == pytest.approx(2.0)


BOX = [105, 104, 103, 104, 105, 104, 103, 104, 105, 104] * 3


def _bars(closes):
    return [x * 1.01 for x in closes], [x * 0.99 for x in closes], list(closes)


def test_darvas_insufficient_data():
    assert brief.darvas(*_bars([100.0] * 5))["signal"] == "INSUFFICIENT_DATA"


def test_darvas_nan_close_is_no_data_not_zero():
    r = brief.darvas([NAN] * 30, [NAN] * 30, [100.0] * 29 + [NAN])
    assert r == {"signal": "NO_DATA", "top": None, "bottom": None}


def test_darvas_zero_close_is_no_data():
    assert brief.darvas(*_bars([100.0] * 29 + [0.0]))["signal"] == "NO_DATA"


def test_darvas_rising_series_is_not_breakdown():
    base = [100 + i * 0.1 for i in range(60)]
    assert brief.darvas(*_bars(base))["signal"] != "BREAKDOWN_SELL"


def test_darvas_breakout_above_box():
    hi, lo, cl = _bars(BOX)
    r = brief.darvas(hi + [120], lo + [119], cl + [120])
    assert r["signal"] == "BREAKOUT_BUY"
    assert r["top"] == pytest.approx(105 * 1.01)
    assert r["bottom"] == pytest.approx(103 * 0.99)


def test_darvas_breakdown_below_box():
    hi, lo, cl = _bars(BOX)
    assert brief.darvas(hi + [80], lo + [79], cl + [80])["signal"] == "BREAKDOWN_SELL"


def test_darvas_inside_box():
    hi, lo, cl = _bars(BOX)
    assert brief.darvas(hi + [104], lo + [103.5], cl + [104])["signal"] == "IN_BOX"


def test_darvas_current_bar_excluded_from_formation():
    # A new all-time high on the CURRENT bar must not become the box top.
    hi, lo, cl = _bars(BOX)
    r = brief.darvas(hi + [200], lo + [150], cl + [190])
    assert r["signal"] == "BREAKOUT_BUY"
    assert r["top"] < 190


def test_darvas_monotonic_highs_have_no_box():
    highs = [float(i) for i in range(30)]       # no high survives 3 bars
    assert brief.darvas(highs, [x - 1 for x in highs], highs)["signal"] == "NO_BOX"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"sma50": 110.0, "sma200": 100.0}, "Golden Cross"),
        ({"sma50": 90.0, "sma200": 100.0}, "Death Cross"),
        ({"sma50": None, "sma200": 100.0}, "—"),
        ({}, "—"),
    ],
)
def test_trend(row, expected):
    assert brief.trend(row) == expected


def test_day_change_pct():
    assert brief.day_change_pct(110.0, 100.0) == pytest.approx(10.0)
    assert brief.day_change_pct(110.0, None) is None
    assert brief.day_change_pct(110.0, 0.0) is None


# ── data ──────────────────────────────────────────────────────────────────────
IDX = pd.bdate_range("2025-10-01", periods=260)


def _frame(seed: int) -> pd.DataFrame:
    close = 100 + np.cumsum(np.random.RandomState(seed).normal(0, 1, len(IDX)))
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1000}, index=IDX)


def test_summarise_row_shape():
    closes = [100.0 + i for i in range(210)]
    r = brief.summarise(closes, [c + 1 for c in closes], [c - 1 for c in closes], "2026-01-02")
    assert r["close"] == 309.0 and r["prev"] == 308.0 and r["date"] == "2026-01-02"
    assert r["sma50"] == pytest.approx(sum(closes[-50:]) / 50)
    assert r["sma200"] == pytest.approx(sum(closes[-200:]) / 200)
    assert set(r["darvas"]) == {"signal", "top", "bottom"}


def test_summarise_single_bar_has_no_prev():
    assert brief.summarise([5.0], [5.0], [5.0], "d")["prev"] is None


def test_fetch_multi_ticker_uses_injected_downloader():
    multi = pd.concat({"AAA": _frame(1), "BBB": _frame(2)}, axis=1)
    multi.loc[IDX[-1], ("BBB", "Close")] = np.nan          # trailing NaN
    calls = []

    def fake(tickers):
        calls.append(list(tickers))
        return multi

    out = brief.fetch(["AAA", "BBB"], downloader=fake)
    assert calls == [["AAA", "BBB"]]
    assert out["AAA"]["date"] == IDX[-1].date().isoformat()
    assert out["BBB"]["date"] == IDX[-2].date().isoformat()   # NaN dropped, not zeroed
    assert out["BBB"]["close"] == pytest.approx(float(multi[("BBB", "Close")].iloc[-2]))


def test_fetch_single_ticker_frame_is_not_indexed_by_ticker():
    out = brief.fetch(["CCC"], downloader=lambda t: _frame(3))
    assert out["CCC"]["close"] == pytest.approx(float(_frame(3)["Close"].iloc[-1]))


def test_fetch_reports_empty_close_as_error_row():
    empty = _frame(4)
    empty["Close"] = np.nan
    assert brief.fetch(["DDD"], downloader=lambda t: empty) == {"DDD": {"error": "no settled bars"}}


def test_fetch_names_parse_failures_instead_of_dropping():
    out = brief.fetch(["EEE"], downloader=lambda t: {"nope": 1})
    assert out["EEE"]["error"].startswith("KeyError:")


def test_collect_names_a_raising_market(capsys):
    def fetcher(tickers):
        if tickers == ["BAD"]:
            raise RuntimeError("boom")
        return {t: {"close": 1.0} for t in tickers}

    results, failures = brief.collect({"OK": ["A", "B"], "Down": ["BAD"]}, fetcher=fetcher)
    assert set(results) == {"OK"}
    assert failures == {"Down": "RuntimeError"}
    out = capsys.readouterr().out
    assert "OK: 2/2 priced" in out and "Down: FAILED RuntimeError: boom" in out


def test_collect_all_error_rows_is_degraded_not_dropped():
    results, failures = brief.collect({"M": ["A"]}, fetcher=lambda t: {"A": {"error": "x"}})
    assert results == {"M": {"A": {"error": "x"}}}
    assert failures == {"M": "no usable data"}


# ── render ────────────────────────────────────────────────────────────────────
RESULTS = {
    "India": {
        "TCS.NS": {"close": 3500.5, "date": "2026-10-31", "sma50": 3400.0, "sma200": 3300.0,
                   "prev": 3450.0, "darvas": {"signal": "BREAKOUT_BUY", "top": 3480.0,
                                              "bottom": 3300.0}},
        "ITC.NS": {"close": 400.0, "date": "2026-10-31", "sma50": 410.0, "sma200": 420.0,
                   "prev": 405.0, "darvas": {"signal": "BREAKDOWN_SELL", "top": 430.0,
                                             "bottom": 402.0}},
        "LT.NS": {"close": 3000.0, "date": "2026-10-31", "sma50": None, "sma200": None,
                  "prev": None, "darvas": {"signal": "IN_BOX", "top": 3100.0, "bottom": 2900.0}},
        "SBIN.NS": {"error": "no settled bars"},
    },
    "US": {},
}


def test_render_characterisation_hash():
    # Byte-identical to the pre-refactor render() for this input.
    html = brief.render(RESULTS, {"US": "no usable data"}, GENERATED)
    assert hashlib.sha256(html.encode()).hexdigest() == (
        "05c56462c9ae89cb58fd2916a81afc9bf822e1aeec31660cb4845bdd975b75c9")


def test_render_contents():
    html = brief.render(RESULTS, {"US": "no usable data"}, GENERATED)
    assert "<h1>Daily Market Brief — 02 Nov 2026</h1>" in html
    assert "<b>Degraded:</b> US (no usable data)" in html
    assert "<td class='up'>+1.46%</td>" in html and "BREAKOUT_BUY" in html
    assert "<td class='dn'>-1.23%</td>" in html and "BREAKDOWN_SELL" in html
    assert "<td class='dn'>—</td><td class='mut'>—</td>" in html      # no prev, no cross
    assert "colspan='5' class='mut'>no settled bars" in html
    assert "prefers-color-scheme:dark" in html
    assert "Generated 2026-11-02 01:05 UTC" in html


def test_render_without_failures_has_no_banner():
    assert "Degraded" not in brief.render({"US": {}}, {}, GENERATED)


def test_build_subject():
    assert brief.build_subject(GENERATED, {}) == "Daily Market Brief — 02 Nov 2026"
    assert brief.build_subject(GENERATED, {"US": "x", "India": "y"}).endswith("(2 degraded)")


# ── email ─────────────────────────────────────────────────────────────────────
def test_env_treats_empty_and_whitespace_as_absent():
    env = {"E": "", "S": "   ", "V": " 587 "}
    assert brief._env("E", "587", env) == "587"
    assert brief._env("S", "587", env) == "587"
    assert brief._env("V", "1", env) == "587"
    assert brief._env("MISSING", "587", env) == "587"
    assert brief._env("MISSING", env=env) is None


def test_smtp_config_unconfigured_lists_missing_keys():
    cfg, reason = brief.smtp_config({"SMTP_PORT": ""})
    assert cfg is None
    assert reason == "not configured (SMTP_USER, SMTP_PASS, SMTP_TO)"


def test_smtp_config_bad_port():
    cfg, reason = brief.smtp_config({"SMTP_PORT": "abc"})
    assert cfg is None and reason == "SMTP_PORT='abc' is not a number"


def test_smtp_config_defaults_and_to_falls_back_to_user():
    cfg, reason = brief.smtp_config({"SMTP_USER": "u@x", "SMTP_PASS": "p"})
    assert reason == ""
    assert cfg == brief.SmtpConfig("smtp.gmail.com", 587, "u@x", "p", "u@x")


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.args = (host, port, timeout)
        self.calls: list[tuple] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def sendmail(self, frm, to, msg):
        self.calls.append(("sendmail", frm, to, msg))


def test_send_skips_when_unconfigured(capsys):
    assert brief.send("<p>x</p>", "s", env={"SMTP_PORT": ""}, smtp_factory=FakeSMTP) is False
    assert "skipping send" in capsys.readouterr().out


def test_send_delivers_to_every_comma_separated_recipient(capsys):
    FakeSMTP.instances.clear()
    env = {"SMTP_HOST": "h", "SMTP_PORT": "2525", "SMTP_USER": "u@x",
           "SMTP_PASS": "p", "SMTP_TO": "a@x, b@x"}
    assert brief.send("<p>hi</p>", "Subj", env=env, smtp_factory=FakeSMTP) is True
    (s,) = FakeSMTP.instances
    assert s.args == ("h", 2525, brief.SMTP_TIMEOUT_S)
    kinds = [c[0] for c in s.calls]
    assert kinds == ["starttls", "login", "sendmail"]
    _, frm, to, raw = s.calls[2]
    assert (frm, to) == ("u@x", ["a@x", "b@x"])
    assert "Subject: Subj" in raw and "<p>hi</p>" in raw
    assert "email: sent to a@x, b@x" in capsys.readouterr().out


def test_send_propagates_delivery_errors():
    class Broken(FakeSMTP):
        def login(self, user, pw):
            raise ConnectionError("refused")

    env = {"SMTP_USER": "u@x", "SMTP_PASS": "p"}
    with pytest.raises(ConnectionError):
        brief.send("<p>x</p>", "s", env=env, smtp_factory=Broken)


# ── CLI ───────────────────────────────────────────────────────────────────────
def test_parse_args_defaults_and_flags():
    a = brief.parse_args([])
    assert (a.email, a.self_test, a.out) == (False, False, str(brief.OUT_FILE))
    a = brief.parse_args(["--email", "--self-test", "--out", "x.html"])
    assert (a.email, a.self_test, a.out) == (True, True, "x.html")


def test_self_test_passes():
    assert brief.self_test() == 0


def test_main_self_test_flag():
    assert brief.main(["--self-test"]) == 0


def _ok_fetch(tickers):
    return {t: {"close": 10.0, "date": "2026-10-31", "sma50": 1.0, "sma200": 2.0, "prev": 9.0,
                "darvas": {"signal": "IN_BOX", "top": 11.0, "bottom": 9.0}} for t in tickers}


def test_main_writes_brief_and_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(brief, "fetch", _ok_fetch)
    out = tmp_path / "b.html"
    assert brief.main(["--out", str(out)]) == 0
    html = out.read_text()
    assert "RELIANCE.NS" in html and "AAPL" in html and "Degraded" not in html
    assert f"wrote {out}" in capsys.readouterr().out


def test_main_exit_1_when_every_market_fails(tmp_path, monkeypatch, capsys):
    def boom(tickers):
        raise RuntimeError("down")

    monkeypatch.setattr(brief, "fetch", boom)
    out = tmp_path / "b.html"
    assert brief.main(["--out", str(out)]) == 1
    assert not out.exists()
    assert "refusing to emit an empty brief" in capsys.readouterr().out


def test_main_exit_2_when_only_delivery_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(brief, "fetch", _ok_fetch)

    def failing_send(html, subject):
        raise OSError("smtp down")

    monkeypatch.setattr(brief, "send", failing_send)
    out = tmp_path / "b.html"
    assert brief.main(["--out", str(out), "--email"]) == 2
    assert out.exists()                                  # brief still written
    assert "email FAILED: OSError: smtp down" in capsys.readouterr().out


def test_main_email_unconfigured_is_exit_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(brief, "fetch", _ok_fetch)
    for k in brief.SMTP_KEYS:
        monkeypatch.delenv(k, raising=False)
    assert brief.main(["--out", str(tmp_path / "b.html"), "--email"]) == 0
