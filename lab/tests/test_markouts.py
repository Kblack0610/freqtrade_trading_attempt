import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from lab import markouts, suggest

CFG = suggest.load_config()["markouts"]
FEE = 0.001
START = pd.Timestamp("2026-10-01", tz="UTC")


def walk(seed: int, n: int = 20_000, vol: float = 0.002) -> pd.Series:
    """Fat-tailed random-walk opens on 5m candles."""
    steps = np.random.default_rng(seed).standard_t(4, n) * vol / np.sqrt(2)
    return pd.Series(100 * np.exp(np.cumsum(steps)), index=pd.date_range(START, periods=n, freq="5min"))


def entries(at, fills, short=False) -> pd.DataFrame:
    return pd.DataFrame({"bot": "b", "trade_id": range(len(at)), "open_date": at, "open_rate": fills, "is_short": short})


def test_btc_itself_hedges_to_minus_the_fee():
    """Beta on itself is 1, so the hedged move is zero and only the round-trip fee is left: the known answer."""
    btc = walk(1)
    at = btc.index[[10_000, 12_000]]
    m = markouts.markouts(entries(at, btc[at].to_numpy()), btc, btc, FEE, CFG)
    assert len(m) == 2 * len(CFG["horizons_minutes"])
    assert m["beta"].to_numpy() == pytest.approx(1.0)
    assert m["net"].to_numpy() == pytest.approx(-2 * FEE) and m["null_net"].to_numpy() == pytest.approx(-2 * FEE)
    assert (m["fill_slip"] == 0).all()


def test_random_entries_sit_on_the_null_and_a_planted_edge_clears_it():
    """The negative control: entries with no skill average near the null, and the null near minus the fee. Then the
    same pair with a jump planted an hour after each signal: the 1h markout clears the null by the jump."""
    btc, alt = walk(2), walk(3)
    rng = np.random.default_rng(4)
    at = alt.index[np.sort(rng.choice(np.arange(9_000, 19_000), 300, replace=False))]
    m = markouts.markouts(entries(at, alt[at].to_numpy()), alt, btc, FEE, CFG)
    h1 = m[m["horizon_min"] == 60]
    se = h1["net"].std() / np.sqrt(len(h1))
    assert abs(h1["net"].mean() - h1["null_net"].mean()) < 3 * se
    assert abs(h1["null_net"].mean() + 2 * FEE) < 3 * se

    few = at[::10]  # sparse, so few random entries share a planted jump
    lift = np.zeros(len(alt))
    for i in alt.index.get_indexer(few):
        lift[i + 6:] += 0.01  # 1% up 30 minutes after each entry, for good
    jump = alt * np.exp(lift)
    p = markouts.markouts(entries(few, jump[few].to_numpy()), jump, btc, FEE, CFG)
    h1 = p[p["horizon_min"] == 60]
    assert h1["net"].mean() - h1["null_net"].mean() > 0.006
    assert (p[p["horizon_min"] == 5]["net"].mean() - p[p["horizon_min"] == 5]["null_net"].mean()) < 0.003


def test_a_short_and_a_worse_fill_are_signed_against_the_trade():
    btc = walk(5)
    alt = pd.Series(np.linspace(100, 20, len(btc)), index=btc.index)  # falls steadily, so a short gains
    at = alt.index[[10_000]]
    m = markouts.markouts(entries(at, alt[at].to_numpy() * 0.999, short=True), alt, btc, FEE, CFG)
    assert (m["side"] == -1).all() and m["fill_slip"].to_numpy() == pytest.approx(0.001)
    # the worse fill costs 0.1% up front; 4 hours of the fall pay it back
    assert m["ret"].is_monotonic_increasing and m["ret"].iloc[0] < 0 < m["ret"].iloc[-1]


def test_an_entry_waits_until_its_window_is_covered():
    btc = walk(6)
    at = btc.index[[len(btc) - 10]]
    assert markouts.markouts(entries(at, btc[at].to_numpy()), btc, btc, FEE, CFG).empty


def test_run_marks_each_entry_once(conn, tmp_path):
    pytest.importorskip("freqtrade")
    btc, alt = walk(7), walk(8)
    for pair, s in (("BTC_USD", btc), ("ETH_USD", alt)):
        (tmp_path / "data" / "binanceus").mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"date": s.index, "open": s.to_numpy()}).to_feather(tmp_path / "data" / "binanceus" / f"{pair}-5m.feather")
    (tmp_path / "ex").mkdir()
    (tmp_path / "ex" / "binanceus.json").write_text(json.dumps({"fee": FEE}))
    conn.execute("INSERT INTO bots (name, exchange, strategy, cohort) VALUES ('b', 'binanceus', 'S', 'league')")
    for i, (bar, pair) in enumerate(((10_000, "ETH/USD"), (11_000, "BTC/USD"), (len(btc) - 5, "ETH/USD"))):
        t = btc.index[bar].to_pydatetime() + dt.timedelta(seconds=7)
        conn.execute("INSERT INTO bot_trades (bot, trade_id, open_date, pair, is_open, open_rate, raw) "
                     "VALUES ('b', %s, %s, %s, false, %s, '{}')", (i, t, pair, float((alt if pair == "ETH/USD" else btc).iloc[bar])))
    out = markouts.run(conn, tmp_path / "data", tmp_path / "ex", CFG)
    assert out == {"markouts_written": 2, "markouts_pending": 1}
    assert conn.execute("SELECT count(*), count(DISTINCT trade_id) FROM markouts").fetchone() == (6, 2)
    assert markouts.run(conn, tmp_path / "data", tmp_path / "ex", CFG) == {"markouts_written": 0, "markouts_pending": 1}
