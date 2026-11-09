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
    from lab import db
    from web.exporter import TraderCollector

    conn.execute("INSERT INTO bots (name, exchange, strategy) VALUES ('a', 'kraken', 'S'), ('b', 'kraken', 'S')")
    conn.execute("INSERT INTO bot_snapshots (bot, ts, dry_run, open_trades) VALUES "
                 "('a', %s, false, 9), ('a', %s, true, 2), ('b', %s, NULL, NULL)",
                 (NOW - dt.timedelta(hours=3), NOW - dt.timedelta(hours=1), NOW - dt.timedelta(minutes=30)))
    conn.execute("UPDATE bot_snapshots SET error = 'connection refused' WHERE bot = 'b'")
    conn.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES "
                 "('analysis', 'old', '{}', %s, %s, true), ('analysis', 'bad', '{}', %s, %s, false), "
                 "('analysis', 'now', '{}', %s, NULL, NULL), "
                 "('reproduce', 'old', '{\"checked\": 4, \"mismatch\": 0}', %s, %s, true), "
                 "('reproduce', 'new', '{\"checked\": 3, \"mismatch\": 1}', %s, %s, true)",
                 (NOW - dt.timedelta(days=2), NOW - dt.timedelta(days=2) + dt.timedelta(hours=1),
                  NOW - dt.timedelta(days=1), NOW - dt.timedelta(days=1) + dt.timedelta(hours=1), NOW,
                  NOW - dt.timedelta(days=2), NOW - dt.timedelta(days=2), NOW - dt.timedelta(hours=2), NOW - dt.timedelta(hours=1)))
    conn.execute("INSERT INTO data_coverage (exchange, pair, timeframe, last_ts) VALUES "
                 "('kraken', 'BTC/USD', '5m', %s), ('kraken', 'ETH/USD', '5m', %s)",
                 (NOW - dt.timedelta(minutes=20), NOW - dt.timedelta(hours=5)))
    conn.execute("INSERT INTO funding_rates (exchange, pair, ts, rate) VALUES ('okx', 'BTC/USDT:USDT', %s, 0.0001)",
                 (NOW - dt.timedelta(hours=8),))
    conn.execute("INSERT INTO suggestions (kind, strategy, exchange, summary, action, gates) VALUES "
                 "('promote', 'S', 'kraken', 's', 'a', '[]'), ('promote', 'T', 'kraken', 's', 'a', '[]')")
    conn.execute("INSERT INTO climate_scorecard (kind, name, exchange, variant, cell, days, trades, lo, trials) VALUES "
                 "('strategy', 'S', 'kraken', 'tuned', 'up/low', 90, 9, 0.1, 5), "
                 "('strategy', 'T', 'kraken', 'tuned', 'up/low', 90, 9, -0.1, 5), "
                 "('mixture', 'm', '', 'abc', 'down/high', 90, 0, NULL, 5), "
                 "('benchmark', 'hold BTC', '', '', 'up/low', 90, 0, 0.3, 5)")

    m = scrape(TraderCollector(db.connect))
    assert m["trader_db_up"] == 1
    assert m['trader_climate_passing{kind="strategy",regime="up/low"}'] == 1
    assert m['trader_climate_passing{kind="mixture",regime="down/high"}'] == 0
    assert 'trader_climate_passing{kind="benchmark",regime="up/low"}' not in m
    assert m['trader_bot_dry_run{bot="a"}'] == 1
    assert 'trader_bot_dry_run{bot="b"}' not in m
    assert m['trader_bot_snapshot_error{bot="b"}'] == 1 and m['trader_bot_snapshot_error{bot="a"}'] == 0
    assert m['trader_bot_open_trades{bot="a"}'] == 2
    assert 1700 < m["trader_ingest_age_seconds"] < 1900
    assert m['trader_run_ok{kind="analysis"}'] == 0
    assert m['trader_run_started_timestamp_seconds{kind="analysis"}'] == NOW.timestamp()
    assert m['trader_run_last_ok_timestamp_seconds{kind="analysis"}'] == (NOW - dt.timedelta(days=2, hours=-1)).timestamp()
    assert 'trader_run_ok{kind="walkforward"}' not in m
    assert m["trader_repro_checked"] == 3 and m["trader_repro_mismatch"] == 1 and m['trader_run_ok{kind="reproduce"}'] == 1
    assert 1100 < m['trader_candle_age_seconds{exchange="kraken"}'] < 1300
    assert m['trader_coverage_series{exchange="kraken"}'] == 2
    assert m['trader_suggestions{kind="promote"}'] == 2
    assert 28700 < m['trader_futures_age_seconds{exchange="okx",kind="funding"}'] < 28900
    assert 'trader_futures_age_seconds{exchange="okx",kind="open_interest"}' not in m


def test_an_unreadable_database_reports_down():
    from web.exporter import TraderCollector

    def broken():
        raise OSError("no route to host")

    assert scrape(TraderCollector(broken)) == {"trader_db_up": 0.0}


