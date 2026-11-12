"""Known answers for the backtest engine the whole pipeline scores with: hand-built candles where the trades are
known before freqtrade runs (its tests/optimize/test_backtest_detail.py pattern), and properties every trade on
random candles must keep. Runs real freqtrade backtests through analyze.Cli with our configs, so it needs the
trader image.

A strategy here trades on markers, not indicators: volume 2 on a candle is an entry signal, volume 3 an exit
signal, so a case says exactly where it enters and exits. ROI, stoploss and trailing come from a config overlay."""

import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("freqtrade")

from lab import analyze  # noqa: E402
from test_analyze import REPO  # noqa: E402

EX = "binanceus"
FEE = json.loads((REPO / "user_data" / "exchanges" / f"{EX}.json").read_text())["fee"]
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
ENTER, EXIT, QUIET = 2.0, 3.0, 1.0
NO_ROI, NO_STOP = {"0": 100.0}, -0.99
STRATEGY = '''
from freqtrade.strategy import IStrategy


class Marked(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {"0": 100.0}
    stoploss = -0.99
    startup_candle_count = 0

    def populate_indicators(self, df, metadata):
        return df

    def populate_entry_trend(self, df, metadata):
        df.loc[df["volume"] == 2.0, "enter_long"] = 1
        return df

    def populate_exit_trend(self, df, metadata):
        df.loc[df["volume"] == 3.0, "exit_long"] = 1
        return df
'''


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """run(candles, overlay, detail=None) backtests Marked over the candles, BTC/USD on binanceus."""
    monkeypatch.setattr(analyze, "USER_DATA", REPO / "user_data")
    monkeypatch.setattr(analyze, "RESULTS", tmp_path)
    data = tmp_path / "data" / EX
    data.mkdir(parents=True)
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "Marked.py").write_text(STRATEGY)

    def run(candles: pd.DataFrame, overlay: dict | None = None, detail: pd.DataFrame | None = None) -> dict:
        candles.to_feather(data / "BTC_USD-1h.feather")
        if detail is not None:  # analyze.detail_coverage wants every whitelist pair's
            for pair in analyze.whitelist(EX):
                detail.to_feather(data / f"{pair.replace('/', '_')}-1m.feather")
        start, end = candles["date"].iloc[0].date(), (candles["date"].iloc[-1] + pd.Timedelta(days=1)).date()
        overlay = {"minimal_roi": NO_ROI, "stoploss": NO_STOP, **(overlay or {})}
        res = analyze.Cli().backtest("Marked", EX, start, end, overlay=overlay, strategy_path=strategies,
                                     detail="1m" if detail is not None else None)
        assert res["ok"], res["error"]
        return res

    return run


def bars(rows: list[tuple], freq: str = "h", start: dt.datetime = T0) -> pd.DataFrame:
    """Candles from (open, high, low, close, volume) rows, one per period from start."""
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], dtype=float)
    df.insert(0, "date", pd.date_range(start, periods=len(rows), freq=freq, tz="UTC"))
    return df


def flat(n: int, price: float = 100.0, volume: float = QUIET) -> list[tuple]:
    return [(price, price, price, price, volume)] * n


def one_trade(res: dict) -> pd.Series:
    assert len(res["trades"]) == 1, res["trades"]
    return res["trades"].iloc[0]


def at(i: int, freq: str = "h") -> pd.Timestamp:
    return pd.Timestamp(T0) + i * pd.Timedelta(1, freq)


def round_trip(entry: float, exit_: float, fee: float = FEE) -> float:
    """freqtrade's profit ratio for a long: what the exit returns after its fee over what the entry cost with its."""
    return exit_ * (1 - fee) / (entry * (1 + fee)) - 1


def test_a_flat_round_trip_loses_exactly_the_fees_on_both_sides(engine):
    t = one_trade(engine(bars(flat(3) + flat(1, volume=ENTER) + flat(3) + flat(1, volume=EXIT) + flat(4))))
    assert (t["open_date"], t["close_date"]) == (at(4), at(8))  # each signal fills at the next candle's open
    assert t["exit_reason"] == "exit_signal"
    assert t["profit_ratio"] == pytest.approx(round_trip(100, 100), rel=1e-6)
    assert t["profit_ratio"] == pytest.approx(-2 * FEE, abs=1e-6)


def test_a_changed_fee_changes_the_round_trip(engine):
    """The negative control for the pin above: the fee freqtrade charges is the config's, not a default."""
    candles = bars(flat(3) + flat(1, volume=ENTER) + flat(3) + flat(1, volume=EXIT) + flat(4))
    t = one_trade(engine(candles, {"fee": 3 * FEE}))
    assert t["profit_ratio"] == pytest.approx(round_trip(100, 100, 3 * FEE), rel=1e-6)
    assert t["profit_ratio"] != pytest.approx(round_trip(100, 100), rel=1e-6)


def test_entry_fills_at_the_next_open_not_the_signal_close(engine):
    rows = flat(3) + [(100, 101, 99, 100, ENTER), (104, 106, 103, 105, QUIET)] + flat(2, 105) \
        + [(105, 105, 105, 105, EXIT)] + flat(3, 105)
    t = one_trade(engine(bars(rows)))
    assert t["open_date"] == at(4) and t["open_rate"] == pytest.approx(104)
    assert t["close_date"] == at(8) and t["close_rate"] == pytest.approx(105)


