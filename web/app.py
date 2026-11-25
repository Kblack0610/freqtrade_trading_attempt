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
from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

from lab.crypto import book as book_lab
from lab.money import guard as guard_lab
from lab import inputs as inputs_lab
from lab.inputs import call_rules
from lab.inputs import calls as calls_lab
from lab.core import db, readiness
from lab.crypto import diversity, suggest, taxonomy, universe
from lab import metrics
from lab.money import leagues as league_rows
from lab.stages import coverage as stage_coverage
from lab.stocks import paper as stocks_lab
from lab.events import paper as paper_lab, paper_venues, sweep_ages as market_sweep_ages
from web import auth, charts, exporter, glossary, lanes, nav
from web import venues as venue_roster

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo(os.environ.get("TRADER_WEB_TZ", "UTC"))
STALE = dt.timedelta(hours=2)
POOL_MAX = 4  # connections per pod; a request holds one for its whole life, the rest wait for it
# Query results are keyed on the newest runs.finished_at, so a finished analysis or walk-forward shows at once.
# Ingest and the markets recorder write no runs row, so their data shows within CACHE_TTL.
CACHE_TTL = 60.0
CACHE_MAX = 512  # entries; the oldest goes first
TF_ORDER = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
FAMILIES = HERE.parent / "lab" / "crypto" / "families.toml"
MARKETS = HERE.parent / "lab" / "events" / "markets.toml"
STRATEGY_DIR = HERE.parent / "user_data" / "strategies"
# Direct IStrategy subclasses, read from source so the toml check needs neither freqtrade nor a database.
STRATEGY_CLASS = re.compile(r"^class (\w+)\((?:\w+, )*IStrategy\)", re.M)  # mixins first, e.g. (EntryInputs, IStrategy)
UNTAGGED = "untagged"  # on disk, no line in lab/crypto/families.toml
GONE = "gone"  # in the results only: a deleted strategy, or the trial register's "(dropped)" hyperopts
# What /coverage joins to lab/crypto/families.toml, each one row per strategy x exchange.
COVER = {
    "trades": "SELECT strategy, exchange, trades AS n FROM strategy_windows WHERE win = 'all'",
    "months": "SELECT strategy, exchange, count(DISTINCT date_trunc('month', fold_start)) FILTER (WHERE ok) AS n "
              "FROM walkforward GROUP BY strategy, exchange",
    "trials": "SELECT strategy, exchange, count(*) AS n FROM trials GROUP BY strategy, exchange",
    "confidence": f"SELECT strategy, exchange, passed, jsonb_array_length(gates) AS gates, ready, oos_trades FROM ({readiness.CRYPTO_ROWS}) c",
}

BOTS = """
SELECT b.name, b.exchange, b.strategy, b.cohort, b.overlay, o.params AS overlay_params, s.ts, s.dry_run,
       s.closed_trades, s.open_trades, s.profit_closed_abs, s.profit_all_pct, s.profit_all_abs, s.winrate_pct, s.max_drawdown_pct,
       s.balance_total, s.raw->'balance'->>'stake' AS balance_currency, s.error, d.matched, d.live_entries, d.bt_entries
FROM bots b
LEFT JOIN LATERAL (SELECT * FROM bot_snapshots WHERE bot = b.name ORDER BY ts DESC LIMIT 1) s ON true
LEFT JOIN drift d ON d.bot = b.name
LEFT JOIN overlays o ON o.id = b.overlay
"""
BOT = BOTS + "WHERE b.name = %s"
BOTS += "ORDER BY s.profit_all_pct DESC NULLS LAST, b.name"
# Every bot's newest snapshot with its open trades, one row per trade (or one empty row for a bot with none).
LIVE = """
SELECT b.name, b.exchange, b.strategy, s.ts, s.error, s.raw->>'timeframe' AS timeframe, s.raw->'candles' AS candles,
       t.trade_id, t.pair, t.open_date, t.open_rate, t.stake_amount, t.profit_ratio, t.profit_abs,
       (t.raw->>'current_rate')::double precision AS last_price
FROM bots b
LEFT JOIN LATERAL (SELECT * FROM bot_snapshots WHERE bot = b.name ORDER BY ts DESC LIMIT 1) s ON true
LEFT JOIN bot_trades t ON t.bot = b.name AND t.is_open
ORDER BY b.name, t.open_date
"""
LIVE_POLL = "30s"  # the table re-fetches this often; ingest writes the numbers, so this only bounds how late they show
TF_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
# The last snapshot of each hour, so the chart's points stay one per hour however often ingest runs.
BOT_SERIES = """
SELECT DISTINCT ON (date_trunc('hour', ts)) ts, profit_all_pct FROM bot_snapshots
WHERE bot = %s AND profit_all_pct IS NOT NULL ORDER BY date_trunc('hour', ts), ts DESC
"""
# The newest 100 trades of each chunk through backtest_trades (chunk_id, close_date), then the newest 100 of those,
# so the page never sorts every trade a strategy has.
STRATEGY_TRADES = """
SELECT t.* FROM backtest_chunks c
CROSS JOIN LATERAL (SELECT * FROM backtest_trades WHERE chunk_id = c.id ORDER BY close_date DESC LIMIT 100) t
WHERE c.strategy = %s AND c.exchange = %s AND c.overlay = '' ORDER BY t.close_date DESC LIMIT 100
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
UNIVERSE = "SELECT raw->'universe' AS universe FROM runs WHERE kind = 'analysis' AND raw ? 'universe' ORDER BY started_at DESC NULLS LAST, id DESC LIMIT 1"
WINDOWS = ["30d", "90d", "180d", "365d", "all"]
SECTIONS = [
    ("promote", "Promote", "Clears every gate on an exchange where the league does not run it yet."),
    ("demote", "Demote", "A league bot whose own dry run loses, whose backtests lose confidently, or whose strategy is biased."),
    ("retire", "Retire", "A strategy file whose results are void (lookahead bias) or that does not load, or a bot whose live trades decayed below its out-of-sample edge."),
    ("fix", "Fix", "A strategy that runs but whose indicators may not match live: its recursive check failed or could not run."),
    ("retune", "Retune", "Walk-forward: hyperopt's parameters beat the strategy as it is on months hyperopt never saw."),
    ("watch", "Watch", "Lookahead-clean candidates one gate short of promotion, best Sharpe interval first."),
]
CONFIDENCE = f"SELECT * FROM ({readiness.CRYPTO_ROWS}) c ORDER BY ready DESC, score DESC NULLS LAST, passed DESC, strategy, exchange"
BOOK = f"""
SELECT c.strategy, c.exchange, c.score, c.ready, c.passed, jsonb_array_length(c.gates) AS gates, w.trades, w.fee_per_trade
FROM ({readiness.CRYPTO_ROWS}) c LEFT JOIN strategy_windows w ON w.strategy = c.strategy AND w.exchange = c.exchange AND w.win = '365d'
WHERE c.score IS NOT NULL ORDER BY c.ready DESC, c.score DESC, c.strategy, c.exchange
"""
# Until confidence.scores() has min_population members it scores nobody; these show how far off that is.
MEMBERS = f"SELECT count(*) AS n FROM ({readiness.CRYPTO_ROWS}) c WHERE oos_trades >= %s"
NEAR = f"""SELECT strategy, exchange, oos_trades, oos_expectancy FROM ({readiness.CRYPTO_ROWS}) c WHERE oos_trades > 0
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

