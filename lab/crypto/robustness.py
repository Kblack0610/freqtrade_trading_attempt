"""Fixed, untuned perturbations of a walk-forward test month, in its temporary strategy directory."""

import ast
import copy
import datetime as dt
import hashlib
import json
import math
from contextlib import nullcontext
from pathlib import Path

import pandas as pd


def settings(cfg: dict) -> dict:
    """The perturbation settings and implementation digest that invalidate stored fold caches."""
    return {**{key: cfg[key] for key in ("entry_delay_candles", "parameter_nudge_fraction", "detail_timeframe",
                                     "detail_chunk_days")},
            "implementation": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def plan(cfg: dict, params: dict | None) -> dict[str, dict]:
    """Scenario ids and instructions; their order is stable and does not depend on results."""
    delay = cfg["entry_delay_candles"]
    fraction = cfg["parameter_nudge_fraction"]
    detail = cfg["detail_timeframe"]
    chunk = cfg["detail_chunk_days"]
    if isinstance(delay, bool) or not isinstance(delay, int) or delay < 1:
        raise ValueError("entry_delay_candles must be a positive integer")
    if isinstance(fraction, bool) or not isinstance(fraction, (float, int)) or not 0 < fraction < 1:
        raise ValueError("parameter_nudge_fraction must be between zero and one")
    if not isinstance(detail, str) or not detail:
        raise ValueError("detail_timeframe must name a smaller candle timeframe")
    if isinstance(chunk, bool) or not isinstance(chunk, int) or chunk < 1:
        raise ValueError("detail_chunk_days must be a positive integer")
    out = {f"delay:{delay}": {"kind": "delay", "candles": delay}}
    for space in ("buy", "sell"):
        for key, value in sorted((params or {}).get(space, {}).items()):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            step = max(1, round(abs(value) * fraction)) if isinstance(value, int) else (abs(value) * fraction if value else fraction)
            for direction, sign in (("down", -1), ("up", 1)):
                candidate = value + sign * step
                if isinstance(value, float):
                    # Keep JSON decimal values stable rather than expose binary rounding noise.
                    candidate = float(f"{candidate:.12g}")
                out[f"nudge:{space}.{key}:{direction}"] = {
                    "kind": "nudge", "space": space, "key": key, "value": candidate}
    if not any(s["kind"] == "nudge" for s in out.values()):
        out["nudge:unsupported"] = {"kind": "unsupported", "error": "no numeric buy or sell parameter to nudge"}
    out[f"detail:{detail}"] = {"kind": "detail", "timeframe": detail, "chunk_days": chunk}
    return out


def expected(scenarios: dict) -> list[str]:
    """Names required by this fixed experiment plan."""
    return list(scenarios)


def pieces(start: dt.date, end: dt.date, days: int) -> list[tuple[dt.date, dt.date]]:
    """[start, end) as consecutive spans of at most `days` days, the last one shorter."""
    out = []
    while start < end:
        out.append((start, min(start + dt.timedelta(days=days), end)))
        start = out[-1][1]
    return out


def join(results: list[dict]) -> dict:
    """Pieces run back to back as one result shaped like analyze.Cli.backtest's. Each piece starts from the configured
    balance, so its return compounds on the balance the pieces before it left, and its trades' profit_abs scale by the
    same factor. max_drawdown is the worst piece's, an approximation: a drawdown that spans a boundary is not seen.

    A trade still open at an inner boundary is force-exited there by freqtrade. It is kept as a boundary_exit at the
    boundary's price, since stats() drops force exits and dropping these would remove the longest trades from the sample
    most often; boundary_exits counts them. Only the last piece's force exits are the span's own end."""
    start = balance = results[0]["start_balance"]
    frames, boundary = [], 0
    for i, r in enumerate(results):
        trades = r["trades"].copy()
        if i < len(results) - 1 and "exit_reason" in trades:
            cut = trades["exit_reason"] == "force_exit"
            boundary += int(cut.sum())
            trades.loc[cut, "exit_reason"] = "boundary_exit"
        if "profit_abs" in trades:
            trades["profit_abs"] = trades["profit_abs"] * (balance / r["start_balance"])
        frames.append(trades)
        balance *= r["final_balance"] / r["start_balance"]
    return {"ok": True, "error": None, "duration_s": sum(r.get("duration_s") or 0.0 for r in results),
            "trades": pd.concat(frames, ignore_index=True), "bt_start": results[0]["bt_start"],
            "bt_end": results[-1]["bt_end"], "start_balance": start, "final_balance": balance,
            "max_drawdown": max(r["max_drawdown"] for r in results),
            "boundary_exits": boundary}


def detail_backtest(bt, strategy: str, exchange: str, start: dt.date, end: dt.date, timeframe: str, days: int,
                    hold=None, **kwargs) -> dict:
    """The --timeframe-detail replay of [start, end) in `days`-day pieces, each run under hold("detail") so a piece's
    intrabar candles, not a whole month's, take the memory budget. The first failed piece is the result."""
    hold = hold or (lambda kind: nullcontext())
    results = []
    for a, b in pieces(start, end, days):
        with hold("detail"):
            r = bt.backtest(strategy, exchange, a, b, detail=timeframe, **kwargs)
        if not r["ok"]:
            return {**r, "error": f"detail piece {a} to {b}: {r.get('error')}"}
        results.append(r)
    return join(results)


def _source(strat: Path, strategy: str) -> Path:
    for path in sorted(strat.glob("*.py")):
        tree = ast.parse(path.read_text())
        if any(isinstance(node, ast.ClassDef) and node.name == strategy for node in tree.body):
            return path
    raise ValueError(f"strategy class {strategy} not found in isolated directory")


def _wrapper(strategy: str, module: str, spec: dict) -> tuple[str, str]:
    suffix = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    name = f"{strategy}{suffix}Robustness"
    source = f"import {module} as _original\n\nclass {name}(_original.{strategy}):\n"
    if spec["kind"] == "delay":
        source += (
            "    def populate_entry_trend(self, dataframe, metadata):\n"
            "        dataframe = super().populate_entry_trend(dataframe, metadata)\n"
            "        for column in ('enter_long', 'enter_short', 'buy'):\n"
            "            if column in dataframe:\n"
            f"                dataframe[column] = dataframe[column].shift({spec['candles']}, fill_value=0)\n"
            "        if 'enter_tag' in dataframe:\n"
            f"            dataframe['enter_tag'] = dataframe['enter_tag'].shift({spec['candles']})\n"
            "        return dataframe\n")
    elif spec["kind"] == "detail":
        source += (
            "    def populate_indicators(self, dataframe, metadata):\n"
            "        from freqtrade.exchange import timeframe_to_seconds\n"
            f"        if timeframe_to_seconds(self.timeframe) <= timeframe_to_seconds({spec['timeframe']!r}):\n"
            "            raise ValueError('detail timeframe must be smaller than strategy timeframe')\n"
            "        return super().populate_indicators(dataframe, metadata)\n")
    else:
        source += (
            "    def populate_indicators(self, dataframe, metadata):\n"
            f"        parameter = getattr(self, {spec['key']!r}, None)\n"
            "        if parameter is None or not hasattr(parameter, 'low') or not hasattr(parameter, 'high'):\n"
            "            raise ValueError('nudge requires a bounded numeric parameter')\n"
            f"        if parameter.value != {spec['value']!r}:\n"
            "            raise ValueError('strategy did not apply the nudged parameter')\n"
            "        if not parameter.low <= parameter.value <= parameter.high:\n"
            "            raise ValueError('nudged value is outside the declared bounds')\n"
            "        if hasattr(parameter, 'decimals') and round(parameter.value, parameter.decimals) != parameter.value:\n"
            "            raise ValueError('nudged value exceeds the declared precision')\n"
            "        return super().populate_indicators(dataframe, metadata)\n")
    return name, source


def run(bt, strategy: str, strat: Path, params_file: Path, exchange: str, start: dt.date, end: dt.date,
        cfg: dict, summarize, hold=None) -> dict[str, dict]:
    """Replay each scenario independently, restore the tuned file even after a runner exception.

    hold(kind) is entered around each replay: "native" for a scenario on the strategy's own candles, "detail" for each
    piece of the detail scenario (detail_backtest). Neither is held while the other runs.

    Errors are evidence, so one failed replay does not discard the original tuned fold or stop the other scenarios.
    Temporary subclasses retain the tuned parameter filename under their own class name, as Freqtrade requires.
    """
    hold = hold or (lambda kind: nullcontext())
    original = params_file.read_bytes()
    document = json.loads(original)
    scenarios = plan(cfg, document.get("params"))
    out = {}
    try:
        for scenario, spec in scenarios.items():
            wrapper = wrapper_params = None
            try:
                params_file.write_bytes(original)
                if spec["kind"] == "unsupported":
                    out[scenario] = {"ok": False, "error": spec["error"], "returns": []}
                    continue
                name, source = _wrapper(strategy, _source(strat, strategy).stem, spec)
                source_path = strat / f"{name}.py"
                params_path = strat / f"{name}.json"
                if source_path.exists() or params_path.exists():
                    raise ValueError("isolated robustness filenames already exist")
                wrapper, wrapper_params = source_path, params_path
                wrapper.write_text(source)
                changed = copy.deepcopy(document)
                changed["strategy_name"] = name
                if spec["kind"] == "nudge":
                    changed["params"][spec["space"]][spec["key"]] = spec["value"]
                wrapper_params.write_text(json.dumps(changed))
                if spec["kind"] == "detail":
                    result = detail_backtest(bt, name, exchange, start, end, spec["timeframe"], spec["chunk_days"],
                                             hold, strategy_path=strat)
                else:
                    with hold("native"):
                        result = bt.backtest(name, exchange, start, end, strategy_path=strat)
                if not result["ok"]:
                    out[scenario] = {"ok": False, "error": result.get("error", "backtest failed"), "returns": []}
                    continue
                summary = {key: value for key, value in summarize(result).items() if key != "dated"}
                if not all(math.isfinite(value) for value in summary.get("returns", [])):
                    raise ValueError("nonfinite scenario returns")
                out[scenario] = {"ok": True, "error": None, **summary}
                if "boundary_exits" in result:
                    out[scenario]["boundary_exits"] = result["boundary_exits"]
            except Exception as exc:
                out[scenario] = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "returns": []}
            finally:
                if wrapper is not None:
                    wrapper.unlink(missing_ok=True)
                if wrapper_params is not None:
                    wrapper_params.unlink(missing_ok=True)
    finally:
        params_file.write_bytes(original)
    return out
