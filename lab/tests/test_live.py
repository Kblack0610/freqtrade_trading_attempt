import json
import re
from pathlib import Path

import pytest
from freqtrade import exchange as venues
from freqtrade.configuration import Configuration
from freqtrade.configuration.config_validation import validate_config_consistency, validate_config_schema

from lab import league, live, suggest

USER_DATA = Path(__file__).resolve().parents[2] / "user_data"
API = {"username": "u", "password": "p", "jwt_secret_key": "j" * 32, "ws_token": "w"}
ENV = {"LIVE_BOT": "live-1", "LIVE_STRATEGY": "Strat", "LIVE_EXCHANGE": "kraken", "FT_API_PASSWORD": "p",
       "FT_API_JWT_SECRET": "j", "FT_API_WS_TOKEN": "w"}


@pytest.fixture
def cfg():
    return suggest.load_config()


@pytest.fixture
def stage(cfg):
    return live.stage_of(cfg["live"], cfg["book"])


def merged(tmp_path, cfg, stage, exchange="kraken", keys=True) -> tuple[dict, list[Path]]:
    """The real base and exchange files under the real overlay, the way main() builds them."""
    over, secret = tmp_path / "live.json", tmp_path / "keys.json"
    over.write_text(json.dumps(live.overlay(cfg["live"], stage, "live-1", API)))
    secret.write_text(json.dumps({"exchange": {"key": "k", "secret": "s"} if keys else {}}))
    files = [USER_DATA / "config.base.json", USER_DATA / "exchanges" / f"{exchange}.json", over, secret]
    return Configuration.from_files([str(p) for p in files]), files


def test_the_guard_is_on():
    assert live.GUARD_LIFTED is False


def test_main_refuses_before_it_reads_anything(monkeypatch, capsys):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(live.suggest, "load_config", lambda: pytest.fail("read config"))
    monkeypatch.setattr(live, "connect", lambda: pytest.fail("opened the database"))
    monkeypatch.setattr(live.os, "execvp", lambda *a: pytest.fail("started freqtrade"))
    with pytest.raises(SystemExit) as e:
        live.main()
    assert e.value.code == 2
    assert "guard is on" in capsys.readouterr().err


def test_the_shipped_config_passes_every_check(tmp_path, cfg, stage):
    for exchange in cfg["live"]["venues"]:
        conf, _ = merged(tmp_path, cfg, stage, exchange)
        assert live.check(conf, cfg["live"], stage, ready=True) == []


def test_freqtrade_accepts_the_shipped_config(tmp_path, cfg, stage):
    conf, _ = merged(tmp_path, cfg, stage)
    conf["strategy"] = "Strat"
    validate_config_schema(conf)
    validate_config_consistency(conf)


def test_league_refuses_the_live_config(tmp_path, cfg, stage):
    _, files = merged(tmp_path, cfg, stage)
    with pytest.raises(SystemExit):
        league.guard(files)


def test_every_live_venue_keeps_its_stop_on_the_exchange(cfg):
    assert venues.Exchange._ft_has_default["stoploss_on_exchange"] is False
    has = {"binanceus": venues.Binanceus, "kraken": venues.Kraken}
    for name in cfg["live"]["venues"]:
        assert has[name]._ft_has["stoploss_on_exchange"] is True
        assert cfg["live"]["order_types"]["stoploss"] in has[name]._ft_has["stoploss_order_types"]


@pytest.mark.parametrize("change, want", [
    ({"dry_run": True}, "dry_run"),
    ({"trading_mode": "futures"}, "not spot"),
    ({"margin_mode": "isolated"}, "not spot"),
    ({"stake_amount": "unlimited"}, "stake_amount"),
    ({"stake_amount": 51}, "stake_amount"),
    ({"max_open_trades": -1}, "max_open_trades"),
    ({"max_open_trades": float("inf")}, "max_open_trades"),
    ({"max_open_trades": 4}, "max_open_trades"),
    ({"position_adjustment_enable": True}, "position_adjustment"),
    ({"force_entry_enable": True}, "force_entry"),
    ({"telegram": {"enabled": True}}, "telegram"),
    ({"initial_state": "running"}, "initial_state"),
    ({"order_types": {"stoploss_on_exchange": False}}, "stop on the exchange"),
    ({"order_types": {}}, "stop on the exchange"),
    ({"api_server": {"enabled": False}}, "api_server"),
    ({"pairlists": [{"method": "VolumePairList"}]}, "pairlists"),
    ({"pairlists": [{"method": "StaticPairList"}, {"method": "VolumePairList"}]}, "pairlists"),
])
def test_refuses_each_field(tmp_path, cfg, stage, change, want):
    conf, _ = merged(tmp_path, cfg, stage)
    problems = live.check({**conf, **change}, cfg["live"], stage, ready=True)
    assert len(problems) == 1 and want in problems[0]


