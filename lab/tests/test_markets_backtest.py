import datetime as dt

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyarrow import fs as pafs

from lab import suggest
from lab.markets import backtest, fees
from lab.markets.backtest import Lookahead, Market, Order
from lab.markets.recorder import SCHEMA, load_config
from lab.markets.venue import Resolution

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 1, 12, tzinfo=UTC)
CFG = load_config()
BCFG = CFG["backtest"]
KALSHI = BCFG["fees"]["kalshi"]


def at(s: float) -> pd.Timestamp:
    return pd.Timestamp(T0) + pd.Timedelta(seconds=s)


def book(*rows) -> pd.DataFrame:
    """rows: (market_id, seconds after T0, bid, ask, depth_yes, depth_no)"""
    return pd.DataFrame([{"market_id": m, "ts": at(s), "bid": b, "ask": a, "depth_yes": dy, "depth_no": dn}
                         for m, s, b, a, dy, dn in rows])


def market(mid: str, close_s: float = 86400) -> Market:
    return Market(mid, "EV", "SER", f"market {mid}", None, "binary", T0 - dt.timedelta(days=1), at(close_s))


def resolved(mid: str, result: str = "yes", value: float | None = 1.0, settled_s: float = 86400) -> Resolution:
    return Resolution(mid, result, value, at(settled_s), 500.0)


class Once:
    """Sends one order per market, on the first row it sees of it."""
    name, cutoff = "test-once", None

    def __init__(self, side="yes", stake=10.0, limit=None, **params):
        self.side, self.stake, self.limit, self.seen = side, stake, limit, set()
        self.params = {"side": side, "stake": stake, **params}

    def on_book(self, row, m):
        if row.market_id in self.seen:
            return []
        self.seen.add(row.market_id)
        return [Order(row.market_id, self.side, self.stake, self.limit)]


def go(b, strategy, markets=None, res=None, fee_cfg=KALSHI, **bcfg):
    ids = b["market_id"].unique()
    markets = markets or {m: market(m) for m in ids}
    res = res if res is not None else {m: resolved(m) for m in ids}
    fills, sent = backtest.replay(b, markets, res, strategy, {**BCFG, **bcfg}, fee_cfg)
    return fills, sent, backtest.settle(fills, res, fee_cfg)


# Fees: Kalshi's own examples (docs/markets.md, Fees): 100 contracts at 50c cost $1.75; at 10c or 90c, $0.63.
@pytest.mark.parametrize("contracts, price, fee", [(100, 0.50, 1.75), (100, 0.10, 0.63), (100, 0.90, 0.63),
                                                   (1, 0.50, 0.02), (10, 0.37, 0.17)])
def test_kalshi_fee_matches_the_schedule(contracts, price, fee):
    assert fees.entry(KALSHI, contracts, price) == fee


def test_kalshi_rounds_each_order_up_not_to_nearest():
    assert fees.entry({**KALSHI, "rounding": "none"}, 1, 0.5) == pytest.approx(0.0175)
    assert fees.entry(KALSHI, 1, 0.5) == 0.02  # nearest would be 0.02 too; 0.0063 shows the ceiling
    assert fees.entry(KALSHI, 1, 0.1) == 0.01 and fees.entry({**KALSHI, "rounding": "none"}, 1, 0.1) < 0.007


def test_polymarket_us_rounds_half_to_even():
    pm = BCFG["fees"]["polymarket_us"]
    assert fees._cents(0.125, "half_even") == 0.12 and fees._cents(0.135, "half_even") == 0.14
    assert fees.entry(pm, 100, 0.5) == 1.74  # 0.0695 * 100 * 0.25 = 1.7375


def test_predictit_takes_a_tenth_of_a_win_and_nothing_on_entry():
    pi = BCFG["fees"]["predictit"]
    assert fees.entry(pi, 10, 0.4) == 0.0
    assert fees.settlement(pi, 4.0, 10.0) == pytest.approx(0.6)
    assert fees.settlement(pi, 4.0, 0.0) == 0.0


def test_a_venue_without_a_table_costs_nothing():
    assert fees.entry(BCFG["fees"].get("manifold"), 100, 0.5) == 0.0


# Fills
def test_yes_buys_at_the_ask_and_no_at_one_minus_the_bid():
    b = book(("A", 0, 0.40, 0.45, 100, 100), ("B", 0, 0.40, 0.45, 100, 100))
    fills, _, _ = go(b[b["market_id"] == "A"], Once("yes"))
    assert fills["price"].tolist() == [0.45]
    fills, _, _ = go(b[b["market_id"] == "B"], Once("no"))
    assert fills["price"].tolist() == [pytest.approx(0.60)]


