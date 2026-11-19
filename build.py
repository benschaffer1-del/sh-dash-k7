"""US Financial System Health dashboard.

Pulls free public data (FRED from the St. Louis Fed, prices from Yahoo Finance
with Stooq as a backup), scores the system, and writes a static web page to
./site. Uses only Python's standard library: nothing to install.

Run locally:            python build.py
Run with sample data:   MOCK=1 python build.py
"""
import csv, datetime as dt, html, io, json, math, os, random, struct, time, urllib.parse, urllib.request, zlib

OUT = "site"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()
MOCK = os.environ.get("MOCK") == "1"
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "")
UA = {"User-Agent": "Mozilla/5.0 (personal dashboard; contact via GitHub)"}

# --------------------------------------------------------------------------- data access

def http(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))
    raise err


def fred(series_id, days=1200):
    """List of (date, float) ascending. Empty list on failure."""
    if MOCK:
        return mock_fred(series_id, days)
    start = (TODAY - dt.timedelta(days=days)).isoformat()
    try:
        if FRED_KEY:
            q = urllib.parse.urlencode({"series_id": series_id, "api_key": FRED_KEY,
                                        "file_type": "json", "observation_start": start})
            obs = json.loads(http("https://api.stlouisfed.org/fred/series/observations?" + q))["observations"]
            rows = [(o["date"], o["value"]) for o in obs]
        else:
            text = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}")
            rows = list(csv.reader(io.StringIO(text)))[1:]
        out = []
        for d, v in rows:
            try:
                out.append((dt.date.fromisoformat(d), float(v)))
            except ValueError:
                pass
        return out
    except Exception as e:
        print(f"FRED {series_id} failed: {e}")
        return []


STOOQ = {"GC=F": "xauusd", "SI=F": "xagusd", "BTC-USD": "btcusd"}

