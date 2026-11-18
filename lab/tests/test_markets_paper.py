import copy
import datetime as dt

import pandas as pd
import pytest
from psycopg.types.json import Jsonb

from lab import watcher
from lab.markets import backtest, paper
from lab.markets.recorder import load_config

NOW = dt.datetime(2026, 11, 6, 12, tzinfo=dt.timezone.utc)
BANK = {"stage": "Stage 1", "cap": 500.0, "hard_stop": 0.15}
BAND = {"stake": 10, "max_spread": 0.05, "hours": 24, "bands": [{"buy": "no", "lo": 0.01, "hi": 0.10}]}


def cfg(**paper_over):
    """The shipped config with one longshot variant (BAND) and no forward family, so the readiness gate decides the
    league and a test sees only the accounts it seeded; a test of the forward path passes forward=."""
    c = copy.deepcopy(load_config())
    c["families"]["longshot"] = BAND
    c["paper"].update({"forward": {}, **paper_over})
    return c


PARAMS = {"stake": 10, "max_spread": 0.05, "hours": 24, "buy": "no", "lo": 0.01, "hi": 0.10}
VARIANT = backtest.variant_of(PARAMS)
ACCOUNT = paper.account_of("longshot", VARIANT, "kalshi")


def ready(conn, family="longshot", params=PARAMS, ok=True):
    """The readiness gate's row for one variant (lab/markets/families.py gate())."""
    conn.execute("DELETE FROM market_readiness WHERE family = %s AND variant = %s",
                 (family, backtest.variant_of(params)))
    conn.execute("INSERT INTO market_readiness (family, venue, variant, params, windows, trades, trials, ready, passed, "
                 "gates) VALUES (%s, 'kalshi', %s, %s, 6, 400, 30, %s, 7, '[]')",
                 (family, backtest.variant_of(params), Jsonb(params), ok))


def seed(conn, sweep_age=dt.timedelta(minutes=5), gate=True):
    """A: a longshot closing in 12 h (enters, buys no at 0.95); B: the same price closing in 48 h (outside hours);
    C: a coin flip closing in 12 h (outside the band)."""
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES ('kalshi', 'Kalshi', 'exchange')")
    for mid, close_h, bid, ask in (("A", 12, 0.05, 0.07), ("B", 48, 0.05, 0.07), ("C", 12, 0.49, 0.51)):
        conn.execute("INSERT INTO markets (venue, market_id, title, market_type, status, close_time, first_seen, "
                     "updated_at) VALUES ('kalshi', %s, %s, 'binary', 'active', %s, %s, %s)",
                     (mid, mid, NOW + dt.timedelta(hours=close_h), NOW, NOW))
        conn.execute("INSERT INTO outcomes (venue, market_id, outcome, bid, ask, bid_size, ask_size, quoted_at) "
                     "VALUES ('kalshi', %s, 'yes', %s, %s, 100, 100, %s)", (mid, bid, ask, NOW - dt.timedelta(minutes=7)))
    conn.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, resolved, ok) "
                 "VALUES ('kalshi', %s, %s, 3, 3, 3, 0, true)", (NOW - sweep_age, NOW - sweep_age))
    if gate:
        ready(conn)


def orders(conn):
    return conn.execute("SELECT account, market_id, side, contracts, price, fee, settled_at, payout, profit "
                        "FROM paper_orders ORDER BY id").fetchall()


def equity(conn, account=ACCOUNT):
    return conn.execute("SELECT equity, realized, open_positions, marked_at FROM paper_accounts WHERE account = %s",
                        (account,)).fetchone()


