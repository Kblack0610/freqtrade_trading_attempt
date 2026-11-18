import datetime as dt

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyarrow import fs as pafs

from lab import grid, inputs
from lab.markets import backtest, families
from lab.markets.backtest import Lookahead, Market
from lab.markets.families import Calibration, Forecaster, Gap, Longshot, RefBook, day
from lab.markets.history import SCHEMAS
from lab.markets.recorder import load_config
from lab.market import Resolution

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
        "SELECT params->>'buy', params->>'lo', (params->>'hours')::int, ready, windows, gates FROM readiness "
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
    traded = conn.execute("SELECT gates FROM readiness WHERE family = 'longshot' AND trades > 0").fetchall()
    assert traded and all(next(g for g in r[0] if g["gate"].startswith("expectancy"))["pass"] is False for r in traded)
    assert conn.execute("SELECT count(*) FROM market_backtests WHERE start_at >= '2026-10-01'").fetchone()[0] == 0


# tt_totals: a sportsbook's totals lines and the pair's earlier finals
def totals_market(mid, close_h, title="Ann at Bea", series="tabletennis_x"):
    return Market(mid, None, series, title, None, "totals", at(close_h - 48), at(close_h))


def totals_quotes(mid, h, over, under, books=("b1",), line="70.5"):
    """One recorded row per book and side, priced as odds.py writes a decimal price d: ask 1/d."""
    return [{"market_id": mid, "outcome": f"{b}:{w} {line}", "ts": at(h), "bid": None, "ask": a, "bid_size": None,
             "ask_size": None} for b in books for w, a in (("Over", over), ("Under", under))]


def test_features_read_meetings_that_did_not_push_against_the_line():
    f = families.features(np.array([60.0, 70.5, 75, 80, 72, 66, 74, 71]), 70.5)
    assert f == {"h2h": 7, "over_rate": pytest.approx(5 / 7), "last5_rate": pytest.approx(4 / 5),
                 "mean_gap": pytest.approx(np.mean([60, 70.5, 75, 80, 72, 66, 74, 71]) - 70.5)}
    assert families.features(np.array([70.5]), 70.5) is None


def test_line_book_takes_the_best_book_each_side_and_settles_the_line():
    m = {"G": totals_market("G", 48)}
    q = pd.DataFrame(totals_quotes("G", 0, 0.55, 0.50, ("b1",)) + totals_quotes("G", 0, 0.52, 0.53, ("b2",))
                     + totals_quotes("G", 0, 0.52, 0.52, ("b1",), line="80.5")[:1])  # 80.5 has no under: no row
    book, lm, lr, fair = families.line_book(q, m, {"G": res("G", "Total 75")}, 1000.0)
    assert list(book["market_id"]) == ["G|70.5"]
    r = book.iloc[0]
    assert (r["bid"], r["ask"], r["depth_yes"]) == (pytest.approx(0.50), 0.52, 1000.0)  # 1 - best under, best over
    assert lm["G|70.5"].event_id == "G" and lm["G|70.5"].subtitle == "Over 70.5"
    assert lr["G|70.5"].result == "yes"
    # each book devigged, then the median: b1 0.55/1.05, b2 0.52/1.05
    assert fair[("G|70.5", at(0).value)] == pytest.approx((0.55 / 1.05 + 0.52 / 1.05) / 2)
    assert families.line_book(pd.DataFrame(totals_quotes("G", 0, 0.5, 0.5, line="75")), m,
                              {"G": res("G", "Total 75")}, 1.0)[2]["G|75"].result == "void"  # a push


def meetings(*finals):
    """Past games of Ann and Bea, the n-th settling n hours after T0, with the given totals."""
    ms = {f"P{i}": totals_market(f"P{i}", i, "Bea at Ann" if i % 2 else "Ann at Bea") for i in range(len(finals))}
    return families.Meetings(ms, {f"P{i}": res(f"P{i}", f"Total {t:g}", settled_h=i) for i, t in enumerate(finals)})


