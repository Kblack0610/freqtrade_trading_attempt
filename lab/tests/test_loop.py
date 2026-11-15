import importlib.util
import pathlib

import pytest

from lab import confidence, loop, suggest

_spec = importlib.util.spec_from_file_location(
    "research_sync", pathlib.Path(__file__).resolve().parents[2] / "ci" / "research_sync.py")
research_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(research_sync)

def score(conn, name, cell, lo, kind="strategy"):
    conn.execute("INSERT INTO climate_scorecard (kind, name, exchange, variant, cell, days, trades, annual, lo, hi, trials) "
                 "VALUES (%s, %s, %s, 'tuned', %s, 90, 9, 0.2, %s, 0.4, 3)",
                 (kind, name, "" if kind == "mixture" else "kraken", cell, lo))


def test_a_change_is_an_event_once_and_a_steady_set_is_none(conn):
    score(conn, "S", "up/low", 0.05)
    score(conn, "T", "up/low", -0.05)
    score(conn, "m", "down/high", 0.01, kind="mixture")
    score(conn, "S", "flat/mid", None)
    conn.execute("INSERT INTO suggestions (kind, strategy, exchange, summary, action, gates) VALUES "
                 "('promote', 'Good', 'kraken', 's', 'a', '[]'), ('watch', 'Meh', 'kraken', 's', 'a', '[]')")
    first = loop.run(conn)
    quiet = {"bet_ready": 0, "bet_drifted": 0}
    assert first == {"research_passing": 2, "research_enters": 3, "research_leaves": 0, **quiet}
    assert loop.run(conn) == {"research_passing": 2, "research_enters": 0, "research_leaves": 0, **quiet}

    conn.execute("UPDATE climate_scorecard SET lo = -0.01 WHERE name = 'S' AND cell = 'up/low'")
    conn.execute("UPDATE climate_scorecard SET lo = 0.02 WHERE name = 'T'")
    conn.execute("DELETE FROM suggestions WHERE kind = 'promote'")
    assert loop.run(conn) == {"research_passing": 2, "research_enters": 1, "research_leaves": 2, **quiet}

    events = loop.pending(conn)
    assert [(e["signal"], e["change"], e["name"]) for e in events] == [
        ("climate", "enters", "m"), ("climate", "enters", "S"), ("promote", "enters", "Good"),
        ("climate", "enters", "T"), ("climate", "leaves", "S"), ("promote", "leaves", "Good")]
    assert all(e["avenue"] == loop.AVENUES for e in events)
    good = events[2]
    assert good["action"] == suggest.roster_line("Good", "kraken") and good["cell"] == "" and good["trials"] == confidence.trial_count(conn)
    assert events[1]["lo"] == 0.05 and events[1]["variant"] == "tuned" and events[0]["exchange"] == ""
    assert loop.ack(conn, [e["id"] for e in events[:3]]) == 3
    assert loop.ack(conn, [events[0]["id"]]) == 0
    assert [e["name"] for e in loop.pending(conn)] == ["T", "S", "Good"]


def test_one_board_row_carries_a_nights_climate_changes():
    ev = [{"at": "2026-11-10T03:30:00+00:00", "change": c, "cell": "up/low", "kind": "strategy", "name": n,
           "exchange": "kraken", "variant": "tuned", "lo": 0.01, "annual": 0.2, "trials": 40}
          for c, n in [("enters", "S"), ("enters", "T"), ("leaves", "U")]]
    text, details = research_sync.climate_row(ev)
    assert "2026-11-10: 2 enter and 1 leave" in text
    assert details[0] == "enters up/low: strategy S on kraken (tuned), annual +20.0%, low end +1.0%"
    assert "trial register at 40" in details[-1] and research_sync.AVENUES in details[-1]
    many = research_sync.climate_row(ev * 20)[1]
    assert len(many) == research_sync.MOST_DETAILS + 2 and many[-2].startswith("and 20 more")


