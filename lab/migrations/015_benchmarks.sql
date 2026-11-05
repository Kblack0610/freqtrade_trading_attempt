-- The plain alternatives to an active strategy, rebuilt by every analysis. trades is NULL for monthly DCA, and
-- so is cagr, since its money goes in over time.
CREATE TABLE benchmarks (
    name              text NOT NULL,
    win               text NOT NULL,
    start_day         date NOT NULL,
    end_day           date NOT NULL,
    total_return      double precision NOT NULL,
    cagr              double precision,
    max_drawdown      double precision NOT NULL,
    after_tax_return  double precision NOT NULL,
    trades            integer,
    PRIMARY KEY (name, win)
);