# Event-contract markets (lab/events): the recorder's health per venue, the history backfill, cross-venue links,
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
MARKET_BACKTESTS = "SELECT * FROM market_backtests ORDER BY start_at DESC, expectancy_lo DESC NULLS LAST, family, venue, variant"
MARKET_READINESS = f"SELECT * FROM ({readiness.EVENT_ROWS}) r ORDER BY ready DESC, passed DESC, expectancy_lo DESC NULLS LAST, family, venue, variant"
MARKET_CATEGORIES = "SELECT * FROM market_category_results ORDER BY category, lo DESC NULLS LAST, family, venue, variant"
PAPER_ACCOUNTS = """
SELECT a.*, w.state, w.breached_at, w.stopped_at,
       (SELECT count(*) FROM paper_orders o WHERE o.account = a.account AND o.contracts > 0) AS fills,
       (SELECT count(*) FROM paper_orders o WHERE o.account = a.account AND o.contracts > 0 AND o.settled_at IS NOT NULL) AS settled
FROM paper_accounts a LEFT JOIN watch w ON w.bot = a.account
WHERE a.venue = ANY(%s)
ORDER BY a.equity DESC NULLS LAST, a.account
"""
DEMO_ORDERS = "SELECT * FROM demo_orders ORDER BY placed_at DESC, id DESC LIMIT 20"
BROKERS = "SELECT DISTINCT ON (broker) * FROM broker_balances ORDER BY broker, read_at DESC"
WALLETS = "SELECT DISTINCT ON (account, coin) * FROM wallet_balances ORDER BY account, coin, read_at DESC"
# Only the newest pass: a bot the watcher no longer reads keeps its last row, with a checked_at that stopped advancing.
KILL_SWITCH = """
SELECT * FROM watch WHERE checked_at = (SELECT max(checked_at) FROM watch)
ORDER BY stopped_at IS NULL, breached_at IS NULL, bot"""
# Shadow paper accounts (lab/crypto/shadow.py): winners, then the rest active, then the newest retired, best return first.
SHADOW = """
SELECT a.account, a.strategy, a.exchange, a.cohort, a.state, a.promoted_on, a.retired_on, a.retired_why, a.capital,
       m.days, coalesce(m.equity, a.capital) AS equity, greatest(coalesce(m.peak, a.capital), a.capital) AS peak
FROM shadow_accounts a LEFT JOIN LATERAL (
    SELECT count(*) AS days, (array_agg(equity ORDER BY day DESC))[1] AS equity, max(equity) AS peak
    FROM shadow_marks WHERE account = a.account) m ON true
ORDER BY a.state = 'retired', a.state <> 'winning', a.retired_on DESC NULLS FIRST,
         coalesce(m.equity, a.capital) / a.capital DESC, a.account
LIMIT 200"""
SHADOW_COUNTS = "SELECT cohort, state, count(*) AS n FROM shadow_accounts GROUP BY 1, 2"

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
            _pool = ConnectionPool(db.url(), min_size=1, max_size=POOL_MAX, kwargs={"autocommit": True, **db.KEEPALIVE},
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


def tv(v) -> str:
    """A traced check or input value: numbers to four significant digits, a list joined, a dict as its pairs."""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, tuple)):
        return " / ".join(tv(x) for x in v)
    if isinstance(v, dict):
        return ", ".join(f"{k} {tv(x)}" for k, x in v.items())
    return gv(v)


def skip_groups(skipped: list[str]) -> list[tuple[str, list[str]]]:
    """Skip lines ("<strategy> on <where>: <why>") grouped by where and why, so a venue still backfilling is one line."""
    groups: dict[str, list[str]] = {}
    for line in skipped:
        name, on, rest = line.partition(" on ")
        groups.setdefault(rest if on else line, []).extend([name] if on else [])
    return [(why, sorted(names)) for why, names in groups.items()]


templates.env.filters.update(local=local, ago=ago, num=num, pct=pct, tone=tone, gv=gv, tv=tv, skip_groups=skip_groups)
templates.env.globals.update(nav_groups=nav.groups, stale=stale, tz=str(TZ), min_trades=MIN_TRADES)


class SignIn(Exception):
    pass


