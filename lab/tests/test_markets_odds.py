"""Contract tests for the Odds API adapter, on responses shaped from the API's v4 docs (no key to record live yet)."""

import datetime as dt

import pytest
import requests
from markets_fakes import Resp, Session

from lab.markets import odds
from lab.markets.odds import OddsApi

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
                                         "fanduel:Phoenix Suns", "fanduel:San Antonio Spurs"]  # spreads left out
    q = page[1]
    assert (q.market_id, q.series_id, q.title, q.subtitle, q.market_type) == (
        "e912304de2b2ce35b473ce2ecd3d1502", "basketball_nba", "San Antonio Spurs at Phoenix Suns", "NBA", "h2h")
    assert q.ask == pytest.approx(1 / 1.53) and q.bid is None and q.last is None
    assert q.close_time == dt.datetime(2026, 11, 10, 1, tzinfo=UTC)
    assert sum(p.ask for p in page[:2]) > 1  # the book's margin stays in


def test_sports_are_paced_and_a_game_with_no_books_yields_no_quotes():
    s, slept = Session((200, "odds_api_odds.json"), (200, [])), []
    pages = list(OddsApi(cfg(sports=["basketball_nba", "icehockey_nhl"]), s, sleep=slept.append, key="k").open_markets())
    assert [len(p) for p in pages] == [4, 0] and slept == [1.0]
    assert s.calls[1][0].endswith("/v4/sports/icehockey_nhl/odds")


def test_resolutions_name_the_winner_or_a_draw_and_skip_unfinished_games():
    s = Session((200, "odds_api_scores.json"))
    since = dt.datetime.now(UTC) - dt.timedelta(hours=30)
    rs = list(OddsApi(cfg(), s, sleep=lambda _: None, key="k").resolutions(since))
    assert s.calls[0][1] == {"apiKey": "k", "daysFrom": 2, "dateFormat": "iso"}
    assert [(r.market_id, r.result, r.value) for r in rs] == [
        ("e912304de2b2ce35b473ce2ecd3d1502", "San Antonio Spurs", 1.0), ("aa11", "Draw", 1.0)]
    assert rs[0].settled_at == dt.datetime(2026, 11, 10, 3, 31, 5, tzinfo=UTC)


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
    assert odds.resolution(e) is None
