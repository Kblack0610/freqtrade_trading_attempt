import datetime as dt
import json
import re
import urllib.parse

import pytest
from psycopg.types.json import Jsonb

NOW = dt.datetime.now(dt.timezone.utc)  # reset per test by fresh_now
ENV = {"TRADER_WEB_BASE_URL": "http://testserver", "FORGEJO_URL": "https://git.example", "OAUTH_CLIENT_ID": "cid",
       "OAUTH_CLIENT_SECRET": "csecret", "SESSION_KEY": "k" * 32, "ALLOWED_USERS": "alice"}


@pytest.fixture(autouse=True)
def fresh_now():
    """Re-read NOW per test: the pages render "3m ago" against the real clock, so an import-time NOW drifts on a slow run."""
    global NOW
    NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def site(monkeypatch):
    from fastapi.testclient import TestClient

    from web import app as app_mod, auth

    monkeypatch.setattr(app_mod, "cfg", auth.Config(ENV))
    monkeypatch.setattr(app_mod, "CACHE_TTL", 0.0)  # tests write between requests; test_cache turns it back on
    app_mod._cache.clear()
    return app_mod, TestClient(app_mod.app, follow_redirects=False)


@pytest.fixture
def signed_in(site):
    app_mod, client = site
    client.cookies.set(app_mod.auth.COOKIE, app_mod.auth.make_session(app_mod.cfg, "alice"))
    return app_mod, client


def seed(c):
    c.execute("INSERT INTO bots (name, exchange, strategy) VALUES ('kraken-s001', 'kraken', 'Strategy001')")
    c.execute("INSERT INTO drift (bot, strategy, exchange, since, bt_start, bt_end, live_entries, bt_entries, matched, "
              "live_closed, bt_closed, live_expectancy, bt_expectancy, live_profit, bt_profit, gap, gap_trades) VALUES "
              "('kraken-s001', 'Strategy001', 'kraken', %s, %s, %s, 4, 6, 3, 3, 5, 0.012, 0.0185, 4, 9.25, -0.0065, 3)",
              (NOW - dt.timedelta(days=2), NOW - dt.timedelta(days=2), NOW))
    for h, pct in ((3, 1.0), (2, -0.5), (1, 2.5)):
        c.execute("INSERT INTO bot_snapshots (bot, ts, dry_run, closed_trades, open_trades, profit_all_pct, "
                  "profit_all_abs, winrate_pct, max_drawdown_pct, balance_total) "
                  "VALUES ('kraken-s001', %s, true, 3, 1, %s, 25, 66, 1.2, 1025)", (NOW - dt.timedelta(hours=h), pct))
    c.execute("INSERT INTO bot_trades (bot, trade_id, open_date, pair, is_open, close_date, stake_amount, profit_ratio, "
              "profit_abs, exit_reason, raw) VALUES ('kraken-s001', 1, %s, 'BTC/USD', false, %s, 330, 0.012, 4, 'roi', "
              "'{}')", (NOW - dt.timedelta(hours=5), NOW - dt.timedelta(hours=4)))
    c.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES ('analysis', '2026-11-04 03:00:00', "
              "%s, now() - interval '1 hour', now(), true)",
              (json.dumps({"strategies": 3, "chunks_run": 4, "chunks_cached": 10, "chunks_failed": 1, "bias_run": 2,
                           "skipped": ["Scalp on binanceus: no 1m candles"] + [f"S{i} on okx: 5m not fully backfilled" for i in range(4)]}),))
    c.execute("INSERT INTO strategies (name, file, file_hash, timeframe, load_error) VALUES "
              "('Strategy001', 'user_data/strategies/Strategy001.py', 'h1', '5m', NULL), "
              "('Planted', 'user_data/strategies/Planted.py', 'h2', '5m', NULL), "
              "('Broken', 'user_data/strategies/Broken.py', 'h3', NULL, 'failed to import')")
    c.execute("INSERT INTO backtest_chunks (strategy, exchange, chunk_start, chunk_end, cache_key, ok, trades, "
              "start_balance, final_balance, max_drawdown, run_id) VALUES "
              "('Strategy001', 'binanceus', '2026-01-01', '2026-11-04', 'k', true, 1, 1000, 1032, 0.04, 1)")
    c.execute("INSERT INTO backtest_chunks (strategy, exchange, chunk_start, chunk_end, cache_key, ok, attempts, error, "
              "run_id) VALUES ('Strategy001', 'binanceus', '2025-01-01', '2026-01-01', 'k', false, 2, 'exit 2: boom', 1)")
    c.execute("INSERT INTO backtest_trades (chunk_id, pair, open_date, close_date, profit_ratio, profit_abs, exit_reason) "
              "VALUES (1, 'ETH/USD', '2026-03-01 10:00+00', '2026-03-01 18:00+00', 0.032, 32, 'roi')")
    for day, ret in (("2026-03-01", 0.032), ("2026-03-02", -0.01), ("2026-03-03", 0.005)):
        c.execute("INSERT INTO strategy_daily VALUES ('Strategy001', 'binanceus', %s, %s, 0, 1)", (day, ret))
        c.execute("INSERT INTO regimes VALUES (%s, 80000, 'up', 0.5, 'mid')", (day,))
    for name, trades, sharpe_lo in (("Strategy001", 40, 0.4), ("Planted", 90, 3.0)):
        c.execute("INSERT INTO strategy_windows (strategy, exchange, win, days, start_day, trades, total_return, sharpe, "
                  "sharpe_lo, sharpe_hi, dsr, trials, trials_raw, trades_needed, max_drawdown, expectancy, gross_expectancy, "
                  "gross_expectancy_lo, fee_per_trade, capital_exposure, hold_excess, hold_excess_lo, hold_excess_hi, worst_open) "
                  "VALUES (%s, 'binanceus', '365d', 365, '2025-11-04', %s, 0.032, 1.1, %s, 1.8, 0.62, 1.7, 2, 4321, 0.04, "
                  "0.012, 0.0145, 0.004, 0.0025, 0.2, 0.123, -0.05, 0.3, -0.271)",
                  (name, trades, sharpe_lo))
    c.execute("INSERT INTO overfit (exchange, win, strategies, days, blocks, splits, pbo) "
              "VALUES ('binanceus', '365d', 2, 365, 16, 12870, 0.64)")
    c.execute("INSERT INTO estimates VALUES ('Strategy001', 'binanceus', 90, -0.05, 0.021, 0.09, 0.31, 0.06, 5000, 1, 365)")
    c.execute("INSERT INTO correlations VALUES ('Strategy001', 'binanceus', 'Strategy002', 0.83, 300)")
    c.execute("INSERT INTO residual_clusters VALUES ('Strategy001', 'binanceus', 1, 300), ('Strategy002', 'binanceus', 1, 300), "
              "('Lone', 'binanceus', 2, 300)")
    c.execute("INSERT INTO residual_venues VALUES ('binanceus', 3, 2, 2.4)")
    c.execute("INSERT INTO bias_checks (strategy, kind, file_hash, has_bias, biased_indicators) VALUES "
              "('Planted', 'lookahead', 'h2', true, 'next_close'), ('Strategy001', 'lookahead', 'h1', false, NULL), "
              "('Strategy001', 'recursive', 'h1', NULL, NULL)")
    c.execute("INSERT INTO data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, oldest_reached) "
              "VALUES ('binanceus', 'BTC/USD', '5m', '2019-09-01', %s, 700000, true)", (NOW,))
    c.execute("INSERT INTO open_interest (exchange, pair, ts, value_usd) VALUES ('okx', 'BTC/USDT:USDT', '2026-08-03', 3.6e9)")


def test_healthz_needs_no_sign_in(site):
    assert site[1].get("/healthz").text == "ok"


@pytest.mark.parametrize("path", ["/", "/live", "/live/rows", "/bots/x", "/strategies", "/strategies/x", "/suggestions", "/confidence", "/book", "/coverage", "/guide", "/data", "/runs", "/runs/1", "/markets", "/links", "/markets/book", "/accounts", "/leagues", "/today", "/trades/paper/1"])
def test_signed_out_goes_to_login(site, path):
    r = site[1].get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_a_signed_out_poll_redirects_the_page_not_the_table(site):
    r = site[1].get("/live/rows", headers={"HX-Request": "true"})
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/login"


def test_live_shows_open_positions_with_candle_and_snapshot_age(conn, signed_in):
    seed(conn)
    conn.execute("INSERT INTO bots (name, exchange, strategy) VALUES ('okx-idle', 'okx', 'Idle')")
    snap = NOW - dt.timedelta(minutes=3)
    late = NOW - dt.timedelta(minutes=30)  # 27 min before the snapshot: over two 5m candles
    conn.execute("INSERT INTO bot_snapshots (bot, ts, dry_run, raw) VALUES ('kraken-s001', %s, true, %s)",
                 (snap, Jsonb({"timeframe": "5m", "candles": {"ETH/USD": int((snap - dt.timedelta(minutes=5)).timestamp() * 1000),
                                                              "SOL/USD": int(late.timestamp() * 1000)}})))
    for tid, pair, ratio, usd, price in ((7, "ETH/USD", 0.021, 6.93, 3063.0), (8, "SOL/USD", -0.04, -13.2, 144.0)):
        conn.execute("INSERT INTO bot_trades (bot, trade_id, open_date, pair, is_open, open_rate, stake_amount, profit_ratio, "
                     "profit_abs, raw) VALUES ('kraken-s001', %s, %s, %s, true, 3000, 330, %s, %s, %s)",
                     (tid, NOW - dt.timedelta(hours=tid), pair, ratio, usd, Jsonb({"current_rate": price})))
    _, client = signed_in
    page = client.get("/live")
    assert page.status_code == 200 and 'hx-get="/live/rows"' in page.text and "every 30s" in page.text
    assert "/static/htmx.min.js" in page.text
    rows_ = client.get("/live/rows", headers={"HX-Request": "true"})
    assert rows_.status_code == 200 and "<header" not in rows_.text
    for needle in ("ETH/USD", "3,063.0000", "2.10%", "6.93", "SOL/USD", "-4.00%", "-6.27 USD", "660.00 USD",
                   "3m ago", "8m ago", '<td class="down" title="5m candles">30m ago</td>', "okx-idle", "no open positions"):
        assert needle in rows_.text, needle
    assert '<td class="" title="5m candles">8m ago</td>' in rows_.text


