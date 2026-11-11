-- One shared request budget per venue (lab/markets/budget.py): the earliest moment the venue's next request may go.
CREATE TABLE venue_budgets (
    venue text PRIMARY KEY,
    next_at timestamptz NOT NULL
);
