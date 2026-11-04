import datetime as dt
import json

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


def test_history_row_dry_run_flag():
    base = {"bot": "b", "ts": "2026-11-04T06:30:00+00:00"}
    assert ingest.history_row(base)["dry_run"] is True
    assert ingest.history_row({**base, "error": "NOT DRY RUN"})["dry_run"] is False
    assert ingest.history_row({**base, "error": "URLError: timeout"})["dry_run"] is None


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
    monkeypatch.setattr(ingest, "RESULTS", tmp_path)
    bots = FakeBots({"closed": [], "open": []}, {"closed": [], "open": [], "dry_run": False})
    assert ingest.main(bots) == 1
    assert count(conn, "SELECT count(*) FROM bot_snapshots WHERE error = 'NOT DRY RUN'") == 1


def test_lab_files_import_once(conn, tmp_path):
    run = tmp_path / "runs" / "2026-11-04"
    run.mkdir(parents=True)
    (tmp_path / "runs" / "latest").symlink_to(run)
    summary = {"run": "2026-11-04", "min_trades": 20,
               "backtests": [{"exchange": "binanceus", "strategy": "S", "timeframe": "5m", "trades": 40,
                              "profit_pct": 3.2, "winrate_pct": 55.0, "max_drawdown_pct": 4.1, "profit_factor": 1.3,
                              "sharpe": 1.1, "start": "2026-08-06 00:00:00", "end": "2026-11-04 00:00:00",
                              "lookahead_bias": False},
                             {"exchange": "coinbase", "strategy": "Broken", "error": "No data found",
                              "lookahead_bias": None}],
               "lookahead": {"S": {"has_bias": False, "biased_indicators": ""}},
               "hyperopt_candidates": ["S"], "hyperopt": {"S": {"params": {"buy": {"x": 1}}}}}
    (run / "summary.json").write_text(json.dumps(summary))
    hist = tmp_path / "league-history.jsonl"
    hist.write_text("\n".join(json.dumps({"ts": f"2026-11-0{d}T06:30:00+00:00", "bot": "k-s", "exchange": "kraken",
                                          "strategy": "S", "closed_trades": d}) for d in (3, 4)) + "\n")

    for _ in range(2):
        assert [p.parent.name for p in ingest.summaries(tmp_path)] == ["2026-11-04"]
        for p in ingest.summaries(tmp_path):
            ingest.import_summary(conn, p)
        ingest.import_history(conn, hist)
    assert count(conn, "SELECT count(*) FROM runs") == 1
    assert count(conn, "SELECT count(*) FROM backtests") == 2
    assert count(conn, "SELECT count(*) FROM bias_checks") == 1
    assert count(conn, "SELECT count(*) FROM hyperopt_runs") == 1
    assert count(conn, "SELECT count(*) FROM bot_snapshots WHERE source = 'report'") == 2

    summary["backtests"][0]["trades"] = 41  # a rewritten summary replaces the old rows
    (run / "summary.json").write_text(json.dumps(summary))
    assert ingest.import_summary(conn, run / "summary.json") is True
    assert count(conn, "SELECT trades FROM backtests WHERE strategy = 'S'") == 41
    assert count(conn, "SELECT count(*) FROM runs") == 1
