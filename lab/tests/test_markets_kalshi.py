import datetime as dt
import json
from pathlib import Path

import pytest

from markets.kalshi import Kalshi

FIX = Path(__file__).parent / "fixtures" / "markets"
CFG = {"base_url": "https://kalshi.test/v2", "page_limit": 2, "page_gap_s": 0.5, "retries": 2}


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    """Answers each GET from a queue of (status, fixture name) and records the params it was sent."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, params, timeout):
        self.calls.append((url, dict(params)))
        status, name = self.answers.pop(0)
        return Resp(status, json.loads((FIX / name).read_text()) if name else None)


def test_open_markets_follow_the_cursor_and_parse_dollar_strings():
    s, slept = Session((200, "kalshi_open_page1.json"), (200, "kalshi_open_page2.json")), []
    pages = list(Kalshi(CFG, s, sleep=slept.append).open_markets())
    assert [len(p) for p in pages] == [2, 2]
    assert s.calls[0][1] == {"status": "open", "mve_filter": "exclude", "limit": 2}
    assert s.calls[1][1]["cursor"] == json.loads((FIX / "kalshi_open_page1.json").read_text())["cursor"]
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