def test_an_account_enters_a_market_once_at_the_backtest_fill_and_marks_at_the_exit_price(conn):
    seed(conn)
    out = paper.run_pass(conn, "kalshi", cfg(), NOW)
    assert out["fresh"] and out["accounts"][ACCOUNT] == {"orders": 1, "fills": 1, "no_cash": 0, "capped": 0}
    # no at 1 - bid = 0.95; 10 contracts in a 10 dollar stake; Kalshi fee ceil(0.07 * 10 * 0.95 * 0.05) = 0.04
    [o] = orders(conn)
    assert o[:6] == (ACCOUNT, "A", "no", 10, pytest.approx(0.95), pytest.approx(0.04))
    # the no exits at 1 - ask = 0.93: 9.30 - 9.50 - 0.04
    assert equity(conn)[:3] == (pytest.approx(999.76), 0, 1) and equity(conn)[3] == NOW

    out = paper.run_pass(conn, "kalshi", cfg(), NOW + dt.timedelta(minutes=5))
    assert out["accounts"][ACCOUNT]["orders"] == 0 and len(orders(conn)) == 1


def test_a_settled_fill_pays_what_the_backtest_pays(conn):
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    settled = NOW + dt.timedelta(hours=12)
    conn.execute("INSERT INTO resolutions (venue, market_id, result, value, settled_at) "
                 "VALUES ('kalshi', 'A', 'no', 0, %s)", (settled,))
    out = paper.run_pass(conn, "kalshi", cfg(), settled + dt.timedelta(minutes=5))
    assert out["settled"] == 1
    *_, settled_at, pay, profit = orders(conn)[0]
    fills = pd.DataFrame([{"market_id": "A", "side": "no", "open_date": pd.Timestamp(NOW), "contracts": 10,
                           "price": 0.95, "fee": 0.04}])
    res = {"A": backtest.Resolution("A", "no", 0.0, settled)}
    expect = backtest.settle(fills, res, load_config()["backtest"]["fees"]["kalshi"]).iloc[0]
    assert (settled_at, pay, profit) == (settled, expect.close_rate, pytest.approx(expect.profit_abs))
    assert profit == pytest.approx(0.46)
    assert equity(conn)[:3] == (pytest.approx(1000.46), pytest.approx(0.46), 0)


def test_a_dead_recorder_means_no_entries(conn):
    seed(conn, sweep_age=dt.timedelta(minutes=20))
    out = paper.run_pass(conn, "kalshi", cfg(), NOW)
    assert not out["fresh"] and out["book"] == 0 and orders(conn) == []
    assert equity(conn)[0] == 1000  # still marked, so the watcher sees the pass


def test_an_order_the_cash_cannot_pay_for_is_not_stored_and_asked_again(conn):
    seed(conn)
    out = paper.run_pass(conn, "kalshi", cfg(capital=5), NOW)
    assert out["accounts"][ACCOUNT] == {"orders": 0, "fills": 0, "no_cash": 1, "capped": 0} and orders(conn) == []
    conn.execute("UPDATE paper_accounts SET capital = 1000")
    out = paper.run_pass(conn, "kalshi", cfg(), NOW + dt.timedelta(minutes=5))
    assert out["accounts"][ACCOUNT]["fills"] == 1


def test_only_ready_variants_trade_and_one_that_stops_passing_retires_and_comes_back(conn):
    seed(conn, gate=False)
    out = paper.run_pass(conn, "kalshi", cfg(), NOW)
    assert out["accounts"] == {} and orders(conn) == []  # nothing passed the gate: no league

    ready(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW + dt.timedelta(minutes=1))
    assert [o[1] for o in orders(conn)] == ["A"]

    ready(conn, ok=False)
    conn.execute("UPDATE outcomes SET bid = 0.05, ask = 0.07 WHERE market_id = 'C'")
    later = NOW + dt.timedelta(minutes=2)
    out = paper.run_pass(conn, "kalshi", cfg(), later)
    assert out["retired"] == [ACCOUNT] and out["accounts"] == {} and len(orders(conn)) == 1
    assert conn.execute("SELECT retired_at FROM paper_accounts").fetchone()[0] == later
    assert equity(conn)[3] == later  # a retired account still marks

    ready(conn)
    out = paper.run_pass(conn, "kalshi", cfg(), NOW + dt.timedelta(minutes=3))
    assert out["retired"] == [] and out["accounts"][ACCOUNT]["fills"] == 1
    assert conn.execute("SELECT retired_at FROM paper_accounts").fetchone()[0] is None


