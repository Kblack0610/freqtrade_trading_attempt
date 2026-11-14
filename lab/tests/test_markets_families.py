import datetime as dt

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyarrow import fs as pafs

from lab import grid
from lab.markets import backtest, families
from lab.markets.backtest import Lookahead, Market
from lab.markets.families import Calibration, Forecaster, Gap, Longshot, RefBook
from lab.markets.history import SCHEMAS
from lab.markets.recorder import load_config
from lab.markets.venue import Resolution

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 1, tzinfo=UTC)
CFG = load_config()
BASE = {"stake": 10, "max_spread": 0.05}


def at(h: float) -> pd.Timestamp:
    return pd.Timestamp(T0) + pd.Timedelta(hours=h)


def row(mid, h, bid, ask):
    return next(backtest._book(pd.DataFrame([{"market_id": mid, "ts": at(h), "bid": bid, "ask": ask,
                                              "depth_yes": 100.0, "depth_no": 100.0}])).itertuples(index=False))


def market(mid, close_h=48, series="S"):
    return Market(mid, "EV", series, mid, None, "binary", at(-240), at(close_h))


def res(mid, result="yes", settled_h=48):
    return Resolution(mid, result, {"yes": 1.0, "no": 0.0}.get(result), at(settled_h), 100.0)


def sides(orders):
    return [(o.market_id, o.side) for o in orders]


def test_variants_take_every_list_as_an_axis_and_merge_a_list_of_tables():
    v = grid.variants(CFG["families"]["longshot"])
    assert len(v) == 8 and len({backtest.variant_of(p) for p in v}) == 8
    assert v[0] == {"stake": 10, "max_spread": 0.05, "hours": 24, "buy": "no", "lo": 0.01, "hi": 0.10}
    assert len(grid.variants(CFG["families"]["calibration"])) == 4
    assert grid.variants({"a": 1}) == [{"a": 1}]


def test_longshot_buys_its_side_in_the_band_inside_the_window_once_per_market():
    s = Longshot({**BASE, "hours": 24, "buy": "no", "lo": 0.01, "hi": 0.10})
    m = market("A", close_h=48)
    assert s.on_book(row("A", 0, 0.04, 0.06), m) == []  # 48h before close: outside the window
    assert s.on_book(row("A", 30, 0.20, 0.22), m) == []  # out of the band
    assert s.on_book(row("A", 30, 0.02, 0.10), m) == []  # spread wider than max_spread
    assert sides(s.on_book(row("A", 30, 0.04, 0.06), m)) == [("A", "no")]
    assert s.on_book(row("A", 31, 0.04, 0.06), m) == []  # once per market
    assert s.on_book(row("A", 50, 0.04, 0.06), m) == []  # closed


def test_longshot_skips_a_row_without_a_two_sided_quote():
    s = Longshot({**BASE, "hours": None, "buy": "yes", "lo": 0.8, "hi": 0.99})
    assert s.on_book(row("A", 0, float("nan"), 0.9), market("A")) == []


def calibration_data(n_train=40):
    """n_train markets entered at mid 0.30 in the day before T0+48h, 3 in 4 resolving yes before it."""
    rows, markets, resolutions = [], {}, {}
    for i in range(n_train):
        mid = f"T{i}"
        rows.append({"market_id": mid, "ts": at(i * 0.5), "bid": 0.29, "ask": 0.31, "depth_yes": 1, "depth_no": 1})
        markets[mid] = market(mid, close_h=i * 0.5 + 10)
        resolutions[mid] = res(mid, "yes" if i % 4 else "no", settled_h=i * 0.5 + 10)
    return pd.DataFrame(rows), markets, resolutions


CAL = {**BASE, "hours": 168, "width": 0.05, "prior": 10, "min_n": 30, "key": "all", "edge": 0.06}


def test_fit_counts_only_markets_that_settled_before_the_window():
    book, markets, resolutions = calibration_data()
    before = at(48)
    table = families.fit(book, markets, resolutions, before, CAL)
    assert table == {("", 6): (40, 30.0)}
    # negative control: the same markets settling after the window opens are not known yet
    late = {k: res(k, r.result, settled_h=60) for k, r in resolutions.items()}
    assert families.fit(book, markets, late, before, CAL) == {}
    # and rows from inside the window are never fitted on
    assert families.fit(book, markets, resolutions, at(0), CAL) == {}


