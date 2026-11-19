import datetime as dt

import pytest
from prometheus_client import CollectorRegistry, generate_latest


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

    NOW = dt.datetime.now(dt.timezone.utc)  # at the test, not at import: the ages below are read against the clock

    conn.execute("INSERT INTO bots (name, exchange, strategy, cohort) VALUES ('a', 'kraken', 'S', 'league'), "
                 "('b', 'kraken', 'S', 'aggressive')")
    conn.execute("INSERT INTO bot_snapshots (bot, ts, dry_run, open_trades, profit_all_pct) VALUES "
                 "('a', %s, false, 9, 5), ('a', %s, true, 2, 1.5), ('b', %s, NULL, NULL, -2)",
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

    conn.execute("INSERT INTO markouts (bot, trade_id, open_date, horizon_min, pair, exchange, side, fill_slip, ret, "
                 "btc_ret, beta, net, null_net, null_draws) VALUES "
                 "('a', 1, %s, 60, 'ETH/USD', 'kraken', 1, 0.001, 0.02, 0.01, 1, 0.008, -0.002, 20), "
                 "('a', 2, %s, 60, 'ETH/USD', 'kraken', 1, 0.003, 0.0, 0.0, 1, -0.002, -0.002, 20)", (NOW, NOW))
    conn.execute("INSERT INTO reconcile (bot, n, pair, kind, delay_s, price_gap, fee_gap, profit_gap) VALUES "
                 "('a', 0, 'ETH/USD', 'matched', 60, 0.002, 0, -0.004), ('a', 1, 'ETH/USD', 'bt_only', NULL, NULL, NULL, NULL)")
    m = scrape(TraderCollector(db.connect))
    assert m["trader_db_up"] == 1
    assert m['trader_markout_net{bot="a",horizon="60m"}'] == pytest.approx(0.003)
    assert m['trader_markout_null{bot="a",horizon="60m"}'] == pytest.approx(-0.002)
    assert m['trader_markout_entries{bot="a",horizon="60m"}'] == 2
    assert m['trader_markout_fill_slip{bot="a"}'] == pytest.approx(0.002)
    assert m['trader_reconcile_trades{bot="a",kind="bt_only"}'] == 1
    assert m['trader_shortfall{bot="a",component="price"}'] == 0.002
    assert m['trader_shortfall_delay_seconds{bot="a"}'] == 60
    assert m['trader_climate_passing{kind="strategy",regime="up/low"}'] == 1
    assert m['trader_climate_passing{kind="mixture",regime="down/high"}'] == 0
    assert 'trader_climate_passing{kind="benchmark",regime="up/low"}' not in m
    assert m['trader_bot_dry_run{bot="a"}'] == 1
    assert 'trader_bot_dry_run{bot="b"}' not in m
    assert m['trader_bot_snapshot_error{bot="b"}'] == 1 and m['trader_bot_snapshot_error{bot="a"}'] == 0
    assert m['trader_bot_open_trades{bot="a"}'] == 2
    assert m['trader_bot_profit_pct{bot="a",cohort="league"}'] == 1.5
    assert m['trader_bot_profit_pct{bot="b",cohort="aggressive"}'] == -2
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
    assert m['trader_watch_unseen_limit_seconds{bot="a"}'] == 300


def test_a_bot_the_newest_pass_did_not_write_is_not_exported_and_a_down_one_is(conn):
    """A renamed bot's old row is read by no pass, so it aged as unseen forever (TraderWatchBotBlind on the 2026-11-10
    renames, still firing five days later). The row stays, with its stop, for the kill switch."""
    from lab import db
    from web.exporter import TraderCollector

    now = dt.datetime.now(dt.timezone.utc)
    old = now - dt.timedelta(days=5)
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, stopped_at) VALUES ('old', %s, %s, %s)", (old, old, old))
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, error) VALUES ('down', %s, %s, 'refused'), "
                 "('up', %s, %s, NULL)", (now, now - dt.timedelta(minutes=6), now, now))
    conn.commit()
    m = scrape(TraderCollector(db.connect))
    assert not [k for k in m if 'bot="old"' in k]
    assert m['trader_watch_unseen_seconds{bot="down"}'] >= 360 and m['trader_watch_unseen_seconds{bot="up"}'] < 60
    assert conn.execute("SELECT stopped_at FROM watch WHERE bot = 'old'").fetchone()[0] == old


