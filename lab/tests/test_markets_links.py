"""market_links: the kalshi_game rule, LLM proposals and confirmation by resolution. Kalshi rows use tickers and
subtitles as the live API returned them on 2026-11-06."""

import datetime as dt
import json

import pytest

from lab.markets import links
from lab.markets.recorder import load_config

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 6, 8, tzinfo=UTC)
CFG = load_config()["links"]


def market(conn, venue, mid, title, subtitle=None, event=None, series=None, close=None, status="active",
           outcomes=("yes",)):
    conn.execute("INSERT INTO venues (venue, title, kind) VALUES (%s, %s, 'exchange') ON CONFLICT DO NOTHING",
                 (venue, venue))
    conn.execute("INSERT INTO markets (venue, market_id, event_id, series_id, title, subtitle, market_type, status, "
                 "close_time, first_seen, updated_at) VALUES (%s, %s, %s, %s, %s, %s, 'binary', %s, %s, %s, %s)",
                 (venue, mid, event, series, title, subtitle, status, close, NOW, NOW))
    for o in outcomes:
        conn.execute("INSERT INTO outcomes (venue, market_id, outcome, quoted_at) VALUES (%s, %s, %s, %s)",
                     (venue, mid, o, NOW))


def game(conn, gid, away, home, commence, sport="basketball_nba"):
    market(conn, "odds_api", gid, f"{away} at {home}", "NBA", series=sport, close=commence,
           outcomes=[f"{b}:{t}" for b in ("draftkings", "fanduel") for t in (away, home)])


def kalshi_event(conn, event, *sides, close=dt.datetime(2026, 11, 12, 2, tzinfo=UTC)):
    for code, sub in sides:
        market(conn, "kalshi", f"{event}-{code}", f"{sub} wins", sub, event=event, series=event.split("-")[0],
               close=close)


def resolve(conn, venue, mid, result):
    conn.execute("INSERT INTO resolutions (venue, market_id, result, value, settled_at) VALUES (%s, %s, %s, 1, %s)",
                 (venue, mid, result, NOW))


def all_links(conn):
    return conn.execute("SELECT market_id_a, outcome_a, market_id_b, outcome_b, source, status, confirmed_by, "
                        "resolved_agrees FROM market_links ORDER BY id").fetchall()


@pytest.mark.parametrize("sub, team, want", [
    ("San Antonio", "San Antonio Spurs", True),
    ("Los Angeles C", "Los Angeles Clippers", True),
    ("Los Angeles C", "Los Angeles Lakers", False),
    ("New York I", "New York Islanders", True),
    ("New York I", "New York Rangers", False),
    ("St. Louis", "St Louis Blues", True),
    ("Montréal", "Montreal Canadiens", True),
    ("Phoenix", "San Antonio Spurs", False),
    ("", "Phoenix Suns", False),
])
def test_names_team(sub, team, want):
    assert links.names_team(sub, team) is want


def test_kalshi_date_reads_the_ticker():
    assert links.kalshi_date("KXNBAGAME-26NOV09SASPHX") == dt.date(2026, 11, 9)
    assert links.kalshi_date("KXNBAGAME-26XYZ09SASPHX") is None and links.kalshi_date(None) is None


def test_pays():
    assert links.pays("yes", "yes") and links.pays("yes", "no") is False
    assert links.pays("San Antonio Spurs", "San Antonio Spurs") and links.pays("Phoenix Suns", "Draw") is False
    assert links.pays("yes", "void") is None and links.pays("Phoenix Suns", "split") is None


def test_rule_links_each_team_to_its_kalshi_market_on_the_eastern_date(conn):
    # 01:00Z on the 10th is the evening of the 9th in New York, the date in the Kalshi ticker
    game(conn, "g1", "San Antonio Spurs", "Phoenix Suns", dt.datetime(2026, 11, 10, 1, tzinfo=UTC))
    kalshi_event(conn, "KXNBAGAME-26NOV09SASPHX", ("SAS", "San Antonio"), ("PHX", "Phoenix"))
    kalshi_event(conn, "KXNBAGAME-26NOV10SASPHX", ("SAS", "San Antonio"), ("PHX", "Phoenix"))  # the wrong day
    assert links.run_rules(conn, CFG) == 2
    assert all_links(conn) == [
        ("g1", "San Antonio Spurs", "KXNBAGAME-26NOV09SASPHX-SAS", "yes", "rule:kalshi_game", "confirmed", "rule", None),
        ("g1", "Phoenix Suns", "KXNBAGAME-26NOV09SASPHX-PHX", "yes", "rule:kalshi_game", "confirmed", "rule", None)]
    links.run_rules(conn, CFG)  # a second run adds nothing
    assert len(all_links(conn)) == 2