def test_every_family_that_can_run_on_kalshi_trades_paper_without_a_gate_row(conn):
    """Paper is the forward test: the shipped [paper] forward puts longshot, weather and observed on kalshi with no ready row,
    and a family outside forward still waits on the gate (the negative control)."""
    shipped = load_config()
    forward = shipped["paper"]["forward"]
    assert set(forward["kalshi"]) == {"longshot", "weather", "observed"}
    seed(conn, gate=False)
    out = paper.run_pass(conn, "kalshi", cfg(forward=forward), NOW)
    weather = {paper.account_of(f, backtest.variant_of(p), "kalshi") for f in ("weather", "observed")
               for p in paper.variants(shipped["families"][f])}
    assert set(out["accounts"]) == {ACCOUNT} | weather and out["retired"] == []
    assert out["accounts"][ACCOUNT]["fills"] == 1 and [o[1] for o in orders(conn)] == ["A"]
    assert all(out["accounts"][a]["orders"] == 0 for a in weather)  # no NWS reading: weather and observed place nothing

    out = paper.run_pass(conn, "kalshi", cfg(forward={"kalshi": ["weather"]}), NOW + dt.timedelta(minutes=1))
    # longshot and observed have no forward and no gate row now
    assert ACCOUNT in out["retired"] and ACCOUNT not in out["accounts"]
    assert {a.split("-")[1] for a in out["retired"]} == {"longshot", "observed"} and set(out["accounts"]) == weather - {a for a in weather if "observed" in a}


def test_a_ready_variant_of_a_family_that_needs_a_fit_is_reported_not_traded(conn):
    seed(conn, gate=False)
    ready(conn, family="calibration", params={"edge": 0.05})
    out = paper.run_pass(conn, "kalshi", cfg(), NOW)
    assert out["accounts"] == {} and out["unrunnable"] == [f"calibration/{backtest.variant_of({'edge': 0.05})[:8]}"]


def test_an_account_at_max_open_takes_no_entry_and_asks_again_once_one_settles(conn):
    seed(conn)
    conn.execute("UPDATE outcomes SET bid = 0.05, ask = 0.07 WHERE market_id = 'C'")
    out = paper.run_pass(conn, "kalshi", cfg(max_open=1), NOW)
    assert out["accounts"][ACCOUNT] == {"orders": 1, "fills": 1, "no_cash": 0, "capped": 1}
    assert [o[1] for o in orders(conn)] == ["A"]
    conn.execute("INSERT INTO resolutions (venue, market_id, result, value, settled_at) "
                 "VALUES ('kalshi', 'A', 'no', 0, %s)", (NOW,))
    out = paper.run_pass(conn, "kalshi", cfg(max_open=1), NOW + dt.timedelta(minutes=5))
    assert out["accounts"][ACCOUNT]["fills"] == 1 and [o[1] for o in orders(conn)] == ["A", "C"]


def check(conn):
    return conn.execute("SELECT paper_trades, paper_expectancy, backtest_trades, gap_lo, gap_hi, drifted "
                        "FROM paper_accounts WHERE account = %s", (ACCOUNT,)).fetchone()


def backtested(conn, returns):
    conn.execute("INSERT INTO market_backtests (family, venue, variant, params, start_at, end_at, orders, fills, "
                 "trades, open_fills, metrics, returns, trials) VALUES ('longshot', 'kalshi', %s, %s, %s, %s, 0, 0, 0, "
                 "0, '{}', %s, 30)", (VARIANT, Jsonb(PARAMS), NOW - dt.timedelta(days=60), NOW - dt.timedelta(days=30),
                               returns))


def settled_fills(conn, profits, first=0):
    for i, p in enumerate(profits, first):
        conn.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, "
                     "fee, settled_at, payout, profit) VALUES (%s, %s, 'no', 10, %s, %s, 10, 0.95, 0.05, %s, 1, %s)",
                     (ACCOUNT, f"S{i}", NOW, NOW, NOW, p))


