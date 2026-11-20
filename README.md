# Market Bottom Daily Read

A twice-daily read on three market-bottom signals:

1. **VIX**: level, daily, weekly and monthly moves, and an alert when it drops fast to below 25.
2. **Breadth**: the share of S&P 500 stocks moving up together; flags 90% up and 90% down days.
3. **Junk bond spreads**: the ICE BofA US High Yield spread, rated CALM, NORMAL or VOLATILE.

**Report:** https://alexecrane-ai.github.io/market-bottom-daily-read/

Runs automatically at 10:00am and 3:00pm New York time, Monday to Friday (GitHub Actions).
To refresh on demand, open **Actions → Market Bottom Daily Read → Run workflow** (also works in the GitHub mobile app).

Run it locally: `python3 market_tracker.py` (needs pandas). Thresholds are at the top of the script.

Not investment advice.
