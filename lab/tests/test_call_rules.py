"""Event-to-call rules: a fixture event makes exactly one call and one trial, a repeat or a second event before the
same close makes no second call, a stale or weak event makes none, an edited rule is a new trial, rule trials stay
out of the crypto bar, and records() judges a rule against every rule trial."""

import datetime as dt

import psycopg
import pytest
from psycopg.types.json import Jsonb

from lab.inputs import call_rules, calls
from lab.crypto import confidence

ET = calls.ET
RULE = {"name": "filing-shock", "thesis": "8-K shock", "sources": ["edgar"], "event_types": ["management"],
        "min_abs_sentiment": 0.5, "signal_source": "event", "horizons": [1, 5, 20]}
CFG = {"max_age_hours": 24, "tickers": {"NVDA": {"benchmark": "SMH"}}, "record": {"min_calls": 3, "dsr_min": 0.95},
       "rules": [RULE]}
# Thu 2026-11-12, 11:00 ET: the CEO ousted, an 8-K filed and tagged an hour earlier
NOW = dt.datetime(2026, 11, 12, 11, tzinfo=ET)


def item(**kw) -> dict:
    return {"source": "edgar", "id": "0001045810-26-000123", "ingested_at": NOW - dt.timedelta(hours=1),
            "url": "https://www.sec.gov/Archives/edgar/data/1045810/x.htm", "text": "NVIDIA 8-K: CEO departure",
            "tickers": ["NVDA"], "tags": {"event_type": "management", "sentiment": -0.8}, **kw}


@pytest.fixture
def lab(conn):
    conn.execute("DELETE FROM trials WHERE exchange = 'calls'")  # the register is not truncated between tests
    yield conn
    conn.execute("DELETE FROM trials WHERE exchange = 'calls'")


def test_a_rule_over_a_fixture_event_makes_one_call_and_one_trial(lab):
    crypto = confidence.trial_count(lab)
    out = call_rules.run(lab, [item()], NOW, CFG)
    assert out == {"rule_trials": 1, "rule_calls": 1, "rule_events": 1}
    assert lab.execute("SELECT strategy, variant FROM trials WHERE exchange = 'calls'").fetchall() == \
        [("rule:filing-shock", call_rules.variant(RULE))]
    assert confidence.trial_count(lab) == crypto  # another lane's tries do not raise the crypto bar
    got = lab.execute("SELECT who, author, ticker, direction, instrument, entry_session, horizons, benchmark, source, "
                      "signal_source, created_at FROM calls").fetchall()
    assert len(got) == 1
    who, author, ticker, direction, instrument, entry, horizons, bench, source, signal, created = got[0]
    assert (who, author, ticker, direction, instrument, bench, signal) == \
        ("rule", "rule:filing-shock", "NVDA", "short", "equity", "SMH", "event")
    # the entry is the first close still ahead when the call is written, and the server stamps it
    assert entry == dt.date(2026, 11, 12) and calls.close_at(entry) > NOW and horizons == [1, 5, 20]
    assert source == item()["url"] and created is not None
    # a repeat run is idempotent
    assert call_rules.run(lab, [item()], NOW, CFG)["rule_events"] == 0
    assert lab.execute("SELECT count(*) FROM calls").fetchone()[0] == 1


def test_a_second_event_before_the_same_close_joins_the_first_call(lab):
    call_rules.run(lab, [item(), item(id="0001045810-26-000124")], NOW, CFG)
    assert lab.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
    assert lab.execute("SELECT count(DISTINCT call_id), count(*) FROM call_rule_events").fetchone() == (1, 2)
    # after that close prints, the next event is a new call on the next session
    later = dt.datetime(2026, 11, 12, 17, tzinfo=ET)
    call_rules.run(lab, [item(id="0001045810-26-000125", ingested_at=later)], later, CFG)
    assert [r[0] for r in lab.execute("SELECT entry_session FROM calls ORDER BY id")] == \
        [dt.date(2026, 11, 12), dt.date(2026, 11, 13)]


