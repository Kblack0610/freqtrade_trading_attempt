import datetime as dt
import pytest

import ingest

T0 = dt.datetime(2026, 11, 4, 12, tzinfo=dt.timezone.utc)


def trade(tid: int, is_open: bool, profit: float = 0.01) -> dict:
    t = {"trade_id": tid, "pair": "BTC/USD", "is_open": is_open, "open_date": "2026-11-04 12:20:14",
         "open_timestamp": 1793794814763 + tid, "open_rate": 78000.0, "amount": 0.004, "stake_amount": 330.0,
         "profit_ratio": profit, "profit_abs": 330 * profit, "enter_tag": "", "exit_reason": None if is_open else "roi"}
    if not is_open:
        t.update(close_date="2026-11-04 14:43:53", close_timestamp=1793803433830, close_rate=79000.0)
    return t


class FakeBots:
    """Answers the REST paths ingest uses; bot i's state lives in self.bots[i]."""

    def __init__(self, *bots: dict):
        self.bots = list(bots)

    def __call__(self, i: int, path: str):
        b = self.bots[i]
        if b.get("down"):
            raise ConnectionError("connection refused")
        if path == "show_config":
            return {"dry_run": b.get("dry_run", True)}
        if path == "profit":
            n = len(b["closed"]) + len(b["open"])
            return {"closed_trade_count": len(b["closed"]), "trade_count": n, "profit_closed_percent": 1.5,
                    "profit_all_percent": 1.4, "winrate": 0.5, "max_drawdown": 0.02, "profit_factor": None,
                    "bot_start_timestamp": 1793778204732}
        if path == "balance":
            return {"total": 1007.9, "currencies": [{"currency": "USD"}]}
        if path == "status":
            return b["open"]
        if path.startswith("trades?"):
            q = dict(kv.split("=") for kv in path.split("?")[1].split("&"))
            off, lim = int(q["offset"]), int(q["limit"])
            return {"trades": b["closed"][off:off + lim], "total_trades": len(b["closed"])}
        raise KeyError(path)


def count(conn, sql: str, *args) -> int:
    return conn.execute(sql, args).fetchone()[0]


def test_trade_row_uses_utc_timestamps():
    r = ingest.trade_row(trade(1, False))
    assert r["open_date"].tzinfo is dt.timezone.utc and r["close_date"] > r["open_date"]
    assert r["enter_tag"] is None


def test_rerun_adds_no_duplicates_and_tracks_closes(conn, monkeypatch):
    monkeypatch.setattr(ingest, "PAGE", 2)  # force paging through /trades
    bots = FakeBots({"closed": [trade(1, False), trade(2, False), trade(3, False)], "open": [trade(4, True)]})
    ingest.ingest_bot(conn, bots, 0, "k-s", "kraken", "S", T0)
    ingest.ingest_bot(conn, bots, 0, "k-s", "kraken", "S", T0 + dt.timedelta(hours=1))
    assert count(conn, "SELECT count(*) FROM bot_trades") == 4
    assert count(conn, "SELECT count(*) FROM bot_snapshots WHERE bot = 'k-s'") == 2

    bots.bots[0]["closed"].append(trade(4, False))
    bots.bots[0]["open"] = [trade(5, True)]
    ingest.ingest_bot(conn, bots, 0, "k-s", "kraken", "S", T0 + dt.timedelta(hours=2))
    assert count(conn, "SELECT count(*) FROM bot_trades WHERE is_open") == 1
    assert count(conn, "SELECT count(*) FROM bot_trades WHERE trade_id = 4 AND NOT is_open") == 1

    bots.bots[0]["open"] = []  # trade 5 timed out unfilled and the bot dropped it
    ingest.ingest_bot(conn, bots, 0, "k-s", "kraken", "S", T0 + dt.timedelta(hours=3))
    assert count(conn, "SELECT count(*) FROM bot_trades") == 4


def test_down_bot_gets_an_error_snapshot_and_keeps_its_trades(conn):
    bots = FakeBots({"closed": [trade(1, False)], "open": [trade(2, True)]})
    ingest.ingest_bot(conn, bots, 0, "k-s", "kraken", "S", T0)
    bots.bots[0]["down"] = True
    snap = ingest.ingest_bot(conn, bots, 0, "k-s", "kraken", "S", T0 + dt.timedelta(hours=1))
    assert snap["error"].startswith("ConnectionError")
    assert count(conn, "SELECT count(*) FROM bot_trades") == 2


def test_main_fails_on_a_bot_that_is_not_dry_run(conn, monkeypatch, tmp_path):
    (tmp_path / "roster.tsv").write_text("# name exchange strategy\na kraken S\nb coinbase T\n")
    monkeypatch.setattr(ingest, "ROSTER", tmp_path / "roster.tsv")
    bots = FakeBots({"closed": [], "open": []}, {"closed": [], "open": [], "dry_run": False})
    assert ingest.main(bots) == 1
    assert count(conn, "SELECT count(*) FROM bot_snapshots WHERE error = 'NOT DRY RUN'") == 1