def test_login_redirects_to_forgejo_with_state(site):
    app_mod, client = site
    r = client.get("/login")
    url = urllib.parse.urlparse(r.headers["location"])
    q = urllib.parse.parse_qs(url.query)
    assert r.status_code == 303 and url.netloc == "git.example" and url.path == "/login/oauth/authorize"
    assert q["redirect_uri"] == ["http://testserver/oauth/callback"] and q["client_id"] == ["cid"]
    assert q["state"] == [r.cookies[app_mod.auth.STATE_COOKIE]]


def test_session_cookie_rejects_forged_expired_and_unlisted():
    from web import auth

    cfg = auth.Config(ENV)
    good = auth.make_session(cfg, "alice")
    assert auth.read_session(cfg, good) == "alice"
    payload, sig = good.rsplit(".", 1)
    assert auth.read_session(cfg, payload + "." + sig[::-1]) is None
    assert auth.read_session(cfg, auth.make_session(auth.Config({**ENV, "SESSION_KEY": "x" * 32}), "alice")) is None
    assert auth.read_session(cfg, auth.make_session(cfg, "alice", now=0)) is None
    assert auth.read_session(cfg, auth.make_session(cfg, "mallory")) is None
    assert auth.read_session(cfg, "garbage") is None


def test_callback_rejects_a_state_it_did_not_issue(site):
    app_mod, client = site
    client.cookies.set(app_mod.auth.STATE_COOKIE, "issued")
    assert client.get("/oauth/callback", params={"code": "c", "state": "other"}).status_code == 400
    assert client.get("/oauth/callback", params={"code": "c"}).status_code == 400


def test_callback_signs_in_only_allowed_logins(site, monkeypatch):
    app_mod, client = site
    client.cookies.set(app_mod.auth.STATE_COOKIE, "s1")
    monkeypatch.setattr(app_mod.auth, "login_for_code", lambda cfg, code: "mallory")
    r = client.get("/oauth/callback", params={"code": "c", "state": "s1"})
    assert r.status_code == 403 and "mallory" in r.text and app_mod.auth.COOKIE not in r.cookies

    monkeypatch.setattr(app_mod.auth, "login_for_code", lambda cfg, code: "alice")
    r = client.get("/oauth/callback", params={"code": "c", "state": "s1"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert app_mod.auth.read_session(app_mod.cfg, r.cookies[app_mod.auth.COOKIE]) == "alice"


def test_login_for_code_returns_the_forgejo_login():
    import httpx

    from web import auth

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/login/oauth/access_token":
            body = urllib.parse.parse_qs(req.content.decode())
            assert body["code"] == ["c"] and body["client_secret"] == ["csecret"]
            return httpx.Response(200, json={"access_token": "t"})
        assert req.headers["authorization"] == "Bearer t"
        return httpx.Response(200, json={"login": "alice"})

    cfg = auth.Config({**ENV, "FORGEJO_API_URL": "http://forgejo.svc"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert auth.login_for_code(cfg, "c", client=c) == "alice"


def test_every_page_renders_with_data(conn, signed_in):
    seed(conn)
    _, client = signed_in
    pages = {"/": ["kraken-s001", "2.50%", "binanceus", "Best strategies", "/strategies/Strategy001?exchange=binanceus",
                   "Crypto league", "of 1 bots reporting", "Research gates", "Money and guard", "Dry run"],
             "/bots/kraken-s001": ["Strategy001 on kraken", "<svg", "BTC/USD", "1.20%", "Dry run against backtest", "1.85%",
                                   "3 of 4 dry-run entries matched the backtest; the gap is -0.65%"],
             "/strategies": ["Strategy001", "0.40", "62%", "-5% / ", "3.2%", "1.20%", "lookahead bias", "Broken", "failed to import",
                             "binanceus: no 1m candles: Scalp", 'okx: 5m not fully backfilled: <span title="S0, S1, S2, S3">4 strategies</span>', "Fees by venue", "0.25%", "1.45%", "worth 1.7 independent",
                             "4321", '<td class="n down" title="2 strategies">64%</td>'],
             "/strategies?win=all": ["No results for this window yet"],
             "/strategies/Strategy001": ["binanceus", "<svg", "3.2%", "Strategy002: 0.83", "up", "ETH/USD",
                                         "exit 2: boom", "not checked", "clean", "31%", "1.45%", "0.25%", "20%", "12.3%",
                                         "-5.0%", "-27.1%", "4321", "At a glance on binanceus", "no out-of-sample months",
                                         "not measured yet", "no climate scorecard", "not scored", "Payoff ratio",
                                         "Wins cut to 50%"],
             "/coverage": ["Diversity archive", "cells filled: 1, from 1 strategy", "/strategies/Strategy001?exchange=binanceus",
                           "Sharpe low 0.40", "By climate it earns in", "untested", "Residual clusters",
                           "3 strategies, 2 clusters, residual effective count 2.4", "1 strategies sit alone"],
             "/data": ["BTC/USD", "700,000", "2019-09-01", "Open interest", "<td>okx</td>", "2026-08-03"],
             "/runs": ["2026-11-04 03:00:00", "ok", "60 min"],
             "/runs/1": ["exit 2: boom", "binanceus: no 1m candles: Scalp", "4 strategies", "Strategy001"]}
    for path, needles in pages.items():
        r = client.get(path)
        assert r.status_code == 200, path
        for n in needles:
            assert n in r.text, (path, n)
    assert "Not dry run" not in client.get("/").text
    assert client.get("/bots/nobody").status_code == 404
    assert client.get("/runs/99").status_code == 404
    assert client.get("/strategies/Nope").status_code == 404
    assert client.get("/strategies?win=7d").status_code == 404


def test_a_biased_strategy_ranks_last_however_good_it_looks(conn, signed_in):
    seed(conn)
    text = signed_in[1].get("/strategies").text
    assert text.index("/strategies/Strategy001?") < text.index("/strategies/Planted?")
    top = signed_in[1].get("/").text
    assert "/strategies/Planted?" not in top


def test_recursive_bias_warns_but_does_not_sink_a_strategy(conn, signed_in):
    seed(conn)
    conn.execute("UPDATE bias_checks SET has_bias = true, biased_indicators = 'htsine' "
                 "WHERE strategy = 'Strategy001' AND kind = 'recursive'")
    text = signed_in[1].get("/strategies").text
    assert text.index("/strategies/Strategy001?") < text.index("/strategies/Planted?")
    assert ">short warmup</span>" in text
    assert "/strategies/Strategy001?" in signed_in[1].get("/").text


def test_overview_warns_when_a_bot_is_not_dry_run(conn, signed_in):
    seed(conn)
    conn.execute("INSERT INTO bot_snapshots (bot, ts, dry_run) VALUES ('kraken-s001', now(), false)")
    assert "Not dry run: kraken-s001" in signed_in[1].get("/").text


def test_kill_switch_lists_only_bots_the_watcher_still_reads(conn, signed_in):
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, state) VALUES ('live-bot', now(), now(), 'running'), "
                 "('retired-bot', now() - interval '3 days', now() - interval '3 days', 'running')")
    page = signed_in[1].get("/accounts").text
    assert "live-bot" in page and "retired-bot" not in page


def test_each_cohort_gets_a_row_and_profit_stays_inside_its_lane(conn, signed_in):
    seed(conn)
    conn.execute("INSERT INTO overlays (id, grp, params) VALUES ('ab12cd34', 'slots', '{\"max_open_trades\": 5}')")
    conn.execute("INSERT INTO bots (name, exchange, strategy, cohort, overlay) VALUES "
                 "('kraken-agg', 'kraken', 'Strategy001', 'aggressive', 'ab12cd34'), "
                 "('coinbase-agg', 'coinbase', 'Strategy001', 'aggressive', ''), ('new-agg', 'okx', 'S', 'aggressive', '')")
    conn.execute("INSERT INTO bot_snapshots (bot, ts, profit_all_pct, profit_all_abs, balance_total, closed_trades) VALUES "
                 "('kraken-agg', now(), 4, 40, 1040, 2), ('coinbase-agg', now(), -1, -10, 990, 1)")
    _, client = signed_in
    top = client.get("/").text
    assert "Total profit" not in top and "League profit" not in top
    crypto = top[top.index("Crypto league"):].split("</article>")[0]
    assert "55.00 USD" in crypto and "of 4 bots reporting" in crypto
    assert top.index('href="/cohorts#league"') < top.index('href="/cohorts#aggressive"')
    assert "2 of 3" in top and "1.50%" in top and "30.00" in top
    page = client.get("/cohorts").text
    assert '<h2 class="sec" id="aggressive">' in page and 'ab12cd34 <span class="muted">max_open_trades 5</span>' in page
    assert page.rindex("kraken-agg") > page.index('<h2 class="sec" id="aggressive">') > page.rindex("kraken-s001")
    assert ">Backtest match</th>" in page and "<td class=\"n\">3 of 4</td>" in page and "<td class=\"n\">66%</td>" in page  # moved off /


def test_the_event_contract_league_is_not_a_cohort_and_scores_beside_the_crypto_league(conn, signed_in):
    seed(conn)
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at, equity, "
                 "realized, open_positions, marked_at) VALUES "
                 "('paper-longshot-aaaa1111', 'longshot', 'kalshi', 'aaaa1111', "
                 "'{\"buy\": \"yes\", \"lo\": 0.9, \"hi\": 0.99, \"hours\": 24}', 1000, now(), 1020, 20, 2, now()), "
                 "('paper-longshot-cccc3333', 'longshot', 'kalshi', 'cccc3333', '{}', 1000, now(), NULL, 0, 0, NULL)")
    for i, profit in enumerate((10, -30, 5)):  # settled profits in order: the curve falls 3% from its first peak
        conn.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, fee, "
                     "settled_at, payout, profit) VALUES ('paper-longshot-aaaa1111', %s, 'no', 10, now(), now(), 10, 0.9, 0, "
                     "now() + make_interval(mins => %s), 1, %s)", (f"M{i}", i, profit))
    _, client = signed_in
    top = client.get("/").text
    assert 'href="/cohorts#markets"' not in top and 'href="/cohorts#league"' in top
    assert "35.00 USD" not in top  # no total across lanes
    cohorts = client.get("/cohorts").text
    assert '<h2 class="sec" id="markets">' not in cohorts and "paper-longshot" not in cohorts
    page = client.get("/leagues").text
    crypto = page[page.index('<h2 class="sec" id="crypto">'):page.index('<h2 class="sec" id="event-contracts">')]
    events = page[page.index('<h2 class="sec" id="event-contracts">'):page.index('<h2 class="sec" id="stocks">')]
    assert "kraken-s001" in crypto and "Strategy001" in crypto
    assert "longshot: buy yes when the yes mid is 0.90-0.99, within 24 h of close" in events
    assert "2.00%" in events and "3.0%" in events and "paper-longshot-cccc3333" in events
    head = lambda t: re.findall(r"<th[^>]*>([^<]+)</th>", t[t.index("<thead>"):t.index("</thead>")])  # noqa: E731
    assert head(crypto) == head(events) and "Max drawdown" in head(events)  # one scorecard, the same columns
    stocks = page[page.index('<h2 class="sec" id="stocks">'):]
    assert "No accounts in this league yet" in stocks  # no stock pass has run


