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
    assert first == {"research_passing": 2, "research_enters": 3, "research_leaves": 0}
    assert loop.run(conn) == {"research_passing": 2, "research_enters": 0, "research_leaves": 0}

    conn.execute("UPDATE climate_scorecard SET lo = -0.01 WHERE name = 'S' AND cell = 'up/low'")
    conn.execute("UPDATE climate_scorecard SET lo = 0.02 WHERE name = 'T'")
    conn.execute("DELETE FROM suggestions WHERE kind = 'promote'")
    assert loop.run(conn) == {"research_passing": 2, "research_enters": 1, "research_leaves": 2}

    events = loop.pending(conn)
    assert [(e["signal"], e["change"], e["name"]) for e in events] == [
        ("climate", "enters", "m"), ("climate", "enters", "S"), ("promote", "enters", "Good"),
        ("climate", "enters", "T"), ("climate", "leaves", "S"), ("promote", "leaves", "Good")]
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
    assert "trial register at 40" in details[-1]
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
    assert loop.run(conn) == {"research_passing": 0, "research_enters": 1, "research_leaves": 1}
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
    assert not hasattr(research_sync, "roster_pr") and not hasattr(research_sync, "call")
