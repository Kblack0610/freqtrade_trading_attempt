"""The overview's lane cards (web/lanes.py) from plain rows: no database."""

import datetime as dt

from web import lanes

NOW = dt.datetime(2026, 11, 13, 12, tzinfo=dt.timezone.utc)


def empty(**over) -> dict:
    read = {"stale_h": 2, "bots": [], "paper": [], "paper_max_age_h": {}, "shadow_counts": [], "shadow_newest": None, "calls": [],
            "calls_cfg": {"slack_days": 4}, "venues": [], "recorder": [], "recorder_ages": (15, {}), "coverage": [],
            "runs": [], "confidence": {"ready": 0, "n": 0}, "readiness": {"ready": 0, "n": 0}, "suggestions": [],
            "bankroll": None, "watch": [], "guard_lifted": False}
    return read | over


def bot(name: str, hours: float | None = 0.5, dry_run: bool = True, error: str | None = None, profit: float = 10) -> dict:
    return {"name": name, "ts": None if hours is None else NOW - dt.timedelta(hours=hours), "dry_run": dry_run,
            "error": error, "open_trades": 1, "closed_trades": 2, "profit_all_abs": profit}


def by_id(cards: list[dict]) -> dict:
    return {c["id"]: c for c in cards}


def test_every_lane_in_the_toml_has_a_reader_and_a_card():
    assert [lane["id"] for lane in lanes.load()] and {lane["id"] for lane in lanes.load()} == set(lanes.READERS)
    assert len(lanes.cards(empty(), NOW)) == len(lanes.READERS)


def test_an_empty_lane_is_no_data_never_green():
    cards = by_id(lanes.cards(empty(), NOW))
    for lane, c in cards.items():
        if lane != "money":  # the guard is code, so it is counted even with no rows
            assert c["level"] == "nodata" and c["big"] == "-", lane
    assert cards["money"]["level"] == "green" and cards["money"]["big"] == "Dry run"


def test_a_stale_bot_turns_the_crypto_lane_amber_and_sorts_it_before_green():
    cards = lanes.cards(empty(bots=[bot("a"), bot("b", hours=5)]), NOW)
    crypto = by_id(cards)["crypto"]
    assert crypto["level"] == "amber" and crypto["big"] == "1" and crypto["unit"] == "of 2 bots reporting"
    assert [r["text"] for r in crypto["first"]] == ["b"]
    assert cards[0]["id"] == "crypto"  # the first amber; every other lane is no data or green


def test_a_bot_off_dry_run_is_red_on_crypto_and_money():
    cards = by_id(lanes.cards(empty(bots=[bot("a", dry_run=False)]), NOW))
    assert cards["crypto"]["level"] == "red" and cards["money"]["level"] == "red"
    assert cards["money"]["first"][0]["sub"] == "not dry run"


def test_profit_is_each_lanes_own_and_never_summed_across_lanes():
    paper = [{"account": "p1", "venue": "kalshi", "marked_at": NOW, "equity": 1020, "capital": 1000, "fills": 3,
              "settled": 1, "open_positions": 2}]
    cards = by_id(lanes.cards(empty(bots=[bot("a", profit=25)], paper=paper, paper_max_age_h={"kalshi": 1}), NOW))
    pnl = {lane: {p["label"]: p["value"] for p in c["parts"]}.get("paper P&L") for lane, c in cards.items()}
    assert pnl["crypto"] == "25.00 USD" and pnl["betting"] == "20.00 USD"
    assert "45.00 USD" not in str(cards)


def test_a_paper_account_is_stale_by_its_venues_age():
    paper = [{"account": "p1", "venue": "manifold", "marked_at": NOW - dt.timedelta(hours=3), "equity": 1000,
              "capital": 1000, "fills": 0, "settled": 0, "open_positions": 0}]
    assert by_id(lanes.cards(empty(paper=paper), NOW))["betting"]["level"] == "amber"
    assert by_id(lanes.cards(empty(paper=paper, paper_max_age_h={"manifold": 6}), NOW))["betting"]["level"] == "green"


def test_an_overdue_call_is_red_and_a_waiting_one_is_not():
    call = {"ticker": "SPY", "direction": "long", "entry_session": NOW.date() - dt.timedelta(days=30),
            "horizons": [1, 5], "scores": {"1": {"pnl": 0.01}}}
    cards = by_id(lanes.cards(empty(calls=[call]), NOW))
    assert cards["calls"]["level"] == "red" and cards["calls"]["first"][0]["sub"] == "5-session horizon unscored"
    fresh = call | {"entry_session": NOW.date()}
    assert by_id(lanes.cards(empty(calls=[fresh]), NOW))["calls"]["level"] == "green"


def test_a_failed_analysis_is_red_and_one_that_never_finished_amber():
    run = {"id": 7, "name": "nightly", "started_at": NOW - dt.timedelta(hours=2), "finished_at": NOW, "ok": False}
    assert by_id(lanes.cards(empty(runs=[run]), NOW))["research"]["level"] == "red"
    good = run | {"id": 6, "ok": True, "finished_at": NOW - dt.timedelta(hours=40)}
    killed = run | {"finished_at": None, "ok": None, "started_at": NOW - dt.timedelta(hours=14)}
    research = by_id(lanes.cards(empty(runs=[killed, good]), NOW))["research"]
    assert research["level"] == "amber"
    assert [r["sub"] for r in research["first"]] == ["started, not finished", "nightly"]
    fresh = good | {"finished_at": NOW - dt.timedelta(hours=3)}
    assert by_id(lanes.cards(empty(runs=[killed, fresh]), NOW))["research"]["level"] == "green"


def test_look_first_rows_stop_at_three_and_count_the_rest():
    crypto = by_id(lanes.cards(empty(bots=[bot(f"b{i}", hours=None) for i in range(5)]), NOW))["crypto"]
    assert len(crypto["first"]) == lanes.SHOWN_FIRST and crypto["more"] == 2


def test_a_venue_is_stale_by_its_own_sweep_cadence():
    venue = {"venue": "odds_api", "started_at": NOW, "ok": True, "error": None, "failed_1d": 0,
             "last_ok": NOW - dt.timedelta(hours=2)}
    assert by_id(lanes.cards(empty(recorder=[venue]), NOW))["recorder"]["level"] == "amber"
    six_hourly = empty(recorder=[venue], recorder_ages=(15, {"odds_api": 390}))
    assert by_id(lanes.cards(six_hourly, NOW))["recorder"]["level"] == "green"
