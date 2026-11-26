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
7. Correlation - Cboe 1-month implied correlation (COR1M): are stocks moving in lockstep?
Plus a "bottom checklist" that counts how many signals point to a bottom, and an
"entry tranches" panel showing which of three staged-entry triggers are met.

Writes:  report.html   (open in any browser)
         history.csv   (one row per trading day, appended each run)

Data: Yahoo Finance (VIX + S&P 500 member prices), FRED (high-yield spread),
      Cboe (COR1M history).
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
CORR_HIGH_PCTILE = 80      # implied correlation at/above this 1y percentile = lockstep selling
CORR_LOW_PCTILE = 30       # at/below this 1y percentile = stocks moving on their own
CORR_WINDOW = 20           # days for the computed sector-correlation fallback
CORR_TREND_DAYS = 5        # days to judge "correlation falling while prices rise"

# ---- Entry tranche triggers (staged entry plan) -------------------------------
# Tranche 1, capitulation: TRANCHE1_MIN_OF_3 of (VIX/VIX3M < TS_CALM_RATIO, HY OAS 5-day
#   change <= 0, a reversal-up day in the last REVERSAL_LOOKBACK sessions), OR SPY at
#   least TRANCHE1_SPY_OFF_HIGH_PCT below its 1-year closing high.
# Tranche 2, thrust: a 90% up day or VIX fast drop below 25 (last 10 sessions) with the
#   checklist at TRANCHE2_MIN_SCORE+, OR SPY at least TRANCHE2_SPY_OFF_HIGH_PCT below its high.
# Tranche 3, confirmation: more than TRANCHE3_ABOVE50_PCT% of stocks above their 50-day
#   average, junk spreads CALM or NORMAL, dollar reversing, checklist at TRANCHE3_MIN_SCORE+.
TS_CALM_RATIO = 1.0
TRANCHE1_MIN_OF_3 = 2
REVERSAL_LOOKBACK = 5
TRANCHE1_SPY_OFF_HIGH_PCT = 15.0
TRANCHE2_MIN_SCORE = 4
TRANCHE2_SPY_OFF_HIGH_PCT = 20.0
TRANCHE3_ABOVE50_PCT = 50.0
TRANCHE3_MIN_SCORE = 6


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
    reversal_recent = sorted(reversal_days & set(rets.index[-REVERSAL_LOOKBACK:]))
    return {"date": rets.index[-1], "n_down": n_down, "n_up": n_up,
            "best": (SECTORS[best], today[best]), "worst": (SECTORS[worst], today[worst]),
            "broad_down_days_10": int((n_down_series.iloc[-10:] >= 10).sum()),
            "idx": days, "failed_today": failed_today, "reversal_today": reversal_today,
            "failed_days": sorted(failed_days), "reversal_days": sorted(reversal_days),
            "recent_buying": recent_buying, "reversal_recent": reversal_recent, "alert": status}


# ---- 7. Stock correlation ------------------------------------------------------
CBOE_COR1M = "https://cdn.cboe.com/api/global/us_indices/daily_prices/COR1M_History.csv"


def analyze_spy():
    df = yahoo_daily("SPY", "1y")
    s = df["close"]
    hi, hi_date = s.max(), s.idxmax()
    return {"date": s.index[-1], "last": s.iloc[-1], "hi": hi, "hi_date": hi_date,
            "off_high": (s.iloc[-1] / hi - 1) * 100,
            "c5": (s.iloc[-1] / s.iloc[-1 - CORR_TREND_DAYS] - 1) * 100, "series": s}


def cor1m_series():
    """Cboe's daily COR1M history, topped up with Yahoo's live quote when it is newer."""
    df = pd.read_csv(io.StringIO(http_get(CBOE_COR1M)))
    s = pd.Series(pd.to_numeric(df["CLOSE"], errors="coerce").values,
                  index=pd.to_datetime(df["DATE"], format="%m/%d/%Y")).dropna()
    try:
        live = yahoo_daily("^COR1M", "5d")["close"]
        newer = live[live.index > s.index[-1]]
        if len(newer):
            s = pd.concat([s, newer])
    except Exception:
        pass
    return s[s.index >= s.index[-1] - pd.Timedelta(days=400)]