def test_rule_leaves_ambiguous_games_alone(conn):
    commence = dt.datetime(2026, 11, 10, 1, tzinfo=UTC)
    # two events on the date: a doubleheader, so neither is sure
    game(conn, "dh", "San Antonio Spurs", "Phoenix Suns", commence)
    kalshi_event(conn, "KXNBAGAME-26NOV09SASPHX", ("SAS", "San Antonio"), ("PHX", "Phoenix"))
    kalshi_event(conn, "KXNBAGAME-26NOV09SASPHXG2", ("SAS", "San Antonio"), ("PHX", "Phoenix"))
    # a subtitle that names both teams of an all-LA game
    game(conn, "la", "Los Angeles Lakers", "Los Angeles Clippers", commence)
    kalshi_event(conn, "KXNBAGAME-26NOV09LALLAC", ("LAL", "Los Angeles"), ("LAC", "Los Angeles C"))
    assert links.run_rules(conn, CFG) == 0


def test_rule_ignores_a_tie_market_and_other_series(conn):
    commence = dt.datetime(2026, 11, 13, 0, tzinfo=UTC)
    game(conn, "nhl", "New York Islanders", "Los Angeles Kings", commence, sport="icehockey_nhl")
    kalshi_event(conn, "KXNHLGAME-26NOV12NYILA", ("NYI", "New York I"), ("LA", "Los Angeles"), ("TIE", "Tie"))
    game(conn, "soc", "Arsenal", "Chelsea", commence, sport="soccer_epl")  # no series configured
    assert links.run_rules(conn, CFG) == 2


class FakeLlm:
    model = "fake"

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.reply(prompt) if callable(self.reply) else self.reply


def test_llm_proposes_only_offered_outcomes_and_is_asked_once_per_market(conn):
    close = NOW + dt.timedelta(days=2)
    market(conn, "polymarket_us", "pm-sas", "San Antonio vs Phoenix", "San Antonio Spurs", close=close)
    kalshi_event(conn, "KXNBAGAME-26NOV09SASPHX", ("SAS", "San Antonio"), ("PHX", "Phoenix"),
                 close=close + dt.timedelta(days=2))
    market(conn, "kalshi", "KXFAR", "San Antonio wins the title", "San Antonio", close=close + dt.timedelta(days=30))

    def reply(prompt):
        cands = [json.loads(line.split(": ", 1)[1]) for line in prompt.split("Candidates:\n")[1].split("\n\n")[0].splitlines()]
        assert "KXFAR" not in [c["id"] for c in cands]  # outside the close window
        sas = [c["id"] for c in cands].index("KXNBAGAME-26NOV09SASPHX-SAS")
        return "<think>hmm</think>" + json.dumps({"matches": [
            {"candidate": sas, "outcome_a": "yes", "outcome_b": "yes", "confidence": 0.9, "reason": "same game"},
            {"candidate": 99, "outcome_a": "yes", "outcome_b": "yes"},
            {"candidate": 0, "outcome_a": "maybe", "outcome_b": "yes"}]})

    llm = FakeLlm(reply)
    cfg = {**CFG, "llm": {**CFG["llm"], "pairs": [["polymarket_us", "kalshi"]]}}
    assert links.run_llm(conn, cfg, llm, NOW) == (1, 1)
    assert all_links(conn) == [("pm-sas", "yes", "KXNBAGAME-26NOV09SASPHX-SAS", "yes", "llm:fake", "proposed", None, None)]
    assert links.run_llm(conn, cfg, llm, NOW) == (0, 0) and len(llm.prompts) == 1


