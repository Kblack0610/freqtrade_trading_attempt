#!/usr/bin/env python3
"""Link the same event across venues: rules first, then LLM proposals, then confirmation by resolution.

  python -m lab.markets.links [--no-llm]

Rules are exact and confirm a link when they make it. The LLM, reached through the LiteLLM gateway with a scoped key
(LITELLM_API_KEY; base URL and model in lab/markets.toml [links.llm]), only proposes: its links stay `proposed` until
both markets resolve the same way. Every pass ends with the resolution check, which confirms agreeing links and
rejects disagreeing ones, rule links included, so a broken rule shows as rejections. With no key the LLM pass is
skipped and says so; rules and the check still run. The only writer of market_links and market_link_asks
(migration 020). Reads markets, outcomes and resolutions, which lab/markets/recorder.py writes.

Rule kalshi_game: an odds_api game links to the Kalshi game-winner event of its series ([links.rules.kalshi_game])
whose ticker date (KXNBAGAME-26NOV09SASPHX) is the game's date in US Eastern time, when each team names exactly one
Kalshi market by its yes subtitle ("San Antonio" for San Antonio Spurs, "Los Angeles C" for Los Angeles Clippers).
More than one such event on the date (a doubleheader) is ambiguous, and the rule leaves it to the LLM.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict
from zoneinfo import ZoneInfo

import requests

from lab.db import connect
from lab.markets.recorder import load_config

EASTERN = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                      "DEC"), 1)}
INSERT = """INSERT INTO market_links (venue_a, market_id_a, outcome_a, venue_b, market_id_b, outcome_b, source, score,
                reason, status, confirmed_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING"""


def words(s: str | None) -> list[str]:
    """Lower-case ASCII words: accents and punctuation gone, so St. Louis is st louis and Montreal matches."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", s.lower())


def names_team(sub: str | None, team: str) -> bool:
    """Kalshi's subtitle names the team by its leading words, the last one possibly cut to an initial."""
    s, t = words(sub), words(team)
    if not s or len(s) > len(t):
        return False
    *head, last = s
    return head == t[:len(head)] and (last == t[len(s) - 1] or len(last) == 1 and t[len(s) - 1].startswith(last))


def kalshi_date(event_id: str | None) -> dt.date | None:
    m = re.match(r"[^-]+-(\d\d)([A-Z]{3})(\d\d)", event_id or "")
    if not m or m[2] not in MONTHS:
        return None
    return dt.date(2000 + int(m[1]), MONTHS[m[2]], int(m[3]))


def outcome_word(outcome: str) -> str:
    return outcome.rsplit(":", 1)[-1]


def rule_kalshi_game(conn, rcfg: dict) -> list[tuple]:
    series = rcfg["series"]
    events = defaultdict(lambda: defaultdict(list))  # (series, date) -> event -> [(market, subtitle)]
    for mid, eid, ser, sub in conn.execute(
            "SELECT market_id, event_id, series_id, subtitle FROM markets WHERE venue = 'kalshi' AND series_id = ANY(%s)",
            (list(series.values()),)):
        if (d := kalshi_date(eid)) is not None:
            events[(ser, d)][eid].append((mid, sub))
    links = []
    for gid, sport, title, close in conn.execute(
            "SELECT market_id, series_id, title, close_time FROM markets WHERE venue = 'odds_api' AND series_id = ANY(%s)"
            " AND close_time IS NOT NULL", (list(series),)):
        away, home = title.rsplit(" at ", 1)
        found = []
        for markets in events[(series[sport], close.astimezone(EASTERN).date())].values():
            pairs = {}
            for mid, sub in markets:
                hits = [t for t in (away, home) if names_team(sub, t)]
                if len(hits) == 1:
                    pairs.setdefault(hits[0], []).append(mid)
            if sorted(pairs) == sorted((away, home)) and all(len(v) == 1 for v in pairs.values()):
                found.append(pairs)
        if len(found) == 1:
            links += [("odds_api", gid, team, "kalshi", mids[0], "yes") for team, mids in found[0].items()]
    return links


def run_rules(conn, cfg: dict) -> int:
    links = rule_kalshi_game(conn, cfg["rules"]["kalshi_game"])
    with conn.transaction():
        cur = conn.cursor()
        cur.executemany(INSERT, [(*lk, "rule:kalshi_game", None, None, "confirmed", "rule") for lk in links])
    return len(links)


