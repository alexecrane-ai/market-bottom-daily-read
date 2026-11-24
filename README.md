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

GitHub's own timer often fires hours late or not at all, so a small job on the Mac backs it up.
It checks every 15 minutes (and when the Mac wakes) and asks GitHub to run the report once per
window if it hasn't been saved yet. To install it once (needs the `gh` CLI, logged in):

```bash
bash local-trigger/install.sh
```

Remove it with `bash local-trigger/install.sh --uninstall`. Log: `~/Library/Logs/market-read-trigger.log`.
A late run still counts: the afternoon window runs until midnight New York time, so a delayed
run still saves the day's close.
To refresh on demand, open **Actions → Market Bottom Daily Read → Run workflow** (also works in the GitHub mobile app).

Run it locally: `python3 market_tracker.py` (needs pandas). Thresholds are at the top of the script.

Not investment advice.
