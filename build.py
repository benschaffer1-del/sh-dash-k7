"""US Financial System Health dashboard.

Pulls free public data (FRED from the St. Louis Fed, prices from Yahoo Finance
with Stooq as a backup), scores the system, and writes a static web page to
./site. Uses only Python's standard library: nothing to install.

Run locally:            python build.py
Run with sample data:   MOCK=1 python build.py

Color rule used everywhere: green = healthy, yellow = caution, red = stress.
A tile's color shows where a reading IS. The arrow next to it shows which way
it moved over the last month, colored by whether that move is good or bad.
"""
import csv, datetime as dt, html, io, json, math, os, random, re, struct, time, urllib.parse, urllib.request, zlib

OUT = "site"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()
MONTH_AGO = TODAY - dt.timedelta(days=28)
MOCK = os.environ.get("MOCK") == "1"
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
REPO = os.environ.get("GITHUB_REPOSITORY", "")
UA = {"User-Agent": "Mozilla/5.0 (personal dashboard; contact via GitHub)"}
R, Y, G, N = "RED", "YELLOW", "GREEN", "NODATA"

# =========================================================================== data access

def http(url, tries=3):
    err = None
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


STOOQ = {"GC=F": "xauusd", "SI=F": "xagusd", "BTC-USD": "btcusd", "JPY=X": "usdjpy"}