def test_monthly_retune_reports_new_fits_and_lost_qualification(conn):
    action = 'Save parameters trained 2026-01-01 to 2026-04-01: {"buy": 1}'
    conn.execute("INSERT INTO suggestions (kind, strategy, exchange, summary, action, gates) "
                 "VALUES ('retune', 'S', 'kraken', 's', %s, '[]')", (action,))
    assert loop.run(conn)["research_enters"] == 1
    assert loop.run(conn)["research_enters"] == 0
    assert loop.pending(conn)[0]["action"] == action
    newer = action.replace('2026-04-01', '2026-05-01')
    conn.execute("UPDATE suggestions SET action = %s WHERE kind = 'retune'", (newer,))
    assert loop.run(conn) == {"research_passing": 0, "research_enters": 1, "research_leaves": 1, "bet_ready": 0,
                              "bet_drifted": 0}
    events = loop.pending(conn)
    assert [e["action"] for e in events] == [action, newer, action]
    assert events[0]["variant"] != events[1]["variant"]
    conn.execute("DELETE FROM suggestions WHERE kind = 'retune'")
    assert loop.run(conn)["research_leaves"] == 1
    assert loop.run(conn)["research_leaves"] == 0


@pytest.mark.parametrize("dry", [False, True])
def test_sync_reports_retunes_and_only_acks_after_board_success(monkeypatch, dry):
    events = [dict(id=i, signal="retune", change=change, name="S", exchange="kraken", variant="abc123", action="fit JSON")
              for i, change in enumerate(("enters", "leaves"), 1)]
    calls, rows = [], []

    def kubectl(*args):
        calls.append(args)
        return research_sync.json.dumps(events) if args == ("pending",) else "acked"

    monkeypatch.setenv("DRY_RUN", "1" if dry else "0")
    monkeypatch.setattr(research_sync, "kubectl_research", kubectl)
    monkeypatch.setattr(research_sync, "board", lambda *args: rows.append(args))
    assert research_sync.main() == 0
    assert calls == ([("pending",)] if dry else [("pending",), ("ack", "1", "2")])
    assert len(rows) == (0 if dry else 2)
    if not dry:
        assert rows[0][1][0] == "fit JSON"
        assert all(research_sync.AVENUES in details[-1] for _, details in rows)
        assert "no longer qualifies" in rows[1][0]

    def failed_board(*args):
        raise RuntimeError("board unavailable")

    if not dry:
        calls.clear()
        monkeypatch.setattr(research_sync, "board", failed_board)
        with pytest.raises(RuntimeError, match="board unavailable"):
            research_sync.main()
        assert calls == [("pending",)]


def test_sync_reads_on_the_site_and_acks_with_the_writer_role(monkeypatch):
    cmds = []

    def run(cmd, **kw):
        cmds.append(cmd)
        fail = cmd[-2:] == ["ack", "9"]
        return research_sync.subprocess.CompletedProcess(
            cmd, 1 if fail else 0, "[]", "psycopg.errors.ReadOnlySqlTransaction: cannot execute UPDATE" if fail else "")

    monkeypatch.setattr(research_sync.subprocess, "run", run)
    research_sync.kubectl_research("pending")
    research_sync.kubectl_research("ack", "1")
    assert [c[c.index("exec") + 1:c.index("--")] for c in cmds] == [
        ["deploy/trader-web", "-c", "web"], ["deploy/freqtrade-watcher", "-c", "watcher"]]
    with pytest.raises(SystemExit, match="ack on deploy/freqtrade-watcher exited 1: .*ReadOnlySqlTransaction"):
        research_sync.kubectl_research("ack", "9")


def test_a_promotion_files_a_board_row_and_opens_no_roster_pr(monkeypatch):
    events = [dict(id=1, signal="promote", change="enters", name="Good", exchange="kraken", variant="", cell="",
                   action=suggest.roster_line("Good", "kraken"))]
    rows = []
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(research_sync, "kubectl_research",
                        lambda *a: research_sync.json.dumps(events) if a == ("pending",) else "acked")
    monkeypatch.setattr(research_sync, "board", lambda *a: rows.append(a))
    assert research_sync.main() == 0
    assert "shadow account" in rows[0][0] and rows[0][1][0] == "https://trader.kblab.me/accounts"
    assert research_sync.AVENUES in rows[0][1][-1]
    assert not hasattr(research_sync, "roster_pr") and not hasattr(research_sync, "call")


def ready(conn, variant, ok, lo=0.012):
    conn.execute("INSERT INTO market_readiness (family, venue, variant, params, windows, trades, expectancy, expectancy_lo, "
                 "expectancy_hi, dsr, pbo, trials, ready, passed, gates) VALUES ('longshot', 'kalshi', %s, '{\"edge\": 0.06}', "
                 "4, 120, 0.02, %s, 0.03, 0.97, 0.1, 9, %s, 7, '[]')", (variant, lo, ok))