@app.exception_handler(SignIn)
def _sign_in(request: Request, _exc: SignIn):
    if request.headers.get("HX-Request"):  # a poll: htmx loads /login as a page instead of swapping it into the table
        return Response(status_code=204, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def user_of(request: Request) -> str:
    user = auth.read_session(cfg, request.cookies.get(auth.COOKIE))
    if not user:
        raise SignIn()
    return user


def paper_accounts() -> list[dict]:
    """The paper league's accounts: those of a venue in `[paper] venues`. One taken out keeps its rows, off every page."""
    return rows(PAPER_ACCOUNTS, (paper_venues(),))


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


def cohorts(bots: list[dict]) -> list[dict]:
    """One row per cohort (lab/crypto/league.py), league first then by name: its bots, the ones with a snapshot, and their
    summed profit, balance and trades, mean profit percent, and best and worst bot. Profit percents are each bot's own
    starting balance, so the mean weighs every bot alike."""
    out: dict[str, dict] = {}
    for b in bots:
        c = out.setdefault(b["cohort"], {"cohort": b["cohort"], "bots": 0, "reporting": [], "profit_abs": 0.0,
                                         "balance": 0.0, "open_trades": 0, "closed_trades": 0})
        c["bots"] += 1
        if b["ts"] and b["profit_all_pct"] is not None:
            c["reporting"].append(b)
            c["profit_abs"] += b["profit_all_abs"] or 0
            c["balance"] += b["balance_total"] or 0
            c["open_trades"] += b["open_trades"] or 0
            c["closed_trades"] += b["closed_trades"] or 0
    for c in out.values():
        ranked = sorted(c.pop("reporting"), key=lambda b: b["profit_all_pct"])
        c["reporting"] = len(ranked)
        c["mean_pct"] = sum(b["profit_all_pct"] for b in ranked) / len(ranked) if ranked else None
        c["worst"], c["best"] = (ranked[0], ranked[-1]) if ranked else (None, None)
    return sorted(out.values(), key=lambda c: (c["cohort"] != "league", c["cohort"]))


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    user_of(request)
    bots = rows(BOTS)
    snaps = [b for b in bots if b["ts"]]
    paper = paper_accounts()
    return page(request, "overview.html", cohorts=cohorts(bots), lanes=lanes.cards(lane_reads(bots, paper)),
                live_bots=[b["name"] for b in snaps if b["dry_run"] is False],
                top=[r for r in rows(LEADERBOARD, ("365d",)) if not r["biased"] and r["trades"] >= MIN_TRADES][:5])


def lane_reads(bots: list[dict], paper: list[dict]) -> dict:
    """The rows each lane card maps (web/lanes.py), from the same SQL as the lane's own tab."""
    ready = "SELECT count(*) FILTER (WHERE ready) AS ready, count(*) AS n FROM readiness WHERE market = %s"
    paper_age = {c["match"]: c["max_age_h"] for v in venue_roster.load() for c in v["checks"] if c["source"] == "paper"}
    return {"stale_h": STALE.total_seconds() / 3600, "bots": bots, "paper": paper, "paper_max_age_h": paper_age,
            "stocks": rows(PAPER_ACCOUNTS, ([stocks_lab.VENUE],)),
            "shadow_counts": rows(SHADOW_COUNTS),
            "shadow_newest": rows("SELECT max(day) AS day FROM shadow_marks")[0]["day"],
            "calls": rows(CALLS), "calls_cfg": calls_lab.load_config(), "venues": venue_roster.rows(rows),
            "recorder": rows(MARKET_VENUES), "recorder_ages": sweep_ages(),
            "inputs": {r["input"]: r["newest"] for r in rows(inputs_lab.NEWEST)},
            "coverage": rows(COVERAGE_BY_EXCHANGE), "runs": rows(RUNS + " LIMIT 50"),
            "confidence": rows(ready, (readiness.CRYPTO,))[0], "readiness": rows(ready, (readiness.EVENTS,))[0],
            "suggestions": rows("SELECT kind, count(*) AS n FROM suggestions GROUP BY kind ORDER BY kind"),
            "bankroll": next(iter(rows("SELECT * FROM bankroll")), None), "watch": rows(KILL_SWITCH),
            "guard_lifted": guard_lab.GUARD_LIFTED}


def tf_seconds(tf: str | None) -> int | None:
    """A freqtrade timeframe ("5m", "1h") in seconds."""
    if not tf or tf[-1] not in TF_SECONDS or not tf[:-1].isdigit():
        return None
    return int(tf[:-1]) * TF_SECONDS[tf[-1]]


def live_view() -> dict:
    """Each bot with its open positions, and the totals over them. A candle is late when the bot's newest one was
    more than two timeframes old at snapshot time: its prices had stopped moving, whatever the snapshot age says."""
    bots: dict[str, dict] = {}
    for r in rows(LIVE):
        b = bots.setdefault(r["name"], {k: r[k] for k in ("name", "exchange", "strategy", "ts", "error", "timeframe")}
                            | {"positions": []})
        if r["trade_id"] is None:
            continue
        ms = (r["candles"] or {}).get(r["pair"])
        candle = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc) if ms else None
        step = tf_seconds(b["timeframe"])
        r["candle"] = candle
        r["candle_late"] = bool(candle and step and b["ts"] and (b["ts"] - candle).total_seconds() > 2 * step)
        b["positions"].append(r)
    found = list(bots.values())
    open_ = [p for b in found for p in b["positions"]]
    return {"bots": found, "poll": LIVE_POLL, "positions": len(open_),
            "unrealized": sum(p["profit_abs"] or 0 for p in open_), "stake": sum(p["stake_amount"] or 0 for p in open_),
            "newest": max((b["ts"] for b in found if b["ts"]), default=None)}


@app.get("/cohorts", response_class=HTMLResponse)
def cohorts_page(request: Request):
    user_of(request)
    bots = rows(BOTS)
    return page(request, "cohorts.html", cohorts=cohorts(bots), bots=bots)


# Each paper account's settled profits, oldest first: the curve its max drawdown is read from.
PAPER_PROFITS = """
SELECT account, array_agg(profit ORDER BY settled_at, id) AS profits FROM paper_orders
WHERE contracts > 0 AND settled_at IS NOT NULL GROUP BY account
"""
# /today: every dry run of one UTC day. Orders are the paper_orders sent that day (filled or not); a crypto
# fill is its own order. Day P&L is the profit of fills that closed that day.
DRY_SOURCES = ("freqtrade", "paper")
TODAY_FILLS = """
SELECT f.source, f.venue, CASE WHEN f.source = 'freqtrade' THEN NULL ELSE f.strategy END AS family,
       count(DISTINCT f.account) AS accounts, count(DISTINCT f.strategy) AS strategies,
       count(*) FILTER (WHERE f.open_date >= %(a)s AND f.open_date < %(b)s) AS fills,
       count(DISTINCT f.instrument) FILTER (WHERE f.open_date >= %(a)s AND f.open_date < %(b)s) AS markets,
       sum(f.profit_abs) FILTER (WHERE f.close_date >= %(a)s AND f.close_date < %(b)s) AS pnl
FROM fills f
WHERE f.source = ANY(%(src)s) AND ((f.open_date >= %(a)s AND f.open_date < %(b)s) OR (f.close_date >= %(a)s AND f.close_date < %(b)s))
GROUP BY 1, 2, 3
"""
TODAY_ORDERS = """
SELECT a.venue, a.family, count(*) AS orders FROM paper_orders o JOIN paper_accounts a USING (account)
WHERE o.sent_at >= %s AND o.sent_at < %s GROUP BY 1, 2
"""
TODAY_BEST = """
SELECT f.*, m.title FROM fills f
LEFT JOIN markets m ON f.source = 'paper' AND m.venue = f.venue AND m.market_id = f.instrument
WHERE f.source = ANY(%s) AND f.close_date >= %s AND f.close_date < %s AND f.profit_ratio IS NOT NULL
ORDER BY f.profit_ratio {order}, f.fill_id LIMIT 10
"""
# Distinct markets the day's fills opened in: a market two families traded counts once.
TODAY_MARKETS = """
SELECT count(DISTINCT (venue, instrument)) AS n FROM fills WHERE source = ANY(%s) AND open_date >= %s AND open_date < %s
"""
TODAY_ACTIVE = """
SELECT 'paper' AS source, venue, family FROM paper_accounts WHERE retired_at IS NULL GROUP BY 2, 3
"""