@pytest.mark.parametrize("paper_profit, drifted", [(0.46, False), (-9.5, True)])
def test_the_forward_check_flags_paper_that_parts_from_its_backtest(conn, paper_profit, drifted):
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    assert check(conn)[3:] == (None, None, None)  # nothing settled, no backtest: no verdict
    backtested(conn, [0.096, 0.0] * 50)  # mean 0.048, paper's 0.46 / 9.55
    settled_fills(conn, [paper_profit] * 29)
    paper.run_pass(conn, "kalshi", cfg(drift_min_trades=30), NOW + dt.timedelta(minutes=1))
    assert check(conn)[0] == 29 and check(conn)[5] is None  # under drift_min_trades
    settled_fills(conn, [paper_profit], first=29)
    out = paper.run_pass(conn, "kalshi", cfg(drift_min_trades=30), NOW + dt.timedelta(minutes=2))
    n, mean, bt, lo, hi, flag = check(conn)
    assert (n, bt, flag, out["drifted"]) == (30, 100, drifted, int(drifted))
    assert mean == pytest.approx(paper_profit / 9.55) and lo <= hi


def test_the_watcher_drill_stops_a_paper_account_and_ending_it_starts_it_again(conn):
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    watcher.tick(conn, [], 0.03, None, NOW, BANK)
    w = conn.execute("SELECT dry_run, state, equity, seen_at FROM watch WHERE bot = %s", (ACCOUNT,)).fetchone()
    assert w == (True, "running", pytest.approx(999.76), NOW)

    watcher.drill(conn, ACCOUNT)
    later = NOW + dt.timedelta(minutes=1)
    watcher.tick(conn, [], 0.03, None, later, BANK)
    assert conn.execute("SELECT state, stopped_at, breached_at FROM watch WHERE bot = %s",
                        (ACCOUNT,)).fetchone() == ("paused", later, later)
    # a new market qualifies, but the stopped account takes no entry; its marks go on
    conn.execute("UPDATE outcomes SET bid = 0.05, ask = 0.07 WHERE market_id = 'C'")
    out = paper.run_pass(conn, "kalshi", cfg(), later + dt.timedelta(minutes=1))
    assert out["stopped"] == [ACCOUNT] and ACCOUNT not in out["accounts"] and len(orders(conn)) == 1
    assert equity(conn)[3] == later + dt.timedelta(minutes=1)

    watcher.drill_end(conn, ACCOUNT, [], None)  # no API to call: fetch is never touched
    watcher.tick(conn, [], 0.03, None, later + dt.timedelta(minutes=2), BANK)
    out = paper.run_pass(conn, "kalshi", cfg(), later + dt.timedelta(minutes=3))
    assert out["accounts"][ACCOUNT]["fills"] == 1 and [o[1] for o in orders(conn)] == ["A", "C"]


def test_a_paper_loss_past_the_day_limit_breaches_without_stopping(conn):
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    watcher.tick(conn, [], 0.03, None, NOW, BANK)
    conn.execute("UPDATE paper_accounts SET equity = 960, marked_at = %s", (NOW + dt.timedelta(minutes=5),))
    watcher.tick(conn, [], 0.03, None, NOW + dt.timedelta(minutes=6), BANK)
    breached, stopped, seen = conn.execute("SELECT breached_at, stopped_at, seen_at FROM watch WHERE bot = %s",
                                           (ACCOUNT,)).fetchone()
    assert breached is not None and stopped is None and seen == NOW + dt.timedelta(minutes=5)


def test_the_watcher_skips_an_account_whose_venue_left_paper(conn):
    """No pass marks an account once its venue leaves [paper] venues (odds_api, ar-2743), so watching it would report
    it blind forever; a kalshi account is still watched (the control)."""
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at, equity, "
                 "marked_at) VALUES ('paper-tt_totals-gone', 'tt_totals', 'odds_api', 'v', '{}', 1000, %s, 1000, %s)",
                 (NOW, NOW))
    assert "odds_api" not in load_config()["paper"]["venues"]
    watcher.tick(conn, [], 0.03, None, NOW, BANK)
    assert {b for (b,) in conn.execute("SELECT bot FROM watch")} == {ACCOUNT}


