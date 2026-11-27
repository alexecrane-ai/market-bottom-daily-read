"""
Daily macro brief
-----------------
1. Latest official US data from FRED (no key needed): inflation, growth, jobs, the Fed rate.
2. If ANTHROPIC_API_KEY is set, Claude searches today's financial news and the economic
   calendar, reads the tracker's own readings, and writes the highlights, a table of recent
   releases with what each print means, and the upcoming events that matter.

The last good brief is kept in macro_brief.json, so a failed call still shows the earlier one.
"""
import html
import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SAVED = os.path.join(HERE, "macro_brief.json")
MODEL = "claude-opus-5-5"

# FRED series: (label, id, how to show it, which way is "hotter")
INDICATORS = [
    ("CPI inflation (y/y)", "CPIAUCSL", "yoy", "up"),
    ("Core CPI (y/y)", "CPILFESL", "yoy", "up"),
    ("PCE inflation (y/y)", "PCEPI", "yoy", "up"),
    ("Core PCE, the Fed's gauge (y/y)", "PCEPILFE", "yoy", "up"),
    ("Real GDP growth (q/q annualized)", "A191RL1Q225SBEA", "level", "up"),
    ("Unemployment rate", "UNRATE", "level", "down"),
    ("Nonfarm payrolls (monthly change, k)", "PAYEMS", "diff", "up"),
    ("Initial jobless claims (weekly, k)", "ICSA", "claims", "down"),
    ("Fed funds target (upper bound)", "DFEDTARU", "level", None),
]


# ---- 1. Official data ---------------------------------------------------------------
def official_data(fred_series):
    rows = []
    for label, sid, kind, hot in INDICATORS:
        try:
            s = fred_series(sid)
            if kind == "yoy":
                s = (s / s.shift(12) - 1) * 100
            elif kind == "diff":
                s = s.diff()
            elif kind == "claims":
                s = s / 1000
            s = s.dropna()
            if sid == "DFEDTARU":   # daily series: report the last change, not yesterday
                changed = s[s.ne(s.shift())]
                prior = changed.iloc[-2] if len(changed) > 1 else s.iloc[-1]
                rows.append({"label": label, "period": changed.index[-1], "value": s.iloc[-1],
                             "prior": prior, "unit": "%", "hot": hot})
                continue
            rows.append({"label": label, "period": s.index[-1], "value": s.iloc[-1],
                         "prior": s.iloc[-2], "unit": "" if kind in ("diff", "claims") else "%",
                         "hot": hot, "kind": kind, "sid": sid})
        except Exception as e:
            print(f"  FRED {sid} failed: {e}")
    return rows


def period_label(r):
    d = r["period"]
    if r.get("sid") == "A191RL1Q225SBEA":
        return f"Q{(d.month - 1) // 3 + 1} {d.year}"
    if r.get("kind") == "claims" or r["label"].startswith("Fed funds"):
        return f"{d:%b %-d, %Y}"
    return f"{d:%b %Y}"


def fmt_val(r, v):
    if r["unit"] == "%":
        return f"{v:.2f}%" if r["label"].startswith("Fed") else f"{v:.1f}%"
    return f"{v:+,.0f}" if r.get("kind") == "diff" else f"{v:,.0f}"


# ---- 2. Claude: news, calendar, highlights ------------------------------------------------
SYSTEM = """You write the daily macro brief at the top of a personal market dashboard for one \
individual investor. Each run you get today's date and the dashboard's own readings (official US \
data from FRED, market-bottom signals, trend signals and buy/sell levels for ETFs and stocks).

Use web search to find, as of today:
- the financial news that moved or is moving markets (the Fed and its speakers, inflation, growth, \
jobs, rates, the dollar, oil, credit, big earnings, policy);
- economic releases from the last few days: the actual print, the consensus expectation and the \
prior, and what the print means;
- the economic calendar for the next 7 days: FOMC meetings and minutes, Fed speakers, CPI, PPI, PCE, \
jobs, GDP, retail sales, ISM, jobless claims, and major earnings, with consensus where published.

Then write plainly, like a strategist's morning note: what happened, why it matters, and how it fits \
what the dashboard shows (for example, a hot inflation print alongside bearish trends in rate-sensitive \
ETFs). Tie points to the dashboard's symbols and levels where they apply. Use only numbers you found \
in a source or in the dashboard data; if a consensus figure wasn't available, write "n/a". Report \
what the data and the dashboard say rather than telling the reader to buy or sell.

Reply with only a JSON object, no other text, in this shape:
{"headline": "one sentence on the day's main macro story",
 "highlights": ["4 to 6 bullets, 1 to 3 sentences each"],
 "releases": [{"date": "Nov 26", "event": "Core PCE (Oct, y/y)", "actual": "2.9%", "expected": "2.8%",
               "prior": "2.8%", "read": "hotter than expected; ..."}],
 "upcoming": [{"date": "Dec 2", "time": "10:00 ET", "event": "ISM Manufacturing (Nov)",
               "expected": "48.5", "why": "..."}]}"""