def prices(sym):
    """dict(price, prev, closes, series[(date, close)], high52, src) or None."""
    if MOCK:
        return mock_prices(sym)
    try:
        j = json.loads(http(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=1y&interval=1d"))
        r = j["chart"]["result"][0]; m = r["meta"]
        q = r["indicators"]["quote"][0]
        rows = [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date(), c, h)
                for t, c, h in zip(r["timestamp"], q["close"], q["high"]) if c is not None]
        closes = [c for _, c, _ in rows]
        price = m.get("regularMarketPrice") or closes[-1]
        mkt_day = dt.datetime.fromtimestamp(m.get("regularMarketTime", 0) or 0, dt.timezone.utc).date()
        prev = closes[-2] if rows[-1][0] == mkt_day and len(closes) > 1 else closes[-1]
        hi = m.get("fiftyTwoWeekHigh") or max(h for _, _, h in rows if h is not None)
        series = [(d, c) for d, c, _ in rows]
        if series[-1][0] == mkt_day: series[-1] = (mkt_day, price)
        return {"price": price, "prev": prev, "closes": closes, "series": series,
                "high52": max(hi, price), "src": "Yahoo Finance"}
    except Exception as e:
        print(f"Yahoo {sym} failed: {e}; trying Stooq")
    try:
        s = STOOQ.get(sym, sym.lower().replace("-", ".") + ".us")
        rows = list(csv.DictReader(io.StringIO(http(f"https://stooq.com/q/d/l/?s={s}&i=d"))))
        rows = [r for r in rows if r.get("Close") not in (None, "", "N/D")][-260:]
        series = [(dt.date.fromisoformat(r["Date"]), float(r["Close"])) for r in rows]
        closes = [c for _, c in series]
        return {"price": closes[-1], "prev": closes[-2], "closes": closes, "series": series,
                "high52": max(float(r["High"]) for r in rows), "src": "Stooq (end of day)"}
    except Exception as e:
        print(f"Stooq {sym} failed: {e}")
        return None


def prices_long(sym):
    """Monthly (date, close) history, as long as Yahoo has it. Used for the red-zone percentiles."""
    if MOCK:
        random.seed(sym + "L"); x, out = 20.0, []
        for i in range(12 * 70):
            x *= 1 + random.gauss(0.006, 0.042); out.append((dt.date(1956, 1, 1) + dt.timedelta(days=int(i * 30.44)), x))
        return out
    try:
        j = json.loads(http(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range=max&interval=1mo"))
        r = j["chart"]["result"][0]
        return [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date(), c)
                for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c is not None]
    except Exception as e:
        print(f"Yahoo long {sym} failed: {e}")
        return []


# ---- DailyJobCuts.com: a hand-curated daily list of announced US layoffs. No data feed, so we read the
# pages once a day (reusing the copy we published earlier that day) and tally announced cuts by week.
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
DJC_SLUGS = [["jan"], ["feb"], ["mar", "march"], ["apr", "april"], ["may"], ["june", "jun"], ["july", "jul"], ["aug"],
             ["sept", "sep"], ["oct"], ["nov"], ["dec"]]
DATE_RE = re.compile(r"^(" + "|".join(MONTHS) + r")\s+(\d{1,2})\s*,\s*(\d{4})")
EST_RE = re.compile(r"(\w{3,9})\.?\s+U\.?S\.?\s+Layoff\s+Est\.?\s*-\s*([\d,]+)", re.I)

def djc_parse(page):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", page)
    t = re.sub(r"(?i)<br\s*/?>|</(p|li|div|tr|td|h\d|ul|table)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    entries, ests, day, section = [], {}, None, "layoffs"
    for m in EST_RE.finditer(t):
        ests[m.group(1)[:3].title()] = int(m.group(2).replace(",", ""))
    for raw in t.split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if not line: continue
        low = line.lower()
        if " - " not in line and len(line) < 60:
            if "closing" in low or "closure" in low: section = "closings"
            elif "hiring" in low: section = "hiring"
            elif "layoff" in low: section = "layoffs"
        m = DATE_RE.match(line)
        if m:
            try: day = dt.date(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)))
            except ValueError: day = None
            continue
        if day is None or section != "layoffs" or " - " not in line or " est." in low: continue
        name, rest = line.split(" - ", 1)
        if name.lower().startswith("update") or rest.strip().startswith("+") or len(name) > 90: continue
        jobs = None
        for n in re.finditer(r"(\d{1,3}(?:,\d{3})+|\d+)(\+)?", rest):
            after = rest[n.end():n.end() + 9].lower()
            if after.lstrip().startswith(("%", "percent")): break
            v = int(n.group(1).replace(",", ""))
            if 1990 <= v <= 2040 and "," not in n.group(1): continue
            jobs = v; break
        entries.append((day, name.strip(), jobs))
    return entries, ests

def djc():
    if MOCK:
        random.seed("djc"); out = []
        for i in range(100):
            d = TODAY - days(i)
            if d.weekday() < 5:
                for k in range(random.randint(2, 8)):
                    out.append((d, f"Co{i}-{k}", random.choice([None, random.randint(20, 900)])))
        return dict(entries=out, ests={"Oct": 13956, "Sep": 11200}, fetched=str(TODAY), ok=True)
    if REPO and "/" in REPO:
        owner, repo = REPO.split("/", 1)
        try:
            prev = json.loads(http(f"https://{owner.lower()}.github.io/{repo}/djc.json", tries=1))
            if prev.get("fetched") == str(TODAY) and prev.get("entries"):
                prev["entries"] = [(dt.date.fromisoformat(d), n, j) for d, n, j in prev["entries"]]
                print("DailyJobCuts: reusing today's copy"); return prev
        except Exception:
            pass
    urls = ["https://www.dailyjobcuts.com/"]
    y, m = TODAY.year, TODAY.month
    for _ in range(3):
        m -= 1
        if m == 0: y, m = y - 1, 12
        urls.append([f"https://www.dailyjobcuts.com/layoffs-{slug}.htm" for slug in DJC_SLUGS[m - 1]])
    seen, entries, ests = set(), [], {}
    for u in urls:
        for url in ([u] if isinstance(u, str) else u):
            try:
                page = http(url, tries=2)
            except Exception:
                continue
            es, est = djc_parse(page); ests.update(est)
            for d, n, j in es:
                if (d, n) not in seen and d <= TODAY:
                    seen.add((d, n)); entries.append((d, n, j))
            break
    ok = len(entries) >= 20
    print(f"DailyJobCuts: {len(entries)} entries parsed" + ("" if ok else " (too few; tile hidden)"))
    return dict(entries=entries, ests=ests, fetched=str(TODAY), ok=ok)

def djc_weeks(dj, n=13):
    """Last n Monday-start weeks: list of dict(start, jobs, count, partial)."""
    this_mon = TODAY - days(TODAY.weekday())
    weeks = []
    for k in range(n - 1, -1, -1):
        a = this_mon - days(7 * k); b = a + days(6)
        es = [e for e in dj["entries"] if a <= e[0] <= b]
        weeks.append(dict(start=a, jobs=sum(e[2] or 0 for e in es), count=len(es), partial=(k == 0)))
    return weeks

# White-collar fields on Indeed (postings index, Feb 1 2020 = 100), the jobs most exposed to AI.
WHITE_COLLAR = [("IHLIDXUSTPSOFTDEVE", "Software development"), ("IHLIDXUSTPITOPHE", "IT operations & helpdesk"),
                ("IHLIDXUSTPMECO", "Media & communications"), ("IHLIDXUSTPMARK", "Marketing"),
                ("IHLIDXUSTPCUSTSERV", "Customer service"), ("IHLIDXUSTPADMIASSI", "Administrative assistance"),
                ("IHLIDXUSTPHUMARESO", "Human resources"), ("IHLIDXUSTPACCO", "Accounting"),
                ("IHLIDXUSTPBAFI", "Banking & finance"), ("IHLIDXUSTPLEGA", "Legal")]

# =========================================================================== sample data (testing only)

MOCK_BASE = {
    "DGS10": 5.1, "DGS30": 5.45, "DGS2": 4.35, "DFII10": 2.85, "T10Y3M": 0.8, "SOFR": 4.42, "IORB": 4.40,
    "BAMLH0A0HYM2": 3.45, "BAMLC0A0CM": 0.95, "BAMLH0A3HYC": 13.2, "DRTSCILM": 2.0, "NFCI": -0.48,
    "STLFSI4": -0.85, "VIXCLS": 23.0, "DTWEXBGS": 128.0, "DCOILBRENTEU": 92.0, "DFF": 4.58,
    "WALCL": 6.5e6, "SAHMREALTIME": 0.07, "ICSA": 205000, "CCSA": 1.70e6, "U6RATE": 7.9,
    "PSAVERT": 3.8, "DRCCLACBS": 2.9, "T10YIE": 2.2, "CPIAUCSL": 330.0, "A091RC1Q027SBEA": 1250.0,
    "FGRECPT": 5600.0, "FYFSGDA188S": -5.8, "GFDEGDQ188S": 121.0, "DEXJPUS": 152.0,
    "IRLTLT01JPM156N": 1.95, "WFRBST01134": 31.0, "WFRBSB50215": 2.5, "LNS14000036": 8.6,
    "CES6054150001": 2550.0, "USINFO": 2900.0, "TEMPHELPS": 2500.0, "JTSQUR": 1.9,
    "CUSR0000SAF11": 320.0, "CUSR0000SAH1": 420.0, "CUSR0000SEHF01": 290.0, "CUSR0000SETE": 900.0,
    "UMCSENT": 54.0, "DRCRELEXFACBS": 1.6, "T10Y2Y": 0.55, "RIFSPPFAAD90NB": 4.35, "DTB3": 4.20,
    "DRTSCLCC": 4.0, "NFCICREDIT": -0.3, "NFCILEVERAGE": -0.2, "IC4WSA": 210000, "JTSLDR": 1.0,
    "IHLIDXUS": 106.0, "IHLIDXUSTPSOFTDEVE": 81.7, "TDSP": 11.3, "BCNSDODNS": 2.1e7, "CMDEBT": 1.9e4,
    "CSUSHPINSA": 330.0, "GDP": 31000.0, "IHLIDXUSTPITOPHE": 70.0, "IHLIDXUSTPMECO": 67.2, "IHLIDXUSTPMARK": 78.0,
    "IHLIDXUSTPCUSTSERV": 87.5, "IHLIDXUSTPADMIASSI": 92.2, "IHLIDXUSTPHUMARESO": 99.3, "IHLIDXUSTPACCO": 107.5,
    "IHLIDXUSTPBAFI": 107.2, "IHLIDXUSTPLEGA": 125.3}
MOCK_STEP = {"WALCL": 7, "ICSA": 7, "CCSA": 7, "NFCI": 7, "STLFSI4": 7, "DRCCLACBS": 91, "DRTSCILM": 91,
             "A091RC1Q027SBEA": 91, "FGRECPT": 91, "GFDEGDQ188S": 91, "FYFSGDA188S": 365,
             "WFRBST01134": 91, "WFRBSB50215": 91, "DRCRELEXFACBS": 91, "DRTSCLCC": 91, "NFCICREDIT": 7,
             "NFCILEVERAGE": 7, "IC4WSA": 7, "TDSP": 91, "BCNSDODNS": 91, "CMDEBT": 91, "GDP": 91}
MOCK_MONTHLY = {"U6RATE", "PSAVERT", "SAHMREALTIME", "CPIAUCSL", "IRLTLT01JPM156N", "LNS14000036",
                "CES6054150001", "USINFO", "TEMPHELPS", "JTSQUR", "CUSR0000SAF11", "CUSR0000SAH1",
                "CUSR0000SEHF01", "CUSR0000SETE", "UMCSENT", "JTSLDR", "CSUSHPINSA"}
MOCK_TREND = {"CPIAUCSL": 0.0025, "CUSR0000SAF11": 0.0025, "CUSR0000SAH1": 0.003, "CUSR0000SEHF01": 0.004,
              "CUSR0000SETE": 0.005, "CES6054150001": -0.002, "USINFO": -0.001, "TEMPHELPS": -0.004,
              "BCNSDODNS": 0.012, "CMDEBT": 0.013, "CSUSHPINSA": 0.004, "GDP": 0.012}

def mock_fred(sid, days):
    random.seed(sid)
    base = MOCK_BASE[sid]
    step = MOCK_STEP.get(sid, 30 if sid in MOCK_MONTHLY else 1)
    n = days // step
    out = []
    for i in range(n):
        d = TODAY - dt.timedelta(days=(n - i) * step)
        k = n - 1 - i
        if sid in MOCK_TREND:
            v = base * (1 - MOCK_TREND[sid]) ** k
        else:
            v = base + math.sin(i / 9) * abs(base) * 0.03 + random.gauss(0, abs(base) * 0.01 + 0.005)
        out.append((d, v))
    out[-1] = (out[-1][0], base)
    return out


def mock_prices(sym):
    random.seed(sym)
    p = {"GC=F": 3850, "SI=F": 47, "BTC-USD": 64500, "JPY=X": 152.0}.get(sym, random.uniform(30, 700))
    closes, x = [], 1.0
    drift = random.uniform(-0.0012, 0.0012)
    for _ in range(252):
        x *= 1 + random.gauss(drift, 0.013); closes.append(x)
    k = p / closes[-1]; closes = [c * k for c in closes]
    series = [(TODAY - dt.timedelta(days=int((251 - i) * 1.45)), c) for i, c in enumerate(closes)]
    return {"price": p, "prev": closes[-2], "closes": closes, "series": series,
            "high52": max(closes) * 1.01, "src": "sample"}

# =========================================================================== helpers

def last(s):
    return s[-1] if s else (None, None)

def at(s, d):
    """Value at or before date d."""
    v = None
    for dd, vv in s:
        if dd <= d: v = vv
        else: break
    return v

def max_between(s, a, b):
    vals = [v for d, v in s if a <= d <= b]
    return max(vals) if vals else None

def min_between(s, a, b):
    vals = [v for d, v in s if a <= d <= b]
    return min(vals) if vals else None

def join(a, b, fn):
    """Combine two date series on a's dates."""
    out, j, bv = [], 0, None
    for d, v in a:
        while j < len(b) and b[j][0] <= d:
            bv = b[j][1]; j += 1
        if bv is not None:
            try: out.append((d, fn(v, bv)))
            except ZeroDivisionError: pass
    return out

def num(*xs):
    return all(isinstance(x, (int, float)) and not math.isnan(x) for x in xs)

def days(n):
    return dt.timedelta(days=n)

def pct(v, signed=True, nd=1):
    if not num(v): return "n/a"
    return f"{'+' if signed and v > 0 else ''}{v*100:.{nd}f}%"

def money(v):
    if not num(v): return "n/a"
    return f"${v:,.0f}" if v >= 1000 else f"${v:,.2f}"

def level(v, direction, y, r):
    """Status from a level. direction 'up' = higher is worse."""
    if not num(v): return N
    if direction == "up":
        return R if v >= r else Y if v >= y else G
    return R if v <= r else Y if v <= y else G

def worst(*sts):
    for s in (R, Y, G):
        if s in sts: return s
    return N

# =========================================================================== system gauges (full list)

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
    if unit == "x": return f"{sg}{v:.2f}x"
    return f"{sg}{v:.2f}"

def g_level(sid, scale=1.0):
    return lambda S, t: (at(S[sid], t) * scale) if num(at(S[sid], t)) else None

def g_spread(a, b):
    return lambda S, t: (at(S[a], t) - at(S[b], t)) * 100 if num(at(S[a], t), at(S[b], t)) else None

def g_ratio(a, b):
    return lambda S, t: at(S[a], t) / at(S[b], t) if num(at(S[a], t), at(S[b], t)) else None

def g_change(sid, n):
    return lambda S, t: at(S[sid], t) / at(S[sid], t - days(n)) - 1 if num(at(S[sid], t), at(S[sid], t - days(n))) else None

def g_cpi6(S, t):
    a, b = at(S["CPIAUCSL"], t), at(S["CPIAUCSL"], t - days(170))
    return (a / b) ** 2 - 1 if num(a, b) else None

# (category, name, spark series, value fn(S, t), unit, direction, yellow, red, meaning)
GAUGES = [
    ("Rates & Treasuries", "10-yr Treasury yield", "DGS10", g_level("DGS10"), "%", "up", 4.5, 5.0, "Cost of money for Washington and mortgages."),
    ("Rates & Treasuries", "30-yr Treasury yield", "DGS30", g_level("DGS30"), "%", "up", 4.75, 5.25, "Long-end stress: buyers demanding more to hold US debt."),
    ("Rates & Treasuries", "10-yr real yield (TIPS)", "DFII10", g_level("DFII10"), "%", "up", 2.0, 2.5, "How tight money is after inflation."),
    ("Rates & Treasuries", "10-yr minus 3-mo curve", "T10Y3M", g_level("T10Y3M"), "pts", "down", 0.5, 0.0, "Below 0 (inverted) has come before recessions."),
    ("Rates & Treasuries", "2-yr to 10-yr curve", "T10Y2Y", g_level("T10Y2Y"), "pts", "down", 0.25, 0.0, "Below 0 = inverted. A fast re-steepening as short rates fall (see next row) often marks recession onset."),
    ("Rates & Treasuries", "SOFR minus IORB", "SOFR", g_spread("SOFR", "IORB"), "bp", "up", 0.5, 5, "Above 0 means banks are short of cash: plumbing stress."),
    ("Rates & Treasuries", "Commercial paper spread", "X:CP", g_level("X:CP"), "bp", "up", 30, 60, "90-day financial commercial paper vs T-bills. Spikes when lenders hoard cash (2008, 2020)."),
    ("Credit", "High-yield bond spread", "BAMLH0A0HYM2", g_level("BAMLH0A0HYM2"), "%", "up", 4.0, 5.0, "Fear in risky corporate debt. The key crash tell."),
    ("Credit", "Investment-grade spread", "BAMLC0A0CM", g_level("BAMLC0A0CM"), "%", "up", 1.3, 1.8, "Stress in blue-chip corporate debt."),
    ("Credit", "CCC junk spread", "BAMLH0A3HYC", g_level("BAMLH0A3HYC"), "%", "up", 9, 12, "Weakest borrowers. Cracks show here first."),
    ("Credit", "CCC vs high-yield ratio", "X:CCCHY", g_level("X:CCCHY"), "x", "up", 3.0, 3.5, "How much worse the weakest borrowers are doing than junk overall. Rising = stress concentrating at the bottom."),
    ("Credit", "Banks tightening business loans", "DRTSCILM", g_level("DRTSCILM"), "net %", "up", 10, 25, "Fed loan officer survey. Credit crunch gauge."),
    ("Credit", "Banks tightening credit cards", "DRTSCLCC", g_level("DRTSCLCC"), "net %", "up", 10, 25, "Fed loan officer survey, consumer side."),
    ("Conditions", "Chicago Fed financial conditions", "NFCI", g_level("NFCI"), "", "up", -0.3, 0.0, "Above 0 = tighter than average."),
    ("Conditions", "St. Louis Fed stress index", "STLFSI4", g_level("STLFSI4"), "", "up", 0.0, 1.0, "Above 0 = above-normal stress."),
    ("Conditions", "NFCI credit component", "NFCICREDIT", g_level("NFCICREDIT"), "", "up", -0.2, 0.0, "Credit part of the Chicago Fed index. Above 0 = tighter than average."),
    ("Conditions", "NFCI leverage component", "NFCILEVERAGE", g_level("NFCILEVERAGE"), "", "up", -0.1, 0.25, "Leverage part of the Chicago Fed index. Rising = leverage building."),
    ("Conditions", "VIX", "VIXCLS", g_level("VIXCLS"), "", "up", 20, 27, "Fear gauge for stocks."),
    ("Conditions", "VIX curve (1-mo vs 3-mo)", "X:VIXT", g_level("X:VIXT"), "x", "up", 0.95, 1.0, "Above 1.0 = inverted: traders paying up for protection right now. Often leads selloffs."),
    ("Conditions", "Market breadth, 3-mo", "X:BREADTH", g_change("X:BREADTH", 90), "pct", "down", -0.03, -0.06, "Equal-weight vs cap-weight S&P (RSP vs SPY). Falling = fewer stocks carrying the index."),
    ("Dollar & oil", "Broad dollar, 3-mo change", "DTWEXBGS", g_change("DTWEXBGS", 90), "pct", "up", 0.02, 0.04, "A fast-rising dollar squeezes global dollar borrowers."),
    ("Dollar & oil", "Brent crude", "DCOILBRENTEU", g_level("DCOILBRENTEU"), "$", "up", 85, 100, "War and supply strain. Feeds inflation."),
    ("Labor", "Sahm rule", "SAHMREALTIME", g_level("SAHMREALTIME"), "", "up", 0.3, 0.5, "0.5 or higher has historically meant recession."),
    ("Labor", "Initial jobless claims", "ICSA", g_level("ICSA", 0.001), "k", "up", 250, 300, "Weekly layoffs."),
    ("Labor", "Continuing claims", "CCSA", g_level("CCSA", 1e-6), "M", "up", 1.95, 2.1, "How hard it is to land the next job."),
    ("Labor", "U-6 underemployment", "U6RATE", g_level("U6RATE"), "%", "up", 8.5, 10, "Broad slack, including part-timers who want more work."),
    ("Households", "Personal savings rate", "PSAVERT", g_level("PSAVERT"), "%", "down", 4.5, 3.5, "Low = consumers running out of cushion."),
    ("Households", "Credit card delinquency", "DRCCLACBS", g_level("DRCCLACBS"), "%", "up", 3.0, 4.0, "Bank card loans 30+ days late (quarterly)."),
    ("Inflation", "CPI, 6-mo annualized", "CPIAUCSL", g_cpi6, "pct", "up", 0.03, 0.04, "Official inflation, recent pace."),
    ("Inflation", "10-yr breakeven inflation", "T10YIE", g_level("T10YIE"), "%", "up", 2.5, 3.0, "What bonds expect inflation to average."),
    ("Fiscal", "Federal interest / receipts", "A091RC1Q027SBEA", g_ratio("A091RC1Q027SBEA", "FGRECPT"), "pct", "up", 0.15, 0.20, "Share of tax revenue eaten by interest (Gromen's core point)."),
    ("Fiscal", "Federal deficit, % of GDP", "FYFSGDA188S", g_level("FYFSGDA188S"), "%", "down", -3, -6, "Annual, fiscal year. Negative = deficit."),
    ("Fiscal", "Federal debt, % of GDP", "GFDEGDQ188S", g_level("GFDEGDQ188S"), "%", "up", 100, 115, "Total public debt vs the economy."),
]

# =========================================================================== crisis core: the six lights
# Each takes (S, P, t) and returns dict(st, value, detail) as of date t.

def L_dollar(S, P, t):
    s = S["DTWEXBGS"]; v, v6, v3 = at(s, t), at(s, t - days(182)), at(s, t - days(91))
    hi = max_between(s, t - days(365), t)
    if not num(v, v6, v3, hi): return dict(st=N, value="n/a", detail="")
    c6, c3 = v / v6 - 1, v / v3 - 1
    st = R if (c6 >= 0.05 or v >= hi * 0.998) else Y if (c6 >= 0.025 or c3 >= 0.02) else G
    return dict(st=st, value=pct(c6), detail=f"6-mo change · {pct(v / hi - 1)} vs 52-wk high")

def L_us10(S, P, t):
    s = S["DGS10"]; v, v1 = at(s, t), at(s, t - days(30))
    if not num(v, v1): return dict(st=N, value="n/a", detail="")
    c = v - v1
    st = R if (v >= 5.5 or c >= 0.5) else Y if (v >= 4.75 or c >= 0.25) else G
    return dict(st=st, value=f"{v:.2f}%", detail=f"{c:+.2f} pts in 1 month")

def L_japan(S, P, t):
    ys = (P.get("JPY=X") or {}).get("series") or S["DEXJPUS"]
    u, u1 = at(ys, t), at(ys, t - days(30))
    j, j6 = at(S["IRLTLT01JPM156N"], t), at(S["IRLTLT01JPM156N"], t - days(182))
    sts, bits = [], []
    if num(u, u1):
        yen = u1 / u - 1  # positive = yen stronger vs dollar
        sts.append(R if yen >= 0.05 else Y if yen >= 0.025 else G); bits.append(f"Yen {pct(yen)} vs $ in 1 mo")
    if num(j, j6):
        jc = j - j6
        sts.append(R if jc >= 0.5 else Y if jc >= 0.25 else G); bits.append(f"JGB {jc:+.2f} pts in 6 mo")
    if not sts: return dict(st=N, value="n/a", detail="")
    return dict(st=worst(*sts), value=f"{j:.2f}%" if num(j) else "n/a", detail="Japan 10-yr · " + " · ".join(bits))

def L_credit(S, P, t):
    s = S["BAMLH0A0HYM2"]; v, v1 = at(s, t), at(s, t - days(30)); ccc = at(S["BAMLH0A3HYC"], t)
    vt = at(S["X:VIXT"], t)
    if not num(v, v1): return dict(st=N, value="n/a", detail="")
    c = v - v1
    st = R if (v >= 5 or c >= 1.0) else Y if (v >= 4 or c >= 0.5 or (num(ccc) and ccc >= 12) or (num(vt) and vt >= 1.0)) else G
    extra = f" · CCC {ccc:.1f}%" if num(ccc) else ""
    return dict(st=st, value=f"{v:.2f}%", detail=f"Junk spread, {c:+.2f} in 1 mo{extra}")

def L_funding(S, P, t):
    a, b = at(S["SOFR"], t), at(S["IORB"], t); cp = at(S["X:CP"], t)
    if not num(a, b): return dict(st=N, value="n/a", detail="")
    bp = (a - b) * 100
    st1 = R if bp >= 5 else Y if bp > 0.5 else G
    st2 = (R if cp >= 60 else Y if cp >= 30 else G) if num(cp) else N
    return dict(st=worst(st1, st2), value=f"{bp:+.0f} bp",
                detail="SOFR vs Fed reserve rate" + (f" \xb7 CP spread {cp:.0f} bp" if num(cp) else ""))

def fed_parts(S, t):
    ff, ff90 = at(S["DFF"], t), at(S["DFF"], t - days(90))
    two = at(S["DGS2"], t)
    c, c12 = at(S["CPIAUCSL"], t), at(S["CPIAUCSL"], t - days(365))
    ic = S["ICSA"]
    claims_now = [v for d, v in ic if t - days(28) < d <= t]
    claims_then = [v for d, v in ic if t - days(119) < d <= t - days(91)]
    hy, hy1 = at(S["BAMLH0A0HYM2"], t), at(S["BAMLH0A0HYM2"], t - days(30))
    tp, tp12 = at(S["TEMPHELPS"], t), at(S["TEMPHELPS"], t - days(365))
    p = dict(ff=ff, hike=(ff - ff90) if num(ff, ff90) else None, gap=(two - ff) if num(two, ff) else None,
             real=(ff - (c / c12 - 1) * 100) if num(ff, c, c12) else None,
             claims=(sum(claims_now) / len(claims_now)) / (sum(claims_then) / len(claims_then)) - 1 if claims_now and claims_then else None,
             hy=(hy - hy1) if num(hy, hy1) else None, temp=(tp / tp12 - 1) if num(tp, tp12) else None)
    det = []
    if num(p["claims"]) and p["claims"] >= 0.10: det.append("claims rising")
    if num(p["hy"]) and p["hy"] >= 0.5: det.append("spreads widening")
    if num(p["temp"]) and p["temp"] <= -0.03: det.append("temp jobs shrinking")
    p["deteriorating"] = det
    return p

def L_fed(S, P, t):
    p = fed_parts(S, t)
    if not num(p["ff"], p["hike"]): return dict(st=N, value="n/a", detail="")
    hiking = p["hike"] >= 0.2
    gap = p["gap"] if num(p["gap"]) else 0
    real = p["real"] if num(p["real"]) else 0
    if hiking and (gap <= -0.25 or p["deteriorating"]): st = R
    elif hiking or gap <= -0.5 or real >= 2.0: st = Y
    else: st = G
    act = f"Hiked {p['hike']:+.2f} in 90 days" if hiking else f"Cut {p['hike']:+.2f} in 90 days" if p["hike"] <= -0.2 else "On hold"
    return dict(st=st, value=f"{p['ff']:.2f}%", detail=f"Fed funds · {act.lower()}")

SHORT = {"dollar": "Dollar", "us10": "US 10Y", "japan": "Japan", "credit": "Credit", "funding": "Funding", "fed": "Fed"}
LIGHTS = [
    ("dollar", "Dollar wrecking ball", L_dollar, "Red: dollar up 5%+ in 6 months, or at a 52-week high"),
    ("us10", "US 10-yr yield", L_us10, "Red: 5.5%+, or up 0.5 pt in a month"),
    ("japan", "Japan and the yen carry", L_japan, "Red: yen up 5%+ vs $ in a month, or Japan 10-yr up 0.5 pt in 6 months"),
    ("credit", "Credit spreads", L_credit, "Red: junk spread 5%+, or up 1 pt in a month. Yellow also if CCC spreads pass 12% or the VIX curve inverts"),
    ("funding", "Funding plumbing", L_funding, "Red: overnight rates 5+ bp above the Fed's reserve rate, or commercial paper 60+ bp over T-bills"),
    ("fed", "Fed policy error", L_fed, "Red: Fed hiking while the bond market or early data says stop"),
]

def regime_from(lights, S, t):
    sts = [l["st"] for l in lights]
    if sts.count(N) >= 3: return "NO DATA"
    reds, yels = sts.count(R), sts.count(Y)
    sahm, hy = at(S["SAHMREALTIME"], t), at(S["BAMLH0A0HYM2"], t)
    if reds >= 4 or (num(sahm) and sahm >= 0.5) or (num(hy) and hy >= 6): return "RED"
    if reds >= 2 or reds + 0.5 * yels >= 2: return "YELLOW"
    return "GREEN"

# =========================================================================== main street (K-shape)

def yoy(S, sid, t):
    a, b = at(S[sid], t), at(S[sid], t - days(365))
    return a / b - 1 if num(a, b) else None

def _rows():
    rows = []
    def add(name, value, st, delta, worse, spark, asof, meaning, rule):
        rows.append(dict(name=name, value=value, st=st, delta=delta, worse=worse, spark=spark, asof=asof, meaning=meaning, rule=rule))
    return rows, add

def tail(S, sid, n=730):
    s = S[sid]; d = last(s)[0]
    return [v for dd, v in s if d and dd >= d - days(n)]

def jobs(S, P, dj):
    """Jobs and layoffs. Weekly and daily series show 3-month lines; monthly ones show 1 year."""
    rows, add = _rows()
    series_tail = lambda sid, n=100: tail(S, sid, n)
    v, v3 = at(S["IC4WSA"], TODAY), at(S["IC4WSA"], TODAY - days(91))
    if num(v, v3):
        c = v / v3 - 1
        add("Jobless claims, 4-wk avg", f"{v/1000:,.0f}k", R if (v >= 300000 or c >= 0.20) else Y if (v >= 250000 or c >= 0.10) else G,
            f"{pct(c)} in 3 mo", c > 0, series_tail("IC4WSA"), last(S["IC4WSA"])[0],
            "New unemployment filings, smoothed. The fastest official layoff gauge.", "Yellow at 250k or up 10% in 3 months; red at 300k or up 20%")
    v, v3 = at(S["CCSA"], TODAY), at(S["CCSA"], TODAY - days(91))
    if num(v, v3):
        c = v / v3 - 1
        add("Continuing claims", f"{v/1e6:.2f}M", R if (v >= 2.1e6 or c >= 0.10) else Y if (v >= 1.95e6 or c >= 0.05) else G,
            f"{pct(c)} in 3 mo", c > 0, series_tail("CCSA"), last(S["CCSA"])[0],
            "People still collecting. Rising = laid-off workers not finding jobs.", "Yellow at 1.95M or up 5% in 3 months; red at 2.1M or up 10%")
    v, v1 = at(S["JTSLDR"], TODAY), at(S["JTSLDR"], TODAY - days(91))
    if num(v, v1):
        add("Layoff rate (JOLTS)", f"{v:.1f}%", level(v, "up", 1.2, 1.5), f"{v - v1:+.1f} pts in 3 mo", v > v1,
            series_tail("JTSLDR", 400), last(S["JTSLDR"])[0], "Share of workers laid off or fired each month. Lags about 6 weeks.", "Yellow at 1.2%, red at 1.5%")
    for sid, nm, y, r, mean in [("IHLIDXUS", "Job postings, all (Indeed)", 100, 90, "New job ads vs Feb 2020 (=100). Hiring appetite in real time.")]:
        v, v3 = at(S[sid], TODAY), at(S[sid], TODAY - days(91))
        if num(v, v3):
            add(nm, f"{v:.0f}", level(v, "down", y, r), f"{pct(v / v3 - 1)} in 3 mo", v < v3, series_tail(sid), last(S[sid])[0],
                mean, f"Yellow under {y}, red under {r}")
    # young adults
    v, v1 = at(S["LNS14000036"], TODAY), at(S["LNS14000036"], TODAY - days(365))
    if num(v, v1):
        add("Unemployment, ages 20-24", f"{v:.1f}%", level(v, "up", 8.0, 9.5), f"{v - v1:+.1f} pts in 1 yr", v > v1,
            series_tail("LNS14000036", 400), last(S["LNS14000036"])[0],
            "Entry-level hiring. Where AI hits first.", "Yellow at 8%, red at 9.5%")
    # AI displacement
    for sid, nm, mean in [("CES6054150001", "Computer systems jobs", "Coding and IT services employment. An early AI displacement read."),
                          ("USINFO", "Information sector jobs", "Media, software, telecom and data work.")]:
        c = yoy(S, sid, TODAY)
        if num(c):
            add(nm, f"{pct(c)} in 1 yr", R if c <= -0.02 else Y if c < 0 else G, None, c < 0,
                series_tail(sid, 400), last(S[sid])[0], mean, "Red: down 2%+ in a year. Yellow: any decline")
    c = yoy(S, "TEMPHELPS", TODAY)
    if num(c):
        add("Temp help jobs", f"{pct(c)} in 1 yr", R if c <= -0.05 else Y if c < 0 else G, None, c < 0,
            series_tail("TEMPHELPS", 400), last(S["TEMPHELPS"])[0], "Companies cut temps first. Has rolled over before every recession.",
            "Red: down 5%+ in a year. Yellow: any decline")
    v, v1 = at(S["JTSQUR"], TODAY), at(S["JTSQUR"], TODAY - days(365))
    if num(v, v1):
        add("Quits rate", f"{v:.1f}%", level(v, "down", 2.2, 1.9), f"{v - v1:+.1f} pts in 1 yr", v < v1,
            series_tail("JTSQUR", 400), last(S["JTSQUR"])[0], "Low = workers afraid to leave their jobs.", "Yellow under 2.2%, red under 1.9%")
    return rows

def main_street(S, P):
    rows, add = _rows()
    series_tail = lambda sid, n=730: tail(S, sid, n)

    # wealth split
    b, b1 = at(S["WFRBSB50215"], TODAY), at(S["WFRBSB50215"], TODAY - days(365)); top = at(S["WFRBST01134"], TODAY)
    if num(b, b1):
        c = b - b1
        add("Bottom 50%'s share of wealth", f"{b:.1f}%", R if c <= -0.3 else Y if c < 0 else G,
            f"{c:+.1f} pts in 1 yr", c < 0, series_tail("WFRBSB50215", 1500), last(S["WFRBSB50215"])[0],
            f"The top 1% hold {top:.1f}%. The clearest measure of the K." if num(top) else "The clearest measure of the K.",
            "Red: down 0.3+ pt in a year. Yellow: any decline")
    # necessities
    parts = [(nm, yoy(S, sid, TODAY)) for sid, nm in [("CUSR0000SAF11", "Groceries"), ("CUSR0000SAH1", "Shelter"),
                                                      ("CUSR0000SEHF01", "Electricity"), ("CUSR0000SETE", "Car insurance")]]
    ok = [c for _, c in parts if num(c)]
    if ok:
        avg = sum(ok) / len(ok)
        add("Necessities inflation", f"{pct(avg)} in 1 yr", level(avg, "up", 0.03, 0.05), None, True,
            series_tail("CUSR0000SEHF01"), last(S["CUSR0000SAF11"])[0],
            "Average of: " + ", ".join(f"{nm} {pct(c)}" for nm, c in parts if num(c)) + ". Bars show electricity.",
            "Yellow at 3%, red at 5%")
    v, v1 = at(S["UMCSENT"], TODAY), at(S["UMCSENT"], TODAY - days(90))
    if num(v, v1):
        add("Consumer sentiment", f"{v:.0f}", level(v, "down", 75, 60), f"{v - v1:+.0f} in 3 mo", v < v1,
            series_tail("UMCSENT"), last(S["UMCSENT"])[0], "University of Michigan survey. How households feel.", "Yellow under 75, red under 60")
    v, v1 = at(S["DRCCLACBS"], TODAY), at(S["DRCCLACBS"], TODAY - days(365))
    if num(v, v1):
        add("Credit card delinquency", f"{v:.2f}%", level(v, "up", 3.0, 4.0), f"{v - v1:+.2f} pts in 1 yr", v > v1,
            series_tail("DRCCLACBS", 1500), last(S["DRCCLACBS"])[0], "Bank card loans 30+ days late.", "Yellow at 3%, red at 4%")
    v, v1 = at(S["DRCRELEXFACBS"], TODAY), at(S["DRCRELEXFACBS"], TODAY - days(365))
    if num(v, v1):
        add("Commercial real estate delinquency", f"{v:.2f}%", level(v, "up", 1.5, 3.0), f"{v - v1:+.2f} pts in 1 yr", v > v1,
            series_tail("DRCRELEXFACBS", 1500), last(S["DRCRELEXFACBS"])[0], "Bank CRE loans 30+ days late.", "Yellow at 1.5%, red at 3%")
    for sym, nm, mean in [("BIZD", "Private credit lenders (BIZD)", "Listed private credit funds. Falling prices = stress in private loans."),
                          ("KRE", "Regional banks (KRE)", "The banks most exposed to commercial real estate.")]:
        p = P.get(sym)
        if p:
            off = p["price"] / p["high52"] - 1
            m1 = p["price"] / p["closes"][-22] - 1 if len(p["closes"]) > 22 else None
            add(nm, f"{pct(off)} from high", R if off <= -0.15 else Y if off <= -0.07 else G,
                f"{pct(m1)} in 1 mo" if num(m1) else None, num(m1) and m1 < 0, p["closes"], TODAY, mean,
                "Yellow at 7% off the high, red at 15%")
    return rows

# =========================================================================== build-up risk (slow, 2-3 year horizon)

def ordinal(n):
    n = int(round(n)); suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"

def pctile(hist, v):
    h = [x for x in hist if num(x)]
    return 100 * sum(1 for x in h if x <= v) / len(h) if h and num(v) else None

def three_yr(series, fn):
    """fn(v_now, v_3y_ago) on quarterly-ish points."""
    out = []
    for d, v in series:
        p = at(series, d - days(1095))
        if num(p) and d >= series[0][0] + days(1095): out.append((d, fn(v, p)))
    return out

def quarterly(series):
    q, seen = [], set()
    for d, v in series:
        k = (d.year, (d.month - 1) // 3)
        if k in seen: q[-1] = (d, v)
        else: seen.add(k); q.append((d, v))
    return q

def buildup(S, spx):
    rows, add = _rows()
    v, v1 = at(S["TDSP"], TODAY), at(S["TDSP"], TODAY - days(365))
    if num(v, v1):
        add("Household debt service", f"{v:.1f}% of income", level(v, "up", 10.5, 12.0), f"{v - v1:+.1f} pts in 1 yr", v > v1,
            tail(S, "TDSP", 3650), last(S["TDSP"])[0], "Mortgage and consumer debt payments as a share of disposable income.",
            "Yellow at 10.5%, red at 12% (the 2007 peak was about 13%)")
    gdp = S["GDP"]
    for nm, credit_id, asset, asset_nm in [("Business red zone", "BCNSDODNS", quarterly(spx), "stock returns"),
                                           ("Household red zone", "CMDEBT", quarterly(S["CSUSHPINSA"]), "home prices")]:
        ratio = quarterly(join(S[credit_id], gdp, lambda a, b: a / b))
        cg = three_yr(ratio, lambda a, b: a / b - 1)
        ag = three_yr(asset, lambda a, b: a / b - 1)
        if not cg or not ag: continue
        cp_, ap_ = pctile([x for _, x in cg], cg[-1][1]), pctile([x for _, x in ag], ag[-1][1])
        if not num(cp_, ap_): continue
        hot_c, hot_a = cp_ >= 80, ap_ >= 67
        st = R if (hot_c and hot_a) else Y if (hot_c or (cp_ >= 60 and hot_a)) else G
        word = "Red zone" if st == R else "Credit hot" if hot_c else "Assets hot" if hot_a else "Not hot"
        add(nm, word, st, f"credit {ordinal(cp_)} \xb7 {asset_nm.split()[0]} {ordinal(ap_)} pctile", "note", [x for _, x in cg][-40:], cg[-1][0],
            f"3-yr credit growth (vs GDP) is in the {ordinal(cp_)} percentile of history; 3-yr {asset_nm} in the {ordinal(ap_)}. "
            f"Greenwood and Shleifer found crisis odds jump from about 7% to 40% when both run hot. Line shows credit growth.",
            "Red: credit in the top 20% of history AND assets in the top third. Yellow: credit hot alone")
    return rows

# =========================================================================== watchlist logic

def tech(p):
    c = p["closes"]; price = p["price"]
    a20 = sum(c[-20:]) / len(c[-20:]); a200 = sum(c[-200:]) / len(c[-200:])
    lo, hi = min(c[-20:] + [price]), max(c[-20:] + [price])
    rng = (price - lo) / (hi - lo) if hi > lo else 0.5
    vs20 = price / a20 - 1
    trend = "Uptrend" if price >= a200 else "Downtrend"
    setup = "Oversold" if (rng <= 0.2 or vs20 <= -0.05) else "Overbought" if (rng >= 0.85 and vs20 >= 0.04) else "Neutral"
    m1 = price / c[-22] - 1 if len(c) > 22 else None
    return dict(a20=a20, a200=a200, rng=rng, vs20=vs20, trend=trend, setup=setup,
                off_high=price / p["high52"] - 1, day=price / p["prev"] - 1 if p["prev"] else None, m1=m1)

def price_status(m):
    """Price tiles: green = uptrend, red = downtrend and still falling, yellow = mixed."""
    if m["trend"] == "Uptrend": return G if (not num(m["m1"]) or m["m1"] > -0.05) else Y
    return R if (num(m["m1"]) and m["m1"] < 0) else Y

def signal(regime, setup, trend):
    if regime == "NO DATA": return ("Awaiting data", "grey", "None")
    if regime == "RED":
        if setup == "Oversold": return ("Wait: falling knife", "yellow", "None")
        if setup == "Overbought": return ("Trim / raise cash", "red", "None")
        return ("No new buys", "red", "None")
    if regime == "YELLOW":
        if setup == "Oversold":
            return ("Nibble 1/3", "green", "1/3 of planned") if trend == "Uptrend" else ("Watch", "yellow", "None")
        if setup == "Overbought": return ("Trim", "red", "None")
        return ("Hold", "yellow", "None")
    if setup == "Oversold":
        return ("Buy dip", "green", "Full planned size") if trend == "Uptrend" else ("Nibble 1/3", "green", "1/3 of planned")
    if setup == "Overbought": return ("Hold, don't chase", "yellow", "None")
    return ("Hold / add", "green", "Up to 1/2 planned") if trend == "Uptrend" else ("Hold", "yellow", "None")

# =========================================================================== build

FRED_IDS = sorted(({g[2] for g in GAUGES} | {"IORB", "FGRECPT", "DFF", "WALCL", "DGS2", "DEXJPUS", "IRLTLT01JPM156N",
    "WFRBST01134", "WFRBSB50215", "LNS14000036", "CES6054150001", "USINFO", "TEMPHELPS", "JTSQUR",
    "CUSR0000SAF11", "CUSR0000SAH1", "CUSR0000SEHF01", "CUSR0000SETE", "UMCSENT", "DRCRELEXFACBS", "BAMLH0A3HYC",
    "RIFSPPFAAD90NB", "DTB3", "IC4WSA", "JTSLDR", "IHLIDXUS", "TDSP"} | {w[0] for w in WHITE_COLLAR}) - {"X:CP", "X:CCCHY", "X:VIXT", "X:BREADTH"})
LONG_IDS = ["BCNSDODNS", "CMDEBT", "CSUSHPINSA", "GDP"]

def build():
    S = {i: fred(i) for i in FRED_IDS}
    S.update({i: fred(i, days=26000) for i in LONG_IDS})
    P = {sym: prices(sym) for sym in ["JPY=X", "SPY", "RSP", "^VIX", "^VIX3M", "BIZD", "KRE", "GC=F", "SI=F", "BTC-USD", "GLD", "SLV", "GDX"]}
    ser = lambda sym: (P.get(sym) or {}).get("series") or []
    # derived series
    S["X:CP"] = join(S["RIFSPPFAAD90NB"], S["DTB3"], lambda a, b: (a - b) * 100)
    S["X:CCCHY"] = join(S["BAMLH0A3HYC"], S["BAMLH0A0HYM2"], lambda a, b: a / b)
    S["X:VIXT"] = join(ser("^VIX"), ser("^VIX3M"), lambda a, b: a / b)
    S["X:BREADTH"] = join(ser("RSP"), ser("SPY"), lambda a, b: a / b)
    spx = prices_long("^GSPC")
    dj = djc()

    # ---- crisis core
    core, core_prev = [], []
    for key, name, fn, rule in LIGHTS:
        now = fn(S, P, TODAY); prev = fn(S, P, MONTH_AGO)
        core.append(dict(key=key, name=name, rule=rule, prev=prev["st"], **now)); core_prev.append(prev)
    calc = regime_from(core, S, TODAY); calc_prev = regime_from(core_prev, S, MONTH_AGO)
    override = ""
    if os.path.exists("regime_override.txt"):
        for line in open("regime_override.txt"):
            w = line.split("#")[0].strip().upper()
            if w in ("GREEN", "YELLOW", "RED"): override = w
    regime = override or calc
    reds = sum(1 for l in core if l["st"] == R); yels = sum(1 for l in core if l["st"] == Y)
    reds_prev = sum(1 for l in core_prev if l["st"] == R)

    # ---- gauges
    gauges = []
    for cat, name, sid, fn, unit, direction, y, r, meaning in GAUGES:
        v, v0 = fn(S, TODAY), fn(S, MONTH_AGO)
        d = v - v0 if num(v, v0) else None
        asof = last(S[sid])[0]
        spark = [vv for dd, vv in S[sid] if asof and dd >= asof - days(365)]
        gauges.append(dict(cat=cat, name=name, v=v, unit=unit, st=level(v, direction, y, r), d=d,
                           worse=(d > 0 if direction == "up" else d < 0) if num(d) else None,
                           y=y, r=r, direction=direction, meaning=meaning, spark=spark, asof=asof, sid=sid))

    # bull re-steepening: curve up 0.5+ in 3 months while the 2-yr falls 0.5+
    cv, cv3, tw, tw3 = at(S["T10Y2Y"], TODAY), at(S["T10Y2Y"], TODAY - days(91)), at(S["DGS2"], TODAY), at(S["DGS2"], TODAY - days(91))
    if num(cv, cv3, tw, tw3):
        dc, d2 = cv - cv3, tw - tw3
        st = R if (dc >= 0.5 and d2 <= -0.5) else Y if (dc >= 0.25 and d2 <= -0.25) else G
        i = next(k for k, g in enumerate(gauges) if g["sid"] == "T10Y2Y")
        gauges.insert(i + 1, dict(cat="Rates & Treasuries", name="Bull re-steepening", v=dc, unit="pts", st=st,
            vtext={R: "Yes", Y: "Watch", G: "No"}[st], d=None, worse=None,
            y=None, r=None, direction="up", meaning=f"Curve {dc:+.2f} pts in 3 months while the 2-yr moved {d2:+.2f}. A steepening driven by falling short rates has often come right as recessions start.",
            spark=tail(S, "T10Y2Y", 365), asof=last(S["T10Y2Y"])[0], sid="T10Y2Y", rule="Red: curve up 0.5+ pt in 3 months while the 2-yr falls 0.5+. Yellow at half those moves"))

    # ---- pivot watch
    def safe(f):
        try: return f()
        except Exception: return None
    dff, walcl, dxy, hy, t10 = S["DFF"], S["WALCL"], S["DTWEXBGS"], S["BAMLH0A0HYM2"], S["DGS10"]
    pivots = []
    v = safe(lambda: at(dff, TODAY) - at(dff, TODAY - days(90)))
    pivots.append(("Fed has started cutting", f"Fed funds {v:+.2f} pts vs 90 days ago" if num(v) else "n/a", num(v) and v <= -0.25, "down 0.25+"))
    v = safe(lambda: at(walcl, TODAY) / at(walcl, TODAY - days(90)) - 1)
    pivots.append(("Fed balance sheet growing", f"{pct(v)} vs 90 days ago", num(v) and v >= 0.01, "up 1%+"))
    v = safe(lambda: at(dxy, TODAY) / max_between(dxy, TODAY - days(90), TODAY) - 1)
    pivots.append(("Dollar rolling over", f"{pct(v)} vs 90-day high", num(v) and v <= -0.03, "3%+ off its high"))
    hmax = safe(lambda: max_between(hy, TODAY - days(120), TODAY))
    v = safe(lambda: hmax - at(hy, TODAY))
    pivots.append(("Junk spreads peaked and easing", f"{v:.2f} pts off 120-day high" if num(v) else "n/a", num(v, hmax) and hmax >= 5 and v >= 0.75, "peak 5%+, then down 0.75+"))
    v = safe(lambda: max_between(t10, TODAY - days(90), TODAY) - at(t10, TODAY))
    pivots.append(("10-yr yield off its highs", f"{v:.2f} pts below 90-day high" if num(v) else "n/a", num(v) and v >= 0.4, "0.40+ below high"))
    npiv = sum(1 for p in pivots if p[2])

    # ---- blood in the streets
    spy = P["SPY"]; off = (spy["price"] / spy["high52"] - 1) if spy else None
    def peak_now(s):
        return max_between(s, TODAY - days(120), TODAY), at(s, TODAY)
    caps = []
    pk, v = peak_now(S["VIXCLS"])
    caps.append(("VIX spiked, then calmed", f"120-day peak {pk:.1f}, now {v:.1f}" if num(pk, v) else "n/a", num(pk, v) and pk >= 40 and v < 30, "peak above 40, now below 30"))
    pk, v = peak_now(hy)
    caps.append(("Junk spreads blew out, now easing", f"120-day peak {pk:.2f}%, now {v:.2f}%" if num(pk, v) else "n/a", num(pk, v) and pk >= 7 and v <= pk - 1, "peak above 7%, now 1+ pt lower"))
    pk, v = peak_now(S["STLFSI4"])
    caps.append(("Stress index peaked and falling", f"120-day peak {pk:.2f}, now {v:.2f}" if num(pk, v) else "n/a", num(pk, v) and pk >= 2 and v <= pk - 0.5, "peak above 2, now 0.5+ lower"))
    caps.append(("Rescue under way", f"{npiv} of 5 pivot signals lit", npiv >= 2, "2+ pivot signals"))
    caps.append(("Stocks deeply marked down", f"SPY {pct(off)} from 52-wk high" if num(off) else "n/a", num(off) and off <= -0.25, "SPY 25%+ below its high"))
    ncap = sum(1 for c in caps if c[2]); capitulation = ncap >= 3

    # ---- fed tile
    fp = fed_parts(S, TODAY)

    # ---- dale vs karsan
    duel = []
    icsa, sahm = at(S["ICSA"], TODAY), at(S["SAHMREALTIME"], TODAY)
    if num(icsa, sahm):
        k = icsa / 1000
        side = "Karsan" if (k >= 250 or sahm >= 0.5) else "Dale" if (k < 230 and sahm < 0.3) else None
        duel.append(("Recession?", f"Claims {k:,.0f}k, Sahm {sahm:.2f}", side, "Dale: no recession (claims under 230k)", "Karsan: claims 250k+ or Sahm 0.5+"))
    y10 = at(t10, TODAY)
    if num(y10):
        duel.append(("10-yr yield", f"{y10:.2f}%", "Karsan" if y10 >= 5.5 else "Dale" if y10 <= 4.75 else None, "Dale: back under 4.75%", "Karsan: 5.5% or higher"))
    hyv = at(hy, TODAY); hylo = min_between(hy, TODAY - days(120), TODAY)
    if num(hyv):
        side = "Karsan" if (hyv >= 5 or (num(hylo) and hyv - hylo >= 1.5)) else "Dale" if hyv <= 4 else None
        duel.append(("Credit", f"Junk spread {hyv:.2f}%", side, "Dale: stays under 4%", "Karsan: 5%+ or a 1.5-pt blowout"))
    if num(off):
        side = "Karsan" if off <= -0.25 else "Dale" if -0.20 <= off <= -0.10 else None
        duel.append(("Size of the drop", f"SPY {pct(off)} from high", side, "Dale: a 10-20% correction", "Karsan: a 25-40% crash"))
    if spy and len(spy["closes"]) > 22:
        m1 = spy["price"] / spy["closes"][-22] - 1
        if TODAY <= dt.date(2026, 12, 31):
            duel.append(("Year-end direction", f"SPY {pct(m1)} over 1 month", "Dale" if m1 <= -0.02 else "Karsan" if m1 >= 0.02 else None,
                         "Dale: risk-off, stocks drift lower", "Karsan: positive year-end flows lift stocks"))
        elif TODAY <= dt.date(2027, 3, 31):
            duel.append(("Crisis window (Jan-Mar 2027)", f"SPY {pct(off)} from high", "Karsan" if off <= -0.20 else None,
                         "Dale: no crash in the window", "Karsan: crash by end of March"))
        elif TODAY <= dt.date(2027, 6, 30):
            duel.append(("Crisis window verdict", f"SPY {pct(off)} from high", "Dale" if off > -0.20 else "Karsan",
                         "Dale: window passed without a crash", "Karsan: bear market by mid-2027"))
    nd = sum(1 for x in duel if x[2] == "Dale"); nk = sum(1 for x in duel if x[2] == "Karsan")

    # ---- main street, jobs, build-up
    ms = main_street(S, P)
    jb = jobs(S, P, dj)
    bu = buildup(S, spx)
    weeks = djc_weeks(dj) if dj.get("ok") else []
    wc = []
    for sid, nm in WHITE_COLLAR:
        v, v3 = at(S[sid], TODAY), at(S[sid], TODAY - days(91))
        if num(v):
            wc.append(dict(name=nm, v=v, c3=(v / v3 - 1) if num(v3) else None, st=R if v < 75 else Y if v < 95 else G, asof=last(S[sid])[0]))
    wc.sort(key=lambda r: r["v"])
    wc_all = at(S["IHLIDXUS"], TODAY)

    # ---- hard assets
    names = [("GC=F", "Gold", "$/oz"), ("SI=F", "Silver", "$/oz"), ("BTC-USD", "Bitcoin", ""), ("GLD", "GLD", "gold ETF"),
             ("SLV", "SLV", "silver ETF"), ("GDX", "GDX", "gold miners")]
    hard = []
    for sym, nm, sub in names:
        p = P.get(sym)
        if p and len(p["closes"]) > 25:
            m = tech(p)
            hard.append(dict(name=nm, sub=sub, price=p["price"], st=price_status(m), spark=p["closes"], **m))
        else:
            hard.append(dict(name=nm, sub=sub, price=None, st=N, spark=[], day=None, m1=None, off_high=None, trend=""))
    g_, s_, brent = (P["GC=F"] or {}).get("price"), (P["SI=F"] or {}).get("price"), at(S["DCOILBRENTEU"], TODAY)
    ratios = [("Gold / silver", g_ / s_ if num(g_, s_) else None, "above 80 = silver cheap vs gold"),
              ("Gold / Brent", g_ / brent if num(g_, brent) else None, "rising = gold gaining vs energy")]

    # ---- watchlist
    tickers = []
    if os.path.exists("watchlist.txt"):
        for line in open("watchlist.txt"):
            t = line.split("#")[0].strip().upper()
            if t and t not in tickers: tickers.append(t)
    watch = []
    for t in tickers[:60]:
        p = P.get(t) or prices(t)
        if not p or len(p["closes"]) < 25:
            watch.append(dict(t=t, ok=False)); continue
        m = tech(p)
        lab, tone, size = signal(regime, m["setup"], m["trend"])
        if capitulation and m["setup"] != "Overbought":
            lab, tone, size = "Capitulation: buy 1/3", "green", "1/3 of planned"
        watch.append(dict(t=t, ok=True, price=p["price"], sig=lab, tone=tone, size=size, spark=p["closes"][-130:], **m))

    os.makedirs(OUT, exist_ok=True)
    d = dict(regime=regime, calc=calc, calc_prev=calc_prev, override=override, core=core, reds=reds, yels=yels,
             reds_prev=reds_prev, gauges=gauges, pivots=pivots, npiv=npiv, caps=caps, ncap=ncap, capitulation=capitulation,
             fp=fp, fed_light=core[-1], duel=duel, nd=nd, nk=nk, ms=ms, jb=jb, bu=bu, weeks=weeks, dj=dj, wc=wc, wc_all=wc_all, hard=hard, ratios=ratios, watch=watch, off=off)
    open(os.path.join(OUT, "index.html"), "w").write(render(d))
    open(os.path.join(OUT, "manifest.webmanifest"), "w").write(json.dumps({
        "name": "System Health", "short_name": "Sys Health", "start_url": "./", "display": "standalone",
        "background_color": "#0f1a14", "theme_color": "#0f1a14", "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png"}]}))
    write_icon(os.path.join(OUT, "icon.png"))
    slim = {"updated": NOW.isoformat(), "regime": regime, "regime_month_ago": calc_prev,
            "crisis_core": [{k: v for k, v in l.items()} for l in core],
            "gauges": [{k: v for k, v in g.items() if k != "spark"} for g in gauges],
            "main_street": [{k: v for k, v in r.items() if k != "spark"} for r in ms],
            "watchlist": [{k: v for k, v in w.items() if k != "spark"} for w in watch]}
    json.dump(slim, open(os.path.join(OUT, "data.json"), "w"), default=str, indent=1)
    json.dump({"fetched": dj.get("fetched"), "ok": dj.get("ok"), "ests": dj.get("ests", {}),
               "entries": [(str(d_), n_, j_) for d_, n_, j_ in dj.get("entries", [])]}, open(os.path.join(OUT, "djc.json"), "w"))
    print(f"Built: regime {regime} (month ago {calc_prev}), {reds} red / {yels} yellow lights, {len(watch)} tickers")