# tt_totals on a sportsbook: the forward test of a family no backtest can judge yet
def seed_odds(conn, meetings=6):
    """Ann and Bea met `meetings` times before NOW, every one 75 points (over 70.5 and 72.5); their next game, G,
    starts in 10 h with two lines at two books, the over at decimal 2.0 and the under at 1.9."""
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES ('odds_api', 'The Odds API', 'sportsbook')")
    for i in range(meetings):
        mid, start = f"P{i}:totals", NOW - dt.timedelta(days=10 - i)
        conn.execute("INSERT INTO markets (venue, market_id, series_id, title, market_type, status, close_time, "
                     "first_seen, updated_at) VALUES ('odds_api', %s, 'tt', %s, 'totals', 'settled', %s, %s, %s)",
                     (mid, "Ann at Bea" if i % 2 else "Bea at Ann", start, start, start))
        conn.execute("INSERT INTO resolutions (venue, market_id, result, settled_at) VALUES ('odds_api', %s, "
                     "'Total 75', %s)", (mid, start + dt.timedelta(hours=1)))
    conn.execute("INSERT INTO markets (venue, market_id, series_id, title, market_type, status, close_time, "
                 "first_seen, updated_at) VALUES ('odds_api', 'G:totals', 'tt', 'Ann at Bea', 'totals', 'upcoming', "
                 "%s, %s, %s)", (NOW + dt.timedelta(hours=10), NOW, NOW))
    for book in ("b1", "b2"):
        for line in ("70.5", "72.5"):
            for side, ask in (("Over", 0.5), ("Under", 1 / 1.9)):
                conn.execute("INSERT INTO outcomes (venue, market_id, outcome, ask, quoted_at) VALUES ('odds_api', "
                             "'G:totals', %s, %s, %s)", (f"{book}:{side} {line}", ask, NOW - dt.timedelta(hours=3)))
    sweep(conn, NOW)


def sweep(conn, at):
    conn.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, resolved, ok) "
                 "VALUES ('odds_api', %s, %s, 1, 0, 1, 0, true)", (at - dt.timedelta(minutes=2), at))


def test_tt_totals_trades_the_line_book_without_a_gate_row_settles_its_line_and_reports_roi_and_clv(conn):
    seed_odds(conn)
    c = load_config()
    c["paper"]["forward"] = {"odds_api": ["tt_totals"]}  # no shipped venue runs tt_totals until one records table tennis
    out = paper.run_pass(conn, "odds_api", c, NOW)
    n = len(paper.variants(c["families"]["tt_totals"]))
    assert out["fresh"] and out["book"] == 2 and len(out["accounts"]) == n == 4  # every variant, no readiness row
    # six meetings: the min_h2h 5 variants buy the over of one line, once for the game; min_h2h 10 waits
    fills = [o for o in orders(conn) if o[3]]
    assert sorted(out["accounts"][a]["fills"] for a in out["accounts"]) == [0, 0, 1, 1]
    assert [o[1:6] for o in fills] == [("G:totals|70.5", "yes", 20, 0.5, 0)] * 2

    # the over shortens to 0.55 before the start: the fill's closing line moves our way
    conn.execute("UPDATE outcomes SET ask = 0.55, quoted_at = %s WHERE outcome LIKE '%%Over 70.5'",
                 (NOW + dt.timedelta(hours=5),))
    sweep(conn, NOW + dt.timedelta(hours=6))
    out = paper.run_pass(conn, "odds_api", c, NOW + dt.timedelta(hours=6))
    assert sum(a["orders"] for a in out["accounts"].values()) == 0
    mid = (1 - 1 / 1.9 + 0.55) / 2
    closes = [r[0] for r in conn.execute("SELECT close_mid FROM paper_orders WHERE contracts > 0")]
    assert closes == [pytest.approx(mid)] * 2

    # G ends 74: over 70.5 pays one a contract, 20 - 10 = 10 each
    conn.execute("INSERT INTO resolutions (venue, market_id, result, settled_at) VALUES ('odds_api', 'G:totals', "
                 "'Total 74', %s)", (NOW + dt.timedelta(hours=12),))
    out = paper.run_pass(conn, "odds_api", c, NOW + dt.timedelta(hours=12, minutes=5))
    assert out["settled"] == 2 and {o[7:] for o in orders(conn) if o[3]} == {(1.0, 10.0)}

    lines = paper.report(conn, "odds_api", c["paper"], NOW + dt.timedelta(days=1))
    assert lines[0] == ("tt_totals on odds_api: 2 accounts, first fill 2026-11-06 (day 1), 2 settled; "
                        "forward test runs to 2026-12-04 and 300 settled bets")
    assert len(lines) == 3 and all(l.endswith(f"1 bets, 1-0, ROI +100.0%, CLV {mid - 0.5:+.3f} over 1")
                                   for l in lines[1:])
    assert all(l.startswith("  2026-W45 paper-odds_api-tt_totals-") for l in lines[1:])


