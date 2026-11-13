import datetime as dt

import pyarrow.parquet as pq
import pytest
from markets_fakes import Session
from pyarrow import fs as pafs

from lab.markets import history
from lab.markets.kalshi import Kalshi
from lab.markets.manifold import Manifold
from lab.markets.polymarket import Polymarket, PolymarketUS
from lab.markets.venue import Bar, Quote, Resolution, Settled, Span, Trade

UTC = dt.timezone.utc
D1, D2 = dt.date(2026, 11, 3), dt.date(2026, 11, 4)
TODAY = dt.date(2026, 11, 6)
VCFG = {"title": "Fake", "kind": "exchange"}
FAR = 1e18  # a deadline no test reaches


def nosleep(_):
    pass


def kcfg(**kw):
    return {"base_url": "https://k/v2", "page_limit": 2, "page_gap_s": 0.5, "retries": 1, **kw}


def at(d: dt.date, h: int = 12) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(h), UTC)


def settled(mid: str, d: dt.date, volume: float | None = 500.0) -> Settled:
    t = at(d)
    q = Quote(market_id=mid, outcome="yes", ts=t, event_id="EV-1", series_id="EV", title=f"market {mid}",
              subtitle=None, market_type="binary", status="finalized", open_time=t - dt.timedelta(days=1),
              close_time=t, bid=None, ask=None, last=None, bid_size=None, ask_size=None, volume=volume,
              open_interest=None)
    return Settled(q, Resolution(mid, "yes", 1.0, t, volume))


class FakeHistory:
    name = "fake"

    def __init__(self, pages=(), fail_after=None):
        self.pages, self.fail_after, self.asked, self.cursors = list(pages), fail_after, [], []

        class _Api:
            gap = 0.0
            sleep = staticmethod(nosleep)
        self.api = _Api()

    def settled(self, cursor):
        self.cursors.append(cursor)
        start = int(cursor or 0)
        for i in range(start, len(self.pages)):
            if self.fail_after is not None and i == self.fail_after:
                raise RuntimeError("venue down")
            yield self.pages[i], str(i + 1) if i + 1 < len(self.pages) else None

    def bars(self, span, period_s):
        self.asked.append(span.market_id)
        return [Bar(span.market_id, span.settled_at, period_s, 0.4, 0.5, None, None, None, 0.45, 10.0, None)]

    def trades(self, span):
        self.asked.append(span.market_id)
        yield [Trade(span.market_id, f"{span.market_id}-t1", span.settled_at, 0.4, 2.0, "yes")]
        yield []


def walk(conn, venue, **kw):
    return history.walk_settled(venue, VCFG, conn, kw.pop("deadline", FAR), **kw)


def test_settled_walk_resumes_from_its_cursor_and_does_not_repeat_once_done(conn):
    pages = [[settled("A", D2), settled("B", D2)], [settled("C", D1)], [settled("D", D1)]]
    with pytest.raises(RuntimeError):
        walk(conn, FakeHistory(pages, fail_after=2))
    assert conn.execute("SELECT cursor, rows, done FROM market_backfill WHERE job = 'settled'").fetchone() == ("2", 3, False)

    v = FakeHistory(pages)
    assert walk(conn, v) == {"done": True, "rows": 4, "this_run": 1}
    assert v.cursors == ["2"]
    assert conn.execute("SELECT count(*), count(volume) FROM resolutions").fetchone() == (4, 4)
    assert conn.execute("SELECT DISTINCT status FROM markets").fetchall() == [("settled",)]

    again = FakeHistory(pages)
    assert walk(conn, again)["this_run"] == 0 and again.cursors == []
    restarted = FakeHistory(pages)
    assert walk(conn, restarted, restart=True) == {"done": True, "rows": 4, "this_run": 4}
    assert restarted.cursors == [None]