def test_pages_render_on_an_empty_database(conn, signed_in):
    assert signed_in[1].get("/").text.count('<span class="tag">no data</span>') == 9  # every lane but the guard
    for path in ("/", "/cohorts", "/data", "/runs", "/strategies", "/suggestions", "/walkforward", "/coverage", "/markets", "/links", "/markets/book", "/accounts", "/calls", "/leagues", "/today"):
        assert signed_in[1].get(path).status_code == 200, path


def test_suggestions_show_each_kind_with_its_gates(conn, signed_in):
    from lab.crypto import suggest

    seed(conn)
    assert suggest.run(conn)["suggestions"] == {"retire": 2, "fix": 1}
    text = signed_in[1].get("/suggestions").text
    for needle in ("Retire <span", "next_close", "Broken", "failed to import", "Fix <span", "startup_candle_count",
                   "Gates: 0 of 1 pass", "Promote <span"):
        assert needle in text, needle
    assert "1 fix, 2 retire" in signed_in[1].get("/").text
    assert "startup_candle_count" in signed_in[1].get("/strategies/Strategy001").text


def test_calls_show_each_call_its_scores_and_the_record(conn, signed_in):
    first = signed_in[1].get("/calls").text
    assert "No calls yet" in first and "No rule call scored yet" in first
    cid = conn.execute("INSERT INTO calls (who, author, thesis, ticker, direction, instrument, entry_session, horizons, "
                       "benchmark, signal_source) VALUES ('human', 'kenneth', 'board ousted the CEO', 'NVDA', 'short', 'equity', "
                       "'2026-11-13', '{1,5}', 'SMH', 'event') RETURNING id").fetchone()[0]
    conn.execute("INSERT INTO call_scores (call_id, horizon, exit_session, entry_close, exit_close, bench_entry, "
                 "bench_exit, ret, pnl, excess) VALUES (%s, 1, '2026-11-16', 100, 90, 200, 201, -0.1, 0.0996, 0.105)",
                 (cid,))
    body = signed_in[1].get("/calls").text
    for needle in ("short NVDA", "board ousted the CEO", "9.96%", "10.50%", "pending", "not asked", "Mean net", "event", "Signal source"):
        assert needle in body, needle
    rule = conn.execute("INSERT INTO calls (who, author, thesis, ticker, direction, instrument, entry_session, horizons, "
                        "benchmark, signal_source) VALUES ('rule', 'rule:filing-shock', '8-K shock', 'AMD', 'long', 'equity', "
                        "'2026-11-13', '{1}', 'SMH', 'event') RETURNING id").fetchone()[0]
    conn.execute("INSERT INTO call_rule_events (rule, variant, feed_source, feed_id, ticker, ingested_at, call_id) "
                 "VALUES ('filing-shock', 'abcdef0123', 'edgar', 'x', 'AMD', now(), %s)", (rule,))
    conn.execute("INSERT INTO call_scores (call_id, horizon, exit_session, entry_close, exit_close, bench_entry, "
                 "bench_exit, ret, pnl, excess) VALUES (%s, 1, '2026-11-16', 100, 103, 200, 201, 0.03, 0.0296, 0.025)",
                 (rule,))
    body = signed_in[1].get("/calls").text
    for needle in ("Event rules", "filing-shock", "abcdef01", "no edge yet"):
        assert needle in body, needle


def test_confidence_shows_every_gate_and_the_first_it_fails(conn, signed_in):
    assert "Nothing scored yet" in signed_in[1].get("/confidence").text
    gates = [{"gate": "bias", "value": "clean", "need": "clean", "pass": True},
             {"gate": "drift", "value": None, "need": "inside the interval", "pass": False}]
    conn.execute("INSERT INTO readiness (market, strategy, venue, score, ready, passed, trades, gates) "
                 "VALUES ('crypto', 'Alpha', 'binanceus', 71.4, false, 1, 42, %s)", (Jsonb(gates),))
    body = signed_in[1].get("/confidence").text
    assert "not ready: drift" in body and "1 of 2 gates" in body and "inside the interval" in body


def test_a_page_holds_one_pooled_connection_and_gives_it_back(conn, signed_in, monkeypatch):
    seed(conn)
    app_mod, client = signed_in
    pool, taken, given = app_mod.pool(), [], []
    monkeypatch.setattr(pool, "getconn", lambda real=pool.getconn: taken.append(1) or real())
    monkeypatch.setattr(pool, "putconn", lambda c, real=pool.putconn: given.append(1) or real(c))
    assert client.get("/strategies/Strategy001").status_code == 200
    assert len(taken) == len(given) == 1


def test_cache_serves_until_a_run_finishes(conn, signed_in, monkeypatch):
    app_mod, client = signed_in
    monkeypatch.setattr(app_mod, "CACHE_TTL", 60.0)
    assert "Nothing scored yet" in client.get("/confidence").text
    gates = [{"gate": "bias", "value": "clean", "need": "clean", "pass": True}]
    conn.execute("INSERT INTO readiness (market, strategy, venue, score, ready, passed, trades, gates) "
                 "VALUES ('crypto', 'Alpha', 'binanceus', 71.4, true, 1, 42, %s)", (Jsonb(gates),))
    assert "Nothing scored yet" in client.get("/confidence").text
    conn.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES ('analysis', 'a', '{}', now(), now(), true)")
    assert "Nothing scored yet" not in client.get("/confidence").text


def test_book_shows_no_number_until_a_strategy_is_scored(conn, signed_in):
    body = signed_in[1].get("/book").text
    assert "Not enough evidence" in body and "0 of 20" in body and "Stage 1" in body
    conn.execute("INSERT INTO readiness (market, strategy, venue, score, ready, passed, trades, gates) "
                 "VALUES ('crypto', 'Alpha', 'binanceus', 71.4, false, 1, 42, %s)", (Jsonb([{"gate": "bias", "pass": True}],),))
    assert "not enough months" in signed_in[1].get("/book").text


def test_until_scored_both_pages_show_how_far_the_population_is(conn, signed_in):
    gates = Jsonb([{"gate": "bias", "pass": True}, {"gate": "score", "pass": False}])
    for name, trades, mean in (("Alpha", 42, 0.0123), ("Beta", 12, -0.004)):
        conn.execute("INSERT INTO readiness (market, strategy, venue, score, ready, passed, trades, expectancy, gates) "
                     "VALUES ('crypto', %s, 'okx', NULL, false, 1, %s, %s, %s)", (name, trades, mean, gates))
    for path in ("/book", "/confidence"):
        body = signed_in[1].get(path).text
        assert "<strong>1 of 20</strong>" in body and "Unscored" in body, path
        assert "1.23%" in body and "-0.40%" in body and "no, 18 short" in body, path
        assert body.index("Alpha") < body.index("Beta"), path
    # Negative control: once anything is scored, the scored board replaces the panel.
    conn.execute("UPDATE readiness SET score = 71.4 WHERE strategy = 'Alpha'")
    for path in ("/book", "/confidence"):
        assert "How close the score is" not in signed_in[1].get(path).text, path


def test_guide_lists_every_page_in_reading_order(conn, signed_in):
    body = signed_in[1].get("/guide").text
    order = [body.index(f'<li><a href="{p}">') for p in ("/", "/cohorts", "/data", "/strategies", "/walkforward", "/suggestions",
                                                          "/confidence", "/book", "/coverage", "/markets", "/accounts", "/runs")]
    assert order == sorted(order) and "needs 20 strategies with at least 30" in body and "trial register" in body


