import datetime as dt
import json
import re
import urllib.parse

import pytest
from psycopg.types.json import Jsonb

NOW = dt.datetime.now(dt.timezone.utc)
ENV = {"TRADER_WEB_BASE_URL": "http://testserver", "FORGEJO_URL": "https://git.example", "OAUTH_CLIENT_ID": "cid",
       "OAUTH_CLIENT_SECRET": "csecret", "SESSION_KEY": "k" * 32, "ALLOWED_USERS": "alice"}


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
        c.execute("INSERT INTO regimes VALUES (%s, 80000, 'bull', 0.5, 'mid')", (day,))
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
    c.execute("INSERT INTO bias_checks (strategy, kind, file_hash, has_bias, biased_indicators) VALUES "
              "('Planted', 'lookahead', 'h2', true, 'next_close'), ('Strategy001', 'lookahead', 'h1', false, NULL), "
              "('Strategy001', 'recursive', 'h1', NULL, NULL)")
    c.execute("INSERT INTO data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, oldest_reached) "
              "VALUES ('binanceus', 'BTC/USD', '5m', '2019-09-01', %s, 700000, true)", (NOW,))
    c.execute("INSERT INTO open_interest (exchange, pair, ts, value_usd) VALUES ('okx', 'BTC/USDT:USDT', '2026-08-03', 3.6e9)")


def test_healthz_needs_no_sign_in(site):
    assert site[1].get("/healthz").text == "ok"


