"""The trader site: a read-only view of the league and the lab, from the trader database.

Every page needs a Forgejo sign-in (web/auth.py) except /healthz. The site only reads: its
database role has SELECT grants and nothing else, and there are no trade controls.

Run: uvicorn web.app:app (env: DATABASE_URL plus the web/auth.py Config variables).
"""

import atexit
import datetime as dt
import math
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

from lab import book as book_lab
from lab import db, metrics, suggest
from web import auth, charts, exporter

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo(os.environ.get("TRADER_WEB_TZ", "UTC"))
STALE = dt.timedelta(hours=2)
POOL_MAX = 4  # connections per pod; a request holds one for its whole life, the rest wait for it
# Query results are keyed on the newest runs.finished_at, so a finished analysis or walk-forward shows at once.
# Ingest and the markets recorder write no runs row, so their data shows within CACHE_TTL.
CACHE_TTL = 60.0
CACHE_MAX = 512  # entries; the oldest goes first
TF_ORDER = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
FAMILIES = HERE.parent / "lab" / "families.toml"
MARKETS = HERE.parent / "lab" / "markets.toml"
STRATEGY_DIR = HERE.parent / "user_data" / "strategies"
# Direct IStrategy subclasses, read from source so the toml check needs neither freqtrade nor a database.
STRATEGY_CLASS = re.compile(r"^class (\w+)\(IStrategy\)", re.M)
UNTAGGED = "untagged"  # on disk, no line in lab/families.toml
GONE = "gone"  # in the results only: a deleted strategy, or the trial register's "(dropped)" hyperopts
# What /coverage joins to lab/families.toml, each one row per strategy x exchange.
COVER = {
    "trades": "SELECT strategy, exchange, trades AS n FROM strategy_windows WHERE win = 'all'",
    "months": "SELECT strategy, exchange, count(DISTINCT date_trunc('month', fold_start)) FILTER (WHERE ok) AS n "
              "FROM walkforward GROUP BY strategy, exchange",
    "trials": "SELECT strategy, exchange, count(*) AS n FROM trials GROUP BY strategy, exchange",
    "confidence": "SELECT strategy, exchange, passed, jsonb_array_length(gates) AS gates, ready, oos_trades FROM confidence",
}

