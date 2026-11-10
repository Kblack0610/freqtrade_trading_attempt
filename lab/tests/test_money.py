"""Money goes to the right places: the one suite over every path that could move, count or reach money.

1. Ledger: paper money adds up. Each fill's cost and fee leave cash once, a settlement pays what the backtest pays
   and charges its fee once, a second settle pass changes nothing, and an order the cash cannot cover is refused.
2. Egress: every outbound client in lab/ and web/ is on MONEY_MAP, and sends only where its row says. A client that
   can write sends only to a paper, demo or play-money host; a client on a real account sends GET only.
3. Refusals: the shipped configs and the league entry point refuse real money. lab/live.py's guard and check() are
   proved in test_live.py.

A new money path (the money bank of tr-148, a live exchange client) joins here: the egress scan fails until it has
a MONEY_MAP row, and a kind that writes real money does not exist yet, so adding one is a reviewed change to this file.
"""

import ast
import base64
import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import pandas as pd
import pytest
from test_broker import RH, SEED, ST, Paper, Robinhood, Snap
from test_markets_kalshi_trade import RSA, Demo, pem
from test_markets_manifold_trade import Manifold

from lab import broker, league
from lab.markets import backtest, fees, kalshi_trade, manifold_trade, paper
from lab.markets.backtest import Market, Order
from lab.markets.recorder import load_config

REPO = Path(__file__).resolve().parents[2]
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 10, 12, tzinfo=UTC)
CREDENTIALS = ("key", "secret", "password", "uid", "private_key", "walletAddress")

# 1. Ledger ----------------------------------------------------------------------------------------------------------


class Script:
    """A strategy that sends one fixed order per market, once: the ledger is the paper broker's, not a family's."""

    name, params, cutoff = "script", {}, None

    def __init__(self, orders: dict[str, Order]):
        self.orders, self.seen = orders, set()

    def on_book(self, row, market):
        o = self.orders.get(row.market_id)
        return [o] if o and row.market_id not in self.seen else []


# market: (bid, ask, ask_size, order, resolution (result, value) or None). Every book has bid_size 100, so a no order
# meets the bid side's depth; take() allows depth_share of each side.
BOOKS = {
    "Y": (0.38, 0.40, 100, Order("Y", "yes", 10), ("yes", 1.0)),     # 25 yes at 0.40, wins
    "N": (0.70, 0.72, 100, Order("N", "no", 6), ("yes", 1.0)),       # 20 no at 0.30, loses
    "V": (0.48, 0.50, 100, Order("V", "yes", 5), ("void", None)),    # 10 yes at 0.50, voided: the price back
    "U": (0.18, 0.20, 100, Order("U", "yes", 2), ("unknown", None)),  # 10 yes at 0.20, a result payout() cannot read
    "X": (0.88, 0.90, 200, Order("X", "yes", 90), None),             # 100 yes at 0.90 costs more than the cash left
}
CAPITAL = 100.0
ACCOUNT = "paper-script-ledger"


@dataclasses.dataclass(frozen=True)
class Venue:
    name: str
    fees: dict[str, float]     # entry fee per filled market
    profits: dict[str, float]  # profit per settled market, net of both fees
    equity_open: float         # after the fills, every position at its exit price
    equity_settled: float      # after Y, N and V settle; U stays open


# Each number by hand from the fee schedule in lab/markets.toml, so a change to fees.py or paper.py cannot move both
# sides of an assertion. Kalshi: ceil(0.07 C P (1 - P)) to the cent on entry. PredictIt: 10% of a win, at settlement.
VENUES = [
    Venue("kalshi", {"Y": 0.42, "N": 0.30, "V": 0.18, "U": 0.12}, {"Y": 14.58, "N": -6.30, "V": -0.18},
          equity_open=97.68, equity_settled=107.78),
    Venue("predictit", {"Y": 0.0, "N": 0.0, "V": 0.0, "U": 0.0}, {"Y": 13.50, "N": -6.00, "V": 0.0},
          equity_open=98.70, equity_settled=107.30),
]


