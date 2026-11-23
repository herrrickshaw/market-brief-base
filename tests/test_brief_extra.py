"""More offline tests for brief.py: indicator boundaries, fetch details, render, SMTP, main() exit codes."""
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brief  # noqa: E402

NAN = float("nan")
GEN = datetime(2026, 11, 20, 1, 5, tzinfo=timezone.utc)


def box(n=30, last=None):
    base = [105, 104, 103, 104, 105, 104, 103, 104, 105, 104] * (n // 10)
    closes = list(base)
    if last is not None:
        closes.append(last)
    return [c * 1.01 for c in closes], [c * 0.99 for c in closes], closes


def good(close=100.0, prev=99.0, signal="IN_BOX", sma50=110.0, sma200=100.0, date="2026-11-19"):
    return {"close": close, "prev": prev, "date": date, "sma50": sma50, "sma200": sma200,
            "darvas": {"signal": signal, "top": 1.0, "bottom": 0.5}}


# ---- indicators ------------------------------------------------------------------------------------

def test_sma_uses_exactly_the_last_n_of_many():
    assert brief.sma(list(map(float, range(1, 11))), 3) == pytest.approx(9.0)         # (8 + 9 + 10) / 3
    assert brief.sma([5.0], 1) == 5.0
    assert brief.sma([1.0, 2.0], 3) is None


def test_darvas_exactly_twenty_bars_is_enough_and_nineteen_is_not():
    h, l, c = [10.0] * 20, [9.0] * 20, [9.5] * 20
    assert brief.darvas(h, l, c)["signal"] != "INSUFFICIENT_DATA"
    assert brief.darvas(h[:19], l[:19], c[:19])["signal"] == "INSUFFICIENT_DATA"


def series(highs_hist, lows_hist, cur):
    """Historical bars plus a current bar closing at `cur` (the box is formed on the historical bars only)."""
    n = len(highs_hist)
    closes = [4.5] * n + [cur]
    return highs_hist + [cur * 1.01], lows_hist + [cur * 0.99], closes


HIST_H = [5.0] * 20 + [9.0, 6.0, 6.0, 6.0]            # box top 9.0 confirmed by three lower highs
HIST_L = [4.0] * 22 + [3.5, 4.0]                       # box bottom 3.5 (inside the segment from the top onwards)


def test_darvas_top_and_bottom_come_from_the_confirmed_segment():
    d = brief.darvas(*series(HIST_H, HIST_L, 5.0))
    assert d == {"signal": "IN_BOX", "top": 9.0, "bottom": 3.5}


@pytest.mark.parametrize("cur,signal", [(9.0, "IN_BOX"), (9.01, "BREAKOUT_BUY"), (3.5, "IN_BOX"), (3.49, "BREAKDOWN_SELL")])
def test_darvas_close_exactly_on_a_box_edge_is_inside(cur, signal):
    assert brief.darvas(*series(HIST_H, HIST_L, cur))["signal"] == signal


def test_darvas_lows_before_the_top_do_not_lower_the_bottom():
    lows = [1.0] * 20 + [4.0, 4.0, 4.0, 4.0]               # very low bars exist, but only before the top (index 20)
    assert brief.darvas(*series(HIST_H, lows, 5.0))["bottom"] == 4.0


def test_darvas_needs_three_lower_highs_after_the_candidate():
    two_lower_then_higher = [5.0] * 20 + [9.0, 6.0, 6.0, 10.0]
    d = brief.darvas(*series(two_lower_then_higher, [4.0] * 24, 5.0))
    assert d == {"signal": "NO_BOX", "top": None, "bottom": None}
    # the same candidate with its third lower high is a box top
    assert brief.darvas(*series([5.0] * 20 + [9.0, 6.0, 6.0, 6.0], [4.0] * 24, 5.0))["top"] == 9.0


def test_darvas_latest_confirmed_top_wins():
    hist = [5.0] * 12 + [20.0, 6.0, 6.0, 6.0] + [5.0] * 4 + [9.0, 6.0, 6.0, 6.0]
    assert brief.darvas(*series(hist, [4.0] * len(hist), 5.0))["top"] == 9.0


def test_darvas_nan_high_is_skipped_not_chosen():
    hist = [5.0] * 20 + [NAN, 6.0, 6.0, 6.0]
    assert brief.darvas(*series(hist, [4.0] * 24, 5.0))["signal"] == "NO_BOX"
    assert brief.darvas(*series([5.0] * 20 + [9.0, 6.0, 6.0, 6.0], [4.0] * 24, 5.0))["top"] == 9.0


def test_darvas_zero_high_is_never_the_box_top():
    hist = [5.0] * 20 + [0.0, -1.0, -1.0, -1.0]
    d = brief.darvas(*series(hist, [4.0] * 24, 5.0))
    assert d["top"] == 5.0                                  # the real high before the zeros, not 0.0


def test_darvas_all_lows_unusable_is_no_box():
    assert brief.darvas(*series(HIST_H, [NAN] * 24, 5.0)) == {"signal": "NO_BOX", "top": None, "bottom": None}
    assert brief.darvas(*series(HIST_H, [0.0] * 24, 5.0)) == {"signal": "NO_BOX", "top": None, "bottom": None}


def test_trend_labels_and_equal_averages():
    assert brief.trend({"sma50": 2, "sma200": 1}) == "Golden Cross"
    assert brief.trend({"sma50": 1, "sma200": 2}) == "Death Cross"
    assert brief.trend({"sma50": 2, "sma200": 2}) == "Death Cross"          # equal is not golden
    assert brief.trend({"sma50": None, "sma200": 2}) == "—"
    assert brief.trend({"sma50": 2}) == "—"
    assert brief.trend({"sma50": 0, "sma200": 2}) == "—"                # a zero average is treated as missing
    assert brief.trend({}) == "—"


# ---- fetch ------------------------------------------------------------------------------------------

def install_yf(monkeypatch, frames):
    seen = {}

    def download(tickers, **kw):
        seen.update(tickers=tickers, kw=kw)
        if len(tickers) == 1:
            return frames[tickers[0]]
        return pd.concat({t: frames[t] for t in tickers}, axis=1)

    mod = types.ModuleType("yfinance")
    mod.download = download
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    return seen


def frame(closes, start="2025-01-02"):
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({"Close": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes]}, index=idx)