def test_meetings_show_only_games_settled_before_the_row_and_refuse_a_later_score():
    mt, m = meetings(75, 60, 80), totals_market("G", 10)
    assert list(mt.before(m, at(1.5))) == [75, 60]  # P2 settles at 2h
    assert list(mt.before(m, at(3))) == [75, 60, 80]
    assert list(mt.before(totals_market("H", 10, "Ann at Cat"), at(3))) == []
    assert mt.score("P0", at(1)) == 75
    with pytest.raises(Lookahead):
        mt.score("P2", at(2))


TT = {**BASE, "max_spread": 0.08, "hours": 24, "prior": 10, "last5": 0.6, "min_h2h": 5, "edge": 0.03}


def tt_row(h, bid=0.48, ask=0.52, mid="G|70.5"):
    return next(backtest._book(pd.DataFrame([{"market_id": mid, "ts": at(h), "bid": bid, "ask": ask,
                                              "depth_yes": 100.0, "depth_no": 100.0}])).itertuples(index=False))


def line_market(close_h=30, line="70.5"):
    return Market(f"G|{line}", "G", "tabletennis_x", "Ann at Bea", f"Over {line}", "total_line", at(0), at(close_h))


def test_tt_totals_buys_the_side_its_head_to_head_backs_once_per_game():
    fairs = {("G|70.5", at(h).value): 0.5 for h in (10, 11, 12)} | {("G|72.5", at(11).value): 0.5}
    s = families.TTTotals(TT, meetings(75, 80, 74, 66, 76, 79, 73, 77), fairs)
    assert s.on_book(tt_row(1), line_market()) == []  # 29h before the start
    assert sides(s.on_book(tt_row(10), line_market())) == [("G|70.5", "yes")]  # 7 of 8 over, the last five all
    assert s.on_book(tt_row(11, mid="G|72.5"), line_market(line="72.5")) == []  # once per game
    unders = families.TTTotals(TT, meetings(60, 62, 75, 61, 63, 64, 65, 66), fairs)
    assert sides(unders.on_book(tt_row(10), line_market())) == [("G|70.5", "no")]


def test_tt_totals_needs_the_meetings_the_fair_and_the_last_five_to_agree():
    fairs = {("G|70.5", at(10).value): 0.5}
    assert families.TTTotals(TT, meetings(75, 80, 74, 76), fairs).on_book(tt_row(10), line_market()) == []  # 4 < 5
    assert families.TTTotals(TT, meetings(75, 80, 74, 76, 79, 77), {}).on_book(tt_row(10), line_market()) == []
    # 6 of 9 over, but the last five only 2 over
    late = meetings(75, 80, 74, 76, 79, 60, 61, 77, 62)
    assert families.TTTotals(TT, late, fairs).on_book(tt_row(10), line_market()) == []


class Peek(families.TTTotals):
    """The negative control: a variant that reads its own game's final score."""

    def on_book(self, row, m):
        total = self.meetings.score(m.event_id, row.ts)
        return self.order(row, "yes" if total and total > float(m.subtitle.split()[-1]) else "no")


def test_a_variant_that_reads_the_final_score_is_refused():
    mt = families.Meetings({"G": totals_market("G", 30)}, {"G": res("G", "Total 75", settled_h=32)})
    book = backtest._book(pd.DataFrame([{"market_id": "G|70.5", "ts": at(10), "bid": 0.48, "ask": 0.52,
                                         "depth_yes": 100.0, "depth_no": 100.0}]))
    with pytest.raises(Lookahead):
        backtest.replay(book, {"G|70.5": line_market()}, {}, Peek(TT, mt, {}), CFG["backtest"], None)


@pytest.fixture
def odds_trials(conn):
    """The trials register outlives the truncate, and the betting lane's count spans venues: leave none behind."""
    yield conn
    conn.execute("DELETE FROM trials WHERE exchange = 'odds_api'")


