# Markets: venue reference

The facts `lab/markets/` is built on, one section per venue. Checked 2026-11-05; each claim names its source and how sure we are of it. Nothing here trades money. Every adapter is read-only until P7, and P7 trades only on the Kalshi demo exchange. Plan: `id-like-to-setup-abundant-seahorse` (epic tr-37). Brief: `docs/briefs/2026-11-05-markets.md` in the lab project.

Confidence: **doc** means read on the venue's own page, **secondary** means a law firm, tracker or news page, and **snippet** means a search summary only. A snippet claim is re-checked before code relies on it.

## Summary

| Venue | Role for us | Public reads | History from the venue | Demo | US resident may trade |
|---|---|---|---|---|---|
| Kalshi | data, demo orders in P7, live in P9 behind the decide row | REST without a key; websocket needs a key | yes, `/historical/*` | yes | yes, state litigation on sports contracts |
| Polymarket US | data | REST without a key | price history only, 45-day retention | institutional Preprod only | yes, Nevada blocked |
| Polymarket global | data only | REST and websocket without a key | price history | none | no: the US is close-only |
| PredictIt | data | one snapshot endpoint | none | none | yes, politics only, no trading API |
| Manifold | data, play money | REST and websocket without a key | bets API plus dumps to 2024-07 | play money | play money |
| The Odds API | sportsbook implied odds | API key (free tier 500 credits/month) | paid plans, from 2020-06 | n/a | we never bet at a sportsbook |

Open markets counted 2026-11-05: Kalshi 178,883 open with combos excluded (`/markets?status=open&mve_filter=exclude`, 179 pages of 1000); Polymarket global more than 2,100 (Gamma refuses an offset past 2,100; third parties put it at 12k to 20k); PredictIt 148 markets with 542 contracts. Counted 2026-11-06: Polymarket global 234,259 through `/markets/keyset` (127,582 with a best bid), Polymarket US about 74,800, Manifold 26,299 open binary markets.

## Recorder

`python -m lab.markets.recorder [venue ...]` (cwd `/repo`) runs one sweep per named venue; with no names it runs `[recorder] default_venues` in `lab/markets.toml`, which is Kalshi only, because the 5-min CronJob runs it bare and a Polymarket global sweep alone takes about 10 minutes. The other venues run by name, each in its own home-config CronJob `trader-markets-recorder-<venue>`: `polymarket_us`, `predictit` and `manifold` every 5 minutes, `polymarket` every 15. `odds_api` runs as `trader-markets-recorder-odds-api`, every 6 hours at :07 (Sportsbook odds below). Each sweep reads every open market a page at a time and queries stored metadata and outcomes only for that page's market IDs. Historical venue rows stay in Postgres. Each outcome's top of book is diffed against `outcomes`. Changed quotes go to one parquet object, `s3://trader-markets/quotes/venue=<v>/date=<YYYY-MM-DD>/<HHMMSS>Z.parquet`; only after it lands are `markets`, `outcomes`, `resolutions` and a `market_sweeps` row committed together. Endpoint and keys come from `S3_ENDPOINT`, `S3_ACCESS_KEY` and `S3_SECRET_KEY`, and the database from `DATABASE_URL`.

Kalshi's `min_updated_ts` filter cannot be combined with `status=open`, so the venue cannot hand over only what changed. The recorder sweeps every open market and diffs on its side. Measured 2026-11-06 against the live API: 179k open markets in about 180 pages, 3 to 4.5 minutes a sweep with a few 429s absorbed by the backoff, 470 MB peak memory, and 12k to 16k changed quotes per 5 minutes after the first sweep, which writes everything. Resolutions are read from the last good sweep's start minus `resolutions_lookback_h`, so about 4k settled markets are re-read each run and stored once.

Reading the parquet: a row is a quote as first seen at `ts`. If the database commit fails after the object is written, the next run writes the same changes again with a later `ts`, so keep the first row of a run of equal quotes.

`python -m lab.markets.compact [venue ...]` (same cwd; no names means every `venue=` directory in the bucket) runs nightly and folds each settled day's parts into one object, `quotes/venue=<v>/date=<d>/day.parquet`, then deletes the parts. A day is settled once its end plus `[compact] grace_h` (1 h) is past, so no sweep still writes into it. It streams one part at a time, so memory stays near one part's size. The day object lists the parts it holds in its parquet metadata (`parts`), which is how a run that died after the write deletes them next time, and how a part that lands late is folded in the next night. `market_sweeps.object_key` keeps the part's name; after compaction its rows are in that day's `day.parquet`. In the window between the write and the deletes a reader sees those rows twice; `load_quotes` drops them as a run of equal quotes, and `read_quotes` (raw rows) does not.

## Links

`python -m lab.markets.links [--no-llm]` (same cwd as the recorder) maps the same event across venues into `market_links` (migration 020). A link says outcome_a of market a pays exactly when outcome_b of market b pays; the outcome word drops any quoter prefix, so it is `yes` for a binary market and the team for an `odds_api` game. A strategy uses a link only once it is `confirmed`.

