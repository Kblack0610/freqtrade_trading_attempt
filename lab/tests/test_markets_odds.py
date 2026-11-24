"""Contract tests for the Odds API adapter, on responses shaped from the API's v4 docs (no key to record live yet)."""

import datetime as dt

import pytest
import requests
from lab.tests.markets_fakes import Resp, Session

from lab.sports import odds
from lab.sports.odds import OddsApi

UTC = dt.timezone.utc


def cfg(**kw):
    return {"base_url": "https://odds.test", "sports": ["basketball_nba"], "regions": "us", "page_gap_s": 1,
            "retries": 1, **kw}


def test_each_book_price_on_each_side_is_an_outcome_priced_as_a_buy():
    s = Session((200, "odds_api_odds.json"))
    [page] = list(OddsApi(cfg(), s, sleep=lambda _: None, key="k").open_markets())
    assert s.calls == [("https://odds.test/v4/sports/basketball_nba/odds", {
        "apiKey": "k", "regions": "us", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"})]
    assert [q.outcome for q in page] == ["draftkings:Phoenix Suns", "draftkings:San Antonio Spurs",
                                         "fanduel:Phoenix Suns", "fanduel:San Antonio Spurs"]  # only the configured h2h
    q = page[1]
    assert (q.market_id, q.series_id, q.title, q.subtitle, q.market_type) == (
        "e912304de2b2ce35b473ce2ecd3d1502", "basketball_nba", "San Antonio Spurs at Phoenix Suns", "NBA", "h2h")
    assert q.ask == pytest.approx(1 / 1.53) and q.bid is None and q.last is None
    assert q.close_time == dt.datetime(2026, 11, 10, 1, tzinfo=UTC)
    assert sum(p.ask for p in page[:2]) > 1  # the book's margin stays in


def test_configured_markets_are_one_call_and_each_line_is_its_own_market():
    s = Session((200, "odds_api_odds.json"))
    [page] = list(OddsApi(cfg(markets=["h2h", "totals", "spreads"]), s, sleep=lambda _: None, key="k").open_markets())
    assert s.calls[0][1]["markets"] == "h2h,totals,spreads"
    gid = "e912304de2b2ce35b473ce2ecd3d1502"
    by = {(q.market_id, q.market_type, q.outcome): q for q in page}
    assert sorted(k for k in by if k[1] != "h2h") == [
        (f"{gid}:spreads", "spreads", "fanduel:Phoenix Suns +4.5"),
        (f"{gid}:spreads", "spreads", "fanduel:San Antonio Spurs -4.5"),
        (f"{gid}:totals", "totals", "fanduel:Over 216.5"), (f"{gid}:totals", "totals", "fanduel:Under 216.5")]
    q = by[(f"{gid}:totals", "totals", "fanduel:Over 216.5")]
    assert q.ask == pytest.approx(1 / 1.87) and q.title == "San Antonio Spurs at Phoenix Suns"
    assert len([k for k in by if k[1] == "h2h"]) == 4 and all(k[0] == gid for k in by if k[1] == "h2h")


def test_a_finished_game_resolves_each_configured_market_from_its_score():
    s = Session((200, "odds_api_scores.json"))
    since = dt.datetime.now(UTC) - dt.timedelta(hours=30)
    rs = [r for page, _ in OddsApi(cfg(markets=["h2h", "totals", "spreads"]), s, sleep=lambda _: None,
                                   key="k").resolutions(since) for r in page]
    gid = "e912304de2b2ce35b473ce2ecd3d1502"
    assert [(r.market_id, r.result) for r in rs] == [
        (gid, "San Antonio Spurs"), (f"{gid}:totals", "Total 216"), (f"{gid}:spreads", "Phoenix Suns -8"),
        ("aa11", "Draw"), ("aa11:totals", "Total 198"), ("aa11:spreads", "Utah Jazz +0")]


def test_sports_are_paced_and_a_game_with_no_books_yields_no_quotes():
    s, slept = Session((200, "odds_api_odds.json"), (200, [])), []
    pages = list(OddsApi(cfg(sports=["basketball_nba", "icehockey_nhl"]), s, sleep=slept.append, key="k").open_markets())
    assert [len(p) for p in pages] == [4, 0] and slept == [1.0]
    assert s.calls[1][0].endswith("/v4/sports/icehockey_nhl/odds")


def test_resolutions_name_the_winner_or_a_draw_and_skip_unfinished_games():
    s = Session((200, "odds_api_scores.json"))
    since = dt.datetime.now(UTC) - dt.timedelta(hours=30)
    rs = [r for page, _ in OddsApi(cfg(), s, sleep=lambda _: None, key="k").resolutions(since) for r in page]
    assert s.calls[0][1] == {"apiKey": "k", "daysFrom": 2, "dateFormat": "iso"}
    assert [(r.market_id, r.result, r.value) for r in rs] == [
        ("e912304de2b2ce35b473ce2ecd3d1502", "San Antonio Spurs", 1.0), ("aa11", "Draw", 1.0)]
    assert rs[0].settled_at == dt.datetime(2026, 11, 10, 3, 31, 5, tzinfo=UTC)


def test_the_resolution_cursor_names_the_next_sport_and_an_unknown_one_starts_over():
    since = dt.datetime.now(UTC) - dt.timedelta(hours=30)
    api = OddsApi(cfg(sports=["basketball_nba", "icehockey_nhl"]), Session((200, []), (200, [])), sleep=lambda _: None,
                  key="k")
    assert [c for _, c in api.resolutions(since)] == ["icehockey_nhl", None]
    for cursor, want in (("icehockey_nhl", ["icehockey_nhl"]), ("dropped_sport", ["basketball_nba", "icehockey_nhl"])):
        s = Session((200, []), (200, []))
        list(OddsApi(cfg(sports=["basketball_nba", "icehockey_nhl"]), s, sleep=lambda _: None, key="k")
             .resolutions(since, cursor))
        assert [c[0].rsplit("/", 2)[-2] for c in s.calls] == want


def test_days_from_stays_within_what_the_api_takes():
    now = dt.datetime.now(UTC)
    for back, want in ((dt.timedelta(minutes=5), 1), (dt.timedelta(days=10), 3)):
        s = Session((200, []))
        list(OddsApi(cfg(), s, sleep=lambda _: None, key="k").resolutions(now - back))
        assert s.calls[0][1]["daysFrom"] == want


def test_no_key_refuses_to_start(monkeypatch):
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ODDS_API_KEY"):
        OddsApi(cfg())


class HttpErr(Resp):
    def raise_for_status(self):
        e = requests.HTTPError(f"401 for url https://odds.test/v4/sports/x/odds?apiKey=secret-key")
        e.response = self
        raise e


def test_an_http_error_never_carries_the_key():
    s = Session()
    s.get = lambda url, params, timeout: HttpErr(401)
    with pytest.raises(RuntimeError) as e:
        list(OddsApi(cfg(), s, sleep=lambda _: None, key="secret-key").open_markets())
    assert "HTTP 401" in str(e.value) and "secret-key" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_a_score_missing_a_side_is_not_a_result():
    e = {"id": "x", "completed": True, "home_team": "A", "away_team": "B", "scores": [{"name": "A", "score": "1"}]}
    assert odds.resolutions_of(e) == []


def test_a_run_counts_the_credits_it_spends(capsys):
    """One sport, h2h and totals, one region is 2 for the odds and 2 for the scores: the 4 a run docs/markets.md sizes."""
    api = OddsApi(cfg(markets=["h2h", "totals"]), Session((200, "odds_api_odds.json"), (200, "odds_api_scores.json")),
                  sleep=lambda _: None, key="k")
    list(api.open_markets())
    list(api.resolutions(dt.datetime.now(UTC) - dt.timedelta(hours=30)))
    assert api.credits == 4 and "4 this run" in capsys.readouterr().out