def test_tt_totals_needs_its_meetings_and_a_fresh_book(conn):
    seed_odds(conn, meetings=4)
    c = load_config()
    c["paper"]["forward"] = {"odds_api": ["tt_totals"]}
    gated = copy.deepcopy(c)
    gated["paper"]["forward"] = {}
    assert paper.run_pass(conn, "odds_api", gated, NOW)["accounts"] == {}  # without forward, the gate decides
    out = paper.run_pass(conn, "odds_api", c, NOW)
    assert len(out["accounts"]) == 4 and not [o for o in orders(conn) if o[3]]  # 4 meetings < every min_h2h
    out = paper.run_pass(conn, "odds_api", c, NOW + dt.timedelta(hours=1))
    assert not out["fresh"] and out["book"] == 0
    assert paper.report(conn, "odds_api", c["paper"], NOW) == ["odds_api: no paper fills yet"]


def decisions(conn):
    return conn.execute("SELECT account, instrument, action, rule, inputs, checks, sizing, fill_source, fill_id "
                        "FROM decisions ORDER BY id").fetchall()


def funnel(conn, at=NOW):
    return dict(conn.execute("SELECT reason, n FROM decision_funnel WHERE account = %s AND pass_at = %s",
                             (ACCOUNT, at)).fetchall())


def test_each_fill_has_its_decision_and_every_book_row_is_counted_in_the_funnel(conn):
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    [d] = [r for r in decisions(conn) if r[2] == "enter"]
    [(oid,)] = conn.execute("SELECT id FROM paper_orders").fetchall()
    account, instrument, action, rule, inputs, checks, sizing, source, fill_id = d
    assert (account, instrument, action, source, fill_id) == (ACCOUNT, "A", "enter", "paper", oid)
    assert conn.execute("SELECT count(*) FROM fills WHERE source = 'paper' AND fill_id = %s", (fill_id,)).fetchone()[0] == 1
    assert rule == "longshot: buy no when the yes mid is 0.01-0.10, within 24 h of close"
    assert all(c["pass"] for c in checks) and {c["name"] for c in checks} == set(paper.Longshot.CHECKS)
    assert {i["name"]: (i["value"], i["source"]) for i in inputs}["ask"] == (0.07, "kalshi recorder")
    assert all(i["as_of"] for i in inputs)
    assert sizing["contracts"] == 10 and sizing["price"] == pytest.approx(0.95) and sizing["cash"] == 1000
    assert sizing["depth"] == 100 and sizing["fee"] == pytest.approx(0.04)
    # A ordered; B is 48 h out and C is out of the band: each a near-miss, one check failed of a full trace
    assert funnel(conn) == {"ordered": 1, "hours to close": 1, "mid in band": 1}
    assert sum(funnel(conn).values()) == 3
    near = [(r[1], [c["name"] for c in r[5] if not c["pass"]]) for r in decisions(conn) if r[2] == "near_miss"]
    assert near == [("B", ["hours to close"]), ("C", ["mid in band"])]

    paper.run_pass(conn, "kalshi", cfg(near_misses=1), NOW + dt.timedelta(minutes=5))
    later = funnel(conn, NOW + dt.timedelta(minutes=5))
    assert later == {"new market": 1, "hours to close": 1, "mid in band": 1}  # A is held now
    near = [r[1] for r in decisions(conn) if r[2] == "near_miss"]
    assert near == ["B", "C", "B"]  # capped at 1 this pass, the oldest quote first
    assert len(decisions(conn)) == 4