def test_settled_walk_keeps_the_recorders_rows_and_fills_only_a_missing_volume(conn):
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES ('fake', 'Recorder', 'exchange')")
    conn.execute("INSERT INTO markets (venue, market_id, title, market_type, status, first_seen, updated_at) "
                 "VALUES ('fake', 'A', 'as recorded', 'binary', 'settled', now(), now())")
    conn.execute("INSERT INTO resolutions (venue, market_id, result, value, settled_at) VALUES ('fake', 'A', 'no', 0, %s)",
                 (at(D1),))
    walk(conn, FakeHistory([[settled("A", D2, volume=700.0)]]))
    assert conn.execute("SELECT title FROM markets").fetchone() == ("as recorded",)
    assert conn.execute("SELECT result, settled_at, volume FROM resolutions").fetchone() == ("no", at(D1), 700.0)
    assert conn.execute("SELECT title FROM venues").fetchone() == ("Recorder",)


def test_settled_walk_stops_at_the_budget_between_pages(conn):
    pages = [[settled("A", D2)], [settled("B", D2)]]
    assert walk(conn, FakeHistory(pages), deadline=0) == {"done": False, "rows": 1, "this_run": 1}
    assert walk(conn, FakeHistory(pages)) == {"done": True, "rows": 2, "this_run": 1}


def test_settled_walk_skips_markets_under_the_volume_floor_and_still_moves_the_cursor(conn):
    pages = [[settled("A", D2, volume=50.0), settled("B", D2, volume=150.0)], [settled("C", D1, volume=0.0)]]
    assert walk(conn, FakeHistory(pages), floor=100) == {"done": True, "rows": 1, "this_run": 1}
    assert conn.execute("SELECT market_id FROM resolutions").fetchall() == [("B",)]


def test_bars_one_object_per_date_over_the_volume_floor_and_redone_when_the_date_grows(conn, tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "bucket/history")
    walk(conn, FakeHistory([[settled("A", D2), settled("B", D2), settled("thin", D2, volume=5.0)],
                            [settled("C", D1)], [settled("today", TODAY)]]))
    hcfg = {"bars_min_volume": 100, "bars_since_days": 30, "bars_period_min": 60}
    v = FakeHistory()
    assert history.backfill(v, "bars", hcfg, conn, fs, root, FAR, TODAY) == {"dates": 2, "left": 0, "rows": 3}
    assert v.asked == ["A", "B", "C"]  # newest date first; nothing under the floor, nothing settled today
    rows = pq.read_table(f"{root}/bars/venue=fake/date=2026-11-04.parquet").to_pylist()
    assert [(r["venue"], r["market_id"], r["period_s"], r["close"]) for r in rows] == [
        ("fake", "A", 3600, 0.45), ("fake", "B", 3600, 0.45)]

    rerun = FakeHistory()
    assert history.backfill(rerun, "bars", hcfg, conn, fs, root, FAR, TODAY)["dates"] == 0 and rerun.asked == []

    walk(conn, FakeHistory([[settled("E", D1)]]), restart=True)
    grown = FakeHistory()
    assert history.backfill(grown, "bars", hcfg, conn, fs, root, FAR, TODAY)["dates"] == 1
    assert grown.asked == ["C", "E"]
    assert len(pq.read_table(f"{root}/bars/venue=fake/date=2026-11-03.parquet")) == 2


def test_trades_stream_every_page_and_a_budget_stops_between_dates(conn, tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "bucket/history")
    walk(conn, FakeHistory([[settled("A", D2)], [settled("C", D1)]]))
    hcfg = {"trades_min_volume": 0, "trades_since_days": 30}
    assert history.backfill(FakeHistory(), "trades", hcfg, conn, fs, root, 0, TODAY) == {"dates": 1, "left": 1, "rows": 1}
    assert history.backfill(FakeHistory(), "trades", hcfg, conn, fs, root, FAR, TODAY) == {"dates": 1, "left": 0, "rows": 1}
    rows = pq.read_table(f"{root}/trades/venue=fake/date=2026-11-03.parquet").to_pylist()
    assert [(r["trade_id"], r["count"], r["taker_side"]) for r in rows] == [("C-t1", 2.0, "yes")]


