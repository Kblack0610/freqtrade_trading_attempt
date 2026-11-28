"""lab/events/research/obs_lag.py: the minutes from the station report that decides a Kalshi weather market to the
first recorder quote the observed family could no longer trade."""

import datetime as dt

import pandas as pd

from lab import inputs
from lab.events.research import obs_lag

UTC = dt.timezone.utc
DAY = dt.date(2026, 11, 2)  # KNYC's local standard day starts 05:00 UTC
EVENT = "KXHIGHNY-26NOV02"


def t(h, m=0):
    return dt.datetime(2026, 11, 2, h, m, tzinfo=UTC)


# The high so far is 62 at 13:51 and 66 at 14:51: "63° or above" is decided yes (margin 1) by the 14:51 report.
OBS = {"KNYC": [(t(12, 51), 60, None), (t(13, 51), 62, None), (t(14, 51), 66, None)]}


def book(*quotes):
    """(market, hour, minute, bid, ask) rows as load_quotes returns them."""
    return pd.DataFrame([{"market_id": mid, "ts": pd.Timestamp(t(h, m)), "bid": b, "ask": a}
                         for mid, h, m, b, a in quotes])


def run(markets, quotes):
    return obs_lag.measure(markets, OBS, quotes, inputs.load(), 1, 0.03)


def high(mid, subtitle="63° or above", result="yes"):
    return (mid, EVENT, "KXHIGHNY", subtitle, result)


def test_the_lag_runs_from_the_deciding_report_to_the_first_untradeable_quote():
    q = book(("A", 13, 0, 0.50, 0.52), ("A", 14, 55, 0.55, 0.58), ("A", 15, 3, 0.97, 0.99))
    assert run([high("A")], q) == [("high", "lagged", 12.0)]  # 14:51 report, 15:03 reprice
    assert obs_lag.deciding(OBS["KNYC"], inputs.load()["nws_high"]["stations"]["KNYC"], "high", (63, None), DAY, 1) \
        == (t(14, 51), 1.0)


def test_a_market_priced_before_the_report_is_ahead_by_the_start_of_its_untradeable_run():
    q = book(("A", 11, 0, 0.50, 0.52), ("A", 12, 0, 0.98, 0.99), ("A", 15, 0, 0.98, 1.0))
    assert run([high("A")], q) == [("high", "ahead", -171.0)]  # untradeable from 12:00, 171 min before 14:51
    never = book(("A", 11, 0, 0.98, 0.99))
    assert run([high("A")], never) == [("high", "ahead", None)]


def test_every_other_market_lands_in_its_own_bucket():
    q = book(("H", 13, 0, 0.50, 0.52), ("H", 16, 0, 0.60, 0.62), ("M", 15, 0, 0.50, 0.52))
    rows = run([high("H"), high("M"), high("N"), high("U", "70° or above"), high("W", result="no"),
                ("X", EVENT, "KXHIGHNY", "not a range", "yes"), high("V", result="void"), ("Y", "KXHIGHTTTN-26NOV02", "KXHIGHTTTN", "63° or above", "yes")],
               q)
    assert rows == [("high", "held", None),  # still tradeable at its last quote
                    ("high", "missing", None),  # its first quote came after the report
                    ("high", "missing", None),  # no quote at all
                    ("high", "undecided", None),  # the day never reached 71
                    ("high", "wrong", None)]  # the report's side lost; the unparsed, void and unmapped markets are left out


def test_the_report_gives_each_kind_its_lag_quantiles_and_bucket_counts():
    rows = [("high", "lagged", m) for m in (2.0, 4.0, 6.0, 8.0, 40.0)] + [("high", "ahead", -100.0),
                                                                        ("high", "ahead", None), ("rain", "undecided", None)]
    lines = obs_lag.report(rows)
    assert lines[0].startswith("high: 5 lagged (median 6 min, p25 4, p90 27); ahead 2 (median 100 min before the "
                               "report, 1 never tradeable); held 0, missing 0, undecided 0, wrong 0")
    assert lines[1] == "rain: 0 lagged (no lagged market); ahead 0 (0 never tradeable); held 0, missing 0, undecided 1, wrong 0"
    assert lines[2].startswith("all: 5 lagged")
