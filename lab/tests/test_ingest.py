import datetime as dt
import pytest

from lab.crypto import ingest
from lab.crypto.league import Row

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
        self.candle_paths: list[str] = []
        self.custom_paths: list[str] = []

    def __call__(self, i: int, path: str):
        b = self.bots[i]
        if b.get("down"):
            raise ConnectionError("connection refused")
        if path == "show_config":
            return {"dry_run": b.get("dry_run", True), "timeframe": "5m"}
        if path == "profit":
            n = len(b["closed"]) + len(b["open"])
            return {"closed_trade_count": len(b["closed"]), "trade_count": n, "profit_closed_percent": 1.5,
                    "profit_all_percent": 1.4, "winrate": 0.5, "max_drawdown": 0.02, "profit_factor": None,
                    "bot_start_timestamp": 1793778204732}
        if path == "balance":
            return {"total": 1007.9, "currencies": [{"currency": "USD"}]}
        if path == "status":
            return b["open"]
        if path.startswith("pair_candles?"):
            self.candle_paths.append(path)
            return {"data_stop_ts": 1793794800000}
        if "custom-data" in path:  # b["custom"]: trade_id -> its entry_inputs, as lab/entry_inputs.py writes it
            self.custom_paths.append(path)
            if b.get("custom_down"):
                raise ConnectionError("custom data refused")
            tid = path.split("/")[1]
            ids = [t["trade_id"] for t in b["open"]] if tid == "open" else [int(tid)]
            return [{"trade_id": i, "custom_data": [{"key": "entry_inputs", "type": "dict", "value": b["custom"][i]}]}
                    for i in ids if i in b.get("custom", {})]
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


@pytest.mark.parametrize("ws_off", [False, True])
def test_rerun_adds_no_duplicates_and_tracks_closes(conn, monkeypatch, ws_off):
    monkeypatch.setattr(ingest, "PAGE", 2)  # force paging through /trades
    bots = FakeBots({"closed": [trade(1, False), trade(2, False), trade(3, False)], "open": [trade(4, True)]})
    ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S", ws_off=ws_off), T0)
    ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S"), T0 + dt.timedelta(hours=1))
    assert count(conn, "SELECT count(*) FROM bot_trades") == 4
    assert count(conn, "SELECT count(*) FROM bot_snapshots WHERE bot = 'k-s'") == 2

    bots.bots[0]["closed"].append(trade(4, False))
    bots.bots[0]["open"] = [trade(5, True)]
    ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S"), T0 + dt.timedelta(hours=2))
    assert count(conn, "SELECT count(*) FROM bot_trades WHERE is_open") == 1
    assert count(conn, "SELECT count(*) FROM bot_trades WHERE trade_id = 4 AND NOT is_open") == 1

    bots.bots[0]["open"] = []  # trade 5 timed out unfilled and the bot dropped it
    ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S"), T0 + dt.timedelta(hours=3))
    assert count(conn, "SELECT count(*) FROM bot_trades") == 4


def test_snapshot_keeps_the_newest_candle_of_each_open_pair(conn):
    bots = FakeBots({"closed": [trade(1, False)], "open": [trade(2, True), trade(3, True)]})
    ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S"), T0)
    raw = conn.execute("SELECT raw FROM bot_snapshots WHERE bot = 'k-s'").fetchone()[0]
    assert raw["timeframe"] == "5m" and raw["candles"] == {"BTC/USD": 1793794800000}
    assert bots.candle_paths == ["pair_candles?pair=BTC%2FUSD&timeframe=5m&limit=1"]  # one call per pair, not per trade


def test_down_bot_gets_an_error_snapshot_and_keeps_its_trades(conn):
    bots = FakeBots({"closed": [trade(1, False)], "open": [trade(2, True)]})
    ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S"), T0)
    bots.bots[0]["down"] = True
    snap = ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S"), T0 + dt.timedelta(hours=1))
    assert snap["error"].startswith("ConnectionError")
    assert count(conn, "SELECT count(*) FROM bot_trades") == 2


