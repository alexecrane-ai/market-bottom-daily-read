#!/usr/bin/env python3
"""
Daily Market Tracker
--------------------
1. VIX      - level, daily/weekly/monthly moves, and a "FAST DROP BELOW 25" alert.
2. Breadth  - % of S&P 500 stocks up on the day; flags 90% upside (and downside) days.
3. Junk     - ICE BofA US High Yield OAS: is it CALM, NORMAL or VOLATILE?
4. Dollar   - US Dollar Index: still making highs, or reversing?
5. Rates    - 10y-2y yield curve (flattening = stress) and whether 10y yields are stabilizing.
6. Pressure - how many of the 11 S&P sectors fell, and failed rallies / reversals up
             in the Nasdaq and S&P 500.
Plus a "bottom checklist" that counts how many signals point to a bottom.

Writes:  report.html   (open in any browser)
         history.csv   (one row per trading day, appended each run)

Data: Yahoo Finance (VIX + S&P 500 member prices), FRED (high-yield spread).
Needs only Python 3 + pandas. Run:  python3 market_tracker.py
"""
import csv
import io
import json
import math
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

STAMP = datetime.now().strftime('%Y%m%d%H%M%S')  # build id, checked by the page
FRESH_JS = '''<script>
/* Phones keep an old copy of this page. On every open, check stamp.txt (never cached)
   and reload if a newer report has been published. */
(function () {
  var S = "__STAMP__";
  function chk() {
    fetch("stamp.txt?t=" + Date.now(), {cache: "no-store"})
      .then(function (r) { return r.ok ? r.text() : ""; })
      .then(function (t) {
        t = t.trim();
        if (t && t !== S && location.search.indexOf("v=" + t) < 0)
          location.replace(location.pathname + "?v=" + encodeURIComponent(t));
      }).catch(function () {});
  }
  chk();
  document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") chk(); });
  window.addEventListener("pageshow", function (e) { if (e.persisted) chk(); });
})();
</script>'''.replace('__STAMP__', STAMP)

import pandas as pd
import macro_brief

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "index.html")
REFRESH_URL = ("https://github.com/alexecrane-ai/market-bottom-daily-read"
               "/actions/workflows/daily.yml")
HISTORY = os.path.join(HERE, "history.csv")
UA = {"User-Agent": "Mozilla/5.0 (market-tracker)"}

# ---- Thresholds (edit to taste) ---------------------------------------------
VIX_LINE = 25.0            # the level you care about
VIX_FAST_DROP_PCT = 20.0   # % fall from the recent high that counts as "fast"
VIX_LOOKBACK = 10          # trading days to look back for that recent high
BREADTH_THRUST = 90.0      # % of stocks up (or down) to flag a 90% day
HY_CALM_PCTILE = 50        # spread volatility below this 1y percentile = calm
HY_VOLATILE_PCTILE = 80    # at/above this percentile = volatile
HY_WIDEN_20D_BPS = 50      # spreads widening this much in 20 days = volatile
HY_WIDEN_5D_BPS = 30       # ... or this much in 5 days = volatile
DXY_REVERSAL_PCT = 1.5     # dollar this far off a recent 60-day high (and under 20d avg) = reversing
CURVE_MOVE_BPS = 10        # 10y-2y change over 20 days that counts as flattening/steepening
RATES_STABLE_BPS = 25      # 10y yield moving less than this over 20 days = stabilizing
RALLY_PCT = 1.0            # intraday move vs prior close that counts as a big rally/drop
WASHOUT_PCT = 20           # under this % of stocks above their 50-day average = washed out
# Trend ranges, calibrated to a published buy/sell-level sheet (Nov 24, 2026 closes):
TREND_FAST, TREND_SLOW = 10, 30   # EMA crossover sets bullish/bearish
RANGE_EMA = 5              # trade range is anchored on this EMA ...
RANGE_ATR = 10             # ... and scaled by this many days' average daily range (ATR)
RANGE_BUY_MULT = 1.71      # buy level = EMA - this x ATR
RANGE_SELL_MULT = 1.04     # sell level = EMA + this x ATR
RANGE_NEAR = 0.25          # within this share of the range from an end = "near" that level