def test_book_sets_a_strategy_beside_hold(conn, signed_in):
    conn.execute("INSERT INTO readiness (market, strategy, venue, score, ready, passed, trades, gates) "
                 "VALUES ('crypto', 'Alpha', 'binanceus', 71.4, false, 1, 42, %s)", (Jsonb([{"gate": "bias", "pass": True}],),))
    for basis, mid in (("strategy", 0.31), ("hold", 0.12)):
        for months in (12, 60):
            conn.execute("INSERT INTO book (strategy, exchange, basis, horizon_months, p5, p50, p95, prob_loss, "
                         "exp_max_drawdown, paths, basis_months) VALUES ('Alpha', 'binanceus', %s, %s, -0.2, %s, 0.9, "
                         "0.25, 0.3, 5000, 12)", (basis, months, mid))
    body = signed_in[1].get("/book").text
    assert "Alpha" in body and "31%" in body and "12%" in body and "Not enough evidence" not in body
    for needle in ("24% / 9%", "T-bills at 4.4%", "USDT", "No mixture yet"):
        assert needle in body, needle
    conn.execute("INSERT INTO mixtures (variant, name, spec, weighting, gated, baseline, sleeves, months, days, annual, lo, hi, "
                 "sharpe, max_drawdown, dsr, trials, pbo, weights) VALUES ('v1', '1/N: Alpha binanceus, Beta okx', '{}', "
                 "'equal', false, true, 2, 4, 120, 0.2, -0.1, 0.5, 1.1, 0.07, 0.31, 96, 0.42, %s)",
                 (Jsonb({"Alpha binanceus": 0.5, "Beta okx": 0.5}),))
    body = signed_in[1].get("/book").text.split("Running bots")[1]
    assert "(baseline)" in body and "Alpha binanceus 50%" in body and "96 in the register" in body and "0.42" in body


def test_book_flags_venues_whose_pairs_are_today_survivors(conn, signed_in):
    body = signed_in[1].get("/book").text
    assert "Survivors only" not in body
    universe = {"okx": {"source": "current-listing", "recorded": True, "pairs": 22, "as_of": "2026-11-07",
                        "listed_by": "2022-01-01", "min_volume": 2e6},
                "binanceus": {"source": "current-listing", "recorded": False, "pairs": 10},
                "kucoin": {"source": "point-in-time", "recorded": True, "pairs": 18, "as_of": "2026-11-07",
                           "listed_by": "2022-01-01", "min_volume": 2e6}}
    conn.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES ('analysis', 'a', %s, now(), now(), true)",
                 (Jsonb({"universe": universe}),))
    body = signed_in[1].get("/book").text
    assert "Survivors only" in body and "okx: 22 pairs, volume floor $2M a day, picked 2026-11-07" in body
    assert "binanceus: no pick record, 10 pairs" in body and "kucoin:" not in body


def test_book_shows_whether_har_beats_the_plain_forecasts(conn, signed_in):
    assert "No forecast scores yet" in signed_in[1].get("/book").text
    conn.execute("INSERT INTO vol_verdict VALUES ('binanceus', 'mean', 12, 900, 0.21, 0.27, -0.08, -0.03, true), "
                 "('binanceus', 'ewma', 12, 900, 0.21, 0.22, -0.04, 0.01, false)")
    body = signed_in[1].get("/book").text
    for needle in ("Forecasting volatility", "30-day mean", "EWMA", "-0.080 to -0.030", "0.270"):
        assert needle in body, needle


def test_chart_draws_only_with_two_points():
    from web import charts

    assert "Not enough points" in charts.line([])
    svg = charts.line([(NOW - dt.timedelta(hours=1), -1.0), (NOW, 2.0)], label="x")
    assert svg.startswith("<svg") and "series up" in svg and json.dumps("x")[1:-1] in svg


def test_walkforward_pools_the_months_with_intervals_and_efficiency(conn, signed_in):
    seed(conn)
    for month, base, tuned, err in ((9, 0.01, 0.03, None), (10, 0.02, 0.01, None), (8, None, None, "hyperopt exit 2: boom")):
        start = dt.date(2026, month, 1)
        ok = err is None
        conn.execute("INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok, attempts, "
                     "error, epochs, loss, params, is_trades, is_profit, base_trades, base_profit, base_returns, tuned_trades, "
                     "tuned_profit, tuned_returns) VALUES ('Strategy001', 'binanceus', %s, %s, %s, 'k', %s, 1, %s, 150, 'L', "
                     "%s, 60, 0.18, 4, %s, %s, 4, %s, %s)",
                     (start, start - dt.timedelta(days=180), (start + dt.timedelta(days=31)).replace(day=1), ok, err,
                      json.dumps({"roi": {"0": 0.02}}) if ok else None, base, [base / 4] * 4 if ok else None, tuned,
                      [tuned / 4 + d for d in (0.001, -0.001, 0.001, -0.001)] if ok else None))
    conn.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES ('walkforward', 'wf-1', %s, now(), "
                 "now(), true)", (json.dumps({"folds_run": 3, "folds_cached": 0, "folds_failed": 1}),))
    text = signed_in[1].get("/walkforward").text
    for needle in ("/strategies/Strategy001?exchange=binanceus#walk-forward", "1 of 2", "(1 failed)", "0.50%",
                   "3 folds run, 0 cached, 1 failed", ">8<", "0.66"):
        assert needle in text, needle
    page = signed_in[1].get("/strategies/Strategy001").text
    for needle in ('id="walk-forward"', "2026-09", "2026-03-05 to 2026-09-01", "hyperopt exit 2: boom", "beat as is in 1 of 2"):
        assert needle in page, needle
    assert "Not a walk-forward candidate yet" in signed_in[1].get("/strategies/Planted").text
    assert "2 walk-forward months" in page


def test_the_card_lists_climates_that_clear_zero_in_order_and_the_gates_missed():
    from web import app as app_mod
    w = {"win": "365d", "cagr": 0.2, "sharpe_lo": 0.1, "sharpe_hi": 1.0, "expectancy": 0.01, "expectancy_lo": 0.0,
         "expectancy_hi": 0.02, "winrate": 0.55, "trades": 120, "days": 365, "median_hold_hours": 5.0}
    climates = [{"cell": "down/high", "lo": 0.2}, {"cell": "up/low", "lo": 0.1}, {"cell": "up/low", "lo": -0.1},
                {"cell": "flat/mid", "lo": None}]
    gates = [{"gate": "bias", "pass": True}, {"gate": "costs", "pass": False}]
    c = app_mod.card([w], [{"passed": 1, "ready": False, "gates": gates}], climates, [], 0.5)
    assert c["climates"] == ["up/low", "down/high"]
    assert c["payoff"] is None and c["haircut"] is None  # a window from before avg_win and avg_loss were stored
    assert c["missed"] == ["costs"] and c["gates"] == 2
    assert round(c["per_month"], 1) == 10.0 and c["oos_months"] == 0
    empty = app_mod.card([], [], [], [], 0.5)
    assert empty["window"] is None and empty["gates"] is None and not empty["climates_tested"]


def test_the_card_payoff_ratio_and_its_haircut_can_turn_a_winner_into_a_loser():
    from web import app as app_mod
    w = {"win": "365d", "cagr": 0.1, "sharpe_lo": 0.1, "sharpe_hi": 1.0, "expectancy": 0.006, "expectancy_lo": 0.0,
         "expectancy_hi": 0.01, "winrate": 0.4, "avg_win": 0.03, "avg_loss": 0.01, "trades": 100, "days": 365}
    c = app_mod.card([w], [], [], [], 0.5)
    assert round(c["payoff"], 6) == 3.0
    # 0.4 x 1.5% - 0.6 x 1% = 0: half-size wins leave nothing; full size makes 0.6% a trade.
    assert abs(c["haircut"]) < 1e-12 and round(0.4 * 0.03 - 0.6 * 0.01, 6) == 0.006
    assert app_mod.card([{**w, "avg_win": 0.02}], [], [], [], 0.5)["haircut"] < 0
    no_loss = app_mod.card([{**w, "winrate": 1.0, "avg_loss": None}], [], [], [], 0.5)
    assert no_loss["payoff"] is None and round(no_loss["haircut"], 6) == 0.015


def families():
    from web import app as app_mod

    return app_mod.suggest.load_config(app_mod.FAMILIES)


def test_every_strategy_class_has_a_family():
    from web import app as app_mod

    toml, on_disk = families(), app_mod.strategy_classes()
    assert {"Strategy001", "ADXMomentum", "DevilStra"} <= on_disk and "StrategyHelper" not in on_disk
    research = {c for f in (app_mod.HERE.parent / "lab/futures/research").glob("*.py") for c in app_mod.STRATEGY_CLASS.findall(f.read_text())}
    assert set(toml["strategies"]) == on_disk | set(research), "add the class to lab/crypto/families.toml, or drop a stale line"
    assert {t["family"] for t in toml["strategies"].values()} <= set(toml["families"])
    assert all(t["inputs"] for t in toml["strategies"].values())


