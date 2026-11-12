"""Signal markouts: what each paper entry was worth a fixed time after its fill, against random entries.

For every league bot entry (bot_trades), at each horizon in [markouts] of lab/suggest.toml:
- ret: the move from the fill to the candle open at the horizon, signed by side;
- net: ret minus side x beta x BTC's move over the same candles, minus the venue's round-trip fee (the configured fee,
  which already carries the slippage guess, README "Real fees"); beta is the pair's on BTC over the beta_days before;
- null_net: the mean net of `draws` random entries on the same pair and side, at candle opens within window_hours of
  the entry. A random entry has no skill and pays only the fee, so the null's mean sits near minus the round-trip fee,
  and an entry with no skill sits near the null;
- fill_slip: the fill over the entry candle's open, signed by side, positive when worse: the spread and slippage paid.

This is closing line value from sports betting: a markout separates skill from luck in far fewer trades than profit
does, because one horizon's move is far less noisy than a whole trade's result. It stays a diagnostic, never a gate
(docs/pipeline.md, How the pipeline is tested). An entry is written once, all horizons together, when its venue's
candles cover the entry plus the null window plus the longest horizon; the nightly analysis runs it after drift.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from lab.metrics import seed_of
from lab.util import finite

PENDING = """
SELECT t.bot, b.exchange, t.trade_id, t.open_date, t.pair, t.open_rate, coalesce((t.raw->>'is_short')::boolean, false)
FROM bot_trades t JOIN bots b ON b.name = t.bot
WHERE t.open_rate IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM markouts m WHERE m.bot = t.bot AND m.trade_id = t.trade_id AND m.open_date = t.open_date)
ORDER BY t.bot, t.open_date
"""
COLS = ["bot", "trade_id", "open_date", "horizon_min", "pair", "exchange", "side", "fill_slip", "ret", "btc_ret", "beta",
        "net", "null_net", "null_draws"]
MIN_BETA_POINTS = 100  # fewer candle returns than this before an entry give no beta, so no net


def opens(path: Path) -> pd.Series:
    """A feather candle file's open prices by candle start, UTC."""
    df = pd.read_feather(path, columns=["date", "open"])
    return pd.Series(df["open"].astype(float).to_numpy(), index=pd.DatetimeIndex(pd.to_datetime(df["date"], utc=True)))


def markouts(entries: pd.DataFrame, price: pd.Series, btc: pd.Series, fee: float, cfg: dict) -> pd.DataFrame:
    """One pair's entries (bot, trade_id, open_date, open_rate, is_short) against its candle opens and BTC's (both
    indexed by candle start on cfg["timeframe"]). Entries whose window the candles do not cover yet are left out."""
    step = pd.Timedelta(cfg["timeframe"])
    horizons = [int(h) for h in cfg["horizons_minutes"]]
    window, beta_span = pd.Timedelta(hours=cfg["window_hours"]), pd.Timedelta(days=cfg["beta_days"])
    longest = pd.Timedelta(minutes=max(horizons))
    grid = pd.date_range(min(price.index.min(), btc.index.min()), max(price.index.max(), btc.index.max()), freq=step)
    p, b = price.reindex(grid).to_numpy(), btc.reindex(grid).to_numpy()
    lp, lb = np.diff(np.log(p)), np.diff(np.log(b))
    shift = {h: int(pd.Timedelta(minutes=h) / step) for h in horizons}
    last = min(price.index.max(), btc.index.max())
    rows = []
    for e in entries.itertuples(index=False):
        bar = pd.Timestamp(e.open_date).tz_convert("UTC").floor(step)
        if bar + window + longest > last or bar < grid[0]:
            continue
        i, side = grid.get_loc(bar), -1.0 if e.is_short else 1.0
        lo = max(grid.searchsorted(bar - beta_span), 0)
        ok = np.isfinite(lp[lo:i]) & np.isfinite(lb[lo:i])
        x, y = lb[lo:i][ok], lp[lo:i][ok]
        beta = float(np.cov(y, x)[0, 1] / np.var(x, ddof=1)) if len(x) >= MIN_BETA_POINTS and np.var(x) > 0 else np.nan
        span = np.arange(max(i - int(window / step), 0), i + int(window / step) + 1)
        span = span[np.isfinite(p[span]) & np.isfinite(b[span])]
        rng = np.random.default_rng(seed_of(e.bot, e.trade_id, bar))
        draws = rng.choice(span, size=int(cfg["draws"])) if len(span) else np.array([], dtype=int)
        for h in horizons:
            k = shift[h]
            btc_ret = b[i + k] / b[i] - 1
            ret = side * (p[i + k] / e.open_rate - 1)
            null = side * (p[draws + k] / p[draws] - 1) - side * beta * (b[draws + k] / b[draws] - 1) - 2 * fee
            null = null[np.isfinite(null)]
            rows.append({"bot": e.bot, "trade_id": e.trade_id, "open_date": e.open_date, "horizon_min": h,
                         "side": int(side), "fill_slip": side * (e.open_rate / p[i] - 1), "ret": ret, "btc_ret": btc_ret,
                         "beta": beta, "net": ret - side * beta * btc_ret - 2 * fee,
                         "null_net": float(null.mean()) if len(null) else np.nan, "null_draws": len(null)})
    return pd.DataFrame(rows, columns=[c for c in COLS if c not in ("pair", "exchange")])


def run(conn, data_dir: Path, exchanges_dir: Path, cfg: dict) -> dict:
    """Markouts for every league entry not yet marked whose candles now cover it."""
    from freqtrade.misc import pair_to_filename

    pending = pd.DataFrame(conn.execute(PENDING).fetchall(),
                           columns=["bot", "exchange", "trade_id", "open_date", "pair", "open_rate", "is_short"])
    written, rows = 0, []
    for (exchange, pair), entries in pending.groupby(["exchange", "pair"]):
        conf = exchanges_dir / f"{exchange}.json"
        files = [data_dir / exchange / f"{pair_to_filename(q)}-{cfg['timeframe']}.feather"
                 for q in (pair, "BTC/" + pair.split("/")[1].split(":")[0])]
        if not conf.exists() or not all(f.exists() for f in files):
            continue
        m = markouts(entries, opens(files[0]), opens(files[1]), float(json.loads(conf.read_text())["fee"]), cfg)
        if len(m):
            rows += m.assign(pair=pair, exchange=exchange)[COLS].to_dict("records")
            written += len(m) // len(cfg["horizons_minutes"])
    with conn.transaction():
        conn.cursor().executemany(
            f"INSERT INTO markouts ({', '.join(COLS)}) VALUES ({', '.join(['%s'] * len(COLS))}) ON CONFLICT DO NOTHING",
            [[finite(r[c]) for c in COLS] for r in rows])
    return {"markouts_written": written, "markouts_pending": len(pending) - written}
