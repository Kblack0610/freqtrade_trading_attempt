"""The determinism proof under lab/reproduce.py: one strategy backtested twice on fixed candles gives the same
trades byte for byte, and one changed candle changes both its candle digest and the trades, which the
reproduce check reports as a mismatch. Runs real freqtrade backtests, so it needs the trader image."""

import datetime as dt

import pytest

pytest.importorskip("freqtrade")

from lab import analyze, reproduce, walkforward  # noqa: E402
from test_analyze import REPO  # noqa: E402
from test_families import synthetic_candles  # noqa: E402

EX = "binanceus"
START, END = dt.date(2026, 1, 10), dt.date(2026, 2, 25)
STRATEGY = '''
from freqtrade.strategy import IStrategy


class GoldenCross(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {"0": 0.02}
    stoploss = -0.03
    startup_candle_count = 30

    def populate_indicators(self, df, metadata):
        df["fast"] = df["close"].rolling(5).mean()
        df["slow"] = df["close"].rolling(20).mean()
        return df

    def populate_entry_trend(self, df, metadata):
        df.loc[(df["fast"] > df["slow"]) & (df["fast"].shift(1) <= df["slow"].shift(1)), "enter_long"] = 1
        return df

    def populate_exit_trend(self, df, metadata):
        df.loc[(df["fast"] < df["slow"]) & (df["fast"].shift(1) >= df["slow"].shift(1)), "exit_long"] = 1
        return df
'''


@pytest.fixture
def golden(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "USER_DATA", REPO / "user_data")
    monkeypatch.setattr(analyze, "RESULTS", tmp_path)
    data = tmp_path / "data" / EX
    data.mkdir(parents=True)
    utc = dt.timezone.utc
    candles = synthetic_candles(dt.datetime(2026, 1, 1, tzinfo=utc), dt.datetime(2026, 3, 1, tzinfo=utc), "h")
    candles.to_feather(data / "BTC_USD-1h.feather")
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "GoldenCross.py").write_text(STRATEGY)
    return data / "BTC_USD-1h.feather", strategies


def backtest(strategies) -> dict:
    res = analyze.Cli().backtest("GoldenCross", EX, START, END, strategy_path=strategies)
    assert res["ok"], res["error"]
    return res


def as_bytes(res: dict) -> bytes:
    return res["trades"][analyze.TRADE_COLS].to_csv(index=False, float_format="%.17g").encode()


def nudge_first_entry(feather, strategies):
    """Raise the open of the candle the first trade fills on: its signal is unchanged, its fill price is not."""
    import pandas as pd

    df = pd.read_feather(feather)
    first = backtest(strategies)["trades"]["open_date"].min()
    i = df.index[df["date"] == first][0]
    df.loc[i, "open"] *= 1.001
    df.loc[i, "high"] = max(df.loc[i, "high"], df.loc[i, "open"])
    df.to_feather(feather)
    return first.date()


def test_the_same_backtest_twice_gives_the_same_trades_byte_for_byte(golden):
    _, strategies = golden
    a, b = backtest(strategies), backtest(strategies)
    assert len(a["trades"]) >= 5
    assert as_bytes(a) == as_bytes(b)
    assert a["final_balance"] == b["final_balance"]
    assert reproduce.trade_rows(a["trades"][analyze.TRADE_COLS].itertuples(index=False)) == \
        reproduce.trade_rows(b["trades"][analyze.TRADE_COLS].itertuples(index=False))


def test_one_changed_candle_changes_its_digest_and_the_trades(golden):
    feather, strategies = golden
    before, days = backtest(strategies), analyze.candle_digests(EX, "1h", by="day")
    day = nudge_first_entry(feather, strategies)
    after, days_after = backtest(strategies), analyze.candle_digests(EX, "1h", by="day")
    assert [d for d in days if days[d] != days_after[d]] == [day]
    assert analyze.span_digest(days, START, END) != analyze.span_digest(days_after, START, END)
    assert analyze.span_digest(days, day + dt.timedelta(days=1), END) == analyze.span_digest(days_after, day + dt.timedelta(days=1), END)
    assert as_bytes(before) != as_bytes(after)


def test_reproduce_reports_the_changed_candle_as_a_mismatch(golden, conn):
    feather, strategies = golden

    class Runner(analyze.Cli):
        def backtest(self, strategy, exchange, start, end, overlay=None, strategy_path=None):
            return super().backtest(strategy, exchange, start, end, overlay, strategy_path=strategies)

    task = {"strategy": "GoldenCross", "exchange": EX, "start": START, "end": END, "key": "k", "attempts": 0, "candles": "c"}
    analyze.store_chunk(conn, task, backtest(strategies), None)
    cid, final = conn.execute("SELECT id, final_balance FROM backtest_chunks").fetchone()
    chunk = {"id": cid, "strategy": "GoldenCross", "exchange": EX, "start": START, "end": END, "final_balance": final}
    assert reproduce.check_chunk(conn, Runner(), chunk) == {"diff": []}
    nudge_first_entry(feather, strategies)
    assert reproduce.check_chunk(conn, Runner(), chunk)["diff"][0].startswith("trades:")


def test_an_overlay_reaches_freqtrade_and_changes_the_backtest(golden):
    _, strategies = golden
    base = backtest(strategies)
    tight = analyze.overlay_config(EX, {"minimal_roi": {"0": 0.004}, "stoploss": -0.006, "pairs": 1})
    assert tight["exchange"]["pair_whitelist"] == ["BTC/USD"]
    res = analyze.Cli().backtest("GoldenCross", EX, START, END, tight, strategy_path=strategies)
    assert res["ok"], res["error"]
    assert len(res["trades"]) and as_bytes(res) != as_bytes(base)
    # the overlay's roi and stop, not the strategy's 2% and 3%, close every trade
    assert res["trades"]["profit_ratio"].between(-0.02, 0.01).all()


def test_a_walkforward_fold_runs_real_hyperopt_with_its_sampler_and_times_each_step(golden, monkeypatch):
    """GoldenCross declares no parameters, so it tunes ROI and stoploss only; an unknown sampler failing proves
    the fold's sampler reaches freqtrade."""
    _, strategies = golden
    monkeypatch.setattr(analyze, "REPO", strategies.parent)
    monkeypatch.setattr(walkforward, "EPOCHS_ROI", 8)
    monkeypatch.setattr(walkforward, "EARLY_STOP", 4)
    monkeypatch.setattr(walkforward, "MIN_TRADES", 1)
    fold = (dt.date(2026, 1, 3), dt.date(2026, 2, 10), dt.date(2026, 2, 28))
    monkeypatch.setattr(walkforward, "SAMPLERS", ["NoSuchSampler"])
    bad = walkforward.Cli().fold("GoldenCross", "strategies/GoldenCross.py", EX, fold, 7)
    assert not bad["ok"] and "NoSuchSampler" in bad["error"] and bad["hyperopt_s"] > 0
    monkeypatch.setattr(walkforward, "SAMPLERS", ["TPESampler"])
    res = walkforward.Cli().fold("GoldenCross", "strategies/GoldenCross.py", EX, fold, 7)
    assert res["ok"], res["error"]
    assert {"roi", "stoploss"} <= set(res["params"]) and not {"buy", "sell"} & set(res["params"])
    assert 4 <= res["epochs_run"] <= 8
    assert res["hyperopt_s"] > 0 and res["backtest_s"] > 0
    assert res["duration_s"] == pytest.approx(res["hyperopt_s"] + res["backtest_s"])