# ---- Data fetching -----------------------------------------------------------
def http_get(url, timeout=30, ua=None, tries=3):
    # FRED stalls on browser-style user agents; Yahoo wants one.
    headers = {"User-Agent": ua} if ua else UA
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def yahoo_daily(symbol, rng="1y"):
    """Daily close + volume from Yahoo's chart endpoint, indexed by date."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.request.quote(symbol)}?range={rng}&interval=1d")
    data = json.loads(http_get(url))["chart"]["result"][0]
    ts = data.get("timestamp") or []
    q = data["indicators"]["quote"][0]
    tz = data["meta"].get("exchangeTimezoneName", "America/New_York")
    idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert(tz).normalize().tz_localize(None)
    df = pd.DataFrame({"close": q.get("close"), "volume": q.get("volume"),
                       "high": q.get("high"), "low": q.get("low")}, index=idx)
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    # Yahoo's daily bars often lag a session; the meta block has the latest close.
    meta = data["meta"]
    if meta.get("regularMarketTime") and meta.get("regularMarketPrice") is not None:
        mday = (pd.Timestamp(meta["regularMarketTime"], unit="s", tz="UTC")
                .tz_convert(tz).normalize().tz_localize(None))
        if df.empty or mday > df.index[-1]:
            df.loc[mday] = [meta["regularMarketPrice"], meta.get("regularMarketVolume"),
                            meta.get("regularMarketDayHigh"), meta.get("regularMarketDayLow")]
    return df


def fred_series(series_id):
    start = (datetime.now() - pd.Timedelta(days=900)).strftime("%Y-%m-%d")
    text = http_get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}",
                    ua="curl/8.7.1")
    df = pd.read_csv(io.StringIO(text))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna().set_index("date")["value"]


def sp500_tickers():
    text = http_get("https://raw.githubusercontent.com/datasets/"
                    "s-and-p-500-companies/main/data/constituents.csv")
    rows = list(csv.DictReader(io.StringIO(text)))
    return [r["Symbol"].replace(".", "-") for r in rows]


# ---- 1. VIX ------------------------------------------------------------------
def analyze_vix():
    try:
        s = yahoo_daily("^VIX", "1y")["close"]
        source = "Yahoo Finance (^VIX)"
    except Exception:
        s = fred_series("VIXCLS")
        s = s[s.index >= s.index[-1] - pd.Timedelta(days=370)]
        source = "FRED (VIXCLS)"

    last, prev = s.iloc[-1], s.iloc[-2]
    def chg(n):
        if len(s) <= n:
            return None, None
        base = s.iloc[-1 - n]
        return last - base, (last / base - 1) * 100

    def fast_drop_on(i):
        w = s.iloc[max(0, i - VIX_LOOKBACK):i + 1]
        v = s.iloc[i]
        return (v < VIX_LINE and (w.iloc[:-1] >= VIX_LINE).any()
                and (1 - v / w.max()) * 100 >= VIX_FAST_DROP_PCT)
    recent_fast_drops = [s.index[i] for i in range(len(s) - 10, len(s)) if fast_drop_on(i)]

    # Term structure: VIX vs 3-month VIX. Above 1.0 (backwardation) = panic.
    try:
        v3 = yahoo_daily("^VIX3M", "1y")["close"]
        ratio = (s / v3).dropna()
        ts_ratio = ratio.iloc[-1]
        ts_recent_max = ratio.iloc[-20:].max()
    except Exception:
        ts_ratio = ts_recent_max = None

    window = s.iloc[-(VIX_LOOKBACK + 1):]
    recent_high = window.max()
    recent_high_date = window.idxmax()
    drop_from_high = (last / recent_high - 1) * 100

    crossed_today = last < VIX_LINE <= prev
    was_above_recently = (window.iloc[:-1] >= VIX_LINE).any()
    fast_drop = (last < VIX_LINE and was_above_recently
                 and -drop_from_high >= VIX_FAST_DROP_PCT)

    # Most recent day VIX closed below the line after being above it
    cross_date = None
    below = s < VIX_LINE
    for i in range(len(s) - 1, 0, -1):
        if below.iloc[i] and not below.iloc[i - 1]:
            cross_date = s.index[i]
            break

    if last < 15:   regime = "Calm"
    elif last < 20: regime = "Normal"
    elif last < 25: regime = "Elevated"
    elif last < 30: regime = "High"
    else:           regime = "Fear"

    if fast_drop:
        alert = ("FAST DROP BELOW 25", "good",
                 f"VIX fell {abs(drop_from_high):.0f}% from {recent_high:.2f} "
                 f"({recent_high_date:%b %d}) to {last:.2f}.")
    elif crossed_today:
        alert = ("CROSSED BELOW 25 TODAY", "good",
                 f"Closed {last:.2f} after {prev:.2f} yesterday.")
    elif last >= VIX_LINE:
        alert = ("ABOVE 25", "bad", f"VIX is {last - VIX_LINE:.2f} pts above 25.")
    else:
        alert = ("BELOW 25", "neutral", "No fast drop in the lookback window.")

    return {
        "source": source, "date": s.index[-1], "last": last, "regime": regime,
        "d1": chg(1), "d5": chg(5), "d20": chg(20),
        "recent_high": recent_high, "recent_high_date": recent_high_date,
        "drop_from_high": drop_from_high, "fast_drop": fast_drop,
        "crossed_today": crossed_today, "cross_date": cross_date,
        "alert": alert, "series": s.iloc[-120:],
        "recent_fast_drops": recent_fast_drops,
        "ts_ratio": ts_ratio, "ts_recent_max": ts_recent_max,
    }


# ---- 2. Breadth --------------------------------------------------------------
def analyze_breadth():
    tickers = sp500_tickers()

    def fetch(t):
        try:
            return t, yahoo_daily(t, "6mo")
        except Exception:
            return t, None

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(fetch, tickers))

    closes = pd.DataFrame({t: d["close"] for t, d in results if d is not None})
    vols = pd.DataFrame({t: d["volume"] for t, d in results if d is not None})
    got = closes.shape[1]

    rets = closes.pct_change(fill_method=None)
    valid = rets.notna()
    up = (rets > 0) & valid
    down = (rets < 0) & valid
    n = valid.sum(axis=1)

    daily = pd.DataFrame({
        "pct_up": up.sum(axis=1) / n * 100,
        "pct_down": down.sum(axis=1) / n * 100,
        "up_vol_pct": (vols.where(up).sum(axis=1)
                       / vols.where(valid).sum(axis=1)) * 100,
        "n": n,
    })
    # Keep only days where most of the index reported (drops partial/holiday rows)
    daily = daily[daily["n"] >= got * 0.9].dropna()

    ma50 = closes.rolling(50, min_periods=50).mean()
    has_ma = ma50.notna() & closes.notna()
    pct_above = ((closes > ma50) & has_ma).sum(axis=1) / has_ma.sum(axis=1) * 100
    pct_above = pct_above.reindex(daily.index)

    t = daily.iloc[-1]
    is_up90 = t.pct_up >= BREADTH_THRUST
    is_down90 = t.pct_down >= BREADTH_THRUST
    if is_up90:
        alert = ("90% UPSIDE DAY", "good",
                 f"{t.pct_up:.1f}% of S&P 500 stocks rose together.")
    elif is_down90:
        alert = ("90% DOWNSIDE DAY", "bad",
                 f"{t.pct_down:.1f}% of S&P 500 stocks fell together.")
    else:
        alert = ("NO 90% DAY", "neutral",
                 f"{t.pct_up:.1f}% up / {t.pct_down:.1f}% down.")

    recent = daily.iloc[-60:]
    up90_days = list(recent.index[recent.pct_up >= BREADTH_THRUST])
    down90_days = list(recent.index[recent.pct_down >= BREADTH_THRUST])

    return {
        "date": daily.index[-1], "pct_up": t.pct_up, "pct_down": t.pct_down,
        "up_vol_pct": t.up_vol_pct, "count": int(t.n), "universe": len(tickers),
        "is_up90": is_up90, "is_down90": is_down90, "alert": alert,
        "up90_days": up90_days, "down90_days": down90_days,
        "series": recent["pct_up"],
        "pct_above50": pct_above.iloc[-1],
        "above50_min20": pct_above.iloc[-20:].min(),
        "above50_series": pct_above.iloc[-60:].dropna(),
        "up90_last10": bool((daily["pct_up"].iloc[-10:] >= BREADTH_THRUST).any()),
        "last_down90": (daily.index[daily.pct_down >= BREADTH_THRUST][-1]
                        if (daily.pct_down >= BREADTH_THRUST).any() else None),
        "sessions_since_down90": (len(daily) - 1 - list(daily.index).index(
            daily.index[daily.pct_down >= BREADTH_THRUST][-1])
            if (daily.pct_down >= BREADTH_THRUST).any() else None),
    }


# ---- 3. Junk bond spreads ----------------------------------------------------
def analyze_hy():
    s = fred_series("BAMLH0A0HYM2") * 100  # percent -> basis points
    s = s[s.index >= s.index[-1] - pd.Timedelta(days=800)]
    d = s.diff()
    vol20 = d.rolling(20).std()          # 20-day realized volatility, bps/day
    last_vol = vol20.iloc[-1]
    hist = vol20.iloc[-252:].dropna()
    pctile = (hist <= last_vol).mean() * 100

    last = s.iloc[-1]
    c1 = last - s.iloc[-2]
    c5 = last - s.iloc[-6]
    c20 = last - s.iloc[-21]
    yr = s.iloc[-252:]
    level_pctile = (yr <= last).mean() * 100

    reasons = []
    if pctile >= HY_VOLATILE_PCTILE:
        reasons.append(f"day-to-day swings in the top {100 - pctile:.0f}% of the past year")
    if c20 >= HY_WIDEN_20D_BPS:
        reasons.append(f"widened {c20:.0f} bps in 20 days")
    if c5 >= HY_WIDEN_5D_BPS:
        reasons.append(f"widened {c5:.0f} bps in 5 days")

    if reasons:
        status = ("VOLATILE", "bad", "Spreads are " + "; ".join(reasons) + ".")
    elif pctile < HY_CALM_PCTILE and c20 < HY_WIDEN_20D_BPS / 2:
        status = ("CALM", "good",
                  f"Daily swings are quieter than {100 - pctile:.0f}% of the past year.")
    else:
        status = ("NORMAL", "neutral",
                  "Neither unusually quiet nor stressed.")

    return {
        "date": s.index[-1], "last": last, "c1": c1, "c5": c5, "c20": c20,
        "vol20": last_vol, "vol_pctile": pctile, "level_pctile": level_pctile,
        "yr_low": yr.min(), "yr_high": yr.max(), "alert": status,
        "series": s.iloc[-120:],
    }


# ---- 4. US dollar -------------------------------------------------------------
def analyze_dollar():
    s = yahoo_daily("DX-Y.NYB", "1y")["close"]
    last = s.iloc[-1]
    pct = lambda n: (last / s.iloc[-1 - n] - 1) * 100
    ma20, ma50 = s.iloc[-20:].mean(), s.iloc[-50:].mean()
    w60 = s.iloc[-60:]
    hi60, hi_date = w60.max(), w60.idxmax()
    off_high = (last / hi60 - 1) * 100
    high_recent = hi_date >= s.index[-20]

    if high_recent and last < ma20 and off_high <= -DXY_REVERSAL_PCT:
        status = ("REVERSING", "good",
                  f"Down {abs(off_high):.1f}% from its {hi_date:%b %d} high and back under its 20-day average.")
    elif last < ma50:
        status = ("FALLING", "good", "Below its 50-day average, so pressure on stocks is easing.")
    elif off_high > -0.5:
        status = ("AT HIGHS", "bad", "Strong dollar at or near a 60-day high, still a headwind.")
    else:
        status = ("FIRM", "neutral", "Above its 50-day average but off its highs.")

    return {"date": s.index[-1], "last": last, "c1": pct(1), "c5": pct(5), "c20": pct(20),
            "ma50": ma50, "hi60": hi60, "hi_date": hi_date, "off_high": off_high,
            "alert": status, "ok": status[1] == "good", "series": s.iloc[-120:]}


# ---- 5. Yield curve & rates ----------------------------------------------------
def analyze_rates():
    curve = fred_series("T10Y2Y") * 100      # bps
    y10 = fred_series("DGS10")
    y2 = fred_series("DGS2")
    c = curve.iloc[-1]
    c5, c20 = c - curve.iloc[-6], c - curve.iloc[-21]
    y10_c20 = (y10.iloc[-1] - y10.iloc[-21]) * 100
    y2_c20 = (y2.iloc[-1] - y2.iloc[-21]) * 100
    rates_stable = abs(y10_c20) < RATES_STABLE_BPS

    if c20 <= -CURVE_MOVE_BPS:
        status = ("FLATTENING", "bad",
                  f"Curve flattened {abs(c20):.0f} bps in 20 days, a sign of growth and credit stress.")
    elif c20 >= CURVE_MOVE_BPS:
        status = ("STEEPENING", "neutral",
                  f"Curve steepened {c20:.0f} bps in 20 days, often as markets price in rate cuts.")
    else:
        status = ("STABLE", "good", "Curve little changed over 20 days.")
    status = (status[0], status[1], status[2] + (
        f" 10-year yield {'stabilizing' if rates_stable else 'still moving fast'} "
        f"({y10_c20:+.0f} bps in 20 days)."))

    return {"date": curve.index[-1], "curve": c, "c5": c5, "c20": c20,
            "y10": y10.iloc[-1], "y2": y2.iloc[-1], "y10_c20": y10_c20, "y2_c20": y2_c20,
            "rates_stable": rates_stable, "alert": status,
            "ok": status[0] != "FLATTENING" and rates_stable, "series": curve.iloc[-120:]}


# ---- 6. Selling pressure: sectors + failed rallies --------------------------------
SECTORS = {"XLK": "Tech", "XLF": "Financials", "XLE": "Energy", "XLV": "Health care",
           "XLY": "Discretionary", "XLP": "Staples", "XLI": "Industrials",
           "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real estate",
           "XLC": "Communication"}
INDEXES = {"^IXIC": "Nasdaq", "^GSPC": "S&P 500"}


def analyze_pressure():
    with ThreadPoolExecutor(max_workers=8) as ex:
        sec = dict(zip(SECTORS, ex.map(lambda t: yahoo_daily(t, "3mo")["close"], SECTORS)))
        idx = dict(zip(INDEXES, ex.map(lambda t: yahoo_daily(t, "3mo"), INDEXES)))

    rets = pd.DataFrame(sec).pct_change(fill_method=None).dropna(how="all") * 100
    today = rets.iloc[-1].dropna()
    n_down, n_up = int((today < 0).sum()), int((today > 0).sum())
    n_down_series = (rets < 0).sum(axis=1)
    n_up_series = (rets > 0).sum(axis=1)
    best, worst = today.idxmax(), today.idxmin()

    days = {}
    failed_days, reversal_days = set(), set()
    for sym, df in idx.items():
        prev = df["close"].shift(1)
        at_high = (df["high"] / prev - 1) * 100
        at_low = (df["low"] / prev - 1) * 100
        chg = (df["close"] / prev - 1) * 100
        failed = (at_high >= RALLY_PCT) & (chg < 0)
        reversal = (at_low <= -RALLY_PCT) & (chg > 0)
        failed_days |= set(df.index[-20:][failed.iloc[-20:].values])
        reversal_days |= set(df.index[-20:][reversal.iloc[-20:].values])
        days[INDEXES[sym]] = {"date": df.index[-1], "at_high": at_high.iloc[-1],
                              "at_low": at_low.iloc[-1], "chg": chg.iloc[-1],
                              "failed": bool(failed.iloc[-1]), "reversal": bool(reversal.iloc[-1])}

    d = days["Nasdaq"]
    failed_today = any(v["failed"] for v in days.values())
    reversal_today = any(v["reversal"] for v in days.values())
    if failed_today:
        who = " and ".join(k for k, v in days.items() if v["failed"])
        status = ("FAILED RALLY", "bad",
                  f"{who} rallied {max(v['at_high'] for v in days.values()):.1f}% then turned negative: "
                  "sellers are using bounces to get out.")
    elif reversal_today:
        who = " and ".join(k for k, v in days.items() if v["reversal"])
        status = ("REVERSAL UP", "good",
                  f"{who} fell {abs(min(v['at_low'] for v in days.values())):.1f}% then closed higher: "
                  "buyers stepped in on the dip.")
    elif n_down >= 10:
        status = ("BROAD SELLING", "bad", f"{n_down} of 11 sectors fell: money is leaving stocks, not rotating.")
    elif n_up >= 10:
        status = ("BROAD BUYING", "good", f"{n_up} of 11 sectors rose together.")
    else:
        status = ("MIXED", "neutral", f"{n_up} sectors up, {n_down} down.")

    recent_buying = bool(reversal_days & set(rets.index[-5:])) or bool((n_up_series.iloc[-5:] >= 10).any())
    return {"date": rets.index[-1], "n_down": n_down, "n_up": n_up,
            "best": (SECTORS[best], today[best]), "worst": (SECTORS[worst], today[worst]),
            "broad_down_days_10": int((n_down_series.iloc[-10:] >= 10).sum()),
            "idx": days, "failed_today": failed_today, "reversal_today": reversal_today,
            "failed_days": sorted(failed_days), "reversal_days": sorted(reversal_days),
            "recent_buying": recent_buying, "alert": status}


# ---- 7. Trend ranges: buy/sell levels and trend signals ---------------------------
TREND_GROUPS = [
    ("Top Index ETFs", {"IWM": "Russell 2000", "HYG": "High Yield Bonds", "GLD": "Gold",
                        "UUP": "U.S. Dollar", "FXE": "Euro", "SLV": "Silver", "GDX": "Gold Miners",
                        "DBA": "Agriculture", "CPER": "Copper", "ETHA": "Ethereum",
                        "QQQ": "Nasdaq 100", "IBIT": "Bitcoin", "SPY": "S&P 500",
                        "GSG": "Broad Commodities", "USO": "Oil"}),
    ("Top Sector ETFs", {"XLV": "Healthcare", "XLI": "Industrials", "XLC": "Communication Services",
                         "XLP": "Consumer Staples", "XLK": "Technology", "XLU": "Utilities",
                         "XLRE": "Real Estate", "XLE": "Energy", "XLB": "Materials",
                         "XLF": "Financials", "XLY": "Consumer Discretionary"}),
] + [(f"{name} · {etf} stocks", stocks) for etf, name, stocks in [
    # Largest holdings of each Select Sector SPDR (edit to taste).
    ("XLK", "Technology", {"NVDA": "Nvidia", "AAPL": "Apple", "MSFT": "Microsoft", "AVGO": "Broadcom",
                           "ORCL": "Oracle", "AMD": "AMD", "MU": "Micron", "SNDK": "SanDisk"}),
    ("XLF", "Financials", {"BRK-B": "Berkshire Hathaway", "JPM": "JPMorgan", "V": "Visa",
                           "MA": "Mastercard", "BAC": "Bank of America", "WFC": "Wells Fargo",
                           "GS": "Goldman Sachs"}),
    ("XLV", "Healthcare", {"LLY": "Eli Lilly", "JNJ": "Johnson & Johnson", "ABBV": "AbbVie",
                           "UNH": "UnitedHealth", "MRK": "Merck", "ABT": "Abbott"}),
    ("XLI", "Industrials", {"GE": "GE Aerospace", "CAT": "Caterpillar", "RTX": "RTX",
                            "GEV": "GE Vernova", "UBER": "Uber", "BA": "Boeing", "HON": "Honeywell"}),
    ("XLC", "Communication Services", {"META": "Meta", "GOOGL": "Alphabet", "NFLX": "Netflix",
                                       "DIS": "Disney", "TMUS": "T-Mobile", "T": "AT&T"}),
    ("XLP", "Consumer Staples", {"WMT": "Walmart", "COST": "Costco", "PG": "Procter & Gamble",
                                 "KO": "Coca-Cola", "PM": "Philip Morris", "PEP": "PepsiCo"}),
    ("XLU", "Utilities", {"NEE": "NextEra Energy", "SO": "Southern Co", "CEG": "Constellation Energy",
                          "DUK": "Duke Energy", "VST": "Vistra"}),
    ("XLRE", "Real Estate", {"WELL": "Welltower", "PLD": "Prologis", "AMT": "American Tower",
                             "EQIX": "Equinix", "SPG": "Simon Property"}),
    ("XLE", "Energy", {"XOM": "Exxon Mobil", "CVX": "Chevron", "COP": "ConocoPhillips",
                       "WMB": "Williams", "EOG": "EOG Resources"}),
    ("XLB", "Materials", {"LIN": "Linde", "NEM": "Newmont", "SHW": "Sherwin-Williams",
                          "ECL": "Ecolab", "FCX": "Freeport-McMoRan"}),
    ("XLY", "Consumer Discretionary", {"AMZN": "Amazon", "TSLA": "Tesla", "HD": "Home Depot",
                                       "MCD": "McDonald's", "BKNG": "Booking", "TJX": "TJX"}),
]]


def trend_row(sym, name, df):
    """Trade range, trend state and when that state began, from daily bars."""
    c = df["close"]
    hi, lo = df["high"].fillna(c), df["low"].fillna(c)
    tr = pd.concat([hi - lo, (hi - c.shift()).abs(), (lo - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(RANGE_ATR).mean().iloc[-1]
    price = float(c.iloc[-1])
    mid = c.ewm(span=RANGE_EMA, adjust=False).mean().iloc[-1]
    buy = mid - RANGE_BUY_MULT * atr
    sell = mid + RANGE_SELL_MULT * atr

    fast = c.ewm(span=TREND_FAST, adjust=False).mean()
    slow = c.ewm(span=TREND_SLOW, adjust=False).mean()
    up = fast > slow
    state = pd.Series("Bearish", index=c.index)
    state[up] = "Bullish"
    state[(up & (c < slow)) | (~up & (c > slow))] = "Neutral"   # price crossed back against the trend
    state, trend = state.iloc[TREND_SLOW * 2:], up.iloc[TREND_SLOW * 2:]   # EMAs need time to settle
    # A trend signal dates from the crossover; a neutral one from when it turned neutral.
    run = state if state.iloc[-1] == "Neutral" else trend
    start = run.index[run.ne(run.shift())][-1]
    since_start = run.index[0] == start      # no flip in the data we have
    sig_price = float(c.loc[start])
    return {"sym": sym.replace("-", "."), "name": name, "price": price, "buy": buy, "sell": sell,
            "upside": (sell / price - 1) * 100, "downside": (buy / price - 1) * 100,
            "signal": state.iloc[-1], "rising": not bool(up.iloc[-1]),
            "sig_date": start, "sig_old": since_start, "sig_price": sig_price,
            "sig_perf": (price / sig_price - 1) * 100,
            "days": (c.index[-1] - start).days, "date": c.index[-1],
            # sessions missing from the last two weeks (Yahoo sometimes drops bars)
            "gaps": max(0, len(pd.bdate_range(c.index[-1] - pd.Timedelta(days=13), c.index[-1]))
                        - 1 - int((c.index > c.index[-1] - pd.Timedelta(days=13)).sum()))}


def analyze_trends():
    syms = [(s, n) for _, g in TREND_GROUPS for s, n in g.items()]

    def one(item):
        try:
            df = yahoo_daily(item[0], "2y")
            recent = yahoo_daily(item[0], "1mo")   # long ranges sometimes miss the latest bars
            df = recent.combine_first(df)
            return trend_row(item[0], item[1], df)
        except Exception as e:
            print(f"  trend {item[0]} failed: {e}")
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = dict(zip([s for s, _ in syms], ex.map(one, syms)))
    out = []
    for title, g in TREND_GROUPS:
        got = [rows[s] for s in g if rows.get(s)]
        got.sort(key=lambda r: r["sig_date"])
        out.append((title, got))
    dates = [r["date"] for _, g in out for r in g]
    return {"groups": out, "date": max(dates) if dates else None}


# ---- Bottom checklist ----------------------------------------------------------
def checklist(vix, br, hy, dx, rt, pr):
    items = []
    fd = vix["recent_fast_drops"]
    still_below = vix["last"] < VIX_LINE
    if fd and still_below:
        detail = f"Yes, {fd[-1]:%b %d}, still below"
    elif fd:
        detail = f"Did on {fd[-1]:%b %d}, but back above 25 ({vix['last']:.1f})"
    else:
        detail = f"No, VIX {vix['last']:.1f}"
    items.append(("VIX dropped fast below 25 and stayed there", bool(fd) and still_below, detail))
    if vix["ts_ratio"] is not None:
        r, mx = vix["ts_ratio"], vix["ts_recent_max"]
        ok = r < 1
        detail = (f"Calm ({r:.2f})" + (" after panic in last 20 days" if ok and mx >= 1 else "")
                  if ok else f"Panic: short-term fear above 3-month ({r:.2f})")
        items.append(("VIX term structure back to normal", ok, detail))
    if br["up90_last10"]:
        detail = "Yes"
    elif br["last_down90"] is not None and br["sessions_since_down90"] <= 10:
        detail = (f"None yet. Last 90% down day {br['last_down90']:%b %d}; "
                  "a 90% up day soon after would be the strongest signal")
    else:
        detail = "None yet"
    items.append(("A 90% up day (last 10 days)", br["up90_last10"], detail))
    a, mn = br["pct_above50"], br["above50_min20"]
    ok = a >= 50 or (mn < WASHOUT_PCT and a >= mn + 10)
    items.append(("Stocks recovering above 50-day averages", ok,
                  f"{a:.0f}% above (20-day low {mn:.0f}%)"))
    ok = hy["c20"] < 0 and hy["alert"][0] != "VOLATILE"
    items.append(("Junk spreads calming and narrowing", ok,
                  f"{hy['alert'][0].title()}, {hy['c20']:+.0f} bps in 20 days"))
    items.append(("Dollar reversing lower", dx["ok"], dx["alert"][0].title()))
    items.append(("Yield curve not flattening, rates stable", rt["ok"],
                  f"{rt['alert'][0].title()}, 10y {rt['y10_c20']:+.0f} bps in 20d"))
    if pr["recent_buying"]:
        detail = ("Reversal up on " + ", ".join(f"{d:%b %d}" for d in pr["reversal_days"][-2:])
                  if pr["reversal_days"] else "10+ sectors up together")
    else:
        detail = (f"No. {pr['n_down']}/11 sectors down today"
                  + (", failed rally today" if pr["failed_today"] else ""))
    items.append(("Buyers stepping in: reversal up or 10+ sectors up (last 5 days)",
                  pr["recent_buying"], detail))
    return items


# ---- History log -------------------------------------------------------------
def append_history(vix, br, hy, dx, rt, pr, checks):
    row = {
        "run_date": datetime.now().strftime("%Y-%m-%d"),
        "vix_date": f"{vix['date']:%Y-%m-%d}",
        "vix": round(vix["last"], 2),
        "vix_chg_1d": round(vix["d1"][0], 2),
        "vix_chg_1d_pct": round(vix["d1"][1], 2),
        "vix_drop_from_10d_high_pct": round(vix["drop_from_high"], 1),
        "vix_fast_drop_below_25": vix["fast_drop"],
        "breadth_date": f"{br['date']:%Y-%m-%d}",
        "pct_stocks_up": round(br["pct_up"], 1),
        "pct_stocks_down": round(br["pct_down"], 1),
        "pct_up_volume": round(br["up_vol_pct"], 1),
        "ninety_pct_up_day": br["is_up90"],
        "ninety_pct_down_day": br["is_down90"],
        "hy_date": f"{hy['date']:%Y-%m-%d}",
        "hy_oas_bps": round(hy["last"]),
        "hy_chg_1d_bps": round(hy["c1"]),
        "hy_chg_20d_bps": round(hy["c20"]),
        "hy_vol_pctile": round(hy["vol_pctile"]),
        "hy_status": hy["alert"][0],
        "vix_vs_vix3m": round(vix["ts_ratio"], 3) if vix["ts_ratio"] is not None else "",
        "pct_above_50dma": round(br["pct_above50"], 1),
        "dxy": round(dx["last"], 2),
        "dxy_chg_1d_pct": round(dx["c1"], 2),
        "dxy_status": dx["alert"][0],
        "curve_10y2y_bps": round(rt["curve"]),
        "curve_chg_20d_bps": round(rt["c20"]),
        "us10y": round(rt["y10"], 2),
        "curve_status": rt["alert"][0],
        "sectors_down": pr["n_down"],
        "failed_rally": pr["failed_today"],
        "reversal_up": pr["reversal_today"],
        "bottom_score": f"{sum(ok for _, ok, _ in checks)}/{len(checks)}",
    }
    rows = []
    if os.path.exists(HISTORY):
        with open(HISTORY, newline="") as f:
            rows = [r for r in csv.DictReader(f) if r["vix_date"] != row["vix_date"]]
    rows.append({k: str(v) for k, v in row.items()})
    with open(HISTORY, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        w.writeheader()
        w.writerows(rows)
    return rows


# ---- HTML report -------------------------------------------------------------
def sparkline(series, line=None, w=560, h=120, bars=False, fmt="{:.1f}"):
    vals = [float(v) for v in series.values]
    lo, hi = min(vals), max(vals)
    if line is not None:
        lo, hi = min(lo, line), max(hi, line)
    if bars:
        lo, hi = 0, 100
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - (0 if bars else pad), hi + (0 if bars else pad)
    X = lambda i: 4 + i * (w - 8) / max(len(vals) - 1, 1)
    Y = lambda v: h - 4 - (v - lo) / (hi - lo) * (h - 8)
    parts = [f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" class="spark">']
    if line is not None:
        parts.append(f'<line x1="0" x2="{w}" y1="{Y(line):.1f}" y2="{Y(line):.1f}" class="ref"/>'
                     f'<text x="{w - 4}" y="{Y(line) - 4:.1f}" class="reflbl">{fmt.format(line)}</text>')
    if bars:
        bw = (w - 8) / len(vals)
        for i, v in enumerate(vals):
            cls = "bar hit" if v >= BREADTH_THRUST else ("bar low" if v <= 100 - BREADTH_THRUST else "bar")
            parts.append(f'<rect x="{4 + i * bw:.1f}" y="{Y(v):.1f}" width="{bw * 0.75:.1f}" '
                         f'height="{h - 4 - Y(v):.1f}" class="{cls}"/>')
    else:
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
        parts.append(f'<polyline points="{pts}" class="ln"/>')
        parts.append(f'<circle cx="{X(len(vals) - 1):.1f}" cy="{Y(vals[-1]):.1f}" r="3.5" class="dot"/>')
    parts.append("</svg>")
    return "".join(parts)


def signed(v, unit="", dec=2):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    cls = "up" if v > 0 else ("down" if v < 0 else "")
    return f'<span class="{cls}">{v:+.{dec}f}{unit}</span>'


def badge(alert):
    label, tone, text = alert
    return f'<div class="badge {tone}">{label}</div><p class="why">{text}</p>'


def money(v):
    return f"${v:,.2f}"


# What to make of a price near each end of its range, by trend state.
TREND_STATES = [  # key, label, dot, meaning
    ("bull", "Bullish ↑", "🟢", "Favorable upside conditions"),
    ("nup", "Neutral ↑", "🟡", "Improving conditions; upside potential increasing"),
    ("ndn", "Neutral ↓", "🟠", "Weakening conditions; downside risk increasing"),
    ("bear", "Bearish ↓", "🔴", "Unfavorable conditions; downside risk elevated"),
]
NEAR_BUY = {"bull": "Potential buying opportunity", "nup": "Watch for support",
            "ndn": "Caution buying dips", "bear": "New exposure generally unfavorable"}
NEAR_SELL = {"bull": "Potential profit-taking opportunity", "nup": "Watch for upside continuation",
             "ndn": "Reduced or no exposure favored", "bear": "Potential selling or shorting opportunity"}


def state_key(r):
    if r["signal"] == "Bullish":
        return "bull"
    if r["signal"] == "Bearish":
        return "bear"
    return "nup" if r["rising"] else "ndn"


def range_read(r):
    pos = (r["price"] - r["buy"]) / (r["sell"] - r["buy"])
    k = state_key(r)
    if pos <= RANGE_NEAR:
        return "Near buy: " + NEAR_BUY[k]
    if pos >= 1 - RANGE_NEAR:
        return "Near sell: " + NEAR_SELL[k]
    return "Mid-range"


def trend_legend():
    li = lambda text: "".join(f'<li><span class="pill {k}">{dot} {lbl}</span> {text[k]}</li>'
                              for k, lbl, dot, _ in TREND_STATES)
    states = li({k: m for k, _, _, m in TREND_STATES})
    return (f'<div class="legend"><div><h3>How to read the trend signal</h3><ul>{states}</ul></div>'
            f'<div><h3>Near the buy level (low end of range)</h3><ul>{li(NEAR_BUY)}</ul></div>'
            f'<div><h3>Near the sell level (top end of range)</h3><ul>{li(NEAR_SELL)}</ul></div></div>'
            f'<p class="why">The trading range marks likely turning levels; the trend signal says how to read a test of either end. '
            f'They are reference levels, not guaranteed turning points. Short selling carries extra risk, including unlimited losses.</p>')


def trend_table(tr):
    if not tr["date"]:
        return '<section class="card trend"><h2>Trend ranges</h2><p class="why">No price data this run.</p></section>'
    head = ('<tr class="thead"><th>Symbol</th><th class="nm">{title}</th><th>Current price</th>'
            '<th class="buyh">Buy level</th><th class="sellh">Sell level</th><th>Upside</th>'
            '<th>Downside</th><th class="sigh">Trend signal</th><th>Signal date</th>'
            '<th>Signal price</th><th>Signal perf</th><th>Days</th><th class="rd">Read</th></tr>')
    body = []
    for title, rows in tr["groups"]:
        body.append(head.format(title=title))
        for r in rows:
            sig = r["signal"]
            arrow = "↑" if (sig == "Bullish" or (sig == "Neutral" and r["rising"])) else "↓"
            perf_cls = "pg" if r["sig_perf"] > 0 else "pb"
            read = range_read(r)
            rcls = "mid" if read == "Mid-range" else state_key(r)
            if r["gaps"] >= 2:
                read, rcls = f"⚠ {r['gaps']} recent sessions missing from Yahoo; levels less reliable", "mid"
            date = ("before " if r["sig_old"] else "") + f"{r['sig_date']:%-m/%-d/%Y}"
            body.append(
                f'<tr><td class="sym">{r["sym"]}</td><td class="nm">{r["name"]}</td>'
                f'<td>{money(r["price"])}</td><td class="buy">{money(r["buy"])}</td>'
                f'<td class="sell">{money(r["sell"])}</td>'
                f'<td class="gtxt">{r["upside"]:.2f}%</td><td class="rtxt">{r["downside"]:.2f}%</td>'
                f'<td class="sig {state_key(r)}">{sig} {arrow}</td><td>{date}</td>'
                f'<td>{money(r["sig_price"])}</td><td class="{perf_cls}">{r["sig_perf"]:.2f}%</td>'
                f'<td>{r["days"]}</td><td class="rd {rcls}">{read}</td></tr>')
    return (f'<section class="card trend"><h2>Trend ranges · buy/sell levels &amp; trend signals</h2>'
            f'<p class="why">Prices as of {tr["date"]:%b %d}. Buy and sell levels are a short-term trading range; '
            f'the trend signal is the bigger picture. Index and sector ETFs first, then the largest stocks in each of the 11 sectors. Within each group, sorted by how long the trend has run.</p>'
            f'<div class="tscroll"><table class="tt"><tbody>{"".join(body)}</tbody></table></div>'
            f'{trend_legend()}</section>')


def render(vix, br, hy, dx, rt, pr, checks, history, tr, macro):
    d1, d5, d20 = vix["d1"], vix["d5"], vix["d20"]
    cross = f"{vix['cross_date']:%b %d, %Y}" if vix["cross_date"] is not None else "–"
    fmt_days = lambda ds: ", ".join(f"{d:%b %d}" for d in ds[-8:]) or "none"

    score = sum(ok for _, ok, _ in checks)
    idx_rows = "".join(
        f"<tr><td>{name}: high / close vs prior close</td><td>{signed(v['at_high'], '%', 1)} / {signed(v['chg'], '%', 1)}</td></tr>"
        for name, v in pr["idx"].items())
    check_rows = "".join(
        f'<li class="{"on" if ok else "off"}"><span class="mark">{"✓" if ok else "✗"}</span>'
        f'<span class="cname">{name}</span><span class="cdet">{detail}</span></li>'
        for name, ok, detail in checks)
    ts = (f"{vix['ts_ratio']:.2f} " + ("(panic: above 1)" if vix["ts_ratio"] >= 1 else "(normal: below 1)")
          if vix["ts_ratio"] is not None else "–")

    hist_rows = "".join(
        f"<tr><td>{r['vix_date']}</td><td>{r['vix']}</td><td>{r['vix_chg_1d']}</td>"
        f"<td>{'✔' if r['vix_fast_drop_below_25'] == 'True' else ''}</td>"
        f"<td>{r['pct_stocks_up']}%</td>"
        f"<td>{'▲ 90%' if r['ninety_pct_up_day'] == 'True' else ('▼ 90%' if r['ninety_pct_down_day'] == 'True' else '')}</td>"
        f"<td>{r['hy_oas_bps']}</td><td>{r['hy_status']}</td>"
        f"<td>{r.get('dxy') or ''}</td><td>{r.get('curve_10y2y_bps') or ''}</td>"
        f"<td>{r.get('sectors_down') or ''}</td>"
        f"<td>{'✗ failed' if r.get('failed_rally') == 'True' else ('✓ reversal' if r.get('reversal_up') == 'True' else '')}</td>"
        f"<td>{r.get('bottom_score') or ''}</td></tr>"
        for r in reversed(history[-30:]))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Bottom Daily Read</title>
<meta name="apple-mobile-web-app-title" content="Market Read">
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --ink:#15181d; --muted:#667085; --line:#e4e7ec;
  --good:#0f8a5f; --good-bg:#e3f6ee; --bad:#c2352b; --bad-bg:#fbe9e7;
  --neu:#475467; --neu-bg:#eef1f5; --accent:#3559e0; --hdr:#1d2b3a; --warn:#8a6100; --warn-bg:#fdf3d0; --orange:#b2470b; --orange-bg:#fde6d6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#181b21; --ink:#e8eaee;
  --muted:#98a2b3; --line:#2a2f38; --good:#3ecf8e; --good-bg:#12302a; --bad:#ff6b5e;
  --bad-bg:#3a1a18; --neu:#c0c7d2; --neu-bg:#232832; --accent:#7c9bff; --hdr:#26354a;
  --warn:#f5c542; --warn-bg:#3a3014; --orange:#ff9a52; --orange-bg:#3a2214; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width:1180px; margin:0 auto; padding:28px 16px 48px }}
h1 {{ margin:0 0 4px; font-size:26px }} .sub {{ color:var(--muted); margin:0 0 24px }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(330px, 1fr)); gap:16px }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px }}
.card h2 {{ margin:0; font-size:13px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted) }}
.big {{ font-size:44px; font-weight:700; letter-spacing:-.02em; margin:6px 0 2px; font-variant-numeric:tabular-nums }}
.big small {{ font-size:16px; font-weight:500; color:var(--muted); margin-left:6px }}
.badge {{ display:inline-block; font-weight:700; font-size:13px; padding:5px 10px; border-radius:999px; margin-top:8px }}
.badge.good {{ color:var(--good); background:var(--good-bg) }} .badge.bad {{ color:var(--bad); background:var(--bad-bg) }}
.badge.neutral {{ color:var(--neu); background:var(--neu-bg) }}
.why {{ margin:8px 0 12px; color:var(--muted) }}
table.kv {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums }}
table.kv td {{ padding:6px 0; border-top:1px solid var(--line) }} table.kv td:last-child {{ text-align:right }}
.up {{ color:var(--bad) }} .down {{ color:var(--good) }}
.breadth .up {{ color:var(--good) }} .breadth .down {{ color:var(--bad) }}
.spark {{ width:100%; height:110px; margin-top:12px; display:block }}
.ln {{ fill:none; stroke:var(--accent); stroke-width:2; vector-effect:non-scaling-stroke }}
.dot {{ fill:var(--accent) }} .ref {{ stroke:var(--muted); stroke-dasharray:4 4; vector-effect:non-scaling-stroke }}
.reflbl {{ fill:var(--muted); font-size:11px; text-anchor:end }}
.bar {{ fill:var(--line) }} .bar.hit {{ fill:var(--good) }} .bar.low {{ fill:var(--bad) }}
.cap {{ font-size:12px; color:var(--muted); margin-top:4px }}
.hist {{ margin-top:16px; overflow-x:auto }}
.hist table {{ width:100%; border-collapse:collapse; font-size:14px; font-variant-numeric:tabular-nums }}
.hist th, .hist td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); white-space:nowrap }}
.hist th {{ color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.04em }}
.sub a {{ color:var(--accent); font-weight:600; text-decoration:none }}
.check {{ margin-bottom:16px }}
.score {{ font-size:34px; font-weight:700; margin:6px 0 10px }} .score small {{ font-size:15px; color:var(--muted); font-weight:500 }}
.check ul {{ list-style:none; margin:0; padding:0; display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:6px 24px }}
.check li {{ display:grid; grid-template-columns:18px 1fr; column-gap:8px; padding:8px 0; border-top:1px solid var(--line) }}
.check .mark {{ font-weight:700; grid-row:span 2 }} .check li.on .mark {{ color:var(--good) }} .check li.off .mark {{ color:var(--bad) }}
.check .cname {{ font-weight:600 }} .check .cdet {{ color:var(--muted); font-size:13px }}
.notes {{ color:var(--muted); font-size:13px; margin-top:20px }}
{macro_brief.CSS.replace('{{', '{').replace('}}', '}')}
.trend {{ margin-top:16px; padding:20px 0 12px }} .trend h2, .trend .why {{ padding:0 20px }}
.tscroll {{ overflow-x:auto; -webkit-overflow-scrolling:touch }}
.tt {{ width:100%; border-collapse:collapse; font-size:13px; font-variant-numeric:tabular-nums }}
.tt td, .tt th {{ padding:5px 9px; text-align:center; white-space:nowrap; border-bottom:1px solid var(--line) }}
.tt .thead th {{ background:var(--hdr); color:#fff; font-size:12px; font-weight:700; position:sticky; top:0 }}
.tt .thead th.buyh {{ box-shadow:inset 0 -3px 0 var(--good) }} .tt .thead th.sellh {{ box-shadow:inset 0 -3px 0 var(--bad) }}
.tt .thead th.sigh {{ color:#f5c542 }}
.tt .sym {{ font-weight:700; position:sticky; left:0; background:var(--card) }}
.tt .thead th:first-child {{ position:sticky; left:0; z-index:1 }}
.tt .buy {{ background:var(--good-bg); color:var(--ink); font-weight:700 }}
.tt .sell {{ background:var(--bad-bg); color:var(--ink); font-weight:700 }}
.tt .gtxt {{ color:var(--good) }} .tt .rtxt {{ color:var(--bad) }}
.tt .sig {{ font-weight:700 }}
.bull {{ color:var(--good); background:var(--good-bg) }} .bear {{ color:var(--bad); background:var(--bad-bg) }}
.nup {{ color:var(--warn); background:var(--warn-bg) }} .ndn {{ color:var(--orange); background:var(--orange-bg) }}
.tt td.rd {{ text-align:left; font-size:12px; font-weight:600 }} .tt td.rd.mid {{ color:var(--muted); font-weight:400 }}
.legend {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:4px 24px; padding:16px 20px 0 }}
.legend h3 {{ font-size:14px; margin:0 0 6px }} .legend ul {{ list-style:none; margin:0 0 10px; padding:0 }}
.legend li {{ padding:4px 0; font-size:13px; color:var(--muted) }}
.pill {{ display:inline-block; font-weight:700; font-size:12px; padding:2px 8px; border-radius:999px; margin-right:4px }}
.tt .pg {{ color:var(--good); background:var(--good-bg) }} .tt .pb {{ color:var(--bad); background:var(--bad-bg) }}
</style></head><body><main>
<h1>Market Bottom Daily Read</h1>
<p class="sub">Updated {datetime.now():%a %b %d, %Y · %I:%M %p} ET ·
<a href="{REFRESH_URL}">Refresh now ↻</a></p>

<section class="card check">
  <h2>Bottom checklist</h2>
  <div class="score">{score} of {len(checks)}<small> signals point to a bottom</small></div>
  <ul>{check_rows}</ul>
</section>

{macro_brief.render(macro)}

<div class="grid">
  <section class="card">
    <h2>VIX · volatility index</h2>
    <div class="big">{vix['last']:.2f}<small>{vix['regime']} · {vix['date']:%b %d}</small></div>
    {badge(vix['alert'])}
    <table class="kv">
      <tr><td>1-day move</td><td>{signed(d1[0], ' pts')} ({signed(d1[1], '%', 1)})</td></tr>
      <tr><td>5-day move</td><td>{signed(d5[0], ' pts')} ({signed(d5[1], '%', 1)})</td></tr>
      <tr><td>20-day move</td><td>{signed(d20[0], ' pts')} ({signed(d20[1], '%', 1)})</td></tr>
      <tr><td>{VIX_LOOKBACK}-day high</td><td>{vix['recent_high']:.2f} ({vix['recent_high_date']:%b %d})</td></tr>
      <tr><td>Change from that high</td><td>{signed(vix['drop_from_high'], '%', 1)}</td></tr>
      <tr><td>Last cross below 25</td><td>{cross}</td></tr>
      <tr><td>VIX ÷ 3-month VIX</td><td>{ts}</td></tr>
    </table>
    {sparkline(vix['series'], line=VIX_LINE, fmt="{:.0f}")}
    <div class="cap">Last 6 months · dashed line = 25</div>
  </section>

  <section class="card breadth">
    <h2>Breadth · S&amp;P 500 stocks up</h2>
    <div class="big">{br['pct_up']:.1f}%<small>of {br['count']} stocks · {br['date']:%b %d}</small></div>
    {badge(br['alert'])}
    <table class="kv">
      <tr><td>Stocks up</td><td><span class="up">{br['pct_up']:.1f}%</span></td></tr>
      <tr><td>Stocks down</td><td>{br['pct_down']:.1f}%</td></tr>
      <tr><td>Share of volume in rising stocks</td><td>{br['up_vol_pct']:.1f}%</td></tr>
      <tr><td>90% up days (last 60)</td><td>{len(br['up90_days'])} — {fmt_days(br['up90_days'])}</td></tr>
      <tr><td>Stocks above 50-day average</td><td>{br['pct_above50']:.0f}% (20-day low {br['above50_min20']:.0f}%)</td></tr>
      <tr><td>90% down days (last 60)</td><td>{len(br['down90_days'])} — {fmt_days(br['down90_days'])}</td></tr>
      <tr><td>Since last 90% down day</td><td>{(str(br['sessions_since_down90']) + ' sessions') if br['last_down90'] is not None else '–'}</td></tr>
    </table>
    {sparkline(br['series'], line=BREADTH_THRUST, bars=True, fmt="{:.0f}%")}
    <div class="cap">% of stocks up each day, last 60 sessions · green = 90%+ up, red = 90%+ down</div>
  </section>

  <section class="card">
    <h2>Junk bond spreads · HY OAS</h2>
    <div class="big">{hy['last']:.0f}<small>bps · {hy['date']:%b %d}</small></div>
    {badge(hy['alert'])}
    <table class="kv">
      <tr><td>1-day change</td><td>{signed(hy['c1'], ' bps', 0)}</td></tr>
      <tr><td>5-day change</td><td>{signed(hy['c5'], ' bps', 0)}</td></tr>
      <tr><td>20-day change</td><td>{signed(hy['c20'], ' bps', 0)}</td></tr>
      <tr><td>Typical daily swing (20d)</td><td>{hy['vol20']:.1f} bps · {hy['vol_pctile']:.0f}th pctile (1y)</td></tr>
      <tr><td>1-year range</td><td>{hy['yr_low']:.0f} – {hy['yr_high']:.0f} bps</td></tr>
    </table>
    {sparkline(hy['series'], fmt="{:.0f}")}
    <div class="cap">Last ~6 months · rising line = spreads widening (more stress)</div>
  </section>

  <section class="card breadth">
    <h2>Selling pressure · sectors &amp; rallies</h2>
    <div class="big">{pr['n_down']}/11<small>sectors down · {pr['date']:%b %d}</small></div>
    {badge(pr['alert'])}
    <table class="kv">
      <tr><td>Best sector</td><td>{pr['best'][0]} {signed(pr['best'][1], '%', 1)}</td></tr>
      <tr><td>Worst sector</td><td>{pr['worst'][0]} {signed(pr['worst'][1], '%', 1)}</td></tr>
      <tr><td>Days with 10+ sectors down (last 10)</td><td>{pr['broad_down_days_10']}</td></tr>
      {idx_rows}
      <tr><td>Failed rallies (last 20)</td><td>{len(pr['failed_days'])} — {fmt_days(pr['failed_days'])}</td></tr>
      <tr><td>Reversals up (last 20)</td><td>{len(pr['reversal_days'])} — {fmt_days(pr['reversal_days'])}</td></tr>
    </table>
    <div class="cap">At the 10am run, "close" means the price so far today.</div>
  </section>

  <section class="card">
    <h2>US dollar index · DXY</h2>
    <div class="big">{dx['last']:.2f}<small>{dx['date']:%b %d}</small></div>
    {badge(dx['alert'])}
    <table class="kv">
      <tr><td>1-day change</td><td>{signed(dx['c1'], '%')}</td></tr>
      <tr><td>5-day change</td><td>{signed(dx['c5'], '%')}</td></tr>
      <tr><td>20-day change</td><td>{signed(dx['c20'], '%')}</td></tr>
      <tr><td>60-day high</td><td>{dx['hi60']:.2f} ({dx['hi_date']:%b %d})</td></tr>
      <tr><td>Change from that high</td><td>{signed(dx['off_high'], '%')}</td></tr>
      <tr><td>50-day average</td><td>{dx['ma50']:.2f}</td></tr>
    </table>
    {sparkline(dx['series'], fmt="{:.0f}")}
    <div class="cap">Last 6 months · a strong, rising dollar is a headwind for stocks</div>
  </section>

  <section class="card">
    <h2>Yield curve · 10y minus 2y</h2>
    <div class="big">{rt['curve']:+.0f}<small>bps · {rt['date']:%b %d}</small></div>
    {badge(rt['alert'])}
    <table class="kv">
      <tr><td>5-day change</td><td>{signed(rt['c5'], ' bps', 0)}</td></tr>
      <tr><td>20-day change</td><td>{signed(rt['c20'], ' bps', 0)}</td></tr>
      <tr><td>10-year yield</td><td>{rt['y10']:.2f}% ({signed(rt['y10_c20'], ' bps', 0)} in 20d)</td></tr>
      <tr><td>2-year yield</td><td>{rt['y2']:.2f}% ({signed(rt['y2_c20'], ' bps', 0)} in 20d)</td></tr>
    </table>
    {sparkline(rt['series'], line=0, fmt="{:.0f}")}
    <div class="cap">Last ~6 months · dashed line = 0 (inverted below)</div>
  </section>
</div>

{trend_table(tr)}

<section class="card hist">
  <h2>Daily log</h2>
  <table><thead><tr><th>Date</th><th>VIX</th><th>VIX 1d</th><th>Fast drop &lt;25</th>
  <th>% stocks up</th><th>90% day</th><th>HY OAS</th><th>HY status</th><th>DXY</th><th>Curve</th><th>Sectors down</th><th>Rally</th><th>Score</th></tr></thead>
  <tbody>{hist_rows}</tbody></table>
</section>

<p class="notes">
<b>How signals are defined.</b>
<b>Fast drop below 25:</b> VIX closes under {VIX_LINE:.0f}, was at or above {VIX_LINE:.0f} within the last {VIX_LOOKBACK} sessions,
and is down at least {VIX_FAST_DROP_PCT:.0f}% from its {VIX_LOOKBACK}-day high.
<b>90% day:</b> at least {BREADTH_THRUST:.0f}% of S&amp;P 500 members closed up (or down) versus the prior close.
This is a large-cap proxy; the classic Lowry study uses NYSE up-volume and points gained.
<b>Junk spreads:</b> ICE BofA US High Yield option-adjusted spread from FRED (usually one business day behind).
Volatile = 20-day swing size in the top {100 - HY_VOLATILE_PCTILE}% of the past year, or widening of {HY_WIDEN_5D_BPS}+ bps in 5 days or {HY_WIDEN_20D_BPS}+ bps in 20 days.
Calm = swing size below the {HY_CALM_PCTILE}th percentile with little widening.
<b>VIX term structure:</b> VIX divided by the 3-month VIX; above 1 means short-term fear exceeds longer-term fear (panic), and a move back below 1 often comes near bottoms.
<b>Stocks above 50-day average:</b> under {WASHOUT_PCT}% is a washout; the checklist turns on when it's above 50%, or 10+ points up from a washout low.
<b>Sectors:</b> the 11 Select Sector SPDR funds (XLK, XLF, …). 10+ falling together = broad selling; 10+ rising = broad buying.
<b>Failed rally:</b> Nasdaq or S&amp;P 500 up {RALLY_PCT:.0f}%+ at its high but closing lower than the prior day. <b>Reversal up:</b> down {RALLY_PCT:.0f}%+ at its low but closing higher.
<b>Strongest 90% signal:</b> a 90% up day within about 10 sessions of a 90% down day (Lowry's research).
<b>Dollar:</b> ICE US Dollar Index. Reversing = made a 60-day high in the last 20 sessions, now {DXY_REVERSAL_PCT}%+ below it and under its 20-day average.
<b>Yield curve:</b> 10-year minus 2-year Treasury yield from FRED (one business day behind). Flattening/steepening = {CURVE_MOVE_BPS}+ bps move in 20 days; rates stable = 10-year moved less than {RATES_STABLE_BPS} bps in 20 days.
<b>Trend ranges:</b> buy level = {RANGE_EMA}-day exponential average minus {RANGE_BUY_MULT}× the {RANGE_ATR}-day average daily range (ATR);
sell level = the same average plus {RANGE_SELL_MULT}× that range. Upside/downside = distance from today's price to the sell/buy level.
Bullish/bearish = the {TREND_FAST}-day exponential average above/below the {TREND_SLOW}-day; neutral = the price has crossed back over the
{TREND_SLOW}-day against the trend (↑ in a downtrend, yellow; ↓ in an uptrend, orange). These formulas were fitted to a published buy/sell-level
sheet for Nov 24, 2026: most levels within about 1%, trend direction on 40 of 41 symbols. Signal date/price = the close when the current signal began; days are calendar days.
Read = near buy/sell when the price is within {RANGE_NEAR:.0%} of the range from that end.
In the VIX and spread tables, red means rising (more stress) and green means falling.
Not investment advice.
</p>
</main>{FRESH_JS}</body></html>"""


