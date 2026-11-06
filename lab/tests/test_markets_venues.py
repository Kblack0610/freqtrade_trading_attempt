"""Contract tests for the P2 venue adapters, on responses recorded from each live API on 2026-11-06."""

import datetime as dt

import pytest
from markets_fakes import Session, fixture

from markets import predictit
from markets.manifold import Manifold
from markets.polymarket import Polymarket, PolymarketUS
from markets.predictit import PredictIt
from markets.recorder import ADAPTERS, load_config

UTC = dt.timezone.utc


def cfg(**kw):
    return {"base_url": "https://venue.test", "page_gap_s": 0.1, "retries": 2, **kw}


def nosleep(_):
    pass


def test_every_venue_in_the_config_has_an_adapter_and_the_bare_run_stays_kalshi():
    c = load_config()
    assert set(c["venues"]) == set(ADAPTERS) == {"kalshi", "polymarket_us", "polymarket", "predictit", "manifold", "odds_api"}
    assert c["recorder"]["default_venues"] == ["kalshi"]
    assert all(ADAPTERS[n].name == n for n in ADAPTERS)


def test_polymarket_us_pages_by_offset_and_reads_the_list_quotes():
    s, slept = Session((200, "polymarket_us_open_page1.json"), (200, "polymarket_us_open_page2.json")), []
    pages = list(PolymarketUS(cfg(page_limit=2), s, sleep=slept.append).open_markets())
    assert [len(p) for p in pages] == [2, 1]  # a short page ends the sweep
    assert s.calls[0] == ("https://venue.test/v1/markets", {"active": "true", "closed": "false", "limit": 2, "offset": 0})
    assert s.calls[1][1]["offset"] == 2 and slept == [0.1]
    q = pages[0][0]
    assert (q.market_id, q.outcome, q.status, q.market_type) == ("tec-mls-winner-2026-11-07-vwh", "yes", "open", "binary")
    assert (q.title, q.subtitle) == ("Major League Soccer Champion", "Vancouver Whitecaps FC")
    assert (q.bid, q.ask, q.last, q.bid_size, q.volume) == (0.165, 0.196, None, None, None)
    assert q.close_time == dt.datetime(2027, 1, 1, 21, 20, 9, tzinfo=UTC)


def test_polymarket_us_resolutions_read_the_long_side_and_stop_at_since():
    s = Session((200, "polymarket_us_closed.json"))
    rs = list(PolymarketUS(cfg(page_limit=500), s, sleep=nosleep).resolutions(dt.datetime(2026, 11, 6, 6, tzinfo=UTC)))
    assert s.calls[0][1] == {"closed": "true", "orderBy": "updated_at", "orderDirection": "desc", "limit": 500, "offset": 0}
    assert [(r.market_id.split("-")[-1], r.result, r.value) for r in rs] == [
        ("06", "yes", 1.0), ("3pt5", "no", 0.0), ("4pt5", "no", 0.0), ("sw3", "yes", 1.0)]
    assert rs[0].settled_at == dt.datetime(2026, 11, 6, 6, 51, 34, tzinfo=UTC)

    s = Session((200, "polymarket_us_closed.json"), (200, "polymarket_us_closed.json"))
    later = dt.datetime(2026, 11, 6, 7, tzinfo=UTC)  # every row is older: none read, and no second page asked for
    assert list(PolymarketUS(cfg(page_limit=4), s, sleep=nosleep).resolutions(later)) == [] and len(s.calls) == 1


def test_polymarket_global_follows_the_keyset_cursor():
    s, slept = Session((200, "polymarket_open_page1.json"), (200, "polymarket_open_page2.json")), []
    pages = list(Polymarket(cfg(page_limit=2), s, sleep=slept.append).open_markets())
    assert [len(p) for p in pages] == [2, 1]
    assert s.calls[0] == ("https://venue.test/markets/keyset", {"limit": 2})
    assert s.calls[1][1]["after_cursor"] == fixture("polymarket_open_page1.json")["next_cursor"]
    assert slept == [0.1]
    xi, newsom = pages[0]
    assert xi.market_id.startswith("0xa467b14d") and (xi.event_id, xi.series_id) == ("30828", "xi-jinping-out")
    assert (xi.status, xi.subtitle, xi.bid, xi.ask, xi.last) == ("active", None, 0.017, 0.018, 0.018)
    assert (newsom.subtitle, newsom.event_id) == ("Gavin Newsom", "30829")
    assert xi.open_time == dt.datetime(2025, 7, 3, 20, 37, 0, 228000, tzinfo=UTC)


