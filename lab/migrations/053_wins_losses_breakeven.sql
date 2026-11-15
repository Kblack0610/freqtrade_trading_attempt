-- Wins, losses and the breakeven win rate of a window, so the board shows "W-L, win% vs breakeven%".
ALTER TABLE strategy_windows ADD COLUMN wins integer, ADD COLUMN losses integer, ADD COLUMN breakeven_winrate double precision;
ALTER TABLE overlay_windows ADD COLUMN wins integer, ADD COLUMN losses integer, ADD COLUMN breakeven_winrate double precision;
