#!/usr/bin/env python3
"""
Daily Market Tracker
--------------------
1. VIX      - level, daily/weekly/monthly moves, and a "FAST DROP BELOW 25" alert.
2. Breadth  - % of S&P 500 stocks up on the day; flags 90% upside (and downside) days.
3. Junk     - ICE BofA US High Yield OAS: is it CALM, NORMAL or VOLATILE?

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
    df = pd.DataFrame({"close": q.get("close"), "volume": q.get("volume")}, index=idx)
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    # Yahoo's daily bars often lag a session; the meta block has the latest close.
    meta = data["meta"]
    if meta.get("regularMarketTime") and meta.get("regularMarketPrice") is not None:
        mday = (pd.Timestamp(meta["regularMarketTime"], unit="s", tz="UTC")
                .tz_convert(tz).normalize().tz_localize(None))
        if df.empty or mday > df.index[-1]:
            df.loc[mday] = [meta["regularMarketPrice"], meta.get("regularMarketVolume")]
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


# ---- History log -------------------------------------------------------------
def append_history(vix, br, hy):
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


def render(vix, br, hy, history):
    d1, d5, d20 = vix["d1"], vix["d5"], vix["d20"]
    cross = f"{vix['cross_date']:%b %d, %Y}" if vix["cross_date"] is not None else "–"
    fmt_days = lambda ds: ", ".join(f"{d:%b %d}" for d in ds[-8:]) or "none"

    hist_rows = "".join(
        f"<tr><td>{r['vix_date']}</td><td>{r['vix']}</td><td>{r['vix_chg_1d']}</td>"
        f"<td>{'✔' if r['vix_fast_drop_below_25'] == 'True' else ''}</td>"
        f"<td>{r['pct_stocks_up']}%</td>"
        f"<td>{'▲ 90%' if r['ninety_pct_up_day'] == 'True' else ('▼ 90%' if r['ninety_pct_down_day'] == 'True' else '')}</td>"
        f"<td>{r['hy_oas_bps']}</td><td>{r['hy_status']}</td></tr>"
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
.breadth .up {{ color:var(--good) }}
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
.notes {{ color:var(--muted); font-size:13px; margin-top:20px }}
</style></head><body><main>
<h1>Market Bottom Daily Read</h1>
<p class="sub">Updated {datetime.now():%a %b %d, %Y · %I:%M %p} ET ·
<a href="{REFRESH_URL}">Refresh now ↻</a></p>

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
      <tr><td>90% down days (last 60)</td><td>{len(br['down90_days'])} — {fmt_days(br['down90_days'])}</td></tr>
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
</div>

<section class="card hist">
  <h2>Daily log</h2>
  <table><thead><tr><th>Date</th><th>VIX</th><th>VIX 1d</th><th>Fast drop &lt;25</th>
  <th>% stocks up</th><th>90% day</th><th>HY OAS</th><th>HY status</th></tr></thead>
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
In the VIX and spread tables, red means rising (more stress) and green means falling.
Not investment advice.
</p>
</main></body></html>"""


def main():
    print("Fetching VIX…")
    vix = analyze_vix()
    print("Fetching high-yield spreads…")
    hy = analyze_hy()
    print("Fetching ~500 S&P 500 stocks for breadth (about a minute)…")
    br = analyze_breadth()
    history = append_history(vix, br, hy)
    with open(REPORT, "w") as f:
        f.write(render(vix, br, hy, history))

    d1 = vix["d1"]
    print(f"\nVIX  {vix['last']:.2f} ({d1[0]:+.2f}, {d1[1]:+.1f}%)  -> {vix['alert'][0]}")
    print(f"Breadth {br['pct_up']:.1f}% up / {br['pct_down']:.1f}% down ({br['date']:%Y-%m-%d})  -> {br['alert'][0]}")
    print(f"HY OAS {hy['last']:.0f} bps ({hy['c1']:+.0f} 1d, {hy['c20']:+.0f} 20d)  -> {hy['alert'][0]}")
    print(f"\nReport: {REPORT}")


if __name__ == "__main__":
    sys.exit(main())