def test_a_stale_book_writes_no_decision_and_one_stale_book_row(conn):
    seed(conn, sweep_age=dt.timedelta(minutes=20))
    paper.run_pass(conn, "kalshi", cfg(), NOW)
    assert decisions(conn) == [] and funnel(conn) == {"stale_book": 1}


def test_near_misses_and_funnel_rows_past_trace_days_are_cut(conn):
    seed(conn)
    paper.run_pass(conn, "kalshi", cfg(near_misses=5), NOW)
    assert {r[2] for r in decisions(conn)} == {"enter", "near_miss"}
    paper.run_pass(conn, "kalshi", cfg(near_misses=0, trace_days=1), NOW + dt.timedelta(days=2))
    assert [r[2] for r in decisions(conn)] == ["enter"]  # the fill's decision stays with its fill
    assert funnel(conn) == {}


WEATHER = {"stake": 10, "max_spread": 0.05, "hours": 24, "sd": 2.0, "edge": 0.05}


def test_weather_joins_the_league_only_once_a_variant_passes_and_trades_on_the_newest_forecast(conn):
    """W: New York's high on Nov 7 is 57 or above, the yes at 0.30 / 0.32; NWS read 60 an hour ago, so about 0.96."""
    from lab import inputs

    conn.execute("INSERT INTO venues (venue, title, kind) VALUES ('kalshi', 'Kalshi', 'exchange')")
    conn.execute("INSERT INTO markets (venue, market_id, event_id, series_id, title, subtitle, market_type, status, "
                 "close_time, first_seen, updated_at) VALUES ('kalshi', 'W', 'KXHIGHNY-26NOV07', 'KXHIGHNY', 'W', "
                 "'57° or above', 'binary', 'active', %s, %s, %s)", (NOW + dt.timedelta(hours=20), NOW, NOW))
    conn.execute("INSERT INTO outcomes (venue, market_id, outcome, bid, ask, bid_size, ask_size, quoted_at) "
                 "VALUES ('kalshi', 'W', 'yes', 0.30, 0.32, 100, 100, %s)", (NOW - dt.timedelta(minutes=7),))
    conn.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, resolved, ok) "
                 "VALUES ('kalshi', %s, %s, 1, 1, 1, 0, true)", (NOW - dt.timedelta(minutes=5),) * 2)
    read_at = NOW - dt.timedelta(hours=1)
    inputs.write(conn, "nws_high", "KNYC", read_at, {"highs": {"2026-11-07": 60}, "grid": "OKX/33,37"})
    ready(conn, "weather", WEATHER, ok=False)
    assert paper.run_pass(conn, "kalshi", cfg(), NOW)["accounts"] == {}  # failed the gate: not in the league
    ready(conn, "weather", WEATHER)
    account = paper.account_of("weather", backtest.variant_of(WEATHER), "kalshi")
    out = paper.run_pass(conn, "kalshi", cfg(), NOW + dt.timedelta(minutes=1))
    assert out["accounts"][account]["fills"] == 1 and orders(conn)[0][:3] == (account, "W", "yes")
    [d] = [r for r in decisions(conn) if r[2] == "enter"]
    assert all(c["pass"] for c in d[5]) and {c["name"] for c in d[5]} == set(paper.Weather.CHECKS)
    [nws] = [i for i in d[4] if i["name"] == "nws_high"]
    assert nws["value"]["high"] == 60 and nws["source"] == "api.weather.gov gridpoint forecast"
    assert pd.Timestamp(nws["as_of"]) == pd.Timestamp(read_at)