def dashboard_context(official, vix, br, hy, dx, rt, pr, checks, tr):
    ctx = {
        "official_data": [{"indicator": r["label"], "period": period_label(r),
                           "latest": fmt_val(r, r["value"]), "prior": fmt_val(r, r["prior"])}
                          for r in official],
        "bottom_checklist": {"score": f"{sum(ok for _, ok, _ in checks)}/{len(checks)}",
                             "items": [{"signal": n, "on": bool(ok), "detail": d} for n, ok, d in checks]},
        "vix": {"level": round(float(vix["last"]), 2), "status": vix["alert"][0]},
        "breadth_pct_stocks_up": round(float(br["pct_up"]), 1),
        "high_yield_spread_bps": round(float(hy["last"])), "high_yield_status": hy["alert"][0],
        "dollar_index": round(float(dx["last"]), 2), "dollar_status": dx["alert"][0],
        "curve_10y_2y_bps": round(float(rt["curve"])), "us10y_pct": round(float(rt["y10"]), 2),
        "sectors_down_today": pr["n_down"],
    }
    if tr and tr.get("groups"):
        ctx["trend_ranges"] = {title: [{"symbol": r["sym"], "name": r["name"], "price": round(r["price"], 2),
                                        "buy_level": round(r["buy"], 2), "sell_level": round(r["sell"], 2),
                                        "trend": r["signal"], "since": f"{r['sig_date']:%Y-%m-%d}"}
                                       for r in rows]
                               for title, rows in tr["groups"]}
    return ctx


def ask_claude(context):
    import anthropic
    client = anthropic.Anthropic()
    now = datetime.now()
    user = (f"Today is {now:%A, %B %-d, %Y}, {now:%-I:%M %p} New York time.\n\n"
            f"Dashboard readings:\n{json.dumps(context, default=str)}")
    messages = [{"role": "user", "content": user}]
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 10}]
    sources = {}
    for _ in range(5):   # resume if the server-side search loop pauses
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=16000, system=SYSTEM, messages=messages, tools=tools,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        for block in resp.content:
            if block.type == "web_search_tool_result" and isinstance(block.content, list):
                for res in block.content:
                    if getattr(res, "url", None):
                        sources.setdefault(res.url, getattr(res, "title", res.url))
        if resp.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": user}, {"role": "assistant", "content": resp.content}]
    if resp.stop_reason == "refusal":
        raise RuntimeError("the request was declined")
    text = "".join(b.text for b in resp.content if b.type == "text")
    start, end = text.find("{"), text.rfind("}")
    brief = json.loads(text[start:end + 1])
    brief["sources"] = [{"url": u, "title": t} for u, t in list(sources.items())[:10]]
    brief["generated"] = now.strftime("%Y-%m-%d %H:%M")
    return brief


def build(fred_series, vix, br, hy, dx, rt, pr, checks, tr):
    official = official_data(fred_series)
    brief, note = None, ""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            brief = ask_claude(dashboard_context(official, vix, br, hy, dx, rt, pr, checks, tr))
            with open(SAVED, "w") as f:
                json.dump(brief, f, indent=1)
        except Exception as e:
            print(f"  macro brief failed: {e}")
            note = "Today's update failed; showing the last good brief."
    else:
        note = "News highlights are off: add an ANTHROPIC_API_KEY secret to the repository to turn them on."
    if brief is None and os.path.exists(SAVED):
        with open(SAVED) as f:
            brief = json.load(f)
    return {"official": official, "brief": brief, "note": note}