def sector_corr_series():
    """Fallback: average 20-day pairwise correlation of the 11 sector SPDRs, scaled 0-100."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        sec = dict(zip(SECTORS, ex.map(lambda t: yahoo_daily(t, "2y")["close"], SECTORS)))
    rets = pd.DataFrame(sec).pct_change(fill_method=None).dropna()
    n = rets.shape[1]
    vals = {}
    for i in range(CORR_WINDOW, len(rets) + 1):
        m = rets.iloc[i - CORR_WINDOW:i].corr().values
        vals[rets.index[i - 1]] = (m.sum() - n) / (n * (n - 1)) * 100
    s = pd.Series(vals)
    return s[s.index >= s.index[-1] - pd.Timedelta(days=400)]


def analyze_correlation(spy):
    try:
        s = cor1m_series()
        source, implied = "Cboe COR1M (1-month implied correlation)", True
    except Exception:
        s = sector_corr_series()
        source, implied = f"Computed: average {CORR_WINDOW}-day correlation of the 11 sector SPDRs", False

    last = s.iloc[-1]
    yr = s.iloc[-252:]
    pctile = (yr <= last).mean() * 100
    c5 = last - s.iloc[-1 - CORR_TREND_DAYS]
    falling_as_prices_rise = c5 < 0 and spy["c5"] > 0

    if falling_as_prices_rise:
        status = ("FALLING AS PRICES RISE", "good",
                  f"Correlation down {abs(c5):.1f} pts in {CORR_TREND_DAYS} days while SPY rose "
                  f"{spy['c5']:.1f}%: stocks are trading on their own merits again, a typical bottoming sign.")
    elif pctile >= CORR_HIGH_PCTILE:
        status = ("HIGH", "bad",
                  f"Stocks are moving in lockstep, higher than {pctile:.0f}% of the past year: "
                  "indiscriminate selling.")
    elif pctile <= CORR_LOW_PCTILE:
        status = ("LOW", "neutral", "Stocks are moving independently; no sign of panic selling.")
    else:
        status = ("NORMAL", "neutral", "Stocks are neither in lockstep nor unusually independent.")

    return {"date": s.index[-1], "last": last, "pctile": pctile, "c5": c5,
            "c20": last - s.iloc[-21], "yr_low": yr.min(), "yr_high": yr.max(),
            "source": source, "implied": implied, "falling_as_prices_rise": falling_as_prices_rise,
            "alert": status, "series": s.iloc[-120:]}


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


# ---- Entry tranches --------------------------------------------------------------
def entry_tranches(vix, br, hy, dx, pr, spy, checks):
    """Each tranche: (name, short rule, met, [(condition, ok, reading), ...])."""
    score, n = sum(ok for _, ok, _ in checks), len(checks)
    off = spy["off_high"]
    spy_txt = f"{off:+.1f}% vs {spy['hi']:.2f} high ({spy['hi_date']:%b %d})"
    ts = vix["ts_ratio"]

    # Tranche 1 · capitulation
    rev = pr["reversal_recent"]
    t1 = [(f"VIX ÷ 3-month VIX below {TS_CALM_RATIO:.1f}", ts is not None and ts < TS_CALM_RATIO,
           f"{ts:.2f}" if ts is not None else "No data"),
          ("Junk spreads flat or narrower over 5 days", hy["c5"] <= 0, f"{hy['c5']:+.0f} bps in 5 days"),
          (f"Reversal-up day (last {REVERSAL_LOOKBACK} sessions)", bool(rev),
           ", ".join(f"{d:%b %d}" for d in rev) if rev else "None")]
    hits = sum(ok for _, ok, _ in t1)
    t1_back = off <= -TRANCHE1_SPY_OFF_HIGH_PCT
    t1.append((f"Or: SPY {TRANCHE1_SPY_OFF_HIGH_PCT:.0f}%+ below its 1-year high", t1_back, spy_txt))
    t1_met = hits >= TRANCHE1_MIN_OF_3 or t1_back

    # Tranche 2 · thrust
    fd = vix["recent_fast_drops"]
    thrust = br["up90_last10"] or bool(fd)
    if br["up90_last10"]:
        thrust_txt = "90% up day in last 10 sessions"
    elif fd:
        thrust_txt = f"VIX fast drop below 25 on {fd[-1]:%b %d}" + (
            f", now back above ({vix['last']:.1f})" if vix["last"] >= VIX_LINE else "")
    else:
        thrust_txt = f"Neither yet ({br['pct_up']:.0f}% up today, VIX {vix['last']:.1f})"
    t2_back = off <= -TRANCHE2_SPY_OFF_HIGH_PCT
    t2 = [("A 90% up day or VIX fast drop below 25 (last 10 sessions)", thrust, thrust_txt),
          (f"Bottom checklist {TRANCHE2_MIN_SCORE}/8 or higher", score >= TRANCHE2_MIN_SCORE, f"{score}/{n}"),
          (f"Or: SPY {TRANCHE2_SPY_OFF_HIGH_PCT:.0f}%+ below its 1-year high", t2_back, spy_txt)]
    t2_met = (thrust and score >= TRANCHE2_MIN_SCORE) or t2_back

    # Tranche 3 · confirmation
    t3 = [(f"More than {TRANCHE3_ABOVE50_PCT:.0f}% of stocks above 50-day average",
           br["pct_above50"] > TRANCHE3_ABOVE50_PCT, f"{br['pct_above50']:.0f}%"),
          ("Junk spreads Calm or Normal", hy["alert"][0] in ("CALM", "NORMAL"), hy["alert"][0].title()),
          ("Dollar reversing lower", dx["ok"], dx["alert"][0].title()),
          (f"Bottom checklist {TRANCHE3_MIN_SCORE}/8 or higher", score >= TRANCHE3_MIN_SCORE, f"{score}/{n}")]
    t3_met = all(ok for _, ok, _ in t3)

    return [
        ("Tranche 1 · Capitulation",
         f"{TRANCHE1_MIN_OF_3} of the first 3, or SPY {TRANCHE1_SPY_OFF_HIGH_PCT:.0f}% off its high. "
         "Panic is peaking and starting to ease.", t1_met, t1),
        ("Tranche 2 · Thrust",
         f"A buying thrust with the checklist at {TRANCHE2_MIN_SCORE}+, or SPY "
         f"{TRANCHE2_SPY_OFF_HIGH_PCT:.0f}% off its high. Buyers are coming back in force.", t2_met, t2),
        ("Tranche 3 · Confirmation",
         "All four. The recovery is broad and the backdrop has calmed.", t3_met, t3),
    ]


# ---- History log -------------------------------------------------------------
def append_history(vix, br, hy, dx, rt, pr, checks, cor, spy, tr):
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
        "corr_date": f"{cor['date']:%Y-%m-%d}",
        "corr": round(cor["last"], 2),
        "corr_pctile_1y": round(cor["pctile"]),
        "corr_source": "COR1M" if cor["implied"] else "sector_20d",
        "spy_off_1y_high_pct": round(spy["off_high"], 1),
        "tranche1_capitulation_met": tr[0][2],
        "tranche2_thrust_met": tr[1][2],
        "tranche3_confirmation_met": tr[2][2],
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


def render(vix, br, hy, dx, rt, pr, checks, history, cor, spy, tr):
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
    tranche_cards = "".join(
        f'<div class="tranche {"met" if met else ""}"><h3>{name}</h3>'
        f'<div class="badge {"good" if met else "neutral"}">{"MET" if met else "NOT MET"}</div>'
        f'<p class="why">{rule}</p><ul>' + "".join(
            f'<li class="{"on" if ok else "off"}"><span class="mark">{"✓" if ok else "✗"}</span>'
            f'<span class="cname">{c}</span><span class="cdet">{d}</span></li>' for c, ok, d in conds)
        + '</ul></div>'
        for name, rule, met, conds in tr)
    n_met = sum(met for _, _, met, _ in tr)
    tmark = lambda r: " ".join(
        str(i) if r.get(k) == "True" else "–" for i, k in enumerate(
            ("tranche1_capitulation_met", "tranche2_thrust_met", "tranche3_confirmation_met"), 1)
    ) if r.get("tranche1_capitulation_met") else ""
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
        f"<td>{r.get('bottom_score') or ''}</td><td>{r.get('corr') or ''}</td><td>{tmark(r)}</td></tr>"
        for r in reversed(history[-30:]))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Bottom Daily Read</title>
<meta name="apple-mobile-web-app-title" content="Market Read">
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --ink:#15181d; --muted:#667085; --line:#e4e7ec;
  --good:#0f8a5f; --good-bg:#e3f6ee; --bad:#c2352b; --bad-bg:#fbe9e7;
  --neu:#475467; --neu-bg:#eef1f5; --accent:#3559e0; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#181b21; --ink:#e8eaee;
  --muted:#98a2b3; --line:#2a2f38; --good:#3ecf8e; --good-bg:#12302a; --bad:#ff6b5e;
  --bad-bg:#3a1a18; --neu:#c0c7d2; --neu-bg:#232832; --accent:#7c9bff; }} }}
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
.tranches {{ margin-bottom:16px }}
.tranches .tsub {{ color:var(--muted); margin:6px 0 12px }}
.tgrid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:16px }}
.tranche {{ border:1px solid var(--line); border-radius:12px; padding:14px 16px }}
.tranche.met {{ border-color:var(--good) }}
.tranche h3 {{ margin:0; font-size:16px }}
.tranche .why {{ margin:8px 0 6px; font-size:13px }}
.tranche ul {{ list-style:none; margin:0; padding:0 }}
.tranche li {{ display:grid; grid-template-columns:18px 1fr; column-gap:8px; padding:7px 0; border-top:1px solid var(--line) }}
.tranche .mark {{ font-weight:700; grid-row:span 2 }} .tranche li.on .mark {{ color:var(--good) }} .tranche li.off .mark {{ color:var(--bad) }}
.tranche .cname {{ font-weight:600; font-size:14px }} .tranche .cdet {{ color:var(--muted); font-size:13px }}
</style></head><body><main>
<h1>Market Bottom Daily Read</h1>
<p class="sub">Updated {datetime.now():%a %b %d, %Y · %I:%M %p} ET ·
<a href="{REFRESH_URL}">Refresh now ↻</a></p>

<section class="card check">
  <h2>Bottom checklist</h2>
  <div class="score">{score} of {len(checks)}<small> signals point to a bottom</small></div>
  <ul>{check_rows}</ul>
</section>

<section class="card tranches">
  <h2>Entry tranches</h2>
  <div class="score">{n_met} of 3<small> staged-entry triggers met</small></div>
  <p class="tsub">Each tranche is checked on its own, every run. ✓ = condition met right now.</p>
  <div class="tgrid">{tranche_cards}</div>
</section>

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

  <section class="card">
    <h2>Stock correlation · {"COR1M" if cor['implied'] else "sector average"}</h2>
    <div class="big">{cor['last']:.1f}<small>{cor['pctile']:.0f}th pctile (1y) · {cor['date']:%b %d}</small></div>
    {badge(cor['alert'])}
    <table class="kv">
      <tr><td>{CORR_TREND_DAYS}-day change</td><td>{signed(cor['c5'], ' pts', 1)}</td></tr>
      <tr><td>20-day change</td><td>{signed(cor['c20'], ' pts', 1)}</td></tr>
      <tr><td>1-year range</td><td>{cor['yr_low']:.1f} – {cor['yr_high']:.1f}</td></tr>
      <tr><td>SPY {CORR_TREND_DAYS}-day change</td><td><span class="{'down' if spy['c5'] > 0 else 'up'}">{spy['c5']:+.1f}%</span></td></tr>
      <tr><td>SPY vs 1-year high</td><td>{spy['off_high']:+.1f}% ({spy['hi']:.2f}, {spy['hi_date']:%b %d})</td></tr>
    </table>
    {sparkline(cor['series'], fmt="{:.0f}")}
    <div class="cap">Last ~6 months · high = stocks moving together (indiscriminate selling).
    Correlation falling while prices rise is a typical bottoming sign.</div>
  </section>
</div>

<section class="card hist">
  <h2>Daily log</h2>
  <table><thead><tr><th>Date</th><th>VIX</th><th>VIX 1d</th><th>Fast drop &lt;25</th>
  <th>% stocks up</th><th>90% day</th><th>HY OAS</th><th>HY status</th><th>DXY</th><th>Curve</th><th>Sectors down</th><th>Rally</th><th>Score</th><th>Corr</th><th>Tranches</th></tr></thead>
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
<b>Stock correlation:</b> {cor['source']}. It measures how much S&amp;P 500 stocks are expected to move together over the next month (0–100).
High (top {100 - CORR_HIGH_PCTILE}% of the past year) means indiscriminate selling: everything is sold regardless of quality.
Correlation falling over {CORR_TREND_DAYS} days while SPY rises means stocks are trading on their own merits again, which typically happens near bottoms.
If Cboe's data is unavailable, the tracker falls back to the average {CORR_WINDOW}-day pairwise correlation of the 11 sector SPDRs.
<b>Entry tranches:</b> three staged-entry triggers, each checked independently on every run.
Tranche 1 (capitulation): {TRANCHE1_MIN_OF_3} of VIX ÷ 3-month VIX below {TS_CALM_RATIO:.1f}, junk spreads flat or narrower over 5 days, and a reversal-up day in the last {REVERSAL_LOOKBACK} sessions; or SPY {TRANCHE1_SPY_OFF_HIGH_PCT:.0f}%+ below its 1-year closing high.
Tranche 2 (thrust): a 90% up day or VIX fast drop below 25 in the last 10 sessions with the checklist at {TRANCHE2_MIN_SCORE}+; or SPY {TRANCHE2_SPY_OFF_HIGH_PCT:.0f}%+ below its 1-year high.
Tranche 3 (confirmation): more than {TRANCHE3_ABOVE50_PCT:.0f}% of stocks above their 50-day average, junk spreads Calm or Normal, the dollar Reversing or Falling, and the checklist at {TRANCHE3_MIN_SCORE}+.
In the log, "1 2 –" means tranches 1 and 2 were met that day.
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
    print("Fetching SPY and stock correlation…")
    spy = analyze_spy()
    cor = analyze_correlation(spy)
    checks = checklist(vix, br, hy, dx, rt, pr)
    tr = entry_tranches(vix, br, hy, dx, pr, spy, checks)
    history = append_history(vix, br, hy, dx, rt, pr, checks, cor, spy, tr)
    with open(REPORT, "w") as f:
        f.write(render(vix, br, hy, dx, rt, pr, checks, history, cor, spy, tr))
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
    print(f"Correlation {cor['last']:.1f} ({cor['pctile']:.0f}th pctile, {cor['c5']:+.1f} 5d) "
          f"[{'COR1M' if cor['implied'] else 'sector fallback'}]  -> {cor['alert'][0]}")
    print(f"SPY {spy['off_high']:+.1f}% vs 1-year high")
    for name, _, met, conds in tr:
        print(f"{name}: {'MET' if met else 'not met'}")
        for c, ok, d in conds:
            print(f"  [{'x' if ok else ' '}] {c}: {d}")
    print(f"\nReport: {REPORT}")


if __name__ == "__main__":
    sys.exit(main())
