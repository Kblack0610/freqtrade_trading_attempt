"""lab/requirements.lock agrees with lab/requirements.txt: the image installs only the lock, so a pin edited in
requirements.txt without re-running ci/lock_requirements.sh would never reach the image."""

import re
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s\\;]+)")


def _pins(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text().splitlines():
        m = PIN.match(line)
        if m:
            pins[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return pins


def test_every_top_level_pin_is_in_the_lock():
    wanted = _pins(LAB / "requirements.txt")
    locked = _pins(LAB / "requirements.lock")
    assert wanted, "requirements.txt has no pins"
    stale = {name: (version, locked.get(name)) for name, version in wanted.items() if locked.get(name) != version}
    assert not stale, f"re-run ci/lock_requirements.sh; requirements.txt vs lock: {stale}"


def test_every_locked_package_carries_a_hash():
    text = (LAB / "requirements.lock").read_text()
    blocks = re.split(r"\n(?=[A-Za-z0-9])", text.split("\n# The following packages were excluded")[0])
    for block in blocks:
        if PIN.match(block):
            assert "--hash=sha256:" in block, block.splitlines()[0]
