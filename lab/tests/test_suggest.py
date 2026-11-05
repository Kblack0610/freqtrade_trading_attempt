import datetime as dt

import suggest

CFG = suggest.load_config()


def window(**kw):
    w = {"trades": 120, "expectancy_lo": 0.002, "expectancy_hi": 0.01, "sharpe_lo": 0.6, "dsr": 0.97,
         "max_drawdown": 0.18}
    return {**w, **kw}


def years(*rets):
    return [{"year": 2022 + i, "days": 365, "ret": r} for i, r in enumerate(rets)]


def strat(**kw):
    s = {"file": "user_data/strategies/x.py", "startup": 200, "load_error": None, "lookahead": False,
         "lookahead_indicators": None, "recursive": False, "recursive_indicators": None}
    return {**s, **kw}


def facts():
    return {
        "strategies": {
            "Good": strat(), "NearMiss": strat(), "Copycat": strat(), "Leaky": strat(lookahead=True,
            lookahead_indicators="next_close"), "Broken": strat(load_error="failed to import"),
            "Unwarmed": strat(startup=0, recursive=None), "Wobbly": strat(recursive=True, recursive_indicators="ema"),
            "Unchecked": strat(lookahead=None), "Running": strat(), "Loser": strat(),
        },
        "series": {
            ("Good", "binanceus"): {"windows": {"365d": window()}, "years": years(0.3, 0.1, 0.2), "max_rho": (0.2, "Running")},
            ("NearMiss", "binanceus"): {"windows": {"365d": window(dsr=0.5)}, "years": years(0.3, 0.1, 0.2), "max_rho": None},
            ("Copycat", "binanceus"): {"windows": {"365d": window()}, "years": years(0.3, 0.1, 0.2), "max_rho": (0.93, "Running")},
            ("Leaky", "binanceus"): {"windows": {"365d": window(sharpe_lo=3.0)}, "years": years(0.3, 0.1), "max_rho": None},
            ("Unchecked", "binanceus"): {"windows": {"365d": window(dsr=0.5)}, "years": years(0.3, 0.1), "max_rho": None},
            ("Running", "binanceus"): {"windows": {"365d": window()}, "years": years(0.3, 0.1, 0.2), "max_rho": None},
            ("Loser", "binanceus"): {"windows": {"365d": window(expectancy_lo=-0.02, expectancy_hi=-0.004, sharpe_lo=-1)},
                                     "years": years(-0.2, -0.1), "max_rho": None},
            ("Loser", "coinbase"): {"windows": {"365d": window(expectancy_lo=-0.02, expectancy_hi=-0.001, sharpe_lo=-1)},
                                    "years": years(-0.2, -0.1), "max_rho": None},
        },
        "bots": [
            {"name": "binanceus-running", "exchange": "binanceus", "strategy": "Running", "live_trades": 25, "live_pnl": 4.0},
            {"name": "kraken-loser", "exchange": "kraken", "strategy": "Loser", "live_trades": 3, "live_pnl": -1.0},
            {"name": "coinbase-running", "exchange": "coinbase", "strategy": "Running", "live_trades": 40, "live_pnl": -12.5},
            {"name": "binanceus-leaky", "exchange": "binanceus", "strategy": "Leaky", "live_trades": 0, "live_pnl": 0.0},
        ],
    }


def kinds(rows):
    return {(r["kind"], r["strategy"], r["exchange"], r["bot"]) for r in rows}


def test_fixture_rows_give_the_expected_suggestions():
    assert kinds(suggest.decide(facts(), CFG)) == {
        ("promote", "Good", "binanceus", ""),
        ("watch", "NearMiss", "binanceus", ""),
        ("watch", "Copycat", "binanceus", ""),
        ("retire", "Leaky", "", ""),
        ("retire", "Broken", "", ""),
        ("fix", "Unwarmed", "", ""),
        ("fix", "Wobbly", "", ""),
        ("demote", "Loser", "kraken", "kraken-loser"),
        ("demote", "Running", "coinbase", "coinbase-running"),
        ("demote", "Leaky", "binanceus", "binanceus-leaky"),
    }


