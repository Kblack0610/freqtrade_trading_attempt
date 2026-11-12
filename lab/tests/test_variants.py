import pytest

from lab import grid, variants

TOY = """
[sweep]
max_chunks = 10

[overlays.slots]
max_open_trades = [1, 5]
stoploss = [-0.05, -0.15]

[overlays.majors]
pairs = [3]
max_open_trades = 1
stoploss = -0.05
"""


def test_a_grid_expands_every_axis_and_names_each_overlay_by_its_content(tmp_path):
    (tmp_path / "v.toml").write_text(TOY)
    overlays, sweep = variants.load(tmp_path / "v.toml")
    assert sweep == {"max_chunks": 10}
    assert len(overlays) == 5 and [o["group"] for o in overlays.values()] == ["slots"] * 4 + ["majors"]
    assert all(oid == grid.variant_of(o["params"])[:8] for oid, o in overlays.items())
    # the same settings in another group, in another order, are the same overlay
    (tmp_path / "w.toml").write_text(TOY + '\n[overlays.again]\nstoploss = -0.15\nmax_open_trades = 5\n')
    assert variants.load(tmp_path / "w.toml")[0].keys() == overlays.keys()


# Overlays the nightly sweep had run by 2026-11-12: dropping one deletes its stored chunks (analyze.sync_overlays).
RUN = {"bca857b8", "89287667", "98de77f2", "dba07a1d", "76524a07", "e90ae5ce"}


def test_the_shipped_grid_keeps_every_overlay_already_run_and_spawns_wider_than_one_axis_at_a_time():
    overlays, sweep = variants.load()
    assert RUN <= overlays.keys() and sweep["max_chunks"] > 0
    assert len(overlays) >= 20
    # the spawn crosses settings: overlays that set more than one, and take-profit exits both short and absent
    assert sum(len(o["params"]) > 1 for o in overlays.values()) >= 10
    assert {o["params"]["minimal_roi"]["0"] for o in overlays.values() if "minimal_roi" in o["params"]} >= {0.01, 10}


@pytest.mark.parametrize("params", [{"dry_run": False}, {"exchange": {"key": "k"}}, {"timeframe": "1h"},
                                    {"pairs": 0}, {"stoploss": 0.1}, {"stake_amount": "all"}])
def test_an_overlay_outside_the_allowed_settings_is_refused(tmp_path, params):
    with pytest.raises(ValueError):
        variants.check(params)
    body = "\n".join(f"{k} = {str(v).lower() if isinstance(v, bool) else repr(v)}" for k, v in params.items()
                     if not isinstance(v, dict))
    if body:
        (tmp_path / "v.toml").write_text(f"[overlays.bad]\n{body}\n")
        with pytest.raises(ValueError):
            variants.load(tmp_path / "v.toml")


def test_pairs_takes_the_first_of_the_whitelist_in_its_order():
    wl = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"]
    assert variants.config({"pairs": 2, "max_open_trades": 1}, wl) == {"max_open_trades": 1,
                                                                        "exchange": {"pair_whitelist": wl[:2]}}
    assert variants.config({"stake_amount": 100}, wl) == {"stake_amount": 100}