def test_weak_untagged_off_rule_and_stale_events_make_no_call(lab):
    negatives = [item(id="weak", tags={"event_type": "management", "sentiment": -0.3}),
                 item(id="neutral", tags={"event_type": "management", "sentiment": 0}),
                 item(id="product", tags={"event_type": "product", "sentiment": -0.9}),
                 item(id="rss", source="rss"),
                 item(id="stale", ingested_at=NOW - dt.timedelta(hours=25))]
    out = call_rules.run(lab, negatives, NOW, CFG)
    assert out["rule_calls"] == 0 and lab.execute("SELECT count(*) FROM calls").fetchone()[0] == 0
    assert out["rule_trials"] == 1  # registered even though it made no call
    assert call_rules.run(lab, [item(id="good", tags={"event_type": "management", "sentiment": 0.7})], NOW,
                          CFG)["rule_calls"] == 1
    assert lab.execute("SELECT direction FROM calls").fetchone()[0] == "long"


def test_an_edited_rule_is_a_new_trial_and_events_are_append_only(lab):
    call_rules.run(lab, [item()], NOW, CFG)
    edited = {**CFG, "rules": [{**RULE, "min_abs_sentiment": 0.4}]}
    out = call_rules.run(lab, [item()], NOW, edited)
    assert out["rule_trials"] == 2 and out["rule_calls"] == 1  # the edited rule's own call, its own trial
    for sql in ("UPDATE call_rule_events SET ticker = 'AMD'", "DELETE FROM call_rule_events"):
        with pytest.raises(psycopg.errors.RaiseException):
            lab.execute(sql)


def test_the_feed_query_reads_only_tagged_watchlist_items_since_the_window(lab):
    lab.execute("CREATE TEMP TABLE feed_items (source text, id text, ts timestamptz, ingested_at timestamptz, url text, "
                "text text, entities jsonb, tickers text[], tags jsonb, topic text)")
    rows = [("edgar", "a", NOW, "{NVDA}", Jsonb({"event_type": "management", "sentiment": -0.8})),
            ("edgar", "untagged", NOW, "{NVDA}", None),
            ("rss", "off-watchlist", NOW, "{}", Jsonb({"event_type": "macro", "sentiment": 0.1})),
            ("edgar", "old", NOW - dt.timedelta(days=2), "{NVDA}", Jsonb({"event_type": "management", "sentiment": -0.8}))]
    lab.cursor().executemany("INSERT INTO feed_items (source, id, ingested_at, url, text, tickers, tags, topic) "
                             "VALUES (%s, %s, %s, 'u', 't', %s, %s, 'nvda')", rows)
    found = call_rules.items(lab, NOW - dt.timedelta(hours=24))
    assert [f["id"] for f in found] == ["a"] and found[0]["tags"]["sentiment"] == -0.8
    lab.execute("DROP TABLE feed_items")


def test_records_judge_a_rule_against_every_rule_trial():
    rows = [{"rule": "filing-shock", "variant": "v", "horizon": 5, "pnl": p} for p in (0.02, 0.03, 0.025, 0.028)]
    one = call_rules.records(rows, 1, CFG)[0]
    many = call_rules.records(rows, 50, CFG)[0]
    assert one["n"] == 4 and one["mean"] == pytest.approx(0.02575)
    assert many["dsr"] < one["dsr"]  # more rules tried, a higher bar
    assert one["edge"] is True
    assert call_rules.records(rows[:2], 1, CFG)[0]["edge"] is False  # under min_calls


def test_the_config_registers_valid_unique_rules():
    cfg = call_rules.load_config()
    assert cfg["rules"] and len({call_rules.variant(r) for r in cfg["rules"]}) == len(cfg["rules"])
    for r in cfg["rules"]:
        assert set(call_rules.SETTINGS) <= r.keys() and r["thesis"]
