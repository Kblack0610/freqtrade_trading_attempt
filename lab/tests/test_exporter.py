import datetime as dt

from prometheus_client import CollectorRegistry, generate_latest

NOW = dt.datetime.now(dt.timezone.utc)


def scrape(collector) -> dict[str, float]:
    registry = CollectorRegistry()
    registry.register(collector)
    out = {}
    for line in generate_latest(registry).decode().splitlines():
        if line and not line.startswith("#"):
            key, value = line.rsplit(" ", 1)
            out[key] = float(value)
    return out


def test_metrics_read_the_newest_state(conn):
    import db
    from web.exporter import TraderCollector

    conn.execute("INSERT INTO bots (name, exchange, strategy) VALUES ('a', 'kraken', 'S'), ('b', 'kraken', 'S')")
    conn.execute("INSERT INTO bot_snapshots (bot, ts, dry_run, open_trades) VALUES "
                 "('a', %s, false, 9), ('a', %s, true, 2), ('b', %s, NULL, NULL)",
                 (NOW - dt.timedelta(hours=3), NOW - dt.timedelta(hours=1), NOW - dt.timedelta(minutes=30)))
    conn.execute("UPDATE bot_snapshots SET error = 'connection refused' WHERE bot = 'b'")
    conn.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES "
                 "('analysis', 'old', '{}', %s, %s, true), ('analysis', 'bad', '{}', %s, %s, false), "
                 "('analysis', 'now', '{}', %s, NULL, NULL)",
                 (NOW - dt.timedelta(days=2), NOW - dt.timedelta(days=2) + dt.timedelta(hours=1),
                  NOW - dt.timedelta(days=1), NOW - dt.timedelta(days=1) + dt.timedelta(hours=1), NOW))
    conn.execute("INSERT INTO data_coverage (exchange, pair, timeframe, last_ts) VALUES "
                 "('kraken', 'BTC/USD', '5m', %s), ('kraken', 'ETH/USD', '5m', %s)",
                 (NOW - dt.timedelta(minutes=20), NOW - dt.timedelta(hours=5)))
    conn.execute("INSERT INTO suggestions (kind, strategy, exchange, summary, action, gates) VALUES "
                 "('promote', 'S', 'kraken', 's', 'a', '[]'), ('promote', 'T', 'kraken', 's', 'a', '[]')")

    m = scrape(TraderCollector(db.connect))
    assert m["trader_db_up"] == 1
    assert m['trader_bot_dry_run{bot="a"}'] == 1
    assert 'trader_bot_dry_run{bot="b"}' not in m
    assert m['trader_bot_snapshot_error{bot="b"}'] == 1 and m['trader_bot_snapshot_error{bot="a"}'] == 0
    assert m['trader_bot_open_trades{bot="a"}'] == 2
    assert 1700 < m["trader_ingest_age_seconds"] < 1900
    assert m['trader_run_ok{kind="analysis"}'] == 0
    assert m['trader_run_started_timestamp_seconds{kind="analysis"}'] == NOW.timestamp()
    assert m['trader_run_last_ok_timestamp_seconds{kind="analysis"}'] == (NOW - dt.timedelta(days=2, hours=-1)).timestamp()
    assert 'trader_run_ok{kind="walkforward"}' not in m
    assert 1100 < m['trader_candle_age_seconds{exchange="kraken"}'] < 1300
    assert m['trader_coverage_series{exchange="kraken"}'] == 2
    assert m['trader_suggestions{kind="promote"}'] == 2


def test_an_unreadable_database_reports_down():
    from web.exporter import TraderCollector

    def broken():
        raise OSError("no route to host")

    assert scrape(TraderCollector(broken)) == {"trader_db_up": 0.0}


def test_watch_metrics(conn):
    import db
    from web.exporter import TraderCollector

    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, state, capital, equity, day_open, breached_at) VALUES "
                 "('a', %s, %s, 'paused', 1000, 960, 1000, %s)", (NOW, NOW - dt.timedelta(minutes=6), NOW))
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, error) VALUES ('new', %s, %s, 'refused')", (NOW, NOW))
    conn.commit()
    m = scrape(TraderCollector(db.connect))
    assert m['trader_watch_day_loss_ratio{bot="a"}'] == 0.04
    assert m['trader_watch_breached{bot="a"}'] == 1 and m['trader_watch_breached{bot="new"}'] == 0
    assert m['trader_watch_running{bot="a"}'] == 0 and 'trader_watch_running{bot="new"}' not in m
    assert m['trader_watch_unseen_seconds{bot="a"}'] >= 360 and m["trader_watch_age_seconds"] < 60
