"""The order drill: the proof that an order path works end to end on mock funds.

Rests one order, reads it back, cancels it and reads it back, writing each step to demo_orders (migration 023). A
client gives `venue`, `resting` (the statuses that mean the order is on the book), place(), order() and cancel(). The
row's error is None only when the order was seen resting and then canceled. A place is never retried: a timeout could
have placed it, and a second one would double it. A read-back that fails says nothing about the order, so the drill
cancels before it reports the failure; only a status it did read, and that is not resting, leaves the order alone.
"""

import datetime as dt
import uuid

UTC = dt.timezone.utc


def rest_and_cancel(conn, client, ticker: str, price: float, now=None) -> dict:
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
