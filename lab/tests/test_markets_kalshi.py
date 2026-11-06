import datetime as dt

import pytest
from markets_fakes import Session, fixture

from lab.markets.kalshi import Kalshi

CFG = {"base_url": "https://kalshi.test/v2", "page_limit": 2, "page_gap_s": 0.5, "retries": 2}


def test_open_markets_follow_the_cursor_and_parse_dollar_strings():
    s, slept = Session((200, "kalshi_open_page1.json"), (200, "kalshi_open_page2.json")), []
    pages = list(Kalshi(CFG, s, sleep=slept.append).open_markets())
    assert [len(p) for p in pages] == [2, 2]
    assert s.calls[0][1] == {"status": "open", "mve_filter": "exclude", "limit": 2}
    assert s.calls[1][1]["cursor"] == fixture("kalshi_open_page1.json")["cursor"]
    assert slept == [0.5]  # spaced between pages, not after the last
    q = pages[0][0]
    assert (q.market_id, q.outcome, q.status, q.market_type) == ("KXNBA4QSPREAD-26NOV06PORLAL-POR9", "yes", "active", "binary")
    assert (q.bid, q.ask, q.last, q.bid_size, q.volume) == (0.01, 0.98, 0.0, 19.0, 0.0)
    assert (q.event_id, q.series_id) == ("KXNBA4QSPREAD-26NOV06PORLAL", "KXNBA4QSPREAD")
    assert q.close_time == dt.datetime(2026, 11, 9, 3, tzinfo=dt.timezone.utc)


def test_a_429_backs_off_exponentially_then_retries():
    s, slept = Session((429, None), (429, None), (200, "kalshi_open_page2.json")), []
    pages = list(Kalshi(CFG, s, sleep=slept.append).open_markets())
    assert [len(p) for p in pages] == [2]
    assert slept == [1.0, 2.0]


def test_a_429_past_the_retries_raises():
    s = Session((429, None), (429, None), (429, None))
    with pytest.raises(RuntimeError, match="429"):
        list(Kalshi(CFG, s, sleep=lambda _: None).open_markets())


def test_resolutions_ask_for_settled_since_and_read_the_result():
    s = Session((200, "kalshi_settled.json"))
    since = dt.datetime(2026, 11, 6, 4, tzinfo=dt.timezone.utc)
    rs = list(Kalshi(CFG, s, sleep=lambda _: None).resolutions(since))
    assert s.calls[0][1] == {"status": "settled", "mve_filter": "exclude", "min_settled_ts": int(since.timestamp()), "limit": 2}
    assert len(rs) == 3 and {r.result for r in rs} == {"no"} and rs[0].value == 0.0
    assert rs[0].settled_at.tzinfo is not None
