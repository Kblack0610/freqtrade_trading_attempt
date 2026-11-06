import numpy as np

from lab.markets.research import kalshi_calibration as kc

EXP = "2026-10-01T20:00:00Z"
T = kc.ts(EXP)


def market(close=EXP, result="yes", event="E1", cat="sports", ticker="M1"):
    return {"ticker": ticker, "event_ticker": event, "cat": cat, "result": result, "close_time": close,
            "expected_expiration_time": EXP, "open_time": "2026-09-20T00:00:00Z"}


def candle(end, bid, ask):
    return {"end_period_ts": end, "yes_bid": {"close_dollars": str(bid)}, "yes_ask": {"close_dollars": str(ask)}}


def test_fee_matches_kalshi_formula_with_cent_rounding():
    # 20 contracts at 0.50: 0.07 * 20 * 0.25 = 0.35 dollars, 0.0175 a contract.
    assert abs(kc.fee(0.5, stake=10) - 0.0175) < 1e-12
    # One contract at 0.50: 0.0175 rounds up to 0.02.
    assert abs(kc.fee(0.5, stake=0.5) - 0.02) < 1e-12
    assert kc.fee(0.0) == 0 and kc.fee(1.0) == 0


def test_entry_counts_back_from_the_scheduled_expiration_not_the_close():
    early = market(close="2026-10-01T19:30:00Z")
    assert kc.entry_ts(early, 1) == T - 3600
    # Closed 2h early: a 1h-before trade was never possible, so the market is not used there.
    gone = market(close="2026-10-01T18:00:00Z")
    assert kc.entry_ts(gone, 1) is None and kc.entry_ts(gone, 6) == T - 6 * 3600


def test_quote_needs_both_sides_and_a_fresh_candle():
    cs = [candle(T - 7200, 0.40, 0.45), candle(T - 3600, 0.0, 0.45)]
    assert kc.quote_at(cs, T - 7200) == (0.40, 0.45)
    assert kc.quote_at(cs, T - 3600) is None  # no bid
    assert kc.quote_at(cs, T - 3 * 3600 - 1) is None  # nothing at or before
    assert kc.quote_at([candle(T - 5 * 3600, 0.4, 0.45)], T - 3600) is None  # stale


def test_rows_pay_the_ask_and_net_the_fee():
    cs = [candle(T - h * 3600, 0.40, 0.45) for h in (24, 6, 1)]
    out = kc.rows([market()], {"M1": cs})
    assert len(out) == 6
    yes = next(r for r in out if r["side"] == "yes" and r["h"] == 1)
    no = next(r for r in out if r["side"] == "no" and r["h"] == 1)
    assert abs(yes["pnl"] - (1 - 0.45 - kc.fee(0.45))) < 1e-12
    assert abs(no["entry"] - 0.60) < 1e-12 and abs(no["pnl"] - (0 - 0.60 - kc.fee(0.60))) < 1e-12


def test_bootstrap_resamples_events_not_rows():
    # One event with 100 identical rows is one draw: the CI cannot shrink to a point from it.
    pnl = np.array([1.0] * 100 + [-1.0])
    groups = np.array(["a"] * 100 + ["b"])
    lo, hi = kc.boot_ci(pnl, groups, 0.05, np.random.default_rng(0))
    assert lo < 0 < hi


def test_every_cell_counts_as_a_trial_even_when_too_thin_to_score():
    scored, trials = kc.score(kc.cells([]))
    assert trials == 5 * 3 * 2 * 10 and scored == []


def test_sample_caps_each_series_and_drops_markets_closed_before_any_entry():
    ms = [dict(market(ticker=f"A{i}"), series="A") for i in range(5)] + [dict(market(ticker="B0"), series="B")]
    ms.append(dict(market(ticker="C0", close="2026-09-30T19:00:00Z"), series="C"))
    got = kc.sample(ms, cap=2)
    assert sorted(m["series"] for m in got) == ["A", "A", "B"]
    assert kc.sample(ms, cap=2) == got  # stable


def test_win_lower_matches_the_closed_form_for_a_perfect_record():
    # 74 wins in 74: the bound is alpha ** (1 / 74), about 0.960, under a 0.964 break-even.
    assert abs(kc.win_lower(74, 74, 0.05) - 0.05 ** (1 / 74)) < 1e-6
    assert kc.win_lower(0, 10, 0.05) == 0.0
    assert kc.win_lower(50, 100, 0.05) < 0.5
