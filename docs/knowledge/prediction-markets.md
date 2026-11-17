# Edge in event contracts and sportsbooks

What the literature says about beating prediction markets and sportsbooks, and what it means for the families in [markets.md](../markets.md). Researched 2026-11-17. The lab's own verdicts stay in [avenues.md](../avenues.md); this page is the outside evidence beside them.

## The short version

- Longshots are overpriced. Buying them loses money on every venue studied; on Kalshi the loss is worse for takers than makers.
- Seeing the bias is not profiting from it. After spread and fees, no price bucket studied on Kalshi pays a taker. This matches our own NO-GO rows for Kalshi taker calibration and longshot taker bias.
- How you group trades can flip the result. On Polymarket, longshots lose 6.3c a dollar weighted by contract and gain 4.1c grouped by event.
- The bias is weak in sports on Polymarket, so sportsbook evidence does not carry straight over.
- Closing line value shows skill within weeks; settled profit takes far longer to say anything.
- Size well under Kelly: the edge estimate is noisy.

## Evidence

Tier: primary means the paper or page was read; snippet means a search summary only (most SSRN pages refuse automated fetches).

| Finding | Source | Tier |
| --- | --- | --- |
| Kalshi, 12,403 events, over 300,000 prices (both sides of each trade): a clear favorite-longshot bias, more pronounced for prices takers accept than makers; contracts under 10c lose over 60% of the money; the average contract returns -20% before fees and -22% after (verified in the paper's text 2026-11-17) | Burgi, Deng, Whelan, "Makers and Takers: The Economics of the Kalshi Prediction Market" (2025), https://papers.ssrn.com/sol3/papers.cfm?abstract_id=5502658, text read at the MPRA copy https://mpra.ub.uni-muenchen.de/126350/1/MPRA_paper_126350.pdf (September 2025; the UCD, SSRN and CEPR pages return 403) | primary |
| 46,168 resolved Kalshi and Polymarket markets: Kalshi's calibration slope rises from 1.02 at 30 days out to 1.22 an hour out; the bias sits in thin markets (1.18) and is absent in the most-traded third (1.02); 0-2c longshots bought at the ask lose 89c a dollar; no bucket pays after spread and taker fees | https://github.com/hrishi-tailor/prediction-market-calibration (one author and unreviewed, but reproducible from public APIs) | primary |
| Polymarket, 588M trades: under-10c contracts lose about 19.3c per dollar in raw flow, 90c plus earn about 0.83c; by contract -6.3c, by event +4.1c; present in crypto and politics, largely absent in sports | Cardozo, Rivero-Wildemauwe, arXiv 2609.12878 (2026-09-11), https://arxiv.org/abs/2609.12878 | primary |
| No general longshot bias on Polymarket over 124M trades | Reichenbach, Walther (2025), SSRN | snippet |
| Correlated trades can manufacture a spurious longshot bias in 5-minute crypto markets | Gruener, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6863546 | snippet |
| Kalshi macro contracts under 30c show a bias; 95 CPI contracts show none | Krause, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7117919 and https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7087538 | snippet |
| The bias comes from misjudged probabilities more than a taste for risk, so it should persist where retail traders dominate | Snowberg, Wolfers, NBER w15923 (2010), https://www.nber.org/papers/w15923 | primary |
| The classic survey of racetrack betting anomalies | Thaler, Ziemba, "Anomalies: Parimutuel Betting Markets", JEP 2(2), 1988, https://pubs.aeaweb.org/doi/abs/10.1257/jep.2.2.161 | snippet |
| How prediction market prices become forecasts | Wolfers, Zitzewitz, "Prediction Markets", JEP 18(2), 2004, https://www.aeaweb.org/articles?id=10.1257/0895330041371321 | primary |
| Beating the margin-free ("fair") Pinnacle closing price is a much more reliable indicator of bettor skill than a betting history, provided the closing line is efficient; in his worked record possibly two-thirds of the profit over 1,214 bets was luck | Buchdahl, "Using the closing line to test your skill in betting" (Pinnacle, 2019-04-04), body read at https://www.sportstradingnetwork.com/article/using-the-closing-line-to-test-your-skill-in-betting/ (the Pinnacle page renders no body) | primary |
| Free historical football odds with Pinnacle closing prices from 2012/13; its notice says Pinnacle's public API has been unreliable since 2025-07-23 | https://www.football-data.co.uk/data.php | primary |
| Expected log growth is extremely sensitive to errors in the mean estimate, so wagers should be reduced; even half Kelly can lose heavily in the paper's example | MacLean, Thorp, Ziemba (2010), PDF read at https://www.stat.berkeley.edu/~aldous/157/Papers/Good_Bad_Kelly.pdf | primary |

## What this means for the lab

| Family or practice | What the evidence says | Where it stands |
| --- | --- | --- |
| `longshot` as a taker | Loses after costs in every study that counts fees | NO-GO in [avenues.md](../avenues.md); still paper traded as a forward test |
| Longshot or fade as a maker | The Kalshi maker result is the opening the literature leaves | Blocked in [avenues.md](../avenues.md) "Kalshi maker orders": needs a maker fill model |
| Thin markets close to settlement | Where the miscalibration is largest, and where depth is smallest | The `hours` window and `depth_share` already bound it |
| `calibration` | On Polymarket the sign of the longshot result depends on grouping by contract or by event | Candidate: score by event as well as by contract |
| `gap` against sportsbooks | The bias is weak in sports, so a sports gap has to come from the devigged sharp price | Fair price is the median devigged book |
| Skill measurement | CLV needs far fewer bets than settled profit to show skill, if the close is efficient (Buchdahl gives no time in weeks) | `close_mid` is recorded per fill; reading CLV per account is a candidate report |
| Sizing | Fractional Kelly at most, and only after a verified edge | Fixed $10 stakes per bet (`lab/markets.toml`) |

## Not checked

- The Kalshi fee schedule PDF (429); [markets.md](../markets.md) keeps the checked formula.
- The Burgi, Deng, Whelan maker and taker return levels: the abstract and body give the -20% and -22% averages, and the maker and taker averages differ between versions in search summaries (UCD, CESifo), so none is quoted here.
- Pinnacle's other CLV articles; only the one above was read. Whether Thaler and Ziemba's page says what the search summary says (AEA returns 403; citation and DOI confirmed by search).
- Whether The Odds API carries Kalshi or Polymarket prices.