def test_main_fails_on_a_bot_that_is_not_dry_run(conn, monkeypatch, tmp_path):
    (tmp_path / "roster.tsv").write_text("# name exchange strategy\na kraken S\nb coinbase T\n")
    monkeypatch.setattr(ingest, "ROSTER", tmp_path / "roster.tsv")
    bots = FakeBots({"closed": [], "open": []}, {"closed": [], "open": [], "dry_run": False})
    assert ingest.main(bots) == 1
    assert count(conn, "SELECT count(*) FROM bot_snapshots WHERE error = 'NOT DRY RUN'") == 1


NOW = dt.datetime(2026, 11, 6, 12, 30, tzinfo=dt.timezone.utc)


def snapshots(conn, stamps: list[dt.datetime], bot: str = "k-s", profit: float | None = 1.0, dry_run: bool | None = True,
              error: str | None = None) -> None:
    conn.execute(ingest.UPSERT_BOT, (bot, "kraken", "S", "league", "", NOW, NOW))
    conn.cursor().executemany("INSERT INTO bot_snapshots (bot, ts, profit_all_pct, dry_run, error) VALUES (%s, %s, %s, %s, %s)",
                              [(bot, ts, profit, dry_run, error) for ts in stamps])


def kept(conn, bot: str = "k-s") -> list[dt.datetime]:
    return [r[0] for r in conn.execute("SELECT ts FROM bot_snapshots WHERE bot = %s ORDER BY ts", (bot,))]


def every_5_min(start: dt.datetime, hours: int) -> list[dt.datetime]:
    return [start + dt.timedelta(minutes=5 * n) for n in range(12 * hours)]


def test_prune_thins_each_tier_and_is_idempotent(conn):
    recent = NOW - dt.timedelta(days=6)
    hourly = dt.datetime(2026, 10, 1, 10, tzinfo=dt.timezone.utc)
    daily = dt.datetime(2026, 7, 1, 22, tzinfo=dt.timezone.utc)  # four hours across midnight UTC
    snapshots(conn, every_5_min(recent, 2) + every_5_min(hourly, 2) + every_5_min(daily, 4))
    snapshots(conn, every_5_min(hourly, 1), bot="k-t")

    assert ingest.prune_snapshots(conn, NOW) == 22 + 46 + 11
    last_of_hour = [hourly + dt.timedelta(minutes=55), hourly + dt.timedelta(minutes=115)]
    last_of_day = [daily + dt.timedelta(minutes=115), daily + dt.timedelta(minutes=235)]
    assert kept(conn) == last_of_day + last_of_hour + every_5_min(recent, 2)
    assert kept(conn, "k-t") == last_of_hour[:1]  # buckets are per bot
    assert ingest.prune_snapshots(conn, NOW) == 0


def test_prune_leaves_a_bucket_the_cutoff_falls_inside(conn):
    cutoff = NOW - dt.timedelta(days=ingest.SNAPSHOT_FULL_DAYS)
    stamps = every_5_min(cutoff.replace(minute=0), 1)  # the hour NOW's 7 day cutoff lands in
    snapshots(conn, stamps)
    assert ingest.prune_snapshots(conn, NOW) == 0
    assert kept(conn) == stamps


def test_prune_prefers_a_row_with_profit_over_a_newer_error(conn):
    hour = dt.datetime(2026, 10, 1, 10, tzinfo=dt.timezone.utc)
    down = dt.datetime(2026, 10, 1, 11, tzinfo=dt.timezone.utc)
    snapshots(conn, [hour, hour + dt.timedelta(minutes=5)])
    snapshots(conn, [hour + dt.timedelta(minutes=10)], profit=None, dry_run=None, error="ConnectionError: refused")
    snapshots(conn, [down, down + dt.timedelta(minutes=5)], profit=None, dry_run=None, error="ConnectionError: refused")
    ingest.prune_snapshots(conn, NOW)
    assert kept(conn) == [hour + dt.timedelta(minutes=5), down + dt.timedelta(minutes=5)]  # a dead hour still shows


def test_prune_never_touches_a_not_dry_run_row(conn):
    day = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    snapshots(conn, [day, day + dt.timedelta(minutes=5)], dry_run=False, error="NOT DRY RUN")
    snapshots(conn, [day + dt.timedelta(minutes=10)], dry_run=None, error="NOT DRY RUN")  # /show_config left it out
    snapshots(conn, [day + dt.timedelta(minutes=15)], dry_run=False)
    snapshots(conn, [day + dt.timedelta(minutes=20), day + dt.timedelta(minutes=25)])
    assert ingest.prune_snapshots(conn, NOW) == 1
    assert len(kept(conn)) == 5


