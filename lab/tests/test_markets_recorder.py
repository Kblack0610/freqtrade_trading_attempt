import dataclasses
import datetime as dt

import pyarrow.parquet as pq
from pyarrow import fs as pafs

from markets import recorder
from markets.venue import Quote, Resolution

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

    def __init__(self, pages, resolved=(), broken=False):
        self.pages, self.resolved, self.broken, self.since = pages, list(resolved), broken, None

    def open_markets(self):
        for p in self.pages:
            if self.broken:
                raise RuntimeError("venue down")
            yield p

    def resolutions(self, since):
        self.since = since
        return iter(self.resolved)


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


def test_resolutions_read_from_the_last_good_sweep_and_settle_the_market(conn, tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "b")
    first = FakeVenue([[q("A", 0.1, 0.2)]])
    recorder.sweep(first, VCFG, conn, fs, root, LOOKBACK, T0)
    assert first.since == T0 - LOOKBACK

    t1 = T0 + dt.timedelta(minutes=5)
    r = Resolution(market_id="A", result="no", value=0.0, settled_at=t1)
    later = FakeVenue([[]], resolved=[r, r])  # the overlap returns it twice; stored once
    s = recorder.sweep(later, VCFG, conn, fs, root, LOOKBACK, t1)
    assert later.since == T0 - LOOKBACK and s["resolved"] == 2
    assert conn.execute("SELECT result, value FROM resolutions WHERE market_id = 'A'").fetchone() == ("no", 0.0)
    assert conn.execute("SELECT status FROM markets WHERE market_id = 'A'").fetchone() == ("settled",)


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