BOTS = """
SELECT b.name, b.exchange, b.strategy, s.ts, s.dry_run, s.closed_trades, s.open_trades,
       s.profit_all_pct, s.profit_all_abs, s.winrate_pct, s.max_drawdown_pct, s.balance_total, s.error,
       d.matched, d.live_entries, d.bt_entries
FROM bots b
LEFT JOIN LATERAL (SELECT * FROM bot_snapshots WHERE bot = b.name ORDER BY ts DESC LIMIT 1) s ON true
LEFT JOIN drift d ON d.bot = b.name
ORDER BY s.profit_all_pct DESC NULLS LAST, b.name
"""
COVERAGE_BY_EXCHANGE = """
SELECT exchange, count(*) AS series, count(*) FILTER (WHERE oldest_reached) AS at_oldest,
       min(first_ts) AS first_ts, max(last_ts) AS last_ts
FROM data_coverage GROUP BY exchange ORDER BY exchange
"""
FUTURES = """
SELECT 'Funding rate' AS kind, exchange, count(DISTINCT pair) AS pairs, count(*) AS n, min(ts) AS first_ts, max(ts) AS last_ts
FROM funding_rates GROUP BY exchange
UNION ALL
SELECT 'Open interest', exchange, count(DISTINCT pair), count(*), min(ts), max(ts) FROM open_interest GROUP BY exchange
ORDER BY 1, 2
"""
COVERAGE = "SELECT * FROM data_coverage ORDER BY exchange, array_position(%s, timeframe), pair"
RUNS = """
SELECT id, name, started_at, finished_at, ok, raw, finished_at - started_at AS took
FROM runs WHERE kind = 'analysis' ORDER BY started_at DESC NULLS LAST, id DESC
"""
WINDOWS = ["30d", "90d", "180d", "365d", "all"]
SECTIONS = [
    ("promote", "Promote", "Clears every gate on an exchange where the league does not run it yet."),
    ("demote", "Demote", "A league bot whose own dry run loses, whose backtests lose confidently, or whose strategy is biased."),
    ("retire", "Retire", "A strategy file whose results are void (lookahead bias) or that does not load."),
    ("fix", "Fix", "A strategy that runs but whose indicators may not match live: its recursive check failed or could not run."),
    ("retune", "Retune", "Walk-forward: hyperopt's parameters beat the strategy as it is on months hyperopt never saw."),
    ("watch", "Watch", "Lookahead-clean candidates one gate short of promotion, best Sharpe interval first."),
]
CONFIDENCE = "SELECT * FROM confidence ORDER BY ready DESC, score DESC NULLS LAST, passed DESC, strategy, exchange"
BOOK = """
SELECT c.strategy, c.exchange, c.score, c.ready, c.passed, jsonb_array_length(c.gates) AS gates, w.trades, w.fee_per_trade
FROM confidence c LEFT JOIN strategy_windows w ON w.strategy = c.strategy AND w.exchange = c.exchange AND w.win = '365d'
WHERE c.score IS NOT NULL ORDER BY c.ready DESC, c.score DESC, c.strategy, c.exchange
"""
# Until confidence.scores() has min_population members it scores nobody; these show how far off that is.
MEMBERS = "SELECT count(*) AS n FROM confidence WHERE oos_trades >= %s"
NEAR = """SELECT strategy, exchange, oos_trades, oos_expectancy FROM confidence WHERE oos_trades > 0
ORDER BY oos_trades DESC, strategy, exchange LIMIT 30"""
SUGGESTIONS = "SELECT * FROM suggestions ORDER BY score DESC NULLS LAST, strategy, exchange, bot"
MIN_TRADES = 20  # below this a window's numbers are shown but not ranked
# Lookahead bias voids the backtest, so it ranks last. Recursive bias only warns: the flagged indicator may not feed a signal.
BIAS = """SELECT strategy, bool_or(has_bias) FILTER (WHERE kind = 'lookahead') AS biased,
                 bool_or(has_bias) FILTER (WHERE kind = 'recursive') AS unstable,
                 count(*) FILTER (WHERE has_bias IS NULL) AS unchecked FROM bias_checks GROUP BY strategy"""
# Ranked by the low end of the Sharpe interval: a strategy has to be good even on its unlucky draw.
LEADERBOARD = f"""
SELECT w.*, s.timeframe, e.p10, e.p50, e.p90, e.prob_loss, b.biased, b.unstable, b.unchecked,
       EXISTS (SELECT 1 FROM bots WHERE strategy = w.strategy AND exchange = w.exchange) AS in_league
FROM strategy_windows w
JOIN strategies s ON s.name = w.strategy
LEFT JOIN estimates e ON e.strategy = w.strategy AND e.exchange = w.exchange AND e.horizon_days = 90
LEFT JOIN ({BIAS}) b ON b.strategy = w.strategy
WHERE w.win = %s
ORDER BY b.biased IS TRUE, w.trades < {MIN_TRADES}, w.sharpe_lo DESC NULLS LAST, w.sharpe DESC NULLS LAST
"""
# Fees differ about tenfold between venues, so this sets each venue's fee against how many strategies keep an edge,
# next to how likely the best backtest there is overfit.
VENUES = f"""
SELECT w.exchange, count(*) AS strategies, avg(w.fee_per_trade) AS fee,
       count(*) FILTER (WHERE w.gross_expectancy_lo > 0) AS gross_edge,
       count(*) FILTER (WHERE w.expectancy_lo > 0) AS net_edge,
       o.pbo, o.strategies AS pbo_strategies
FROM strategy_windows w LEFT JOIN ({BIAS}) b ON b.strategy = w.strategy
LEFT JOIN overfit o ON o.exchange = w.exchange AND o.win = w.win
WHERE w.win = %s AND w.trades >= {MIN_TRADES} AND b.biased IS NOT TRUE
GROUP BY w.exchange, o.pbo, o.strategies ORDER BY w.exchange
"""
REGIME_SPLIT = """
SELECT coalesce(r.trend, 'any') AS trend, coalesce(r.vol_bucket, 'any') AS vol, count(*) AS days, sum(d.trades) AS trades,
       avg(d.ret) AS mean, stddev_samp(d.ret) AS sd
FROM strategy_daily d JOIN regimes r USING (day)
WHERE d.strategy = %s AND d.exchange = %s AND r.trend IS NOT NULL
GROUP BY GROUPING SETS ((r.trend), (r.vol_bucket)) ORDER BY 1, array_position(ARRAY['low', 'mid', 'high', 'any'], coalesce(r.vol_bucket, 'any'))
"""