def test_calibration_buys_yes_when_the_base_rate_beats_the_ask_and_never_before_the_window():
    book, markets, resolutions = calibration_data()
    before = at(48)
    s = Calibration(CAL, families.fit(book, markets, resolutions, before, CAL), before)
    # (30 + 10 * 0.325) / 50 = 0.665 against an ask of 0.31
    assert s.rate("", 0.30) == pytest.approx(0.665)
    m = market("X", close_h=100)
    assert s.on_book(row("X", 40, 0.29, 0.31), m) == []  # before the window: fitted on results not yet known
    assert sides(s.on_book(row("X", 50, 0.29, 0.31), m)) == [("X", "yes")]
    thin = Calibration({**CAL, "min_n": 41}, s.table, before)
    assert thin.on_book(row("X", 50, 0.29, 0.31), m) == []


def test_calibration_by_series_keeps_each_series_apart():
    book, markets, resolutions = calibration_data()
    p = {**CAL, "key": "series"}
    s = Calibration(p, families.fit(book, markets, resolutions, at(48), p), at(48))
    assert s.on_book(row("X", 50, 0.29, 0.31), market("X", 100, series="OTHER")) == []
    assert sides(s.on_book(row("Y", 50, 0.29, 0.31), market("Y", 100, series="S"))) == [("Y", "yes")]


def ref_quotes(*rows):
    """rows: (market_id, outcome, hours after T0, bid, ask)"""
    return pd.DataFrame([(m, o, at(h), b, a) for m, o, h, b, a in rows],
                        columns=["market_id", "outcome", "ts", "bid", "ask"])


def test_refbook_removes_each_books_margin_and_takes_the_median_book():
    q = ref_quotes(("G", "dk:Home", 0, None, 0.625), ("G", "dk:Away", 0, None, 0.4545),  # 1.6 and 2.2 decimal
                   ("G", "fd:Home", 0, None, 0.6), ("G", "fd:Away", 0, None, 0.48),
                   ("G", "mgm:Home", 0, None, 0.9),  # missing Away: cannot be devigged, left out
                   ("G", "dk:Home", 5, None, 0.5), ("G", "dk:Away", 5, None, 0.55))
    r = RefBook(q, {"G": market("G", close_h=10)})
    dk, fd = 0.625 / (0.625 + 0.4545), 0.6 / 1.08
    assert r.fair("G", "Home", at(1)) == pytest.approx((dk + fd) / 2)
    assert r.fair("G", "Home", at(-1)) is None  # nothing quoted yet
    assert r.fair("G", "Home", at(6)) == pytest.approx((0.5 / 1.05 + fd) / 2)  # the later dk quote, only from then
    assert r.fair("G", "Home", at(10)) is None  # the reference market has closed


def test_refbook_takes_a_binary_venues_mid():
    r = RefBook(ref_quotes(("P", "yes", 0, 0.40, 0.44)), {})
    assert r.fair("P", "yes", at(1)) == pytest.approx(0.42)


def test_gap_buys_the_cheap_side_against_the_reference():
    ref = RefBook(ref_quotes(("G", "dk:Home", 0, None, 0.6), ("G", "dk:Away", 0, None, 0.4)), {})
    s = Gap({**BASE, "source": "rule:kalshi_game", "edge": 0.05}, {"K1": ("G", "Home"), "K2": ("G", "Away")}, ref)
    assert sides(s.on_book(row("K1", 1, 0.50, 0.52), market("K1"))) == [("K1", "yes")]  # 0.60 fair vs 0.52
    assert sides(s.on_book(row("K2", 1, 0.47, 0.49), market("K2"))) == [("K2", "no")]  # bid 0.47 vs 0.40 fair
    assert s.on_book(row("K3", 1, 0.10, 0.12), market("K3")) == []  # not linked


def test_gap_refuses_an_llm_source():
    with pytest.raises(ValueError, match="rule links only"):
        Gap({**BASE, "source": "llm:qwen", "edge": 0.05}, {}, RefBook(ref_quotes(), {}))


def test_the_forecaster_needs_a_cutoff_and_asks_once_per_market():
    with pytest.raises(ValueError):
        Forecaster({**BASE, "hours": None, "edge": 0.05}, lambda s, p: "", None)
    calls = []

    def ask(system, prompt):
        calls.append(prompt)
        return 'thinking... {"p": 0.8}'
    s = Forecaster({**BASE, "hours": None, "edge": 0.05}, ask, T0)
    assert sides(s.on_book(row("A", 1, 0.60, 0.62), market("A"))) == [("A", "yes")]
    s.on_book(row("A", 2, 0.60, 0.62), market("A"))
    assert len(calls) == 1