def test_a_date_bigger_than_one_run_resumes_at_its_next_chunk_and_starts_over_when_it_grows(conn, tmp_path):
    """A run past its budget keeps the chunks it finished; the date's object appears only once every chunk is in."""
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "bucket/history")
    walk(conn, FakeHistory([[settled(m, D1) for m in "ABCDE"]]))
    hcfg = {"bars_min_volume": 0, "bars_since_days": 30, "chunk_markets": 2}
    final = f"{root}/bars/venue=fake/date=2026-11-03.parquet"

    first = FakeHistory()
    assert history.backfill(first, "bars", hcfg, conn, fs, root, 0, TODAY) == {"dates": 0, "left": 1, "rows": 2}
    assert first.asked == ["A", "B"] and not (tmp_path / "bucket/history/bars").exists()
    assert conn.execute("SELECT cursor, markets, done FROM market_backfill WHERE job = 'bars'").fetchone() == (
        "2", 5, False)

    second = FakeHistory()
    assert history.backfill(second, "bars", hcfg, conn, fs, root, FAR, TODAY) == {"dates": 1, "left": 0, "rows": 5}
    assert second.asked == ["C", "D", "E"], "resumes after the chunk the last run finished"
    assert [r["market_id"] for r in pq.read_table(final).to_pylist()] == list("ABCDE")
    assert not list((tmp_path / "bucket/history/parts").rglob("*.parquet")), "the parts go once merged"
    assert conn.execute("SELECT cursor, markets, rows, done FROM market_backfill WHERE job = 'bars'").fetchone() == (
        None, 5, 5, True)

    walk(conn, FakeHistory([[settled("F", D1)]]), restart=True)
    grown = FakeHistory()
    assert history.backfill(grown, "bars", hcfg, conn, fs, root, 0, TODAY)["dates"] == 0
    assert grown.asked == ["A", "B"], "a date that grew starts over"
    assert len(pq.read_table(final)) == 5, "the old object stands until the new one is whole"
    again = FakeHistory()
    history.backfill(again, "bars", hcfg, conn, fs, root, FAR, TODAY)
    assert again.asked == ["C", "D", "E", "F"] and len(pq.read_table(final)) == 6


def test_main_refuses_a_venue_without_that_history_and_still_runs_the_rest(conn, tmp_path, monkeypatch, capsys):
    class NoTrades:
        name = "none"

        def __init__(self, cfg):
            pass

    ran = []

    class Ok(FakeHistory):
        def __init__(self, cfg):
            super().__init__()
            ran.append(cfg)
    cfg = {"recorder": {"bucket": str(tmp_path / "bucket")},
           "history": {"prefix": "history", "trades_venues": ["none", "ok"], "ok": {"page_gap_s": 0.01}},
           "venues": {"none": {}, "ok": {"page_gap_s": 0.5, "title": "Ok"}}}
    monkeypatch.setattr(history, "load_config", lambda: cfg)
    rc = history.main(["trades"], fs=pafs.LocalFileSystem(), adapters={"none": NoTrades, "ok": Ok})
    assert rc == 1 and "none serves no trades history" in capsys.readouterr().err
    assert ran == [{"page_gap_s": 0.01, "title": "Ok"}]  # the history section's gap replaces the recorder's


# Adapters, against responses recorded 2026-11-06.