def today_board(fills: list[dict], orders: list[dict], active: list[dict]) -> dict:
    """The /today table: one row per (source, venue, family), the day's totals, and the families that run but placed nothing."""
    placed = {(o["venue"], o["family"]): o["orders"] for o in orders}
    table = []
    for f in fills:
        paper = f["source"] == "paper"
        n = placed.pop((f["venue"], f["family"]), 0) if paper else f["fills"]
        table.append({**f, "orders": max(n, f["fills"]), "label": f["venue"] if f["source"] != "freqtrade"
                      else f"{f['venue']} ({f['accounts']} bots)", "family": f["family"] or f"{f['strategies']} strategies"})
    for (venue, family), n in placed.items():  # orders that never filled: the family still traded today
        table.append({"source": "paper", "venue": venue, "family": family, "label": venue, "orders": n, "fills": 0,
                      "markets": 0, "pnl": None, "accounts": None})
    seen = {(r["source"], r["venue"], r["family"]) for r in table}
    table.sort(key=lambda r: (-r["orders"], r["label"], r["family"]))
    return {"table": table,
            "orders": sum(r["orders"] for r in table), "fills": sum(r["fills"] for r in table),
            "venues": len({r["venue"] for r in table}),
            "idle": sorted((a["source"], a["venue"], a["family"]) for a in active
                           if (a["source"], a["venue"], a["family"]) not in seen)}


@app.get("/today", response_class=HTMLResponse)
def today(request: Request, day: str = ""):
    """Every dry run of one UTC day on one page: orders, fills and day P&L per venue and family, the day's best and worst."""
    user_of(request)
    try:
        d = dt.date.fromisoformat(day) if day else dt.datetime.now(dt.timezone.utc).date()
    except ValueError:
        raise HTTPException(404, "day is YYYY-MM-DD")
    a = dt.datetime.combine(d, dt.time(), dt.timezone.utc)
    b = a + dt.timedelta(days=1)
    board = today_board(rows(TODAY_FILLS, {"a": a, "b": b, "src": list(DRY_SOURCES)}), rows(TODAY_ORDERS, (a, b)),
                        rows(TODAY_ACTIVE))
    return page(request, "today.html", day=d, prev=d - dt.timedelta(days=1),
                next=d + dt.timedelta(days=1) if b <= dt.datetime.now(dt.timezone.utc) else None, **board,
                markets=rows(TODAY_MARKETS, (list(DRY_SOURCES), a, b))[0]["n"],
                best=rows(TODAY_BEST.format(order="DESC"), (list(DRY_SOURCES), a, b)),
                worst=rows(TODAY_BEST.format(order="ASC"), (list(DRY_SOURCES), a, b)))



# /desk: the lot-size notes, each recent day's best dry run, and the weather bets, on one page. A weather order is
# "extreme" when it bought a side at 90c or more, or 10c or less: the market already called it.
DESK_DAYS = 14
DESK_WEATHER_FAMILIES = ("weather", "observed")
DESK_EXTREME = 0.90
DESK_BEST = """
SELECT DISTINCT ON (d.day) d.day, f.*, m.title, d.pnl, d.closed FROM (
  SELECT (close_date AT TIME ZONE 'UTC')::date AS day, sum(profit_abs) AS pnl, count(*) AS closed FROM fills
  WHERE source = ANY(%s) AND close_date >= %s AND profit_ratio IS NOT NULL GROUP BY 1) d
JOIN fills f ON (f.close_date AT TIME ZONE 'UTC')::date = d.day AND f.source = ANY(%s) AND f.profit_ratio IS NOT NULL
LEFT JOIN markets m ON f.source = 'paper' AND m.venue = f.venue AND m.market_id = f.instrument
ORDER BY d.day DESC, f.profit_ratio DESC, f.fill_id
"""
DESK_WEATHER = """
SELECT o.account, a.family, o.side, o.price, o.contracts, o.stake, o.sent_at, o.settled_at, o.profit, m.title, m.subtitle
FROM paper_orders o JOIN paper_accounts a USING (account)
LEFT JOIN markets m ON m.venue = a.venue AND m.market_id = o.market_id
WHERE a.family = ANY(%s) AND o.contracts > 0 ORDER BY o.sent_at DESC, o.id DESC LIMIT 200
"""


def weather_bets(orders: list[dict]) -> list[dict]:
    """Mark each weather order extreme when its side was bought at DESK_EXTREME or more, or at 1 - DESK_EXTREME or less; extremes first."""
    marked = [{**o, "extreme": o["price"] is not None and (o["price"] >= DESK_EXTREME or o["price"] <= 1 - DESK_EXTREME)}
              for o in orders]
    return sorted(marked, key=lambda o: not o["extreme"])


@app.get("/desk", response_class=HTMLResponse)
def desk(request: Request):
    """Lot-size notes, the best dry run of each recent day and the weather bets, together."""
    user_of(request)
    since = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=DESK_DAYS - 1)
    sources = list(DRY_SOURCES)
    g = glossary.load()
    return page(request, "desk.html", days=DESK_DAYS, extreme=DESK_EXTREME,
                sizing=[t for t in g["trading"] if t["group"] == "Sizing"],
                best=rows(DESK_BEST, (sources, since, sources)),
                weather=weather_bets(rows(DESK_WEATHER, (list(DESK_WEATHER_FAMILIES),))))


def fills_today(source: str, venues: list[str] | None = None) -> int:
    """Fills a league opened today (UTC), for the count that links to /today; `venues` narrows a paper source."""
    start = dt.datetime.combine(dt.datetime.now(dt.timezone.utc).date(), dt.time(), dt.timezone.utc)
    return rows("SELECT count(*) AS n FROM fills WHERE source = %s AND open_date >= %s AND (%s::text[] IS NULL OR venue = ANY(%s))",
                (source, start, venues, venues))[0]["n"]


