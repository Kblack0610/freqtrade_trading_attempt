import datetime as dt
import sys
from pathlib import Path

import pytest
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import watcher  # noqa: E402

NOW = dt.datetime(2026, 11, 5, 12, tzinfo=dt.timezone.utc)
LIMIT = 0.03


class Bots:
    """Fake bot APIs, by roster index: profit and state to report, a failure to raise, POSTs seen."""

    def __init__(self, *profits):
        self.profit = list(profits)
        self.state = ["running"] * len(profits)
        self.dry_run = [True] * len(profits)
        self.down = set()
        self.refuse_stop = False
        self.posts = []

    def __call__(self, i, path, post=False):
        if post:
            if path == "stopentry" and self.refuse_stop:
                raise OSError("refused")
            self.posts.append((i, path))
            self.state[i] = "paused" if path == "stopentry" else "running"
            return {}
        if i in self.down:
            raise OSError("connection refused")
        return {"show_config": {"dry_run": self.dry_run[i], "state": self.state[i]},
                "profit": {"profit_all_coin": self.profit[i]},
                "balance": {"starting_capital": 1000.0}}[path]


def row(conn, bot="a"):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM watch WHERE bot = %s", (bot,)).fetchone()


def tick(conn, bots, now=NOW, names=("a",)):
    watcher.tick(conn, list(names), LIMIT, bots, now)


def test_a_loss_past_the_limit_breaches_and_holds_until_the_next_day(conn):
    bots = Bots(10.0)
    tick(conn, bots)
    assert row(conn)["day_open"] == 1010 and row(conn)["breached_at"] is None
    bots.profit[0] = -19.0  # 2.9% of capital down
    tick(conn, bots, NOW + dt.timedelta(minutes=1))
    assert row(conn)["breached_at"] is None
    bots.profit[0] = -21.0  # 3.1% down
    at = NOW + dt.timedelta(minutes=2)
    tick(conn, bots, at)
    assert row(conn)["breached_at"] == at
    bots.profit[0] = 50.0  # a recovery does not clear it the same day
    tick(conn, bots, NOW + dt.timedelta(minutes=3))
    assert row(conn)["breached_at"] == at
    tick(conn, bots, NOW + dt.timedelta(days=1))
    assert row(conn)["breached_at"] is None and row(conn)["day_open"] == 1050


def test_a_real_breach_only_alerts_at_stage_0(conn):
    bots = Bots(0.0)
    tick(conn, bots)
    bots.profit[0] = -100.0
    tick(conn, bots, NOW + dt.timedelta(minutes=1))
    assert row(conn)["breached_at"] is not None and row(conn)["stopped_at"] is None
    assert bots.posts == []


def test_an_unseen_bot_keeps_its_last_good_read_and_the_others_carry_on(conn):
    bots = Bots(5.0, 7.0)
    tick(conn, bots, names=("a", "b"))
    bots.down.add(0)
    bots.profit[1] = 8.0
    later = NOW + dt.timedelta(minutes=1)
    tick(conn, bots, later, names=("a", "b"))
    a = row(conn)
    assert a["checked_at"] == later and a["seen_at"] == NOW and "refused" in a["error"] and a["equity"] == 1005
    assert row(conn, "b")["equity"] == 1008
    bots.down.clear()
    tick(conn, bots, later + dt.timedelta(minutes=1), names=("a", "b"))
    assert row(conn)["error"] is None


def test_a_bot_never_seen_still_gets_a_row_the_alert_can_age(conn):
    bots = Bots(0.0)
    bots.down.add(0)
    tick(conn, bots)
    assert row(conn)["seen_at"] == NOW and row(conn)["equity"] is None
    bots.down.clear()
    tick(conn, bots, NOW + dt.timedelta(minutes=1))
    assert row(conn)["day_open"] == 1000


def test_an_unpriced_open_trade_is_unseen_not_flat(conn):
    bots = Bots(float("nan"))
    tick(conn, bots)
    assert "no usable equity" in row(conn)["error"]


def test_the_drill_stops_entries_on_the_next_pass_and_ends_clean(conn):
    bots = Bots(0.0, 0.0)
    names = ("a", "b")
    tick(conn, bots, names=names)
    watcher.drill(conn, "b")
    at = NOW + dt.timedelta(minutes=1)
    tick(conn, bots, at, names=names)
    assert bots.posts == [(1, "stopentry")]
    assert row(conn, "b")["stopped_at"] == at and row(conn, "b")["breached_at"] == at
    tick(conn, bots, at + dt.timedelta(minutes=1), names=names)
    assert bots.posts == [(1, "stopentry")] and row(conn, "b")["state"] == "paused"
    assert row(conn, "b")["drill_at"] is not None  # a pass must not overwrite the flag

    bots.state[1] = "running"  # the pod restarted mid-drill
    tick(conn, bots, at + dt.timedelta(minutes=2), names=names)
    assert bots.posts == [(1, "stopentry")] * 2 and row(conn, "b")["stopped_at"] == at

    watcher.drill_end(conn, "b", list(names), bots)
    assert bots.posts[-1] == (1, "start")
    tick(conn, bots, at + dt.timedelta(minutes=3), names=names)
    b = row(conn, "b")
    assert (b["state"], b["drill_at"], b["breached_at"], b["stopped_at"]) == ("running", None, None, None)
    assert len(bots.posts) == 3


def test_a_refused_stop_is_recorded_and_retried(conn):
    bots = Bots(0.0, 0.0)
    names = ("a", "b")
    tick(conn, bots, names=names)
    watcher.drill(conn, "a")
    bots.refuse_stop = True
    tick(conn, bots, NOW + dt.timedelta(minutes=1), names=names)
    assert "stopentry" in row(conn)["error"] and row(conn)["stopped_at"] is None
    assert row(conn, "b")["checked_at"] == NOW + dt.timedelta(minutes=1)
    bots.refuse_stop = False
    tick(conn, bots, NOW + dt.timedelta(minutes=2), names=names)
    assert row(conn)["stopped_at"] is not None


def test_a_drill_needs_a_dry_run_bot(conn):
    bots = Bots(0.0)
    bots.dry_run[0] = False
    tick(conn, bots)
    with pytest.raises(SystemExit):
        watcher.drill(conn, "a")
    conn.execute("UPDATE watch SET drill_at = now()")  # and if the flag gets there anyway
    tick(conn, bots, NOW + dt.timedelta(minutes=1))
    assert bots.posts == []


def test_ending_no_drill_starts_nothing(conn):
    bots = Bots(0.0)
    tick(conn, bots)
    with pytest.raises(SystemExit):
        watcher.drill_end(conn, "a", ["a"], bots)
    assert bots.posts == []