def test_the_forecaster_is_refused_on_a_book_before_its_cutoff():
    s = Forecaster({**BASE, "hours": None, "edge": 0.05}, lambda *_: '{"p": 0.9}', CFG["families"]["llm"]["cutoff"])
    book = backtest._book(pd.DataFrame([{"market_id": "A", "ts": pd.Timestamp("2026-11-05T12:00Z"), "bid": 0.4,
                                         "ask": 0.42, "depth_yes": 10.0, "depth_no": 10.0}]))
    with pytest.raises(Lookahead):
        backtest.replay(book, {"A": market("A")}, {}, s, CFG["backtest"], None)


# Against the database
def seed(conn, venue, ids, close_h=48, results=None):
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES (%s, %s, 'exchange') ON CONFLICT DO NOTHING",
                 (venue, venue))
    for i, mid in enumerate(ids):
        conn.execute("INSERT INTO markets (venue, market_id, series_id, title, market_type, status, open_time, "
                     "close_time, first_seen, updated_at) VALUES (%s, %s, 'S', %s, 'binary', 'settled', %s, %s, "
                     "now(), now())", (venue, mid, mid, at(-240), at(close_h)))
        if results:
            conn.execute("INSERT INTO resolutions (venue, market_id, result, value, settled_at) "
                         "VALUES (%s, %s, %s, %s, %s)", (venue, mid, results[i], float(results[i] == "yes"),
                                                         at(close_h)))


def link(conn, a, b, source, status="confirmed", by="rule"):
    conn.execute("INSERT INTO market_links (venue_a, market_id_a, outcome_a, venue_b, market_id_b, outcome_b, source, "
                 "status, confirmed_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", (*a, *b, source, status, by))


def test_load_links_takes_every_rule_link_and_no_llm_or_ambiguous_one(conn):
    seed(conn, "kalshi", ["K1", "K2", "K3", "K4"])
    seed(conn, "odds_api", ["G1", "G2"])
    link(conn, ("odds_api", "G1", "Home"), ("kalshi", "K1", "yes"), "rule:kalshi_game")
    link(conn, ("odds_api", "G1", "Away"), ("kalshi", "K2", "yes"), "rule:kalshi_game", status="rejected")
    link(conn, ("odds_api", "G2", "Home"), ("kalshi", "K3", "yes"), "llm:qwen", by="resolution")
    link(conn, ("odds_api", "G1", "Home"), ("kalshi", "K4", "yes"), "rule:kalshi_game")
    link(conn, ("odds_api", "G2", "Away"), ("kalshi", "K4", "yes"), "rule:kalshi_game")
    got = families.load_links(conn, "kalshi", "odds_api", "rule:kalshi_game")
    # a rule link later rejected by its resolution is what the rule would have traded; K4 names two games
    assert got == {"K1": ("G1", "Home"), "K2": ("G1", "Away")}


def write_bars(fs, root, d, rows):
    """rows of (market_id, ts, bid, ask), ts an hour offset from T0 or a timestamp."""
    key = f"{root}/venue=kalshi/date={d}.parquet"
    fs.create_dir(key.rsplit("/", 1)[0], recursive=True)
    cols = {f.name: [] for f in SCHEMAS["bars"]}
    for mid, h, bid, ask in rows:
        ts = h if isinstance(h, pd.Timestamp) else at(h)
        for k, v in zip(cols, ("kalshi", mid, ts, 3600, bid, ask, None, None, None, None, 1000.0, None)):
            cols[k].append(v)
    pq.write_table(pa.table(cols, schema=SCHEMAS["bars"]), key, filesystem=fs)


def test_a_window_is_a_whole_month_whose_every_settle_date_is_backfilled():
    days = lambda a, b: {a + dt.timedelta(days=i) for i in range((b - a).days)}  # noqa: E731
    done = days(dt.date(2026, 7, 15), dt.date(2026, 9, 1)) | days(dt.date(2026, 9, 2), dt.date(2026, 11, 14))
    # July starts mid month, September misses its 1st, November is not over
    assert families.months(done, dt.date(2026, 11, 14)) == [(dt.date(2026, 8, 1), dt.date(2026, 9, 1)),
                                                           (dt.date(2026, 10, 1), dt.date(2026, 11, 1))]
    assert families.months(set(), dt.date(2026, 11, 14)) == []


def test_a_training_book_that_reaches_into_its_window_raises_lookahead():
    book, markets, resolutions = calibration_data()
    start = (T0 + dt.timedelta(days=30)).date()
    families.trained(CAL, (book, markets, resolutions), start)  # ends before the window: fine
    peek = pd.concat([book, pd.DataFrame([{"market_id": "T0", "ts": pd.Timestamp(start, tz="UTC"), "bid": 0.29,
                                           "ask": 0.31, "depth_yes": 1, "depth_no": 1}])], ignore_index=True)
    with pytest.raises(Lookahead):
        families.trained(CAL, (peek, markets, resolutions), start)


