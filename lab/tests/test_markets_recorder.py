import dataclasses
import datetime as dt

import pyarrow.parquet as pq
from pyarrow import fs as pafs

from lab.markets import recorder
from lab.market import Quote, Resolution

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
LOOKBACK = dt.timedelta(hours=1)
VCFG = {"title": "Fake", "kind": "exchange"}


def q(mid, bid, ask, ts=T0, status="active"):
    return Quote(market_id=mid, outcome="yes", ts=ts, event_id="EV-1", series_id="EV", title=f"market {mid}",
                 subtitle=None, market_type="binary", status=status, open_time=None,
                 close_time=dt.datetime(2026, 11, 9, tzinfo=UTC), bid=bid, ask=ask, last=None, bid_size=10.0,
                 ask_size=10.0, volume=0.0, open_interest=0.0)


class FakeVenue:
    name = "fake"

    def __init__(self, pages, resolved=(), broken=False, page_size=None):
        self.pages, self.resolved, self.broken, self.since, self.cursor = pages, list(resolved), broken, None, None
        self.page_size = page_size or max(len(self.resolved), 1)

    def open_markets(self):
        for p in self.pages:
            if self.broken:
                raise RuntimeError("venue down")
            yield p

    def resolutions(self, since, cursor=None):
        self.since, self.cursor = since, cursor
        fresh = [r for r in self.resolved if r.settled_at >= since]  # newest first, as a venue lists them
        for i in range(int(cursor or 0), len(fresh), self.page_size):
            nxt = i + self.page_size
            yield fresh[i:nxt], str(nxt) if nxt < len(fresh) else None


def objects(root):
    return sorted(str(p) for p in root.rglob("*.parquet"))


def test_first_sweep_records_everything_then_only_what_changed(conn, tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "bucket/quotes")
    first = recorder.sweep(FakeVenue([[q("A", 0.1, 0.2), q("B", 0.5, 0.6)], [q("C", 0.3, 0.4)]]),
                           VCFG, conn, fs, root, LOOKBACK, T0)
    assert (first["seen"], first["new_markets"], first["changes"]) == (3, 3, 3)
    assert first["object_key"].endswith("venue=fake/date=2026-11-06/060000Z.parquet")

    t1 = T0 + dt.timedelta(minutes=5)
    second = recorder.sweep(FakeVenue([[q("A", 0.1, 0.2, t1), q("B", 0.55, 0.6, t1)], [q("C", 0.3, 0.4, t1)]]),
                            VCFG, conn, fs, root, LOOKBACK, t1)
    assert (second["seen"], second["new_markets"], second["changes"]) == (3, 0, 1)
    rows = pq.read_table(second["object_key"]).to_pylist()
    assert [(r["market_id"], r["bid"], r["ts"]) for r in rows] == [("B", 0.55, t1)]
    assert conn.execute("SELECT bid, quoted_at FROM outcomes WHERE market_id = 'B'").fetchone() == (0.55, t1)
    assert conn.execute("SELECT quoted_at FROM outcomes WHERE market_id = 'A'").fetchone() == (T0,)

    t2 = t1 + dt.timedelta(minutes=5)
    quiet = recorder.sweep(FakeVenue([[q("A", 0.1, 0.2, t2)]]), VCFG, conn, fs, root, LOOKBACK, t2)
    assert quiet["changes"] == 0 and quiet["object_key"] is None
    assert len(objects(tmp_path)) == 2
    assert conn.execute("SELECT count(*), bool_and(ok) FROM market_sweeps").fetchone() == (3, True)


def test_a_metadata_change_alone_updates_the_market_without_a_quote_row(conn, tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "b")
    recorder.sweep(FakeVenue([[q("A", 0.1, 0.2)]]), VCFG, conn, fs, root, LOOKBACK, T0)
    moved = dataclasses.replace(q("A", 0.1, 0.2), close_time=dt.datetime(2026, 11, 10, tzinfo=UTC))
    s = recorder.sweep(FakeVenue([[moved]]), VCFG, conn, fs, root, LOOKBACK, T0 + dt.timedelta(minutes=5))
    assert (s["new_markets"], s["changes"]) == (0, 0)
    assert conn.execute("SELECT close_time FROM markets WHERE market_id = 'A'").fetchone()[0].day == 10