def tt_walkforward(conn, fs, over_rate):
    """A game of Ann and Bea every day of August to October, its 70.5 line quoted at 0.526 a side by two books at
    06:00, the over at 0.588 and the under at 0.476 by 10:00, and settled at 14:00, `over_rate` of them over (75), the rest under (66)."""
    root = f"{CFG['recorder']['bucket']}/{CFG['recorder']['prefix']}/venue=odds_api"
    conn.execute("DELETE FROM trials WHERE exchange = 'odds_api'")
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES ('odds_api', 'odds_api', 'sportsbook') "
                 "ON CONFLICT DO NOTHING")
    first = dt.date(2026, 8, 1)
    for i in range((dt.date(2026, 11, 1) - first).days):
        d, mid = first + dt.timedelta(days=i), f"T{i}:totals"
        close = pd.Timestamp(d, tz="UTC") + pd.Timedelta(hours=12)
        total = 75 if (i * over_rate) % 1 + over_rate >= 1 else 66
        conn.execute("INSERT INTO markets (venue, market_id, series_id, title, market_type, status, open_time, "
                     "close_time, first_seen, updated_at) VALUES ('odds_api', %s, 'tabletennis_x', %s, 'totals', "
                     "'settled', %s, %s, now(), now())", (mid, "Ann at Bea" if i % 2 else "Bea at Ann",
                                                          close - pd.Timedelta(days=1), close))
        conn.execute("INSERT INTO resolutions (venue, market_id, result, settled_at) VALUES ('odds_api', %s, %s, %s)",
                     (mid, f"Total {total}", close + pd.Timedelta(hours=2)))
        rows = [{**r, "ts": close - pd.Timedelta(hours=6)} for r in
                totals_quotes(mid, 0, 1 / 1.9, 1 / 1.9, ("b1", "b2"))]
        rows += [{**r, "ts": close - pd.Timedelta(hours=2)} for r in  # the over shortens toward the start
                 totals_quotes(mid, 0, 1 / 1.7, 1 / 2.1, ("b1", "b2"))]
        fs.create_dir(f"{root}/date={d}", recursive=True)
        pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows), preserve_index=False),
                       f"{root}/date={d}/060000Z.parquet", filesystem=fs)


def test_tt_totals_walks_forward_on_a_sportsbook_and_counts_every_variant_as_a_trial(odds_trials, tmp_path):
    conn = odds_trials
    fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())
    tt_walkforward(conn, fs, 0.9)
    with pytest.raises(SystemExit):  # an exchange family on a sportsbook
        families.main(["--venue", "odds_api", "--family", "longshot"], fs=fs, today=TODAY)
    assert families.main(["--venue", "odds_api"], fs=fs, today=TODAY) == 0
    n = len(grid.variants(CFG["families"]["tt_totals"]))
    test = conn.execute("SELECT variant, start_at, trades, expectancy_lo, (metrics->>'clv')::float FROM market_backtests "
                        "WHERE family = 'tt_totals' AND start_at < '2026-10-01'").fetchall()
    assert len(test) == 2 * n  # August and September; October is the lockbox
    trials = conn.execute("SELECT count(*) FROM trials WHERE exchange = 'odds_api' AND strategy = 'tt_totals'")
    assert trials.fetchone()[0] == conn.execute("SELECT count(*) FROM market_backtests WHERE family = 'tt_totals'"
                                                ).fetchone()[0]
    september = [r for r in test if day(r[1]) == dt.date(2026, 9, 1)]
    assert all(r[2] == 30 and r[3] > 0 for r in september)  # one bet a game, overs paying 0.9 x 1.9
    # bought at 0.526, closing mid (0.588 + 1 - 0.476) / 2
    assert all(r[4] == pytest.approx((0.588 + 0.524) / 2 - 1 / 1.9, abs=2e-3) for r in september)
    assert conn.execute("SELECT count(*) FROM readiness WHERE family = 'tt_totals'").fetchone()[0] == n


def test_a_lookahead_variant_is_refused_in_the_walk_forward(odds_trials, tmp_path, monkeypatch):
    conn = odds_trials
    fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())
    tt_walkforward(conn, fs, 0.5)
    monkeypatch.setattr(families, "TTTotals", Peek)
    with pytest.raises(Lookahead):
        families.main(["--venue", "odds_api"], fs=fs, today=TODAY)