def paper(conn, account, drifted, retired=False):
    conn.execute("INSERT INTO paper_accounts (account, family, venue, variant, params, capital, started_at, retired_at, "
                 "paper_trades, paper_expectancy, backtest_trades, backtest_expectancy, gap_lo, gap_hi, drifted) "
                 "VALUES (%s, 'longshot', 'kalshi', %s, '{}', 1000, now(), %s, 40, -0.03, 120, 0.02, -0.08, -0.01, %s)",
                 (account, account[-8:] * 2, "2026-11-14" if retired else None, drifted))


def test_betting_gate_and_paper_drift_are_events_against_the_markets_trial_count(conn):
    crypto = confidence.trial_count(conn)
    conn.execute("INSERT INTO trials (strategy, exchange, variant) VALUES ('longshot', 'kalshi', 'a'), "
                 "('longshot', 'kalshi', 'b'), ('longshot', 'kalshi', 'c')")
    ready(conn, "aaaa1111aaaa1111", True)
    ready(conn, "bbbb2222bbbb2222", False, lo=-0.01)
    paper(conn, "paper-longshot-aaaa1111", True)
    paper(conn, "paper-longshot-cccc3333", True, retired=True)  # retired: its variant already left the gate
    paper(conn, "paper-longshot-dddd4444", False)
    out = loop.run(conn)
    assert out["bet_ready"] == 1 and out["bet_drifted"] == 1 and out["research_passing"] == 0
    events = loop.pending(conn)
    assert [(e["signal"], e["change"], e["variant"], e["cell"]) for e in events] == [
        ("bet-drift", "enters", "aaaa1111aaaa1111", "paper-longshot-aaaa1111"),
        ("bet-ready", "enters", "aaaa1111aaaa1111", "")]
    assert all(e["trials"] == 3 for e in events) and confidence.trial_count(conn) == crypto != 3
    assert events[1]["lo"] == 0.012 and '"edge": 0.06' in events[1]["action"]
    assert events[0]["action"].startswith("paper -0.0300 per trade over 40 settled, backtest 0.0200 over 120")
    assert loop.run(conn)["research_enters"] == 0

    conn.execute("UPDATE market_readiness SET ready = NOT ready")
    conn.execute("UPDATE paper_accounts SET drifted = false")
    assert loop.run(conn) == {"research_passing": 0, "research_enters": 1, "research_leaves": 2, "bet_ready": 1,
                              "bet_drifted": 0}
    assert [(e["signal"], e["change"], e["variant"][:4], e["trials"]) for e in loop.pending(conn)[2:]] == [
        ("bet-ready", "enters", "bbbb", 3), ("bet-drift", "leaves", "aaaa", 3), ("bet-ready", "leaves", "aaaa", 3)]


def test_sync_files_one_row_per_betting_signal_and_acks_it(monkeypatch):
    def ev(i, signal, change, cell="", action=""):
        return dict(id=i, at="2026-11-14T12:45:00+00:00", signal=signal, change=change, kind="family", name="longshot",
                    exchange="kalshi", variant="aaaa1111aaaa1111", cell=cell, lo=0.012, annual=None, trials=60, action=action)
    events = [ev(1, "bet-ready", "enters"), ev(2, "bet-ready", "leaves"),
              ev(3, "bet-drift", "enters", "paper-longshot-aaaa1111", "paper -0.03 per trade")]
    calls, rows = [], []
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setattr(research_sync, "kubectl_research",
                        lambda *a: calls.append(a) or (research_sync.json.dumps(events) if a == ("pending",) else "acked"))
    monkeypatch.setattr(research_sync, "board", lambda *a: rows.append(a))
    assert research_sync.main() == 0
    assert calls == [("pending",), ("ack", "1", "2", "3")]
    (gate, gate_details), (drift, drift_details) = rows
    assert "betting gate 2026-11-14: 1 enter and 1 leave" in gate and gate.endswith("#size/s")
    assert gate_details[0] == "enters: longshot/aaaa1111 on kalshi, low end +1.20% per trade"
    assert "paper drift 2026-11-14: 1 paper accounts drifted" in drift
    assert drift_details[0] == ("enters: longshot/aaaa1111 on kalshi (paper-longshot-aaaa1111), gap low end +1.20%: "
                                "paper -0.03 per trade")
    assert all("markets trial register at 60" in d[-1] and research_sync.AVENUES in d[-1] for d in (gate_details, drift_details))
    many = research_sync.bet_row("bet-ready", [ev(i, "bet-ready", "enters") for i in range(50)])[1]
    assert len(many) == research_sync.MOST_DETAILS + 3 and many[-3].startswith("and 10 more")
