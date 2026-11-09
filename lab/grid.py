"""The one grid expander every sweep uses: the event-contract families (lab/markets/families.py, lab/markets/paper.py)
and the crypto setup overlays (lab/variants.py). A grid is a table: a list is an axis, a list of tables merges each
table into the variant, anything else is fixed. variant_of names a params dict by its content, so the same variant
has the same name however it was reached, and a trial register keyed by it counts each variant once."""

import hashlib
import itertools
import json


def variants(grid: dict) -> list[dict]:
    """Every params dict of a grid, in the order the grid lists them."""
    axes = [[(k, x) for x in v] if isinstance(v, list) else [(k, v)] for k, v in grid.items()]
    out = []
    for combo in itertools.product(*axes):
        p = {}
        for k, v in combo:
            if isinstance(v, dict):
                p.update(v)
            else:
                p[k] = v
        out.append(p)
    return out


def variant_of(params: dict) -> str:
    return hashlib.md5(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()