def run(s, book, traced):
    out = []
    for r, m in book:
        if traced:
            s.checks, s.inputs = [], []
        out.append(sides(s.on_book(r, m)))
    return out


def test_a_traced_family_makes_the_same_orders_as_an_untraced_one_and_names_each_check():
    """The paper league traces; a backtest does not. Every branch of longshot and tt_totals, both ways."""
    m = market("A", close_h=48)
    book = [(row("A", 0, 0.04, 0.06), m), (row("A", 30, 0.20, 0.22), m), (row("A", 30, 0.02, 0.10), m),
            (row("A", 30, float("nan"), 0.06), m), (row("A", 30, 0.04, 0.06), m), (row("A", 31, 0.04, 0.06), m),
            (row("B", 50, 0.04, 0.06), market("B")), (row("C", 30, 0.04, 0.06), Market("C", "EV", "S", "C", None, "binary", at(-240), None))]
    params = {**BASE, "hours": 24, "buy": "no", "lo": 0.01, "hi": 0.10}
    assert run(Longshot(params), book, True) == run(Longshot(params), book, False)
    assert run(Longshot(params), book, False)[4] == [("A", "no")]

    s = Longshot(params)
    s.checks, s.inputs = [], []
    s.on_book(row("A", 0, 0.04, 0.06), m)  # 48 h out: fails the window, the rest still run
    assert [(c["name"], c["pass"]) for c in s.checks] == [("new market", True), ("hours to close", False),
                                                          ("quote valid", True), ("spread", True), ("mid in band", True)]
    assert s.checks[1]["value"] == pytest.approx(48) and s.checks[1]["threshold"] == 24
    assert {c["name"] for c in s.checks} == set(Longshot.CHECKS)
    assert "buy no when the yes mid is 0.01-0.10, within 24 h" in s.rule()

    fairs = {("G|70.5", at(h).value): 0.5 for h in (10, 11, 12)} | {("G|72.5", at(11).value): 0.5}
    tt_book = [(tt_row(1), line_market()), (tt_row(10), line_market()),
               (tt_row(11, mid="G|72.5"), line_market(line="72.5"))]
    for finals in ((75, 80, 74, 66, 76, 79, 73, 77), (60, 62, 75, 61, 63, 64, 65, 66), (75, 80, 74, 76),
                   (75, 80, 74, 76, 79, 60, 61, 77, 62)):
        assert run(families.TTTotals(TT, meetings(*finals), fairs), tt_book, True) == \
            run(families.TTTotals(TT, meetings(*finals), fairs), tt_book, False)
    s = families.TTTotals(TT, meetings(75, 80, 74, 76, 79, 60, 61, 77, 62), fairs)
    s.checks, s.inputs = [], []
    assert s.on_book(tt_row(10), line_market()) == []
    assert [c["name"] for c in s.checks if not c["pass"]] == ["form agrees"]
    assert {c["name"] for c in s.checks} == set(families.TTTotals.CHECKS)
    assert [i["name"] for i in s.inputs] == ["head_to_head", "no_vig_fair"]


# weather: a Kalshi daily high against the NWS forecast high (lab/inputs.toml nws_high)

WX = {**BASE, "hours": 24, "sd": 2.0, "edge": 0.05}


def wx_market(mid, subtitle, series="KXHIGHNY", event="KXHIGHNY-26NOV02"):
    return Market(mid, event, series, mid, subtitle, "binary", at(-240), at(30))


def wx(*readings):
    """A Weather variant over KNYC readings of (hour, {date: high})."""
    rows = [("KNYC", at(h), {"highs": highs, "grid": "OKX/33,37"}) for h, highs in readings]
    return families.Weather(WX, families.Forecasts(rows), inputs.load())


