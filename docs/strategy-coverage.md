# Strategy coverage: every strategy in dry run, every family as a trial

Two questions from 2026-11-09: can every strategy run as a dry-run bot, and does the repo cover every strategy family people trade. Numbers measured on home-k3s that day.

## Every strategy in dry run

### What one bot costs

| Resource | Per bot | Source |
|---|---|---|
| Memory in use | about 320 MiB (308 to 351 across the 12 league bots) | `kubectl top pods -n freqtrade` |
| Memory request / limit | 256 Mi / 768 Mi | home-config `apps/freqtrade/statefulset.yaml` |
| CPU | 6 to 15 m | `kubectl top pods` |
| PVC | 1 Gi claimed on local-path, under 1 MiB used (488 K after 5 days) | `du` in league-1 |
| Candle calls | one OHLCV call per pair per new candle: 10 pairs on a 5m strategy is 2 a minute, on a 1m strategy 10 | freqtrade refresh, 10-pair whitelists |

Freqtrade runs one strategy per process, so many strategies per bot means one process each anyway. Three ways to share a bot were looked at and dropped:

- Several freqtrade processes in one pod: each still needs its own roughly 300 MiB (Python, ccxt, pandas), so the only saving is the PVC, which local-path does not really reserve. It breaks the one-pod-one-row roster that ingest, the watcher and the metrics read.
- Producer and consumer mode: a consumer gets the producer's analysed dataframe, which holds one strategy's indicators, so the other strategies would not run their own logic.
- One strategy that ORs every signal: that is a new strategy, and it forward-tests none of the originals. Running strategies together is what `lab/mixtures.py` scores.

### What every strategy on one venue costs

61 strategy classes are on disk. Leaving out templates, exit examples, the lookahead-biased controls and the futures research (the `skip_families` of `[strategies]` in `lab/cohorts.toml`), 52 can trade: 23 on 5m, 12 on 1h, 6 on 1m, 5 on 4h, 3 on 15m, 3 on 1d.

| Venue | Calls a minute for 52 bots | Venue public limit | Fits |
|---|---|---|---|
| binanceus | 110 (about 220 request weight) | 1200 weight a minute per IP | yes, at 18% |
| kraken | 110 | about 1 call a second per IP (Kraken's published guidance; unmeasured) | no: almost twice the limit |
| coinbase | 110 | 10 calls a second per IP | yes, but its 2.4% round trip ends nearly every backtest in ruin, so the bots would mostly measure fees |

On binanceus the cohort needs about 16 GiB of memory in use, 13 GiB requested, and 52 PVCs. The amd64 nodes have room: requests sit at 13% (hp-victus), 19% (asus-laptop) and 21% (razer-laptop) of allocatable memory. Every pod shares the cluster's one public address with the backfill job, so the cohort gets half of binanceus' limit (`[strategies.budget]`) and the picker refuses a cohort over it.

### What dry run adds

Dry run adds no trial: a strategy file in the repo is a trial whether or not a bot runs it, and the nightly analysis reruns each strategy's current year every night, so every strategy is already tested on candles that did not exist when it was written. What a bot adds is what the backtest leaves out: live fills, fees on real order books, candles arriving late. The `drift` table compares the two, and it needs weeks of trades per bot. That is the reason to run every strategy: drift per strategy. Edge still comes from the backtests.

### Recommendation and what was built

Run every tradable strategy on binanceus only, one bot each, under its own settings: the `strategies` cohort.

- `python -m lab.cohorts pick strategies` prints the rows: every loadable, lookahead-clean strategy from the newest discovery outside the skipped families, overlay `base`, refused when the venue's candle calls pass its budget.
- `ci/cohort_roster.py strategies` opens the home-config PR that appends them. It now skips a row that would run a strategy, exchange and overlay a bot already runs, so the league's Bandtastic, Strategy001 and Supertrend on binanceus are not doubled: 49 new bots.
- Seeding waits for the image with this change to deploy; a person merges the roster PR, as with every cohort.

## Strategy families

Status is `scanned` (in `user_data/strategies/`, backtested nightly, a counted trial), `staged` (in `lab/families/`, loads and is checked but adds no trials) or `blocked`. The family of each scanned class is in `lab/families.toml`.

| Family | Status | In the repo |
|---|---|---|
| Trend | scanned, staged | 10 scanned (MA crosses, Supertrend, ADX); `lab/families/trend/` Donchian ensemble, staged |
| Breakout | scanned, staged | Diamond, SqueezeBreakout (new); `lab/families/chart/` pattern breaks, staged |
| Mean reversion | scanned, staged | 24 scanned (RSI, Bollinger, CCI dips); `lab/families/range/` flat-climate z-score, staged |
| Momentum | scanned | 8 MACD-style; TSMomentum and XSMomentumRotation (new): time-series and cross-sectional |
| Grid | scanned | GridLadder (new): layers a step apart under a 72-hour mean, each sold a step up |
| DCA as a strategy | scanned | DCASafetyOrders (new): base order, four scaled safety orders, one take profit on the average. Plain buy-every-week DCA has no exit, so it is not a trade strategy; the book scores holding BTC instead |
| Pairs, long leg | scanned | RatioRevert (new): buy a coin cheap against BTC. Unhedged, so it carries the market as well |
| Candlestick and count patterns | scanned | PatternRecognition, TDSequential; `user_data/strategies/candles/`, 47 bullish TA-Lib patterns with a fixed exit |
| Time based | scanned | HourBasedStrategy |
| FreqAI ML | staged | `lab/families/ml/` LightGBM: needs the ML image and purged walk-forward folds (README, Staged families) |
| Pairs, market neutral | blocked | the short leg needs margin or futures; `lab/league.py` runs spot only |
| Funding carry and basis | blocked | long spot, short perpetual: needs a futures short. Funding is recorded (`funding_rates`, `lab/futures.py`), so carry can be measured offline |
| Shorts on a trend or a bust | blocked | futures; research only in `lab/research/` (BustShort, DonchianLongShort, docs/short-bust.md) |
| Cross-venue spread | blocked | needs inventory on two exchanges and two legs at once; a freqtrade bot trades one exchange. The event-contract version is `lab/markets/` cross-venue gaps |
| Market making | blocked by the backtester | freqtrade fills a limit order when price touches it, with no queue position, so a maker's edge cannot be measured; it would read as free money |

## Every added strategy raises the bar

Each strategy added here is a trial, and so is each venue it runs on. The deflated Sharpe compares every strategy with the best Sharpe luck alone would give across the effective number of strategies tried, so six new families on five venues add about 30 series to the 251 behind the 365-day window. The effective count (12.1 on 2026-11-09) grows by less than that, since the count discounts correlated series. That is the cost of coverage, and it is why the trial register (922 rows) keeps every trial even after its file is gone.

## Control set (tr-67)

Retiring strategies cannot lower the bar honestly: their trials stay in the register. Retiring saves compute and nothing else. On the 2026-11-09 365-day binanceus window, the proposal:

- Keep as the control set: the best of each family by the low end of the Sharpe interval (BinHV45, CombinedBinHAndCluc, ClucMay72018, BinHV27, UniversalMACD, SwingHighToSky, Bandtastic, MACDStrategy_crossed, MultiMa, PatternRecognition, Diamond, HourBasedStrategy), the null control DoesNothingStrategy, the three lookahead-biased controls and every strategy added here.
- Candidates to retire from the nightly backtests: the fee churners with thousands of trades and a Sharpe interval wholly under -5 (Scalp, ASDTSRockwellTrading, CofiBitStrategy, SmoothScalp, ReinforcedSmoothScalp). Retiring them is a separate PR and a person's call.
