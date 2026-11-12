-- The average winning and losing trade of a window, as profit ratios (the loss positive): the strategy card's payoff
-- ratio (docs/strategy-coverage.md, The strategy card). NULL until the window's strategy reruns.
ALTER TABLE strategy_windows ADD COLUMN avg_win double precision, ADD COLUMN avg_loss double precision;
ALTER TABLE overlay_windows ADD COLUMN avg_win double precision, ADD COLUMN avg_loss double precision;

-- The residual clusters /coverage groups strategies by (lab/diversity.py clusters): per venue, average-linkage
-- clusters on the |rho| of BTC-residual daily returns over the last 365 days, cut at [diversity] max_rho. Cluster 1
-- is the largest. Written only by the nightly analysis.
CREATE TABLE residual_clusters (
    strategy text NOT NULL,
    exchange text NOT NULL,
    cluster  integer NOT NULL,
    days     integer NOT NULL,
    PRIMARY KEY (strategy, exchange)
);
-- Per venue: how many series were clustered, into how many clusters, and their effective count on residual returns.
CREATE TABLE residual_venues (
    exchange text PRIMARY KEY,
    series   integer NOT NULL,
    clusters integer NOT NULL,
    neff     double precision NOT NULL
);