def test_temperature_markets_read_their_range_and_day_and_a_normal_high_gives_each_range_a_chance():
    assert families.degrees("57° or above") == (57, None)
    assert families.degrees("48° or below") == (None, 48)
    assert families.degrees("55° to 56°") == (55, 56)
    assert families.degrees("Above 57") is None and families.degrees(None) is None
    assert families.day_of("KXHIGHNY-26NOV15") == dt.date(2026, 11, 15) and families.day_of("EV") is None
    # a forecast of 58 with sd 2: 57 or above is above 56.5, P = Phi(0.75)
    assert families.chance(57, None, 58, 2) == pytest.approx(0.7734, abs=1e-4)
    parts = [families.chance(None, 54, 58, 2), families.chance(55, 56, 58, 2), families.chance(57, None, 58, 2)]
    assert sum(parts) == pytest.approx(1)


def test_weather_buys_the_side_the_forecast_favours_and_never_reads_a_forecast_made_after_the_row():
    day = {"2026-11-02": 58}
    s = wx((9, day))
    assert sides(s.on_book(row("A", 10, 0.58, 0.60), wx_market("A", "57° or above"))) == [("A", "yes")]  # 0.77 vs 0.60
    assert sides(s.on_book(row("B", 10, 0.20, 0.22), wx_market("B", "48° or below"))) == [("B", "no")]  # ~0 vs 0.20
    assert s.on_book(row("C", 10, 0.74, 0.76), wx_market("C", "57° or above")) == []  # no edge at 0.76
    assert s.on_book(row("D", 10, 0.58, 0.60), wx_market("D", "57° or above", series="S")) == []  # no station
    assert s.on_book(row("E", 10, 0.58, 0.60), wx_market("E", "57° or above", event="KXHIGHNY-26NOV05")) == []  # no day
    assert wx((11, day)).on_book(row("A", 10, 0.58, 0.60), wx_market("A", "57° or above")) == []  # read after the row
    assert wx((5, day)).on_book(row("A", 10, 0.58, 0.60), wx_market("A", "57° or above")) == []  # 5 h old: stale


def test_a_traced_weather_variant_makes_the_same_orders_and_names_its_forecast_input():
    book = [(row("A", 10, 0.58, 0.60), wx_market("A", "57° or above")),
            (row("B", 0, 0.20, 0.22), wx_market("B", "48° or below")),  # 30 h out
            (row("C", 10, 0.74, 0.76), wx_market("C", "57° or above")),
            (row("D", 10, 0.58, 0.60), wx_market("D", "57° or above", series="S")),
            (row("E", 10, 0.58, 0.60), wx_market("E", "57° or above", event="KXHIGHNY-26NOV05"))]
    for reads in (((9, {"2026-11-02": 58}),), ((5, {"2026-11-02": 58}),), ()):
        assert run(wx(*reads), book, True) == run(wx(*reads), book, False)
    s = wx((-1, {"2026-11-02": 58}))
    s.checks, s.inputs = [], []
    s.on_book(row("B", 0, 0.20, 0.22), wx_market("B", "48° or below"))  # the window fails, the rest still run
    assert [c["name"] for c in s.checks if not c["pass"]] == ["hours to close"]
    assert {c["name"] for c in s.checks} == set(families.Weather.CHECKS)
    assert s.inputs == [{"name": "nws_high", "value": {"station": "KNYC", "day": "2026-11-02", "high": 58,
                                                       "grid": "OKX/33,37"}, "as_of": at(-1)}]
    with pytest.raises(ValueError, match="INPUTS"):
        s.read("ohlcv", 1)


def test_a_weather_window_runs_only_when_every_day_of_it_has_a_forecast(conn, tmp_path):
    fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())
    loader = families.Loader(conn, fs, "kalshi", CFG)
    start, end = dt.date(2026, 10, 1), dt.date(2026, 11, 1)
    for d in families.span(start, end - dt.timedelta(days=1)):
        inputs.write(conn, "nws_high", "KNYC", dt.datetime.combine(d, dt.time(12), UTC), {"highs": {}})
    assert families.strategy("weather", WX, loader, start, end, set()) is None  # Oct 31 has no reading
    inputs.write(conn, "nws_high", "KNYC", dt.datetime(2026, 10, 31, 12, tzinfo=UTC), {"highs": {"2026-11-01": 50}})
    s = families.strategy("weather", WX, families.Loader(conn, fs, "kalshi", CFG), start, end, set())
    assert isinstance(s, families.Weather)
    assert s.forecasts.at("KNYC", pd.Timestamp("2026-11-01", tz="UTC"))[0]["highs"] == {"2026-11-01": 50}


