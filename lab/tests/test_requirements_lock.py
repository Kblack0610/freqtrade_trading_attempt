"""Each lab lock agrees with its requirements file: the image installs only requirements.lock and CI's quality job
only requirements-dev.lock, so a pin edited without re-running ci/lock_requirements.sh would never be installed."""

import re
from pathlib import Path

import pytest

LAB = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s\\;]+)")
PAIRS = [("requirements.txt", "requirements.lock"), ("requirements-dev.txt", "requirements-dev.lock")]


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text().splitlines():
        m = PIN.match(line)
        if m:
            pins[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return pins


@pytest.mark.parametrize("source,lock", PAIRS)
def test_every_top_level_pin_is_in_the_lock(source, lock):
    wanted = _pins(LAB / source)
    locked = _pins(LAB / lock)
    assert wanted, f"{source} has no pins"
    stale = {name: (version, locked.get(name)) for name, version in wanted.items() if locked.get(name) != version}
    assert not stale, f"re-run ci/lock_requirements.sh; {source} vs {lock}: {stale}"


@pytest.mark.parametrize("lock", [lock for _, lock in PAIRS])
def test_every_locked_package_carries_a_hash(lock):
    text = (LAB / lock).read_text()
    blocks = re.split(r"\n(?=[A-Za-z0-9])", text.split("\n# The following packages were excluded")[0])
    for block in blocks:
        if PIN.match(block):
            assert "--hash=sha256:" in block, block.splitlines()[0]
