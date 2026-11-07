import datetime as dt

import pyarrow.parquet as pq
from pyarrow import fs as pafs

from lab.markets import backtest, compact, recorder
from lab.markets.venue import Quote

UTC = dt.timezone.utc
D = dt.datetime(2026, 11, 6, tzinfo=UTC)
GRACE = dt.timedelta(hours=1)


def q(mid, bid, ts):
    return Quote(market_id=mid, outcome="yes", ts=ts, event_id="EV-1", series_id="EV", title=mid, subtitle=None,
                 market_type="binary", status="active", open_time=None, close_time=None, bid=bid, ask=bid + 0.1,
                 last=None, bid_size=10.0, ask_size=10.0, volume=0.0, open_interest=0.0)


def part(fs, root, ts, *quotes):
    return recorder.write_quotes(fs, root, "fake", ts, list(quotes))


def files(tmp_path):
    return sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())


def test_a_settled_day_becomes_one_object_and_reads_the_same(tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "quotes")
    t = [D + dt.timedelta(minutes=5 * i) for i in range(3)]
    part(fs, root, t[0], q("A", 0.1, t[0]), q("B", 0.5, t[0]))
    part(fs, root, t[1], q("A", 0.2, t[1]))
    part(fs, root, t[2], q("B", 0.6, t[2]))
    part(fs, root, D + dt.timedelta(days=1), q("A", 0.3, D + dt.timedelta(days=1)))  # today: left alone
    before = backtest.load_quotes(fs, root, "fake", D.date(), D.date() + dt.timedelta(days=1))

    s = compact.compact_venue(fs, root, "fake", GRACE, D + dt.timedelta(days=1, hours=1))
    assert s == {"days": 1, "parts": 3, "rows": 4}
    assert files(tmp_path) == ["quotes/venue=fake/date=2026-11-06/day.parquet",
                               "quotes/venue=fake/date=2026-11-07/000000Z.parquet"]
    day = f"{root}/venue=fake/date=2026-11-06/day.parquet"
    assert compact.held(fs, day) == ["000000Z.parquet", "000500Z.parquet", "001000Z.parquet"]
    after = backtest.load_quotes(fs, root, "fake", D.date(), D.date() + dt.timedelta(days=1))
    assert after.equals(before)

    again = compact.compact_venue(fs, root, "fake", GRACE, D + dt.timedelta(days=1, hours=1))
    assert again == {"days": 0, "parts": 0, "rows": 0}


def test_inside_the_grace_a_day_is_not_touched(tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "quotes")
    part(fs, root, D, q("A", 0.1, D))
    assert compact.compact_venue(fs, root, "fake", GRACE, D + dt.timedelta(days=1, minutes=59))["days"] == 0
    assert files(tmp_path) == ["quotes/venue=fake/date=2026-11-06/000000Z.parquet"]


def test_a_late_part_folds_in_and_a_part_already_held_is_only_deleted(tmp_path):
    fs, root = pafs.LocalFileSystem(), str(tmp_path / "quotes")
    now = D + dt.timedelta(days=2)
    part(fs, root, D, q("A", 0.1, D))
    compact.compact_venue(fs, root, "fake", GRACE, now)
    # a run that died after the write but before the delete leaves a held part behind; a late sweep adds another
    part(fs, root, D, q("A", 0.1, D))
    late = D + dt.timedelta(hours=23, minutes=55)
    part(fs, root, late, q("A", 0.4, late))

    s = compact.compact_venue(fs, root, "fake", GRACE, now)
    assert s == {"days": 1, "parts": 2, "rows": 2}
    day = f"{root}/venue=fake/date=2026-11-06/day.parquet"
    assert files(tmp_path) == ["quotes/venue=fake/date=2026-11-06/day.parquet"]
    assert compact.held(fs, day) == ["000000Z.parquet", "235500Z.parquet"]
    assert [(r["bid"], r["ts"]) for r in pq.read_table(day).to_pylist()] == [(0.1, D), (0.4, late)]


def test_main_compacts_every_recorded_venue(tmp_path, monkeypatch):
    fs = pafs.LocalFileSystem()
    monkeypatch.setattr(compact, "load_config", lambda: {"recorder": {"bucket": str(tmp_path), "prefix": "quotes"}})
    root = f"{tmp_path}/quotes"
    recorder.write_quotes(fs, root, "fake", D, [q("A", 0.1, D)])
    recorder.write_quotes(fs, root, "other", D, [q("A", 0.1, D)])
    assert compact.main([], fs, D + dt.timedelta(days=3)) == 0
    assert files(tmp_path) == ["quotes/venue=fake/date=2026-11-06/day.parquet",
                               "quotes/venue=other/date=2026-11-06/day.parquet"]
