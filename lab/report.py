#!/usr/bin/env python3
"""Daily league report: live dry-run stats from every bot plus the latest experiments ranking.

Reads each roster bot's REST API, appends one line per bot to <results>/league-history.jsonl
(the time series that tells a lucky week from a real edge), and writes <results>/reports/<date>.md
with the live leaderboard and the strategies worth promoting into the roster.
Stdlib only, so it runs in any python image.
"""

import base64
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path

ROSTER = Path(os.environ.get("LEAGUE_ROSTER", "/etc/freqtrade-league/roster.tsv"))
RESULTS = Path(os.environ.get("LAB_RESULTS", "/lab"))
# Pod N of the StatefulSet answers at <set>-N.<service>; both names default to freqtrade-league.
HOST_FMT = os.environ.get("LEAGUE_HOST_FMT", "freqtrade-league-{i}.freqtrade-league:8080")
AUTH = base64.b64encode(f"{os.environ.get('FT_API_USERNAME', 'freqtrader')}:{os.environ.get('FT_API_PASSWORD', '')}".encode()).decode()
PROMOTE_MIN_TRADES = 20


def roster() -> list[tuple[str, str, str]]:
    rows = []
    for raw in ROSTER.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            name, exchange, strategy = line.split()
            rows.append((name, exchange, strategy))
    return rows


def api(i: int, path: str) -> dict:
    req = urllib.request.Request(f"http://{HOST_FMT.format(i=i)}/api/v1/{path}", headers={"Authorization": f"Basic {AUTH}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def live(now: str) -> list[dict]:
    out = []
    for i, (name, exchange, strategy) in enumerate(roster()):
        row = {"ts": now, "bot": name, "exchange": exchange, "strategy": strategy}
        try:
            cfg = api(i, "show_config")
            prof = api(i, "profit")
            if cfg.get("dry_run") is not True:
                row["error"] = "NOT DRY RUN"
            row.update({
                "closed_trades": prof.get("closed_trade_count", 0),
                "open_trades": prof.get("trade_count", 0) - prof.get("closed_trade_count", 0),
                "profit_closed_pct": round(prof.get("profit_closed_percent", 0) or 0, 2),
                "profit_all_pct": round(prof.get("profit_all_percent", 0) or 0, 2),
                "winrate_pct": round(100 * (prof.get("winrate") or 0), 1),
                "max_drawdown_pct": round(100 * (prof.get("max_drawdown") or 0), 2),
                "profit_factor": round(prof.get("profit_factor") or 0, 2),
                "bot_start": prof.get("bot_start_date"),
            })
        except Exception as e:  # one dead bot must not hide the others
            row["error"] = f"{type(e).__name__}: {e}"
        out.append(row)
    return out


def latest_summary() -> dict | None:
    f = RESULTS / "runs" / "latest" / "summary.json"
    return json.loads(f.read_text()) if f.is_file() else None


def render(rows: list[dict], summary: dict | None, today: str) -> str:
    lines = [f"# Freqtrade league {today}", "", "All bots are dry run. Ranked by closed-trade profit.", "",
             "| bot | exchange | strategy | closed | open | closed profit % | all profit % | win % | max dd % | PF |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    good = sorted((r for r in rows if "error" not in r), key=lambda r: r["profit_closed_pct"], reverse=True)
    for r in good:
        lines.append(f"| {r['bot']} | {r['exchange']} | {r['strategy']} | {r['closed_trades']} | {r['open_trades']} | "
                     f"{r['profit_closed_pct']} | {r['profit_all_pct']} | {r['winrate_pct']} | {r['max_drawdown_pct']} | {r['profit_factor']} |")
    bad = [r for r in rows if "error" in r]
    if bad:
        lines += ["", "## Unreachable or unsafe bots", ""] + [f"- {r['bot']}: {r['error']}" for r in bad]

    lines += ["", "## Promote candidates", ""]
    if summary is None:
        lines.append("No experiments run yet.")
    else:
        in_league = {r["strategy"] for r in rows}
        picks = [b for b in summary["backtests"]
                 if "error" not in b and b["lookahead_bias"] is False and b["trades"] >= PROMOTE_MIN_TRADES
                 and b["profit_pct"] > 0 and b["strategy"] not in in_league][:5]
        lines.append(f"From experiments run {summary['run']}: profitable, at least {PROMOTE_MIN_TRADES} trades, "
                     "no lookahead bias, not already in the league. Add a roster row to try one live.")
        lines.append("")
        lines += [f"- {b['strategy']} on {b['exchange']}: {b['profit_pct']}% over {b['trades']} trades, "
                  f"max dd {b['max_drawdown_pct']}%" for b in picks] or ["- none this run"]
    return "\n".join(lines) + "\n"


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    rows = live(now.isoformat(timespec="seconds"))
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / "league-history.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    report = render(rows, latest_summary(), now.date().isoformat())
    (RESULTS / "reports").mkdir(exist_ok=True)
    (RESULTS / "reports" / f"{now.date().isoformat()}.md").write_text(report)
    print(report)
    return 1 if any(r.get("error") == "NOT DRY RUN" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