def test_a_paper_account_on_a_slow_venue_is_blind_only_past_its_venue_limit(conn):
    """odds_api is swept every 6 h, so its accounts are marked every 6 h: a 5-minute limit called them blind all day."""
    from lab import db
    from web.exporter import TraderCollector

    now = dt.datetime.now(dt.timezone.utc)
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) VALUES "
                 "('paper-tt', 'tt_totals', 'odds_api', 'v', '{}', 100, %s), ('paper-ls', 'longshot', 'kalshi', 'v', '{}', 100, %s)",
                 (now, now))
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at) VALUES ('paper-tt', %s, %s), ('paper-ls', %s, %s)",
                 (now, now - dt.timedelta(hours=1), now, now))
    conn.commit()
    m = scrape(TraderCollector(db.connect))
    from lab.markets import sweep_ages
    book, venues = sweep_ages()
    assert venues["odds_api"] > book  # the premise: odds_api is swept slower than the paper book age
    assert m['trader_watch_unseen_limit_seconds{bot="paper-tt"}'] == venues["odds_api"] * 60
    assert m['trader_watch_unseen_limit_seconds{bot="paper-ls"}'] == book * 60  # kalshi: the paper book age


def test_an_account_marked_by_a_5_minute_pass_is_not_blind_when_one_pass_ends_late(conn):
    """A kalshi or stock account is marked once a */5 pass, so a 300 s limit was one pass with no slack: half the gaps
    run 300 to 412 s, and TraderWatchBotBlind fired for a dozen accounts every hour (2026-11-18). A bot the watcher
    reads itself keeps [watcher] unseen_after_seconds."""
    from lab import db, stocks
    from web.exporter import TraderCollector

    now = dt.datetime.now(dt.timezone.utc)
    late = now - dt.timedelta(seconds=380)  # one pass that ended 80 s after the one before
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) VALUES "
                 "('paper-ls', 'longshot', 'kalshi', 'v', '{}', 100, %s)", (now,))
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) VALUES "
                 "('stock-trend-v', 'trend', 'alpaca', 'v', '{}', 10000, %s)", (now,))
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at) VALUES ('paper-ls', %s, %s), ('stock-trend-v', %s, %s), "
                 "('bot', %s, %s)", (now, late, now, late, now, late))
    conn.commit()
    m = scrape(TraderCollector(db.connect))
    for bot in ("paper-ls", "stock-trend-v"):
        assert m[f'trader_watch_unseen_seconds{{bot="{bot}"}}'] < m[f'trader_watch_unseen_limit_seconds{{bot="{bot}"}}']
    assert m['trader_watch_unseen_limit_seconds{bot="stock-trend-v"}'] == stocks.load_config()["paper"]["max_quote_age_min"] * 60
    assert m['trader_watch_unseen_limit_seconds{bot="bot"}'] == 300
    assert m['trader_watch_unseen_seconds{bot="bot"}'] > 300