def test_a_fill_takes_whole_contracts_within_the_stake_and_pays_the_fee_on_top():
    fills, _, t = go(book(("A", 0, 0.40, 0.45, 1000, 1000)), Once("yes", stake=10.0))
    assert fills["contracts"].tolist() == [22]  # floor(10 / 0.45)
    assert t["profit_abs"].iloc[0] == pytest.approx(22 * (1 - 0.45) - fees.entry(KALSHI, 22, 0.45))


def test_depth_caps_a_fill_and_a_second_order_on_the_same_row_gets_what_is_left():
    class Twice(Once):
        def on_book(self, row, m):
            return [Order(row.market_id, "yes", 100.0), Order(row.market_id, "yes", 100.0)]
    fills, sent, _ = go(book(("A", 0, 0.40, 0.50, 30, 30)), Twice(), depth_share=0.5)
    assert sent == 2 and fills["contracts"].tolist() == [15]  # half of 30, then nothing left


def test_no_depth_no_fill():
    fills, sent, _ = go(book(("A", 0, 0.40, 0.50, np.nan, np.nan)), Once())
    assert sent == 1 and fills.empty


def test_a_limit_below_the_ask_does_not_fill():
    fills, _, _ = go(book(("A", 0, 0.40, 0.50, 100, 100)), Once(limit=0.49))
    assert fills.empty


def test_the_order_meets_the_book_as_it_stood_after_the_latency():
    b = book(("A", 0, 0.40, 0.45, 100, 100), ("A", 1, 0.50, 0.60, 100, 100), ("A", 10, 0.60, 0.70, 100, 100))
    fills, _, _ = go(b, Once(), latency_s=2)
    assert fills["price"].tolist() == [0.60] and fills["open_date"].tolist() == [at(2)]
    fills, _, _ = go(b, Once(), latency_s=0)
    assert fills["price"].tolist() == [0.45]


def test_nothing_fills_once_the_market_has_closed_or_settled():
    b = book(("A", 100, 0.40, 0.45, 100, 100))
    assert go(b, Once(), markets={"A": market("A", close_s=101)}, latency_s=2)[0].empty
    assert go(b, Once(), res={"A": resolved("A", settled_s=101)}, latency_s=2)[0].empty
    assert len(go(b, Once(), res={"A": resolved("A", settled_s=103)}, latency_s=2)[0]) == 1


def test_a_market_with_no_metadata_is_not_traded():
    _, sent, _ = go(book(("A", 0, 0.4, 0.5, 100, 100)), Once(), markets={"Z": market("Z")})
    assert sent == 0


# Settlement
def test_settlement_pays_yes_the_value_and_no_the_rest_and_void_refunds_less_the_fee():
    b = book(("Y", 0, 0.30, 0.40, 1000, 1000), ("N", 0, 0.30, 0.40, 1000, 1000), ("V", 0, 0.30, 0.40, 1000, 1000),
             ("S", 0, 0.30, 0.40, 1000, 1000), ("U", 0, 0.30, 0.40, 1000, 1000))
    res = {"Y": resolved("Y", "yes", 1.0), "N": resolved("N", "no", 0.0), "V": resolved("V", "void", None),
           "S": resolved("S", "yes", 0.25)}
    fills, _, t = go(b, Once("yes", stake=4.0), res=res)
    t = t.set_index("market_id")
    assert len(fills) == 5 and set(t.index) == {"Y", "N", "V", "S"}  # U never resolved
    assert t.loc["Y", "close_rate"] == 1.0 and t.loc["N", "close_rate"] == 0.0 and t.loc["S", "close_rate"] == 0.25
    assert t.loc["V", "profit_abs"] == pytest.approx(-t.loc["V", "fee"])
    _, _, t = go(b, Once("no", stake=7.0), res=res)
    assert t.set_index("market_id").loc["N", "close_rate"] == 1.0


def test_a_yes_and_a_no_together_pay_exactly_one():
    class Both(Once):
        def on_book(self, row, m):
            return [Order(row.market_id, "yes", 0.5), Order(row.market_id, "no", 0.7)]
    _, _, t = go(book(("A", 0, 0.30, 0.40, 1000, 1000)), Both(), res={"A": resolved("A", "no", 0.0)})
    assert (t["contracts"] * t["close_rate"]).sum() == 1.0