@app.get("/leagues", response_class=HTMLResponse)
def leagues(request: Request):
    """How the paper accounts are doing: one scorecard, the same columns for every league; the day's fills are /today."""
    user_of(request)
    methods = suggest.load_config(FAMILIES)
    watching = {w["bot"]: w for w in rows(KILL_SWITCH)}
    profits = {r["account"]: r["profits"] for r in rows(PAPER_PROFITS)}
    crypto = league_rows.crypto_rows(
        rows(BOTS), watching, lambda s: f"{s}: {strategy_method(s, methods)['description']}")
    accounts = paper_accounts()
    events = league_rows.event_rows(accounts, profits, paper_lab.rule_of)
    stocks = league_rows.event_rows(rows(PAPER_ACCOUNTS, ([stocks_lab.VENUE],)), profits,
                                    stocks_lab.rule_of)
    return page(request, "leagues.html", glossary=glossary.load(),
                tabs=[{"id": "crypto", "title": "Crypto", "rows": crypto, "fills": fills_today("freqtrade"),
                       "about": "Freqtrade spot bots on live prices (lab/crypto/league.py), grouped into cohorts."},
                      {"id": "event-contracts", "title": "Event contracts", "rows": events,
                       "fills": fills_today("paper", paper_venues()),
                       "forward": [a for a in accounts if a["drifted"] is not None],
                       "about": "One account per family variant, filled against the recorded books (lab/events/paper.py)."},
                      {"id": "stocks", "title": "Stocks", "rows": stocks, "fills": fills_today("paper", [stocks_lab.VENUE]),
                       "about": "One account per family variant on a small ETF universe, filled at Alpaca's latest "
                                "quote with no order sent (lab/stocks/paper.py)."}],
                hard_stop=suggest.load_config()["book"]["hard_stop"])


@app.get("/live", response_class=HTMLResponse)
def live(request: Request):
    user_of(request)
    return page(request, "live.html", **live_view())


@app.get("/live/rows", response_class=HTMLResponse)
def live_rows(request: Request):
    user_of(request)
    return page(request, "_live.html", **live_view())


@app.get("/bots/{name}", response_class=HTMLResponse)
def bot(request: Request, name: str):
    user_of(request)
    found = rows(BOT, (name,))
    if not found:
        raise HTTPException(404, f"No bot named {name}")
    series = rows(BOT_SERIES, (name,))
    trades = rows("SELECT * FROM bot_trades WHERE bot = %s ORDER BY is_open DESC, "
                  "coalesce(close_date, open_date) DESC LIMIT 500", (name,))
    chart = charts.line([(r["ts"].astimezone(TZ), r["profit_all_pct"]) for r in series], label=f"{name} profit over time")
    return page(request, "bot.html", bot=found[0], trades=trades, chart=chart,
                drift=(rows("SELECT * FROM drift WHERE bot = %s", (name,)) or [None])[0])


# One fill or trade of any league with everything recorded about it (plan recorder-links-vivid-stream P2): the
# `fills` view row is the key; a freqtrade trade id is per bot, so its URL carries ?bot=.
TRADE_SOURCES = ("paper", "freqtrade", "drill", "call")
FILL = "SELECT * FROM fills WHERE source = %s AND fill_id = %s AND (%s = '' OR account = %s) ORDER BY open_date DESC LIMIT 1"
# A crypto decision is the bot trade's (lab/crypto/ingest.py, migration 057): same bot and open_date.
DECISION = """SELECT * FROM decisions WHERE fill_source = %s AND fill_id = %s AND account = %s
AND (fill_source <> 'freqtrade' OR decided_at = %s)"""
NEAR_MISSES = """SELECT d.instrument, d.checks, m.title FROM decisions d
LEFT JOIN markets m ON m.venue = d.venue AND m.market_id = d.instrument
WHERE d.league = %s AND d.account = %s AND d.decided_at = %s AND d.action = 'near_miss' ORDER BY d.id LIMIT 20"""
FUNNEL = "SELECT reason, n FROM decision_funnel WHERE league = %s AND account = %s AND pass_at = %s ORDER BY n DESC, reason"
PAPER_ORDER = """
SELECT o.*, a.params, a.family, a.venue, m.title, m.subtitle, m.close_time, r.result, r.value AS paid,
       r.settled_at AS resolved_at
FROM paper_orders o JOIN paper_accounts a USING (account)
LEFT JOIN markets m ON m.venue = a.venue AND m.market_id = o.market_id
LEFT JOIN resolutions r ON r.venue = a.venue AND r.market_id = o.market_id
WHERE o.id = %s"""
BOT_TRADE = """SELECT t.bot, t.trade_id, t.open_date, t.pair, t.is_open, t.close_date, t.open_rate, t.close_rate,
       t.amount, t.stake_amount, t.profit_ratio, t.profit_abs, t.exit_reason, t.enter_tag
FROM bot_trades t WHERE t.bot = %s AND t.trade_id = %s AND t.open_date = %s"""
# An exit's decision is keyed by its account, ticker and time: the entry's decision holds the fill key.
STOCK_EXIT = ("SELECT * FROM decisions WHERE league = 'stocks' AND action = 'exit' AND account = %s AND instrument = %s "
              "AND decided_at = %s")
MARKOUTS = "SELECT * FROM markouts WHERE bot = %s AND trade_id = %s AND open_date = %s ORDER BY horizon_min"
RECONCILE = "SELECT * FROM reconcile WHERE bot = %s AND pair = %s AND live_open = %s ORDER BY n"


def near_miss_reasons(near: list[dict]) -> list[tuple[str, int]]:
    """The same pass's near-misses counted by the one check each failed."""
    counts: dict[str, int] = {}
    for d in near:
        d["failed"] = next((c for c in d["checks"] if not c["pass"]), None)
        name = d["failed"]["name"] if d["failed"] else "-"
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