def test_an_unusable_reply_proposes_nothing_but_counts_as_asked(conn):
    close = NOW + dt.timedelta(days=1)
    market(conn, "polymarket_us", "pm-x", "Phoenix game", None, close=close)
    market(conn, "kalshi", "KX-PHX", "Phoenix wins", "Phoenix", close=close)
    cfg = {**CFG, "llm": {**CFG["llm"], "pairs": [["polymarket_us", "kalshi"]]}}
    assert links.run_llm(conn, cfg, FakeLlm("no idea"), NOW) == (1, 0)
    assert conn.execute("SELECT proposed FROM market_link_asks").fetchall() == [(0,)]


def test_resolution_confirms_agreeing_links_and_rejects_disagreeing_ones(conn):
    game(conn, "g1", "San Antonio Spurs", "Phoenix Suns", dt.datetime(2026, 11, 10, 1, tzinfo=UTC))
    kalshi_event(conn, "KXNBAGAME-26NOV09SASPHX", ("SAS", "San Antonio"), ("PHX", "Phoenix"))
    links.run_rules(conn, CFG)
    market(conn, "polymarket_us", "pm-sas", "San Antonio vs Phoenix", close=NOW)
    market(conn, "polymarket_us", "pm-void", "Phoenix vs ?", close=NOW)
    conn.execute(links.INSERT, ("polymarket_us", "pm-sas", "yes", "kalshi", "KXNBAGAME-26NOV09SASPHX-PHX", "yes",
                                "llm:fake", 0.6, None, "proposed", None))  # a wrong proposal
    conn.execute(links.INSERT, ("polymarket_us", "pm-void", "yes", "kalshi", "KXNBAGAME-26NOV09SASPHX-SAS", "yes",
                                "llm:fake", 0.6, None, "proposed", None))
    assert links.check_resolutions(conn) == {"agree": 0, "disagree": 0, "void": 0}  # nothing resolved yet

    resolve(conn, "odds_api", "g1", "San Antonio Spurs")
    resolve(conn, "kalshi", "KXNBAGAME-26NOV09SASPHX-SAS", "yes")
    resolve(conn, "kalshi", "KXNBAGAME-26NOV09SASPHX-PHX", "no")
    resolve(conn, "polymarket_us", "pm-sas", "yes")
    resolve(conn, "polymarket_us", "pm-void", "void")
    assert links.check_resolutions(conn) == {"agree": 2, "disagree": 1, "void": 1}
    got = {(a, oa, b): (st, by, agrees) for a, oa, b, _, _, st, by, agrees in all_links(conn)}
    assert got == {
        ("g1", "San Antonio Spurs", "KXNBAGAME-26NOV09SASPHX-SAS"): ("confirmed", "rule", True),
        ("g1", "Phoenix Suns", "KXNBAGAME-26NOV09SASPHX-PHX"): ("confirmed", "rule", True),
        ("pm-sas", "yes", "KXNBAGAME-26NOV09SASPHX-PHX"): ("rejected", None, False),
        ("pm-void", "yes", "KXNBAGAME-26NOV09SASPHX-SAS"): ("proposed", None, None)}
    assert links.check_resolutions(conn) == {"agree": 0, "disagree": 0, "void": 0}  # each link is checked once


def test_main_without_a_key_runs_rules_and_the_check(conn, monkeypatch, capsys):
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    assert links.main([], now=NOW) == 0
    out = capsys.readouterr().out
    assert "LITELLM_API_KEY not set" in out and "resolutions:" in out


def test_a_market_with_no_candidates_does_not_use_up_the_ask_budget(conn):
    close = NOW + dt.timedelta(days=1)
    market(conn, "polymarket_us", "pm-lonely", "Curling final", None, close=close)
    market(conn, "polymarket_us", "pm-phx", "Phoenix game", None, close=close + dt.timedelta(hours=1))
    market(conn, "polymarket_us", "pm-phx2", "Phoenix other game", None, close=close + dt.timedelta(hours=2))
    market(conn, "kalshi", "KX-PHX", "Phoenix wins", "Phoenix", close=close)
    cfg = {**CFG, "llm": {**CFG["llm"], "pairs": [["polymarket_us", "kalshi"]], "max_asks": 1}}
    assert links.run_llm(conn, cfg, FakeLlm('{"matches": []}'), NOW) == (1, 0)
    assert conn.execute("SELECT market_id_a FROM market_link_asks").fetchall() == [("pm-phx",)]