# observed: a station's own readings of the day decide a market before the day ends (lab/inputs.toml nws_obs)

OB = {**BASE, "hours": 24, "margin": 1, "edge": 0.03}


def ob_market(mid, subtitle, series="KXHIGHNY", event="KXHIGHNY-26NOV02"):
    return Market(mid, event, series, mid, subtitle, "binary", at(-240), at(30))


def ob(*readings):
    """An Observed variant over KNYC readings of (hour, day so far)."""
    rows = [("KNYC", at(h), {"day": "2026-11-02", "high": None, "low": None, "rained": False, "n": 5} | so_far)
            for h, so_far in readings]
    return families.Observed(OB, families.Forecasts(rows), inputs.load())


def test_a_reading_settles_a_high_only_past_its_slack_a_low_only_downward_and_rain_only_upward():
    done = families.settled_by
    assert done("high", (None, 60), {"high": 62}, 1) == 0.0 and done("high", (None, 60), {"high": 61}, 1) is None
    assert done("high", (63, None), {"high": 64}, 1) == 1.0 and done("high", (63, None), {"high": 63}, 1) is None
    assert done("high", (57, 58), {"high": 70}, 1) == 0.0 and done("high", (57, 58), {"high": 58}, 1) is None
    assert done("low", (40, None), {"low": 38}, 1) == 0.0 and done("low", (40, None), {"low": 40}, 1) is None
    assert done("low", (None, 44), {"low": 43}, 1) == 1.0 and done("low", (None, 44), {"low": 46}, 1) is None
    assert done("rain", None, {"rained": True}, 1) == 1.0 and done("rain", None, {"rained": False}, 1) is None
    assert done("high", (63, None), {"high": None}, 1) is None


def test_observed_buys_what_the_station_has_settled_while_the_price_lags():
    s = ob((9, {"high": 66, "low": 41}))
    assert sides(s.on_book(row("A", 10, 0.50, 0.52), ob_market("A", "63° or above"))) == [("A", "yes")]  # high 66: yes
    assert sides(s.on_book(row("B", 10, 0.30, 0.32), ob_market("B", "61° to 62°"))) == [("B", "no")]  # high is past it
    assert s.on_book(row("C", 10, 0.98, 0.99), ob_market("C", "63° or above")) == []  # the price caught up
    assert s.on_book(row("D", 10, 0.50, 0.52), ob_market("D", "70° or above")) == []  # not decided: it may still rise
    assert sides(s.on_book(row("L", 10, 0.50, 0.52), ob_market("L", "40° or above", "KXLOWTNYC", "KXLOWTNYC-26NOV02"))) == []
    assert sides(s.on_book(row("M", 10, 0.50, 0.52), ob_market("M", "45° or above", "KXLOWTNYC", "KXLOWTNYC-26NOV02"))) \
        == [("M", "no")]  # the low is already 41, under 45
    assert sides(s.on_book(row("N", 10, 0.50, 0.52), ob_market("N", "44° or below", "KXLOWTNYC", "KXLOWTNYC-26NOV02"))) \
        == [("N", "yes")]