@app.get("/trades/{source}/{fill_id}", response_class=HTMLResponse)
def trade(request: Request, source: str, fill_id: int, bot: str = ""):
    """Why a fill happened and how it went: the decision recorded with it (rule, checks, inputs, sizing), the same
    pass's near-misses and funnel, and per source the order, market and resolution, markouts and reconcile, or scores."""
    user_of(request)
    if source not in TRADE_SOURCES:
        raise HTTPException(404, f"No trade source {source}")
    if source == "freqtrade" and not bot:
        raise HTTPException(404, "A crypto trade id is per bot: add ?bot=<name>")
    found = rows(FILL, (source, fill_id, bot, bot))
    if not found:
        raise HTTPException(404, f"No {source} fill {fill_id}")
    fill = found[0]
    decision = (rows(DECISION, (source, fill_id, fill["account"], fill["open_date"])) or [None])[0]
    near, funnel = [], []
    if decision:
        key = (decision["league"], decision["account"], decision["decided_at"])
        near, funnel = rows(NEAR_MISSES, key), rows(FUNNEL, key)
    ctx: dict = {"fill": fill, "decision": decision, "near": near, "near_reasons": near_miss_reasons(near),
                 "funnel": funnel, "source": source}
    if source == "paper":
        o = rows(PAPER_ORDER, (fill_id,))[0]
        if o["venue"] == stocks_lab.VENUE:
            p = o | {"ticker": o["market_id"], "shares": o["contracts"], "entry_price": o["price"],
                     "entry_cost": o["fee"], "exited_at": o["settled_at"], "exit_price": o["payout"],
                     "exit_cost": o["exit_fee"]}
            ctx |= {"position": p, "exit": (rows(STOCK_EXIT, (p["account"], p["ticker"], p["exited_at"])) or [None])[0]}
        else:
            cost = o["contracts"] * o["price"] + o["fee"]
            ctx |= {"order": o, "cost": cost, "if_win": o["contracts"] - cost, "if_lose": -cost}
    elif source == "freqtrade":
        key = (fill["account"], fill_id, fill["open_date"])
        ctx |= {"bot_trade": rows(BOT_TRADE, key)[0], "markouts": rows(MARKOUTS, key),
                "reconcile": rows(RECONCILE, (fill["account"], fill["instrument"], fill["open_date"]))}
    elif source == "drill":
        ctx["drill"] = rows("SELECT * FROM demo_orders WHERE id = %s", (fill_id,))[0]
    else:
        ctx |= {"call": rows("SELECT * FROM calls WHERE id = %s", (fill_id,))[0],
                "scores": rows("SELECT * FROM call_scores WHERE call_id = %s ORDER BY horizon", (fill_id,))}
    return page(request, "trade.html", **ctx)


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
    dcfg = diversity.config()
    daily = rows("SELECT day, ret FROM strategy_daily WHERE strategy = %s AND exchange = %s ORDER BY day", key)
    regimes = rows(REGIME_SPLIT, key)
    for r in regimes:
        r["sharpe"] = r["mean"] / r["sd"] * math.sqrt(365) if r["sd"] else None
        r["annual"] = (1 + r["mean"]) ** 365 - 1 if r["mean"] is not None and r["mean"] > -1 else None
    return page(
        request, "strategy.html", s=found[0], exchange=exchange, exchanges=exchanges, method=strategy_method(name),
        windows=sorted(rows("SELECT * FROM strategy_windows WHERE strategy = %s AND exchange = %s", key),
                       key=lambda w: WINDOWS.index(w["win"])),
        chart=charts.line(equity_points(daily), label=f"{name} on {exchange} cumulative return", fmt="%Y-%m-%d"),
        estimates=rows("SELECT * FROM estimates WHERE strategy = %s AND exchange = %s ORDER BY horizon_days", key),
        bias=rows("SELECT * FROM bias_checks WHERE strategy = %s ORDER BY kind", (name,)),
        regimes=regimes,
        wf=[(pooled(f), f) for f in walkforward_by_pair("WHERE strategy = %s", (name,)).values()],
        suggestions=rows("SELECT * FROM suggestions WHERE strategy = %s ORDER BY kind, exchange, bot", (name,)),
        correlations=rows("SELECT * FROM correlations WHERE strategy = %s AND exchange = %s ORDER BY abs(rho) DESC", key),
        chunks=rows("SELECT * FROM backtest_chunks WHERE strategy = %s AND exchange = %s AND overlay = '' "
                    "ORDER BY chunk_start DESC", key),
        trades=rows(STRATEGY_TRADES, key),
        card=card(rows("SELECT * FROM strategy_windows WHERE strategy = %s AND exchange = %s AND win = %s",
                       (*key, dcfg["window"])),
                  rows(f"SELECT * FROM ({readiness.CRYPTO_ROWS}) c WHERE strategy = %s AND exchange = %s", key),
                  rows("SELECT cell, lo FROM climate_scorecard WHERE kind = 'strategy' AND name = %s AND exchange = %s",
                       key),
                  walkforward_by_pair("WHERE strategy = %s AND exchange = %s", key).get(key, []), dcfg["payoff_haircut"]))


def card(window: list[dict], conf: list[dict], climates: list[dict], folds: list[dict], haircut: float) -> dict:
    """Pure: the strategy card (docs/strategy-coverage.md, The strategy card) for one strategy x exchange. A field
    with no input is None, and the template says why it is missing. The payoff ratio is the average win over the
    average loss; `haircut` is the per-trade expectancy with every win cut to that share of its size."""
    w = window[0] if window else None
    win, loss, rate = (w.get("avg_win"), w.get("avg_loss"), w["winrate"]) if w else (None, None, None)
    c = conf[0] if conf else None
    earns = {}
    for r in climates:
        if r["lo"] is not None and r["lo"] > 0:
            earns[r["cell"]] = max(earns.get(r["cell"], 0.0), r["lo"])
    return {
        "window": w and w["win"], "cagr": w and w["cagr"], "sharpe_lo": w and w["sharpe_lo"], "sharpe_hi": w and w["sharpe_hi"],
        "expectancy": w and w["expectancy"], "expectancy_lo": w and w["expectancy_lo"],
        "expectancy_hi": w and w["expectancy_hi"], "winrate": rate, "trades": w and w["trades"],
        "wins": w and w.get("wins"), "losses": w and w.get("losses"), "breakeven_winrate": w and w.get("breakeven_winrate"),
        "payoff": win / loss if win is not None and loss else None, "haircut_share": haircut,
        "haircut": rate * haircut * (win or 0.0) - (1 - rate) * (loss or 0.0)
        if rate is not None and (win is not None or loss is not None) else None,
        "per_month": w["trades"] / w["days"] * diversity.DAYS_A_MONTH if w and w["days"] else None,
        "median_hold_hours": w and w.get("median_hold_hours"),
        "climates": sorted(earns, key=CLIMATE_ORDER.index), "climates_tested": bool(climates),
        "oos_months": len({(f["fold_start"].year, f["fold_start"].month) for f in folds if f["ok"]}),
        "passed": c and c["passed"], "gates": c and len(c["gates"] or []), "ready": c and c["ready"],
        "missed": [g["gate"] for g in (c["gates"] or []) if not g["pass"]] if c else []}


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
                ready=c["ready_score"], oos=c["min_oos_trades"], glossary=glossary.load())


@app.get("/confidence", response_class=HTMLResponse)
def confidence(request: Request):
    user_of(request)
    board = rows(CONFIDENCE)
    pop = None if any(r["score"] is not None for r in board) else population(suggest.load_config()["confidence"])
    return page(request, "confidence.html", board=board, pop=pop)


CLIMATES = "SELECT * FROM climate_scorecard ORDER BY cell, kind, name, exchange, variant"
MIXTURES = "SELECT * FROM mixtures ORDER BY baseline DESC, lo DESC NULLS LAST, name LIMIT %s"
CLIMATE_ORDER = [f"{t}/{v}" for t in ("up", "flat", "down") for v in ("low", "mid", "high")]


