"""The site's tabs, as data: group headings, titles, paths and the paths each tab also marks as current.

One question per tab. base.html loops over `groups(path)`; the shell markup is web-kit's (web/kit.lock).
"""

# (group, [(title, path, extra prefixes the tab also owns)])
TABS = (
    ("Now", (
        ("Overview", "/", ("/bots",)),
        ("Live", "/live", ()),
        ("Book", "/book", ()),
        ("Accounts", "/accounts", ()),
        ("Suggestions", "/suggestions", ()),
    )),
    ("Evidence", (
        ("Strategies", "/strategies", ()),
        ("Cohorts", "/cohorts", ()),
        ("Walk-forward", "/walkforward", ()),
        ("Confidence", "/confidence", ()),
        ("Calls", "/calls", ()),
        ("Coverage", "/coverage", ()),
    )),
    ("Pipeline", (
        ("Markets", "/markets", ("/links",)),
        ("Data", "/data", ()),
        ("Runs", "/runs", ()),
        ("Guide", "/guide", ()),
    )),
)


def current(path: str, tab_path: str, extra: tuple = ()) -> bool:
    if tab_path == "/":
        return path == "/" or any(path == p or path.startswith(p + "/") for p in extra)
    return any(path == p or path.startswith(p + "/") for p in (tab_path, *extra))


def groups(path: str) -> list[dict]:
    return [{"name": name, "tabs": [{"title": t, "path": p, "current": current(path, p, extra)} for t, p, extra in tabs]}
            for name, tabs in TABS]