def test_main_prunes_after_ingesting(conn, monkeypatch, tmp_path):
    (tmp_path / "roster.tsv").write_text("# name exchange strategy\nk-s kraken S\n")
    monkeypatch.setattr(ingest, "ROSTER", tmp_path / "roster.tsv")
    old = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    snapshots(conn, [old, old + dt.timedelta(minutes=5)])
    assert ingest.main(FakeBots({"closed": [], "open": []})) == 0
    assert kept(conn)[0] == old + dt.timedelta(minutes=5) and len(kept(conn)) == 2


def candle(rsi: float) -> dict:
    return {"candle": "2026-11-04T12:15:00+00:00", "timeframe": "5m", "values": {"close": 77990.0, "rsi": rsi}}


def decisions(conn) -> list[tuple]:
    return conn.execute("SELECT account, fill_id, decided_at, rule, inputs, sizing FROM decisions "
                        "WHERE league = 'crypto' ORDER BY account, fill_id").fetchall()


def test_each_traced_trade_gets_one_decision_with_its_entry_candle(conn):
    open_t = dict(trade(4, True), enter_tag="dip", fee_open=0.0026, fee_open_cost=0.86)
    bots = FakeBots({"closed": [trade(1, False), trade(2, False)], "open": [open_t, trade(5, True)],
                     "custom": {1: candle(28.0), 4: candle(31.5)}})
    snap = ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "BinHV45"), T0)
    assert snap["decision_rows"] == 2
    got = decisions(conn)
    assert [(a, f) for a, f, *_ in got] == [("k-s", 1), ("k-s", 4)]
    _, _, at, rule, inputs, sizing = got[1]
    assert at == ingest.trade_row(open_t)["open_date"] and rule == "BinHV45: entry signal dip"
    assert inputs[1] == {"name": "rsi", "value": 31.5, "source": "kraken 5m candle", "as_of": "2026-11-04T12:15:00+00:00"}
    assert sizing == {"stake": 330.0, "amount": 0.004, "price": 78000.0, "fee_rate": 0.0026, "fee": 0.86}

    bots.custom_paths.clear()
    ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "BinHV45"), T0 + dt.timedelta(minutes=5))
    assert len(decisions(conn)) == 2  # a rerun adds none
    # trade 5 (open, untraced) is asked for in the one open call; traced 1 is not asked again, untraced 2 is
    assert bots.custom_paths == ["trades/open/custom-data?key=entry_inputs&limit=500", "trades/2/custom-data?key=entry_inputs"]


def test_a_closed_trade_is_read_one_by_one_only_while_recent(conn):
    bots = FakeBots({"closed": [trade(1, False)], "open": [], "custom": {1: candle(28.0)}})
    later = ingest.trade_row(trade(1, False))["close_date"] + dt.timedelta(hours=ingest.TRACE_CLOSED_HOURS, minutes=1)
    ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S"), later)
    assert bots.custom_paths == [] and decisions(conn) == []


def test_two_bots_with_the_same_trade_id_each_keep_their_decision(conn):
    bot = {"closed": [], "open": [trade(1, True)], "custom": {1: candle(30.0)}}
    bots = FakeBots(bot, dict(bot))
    ingest.ingest_bot(conn, bots, 0, Row("k-a", "kraken", "S"), T0)
    ingest.ingest_bot(conn, bots, 1, Row("k-b", "kraken", "S"), T0)
    assert [(a, f) for a, f, *_ in decisions(conn)] == [("k-a", 1), ("k-b", 1)]


def test_a_failed_custom_data_read_keeps_the_trades_and_traces_them_later(conn):
    bots = FakeBots({"closed": [], "open": [trade(1, True)], "custom": {1: candle(30.0)}, "custom_down": True})
    snap = ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S"), T0)
    assert snap["error"] is None and snap["raw"].obj["trace_error"].startswith("ConnectionError")
    assert count(conn, "SELECT count(*) FROM bot_trades") == 1 and decisions(conn) == []
    bots.bots[0]["custom_down"] = False
    ingest.ingest_bot(conn, bots, 0, Row("k-s", "kraken", "S"), T0 + dt.timedelta(minutes=5))
    assert len(decisions(conn)) == 1