def test_observed_bets_rain_in_both_market_shapes_and_only_after_rain_is_on_record():
    wet, dry = ob((9, {"rained": True})), ob((9, {"rained": False}))
    daily = ob_market("R", "New York City", "KXRAIN", "KXRAIN-26NOV02")
    daily = Market("KXRAIN-26NOV02-NYC", daily.event_id, "KXRAIN", daily.title, "New York City", "binary", at(-240), at(30))
    single = Market("KXRAINDNYC-261102-0", "KXRAINDNYC-261102", "KXRAINDNYC", "t", "Above 0 inches", "binary", at(-240), at(30))
    for m in (daily, single):
        assert sides(wet.on_book(row(m.market_id, 10, 0.60, 0.62), m)) == [(m.market_id, "yes")]
        assert dry.on_book(row(m.market_id, 10, 0.60, 0.62), m) == []  # negative control: nothing measured yet
    other = Market("KXRAIN-26NOV02-TTN", daily.event_id, "KXRAIN", "t", "Trenton", "binary", at(-240), at(30))
    assert wet.on_book(row(other.market_id, 10, 0.60, 0.62), other) == []  # a city with no station


def test_observed_never_reads_a_reading_made_after_the_row_or_of_another_day_or_a_stale_one():
    market_ = ob_market("A", "63° or above")
    base = {"high": 66}
    assert ob((11, base)).on_book(row("A", 10, 0.50, 0.52), market_) == []  # made after the row
    assert ob((5, base)).on_book(row("A", 10, 0.50, 0.52), market_) == []  # 5 h old
    old = families.Observed(OB, families.Forecasts([("KNYC", at(9), {"day": "2026-11-01", "high": 66, "low": 40,
                                                                      "rained": False, "n": 9})]), inputs.load())
    assert old.on_book(row("A", 10, 0.50, 0.52), market_) == []  # yesterday's high says nothing of today's market


def test_a_traced_observed_variant_makes_the_same_orders_and_names_its_observation_input():
    book = [(row("A", 10, 0.50, 0.52), ob_market("A", "63° or above")),
            (row("B", 0, 0.50, 0.52), ob_market("B", "63° or above")),  # 30 h out
            (row("C", 10, 0.98, 0.99), ob_market("C", "63° or above")),
            (row("D", 10, 0.50, 0.52), ob_market("D", "63° or above", series="S"))]
    for reads in (((9, {"high": 66}),), ((5, {"high": 66}),), ()):
        assert run(ob(*reads), book, True) == run(ob(*reads), book, False)
    s = ob((-1, {"high": 66}))
    s.checks, s.inputs = [], []
    s.on_book(row("B", 0, 0.50, 0.52), ob_market("B", "63° or above"))
    assert [c["name"] for c in s.checks if not c["pass"]] == ["hours to close"]
    assert {c["name"] for c in s.checks} == set(families.Observed.CHECKS)
    assert s.inputs == [{"name": "nws_obs", "value": {"station": "KNYC", "day": "2026-11-02", "high": 66, "low": None,
                                                      "rained": False}, "as_of": at(-1)}]


def test_an_observed_window_runs_only_when_every_day_of_it_has_an_observation(conn, tmp_path):
    fs = pafs.SubTreeFileSystem(str(tmp_path), pafs.LocalFileSystem())
    start, end = dt.date(2026, 10, 1), dt.date(2026, 11, 1)
    for d in families.span(start, end - dt.timedelta(days=1)):
        inputs.write(conn, "nws_obs", "KNYC", dt.datetime.combine(d, dt.time(12), UTC), {"day": d.isoformat()})
    assert families.strategy("observed", OB, families.Loader(conn, fs, "kalshi", CFG), start, end, set()) is None
    inputs.write(conn, "nws_obs", "KNYC", dt.datetime(2026, 10, 31, 12, tzinfo=UTC), {"day": "2026-10-31"})
    assert isinstance(families.strategy("observed", OB, families.Loader(conn, fs, "kalshi", CFG), start, end, set()),
                      families.Observed)


def test_every_weather_series_of_the_registry_names_one_station_and_a_cli_report():
    stations = inputs.load()["nws_high"]["stations"]
    assert len(stations) == 23
    series = [s for st in stations.values() for s in st["series"]]
    assert len(series) == len(set(series)) and len({st["code"] for st in stations.values()}) == 23
    assert all(st["cli"].startswith("CLI") and -9 <= st["utc_offset"] <= -5 for st in stations.values())
    assert families.day_of("KXRAINDNYC-261115") == dt.date(2026, 11, 15)
