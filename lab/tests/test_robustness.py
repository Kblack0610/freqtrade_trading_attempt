import json
from pathlib import Path

import pandas as pd
import pytest

from lab import robustness

CFG = {"entry_delay_candles": 1, "parameter_nudge_fraction": 0.1, "detail_timeframe": "1m"}


def isolated(tmp_path):
    (tmp_path / "Example.py").write_text("class Example:\n    pass\n")
    path = tmp_path / "Example.json"
    path.write_text(json.dumps({"strategy_name": "Example", "params": {
        "buy": {"period": 20, "switch": True, "choice": "x", "ratio": 0.02}, "roi": {"0": 0.1}}}))
    return path


def test_fixed_plan_changes_one_numeric_buy_sell_leaf_at_a_time():
    params = {"buy": {"period": 20, "switch": False, "choice": "slow", "ratio": 0.02},
              "sell": {"period": 2}, "roi": {"0": 0.1}, "stoploss": {"stoploss": -0.1}}
    planned = robustness.plan(CFG, params)
    assert list(planned) == ["delay:1", "nudge:buy.period:down", "nudge:buy.period:up",
                             "nudge:buy.ratio:down", "nudge:buy.ratio:up",
                             "nudge:sell.period:down", "nudge:sell.period:up", "detail:1m"]
    assert planned["nudge:buy.period:down"]["value"] == 18
    assert planned["nudge:buy.ratio:up"]["value"] == pytest.approx(0.022)
    assert isinstance(planned["nudge:sell.period:down"]["value"], int)
    assert robustness.expected(planned) == list(planned)
    assert "nudge:unsupported" in robustness.plan(CFG, {})


@pytest.mark.parametrize("key,value", [("entry_delay_candles", 0), ("entry_delay_candles", True),
                                      ("parameter_nudge_fraction", 0), ("parameter_nudge_fraction", 1),
                                      ("detail_timeframe", "")])
def test_invalid_experiment_settings_are_rejected(key, value):
    with pytest.raises(ValueError):
        robustness.plan({**CFG, key: value}, {})


def test_every_replay_uses_original_parameters_and_restores_files_after_errors(tmp_path):
    path = isolated(tmp_path)
    original = path.read_bytes()
    seen = []

    class Runner:
        def backtest(self, strategy, exchange, start, end, **kwargs):
            assert path.read_bytes() == original
            if strategy.endswith("Robustness"):
                seen.append(json.loads((tmp_path / f"{strategy}.json").read_text())["params"])
            else:
                assert kwargs["detail"] == "1m"
            if len(seen) == 2:
                raise RuntimeError("scenario error")
            return {"ok": True}

    results = robustness.run(Runner(), "Example", tmp_path, path, "venue", "start", "end", CFG,
                             lambda r: {"returns": [0.01], "trades": 1, "dated": [("non-json", object())]})
    assert list(results) == robustness.expected(robustness.plan(CFG, json.loads(original)["params"]))
    assert any(not r["ok"] and "scenario error" in r["error"] for r in results.values())
    assert all("dated" not in r for r in results.values())
    json.dumps(results)
    assert seen[1]["buy"]["period"] == 18 and seen[1]["buy"]["ratio"] == 0.02
    assert seen[2]["buy"]["period"] == 22 and seen[2]["buy"]["ratio"] == 0.02
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*Robustness.py"))
    assert not list(tmp_path.glob("*Robustness.json"))


def test_delay_shifts_entries_and_tags_preserving_exit_signals():
    class Base:
        def populate_entry_trend(self, dataframe, metadata):
            return dataframe

    name, source = robustness._wrapper("Example", "example_module", {"kind": "delay", "candles": 1})
    import sys
    import types
    module = types.ModuleType("example_module")
    module.Example = Base
    sys.modules["example_module"] = module
    try:
        namespace = {}
        exec(source, namespace)
        df = pd.DataFrame({"enter_long": [1, 0, 0], "enter_short": [0, 1, 0], "enter_tag": ["a", "b", "c"],
                           "exit_long": [0, 1, 0]})
        out = namespace[name]().populate_entry_trend(df, {})
        assert list(out.enter_long) == [0, 1, 0]
        assert list(out.enter_short) == [0, 0, 1]
        assert list(out.enter_tag)[1:] == ["a", "b"]
        assert list(out.exit_long) == [0, 1, 0]
    finally:
        del sys.modules["example_module"]