def seed_ledger(conn, venue: str) -> list:
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES (%s, %s, 'exchange')", (venue, venue))
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at) "
                 "VALUES (%s, 'script', %s, 'ledger', '{}', %s, %s)", (ACCOUNT, venue, CAPITAL, NOW))
    book = []
    for mid, (bid, ask, ask_size, _, _) in BOOKS.items():
        conn.execute("INSERT INTO markets (venue, market_id, title, market_type, status, close_time, first_seen, "
                     "updated_at) VALUES (%s, %s, %s, 'binary', 'active', %s, %s, %s)",
                     (venue, mid, mid, NOW + dt.timedelta(hours=12), NOW, NOW))
        conn.execute("INSERT INTO outcomes (venue, market_id, outcome, bid, ask, bid_size, ask_size, quoted_at) "
                     "VALUES (%s, %s, 'yes', %s, %s, 100, %s, %s)", (venue, mid, bid, ask, ask_size, NOW))
        row = paper.Row(mid, pd.Timestamp(NOW), bid, ask, ask_size, 100)
        book.append((row, Market(mid, None, None, mid, None, "binary", NOW, NOW + dt.timedelta(hours=12))))
    return book


def resolve(conn, venue: str, at: dt.datetime) -> None:
    for mid, (*_, res) in BOOKS.items():
        if res:
            conn.execute("INSERT INTO resolutions (venue, market_id, result, value, settled_at) VALUES (%s, %s, %s, %s, %s)",
                         (venue, mid, res[0], res[1], at))


def fills(conn) -> dict:
    rows = conn.execute("SELECT market_id, side, contracts, price, fee, settled_at, payout, profit FROM paper_orders "
                        "WHERE account = %s", (ACCOUNT,)).fetchall()
    return {r[0]: r[1:] for r in rows}


def account(conn) -> tuple:
    return conn.execute("SELECT equity, realized, open_positions FROM paper_accounts WHERE account = %s",
                        (ACCOUNT,)).fetchone()


def money_in(f: dict) -> float:
    """What the open fills hold, cost plus entry fee: what the cash no longer has."""
    return sum(n * p + fee for _, n, p, fee, settled, *_ in f.values() if settled is None and n > 0)


@pytest.mark.parametrize("v", VENUES, ids=[v.name for v in VENUES])
def test_paper_money_adds_up_through_fills_fees_and_settlement(conn, v):
    fee_cfg = load_config()["backtest"]["fees"].get(v.name)
    book = seed_ledger(conn, v.name)

    # pass 1: four fills; X would cost more than the cash those four left, so it is refused and not stored
    out = paper.trade(conn, ACCOUNT, Script({m: b[3] for m, b in BOOKS.items()}), book, CAPITAL, 0.5, fee_cfg, NOW)
    paper.mark(conn, v.name, NOW)
    assert out == {"orders": 4, "fills": 4, "no_cash": 1}
    f = fills(conn)
    assert {m: (r[0], r[1], r[2]) for m, r in f.items()} == {
        "Y": ("yes", 25, pytest.approx(0.40)), "N": ("no", 20, pytest.approx(0.30)),
        "V": ("yes", 10, pytest.approx(0.50)), "U": ("yes", 10, pytest.approx(0.20))}
    assert {m: r[3] for m, r in f.items()} == pytest.approx(v.fees)
    cost = sum(n * p for _, n, p, *_ in f.values())
    assert paper.cash(conn, ACCOUNT, CAPITAL) == pytest.approx(CAPITAL - cost - sum(v.fees.values()))
    assert account(conn) == (pytest.approx(v.equity_open), 0, 4)

    # pass 2: Y wins, N loses, V is void, U has a result payout() cannot read and stays open
    settled_at = NOW + dt.timedelta(hours=12)
    resolve(conn, v.name, settled_at)
    assert paper.settle(conn, v.name, fee_cfg) == 3
    paper.mark(conn, v.name, settled_at)
    f = fills(conn)
    assert {m: r[6] for m, r in f.items() if r[4] is not None} == pytest.approx(v.profits)
    assert f["U"][4:] == (None, None, None)
    realized = sum(v.profits.values())
    # the entry fee leaves cash once: in what an open fill holds, then inside its profit, never both
    assert paper.cash(conn, ACCOUNT, CAPITAL) == pytest.approx(CAPITAL + realized - money_in(f))
    assert money_in(f) == pytest.approx(10 * 0.20 + v.fees["U"])
    assert account(conn) == (pytest.approx(v.equity_settled), pytest.approx(realized), 1)

    # each paper settlement is the backtest's settlement of the same fill, to the cent
    frame = pd.DataFrame([{"market_id": m, "side": r[0], "open_date": pd.Timestamp(NOW), "contracts": r[1],
                           "price": r[2], "fee": r[3]} for m, r in f.items() if r[4] is not None])
    res = {m: backtest.Resolution(m, *BOOKS[m][4], settled_at) for m in frame.market_id}
    expect = backtest.settle(frame, res, fee_cfg).set_index("market_id")
    for m, r in f.items():
        if r[4] is not None:
            assert (r[5], r[6]) == (pytest.approx(expect.close_rate[m]), pytest.approx(expect.profit_abs[m]))

    # pass 3: nothing settles twice and no number moves
    before = (fills(conn), account(conn))
    assert paper.settle(conn, v.name, fee_cfg) == 0
    paper.mark(conn, v.name, settled_at + dt.timedelta(minutes=5))
    assert (fills(conn), account(conn)) == before


