"""Research-only HAR stake scaling of dated tuned OOS trades, counted and scored beside the original.

The open day's forecast uses only previous closes. Its eligibility was decided from earlier months' QLIKE,
never the whole-history verdict. Missing or ineligible forecasts allocate no capital. Ratios are scaled returns
of the research sleeve, not returns of a leveraged trade; no bot or roster consumes this variant.
"""

import math

from lab import suggest
from lab.grid import variant_of


def scale(variance: float | None, eligible: bool, cfg: dict) -> float:
    c = cfg["volatility"]
    if not 0 < c["target_max_scale"] <= 1 or c["target_annual"] <= 0:
        raise ValueError("HAR target requires a positive target and a maximum scale in (0, 1]")
    if not eligible or variance is None or not math.isfinite(variance) or variance <= 0:
        return 0.0
    return min(c["target_max_scale"], c["target_annual"] / math.sqrt(365 * variance))


def run(conn, cfg: dict | None = None) -> dict:
    """Rebuild the HAR sleeve in the canonical dated-trade store; the daily view and climate scorecard read it."""
    cfg = cfg or suggest.load_config()
    scale(None, False, cfg)
    rows = conn.execute("""
        SELECT t.strategy, t.exchange, t.fold_start, t.pair, t.open_date, t.close_date, t.profit_ratio,
               f.variance, coalesce(f.eligible, false)
        FROM walkforward_trades t JOIN walkforward w USING (strategy, exchange, fold_start)
        LEFT JOIN vol_forecasts f ON f.exchange = t.exchange AND f.pair = t.pair
            AND f.day = (t.open_date AT TIME ZONE 'UTC')::date
        WHERE t.variant = 'tuned' AND w.ok AND w.test_end <= %s
        ORDER BY t.strategy, t.exchange, t.fold_start, t.open_date, t.close_date, t.pair
        """, (cfg["lockbox"]["start"],)).fetchall()
    out, active, missing = [], 0, 0
    for s, e, fold, pair, opened, closed, ret, variance, eligible in rows:
        factor = scale(variance, eligible, cfg)
        active += factor > 0
        missing += variance is None
        out.append((s, e, "har_target", fold, pair, opened, closed, ret * factor))
    spec = {"kind": "har_target", "volatility": cfg["volatility"]}
    with conn.transaction():
        conn.execute("DELETE FROM walkforward_trades WHERE variant = 'har_target'")
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO walkforward_trades "
                            "(strategy,exchange,variant,fold_start,pair,open_date,close_date,profit_ratio) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", out)
            cur.executemany("INSERT INTO trials (strategy, exchange, variant) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                            [(s, e, "har_target:" + variant_of(spec)) for s, e in sorted({(r[0], r[1]) for r in rows})])
    return {"har_target_trades": len(out), "har_target_active": active, "har_target_missing": missing}