def test_coverage_joins_the_families_to_the_results(conn, signed_in):
    seed(conn)
    conn.execute("TRUNCATE trials")  # family totals must exclude migration seeds and earlier tests' trials
    conn.execute("INSERT INTO strategies (name, file, file_hash, timeframe) VALUES ('Mystery', 'x.py', 'h4', '5m')")
    for name, exchange, trades in (("Strategy001", "binanceus", 120), ("Strategy001", "kraken", 30), ("Mystery", "binanceus", 7)):
        conn.execute("INSERT INTO strategy_windows (strategy, exchange, win, days, trades) VALUES (%s, %s, 'all', 900, %s)",
                     (name, exchange, trades))
    for month, ok in ((8, False), (9, True), (10, True)):
        start = dt.date(2026, month, 1)
        conn.execute("INSERT INTO walkforward (strategy, exchange, fold_start, train_start, test_end, cache_key, ok) "
                     "VALUES ('Strategy001', 'binanceus', %s, %s, %s, 'k', %s)", (start, start - dt.timedelta(days=180), start, ok))
    conn.execute("INSERT INTO trials (strategy, exchange, variant) VALUES ('Strategy001', 'binanceus', 'as-is'), "
                 "('Strategy001', 'binanceus', 'p1'), ('Strategy001', 'kraken', 'as-is'), ('Mystery', 'binanceus', 'as-is')")
    gates = [{"gate": g, "pass": True} for g in "abcde"]
    for exchange, passed, oos in (("binanceus", 3, 42), ("kraken", 4, 8)):
        conn.execute("INSERT INTO readiness (market, strategy, venue, score, ready, passed, trades, gates) "
                     "VALUES ('crypto', 'Strategy001', %s, NULL, false, %s, %s, %s)", (exchange, passed, oos, Jsonb(gates)))
    trend = sum(t["family"] == "trend" for t in families()["strategies"].values())
    body = signed_in[1].get("/coverage").text
    for needle in (f'1 of {trend}<br><span class="muted">2 months, 2 trials</span>',  # binanceus: a failed fold is no month
                   f'1 of {trend}<br><span class="muted">0 months, 1 trials</span>',  # kraken
                   f'1 of {trend}<br><span class="muted">2 months, 3 trials</span>',  # all venues
                   "binanceus, kraken", ">150<", ">50<", "4 of 5 on kraken", "Hyperopt picks which raw columns cross",
                   '<td title="In the results only, not on disk.">gone</td>'):
        assert needle in body, needle
    # Mystery has results but no file and no toml line: gone, not untagged, and listed after every family.
    assert re.search(r"<td>Mystery</td>\s*<td>gone</td>", body) and ">untagged<" not in body
    assert body.index("/strategies/Strategy001") < body.index(">Mystery<")


def test_coverage_on_an_empty_database_has_no_untagged_row(conn, signed_in):
    body = signed_in[1].get("/coverage").text
    assert ">untagged<" not in body and ">Diamond</a>" in body and ">BustShort<" in body


def test_a_strategy_class_missing_from_the_toml_shows_untagged(conn, signed_in, tmp_path, monkeypatch):
    app_mod, client = signed_in
    (tmp_path / "Strategy001.py").write_text("class Strategy001(IStrategy):\n    pass\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "Ghost.py").write_text("class Ghost(IStrategy):\n    pass\n\n\nclass Helper:\n    pass\n")
    monkeypatch.setattr(app_mod, "STRATEGY_DIR", tmp_path)
    assert app_mod.strategy_classes() == {"Strategy001", "Ghost"}
    body = client.get("/coverage").text
    assert '<td class="down">untagged</td>' in body and ">Ghost</a>" in body and ">Helper<" not in body


def seed_markets(c):
    c.execute("INSERT INTO venues (venue, title, kind) VALUES ('kalshi', 'Kalshi', 'exchange'), "
              "('odds_api', 'The Odds API', 'sportsbook'), ('manifold', 'Manifold', 'play-money')")
    c.execute("INSERT INTO market_sweeps (venue, started_at, finished_at, seen, new_markets, changes, resolved, ok, error) VALUES "
              "('kalshi', now() - interval '40 minutes', now() - interval '37 minutes', 179162, 12, 10099, 41, true, NULL), "
              "('kalshi', now() - interval '5 minutes', now() - interval '5 minutes', 0, 0, 0, 0, false, 'HTTPError: 503 upstream'), "
              "('odds_api', now() - interval '4 minutes', now() - interval '3 minutes', 220, 0, 31, 2, true, NULL)")
    c.execute("INSERT INTO markets (venue, market_id, title, subtitle, market_type, status, close_time, first_seen, updated_at) VALUES "
              "('kalshi', 'KXNBAGAME-26NOV09SASPHX-SAS', 'San Antonio at Phoenix', 'San Antonio', 'binary', 'open', now() + interval '3 days', now(), now()), "
              "('odds_api', 'g1', 'Spurs at Suns', NULL, 'binary', 'open', now() + interval '3 days', now(), now())")
    c.execute("INSERT INTO market_links (venue_a, market_id_a, outcome_a, venue_b, market_id_b, outcome_b, source, status, confirmed_by) "
              "VALUES ('odds_api', 'g1', 'San Antonio Spurs', 'kalshi', 'KXNBAGAME-26NOV09SASPHX-SAS', 'yes', 'rule:kalshi_game', 'confirmed', 'rule')")
    c.execute("INSERT INTO market_link_asks (venue_a, market_id_a, venue_b, model, proposed) VALUES ('kalshi', 'KXNBAGAME-26NOV09SASPHX-SAS', 'odds_api', 'claude-haiku', 0)")
    c.execute("INSERT INTO market_backfill (venue, job, unit, rows, done) VALUES ('kalshi', 'bars', '2026-01-02', 4321, true), "
              "('kalshi', 'bars', '2026-01-03', 0, false)")
    c.execute("INSERT INTO market_backtests (family, venue, variant, params, start_at, end_at, orders, fills, trades, open_fills, metrics, "
              "returns, expectancy, expectancy_lo, expectancy_hi, dsr, trials) VALUES ('longshot', 'kalshi', 'abcdef0123456789', %s, "
              "'2025-01-01', '2026-01-01', 900, 812, 800, 12, %s, '{0.1,-0.05}', 0.0123, -0.004, 0.031, 0.42, 16)",
              (Jsonb({"edge": 0.06}), Jsonb({"winrate": 0.61})))
    c.execute("INSERT INTO market_category_results (family, venue, variant, start_at, category, trades, mean, lo, hi) "
              "VALUES ('longshot', 'kalshi', 'abcdef0123456789', '2025-01-01', 'sports', 450, 0.0211, 0.0043, 0.0377)")
    c.execute("INSERT INTO readiness (market, strategy, family, venue, variant, params, windows, trades, expectancy, expectancy_lo, expectancy_hi, "
              "dsr, pbo, trials, ready, passed, gates) VALUES ('events', 'longshot/abcdef0123456789', 'longshot', 'kalshi', 'abcdef0123456789', %s, 3, 800, 0.0123, -0.004, 0.031, "
              "0.42, 0.2, 16, false, 1, %s)",
              (Jsonb({"edge": 0.06}), Jsonb([{"gate": "out-of-sample trades", "value": 800, "need": ">= 100", "pass": True},
                                            {"gate": "expectancy interval low", "value": -0.004, "need": "> 0 at 90%", "pass": False}])))
    c.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at, equity, realized, open_positions, marked_at, "
              "gap_lo, gap_hi, drifted) VALUES ('paper-longshot-abcdef01', 'longshot', 'kalshi', 'abcdef0123456789', %s, 1000, now(), 987.5, "
              "-4.25, 3, now(), -0.081, -0.012, true)",
              (Jsonb({"edge": 0.06}),))
    c.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, fee) VALUES "
              "('paper-longshot-abcdef01', 'KXNBAGAME-26NOV09SASPHX-SAS', 'no', 10, now(), now(), 11, 0.91, 0.07)")
    c.execute("INSERT INTO watch (bot, checked_at, seen_at, state, stopped_at) VALUES ('paper-longshot-abcdef01', now(), now(), 'stopped', now())")
    c.execute("INSERT INTO demo_orders (venue, ticker, client_id, price, count, placed_at, status, canceled_at) VALUES "
              "('kalshi', 'KXDEMO-1', 'c1', 0.01, 1, now(), 'canceled', now())")
    c.execute("INSERT INTO broker_balances (broker, read_at, paper, status, currency, equity, cash, buying_power, positions) VALUES "
              "('alpaca_paper', now(), true, 'ACTIVE', 'USD', 100012.5, 99000, 199000, 2), "
              "('fidelity_individual_1234', now(), false, 'open', 'USD', 15363.23, NULL, NULL, NULL)")


def test_markets_pages_render_with_data(conn, signed_in):
    seed_markets(conn)
    client = signed_in[1]
    pages = {"/markets": ["Kalshi", "179,162", "10,099", "HTTPError: 503 upstream", '<td class="down">37m ago</td>',
                          "The Odds API", "1 of 2", "2026-01-02 to 2026-01-03", "4,321", "never swept"],
             "/links": ["rule:kalshi_game", "San Antonio at Phoenix", "Spurs at Suns", "by rule", "claude-haiku"],
             "/links?status=rejected": ["No rejected links."],
             "/markets/book": ["longshot", "abcdef01", "1.23%", "-0.40% to 3.10%", "61%", "0.42", "By contract category", "2.11%", "0.43% to 3.77%", 
                               "Readiness gate", "0 of 1 variants are ready", "not ready: expectancy interval low", "1 of 2 gates, 3 windows",
                               "KXDEMO-1", "canceled"],
             "/leagues": ["paper-longshot-abcdef01", "987.50", "-1.25%", "stopped", "-8.10% to -1.20% drifted", "15%"]}
    for path, needles in pages.items():
        r = client.get(path)
        assert r.status_code == 200, path
        for n in needles:
            assert n in r.text, (path, n)
    # the healthy venue is not marked stale, and a link filter outside the statuses is a 404
    assert '<td class="">3m ago</td>' in client.get("/markets").text
    assert "Spurs at Suns" not in client.get("/links?status=rejected").text
    assert client.get("/links?status=bogus").status_code == 404


