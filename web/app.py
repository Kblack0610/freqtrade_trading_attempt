"""The trader site: a read-only view of the league and the lab, from the trader database.

Every page needs a Forgejo sign-in (web/auth.py) except /healthz. The site only reads: its
database role has SELECT grants and nothing else, and there are no trade controls.

Run: uvicorn web.app:app (env: DATABASE_URL plus the web/auth.py Config variables).
"""

import datetime as dt
import os
from contextlib import asynccontextmanager
import sys
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
SELECT r.id, r.kind, r.name, r.imported_at,
       (SELECT count(*) FROM backtests WHERE run_id = r.id) AS backtests,
       (SELECT count(*) FROM backtests WHERE run_id = r.id AND profit_pct > 0) AS profitable,
       (SELECT count(*) FROM bias_checks WHERE run_id = r.id AND has_bias) AS biased,
       (SELECT count(*) FROM hyperopt_runs WHERE run_id = r.id) AS hyperopts
FROM runs r ORDER BY r.imported_at DESC
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


templates.env.filters.update(local=local, ago=ago, num=num)
templates.env.globals.update(stale=stale, tz=str(TZ))


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
                coverage=rows(COVERAGE_BY_EXCHANGE), runs=rows(RUNS)[:3])


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
    backtests = rows("SELECT * FROM backtests WHERE run_id = %s ORDER BY error IS NOT NULL, profit_pct DESC NULLS LAST",
                     (run_id,))
    bias = rows("SELECT * FROM bias_checks WHERE run_id = %s ORDER BY has_bias DESC, strategy, kind", (run_id,))
    return page(request, "run.html", run=found[0], backtests=backtests, bias=bias)