def main():
    print("Fetching VIX…")
    vix = analyze_vix()
    print("Fetching high-yield spreads…")
    hy = analyze_hy()
    print("Fetching ~500 S&P 500 stocks for breadth (about a minute)…")
    br = analyze_breadth()
    print("Fetching dollar and Treasury yields…")
    dx = analyze_dollar()
    rt = analyze_rates()
    print("Fetching sectors and index ranges…")
    pr = analyze_pressure()
    print("Fetching trend-range symbols…")
    tr = analyze_trends()
    checks = checklist(vix, br, hy, dx, rt, pr)
    print("Building macro brief (official data + news)…")
    macro = macro_brief.build(fred_series, vix, br, hy, dx, rt, pr, checks, tr)
    history = append_history(vix, br, hy, dx, rt, pr, checks)
    with open(REPORT, "w") as f:
        f.write(render(vix, br, hy, dx, rt, pr, checks, history, tr, macro))
    with open(os.path.join(HERE, "stamp.txt"), "w") as f:
        f.write(STAMP)

    d1 = vix["d1"]
    print(f"\nVIX  {vix['last']:.2f} ({d1[0]:+.2f}, {d1[1]:+.1f}%)  -> {vix['alert'][0]}")
    print(f"Breadth {br['pct_up']:.1f}% up / {br['pct_down']:.1f}% down ({br['date']:%Y-%m-%d})  -> {br['alert'][0]}")
    print(f"HY OAS {hy['last']:.0f} bps ({hy['c1']:+.0f} 1d, {hy['c20']:+.0f} 20d)  -> {hy['alert'][0]}")
    print(f"Dollar {dx['last']:.2f} ({dx['c1']:+.2f}%)  -> {dx['alert'][0]}")
    print(f"Curve {rt['curve']:+.0f} bps ({rt['c20']:+.0f} 20d), 10y {rt['y10']:.2f}%  -> {rt['alert'][0]}")
    print(f"Pressure {pr['n_down']}/11 sectors down  -> {pr['alert'][0]}")
    print(f"Bottom checklist: {sum(ok for _, ok, _ in checks)}/{len(checks)}")
    for name, ok, detail in checks:
        print(f"  [{'x' if ok else ' '}] {name}: {detail}")
    print(f"\nReport: {REPORT}")


if __name__ == "__main__":
    sys.exit(main())