def test_page_state_excludes_history_and_counts_new_multi_outcome_markets_once(conn, tmp_path, monkeypatch):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "b")
    history = [q(str(i), 0.1, 0.2) for i in range(100)]
    recorder.sweep(FakeVenue([history]), VCFG, conn, fs, root, LOOKBACK, T0)
    read_state = recorder.state
    sizes = []

    def measured_state(conn, venue, ids):
        meta, book = read_state(conn, venue, ids)
        sizes.append((len(meta), len(book)))
        return meta, book

    monkeypatch.setattr(recorder, "state", measured_state)
    new = q("new", 0.3, 0.4)
    s = recorder.sweep(FakeVenue([[q("0", 0.1, 0.2), new],
                                 [q("1", 0.2, 0.3), dataclasses.replace(new, outcome="no")]]),
                       VCFG, conn, fs, root, LOOKBACK, T0 + dt.timedelta(minutes=5))
    assert sizes == [(1, 1), (1, 1)]
    assert (s["seen"], s["new_markets"], s["changes"]) == (4, 1, 3)
    assert conn.execute("SELECT count(*) FROM markets").fetchone() == (101,)
    assert conn.execute("SELECT count(*) FROM outcomes WHERE market_id = 'new'").fetchone() == (2,)


def test_failure_after_a_page_leaves_the_stored_state_and_objects_unchanged(conn, tmp_path):
    import pytest

    fs, root = pafs.LocalFileSystem(), str(tmp_path / "b")
    recorder.sweep(FakeVenue([[q("A", 0.1, 0.2)]]), VCFG, conn, fs, root, LOOKBACK, T0)

    class FailsAfterPage(FakeVenue):
        def open_markets(self):
            yield [q("A", 0.8, 0.9), q("B", 0.3, 0.4)]
            raise RuntimeError("second page failed")

    with pytest.raises(RuntimeError, match="second page failed"):
        recorder.sweep(FailsAfterPage([]), VCFG, conn, fs, root, LOOKBACK, T0 + dt.timedelta(minutes=5))
    assert len(objects(tmp_path)) == 1
    assert conn.execute("SELECT market_id, bid FROM outcomes").fetchall() == [("A", 0.1)]
    assert conn.execute("SELECT count(*) FROM market_sweeps").fetchone() == (1,)


def test_resolutions_read_from_the_last_good_sweep_and_settle_the_market(conn, tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "b")
    first = FakeVenue([[q("A", 0.1, 0.2)]])
    recorder.sweep(first, VCFG, conn, fs, root, LOOKBACK, T0)
    assert first.since == T0 - LOOKBACK

    t1 = T0 + dt.timedelta(minutes=5)
    r = Resolution(market_id="A", result="no", value=0.0, settled_at=t1)
    later = FakeVenue([[]], resolved=[r, r])  # the overlap returns it twice; stored once
    s = recorder.sweep(later, VCFG, conn, fs, root, LOOKBACK, t1)
    assert later.since == T0 - LOOKBACK and s["resolved"] == 1
    assert conn.execute("SELECT result, value FROM resolutions WHERE market_id = 'A'").fetchone() == ("no", 0.0)
    assert conn.execute("SELECT status FROM markets WHERE market_id = 'A'").fetchone() == ("settled",)


def test_a_settled_resolution_read_again_is_not_written_again(conn, tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "b")
    t1 = T0 + dt.timedelta(minutes=5)
    a, c = (Resolution(market_id=m, result="no", value=0.0, settled_at=t1) for m in "AC")
    recorder.sweep(FakeVenue([[q("A", 0.1, 0.2), q("B", 0.1, 0.2), q("C", 0.1, 0.2)]], resolved=[a]), VCFG, conn,
                   fs, root, LOOKBACK, t1)
    # C's resolution came from another writer (the history walk), which leaves its market active
    conn.execute("INSERT INTO resolutions (venue, market_id, result, value, settled_at) VALUES ('fake', 'C', 'no', 0, %s)",
                 (t1,))

    t2 = T0 + dt.timedelta(days=5)  # the venue re-stamped A and C days after they settled; B settles now
    again = [dataclasses.replace(a, settled_at=t2), Resolution(market_id="B", result="yes", value=1.0, settled_at=t2),
             dataclasses.replace(c, settled_at=t2)]
    s = recorder.sweep(FakeVenue([[]], resolved=again), VCFG, conn, fs, root, LOOKBACK, t2)
    assert s["resolved"] == 2
    assert conn.execute("SELECT market_id, settled_at FROM resolutions ORDER BY 1").fetchall() == [
        ("A", t1), ("B", t2), ("C", t1)]
    assert conn.execute("SELECT market_id, status FROM markets ORDER BY 1").fetchall() == [
        ("A", "settled"), ("B", "settled"), ("C", "settled")]