def test_the_trades_feed_window_metrics_with_the_fee_as_its_cost():
    b = book(*[(f"M{i}", i, 0.30, 0.40, 1000, 1000) for i in range(40)])
    res = {f"M{i}": resolved(f"M{i}", "yes" if i % 2 else "no", 1.0 if i % 2 else 0.0, 86400 * (1 + i)) for i in range(40)}
    _, _, t = go(b, Once("yes", stake=10.0), res=res)
    e = backtest.evaluate(t, T0, T0 + dt.timedelta(days=1), 1000, 7, {"expectancy_level": 0.95}, 5)
    m = e["metrics"]
    assert m["trades"] == 40 and m["gross_expectancy"] == pytest.approx(0.5 * (1 / 0.4 - 1) - 0.5)
    assert m["fee_per_trade"] == pytest.approx((t["close_rate"] / t["open_rate"] - 1 - t["profit_ratio"]).mean())
    assert m["fee_per_trade"] > 0 and m["days"] >= 40 and e["interval"]["trades"] == 40


# Lookahead
def test_a_forward_only_strategy_is_refused_on_a_book_before_its_cutoff():
    class Forecaster(Once):
        name, cutoff = "llm", T0 + dt.timedelta(hours=1)
    with pytest.raises(Lookahead):
        go(book(("A", 0, 0.4, 0.5, 100, 100)), Forecaster())
    fills, _, _ = go(book(("A", 7200, 0.4, 0.5, 100, 100)), Forecaster())  # negative control: after the cutoff
    assert len(fills) == 1


def test_the_strategy_never_sees_a_row_later_than_the_one_it_is_handed():
    seen = []

    class Spy(Once):
        def on_book(self, row, m):
            seen.append(row.ts)
            return []
    go(book(("B", 5, 0.4, 0.5, 1, 1), ("A", 0, 0.4, 0.5, 1, 1), ("A", 9, 0.4, 0.5, 1, 1)), Spy())
    assert seen == sorted(seen) == [at(0), at(5), at(9)]


# Loaders
def write_quotes(fs, root, venue, ts, rows):
    key = f"{root}/venue={venue}/date={ts:%Y-%m-%d}/{ts:%H%M%S}Z.parquet"
    fs.create_dir(key.rsplit("/", 1)[0], recursive=True)
    cols = {f.name: [] for f in SCHEMA}
    for mid, outcome, bid, ask, bs, as_ in rows:
        for k, v in zip(cols, (venue, mid, outcome, ts, "open", bid, ask, None, bs, as_, None, None)):
            cols[k].append(v)
    pq.write_table(pa.table(cols, schema=SCHEMA), key, filesystem=fs)


def test_load_quotes_reads_the_recorders_objects_and_keeps_the_first_of_equal_quotes(tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "quotes")
    write_quotes(fs, root, "kalshi", T0, [("A", "yes", 0.4, 0.5, 10, 20), ("B", "yes", 0.1, 0.2, 1, 2)])
    write_quotes(fs, root, "kalshi", T0 + dt.timedelta(minutes=5), [("A", "yes", 0.4, 0.5, 10, 20)])  # a retry
    write_quotes(fs, root, "kalshi", T0 + dt.timedelta(minutes=10), [("A", "yes", 0.45, 0.5, 10, 20),
                                                                     ("C", "x", 0.1, 0.2, 1, 1)])
    write_quotes(fs, root, "kalshi", T0 + dt.timedelta(days=2), [("A", "yes", 0.9, 0.95, 1, 1)])  # outside
    b = backtest.load_quotes(fs, root, "kalshi", T0.date(), T0.date() + dt.timedelta(days=1))
    assert list(b.columns) == backtest.BOOK
    assert list(zip(b["market_id"], b["bid"])) == [("A", 0.4), ("B", 0.1), ("A", 0.45)]
    a = b.iloc[0]
    assert (a["depth_yes"], a["depth_no"]) == (20, 10)  # YES takes the ask's size, NO the bid's
    assert backtest.load_quotes(fs, root, "polymarket", T0.date(), T0.date() + dt.timedelta(days=1)).empty


def test_load_bars_takes_a_share_of_each_periods_volume_as_depth(tmp_path):
    from lab.markets.history import SCHEMAS
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "bars")
    fs.create_dir(f"{root}/venue=kalshi", recursive=True)
    pq.write_table(pa.table({"venue": ["kalshi"], "market_id": ["A"], "ts": [T0], "period_s": [3600], "bid": [0.4],
                             "ask": [0.5], "open": [None], "high": [None], "low": [None], "close": [None],
                             "volume": [200.0], "open_interest": [None]}, schema=SCHEMAS["bars"]),
                   f"{root}/venue=kalshi/date={T0.date()}.parquet", filesystem=fs)
    b = backtest.load_bars(fs, root, "kalshi", T0.date(), T0.date() + dt.timedelta(days=1), 0.1)
    assert b[["depth_yes", "depth_no"]].iloc[0].tolist() == [20.0, 20.0]