class Llm:
    """One chat call per market through the LiteLLM gateway's OpenAI-style /v1/chat/completions."""

    def __init__(self, lcfg: dict, key: str, session: requests.Session | None = None):
        self.base, self.model = lcfg["base_url"].rstrip("/"), lcfg["model"]
        self.timeout, self.key = float(lcfg.get("timeout_s", 120)), key
        self.http = session or requests.Session()

    def ask(self, prompt: str) -> str:
        r = self.http.post(f"{self.base}/v1/chat/completions", timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.key}"},
                           json={"model": self.model, "temperature": 0,
                                 "messages": [{"role": "system", "content": SYSTEM},
                                              {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


SYSTEM = ("You match markets across betting venues. Two markets match only when they settle on the same real-world "
          "proposition: the same game or event, the same date, the same condition. A similar title is not enough. "
          "Reply with one JSON object and nothing else.")


def prompt(a: dict, cands: list[dict]) -> str:
    return (
        "Market A:\n" + json.dumps(a) + "\n\nCandidates:\n"
        + "\n".join(f"{i}: {json.dumps(c)}" for i, c in enumerate(cands))
        + '\n\nFor each candidate that settles on the same proposition as one of market A\'s outcomes, give the outcome '
          'of A and the outcome of the candidate that pay together. Reply as {"matches": [{"candidate": <index>, '
          '"outcome_a": "<one of A outcomes>", "outcome_b": "<one of the candidate outcomes>", "confidence": <0 to 1>, '
          '"reason": "<one line>"}]}, with an empty list when none match.')


def parse(reply: str, a: dict, cands: list[dict]) -> list[tuple[dict, str, str, float | None, str]]:
    """The valid matches in a reply; anything naming a candidate or outcome that was not offered is dropped."""
    body = reply[reply.find("{"):reply.rfind("}") + 1]  # past any thinking the model prints first
    try:
        matches = json.loads(body).get("matches") or []
    except (ValueError, AttributeError):
        return []
    out = []
    for m in matches:
        try:
            c = cands[int(m["candidate"])]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        conf = m.get("confidence")
        if m.get("outcome_a") in a["outcomes"] and m.get("outcome_b") in c["outcomes"]:
            out.append((c, m["outcome_a"], m["outcome_b"], float(conf) if isinstance(conf, (int, float)) else None,
                        str(m.get("reason") or "")[:500]))
    return out


def _outcomes(conn, venue: str) -> dict[str, list[str]]:
    by = defaultdict(set)
    for mid, o in conn.execute("SELECT market_id, outcome FROM outcomes WHERE venue = %s", (venue,)):
        by[mid].add(outcome_word(o))
    return {k: sorted(v) for k, v in by.items()}


OPEN = "status NOT IN ('settled', 'finalized', 'closed', 'resolved')"


def _open(conn, venue: str, lo: dt.datetime, hi: dt.datetime) -> list[dict]:
    return [_row(r) for r in conn.execute(
        f"SELECT market_id, title, subtitle, close_time FROM markets WHERE venue = %s AND close_time BETWEEN %s AND %s "
        f"AND {OPEN}", (venue, lo, hi))]


def _unasked(conn, va: str, vb: str, lo: dt.datetime, hi: dt.datetime) -> list[dict]:
    """a's open markets closing in [lo, hi] with no link to b and no ask about b yet, soonest closing first."""
    return [_row(r) for r in conn.execute(
        f"""SELECT market_id, title, subtitle, close_time FROM markets m
            WHERE venue = %s AND close_time BETWEEN %s AND %s AND {OPEN}
            AND NOT EXISTS (SELECT 1 FROM market_links l WHERE l.venue_a = m.venue AND l.market_id_a = m.market_id
                            AND l.venue_b = %s)
            AND NOT EXISTS (SELECT 1 FROM market_link_asks k WHERE k.venue_a = m.venue AND k.market_id_a = m.market_id
                            AND k.venue_b = %s)
            ORDER BY close_time""", (va, lo, hi, vb, vb))]


def _row(r) -> dict:
    return {"market_id": r[0], "title": r[1], "subtitle": r[2], "close_time": r[3]}


def run_llm(conn, cfg: dict, llm: Llm, now: dt.datetime) -> tuple[int, int]:
    lcfg = cfg["llm"]
    stop, window = set(lcfg.get("stopwords", ())), dt.timedelta(hours=lcfg.get("window_h", 96))
    max_asks, asks, proposed = lcfg.get("max_asks", 50), 0, 0
    for va, vb in lcfg["pairs"]:
        todo = _unasked(conn, va, vb, now, now + dt.timedelta(days=lcfg.get("horizon_d", 14)))
        if not todo or asks >= max_asks:
            continue
        pool = _open(conn, vb, todo[0]["close_time"] - window, todo[-1]["close_time"] + window)
        index = defaultdict(list)
        for i, m in enumerate(pool):
            for w in set(words(f"{m['title']} {m['subtitle'] or ''}")) - stop:
                index[w].append(i)
        out_a, out_b = _outcomes(conn, va), _outcomes(conn, vb)
        for m in todo:
            if asks >= max_asks:
                break
            hits = defaultdict(int)
            for w in set(words(f"{m['title']} {m['subtitle'] or ''}")) - stop:
                for i in index.get(w, ()):
                    if abs(pool[i]["close_time"] - m["close_time"]) <= window:
                        hits[i] += 1
            best = sorted(hits, key=lambda i: (-hits[i], pool[i]["close_time"]))[:lcfg.get("candidates", 15)]
            if not best:
                continue  # nothing shares a word yet; ask again once something does
            a = {**_view(m), "venue": va, "outcomes": out_a.get(m["market_id"], ["yes"])}
            cands = [{**_view(pool[i]), "venue": vb, "outcomes": out_b.get(pool[i]["market_id"], ["yes"])} for i in best]
            try:
                found = parse(llm.ask(prompt(a, cands)), a, cands)
            except (requests.RequestException, KeyError, IndexError, ValueError) as e:
                print(f"links: llm: {va} {m['market_id']}: {type(e).__name__}: {e}", flush=True)
                continue
            with conn.transaction():
                cur = conn.cursor()
                cur.executemany(INSERT, [(va, m["market_id"], oa, vb, c["id"], ob, f"llm:{llm.model}", conf,
                                          why, "proposed", None) for c, oa, ob, conf, why in found])
                cur.execute("INSERT INTO market_link_asks (venue_a, market_id_a, venue_b, model, proposed) "
                            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                            (va, m["market_id"], vb, llm.model, len(found)))
            asks, proposed = asks + 1, proposed + len(found)
    return asks, proposed


def _view(m: dict) -> dict:
    return {"id": m["market_id"], "title": m["title"], "subtitle": m["subtitle"],
            "closes": m["close_time"].isoformat() if m["close_time"] else None}


def pays(outcome: str, result: str) -> bool | None:
    """Did this outcome word pay, given the market's resolution? None when the market was void or split."""
    if result in ("void", "split"):
        return None
    return result == "yes" if outcome == "yes" else result == outcome


def check_resolutions(conn) -> dict:
    rows = conn.execute(
        """SELECT l.id, l.outcome_a, ra.result, l.outcome_b, rb.result FROM market_links l
           JOIN resolutions ra ON ra.venue = l.venue_a AND ra.market_id = l.market_id_a
           JOIN resolutions rb ON rb.venue = l.venue_b AND rb.market_id = l.market_id_b
           WHERE l.checked_at IS NULL""").fetchall()
    n = {"agree": 0, "disagree": 0, "void": 0}
    with conn.transaction():
        cur = conn.cursor()
        for lid, oa, ra, ob, rb in rows:
            a, b = pays(oa, ra), pays(ob, rb)
            if a is None or b is None:
                n["void"] += 1
                cur.execute("UPDATE market_links SET checked_at = now() WHERE id = %s", (lid,))
            elif a == b:
                n["agree"] += 1
                cur.execute("UPDATE market_links SET status = 'confirmed', confirmed_by = coalesce(confirmed_by, "
                            "'resolution'), resolved_agrees = true, checked_at = now() WHERE id = %s", (lid,))
            else:
                n["disagree"] += 1
                cur.execute("UPDATE market_links SET status = 'rejected', resolved_agrees = false, checked_at = now() "
                            "WHERE id = %s", (lid,))
    return n


def main(argv: list[str] | None = None, llm: Llm | None = None, now: dt.datetime | None = None) -> int:
    ap = argparse.ArgumentParser(prog="markets.links")
    ap.add_argument("--no-llm", action="store_true", help="rules and the resolution check only")
    args = ap.parse_args(argv)
    cfg = load_config()["links"]
    now = now or dt.datetime.now(dt.timezone.utc)
    with connect(autocommit=True) as conn:
        print(f"links: rules: {run_rules(conn, cfg)} links", flush=True)
        if not args.no_llm:
            key = os.environ.get("LITELLM_API_KEY")
            if llm is None and not key:
                print("links: llm: LITELLM_API_KEY not set, no proposals this run", flush=True)
            else:
                asks, proposed = run_llm(conn, cfg, llm or Llm(cfg["llm"], key), now)
                print(f"links: llm: {asks} asked, {proposed} proposed", flush=True)
        print(f"links: resolutions: {check_resolutions(conn)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