def test_kalshi_settled_walks_the_live_side_then_the_historical_one_and_resumes_on_either():
    s = Session((200, "kalshi_settled_live.json"), (200, {"cursor": "", "markets": []}),
                (200, "kalshi_historical_markets.json"))
    pages = list(Kalshi(kcfg(), s, sleep=nosleep).settled(None))
    assert [c for _, c in pages] == ["live:live-c2", "historical:", None]
    assert [u.split("/v2")[1] for u, _ in s.calls] == ["/markets", "/markets", "/historical/markets"]
    assert s.calls[0][1]["status"] == "settled" and "status" not in s.calls[2][1]
    first = pages[0][0][0]
    assert (first.market.status, first.resolution.result, first.resolution.value, first.resolution.volume) == (
        "finalized", "scalar", 0.5, 0.0)
    hist = pages[2][0][0]
    assert (hist.market.market_id, hist.resolution.result, hist.resolution.volume) == (
        "KXITFMATCH-26SEP06VANKAM-VAN", "no", 207766.16)

    s = Session((200, "kalshi_historical_markets.json"))
    assert [c for _, c in Kalshi(kcfg(), s, sleep=nosleep).settled("historical:abc")] == [None]
    assert s.calls[0][1]["cursor"] == "abc" and s.calls[0][0].endswith("/historical/markets")


def _span(mid, settled, opened=None, series="KXITFMATCH"):
    return Span(mid, series, opened, settled)


def test_kalshi_bars_pick_the_side_by_the_cutoff_and_read_both_field_spellings():
    # Settled before market_settled_ts (2026-09-06T00:00Z in the fixture): only the historical side has it.
    old = _span("KXITFMATCH-26SEP06VANKAM-VAN", dt.datetime(2026, 9, 5, 23, 48, tzinfo=UTC),
                dt.datetime(2026, 9, 5, 21, 48, tzinfo=UTC))
    s = Session((200, "kalshi_cutoff.json"), (200, "kalshi_candles_historical.json"))
    [b, _] = Kalshi(kcfg(), s, sleep=nosleep).bars(old, 3600)
    assert s.calls[1][0].endswith("/historical/markets/KXITFMATCH-26SEP06VANKAM-VAN/candlesticks")
    assert s.calls[1][1]["period_interval"] == 60
    assert (b.bid, b.ask, b.close, b.volume, b.open_interest) == (0.48, 0.67, 0.68, 1304.0, 1304.0)

    new = _span("KXNCAAFSPREAD-26NOV05UTSAFAU-FAU33", dt.datetime(2026, 11, 6, 4, 38, tzinfo=UTC), series=None)
    s = Session((200, "kalshi_cutoff.json"), (200, "kalshi_candles_live.json"))
    [b] = Kalshi(kcfg(), s, sleep=nosleep).bars(new, 3600)
    assert s.calls[1][0].endswith("/series/KXNCAAFSPREAD/markets/KXNCAAFSPREAD-26NOV05UTSAFAU-FAU33/candlesticks")
    assert (b.bid, b.ask, b.close, b.volume) == (0.02, 0.16, None, 0.0)


