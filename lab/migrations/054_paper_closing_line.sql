-- The closing line of each paper fill, for its CLV: the yes mid of its market in the last paper pass before the
-- market closed (lab/markets/paper.py closing_line). CLV of a fill: that mid (one minus it for a no) less its price.
ALTER TABLE paper_orders ADD COLUMN close_mid double precision, ADD COLUMN close_at timestamptz;
