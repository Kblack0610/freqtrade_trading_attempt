
import numpy as np
import pandas as pd
import pytest

freqtrade_strategy = pytest.importorskip("freqtrade.strategy")

from lab.research import bust  # noqa: E402
from lab.research.BustShort import signals  # noqa: E402


def series(values):
    return pd.Series(values, index=pd.date_range("2022-01-01", periods=len(values), freq="D").date, dtype=float)


def frame(close):
    days = pd.date_range("2022-01-01", periods=len(close), freq="D")
    return pd.DataFrame({"date": days, "close": close, "volume": 1.0})


def test_falls_finds_each_peak_and_trough():
    close = series([100, 120, 80, 70, 90, 130, 90])
    day = close.index
    assert bust.falls(close) == [(day[1], day[3]), (day[5], day[6])]


def test_a_small_dip_is_not_a_fall():
    assert bust.falls(series([100, 90, 100, 85, 110])) == []


def test_no_short_while_btc_is_over_its_average():
    close = np.r_[np.linspace(100, 200, 250), np.linspace(200, 50, 60)]
    rising = frame(np.linspace(100, 300, len(close)))
    assert signals(frame(close), rising)["enter_short"].sum() == 0
    assert signals(frame(close), frame(close))["enter_short"].sum() > 0


def test_a_signal_does_not_change_with_later_candles():
    close = np.r_[np.linspace(100, 200, 250), np.linspace(200, 50, 60)]
    whole = signals(frame(close), frame(close))["enter_short"]
    cut = signals(frame(close[:280]), frame(close[:280]))["enter_short"]
    assert whole.iloc[:280].tolist() == cut.tolist()


def trade(open_, close, pair="BTC/USDT:USDT", short=True, profit=10.0, fees=0.0):
    return {"pair": pair, "open_date": pd.Timestamp(open_), "close_date": pd.Timestamp(close), "is_short": short,
            "profit_abs": profit, "stake_amount": 1000.0, "funding_fees": fees}


def rates(rows):
    return pd.DataFrame(rows, columns=["exchange", "pair", "ts", "rate"]).assign(ts=lambda d: pd.to_datetime(d["ts"]))


FUNDING = rates([("kucoin", "BTC/USDT:USDT", f"2024-01-0{d} {h:02d}:00", 0.0001) for d in (1, 2, 3) for h in (0, 8, 16)]
                + [("okx", "BTC/USDT:USDT", "2024-01-02 08:00", 0.0005), ("okx", "BTC/USDT:USDT", "2024-01-02 16:00", 0.0005)])


def test_measured_funding_pays_the_short_a_positive_rate_and_charges_the_long():
    t = pd.DataFrame([trade("2024-01-01 00:00", "2024-01-02 00:00"), trade("2024-01-01 00:00", "2024-01-02 00:00", short=False)])
    out = bust.measured(t, FUNDING)
    # Settlements in (open, close]: 08:00, 16:00 and the next 00:00, kucoin, since okx starts later.
    assert out["funding"].tolist() == pytest.approx([0.3, -0.3]) and out["funding_from"].tolist() == ["kucoin", "kucoin"]


def test_the_trade_venue_wins_where_it_covers_and_an_uncovered_trade_gets_no_guess():
    t = pd.DataFrame([trade("2024-01-02 08:00", "2024-01-02 16:00"), trade("2023-06-01", "2023-06-05"),
                      trade("2024-01-01", "2024-01-02", pair="TAO/USDT:USDT")])
    out = bust.measured(t, FUNDING)
    assert out["funding"].iloc[0] == pytest.approx(0.5) and out["funding_from"].iloc[0] == "okx"
    assert out["funding"].iloc[1:].isna().all() and out["funding_from"].iloc[1:].isna().all()


def test_freqtrades_own_funding_is_taken_out_before_the_measured_is_added():
    t = bust.measured(pd.DataFrame([trade("2024-01-01 00:00", "2024-01-02 00:00", profit=12.0, fees=2.0)]), FUNDING)
    assert bust.result(t, False) == (pytest.approx(0.01), 1, 1.0)
    assert bust.result(t, True)[0] == pytest.approx(10.3 / bust.WALLET)
    assert bust.result(t.assign(funding=float("nan")), True) == (0.0, 0, 0.0)


def test_the_long_short_trend_research_is_causal_and_trades_both_ways():
    from lab.research.DonchianLongShort import signals as both

    close = np.r_[np.linspace(100, 300, 300), np.linspace(300, 80, 300)]
    df = frame(close).assign(volume=1.0)
    whole = both(df)
    assert whole["enter_long"].sum() > 0 and whole["enter_short"].sum() > 0
    cut = both(df.iloc[:450])
    pd.testing.assert_frame_equal(cut, whole.iloc[:450])


@pytest.mark.parametrize("name", ["CarverLongShort64", "CarverLongShort16"])
def test_carver_long_short_is_causal_trades_both_ways_and_mirrors_its_long_half(name):
    from lab import forecasts
    from lab.research import CarverLongShort

    rule = getattr(CarverLongShort, name)
    close = np.r_[np.linspace(100, 300, 600), np.linspace(300, 80, 600)]
    df = frame(close)
    whole = CarverLongShort.signals(df, rule.spec)
    assert whole["enter_long"].sum() > 0 and whole["enter_short"].sum() > 0
    assert not (whole["enter_long"] & whole["enter_short"]).any()
    # The long half is the forecast family's class: the same forecast, long above 0.
    f = forecasts.ewmac(df["close"], rule.spec["args"]["fast"])
    pd.testing.assert_series_equal(whole["forecast"], f, check_names=False)
    w = whole["weight"].dropna()
    assert not w.empty and w.between(0, 1).all()
    cut = CarverLongShort.signals(df.iloc[:900], rule.spec)
    pd.testing.assert_frame_equal(cut, whole.iloc[:900])


def test_windows_split_the_whole_history_by_side():
    t = pd.DataFrame([trade("2024-01-01", "2024-01-02"), trade("2024-02-01", "2024-02-02", short=False)])
    out = bust.windows(t, [(pd.Timestamp("2024-01-01").date(), pd.Timestamp("2024-01-05").date(), -0.3)])
    assert len(out["whole history"][1]) == 2
    assert out["whole history, shorts"][1]["is_short"].tolist() == [True]
    assert out["whole history, longs"][1]["is_short"].tolist() == [False]