def test_a_settlement_fee_is_charged_on_a_win_only_and_on_the_profit():
    pi = load_config()["backtest"]["fees"]["predictit"]
    assert fees.entry(pi, 25, 0.40) == 0
    assert fees.settlement(pi, cost=10.0, payout=25.0) == pytest.approx(1.50)
    assert fees.settlement(pi, cost=6.0, payout=0.0) == 0
    assert fees.settlement(pi, cost=5.0, payout=5.0) == 0


def test_the_paper_league_runs_only_on_its_paper_venues(monkeypatch):
    monkeypatch.setattr(paper, "connect", lambda **k: pytest.fail("connected for a refused venue"))
    with pytest.raises(SystemExit, match="is not a paper venue"):
        paper.main(["--venue", "predictit"])


# 2. Egress ----------------------------------------------------------------------------------------------------------

# What a client may do with money. There is no kind that writes to a real account: the money bank or a live client
# adds one here, in the same PR as the guard that gates it (docs/live.md), never by widening "paper".
KINDS = {
    "paper": "paper, demo or play money: may write, only to its pinned hosts",
    "read": "a real account: GET only, only to its pinned hosts",
    "none": "no account and no money credential: public data or our own services",
}


@dataclasses.dataclass(frozen=True)
class Client:
    kind: str
    hosts: frozenset[str] = frozenset()      # pinned here, apart from the client's own allowlist
    declared: Callable | None = None         # () -> the client's own allowlist, which must equal hosts
    cfg: Callable | None = None              # () -> its shipped config
    make: Callable | None = None             # (cfg, session) -> the client
    drive: Callable | None = None            # (client, conn) -> run its real workflow
    fake: Callable | None = None             # () -> the venue fake the session answers from
    why: str = ""

    def build(self, session, **cfg):
        return self.make({**self.cfg(), **cfg}, session)


def _demo(name):
    return lambda: load_config()["demo"][name]


def _broker(name):
    return lambda: broker.tomllib.loads(broker.CONFIG.read_text())[name]