def test_a_league_limit_under_the_watcher_default_keeps_the_default(conn, monkeypatch):
    from lab import db, stocks
    from web.exporter import TraderCollector

    now = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(stocks, "load_config", lambda: {"paper": {"max_quote_age_min": 2}})
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) VALUES "
                 "('stock-trend-v', 'trend', 'alpaca', 'v', '{}', 10000, %s)", (now,))
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at) VALUES ('stock-trend-v', %s, %s)", (now, now))
    conn.commit()
    assert scrape(TraderCollector(db.connect))['trader_watch_unseen_limit_seconds{bot="stock-trend-v"}'] == 300


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
                 "('paper-new', 'longshot', 'kalshi', 'w', '{}', 1000, %s, NULL, NULL, NULL), "
                 "('paper-gone', 'tt_totals', 'odds_api', 'v', '{}', 1000, %s, 1000, 0, %s)",
                 (now, now - dt.timedelta(minutes=5), now, now, now - dt.timedelta(days=1)))
    for mid, at in (("K1", now - dt.timedelta(hours=1)), ("K0", now - dt.timedelta(hours=25))):
        conn.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, "
                     "fee) VALUES ('paper-a', %s, 'no', 10, %s, %s, 0, NULL, 0)", (mid, at, at))
    conn.execute("INSERT INTO broker_balances (broker, read_at, paper, equity, error) VALUES ('alpaca_paper', %s, true, 100000, NULL), "
                 "('alpaca_paper', %s, true, NULL, 'HTTP 503'), ('robinhood_crypto', %s, false, 5000, NULL)",
                 (now - dt.timedelta(minutes=10), now, now - dt.timedelta(minutes=10)))
    m = scrape(TraderCollector(db.connect))
    assert 1600 < m['trader_markets_sweep_age_seconds{venue="kalshi"}'] < 1700
    assert m['trader_markets_sweep_ok{venue="kalshi"}'] == 0 and m['trader_markets_sweep_ok{venue="manifold"}'] == 1
    assert m['trader_markets_sweeps_failed_1h{venue="kalshi"}'] == 2 and m['trader_markets_sweeps_1h{venue="kalshi"}'] == 3
    assert m['trader_markets_sweep_seen{venue="kalshi"}'] == 179000 and m['trader_markets_sweep_changes{venue="kalshi"}'] == 12000
    assert m['trader_markets_sweep_duration_seconds{venue="kalshi"}'] == 180
    assert not any('venue="predictit"' in k for k in m if k.startswith("trader_markets_sweep"))  # no sweep in a week: the venue is not recorded any more
    assert m['trader_markets_links{source="rule",status="confirmed"}'] == 1
    assert m['trader_markets_links{source="llm",status="proposed"}'] == 1
    assert m['trader_paper_equity{account="paper-a"}'] == 900 and m['trader_paper_capital{account="paper-a"}'] == 1000
    assert m['trader_paper_open_positions{account="paper-a"}'] == 4
    assert 290 < m['trader_paper_marked_age_seconds{account="paper-a"}'] < 320
    assert m['trader_paper_orders_24h{venue="kalshi"}'] == 1  # K1 counts though unfilled; K0 is over a day old
    assert m['trader_paper_orders_24h{venue="polymarket_us"}'] == 0  # in [paper] venues with no order: the volume watch's red
    assert 'trader_paper_orders_24h{venue="odds_api"}' not in m  # not a paper venue: no series for the watch to read
    assert not any('account="paper-new"' in k for k in m)  # never marked: no equity to alert on yet
    assert not any('account="paper-gone"' in k for k in m)  # its venue left [paper]: no pass marks it again
    assert not any(k.startswith("trader_shadow_marked_age") for k in m)  # no mark yet: nothing to call stale
    conn.execute("INSERT INTO shadow_accounts (account, strategy, exchange, cohort, capital, promoted_on, entry, state) "
                 "VALUES ('shadow-a', 'A', 'kraken', 'short', 1000, %s, '{}', 'trial'), "
                 "('shadow-b', 'B', 'kraken', 'short', 1000, %s, '{}', 'trial')", (now.date(), now.date()))
    conn.execute("INSERT INTO shadow_marks (account, day, ret, equity) VALUES ('shadow-a', %s, 0, 1000)",
                 (now.date() - dt.timedelta(days=2),))
    m = scrape(TraderCollector(db.connect))
    assert m['trader_shadow_accounts{cohort="short",state="trial"}'] == 2
    assert 86400 <= m["trader_shadow_marked_age_seconds"] < 2 * 86400
    assert m['trader_broker_equity{broker="alpaca_paper"}'] == 100000  # the good read, not the failed newer one
    assert 590 < m['trader_broker_read_age_seconds{broker="alpaca_paper"}'] < 620
    # a real account exports its read age, never its balance
    assert 'trader_broker_equity{broker="robinhood_crypto"}' not in m
    assert 590 < m['trader_broker_read_age_seconds{broker="robinhood_crypto"}'] < 620


def test_calibration_metrics(conn):
    from lab import db
    from web.exporter import TraderCollector

    conn.execute("INSERT INTO calibration (gate, edge, oos_months, runs, n, passed, rate) VALUES "
                 "('stack', 0, 60, 1, 8, 0, 0), ('stack', 0.02, 60, 1, 4, 3, 0.75), ('bias', 0.01, 12, 1, 4, 4, 1)")
    conn.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES ('calibration', 'x', '{}', now(), now(), true)")
    m = scrape(TraderCollector(db.connect))
    assert m['trader_calibration_fpr{gate="stack",oos_months="60"}'] == 0
    assert m['trader_calibration_power{edge="0.02",gate="stack",oos_months="60"}'] == 0.75
    assert m['trader_calibration_power{edge="0.01",gate="bias",oos_months="12"}'] == 1
    assert 'trader_run_last_ok_timestamp_seconds{kind="calibration"}' in m


