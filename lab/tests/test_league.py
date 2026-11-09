import json

import pytest
from freqtrade.configuration import Configuration

from lab import league, variants
from lab.grid import variant_of
from lab.league import Row

TOML = "[overlays.slots]\nmax_open_trades = [5]\n[overlays.majors]\npairs = [2]\n"
SLOTS5 = variant_of({"max_open_trades": 5})[:8]
MAJORS2 = variant_of({"pairs": 2})[:8]


@pytest.fixture
def pod(tmp_path, monkeypatch):
    """A league pod's files and env; run(row) execs nothing and returns the effective config freqtrade would get."""
    (tmp_path / "variants.toml").write_text(TOML)
    monkeypatch.setattr(variants, "PATH", tmp_path / "variants.toml")
    monkeypatch.setattr(league, "STATE", tmp_path / "state")
    monkeypatch.setattr(league, "ROSTER", tmp_path / "roster.tsv")
    for k in ("FT_API_PASSWORD", "FT_API_JWT_SECRET", "FT_API_WS_TOKEN"):
        monkeypatch.setenv(k, "x" * 32)
    monkeypatch.setenv("LEAGUE_INDEX", "0")
    seen = {}
    monkeypatch.setattr(league.os, "execvp", lambda cmd, args: seen.update(args=args))

    def run(line: str) -> dict:
        (tmp_path / "roster.tsv").write_text(f"# name exchange strategy [cohort overlay]\n{line}\n")
        league.main()
        args = seen["args"]
        return Configuration.from_files([args[i + 1] for i, a in enumerate(args) if a == "--config"])
    return run


def test_a_three_column_row_is_the_league_on_its_own_settings(tmp_path):
    (tmp_path / "r.tsv").write_text("# c\n\nk-s kraken S\nk-a kraken S aggressive " + SLOTS5 + "\nk-b kraken S league base\n")
    assert league.roster_rows(tmp_path / "r.tsv") == [Row("k-s", "kraken", "S", "league", ""),
                                                      Row("k-a", "kraken", "S", "aggressive", SLOTS5),
                                                      Row("k-b", "kraken", "S", "league", "")]


@pytest.mark.parametrize("line", ["k-s kraken", "k-s kraken S aggressive"])
def test_a_row_of_another_width_is_refused(tmp_path, line, capsys):
    (tmp_path / "r.tsv").write_text(line + "\n")
    with pytest.raises(SystemExit):
        league.roster_rows(tmp_path / "r.tsv")
    assert "want 'name exchange strategy [cohort overlay]'" in capsys.readouterr().err


def test_an_overlay_reaches_the_config_freqtrade_runs(pod):
    base = pod("k-s kraken SampleStrategy")
    assert base["max_open_trades"] == 3
    assert pod(f"k-a kraken SampleStrategy aggressive {SLOTS5}")["max_open_trades"] == 5
    assert pod(f"k-m kraken SampleStrategy aggressive {MAJORS2}")["exchange"]["pair_whitelist"] == \
        base["exchange"]["pair_whitelist"][:2]


def test_an_overlay_missing_from_the_toml_is_refused(pod, capsys):
    with pytest.raises(SystemExit):
        pod("k-a kraken SampleStrategy aggressive deadbeef")
    assert "overlay 'deadbeef' is not in" in capsys.readouterr().err


@pytest.mark.parametrize("toml, params", [("dry_run = [false]", {"dry_run": False}),
                                          ('exchange = [{ key = "k" }]', {"exchange": {"key": "k"}})])
def test_an_overlay_cannot_set_dry_run_or_a_key(tmp_path, toml, params, capsys):
    (tmp_path / "variants.toml").write_text("[overlays.bad]\n" + toml + "\n")
    with pytest.raises(ValueError, match="an overlay may not set"):
        variants.load(tmp_path / "variants.toml")
    # and were one to get past variants.py, the guard reads the overlay file with the rest
    over = tmp_path / "overlay.json"
    over.write_text(json.dumps(params))
    with pytest.raises(SystemExit):
        league.guard([league.USER_DATA / "config.base.json", league.USER_DATA / "exchanges" / "kraken.json", over])
    assert "refusing to start" in capsys.readouterr().err
