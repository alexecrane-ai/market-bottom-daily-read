# Market Bottom Daily Read

A twice-daily read on market-bottom signals, summed up in a 8-point **bottom checklist**:

1. **VIX**: level, moves, an alert when it drops fast to below 25, and VIX vs 3-month VIX (panic when above 1).
2. **Breadth**: % of S&P 500 stocks up on the day (flags 90% up/down days) and % above their 50-day average.
3. **Junk bond spreads**: the ICE BofA US High Yield spread, rated CALM, NORMAL or VOLATILE.
4. **US dollar index**: at highs, firm, falling, or reversing.
5. **Yield curve**: 10y minus 2y, flattening, stable, or steepening, plus whether 10-year yields are stabilizing.
6. **Selling pressure**: how many of the 11 sectors fell, and failed rallies or reversals up in the Nasdaq and S&P 500.

**Report:** https://alexecrane-ai.github.io/market-bottom-daily-read/

Runs automatically at 10:00am and 3:00pm New York time, Monday to Friday (GitHub Actions).
To refresh on demand, open **Actions → Market Bottom Daily Read → Run workflow** (also works in the GitHub mobile app).

Run it locally: `python3 market_tracker.py` (needs pandas). Thresholds are at the top of the script.

Under the checklist, **Today's macro highlights** shows the latest official US data from FRED (CPI, PCE, GDP, jobs, jobless claims, the Fed rate). If the repository has an `ANTHROPIC_API_KEY` secret (Settings → Secrets and variables → Actions), Claude also searches the day's financial news and economic calendar and writes the highlights, a table of recent releases (actual vs expected vs prior, and what it means) and the week's upcoming events. Each run is one Claude call with web search.

Further down, a **trend ranges** table (like a buy/sell levels sheet) covers 15 index ETFs, the 11 sector ETFs, and the largest stocks in each of those 11 sectors (edit the lists in `TREND_GROUPS`). It shows a buy and sell level from a short-term trading range, a bullish/bearish/neutral trend signal, when that signal began, and how the price has done since.

Not investment advice.