# Every place lab/ and web/ open a connection, keyed (file, owning class or function). The egress scan below finds
# them; a row without a site, or a site without a row, fails.
MONEY_MAP = {
    ("lab/broker.py", "AlpacaPaper"): Client(
        "paper", frozenset({"paper-api.alpaca.markets"}), lambda: {broker.PAPER_HOST}, _broker("alpaca"),
        lambda cfg, s: broker.AlpacaPaper(cfg, "k", "s", s, sleep=lambda _: None),
        lambda c, conn: (broker.read(c), broker.rest_and_cancel(conn, c, "SPY", 1.0)), Paper),
    ("lab/markets/kalshi_trade.py", "KalshiDemo"): Client(
        "paper", frozenset({"external-api.demo.kalshi.co", "demo-api.kalshi.co"}), lambda: kalshi_trade.DEMO_HOSTS,
        _demo("kalshi"), lambda cfg, s: kalshi_trade.KalshiDemo(cfg, "k", pem(RSA), s, sleep=lambda _: None),
        lambda c, conn: kalshi_trade.drill(conn, c, 0.01), Demo),
    ("lab/markets/manifold_trade.py", "ManifoldBets"): Client(
        "paper", frozenset({"api.manifold.markets"}), lambda: manifold_trade.MANIFOLD_HOSTS, _demo("manifold"),
        lambda cfg, s: manifold_trade.ManifoldBets(cfg, "k", s),
        lambda c, conn: (c.balance(), manifold_trade.drill(conn, c, 0.01, 0.10)), Manifold),
    ("lab/broker.py", "RobinhoodCrypto"): Client(
        "read", frozenset({"trading.robinhood.com"}), lambda: {broker.RobinhoodCrypto.host}, _broker("robinhood"),
        lambda cfg, s: broker.RobinhoodCrypto(cfg, "k", base64.b64encode(SEED).decode(), s), lambda c, conn: c.read(),
        Robinhood),
    ("lab/broker.py", "SnapTrade"): Client(
        "read", frozenset({"api.snaptrade.com"}), lambda: {broker.SnapTrade.host}, _broker("snaptrade"),
        lambda cfg, s: broker.SnapTrade(cfg, "c", "k", s), lambda c, conn: c.read(), Snap),
    ("lab/markets/api.py", "Api"): Client("none", why="venue readers and the Trezor watch (lab/wallet.py): GET only, no key"),
    ("lab/markets/links.py", "Llm"): Client("none", why="our LiteLLM gateway: market text in, no account"),
    ("lab/markets/research/kalshi_calibration.py", "<module>"): Client("none", why="Kalshi public market data"),
    ("lab/ingest.py", "http_fetch"): Client("none", why="our dry-run bots' API; its one POST is stopentry"),
    ("lab/healthcheck.py", "healthy"): Client("none", why="our own pod's health endpoint"),
    ("lab/archive.py", "RangeFile"): Client("none", why="public candle archives"),
    ("lab/futures.py", "Ccxt"): Client("none", why="public funding and open interest through ccxt, no key"),
    ("lab/backfill.py", "Cli"): Client("none", why="public candles through ccxt, no key"),
    ("lab/pairs.py", "main"): Client("none", why="public tickers through ccxt, no key"),
    ("web/auth.py", "login_for_code"): Client("none", why="Forgejo sign-in for the read-only site"),
}
TRADERS = {k: c for k, c in MONEY_MAP.items() if c.kind != "none"}
NET = {"requests.Session", "requests.request", "requests.get", "requests.post", "requests.put", "requests.patch",
       "requests.delete", "urllib.request.urlopen", "httpx.Client", "httpx.AsyncClient", "httpx.get", "httpx.post"}


def dotted(node) -> str:
    return f"{dotted(node.value)}.{node.attr}" if isinstance(node, ast.Attribute) else getattr(node, "id", "")


def net_calls(path: Path):
    """(owner, call node) for each call in the file that opens a connection: requests, urllib, httpx or a ccxt client."""
    tree = ast.parse(path.read_text())
    owners: dict[int, str] = {}

    def walk(node, owner):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, ast.ClassDef) else owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner == "<module>":
                inner = child.name
            owners[id(child)] = inner
            walk(child, inner)

    walk(tree, "<module>")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted(node.func)
        ccxt = (name == "getattr" and node.args and dotted(node.args[0]) == "ccxt") or \
            (name.startswith("ccxt.") and name.split(".")[1][:1].islower())
        if name in NET or ccxt:
            yield owners[id(node)], node


def sites() -> dict:
    out: dict = {}
    for top in ("lab", "web"):
        for p in sorted((REPO / top).rglob("*.py")):
            if "tests" in p.parts:
                continue
            for owner, node in net_calls(p):
                out.setdefault((str(p.relative_to(REPO)), owner), []).append(node)
    return out


def test_every_outbound_client_is_on_the_money_map():
    found = sites()
    assert sorted(set(found) - set(MONEY_MAP)) == [], "a new outbound client: give it a MONEY_MAP row and a kind"
    assert sorted(set(MONEY_MAP) - set(found)) == [], "a MONEY_MAP row whose client is gone: remove the row"
    for key, c in MONEY_MAP.items():
        assert c.kind in KINDS, f"{key}: kind {c.kind!r} is not in KINDS"
        if c.kind == "none":
            assert c.why, f"{key}: say why it touches no money"
        else:
            assert all((c.hosts, c.declared, c.cfg, c.make, c.drive, c.fake)), f"{key}: an account client needs every field"


def test_a_client_with_no_account_carries_no_credential():
    for (path, owner), nodes in sites().items():
        if (row := MONEY_MAP.get((path, owner))) is None or row.kind != "none":
            continue  # an unmapped site fails the map test; an account client is driven below
        for node in nodes:
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                if isinstance(arg, ast.Dict):
                    keys = {k.value for k in arg.keys if isinstance(k, ast.Constant)}
                    assert not keys & {"apiKey", "secret", "password", "uid", "privateKey", "walletAddress"}, \
                        f"{path} {owner} passes a credential to {dotted(node.func)}"


