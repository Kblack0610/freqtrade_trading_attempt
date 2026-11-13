"""The order drill: the proof that an order path works end to end on mock funds.

Rests one order, reads it back, cancels it and reads it back, writing each step to demo_orders (migration 023). A
client is an Orders (lab/markets/venue.py), whose cancel() returns once await_terminal does. The row's error is None only when the order was seen resting and then canceled. A place is never retried: a timeout could
have placed it, and a second one would double it. A read-back that fails says nothing about the order, so the drill
cancels before it reports the failure; only a status it did read, and that is not resting, leaves the order alone.
"""

import datetime as dt
import uuid

from lab.markets.venue import Orders

UTC = dt.timezone.utc
# A cancel is asynchronous on every venue we drill, and none offers an end-of-cancel event a one-shot job can wait on
# (Alpaca's is the trade-updates websocket, Manifold has none), so the order is re-read until it reaches one of the
# client's `terminal` statuses, within this bound. A read right after the cancel can still say it rests (Alpaca and
# Manifold, 2026-11-12).
CANCEL_POLL_S, CANCEL_POLLS = 1.0, 10


def await_terminal(client: Orders, order_id: str, sleep, polls: int = CANCEL_POLLS, gap_s: float = CANCEL_POLL_S) -> None:
    """Return once the order reads terminal, or after the bound; the drill's own read-back then judges it."""
    for _ in range(polls):
        if client.order(order_id)["status"] in client.terminal:
            return
        sleep(gap_s)


def rest_and_cancel(conn, client: Orders, ticker: str, price: float, now=None) -> dict:
    now = now or (lambda: dt.datetime.now(UTC))
    cid = str(uuid.uuid4())
    rid = conn.execute("INSERT INTO demo_orders (venue, ticker, client_id, price, count, placed_at) "
                       "VALUES (%s, %s, %s, %s, 1, %s) RETURNING id", (client.venue, ticker, cid, price, now())).fetchone()[0]

    def note(**kv):
        conn.execute(f"UPDATE demo_orders SET {', '.join(f'{k} = %s' for k in kv)} WHERE id = %s", (*kv.values(), rid))

    try:
        oid = client.place(ticker, price, 1, cid)["order_id"]
        note(order_id=oid)
        try:
            status = client.order(oid)["status"]
        except Exception:
            client.cancel(oid, ticker)
            raise
        note(status=status)
        if status not in client.resting:
            raise RuntimeError(f"order {oid} is {status}, not resting")
        client.cancel(oid, ticker)
        status = client.order(oid)["status"]
        note(status=status, canceled_at=now() if status == "canceled" else None)
        if status != "canceled":
            raise RuntimeError(f"order {oid} is {status} after the cancel")
    except Exception as e:  # noqa: BLE001 - the row carries what went wrong, and the exit code says it failed
        note(error=f"{type(e).__name__}: {e}"[:1000])
    return dict(zip(("id", "ticker", "order_id", "status", "canceled_at", "error"), conn.execute(
        "SELECT id, ticker, order_id, status, canceled_at, error FROM demo_orders WHERE id = %s", (rid,)).fetchone()))