def test_a_strategy_without_numeric_parameters_reports_unsupported(tmp_path):
    path = isolated(tmp_path)
    path.write_text(json.dumps({"params": {}}))

    class Runner:
        def backtest(self, *args, **kwargs):
            return {"ok": False, "error": "no data"}

    results = robustness.run(Runner(), "Example", tmp_path, path, "venue", 1, 2, CFG, lambda r: {})
    assert not results["nudge:unsupported"]["ok"]
    assert not any(r["ok"] for r in results.values())


def test_settings_track_algorithm_and_configuration():
    original = robustness.settings(CFG)
    assert len(original["implementation"]) == 64
    assert original != robustness.settings({**CFG, "entry_delay_candles": 2})


def test_freqtrade_resolver_loads_wrapper_parameters_and_rejects_ignored_nudges(tmp_path):
    pytest.importorskip("freqtrade")
    from freqtrade.configuration import Configuration
    from freqtrade.resolvers import StrategyResolver

    (tmp_path / "NudgeExample.py").write_text('''from freqtrade.strategy import IStrategy, IntParameter
class NudgeExample(IStrategy):
    timeframe = '5m'
    stoploss = -0.1
    period = IntParameter(1, 30, default=20, space='buy')
    def populate_indicators(self, dataframe, metadata):
        dataframe['period'] = self.period.value
        return dataframe
    def populate_entry_trend(self, dataframe, metadata):
        dataframe['enter_long'] = 1
        return dataframe
    def populate_exit_trend(self, dataframe, metadata):
        dataframe['exit_long'] = 0
        return dataframe
''')
    spec = {"kind": "nudge", "key": "period", "value": 22}
    name, source = robustness._wrapper("NudgeExample", "NudgeExample", spec)
    (tmp_path / f"{name}.py").write_text(source)
    (tmp_path / f"{name}.json").write_text(json.dumps({"strategy_name": name, "params": {"buy": {"period": 22}}}))
    repo = Path(__file__).resolve().parents[2]
    base = Configuration.from_files([str(repo / "user_data/config.base.json"),
                                     str(repo / "user_data/exchanges/binanceus.json")])
    strategy = StrategyResolver.load_strategy({**base, "strategy_path": str(tmp_path), "strategy": name})
    strategy.ft_load_hyper_params()
    assert list(strategy.populate_indicators(pd.DataFrame({"close": [1.0]}), {}).period) == [22]
    strategy.period.value = 20
    with pytest.raises(ValueError, match="did not apply"):
        strategy.populate_indicators(pd.DataFrame({"close": [1.0]}), {})
    strategy.period.value = 31
    other_name, out_of_bounds = robustness._wrapper("NudgeExample", "NudgeExample", {**spec, "value": 31})
    namespace = {}
    exec(out_of_bounds, namespace)
    instance = namespace[other_name]({})
    instance.period.value = 31
    with pytest.raises(ValueError, match="outside the declared bounds"):
        instance.populate_indicators(pd.DataFrame({"close": [1.0]}), {})


def test_nonfinite_replays_remain_serializable_failed_evidence(tmp_path):
    path = isolated(tmp_path)
    class Runner:
        def backtest(self, *args, **kwargs):
            return {'ok': True}
    results = robustness.run(Runner(), 'Example', tmp_path, path, 'venue', 1, 2, CFG,
                             lambda r: {'returns': [float('nan')]})
    assert all(not r['ok'] and 'nonfinite' in r['error'] for r in results.values())
    json.dumps(results, allow_nan=False)
