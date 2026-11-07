import datetime as dt
import json

from lab import pairs, universe

D = dt.date


def test_pick_keeps_the_liquid_pairs_with_history_by_volume():
    volumes = {"BTC/USDT": 400e6, "NEW/USDT": 90e6, "ETH/USDT": 200e6, "THIN/USDT": 1e6, "SOL/USDT": 50e6}
    firsts = {"BTC/USDT": D(2019, 1, 1), "NEW/USDT": D(2024, 3, 1), "ETH/USDT": D(2019, 1, 1),
              "THIN/USDT": D(2019, 1, 1), "SOL/USDT": D(2021, 9, 1)}
    then = {"BTC/USDT": 300e6, "ETH/USDT": 150e6, "SOL/USDT": 20e6, "THIN/USDT": 5e6, "NEW/USDT": 0}
    assert pairs.pick(volumes, then, firsts, 40, D(2022, 1, 1), 2e6) == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    assert pairs.pick(volumes, then, firsts, 2, D(2022, 1, 1), 2e6) == ["BTC/USDT", "ETH/USDT"]


def test_a_pair_thin_in_the_first_year_is_dropped_however_liquid_it_is_now():
    volumes = {"BTC/USDT": 400e6, "FADED/USDT": 80e6}
    firsts = {"BTC/USDT": D(2019, 1, 1), "FADED/USDT": D(2019, 1, 1)}
    then = {"BTC/USDT": 300e6, "FADED/USDT": 0.4e6}
    assert pairs.pick(volumes, then, firsts, 40, D(2022, 1, 1), 2e6) == ["BTC/USDT"]
    assert pairs.pick(volumes, {**then, "FADED/USDT": 3e6}, firsts, 40, D(2022, 1, 1), 2e6) == ["BTC/USDT", "FADED/USDT"]


class FakeExchange:
    """ccxt-shaped: 1d candles [ts, o, h, l, c, volume] with close 10 and the base volume given per window."""
    has = {"fetchTickers": True}

    def __init__(self, now_volume, then_volume, first=D(2019, 1, 1)):
        self.now_volume, self.then_volume, self.first = now_volume, then_volume, first

    def load_markets(self):
        return {"BTC/USDT": {"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "spot": True, "active": True}}

    def fetch_tickers(self, symbols):
        return {s: {"quoteVolume": 1} for s in symbols}

    def fetch_ohlcv(self, pair, tf, since, limit):
        day = lambda d, v: [int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000), 0, 0, 0, 10, v]
        if limit == 1:
            return [day(self.first, 1)]
        if since >= int(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000):
            return [day(D(2026, 10, 1) + dt.timedelta(days=i), self.now_volume) for i in range(limit)]
        return [day(D(2022, 1, 1) + dt.timedelta(days=i), self.then_volume) for i in range(limit)]


def test_survey_measures_volume_now_and_from_the_listing_cutoff():
    ex = FakeExchange(now_volume=1e6, then_volume=0.1e6)
    now = dt.datetime(2026, 11, 7, tzinfo=dt.timezone.utc)
    volumes, then, firsts = pairs.survey(ex, "USDT", now, D(2022, 1, 1), 2e6)
    assert volumes == {"BTC/USDT": 1e7} and then == {"BTC/USDT": 1e6} and firsts == {"BTC/USDT": D(2019, 1, 1)}
    # A pair under the floor now is not asked for its old volume.
    volumes, then, _ = pairs.survey(FakeExchange(0.1e6, 1e6), "USDT", now, D(2022, 1, 1), 2e6)
    assert volumes == {"BTC/USDT": 1e6} and then == {}


def test_write_records_the_list_as_current_listing_beside_the_whitelist(tmp_path, monkeypatch):
    (tmp_path / "user_data" / "exchanges").mkdir(parents=True)
    (tmp_path / "user_data" / "exchanges" / "okx.json").write_text(json.dumps({"fee": 0.002, "exchange": {"pair_whitelist": []}}))
    monkeypatch.setattr(pairs, "REPO", tmp_path)
    pairs.write("okx", ["BTC/USDT"], {"BTC/USDT": 4e8}, {"BTC/USDT": 3e8}, D(2022, 1, 1), 2e6, D(2026, 11, 7))
    cfg = json.loads((tmp_path / "user_data" / "exchanges" / "okx.json").read_text())
    assert cfg["fee"] == 0.002 and cfg["exchange"]["pair_whitelist"] == ["BTC/USDT"] and "universe" not in cfg
    rec = universe.describe(tmp_path / "user_data", "okx", ["BTC/USDT"])
    assert rec == {"source": "current-listing", "recorded": True, "pairs": 1, "as_of": "2026-11-07",
                   "listed_by": "2022-01-01", "min_volume": 2e6}


def test_pegged_and_leveraged_tokens_are_not_research_pairs():
    m = {"spot": True, "active": True, "quote": "USDT"}
    assert pairs.tradable({**m, "base": "BTC"}, "USDT")
    assert not pairs.tradable({**m, "base": "USDC"}, "USDT")
    assert not pairs.tradable({**m, "base": "BTC3L"}, "USDT")
    assert not pairs.tradable({**m, "base": "ETHUP"}, "USDT")
    assert pairs.tradable({**m, "base": "UP"}, "USDT")
    assert not pairs.tradable({**m, "base": "BTC"}, "USD")