# Event-contract markets (lab/markets): the recorder's health per venue, the history backfill, cross-venue links,
# backtests and the paper league. Counts come from the sweeps, never count(*) over markets: the archive is millions.
MARKET_VENUES = """
SELECT v.venue, v.title, v.kind, s.started_at, s.ok, s.error, ok.finished_at AS last_ok, ok.seen, ok.changes,
       ok.new_markets, ok.resolved, ok.finished_at - ok.started_at AS took,
       (SELECT count(*) FROM market_sweeps f WHERE f.venue = v.venue AND NOT f.ok
        AND f.started_at > now() - interval '1 day') AS failed_1d
FROM venues v
LEFT JOIN LATERAL (SELECT * FROM market_sweeps WHERE venue = v.venue ORDER BY started_at DESC LIMIT 1) s ON true
LEFT JOIN LATERAL (SELECT * FROM market_sweeps WHERE venue = v.venue AND ok ORDER BY started_at DESC LIMIT 1) ok ON true
ORDER BY s.started_at IS NULL, v.venue
"""
SWEEP_ERRORS = """SELECT venue, started_at, error FROM market_sweeps WHERE NOT ok AND started_at > now() - interval '7 days'
ORDER BY started_at DESC LIMIT 20"""
BACKFILL = """
SELECT venue, job, count(*) AS units, count(*) FILTER (WHERE done) AS done, sum(rows) AS rows, max(updated_at) AS updated,
       min(unit) FILTER (WHERE unit <> '') AS first_unit, max(unit) FILTER (WHERE unit <> '') AS last_unit
FROM market_backfill GROUP BY venue, job ORDER BY venue, job
"""
LINK_STATUSES = ("proposed", "confirmed", "rejected")
LINK_SUMMARY = """
SELECT source, status, count(*) AS n, count(*) FILTER (WHERE resolved_agrees IS false) AS disagreed
FROM market_links GROUP BY source, status ORDER BY source, array_position(%s, status)
"""
LINKS = """
SELECT l.*, a.title AS title_a, a.subtitle AS subtitle_a, a.close_time AS close_a, b.title AS title_b, b.subtitle AS subtitle_b
FROM market_links l
JOIN markets a ON a.venue = l.venue_a AND a.market_id = l.market_id_a
JOIN markets b ON b.venue = l.venue_b AND b.market_id = l.market_id_b
WHERE %s = '' OR l.status = %s
ORDER BY l.proposed_at DESC, l.id DESC LIMIT 200
"""
LINK_ASKS = "SELECT model, venue_b, count(*) AS asked, sum(proposed) AS proposed, max(asked_at) AS last FROM market_link_asks GROUP BY model, venue_b ORDER BY model, venue_b"
MARKET_BACKTESTS = "SELECT * FROM market_backtests ORDER BY score DESC NULLS LAST, expectancy_lo DESC NULLS LAST, family, venue, variant"
PAPER_ACCOUNTS = """
SELECT a.*, w.state, w.breached_at, w.stopped_at,
       (SELECT count(*) FROM paper_orders o WHERE o.account = a.account AND o.contracts > 0) AS fills,
       (SELECT count(*) FROM paper_orders o WHERE o.account = a.account AND o.contracts > 0 AND o.settled_at IS NOT NULL) AS settled
FROM paper_accounts a LEFT JOIN watch w ON w.bot = a.account
ORDER BY a.equity DESC NULLS LAST, a.account
"""
PAPER_FILLS = """
SELECT o.*, m.title, m.subtitle FROM paper_orders o
JOIN paper_accounts a ON a.account = o.account
LEFT JOIN markets m ON m.venue = a.venue AND m.market_id = o.market_id
WHERE o.contracts > 0 ORDER BY o.sent_at DESC, o.id DESC LIMIT 50
"""
DEMO_ORDERS = "SELECT * FROM demo_orders ORDER BY placed_at DESC, id DESC LIMIT 20"