def test_watch_metrics(conn):
    from lab import db
    from web.exporter import TraderCollector

    now = dt.datetime.now(dt.timezone.utc)  # not the module's NOW: the age check is relative to the scrape
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, state, capital, equity, day_open, breached_at) VALUES "
                 "('a', %s, %s, 'paused', 1000, 960, 1000, %s)", (now, now - dt.timedelta(minutes=6), now))
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, error) VALUES ('new', %s, %s, 'refused')", (now, now))
    conn.commit()
    m = scrape(TraderCollector(db.connect))
    assert m['trader_watch_day_loss_ratio{bot="a"}'] == 0.04
    assert m['trader_watch_breached{bot="a"}'] == 1 and m['trader_watch_breached{bot="new"}'] == 0
    assert m['trader_watch_running{bot="a"}'] == 0 and 'trader_watch_running{bot="new"}' not in m
    assert m['trader_watch_unseen_seconds{bot="a"}'] >= 360 and m["trader_watch_age_seconds"] < 60


def test_market_metrics(conn):
    from lab import db
    from web.exporter import TraderCollector

    now = dt.datetime.now(dt.timezone.utc)
    conn.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, resolved, ok, error) VALUES "
                 "('kalshi', %s, %s, 179000, 0, 12000, 40, true, NULL), ('kalshi', %s, %s, 0, 0, 0, 0, false, 'boom'), "
                 "('kalshi', %s, %s, 0, 0, 0, 0, false, 'boom'), ('manifold', %s, %s, 26000, 0, 9, 0, true, NULL), "
                 "('predictit', %s, %s, 900, 0, 1, 0, true, NULL)",
                 (now - dt.timedelta(minutes=30), now - dt.timedelta(minutes=27), now - dt.timedelta(minutes=10),
                  now - dt.timedelta(minutes=10), now - dt.timedelta(minutes=5), now - dt.timedelta(minutes=5),
                  now - dt.timedelta(minutes=3), now - dt.timedelta(minutes=2), now - dt.timedelta(days=8),
                  now - dt.timedelta(days=8)))
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES ('kalshi', 'Kalshi', 'exchange'), ('odds_api', 'Odds', 'sportsbook')")
    conn.execute("INSERT INTO markets (venue, market_id, title, market_type, status, first_seen, updated_at) VALUES "
                 "('kalshi', 'K', 't', 'binary', 'open', now(), now()), ('odds_api', 'g', 't', 'binary', 'open', now(), now())")
    conn.execute("INSERT INTO market_links (venue_a, market_id_a, outcome_a, venue_b, market_id_b, outcome_b, source, status) VALUES "
                 "('odds_api', 'g', 'A', 'kalshi', 'K', 'yes', 'rule:kalshi_game', 'confirmed'), "
                 "('odds_api', 'g', 'B', 'kalshi', 'K', 'no', 'llm:claude-haiku', 'proposed')")
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at, equity, open_positions, marked_at) "
                 "VALUES ('paper-a', 'longshot', 'kalshi', 'v', '{}', 1000, %s, 900, 4, %s), "
                 "('paper-new', 'longshot', 'kalshi', 'w', '{}', 1000, %s, NULL, NULL, NULL)", (now, now - dt.timedelta(minutes=5), now))
    conn.execute("INSERT INTO broker_balances (broker, read_at, paper, equity, error) VALUES ('alpaca_paper', %s, true, 100000, NULL), "
                 "('alpaca_paper', %s, true, NULL, 'HTTP 503'), ('robinhood_crypto', %s, false, 5000, NULL)",
                 (now - dt.timedelta(minutes=10), now, now - dt.timedelta(minutes=10)))
    m = scrape(TraderCollector(db.connect))
    assert 1600 < m['trader_markets_sweep_age_seconds{venue="kalshi"}'] < 1700
    assert m['trader_markets_sweep_ok{venue="kalshi"}'] == 0 and m['trader_markets_sweep_ok{venue="manifold"}'] == 1
    assert m['trader_markets_sweeps_failed_1h{venue="kalshi"}'] == 2 and m['trader_markets_sweeps_1h{venue="kalshi"}'] == 3
    assert m['trader_markets_sweep_seen{venue="kalshi"}'] == 179000 and m['trader_markets_sweep_changes{venue="kalshi"}'] == 12000
    assert m['trader_markets_sweep_duration_seconds{venue="kalshi"}'] == 180
    assert not any('venue="predictit"' in k for k in m)  # no sweep in a week: the venue is not recorded any more
    assert m['trader_markets_links{source="rule",status="confirmed"}'] == 1
    assert m['trader_markets_links{source="llm",status="proposed"}'] == 1
    assert m['trader_paper_equity{account="paper-a"}'] == 900 and m['trader_paper_capital{account="paper-a"}'] == 1000
    assert m['trader_paper_open_positions{account="paper-a"}'] == 4
    assert 290 < m['trader_paper_marked_age_seconds{account="paper-a"}'] < 320
    assert not any('account="paper-new"' in k for k in m)  # never marked: no equity to alert on yet
    assert m['trader_broker_equity{broker="alpaca_paper"}'] == 100000  # the good read, not the failed newer one
    assert 590 < m['trader_broker_read_age_seconds{broker="alpaca_paper"}'] < 620
    # a real account exports its read age, never its balance
    assert 'trader_broker_equity{broker="robinhood_crypto"}' not in m
    assert 590 < m['trader_broker_read_age_seconds{broker="robinhood_crypto"}'] < 620
