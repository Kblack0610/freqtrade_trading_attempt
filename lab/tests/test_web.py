import datetime as dt
import json
import urllib.parse

import pytest

NOW = dt.datetime.now(dt.timezone.utc)
ENV = {"TRADER_WEB_BASE_URL": "http://testserver", "FORGEJO_URL": "https://git.example", "OAUTH_CLIENT_ID": "cid",
       "OAUTH_CLIENT_SECRET": "csecret", "SESSION_KEY": "k" * 32, "ALLOWED_USERS": "alice"}


@pytest.fixture
def site(monkeypatch):
    from fastapi.testclient import TestClient

    from web import app as app_mod, auth

    monkeypatch.setattr(app_mod, "cfg", auth.Config(ENV))
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
                           "skipped": ["Scalp on binanceus: no 1m candles"]}),))
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
                  "sharpe_lo, sharpe_hi, dsr, trials, max_drawdown, expectancy, gross_expectancy, gross_expectancy_lo, "
                  "fee_per_trade) VALUES "
                  "(%s, 'binanceus', '365d', 365, '2025-11-04', %s, 0.032, 1.1, %s, 1.8, 0.62, 2, 0.04, 0.012, 0.0145, "
                  "0.004, 0.0025)",
                  (name, trades, sharpe_lo))
    c.execute("INSERT INTO estimates VALUES ('Strategy001', 'binanceus', 90, -0.05, 0.021, 0.09, 0.31, 0.06, 5000, 1, 365)")
    c.execute("INSERT INTO correlations VALUES ('Strategy001', 'binanceus', 'Strategy002', 0.83, 300)")
    c.execute("INSERT INTO bias_checks (strategy, kind, file_hash, has_bias, biased_indicators) VALUES "
              "('Planted', 'lookahead', 'h2', true, 'next_close'), ('Strategy001', 'lookahead', 'h1', false, NULL), "
              "('Strategy001', 'recursive', 'h1', NULL, NULL)")
    c.execute("INSERT INTO data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, oldest_reached) "
              "VALUES ('binanceus', 'BTC/USD', '5m', '2019-09-01', %s, 700000, true)", (NOW,))


def test_healthz_needs_no_sign_in(site):
    assert site[1].get("/healthz").text == "ok"


@pytest.mark.parametrize("path", ["/", "/bots/x", "/strategies", "/strategies/x", "/suggestions", "/data", "/runs", "/runs/1"])
def test_signed_out_goes_to_login(site, path):
    r = site[1].get(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"


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
             "/bots/kraken-s001": ["Strategy001 on kraken", "<svg", "BTC/USD", "1.20%", "Live against backtest", "1.85%",
                                   "3 of 4 live entries matched; gap -0.65%"],
             "/strategies": ["Strategy001", "0.40", "62%", "-5% / ", "3.2%", "1.20%", "biased", "Broken", "failed to import",
                             "Scalp on binanceus: no 1m candles", "Fees by venue", "0.25%", "1.45%"],
             "/strategies?win=all": ["No results for this window yet"],
             "/strategies/Strategy001": ["binanceus", "<svg", "3.2%", "Strategy002: 0.83", "bull", "ETH/USD",
                                         "exit 2: boom", "not checked", "clean", "31%", "1.45%", "0.25%"],
             "/data": ["BTC/USD", "700,000", "2019-09-01"],
             "/runs": ["2026-11-04 03:00:00", "ok", "60 min"],
             "/runs/1": ["exit 2: boom", "Scalp on binanceus", "Strategy001"]}
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
    assert ">recursive</span>" in text
    assert "/strategies/Strategy001?" in signed_in[1].get("/").text


def test_overview_warns_when_a_bot_is_not_dry_run(conn, signed_in):
    seed(conn)
    conn.execute("INSERT INTO bot_snapshots (bot, ts, dry_run) VALUES ('kraken-s001', now(), false)")
    assert "Not dry run: kraken-s001" in signed_in[1].get("/").text


def test_pages_render_on_an_empty_database(conn, signed_in):
    for path in ("/", "/data", "/runs", "/strategies", "/suggestions"):
        assert signed_in[1].get(path).status_code == 200, path


def test_suggestions_show_each_kind_with_its_gates(conn, signed_in):
    import suggest

    seed(conn)
    assert suggest.run(conn)["suggestions"] == {"retire": 2, "fix": 1}
    text = signed_in[1].get("/suggestions").text
    for needle in ("Retire <span", "next_close", "Broken", "failed to import", "Fix <span", "startup_candle_count",
                   "Gates (0 of 1 pass)", "Promote <span"):
        assert needle in text, needle
    assert "1 fix, 2 retire" in signed_in[1].get("/").text
    assert "startup_candle_count" in signed_in[1].get("/strategies/Strategy001").text


def test_chart_draws_only_with_two_points():
    from web import charts

    assert "Not enough points" in charts.line([])
    svg = charts.line([(NOW - dt.timedelta(hours=1), -1.0), (NOW, 2.0)], label="x")
    assert svg.startswith("<svg") and "series up" in svg and json.dumps("x")[1:-1] in svg