def test_polymarket_global_resolutions_take_the_first_outcome_and_parse_short_offsets():
    s = Session((200, "polymarket_closed.json"))
    since = dt.datetime(2026, 11, 6, 6, 46, 20, 500000, tzinfo=UTC)
    rs = list(Polymarket(cfg(page_limit=100), s, sleep=nosleep).resolutions(since))
    assert s.calls[0][1] == {"closed": "true", "order": "closedTime", "ascending": "false", "limit": 100}
    assert [(r.result, r.value) for r in rs] == [("yes", 1.0), ("no", 0.0), ("no", 0.0)]  # the 4th closed before since
    assert rs[0].settled_at == dt.datetime(2026, 11, 6, 6, 46, 21, tzinfo=UTC)


def test_predictit_makes_each_contract_a_market_under_its_market_as_event():
    s = Session((200, "predictit_all.json"))
    [page] = list(PredictIt(cfg(), s, sleep=nosleep).open_markets())
    assert s.calls[0][0] == "https://venue.test/marketdata/all/"
    assert [(q.market_id, q.event_id, q.subtitle) for q in page] == [
        ("32831", "8345", None), ("32045", "8177", "Republican"), ("32046", "8177", "Democratic")]
    rep = page[1]  # buying yes is our ask, selling yes our bid; an empty side stays empty
    assert (rep.bid, rep.ask, rep.last, rep.status, rep.close_time) == (0.97, None, 0.98, "open", None)
    assert (page[0].bid, page[0].ask) == (0.91, 0.98)
    assert list(PredictIt(cfg(), Session(), sleep=nosleep).resolutions(dt.datetime(2026, 1, 1, tzinfo=UTC))) == []


def test_predictit_dates_are_eastern():
    assert predictit._eastern("2026-11-30T23:59:00") == dt.datetime(2026, 12, 1, 4, 59, tzinfo=UTC)
    assert predictit._eastern("NA") is None


def test_manifold_pages_by_before_time_and_stores_probability_as_last():
    s, slept = Session((200, "manifold_open_page1.json"), (200, "manifold_open_page2.json")), []
    pages = list(Manifold(cfg(page_limit=2), s, sleep=slept.append).open_markets())
    assert [len(p) for p in pages] == [2, 1]
    assert s.calls[0][1] == {"contractType": "BINARY", "limit": 2, "sort": "newest", "filter": "open"}
    assert s.calls[1][1]["beforeTime"] == 1793942769915 and slept == [0.1]
    q = pages[0][0]
    assert (q.market_id, q.status, q.bid, q.ask, q.last) == ("ICLn0OE9Pn", "open", None, None, pytest.approx(0.6))
    assert q.close_time == dt.datetime.fromtimestamp(1795237140, UTC)


def test_manifold_resolutions_map_cancel_to_void_and_stop_at_since():
    s = Session((200, "manifold_resolved.json"))
    since = dt.datetime.fromtimestamp(1793940850, UTC)
    rs = list(Manifold(cfg(page_limit=1000), s, sleep=nosleep).resolutions(since))
    assert s.calls[0][1] == {"contractType": "BINARY", "limit": 1000, "sort": "resolve-date", "filter": "resolved", "offset": 0}
    assert [(r.market_id, r.result, r.value) for r in rs] == [
        ("SPLttc9cQ8", "no", 0.0), ("RRIL6C0OQ2", "no", 0.0), ("yPq220tpN8", "void", None), ("pE0IcQdnCO", "yes", 1.0)]


def test_a_5xx_backs_off_then_raises_after_the_last_retry():
    s = Session((503, None), (503, None), (503, None))
    with pytest.raises(RuntimeError, match="503"):
        list(Manifold(cfg(page_limit=2), s, sleep=nosleep).open_markets())