# The register and the store
def seed_markets(conn, ids, venue="kalshi"):
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES (%s, 'K', 'exchange') ON CONFLICT DO NOTHING", (venue,))
    for i, mid in enumerate(ids):
        conn.execute("INSERT INTO markets (venue, market_id, title, market_type, status, open_time, close_time, "
                     "first_seen, updated_at) VALUES (%s, %s, %s, 'binary', 'settled', %s, %s, now(), now())",
                     (venue, mid, mid, T0 - dt.timedelta(days=1), at(86400 * (i + 1))))
        conn.execute("INSERT INTO resolutions (venue, market_id, result, value, settled_at) VALUES (%s, %s, %s, %s, %s)",
                     (venue, mid, "yes" if i % 3 else "no", 1.0 if i % 3 else 0.0, at(86400 * (i + 1))))


def test_run_registers_the_trial_stores_the_window_and_replaces_only_that_window(conn):
    ids = [f"M{i}" for i in range(30)]
    seed_markets(conn, ids)
    b = book(*[(m, i, 0.30, 0.40, 1000, 1000) for i, m in enumerate(ids)])
    end = T0 + dt.timedelta(days=1)
    before = conn.execute("SELECT count(*) FROM trials WHERE exchange = 'kalshi'").fetchone()[0]
    out = backtest.run(conn, Once(), "kalshi", b, T0, end, CFG)
    assert out["trades"] == 30 and out["trials"] == before + 1
    variant = backtest.variant_of(Once().params)
    assert conn.execute("SELECT 1 FROM trials WHERE strategy = 'test-once' AND exchange = 'kalshi' AND variant = %s",
                        (f"{variant}:{T0.date()}",)).fetchone()
    row = conn.execute("SELECT trades, fills, open_fills, cardinality(returns), metrics->>'trades', expectancy, events, "
                       "cardinality(daily) FROM market_backtests").fetchone()
    assert row[:5] == (30, 30, 0, 30, "30") and row[5] == pytest.approx(out["expectancy"])
    assert row[6] == ids and row[7] == 31  # each market its own event; a day to the day after the last settlement
    assert conn.execute("SELECT category, trades FROM market_category_results").fetchall() == [("other", 30)]
    backtest.run(conn, Once(), "kalshi", b, T0, end, CFG)
    assert conn.execute("SELECT count(*) FROM market_category_results").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM market_backtests").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM trials WHERE exchange = 'kalshi'").fetchone()[0] == before + 1
    later = T0 + dt.timedelta(days=31)
    backtest.run(conn, Once(), "kalshi", b, later, later + dt.timedelta(days=1), CFG)  # another window is another row
    assert conn.execute("SELECT count(*) FROM market_backtests").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM trials WHERE exchange = 'kalshi'").fetchone()[0] == before + 2


def test_a_refused_forward_only_run_still_counts_as_a_trial_and_stores_nothing(conn):
    class Forecaster(Once):
        name, cutoff = "llm-refused", T0 + dt.timedelta(days=1)
    seed_markets(conn, ["A"])
    with pytest.raises(Lookahead):
        backtest.run(conn, Forecaster(), "kalshi", book(("A", 0, 0.4, 0.5, 100, 100)), T0, T0 + dt.timedelta(days=1), CFG)
    assert conn.execute("SELECT 1 FROM trials WHERE strategy = 'llm-refused'").fetchone()
    assert conn.execute("SELECT count(*) FROM market_backtests").fetchone()[0] == 0


def test_a_series_falls_in_the_category_its_prefix_names():
    cats = CFG["categories"]
    assert [backtest.category_of(s, cats) for s in ("KXNBAGAME", "kxpres-2028", "KXFEDDECISION", "basketball_nba", "KXWEATHER", None)] \
        == ["sports", "politics", "economics", "sports", "other", "other"]


def test_results_split_by_the_category_of_each_trade(conn):
    ids = [f"M{i}" for i in range(80)]
    series = {m: ("KXNBAGAME" if i % 2 else "KXFEDRATE") for i, m in enumerate(ids)}
    markets = {m: Market(m, "EV", series[m], "t", None, "binary", T0 - dt.timedelta(days=1), at(86400)) for m in ids}
    trades = pd.DataFrame({"market_id": ids, "profit_ratio": [0.1 if series[m] == "KXNBAGAME" else -0.1 for m in ids]})
    out = {r[0]: r for r in backtest.by_category(trades, markets, CFG["categories"], 1, suggest.load_config()["confidence"])}
    assert set(out) == {"sports", "economics"} and out["sports"][1] == out["economics"][1] == 40
    assert out["sports"][2] > 0 > out["economics"][2] and out["sports"][3] > 0 > out["economics"][4]
    assert backtest.by_category(trades.iloc[:0], markets, CFG["categories"], 1, {}) == []