def climate_table(scores: list[dict]) -> list[dict]:
    """One row per climate: the BTC hold beside the strategies whose interval low end clears zero there."""
    out = []
    for cell in CLIMATE_ORDER:
        mine = [r for r in scores if r["cell"] == cell]
        if not mine:
            continue
        hold = next((r for r in mine if r["kind"] == "benchmark"), None)
        strategies = [r for r in mine if r["kind"] != "benchmark"]
        out.append({"cell": cell, "hold": hold, "tested": sum(r["lo"] is not None for r in strategies),
                    "passing": sorted((r for r in strategies if r["lo"] is not None and r["lo"] > 0), key=lambda r: -r["lo"]),
                    "trials": max(r["trials"] for r in mine)})
    return out


def survivor_venues() -> dict[str, dict]:
    """Venues of the latest analysis whose pair list is not point-in-time, so their results are an upper bound."""
    latest = rows(UNIVERSE)
    return {ex: u for ex, u in (latest[0]["universe"] if latest else {}).items() if u["source"] != universe.POINT_IN_TIME}


@app.get("/book", response_class=HTMLResponse)
def book(request: Request):
    user_of(request)
    cfg = suggest.load_config()
    bands = {(r["strategy"], r["exchange"], r["basis"], r["horizon_months"]): {**r, "after_tax": book_lab.median_after_tax(r, cfg)}
             for r in rows("SELECT * FROM book")}
    board = rows(BOOK)
    return page(request, "book.html", survivors=survivor_venues(), board=board, bands=bands, pop=None if board else population(cfg["confidence"]), budget=book_lab.budget(cfg),
                hurdle=book_lab.hurdle(cfg), yield_=cfg["book"]["tbill_yield"], tax=cfg["book"]["tax_short"],
                mixtures=rows(MIXTURES, (cfg["mixtures"]["show"] + 1,)), climates=climate_table(rows(CLIMATES)),
                vol=rows("SELECT * FROM vol_verdict ORDER BY exchange, baseline"),
                vol_days=cfg["volatility"]["baseline_days"], vol_level=cfg["volatility"]["level"],
                horizon=rows("SELECT * FROM horizon_verdict ORDER BY strategy, exchange, variant, plan"), trend_days=cfg["book"]["trend_days"],
                benchmarks=rows("SELECT * FROM benchmarks ORDER BY end_day - start_day, name"),
                short=cfg["book"]["horizon_months"][0], long=cfg["book"]["horizon_months"][-1],
                stake=cfg["book"]["stages"][0]["stake"], min_days=cfg["scorecard"]["min_days"],
                bankroll=next(iter(rows("SELECT * FROM bankroll")), None))


def strategy_classes() -> set[str]:
    """Every strategy class under user_data/strategies, subfolders included."""
    return {name for p in STRATEGY_DIR.rglob("*.py") for name in STRATEGY_CLASS.findall(p.read_text(encoding="utf-8"))}


def coverage_board(tags: dict, families: list[str], on_disk: set[str], found: dict[str, list[dict]]) -> dict:
    """The family x exchange matrix and one row per strategy, from lab/crypto/families.toml and the COVER results.

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
def coverage(request: Request, signal_source: str = ""):
    user_of(request)
    toml = taxonomy.load(FAMILIES)
    if signal_source and signal_source not in toml["signal_sources"]:
        raise HTTPException(404, "Unknown signal source")
    tags = {n: {**t, **toml["family_categories"][t["family"]]} for n, t in toml["strategies"].items()}
    board = coverage_board(tags, list(toml["families"]), strategy_classes(),
                           {kind: rows(sql) for kind, sql in COVER.items()})
    if signal_source:
        board["strategies"] = [s for s in board["strategies"] if s.get("signal_source") == signal_source]
        board["matrix"] = [f for f in board["matrix"]
                           if f["family"] in toml["family_categories"]
                           and toml["family_categories"][f["family"]]["signal_source"] == signal_source]
    dcfg = diversity.config()
    family = {n: t["family"] for n, t in toml["strategies"].items()}
    found = rows(diversity.ROWS, (dcfg["window"], dcfg["min_trades"]))
    if signal_source:
        found = [r for r in found if tags.get(r["strategy"], {}).get("signal_source") == signal_source]
    clusters = [] if signal_source else diversity.cluster_table(rows("SELECT * FROM residual_clusters"), rows("SELECT * FROM residual_venues"),
                                       family, {(r["strategy"], r["exchange"]): r["sharpe_lo"] for r in found})
    archive = diversity.build(found, rows(diversity.CLIMATE_ROWS), family, dcfg)
    return page(request, "coverage.html", signal_source=signal_source, stage_matrix=stage_coverage.matrix(), signal_sources=toml["signal_sources"],
                family_categories=toml["family_categories"], untagged=UNTAGGED, **board, archive=archive, clusters=clusters, families={
        **toml["families"], UNTAGGED: "On disk, no line in lab/crypto/families.toml.", GONE: "In the results only, not on disk."})


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
    failed = rows("SELECT strategy, exchange, overlay, chunk_start, attempts, error FROM backtest_chunks "
                  "WHERE run_id = %s AND NOT ok ORDER BY strategy, exchange, chunk_start", (run_id,))
    return page(request, "run.html", run=found[0], failed=failed)


def sweep_ages() -> tuple[int, dict[str, int]]:
    return market_sweep_ages(MARKETS)


@app.get("/markets", response_class=HTMLResponse)
def markets(request: Request):
    user_of(request)
    max_age, ages = sweep_ages()
    return page(request, "markets.html", venues=rows(MARKET_VENUES), errors=rows(SWEEP_ERRORS), backfill=rows(BACKFILL),
                max_age=max_age, ages=ages)


@app.get("/links", response_class=HTMLResponse)
def links(request: Request, status: str = ""):
    user_of(request)
    if status and status not in LINK_STATUSES:
        raise HTTPException(404, f"No link status {status}")
    return page(request, "links.html", status=status, statuses=LINK_STATUSES,
                summary=rows(LINK_SUMMARY, (list(LINK_STATUSES),)), links=rows(LINKS, (status, status)),
                asks=rows(LINK_ASKS))


@app.get("/markets/book", response_class=HTMLResponse)
def markets_book(request: Request, signal_source: str = ""):
    user_of(request)
    tax = taxonomy.load()
    if signal_source and signal_source not in tax["signal_sources"]:
        raise HTTPException(404, "Unknown signal source")
    def selected(found):
        return [r for r in found if not signal_source
                or tax["market_categories"].get(r["family"], {}).get("signal_source") == signal_source]
    return page(request, "markets_book.html", backtests=selected(rows(MARKET_BACKTESTS)), readiness=selected(rows(MARKET_READINESS)),
                categories=selected(rows(MARKET_CATEGORIES)), signal_source=signal_source, signal_sources=tax["signal_sources"],
                family_categories=tax["market_categories"], demo=rows(DEMO_ORDERS))


# Every call newest first with its scores by horizon (lab/inputs/calls.py), and the scored rows groups() summarizes.
CALLS = """
SELECT c.*, coalesce(jsonb_object_agg(s.horizon, jsonb_build_object('exit', s.exit_session, 'pnl', s.pnl,
       'excess', s.excess)) FILTER (WHERE s.call_id IS NOT NULL), '{}') AS scores
