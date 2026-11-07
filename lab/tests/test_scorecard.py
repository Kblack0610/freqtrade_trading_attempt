import datetime as dt

import numpy as np
import pandas as pd

from lab import scorecard, suggest

CFG = suggest.load_config()


def market(days=400, step=0.004):
    """BTC rising 0.4% a day, so every labelled day is up; the vol bucket alternates low and high."""
    index = pd.date_range("2023-01-01", periods=days, freq="D")
    close = pd.Series(100 * (1 + step) ** np.arange(days), index=index)
    vol = pd.Series(np.where(np.arange(days) % 2 == 0, "low", "high"), index=index, dtype=object)
    return close, vol


def test_a_label_uses_only_the_day_before():
    close, vol = market()
    label = scorecard.labels(close, vol, CFG["confidence"]["trend_band"])
    full = label.copy()
    cut = scorecard.labels(close.iloc[:300], vol.iloc[:300], CFG["confidence"]["trend_band"])
    assert cut.equals(full.loc[cut.index])
    assert set(label) == {"up/low", "up/high"} and label.index.min() > close.index[199]


def test_a_steady_winner_clears_zero_where_it_trades_and_a_loser_does_not():
    close, vol = market()
    start, end = dt.date(2023, 8, 1), dt.date(2024, 1, 31)
    days = pd.date_range(start, end - dt.timedelta(days=1), freq="D")
    rng = np.random.default_rng(1)
    win = pd.DataFrame({"ret": 0.002 + rng.normal(0, 0.0005, len(days)), "trades": 1}, index=days)
    lose = pd.DataFrame({"ret": -0.002 + rng.normal(0, 0.0005, len(days)), "trades": 1}, index=days)
    out = scorecard.build(close, vol, {("W", "x"): (start, end), ("L", "x"): (start, end)},
                          {("W", "x", "base"): win, ("L", "x", "base"): lose}, CFG, 7)
    by = {(r["name"], r["cell"]): r for r in out}
    assert by["W", "up/low"]["lo"] > 0 and by["L", "up/low"]["hi"] < 0
    assert by["W", "up/low"]["days"] + by["W", "up/high"]["days"] == len(days) and by["W", "up/low"]["trials"] == 7
    assert by["hold BTC", "up/low"]["lo"] > 0 and not any(c.startswith(("down", "flat")) for _, c in by)


def test_a_thin_cell_gets_counts_but_no_interval():
    close, vol = market()
    days = pd.date_range("2023-10-01", periods=10, freq="D")
    f = pd.DataFrame({"ret": 0.01, "trades": 2}, index=days)
    out = scorecard.build(close, vol, {("S", "x"): (dt.date(2023, 10, 1), dt.date(2023, 10, 11))}, {("S", "x", "tuned"): f}, CFG, 1)
    s = [r for r in out if r["name"] == "S"]
    assert sum(r["days"] for r in s) == 10 and sum(r["trades"] for r in s) == 20
    assert all(r["lo"] is None and r["hi"] is None for r in s)


def test_run_stores_the_scorecard_from_walkforward_trades(conn):
    close, vol = market()
    for d, c, v in zip(close.index, close, vol):
        conn.execute("INSERT INTO regimes (day, btc_close, vol_bucket) VALUES (%s, %s, %s)", (d.date(), float(c), v))
    conn.execute("INSERT INTO strategies (name, file, file_hash) VALUES ('A', 'x', 'h')")
    conn.execute("INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok) "
                 "VALUES ('A', 'x', '2023-09-01', '2023-01-01', '2023-10-01', 'k', true)")
    for i in range(30):
        d = dt.datetime(2023, 9, 1, 12, tzinfo=dt.timezone.utc) + dt.timedelta(days=i)
        conn.execute("INSERT INTO walkforward_trades VALUES ('A', 'x', 'base', '2023-09-01', 'BTC/USDT', %s, %s, 0.01)",
                     (d, d + dt.timedelta(hours=1)))
    assert scorecard.run(conn, CFG)["scorecard"] > 0
    assert scorecard.run(conn, CFG) == scorecard.run(conn, CFG)
    days, trades = conn.execute("SELECT sum(days), sum(trades) FROM climate_scorecard WHERE name = 'A'").fetchone()
    assert (days, trades) == (30, 30)
    assert conn.execute("SELECT count(*) FROM climate_scorecard WHERE name = 'hold BTC'").fetchone()[0] > 0