@pytest.mark.parametrize("path", ["/", "/live", "/live/rows", "/bots/x", "/strategies", "/strategies/x", "/suggestions", "/confidence", "/book", "/coverage", "/guide", "/data", "/runs", "/runs/1", "/markets", "/links", "/markets/book"])
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
    pages = {"/": ["kraken-s001", "2.50%", "3 of 4", "binanceus", "Best strategies", "/strategies/Strategy001?exchange=binanceus",
                   "2026-11-04 03:00:00"],
             "/bots/kraken-s001": ["Strategy001 on kraken", "<svg", "BTC/USD", "1.20%", "Dry run against backtest", "1.85%",
                                   "3 of 4 dry-run entries matched the backtest; the gap is -0.65%"],
             "/strategies": ["Strategy001", "0.40", "62%", "-5% / ", "3.2%", "1.20%", "lookahead bias", "Broken", "failed to import",
                             "binanceus: no 1m candles: Scalp", 'okx: 5m not fully backfilled: <span title="S0, S1, S2, S3">4 strategies</span>', "Fees by venue", "0.25%", "1.45%", "worth 1.7 independent",
                             "4321", '<td class="n down" title="2 strategies">64%</td>'],
             "/strategies?win=all": ["No results for this window yet"],
             "/strategies/Strategy001": ["binanceus", "<svg", "3.2%", "Strategy002: 0.83", "bull", "ETH/USD",
                                         "exit 2: boom", "not checked", "clean", "31%", "1.45%", "0.25%", "20%", "12.3%",
                                         "-5.0%", "-27.1%", "4321"],
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


def test_pages_render_on_an_empty_database(conn, signed_in):
    for path in ("/", "/data", "/runs", "/strategies", "/suggestions", "/walkforward", "/coverage", "/markets", "/links", "/markets/book"):
        assert signed_in[1].get(path).status_code == 200, path


def test_suggestions_show_each_kind_with_its_gates(conn, signed_in):
    from lab import suggest

    seed(conn)
    assert suggest.run(conn)["suggestions"] == {"retire": 2, "fix": 1}
    text = signed_in[1].get("/suggestions").text
    for needle in ("Retire <span", "next_close", "Broken", "failed to import", "Fix <span", "startup_candle_count",
                   "Gates: 0 of 1 pass", "Promote <span"):
        assert needle in text, needle
    assert "1 fix, 2 retire" in signed_in[1].get("/").text
    assert "startup_candle_count" in signed_in[1].get("/strategies/Strategy001").text


def test_confidence_shows_every_gate_and_the_first_it_fails(conn, signed_in):
    assert "Nothing scored yet" in signed_in[1].get("/confidence").text
    gates = [{"gate": "bias", "value": "clean", "need": "clean", "pass": True},
             {"gate": "drift", "value": None, "need": "inside the interval", "pass": False}]
    conn.execute("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, gates) "
                 "VALUES ('Alpha', 'binanceus', 71.4, false, 1, 42, %s)", (Jsonb(gates),))
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
    conn.execute("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, gates) "
                 "VALUES ('Alpha', 'binanceus', 71.4, true, 1, 42, %s)", (Jsonb(gates),))
    assert "Nothing scored yet" in client.get("/confidence").text
    conn.execute("INSERT INTO runs (kind, name, raw, started_at, finished_at, ok) VALUES ('analysis', 'a', '{}', now(), now(), true)")
    assert "Nothing scored yet" not in client.get("/confidence").text


def test_book_shows_no_number_until_a_strategy_is_scored(conn, signed_in):
    body = signed_in[1].get("/book").text
    assert "Not enough evidence" in body and "0 of 20" in body and "Stage 1" in body
    conn.execute("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, gates) "
                 "VALUES ('Alpha', 'binanceus', 71.4, false, 1, 42, %s)", (Jsonb([{"gate": "bias", "pass": True}],),))
    assert "not enough months" in signed_in[1].get("/book").text


def test_until_scored_both_pages_show_how_far_the_population_is(conn, signed_in):
    gates = Jsonb([{"gate": "bias", "pass": True}, {"gate": "score", "pass": False}])
    for name, trades, mean in (("Alpha", 42, 0.0123), ("Beta", 12, -0.004)):
        conn.execute("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, oos_expectancy, gates) "
                     "VALUES (%s, 'okx', NULL, false, 1, %s, %s, %s)", (name, trades, mean, gates))
    for path in ("/book", "/confidence"):
        body = signed_in[1].get(path).text
        assert "<strong>1 of 20</strong>" in body and "Unscored" in body, path
        assert "1.23%" in body and "-0.40%" in body and "no, 18 short" in body, path
        assert body.index("Alpha") < body.index("Beta"), path
    # Negative control: once anything is scored, the scored board replaces the panel.
    conn.execute("UPDATE confidence SET score = 71.4 WHERE strategy = 'Alpha'")
    for path in ("/book", "/confidence"):
        assert "How close the score is" not in signed_in[1].get(path).text, path


def test_guide_lists_every_page_in_reading_order(conn, signed_in):
    body = signed_in[1].get("/guide").text
    order = [body.index(f'<li><a href="{p}">') for p in ("/", "/data", "/strategies", "/walkforward", "/suggestions",
                                                          "/confidence", "/book", "/coverage", "/markets", "/runs")]
    assert order == sorted(order) and "needs 20 strategies with at least 30" in body and "trial register" in body


def test_book_sets_a_strategy_beside_hold(conn, signed_in):
    conn.execute("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, gates) "
                 "VALUES ('Alpha', 'binanceus', 71.4, false, 1, 42, %s)", (Jsonb([{"gate": "bias", "pass": True}],),))
    for basis, mid in (("strategy", 0.31), ("hold", 0.12)):
        for months in (12, 60):
            conn.execute("INSERT INTO book (strategy, exchange, basis, horizon_months, p5, p50, p95, prob_loss, "
                         "exp_max_drawdown, paths, basis_months) VALUES ('Alpha', 'binanceus', %s, %s, -0.2, %s, 0.9, "
                         "0.25, 0.3, 5000, 12)", (basis, months, mid))
    body = signed_in[1].get("/book").text
    assert "Alpha" in body and "31%" in body and "12%" in body and "Not enough evidence" not in body
    for needle in ("24% / 9%", "T-bills at 4.4%", "USDT", "No set yet"):
        assert needle in body, needle
    conn.execute("INSERT INTO portfolio VALUES ('Alpha', 'binanceus', 0.21, 0.4, 0.25, 900)")
    assert "0.21" in signed_in[1].get("/book").text


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


def families():
    from web import app as app_mod

    return app_mod.suggest.load_config(app_mod.FAMILIES)


def test_every_strategy_class_has_a_family():
    from web import app as app_mod

    toml, on_disk = families(), app_mod.strategy_classes()
    assert {"Strategy001", "ADXMomentum", "DevilStra"} <= on_disk and "StrategyHelper" not in on_disk
    research = app_mod.STRATEGY_CLASS.findall((app_mod.HERE.parent / "lab/research/BustShort.py").read_text())
    assert set(toml["strategies"]) == on_disk | set(research), "add the class to lab/families.toml, or drop a stale line"
    assert {t["family"] for t in toml["strategies"].values()} <= set(toml["families"])
    assert all(t["inputs"] for t in toml["strategies"].values())


def test_coverage_joins_the_families_to_the_results(conn, signed_in):
    seed(conn)
    conn.execute("DELETE FROM trials WHERE strategy IN ('Strategy001', 'Mystery')")  # keeps the migration's seed rows
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
        conn.execute("INSERT INTO confidence (strategy, exchange, score, ready, passed, oos_trades, gates) "
                     "VALUES ('Strategy001', %s, NULL, false, %s, %s, %s)", (exchange, passed, oos, Jsonb(gates)))
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
    c.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at, equity, realized, open_positions, marked_at) "
              "VALUES ('paper-longshot-abcdef01', 'longshot', 'kalshi', 'abcdef0123456789', %s, 1000, now(), 987.5, -4.25, 3, now())",
              (Jsonb({"edge": 0.06}),))
    c.execute("INSERT INTO paper_orders (account, market_id, side, stake, sent_at, quoted_at, contracts, price, fee) VALUES "
              "('paper-longshot-abcdef01', 'KXNBAGAME-26NOV09SASPHX-SAS', 'no', 10, now(), now(), 11, 0.91, 0.07)")
    c.execute("INSERT INTO watch (bot, checked_at, seen_at, state, stopped_at) VALUES ('paper-longshot-abcdef01', now(), now(), 'stopped', now())")
    c.execute("INSERT INTO demo_orders (venue, ticker, client_id, price, count, placed_at, status, canceled_at) VALUES "
              "('kalshi', 'KXDEMO-1', 'c1', 0.01, 1, now(), 'canceled', now())")
    c.execute("INSERT INTO broker_balances (broker, read_at, status, currency, equity, cash, buying_power, positions) VALUES "
              "('alpaca_paper', now(), 'ACTIVE', 'USD', 100012.5, 99000, 199000, 2)")