FROM calls c LEFT JOIN call_scores s ON s.call_id = c.id GROUP BY c.id ORDER BY c.created_at DESC, c.id DESC
"""


@app.get("/calls", response_class=HTMLResponse)
def calls(request: Request):
    """Every trade idea recorded before its outcome, each horizon's score once its exit close has printed, and the
    record per who, signal source, instrument and horizon, and each event rule's record against every rule trial."""
    trials = rows("SELECT count(*) AS n FROM trials WHERE exchange = %s", (call_rules.EXCHANGE,))[0]["n"]
    return page(request, "calls.html", calls=rows(CALLS), groups=calls_lab.groups(rows(calls_lab.SCORED)),
                rules=call_rules.records(rows(call_rules.RULE_SCORES), trials), rule_trials=trials)


@app.get("/accounts", response_class=HTMLResponse)
def accounts(request: Request):
    """Every account we hold or paper-trade, read only: the newest read of each, and the watcher's kill-switch state."""
    user = user_of(request)
    brokers, wallets, bots, paper = rows(BROKERS), rows(WALLETS), rows(BOTS), paper_accounts()
    refs = [{"id": f"broker:{b['broker']}", "label": f"Broker: {b['broker']}"} for b in brokers]
    refs += [{"id": f"wallet:{w['coin']}:{w['account']}", "label": f"Wallet: {w['account']} ({w['coin']})"} for w in wallets]
    refs += [{"id": f"bot:{b['name']}", "label": f"Bot: {b['name']}"} for b in bots]
    refs += [{"id": f"paper:{a['account']}", "label": f"Paper: {a['account']}"} for a in paper]
    catalog = research_catalog()
    methods = suggest.load_config(FAMILIES)
    research = {"user": user, "accounts": refs,
                "strategies": [{"name": name, **strategy_method(name, methods), "venues": venues}
                               for name, venues in catalog.items()]}
    counts = {(r["cohort"], r["state"]): r["n"] for r in rows(SHADOW_COUNTS)}
    shadow = {"rows": rows(SHADOW), "active": {c: counts.get((c, "trial"), 0) + counts.get((c, "winning"), 0)
                                               for c in ("short", "long")},
              "winning": sum(n for (_, s), n in counts.items() if s == "winning"),
              "retired": sum(n for (_, s), n in counts.items() if s == "retired")}
    return page(request, "accounts.html", guard_lifted=guard_lab.GUARD_LIFTED, brokers=brokers, wallets=wallets,
                watch=rows(KILL_SWITCH), bots=bots, paper_accounts=paper, research=research, shadow=shadow,
                venues=venue_roster.rows(rows), stale_before=dt.datetime.now(dt.timezone.utc) - STALE)


def strategy_method(name: str, config: dict | None = None) -> dict:
    config = config if config is not None else suggest.load_config(FAMILIES)
    tag = config["strategies"].get(name, {})
    family = tag.get("family", "Not classified")
    return {"family": family, "description": config["families"].get(family, "Not classified"),
            "inputs": tag.get("inputs", []), "note": tag.get("note", "")}


def research_catalog() -> dict[str, list[str]]:
    """Known strategy/venue pairs, including bots waiting for their first backtest."""
    found = rows("SELECT s.name, p.exchange FROM strategies s LEFT JOIN ("
                 "SELECT strategy, exchange FROM strategy_windows UNION "
                 "SELECT strategy, exchange FROM backtest_chunks UNION "
                 "SELECT strategy, exchange FROM bots) p ON p.strategy = s.name ORDER BY s.name, p.exchange")
    catalog: dict[str, list[str]] = {}
    for r in found:
        venues = catalog.setdefault(r["name"], [])
        if r["exchange"]:
            venues.append(r["exchange"])
    return catalog


@app.get("/accounts/evidence")
def account_evidence(request: Request, strategy: str, exchange: str):
    user_of(request)
    if exchange not in research_catalog().get(strategy, []):
        raise HTTPException(404, "Unknown strategy and venue. Choose a pair listed on Accounts.")
    result = rows("SELECT start_day, end_day, days, trades, total_return, max_drawdown, computed_at "
                  "FROM strategy_windows WHERE strategy = %s AND exchange = %s AND win = '365d'", (strategy, exchange))
    checks = rows("SELECT * FROM (" + BIAS + ") b WHERE strategy = %s", (strategy,))
    bias = checks[0]["biased"] if checks else None
    unstable = checks[0]["unstable"] if checks else None
    confidence = rows(f"SELECT ready FROM ({readiness.CRYPTO_ROWS}) c WHERE strategy = %s AND exchange = %s", (strategy, exchange))
    return {"strategy": strategy, "exchange": exchange, "window": "365d", "available": bool(result),
            "start_day": None, "end_day": None, "days": None, "trades": None, "total_return": None,
            "max_drawdown": None, "computed_at": None, **(result[0] if result else {}),
            "bias": "not checked" if bias is None else "biased" if bias else "clean",
            "stability": "not checked" if unstable is None else "unstable" if unstable else "stable",
            "confidence": ("ready" if confidence[0]["ready"] else "not ready") if confidence else "not checked",
            **strategy_method(strategy)}


@app.get("/avenues", response_class=HTMLResponse)
def avenues(request: Request):
    user_of(request)
    lines = (HERE.parent / "docs" / "avenues.md").read_text().splitlines()
    table = [[cell.strip() for cell in line.strip().strip("|").split("|")]
             for line in lines if line.startswith("|")]
    avenues = []
    for row in table[2:]:
        evidence = []
        for part in re.split(r"(\[[^]]+\]\([^)]+\))", row[3]):
            link = re.fullmatch(r"\[([^]]+)\]\(([^)]+)\)", part)
            evidence.append({"text": link[1] if link else part,
                             "url": urljoin("https://github.com/Kblack0610/freqtrade_trading_attempt/blob/master/docs/",
                                            link[2]) if link else None})
        avenues.append({"cells": row, "evidence": evidence})
    return page(request, "avenues.html", avenues=avenues)