def test_kalshi_bars_split_a_long_market_into_5000_candle_requests():
    settled = dt.datetime(2026, 9, 1, tzinfo=UTC)
    span = _span("OLD-1", settled, settled - dt.timedelta(hours=12000))
    empty = {"candlesticks": []}
    s, slept = Session((200, "kalshi_cutoff.json"), (200, empty), (200, empty), (200, empty)), []
    Kalshi(kcfg(), s, sleep=slept.append).bars(span, 3600)
    windows = [(p["end_ts"] - p["start_ts"]) // 3600 for _, p in s.calls[1:]]
    assert windows == [5000, 5000, 2000] and slept == [0.5, 0.5, 0.5]  # every request after the cutoff call


def test_kalshi_trades_read_each_side_the_market_can_have_trades_on():
    cut = dt.datetime(2026, 9, 6, tzinfo=UTC)
    both = _span("X-1", cut + dt.timedelta(days=1), cut - dt.timedelta(days=1))
    s = Session((200, "kalshi_cutoff.json"), (200, "kalshi_trades_historical.json"), (200, "kalshi_trades_live.json"))
    pages = list(Kalshi(kcfg(), s, sleep=nosleep).trades(both))
    assert [u.split("/v2")[1] for u, _ in s.calls[1:]] == ["/historical/trades", "/markets/trades"]
    assert s.calls[1][1]["ticker"] == "X-1"
    t = pages[1][0]
    assert (t.trade_id, t.price, t.count, t.taker_side) == ("07271366-46d1-bc1d-8fb0-190f623e419c", 0.01, 187.03, "yes")

    live_only = _span("X-2", cut + dt.timedelta(days=2), cut + dt.timedelta(days=1))
    s = Session((200, "kalshi_cutoff.json"), (200, "kalshi_trades_live.json"))
    list(Kalshi(kcfg(), s, sleep=nosleep).trades(live_only))
    assert len(s.calls) == 2 and s.calls[1][0].endswith("/markets/trades")


def test_polymarket_global_bars_look_up_the_yes_token_then_page_the_price_history():
    cid = "0x7d28e63e97000c6737b3588909043805bcabdfed1c80dcce6f44de4a4ad3e062"
    s = Session((200, "polymarket_tokens.json"), (200, "polymarket_prices_page1.json"), (200, "polymarket_prices_page2.json"))
    span = Span(cid, None, dt.datetime(2026, 10, 1, tzinfo=UTC), dt.datetime(2026, 11, 6, tzinfo=UTC))
    bars = Polymarket({"base_url": "https://gamma", "page_gap_s": 0.1}, s, sleep=nosleep).bars(span, 3600)
    assert s.calls[0][1] == {"condition_ids": cid, "closed": "true"}
    assert s.calls[1][0] == "https://data-api.polymarket.com/v2/prices-history"
    assert s.calls[1][1]["token_id"] == "68630982033465666799582155072572882538645464804910125757126795007526276015718"
    assert s.calls[1][1]["bucket_seconds"] == 3600 and "cursor" in s.calls[2][1]
    assert [b.close for b in bars][:3] == [0.505, 0.545, 0.655] and len(bars) > 3
    assert Polymarket({"base_url": "https://gamma"}, Session((200, [])), sleep=nosleep).bars(span, 3600) == []


def test_polymarket_us_bars_turn_the_short_price_into_the_yes_bid():
    s = Session((200, "polymarket_us_prices.json"))
    span = Span("cpagsp-silver-updown-15m-2026-11-06-0700z", None, None, dt.datetime(2026, 11, 6, 7, 15, tzinfo=UTC))
    [b] = PolymarketUS({"base_url": "https://us"}, s, sleep=nosleep).bars(span, 3600)
    assert s.calls[0][1]["symbol"] == span.market_id and s.calls[0][1]["fidelity"] == 60
    assert (b.ask, b.bid) == (0.01, 0.01)


def test_polymarket_us_settled_pages_by_offset_and_ends_on_a_short_page():
    s = Session((200, "polymarket_us_closed.json"))
    pages = list(PolymarketUS({"base_url": "https://us", "page_limit": 500}, s, sleep=nosleep).settled("1000"))
    assert s.calls[0][1]["offset"] == 1000 and pages[-1][1] is None
    assert all(p.resolution.settled_at for page, _ in pages for p in page)


def test_manifold_settled_keeps_resolved_binary_markets_and_pages_by_id():
    s = Session((200, "manifold_markets.json"))
    [(page, cursor)] = list(Manifold({"base_url": "https://m", "page_limit": 1000}, s, sleep=nosleep).settled("prev"))
    assert s.calls[0][0].endswith("/v0/markets") and s.calls[0][1]["before"] == "prev"
    assert cursor is None
    assert [(p.market.market_id, p.resolution.result, p.resolution.value) for p in page] == [
        ("U9R60LZ5z8", "yes", 1.0), ("zIpgI5lEun", "void", None)]
    assert page[0].resolution.volume == pytest.approx(279.95, abs=0.01)
