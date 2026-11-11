"""One request budget per venue, shared by every process that calls it from this egress IP.

A venue's limit is per IP or account, never per process, so each recorder and history job pacing itself still
adds up past it when they overlap (2026-11-11: three Kalshi history jobs and the recorder drew about 3,000 429s a
run). The budget is one row of `venue_budgets` (migration 041): `next_at` is the earliest moment the venue's next
request may go. Taking a turn moves it on by 1/rps in one statement and returns how long to wait for the slot
taken, so concurrent callers queue in order at the venue's rate however many there are. Time is the database's,
so pods with skewed clocks agree. A hold (a 429 seen by anyone) pushes `next_at` out so every caller pauses.

The budget keeps its own autocommit connection: a turn taken inside a caller's open transaction would hold the
row lock until that transaction ends and stall every other process.
"""

from lab.db import connect

TAKE = """
INSERT INTO venue_budgets AS b (venue, next_at) VALUES (%(venue)s, now() + make_interval(secs => %(step)s))
ON CONFLICT (venue) DO UPDATE SET next_at = greatest(b.next_at, now()) + make_interval(secs => %(step)s)
RETURNING extract(epoch FROM b.next_at - now())::float8 - %(step)s
"""

HOLD = """
INSERT INTO venue_budgets AS b (venue, next_at) VALUES (%(venue)s, now() + make_interval(secs => %(secs)s))
ON CONFLICT (venue) DO UPDATE SET next_at = greatest(b.next_at, now() + make_interval(secs => %(secs)s))
"""


class Budget:
    def __init__(self, venue: str, rps: float, conn=None):
        if rps <= 0:
            raise ValueError(f"{venue}: budget_rps must be above 0, not {rps}")
        self.venue, self.step, self._conn = venue, 1.0 / rps, conn

    @property
    def conn(self):
        if self._conn is None:
            self._conn = connect(autocommit=True)
        return self._conn

    def take(self) -> float:
        """Seconds to wait before this request may go; 0 when the venue is idle."""
        (wait,) = self.conn.execute(TAKE, {"venue": self.venue, "step": self.step}).fetchone()
        return max(0.0, wait)

    def hold(self, secs: float):
        """No request to the venue from any process for `secs` from now."""
        self.conn.execute(HOLD, {"venue": self.venue, "secs": float(secs)})
