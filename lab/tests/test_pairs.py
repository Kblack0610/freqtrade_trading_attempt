import datetime as dt

import pairs

D = dt.date


def test_pick_keeps_the_liquid_pairs_with_history_by_volume():
    volumes = {"BTC/USDT": 400e6, "NEW/USDT": 90e6, "ETH/USDT": 200e6, "THIN/USDT": 1e6, "SOL/USDT": 50e6}
    firsts = {"BTC/USDT": D(2019, 1, 1), "NEW/USDT": D(2024, 3, 1), "ETH/USDT": D(2019, 1, 1),
              "THIN/USDT": D(2019, 1, 1), "SOL/USDT": D(2021, 9, 1)}
    assert pairs.pick(volumes, firsts, 40, D(2022, 1, 1), 2e6) == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    assert pairs.pick(volumes, firsts, 2, D(2022, 1, 1), 2e6) == ["BTC/USDT", "ETH/USDT"]


def test_pegged_and_leveraged_tokens_are_not_research_pairs():
    m = {"spot": True, "active": True, "quote": "USDT"}
    assert pairs.tradable({**m, "base": "BTC"}, "USDT")
    assert not pairs.tradable({**m, "base": "USDC"}, "USDT")
    assert not pairs.tradable({**m, "base": "BTC3L"}, "USDT")
    assert not pairs.tradable({**m, "base": "ETHUP"}, "USDT")
    assert pairs.tradable({**m, "base": "UP"}, "USDT")
    assert not pairs.tradable({**m, "base": "BTC"}, "USD")