def test_accounts_shows_every_balance_and_kill_switch_read_only(conn, signed_in):
    seed_markets(conn)
    conn.execute("INSERT INTO wallet_balances (account, read_at, coin, confirmed_sat, unconfirmed_sat, used_addresses) VALUES "
                 "('trezor-savings', now() - interval '1 hour', 'btc', 1, 0, 1), ('trezor-savings', now(), 'btc', 12345678, -2000, 7)")
    conn.execute("INSERT INTO watch (bot, checked_at, seen_at, dry_run, state, equity, day_open, breached_at) VALUES "
                 "('kraken-s001', now(), now(), true, 'running', 980.5, 1000, now())")
    client = signed_in[1]
    body = client.get("/accounts").text
    for n in ("Real-money guard: <span class=\"up\">on</span>", "alpaca_paper", "100,012.50", "fidelity_individual_1234",
              "real, read only", "15,363.23", "trezor-savings", "0.12345678", "-0.00002000", "kraken-s001", "980.50",
              "paper-longshot-abcdef01"):
        assert n in body, n
    assert "0.00000001" not in body  # only the newest wallet read
    assert body.index("paper-longshot-abcdef01") < body.index("kraken-s001")  # stopped before breached
    assert "alpaca_paper" not in client.get("/markets/book").text  # moved, not copied
    assert body.count("<form") == 2 and 'action="/logout"' in body
    assert 'id="setup-form"' in body  # research choices persist in the browser; no account-changing endpoint


def test_book_lists_the_strategies_clearing_zero_in_each_climate(conn, signed_in):
    body = signed_in[1].get("/book").text
    assert "No climate table yet" in body
    conn.execute("INSERT INTO climate_scorecard (kind, name, exchange, variant, cell, days, trades, annual, lo, hi, trials) VALUES "
                 "('benchmark', 'hold BTC', '', '', 'up/low', 120, 0, 0.80, 0.30, 1.40, 94), "
                 "('strategy', 'Alpha', 'binanceus', 'tuned', 'up/low', 90, 40, 0.50, 0.12, 0.90, 94), "
                 "('strategy', 'Beta', 'binanceus', 'tuned', 'up/low', 90, 40, 0.10, -0.20, 0.40, 94), "
                 "('strategy', 'Beta', 'binanceus', 'tuned', 'down/high', 10, 3, 0.10, NULL, NULL, 94)")
    body = signed_in[1].get("/book").text
    assert "By market climate" in body and "up/low" in body and "12%" in body and "94 trials" in body
    table = body.split("By market climate")[1].split("Running bots")[0]
    assert "Beta" not in table and "Alpha" in table and "<td>down/high</td>" in table


def test_book_sets_each_strategy_against_holding(conn, signed_in):
    assert "No strategy comparison yet" in signed_in[1].get("/book").text
    conn.execute("INSERT INTO horizon_verdict (strategy, exchange, variant, plan, days, strat_annual, plan_annual, diff_lo, diff_hi, verdict) VALUES "
                 "('Alpha', 'binanceus', 'tuned', 'hold', 200, 0.50, 0.20, 0.10, 0.50, 'beats'), "
                 "('Alpha', 'binanceus', 'tuned', 'trend', 200, 0.50, 0.30, -0.10, 0.50, 'open')")
    body = signed_in[1].get("/book").text
    assert "Each strategy against holding" in body and "1 of 2 comparisons beat a passive plan" in body
    assert "200-day filter" in body and ">beats<" in body


def test_desk_shows_lot_size_best_of_day_and_weather_bets(conn, signed_in):
    day = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    seed(conn)
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at, retired_at) VALUES "
                 "('paper-weather-bbbb2222', 'weather', 'kalshi', 'bbbb2222', '{}', 1000, now(), NULL)")
    for market, price, profit in (("W1", 0.95, 0.5), ("W2", 0.4, -5.0)):
        conn.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, fee, "
                     "settled_at, payout, profit) VALUES ('paper-weather-bbbb2222', %s, 'yes', 10, %s, %s, 10, %s, 0, %s, 1, %s)",
                     (market, day, day, price, day, profit))
    _, client = signed_in
    page = client.get("/desk").text
    assert "Lot size" in page and "Best dry run of each day" in page
    assert page.count("<strong>extreme</strong>") == 1 and page.index("0.95") < page.index("0.40")


def test_nav_groups_render_in_order(signed_in):
    _, client = signed_in
    html = client.get("/").text
    heads = re.findall(r'<div class="kb-group">(\w+)</div>', html)
    assert heads == ["Now", "Evidence", "Pipeline"]
    assert 'class="kb-nav"' in html and '<kb-shell>' in html and "/static/kit/kb-shell.js" in html
    assert "/static/kit/ui.css" in html and "/static/kit/kb-ui.js" in html


@pytest.mark.parametrize("path,tab", [("/", "Overview"), ("/bots/kraken-s001", "Overview"), ("/strategies/Strategy001", "Strategies"),
                                      ("/runs/1", "Runs"), ("/links", "Markets"), ("/walkforward", "Walk-forward")])
def test_nav_marks_the_current_tab(path, tab):
    from web import nav

    current = [t["title"] for g in nav.groups(path) for t in g["tabs"] if t["current"]]
    assert current == [tab]


def test_every_tab_path_is_a_route(site):
    app_mod, _ = site
    from web import nav

    routes = {r.path for r in app_mod.app.routes}
    assert all(t[1] in routes for _, tabs in nav.TABS for t in tabs)


def test_kit_files_match_the_lock():
    import hashlib
    from pathlib import Path

    web = Path(__file__).parents[2] / "web"
    lock = json.loads((web / "kit.lock").read_text())
    for name, want in lock["files"].items():
        f = web / "static" / "kit" / name
        assert f.exists(), f"{name} missing: run python web/fetch_kit.py"
        assert hashlib.sha256(f.read_bytes()).hexdigest() == want


def test_fetch_kit_refuses_a_wrong_checksum(tmp_path):
    from web import fetch_kit

    lock = {"repo": "r", "commit": "c", "path": "p", "files": {"a.css": "0" * 64}}
    with pytest.raises(SystemExit, match="does not match"):
        fetch_kit.fetch(lock, tmp_path, get=lambda url: b"body")
    assert not (tmp_path / "a.css").exists()


