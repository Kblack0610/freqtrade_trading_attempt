# Reading list

Books on systematic trading and sizing, ranked for this lab: a Python crypto bot with walk-forward tests, plus event-contract and sportsbook paper trading. The list favours books built on evidence and testing over books about mindset or chart patterns. Researched 2026-11-17; the facts behind each row are in the table at the end, with how sure we are of each.

## Owned

| Book | Bought | Notes |
| --- | --- | --- |
| Robert Carver, *Systematic Trading* (2015) | 2026-11-17 | |
| Ed Thorp, *A Man for All Markets* (2017) | 2026-11-17 | |

Add a dated line under Notes, or a section below, when a chapter changes how the lab works, with a link to the PR or avenue it touched.

## Ranked

1. **Robert Carver, *Systematic Trading* (Harriman House, 2015).** The top pick. One framework from forecast to position: forecasts, combining them, volatility targeting, position sizing, portfolios, and when backtesting and optimisation help or hurt. Asset-class agnostic, so it carries over to crypto. Carver ran a multi-billion fixed income portfolio at AHL (Man Group) until 2013. No code; his own reading order puts it last as his most advanced book. For this lab: the sizing and forecast-scaling chapters are what `lab/vol_target.py` and the stage stakes would follow.
2. **Ernest Chan, *Quantitative Trading*, 2nd edition (Wiley, 2021).** For an independent trader starting a quant operation: finding ideas, backtesting, execution, money management. Python and R code. The 2nd edition adds an ML method for tuning parameters across regimes. Skip his *Algorithmic Trading* (2013): MATLAB code.
3. **David Aronson, *Evidence-Based Technical Analysis* (Wiley, 2006).** The book on why indicator rules, which is what most Freqtrade strategies are, fool you: data-mining bias and how to correct for it. He tested 6,402 rules on the S&P 500 and none survived the correction. It is the argument behind our trial register.
4. **Marcos Lopez de Prado, *Advances in Financial Machine Learning* (Wiley, 2018).** Not introductory, and the author calls it a manual for teams. Read only the chapters on backtest overfitting, purged and embargoed cross-validation, and the deflated Sharpe; the free papers in [resources.md](resources.md) cover the same ground.
5. **Ed Thorp, *A Man for All Markets* (2017), with his 2006 Kelly paper.** The memoir of the man who beat blackjack and ran one of the first quant funds. The method is in the free paper ([resources.md](resources.md), Sizing): half Kelly or less, because the edge estimate is too high more often than not.

For betting and event contracts:
1. **Joseph Buchdahl, *Squares & Sharps, Suckers & Sharks* (High Stakes, 2016, 352 pp).** The first betting book to buy. The statistics of market efficiency, testing a record against the closing line, and how much luck a record hides: the question the `gap` and `tt_totals` paper tests have to answer. His *Monte Carlo or Bust* covers simulating variance to judge whether a record shows skill.
2. **Elihu Feustel, *Beyond the Odds: Efficient Market Theory and Tools of Warfare for the Modern Sports Bettor* (self-published, July 2024, about 164 pp).** Feustel was a Pinnacle oddsmaker in 2004-06 and is a full-time professional bettor. Its unique part is the "Answer Key": pricing alternate lines, halves, teasers and props from the market's main line, with push-rate charts, no-vig odds, the closing line, fractional Kelly and log loss, plus Excel files. That is the job `tt_totals` does on alternate totals lines. The second half is about staying unlimited at US sportsbooks, which does not apply to Kalshi or a paper league. Spreadsheet-level maths. Maurice Berk, a professional exchange bettor, calls it the clearest market-beating model he has seen in print. Buy after Buchdahl.
3. **Ed Miller and Matthew Davidow, *The Logic of Sports Betting* (self-published, 2019, 237 pp).** The accessible primer on how betting markets set and move lines and why the closing line matters. US-odds centred, with no model building. Optional once Buchdahl is read.

Easier way in:
- **Robert Carver, *Leveraged Trading* (Harriman House, 2019).** Starts with one instrument and builds up; written for small accounts. Read this first if *Systematic Trading* feels heavy.

## Lower for this lab

