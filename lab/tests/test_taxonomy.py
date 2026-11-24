import copy
import tomllib

import psycopg
import pytest

from lab import events
from lab.inputs import calls
from lab.crypto import taxonomy
from lab.events import families


def test_every_family_has_a_category_separate_from_its_inputs():
    cfg = taxonomy.load()
    market_cfg = tomllib.loads(events.CONFIG.read_text())
    assert set(families.categories()) == {k for k, v in market_cfg["families"].items() if isinstance(v, dict)}
    for tag in cfg["strategies"].values():
        taxonomy.validate(cfg["family_categories"][tag["family"]], cfg)
        assert tag["inputs"]


@pytest.mark.parametrize("field", ["signal_source", "instrument", "horizon"])
def test_untagged_family_is_rejected(tmp_path, field):
    text = taxonomy.CONFIG.read_text()
    text = text.replace(f'{field} = "price", ', '', 1) if field == "signal_source" else text.replace(
        f'{field} = "crypto", ' if field == "instrument" else ', horizon = "strategy-exit"', '', 1)
    path = tmp_path / "families.toml"
    path.write_text(text)
    with pytest.raises(ValueError):
        taxonomy.load(path)


def test_call_category_is_required_and_must_be_known(conn):
    from lab.tests.test_calls import call

    for source in (None, ""):
        with pytest.raises(psycopg.IntegrityError):
            call(conn, signal_source=source)
    taxonomy.validate({"signal_source": "event", "instrument": "equity", "horizon": [1]})
    with pytest.raises(ValueError, match="signal_source"):
        taxonomy.validate({"signal_source": "unknown", "instrument": "equity", "horizon": [1]})


def test_call_groups_never_pool_different_signal_sources():
    row = {"who": "human", "signal_source": "event", "instrument": "equity", "horizon": 1, "pnl": 0.1}
    rows = [row, {**row, "signal_source": "sentiment", "pnl": -0.1}]
    groups = calls.groups(rows)
    assert [(r["signal_source"], r["n"], r["mean"]) for r in groups] == [("event", 1, 0.1), ("sentiment", 1, -0.1)]
    untagged = copy.copy(row)
    del untagged["signal_source"]
    with pytest.raises(KeyError):
        calls.groups([untagged])


def test_cli_refuses_an_untagged_call_before_opening_a_connection():
    with pytest.raises(SystemExit) as error:
        calls.main(["add", "--who", "human", "--author", "test", "--thesis", "test", "--ticker", "NVDA",
                    "--direction", "short", "--benchmark", "SMH"])
    assert error.value.code == 2