def test_fetch_kit_retries_a_transient_server_error(tmp_path):
    import hashlib
    import urllib.error

    from web import fetch_kit

    body = b"body"
    lock = {"repo": "r", "commit": "c", "path": "p", "files": {"a.css": hashlib.sha256(body).hexdigest()}}
    calls, waits = [], []

    def flaky(url):
        calls.append(url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(url, 503, "Backend.max_conn reached", {}, None)
        return body

    fetch_kit.fetch(lock, tmp_path, get=flaky, sleep=waits.append)
    assert len(calls) == 3 and len(waits) == 2
    assert (tmp_path / "a.css").read_bytes() == body

    def down(url):
        raise urllib.error.HTTPError(url, 503, "x", {}, None)

    with pytest.raises(urllib.error.HTTPError):
        fetch_kit.fetch(lock, tmp_path, get=down, sleep=lambda s: None)


def test_account_setup_catalog_uses_typed_refs_and_latest_observations(conn, signed_in):
    seed(conn)
    seed_markets(conn)
    conn.execute("INSERT INTO wallet_balances (account, read_at, coin, confirmed_sat) VALUES "
                 "('kraken-s001', now(), 'btc', 42)")
    conn.execute("UPDATE bot_snapshots SET balance_total = 2345.67 "
                 "WHERE ts = (SELECT max(ts) FROM bot_snapshots WHERE bot = 'kraken-s001')")
    app_mod, client = signed_in
    response = client.get('/accounts')
    assert response.status_code == 200
    data = json.loads(re.search(r'<script[^>]*id="research-data"[^>]*>(.*?)</script>', response.text, re.S).group(1))
    refs = {a['id'] for a in data['accounts']}
    assert {'bot:kraken-s001', 'wallet:btc:kraken-s001', 'broker:alpaca_paper',
            'paper:paper-longshot-abcdef01'} <= refs
    assert all(set(a) == {'id', 'label'} for a in data['accounts'])
    strategy = next(s for s in data['strategies'] if s['name'] == 'Strategy001')
    assert strategy['family'] == 'trend' and strategy['venues'] == ['binanceus', 'kraken']
    assert strategy['description'] == app_mod.strategy_method('Strategy001')['description']
    assert '2,345.67' in response.text and 'Unknown reported units' in response.text


def test_account_evidence_is_exact_pair_and_explicit_window(conn, signed_in):
    seed(conn)
    conn.execute("INSERT INTO strategy_windows (strategy, exchange, win, days, trades, total_return) "
                 "VALUES ('Strategy001', 'kraken', '365d', 12, 2, -0.8), "
                 "('Strategy001', 'binanceus', 'all', 900, 100, 9)")
    conn.execute("INSERT INTO readiness (market, strategy, venue, ready, passed, trades, gates) "
                 "VALUES ('crypto', 'Strategy001', 'binanceus', false, 1, 40, '[]')")
    client = signed_in[1]
    found = client.get('/accounts/evidence', params={'strategy': 'Strategy001', 'exchange': 'binanceus'}).json()
    assert found['available'] and found['window'] == '365d'
    assert found['total_return'] == 0.032 and found['trades'] == 40
    assert found['max_drawdown'] == 0.04 and found['start_day'] == '2025-11-04'
    assert found['bias'] == 'clean' and found['confidence'] == 'not ready'
    other = client.get('/accounts/evidence', params={'strategy': 'Strategy001', 'exchange': 'kraken'}).json()
    assert other['total_return'] == -0.8 and other['confidence'] == 'not checked'
    assert client.post('/accounts/evidence').status_code == 405


def test_account_evidence_missing_results_are_not_zero(conn, signed_in):
    seed(conn)
    client = signed_in[1]
    found = client.get('/accounts/evidence', params={'strategy': 'Strategy001', 'exchange': 'kraken'}).json()
    assert found['available'] is False
    assert found['total_return'] is None and found['trades'] is None and found['max_drawdown'] is None
    biased = client.get('/accounts/evidence', params={'strategy': 'Planted', 'exchange': 'binanceus'}).json()
    assert biased['bias'] == 'biased' and biased['family'] == 'Not classified'


@pytest.mark.parametrize('strategy,exchange', [('Unknown', 'binanceus'), ('Strategy001', 'bogus'),
                                               ("Strategy001' OR 1=1 --", 'kraken')])
def test_account_evidence_rejects_unknown_choices(conn, signed_in, strategy, exchange):
    seed(conn)
    assert signed_in[1].get('/accounts/evidence', params={'strategy': strategy, 'exchange': exchange}).status_code == 404


def test_account_evidence_requires_sign_in(site):
    r = site[1].get('/accounts/evidence?strategy=Strategy001&exchange=kraken')
    assert r.status_code == 303 and r.headers['location'] == '/login'


def test_strategy_method_and_setup_metadata_are_escaped(conn, signed_in, tmp_path, monkeypatch):
    seed(conn)
    app_mod, client = signed_in
    cfg = tmp_path / 'families.toml'
    cfg.write_text('[families]\ntrend = "<script>bad()</script>"\n[strategies]\n'
                   'Strategy001 = {family="trend", inputs=["ohlcv"], note="<img src=x onerror=bad()>"}\n')
    monkeypatch.setattr(app_mod, 'FAMILIES', cfg)
    body = client.get('/strategies/Strategy001').text
    assert '&lt;script&gt;bad()&lt;/script&gt;' in body and '<script>bad()' not in body
    body = client.get('/accounts').text
    assert '<img src=x onerror=bad()>' not in body
    assert r'\u003cscript\u003e' in body


def test_account_setup_catalog_empty_and_stale_states(conn, signed_in):
    client = signed_in[1]
    assert client.get('/accounts').status_code == 200
    seed(conn)
    conn.execute("UPDATE bot_snapshots SET ts = ts - interval '3 hours'")
    body = client.get('/accounts').text
    assert 'stale' in body.lower()
    assert 'Strategy001' in body


def test_account_balances_preserve_reported_stake_units(conn, signed_in):
    seed(conn)
    conn.execute("UPDATE bot_snapshots SET raw = %s", (Jsonb({'balance': {'stake': 'USDT'}}),))
    body = signed_in[1].get('/accounts').text
    assert 'USDT' in body
    assert 'Equity (USD)' not in body.split('<h2 class="sec">Prediction-market paper accounts</h2>')[0]


def test_account_evidence_distinguishes_recursive_instability(conn, signed_in):
    seed(conn)
    conn.execute("UPDATE bias_checks SET has_bias = true WHERE strategy = 'Strategy001' AND kind = 'recursive'")
    data = signed_in[1].get('/accounts/evidence?strategy=Strategy001&exchange=binanceus').json()
    assert data['bias'] == 'clean' and data['stability'] == 'unstable'


def test_accounts_ranks_shadow_accounts_and_says_how_many_each_cohort_holds(conn, signed_in):
    client = signed_in[1]
    assert "No replay account yet" in client.get("/accounts").text
    conn.execute("INSERT INTO shadow_accounts (account, strategy, exchange, cohort, capital, promoted_on, entry, state, "
                 "retired_on, retired_why) VALUES "
                 "('shadow-A-binanceus', 'A', 'binanceus', 'short', 1000, '2026-10-01', '{}', 'trial', NULL, NULL), "
                 "('shadow-W-kraken', 'W', 'kraken', 'long', 1000, '2026-08-01', '{}', 'winning', NULL, NULL), "
                 "('shadow-R-binanceus', 'R', 'binanceus', 'short', 1000, '2026-09-01', '{}', 'retired', '2026-10-01', "
                 "'drawdown 25.0% past 20%')")
    conn.execute("INSERT INTO shadow_marks (account, day, ret, equity) VALUES "
                 "('shadow-A-binanceus', '2026-10-02', 0.10, 1100), ('shadow-A-binanceus', '2026-10-03', -0.05, 1045), "
                 "('shadow-W-kraken', '2026-08-02', 0.30, 1300)")
    body = client.get("/accounts").text
    assert "Active short 1, active long 1, winning 1, retired 1." in body
    table = body.split('id="shadow"')[1].split("Event-contract league accounts")[0]
    assert table.index("shadow-W-kraken") < table.index("shadow-A-binanceus") < table.index("shadow-R-binanceus")
    assert "+30.0%" in table and "+4.5%" in table and "5.0%" in table and "drawdown 25.0% past 20%" in table


def test_every_table_sits_in_a_scroll_box():
    """Cells never wrap (site.css), so a bare table widens the whole page past the viewport (tr-242: /accounts at 2325 px on a 1280 px window)."""
    import pathlib

    bare = []
    for p in sorted((pathlib.Path(__file__).parents[2] / "web" / "templates").glob("*.html")):
        s = p.read_text()
        bare += [f"{p.name}:{s.count(chr(10), 0, m.start()) + 1}" for m in re.finditer(r"<table\b", s)
                 if not re.search(r'<div class="scroll">\s*$', s[:m.start()])]
    assert bare == []


def test_signal_filter_keeps_coverage_categories_separate(conn, signed_in):
    client = signed_in[1]
    price = client.get("/coverage?signal_source=price").text
    structural = client.get("/coverage?signal_source=structural").text
    assert ">Strategy001</a>" in price and ">HourBasedStrategy</a>" not in price
    assert ">HourBasedStrategy</a>" in structural and ">Strategy001</a>" not in structural
    assert "Signal source" in structural and "strategy-exit" in structural
    assert '<h2 class="sec">Residual clusters</h2>' not in structural
    assert "Clear the signal source filter" in structural
    for path in ("/coverage", "/markets/book"):
        assert client.get(path + "?signal_source=unknown").status_code == 404


def test_avenues_page_uses_the_canonical_register(conn, signed_in):
    from pathlib import Path

    body = signed_in[1].get("/avenues").text
    text = (Path(__file__).parents[2] / "docs/avenues.md").read_text()
    names = [line.split("|")[1].strip() for line in text.splitlines() if line.startswith("| ")][1:]
    assert names and all(name in body for name in names)
    assert 'href="https://github.com/Kblack0610/freqtrade_trading_attempt/blob/master/docs/strategy-coverage.md#strategy-families"' in body
    assert "[Strategy coverage](" not in body


def test_signal_filter_applies_to_market_results(conn, signed_in):
    seed_markets(conn)
    client = signed_in[1]
    structural = client.get("/markets/book?signal_source=structural").text
    price = client.get("/markets/book?signal_source=price").text
    assert "1.23%" in structural
    assert "event-contract" in structural and "resolution" in structural
    assert "1.23%" not in price
    assert "No markets backtest has run yet" in price


def paper_fill(c, traced: bool) -> int:
    c.execute("INSERT INTO venues (venue, title, kind) VALUES ('kalshi', 'Kalshi', 'exchange') ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) VALUES "
              "('paper-longshot-aaaa1111', 'longshot', 'kalshi', 'aaaa1111', '{}', 1000, now()) ON CONFLICT DO NOTHING")
    c.execute("INSERT INTO markets (venue, market_id, title, market_type, status, close_time, first_seen, updated_at) "
              "VALUES ('kalshi', 'MIA-70', 'Miami above 69.99?', 'binary', 'open', %s, now(), now()), "
              "('kalshi', 'MIA-68', 'Miami above 67.99?', 'binary', 'open', %s, now(), now()) ON CONFLICT DO NOTHING",
              (NOW + dt.timedelta(hours=3), NOW + dt.timedelta(hours=3)))
    market = "MIA-70" if traced else "MIA-68"
    oid = c.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, bid, ask, contracts, "
                    "price, fee) VALUES ('paper-longshot-aaaa1111', %s, 'yes', 10, %s, %s, 0.96, 0.98, 10, 0.97, 0.03) "
                    "RETURNING id", (market, NOW, NOW)).fetchone()[0]
    if traced:
        checks = [{"name": "spread", "value": 0.02, "threshold": 0.05, "pass": True},
                  {"name": "mid in band", "value": 0.97, "threshold": [0.9, 0.99], "pass": True}]
        inputs = [{"name": "ask depth", "value": 212, "source": "kalshi recorder", "as_of": NOW.isoformat()}]
        sizing = {"side": "yes", "stake": 10, "limit": None, "share": 0.5, "taken": 0, "depth": 212, "cash": 412.3,
                  "contracts": 10, "price": 0.97, "fee": 0.03, "fees": {"rate": 0.07}}
        rule = "longshot: buy yes when the yes mid is 0.90-0.99, within 24 h of close"
        for action, instrument, ch, fill in (("enter", "MIA-70", checks, oid),
                                             ("near_miss", "MIA-68", [dict(checks[0], value=0.07, **{"pass": False})], None)):
            c.execute("INSERT INTO decisions (league, account, venue, instrument, decided_at, action, rule, inputs, "
                      "checks, sizing, fill_source, fill_id) VALUES ('event_contracts', 'paper-longshot-aaaa1111', "
                      "'kalshi', %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                      (instrument, NOW, action, rule, Jsonb(inputs), Jsonb(ch), Jsonb(sizing) if fill else None,
                       fill and "paper", fill))
        c.execute("INSERT INTO decision_funnel VALUES ('event_contracts', 'paper-longshot-aaaa1111', 'kalshi', %s, "
                  "'spread', 1840), ('event_contracts', 'paper-longshot-aaaa1111', 'kalshi', %s, 'ordered', 1)", (NOW, NOW))
    return oid


def test_a_paper_fill_opens_its_decision(conn, signed_in):
    oid = paper_fill(conn, traced=True)
    _, client = signed_in
    body = client.get(f"/trades/paper/{oid}").text
    for needle in ("Miami above 69.99?", "yes 10 @ 0.97", "longshot: buy yes when the yes mid is 0.90-0.99",
                   "mid in band", "0.9 / 0.99", "ask depth", "kalshi recorder", "depth cap 0.5 x 212", "cash 412.30",
                   "fee 0.03", "rate 0.07", "spread 1,840", "1 near-miss: spread (1)", "Miami above 67.99?",
                   "profit if yes", "0.27", "-9.73"):
        assert needle in body, needle
    assert "Recorded before decisions were traced" not in body