# =========================================================================== render

E = html.escape
TONE = {G: "green", Y: "yellow", R: "red", N: "grey", "GREEN": "green", "YELLOW": "yellow", "RED": "red", "NO DATA": "grey"}
WORD = {G: "Healthy", Y: "Caution", R: "Stress", N: "No data"}

def spark(vals, w=100, h=28, cls=""):
    vals = [v for v in vals if num(v)][-260:]
    if len(vals) < 3: return ""
    lo, hi = min(vals), max(vals)
    if hi == lo: hi = lo + 1
    pts = [(i * w / (len(vals) - 1), h - 3 - (v - lo) / (hi - lo) * (h - 6)) for i, v in enumerate(vals)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    x, y = pts[-1]
    return (f'<svg class="spark {cls}" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
            f'<polyline points="{poly}"/><circle cx="{x:.1f}" cy="{y:.1f}" r="2.4"/></svg>')

def arrow(delta_text, worse):
    """Direction chip: arrow up/down by sign, color by better/worse."""
    if not delta_text: return ""
    if worse == "note": return f'<span class="arr flat">{E(delta_text)}</span>'
    if re.match(r"^[+-]?0(\.0+)?(\s|%|$)", delta_text.strip()):
        tail_ = re.search(r"in \S+ \S+$", delta_text.strip())
        return f'<span class="arr flat">&#9644; no change{" " + E(tail_.group(0)) if tail_ else ""}</span>'
    up = not delta_text.strip().startswith("-")
    cls = "worse" if worse else "better" if worse is False else "flat"
    return f'<span class="arr {cls}">{"&#9650;" if up else "&#9660;"} {E(delta_text.lstrip("+-"))}</span>'

def chg_class(v):
    return "pos" if num(v) and v > 0 else "neg" if num(v) and v < 0 else ""

def render(d):
    reg = d["regime"]; tone = TONE[reg]
    guide = {"GREEN": "Healthy. Buy dips in uptrends.", "YELLOW": "Stress building. Small, staged buys only.",
             "RED": "Stress high. Protect capital, keep dry powder.", "NO DATA": "Not enough data yet to judge."}[reg]
    deploy = {"GREEN": "Full planned size", "YELLOW": "1/3 size, in tranches", "RED": "None", "NO DATA": "Wait"}[reg]
    if d["capitulation"]: deploy = "1/3 size (capitulation)"
    rp = d["reds_prev"]; rn = d["reds"]
    trend_txt = ("worse than" if rn > rp else "better than" if rn < rp else "same as") + f" a month ago ({rp})"
    trend_cls = "worse" if rn > rp else "better" if rn < rp else "flat"
    regime_prev = d["calc_prev"]

    # ---------------- crisis core
    core = []
    for l in d["core"]:
        t = TONE[l["st"]]
        moved = {(R, Y): "better", (R, G): "better", (Y, G): "better", (G, Y): "worse", (G, R): "worse", (Y, R): "worse"}.get((l["prev"], l["st"]), "flat")
        if moved == "flat": was = '<span class="was flat">Unchanged from a month ago</span>'
        else: was = f'<span class="was {moved}">{"&#9650; Worse" if moved=="worse" else "&#9660; Better"}: was {TONE[l["prev"]]} a month ago</span>'
        core.append(f'''<details class="light s-{t}" id="light-{l["key"]}"><summary><div class="lt"><span class="lamp"></span>{E(l["name"])}</div>
<div class="lv">{E(l["value"])}</div><div class="ld">{E(l["detail"])}</div>{was}</summary><div class="lr">{E(l["rule"])}</div></details>''')

    # ---------------- checklists (blood, pivot)
    def checklist(items):
        return "".join(f'''<li class="{'hit' if hit else ''}"><span class="dot"></span><div><b>{E(n)}</b><small>{E(reading)} &middot; trigger: {E(trig)}</small></div><span class="yn">{'YES' if hit else 'no'}</span></li>'''
                       for n, reading, hit, trig in items)
    nc = d["ncap"]
    capv = ("ON: start buying in 1/3 tranches, even if the regime reads red", "green") if d["capitulation"] else \
           ("Close: 2 of 5. Line up your buy list and cash", "yellow") if nc == 2 else ("Off: no washout yet", "grey")
    npv = d["npiv"]
    pivv = ("Confirmed: deploy dry powder in tranches", "green") if npv >= 4 else ("Forming: consider a first tranche", "yellow") if npv >= 2 else ("Not yet: keep dry powder", "grey")

    # ---------------- fed tile
    fp = d["fp"]; fl = d["fed_light"]
    def frow(label, val, bad, note):
        cls = "s-red" if bad is True else "s-green" if bad is False else "s-grey"
        return f'<li class="frow"><span>{E(label)}<small>{E(note)}</small></span><b class="pill {cls}">{E(val)}</b></li>'
    fed_rows = "".join([
        frow("Fed funds rate", f"{fp['ff']:.2f}%" if num(fp["ff"]) else "n/a", None, "Where the Fed has set rates"),
        frow("Rate change, last 90 days", (f"{fp['hike']:+.2f} pts" if num(fp["hike"]) else "n/a"), (fp["hike"] >= 0.2) if num(fp["hike"]) else None, "Hiking = red"),
        frow("2-yr yield vs fed funds", (f"{fp['gap']:+.2f} pts" if num(fp["gap"]) else "n/a"), (fp["gap"] <= -0.25) if num(fp["gap"]) else None, "Below 0 = market expects cuts; the Fed may be too tight"),
        frow("Real fed funds rate", (f"{fp['real']:+.2f}%" if num(fp["real"]) else "n/a"), (fp["real"] >= 2.0) if num(fp["real"]) else None, "Fed funds minus CPI inflation; 2%+ is very tight"),
        frow("Jobless claims, 4 wks vs 3 mo ago", pct(fp["claims"]), (fp["claims"] >= 0.10) if num(fp["claims"]) else None, "Up 10%+ = layoffs building"),
        frow("Junk spreads, 1 mo", (f"{fp['hy']:+.2f} pts" if num(fp["hy"]) else "n/a"), (fp["hy"] >= 0.5) if num(fp["hy"]) else None, "Up 0.5+ = credit tightening"),
        frow("Temp help jobs, 1 yr", pct(fp["temp"]), (fp["temp"] <= -0.03) if num(fp["temp"]) else None, "Down 3%+ = firms cutting"),
    ])
    fed_verdict = {R: "Policy error risk HIGH: hiking into a weakening economy", Y: "Policy is tight. Watch for the data to crack",
                   G: "No sign of a policy error", N: "No data"}[fl["st"]]

    # ---------------- generic metric row (gauges and main street)
    def mrow(name, value, st, delta_txt, worse, sp, asof, meaning, rule):
        t = TONE[st]
        asof_s = asof.strftime("%b %-d, %Y") if isinstance(asof, dt.date) else "n/a"
        return f'''<details class="mrow"><summary><span class="bar s-{t}"></span>
<span class="mn">{E(name)}<small>as of {asof_s}</small></span>{spark(sp, cls="g")}
<span class="mv"><b class="pill s-{t}">{E(value)}</b>{arrow(delta_txt, worse)}</span></summary>
<p>{E(meaning)}</p><p class="muted">{E(rule)}.</p></details>'''

    ms = d["ms"]
    ms_counts = {k: sum(1 for r in ms if r["st"] == k) for k in (R, Y, G)}
    ms_rows = "".join(mrow(r["name"], r["value"], r["st"], r["delta"], r["worse"], r["spark"], r["asof"], r["meaning"], r["rule"]) for r in ms)

    gauge_html, cats = [], []
    for g in d["gauges"]:
        if g["cat"] not in cats:
            cats.append(g["cat"]); gauge_html.append(f'<h3>{E(g["cat"])}</h3>')
        du = "pts" if g["unit"] in ("%", "net %", "pts") else g["unit"]
        dtxt = (fmt(g["d"], du, True) + (" pts" if du == "pts" else "") + " in 1 mo") if num(g["d"]) else ""
        rule = g.get("rule") or f'Yellow at {fmt(g["y"], g["unit"])}, red at {fmt(g["r"], g["unit"])} ({"higher" if g["direction"]=="up" else "lower"} is worse). Source: {g["sid"].replace("X:", "derived ")}'
        gauge_html.append(mrow(g["name"], g.get("vtext") or fmt(g["v"], g["unit"]), g["st"], dtxt, g["worse"], g["spark"], g["asof"], g["meaning"], rule))

    # ---------------- jobs and layoffs
    jb = d["jb"]
    jb_rows = "".join(mrow(r["name"], r["value"], r["st"], r["delta"], r["worse"], r["spark"], r["asof"], r["meaning"], r["rule"]) for r in jb)
    wk = d["weeks"]
    djc_html, lay_trend, lay_tone = "", None, "grey"
    if wk:
        full = [w for w in wk if not w["partial"]]
        rec, base = full[-4:], full[-12:-4]
        ra = sum(w["jobs"] for w in rec) / max(len(rec), 1); ba = sum(w["jobs"] for w in base) / max(len(base), 1)
        rc = sum(w["count"] for w in rec) / max(len(rec), 1); bc = sum(w["count"] for w in base) / max(len(base), 1)
        chg = ra / ba - 1 if ba else None
        cchg = rc / bc - 1 if bc else None
        if num(chg):
            lay_trend = "Rising" if chg >= 0.25 else "Easing" if chg <= -0.20 else "Steady"
            lay_tone = "red" if chg >= 0.25 else "yellow" if chg >= 0.10 else "green"
        mx = max(max(w["jobs"] for w in wk), 1)
        W, H, n = 320, 96, len(wk); bw = W / n
        bars = "".join(f'<rect x="{i*bw+2:.1f}" y="{H-14-(w["jobs"]/mx)*(H-24):.1f}" width="{bw-4:.1f}" height="{max((w["jobs"]/mx)*(H-24),1):.1f}" rx="2" class="{"pb" if w["partial"] else "fb"}"><title>Week of {w["start"].strftime("%b %-d")}: {w["jobs"]:,} jobs in {w["count"]} announcements{" (week in progress)" if w["partial"] else ""}</title></rect>' for i, w in enumerate(wk))
        avg_y = H - 14 - (ra / mx) * (H - 24)
        bars += f'<line x1="{W-4*bw-bw:.1f}" x2="{W-bw:.1f}" y1="{avg_y:.1f}" y2="{avg_y:.1f}" class="avg"/>'
        bars += f'<text x="0" y="{H-1}" class="ax">{wk[0]["start"].strftime("%b %-d")}</text><text x="{W}" y="{H-1}" text-anchor="end" class="ax">this week</text>'
        ests = d["dj"].get("ests", {})
        est_txt = " &middot; ".join(f"{k} est. {v:,}" for k, v in list(ests.items())[-2:])
        djc_html = f'''<div class="djc"><div class="djh"><b>Announced layoffs, by week</b><span class="pill s-{lay_tone}">{lay_trend or "n/a"}</span></div>
<svg viewBox="0 0 {W} {H}" class="bars" role="img" aria-label="Weekly announced job cuts, last 13 weeks">{bars}</svg>
<p class="djs">Last 4 weeks: <b>{ra:,.0f}</b> jobs/week in {rc:.0f} announcements, <span class="{'neg' if num(chg) and chg > 0 else 'pos'}">{pct(chg)}</span> vs the 8 weeks before
({'+' if num(cchg) and cchg > 0 else ''}{pct(cchg, False) if num(cchg) else 'n/a'} in announcement count). {est_txt}</p>
<p class="muted fine">Tallied from <a href="https://www.dailyjobcuts.com/">DailyJobCuts.com</a> once a day. Counts only announcements with a number; percentages and "no #" entries count as announcements only. The dashed line is the 4-week average; the pale bar is this week so far.</p></div>'''
    wc, wc_all = d["wc"], d["wc_all"]
    wc_html = ""
    if wc:
        avg = sum(r["v"] for r in wc) / len(wc); below = sum(1 for r in wc if r["v"] < 100)
        rows_ = []
        for r in wc:
            w_ = max(2, min(100, r["v"] / 150 * 100))
            c3 = r["c3"]
            arr = arrow(f"{pct(c3)} 3 mo", c3 < 0) if num(c3) else ""
            rows_.append(f'''<li class="s-{TONE[r["st"]]}"><div class="wcn"><span>{E(r["name"])}</span><div class="track"><i style="width:{w_:.1f}%"></i><b></b></div></div>
<span class="pill">{r["v"]:.0f}</span><span class="wca">{arr}</span></li>''')
        allv = f"{wc_all:.0f}" if num(wc_all) else "n/a"
        latest = max(r["asof"] for r in wc).strftime("%b %-d")
        wc_html = f'''<div class="wc"><div class="djh"><b>White-collar hiring</b><span class="muted fine0">postings vs Feb 2020 = 100</span></div>
<p class="djs"><b>{below} of {len(wc)}</b> fields below pre-pandemic hiring. Average <b>{avg:.0f}</b> vs <b>{allv}</b> for all jobs.</p>
<ul class="wcl">{"".join(rows_)}</ul>
<p class="muted fine">Indeed job postings via FRED, as of {latest}. Green 95+, yellow 75-95, red under 75. The tick marks 100. Arrows show the 3-month change.</p></div>'''
    claims = next((r for r in jb if r["name"].startswith("Jobless claims")), None)
    jb_red = sum(1 for r in jb if r["st"] == R); jb_yel = sum(1 for r in jb if r["st"] == Y)
    jverd_tone = "red" if (lay_trend == "Rising" and claims and claims["st"] != G) or jb_red >= 4 else "yellow" if (lay_trend == "Rising" or jb_red >= 2 or (claims and claims["st"] != G)) else "green"
    jverd = (f'<b>Layoffs: {lay_trend or "n/a"}</b> &middot; claims {claims["value"]}, {claims["delta"]}' if claims else f'<b>Layoffs: {lay_trend or "n/a"}</b>') + f' &middot; {jb_red} red, {jb_yel} yellow below'

    # ---------------- build-up risk
    bu = d["bu"]
    bu_rows = "".join(mrow(r["name"], r["value"], r["st"], r["delta"], r["worse"], r["spark"], r["asof"], r["meaning"], r["rule"]) for r in bu)
    bu_red = sum(1 for r in bu if r["st"] == R)
    bu_tone = "red" if bu_red else "yellow" if any(r["st"] == Y for r in bu) else "green"

    # ---------------- dale vs karsan
    nd, nk = d["nd"], d["nk"]; nu = len(d["duel"]) - nd - nk
    lead = "Leaning Dale" if nd > nk else "Leaning Karsan" if nk > nd else "Too close to call"
    dpct = round(100 * nd / (nd + nk)) if nd + nk else 50
    duel = "".join(f'''<li><div><b>{E(t)}</b><small>{E(reading)}</small><small class="dk"><span>{E(dw)}</span><span>{E(kw)}</span></small></div>
<span class="pill {'p-dale' if side=='Dale' else 'p-kar' if side=='Karsan' else 's-grey'}">{E(side or 'Undecided')}</span></li>''' for t, reading, side, dw, kw in d["duel"])

    # ---------------- hard assets
    hard = "".join(f'''<div class="tile s-{TONE[h["st"]]}"><div class="tn">{E(h["name"])} <small>{E(h["sub"])}</small></div>
<div class="tp">{money(h["price"])}</div>{spark(h["spark"], 100, 26, "t")}
<div class="tc"><span class="{chg_class(h['day'])}">{pct(h["day"])} <i>day</i></span><span class="{chg_class(h['m1'])}">{pct(h["m1"])} <i>1 mo</i></span></div>
<div class="tf">{E(h["trend"])}{' &middot; ' if h["trend"] else ''}{pct(h["off_high"])} from high</div></div>''' for h in d["hard"])
    ratios = " &middot; ".join(f'<b>{E(n)}</b> {v:.1f} <span class="muted">({E(note)})</span>' for n, v, note in d["ratios"] if num(v))

    # ---------------- watchlist
    wl = []
    for w in d["watch"]:
        if not w["ok"]:
            wl.append(f'<div class="wrow"><div class="wmain"><span class="tkc"><span class="tk">{E(w["t"])}</span><small>No data. Check the symbol.</small></span></div></div>'); continue
        sc = "pos" if w["setup"] == "Oversold" else "neg" if w["setup"] == "Overbought" else ""
        tc = "neg" if w["trend"] == "Downtrend" else ""
        wl.append(f'''<details class="wrow"><summary class="wmain">
<span class="tkc"><span class="tk">{E(w["t"])}</span><small><span class="{sc}">{w["setup"]}</span><br><span class="{tc}">{w["trend"]}</span></small></span>
{spark(w["spark"], 60, 30, "w")}
<span class="px">{money(w["price"])}<small class="{chg_class(w['day'])}">{pct(w["day"])} day</small><small class="{chg_class(w['m1'])}">{pct(w["m1"])} 1 mo</small></span>
<span class="pill sig s-{w["tone"]}">{E(w["sig"])}</span></summary>
<dl class="kv"><div><dt>New-buy size</dt><dd>{w["size"]}</dd></div><div><dt>vs 20-day avg</dt><dd>{pct(w["vs20"])}</dd></div>
<div><dt>20-day range</dt><dd>{w["rng"]*100:.0f}% of the way up</dd></div><div><dt>200-day avg</dt><dd>{money(w["a200"])}</dd></div>
<div><dt>Off 52-wk high</dt><dd>{pct(w["off_high"])}</dd></div></dl></details>''')

    edit = f'<a href="https://github.com/{REPO}/edit/main/watchlist.txt">Edit watchlist</a>' if REPO else "Edit watchlist.txt in your repo."
    ovr = f'<a href="https://github.com/{REPO}/edit/main/regime_override.txt">Manual regime override</a>' if REPO else ""
    off = d["off"]
    upd = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    ov_note = f'<p class="note">Manual override on. The six lights say {d["calc"]}.</p>' if d["override"] else ""
    cap_note = '<p class="note on">Capitulation signal is ON. See "Blood in the streets".</p>' if d["capitulation"] else ""
    lamps = "".join(f'<a href="#light-{l["key"]}" class="s-{TONE[l["st"]]}"><i></i><span>{SHORT[l["key"]]}</span></a>' for l in d["core"])

    body = f'''
<header class="banner b-{tone}"><div class="wrap">
<div class="top"><span>US financial system</span><span id="upd" data-t="{upd}">Updated {upd}</span></div>
<h1>{reg}</h1>
<p class="sub">{E(guide)}</p>
<div class="lamps" aria-label="The six crisis lights">{lamps}</div>
<div class="kpis">
<div><b>{rn} of 6</b><small>crisis lights red ({d["yels"]} yellow)</small></div>
<div><b class="t-{trend_cls}">{"&#9650; Worse" if trend_cls=="worse" else "&#9660; Better" if trend_cls=="better" else "Unchanged"}</b><small>vs a month ago, when {rp} {"was" if rp==1 else "were"} red and the regime was {regime_prev}</small></div>
<div><b>{E(deploy)}</b><small>new money to deploy</small></div>
<div><b>{"ON" if d["capitulation"] else "Off"}</b><small>capitulation signal ({nc} of 5)</small></div>
</div>{cap_note}{ov_note}
<div class="stale" id="stale">Data is more than 3 hours old. The hourly update may have paused.</div>
</div></header>
<nav class="jump wrap"><a href="#core">Crisis core</a><a href="#blood">Blood</a><a href="#pivot">Pivot</a><a href="#fed">Fed</a><a href="#jobs">Jobs</a><a href="#main">Main St</a><a href="#buildup">Build-up</a><a href="#duel">Dale/Karsan</a><a href="#hard">Hard assets</a><a href="#gauges">Gauges</a><a href="#watch">Watchlist</a></nav>
<div class="legend wrap"><span><i class="s-green"></i>Healthy</span><span><i class="s-yellow"></i>Caution</span><span><i class="s-red"></i>Stress</span><span class="arr better">&#9660; better</span><span class="arr worse">&#9650; worse</span></div>
<main class="wrap">
<section id="core"><h2>Crisis core</h2><p class="lede">The six channels behind past breaks. They alone set the regime: 4+ red = RED; 2+ red, with yellows counting half = YELLOW. Tap a light for its trigger.</p>
<div class="lights">{"".join(core)}</div></section>

<section id="blood"><h2>Blood in the streets</h2><div class="verdict s-{capv[1]}"><b>{nc} of 5</b> {E(capv[0])}</div>
<p class="lede">Washout signals that marked past bottoms. 3+ turns it on and overrides the regime for buying.</p><ul class="chk">{checklist(d["caps"])}</ul></section>

<section id="pivot"><h2>Pivot watch</h2><div class="verdict s-{pivv[1]}"><b>{npv} of 5</b> {E(pivv[0])}</div>
<p class="lede">Signs the Fed and Treasury rescue has started.</p><ul class="chk">{checklist(d["pivots"])}</ul></section>

<section id="fed"><h2>Fed policy error</h2><div class="verdict s-{TONE[fl["st"]]}">{E(fed_verdict)}</div>
<p class="lede">Is the Fed tightening into weakness? The bond market and early-warning data, not the headlines.</p><ul class="frows">{fed_rows}</ul></section>

<section id="jobs"><h2>Jobs and layoffs</h2><div class="verdict s-{jverd_tone}">{jverd}</div>
<p class="lede">Is the job market cracking? Weekly and daily lines show the last 3 months; monthly ones show a year. Tap any row for details.</p>{djc_html}{wc_html}{jb_rows}</section>

<section id="main"><h2>Main Street</h2><div class="verdict s-{'red' if ms_counts[R] >= 4 else 'yellow' if ms_counts[R] + ms_counts[Y] >= 4 else 'green'}"><b>{ms_counts[R]} red &middot; {ms_counts[Y]} yellow &middot; {ms_counts[G]} green</b> while SPY is {pct(off)} from its high</div>
<p class="lede">The bottom of the K: wealth, essential costs, sentiment and credit for ordinary households and the lenders exposed to them. Tap any row for details.</p>{ms_rows}</section>

<section id="buildup"><h2>Build-up risk</h2><div class="verdict s-{bu_tone}"><b>{"Excesses building" if bu_tone=="red" else "Some heat" if bu_tone=="yellow" else "No major excesses"}</b> &middot; a 2-3 year horizon, not timing</div>
<p class="lede">Slow-moving vulnerability. Red-zone checks rank 3-year credit growth and asset returns against history. These don't set the regime.</p>{bu_rows}</section>

<section id="duel"><h2>Dale vs. Karsan</h2>
<div class="tug"><span class="tl">Dale {nd}</span><div class="tbar"><i style="width:{dpct}%"></i></div><span class="tr">{nk} Karsan</span></div>
<p class="tugv"><b>{lead}</b> <span class="muted">({nu} undecided)</span></p>
<p class="lede"><b>Dale:</b> no recession; risk-off now; a correction, not a crash. <b>Karsan:</b> crisis Jan-Mar 2027, S&amp;P down 25-40%, 10-yr 5.5-6%, then the Fed prints.</p>
<ul class="chk duel">{duel}</ul><p class="muted fine">Theses as of Nov 19, 2026. Tests are my framing of their calls.</p></section>

<section id="hard"><h2>Hard assets and crypto</h2><p class="lede">Tiles: green = uptrend, yellow = mixed, red = downtrend and still falling. Gold and silver are front-month futures.</p>
<div class="tiles">{hard}</div><p class="ratios">{ratios}</p></section>

<section id="gauges"><h2>All system gauges</h2><p class="lede">Color shows the level. The arrow shows the last month's move, colored by better or worse. Lines show the past year.</p>{"".join(gauge_html)}</section>

<section id="watch"><h2>Watchlist</h2><p class="lede">Each ticker's setup, filtered through the regime. Nothing turns green in a red regime unless the capitulation signal is on. Lines show 6 months. {edit}</p>{"".join(wl) or '<p class="muted">Your watchlist is empty.</p>'}</section>
</main>
<footer class="wrap"><p>Data: FRED (St. Louis Fed), Yahoo Finance, Stooq. Rebuilt hourly. Thresholds are rules of thumb from past cycles, not official cutoffs. Not financial advice.</p><p>{ovr}</p></footer>
<script>{{const el=document.getElementById('upd');const t=new Date(el.dataset.t);const m=Math.round((Date.now()-t)/60000);
el.textContent='Updated '+(m<1?'just now':m<60?m+' min ago':m<1440?Math.round(m/60)+' hr ago':Math.round(m/1440)+' days ago');
if(m>180)document.getElementById('stale').style.display='block';}}</script>'''
    return HEAD + body + "\n</body></html>"


HEAD = """<!DOCTYPE html>
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
/* VOICE: a control-room annunciator: calm until something lights up.
   IDEA: one master light (regime), six crisis lamps beneath it, then the instruments.
   PICTURE: full-bleed regime banner with the six lamps beside the verdict.
   COLOUR and TYPE: one rule everywhere: green healthy, amber caution, red stress. Inter Tight + Inter.
   LAYOUT: single phone column; verdict first, then the six lights, then detail in order of decision value. */
@import url('https://fonts.googleapis.com/css2?family=Inter+Tight:wght@600;800&family=Inter:wght@400;500;600;700&display=swap');
:root{color-scheme:light dark;--bg:#fff;--ink:#14201a;--mut:#5d6b64;--line:#e3e8e5;--card:#f5f7f6;
--g:#18794a;--gbg:#d8f0e1;--y:#946200;--ybg:#fdefc3;--r:#b42d22;--rbg:#fadad5;--n:#6b7280;--nbg:#e9ebee;
--dale:#1d5fa8;--daleb:#dce9f8;--kar:#8a3fa0;--karb:#f1e1f6}
@media (prefers-color-scheme:dark){:root{--bg:#0d1310;--ink:#e7eee9;--mut:#93a39a;--line:#223029;--card:#141d18;
--g:#5ad38c;--gbg:#123a24;--y:#ffc94d;--ybg:#3b2d08;--r:#ff8a7f;--rbg:#43150f;--n:#a3aab3;--nbg:#262b31;
--dale:#7fb3ee;--daleb:#14263d;--kar:#d79be8;--karb:#2f1838}}
*{box-sizing:border-box;min-width:0}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 Inter,system-ui,-apple-system,sans-serif;overflow-x:clip;-webkit-text-size-adjust:100%}
h1,h2,h3{font-family:'Inter Tight',Inter,sans-serif;margin:0}
.wrap{max-width:760px;margin:0 auto;padding:0 16px}
.muted{color:var(--mut)}.pos{color:var(--g)}.neg{color:var(--r)}
/* status fills, one rule everywhere */
.s-green{--c:var(--g);--cb:var(--gbg)}.s-yellow{--c:var(--y);--cb:var(--ybg)}.s-red{--c:var(--r);--cb:var(--rbg)}.s-grey{--c:var(--n);--cb:var(--nbg)}
.pill{display:inline-block;border-radius:8px;padding:5px 9px;font-weight:700;background:var(--cb);color:var(--c);white-space:nowrap;font-variant-numeric:tabular-nums;text-align:center}
.p-dale{background:var(--daleb);color:var(--dale)}.p-kar{background:var(--karb);color:var(--kar)}
.arr{font-size:12px;font-weight:600;white-space:nowrap}.arr.worse,.t-worse{color:var(--r)}.arr.better,.t-better{color:var(--g)}.arr.flat,.t-flat{color:var(--mut)}
/* banner */
.banner{padding:calc(env(safe-area-inset-top) + 18px) 0 20px;color:#fff;background:#3b4148}
.b-green{background:#0f6b3a}.b-yellow{background:#8a5a00}.b-red{background:#9e2219}
.banner .top{display:flex;justify-content:space-between;font-size:13px;opacity:.85}

.banner h1{font-size:clamp(50px,15vw,80px);line-height:1;font-weight:800;letter-spacing:-.02em;margin:10px 0 4px}
.lamps{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:4px;padding:10px 6px;border-radius:12px;background:rgba(0,0,0,.24);margin:0 0 10px}
.lamps a{display:flex;flex-direction:column;align-items:center;gap:5px;color:#fff;text-decoration:none;font-size:11.5px;font-weight:600;white-space:nowrap}
.lamps i{width:22px;height:22px;border-radius:50%;background:var(--c);box-shadow:0 0 0 2px rgba(255,255,255,.8)}
.banner .lamps .s-green{--c:#4fe08f}.banner .lamps .s-yellow{--c:#ffd24d}.banner .lamps .s-red{--c:#ff6b5e}.banner .lamps .s-grey{--c:#9aa1a8}
.sub{font-size:18px;font-weight:600;margin:0 0 14px;text-wrap:balance}
.kpis{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.kpis div{background:rgba(255,255,255,.14);border-radius:10px;padding:8px 11px}
.kpis b{display:block;font-family:'Inter Tight',sans-serif;font-size:18px;line-height:1.2}.kpis small{font-size:12px;opacity:.85}
.kpis .t-worse{color:#ffd0ca}.kpis .t-better{color:#c9f5d8}.kpis .t-flat{color:#fff}
.note{background:rgba(255,255,255,.95);color:#14201a;border-radius:8px;padding:8px 12px;margin:10px 0 0;font-weight:600;font-size:14px}
.note.on{color:#0f6b3a}
.stale{display:none;background:#fdefc3;color:#7a5200;padding:8px 12px;border-radius:8px;margin-top:10px;font-size:14px;font-weight:600}
.jump{display:flex;gap:6px;overflow-x:auto;padding-top:12px;padding-bottom:4px;scrollbar-width:none}
.jump::-webkit-scrollbar{display:none}
.jump a{flex:none;font-size:13px;font-weight:600;color:var(--ink);text-decoration:none;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:7px 12px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12px;color:var(--mut);padding-top:10px;padding-bottom:4px}
.legend span{display:flex;align-items:center;gap:5px}.legend i{width:10px;height:10px;border-radius:3px;background:var(--c)}
section{padding:22px 0 8px;border-bottom:1px solid var(--line);scroll-margin-top:8px}
h2{font-size:22px;margin-bottom:8px}
h3{font-size:14px;color:var(--mut);font-weight:600;margin:16px 0 4px}
.lede{color:var(--mut);font-size:14px;margin:0 0 12px}
.verdict{border-radius:10px;padding:11px 13px;margin:0 0 10px;background:var(--cb);color:var(--c);font-weight:600;font-size:15px}
.verdict b{font-family:'Inter Tight',sans-serif;margin-right:4px}
/* six lights */
.lights{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.light{background:var(--cb);border-radius:14px;padding:12px;border:1px solid color-mix(in srgb,var(--c) 25%,transparent)}
.light summary{display:flex;flex-direction:column;gap:2px}
.lt{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:700;color:var(--c);line-height:1.2}
.lamp{flex:none;width:11px;height:11px;border-radius:50%;background:var(--c);box-shadow:0 0 0 3px color-mix(in srgb,var(--c) 25%,transparent)}
.lv{font-family:'Inter Tight',sans-serif;font-weight:800;font-size:28px;line-height:1.15;margin-top:4px;font-variant-numeric:tabular-nums}
.ld{font-size:13px}
.was{font-size:12px;font-weight:600}.was.worse{color:var(--r)}.was.better{color:var(--g)}.was.flat{color:var(--mut)}
.lr{font-size:12px;color:var(--mut);margin-top:6px;line-height:1.35;border-top:1px solid color-mix(in srgb,var(--c) 25%,transparent);padding-top:6px}
.ld{color:var(--mut)}
/* checklists */
.chk{list-style:none;padding:0;margin:0}
.chk li{display:grid;grid-template-columns:14px minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 0;border-top:1px solid var(--line)}
.chk small{display:block;color:var(--mut);font-size:13px}
.dot{width:12px;height:12px;border-radius:50%;border:2px solid var(--n)}.hit .dot{background:var(--g);border-color:var(--g)}
.yn{font-weight:700;font-size:14px;color:var(--mut)}.hit .yn{color:var(--g)}
.chk.duel li{grid-template-columns:minmax(0,1fr) auto}.chk.duel .pill{min-width:92px;font-size:13px}
.chk small.dk{display:flex;flex-direction:column;font-size:12px;margin-top:2px}
/* fed rows */
.frows{list-style:none;padding:0;margin:0}
.frow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;padding:9px 0;border-top:1px solid var(--line);font-size:15px}
.frow small{display:block;font-size:12px;color:var(--mut)}
/* metric rows */
summary{list-style:none;cursor:pointer}summary::-webkit-details-marker{display:none}
.mrow{border-top:1px solid var(--line)}
.mrow summary{display:grid;grid-template-columns:5px minmax(0,1fr) 64px auto;gap:10px;align-items:center;padding:10px 0;min-height:54px}
.bar{align-self:stretch;border-radius:3px;background:var(--c)}
.mn{font-size:15px;font-weight:500;line-height:1.25}.mn small{display:block;font-size:12px;color:var(--mut);font-weight:400}
.mv{display:flex;flex-direction:column;align-items:flex-end;gap:3px}
.mv .pill{font-size:15px}
.mrow p{margin:0 0 8px;font-size:14px}
.spark{display:block}.spark polyline{fill:none;stroke:var(--mut);stroke-width:1.5;vector-effect:non-scaling-stroke;opacity:.8}.spark circle{fill:var(--ink)}
.spark.g{width:64px;height:28px}
/* tiles */
.tiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.tile{background:var(--cb);border-radius:14px;padding:12px;border:1px solid color-mix(in srgb,var(--c) 25%,transparent)}
.tn{font-size:14px;font-weight:700;color:var(--c)}.tn small{font-weight:500;color:var(--mut)}
.tp{font-family:'Inter Tight',sans-serif;font-size:24px;font-weight:800;font-variant-numeric:tabular-nums}
.spark.t{width:100%;height:28px;margin:2px 0 4px}
.tc{display:flex;gap:10px;flex-wrap:wrap;font-size:13px;font-weight:600}.tc i{font-style:normal;font-weight:400;color:var(--mut)}
.tf{font-size:12px;color:var(--mut)}
.ratios{font-size:13px;margin:12px 0 4px}
/* duel */
.tug{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:10px;align-items:center;font-weight:700;font-family:'Inter Tight',sans-serif}
.tl{color:var(--dale)}.tr{color:var(--kar)}
.tbar{height:14px;border-radius:7px;background:var(--kar);overflow:hidden}.tbar i{display:block;height:100%;background:var(--dale);border-right:2px solid var(--bg)}
.tugv{margin:8px 0 6px;font-size:15px}
.fine{font-size:12px;margin:8px 0 12px}
/* layoffs chart */
.djc{background:var(--card);border-radius:14px;padding:12px;margin:0 0 6px}
.djh{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;font-size:15px}
.bars{width:100%;height:auto;display:block}
.bars .fb{fill:var(--ink);opacity:.72}.bars .pb{fill:var(--ink);opacity:.25}
.bars .avg{stroke:var(--r);stroke-width:1.5;stroke-dasharray:4 3}
.bars .ax{font-size:10px;fill:var(--mut)}
.djs{font-size:13.5px;margin:8px 0 2px}.djc a{color:inherit}
.wc{background:var(--card);border-radius:14px;padding:12px;margin:0 0 6px}
.fine0{font-size:12px}
.wcl{list-style:none;padding:0;margin:8px 0 0}
.wcl li{display:grid;grid-template-columns:minmax(0,1fr) 46px 88px;gap:8px;align-items:center;padding:6px 0;border-top:1px solid var(--line)}
.wcn span{display:block;font-size:14px;line-height:1.25}
.track{position:relative;height:7px;border-radius:4px;background:color-mix(in srgb,var(--mut) 18%,transparent);margin-top:4px}
.track i{display:block;height:100%;border-radius:4px;background:var(--c)}
.track b{position:absolute;left:66.67%;top:-3px;width:2px;height:13px;background:var(--ink);opacity:.55}
.wcl .pill{font-size:14px;padding:3px 0}
.wca{text-align:right}
/* watchlist */
.wrow{border-top:1px solid var(--line)}
.wmain{display:grid;grid-template-columns:minmax(0,1fr) 60px 92px 104px;gap:8px;align-items:center;padding:10px 0;min-height:62px}
.tkc{display:flex;flex-direction:column}.tk{font-weight:800;font-family:'Inter Tight',sans-serif;font-size:17px;line-height:1.2}
.tkc small{font-size:12px;color:var(--mut);line-height:1.3}
.spark.w{width:60px;height:30px}
.px{display:flex;flex-direction:column;align-items:flex-end;font-size:15px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap;line-height:1.3}
.px small{font-size:12px;font-weight:500}
.pill.sig{font-size:12.5px;white-space:normal;line-height:1.2;padding:7px 6px}
.kv{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px 16px;margin:0 0 12px;padding:10px 12px;background:var(--card);border-radius:10px;font-size:14px}
.kv div{display:flex;justify-content:space-between;gap:8px}.kv dt{color:var(--mut)}.kv dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
footer{padding:20px 0 calc(env(safe-area-inset-bottom) + 28px);font-size:13px;color:var(--mut)}
footer a,.lede a{color:inherit}
@media (max-width:380px){.wmain{grid-template-columns:minmax(0,1fr) 88px 96px}.spark.w{display:none}.kv{grid-template-columns:minmax(0,1fr)}}
@media (max-width:430px){.kv{grid-template-columns:minmax(0,1fr)}}
@media (min-width:640px){.lights{grid-template-columns:repeat(3,minmax(0,1fr))}.tiles{grid-template-columns:repeat(3,minmax(0,1fr))}.kpis{grid-template-columns:repeat(4,minmax(0,1fr))}}
</style></head><body>"""


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
