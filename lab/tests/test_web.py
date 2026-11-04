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
    for h, pct in ((3, 1.0), (2, -0.5), (1, 2.5)):
        c.execute("INSERT INTO bot_snapshots (bot, ts, dry_run, closed_trades, open_trades, profit_all_pct, "
                  "profit_all_abs, winrate_pct, max_drawdown_pct, balance_total) "
                  "VALUES ('kraken-s001', %s, true, 3, 1, %s, 25, 66, 1.2, 1025)", (NOW - dt.timedelta(hours=h), pct))
    c.execute("INSERT INTO bot_trades (bot, trade_id, open_date, pair, is_open, close_date, stake_amount, profit_ratio, "
              "profit_abs, exit_reason, raw) VALUES ('kraken-s001', 1, %s, 'BTC/USD', false, %s, 330, 0.012, 4, 'roi', "
              "'{}')", (NOW - dt.timedelta(hours=5), NOW - dt.timedelta(hours=4)))
    c.execute("INSERT INTO runs (kind, name, raw) VALUES ('experiments', '2026-11-04', '{}')")
    c.execute("INSERT INTO backtests (run_id, exchange, strategy, timeframe, trades, profit_pct) "
              "VALUES (1, 'binanceus', 'Strategy001', '5m', 40, 3.2), (1, 'binanceus', 'Broken', NULL, NULL, NULL)")
    c.execute("UPDATE backtests SET error = 'failed to load' WHERE strategy = 'Broken'")
    c.execute("INSERT INTO bias_checks (run_id, strategy, kind, has_bias, biased_indicators) "
              "VALUES (1, 'Strategy001', 'lookahead', true, 'rsi')")
    c.execute("INSERT INTO data_coverage (exchange, pair, timeframe, first_ts, last_ts, rows, oldest_reached) "
              "VALUES ('binanceus', 'BTC/USD', '5m', '2019-09-01', %s, 700000, true)", (NOW,))


def test_healthz_needs_no_sign_in(site):
    assert site[1].get("/healthz").text == "ok"


@pytest.mark.parametrize("path", ["/", "/bots/x", "/data", "/runs", "/runs/1"])
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
    pages = {"/": ["kraken-s001", "2.50%", "binanceus", "experiments 2026-11-04"],
             "/bots/kraken-s001": ["Strategy001 on kraken", "<svg", "BTC/USD", "1.20%"],
             "/data": ["BTC/USD", "700,000", "2019-09-01"],
             "/runs": ["2026-11-04", "experiments"],
             "/runs/1": ["3.20%", "failed to load", "biased", "rsi"]}
    for path, needles in pages.items():
        r = client.get(path)
        assert r.status_code == 200, path
        for n in needles:
            assert n in r.text, (path, n)
    assert "Not dry run" not in client.get("/").text
    assert client.get("/bots/nobody").status_code == 404
    assert client.get("/runs/99").status_code == 404


def test_overview_warns_when_a_bot_is_not_dry_run(conn, signed_in):
    seed(conn)
    conn.execute("INSERT INTO bot_snapshots (bot, ts, dry_run) VALUES ('kraken-s001', now(), false)")
    assert "Not dry run: kraken-s001" in signed_in[1].get("/").text


def test_pages_render_on_an_empty_database(conn, signed_in):
    for path in ("/", "/data", "/runs"):
        assert signed_in[1].get(path).status_code == 200, path


def test_chart_draws_only_with_two_points():
    from web import charts

    assert "Not enough points" in charts.line([])
    svg = charts.line([(NOW - dt.timedelta(hours=1), -1.0), (NOW, 2.0)], label="x")
    assert svg.startswith("<svg") and "series up" in svg and json.dumps("x")[1:-1] in svg
