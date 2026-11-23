# Privacy

Trader is a private research tool with one operator. It has no public sign-up and no end users.

- **Who signs in:** the operator, through the home Forgejo instance. The site stores no password; it keeps the Forgejo identity the sign-in returns, in a session cookie.
- **What the database holds:** market prices, bot results, paper accounts and research output. It holds no personal data about anyone but the operator's Forgejo username.
- **Third parties:** the lab reads public market data from exchanges and event-contract venues and sends no personal data to them. No analytics or advertising scripts run on the site.
- **Money and keys:** nothing trades real money (`docs/operating-rules.md`). Venue keys are never stored in the repo or the image.
- **Retention and contact:** data stays in the home cluster's Postgres and its nightly backup. Questions go to the operator; see [support](../support.md).

If trader ever gains a second user, this page needs a rewrite first.