@pytest.mark.parametrize("field", ["position_adjustment_enable", "force_entry_enable", "dry_run", "stake_amount",
                                   "max_open_trades", "order_types"])
def test_a_missing_field_is_refused(tmp_path, cfg, stage, field):
    conf, _ = merged(tmp_path, cfg, stage)
    del conf[field]
    assert live.check(conf, cfg["live"], stage, ready=True)


def test_refuses_a_venue_with_no_exchange_stop(tmp_path, cfg, stage):
    conf, _ = merged(tmp_path, cfg, stage, "coinbase")
    assert any("coinbase" in p for p in live.check(conf, cfg["live"], stage, ready=True))


def test_refuses_a_thin_pair_and_no_pairs(tmp_path, cfg, stage):
    conf, _ = merged(tmp_path, cfg, stage)
    for pairs in (["BTC/USD", "DOT/USD"], []):
        conf["exchange"]["pair_whitelist"] = pairs
        assert any("pair_whitelist" in p for p in live.check(conf, cfg["live"], stage, ready=True))


def test_refuses_without_keys_and_names_no_value(tmp_path, cfg, stage):
    conf, _ = merged(tmp_path, cfg, stage, keys=False)
    assert live.check(conf, cfg["live"], stage, ready=True) == ["exchange.key is not set", "exchange.secret is not set"]


def test_refuses_a_strategy_not_at_ready(tmp_path, cfg, stage):
    conf, _ = merged(tmp_path, cfg, stage)
    assert live.check(conf, cfg["live"], stage, ready=False) == ["the strategy is not at ready on this venue"]


def test_a_keys_file_cannot_raise_the_caps(tmp_path, cfg, stage):
    conf, files = merged(tmp_path, cfg, stage)
    files[3].write_text(json.dumps({"stake_amount": 5000, "exchange": {"key": "k", "secret": "s"}}))
    conf = Configuration.from_files([str(p) for p in files])
    assert any("stake_amount" in p for p in live.check(conf, cfg["live"], stage, ready=True))


class Conn:
    def __init__(self, row):
        self.row = row

    def execute(self, sql, args):
        return self

    def fetchone(self):
        return self.row


@pytest.mark.parametrize("row, want", [(None, False), ((False,), False), ((True,), True)])
def test_at_ready(row, want):
    assert live.at_ready(Conn(row), "Strat", "kraken") is want


def run_lifted(monkeypatch, tmp_path, ready, env=ENV):
    """main() with the guard lifted by hand, so the rest of it is tested while the shipped constant stays off."""
    started = []
    secret = tmp_path / "keys.json"
    secret.write_text(json.dumps({"exchange": {"key": "k-value", "secret": "s-value"}}))
    monkeypatch.setattr(live, "GUARD_LIFTED", True)
    monkeypatch.setattr(live, "USER_DATA", USER_DATA)
    monkeypatch.setattr(live, "STATE", tmp_path / "state")
    monkeypatch.setattr(live, "KEYS", secret)
    monkeypatch.setattr(live, "connect", lambda: _Ctx(ready))
    monkeypatch.setattr(live.os, "execvp", lambda f, a: started.append(a))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    live.main()
    return started


class _Ctx(Conn):
    def __init__(self, ready):
        super().__init__((ready,))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_lifted_and_clean_starts_with_keys_last(monkeypatch, tmp_path, capsys):
    (args,) = run_lifted(monkeypatch, tmp_path, ready=True)
    configs = [args[i + 1] for i, a in enumerate(args) if a == "--config"]
    assert configs[-1].endswith("keys.json") and args[:2] == ["freqtrade", "trade"]
    assert (tmp_path / "state" / "live.json").stat().st_mode & 0o077 == 0
    assert "k-value" not in capsys.readouterr().out


