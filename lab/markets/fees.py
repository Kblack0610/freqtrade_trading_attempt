"""What a venue charges, from `[backtest.fees.<venue>]` in lab/markets.toml. One model per venue, shared by the
backtester and the paper broker, so a paper fill and a backtest fill of the same order cost the same.

Two kinds (docs/markets.md, each venue's Fees):
- `pq`: a taker fee per order of `rate * C * P * (1 - P)` dollars, rounded per order as the venue states it:
  `ceil` up to the cent (Kalshi), `half_even` to the cent (Polymarket US), or `none`. Polymarket global's rate
  depends on the market's category, and the markets table holds none, so its rate is the highest category's.
- `profit`: a share of the profit on a winning settlement and nothing on entry (PredictIt).
A venue with no table (Manifold, play money) costs nothing.
"""

from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal

CENT = Decimal("0.01")
ROUNDING = {"ceil": ROUND_CEILING, "half_even": ROUND_HALF_EVEN}


def _cents(x: float, mode: str) -> float:
    if mode == "none":
        return x
    # 12 places first, so 1.7500000000000002 from float arithmetic is 1.75 and not ceiled to 1.76
    return float(Decimal(repr(x)).quantize(Decimal("1e-12")).quantize(CENT, rounding=ROUNDING[mode]))


def entry(fcfg: dict | None, contracts: float, price: float) -> float:
    """The fee in dollars for one taker order of `contracts` at `price` dollars."""
    if not fcfg or fcfg["kind"] != "pq" or contracts <= 0:
        return 0.0
    return _cents(fcfg["rate"] * contracts * price * (1.0 - price), fcfg.get("rounding", "none"))


def settlement(fcfg: dict | None, cost: float, payout: float) -> float:
    """The fee in dollars at settlement on a position that cost `cost` and paid `payout`, before entry fees."""
    if not fcfg or fcfg["kind"] != "profit":
        return 0.0
    return _cents(fcfg["share"] * max(0.0, payout - cost), fcfg.get("rounding", "none"))