def test_a_fill_from_before_the_record_says_so(conn, signed_in):
    oid = paper_fill(conn, traced=False)
    body = signed_in[1].get(f"/trades/paper/{oid}").text
    assert "Recorded before decisions were traced" in body and "Miami above 67.99?" in body


def test_a_crypto_trade_shows_its_markouts_and_reconcile(conn, signed_in):
    seed(conn)
    opened = conn.execute("SELECT open_date FROM bot_trades WHERE bot = 'kraken-s001'").fetchone()[0]
    conn.execute("INSERT INTO markouts (bot, trade_id, open_date, horizon_min, pair, exchange, side, fill_slip, ret, "
                 "btc_ret, beta, net, null_net, null_draws) VALUES ('kraken-s001', 1, %s, 60, 'BTC/USD', 'kraken', 1, "
                 "0.0004, 0.015, 0.01, 1.0, 0.0031, -0.0012, 200)", (opened,))
    conn.execute("INSERT INTO reconcile (bot, n, pair, kind, live_open, bt_open, delay_s, price_gap, fee_gap, profit_gap) "
                 "VALUES ('kraken-s001', 1, 'BTC/USD', 'matched', %s, %s, 42, 0.0002, 0, -0.0011)", (opened, opened))
    _, client = signed_in
    assert 'href="/trades/freqtrade/1?bot=kraken-s001"' in client.get("/bots/kraken-s001").text
    body = client.get("/trades/freqtrade/1?bot=kraken-s001").text
    for needle in ("BTC/USD", "60 min", "0.31%", "200 draws", "matched", "42 s", "-0.11%",
                   "Recorded before decisions were traced"):
        assert needle in body, needle
    assert client.get("/trades/freqtrade/1").status_code == 404  # a trade id is per bot
    assert client.get("/trades/freqtrade/1?bot=other").status_code == 404


def test_a_crypto_trade_shows_its_own_bots_entry_decision(conn, signed_in):
    """A trade id counts per bot: kraken-s001's trade 1 shows its decision, never another bot's trade 1."""
    from psycopg.types.json import Jsonb

    seed(conn)
    opened = conn.execute("SELECT open_date FROM bot_trades WHERE bot = 'kraken-s001'").fetchone()[0]
    conn.execute("INSERT INTO bots (name, exchange, strategy) VALUES ('kraken-x', 'kraken', 'BinHV45')")
    for bot, rsi, at in (("kraken-x", 91.234, opened), ("kraken-s001", 31.5, opened), ("kraken-s001", 87.654, NOW)):
        conn.execute("INSERT INTO decisions (league, account, venue, instrument, decided_at, action, rule, inputs, "
                     "sizing, fill_source, fill_id) VALUES ('crypto', %s, 'kraken', 'BTC/USD', %s, 'enter', "
                     "'Strategy001: entry signal', %s, %s, 'freqtrade', 1)",
                     (bot, at, Jsonb([{"name": "rsi", "value": rsi, "source": "kraken 5m candle",
                                       "as_of": "2026-11-04T12:15:00+00:00"}]),
                      Jsonb({"stake": 330.0, "amount": 0.004, "price": 78000.0, "fee_rate": 0.0026, "fee": 0.86})))
    _, client = signed_in
    body = client.get("/trades/freqtrade/1?bot=kraken-s001").text
    for needle in ("Strategy001: entry signal", "31.5", "kraken 5m candle", "2026-11-04 12:15:00", "stake 330.00",
                   "0.26%", "the candle it read is under Inputs"):
        assert needle in body, needle
    for absent in ("91.23", "87.65", "Recorded before decisions were traced", "Same pass", "contracts asked"):
        assert absent not in body, absent


def test_a_drill_and_a_call_open_their_page(conn, signed_in):
    did = conn.execute("INSERT INTO demo_orders (venue, ticker, client_id, price, count, placed_at, status) VALUES "
                       "('alpaca', 'SPY', 'c1', 1.0, 1, now(), 'filled') RETURNING id").fetchone()[0]
    cid = conn.execute("INSERT INTO calls (who, author, thesis, ticker, direction, instrument, entry_session, horizons, "
                       "benchmark, signal_source) VALUES ('human', 'kenneth', 'board ousted the CEO', 'NVDA', 'short', "
                       "'equity', '2026-11-13', '{1}', 'SMH', 'event') RETURNING id").fetchone()[0]
    conn.execute("INSERT INTO call_scores (call_id, horizon, exit_session, entry_close, exit_close, bench_entry, "
                 "bench_exit, ret, pnl, excess) VALUES (%s, 1, '2026-11-16', 100, 90, 200, 201, -0.1, 0.0996, 0.105)",
                 (cid,))
    _, client = signed_in
    assert f'href="/trades/drill/{did}"' in client.get("/markets/book").text
    assert f'href="/trades/call/{cid}"' in client.get("/calls").text
    drill = client.get(f"/trades/drill/{did}").text
    assert "SPY" in drill and "order path" in drill
    call = client.get(f"/trades/call/{cid}").text
    assert "board ousted the CEO" in call and "9.96%" in call and "10.50%" in call


def test_a_stock_fill_shows_its_rule_its_checks_and_its_exit(conn, signed_in):
    from lab.stocks import paper as stocks
    from lab.tests import test_stocks as ts
    ts.run(conn)
    fell = {**ts.CLOSES, "SPY": ts.CLOSES["SPY"][:-1] + [100]}
    ts.run(conn, ts.Data(fell, at=ts.LATER), now=ts.LATER)
    pid = conn.execute("SELECT id FROM paper_orders WHERE account = %s", (ts.TREND,)).fetchone()[0]
    _, client = signed_in
    page = client.get("/leagues").text
    tab = page[page.index('<h2 class="sec" id="stocks">'):]
    assert ts.TREND in tab and "fills opened today" in tab
    assert stocks.rule_of("trend", {"sma": 3}) in tab and "No accounts in this league yet" not in tab
    body = client.get(f"/trades/paper/{pid}").text
    for needle in ("SPY", "Stocks", "close above average", "quote fresh", "alpaca latest quote", "Sizing and costs",
                   "2 bp of the notional", "sold at the bid 99.99", "close below average", "Exit checks"):
        assert needle in body, needle
    assert "Recorded before decisions were traced" not in body


@pytest.mark.parametrize("path", ["/trades/paper/999", "/trades/call/999", "/trades/drill/999",
                                  "/trades/nope/1"])
def test_an_unknown_trade_is_404_not_an_empty_page(conn, signed_in, path):
    assert signed_in[1].get(path).status_code == 404


def test_today_counts_the_days_orders_fills_and_what_did_not_trade(conn, signed_in):
    day = dt.datetime(2026, 1, 5, 12, tzinfo=dt.timezone.utc)
    seed(conn)
    conn.execute("INSERT INTO bot_trades (bot, trade_id, open_date, pair, is_open, close_date, stake_amount, profit_ratio, "
                 "profit_abs, exit_reason, raw) VALUES ('kraken-s001', 9, %s, 'ETH/USD', false, %s, 100, 0.05, 5, 'roi', '{}')",
                 (day, day + dt.timedelta(hours=1)))
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at, retired_at) VALUES "
                 "('paper-longshot-aaaa1111', 'longshot', 'kalshi', 'aaaa1111', '{}', 1000, now(), NULL), "
                 "('paper-weather-bbbb2222', 'weather', 'kalshi', 'bbbb2222', '{}', 1000, now(), NULL), "
                 "('paper-tt_totals-cccc3333', 'tt_totals', 'odds_api', 'cccc3333', '{}', 1000, now(), NULL), "
                 "('paper-longshot-dddd4444', 'longshot', 'kalshi', 'dddd4444', '{}', 1000, now(), now())")
    for market, contracts, profit in (("M0", 10, 2.0), ("M1", 10, -1.0), ("M2", 0, None)):  # M2 never filled
        conn.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, fee, "
                     "settled_at, payout, profit) VALUES ('paper-longshot-aaaa1111', %s, 'no', 10, %s, %s, %s, 0.9, 0, %s, 1, %s)",
                     (market, day, day, contracts, day if profit is not None else None, profit))
    conn.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, fee) VALUES "
                 "('paper-longshot-aaaa1111', 'OLD', 'no', 10, %s, %s, 5, 0.9, 0), "
                 "('paper-weather-bbbb2222', 'M0', 'no', 10, %s, %s, 5, 0.9, 0)",  # a market two families traded
                 (day - dt.timedelta(days=3),) * 2 + (day,) * 2)
    _, client = signed_in
    page = client.get("/today?day=2026-01-05").text
    assert "<strong>5</strong> orders" in page  # 4 paper orders (one unfilled) and 1 crypto fill; OLD is another day
    assert "<strong>4</strong> fills" in page and "<strong>2</strong> venues" in page
    assert "<strong>3</strong> markets" in page  # M0 (two families), M1, ETH/USD: the per-row sum would be 4
    assert "kraken (1 bots)" in page and "longshot" in page
    assert "tt_totals on odds_api" in page and "weather on kalshi" not in page and "longshot on kalshi" not in page
    best = page[page.index('<h2 class="sec">Best</h2>'):page.index('<h2 class="sec">Worst</h2>')]
    assert best.index("M0") < best.index("ETH/USD") < best.index("M1") and "OLD" not in page  # by return: 22%, 5%, -11%
    assert "No dry run" in client.get("/today?day=2020-01-01").text and client.get("/today?day=nope").status_code == 404


def test_coverage_shows_the_stage_gaps_red(conn, signed_in):
    body = signed_in[1].get("/coverage").text
    assert "Lifecycle stages" in body and '<td class="down">missing</td>' in body
    assert "<code>lab.stocks.paper</code>" in body