# ---- HTML ---------------------------------------------------------------------------------
def render(m):
    e = lambda s: html.escape(str(s if s is not None else ""))
    b = m["brief"] or {}
    parts = ['<section class="card macro"><h2>Today\'s macro highlights</h2>']
    if b:
        when = datetime.strptime(b["generated"], "%Y-%m-%d %H:%M") if b.get("generated") else None
        parts.append(f'<p class="mhead">{e(b.get("headline"))}</p>')
        if when:
            parts.append(f'<p class="cap">Written {when:%a %b %-d, %-I:%M %p} ET from web news and this dashboard</p>')
        parts.append("<ul class='mlist'>" + "".join(f"<li>{e(h)}</li>" for h in b.get("highlights", [])) + "</ul>")
    if m["note"]:
        parts.append(f'<p class="why">{e(m["note"])}</p>')

    parts.append('<div class="mgrid">')
    if b.get("releases"):
        rows = "".join(f'<tr><td>{e(r.get("date"))}</td><td class="ev">{e(r.get("event"))}</td>'
                       f'<td><b>{e(r.get("actual"))}</b></td><td>{e(r.get("expected"))}</td>'
                       f'<td>{e(r.get("prior"))}</td><td class="rd">{e(r.get("read"))}</td></tr>'
                       for r in b["releases"])
        parts.append('<div class="mtab wide"><h3>Recent releases</h3><div class="tscroll"><table>'
                     '<tr><th>Date</th><th>Release</th><th>Actual</th><th>Expected</th><th>Prior</th><th>What it means</th></tr>'
                     f'{rows}</table></div></div>')
    if b.get("upcoming"):
        rows = "".join(f'<tr><td>{e(r.get("date"))}</td><td>{e(r.get("time"))}</td>'
                       f'<td class="ev">{e(r.get("event"))}</td><td>{e(r.get("expected"))}</td>'
                       f'<td class="rd">{e(r.get("why"))}</td></tr>' for r in b["upcoming"])
        parts.append('<div class="mtab wide"><h3>Coming up</h3><div class="tscroll"><table>'
                     '<tr><th>Date</th><th>Time</th><th>Event</th><th>Expected</th><th>Why it matters</th></tr>'
                     f'{rows}</table></div></div>')
    if m["official"]:
        def row(r):
            d = r["value"] - r["prior"]
            cls = ""
            if fmt_val(r, r["value"]) == fmt_val(r, r["prior"]):
                d = 0
            if r["hot"] and d:
                hotter = (d > 0) == (r["hot"] == "up")
                cls = "hot" if hotter else "cool"
            word = {"hot": "hotter", "cool": "cooler"}.get(cls, "")
            if r["hot"] == "down" or r.get("kind") in ("diff", "claims") or "GDP" in r["label"]:
                word, cls = {"hot": "stronger", "cool": "weaker"}.get(cls, ""), ""
            if r["hot"] and not d:
                word = "unchanged"
            return (f'<tr><td class="ev">{e(r["label"])}</td><td>{period_label(r)}</td>'
                    f'<td><b>{fmt_val(r, r["value"])}</b></td><td>{fmt_val(r, r["prior"])}</td>'
                    f'<td class="{cls}">{word}</td></tr>')
        parts.append('<div class="mtab"><h3>Latest official data</h3><div class="tscroll"><table>'
                     '<tr><th>Indicator</th><th>Period</th><th>Latest</th><th>Prior</th><th>vs prior</th></tr>'
                     + "".join(row(r) for r in m["official"]) + '</table></div>'
                     '<p class="cap">FRED, Federal Reserve Bank of St. Louis. Inflation: hotter = higher; '
                     'jobs and growth: stronger = better economy.</p></div>')
    parts.append("</div>")
    if b.get("sources"):
        parts.append('<details class="msrc"><summary>Sources</summary><ul>' + "".join(
            f'<li><a href="{e(s["url"])}" rel="noopener">{e(s["title"])}</a></li>' for s in b["sources"])
            + "</ul></details>")
    parts.append("</section>")
    return "".join(parts)


CSS = """
.macro {{ margin-bottom:16px }} .mhead {{ font-size:18px; font-weight:700; margin:8px 0 2px }}
.mlist {{ margin:10px 0 4px; padding-left:20px }} .mlist li {{ margin:6px 0 }}
.mgrid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(420px, 1fr)); gap:8px 24px; margin-top:12px }}
@media (max-width:520px) {{ .mgrid {{ grid-template-columns:1fr }} }}
.mtab {{ min-width:0 }} .mtab.wide {{ grid-column:1 / -1 }} .mtab .tscroll {{ overflow-x:auto }}
.mtab h3 {{ font-size:14px; margin:10px 0 6px }}
.mtab table {{ width:100%; border-collapse:collapse; font-size:13px; font-variant-numeric:tabular-nums }}
.mtab th {{ text-align:left; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap }}
.mtab td {{ padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; vertical-align:top }}
.mtab td.ev {{ font-weight:600 }} .mtab td.rd {{ white-space:normal; min-width:220px; color:var(--muted) }}
.mtab td.hot {{ color:var(--bad); font-weight:600 }} .mtab td.cool {{ color:var(--good); font-weight:600 }}
.msrc {{ margin-top:12px; font-size:13px; color:var(--muted) }} .msrc a {{ color:var(--accent) }}
"""
