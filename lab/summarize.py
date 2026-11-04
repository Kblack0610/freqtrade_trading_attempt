#!/usr/bin/env python3
"""Summarize one experiments run directory into summary.json and summary.md.

Layout read (written by experiments.sh):
  backtest/<exchange>/<strategy>/*.zip | error.txt
  lookahead/<strategy>.csv
  hyperopt/<strategy>.json
"""

import csv
import json
import sys
from pathlib import Path

MIN_TRADES = 20  # fewer closed trades than this is noise, not evidence


def backtest_rows(run_dir: Path) -> list[dict]:
    from freqtrade.data.btanalysis import load_backtest_stats

    rows = []
    for strat_dir in sorted(run_dir.glob("backtest/*/*")):
        exchange, strategy = strat_dir.parent.name, strat_dir.name
        row = {"exchange": exchange, "strategy": strategy}
        zips = sorted(strat_dir.glob("backtest-result-*.zip"))
        if not zips:
            err = strat_dir / "error.txt"
            row["error"] = err.read_text().strip().splitlines()[-1] if err.is_file() else "no result"
            rows.append(row)
            continue
        st = load_backtest_stats(zips[-1])["strategy"].get(strategy, {})
        row.update({
            "timeframe": st.get("timeframe"),
            "trades": st.get("total_trades", 0),
            "profit_pct": round(100 * (st.get("profit_total") or 0), 2),
            "winrate_pct": round(100 * (st.get("winrate") or 0), 1),
            "max_drawdown_pct": round(100 * (st.get("max_drawdown_account") or 0), 2),
            "profit_factor": round(st.get("profit_factor") or 0, 2),
            "sharpe": round(st.get("sharpe") or 0, 2),
            "start": st.get("backtest_start"),
            "end": st.get("backtest_end"),
        })
        rows.append(row)
    return rows


def lookahead(run_dir: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(run_dir.glob("lookahead/*.csv")):
        with f.open() as fh:
            for rec in csv.DictReader(fh):
                name = rec.get("strategy") or f.stem
                out[name] = {
                    "has_bias": str(rec.get("has_bias", "")).strip().lower() == "true",
                    "biased_indicators": rec.get("biased_indicators", ""),
                }
    return out


def hyperopt(run_dir: Path) -> dict[str, dict]:
    out = {}
    for f in sorted(run_dir.glob("hyperopt/*.json")):
        text = f.read_text().strip()
        if not text:
            continue
        try:
            out[f.stem] = json.loads(text.splitlines()[-1])
        except json.JSONDecodeError:
            out[f.stem] = {"raw": text[-500:]}
    return out


def main(run_dir: Path) -> None:
    rows = backtest_rows(run_dir)
    bias = lookahead(run_dir)
    for r in rows:
        b = bias.get(r["strategy"])
        r["lookahead_bias"] = None if b is None else b["has_bias"]

    ok = [r for r in rows if "error" not in r]
    ranked = sorted(ok, key=lambda r: (r["trades"] >= MIN_TRADES, r["profit_pct"]), reverse=True)
    # Hyperopt only what has evidence: enough trades, profitable, and checked clean of lookahead bias.
    seen, candidates = set(), []
    for r in ranked:
        if r["trades"] >= MIN_TRADES and r["profit_pct"] > 0 and r["lookahead_bias"] is False and r["strategy"] not in seen:
            seen.add(r["strategy"])
            candidates.append(r["strategy"])

    summary = {
        "run": run_dir.name,
        "min_trades": MIN_TRADES,
        "backtests": ranked + [r for r in rows if "error" in r],
        "lookahead": bias,
        "hyperopt_candidates": candidates,
        "hyperopt": hyperopt(run_dir),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (run_dir / "summary.md").write_text(render(summary))
    print(render(summary))


def render(s: dict) -> str:
    lines = [f"# Experiments {s['run']}", "",
             f"Ranked by backtest profit; rows under {s['min_trades']} trades sink to the bottom.", "",
             "| exchange | strategy | tf | trades | profit % | win % | max dd % | PF | sharpe | lookahead bias |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in s["backtests"]:
        if "error" in r:
            continue
        bias = {True: "BIASED", False: "clean", None: "unchecked"}[r["lookahead_bias"]]
        lines.append(f"| {r['exchange']} | {r['strategy']} | {r['timeframe']} | {r['trades']} | {r['profit_pct']} | "
                     f"{r['winrate_pct']} | {r['max_drawdown_pct']} | {r['profit_factor']} | {r['sharpe']} | {bias} |")
    errors = [r for r in s["backtests"] if "error" in r]
    if errors:
        lines += ["", "## Failed backtests", ""]
        lines += [f"- {r['exchange']}/{r['strategy']}: {r['error']}" for r in errors]
    lines += ["", "## Hyperopt candidates", "", ", ".join(s["hyperopt_candidates"]) or "none"]
    for name, res in s["hyperopt"].items():
        lines += ["", f"### {name}", "", "```json", json.dumps(res, indent=2)[:2000], "```"]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main(Path(sys.argv[1]))
