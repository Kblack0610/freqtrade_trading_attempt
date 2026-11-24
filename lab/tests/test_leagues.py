import tomllib
from pathlib import Path

import pytest

from lab.money import leagues
from lab.events import paper

GLOSSARY = Path(__file__).parents[2] / "web" / "glossary.toml"


def test_drawdown_is_the_fall_from_the_curves_peak():
    assert leagues.drawdown(1000, [10, -30, 5]) == pytest.approx(30 / 1010)
    assert leagues.drawdown(1000, [10, 10]) == 0.0  # only rising: a drawdown of nothing, not unknown
    assert leagues.drawdown(1000, []) is None and leagues.drawdown(0, [5]) is None


def test_every_league_row_has_the_same_columns():
    event = leagues.event_rows([{"account": "a", "family": "longshot", "params": {}, "capital": 1000, "equity": 1100,
                                 "realized": 90, "open_positions": 1, "fills": 4, "settled": 3, "retired_at": None,
                                 "stopped_at": None, "breached_at": None, "state": "running", "marked_at": None}],
                               {"a": [50, 40]}, paper.rule_of)[0]
    bot = {"name": "b", "strategy": "S", "ts": 1, "balance_total": 1050.0, "profit_all_abs": 50.0, "profit_closed_abs": 40.0,
           "open_trades": 1, "closed_trades": 2, "max_drawdown_pct": 3.0}
    crypto = leagues.crypto_rows([bot], {"b": {"breached_at": 1}}, lambda s: s)[0]
    assert tuple(k for k in event if k != "href") == tuple(k for k in crypto if k != "href") == leagues.COLUMNS
    assert event["ret"] == pytest.approx(0.1) and crypto["ret"] == pytest.approx(0.05)
    assert crypto["capital"] == 1000 and crypto["fills"] == 3 and crypto["max_drawdown"] == pytest.approx(0.03)
    assert crypto["watch"] == "day limit hit" and event["watch"] == "running"


def test_an_unmarked_account_has_no_return_and_a_retired_one_says_so():
    row = leagues.row("a", "", "r", 1000, None, 0, 0, 0, 0, None, leagues.watch_state(retired_at=1, stopped_at=1), None)
    assert row["ret"] is None and row["watch"] == "retired"


def test_a_rule_reads_in_words_and_a_params_mismatch_falls_back_to_the_family():
    params = {"buy": "yes", "lo": 0.9, "hi": 0.99, "hours": 24}
    assert paper.rule_of("longshot", params) == "longshot: buy yes when the yes mid is 0.90-0.99, within 24 h of close"
    assert paper.rule_of("longshot", {}) == "longshot"


def test_the_glossary_defines_every_scorecard_column():
    g = tomllib.loads(GLOSSARY.read_text())
    assert [c["name"].lower().replace(" ", "_") for c in g["column"]] == [
        {"ret": "return", "marked_at": "marked"}.get(c, c) for c in leagues.COLUMNS]
    assert all(t["means"] for t in g["term"]) and {"Paper", "League", "Account", "Replay account", "Cohort"} <= {
        t["name"] for t in g["term"]}


def test_the_glossary_holds_the_trading_terms_and_the_old_page_is_gone():
    g = tomllib.loads(GLOSSARY.read_text())
    names = {t["name"] for t in g["trading"]}
    assert {"Lot size", "Kelly criterion", "Closing line value (CLV)"} <= names
    assert all(t["group"] and t["means"] and t["uses"] for t in g["trading"])
    assert not (GLOSSARY.parents[1] / "docs" / "knowledge" / "glossary.md").exists()