- Rules run first and confirm what they make. `kalshi_game` links an `odds_api` game to the Kalshi game-winner event of its series (`[links.rules.kalshi_game]`) whose ticker date (`KXNBAGAME-26NOV09SASPHX`) is the game's US Eastern date, when each team names exactly one Kalshi market by its yes subtitle. Kalshi writes the city and cuts a shared one to an initial (`Los Angeles C`, `New York I`, seen on the live API 2026-11-06). Two events on one date, or a subtitle that names both teams, is left alone.
- The LLM proposes, through the LiteLLM gateway (`LITELLM_API_KEY`, a scoped virtual key; model in `[links.llm]`). For each `[a, b]` pair it takes a's open markets that are not yet linked or asked about, closing within `horizon_d`, offers the `candidates` b markets that close within `window_h` and share the most title words, and keeps only matches that name an offered candidate and offered outcomes. Its links stay `proposed`. `market_link_asks` records each market asked about, so a market with no match is not paid for twice. With no key the pass is skipped and the run says so.
- Every run ends with the resolution check: once both markets resolve and neither is void, an agreeing link becomes `confirmed` (by `resolution` unless a rule already confirmed it) and a disagreeing one `rejected`, a rule's included, so a broken rule shows as rejections. Report a strategy's results per link `source`.

## History

`python -m lab.markets.history {settled,bars,trades} [venue ...] [--budget-min N] [--restart]` (same cwd as the recorder) backfills what the recorder cannot see: markets that settled before it started. Knobs are `[history]` and `[history.<venue>]` in `lab/markets.toml`; progress is `market_backfill` (migration 021). A run stops between pages (settled) or chunks of `chunk_markets` markets (bars, trades) after `budget_min` (50) minutes and the next run resumes there, so each job can run as a CronJob until it reports nothing left. A Kalshi settle date holds 17k to 36k markets over the bars floor, more than one run fetches at the about 3 requests/s it gets, so each chunk is its own part under `history/parts/<job>/` and the date's object is written from the parts once the last is in (tr-181: before that, a date that outgrew one run restarted from its first market every run).

- `settled` walks each venue's settled markets newest first into `markets` and `resolutions`, plus each market's lifetime volume (`resolutions.volume`, 021). A row the recorder already wrote is kept; only a missing volume is filled in. The cursor is saved per page, and a finished walk is not repeated. Kalshi skips markets under `settled_min_volume` (100 contracts). Venues: Kalshi, Polymarket US and global, Manifold. PredictIt states no results and The Odds API sells history only on paid plans, so neither is here.
- `bars` writes hourly price bars, one object per venue and UTC settle date: `s3://trader-markets/history/bars/venue=<v>/date=<YYYY-MM-DD>.parquet` (venue, market_id, ts, period_s, bid, ask, open, high, low, close, volume, open_interest; a field the venue does not serve is null). It covers markets over `bars_min_volume` that settled within `bars_since_days`: Kalshi 100 contracts and 3 years, Polymarket global $1,000 and 3 years, Polymarket US everything in its 45 days.
- `trades` writes Kalshi trades the same way under `history/trades/` (trade_id, ts, price, count, taker_side), for markets over 10,000 contracts in the last 90 days. Kalshi prints about 3k trades a minute, so every trade back to 2021 is out of reach.

A date is marked done with its market count. When a later walk or the recorder adds markets to that date, the date is written again, and the new object replaces the old one. Rerunning is safe: Postgres writes are `ON CONFLICT`, and a parquet object is published only when it is closed.

Measured 2026-11-06, 3 minutes per venue against the live APIs: Kalshi walked 341k settled markets (5 days of the live side; 71k over 100 contracts), Polymarket global 40k (1 day; 8k over $1,000), Polymarket US 46k (under a day), and Manifold finished all 111k resolved binary markets, back to 2021-12. At those rates the two months of Kalshi's live side take about 40 minutes, and Polymarket global's 3 years about 55 hours of budget. Bars cost one request a market: a Kalshi date has about 14k markets over 100 contracts, about an hour at 0.15 s a market, and a date is never split, so a run can overrun its budget by one date.

What the venues serve:

- Kalshi: `/historical/cutoff` was 2026-09-06 for both markets and trades. The archive reaches back to 2021-06, and markets settled in the few hours around the cutoff appear on both sides. Archive candles use bare field names (`yes_bid.close`), live ones the `_dollars` and `_fp` names; the adapter reads both. One request returns at most 5,000 candles, so a long market is read in windows.
- Polymarket global: `/v2/prices-history` takes the YES token id. The list gives the condition id, so each market costs one Gamma lookup (`/markets?condition_ids=<id>&closed=true`, first of `clobTokenIds`) before its history, which pages by cursor. Only a price is served, stored as `close`.
- Polymarket US: `/v1/price-history?symbol=<slug>` returns `longPrice` and `shortPrice`; the YES ask is `longPrice` and the bid is 1 - `shortPrice`. Its list carries no volume.
- Manifold: `/v0/markets` pages by the last id, 1,000 a page; history keeps resolved binary markets.

## Backtest

`lab/markets/backtest.py` replays a venue's books through a strategy and scores the result with the crypto lab's statistics; fees are `lab/markets/fees.py`. Knobs are `[backtest]` and `[backtest.fees.<venue>]` in `lab/markets.toml`; results are `market_backtests` (migrations 022 and 049), one row per variant and test window. Its callers are the strategy families (Families, below).