FIRST, TODAY = dt.date(2026, 3, 1), dt.date(2026, 11, 14)  # March to September test, October the lockbox


def walkforward_book(conn, fs, yes_rate):
    """Three markets a day, one event, all yes or all no, each quoted at 0.84 / 0.86 half a day before it settles:
    a yes buyer at 0.86 earns when more than about 87% resolve yes. Every settle date is backfilled."""
    root = f"{CFG['recorder']['bucket']}/{CFG['history']['prefix']}/bars"
    conn.execute("DELETE FROM trials WHERE exchange = 'kalshi'")  # the fixture keeps the register's seeded rows
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES ('kalshi', 'kalshi', 'exchange') ON CONFLICT DO NOTHING")
    days = [FIRST + dt.timedelta(days=i) for i in range((dt.date(2026, 11, 1) - FIRST).days)]
    markets, res = [], []
    for i, d in enumerate(days):
        close = pd.Timestamp(d, tz="UTC") + pd.Timedelta(hours=12)
        yes = (i * yes_rate) % 1 + yes_rate >= 1 or yes_rate == 1  # an even spread of yes days at the rate
        ids = [f"D{i}-{k}" for k in range(3)]
        for m in ids:
            markets.append(("kalshi", m, f"E{i}", m, close - pd.Timedelta(days=2), close))
            res.append(("kalshi", m, "yes" if yes else "no", 1.0 if yes else 0.0, close))
        write_bars(fs, root, d, [(m, close - pd.Timedelta(hours=12), 0.84, 0.86) for m in ids])
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO markets (venue, market_id, event_id, series_id, title, market_type, status, "
                        "open_time, close_time, first_seen, updated_at) VALUES (%s, %s, %s, 'S', %s, 'binary', "
                        "'settled', %s, %s, now(), now())", markets)
        cur.executemany("INSERT INTO resolutions (venue, market_id, result, value, settled_at) "
                        "VALUES (%s, %s, %s, %s, %s)", res)
        cur.executemany("INSERT INTO market_backfill (venue, job, unit, done) VALUES ('kalshi', 'bars', %s, true)",
                        [(d.isoformat(),) for d in days])


def readiness(conn):
    """(buy, lo, hours) -> (ready, windows, gates)."""
    return {r[:3]: r[3:] for r in conn.execute(
        "SELECT params->>'buy', params->>'lo', (params->>'hours')::int, ready, windows, gates FROM market_readiness "
        "WHERE family = 'longshot'").fetchall()}


def test_a_planted_edge_passes_the_gate_after_one_look_at_the_lockbox(conn, tmp_path):
    fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())
    walkforward_book(conn, fs, 0.97)
    assert families.main(["--family", "longshot"], fs=fs, today=TODAY) == 0
    n = len(grid.variants(CFG["families"]["longshot"]))
    rows = conn.execute("SELECT variant, start_at FROM market_backtests WHERE family = 'longshot'").fetchall()
    # every variant on each of the 7 test months, and the lockbox only for the two that passed the rest
    assert len(rows) == 7 * n + 2
    trials = conn.execute("SELECT count(*) FROM trials WHERE exchange = 'kalshi'").fetchone()[0]
    assert trials == 7 * n + 2
    ready = readiness(conn)
    assert {k for k, v in ready.items() if v[0]} == {("yes", "0.8", 24), ("yes", "0.8", 168)}
    assert {v[1] for v in ready.values()} == {7}
    lockbox = next(g for g in ready[("yes", "0.8", 24)][2] if g["gate"] == "lockbox")
    assert lockbox["pass"] and lockbox["value"] > 0
    assert families.main(["--family", "longshot"], fs=fs, today=TODAY) == 0  # stored windows are not run again
    assert conn.execute("SELECT count(*) FROM trials WHERE exchange = 'kalshi'").fetchone()[0] == trials


def test_no_edge_fails_the_gate_and_never_reads_the_lockbox(conn, tmp_path):
    fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())
    walkforward_book(conn, fs, 0.86)
    assert families.main(["--family", "longshot"], fs=fs, today=TODAY) == 0
    assert not any(v[0] for v in readiness(conn).values())
    traded = conn.execute("SELECT gates FROM market_readiness WHERE family = 'longshot' AND trades > 0").fetchall()
    assert traded and all(next(g for g in r[0] if g["gate"].startswith("expectancy"))["pass"] is False for r in traded)
    assert conn.execute("SELECT count(*) FROM market_backtests WHERE start_at >= '2026-10-01'").fetchone()[0] == 0