class Recorder:
    """A session that forwards to a venue fake and records the method and host of every request."""

    def __init__(self, fake):
        self.fake, self.sent = fake, []

    def request(self, method, url, **kw):
        self.sent.append((method, urlparse(url).hostname))
        return self.fake.request(method, url, **kw)

    def get(self, url, **kw):
        self.sent.append(("GET", urlparse(url).hostname))
        return self.fake.get(url, **kw)


@pytest.mark.parametrize("key", list(TRADERS), ids=[k[1] for k in TRADERS])
def test_each_client_on_an_account_allows_exactly_its_pinned_hosts(key):
    c = TRADERS[key]
    assert set(c.declared()) == set(c.hosts), f"{key[1]}'s own allowlist moved: review the change here, against KINDS"
    for host in c.hosts:
        c.build(None, base_url=f"https://{host}{urlparse(c.cfg()['base_url']).path}")
    for host in sorted(c.hosts):
        for url in (f"http://{host}", f"https://{host}.evil.example", "https://evil.example"):
            with pytest.raises(ValueError, match="refusing"):
                c.build(None, base_url=url)


@pytest.mark.parametrize("key", list(TRADERS), ids=[k[1] for k in TRADERS])
def test_each_client_on_an_account_sends_only_where_its_kind_allows(conn, key):
    c = TRADERS[key]
    s = Recorder(c.fake())
    c.drive(c.build(s), conn)
    assert s.sent, "the workflow sent nothing, so this proves nothing"
    assert {host for _, host in s.sent} <= c.hosts
    writes = {m for m, _ in s.sent} - {"GET"}
    if c.kind == "read":
        assert writes == set(), f"{key[1]} reads a real account and sent {writes}"
    else:
        assert writes, "a paper client's drill writes; the recorder saw none, so it is blind"


def test_a_kalshi_demo_order_is_a_post_only_bid():
    d = Demo()
    MONEY_MAP[("lab/markets/kalshi_trade.py", "KalshiDemo")].build(d).place("T-OK", 0.01, 1, "cid")
    [(method, path, _, body, _)] = d.calls
    assert (method, path) == ("POST", "/portfolio/events/orders")
    assert (body["side"], body["post_only"], body["count"], body["price"]) == ("bid", True, "1.00", "0.0100")


def test_a_manifold_bet_is_a_limit_that_expires_on_its_own():
    m = Manifold()
    MONEY_MAP[("lab/markets/manifold_trade.py", "ManifoldBets")].build(m).place("OK", 0.01, 1, "cid")
    [(method, path, _, body, _)] = m.calls
    assert (method, path) == ("POST", "/v0/bet")
    assert (body["limitProb"], body["amount"], body["expiresMillisAfter"]) == (0.01, 1.0, 600_000)


# 3. Refusals --------------------------------------------------------------------------------------------------------


def test_the_shipped_configs_are_dry_run_with_no_exchange_keys():
    base = json.loads((REPO / "user_data" / "config.base.json").read_text())
    assert base["dry_run"] is True and base.get("trading_mode", "spot") == "spot"
    exchanges = sorted((REPO / "user_data" / "exchanges").glob("*.json"))
    assert exchanges
    for p in exchanges:
        ex = json.loads(p.read_text()).get("exchange", {})
        assert not any(ex.get(f) for f in CREDENTIALS), f"{p.name} carries a credential"


def guard_with(tmp_path, overlay: dict):
    over = tmp_path / "overlay.json"
    over.write_text(json.dumps(overlay))
    league.guard([league.USER_DATA / "config.base.json", league.USER_DATA / "exchanges" / "kraken.json", over])


@pytest.mark.parametrize("overlay", [{"exchange": {f: "x"}} for f in CREDENTIALS] + [{"trading_mode": "futures"}],
                         ids=[*CREDENTIALS, "futures"])
def test_the_league_refuses_a_credential_or_a_margin_mode(tmp_path, overlay, capsys):
    with pytest.raises(SystemExit):
        guard_with(tmp_path, overlay)
    assert "refusing to start" in capsys.readouterr().err


def test_the_league_refuses_an_env_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FREQTRADE__DRY_RUN", "false")
    with pytest.raises(SystemExit):
        guard_with(tmp_path, {})
    assert "FREQTRADE__ env overrides are not allowed" in capsys.readouterr().err


def test_the_shipped_league_config_passes_the_guard(tmp_path):
    guard_with(tmp_path, {})  # does not exit