| Book | Why not higher |
| --- | --- |
| Andreas Clenow, *Trading Evolved* (2019) | Python, but built on Zipline, which is hard to set up since Quantopian closed; equities and futures |
| Andreas Clenow, *Following the Trend* (2nd ed. 2023) | Futures trend following; retail accounts are too small for its portfolio |
| Kevin Davey, *Building Winning Algorithmic Trading Systems* (2014) | Introductory, EasyLanguage and Excel, futures |
| Stefan Jansen, *Machine Learning for Algorithmic Trading* (2nd ed. 2020) | A large ML survey; reviewers call it repetitive |
| Van Tharp, *Trade Your Way to Financial Freedom* (2nd ed. 2006) | Useful for R-multiples and expectancy; thin on testing |
| Larry Harris, *Trading and Exchanges* (2003) | Market microstructure reference: how orders, dealers and exchanges work. Dense and dated, but the one to open for a microstructure question |
| Ralph Vince, optimal f | Sizing from the past's worst loss; overfits |
| Robert Carver, *Advanced Futures Trading Strategies* (2023) | 30 plus futures strategies; for after *Systematic Trading*. Each one mapped to the lab: [carver-afts.md](../carver-afts.md) |

## Evidence

Tier: primary means the publisher's or author's page was read; snippet means a search summary or review only.

| Claim | Source | Tier |
| --- | --- | --- |
| Carver at AHL until 2013, ran its fixed income portfolio; visiting lecturer at Queen Mary | https://www.systematicmoney.org/bio | primary |
| Carver's reading order ends with *Systematic Trading*, "relatively advanced" | https://www.systematicmoney.org/which-book-should-i-buy | primary |
| *Systematic Trading* parts: Theory, Toolbox, Framework, Practice | https://www.systematicmoney.org/systematic-trading-information | primary |
| *Leveraged Trading*, 29 Oct 2019, starter system, small accounts | https://www.harriman-house.com/authors/robert-carver/leveraged-trading/9780857197214 | primary |
| Chan 2nd ed. July 2021, Python and R; Chan is PhD physics (Cornell), QTS Capital | https://www.wiley.com/en-us/shop/general-finance-investments/quantitative-trading-how-to-build-your-own-algorithmic-trading-business-2nd-edition-p-9781119800064 | primary |
| Aronson's 6,402 rules, none significant after the correction | https://www.cxoadvisory.com/technical-trading/evidence-based-technical-analysis-applying-the-scientific-method-and-statistical-inference-to-trading-signals-chapter-by-chapter-review/ | snippet |
| *AFML* not introductory, "a research manual for teams" | https://www.tandfonline.com/doi/full/10.1080/14697688.2019.1703030 | snippet |
| *Following the Trend* 2nd ed. Jan 2023 | https://www.wiley.com/en-ca/Following+the+Trend:+Diversified+Managed+Futures+Trading,+2nd+Edition-p-9781119908982 | primary |
| *Trading Evolved* Python on Zipline | https://www.followingthetrend.com/trading-evolved/ | snippet |
| Buchdahl, *Squares & Sharps*, 2016, 352 pp (an earlier search summary said 2021) | https://openlibrary.org/books/OL27703196M/Squares_and_Sharps_Suckers_and_Sharks | snippet |
| *Beyond the Odds* contents: Answer Key for derivative lines, push charts, no-vig odds, closing line, Kelly, log loss, account longevity, Excel files | https://books.google.com/books/about/Beyond_The_Odds_Efficient_Market_Theory.html?id=V6oSEQAAQBAJ | primary |
| Feustel a Pinnacle oddsmaker, former insurance lawyer, professional bettor | https://www.lasvegasadvisor.com/blog/life-is-a-gamble-podcast-elihu-feustel/ | primary |
| *Beyond the Odds* July 2024, Goodreads 4.41 from 19 ratings | https://www.goodreads.com/en/book/show/216124921-beyond-the-odds | primary |
| Berk's review: the clearest market-beating model in print; the second half is account management, of little use to an exchange bettor. Miller and Davidow: an excellent primer with no model building | https://algorithmicsportsbetting.substack.com/p/the-books-i-actually-recommend-as | primary |
| Miller and Davidow, 2019, 237 pp | https://www.goodreads.com/book/show/45485081-the-logic-of-sports-betting | snippet |

Not checked: Reddit r/algotrading consensus (fetch blocked); the *A Man for All Markets* publisher page; whether *Systematic Trading* ships code (pysystemtrade is separate). Carver's next book, *The Art and Science of Trading*, is listed for late 2026.