cfg = auth.Config()
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()
# The request's connection and cache stamp, filled on its first rows() call. Middleware sets it; the
# handler's thread sees the same dict because Starlette copies the context into it.
_request: ContextVar[dict | None] = ContextVar("request_db", default=None)
_cache: dict[tuple, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def pool() -> ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(db.url(), min_size=1, max_size=POOL_MAX, kwargs={"autocommit": True},
                                  check=ConnectionPool.check_connection, open=True)
            atexit.register(_pool.close)
        return _pool


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg.check()
    if os.environ.get("METRICS_PORT"):
        exporter.serve()
    yield


app = FastAPI(title="trader", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@app.middleware("http")
async def one_connection(request: Request, call_next):
    """Every rows() call in a request shares one pooled connection, given back when the handler returns."""
    slot: dict = {}
    token = _request.set(slot)
    try:
        return await call_next(request)
    finally:
        _request.reset(token)
        if "conn" in slot:
            await run_in_threadpool(pool().putconn, slot["conn"])


def local(t: dt.datetime | None, fmt: str = "%b %d %H:%M") -> str:
    return t.astimezone(TZ).strftime(fmt) if t else "-"


def ago(t: dt.datetime | None) -> str:
    if not t:
        return "never"
    s = int((dt.datetime.now(dt.timezone.utc) - t).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return "just now"


def stale(t: dt.datetime | None, seconds: float | None = None) -> bool:
    limit = STALE if seconds is None else dt.timedelta(seconds=seconds)
    return t is None or dt.datetime.now(dt.timezone.utc) - t > limit


def num(v: float | None, digits: int = 2, unit: str = "") -> str:
    return "-" if v is None else f"{v:,.{digits}f}{unit}"


def pct(v: float | None, digits: int = 1) -> str:
    """A fraction as a percent."""
    return "-" if v is None else f"{100 * v:,.{digits}f}%"


def tone(v: float | None) -> str:
    return "" if not v else ("up" if v > 0 else "down")


def gv(v) -> str:
    """A gate's value: numbers to four significant digits, anything else as stored."""
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def skip_groups(skipped: list[str]) -> list[tuple[str, list[str]]]:
    """Skip lines ("<strategy> on <where>: <why>") grouped by where and why, so a venue still backfilling is one line."""
    groups: dict[str, list[str]] = {}
    for line in skipped:
        name, on, rest = line.partition(" on ")
        groups.setdefault(rest if on else line, []).extend([name] if on else [])
    return [(why, sorted(names)) for why, names in groups.items()]


templates.env.filters.update(local=local, ago=ago, num=num, pct=pct, tone=tone, gv=gv, skip_groups=skip_groups)
templates.env.globals.update(stale=stale, tz=str(TZ), min_trades=MIN_TRADES)


class SignIn(Exception):
    pass


@app.exception_handler(SignIn)
def _sign_in(request: Request, _exc: SignIn):
    return RedirectResponse("/login", status_code=303)


def user_of(request: Request) -> str:
    user = auth.read_session(cfg, request.cookies.get(auth.COOKIE))
    if not user:
        raise SignIn()
    return user


def rows(sql: str, params: tuple = ()) -> list[dict]:
    """A query's rows, from the cache while no run has finished since and CACHE_TTL has not passed."""
    slot = _request.get()
    if slot is None:
        raise RuntimeError("rows() runs only inside a request")
    if "conn" not in slot:
        slot["conn"] = pool().getconn()
        slot["stamp"] = slot["conn"].execute("SELECT max(finished_at) FROM runs").fetchone()[0]
    key, now = (sql, repr(params), slot["stamp"]), time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return [dict(r) for r in hit[1]]  # copies: handlers add fields to the rows they get
    with slot["conn"].cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        found = cur.fetchall()
    if CACHE_TTL > 0:
        with _cache_lock:
            _keep(key, now, slot["stamp"], found)
    return [dict(r) for r in found]


def _keep(key: tuple, now: float, stamp, found: list[dict]) -> None:
    """Stores one result after dropping expired ones, those from before the newest run, and past CACHE_MAX the oldest."""
    for k in [k for k, (t, _) in _cache.items() if now - t >= CACHE_TTL or k[2] != stamp]:
        del _cache[k]
    while len(_cache) >= CACHE_MAX:
        del _cache[next(iter(_cache))]
    _cache[key] = (now, found)


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"user": user_of(request), **ctx})


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@app.get("/login")
def login(request: Request):
    if auth.read_session(cfg, request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/", status_code=303)
    url, state = auth.authorize_url(cfg)
    resp = RedirectResponse(url, status_code=303)
    resp.set_cookie(auth.STATE_COOKIE, state, max_age=600, httponly=True, secure=cfg.secure, samesite="lax")
    return resp


@app.get("/oauth/callback")
def callback(request: Request, code: str = "", state: str = ""):
    expected = request.cookies.get(auth.STATE_COOKIE)
    if not code or not state or state != expected:
        raise HTTPException(400, "Sign-in expired or was not started here. Go to /login and try again.")
    login_name = auth.login_for_code(cfg, code)
    if login_name not in cfg.allowed:
        return templates.TemplateResponse(request, "denied.html", {"user": None, "login": login_name}, status_code=403)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, auth.make_session(cfg, login_name), max_age=auth.SESSION_DAYS * 86400,
                    httponly=True, secure=cfg.secure, samesite="lax")
    resp.delete_cookie(auth.STATE_COOKIE)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    user_of(request)
    bots = rows(BOTS)
    snaps = [b for b in bots if b["ts"]]
    last_ingest = rows("SELECT max(ts) AS ts FROM bot_snapshots WHERE source = 'ingest'")[0]["ts"]
    return page(request, "overview.html", bots=bots, last_ingest=last_ingest,
                total_abs=sum(b["profit_all_abs"] or 0 for b in snaps),
                total_balance=sum(b["balance_total"] or 0 for b in snaps),
                live_bots=[b["name"] for b in snaps if b["dry_run"] is False],
                coverage=rows(COVERAGE_BY_EXCHANGE), last_run=(rows(RUNS) or [None])[0],
                top=[r for r in rows(LEADERBOARD, ("365d",)) if not r["biased"] and r["trades"] >= MIN_TRADES][:5],
                suggested=rows("SELECT kind, count(*) AS n FROM suggestions GROUP BY kind"))


@app.get("/bots/{name}", response_class=HTMLResponse)
def bot(request: Request, name: str):
    user_of(request)
    found = [b for b in rows(BOTS) if b["name"] == name]
    if not found:
        raise HTTPException(404, f"No bot named {name}")
    series = rows("SELECT ts, profit_all_pct FROM bot_snapshots WHERE bot = %s AND profit_all_pct IS NOT NULL "
                  "ORDER BY ts", (name,))
    trades = rows("SELECT * FROM bot_trades WHERE bot = %s ORDER BY is_open DESC, "
                  "coalesce(close_date, open_date) DESC LIMIT 500", (name,))
    chart = charts.line([(r["ts"].astimezone(TZ), r["profit_all_pct"]) for r in series], label=f"{name} profit over time")
    return page(request, "bot.html", bot=found[0], trades=trades, chart=chart,
                drift=(rows("SELECT * FROM drift WHERE bot = %s", (name,)) or [None])[0])


@app.get("/data", response_class=HTMLResponse)
def data(request: Request):
    user_of(request)
    return page(request, "data.html", by_exchange=rows(COVERAGE_BY_EXCHANGE), series=rows(COVERAGE, (TF_ORDER,)),
                futures=rows(FUTURES))


@app.get("/strategies", response_class=HTMLResponse)
def strategies(request: Request, win: str = "365d"):
    user_of(request)
    if win not in WINDOWS:
        raise HTTPException(404, f"No window {win}")
    last = (rows(RUNS) or [None])[0]
    return page(request, "strategies.html", win=win, windows=WINDOWS, board=rows(LEADERBOARD, (win,)),
                venues=rows(VENUES, (win,)),
                broken=rows("SELECT name, file, load_error FROM strategies WHERE load_error IS NOT NULL ORDER BY name"),
                skipped=last["raw"].get("skipped", []) if last else [], last_run=last)


def equity_points(daily: list[dict], most: int = 400) -> list[tuple[dt.datetime, float]]:
    """Cumulative return in percent, thinned to at most `most` points for the SVG."""
    out, eq = [], 1.0
    step = max(1, math.ceil(len(daily) / most))
    for i, d in enumerate(daily):
        eq *= 1.0 + d["ret"]
        if i % step == 0 or i == len(daily) - 1:
            out.append((dt.datetime.combine(d["day"], dt.time(), dt.timezone.utc), 100 * (eq - 1.0)))
    return out


@app.get("/strategies/{name}", response_class=HTMLResponse)
def strategy(request: Request, name: str, exchange: str = ""):
    user_of(request)
    found = rows("SELECT * FROM strategies WHERE name = %s", (name,))
    if not found:
        raise HTTPException(404, f"No strategy named {name}")
    exchanges = [r["exchange"] for r in rows("SELECT DISTINCT exchange FROM backtest_chunks WHERE strategy = %s "
                                             "ORDER BY exchange", (name,))]
    exchange = exchange or (exchanges[0] if exchanges else "")
    key = (name, exchange)
    daily = rows("SELECT day, ret FROM strategy_daily WHERE strategy = %s AND exchange = %s ORDER BY day", key)
    regimes = rows(REGIME_SPLIT, key)
    for r in regimes:
        r["sharpe"] = r["mean"] / r["sd"] * math.sqrt(365) if r["sd"] else None
        r["annual"] = (1 + r["mean"]) ** 365 - 1 if r["mean"] is not None and r["mean"] > -1 else None
    return page(
        request, "strategy.html", s=found[0], exchange=exchange, exchanges=exchanges,
        windows=sorted(rows("SELECT * FROM strategy_windows WHERE strategy = %s AND exchange = %s", key),
                       key=lambda w: WINDOWS.index(w["win"])),
        chart=charts.line(equity_points(daily), label=f"{name} on {exchange} cumulative return", fmt="%Y-%m-%d"),
        estimates=rows("SELECT * FROM estimates WHERE strategy = %s AND exchange = %s ORDER BY horizon_days", key),
        bias=rows("SELECT * FROM bias_checks WHERE strategy = %s ORDER BY kind", (name,)),
        regimes=regimes,
        wf=[(pooled(f), f) for f in walkforward_by_pair("WHERE strategy = %s", (name,)).values()],
        suggestions=rows("SELECT * FROM suggestions WHERE strategy = %s ORDER BY kind, exchange, bot", (name,)),
        correlations=rows("SELECT * FROM correlations WHERE strategy = %s AND exchange = %s ORDER BY abs(rho) DESC", key),
        chunks=rows("SELECT * FROM backtest_chunks WHERE strategy = %s AND exchange = %s ORDER BY chunk_start DESC", key),
        trades=rows("SELECT t.* FROM backtest_trades t JOIN backtest_chunks c ON c.id = t.chunk_id "
                    "WHERE c.strategy = %s AND c.exchange = %s ORDER BY t.close_date DESC LIMIT 100", key))


def pooled(folds: list[dict]) -> dict:
    """One strategy x exchange's walk-forward folds as pooled out-of-sample numbers with 90% intervals."""
    tuned = [f for f in folds if f["ok"] and f["params"]]
    t = [x for f in tuned for x in (f["tuned_returns"] or [])]
    b = [x for f in tuned for x in (f["base_returns"] or [])]
    seed = metrics.seed_of(folds[0]["strategy"], folds[0]["exchange"], "wf")
    t_lo, t_hi = metrics.mean_ci(t, seed)
    b_lo, b_hi = metrics.mean_ci(b, seed)
    test_days = sum((f["test_end"] - f["fold_start"]).days for f in tuned)
    train_days = sum((f["fold_start"] - f["train_start"]).days for f in tuned)
    oos = sum(f["tuned_profit"] or 0.0 for f in tuned)
    ins = sum(f["is_profit"] or 0.0 for f in tuned)
    return {"strategy": folds[0]["strategy"], "exchange": folds[0]["exchange"], "folds": len(folds),
            "tuned_folds": len(tuned), "beat": sum((f["tuned_profit"] or 0) > (f["base_profit"] or 0) for f in tuned),
            "tuned_trades": len(t), "tuned_mean": sum(t) / len(t) if t else None, "tuned_lo": t_lo, "tuned_hi": t_hi,
            "base_trades": len(b), "base_mean": sum(b) / len(b) if b else None, "base_lo": b_lo, "base_hi": b_hi,
            # walk-forward efficiency: the tuned return per day out of sample over the same in sample
            "wfe": (oos / test_days) / (ins / train_days) if test_days and train_days and ins > 0 else None,
            "failed": sum(not f["ok"] for f in folds)}


def walkforward_by_pair(where: str = "", params: tuple = ()) -> dict[tuple[str, str], list[dict]]:
    out: dict = {}
    for r in rows(f"SELECT * FROM walkforward {where} ORDER BY strategy, exchange, fold_start", params):
        out.setdefault((r["strategy"], r["exchange"]), []).append(r)
    return out


@app.get("/walkforward", response_class=HTMLResponse)
def walkforward(request: Request):
    user_of(request)
    board = [pooled(f) for f in walkforward_by_pair().values()]
    board.sort(key=lambda r: -(r["tuned_lo"] if r["tuned_lo"] is not None else -math.inf))
    last = (rows("SELECT id, name, started_at, finished_at, ok, raw, finished_at - started_at AS took FROM runs "
                 "WHERE kind = 'walkforward' ORDER BY started_at DESC NULLS LAST, id DESC LIMIT 1") or [None])[0]
    return page(request, "walkforward.html", board=board, last_run=last)


@app.get("/suggestions", response_class=HTMLResponse)
def suggestions(request: Request):
    user_of(request)
    by_kind = {k: [] for k, _, _ in SECTIONS}
    for r in rows(SUGGESTIONS):
        by_kind.setdefault(r["kind"], []).append(r)
    return page(request, "suggestions.html", sections=SECTIONS, by_kind=by_kind)


def population(c: dict) -> dict:
    """How many strategies count toward the score's population, and the ones with out-of-sample trades so far."""
    return {"have": rows(MEMBERS, (c["min_member_trades"],))[0]["n"], "need": c["min_population"],
            "member_trades": c["min_member_trades"], "rows": rows(NEAR)}


@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    user_of(request)
    c = suggest.load_config()["confidence"]
    return page(request, "guide.html", need=c["min_population"], member_trades=c["min_member_trades"],
                ready=c["ready_score"], oos=c["min_oos_trades"])


@app.get("/confidence", response_class=HTMLResponse)
def confidence(request: Request):
    user_of(request)
    board = rows(CONFIDENCE)
    pop = None if any(r["score"] is not None for r in board) else population(suggest.load_config()["confidence"])
    return page(request, "confidence.html", board=board, pop=pop)


@app.get("/book", response_class=HTMLResponse)
def book(request: Request):
    user_of(request)
    cfg = suggest.load_config()
    bands = {(r["strategy"], r["exchange"], r["basis"], r["horizon_months"]): {**r, "after_tax": book_lab.median_after_tax(r, cfg)}
             for r in rows("SELECT * FROM book")}
    board = rows(BOOK)
    return page(request, "book.html", board=board, bands=bands, pop=None if board else population(cfg["confidence"]), budget=book_lab.budget(cfg),
                hurdle=book_lab.hurdle(cfg), yield_=cfg["book"]["tbill_yield"], tax=cfg["book"]["tax_short"],
                portfolio=rows("SELECT * FROM portfolio ORDER BY worst_corr"),
                benchmarks=rows("SELECT * FROM benchmarks ORDER BY end_day - start_day, name"),
                short=cfg["book"]["horizon_months"][0], long=cfg["book"]["horizon_months"][-1],
                stake=cfg["book"]["stages"][0]["stake"])


def strategy_classes() -> set[str]:
    """Every strategy class under user_data/strategies, subfolders included."""
    return {name for p in STRATEGY_DIR.rglob("*.py") for name in STRATEGY_CLASS.findall(p.read_text(encoding="utf-8"))}


def coverage_board(tags: dict, families: list[str], on_disk: set[str], found: dict[str, list[dict]]) -> dict:
    """The family x exchange matrix and one row per strategy, from lab/families.toml and the COVER results.

    A strategy on disk but not in the toml is untagged, so drift shows; one only in the results is gone."""
    cells: dict[str, dict[str, dict]] = {}
    for kind, rs in found.items():
        for r in rs:
            cells.setdefault(r["strategy"], {}).setdefault(r["exchange"], {})[kind] = r if kind == "confidence" else r["n"]
    exchanges = sorted({e for by in cells.values() for e in by if e})
    family_of = {n: tags[n]["family"] if n in tags else UNTAGGED if n in on_disk else GONE
                 for n in set(tags) | on_disk | set(cells)}
    order = families + [f for f in (UNTAGGED, GONE) if f in family_of.values()]
    strategies = []
    for name in sorted(family_of, key=lambda n: (order.index(family_of[n]), n.casefold())):
        mine = cells.get(name, {})
        conf = [c["confidence"] for c in mine.values() if "confidence" in c]
        strategies.append({
            "inputs": [], "note": None, **tags.get(name, {}), "name": name, "family": family_of[name], "on_disk": name in on_disk, "cells": mine,
            "venues": sorted(e for e, c in mine.items() if "trades" in c),
            **{k: sum(c.get(k, 0) for c in mine.values()) for k in ("trades", "months", "trials")},
            "best": max(conf, key=lambda c: (c["ready"], c["passed"]), default=None),
            "oos_trades": sum(c["oos_trades"] for c in conf)})
    def cell(members: list[dict], col: str | None) -> dict:
        """None is every venue together, so a trial with no venue (a load failure) counts only there."""
        picked = [c for s in members for e, c in s["cells"].items() if col is None or e == col]
        return {"tested": sum(bool(s["venues"]) if col is None else col in s["venues"] for s in members),
                **{k: sum(c.get(k, 0) for c in picked) for k in ("months", "trials")}}

    by_family = {f: [s for s in strategies if s["family"] == f] for f in order}
    matrix = [{"family": f, "n": len(ms), "cells": [cell(ms, col) for col in exchanges + [None]]} for f, ms in by_family.items()]
    return {"matrix": matrix, "strategies": strategies, "exchanges": exchanges}


@app.get("/coverage", response_class=HTMLResponse)
def coverage(request: Request):
    user_of(request)
    toml = suggest.load_config(FAMILIES)
    board = coverage_board(toml["strategies"], list(toml["families"]), strategy_classes(),
                           {kind: rows(sql) for kind, sql in COVER.items()})
    return page(request, "coverage.html", untagged=UNTAGGED, **board, families={
        **toml["families"], UNTAGGED: "On disk, no line in lab/families.toml.", GONE: "In the results only, not on disk."})


@app.get("/runs", response_class=HTMLResponse)
def runs(request: Request):
    user_of(request)
    return page(request, "runs.html", runs=rows(RUNS))


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run(request: Request, run_id: int):
    user_of(request)
    found = [r for r in rows(RUNS) if r["id"] == run_id]
    if not found:
        raise HTTPException(404, f"No run {run_id}")
    failed = rows("SELECT strategy, exchange, chunk_start, attempts, error FROM backtest_chunks "
                  "WHERE run_id = %s AND NOT ok ORDER BY strategy, exchange, chunk_start", (run_id,))
    return page(request, "run.html", run=found[0], failed=failed)


@app.get("/markets", response_class=HTMLResponse)
def markets(request: Request):
    user_of(request)
    max_age = suggest.load_config(MARKETS)["paper"]["max_book_age_min"]
    return page(request, "markets.html", venues=rows(MARKET_VENUES), errors=rows(SWEEP_ERRORS), backfill=rows(BACKFILL),
                max_age=max_age)


@app.get("/links", response_class=HTMLResponse)
def links(request: Request, status: str = ""):
    user_of(request)
    if status and status not in LINK_STATUSES:
        raise HTTPException(404, f"No link status {status}")
    return page(request, "links.html", status=status, statuses=LINK_STATUSES,
                summary=rows(LINK_SUMMARY, (list(LINK_STATUSES),)), links=rows(LINKS, (status, status)),
                asks=rows(LINK_ASKS))


@app.get("/markets/book", response_class=HTMLResponse)
def markets_book(request: Request):
    user_of(request)
    hard_stop = suggest.load_config()["book"]["hard_stop"]
    accounts = rows(PAPER_ACCOUNTS)
    for a in accounts:
        a["ret"] = None if a["equity"] is None else a["equity"] / a["capital"] - 1
    return page(request, "markets_book.html", backtests=rows(MARKET_BACKTESTS), accounts=accounts,
                fills=rows(PAPER_FILLS), demo=rows(DEMO_ORDERS), hard_stop=hard_stop)