# polymarket_us and predictit: recorded quotes with no sizes
def seed_unsized(conn, venue, sweep_age=dt.timedelta(minutes=5)):
    """One longshot, A, closing in 12 h at 0.05/0.07 with no recorded size."""
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES (%s, %s, 'exchange')", (venue, venue))
    conn.execute("INSERT INTO markets (venue, market_id, title, market_type, status, close_time, first_seen, "
                 "updated_at) VALUES (%s, 'A', 'A', 'binary', 'active', %s, %s, %s)",
                 (venue, NOW + dt.timedelta(hours=12), NOW, NOW))
    conn.execute("INSERT INTO outcomes (venue, market_id, outcome, bid, ask, quoted_at) VALUES (%s, 'A', 'yes', 0.05, "
                 "0.07, %s)", (venue, NOW - dt.timedelta(minutes=7)))
    conn.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, resolved, ok) "
                 "VALUES (%s, %s, %s, 1, 1, 1, 0, true)", (venue, NOW - sweep_age, NOW - sweep_age))


@pytest.mark.parametrize("venue", ["polymarket_us", "predictit"])
def test_a_venue_that_records_no_sizes_trades_paper_at_its_quote_and_a_stale_book_places_nothing(conn, venue):
    shipped = load_config()
    assert venue in shipped["paper"]["venues"] and shipped["paper"]["forward"][venue] == ["longshot"]
    c = cfg(forward={venue: ["longshot"]})
    account = paper.account_of("longshot", VARIANT, venue)
    seed_unsized(conn, venue, sweep_age=dt.timedelta(minutes=30))
    assert paper.run_pass(conn, venue, c, NOW)["accounts"][account]["orders"] == 0  # older than max_book_age_min
    conn.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, resolved, ok) "
                 "VALUES (%s, %s, %s, 1, 0, 0, 0, true)", (venue, NOW, NOW))
    out = paper.run_pass(conn, venue, c, NOW + dt.timedelta(minutes=1))
    assert out["accounts"][account]["fills"] == 1
    assert [(o[1], o[2], o[4]) for o in orders(conn)] == [("A", "no", 0.95)]


def test_two_venues_trading_one_variant_each_get_their_own_account(conn):
    """The same longshot variant on kalshi and polymarket_us: one name for both made the second venue's insert a
    no-op, and its pass died on a KeyError for the account (ar-2743)."""
    c = cfg(forward={"kalshi": ["longshot"], "polymarket_us": ["longshot"]})
    seed(conn, gate=False)
    seed_unsized(conn, "polymarket_us")
    assert paper.run_pass(conn, "kalshi", c, NOW)["accounts"][ACCOUNT]["fills"] == 1
    other = paper.account_of("longshot", VARIANT, "polymarket_us")
    assert other != ACCOUNT and paper.run_pass(conn, "polymarket_us", c, NOW)["accounts"][other]["fills"] == 1
    assert dict(conn.execute("SELECT account, venue FROM paper_accounts").fetchall()) == {
        ACCOUNT: "kalshi", other: "polymarket_us"}


def test_without_unsized_depth_a_sizeless_quote_cannot_fill(conn):
    """The negative control: books() leaves an unrecorded size NaN, which backtest.take reads as no room."""
    seed_unsized(conn, "predictit")
    row, _ = paper.books(conn, "predictit", NOW, 168)[0]
    assert row.depth_yes != row.depth_yes
    row, _ = paper.books(conn, "predictit", NOW, 168, unsized=1000)[0]
    assert (row.depth_yes, row.depth_no) == (1000, 1000)