def test_markets_pages_render_with_data(conn, signed_in):
    seed_markets(conn)
    client = signed_in[1]
    pages = {"/markets": ["Kalshi", "179,162", "10,099", "HTTPError: 503 upstream", '<td class="down">37m ago</td>',
                          "The Odds API", "1 of 2", "2026-01-02 to 2026-01-03", "4,321", "never swept"],
             "/links": ["rule:kalshi_game", "San Antonio at Phoenix", "Spurs at Suns", "by rule", "claude-haiku"],
             "/links?status=rejected": ["No rejected links."],
             "/markets/book": ["longshot", "abcdef01", "1.23%", "-0.40% to 3.10%", "61%", "0.42", "paper-longshot-abcdef01",
                               "987.50", "-1.25%", "stopped", "San Antonio at Phoenix", "0.91", "KXDEMO-1", "canceled", "15%",
                               "alpaca_paper", "100,012.50"]}
    for path, needles in pages.items():
        r = client.get(path)
        assert r.status_code == 200, path
        for n in needles:
            assert n in r.text, (path, n)
    # the healthy venue is not marked stale, and a link filter outside the statuses is a 404
    assert '<td class="">3m ago</td>' in client.get("/markets").text
    assert "Spurs at Suns" not in client.get("/links?status=rejected").text
    assert client.get("/links?status=bogus").status_code == 404