def test_fetch_computes_averages_dates_and_previous_close(monkeypatch):
    closes = [100.0 + i for i in range(260)]
    seen = install_yf(monkeypatch, {"AAA": frame(closes), "BBB": frame(closes)})
    out = brief.fetch(["AAA", "BBB"])
    r = out["AAA"]
    assert r["close"] == 359.0 and r["prev"] == 358.0
    assert r["sma50"] == pytest.approx(sum(closes[-50:]) / 50) and r["sma200"] == pytest.approx(sum(closes[-200:]) / 200)
    assert r["date"] == pd.bdate_range("2025-01-02", periods=260)[-1].date().isoformat()
    assert r["darvas"]["signal"] in {"BREAKOUT_BUY", "NO_BOX", "IN_BOX", "BREAKDOWN_SELL"}
    assert seen["kw"] == {"period": "1y", "auto_adjust": False, "progress": False, "group_by": "ticker", "threads": True}


def test_fetch_single_ticker_uses_flat_frame_and_short_history_has_no_sma200(monkeypatch):
    install_yf(monkeypatch, {"AAA": frame([10.0, 11.0, 12.0])})
    r = brief.fetch(["AAA"])["AAA"]
    assert r["close"] == 12.0 and r["prev"] == 11.0 and r["sma50"] is None and r["sma200"] is None
    assert r["darvas"]["signal"] == "INSUFFICIENT_DATA"


def test_fetch_single_bar_has_no_previous_close(monkeypatch):
    install_yf(monkeypatch, {"AAA": frame([10.0])})
    assert brief.fetch(["AAA"])["AAA"]["prev"] is None


