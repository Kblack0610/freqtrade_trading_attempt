"""Api.get pacing on a fake clock, and the shared venue budget against Postgres."""

import email.utils
import time

import pytest

from lab.markets.api import Api, RECOVER_S, retry_after
from lab.markets.budget import Budget
from lab.markets.recorder import load_config
from lab.tests.markets_fakes import Resp, Session


class Clock:
    """Sleeping advances time and is recorded; nothing really waits."""

    def __init__(self):
        self.t, self.slept = 0.0, []

    def sleep(self, s):
        self.slept.append(round(s, 6))
        self.t += s


class Shared:
    """A budget stand-in: hands out the waits given, records holds."""

    def __init__(self, *waits):
        self.waits, self.holds = list(waits), []

    def take(self):
        return self.waits.pop(0) if self.waits else 0.0

    def hold(self, secs):
        self.holds.append(secs)


def api(*answers, budget=None, **kw):
    clock = Clock()
    a = Api("v", {"base_url": "https://venue.test", "page_gap_s": 0.1, "retries": 3, **kw}, Session(*answers),
            sleep=clock.sleep, budget=budget)
    return a, clock


def test_429_widens_the_gap_for_the_rest_of_the_run_and_successes_walk_it_back():
    a, clock = api((200, {}), (429, {}), (429, {}), (200, {}), (200, {}))
    a.get("/a")  # the first request waits for nothing
    a.get("/b")  # paced, 429 (gap 0.2, back off 1 s), paced, 429 (gap 0.4, back off 2 s), paced, ok
    assert clock.slept == [0.1, 1.0, 0.2, 2.0, 0.4]
    assert a.pace == pytest.approx(0.4 - RECOVER_S)
    a.get("/c")  # the next request still keeps the wider gap
    assert clock.slept[-1] == pytest.approx(0.4 - RECOVER_S)


def test_the_gap_never_recovers_below_page_gap_s():
    a, clock = api(*[(200, {})] * 5)
    for _ in range(5):
        a.get("/x")
    assert clock.slept == [0.1] * 4 and a.pace == 0.1


def test_retry_after_is_honoured_in_seconds_and_as_a_date():
    a, clock = api(Resp(429, {}, {"Retry-After": "7"}), (200, {}))
    a.get("/x")
    assert clock.slept == [7.0, 0.2]
    later = email.utils.formatdate(time.time() + 30, usegmt=True)
    assert 28 <= retry_after(Resp(429, {}, {"Retry-After": later})) <= 30
    assert retry_after(Resp(429, {})) is None and retry_after(Resp(429, {}, {"Retry-After": "soon"})) is None


def test_the_shared_budget_paces_every_request_and_a_429_holds_it_for_everyone():
    shared = Shared(0.0, 0.5, 0.25)
    a, clock = api((200, {}), (429, {}), (200, {}), budget=shared)
    a.get("/a")
    a.get("/b")
    # /a: no wait. /b: gap 0.1, budget 0.5, 429 holds the budget 1 s and waits it, then gap 0.2 and budget 0.25.
    assert clock.slept == [0.1, 0.5, 1.0, 0.2, 0.25]
    assert shared.holds == [1.0]


def test_retries_run_out_on_a_429():
    a, _ = api(*[(429, {})] * 4)
    with pytest.raises(RuntimeError, match="HTTP 429"):
        a.get("/x")


def test_every_venue_has_a_shared_budget():
    venues = load_config()["venues"]
    for name, cfg in venues.items():
        assert cfg.get("budget_rps", 0) > 0, name
        assert isinstance(Api(name, cfg).budget, Budget)  # no connection until the first request


def test_the_budget_queues_every_caller_of_a_venue_at_its_rate(conn):
    """Three callers of one venue take turns 0.1 s apart; another venue is not slowed. now() is frozen in a
    transaction, so the database's clock stands still like a fake one."""
    callers = [Budget("kalshi", 10, conn) for _ in range(3)]
    other = Budget("manifold", 5, conn)
    with conn.transaction():
        waits = [b.take() for _ in range(2) for b in callers]
        assert waits == pytest.approx([0, 0.1, 0.2, 0.3, 0.4, 0.5])
        assert other.take() == 0
        callers[0].hold(5)  # a 429: nobody calls kalshi for 5 s
        assert callers[1].take() == pytest.approx(5)
        assert callers[2].take() == pytest.approx(5.1)
        assert other.take() == pytest.approx(0.2)


def test_a_budget_refuses_no_rate():
    with pytest.raises(ValueError):
        Budget("kalshi", 0)
