import copy
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from lab import scorecard, suggest, vol_target, volatility

CFG = suggest.load_config()


def test_scaling_caps_exposure_and_holds_cash_without_a_winning_forecast():
    assert vol_target.scale(0.0001, True, CFG) == 1.0
    assert vol_target.scale(0.01, True, CFG) == pytest.approx(0.20 / np.sqrt(3.65))
    for variance, eligible in [(None, True), (0, True), (-1, True), (float('nan'), True), (0.01, False)]:
        assert vol_target.scale(variance, eligible, CFG) == 0
    cfg = copy.deepcopy(CFG)
    cfg['volatility']['target_max_scale'] = 2
    with pytest.raises(ValueError):
        vol_target.scale(0.01, True, cfg)


def test_monthly_gate_cannot_use_current_or_future_losses():
    days = pd.date_range('2023-01-01', '2023-07-31').date
    loss = pd.DataFrame({'har': 0.1, 'mean': 0.5, 'ewma': 0.4}, index=days)
    months = [dt.date(2023, i, 1) for i in range(1, 8)]
    before = volatility.monthly_eligibility(loss, months, CFG)
    assert not before[months[0]] and not before[months[2]] and before[months[3]]
    loss.loc[loss.index >= months[3], 'har'] = 100
    after = volatility.monthly_eligibility(loss, months, CFG)
    assert all(before[m] == after[m] for m in months[:4])
    assert not after[months[-1]]
    loss['har'] = loss['mean'] = loss['ewma'] = 1
    assert not any(volatility.monthly_eligibility(loss, months, CFG).values())


def test_forecast_on_a_day_does_not_require_that_days_realized_return():
    from lab.tests.test_volatility import daily, persistent
    rv = daily(persistent(1200, 3))
    month = dt.date(2023, 3, 1)
    full = volatility.forecasts(rv, [month], CFG)
    rv.loc[month] = np.nan
    missing = volatility.forecasts(rv, [month], CFG)
    assert full.loc[full.day == month, 'har'].iloc[0] == missing.loc[missing.day == month, 'har'].iloc[0]


def test_target_is_counted_rebuilt_and_lockbox_safe(conn):
    conn.execute("INSERT INTO strategies (name,file,file_hash) VALUES ('A','x','h')")
    for month, end in [('2023-09-01', '2023-10-01'), ('2026-10-01', '2026-11-01')]:
        conn.execute("INSERT INTO walkforward (strategy,exchange,fold_start,train_start,test_end,cache_key,ok) "
                     "VALUES ('A','x',%s,'2023-01-01',%s,'k',true)", (month, end))
        conn.execute("INSERT INTO walkforward_trades VALUES ('A','x','tuned',%s,'BTC/USD',%s,%s,0.1)",
                     (month, month + ' 01:00+00', month + ' 02:00+00'))
    conn.execute("INSERT INTO vol_forecasts VALUES ('x','BTC/USD','2023-09-01',0.01,true)")
    conn.execute("DELETE FROM trials WHERE strategy='A' AND exchange='x' AND variant LIKE 'har_target:%'")
    count = conn.execute('SELECT count(*) FROM trials').fetchone()[0]
    first = vol_target.run(conn, CFG)
    assert first == {'har_target_trades': 1, 'har_target_active': 1, 'har_target_missing': 0}
    assert vol_target.run(conn, CFG) == first
    assert conn.execute('SELECT count(*) FROM trials').fetchone()[0] == count + 1
    ret = conn.execute("SELECT profit_ratio FROM walkforward_trades WHERE variant='har_target'").fetchone()[0]
    assert ret == pytest.approx(0.1 * 0.20 / np.sqrt(3.65))
    cfg = copy.deepcopy(CFG)
    cfg['volatility']['target_annual'] = 0.10
    vol_target.run(conn, cfg)
    assert conn.execute('SELECT count(*) FROM trials').fetchone()[0] == count + 2
    conn.execute('DELETE FROM vol_forecasts')
    assert vol_target.run(conn, CFG)['har_target_missing'] == 1
    assert conn.execute("SELECT profit_ratio FROM walkforward_trades WHERE variant='har_target'").fetchone()[0] == 0
    from lab.tests.test_scorecard import market
    for day, r in market().iterrows():
        conn.execute("INSERT INTO regimes (day,btc_close,trend,vol_bucket) VALUES (%s,%s,%s,%s)",
                     (day.date(), float(r["btc_close"]), r["trend"], r["vol_bucket"]))
    scorecard.run(conn, CFG)
    assert conn.execute("SELECT sum(trades), sum(annual) FROM climate_scorecard "
                        "WHERE name='A' AND variant='har_target'").fetchone() == (1, 0)
    conn.execute("DELETE FROM walkforward_trades WHERE variant='tuned'")
    assert vol_target.run(conn, CFG)['har_target_trades'] == 0
    assert conn.execute('SELECT count(*) FROM trials').fetchone()[0] == count + 2