def test_fetch_error_message_is_truncated_to_sixty_chars(monkeypatch):
    class Boom(Exception):
        pass

    class Frame:
        def __getitem__(self, k):
            raise Boom("x" * 200)

    mod = types.ModuleType("yfinance")
    mod.download = lambda tickers, **kw: Frame()
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    err = brief.fetch(["AAA"])["AAA"]["error"]
    assert err == "Boom: " + "x" * 60


def test_fetch_no_settled_bars_message(monkeypatch):
    install_yf(monkeypatch, {"AAA": frame([NAN, 0.0, NAN])})
    assert brief.fetch(["AAA"])["AAA"] == {"error": "no settled bars"}


def test_universe_and_benchmarks_are_the_documented_sets():
    assert set(brief.UNIVERSE) == {"India", "US"} == set(brief.BENCH)
    assert len(brief.UNIVERSE["India"]) == 15 and len(brief.UNIVERSE["US"]) == 15
    assert all(t.endswith(".NS") for t in brief.UNIVERSE["India"])
    assert brief.LOOKBACK == "1y" and brief.DARVAS_CONFIRM == 3


# ---- render ------------------------------------------------------------------------------------------

def test_render_rows_sorted_and_coloured():
    html = brief.render({"US": {"ZZZ": good(close=101.0, prev=100.0, signal="BREAKOUT_BUY"),
                                 "AAA": good(close=99.0, prev=100.0, signal="BREAKDOWN_SELL"),
                                 "MMM": good(close=100.0, prev=100.0, signal="IN_BOX")}}, {}, GEN)
    assert html.index("<b>AAA</b>") < html.index("<b>MMM</b>") < html.index("<b>ZZZ</b>")
    assert "<td class='up'>+1.00%</td>" in html and "<td class='dn'>-1.00%</td>" in html
    assert "<td class='dn'>+0.00%</td>" in html                                  # flat is not "up"
    assert "<td class='up'>BREAKOUT_BUY</td>" in html and "<td class='dn'>BREAKDOWN_SELL</td>" in html
    assert "<td class='mut'>IN_BOX</td>" in html
    assert "<td>101.00</td>" in html and "Golden Cross" in html


def test_render_missing_previous_close_shows_dash():
    html = brief.render({"US": {"AAA": good(prev=None)}}, {}, GEN)
    assert "<td class='dn'>—</td>" in html


def test_render_banner_only_when_failures_and_lists_each_market():
    clean = brief.render({"US": {"AAA": good()}}, {}, GEN)
    assert "Degraded" not in clean
    html = brief.render({"US": {"AAA": good()}}, {"India": "no usable data", "Japan": "ValueError"}, GEN)
    assert "<b>Degraded:</b> India (no usable data); Japan (ValueError)." in html


def test_render_header_footer_and_markets_in_order():
    html = brief.render({"India": {"AAA": good()}, "US": {"BBB": good()}}, {}, GEN)
    assert "<h1>Daily Market Brief — 20 Nov 2026</h1>" in html
    assert "Generated 2026-11-20 01:05 UTC" in html
    assert html.index("<h2>India</h2>") < html.index("<h2>US</h2>")
    assert "NOT investment advice" in html and html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    assert "<th>Symbol</th><th>Close</th><th>As of</th><th>Day</th><th>Trend</th><th>Darvas</th>" in html


# ---- send --------------------------------------------------------------------------------------------

