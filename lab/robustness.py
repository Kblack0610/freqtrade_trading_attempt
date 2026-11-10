"""Fixed, untuned perturbations of a walk-forward test month, in its temporary strategy directory."""

import ast
import copy
import hashlib
import json
import math
from pathlib import Path


def settings(cfg: dict) -> dict:
    """The perturbation settings and implementation digest that invalidate stored fold caches."""
    return {**{key: cfg[key] for key in ("entry_delay_candles", "parameter_nudge_fraction", "detail_timeframe")},
            "implementation": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}


def plan(cfg: dict, params: dict | None) -> dict[str, dict]:
    """Scenario ids and instructions; their order is stable and does not depend on results."""
    delay = cfg["entry_delay_candles"]
    fraction = cfg["parameter_nudge_fraction"]
    detail = cfg["detail_timeframe"]
    if isinstance(delay, bool) or not isinstance(delay, int) or delay < 1:
        raise ValueError("entry_delay_candles must be a positive integer")
    if isinstance(fraction, bool) or not isinstance(fraction, (float, int)) or not 0 < fraction < 1:
        raise ValueError("parameter_nudge_fraction must be between zero and one")
    if not isinstance(detail, str) or not detail:
        raise ValueError("detail_timeframe must name a smaller candle timeframe")
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
    out[f"detail:{detail}"] = {"kind": "detail", "timeframe": detail}
    return out


def expected(scenarios: dict) -> list[str]:
    """Names required by this fixed experiment plan."""
    return list(scenarios)


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


def run(bt, strategy: str, strat: Path, params_file: Path, exchange: str, start, end,
        cfg: dict, summarize) -> dict[str, dict]:
    """Replay each scenario independently, restore the tuned file even after a runner exception.

    Errors are evidence, so one failed replay does not discard the original tuned fold or stop the other scenarios.
    Temporary subclasses retain the tuned parameter filename under their own class name, as Freqtrade requires.
    """
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
                name = strategy
                kwargs = {"strategy_path": strat}
                if spec["kind"] == "detail":
                    kwargs["detail"] = spec["timeframe"]
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
                result = bt.backtest(name, exchange, start, end, **kwargs)
                if not result["ok"]:
                    out[scenario] = {"ok": False, "error": result.get("error", "backtest failed"), "returns": []}
                    continue
                summary = {key: value for key, value in summarize(result).items() if key != "dated"}
                if not all(math.isfinite(value) for value in summary.get("returns", [])):
                    raise ValueError("nonfinite scenario returns")
                out[scenario] = {"ok": True, "error": None, **summary}
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