def test_each_suggestion_explains_itself():
    rows = {(r["kind"], r["strategy"], r["bot"]): r for r in suggest.decide(facts(), CFG)}
    good = rows[("promote", "Good", "")]
    assert all(g["pass"] for g in good["gates"]) and len(good["gates"]) == 8
    assert suggest.roster_line("Good", "binanceus") in good["action"] and "replicas" in good["action"]
    assert "to 5." in good["action"]
    near = rows[("watch", "NearMiss", "")]
    assert [g["gate"] for g in near["gates"] if not g["pass"]] == ["deflated Sharpe"]
    copy = rows[("watch", "Copycat", "")]
    assert [g["gate"] for g in copy["gates"] if not g["pass"]] == ["overlap with league (Running)"]
    assert "in place" in rows[("demote", "Running", "coinbase-running")]["action"]
    assert "every research exchange" in rows[("demote", "Loser", "kraken-loser")]["summary"]
    assert "next_close" in rows[("retire", "Leaky", "")]["summary"]


def test_a_bot_needs_enough_trades_before_its_dry_run_counts():
    f = facts()
    f["bots"][2]["live_trades"] = 5
    assert ("demote", "Running", "coinbase", "coinbase-running") not in kinds(suggest.decide(f, CFG))


def test_unchecked_lookahead_never_promotes_or_watches():
    f = facts()
    f["series"][("Unchecked", "binanceus")]["windows"]["365d"] = window()
    assert not any(r["strategy"] == "Unchecked" for r in suggest.decide(f, CFG))


def test_suggestions_persist_their_first_seen_and_drop_when_they_stop_applying(conn):
    now = dt.datetime.now(dt.timezone.utc)
    conn.execute("INSERT INTO strategies (name, file, file_hash, timeframe, startup_candle_count) VALUES "
                 "('Good', 'user_data/strategies/good.py', 'h', '5m', 100), "
                 "('Unwarmed', 'user_data/strategies/u.py', 'h', '5m', 0)")
    conn.execute("INSERT INTO bias_checks (strategy, kind, file_hash, has_bias) VALUES ('Good', 'lookahead', 'h', false), "
                 "('Good', 'recursive', 'h', false), ('Unwarmed', 'lookahead', 'h', false)")
    conn.execute("INSERT INTO strategy_windows (strategy, exchange, win, days, trades, expectancy_lo, expectancy_hi, "
                 "sharpe_lo, dsr, max_drawdown) VALUES ('Good', 'binanceus', '365d', 365, 120, 0.002, 0.01, 0.6, 0.97, 0.2)")
    for i in range(3 * 365):
        conn.execute("INSERT INTO strategy_daily VALUES ('Good', 'binanceus', %s, 0.001, 1, 0)",
                     (dt.date(2023, 1, 1) + dt.timedelta(days=i),))
    conn.execute("INSERT INTO bots (name, exchange, strategy, last_seen) VALUES ('kraken-s001', 'kraken', 'Strategy001', %s), "
                 "('old-bot', 'kraken', 'Gone', %s)", (now, now - dt.timedelta(days=3)))
    first = suggest.run(conn)
    assert first["suggestions"] == {"promote": 1, "fix": 1}
    seen = dict(conn.execute("SELECT kind, first_seen FROM suggestions").fetchall())
    action = conn.execute("SELECT action FROM suggestions WHERE kind = 'promote'").fetchone()[0]
    assert "set replicas in statefulset.yaml to 2." in action
    conn.execute("UPDATE strategies SET startup_candle_count = 100 WHERE name = 'Unwarmed'")
    second = suggest.run(conn)
    assert second == {"suggestions": {"promote": 1}, "suggestions_gone": 1}
    assert conn.execute("SELECT first_seen FROM suggestions WHERE kind = 'promote'").fetchone()[0] == seen["promote"]
