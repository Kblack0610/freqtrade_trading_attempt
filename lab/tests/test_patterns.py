import numpy as np
import pandas as pd
import pytest

from lab import patterns


def frame(close, noise: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """OHLC from a close path: open is the previous close, high and low the candle body, widened
    by `noise` (relative) when given."""
    close = np.asarray(close, dtype=float)
    opn = np.r_[close[0], close[:-1]]
    rng = np.random.default_rng(seed)
    wick = np.abs(rng.normal(0, noise, len(close))) if noise else 0.0
    return pd.DataFrame({"open": opn, "high": np.maximum(opn, close) * (1 + wick),
                         "low": np.minimum(opn, close) * (1 - wick), "close": close, "volume": 1.0},
                        index=pd.date_range("2025-01-01", periods=len(close), freq="h", tz="UTC"))


def path(*points) -> np.ndarray:
    """A piecewise-linear close path through (bar, price) points, one value per bar."""
    bars, prices = zip(*points)
    return np.interp(np.arange(bars[-1] + 1), bars, prices)


def fired(sig: pd.Series) -> list[int]:
    return np.flatnonzero(sig.to_numpy()).tolist()


def test_a_swing_high_is_reported_k_bars_after_it():
    """Bars 10 and 11 both have a high of 110 (bar 11 opens at bar 10's close); ties go to the later
    bar, so the pivot is bar 11 and it is known at bar 16."""
    df = frame(path((0, 100), (10, 110), (30, 90)))
    piv = patterns.swing_pivots(df, k=5)
    assert piv["pivot_high"].notna().sum() == 1
    assert piv["pivot_high"].iloc[16] == 110 and piv["pivot_high"].iloc[:16].isna().all()


def test_a_close_through_the_last_swing_high_is_a_resistance_break():
    df = frame(path((0, 100), (10, 110), (20, 100), (40, 120)))
    first_above = int(np.flatnonzero(df["close"].to_numpy()[20:] > 110)[0]) + 20
    assert fired(patterns.resistance_break(df, k=3)) == [first_above]
    assert fired(patterns.resistance_break(df, k=3, max_age=5)) == []


def test_a_close_through_the_last_swing_low_is_a_support_break():
    df = frame(path((0, 110), (10, 100), (20, 110), (40, 90)))
    first_below = int(np.flatnonzero(df["close"].to_numpy()[20:] < 100)[0]) + 20
    assert fired(patterns.support_break(df, k=3)) == [first_below]


def test_double_bottom_fires_once_on_the_neckline_break():
    df = frame(path((0, 120), (10, 100), (20, 110), (30, 100.5), (45, 115)))
    first_above = int(np.flatnonzero(df["close"].to_numpy()[30:] > 110)[0]) + 30
    assert fired(patterns.double_bottom(df, k=3)) == [first_above]
    assert fired(patterns.double_top(df, k=3)) == []


def test_lows_too_far_apart_are_not_a_double_bottom():
    df = frame(path((0, 120), (10, 100), (20, 110), (30, 95), (45, 115)))
    assert fired(patterns.double_bottom(df, k=3, tol=0.01)) == []


def test_double_top_fires_on_the_neckline_break_down():
    df = frame(path((0, 90), (10, 110), (20, 100), (30, 109.5), (45, 95)))
    first_below = int(np.flatnonzero(df["close"].to_numpy()[30:] < 100)[0]) + 30
    assert fired(patterns.double_top(df, k=3)) == [first_below]


@pytest.mark.parametrize("kind, points, direction", [
    ("ascending", [(0, 104), (8, 110), (16, 100), (24, 110), (32, 103), (40, 110), (48, 106), (60, 115)], "up"),
    ("symmetric", [(0, 106), (8, 112), (16, 100), (24, 110), (32, 102), (40, 108), (48, 104), (56, 105), (66, 115)], "up"),
    ("descending", [(0, 106), (8, 112), (16, 100), (24, 109), (32, 100), (40, 106), (48, 100), (52, 103), (56, 101), (66, 90)], "down"),
])
def test_each_triangle_fires_once_on_its_break_and_no_other_shape_does(kind, points, direction):
    df = frame(path(*points))
    got = fired(patterns.triangle_break(df, kind, direction, k=3))
    assert len(got) == 1 and got[0] > 51  # after the third low is confirmed at bar 48 + 3
    other = "down" if direction == "up" else "up"
    assert fired(patterns.triangle_break(df, kind, other, k=3)) == []
    for shape in set(patterns.TRIANGLES) - {kind}:
        assert fired(patterns.triangle_break(df, shape, direction, k=3)) == []


def test_triangle_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        patterns.triangle_break(frame(path((0, 1), (10, 2))), "wedge")


def test_bull_flag_fires_on_the_break_above_the_flag():
    close = np.r_[np.full(21, 100.0), np.linspace(101, 110, 10), np.linspace(109.6, 107, 8), np.linspace(112, 130, 20)]
    df = frame(close)
    assert fired(patterns.bull_flag(df, pole=10, flag=8)) == [39]
    assert fired(patterns.bull_flag(df, pole=10, flag=8, impulse=0.2)) == []


def test_bear_flag_fires_on_the_break_below_the_flag():
    close = np.r_[np.full(21, 100.0), np.linspace(99, 90, 10), np.linspace(90.4, 93, 8), np.linspace(88, 70, 20)]
    assert fired(patterns.bear_flag(frame(close), pole=10, flag=8)) == [39]


def random_walk(n: int = 4000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return frame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), noise=0.004, seed=seed)


CAUSAL = {
    "swing_pivots": lambda d: patterns.swing_pivots(d, k=3),
    "last_level": lambda d: patterns.last_level(d, "high", k=3, max_age=50),
    "resistance_break": lambda d: patterns.resistance_break(d, k=3),
    "support_break": lambda d: patterns.support_break(d, k=3),
    "double_bottom": lambda d: patterns.double_bottom(d, k=3, tol=0.01, min_depth=0.01),
    "double_top": lambda d: patterns.double_top(d, k=3, tol=0.01, min_depth=0.01),
    **{f"triangle_{kind}_{way}": (lambda d, kind=kind, way=way: patterns.triangle_break(d, kind, way, k=3, flat=0.02))
       for kind in patterns.TRIANGLES for way in ("up", "down")},
    "bull_flag": lambda d: patterns.bull_flag(d, impulse=0.03),
    "bear_flag": lambda d: patterns.bear_flag(d, impulse=0.03),
}
CUTS = (50, 333, 1000, 2718, 3999)


def same_on_prefixes(fn, df) -> bool:
    full = fn(df)
    for t in CUTS:
        part, head = fn(df.iloc[:t]), full.iloc[:t]
        if not (part.equals(head) and part.index.equals(head.index)):
            return False
    return True


@pytest.mark.parametrize("name", CAUSAL)
def test_no_lookahead_prefix_and_full_frame_agree_on_every_earlier_row(name):
    df = random_walk()
    assert same_on_prefixes(CAUSAL[name], df)
    if name not in ("swing_pivots", "last_level"):
        assert CAUSAL[name](df).sum() > 0, "the signal never fired, so the check proved nothing"


def test_the_prefix_check_catches_a_planted_lookahead():
    """Negative control: the rule of fixtures/strategies/PlantedLookahead.py, which reads the next
    close, must fail the same check."""
    assert not same_on_prefixes(lambda d: d["close"].shift(-1) > d["close"] * 1.002, random_walk())