def test_a_walk_past_its_budget_stops_at_a_page_and_the_next_sweep_resumes_it(conn, tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "b")
    recorder.sweep(FakeVenue([[q("A", 0.1, 0.2), q("B", 0.1, 0.2), q("C", 0.1, 0.2)]]), VCFG, conn, fs, root, LOOKBACK, T0)
    t = [T0 + dt.timedelta(minutes=5 * i) for i in range(5)]
    rs = [Resolution(market_id=m, result="no", value=0.0, settled_at=t[1]) for m in "ABC"]

    def run(i, budget_s):
        v = FakeVenue([[q("D", 0.1, 0.2, t[i])]], resolved=rs, page_size=1)
        return v, recorder.sweep(v, VCFG, conn, fs, root, LOOKBACK, t[i], budget_s=budget_s)

    v, s = run(1, 0)  # out of time after the first page
    assert (v.since, v.cursor, s["resolved"], s["walk_cursor"]) == (T0 - LOOKBACK, None, 1, "1")
    v, s = run(2, 0)  # the next sweep resumes that walk, not a new one from its own start
    assert (v.since, v.cursor, s["resolved"], s["walk_cursor"]) == (T0 - LOOKBACK, "1", 1, "2")
    v, s = run(3, float("inf"))
    assert (v.since, v.cursor, s["resolved"], s["walk_cursor"]) == (T0 - LOOKBACK, "2", 1, None)
    v, s = run(4, float("inf"))  # a new walk from the start of the one that finished, so nothing between is missed
    assert (v.since, v.cursor, s["resolved"]) == (t[1] - LOOKBACK, None, 0)
    assert conn.execute("SELECT count(*), bool_and(ok) FROM market_sweeps").fetchone() == (5, True)
    assert conn.execute("SELECT count(*) FROM markets WHERE status = 'settled'").fetchone() == (3,)


def test_a_resumed_walk_still_reads_what_settled_since_it_began_and_a_dead_cursor_walks_again(conn, tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "b")
    recorder.sweep(FakeVenue([[q("X", 0.1, 0.2)]]), VCFG, conn, fs, root, LOOKBACK, T0)
    t1, t2, t3 = (T0 + dt.timedelta(hours=h) for h in (1, 2, 3))
    old = [Resolution(market_id=m, result="no", value=0.0, settled_at=t1) for m in "ABC"]
    v = FakeVenue([[q("X", 0.1, 0.2, t1)]], resolved=old, page_size=1)
    assert recorder.sweep(v, VCFG, conn, fs, root, LOOKBACK, t1, budget_s=0)["walk_cursor"] == "1"

    new = Resolution(market_id="D", result="yes", value=1.0, settled_at=t2)  # settled after the walk began
    v = FakeVenue([[q("X", 0.1, 0.2, t2)]], resolved=[new, *old], page_size=1)
    s = recorder.sweep(v, VCFG, conn, fs, root, LOOKBACK, t2, budget_s=0)
    assert conn.execute("SELECT 1 FROM resolutions WHERE market_id = 'D'").fetchone() == (1,)
    assert s["walk_cursor"] is not None

    class Refuses(FakeVenue):
        def resolutions(self, since, cursor=None):
            if cursor:
                raise RuntimeError("cursor expired")
            return super().resolutions(since, cursor)

    s = recorder.sweep(Refuses([[q("X", 0.1, 0.2, t3)]], resolved=[new, *old], page_size=1), VCFG, conn, fs, root,
                       LOOKBACK, t3)
    assert s["walk_cursor"] is None
    assert conn.execute("SELECT count(*) FROM resolutions").fetchone() == (4,)
    assert conn.execute("SELECT bool_and(ok) FROM market_sweeps").fetchone() == (True,)


def test_a_venue_that_fails_midway_writes_nothing_and_logs_a_failed_sweep(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "load_config", lambda: {
        "recorder": {"bucket": str(tmp_path / "b"), "prefix": "quotes"},
        "venues": {"fake": VCFG, "good": VCFG}})

    class Good(FakeVenue):
        name = "good"

    adapters = {"fake": lambda cfg: FakeVenue([[q("A", 0.1, 0.2)]], broken=True),
                "good": lambda cfg: Good([[q("G", 0.4, 0.5)]])}
    assert recorder.main([], fs=pafs.LocalFileSystem(), adapters=adapters) == 1
    assert conn.execute("SELECT venue, ok, error FROM market_sweeps ORDER BY venue").fetchall() == [
        ("fake", False, "RuntimeError: venue down"), ("good", True, None)]
    assert conn.execute("SELECT venue, market_id FROM markets").fetchall() == [("good", "G")]
    assert [p.split("venue=")[1].split("/")[0] for p in objects(tmp_path)] == ["good"]