def clear_smtp(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_TO"):
        monkeypatch.delenv(k, raising=False)


class Smtp:
    log = []

    def __init__(self, host, port, timeout):
        Smtp.log = [("init", host, port, timeout)]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        Smtp.log.append(("tls",))

    def login(self, u, p):
        Smtp.log.append(("login", u, p))

    def sendmail(self, frm, to, msg):
        Smtp.log.append(("send", frm, to, msg))


def test_send_message_structure_defaults_and_order_of_calls(monkeypatch, capsys):
    clear_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_USER", "u@example.test")
    monkeypatch.setenv("SMTP_PASS", "pw")
    monkeypatch.setattr(brief.smtplib, "SMTP", Smtp)
    assert brief.send("<p>hi</p>", "Subj") is True
    kinds = [e[0] for e in Smtp.log]
    assert kinds == ["init", "tls", "login", "send"]
    assert Smtp.log[0] == ("init", "smtp.gmail.com", 587, 45)
    assert Smtp.log[2] == ("login", "u@example.test", "pw")
    _, frm, to, msg = Smtp.log[3]
    assert (frm, to) == ("u@example.test", ["u@example.test"])                  # recipient defaults to the sender
    assert "Subject: Subj" in msg and "From: u@example.test" in msg and "To: u@example.test" in msg
    assert "multipart/alternative" in msg and "text/plain" in msg and "text/html" in msg
    assert msg.index("text/plain") < msg.index("text/html")                       # plain part first, HTML preferred last
    assert "email: sent to u@example.test" in capsys.readouterr().out


def test_send_honours_host_port_and_explicit_recipient(monkeypatch):
    clear_smtp(monkeypatch)
    for k, v in {"SMTP_HOST": "mail.example.test", "SMTP_PORT": "2525", "SMTP_USER": "u@x.test",
                 "SMTP_PASS": "pw", "SMTP_TO": "to@x.test"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(brief.smtplib, "SMTP", Smtp)
    brief.send("<p/>", "s")
    assert Smtp.log[0] == ("init", "mail.example.test", 2525, 45)
    assert Smtp.log[3][2] == ["to@x.test"]


@pytest.mark.parametrize("missing", ["SMTP_USER", "SMTP_PASS"])
def test_send_reports_exactly_which_settings_are_missing(monkeypatch, capsys, missing):
    clear_smtp(monkeypatch)
    for k in ("SMTP_USER", "SMTP_PASS"):
        if k != missing:
            monkeypatch.setenv(k, "v")
    monkeypatch.setattr(brief.smtplib, "SMTP", lambda *a, **k: pytest.fail("must not connect"))
    assert brief.send("<p/>", "s") is False
    out = capsys.readouterr().out
    assert f"not configured ({missing})" in out or f"not configured ({missing}, SMTP_TO)" in out


def test_send_bad_port_message_names_the_value(monkeypatch, capsys):
    clear_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_PORT", "5x7")
    assert brief.send("<p/>", "s") is False
    assert "SMTP_PORT='5x7' is not a number" in capsys.readouterr().out


def test_send_propagates_delivery_errors(monkeypatch):
    clear_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_USER", "u@x.test")
    monkeypatch.setenv("SMTP_PASS", "pw")

    class Broken(Smtp):
        def login(self, u, p):
            raise OSError("auth failed")

    monkeypatch.setattr(brief.smtplib, "SMTP", Broken)
    with pytest.raises(OSError):
        brief.send("<p/>", "s")


def test_env_strips_whitespace_around_real_values(monkeypatch):
    monkeypatch.setenv("_T_V", "  587  ")
    assert brief._env("_T_V", "1") == "587"
    assert brief._env("_T_NOPE") is None


# ---- main --------------------------------------------------------------------------------------------

def fake_fetch(markets_down=()):
    def _f(tickers):
        if tickers[0] in markets_down:
            raise RuntimeError("down")
        return {t: good() for t in tickers}
    return _f


def run_main(monkeypatch, tmp_path, *argv, fetch=None, send=None):
    monkeypatch.setattr(brief, "fetch", fetch or fake_fetch())
    sent = []
    monkeypatch.setattr(brief, "send", send or (lambda html, subject: sent.append((html, subject)) or True))
    out = tmp_path / "b.html"
    monkeypatch.setattr(sys, "argv", ["brief.py", "--out", str(out), *argv])
    return brief.main(), out, sent


def test_main_writes_html_prints_counts_and_does_not_email_by_default(monkeypatch, tmp_path, capsys):
    rc, out, sent = run_main(monkeypatch, tmp_path)
    assert rc == 0 and sent == []
    assert out.read_text().startswith("<!doctype html>")
    text = capsys.readouterr().out
    assert "India: 15/15 priced" in text and "US: 15/15 priced" in text and f"wrote {out} (" in text


def test_main_email_subject_marks_degraded_markets(monkeypatch, tmp_path):
    rc, out, sent = run_main(monkeypatch, tmp_path, "--email")
    assert rc == 0 and len(sent) == 1
    assert sent[0][1].startswith("Daily Market Brief — ") and "degraded" not in sent[0][1]
    rc, out, sent = run_main(monkeypatch, tmp_path, "--email", fetch=fake_fetch(markets_down=(brief.UNIVERSE["US"][0],)))
    assert rc == 0 and sent[0][1].endswith("(1 degraded)")
    assert "US (RuntimeError)" in sent[0][0]


def test_main_market_with_no_usable_rows_is_flagged_but_still_listed(monkeypatch, tmp_path, capsys):
    def only_errors(tickers):
        return {t: {"error": "no settled bars"} for t in tickers} if tickers[0] == "AAPL" else {t: good() for t in tickers}
    rc, out, _ = run_main(monkeypatch, tmp_path, fetch=only_errors)
    html = out.read_text()
    assert rc == 0 and "US (no usable data)" in html and "no settled bars" in html
    assert "US: 0/15 priced" in capsys.readouterr().out


def test_main_delivery_failure_is_exit_two_but_brief_is_written(monkeypatch, tmp_path, capsys):
    def boom(html, subject):
        raise OSError("smtp down " + "x" * 200)
    rc, out, _ = run_main(monkeypatch, tmp_path, "--email", send=boom)
    assert rc == 2 and out.exists()
    assert "email FAILED: OSError: smtp down" in capsys.readouterr().out


def test_main_unconfigured_send_is_not_a_failure(monkeypatch, tmp_path):
    rc, _, _ = run_main(monkeypatch, tmp_path, "--email", send=lambda h, s: False)
    assert rc == 0


def test_main_every_market_down_exits_one_without_writing(monkeypatch, tmp_path, capsys):
    rc, out, sent = run_main(monkeypatch, tmp_path, "--email",
                             fetch=fake_fetch(markets_down=(brief.UNIVERSE["India"][0], brief.UNIVERSE["US"][0])))
    assert rc == 1 and not out.exists() and sent == []
    assert "refusing to emit an empty brief" in capsys.readouterr().out


def test_main_self_test_flag_skips_network_and_files(monkeypatch, tmp_path):
    monkeypatch.setattr(brief, "fetch", lambda t: pytest.fail("no network in --self-test"))
    monkeypatch.setattr(sys, "argv", ["brief.py", "--self-test", "--out", str(tmp_path / "b.html")])
    assert brief.main() == 0 and not (tmp_path / "b.html").exists()


def test_self_test_output_lists_passes_and_restores_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("SMTP_USER", "keep@x.test")
    monkeypatch.setenv("SMTP_PORT", "2525")
    assert brief.self_test() == 0
    out = capsys.readouterr().out
    assert out.count("PASS") >= 10 and "FAIL" not in out and "self-test: OK" in out
    import os
    assert os.environ["SMTP_USER"] == "keep@x.test" and os.environ["SMTP_PORT"] == "2525"
    assert "_T_SET" not in os.environ


def test_main_returns_the_self_test_result(monkeypatch, tmp_path):
    monkeypatch.setattr(brief, "self_test", lambda: 1)
    monkeypatch.setattr(sys, "argv", ["brief.py", "--self-test"])
    assert brief.main() == 1


def test_self_test_fails_loudly_when_the_logic_is_broken(monkeypatch, capsys):
    monkeypatch.setattr(brief, "sma", lambda vals, n: 0.0)                    # an "invented" average
    assert brief.self_test() == 1
    out = capsys.readouterr().out
    assert "FAIL  sma returns None rather than inventing a short average" in out and "self-test: FAILED" in out
    assert "PASS" in out                                                          # unrelated checks still report


def test_self_test_reports_an_unexpected_send_exception_as_a_failure(monkeypatch, capsys):
    def boom(html, subject):
        raise RuntimeError("smtp exploded")
    monkeypatch.setattr(brief, "send", boom)
    assert brief.self_test() == 1
    assert "FAIL  unconfigured send raised RuntimeError" in capsys.readouterr().out
