"""The one readiness table and its one writer. Every gate (lab/crypto/confidence.py for crypto, lab/events/families.py for
event contracts, lab/stocks/gate.py for stocks) hands its verdict rows to store(); every reader selects from `readiness` by market.

A row is one strategy on one venue: market, venue, strategy, and the verdict (score, ready, passed, trades, the
expectancy and its interval, gates). An event-contract strategy is 'family/variant' and also keeps family, variant and
params for the paper pass."""
from psycopg.types.json import Jsonb

CRYPTO, EVENTS, STOCKS = "crypto", "events", "stocks"

# Crypto rows under the names its readers use: exchange, oos_trades and oos_expectancy.
CRYPTO_ROWS = ("SELECT strategy, venue AS exchange, score, ready, passed, trades AS oos_trades, expectancy AS oos_expectancy, "
               "gates, computed_at FROM readiness WHERE market = 'crypto'")
EVENT_ROWS = ("SELECT family, venue, variant, params, windows, trades, expectancy, expectancy_lo, expectancy_hi, dsr, pbo, "
              "trials, ready, passed, gates, computed_at FROM readiness WHERE market = 'events'")

COLS = ("venue", "strategy", "family", "variant", "params", "score", "ready", "passed", "windows", "trades", "expectancy",
        "expectancy_lo", "expectancy_hi", "dsr", "pbo", "trials", "gates")


def store(conn, market: str, rows: list[dict], venue: str | None = None, family: str | None = None) -> None:
    """Replace the market's rows (only those of `venue`, and of `family` within it, when given) with `rows`."""
    where, args = "market = %s", [market]
    for col, val in (("venue", venue), ("family", family)):
        if val is not None:
            where, args = f"{where} AND {col} = %s", [*args, val]
    full = [{**dict.fromkeys(COLS), "windows": 0, "trials": 0, **r, "market": market,
             "params": None if r.get("params") is None else Jsonb(r["params"]),
             "gates": r["gates"] if isinstance(r["gates"], Jsonb) else Jsonb(r["gates"])} for r in rows]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"DELETE FROM readiness WHERE {where}", args)
        cur.executemany(f"INSERT INTO readiness (market, {', '.join(COLS)}) "
                        f"VALUES (%(market)s, {', '.join(f'%({c})s' for c in COLS)})", full)


def at_ready(conn, market: str, venue: str, strategy: str) -> bool:
    row = conn.execute("SELECT ready FROM readiness WHERE market = %s AND venue = %s AND strategy = %s",
                       (market, venue, strategy)).fetchone()
    return bool(row and row[0])