def test_decision_metrics(conn):
    """Decisions per action, the newest pass's funnel, and a fill after the first traced entry with no decision."""
    from lab import db
    from web.exporter import TraderCollector

    now = dt.datetime.now(dt.timezone.utc)
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) "
                 "VALUES ('paper-a', 'longshot', 'kalshi', 'v', '{}', 1000, %s)", (now,))
    for mid, at in (("OLD", now - dt.timedelta(hours=2)), ("A", now), ("B", now)):
        conn.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, "
                     "fee) VALUES ('paper-a', %s, 'no', 10, %s, %s, 10, 0.95, 0.04)", (mid, at, at))
    conn.execute("INSERT INTO decisions (league, account, venue, instrument, decided_at, action, rule, fill_source, "
                 "fill_id) VALUES ('event_contracts', 'paper-a', 'kalshi', 'A', %s, 'enter', 'r', 'paper', 2), "
                 "('event_contracts', 'paper-a', 'kalshi', 'C', %s, 'near_miss', 'r', NULL, NULL)", (now, now))
    conn.execute("INSERT INTO decision_funnel (league, account, venue, pass_at, reason, n) VALUES "
                 "('event_contracts', 'paper-a', 'kalshi', %s, 'spread', 9), "
                 "('event_contracts', 'paper-a', 'kalshi', %s, 'spread', 4), "
                 "('event_contracts', 'paper-a', 'kalshi', %s, 'ordered', 2)", (now - dt.timedelta(minutes=5), now, now))
    m = scrape(TraderCollector(db.connect))
    assert m['trader_decisions_total{account="paper-a",action="enter",league="event_contracts"}'] == 1
    assert m['trader_decisions_total{account="paper-a",action="near_miss",league="event_contracts"}'] == 1
    assert m['trader_decision_funnel{account="paper-a",check="spread",league="event_contracts"}'] == 4
    assert m['trader_decision_funnel{account="paper-a",check="ordered",league="event_contracts"}'] == 2
    # B has no decision; OLD was sent before the first traced entry, so it is not counted
    assert m['trader_fills_untraced{league="event_contracts"}'] == 1
    assert m['trader_fills_untraced{league="stocks"}'] == 0
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) VALUES "
                 "('stock-a', 'trend', 'alpaca', 'v', '{}', 1000, now())")
    conn.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, fee) "
                 "VALUES ('stock-a', 'SPY', 'long', 600, now(), now(), 1, 600, 0.12)")
    assert scrape(TraderCollector(db.connect))['trader_fills_untraced{league="stocks"}'] == 1


def test_crypto_untraced_counts_filled_bot_trades_since_the_first_crypto_decision(conn):
    """A bot trade with an entry fill and no decision is untraced; one still waiting on its entry order, or opened
    before the first crypto decision, is not."""
    from psycopg.types.json import Jsonb

    from lab import db
    from web.exporter import TraderCollector

    now = dt.datetime.now(dt.timezone.utc)
    conn.execute("INSERT INTO bots (name, exchange, strategy) VALUES ('k-a', 'kraken', 'S'), ('k-b', 'kraken', 'S')")
    for bot, tid, at, entries in (("k-a", 1, now, 1), ("k-b", 1, now, 1), ("k-b", 2, now, 1), ("k-b", 3, now, 0),
                                  ("k-b", 4, now - dt.timedelta(days=1), 1)):
        conn.execute("INSERT INTO bot_trades (bot, trade_id, open_date, pair, is_open, raw) VALUES "
                     "(%s, %s, %s, 'BTC/USD', true, %s)", (bot, tid, at, Jsonb({"nr_of_successful_entries": entries})))
    conn.execute("INSERT INTO decisions (league, account, venue, instrument, decided_at, action, rule, fill_source, "
                 "fill_id) VALUES ('crypto', 'k-a', 'kraken', 'BTC/USD', %s, 'enter', 'S: entry signal', 'freqtrade', 1)",
                 (now,))
    m = scrape(TraderCollector(db.connect))
    assert m['trader_fills_untraced{league="crypto"}'] == 2  # k-b's 1 and 2: k-a's 1 is no proof for k-b's


@pytest.mark.parametrize("venue,minutes", [("kalshi", 15), ("polymarket", 45), ("odds_api", 390)])
def test_recorder_freshness_uses_venue_config(venue, minutes):
    from web.exporter import market_metrics

    sweeps = [{"venue": venue, "last_ok": None}]
    metric = next(market_metrics(None, sweeps, [], []))
    assert metric.name == "trader_markets_sweep_stale_seconds"
    assert metric.samples[0].labels == {"venue": venue}
    assert metric.samples[0].value == minutes * 60


def test_recorder_freshness_changes_with_config_and_only_reports_active_venues(tmp_path, monkeypatch):
    from lab import markets
    from web.exporter import market_metrics

    cfg = tmp_path / "markets.toml"
    cfg.write_text('[paper]\nmax_book_age_min = 23\n[venues.odds_api]\nstale_after_min = 401\n[venues.retired]\n')
    monkeypatch.setattr(markets, "CONFIG", cfg)
    sweeps = [{"venue": v, "last_ok": None} for v in ("odds_api", "kalshi")]
    metric = next(market_metrics(None, sweeps, [], []))
    assert [(s.labels["venue"], s.value) for s in metric.samples] == [("odds_api", 401 * 60), ("kalshi", 23 * 60)]
    assert not next(market_metrics(None, [], [], [])).samples
