"""The trader site: a read-only view of the league and the lab, from the trader database.

Every page needs a Forgejo sign-in (web/auth.py) except /healthz. The site only reads: its
database role has SELECT grants and nothing else, and there are no trade controls.

Run: uvicorn web.app:app (env: DATABASE_URL plus the web/auth.py Config variables).
"""

import datetime as dt
import math
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lab"))
from db import connect  # noqa: E402

from web import auth, charts  # noqa: E402

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo(os.environ.get("TRADER_WEB_TZ", "UTC"))
STALE = dt.timedelta(hours=2)
TF_ORDER = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

BOTS = """
SELECT b.name, b.exchange, b.strategy, s.ts, s.dry_run, s.closed_trades, s.open_trades,
       s.profit_all_pct, s.profit_all_abs, s.winrate_pct, s.max_drawdown_pct, s.balance_total, s.error
FROM bots b
LEFT JOIN LATERAL (SELECT * FROM bot_snapshots WHERE bot = b.name ORDER BY ts DESC LIMIT 1) s ON true
ORDER BY s.profit_all_pct DESC NULLS LAST, b.name
"""
COVERAGE_BY_EXCHANGE = """
SELECT exchange, count(*) AS series, count(*) FILTER (WHERE oldest_reached) AS at_oldest,
       min(first_ts) AS first_ts, max(last_ts) AS last_ts
FROM data_coverage GROUP BY exchange ORDER BY exchange
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
    ("watch", "Watch", "Lookahead-clean candidates one gate short of promotion, best Sharpe interval first."),
]
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
REGIME_SPLIT = """
SELECT coalesce(r.trend, 'any') AS trend, coalesce(r.vol_bucket, 'any') AS vol, count(*) AS days, sum(d.trades) AS trades,
       avg(d.ret) AS mean, stddev_samp(d.ret) AS sd
FROM strategy_daily d JOIN regimes r USING (day)
WHERE d.strategy = %s AND d.exchange = %s AND r.trend IS NOT NULL
GROUP BY GROUPING SETS ((r.trend), (r.vol_bucket)) ORDER BY 1, array_position(ARRAY['low', 'mid', 'high', 'any'], coalesce(r.vol_bucket, 'any'))
"""

cfg = auth.Config()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg.check()
    yield


app = FastAPI(title="trader", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


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


def stale(t: dt.datetime | None) -> bool:
    return t is None or dt.datetime.now(dt.timezone.utc) - t > STALE


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


templates.env.filters.update(local=local, ago=ago, num=num, pct=pct, tone=tone, gv=gv)
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
    with connect() as c, c.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


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
    return page(request, "bot.html", bot=found[0], trades=trades, chart=chart)


@app.get("/data", response_class=HTMLResponse)
def data(request: Request):
    user_of(request)
    return page(request, "data.html", by_exchange=rows(COVERAGE_BY_EXCHANGE), series=rows(COVERAGE, (TF_ORDER,)))


@app.get("/strategies", response_class=HTMLResponse)
def strategies(request: Request, win: str = "365d"):
    user_of(request)
    if win not in WINDOWS:
        raise HTTPException(404, f"No window {win}")
    last = (rows(RUNS) or [None])[0]
    return page(request, "strategies.html", win=win, windows=WINDOWS, board=rows(LEADERBOARD, (win,)),
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
        suggestions=rows("SELECT * FROM suggestions WHERE strategy = %s ORDER BY kind, exchange, bot", (name,)),
        correlations=rows("SELECT * FROM correlations WHERE strategy = %s AND exchange = %s ORDER BY abs(rho) DESC", key),
        chunks=rows("SELECT * FROM backtest_chunks WHERE strategy = %s AND exchange = %s ORDER BY chunk_start DESC", key),
        trades=rows("SELECT t.* FROM backtest_trades t JOIN backtest_chunks c ON c.id = t.chunk_id "
                    "WHERE c.strategy = %s AND c.exchange = %s ORDER BY t.close_date DESC LIMIT 100", key))


@app.get("/suggestions", response_class=HTMLResponse)
def suggestions(request: Request):
    user_of(request)
    by_kind = {k: [] for k, _, _ in SECTIONS}
    for r in rows(SUGGESTIONS):
        by_kind.setdefault(r["kind"], []).append(r)
    return page(request, "suggestions.html", sections=SECTIONS, by_kind=by_kind)


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