- Books: `load_quotes` reads the recorder's change-only quotes, `load_bars` the backfill's bars by settle date. Bars carry no sizes, so each side's depth is `bar_volume_share` (10%) of the period's traded contracts. A market first appears at its first change in the window.
- Fills: the strategy sees each row in time order with the market's metadata, never its resolution. An order fills against the book as it stood `latency_s` later: YES at the ask, NO at one minus the bid, whole contracts, at most `depth_share` (half) of that side's top-of-book size less what earlier orders took from the same row. The rest is cancelled, because only the top of the book is recorded. Nothing fills once the market has closed or settled.
- Settlement: every fill is held to resolution. YES pays the resolution's value, NO one minus it, void refunds the price and keeps the fee. A strategy exits early by buying the other side.
- Fees: Kalshi `ceil(0.07 * C * P * (1 - P))` to the cent per order, checked against the schedule's examples (100 at 50c is $1.75, at 10c or 90c $0.63). Polymarket US 0.0695 with half-even cents. Polymarket global at 0.07, its highest category, since the markets table holds no category. PredictIt 10% of a winning position's profit. Manifold nothing. Maker fees are not modelled: every fill is a taker.
- Lookahead: a strategy with a `cutoff` (an LLM forecaster's training cutoff) is refused outright on a book that starts before it.
- Scoring: each fill is a trade in the shape `metrics.window_metrics` reads (close_rate is the payout, so the gross edge is payout over price), with daily returns on a 1000 USD wallet. `confidence.interval` gives the per-trade interval, resampling whole events (`metrics.cluster_mean_ci`, the same resampling as `research/kalshi_calibration.py`), and the row keeps each trade's event and the window's daily returns for the gate. `run()` writes the trial to `trials` (strategy = family, exchange = venue, variant = md5 of the params and the window's start date) before it reads a result, a refused run included. `trial_count` is every trial on a markets venue by the plain count, which can only overstate the deflated Sharpe bar compared with the effective count the crypto lab uses.

## Families

`python -m lab.markets.families [--venue kalshi] [--family F ...] [--rerun]` (same cwd as the recorder) walks every variant of each family forward. A test window is one whole calendar month before the current one whose every settle date the backfill has finished (`market_backfill`, bars, done); its book is the bars of the markets that settled in it. Each variant runs on each window through `backtest.run()`, so each window is a trial and its own `market_backtests` row; a stored window is not run again unless `--rerun`, and a run starts no window after `[families] budget_min`. The newest `[families] lockbox_months` windows are the lockbox. Each family's grid is `[families.<name>]` in `lab/markets.toml`: a list is an axis, a list of tables merges each table into the variant. Every family enters a market at most once, on its first row with a spread no wider than `max_spread`, and holds to resolution.

The gate pools each variant's windows before the lockbox and applies `suggest.toml [confidence]`: at least `min_oos_trades` trades, the `expectancy_level` interval low end above zero net of fees with whole events resampled, the deflated Sharpe over `min_dsr` at the markets trial count, PBO across the family's variants on the venue (`metrics.pbo_panel`, `[families] pbo_blocks`) under `max_pbo`, the minimum backtest length (`min_btl_ratio`), and at least `min_folds` months with `min_folds_positive` of them positive. A variant that passes every one replays the lockbox once, registered as trials, and is ready only if it is positive there too; the rest never read it. Results are `market_readiness`, one row per variant with each gate. Deploy: the nightly home-config CronJob `trader-markets-families` (Kalshi). Until the backfill finishes a whole month (2026-11-13: four settle dates), it writes no window.

- `longshot`, favourite-longshot bias: within `hours` of close, buy no on a yes mid in a longshot band or yes in a favourite band. 8 variants.
- `calibration`, base rates: count how often markets entered the same way resolved yes over the `train_days` before the window, per 5c price bucket, across the venue or per series, shrunk toward the bucket's price by `prior` markets; trade when the rate beats the ask (or the bid beats it) by `edge`. Only resolutions settled before the window are counted and no row before it is traded. 4 variants.
- `gap`, cross-venue gaps: a market linked by a rule (`market_links`, `confirmed_by = 'rule'`) to a reference venue's market trades when the reference's fair price beats its price by `edge`. A sportsbook's fair price is each book's implied odds divided by their sum, median across books; a binary venue's is its mid. A rule link later rejected by its resolution still trades, because the rule would have traded it. LLM links confirm only by resolving, so gap refuses them, and each rule source is its own variant, so results read per source. 2 variants. It needs the recorder's quotes of the reference venue over each window, so a recorded `odds_api`.
- `llm`, the forecaster: forward only. `backtest.replay` refuses any book that starts before its `cutoff` (`[families.llm]`, 2026-11-06: the model states no training cutoff, so the day forward scoring starts). It has no command here; the paper league scores it.

## Paper league

`python -m lab.markets.paper [--venue kalshi]` (same cwd as the recorder) is one pass of the paper league; it is meant to run after each recorder sweep. Each variant of each family in `[paper] families` (`lab/markets.toml`) is one account, `paper-<family>-<md5[:8]>`, with `[paper] capital` (1000) of paper money. Results are `paper_accounts` and `paper_orders` (migration 023). A pass:

- Settles every open fill whose market has a resolution, with the backtest's payout and fees.
- Shows each account the latest recorded book (`outcomes`, the recorder's last quote) of every unresolved open market closing within `horizon_h` (168), but only when the venue's last good sweep finished under `max_book_age_min` (15) ago. A dead recorder means no entries, not entries at old prices.
- Fills through `backtest.take`, the backtest's own fill model: the spread is crossed and at most `depth_share` of the side's top-of-book size is taken. Fees come from `fees.entry`, so a paper fill and a backtest fill of one book cost the same. Every order is stored, filled or not, so an account enters a market once, as in a backtest. An order the account's cash cannot cover is dropped unstored and asked again next pass.
- Marks equity: capital, plus realized profit, plus each open position at its exit price (a yes at the bid, a no at one minus the ask) less its cost.

The league runs `longshot` only (8 accounts). `calibration` needs a bars fit each pass, `gap` needs a recorded `odds_api`, and `llm` needs a LiteLLM key. The forecaster's paper scores are its only scores, so it joins once the key is in SOPS. `lab/watcher.py` watches each account for the daily loss limit and runs the kill drill on it; see the README, Watcher. Deploy: a CronJob in home-config `apps/freqtrade/`, not yet scheduled.

## Demo orders

`python -m lab.markets.kalshi_trade {balance,drill [--ticker T]}` is the order path, on the Kalshi demo exchange only. `KalshiDemo` refuses any base URL whose host is not `external-api.demo.kalshi.co` or `demo-api.kalshi.co`, so no config can point it at real money; P9 adds Kalshi to the live path behind that path's own guard. Keys are the demo account's: `KALSHI_DEMO_KEY_ID` and `KALSHI_DEMO_KEY_FILE` (an RSA or Ed25519 PEM).

- Signing (doc, https://docs.kalshi.com/getting_started/api_keys, 2026-11-06): `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` in epoch ms, and `KALSHI-ACCESS-SIGNATURE`, the base64 signature of timestamp + method + path. The path includes `/trade-api/v2` and drops the query. RSA keys sign with PSS SHA-256 and a digest-length salt.
- Orders (doc, api-reference create-order-v2, cancel-order-v2, get-order, 2026-11-06): `POST /portfolio/events/orders` takes `side` (`bid` buys yes), with `count` and `price` (dollars) as fixed-point strings. `DELETE /portfolio/events/orders/{id}?market_ticker=T` cancels. `GET /portfolio/orders/{id}` returns `status` (`resting`, `canceled` or `executed`). The legacy `/portfolio/orders` POST is deprecated.
- The drill picks an open demo market on exchange index `drill_exchange_index` (0) with an ask above `drill_price` (1c), or takes `--ticker`. Kalshi splits markets across exchange indexes (shards: 1 combos, 2 crypto and commodities, 3 tennis, baseball and basketball, 0 the rest; field `exchange_index` on each market, https://docs.kalshi.com/getting_started/exchange_sharding) and refuses an order on one the account holds no funds on with HTTP 404 `insufficient_shard_balance`, as the 2026-11-13 run did on a basketball market. The mock funds sit on index 0. It rests one yes contract at 1c, post only, reads it back, cancels it and reads it back again, writing each step to `demo_orders`. It exits 1 unless the order was seen resting and then canceled. A POST is retried on a 429 only, never after a 5xx or a timeout, because either may already have placed it (Rate limits).
- Reads lag writes on the demo (measured 2026-11-09): `GET /portfolio/orders/{id}` gave a 404 right after the place and returned the order by the next look, and still said `resting` 0.04 s after a cancel, `canceled` by 0.29 s. The client waits within `read_polls` x `read_poll_s` (`lab/markets.toml`), and a drill whose read-back fails still cancels the order, so it never stays on the book.
- First passed on the demo exchange 2026-11-09: `demo_orders` row 3 rested, then canceled, no error. Rows 1 and 2 are the runs before it: unfunded account, then the read lag. The keys are not in SOPS, so the drill runs by hand: `KALSHI_DEMO_KEY_ID` from rbw, the PEM from `/data/Downloads`, `DATABASE_URL` over a port-forward.
- The demo account starts empty and the API has no deposit call. Funding is on the demo.kalshi.co site, with the test debit cards Kalshi publishes (doc, https://help.kalshi.com/en/articles/13823775-creating-and-using-a-demo-account, 2026-11-09). The account holds $100 of mock money.

`python -m lab.markets.manifold_trade {balance,drill [--ticker ID]}` is the same drill on Manifold, in play money: a 1-mana YES limit bet at 1% on a market priced at 10% or more, expiring on its own after 10 minutes, then canceled and read back. Account, terms and routes: [platforms.md](platforms.md), Manifold. Not yet run: no key exists (sheet row tr-118).

## Site and telemetry

The site's Markets pages read the same tables, behind the same sign-in. `/markets` is each venue's newest recorder sweep (red once the last good one is over `[paper] max_book_age_min` old, the moment the paper league stops entries), the failed sweeps of the last week with their errors, and the history backfill per venue and job. `/links` counts links by source and status, lists the newest 200 (filter with `?status=`) and the LLM asks. `/markets/book` is every `market_backtests` row, the paper accounts with their watch state, the newest paper fills and the demo orders. Counts come from `market_sweeps`, never `count(*)` over `markets`, which holds the settled archive too.

The exporter adds `trader_markets_*` and `trader_paper_*` (README, Metrics). home-config alerts on them in `apps/monitoring/prometheus-rules-freqtrade.yaml`: a venue's last good sweep over 15 minutes old, three or more failed sweeps of one venue in an hour, and a paper account down `[book] hard_stop` (15%) of its capital from its 30-day peak. The Grafana dashboard "Freqtrade league and lab" has a Markets row over the same series.

## Rate limits

Every third-party HTTP call in `lab/` goes through `Api.request` (`lab/markets/api.py`; `Api.get` for a JSON read), which paces it two ways; no caller sleeps or retries on its own. `lab/tests/test_money.py` finds every `Api(...)` and raw HTTP client in `lab/` and `web/` and fails on one without a row.

- Shared budget: `budget_rps` in each `[venues.<v>]` table is how many requests a second every process on our egress IP may send that venue together. It is one row of `venue_budgets` (migration 041, `lab/markets/budget.py`): each request takes the next slot, `1/budget_rps` after the last one handed out, in one statement against the database clock, and sleeps until its slot. The recorder, the three history jobs and anything else calling a venue queue on that row, so jobs that overlap share the rate instead of adding up. The budget keeps its own autocommit connection, so a caller's open transaction never holds the row.
- Own gap: `page_gap_s` is the least time between one process's requests (`[history.<v>] page_gap_s` replaces it for history). A 429 or 5xx doubles that process's gap for the rest of its run, up to 30 s, and each success takes 0.01 s off it until it is back at `page_gap_s`.
- Retries: up to `retries`, each waiting the `Retry-After` the venue sends (seconds or a date), else `page_gap_s * 2^n`, at least 0.5 s and at most 60 s. A retry also holds the shared budget that long, so every process pauses, not only the one that got the 429. A timeout or dropped connection is retried the same way for an idempotent method (GET, DELETE of an order by id) and raised at once for a write; it holds no budget.
- What is retried depends on the method. A 429 is retried for every method: the provider did not act on it. A 5xx is retried only for GET, HEAD, OPTIONS, PUT and DELETE (the only DELETEs cancel an order by id). A POST or PATCH that drew a 5xx may have been carried out, an order placed, so it comes back to the caller at once. A timeout or a dropped connection is raised at once for every method. Signed headers and a signed query are built again for each attempt, so a retry carries a fresh timestamp.

Measured after #130 deployed (`kubectl -n freqtrade logs <pod> | grep -c 'HTTP 429'`, Kalshi pods, 2026-11-12 07:55 UTC):

| Job | 429s before | 429s after |
|---|---|---|
| history-settled (3 runs) | 950-1002 a run | 0 each (Kalshi already `done`) |
| history-bars (one run, 64 min so far) | about 530 | 54, still running |
| history-trades (first 4 min) | about 530 | 12, still running |
| recorder (4 sweeps) | 30-50 a sweep | 3, 3, 5, 3 a sweep |

Settled is clean and the rest is cut by about 10x, not to zero: the recorder's `/markets` pages still draw 3 to 5 429s a sweep, each retried and the gap doubled to 7.6 s by the fifth. The budget stays at 12 req/s, so the Kalshi limit is probably tighter than the published 20 on the history and market-list reads; a lower `budget_rps` for Kalshi is the lever if the count matters.

Budgets, about 60% of each venue's published limit:

| Venue | Published limit | `budget_rps` |
|---|---|---|
| Kalshi | Basic tier 200 read tokens/s, 10 tokens a request: 20 requests/s | 12 |
| Polymarket US | 20 requests/s per IP on the public gateway | 12 |
| Polymarket global | Gamma `/markets` 300 per 10 s; the data API shares the budget | 15 |
| Manifold | 500 requests/min per IP | 5 |
| PredictIt | none published | 1 |
| The Odds API | credits per month, no rate | 1 |

The other providers, each with its config beside the limit it quotes (all read 2026-11-11):

| Provider | Published limit | Config |
|---|---|---|
| Kalshi demo (`lab/markets/kalshi_trade.py`) | as Kalshi; the page does not say the demo differs | `[demo.kalshi]`, budget 5 |
| Manifold drill (`lab/markets/manifold_trade.py`) | per IP, so it takes `[venues.manifold]`'s budget | `[demo.manifold]` |
| Kalshi calibration (by hand) | as Kalshi; drew 429s at 8/s | `[research.kalshi_calibration]`, no budget: a workstation's IP |
| Alpaca paper | 200 requests/min per account | `lab/broker.toml` `[alpaca]`, budget 2 |
| Robinhood Crypto | 100 requests/min per account, bursts to 300 | `[robinhood]`, budget 1 |
| SnapTrade | 250 requests/min per clientId; no `Retry-After` | `[snaptrade]`, budget 2.5 |
| Blockstream Esplora | none published; a maintainer suggests up to 5/s | `lab/wallet.toml` `[esplora]`, budget 1 |
| OKX market-data-history | 5 requests per 2 s per IP | `lab/archive.py` `OKX`, gap 0.8 s |
| Kraken and OKX archive files (CDN) | none published | `lab/archive.py` `KRAKEN`, `OKX_FILES` |

Not on `Api`: our own services (the LiteLLM gateway in `lab/markets/links.py`, the bots' API in `lab/ingest.py`, the pod health check, Forgejo sign-in), and ccxt clients (`lab/futures.py`, `lab/backfill.py`, `lab/pairs.py`), which pace with ccxt's own `enableRateLimit`.

Kalshi (doc, https://docs.kalshi.com/getting_started/rate_limits, rechecked 2026-11-11): a 429 carries no `Retry-After` or `X-RateLimit-*` header, and "there is no penalty or cooldown": the next request succeeds once the bucket refills. Before the shared budget, on 2026-11-11 the history jobs (settled every 3 h, bars and trades an hour and two after it) ran past `budget_min` into each other, three at about 6.7 requests/s each beside the recorder, over the 20/s limit: 950 to 1002 `HTTP 429` lines a settled run, about 530 a bars or trades run, 30 to 50 in a recorder run, and three recorder runs that ran out of retries. Why the history jobs overrun is tr-181. `lab/markets/research/kalshi_calibration.py` is run by hand from a workstation and goes through `Api` with `[research.kalshi_calibration]`, without the shared budget, which counts the cluster's IP.

To check a run: `kubectl -n freqtrade logs <pod> | grep -c 'HTTP 429'`.

## Kalshi

API (doc, https://docs.kalshi.com/getting_started/api_environments.md):
- REST `https://api.elections.kalshi.com/trade-api/v2` (also `external-api.kalshi.com`).
- Websocket `wss://api.elections.kalshi.com/trade-api/ws/v2`. The handshake needs an API key even for public channels (doc, websocket-connection.md). P1 polls REST until a key exists (sheet row tr-50).
- Market data without a key: `/series/{t}`, `/events/{t}`, `/markets`, `/markets/{t}/orderbook`, `/markets/trades`, `/series/{s}/markets/{t}/candlesticks` (doc, quick_start_market_data.md).
- `/markets` pages by cursor, up to 1000 a page, and filters by `status`, `series_ticker`, `event_ticker`, `mve_filter` and the `min/max_*_ts` fields. Prices are dollar strings (`yes_bid_dollars`) and sizes fixed-point strings (`volume_fp`); `liquidity_dollars` was removed 2026-10-01 (doc, changelog).
- Candlesticks: `period_interval` 1, 60 or 1440 minutes, with bid, ask and trade OHLC, volume and open interest.

Rate limits: Basic tier 200 read and 100 write tokens a second, 10 tokens a request (see Rate limits). We got 429s on an unpaced count of `/markets`; a 0.5 s gap between pages was clean. Websocket: 200 connections up to Expert.

Fees (secondary, quoting the fee schedule PDF effective 2026-07-07; the PDF itself returned 429):
- Taker: `ceil(M * 0.07 * C * P * (1 - P))`, M = 1 by default. Example: 100 contracts at 50c costs $1.75; at 10c or 90c, $0.63.
- Maker: `ceil(M * 0.0175 * C * P * (1 - P))`, only on the series in the schedule's non-standard table; maker fees went live 2026-08-20 (doc, changelog).
- Rounding: the model fee is ceiled to 6 decimals and the overpayment is rebated per order (doc, fee_rounding.md). The backtester takes the fill's `fee_cost` as the authority where it has one, and the fee model is tested against the PDF's own examples once fetched.
- No deposit, settlement or membership fee (doc, help.kalshi.com fees article, 2026-08-24).

Demo (doc, https://docs.kalshi.com/getting_started/demo_env, rechecked 2026-11-06): REST `https://external-api.demo.kalshi.co/trade-api/v2` (recommended) or `https://demo-api.kalshi.co/trade-api/v2`, websocket `wss://demo-api.kalshi.co/trade-api/ws/v2`, account at https://demo.kalshi.co/ with mock funds and separate keys. Demo prices do not track the real book, so P7 uses demo only to prove the order path, never to score a strategy.

History (doc, historical_data.md): `GET /historical/cutoff` gives the timestamps where records move from the live endpoints to `/historical/markets`, `/historical/markets/{t}/candlesticks` and `/historical/trades`. The live endpoints stop returning them after the move, so a backfill reads both sides of the cutoff. No earliest date is stated and there is no bulk download; P4 found it reaches 2021-06 (see History).

Terms (secondary): API use is under the Developer Agreement, which we have not read. Own-account automated trading looks allowed; redistributing market data does not. trader.kblab.me is sign-in only, which keeps it out of redistribution, and P8 re-checks against the agreement.

Legality (secondary and snippet, 2026-11-05; https://www.dlapiper.com/en/insights/publications/2026/09/legal-status-at-odds-tracking-developments-in-prediction-markets-and-sports-betting, https://www.cbssports.com/prediction/news/prediction-market-legal-states/, https://defirate.com/prediction-markets/legal/):
- Kalshi is a CFTC DCM. The fight is over sports contracts, and the federal circuits are split.
- Against Kalshi: Ninth Circuit (Nevada) 2026-08-28, rehearing denied 2026-11-04. Sixth Circuit (Ohio, Tennessee) 2026-09-25. Ohio fined Kalshi $5M.
- For Kalshi: Third Circuit (New Jersey) 2026-04; New Jersey's cert petition is No. 26-299, with Kalshi's response due 2026-12-07.
- Pending: Fourth Circuit (Maryland), argued 2026-05-07. Massachusetts SJC, argued 2026-05. Second Circuit (New York, Connecticut) denied Kalshi injunctions pending appeal 2026-11-03. Illinois, Iowa, Utah (criminal information against the founders), Arkansas, Missouri, Louisiana, Michigan, Washington and California (tribal lands) are in court in some form.
- CFTC: a proposed rule amending Rule 40.11 was published 2026-06-10. It would allow scores, point spreads, season stats and advancement contracts, and bar injury, officiating and in-game action contracts. No final rule; one source puts comments due 2026-11-18, another says they closed 2026-07-27.
- Congress: S.4160 and H.R. 9856 would bar sports contracts on CFTC venues. Both are in committee.
- What this means for us: data is legal everywhere. Which contracts a live stage may trade depends on the state of residence, which is decide row tr-51.

## Polymarket US

The CFTC-designated exchange (QCX LLC, amended order 2025-11-25; snippet), separate from global, with its own keys.

- Public data without a key: `https://gateway.polymarket.us`, including `GET /v1/markets`, book, BBO, settlement and `GET /v1/price-history` (doc, https://docs.polymarket.us/api-reference/introduction). Price history is built from quotes (best ask, 1 - best bid) rather than trades. It is 1 min for the last 1h/6h, 5 min for 1D and 3 h beyond.
- Limits: 20 requests/s per IP on the public gateway with a 30 s cache. The authenticated Retail API is 25 requests/s per IP (doc, rate-limits page, 2026-11-06).
- Fees from 2026-10-28 (doc, https://docs.polymarket.us/fees): `Theta * C * p * (1 - p)`, taker Theta 0.0695 (table tennis 0.10), banker's rounding to the cent; maker rebates and taker rebates by 30-day volume tier. Third-party fee guides quote old rates.
- Retention (doc, changelog 2026-07-13): market data 45 days, orders 90 days, executions 7 days. The venue cannot be the history source, so the recorder is.
- Sandbox: Preprod exists for the institutional API only, by contact (doc, environments.md). No retail demo, so Polymarket US is never in P7.
- States: Nevada blocked by court order; Massachusetts, Maryland and others contested (snippet, aggregator sites). No official state list found.
- Adapter (`lab/markets/polymarket.py`, `polymarket_us`; measured 2026-11-06): `/v1/markets?active=true&closed=false` pages by offset, 500 a page, about 150 requests a sweep. The list carries `bestBidQuote` and `bestAskQuote`, which match `/bbo`, so no per-market call. A market is one yes outcome, the long side; `market_id` is the slug, the key the book, BBO and settlement routes take. Resolutions read `closed=true&orderBy=updated_at&orderDirection=desc` (snake_case; the camelCase field is ignored), stop at the lookback, and take the long side's price as the payout and `updatedAt` as the settled time, since `endDate` is the scheduled end.

## Polymarket global

Read-only prices. A US resident must not trade it: the US is close-only on both site and API, and VPN use is barred (doc, https://polymarket.com/tos effective 2026-08-11; https://docs.polymarket.com/api-reference/geoblock).

- Hosts (doc): Gamma `https://gamma-api.polymarket.com`, CLOB `https://clob.polymarket.com`, data `https://data-api.polymarket.com`, websocket `wss://ws-subscriptions-clob.polymarket.com/ws/market`.
- Price history: `GET /v2/prices-history` on the data host since 2026-09-04. The CLOB v1 route retired 2026-11-02, so older tutorials are broken (doc, changelog). CLOB V2 went live 2026-04-28 with pUSD collateral.
- Limits (doc, per 10 s, Cloudflare queues rather than rejects): Gamma `/markets` 300, `/events` 500; CLOB `/book` 1,500; data `/v2/prices-history` 200, `/v2/trades` 300.
- Gamma `/markets` pages by `offset` and refuses an offset past 2,100 with a 422. `/markets/keyset` pages by `after_cursor` (100 a page) and reaches every market: 2,343 pages and 234,259 markets in 575 s on 2026-11-06, too slow for a 5-min job. The events keyset is slower.
- Adapter (`lab/markets/polymarket.py`, `polymarket`): one yes outcome per market, the first outcome token, keyed by `conditionId`, with the event as `event_id`, the series slug as `series_id` and `groupItemTitle` as the subtitle. Resolutions read `closed=true&order=closedTime&ascending=false` and keep markets with `umaResolutionStatus` resolved; the first entry of `outcomePrices` is the payout.
- Fees (doc, https://docs.polymarket.com/trading/fees): taker `C * feeRate * p * (1 - p)`, makers 0. The rate depends on category: crypto 0.07, sports and economics 0.05, politics and finance 0.04, geopolitics 0. Each market's `feeSchedule` object is the authority.
- Terms: market-data distributors may not use site data without a written agreement. Our use is private research, and the adapter reads only the documented API.

## PredictIt

- One endpoint, `https://www.predictit.org/api/marketdata/all/` (and `/markets/{id}`), no key, with best buy and sell prices per contract and the last trade (doc, fetched). No depth, trades, history, websocket or trading API.
- Rate limit not published; secondary sources range from 1 a second to 1 a minute, and the data refreshes about once a minute. Poll once a minute.
- Fees: 10% of profit on winning trades; the 5% withdrawal fee was dropped 2026-09-23 (secondary, nextpredict.io).
- Status (doc, CFTC Letter 25-20, 2025-07-14): runs under no-action relief through a non-profit. The trader cap per contract is gone and the position limit, formerly $850, is now the FECA contribution limit (about $3,500). Political contracts only.
- History: none from the venue. The recorder's snapshots are the history.
- Adapter (`lab/markets/predictit.py`): one GET of the snapshot, open markets only. Each contract is a market, keyed by contract id, with its PredictIt market as `event_id`; `bestBuyYesCost` is our ask and `bestSellYesCost` our bid. Dates are Eastern without an offset. No resolutions: a closed market leaves the snapshot and its own page shows only last prices, with no result or settled time, so the adapter yields none rather than guess.

## Manifold

Play money (mana); the sweepcash real-money model ended 2025-03-28 (snippet).

- API `https://api.manifold.markets`, no key for reads, 500 requests/min per IP (doc, https://docs.manifold.markets/api). `/v0/markets` and `/v0/bets` page up to 1000; `/v0/market-probs` takes up to 100 ids. No price-history endpoint, so history is rebuilt from bets.
- Websocket `wss://api.manifold.markets/ws`.
- Dumps (doc, https://docs.manifold.markets/data): bets, markets and comments from 2021-12 to 2024-07, non-commercial use. Our use is private research.
- Use: calibration and base-rate studies, and a free paper venue. Its prices are not money prices, so a gap against Kalshi is not an arbitrage.
- Adapter (`lab/markets/manifold.py`, measured 2026-11-06): binary markets only, from `/v0/search-markets?contractType=BINARY`; multiple-choice is not read yet. Open markets page newest first by the `beforeTime` cursor, 26,299 in 27 pages and 14.5 s. Manifold is an AMM with no book, so the probability is stored as `last` and bid and ask stay empty. Resolutions sort by resolve-date, which pages by offset only, up to 1,000; YES pays 1, NO 0, MKT its resolution probability, and CANCEL is void.

## Sportsbook odds: The Odds API

Chosen over OpticOdds, SportsDataIO and Sportradar, whose prices are unpublished or need a sales call and whose storage terms we could not read (secondary).

- Pricing (doc, https://the-odds-api.com/): free 500 credits/month, $30 for 20K, $59 for 100K, $119 for 5M, $249 for 15M. A call costs 1 credit per market per region; the sports and events lists are free.
- Coverage (doc): US, UK, EU and AU books, plus a `us_ex` group with Kalshi, Polymarket, Polymarket US, Novig and ProphetX.
- Cadence (doc, update-intervals.html): 60 s pre-match and 40 s in-play for main markets, 5 min for futures.
- History: paid plans, 10 credits per region per market, from 2020-06-06, 5-min snapshots from 2022-09.
- Terms (doc, terms-and-conditions.html): storing data indefinitely and using it in analytics is allowed; reselling or re-serving it as a feed is not.
- Sizing: an odds call is 1 credit per sport, market and region, and the adapter's resolutions read `/scores?daysFrom=N`, 2 credits per sport. One sport, h2h, one region every 5 min is 3 credits a run, 864 a day, about 26,000 a month: the $30 plan's 20K covers it hourly (72 a day) but not every 5 min. The free tier runs it every 6 hours (home-config `trader-markets-recorder-odds-api`, since 2026-11-12); a paid plan and a faster cadence are a human money decision.
- Adapter (`lab/markets/odds.py`, `odds_api`; fixtures follow the v4 docs, not yet a live response): one game is one market keyed by the API's event id, with the sport key as `series_id`, `<away> at <home>` as the title and the commence time as `close_time`. Each book's price on each side is an outcome, `<book>:<team>`, priced as a buy: decimal price d goes in `ask` as 1/d, `bid` stays empty, and the book's margin stays in. A finished game resolves to the winning team's name, or `Draw` on a tied score. The key goes in the query string, so an HTTP error is re-raised without the URL.
- We never place a bet at a sportsbook. FanDuel and DraftKings terms bar automated betting (secondary; the pages returned 403), and Betfair lists the US as a prohibited territory (snippet, clause 4.1.6).

## What this changes in the plan

- Target load: about 180k open markets across venues, up from the 10k the plan assumed, almost all on Kalshi. A 5-min sweep is still cheap: about 180 Kalshi list requests at 1000 a page, well inside the 200 reads/s Basic tier. The naive row count is about 52M a day, so change-only parquet in MinIO is the right store and Postgres holds only market metadata.
- The websocket for watched markets needs a Kalshi API key, so P1 polls REST until tr-50 is done.
- Polymarket US keeps only 45 days of market data, so P4's backfill for it covers 45 days and the recorder keeps everything after.
- Only Kalshi has a retail demo, so P7's order path is Kalshi only, as planned.

## Not verified

- The Kalshi fee schedule PDF and Developer Agreement text: both returned 429.
- Whether the Kalshi rate table includes the 2026-09-24 uplift for upper tiers.
- PredictIt's own terms, rate limit and state list (the site renders in JS).
- The Polymarket US bot terms, and whether its markets websocket needs a key.
- How far back Polymarket `/v2/prices-history` reaches: P4 read 2026 markets only.
- Court status past each source's date. Everything under Legality comes from trackers and law-firm alerts; no docket was read.