def test_lifted_still_refuses_a_strategy_not_at_ready(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit):
        run_lifted(monkeypatch, tmp_path, ready=False)
    assert "not at ready" in capsys.readouterr().err


def test_lifted_refuses_env_overrides(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("FREQTRADE__DRY_RUN", "false")
    with pytest.raises(SystemExit):
        run_lifted(monkeypatch, tmp_path, ready=True)
    assert "FREQTRADE__" in capsys.readouterr().err


def test_no_deploy_file_in_this_repo_starts_it():
    root = Path(__file__).resolve().parents[2]
    for f in [root / "Dockerfile", *(root / ".github").rglob("*.yml")]:
        assert not re.search(r"live\.py", f.read_text()), f


def test_the_shipped_overlay_and_base_carry_no_manual_entry(tmp_path, cfg, stage):
    conf, _ = merged(tmp_path, cfg, stage)
    assert conf["force_entry_enable"] is False and not conf.get("telegram", {}).get("enabled")


def make_strategy(tmp_path, source="x = 1\n", params="{}"):
    d = tmp_path / "ud" / "strategies"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Strat.py").write_text(source)
    (d / "Strat.json").write_text(params)
    return tmp_path / "ud"


def make_db(path, open_count):
    import sqlite3

    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE trades (id INTEGER, is_open INTEGER)")
        c.executemany("INSERT INTO trades VALUES (?, ?)", [(i, 1) for i in range(open_count)] + [(99, 0)])


def test_open_trades_counts_only_open_and_reads_no_file_as_none(tmp_path):
    assert live.open_trades(tmp_path / "none.sqlite") == 0
    make_db(tmp_path / "t.sqlite", 2)
    assert live.open_trades(tmp_path / "t.sqlite") == 2


def test_the_fingerprint_moves_with_source_and_params_but_not_keys(tmp_path):
    base = {"stake_amount": 50, "exchange": {"name": "kraken", "pair_whitelist": ["BTC/USD"], "key": "a"}}
    ud = make_strategy(tmp_path)
    fp = live.fingerprint(base, "Strat", ud)
    assert live.fingerprint({**base, "exchange": {**base["exchange"], "key": "b"}}, "Strat", ud) == fp
    assert live.fingerprint({**base, "stake_amount": 60}, "Strat", ud) != fp
    make_strategy(tmp_path, params='{"buy": 1}')
    assert live.fingerprint(base, "Strat", ud) != fp
    ud = make_strategy(tmp_path, source="x = 2\n")
    assert live.fingerprint(base, "Strat", ud) != fp


def test_an_edit_over_open_trades_is_refused_and_keeps_the_old_record(tmp_path):
    assert live.edited_while_open(tmp_path, "a", trades=3) is False
    assert live.edited_while_open(tmp_path, "a", trades=3) is False
    assert live.edited_while_open(tmp_path, "b", trades=3) is True
    assert json.loads((tmp_path / "started.json").read_text())["fingerprint"] == "a"


def test_an_edit_with_no_open_trades_is_allowed_and_recorded(tmp_path):
    live.edited_while_open(tmp_path, "a", trades=0)
    assert live.edited_while_open(tmp_path, "b", trades=0) is False
    assert json.loads((tmp_path / "started.json").read_text())["fingerprint"] == "b"


def test_lifted_refuses_an_edited_strategy_over_open_trades(monkeypatch, tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    make_db(state / "tradesv3.sqlite", 1)
    (state / "started.json").write_text(json.dumps({"fingerprint": "from-an-earlier-start"}))
    with pytest.raises(SystemExit):
        run_lifted(monkeypatch, tmp_path, ready=True)
    assert "open trades" in capsys.readouterr().err
    assert (state / "started.json").read_text() == json.dumps({"fingerprint": "from-an-earlier-start"})


def test_lifted_starts_an_unchanged_bot_with_open_trades(monkeypatch, tmp_path):
    run_lifted(monkeypatch, tmp_path, ready=True)
    make_db(tmp_path / "state" / "tradesv3.sqlite", 2)
    assert len(run_lifted(monkeypatch, tmp_path, ready=True)) == 1