def prices(sym):
    """dict(price, prev, closes[list], high52) or None."""
    if MOCK:
        return mock_prices(sym)
    try:
        j = json.loads(http(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1y&interval=1d"))
        r = j["chart"]["result"][0]; m = r["meta"]
        q = r["indicators"]["quote"][0]
        pairs = [(t, c, h) for t, c, h in zip(r["timestamp"], q["close"], q["high"]) if c is not None]
        closes = [c for _, c, _ in pairs]
        price = m.get("regularMarketPrice") or closes[-1]
        last_day = dt.datetime.fromtimestamp(pairs[-1][0], dt.timezone.utc).date()
        mkt_day = dt.datetime.fromtimestamp(m.get("regularMarketTime", pairs[-1][0]), dt.timezone.utc).date()
        prev = closes[-2] if last_day == mkt_day and len(closes) > 1 else closes[-1]
        hi = m.get("fiftyTwoWeekHigh") or max(h for _, _, h in pairs if h is not None)
        return {"price": price, "prev": prev, "closes": closes, "high52": max(hi, price), "src": "Yahoo Finance"}
    except Exception as e:
        print(f"Yahoo {sym} failed: {e}; trying Stooq")
    try:
        s = STOOQ.get(sym, sym.lower().replace("-", ".") + ".us")
        rows = list(csv.DictReader(io.StringIO(http(f"https://stooq.com/q/d/l/?s={s}&i=d"))))
        rows = [r for r in rows if r.get("Close") not in (None, "", "N/D")][-260:]
        closes = [float(r["Close"]) for r in rows]
        return {"price": closes[-1], "prev": closes[-2], "closes": closes,
                "high52": max(float(r["High"]) for r in rows), "src": "Stooq (end of day)"}
    except Exception as e:
        print(f"Stooq {sym} failed: {e}")
        return None

# --------------------------------------------------------------------------- sample data for testing

def mock_fred(sid, days):
    random.seed(sid)
    base = {"DGS10": 5.1, "DGS30": 5.45, "DFII10": 2.85, "T10Y3M": 0.8, "SOFR": 4.33, "IORB": 4.40,
            "BAMLH0A0HYM2": 3.6, "BAMLC0A0CM": 1.05, "BAMLH0A3HYC": 9.4, "DRTSCILM": 12.0, "NFCI": -0.42,
            "STLFSI4": -0.3, "VIXCLS": 21.0, "DTWEXBGS": 128.0, "DCOILBRENTEU": 92.0, "DFF": 4.33,
            "WALCL": 6.5e6, "SAHMREALTIME": 0.37, "ICSA": 241000, "CCSA": 1.97e6, "U6RATE": 8.6,
            "PSAVERT": 4.1, "DRCCLACBS": 3.2, "T10YIE": 2.27, "CPIAUCSL": 330.0, "A091RC1Q027SBEA": 1250.0,
            "FGRECPT": 5400.0, "FYFSGDA188S": -6.4, "GFDEGDQ188S": 121.0}[sid]
    step = {"WALCL": 7, "ICSA": 7, "CCSA": 7, "NFCI": 7, "STLFSI4": 7, "U6RATE": 30, "PSAVERT": 30,
            "SAHMREALTIME": 30, "CPIAUCSL": 30, "DRCCLACBS": 91, "DRTSCILM": 91, "A091RC1Q027SBEA": 91,
            "FGRECPT": 91, "GFDEGDQ188S": 91, "FYFSGDA188S": 365}.get(sid, 1)
    out, v = [], base
    n = days // step
    for i in range(n):
        d = TODAY - dt.timedelta(days=(n - i) * step)
        v = v + random.gauss(0, abs(base) * 0.004 + 0.002) if sid != "CPIAUCSL" else base * (1 - 0.0025 * (n - i))
        out.append((d, v))
    out[-1] = (out[-1][0], base)
    return out


def mock_prices(sym):
    random.seed(sym)
    p = {"GC=F": 3850, "SI=F": 47, "BTC-USD": 64500}.get(sym, random.uniform(30, 700))
    closes, x = [], p * random.uniform(0.85, 1.15)
    for _ in range(252):
        x *= 1 + random.gauss(0.0002, 0.015); closes.append(x)
    k = p / closes[-1]; closes = [c * k for c in closes]
    return {"price": p, "prev": closes[-2], "closes": closes, "high52": max(closes) * 1.02, "src": "sample"}

# --------------------------------------------------------------------------- helpers

def last(s):
    return s[-1] if s else (None, None)

def at_or_before(s, d):
    v = None
    for dd, vv in s:
        if dd <= d: v = vv
        else: break
    return v

def max_since(s, d):
    vals = [v for dd, v in s if dd >= d]
    return max(vals) if vals else None

def num(x):
    return isinstance(x, (int, float)) and not math.isnan(x)

# --------------------------------------------------------------------------- system metrics
# (category, name, fred ids, value fn, unit, direction, yellow, red, weight, meaning)
# direction "up" = higher is worse.

def chg(sid, days=28, scale=1.0):
    def f(S):
        s = S[sid]; d, v = last(s)
        if d is None: return None
        p = at_or_before(s, d - dt.timedelta(days=days))
        return (v - p) * scale if num(p) else None
    return f

def latest(sid, scale=1.0):
    return lambda S: (last(S[sid])[1] * scale) if S[sid] else None

def ratio(a, b):
    return lambda S: (last(S[a])[1] / last(S[b])[1]) if S[a] and S[b] else None

def spread_bp(a, b):
    return lambda S: (last(S[a])[1] - last(S[b])[1]) * 100 if S[a] and S[b] else None

def pct_vs_days_ago(sid, days):
    def f(S):
        s = S[sid]; d, v = last(s)
        if d is None: return None
        p = at_or_before(s, d - dt.timedelta(days=days))
        return v / p - 1 if num(p) else None
    return f

def cpi_6m(S):
    s = S["CPIAUCSL"]; d, v = last(s)
    if d is None: return None
    p = at_or_before(s, d - dt.timedelta(days=170))
    return (v / p) ** 2 - 1 if num(p) else None

METRICS = [
    ("Rates & Treasuries", "10-yr Treasury yield", ["DGS10"], latest("DGS10"), "%", "up", 4.5, 5.0, 2, chg("DGS10"), "Cost of money for Washington and mortgages."),
    ("Rates & Treasuries", "30-yr Treasury yield", ["DGS30"], latest("DGS30"), "%", "up", 4.75, 5.25, 1, chg("DGS30"), "Long-end stress: buyers demanding more to hold US debt."),
    ("Rates & Treasuries", "10-yr real yield (TIPS)", ["DFII10"], latest("DFII10"), "%", "up", 2.0, 2.5, 2, chg("DFII10"), "How tight money is after inflation."),
    ("Rates & Treasuries", "10-yr minus 3-mo curve", ["T10Y3M"], latest("T10Y3M"), "pts", "down", 0.5, 0.0, 1, chg("T10Y3M"), "Below 0 (inverted) has come before recessions."),
    ("Rates & Treasuries", "SOFR minus IORB", ["SOFR", "IORB"], spread_bp("SOFR", "IORB"), "bp", "up", 0, 5, 2, None, "Above 0 means banks are short of cash: plumbing stress."),
    ("Credit", "High-yield bond spread", ["BAMLH0A0HYM2"], latest("BAMLH0A0HYM2"), "%", "up", 4.0, 5.0, 2, chg("BAMLH0A0HYM2"), "Fear in risky corporate debt. The key crash tell."),
    ("Credit", "Investment-grade spread", ["BAMLC0A0CM"], latest("BAMLC0A0CM"), "%", "up", 1.3, 1.8, 1, chg("BAMLC0A0CM"), "Stress in blue-chip corporate debt."),
    ("Credit", "CCC junk spread", ["BAMLH0A3HYC"], latest("BAMLH0A3HYC"), "%", "up", 9, 12, 1, chg("BAMLH0A3HYC"), "Weakest borrowers. Cracks show here first."),
    ("Credit", "Banks tightening loans", ["DRTSCILM"], latest("DRTSCILM"), "net %", "up", 10, 25, 1, None, "Fed loan officer survey. Credit crunch gauge."),
    ("Conditions", "Chicago Fed financial conditions", ["NFCI"], latest("NFCI"), "", "up", -0.3, 0.0, 2, chg("NFCI"), "Above 0 = tighter than average."),
    ("Conditions", "St. Louis Fed stress index", ["STLFSI4"], latest("STLFSI4"), "", "up", 0.0, 1.0, 2, chg("STLFSI4"), "Above 0 = above-normal stress."),
    ("Conditions", "VIX", ["VIXCLS"], latest("VIXCLS"), "", "up", 20, 27, 2, chg("VIXCLS"), "Fear gauge for stocks."),
    ("Dollar & oil", "Broad dollar, 3-mo change", ["DTWEXBGS"], pct_vs_days_ago("DTWEXBGS", 90), "pct", "up", 0.02, 0.04, 1, None, "A fast-rising dollar squeezes global dollar borrowers."),
    ("Dollar & oil", "Brent crude", ["DCOILBRENTEU"], latest("DCOILBRENTEU"), "$", "up", 85, 100, 1, chg("DCOILBRENTEU"), "War and supply strain. Feeds inflation."),
    ("Labor", "Sahm rule", ["SAHMREALTIME"], latest("SAHMREALTIME"), "", "up", 0.3, 0.5, 2, chg("SAHMREALTIME", 35), "0.5 or higher has historically meant recession."),
    ("Labor", "Initial jobless claims", ["ICSA"], latest("ICSA", 0.001), "k", "up", 250, 300, 1, chg("ICSA", 28, 0.001), "Weekly layoffs."),
    ("Labor", "Continuing claims", ["CCSA"], latest("CCSA", 1e-6), "M", "up", 1.95, 2.1, 1, chg("CCSA", 28, 1e-6), "How hard it is to land the next job."),
    ("Labor", "U-6 underemployment", ["U6RATE"], latest("U6RATE"), "%", "up", 8.5, 10, 1, chg("U6RATE", 35), "Broad slack, including part-timers who want more work."),
    ("Households", "Personal savings rate", ["PSAVERT"], latest("PSAVERT"), "%", "down", 4.5, 3.5, 1, chg("PSAVERT", 35), "Low = consumers running out of cushion."),
    ("Households", "Credit card delinquency", ["DRCCLACBS"], latest("DRCCLACBS"), "%", "up", 3.0, 4.0, 1, None, "Bank card loans 30+ days late (quarterly)."),
    ("Inflation", "CPI, 6-mo annualized", ["CPIAUCSL"], cpi_6m, "pct", "up", 0.03, 0.04, 1, None, "Official inflation, recent pace."),
    ("Inflation", "10-yr breakeven inflation", ["T10YIE"], latest("T10YIE"), "%", "up", 2.5, 3.0, 1, chg("T10YIE"), "What bonds expect inflation to average."),
    ("Fiscal", "Federal interest / receipts", ["A091RC1Q027SBEA", "FGRECPT"], ratio("A091RC1Q027SBEA", "FGRECPT"), "pct", "up", 0.15, 0.20, 1, None, "Share of tax revenue eaten by interest (Gromen's core point)."),
    ("Fiscal", "Federal deficit, % of GDP", ["FYFSGDA188S"], latest("FYFSGDA188S"), "%", "down", -3, -6, 1, None, "Annual, fiscal year. Negative = deficit."),
    ("Fiscal", "Federal debt, % of GDP", ["GFDEGDQ188S"], latest("GFDEGDQ188S"), "%", "up", 100, 115, 1, None, "Total public debt vs the economy."),
]
EXTRA = ["DFF", "WALCL"]

def status(v, direction, y, r):
    if not num(v): return "NODATA"
    if direction == "up":
        return "RED" if v >= r else "YELLOW" if v >= y else "GREEN"
    return "RED" if v <= r else "YELLOW" if v <= y else "GREEN"

def fmt(v, unit, signed=False):
    if not num(v): return "n/a"
    sg = "+" if signed and v > 0 else ""
    if unit == "pct": return f"{sg}{v*100:.1f}%"
    if unit == "%": return f"{sg}{v:.2f}%"
    if unit == "$": return f"{sg}${v:,.2f}"
    if unit == "bp": return f"{sg}{v:.0f} bp"
    if unit == "k": return f"{sg}{v:,.0f}k"
    if unit == "M": return f"{sg}{v:.2f}M"
    if unit == "net %": return f"{sg}{v:.1f}%"
    if unit == "pts": return f"{sg}{v:.2f}"
    return f"{sg}{v:.2f}"

# --------------------------------------------------------------------------- watchlist logic

def setup_trend(p):
    c = p["closes"]; price = p["price"]
    a20 = sum(c[-20:]) / len(c[-20:])
    a200 = sum(c[-200:]) / len(c[-200:])
    lo, hi = min(c[-20:] + [price]), max(c[-20:] + [price])
    rng = (price - lo) / (hi - lo) if hi > lo else 0.5
    vs20 = price / a20 - 1
    trend = "Uptrend" if price >= a200 else "Downtrend"
    if rng <= 0.2 or vs20 <= -0.05: setup = "Oversold"
    elif rng >= 0.85 and vs20 >= 0.04: setup = "Overbought"
    else: setup = "Neutral"
    return dict(a20=a20, a200=a200, rng=rng, vs20=vs20, trend=trend, setup=setup,
                off_high=price / p["high52"] - 1, day=price / p["prev"] - 1 if p["prev"] else None,
                m1=price / c[-22] - 1 if len(c) > 22 else None)

def signal(regime, setup, trend):
    """Returns (label, tone, size). Tone: green, lgreen, yellow, red, grey."""
    if regime == "NO DATA": return ("Awaiting system data", "grey", "None")
    if regime == "RED":
        if setup == "Oversold": return ("Wait: falling knife", "yellow", "None")
        if setup == "Overbought": return ("Trim / raise cash", "red", "None")
        return ("No new buys", "red", "None")
    if regime == "YELLOW":
        if setup == "Oversold":
            return ("Nibble (1/3 size)", "lgreen", "1/3 of planned") if trend == "Uptrend" else ("Watch", "yellow", "None")
        if setup == "Overbought": return ("Trim", "red", "None")
        return ("Hold", "yellow", "None")
    if setup == "Oversold":
        return ("Buy dip", "green", "Full planned size") if trend == "Uptrend" else ("Nibble (1/3 size)", "lgreen", "1/3 of planned")
    if setup == "Overbought": return ("Hold, don't chase", "yellow", "None")
    return ("Hold / add", "green", "Up to 1/2 planned") if trend == "Uptrend" else ("Hold", "yellow", "None")

# --------------------------------------------------------------------------- build

def build():
    ids = sorted({i for m in METRICS for i in m[2]} | set(EXTRA))
    S = {i: fred(i) for i in ids}

    rows, pts, maxpts, nodata = [], 0, 0, 0
    for cat, name, sids, vf, unit, direction, y, r, w, df, meaning in METRICS:
        v = vf(S)
        st = status(v, direction, y, r)
        asof = last(S[sids[0]])[0]
        d = df(S) if df else None
        spark = [vv for dd, vv in S[sids[0]] if dd >= (asof or TODAY) - dt.timedelta(days=365)] if asof else []
        if st == "NODATA": nodata += 1
        else:
            pts += w * {"GREEN": 0, "YELLOW": 1, "RED": 2}[st]; maxpts += 2 * w
        rows.append(dict(cat=cat, name=name, v=v, unit=unit, st=st, asof=asof, d=d, y=y, r=r,
                         direction=direction, meaning=meaning, spark=spark, sid=sids[0]))
    score = pts / maxpts if maxpts else None
    by = {x["name"]: x for x in rows}
    if nodata > len(rows) * 0.4 or score is None: calc = "NO DATA"
    elif (num(by["Sahm rule"]["v"]) and by["Sahm rule"]["v"] >= 0.5) or (num(by["High-yield bond spread"]["v"]) and by["High-yield bond spread"]["v"] >= 6): calc = "RED"
    else: calc = "RED" if score >= 0.55 else "YELLOW" if score >= 0.35 else "GREEN"
    override = ""
    if os.path.exists("regime_override.txt"):
        for line in open("regime_override.txt"):
            line = line.strip().upper()
            if line in ("GREEN", "YELLOW", "RED"): override = line
    regime = override or calc

    # pivot watch
    def pv_val(fn):
        try: return fn()
        except Exception: return None
    dff, walcl, dxy, hy, t10 = S["DFF"], S["WALCL"], S["DTWEXBGS"], S["BAMLH0A0HYM2"], S["DGS10"]
    def ago(s, n):
        d, v = last(s); p = at_or_before(s, d - dt.timedelta(days=n)); return v, p, d
    pivots = []
    v = pv_val(lambda: (lambda a: a[0] - a[1])(ago(dff, 90)))
    pivots.append(("Fed has started cutting", fmt(v, "pts", True) + " pts vs 90 days ago", num(v) and v <= -0.25, "Fed funds down 0.25+"))
    v = pv_val(lambda: (lambda a: a[0] / a[1] - 1)(ago(walcl, 90)))
    pivots.append(("Fed balance sheet growing", fmt(v, "pct", True) + " vs 90 days ago", num(v) and v >= 0.01, "Up 1%+"))
    v = pv_val(lambda: last(dxy)[1] / max_since(dxy, last(dxy)[0] - dt.timedelta(days=90)) - 1)
    pivots.append(("Dollar rolling over", fmt(v, "pct", True) + " vs 90-day high", num(v) and v <= -0.03, "3%+ off its high"))
    hmax = pv_val(lambda: max_since(hy, last(hy)[0] - dt.timedelta(days=120)))
    v = pv_val(lambda: hmax - last(hy)[1])
    pivots.append(("Junk spreads peaked and easing", fmt(v, "pts") + " pts off 120-day high", num(v) and num(hmax) and hmax >= 5 and v >= 0.75, "Peak 5%+, then down 0.75+"))
    v = pv_val(lambda: max_since(t10, last(t10)[0] - dt.timedelta(days=90)) - last(t10)[1])
    pivots.append(("10-yr yield off its highs", fmt(v, "pts") + " pts below 90-day high", num(v) and v >= 0.4, "0.40+ below high"))
    npiv = sum(1 for p in pivots if p[2])
    verdict = ("Pivot confirmed: deploy dry powder in tranches", "green") if npiv >= 4 else \
              ("Pivot forming: consider a first tranche", "yellow") if npiv >= 2 else ("No pivot yet: keep dry powder", "grey")

    # blood in the streets (capitulation) panel
    vix, stl = S["VIXCLS"], S["STLFSI4"]
    def peak_then(s, days=120):
        d, v = last(s)
        if d is None: return None, None
        return max_since(s, d - dt.timedelta(days=days)), v
    caps = []
    pk, v = peak_then(vix)
    caps.append(("VIX spiked, then calmed", f"120-day peak {fmt(pk, '')}, now {fmt(v, '')}" if num(v) else "n/a",
                 num(pk) and num(v) and pk >= 40 and v < 30, "Peak above 40, now below 30"))
    pk, v = peak_then(hy)
    caps.append(("Junk spreads blew out, now easing", f"120-day peak {fmt(pk, '%')}, now {fmt(v, '%')}" if num(v) else "n/a",
                 num(pk) and num(v) and pk >= 7 and v <= pk - 1, "Peak above 7%, now 1+ pt lower"))
    pk, v = peak_then(stl)
    caps.append(("Stress index peaked and falling", f"120-day peak {fmt(pk, '')}, now {fmt(v, '')}" if num(v) else "n/a",
                 num(pk) and num(v) and pk >= 2 and v <= pk - 0.5, "Peak above 2, now 0.5+ lower"))
    caps.append(("Rescue under way", f"{npiv} of 5 pivot signals lit", npiv >= 2, "2+ pivot signals"))
    spy = prices("SPY")
    off = (spy["price"] / spy["high52"] - 1) if spy else None
    caps.append(("Stocks deeply marked down", f"SPY {pct(off)} from 52-wk high" if num(off) else "n/a",
                 num(off) and off <= -0.25, "SPY 25%+ below its high"))
    ncap = sum(1 for c in caps if c[2])
    capitulation = ncap >= 3

    # prices
    hard = []
    P = {}
    for sym in ["GC=F", "SI=F", "BTC-USD", "GLD", "SLV", "GDX"]:
        P[sym] = prices(sym)
    names = {"GC=F": "Gold ($/oz)", "SI=F": "Silver ($/oz)", "BTC-USD": "Bitcoin", "GLD": "GLD gold ETF", "SLV": "SLV silver ETF", "GDX": "GDX gold miners"}
    for sym, nm in names.items():
        p = P[sym]
        if p:
            c = p["closes"]
            hard.append(dict(name=nm, price=p["price"], day=p["price"] / p["prev"] - 1 if p["prev"] else None,
                             m1=p["price"] / c[-22] - 1 if len(c) > 22 else None, off=p["price"] / p["high52"] - 1))
        else:
            hard.append(dict(name=nm, price=None, day=None, m1=None, off=None))
    g, s_, brent = (P["GC=F"] or {}).get("price"), (P["SI=F"] or {}).get("price"), last(S["DCOILBRENTEU"])[1]
    ratios = [("Gold / silver ratio", g / s_ if g and s_ else None, "Above 80 = silver cheap vs gold"),
              ("Gold / Brent ratio", g / brent if g and brent else None, "Rising = gold gaining vs energy")]

    tickers = []
    if os.path.exists("watchlist.txt"):
        for line in open("watchlist.txt"):
            t = line.split("#")[0].strip().upper()
            if t and t not in tickers: tickers.append(t)
    watch = []
    for t in tickers[:60]:
        p = prices(t)
        if not p or len(p["closes"]) < 25:
            watch.append(dict(t=t, ok=False)); continue
        m = setup_trend(p)
        lab, tone, size = signal(regime, m["setup"], m["trend"])
        if capitulation and m["setup"] != "Overbought":
            lab, tone, size = "Capitulation: buy in tranches", "green", "1/3 of planned"
        watch.append(dict(t=t, ok=True, price=p["price"], sig=lab, tone=tone, size=size, **m))

    os.makedirs(OUT, exist_ok=True)
    render(dict(caps=caps, ncap=ncap, capitulation=capitulation, regime=regime, calc=calc, override=override, score=score, rows=rows, pivots=pivots, npiv=npiv,
                verdict=verdict, hard=hard, ratios=ratios, watch=watch, nodata=nodata))
    write_icon(os.path.join(OUT, "icon.png"))
    json.dump({"updated": NOW.isoformat(), "regime": regime, "score": score,
               "metrics": [{k: (str(v) if isinstance(v, dt.date) else v) for k, v in r.items() if k != "spark"} for r in rows],
               "watchlist": [{k: v for k, v in w.items()} for w in watch]},
              open(os.path.join(OUT, "data.json"), "w"), default=str, indent=1)
    print(f"Built: regime {regime}, score {score}, {nodata} metrics missing, {len(watch)} tickers")

# --------------------------------------------------------------------------- render

E = html.escape
TONE = {"GREEN": "green", "YELLOW": "yellow", "RED": "red", "NODATA": "grey", "NO DATA": "grey"}

def spark_svg(vals, tone):
    if len(vals) < 3: return ""
    vals = vals[-260:]
    lo, hi = min(vals), max(vals)
    if hi == lo: hi = lo + 1
    W, H = 100, 28
    pts = " ".join(f"{i*W/(len(vals)-1):.1f},{H-2-(v-lo)/(hi-lo)*(H-4):.1f}" for i, v in enumerate(vals))
    return f'<svg class="spark t-{tone}" viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true"><polyline points="{pts}"/></svg>'

def pct(v, signed=True):
    if not num(v): return "n/a"
    return f"{'+' if signed and v > 0 else ''}{v*100:.1f}%"

def money(v):
    if not num(v): return "n/a"
    return f"${v:,.0f}" if v >= 1000 else f"${v:,.2f}"

def render(d):
    reg = d["regime"]; tone = TONE[reg]
    deploy = {"GREEN": "Up to full planned size", "YELLOW": "1/3 size, in tranches",
              "RED": "None. Hold cash, trim strength.", "NO DATA": "Waiting on data"}[reg]
    if d["capitulation"]: deploy = "1/3 size tranches (capitulation)"
    line = {"GREEN": "Conditions healthy. Buy dips in uptrends.",
            "YELLOW": "Stress building. Small, staged buys only.",
            "RED": "Stress high. Protect capital and keep dry powder.",
            "NO DATA": "Too much economic data missing to judge. Check back after the next update."}[reg]
    counts = {k: sum(1 for r in d["rows"] if r["st"] == k) for k in ("RED", "YELLOW", "GREEN")}
    edit = f"https://github.com/{REPO}/edit/main/watchlist.txt" if REPO else ""

    # watchlist
    wl = []
    for w in d["watch"]:
        if not w["ok"]:
            wl.append(f'<div class="wrow"><div class="wmain"><span class="tkc"><span class="tk">{E(w["t"])}</span><small>No data. Check the symbol.</small></span></div></div>'); continue
        dc = "up" if num(w["day"]) and w["day"] > 0 else "dn" if num(w["day"]) and w["day"] < 0 else ""
        wl.append(f'''<details class="wrow"><summary class="wmain">
<span class="tkc"><span class="tk">{E(w["t"])}</span><small><b class="{ 'up' if w['setup']=='Oversold' else 'dn' if w['setup']=='Overbought' else ''}">{w["setup"]}</b> · <span class="{'dn' if w['trend']=='Downtrend' else ''}">{w["trend"]}</span></small></span>
<span class="px">{money(w["price"])}<small class="{dc}">{pct(w["day"])}</small></span>
<span class="chip t-{w["tone"]}">{E(w["sig"])}</span></summary>
<dl class="kv"><div><dt>New-buy size</dt><dd>{w["size"]}</dd></div><div><dt>vs 20-day avg</dt><dd>{pct(w["vs20"])}</dd></div>
<div><dt>20-day range</dt><dd>{w["rng"]*100:.0f}% of the way up</dd></div><div><dt>200-day avg</dt><dd>{money(w["a200"])}</dd></div>
<div><dt>Off 52-wk high</dt><dd>{pct(w["off_high"])}</dd></div><div><dt>1 month</dt><dd>{pct(w["m1"])}</dd></div></dl></details>''')

    hard = "".join(f'''<div class="hcell"><div class="hn">{E(h["name"])}</div><div class="hp">{money(h["price"])}</div>
<div class="hm"><span class="{ 'up' if num(h['day']) and h['day']>0 else 'dn' if num(h['day']) and h['day']<0 else ''}">{pct(h["day"])} day</span>
<span class="{ 'up' if num(h['m1']) and h['m1']>0 else 'dn' if num(h['m1']) and h['m1']<0 else ''}">{pct(h["m1"])} 1-mo</span></div>
<div class="hm muted">{pct(h["off"])} from 52-wk high</div></div>''' for h in d["hard"])
    ratios = "".join(f'<div class="ratio"><b>{E(n)}</b> <span>{v:.1f}</span> <small class="muted">{E(note)}</small></div>' if num(v)
                     else f'<div class="ratio"><b>{E(n)}</b> <span>n/a</span></div>' for n, v, note in d["ratios"])

    cap = "".join(f'''<li class="{ 'hit' if hit else ''}"><span class="dot"></span><div><b>{E(n)}</b><small>{E(reading)}. Trigger: {E(trig)}</small></div><span class="yn">{'YES' if hit else 'no'}</span></li>'''
                  for n, reading, hit, trig in d["caps"])
    if d["capitulation"]:
        capv = ("Capitulation signal ON: start buying in tranches, 1/3 of planned size at a time, even though the regime may read red.", "green")
    elif d["ncap"] == 2:
        capv = ("Getting close: 2 of 5 lit. Line up your buy list and cash.", "yellow")
    else:
        capv = ("Not yet. No sign of a washout. Patience.", "grey")
    piv = "".join(f'''<li class="{ 'hit' if hit else ''}"><span class="dot"></span><div><b>{E(n)}</b><small>{E(reading)}. Trigger: {E(trig)}</small></div><span class="yn">{'YES' if hit else 'no'}</span></li>'''
                  for n, reading, hit, trig in d["pivots"])

    cats, mets = [], []
    for r in d["rows"]:
        if r["cat"] not in cats:
            cats.append(r["cat"]); mets.append(f'<h3>{E(r["cat"])}</h3>')
        t = TONE[r["st"]]
        thr = f'Yellow at {fmt(r["y"], r["unit"])}, red at {fmt(r["r"], r["unit"])} ({"higher" if r["direction"]=="up" else "lower"} is worse)'
        du = "pts" if r["unit"] in ("%", "net %") else r["unit"]
        dl = f'<small>{fmt(r["d"], du, True)}{" pts" if du=="pts" else ""} in ~1 mo</small>' if num(r["d"]) else ""
        asof = r["asof"].strftime("%b %-d, %Y") if r["asof"] else "no data"
        mets.append(f'''<details class="mrow"><summary><span class="bar t-{t}"></span>
<span class="mn">{E(r["name"])}<small class="muted">as of {asof}</small></span>
{spark_svg(r["spark"], t)}
<span class="mv t-{t}">{fmt(r["v"], r["unit"])}{dl}</span></summary>
<p>{E(r["meaning"])}</p><p class="muted">{E(thr)}. FRED series {E(r["sid"])}.</p></details>''')

    upd = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    score = f'{d["score"]*100:.0f}%' if num(d["score"]) else "n/a"
    ov = f'<p class="ovr">Manual override is on (model says {d["calc"]}).</p>' if d["override"] else ""
    page = TEMPLATE.format(
        tone=tone, regime=reg, score=score, red=counts["RED"], yel=counts["YELLOW"], grn=counts["GREEN"],
        nodsuffix=(f" ({d['nodata']} missing)" if d["nodata"] else ""), deploy=E(deploy), line=E(line), ov=ov, watch="".join(wl) or '<p class="muted">Your watchlist is empty.</p>',
        hard=hard, ratios=ratios, piv=piv, pivn=d["npiv"], cap=cap, ncap=d["ncap"], capv=E(capv[0]), captone=capv[1],
        capbanner=('<p class="capb">Capitulation signal is ON. See the panel below.</p>' if d["capitulation"] else ""), verdict=E(d["verdict"][0]), vtone=d["verdict"][1],
        metrics="".join(mets), upd=upd,
        edit=(f'<a href="{edit}">Edit watchlist</a>' if edit else "Edit watchlist.txt in your GitHub repo"),
        override_link=(f'<a href="https://github.com/{REPO}/edit/main/regime_override.txt">Set a manual regime override</a>' if REPO else ""))
    open(os.path.join(OUT, "index.html"), "w").write(page)
    open(os.path.join(OUT, "manifest.webmanifest"), "w").write(json.dumps({
        "name": "System Health", "short_name": "Sys Health", "start_url": "./", "display": "standalone",
        "background_color": "#0f1a14", "theme_color": "#0f1a14", "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png"}]}))


TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta http-equiv="refresh" content="1800">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Sys Health">
<meta name="theme-color" content="#0f1a14">
<link rel="apple-touch-icon" href="icon.png"><link rel="manifest" href="manifest.webmanifest">
<title>System Health</title>
<style>
/* VOICE: a control-room status board: calm until something lights up.
   IDEA: the annunciator panel in a plant control room: one master light, then the individual gauges.
   PICTURE: the full-bleed regime banner, colored by the master light.
   COLOUR and TYPE: deep ink ground in the banner, white body; green/amber/red only as status. Inter Tight + Inter.
   LAYOUT: single column for phone; banner, watchlist, hard assets, pivot watch, gauges. */
@import url('https://fonts.googleapis.com/css2?family=Inter+Tight:wght@600;800&family=Inter:wght@400;500;600&display=swap');
:root{{color-scheme:light dark;--bg:#ffffff;--ink:#14201a;--mut:#5d6b64;--line:#e3e8e5;--card:#f5f7f6;
--g:#1f8a4c;--gbg:#d9f2e2;--lg:#5aa972;--lgbg:#ebf7e4;--y:#a86b00;--ybg:#fff0c2;--r:#c0362c;--rbg:#fbd9d5;--n:#6b7280;--nbg:#e9ebee}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1310;--ink:#e7eee9;--mut:#93a39a;--line:#223029;--card:#141d18;
--g:#58d68d;--gbg:#123a24;--lg:#9ad59c;--lgbg:#1b3320;--y:#ffc94d;--ybg:#3d2f08;--r:#ff8a7f;--rbg:#43150f;--n:#a3aab3;--nbg:#262b31}}}}
*{{box-sizing:border-box;min-width:0}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 Inter,system-ui,-apple-system,sans-serif;overflow-x:clip;-webkit-text-size-adjust:100%}}
h1,h2,h3{{font-family:'Inter Tight',Inter,sans-serif;margin:0}}
.wrap{{max-width:760px;margin:0 auto;padding:0 16px}}
.banner{{padding:calc(env(safe-area-inset-top) + 20px) 0 22px;color:#fff;background:#3b4148}}
.banner.green{{background:#0f6b3a}}.banner.yellow{{background:#8a5a00}}.banner.red{{background:#9e2219}}
.banner .top{{display:flex;justify-content:space-between;align-items:center;font-size:13px;opacity:.85}}
.banner h1{{font-size:clamp(44px,13vw,72px);line-height:1;font-weight:800;letter-spacing:-.02em;margin:14px 0 6px}}
.banner .sub{{font-size:17px;font-weight:500;margin:0 0 14px;text-wrap:balance}}
.gauge{{display:flex;gap:10px;flex-wrap:wrap}}
.gauge div{{background:rgba(255,255,255,.14);border-radius:10px;padding:8px 12px;flex:1 1 140px}}
.gauge b{{display:block;font-size:20px;font-family:'Inter Tight',sans-serif}}.gauge small{{font-size:12px;opacity:.85}}
.capb{{background:#fff;color:#0f6b3a;font-weight:700;border-radius:8px;padding:8px 12px;margin:12px 0 0}}
.ovr{{font-size:13px;margin:10px 0 0;opacity:.9}}
section{{padding:22px 0 6px;border-bottom:1px solid var(--line)}}
h2{{font-size:22px;margin-bottom:4px}}
.lede{{color:var(--mut);font-size:14px;margin:0 0 12px}}
.muted{{color:var(--mut)}}.up{{color:var(--g)}}.dn{{color:var(--r)}}
summary{{list-style:none;cursor:pointer}}summary::-webkit-details-marker{{display:none}}
.wrow{{border-top:1px solid var(--line)}}
.wmain{{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr) minmax(0,1.3fr);gap:8px;align-items:center;padding:11px 0;min-height:52px}}
.tkc{{display:flex;flex-direction:column}}.tkc small{{font-size:12px;color:var(--mut);white-space:nowrap}}.tkc small b{{font-weight:600}}
.tk{{font-weight:700;font-family:'Inter Tight',sans-serif;font-size:17px}}
.px{{text-align:right;display:flex;flex-direction:column;font-size:14px;font-variant-numeric:tabular-nums;white-space:nowrap}}
.px small{{font-size:12px}}
.chip{{border-radius:8px;padding:6px 8px;font-size:13px;font-weight:600;text-align:center;line-height:1.2}}
.t-green.chip{{background:var(--gbg);color:var(--g)}}.t-lgreen.chip{{background:var(--lgbg);color:var(--lg)}}
.t-yellow.chip{{background:var(--ybg);color:var(--y)}}.t-red.chip{{background:var(--rbg);color:var(--r)}}.t-grey.chip{{background:var(--nbg);color:var(--n)}}
.kv{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 16px;margin:0 0 12px;padding:10px 12px;background:var(--card);border-radius:10px;font-size:14px}}
.kv div{{display:flex;justify-content:space-between;gap:8px}}.kv dt{{color:var(--mut)}}.kv dd{{margin:0;font-variant-numeric:tabular-nums;text-align:right}}
.hgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
.hcell{{background:var(--card);border-radius:12px;padding:12px}}
.hn{{font-size:13px;color:var(--mut)}}.hp{{font-family:'Inter Tight',sans-serif;font-size:24px;font-weight:800;font-variant-numeric:tabular-nums}}
.hm{{font-size:13px;display:flex;gap:10px;flex-wrap:wrap}}
.ratio{{margin:10px 0 0;font-size:14px}}.ratio span{{font-variant-numeric:tabular-nums;margin-right:6px}}
.piv{{list-style:none;padding:0;margin:0}}
.piv li{{display:grid;grid-template-columns:14px minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}}
.piv small{{display:block;color:var(--mut);font-size:13px}}
.dot{{width:12px;height:12px;border-radius:50%;background:var(--nbg);border:2px solid var(--n)}}
.piv .hit .dot{{background:var(--g);border-color:var(--g)}}.yn{{font-weight:700;font-size:14px;color:var(--mut)}}.hit .yn{{color:var(--g)}}
.verdict{{border-radius:10px;padding:12px;font-weight:600;margin:4px 0 14px}}
.verdict.t-green{{background:var(--gbg);color:var(--g)}}.verdict.t-yellow{{background:var(--ybg);color:var(--y)}}.verdict.t-grey{{background:var(--nbg);color:var(--n)}}
h3{{font-size:14px;color:var(--mut);font-weight:600;margin:16px 0 4px}}
.mrow{{border-top:1px solid var(--line)}}
.mrow summary{{display:grid;grid-template-columns:5px minmax(0,1fr) 70px minmax(92px,auto);gap:10px;align-items:center;padding:10px 0;min-height:52px}}
.bar{{align-self:stretch;border-radius:3px;background:var(--n)}}
.bar.t-green{{background:var(--g)}}.bar.t-yellow{{background:var(--y)}}.bar.t-red{{background:var(--r)}}
.mn{{font-size:15px;font-weight:500;line-height:1.25}}.mn small{{display:block;font-size:12px;font-weight:400}}
.mv{{text-align:right;font-weight:700;font-variant-numeric:tabular-nums;border-radius:8px;padding:5px 8px;white-space:nowrap}}
.mv small{{display:block;font-weight:400;font-size:11px;opacity:.85}}
.mv.t-green{{background:var(--gbg);color:var(--g)}}.mv.t-yellow{{background:var(--ybg);color:var(--y)}}.mv.t-red{{background:var(--rbg);color:var(--r)}}.mv.t-grey{{background:var(--nbg);color:var(--n)}}
.spark{{width:70px;height:26px}}.spark polyline{{fill:none;stroke:currentColor;stroke-width:1.6;vector-effect:non-scaling-stroke}}
.spark.t-green{{color:var(--g)}}.spark.t-yellow{{color:var(--y)}}.spark.t-red{{color:var(--r)}}.spark.t-grey{{color:var(--n)}}
.mrow p{{margin:0 0 8px;font-size:14px}}
footer{{padding:20px 0 calc(env(safe-area-inset-bottom) + 28px);font-size:13px;color:var(--mut)}}
footer a,.lede a{{color:inherit}}
.stale{{display:none;background:var(--ybg);color:var(--y);padding:8px 12px;border-radius:8px;margin-top:12px;font-size:14px;font-weight:600}}
@media (max-width:420px){{.kv{{grid-template-columns:minmax(0,1fr)}}.spark{{display:none}}.mrow summary{{grid-template-columns:5px minmax(0,1fr) minmax(88px,auto)}}}}
</style></head><body>
<header class="banner {tone}"><div class="wrap">
<div class="top"><span>US financial system</span><span id="upd" data-t="{upd}">Updated {upd}</span></div>
<h1>{regime}</h1><p class="sub">{line}</p>
<div class="gauge"><div><b>{score}</b><small>Stress score</small></div><div><b>{deploy}</b><small>New money to deploy</small></div>
<div><b>{red} / {yel} / {grn}</b><small>Red / yellow / green gauges{nodsuffix}</small></div></div>{ov}
{capbanner}<div class="stale" id="stale">Data is more than 3 hours old. The hourly update may have paused.</div>
</div></header>
<main class="wrap">
<section id="capitulation"><h2>Blood in the streets</h2><p class="lede">Washout signals that have marked past bottoms: {ncap} of 5 lit. Three or more turns the signal on and overrides the regime for buying.</p>
<div class="verdict t-{captone}">{capv}</div><ul class="piv">{cap}</ul></section>
<section id="pivot"><h2>Pivot watch</h2><p class="lede">Signs the Fed and Treasury rescue has started: {pivn} of 5 lit.</p>
<div class="verdict t-{vtone}">{verdict}</div><ul class="piv">{piv}</ul></section>
<section id="hard"><h2>Hard assets and crypto</h2><p class="lede">Gold and silver are front-month futures, close to spot.</p><div class="hgrid">{hard}</div>{ratios}</section>
<section id="gauges"><h2>System gauges</h2><p class="lede">Tap any gauge for what it means and its thresholds. Bars show the last year.</p>{metrics}</section>
<section id="watchlist"><h2>Watchlist</h2><p class="lede">Each ticker's setup, filtered through the regime. Nothing turns green when the system is red, unless the blood-in-the-streets signal is on. Tap a row for details. {edit}</p>{watch}</section>
</main>
<footer class="wrap"><p>Data: FRED (Federal Reserve Bank of St. Louis), Yahoo Finance, Stooq. Refreshes hourly. Thresholds are rules of thumb, not official cutoffs. Not financial advice.</p><p>{override_link}</p></footer>
<script>
{{const el=document.getElementById('upd');const t=new Date(el.dataset.t);const m=Math.round((Date.now()-t)/60000);
el.textContent='Updated '+(m<1?'just now':m<60?m+' min ago':Math.round(m/60)+' hr ago');
if(m>180)document.getElementById('stale').style.display='block';}}
</script>
</body></html>"""


def write_icon(path):
    """180x180 PNG app icon: three status lights on a dark tile. Stdlib only."""
    W = H = 180
    bg = (15, 26, 20); cols = [(88, 214, 141), (255, 201, 77), (255, 138, 127)]
    centers = [(50, 90), (90, 90), (130, 90)]
    raw = bytearray()
    for y in range(H):
        raw.append(0)
        for x in range(W):
            px = bg
            for (cx, cy), c in zip(centers, cols):
                dd = (x - cx) ** 2 + (y - cy) ** 2
                if dd <= 15 ** 2: px = c
                elif dd <= 17 ** 2: px = tuple(int(a * .45 + b * .55) for a, b in zip(c, bg))
            raw += bytes(px)
    def chunk(t, b): return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b"")
    open(path, "wb").write(png)


if __name__ == "__main__":
    build()
