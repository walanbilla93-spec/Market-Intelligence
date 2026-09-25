# Source register

| Source | Data used | Authentication / quota | Expected freshness | Trust and licensing treatment |
|---|---|---|---|---|
| BLS official calendar (`bls.gov/schedule/news_release/bls.ics`) | CPI, PPI, Employment Situation/NFP schedule | None; poll every 15 minutes or slower | Schedule revisions normally appear ahead of release | Official U.S. government source. Retain retrieval time, URL, hash, and all revisions. |
| BEA official schedule (`bea.gov/news/schedule`) | Personal Income and Outlays/PCE schedule | None; poll every 15 minutes or slower | Page schedule; HTML can change | Official U.S. government source. Parser failure is an outage, not “no events.” |
| Federal Reserve FOMC calendar | Meeting dates | None; poll every 15 minutes or slower | Calendar revisions | Official source. Meeting dates are verified; 14:00 decision and 14:30 press-conference times are explicitly marked assumptions and should be reviewed prospectively. |
| Federal Reserve RSS | Speeches and monetary-policy releases | None; conservative polling | Publisher-dependent | Official feed. Publication time and local availability time are separate. Only locally observed availability is causal. |
| Bybit V5 public tickers | Funding, OI, mark/index basis, BTC/ETH return and cross-sectional breadth | No key; exchange rate limits apply and can change | Near real-time at retrieval | Official exchange API. A failed fetch is `NOT_AVAILABLE`; it is never converted to zero. No trading credentials are accepted. |
| FRED API (optional) | 10-year Treasury, broad dollar index, S&P 500, VIX | User-supplied FRED key; consult current FRED terms/limits | Source-native, often daily | Official Federal Reserve Bank of St. Louis API. Stored as slow context, not an executable quote. |
| Configured RSS feeds | Exchange incidents, maintenance, listings, upgrades, verified news | Feed-specific | Feed-specific | Only explicitly configured trusted publishers receive verified-feed status. URL/hash/first-seen time are mandatory. |
| Gemini API (optional) | Structured observer summary | Separate API key/quota; Gemini Pro web subscription is not API quota | Asynchronous and deadline-bounded | Non-authoritative analyst output only. Circuit breaker and bounded queue; never a trading dependency. |

Token-unlock calendars and broad news aggregators are deliberately absent by default. Add an adapter only after verifying redistribution rights, quota, timestamp semantics, and whether historical records expose the collector’s true first-seen time. Market-wide liquidation shock tags likewise require a licensed or official stream with receipt timestamps; the service does not fabricate them from incomplete snapshots.