def test_roi_exits_inside_the_candle_that_reaches_it(engine):
    rows = flat(3) + flat(1, volume=ENTER) + flat(2) + [(100, 103, 99.5, 101, QUIET)] + flat(4, 101)
    t = one_trade(engine(bars(rows), {"minimal_roi": {"0": 0.02}}))
    assert t["exit_reason"] == "roi" and t["close_date"] == at(6)
    # the price that nets 2% after both fees (about 102.14) rounded to the pair's 0.1 tick: just under the ROI
    assert t["close_rate"] == pytest.approx(102.1)
    assert t["profit_ratio"] == pytest.approx(round_trip(100, 102.1), rel=1e-9)
    assert t["profit_ratio"] < 0.02


def test_a_stop_fills_at_its_trigger_when_the_price_trades_through_it(engine):
    rows = flat(3) + flat(1, volume=ENTER) + flat(2) + [(100, 100, 90, 92, QUIET)] + flat(4, 92)
    t = one_trade(engine(bars(rows), {"stoploss": -0.05}))
    assert t["exit_reason"] == "stop_loss" and t["close_date"] == at(6)
    assert t["close_rate"] == pytest.approx(95)


def test_a_stop_gapped_through_fills_at_the_open_worse_than_its_trigger(engine):
    rows = flat(3) + flat(1, volume=ENTER) + flat(2) + [(90, 91, 88, 89, QUIET)] + flat(4, 89)
    t = one_trade(engine(bars(rows), {"stoploss": -0.05}))
    assert t["exit_reason"] == "stop_loss" and t["close_date"] == at(6)
    assert t["close_rate"] == pytest.approx(90)


def test_a_trailing_stop_follows_the_high_then_fills_below_it(engine):
    rows = flat(3) + flat(1, volume=ENTER) + flat(1) + [(100, 110, 105, 110, QUIET), (110, 120, 115, 120, QUIET),
                                                         (120, 120, 100, 101, QUIET)] + flat(3, 101)
    t = one_trade(engine(bars(rows), {"stoploss": -0.05, "trailing_stop": True}))
    assert t["exit_reason"] == "trailing_stop_loss" and t["close_date"] == at(7)
    assert t["close_rate"] == pytest.approx(120 * 0.95)


def test_inside_one_candle_the_trailing_stop_rises_on_the_high_before_the_low_is_tested(engine):
    """The pessimistic ordering: a candle wide enough to lift the stop and then trade through it exits in it."""
    rows = flat(3) + flat(1, volume=ENTER) + flat(1) + [(100, 110, 100, 110, QUIET)] + flat(4, 110)
    t = one_trade(engine(bars(rows), {"stoploss": -0.05, "trailing_stop": True}))
    assert t["exit_reason"] == "trailing_stop_loss" and t["close_date"] == at(5)
    assert t["close_rate"] == pytest.approx(110 * 0.95)


def test_one_minute_detail_decides_whether_roi_or_the_stop_came_first(engine):
    """A 1h candle that touches both the ROI price and the stop: only the 1m path inside it says which came first."""
    rows = flat(3) + flat(1, volume=ENTER) + flat(2) + [(100, 104, 94, 100, QUIET)] + flat(4)
    hour = 6
    minutes = flat(60 * hour)
    down_first = minutes + [(100, 100, 94, 94, QUIET)] + flat(29, 94) + [(94, 104, 94, 104, QUIET)] + flat(29, 104)
    up_first = minutes + [(100, 104, 100, 104, QUIET)] + flat(29, 104) + [(104, 104, 94, 94, QUIET)] + flat(29, 94)
    tail = flat(24 * 60 - 60 * (hour + 1))  # detail covers the whole backtest day
    cases = {"stop_loss": down_first, "roi": up_first}
    for reason, path in cases.items():
        t = one_trade(engine(bars(rows), {"minimal_roi": {"0": 0.02}, "stoploss": -0.05},
                             detail=bars(path + tail, "min")))
        assert t["exit_reason"] == reason, reason


def random_candles(seed: int, n: int = 600) -> pd.DataFrame:
    """A fat-tailed walk with entry and exit markers sprinkled at random."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.standard_t(3, n) * 0.01))
    opn = np.r_[100.0, close[:-1]] * np.exp(rng.normal(0, 0.002, n))
    high = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    volume = rng.choice([QUIET, ENTER, EXIT], n, p=[0.8, 0.1, 0.1])
    return bars(list(zip(opn, high, low, close, volume)))


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_trade_on_random_candles_keeps_the_engine_invariants(engine, seed):
    candles = random_candles(seed)
    res = engine(candles, {"minimal_roi": {"0": 0.03}, "stoploss": -0.04})
    t = res["trades"]
    assert len(t) >= 20
    by_date = candles.set_index("date")
    for r in t.itertuples():
        fill, out = by_date.loc[r.open_date], by_date.loc[r.close_date]
        assert fill["low"] - 1e-9 <= r.open_rate <= fill["high"] + 1e-9, r
        assert out["low"] - 1e-9 <= r.close_rate <= out["high"] + 1e-9, r
        assert r.close_date >= r.open_date, r
        signal = candles.index[candles["date"] == r.open_date][0] - 1
        assert candles["volume"].iloc[signal] == ENTER, r  # entered on the open after a signal, never before it
        assert r.profit_ratio == pytest.approx(round_trip(r.open_rate, r.close_rate), abs=1e-6), r
    # wallet = cash + open value: the end balance is the start plus every trade's profit, the last force-closed
    assert res["final_balance"] == pytest.approx(res["start_balance"] + t["profit_abs"].sum(), abs=1e-6)
