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
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()   # set as a GitHub secret to turn on phone alerts
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


def ebp():
    """Gilchrist-Zakrajsek excess bond premium from the Fed (monthly CSV). Returns (ebp_series, recession_prob_series)."""
    if MOCK:
        random.seed("ebp"); e, pr = [], []
        for i in range(60):
            d = TODAY - days(30 * (59 - i)); v = 0.05 + 0.25 * math.sin(i / 7) + random.gauss(0, 0.05)
            e.append((d, v)); pr.append((d, 0.25 + v * 0.3))
        return e, pr
    try:
        text = http("https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv")
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
        head = [h.strip().lower() for h in rows[0]]
        di = next(i for i, h in enumerate(head) if "date" in h)
        ei = next(i for i, h in enumerate(head) if h == "ebp" or ("ebp" in h and "prob" not in h))
        pi = next((i for i, h in enumerate(head) if "prob" in h), None)
        e, pr = [], []
        for r in rows[1:]:
            if len(r) <= ei: continue
            raw = r[di].strip(); d = None
            for f in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m", "%b %Y", "%Y%m"):
                try: d = dt.datetime.strptime(raw, f).date(); break
                except ValueError: pass
            if not d: continue
            try: e.append((d, float(r[ei])))
            except ValueError: continue
            if pi is not None:
                try: pr.append((d, float(r[pi])))
                except ValueError: pass
        return e, pr
    except Exception as ex:
        print(f"EBP failed: {ex}")
        return [], []


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

# ---- prediction markets (Kalshi + Polymarket), free public data, no keys
KALSHI_HOSTS = ["https://api.elections.kalshi.com/trade-api/v2", "https://external-api.kalshi.com/trade-api/v2"]
# Polymarket watch list: (label, search words, words that must all appear in the question, words that win ties)
POLY_WATCH = [("US recession", "US recession", ["recession"], ["2027"]),
              ("Bitcoin to $40k (Howell's risk case)", "Bitcoin 40,000", ["bitcoin", "40"], ["dip", "below", "2026", "2027"]),
              ("Strait of Hormuz", "Strait of Hormuz", ["hormuz"], ["normal", "reopen"])]
MON3 = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

def _f(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def kalshi_prob(m):
    for a, b in [("yes_bid_dollars", "yes_ask_dollars"), ("yes_bid", "yes_ask")]:
        lo, hi = _f(m.get(a)), _f(m.get(b))
        if num(lo, hi) and hi > 0:
            p = (lo + hi) / 2
            return p / 100 if p > 1.0001 else p
    for k in ("last_price_dollars", "last_price"):
        v = _f(m.get(k))
        if num(v): return v / 100 if v > 1.0001 else v
    return None

def fed_bucket(label):
    t = label.lower().replace(" ", "")
    if "maintain" in t or "hold" in t or "nochange" in t or "unchanged" in t: return "hold"
    big = ">25" in t or "50" in t or "morethan25" in t
    if "hike" in t or "raise" in t or "increase" in t: return "hike_big" if big else "hike25"
    if "cut" in t or "lower" in t or "decrease" in t: return "cut_big" if big else "cut25"
    return None

def kalshi_fed():
    """Next three FOMC meetings: list of dict(date, label, odds{bucket: p}, volume)."""
    if MOCK:
        base = [dict(hike_big=0.08, hike25=0.89, hold=0.03, cut25=0.0, cut_big=0.0),
                dict(hike_big=0.03, hike25=0.31, hold=0.62, cut25=0.04, cut_big=0.0),
                dict(hike_big=0.01, hike25=0.15, hold=0.66, cut25=0.17, cut_big=0.01)]
        ds = [dt.date(2026, 12, 9), dt.date(2027, 1, 27), dt.date(2027, 3, 17)]
        return [dict(date=d, label=d.strftime("%b %-d"), odds=o, ticker="mock") for d, o in zip(ds, base)]
    for host in KALSHI_HOSTS:
        try:
            j = json.loads(http(f"{host}/markets?series_ticker=KXFEDDECISION&status=open&limit=200", tries=2))
            ev, closes = {}, {}
            for m in j.get("markets", []):
                et = m.get("event_ticker") or ""
                lab = m.get("yes_sub_title") or m.get("subtitle") or m.get("title") or ""
                b, p = fed_bucket(lab), kalshi_prob(m)
                if not b or not num(p): continue
                ev.setdefault(et, {}).setdefault(b, 0.0); ev[et][b] += p
                ct = (m.get("close_time") or m.get("expected_expiration_time") or "")[:10]
                if ct: closes[et] = min(closes.get(et, ct), ct)
            out = []
            for et, odds in ev.items():
                mm = re.search(r"-(\d{2})([A-Z]{3})(\d{2})?$", et)
                if not mm or mm.group(2) not in MON3: continue
                d = dt.date(2000 + int(mm.group(1)), MON3[mm.group(2)], int(mm.group(3)) if mm.group(3) else 15)
                try:
                    cd = dt.date.fromisoformat(closes[et])
                    if abs((cd - d).days) <= 31: d = cd
                except Exception:
                    pass
                tot = sum(odds.values()) or 1
                out.append(dict(date=d, label=d.strftime("%b %-d, %Y"), odds={k: v / tot for k, v in odds.items()}, ticker=et))
            out.sort(key=lambda x: x["date"])
            if out: return out[:3]
        except Exception as e:
            print(f"Kalshi {host} failed: {e}")
    return []

def poly_watch():
    """Returns list of dict(label, question, p_yes, end, volume, url)."""
    if MOCK:
        return [dict(label="US recession", question="US recession by end of 2027?", p=0.24, end="2027-12-31", volume=850000, url=""),
                dict(label="Bitcoin to $40k (Howell's risk case)", question="Will Bitcoin dip to $40,000 by December 31, 2026?", p=0.11, end="2026-12-31", volume=2.1e6, url=""),
                dict(label="Strait of Hormuz", question="Strait of Hormuz traffic returns to normal by December 31?", p=0.20, end="2026-12-31", volume=1.5e6, url="")]
    out = []
    for label, q, must, prefer in POLY_WATCH:
        try:
            j = json.loads(http("https://gamma-api.polymarket.com/public-search?" + urllib.parse.urlencode({"q": q, "limit_per_type": 20}), tries=2))
            cands = []
            for ev in j.get("events", []) or []:
                for m in ev.get("markets", []) or []:
                    qq = (m.get("question") or "").lower()
                    if m.get("closed") or not all(w in qq for w in must): continue
                    try:
                        outs = json.loads(m.get("outcomes") or "[]"); prs = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
                    except Exception:
                        continue
                    if "Yes" not in outs or len(prs) != len(outs): continue
                    endd = (m.get("endDate") or ev.get("endDate") or "")[:10]
                    if endd and endd < str(TODAY): continue
                    vol = _f(m.get("volume")) or _f(m.get("volumeNum")) or 0
                    score = sum(1 for w in prefer if w in qq) * 1e9 + vol
                    cands.append((score, dict(label=label, question=m.get("question"), p=prs[outs.index("Yes")], end=endd, volume=vol,
                                              url=f"https://polymarket.com/event/{ev.get('slug')}" if ev.get("slug") else "")))
            if cands:
                out.append(max(cands, key=lambda c: c[0])[1])
        except Exception as e:
            print(f"Polymarket '{q}' failed: {e}")
    return out

# ---- history: one snapshot every few hours, carried forward through the published site
def load_history():
    if MOCK:
        return []
    if REPO and "/" in REPO:
        owner, repo = REPO.split("/", 1)
        try:
            h = json.loads(http(f"https://{owner.lower()}.github.io/{repo}/history.json", tries=2))
            if isinstance(h, list): return h
        except Exception as e:
            print(f"No previous history ({e}); starting fresh")
    return []

def save_history(hist, snap):
    hist = [h for h in hist if isinstance(h, dict) and "t" in h]
    if hist:
        last_t = dt.datetime.fromisoformat(hist[-1]["t"])
        if (NOW - last_t).total_seconds() < 3 * 3600: hist[-1] = snap
        else: hist.append(snap)
    else:
        hist.append(snap)
    cutoff = NOW - dt.timedelta(days=120)
    return [h for h in hist if dt.datetime.fromisoformat(h["t"]) >= cutoff]

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
    "IHLIDXUSTPBAFI": 107.2, "IHLIDXUSTPLEGA": 125.3, "WTREGEN": 850000.0, "RRPONTSYD": 8.0, "WRESBAL": 3050.0,
    "ECBASSETSW": 6.2e6, "DEXUSEU": 1.15, "JPNASSETS": 7.4e6, "THREEFYTP10": 0.95, "USALOLITOAASTSAM": 100.96,
    "NOCDFSA066MSFRBPHI": -0.4, "NOCDINA066MNFRBNY": 9.9, "PERMIT": 1295.0, "SUBLPDRCSN": 8.8, "STDSAUTO": -2.1,
    "NFCIRISK": -0.53, "NCBEILQ027S": 8.3e7}
MOCK_STEP = {"WALCL": 7, "ICSA": 7, "CCSA": 7, "NFCI": 7, "STLFSI4": 7, "DRCCLACBS": 91, "DRTSCILM": 91,
             "A091RC1Q027SBEA": 91, "FGRECPT": 91, "GFDEGDQ188S": 91, "FYFSGDA188S": 365,
             "WFRBST01134": 91, "WFRBSB50215": 91, "DRCRELEXFACBS": 91, "DRTSCLCC": 91, "NFCICREDIT": 7,
             "NFCILEVERAGE": 7, "IC4WSA": 7, "TDSP": 91, "BCNSDODNS": 91, "CMDEBT": 91, "GDP": 91,
             "WTREGEN": 7, "WRESBAL": 7, "ECBASSETSW": 7, "THREEFYTP10": 7, "SUBLPDRCSN": 91, "STDSAUTO": 91,
             "NFCIRISK": 7, "NCBEILQ027S": 91}
MOCK_MONTHLY = {"U6RATE", "PSAVERT", "SAHMREALTIME", "CPIAUCSL", "IRLTLT01JPM156N", "LNS14000036",
                "CES6054150001", "USINFO", "TEMPHELPS", "JTSQUR", "CUSR0000SAF11", "CUSR0000SAH1",
                "CUSR0000SEHF01", "CUSR0000SETE", "UMCSENT", "JTSLDR", "CSUSHPINSA", "JPNASSETS",
                "USALOLITOAASTSAM", "NOCDFSA066MSFRBPHI", "NOCDINA066MNFRBNY", "PERMIT"}
MOCK_TREND = {"CPIAUCSL": 0.0025, "CUSR0000SAF11": 0.0025, "CUSR0000SAH1": 0.003, "CUSR0000SEHF01": 0.004,
              "CUSR0000SETE": 0.005, "CES6054150001": -0.002, "USINFO": -0.001, "TEMPHELPS": -0.004,
              "BCNSDODNS": 0.012, "CMDEBT": 0.013, "CSUSHPINSA": 0.004, "GDP": 0.012, "NCBEILQ027S": 0.02}

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
    p = {"GC=F": 3850, "SI=F": 47, "BTC-USD": 64500, "JPY=X": 152.0, "^MOVE": 104.0}.get(sym, random.uniform(30, 700))
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
    ("Credit", "Excess bond premium", "X:EBP", g_level("X:EBP"), "pts", "up", 0.25, 0.75, "Credit spreads beyond expected defaults: pure investor fear. Gilchrist and Zakrajsek found it among the best recession predictors (Fed data, monthly)."),
    ("Credit", "Banks tightening business loans", "DRTSCILM", g_level("DRTSCILM"), "net %", "up", 10, 25, "Fed loan officer survey. Credit crunch gauge."),
    ("Credit", "Banks tightening credit cards", "DRTSCLCC", g_level("DRTSCLCC"), "net %", "up", 10, 25, "Fed loan officer survey, consumer side."),
    ("Credit", "Banks tightening CRE loans", "SUBLPDRCSN", g_level("SUBLPDRCSN"), "net %", "up", 10, 25, "Fed loan officer survey: office, retail and other commercial property loans."),
    ("Credit", "Banks tightening auto loans", "STDSAUTO", g_level("STDSAUTO"), "net %", "up", 10, 25, "Fed loan officer survey: car loans."),
    ("Conditions", "Chicago Fed financial conditions", "NFCI", g_level("NFCI"), "", "up", -0.3, 0.0, "Above 0 = tighter than average."),
    ("Conditions", "St. Louis Fed stress index", "STLFSI4", g_level("STLFSI4"), "", "up", 0.0, 1.0, "Above 0 = above-normal stress."),
    ("Conditions", "NFCI risk component", "NFCIRISK", g_level("NFCIRISK"), "", "up", -0.2, 0.0, "Risk part of the Chicago Fed index: volatility and funding risk. Above 0 = tighter than average."),
    ("Conditions", "NFCI credit component", "NFCICREDIT", g_level("NFCICREDIT"), "", "up", -0.2, 0.0, "Credit part of the Chicago Fed index. Above 0 = tighter than average."),
    ("Conditions", "NFCI leverage component", "NFCILEVERAGE", g_level("NFCILEVERAGE"), "", "up", -0.1, 0.25, "Leverage part of the Chicago Fed index. Rising = leverage building."),
    ("Conditions", "VIX", "VIXCLS", g_level("VIXCLS"), "", "up", 20, 27, "Fear gauge for stocks."),
    ("Conditions", "VIX curve (1-mo vs 3-mo)", "X:VIXT", g_level("X:VIXT"), "x", "up", 0.95, 1.0, "Above 1.0 = inverted: traders paying up for protection right now. Often leads selloffs."),
    ("Conditions", "Market breadth, 3-mo", "X:BREADTH", g_change("X:BREADTH", 90), "pct", "down", -0.03, -0.06, "Equal-weight vs cap-weight S&P (RSP vs SPY). Falling = fewer stocks carrying the index."),
    ("Dollar & oil", "Broad dollar, 3-mo change", "DTWEXBGS", g_change("DTWEXBGS", 90), "pct", "up", 0.02, 0.04, "A fast-rising dollar squeezes global dollar borrowers."),
    ("Dollar & oil", "Brent crude", "DCOILBRENTEU", g_level("DCOILBRENTEU"), "$", "up", 85, 100, "War and supply strain. Feeds inflation."),
    ("Growth", "OECD leading indicator", "USALOLITOAASTSAM", g_level("USALOLITOAASTSAM"), "", "down", 100.0, 99.0, "Free stand-in for the Conference Board LEI. 100 = trend growth; below and falling warns of recession."),
    ("Growth", "Philly Fed new orders", "NOCDFSA066MSFRBPHI", g_level("NOCDFSA066MSFRBPHI"), "", "down", 0, -10, "Factory new orders in the Philadelphia district. Below 0 = shrinking. A free ISM proxy."),
    ("Growth", "NY Fed new orders", "NOCDINA066MNFRBNY", g_level("NOCDINA066MNFRBNY"), "", "down", 0, -10, "Factory new orders in New York State. Below 0 = shrinking."),
    ("Growth", "Building permits, 1 yr", "PERMIT", g_change("PERMIT", 365), "pct", "down", -0.05, -0.15, "Housing permits vs a year ago. Housing usually turns before the economy does."),
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
    h = at(S["BAMLH0A0HYM2"], TODAY)
    if num(h):
        st = R if h <= 2.6 else Y if h <= 3.0 else G
        add("Credit complacency", f"junk {h:.2f}%", st, "can snap wider fast", "note", tail(S, "BAMLH0A0HYM2", 1095), last(S["BAMLH0A0HYM2"])[0],
            "Very tight junk spreads mean investors are paid little for risk, which leaves room for a sudden repricing. The 2007 low was about 2.4%.",
            "Yellow at 3.0% or tighter, red at 2.6% or tighter")
    mc = quarterly(join(S["NCBEILQ027S"], S["GDP"], lambda a, b: a / 1000 / b * 100))
    if len(mc) > 40:
        pc = pctile([v for _, v in mc], mc[-1][1])
        add("Stock market value vs GDP", f"{mc[-1][1]:.0f}% of GDP", R if pc >= 95 else Y if pc >= 80 else G, f"{ordinal(pc)} pctile of history", "note",
            [v for _, v in mc][-40:], mc[-1][0], "US corporate equity value relative to the economy (the 'Buffett indicator'). High readings magnify the damage when credit turns.",
            "Yellow at the 80th percentile of history, red at the 95th")
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

# =========================================================================== recession watch (clusters, not single signals)

def recession_watch(S, t):
    out = []
    a, b, c = at(S["T10Y3M"], t), at(S["T10Y2Y"], t), at(S["T10Y2Y"], t - days(91))
    two, two3 = at(S["DGS2"], t), at(S["DGS2"], t - days(91))
    resteep = num(b, c, two, two3) and (b - c >= 0.5 and two - two3 <= -0.5)
    inv = (num(a) and a < 0) or (num(b) and b < 0)
    out.append(("Yield curve", inv or resteep, ("inverted" if inv else "bull re-steepening" if resteep else "normal") + (f" (10y-3m {a:+.2f}, 2s10s {b:+.2f})" if num(a, b) else "")))
    h, h3 = at(S["BAMLH0A0HYM2"], t), at(S["BAMLH0A0HYM2"], t - days(91)); e = at(S["X:EBP"], t)
    lit = (num(h, h3) and h - h3 >= 1.0) or (num(e) and e >= 0.5)
    out.append(("Credit stress building", lit, (f"junk spread {h - h3:+.2f} in 3 mo" if num(h, h3) else "junk n/a") + (f", bond premium {e:+.2f}" if num(e) else "")))
    l, l6 = at(S["USALOLITOAASTSAM"], t), at(S["USALOLITOAASTSAM"], t - days(182))
    out.append(("Leading index falling", num(l, l6) and l < 100 and l < l6 - 0.3, f"OECD index {l:.2f}, {l - l6:+.2f} in 6 mo" if num(l, l6) else "n/a"))
    p_, n_ = at(S["NOCDFSA066MSFRBPHI"], t), at(S["NOCDINA066MNFRBNY"], t)
    vals = [v for v in (p_, n_) if num(v)]
    avg = sum(vals) / len(vals) if vals else None
    out.append(("Factory orders shrinking", num(avg) and avg < -5, f"Philly {p_:+.1f}, NY {n_:+.1f}" if num(p_, n_) else "n/a"))
    pm, pm1 = at(S["PERMIT"], t), at(S["PERMIT"], t - days(365))
    out.append(("Building permits down 15%+", num(pm, pm1) and pm / pm1 - 1 <= -0.15, f"{pct(pm / pm1 - 1)} in 1 yr" if num(pm, pm1) else "n/a"))
    tight = [(nm, at(S[sid], t)) for sid, nm in [("DRTSCILM", "business"), ("SUBLPDRCSN", "CRE"), ("DRTSCLCC", "cards"), ("STDSAUTO", "autos")]]
    hot = [nm for nm, v in tight if num(v) and v >= 20]
    out.append(("Banks tightening broadly", len(hot) >= 2, f"{len(hot)} of 4 loan types at 20%+ net tightening" + (f" ({', '.join(hot)})" if hot else "")))
    ic, ic3 = at(S["IC4WSA"], t), at(S["IC4WSA"], t - days(91))
    out.append(("Jobless claims up 15%+", num(ic, ic3) and ic / ic3 - 1 >= 0.15, f"{pct(ic / ic3 - 1)} in 3 mo" if num(ic, ic3) else "n/a"))
    sm = at(S["SAHMREALTIME"], t)
    out.append(("Sahm rule triggered", num(sm) and sm >= 0.5, f"{sm:.2f}" if num(sm) else "n/a"))
    return out

# =========================================================================== liquidity (Howell-style proxy) and hard-asset bottoms
# Michael Howell's Global Liquidity Index is proprietary. This is a free proxy built from central bank balance sheets:
#   US net liquidity = Fed balance sheet - Treasury cash (TGA) - reverse repo
#   + ECB balance sheet in dollars + Bank of Japan balance sheet in dollars.
# Howell finds liquidity leads Bitcoin by about 13 weeks; we chart that lead.

def liquidity(S):
    out_us, out_g = [], []
    for d, w in S["WALCL"]:
        tga, rrp = at(S["WTREGEN"], d), at(S["RRPONTSYD"], d)
        if not num(w, tga): continue
        us = (w - tga) / 1e6 - (rrp / 1e3 if num(rrp) else 0)
        out_us.append((d, us))
        ecb, eur, boj, jpy = at(S["ECBASSETSW"], d), at(S["DEXUSEU"], d), at(S["JPNASSETS"], d), at(S["DEXJPUS"], d)
        if num(ecb, eur, boj, jpy):
            out_g.append((d, us + ecb * eur / 1e6 + boj * 1e8 / jpy / 1e12))
    return out_us, out_g

def momentum(series, t, n):
    a, b = at(series, t), at(series, t - days(n))
    return a / b - 1 if num(a, b) and b else None

def liq_state(series, t):
    m6, m13 = momentum(series, t, 42), momentum(series, t, 91)
    m6p = momentum(series, t - days(42), 42)
    if not num(m6, m13): return "No data", N, m6, m13
    if m6 > 0 and m13 > 0: return "Rising", G, m6, m13
    if m6 > 0 or (num(m6p) and m6 > m6p and m6 > -0.002): return "Turning up", Y, m6, m13
    if m13 > 0: return "Rolling over", Y, m6, m13
    return "Falling", R, m6, m13

BOTTOM_CFG = {"BTC-USD": ("Bitcoin", 0.35), "GC=F": ("Gold", 0.12), "SI=F": ("Silver", 0.20)}

def bottom_score(p, washout, liq_ok, usd_ok):
    ser = p["series"]; price = p["price"]; closes = p["closes"]
    win = [c for d, c in ser if d >= TODAY - days(120)]
    peak, mdd = 0, 0
    for c in win:
        peak = max(peak, c); mdd = min(mdd, c / peak - 1)
    low30 = min([c for d, c in ser if d >= TODAY - days(30)] or [price])
    low180 = min([c for d, c in ser if d >= TODAY - days(180)] or [price])
    a20 = sum(closes[-20:]) / 20; a20p = sum(closes[-25:-5]) / 20; a50 = sum(closes[-50:]) / 50
    checks = [
        ("Washout", mdd <= -washout, f"fell {pct(mdd, False)} from a peak in the last 4 months (needs {washout*100:.0f}%+)"),
        ("Retest", low30 <= low180 * 1.10, "held at or above its 6-month low on a pullback in the last 30 days (a higher low counts)"),
        ("Turn", price > a20 and a20 > a20p, "back above a rising 20-day average"),
        ("Base", price > a50, "above its 50-day average"),
        ("Liquidity", liq_ok, "global liquidity proxy rising or turning up"),
        ("Dollar", usd_ok, "dollar 2%+ off its 90-day high"),
    ]
    n = sum(1 for c in checks if c[1])
    ok = {c[0]: c[1] for c in checks}
    if n >= 5 and ok["Washout"] and ok["Base"] and ok["Liquidity"]: tier, st = "Bottom confirmed: buy in size", G
    elif n >= 3: tier, st = "Bottoming: build a 1/3 position", Y
    else: tier, st = "No bottom yet", R
    return dict(n=n, checks=checks, tier=tier, st=st, mdd=mdd, blocked=[])

def trap_guards(core, S, P, capitulation, lay_trend):
    """Bull-trap guards. Each: (name, passed, reading, applies_to or None for all).
    A 'buy in size' signal is blocked while any guard that applies to the asset fails."""
    L = {l["key"]: l for l in core}
    g = []
    reg_red = sum(1 for l in core if l["st"] == R) >= 4
    g.append(("No crisis underway", not reg_red or capitulation,
              f"{sum(1 for l in core if l['st'] == R)} of 6 crisis lights red" + (" (capitulation already on)" if capitulation and reg_red else ""), None))
    hy = S["BAMLH0A0HYM2"]; v, v1 = at(hy, TODAY), at(hy, TODAY - days(30)); hmax = max_between(hy, TODAY - days(120), TODAY)
    widening = num(v, v1) and (v - v1 >= 0.5 or (v >= 5 and num(hmax) and hmax - v < 0.25))
    g.append(("Credit spreads not widening", num(v, v1) and not widening,
              f"junk spread {v:.2f}%, {v - v1:+.2f} in 1 mo" if num(v, v1) else "n/a", None))
    for key, nm in [("funding", "Funding markets calm"), ("dollar", "Dollar not spiking"), ("japan", "No yen carry unwind"), ("fed", "Fed not hiking into weakness")]:
        l = L.get(key)
        g.append((nm, bool(l) and l["st"] != R, f"{l['name']}: {WORD[l['st']].lower()}" if l else "n/a", None))
    mv = P.get("^MOVE")
    if mv:
        a, b = mv["price"], at(mv["series"], TODAY - days(30))
        g.append(("Bond market calm", a < 130 and (not num(b) or a - b < 20), f"MOVE {a:.0f}" + (f", {a - b:+.0f} in 1 mo" if num(b) else ""), None))
    vt = at(S["X:VIXT"], TODAY)
    g.append(("Stocks not in active panic", (not num(vt) or vt < 1.0) or capitulation,
              f"VIX curve {vt:.2f}x" + (" (inverted)" if num(vt) and vt >= 1 else "") if num(vt) else "n/a", None))
    c, c3 = at(S["IC4WSA"], TODAY), at(S["IC4WSA"], TODAY - days(91))
    jobs_bad = (num(c, c3) and c / c3 - 1 >= 0.15) or lay_trend == "Rising"
    g.append(("Layoffs not accelerating", not jobs_bad,
              (f"claims {pct(c / c3 - 1)} in 3 mo" if num(c, c3) else "claims n/a") + f", announced cuts {(lay_trend or 'n/a').lower()}",
              ("BTC-USD", "SI=F")))
    return g

FAIL_LABEL = {'No crisis underway': 'crisis underway', 'Credit spreads not widening': 'credit widening', 'Funding markets calm': 'funding stress', 'Dollar not spiking': 'dollar spike', 'No yen carry unwind': 'yen carry unwind', 'Fed not hiking into weakness': 'Fed hiking into weakness', 'Bond market calm': 'bond market stress', 'Stocks not in active panic': 'panic in stocks', 'Layoffs not accelerating': 'layoffs accelerating'}

def apply_guards(bottoms, guards):
    for b in bottoms:
        fails = [FAIL_LABEL.get(nm, nm) for nm, ok, _, who in guards if not ok and (who is None or b["sym"] in who)]
        b["blocked"] = fails
        if fails and b["st"] == G:
            b["tier"], b["st"] = "Bottom pattern, but blocked: 1/3 max", Y

def djc_trend(weeks):
    full = [w for w in weeks if not w["partial"]]
    rec, base = full[-4:], full[-12:-4]
    ra = sum(w["jobs"] for w in rec) / max(len(rec), 1); ba = sum(w["jobs"] for w in base) / max(len(base), 1)
    chg = ra / ba - 1 if ba else None
    if not num(chg): return None, None
    return ("Rising" if chg >= 0.25 else "Easing" if chg <= -0.20 else "Steady"), chg

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
    "RIFSPPFAAD90NB", "DTB3", "IC4WSA", "JTSLDR", "IHLIDXUS", "TDSP", "WTREGEN", "RRPONTSYD", "WRESBAL", "ECBASSETSW", "DEXUSEU",
    "JPNASSETS", "THREEFYTP10", "USALOLITOAASTSAM", "NOCDFSA066MSFRBPHI", "NOCDINA066MNFRBNY", "PERMIT", "SUBLPDRCSN",
    "STDSAUTO", "NFCIRISK"} | {w[0] for w in WHITE_COLLAR}) - {"X:CP", "X:CCCHY", "X:VIXT", "X:BREADTH", "X:EBP"})
LONG_IDS = ["BCNSDODNS", "CMDEBT", "CSUSHPINSA", "GDP", "NCBEILQ027S"]

def build():
    S = {i: fred(i) for i in FRED_IDS}
    S.update({i: fred(i, days=26000) for i in LONG_IDS})
    P = {sym: prices(sym) for sym in ["JPY=X", "SPY", "RSP", "^VIX", "^VIX3M", "^MOVE", "BIZD", "KRE", "GC=F", "SI=F", "BTC-USD", "GLD", "SLV", "GDX"]}
    ser = lambda sym: (P.get(sym) or {}).get("series") or []
    # derived series
    S["X:CP"] = join(S["RIFSPPFAAD90NB"], S["DTB3"], lambda a, b: (a - b) * 100)
    S["X:CCCHY"] = join(S["BAMLH0A3HYC"], S["BAMLH0A0HYM2"], lambda a, b: a / b)
    S["X:VIXT"] = join(ser("^VIX"), ser("^VIX3M"), lambda a, b: a / b)
    S["X:BREADTH"] = join(ser("RSP"), ser("SPY"), lambda a, b: a / b)
    S["X:EBP"], S["X:EBPPROB"] = ebp()
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

    # ---- prediction markets
    fedodds = kalshi_fed(); polys = poly_watch()

    # ---- recession watch
    rwatch = recession_watch(S, TODAY); rwatch_prev = recession_watch(S, MONTH_AGO)

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

    # ---- liquidity and bottoms
    us_liq, g_liq = liquidity(S)
    lq = g_liq if len(g_liq) > 20 else us_liq
    lstate, ltone, lm6, lm13 = liq_state(lq, TODAY)
    lstate_prev = liq_state(lq, MONTH_AGO)[0]
    liq_ok = lstate in ("Rising", "Turning up")
    dxy_hi = max_between(S["DTWEXBGS"], TODAY - days(90), TODAY); dxy_now = at(S["DTWEXBGS"], TODAY)
    usd_ok = num(dxy_hi, dxy_now) and dxy_now / dxy_hi - 1 <= -0.02
    bottoms = []
    for sym, (nm, wash) in BOTTOM_CFG.items():
        p = P.get(sym)
        if p and len(p["closes"]) > 60:
            bottoms.append(dict(sym=sym, name=nm, price=p["price"], **bottom_score(p, wash, liq_ok, usd_ok)))

    liq_rows = []
    def lrow(name, value, st, delta, worse, sp, asof, meaning, rule):
        liq_rows.append(dict(name=name, value=value, st=st, delta=delta, worse=worse, spark=sp, asof=asof, meaning=meaning, rule=rule))
    if lq:
        lrow("Global liquidity proxy" if lq is g_liq else "US net liquidity", f"${last(lq)[1]:.1f}T", ltone,
             f"{pct(lm13)} in 13 wks" if num(lm13) else None, (lm13 < 0) if num(lm13) else None, [v for d, v in lq if d >= TODAY - days(730)], last(lq)[0],
             "Fed balance sheet minus Treasury cash and reverse repo, plus the ECB and Bank of Japan balance sheets in dollars. A free stand-in for Howell's index, which also includes China and private credit.",
             "Green: rising over 6 and 13 weeks. Yellow: turning up or rolling over. Red: falling")
    if us_liq:
        a, b = at(us_liq, TODAY), at(us_liq, TODAY - days(91))
        if num(a, b):
            lrow("US net liquidity", f"${a:.2f}T", G if a > b * 1.01 else R if a < b * 0.99 else Y, f"{pct(a / b - 1)} in 13 wks", a < b,
                 [v for d, v in us_liq if d >= TODAY - days(730)], last(us_liq)[0],
                 "Fed balance sheet minus the Treasury's cash account and reverse repo. Howell finds Bitcoin tracks Fed liquidity most closely.",
                 "Green: up 1%+ in 13 weeks. Red: down 1%+")
    a, b = at(S["WRESBAL"], TODAY), at(S["WRESBAL"], TODAY - days(91))
    if num(a, b):
        lrow("Bank reserves", f"${a/1000:.2f}T", G if a > b * 1.02 else R if a < b * 0.98 else Y, f"{pct(a / b - 1)} in 13 wks", a < b,
             tail(S, "WRESBAL", 730), last(S["WRESBAL"])[0], "Cash banks hold at the Fed. Howell says rising reserves would change his bearish view.",
             "Green: up 2%+ in 13 weeks. Red: down 2%+")
    a, b = at(S["THREEFYTP10"], TODAY), at(S["THREEFYTP10"], TODAY - days(91))
    if num(a, b):
        lrow("10-yr term premium", f"{a:.2f}%", level(a, "up", 0.75, 1.25), f"{a - b:+.2f} pts in 3 mo", a > b,
             tail(S, "THREEFYTP10", 730), last(S["THREEFYTP10"])[0], "Extra yield for holding long bonds. Rising = collateral losing value and liquidity draining.",
             "Yellow at 0.75%, red at 1.25%")
    mv = P.get("^MOVE")
    if mv:
        a, b = mv["price"], at(mv["series"], TODAY - days(30))
        lrow("MOVE (bond volatility)", f"{a:.0f}", level(a, "up", 110, 140), f"{a - b:+.0f} in 1 mo" if num(b) else None, (a > b) if num(b) else None,
             mv["closes"][-180:], TODAY, "Bond-market fear. High MOVE means bigger haircuts on collateral, which shrinks liquidity.",
             "Yellow at 110, red at 140")
    btc = P.get("BTC-USD"); gld = P.get("GC=F")
    howell = []
    if btc:
        held = min([c for d, c in btc["series"] if d >= TODAY - days(28)] or [0])
        howell.append(("Bitcoin holding above $70,000 (4 weeks)", f"4-week low {money(held)}", held >= 70000))
    a, b = at(S["WRESBAL"], TODAY), at(S["WRESBAL"], TODAY - days(91))
    howell.append(("Bank reserves being pushed up", f"{pct(a / b - 1)} in 13 weeks" if num(a, b) else "n/a", num(a, b) and a > b * 1.02))
    if btc and gld:
        ratio = [(d, c / at(gld["series"], d)) for d, c in btc["series"] if num(at(gld["series"], d))]
        if ratio:
            rv = [v for _, v in ratio]; pos = (rv[-1] - min(rv)) / (max(rv) - min(rv)) if max(rv) > min(rv) else 0.5
            howell.append(("Bitcoin cheap vs gold (sentiment washed out)", f"{rv[-1]:.1f} oz of gold per BTC, {pos*100:.0f}% of its 1-yr range", pos <= 0.2))
    # lead chart: liquidity 6-wk change advanced 13 weeks vs BTC 6-wk change
    lead = None
    if btc and len(lq) > 30:
        pts_l = [(d + days(91), momentum(lq, d, 42)) for d, _ in lq if d >= TODAY - days(365 + 91)]
        pts_b = [(d, momentum(btc["series"], d, 42)) for d, _ in btc["series"][::7] if d >= TODAY - days(365)]
        lead = dict(l=[(d, v) for d, v in pts_l if num(v)], b=[(d, v) for d, v in pts_b if num(v)])

    # ---- main street, jobs, build-up
    ms = main_street(S, P)
    jb = jobs(S, P, dj)
    bu = buildup(S, spx)
    weeks = djc_weeks(dj) if dj.get("ok") else []
    lay_trend = djc_trend(weeks)[0] if weeks else None
    guards = trap_guards(core, S, P, capitulation, lay_trend)
    apply_guards(bottoms, guards)
    nb_conf = sum(1 for b in bottoms if b["st"] == G); nb_bot = sum(1 for b in bottoms if b["st"] in (G, Y))
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
            bs = next((b_ for b_ in bottoms if b_["sym"] == sym), None)
            hard.append(dict(name=nm, sub=sub, price=p["price"], st=price_status(m), spark=p["closes"], bscore=bs["n"] if bs else None, **m))
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
             fp=fp, fed_light=core[-1], fedodds=fedodds, polys=polys, rwatch=rwatch, rwatch_prev=rwatch_prev, lstate=lstate, ltone=ltone, lstate_prev=lstate_prev, liq_rows=liq_rows, bottoms=bottoms,
             nb_conf=nb_conf, nb_bot=nb_bot, howell=howell, guards=guards, lead=lead, liq_ok=liq_ok, usd_ok=usd_ok, duel=duel, nd=nd, nk=nk, ms=ms, jb=jb, bu=bu, weeks=weeks, dj=dj, wc=wc, wc_all=wc_all, hard=hard, ratios=ratios, watch=watch, off=off)
    prev_hist = load_history()
    snap = make_snapshot(d)
    d["snap"] = snap
    if MOCK: prev_hist = mock_history(snap)
    wk = [h for h in prev_hist if dt.datetime.fromisoformat(h["t"]) <= NOW - dt.timedelta(days=6.5)]
    d["odds_week"] = (wk[-1] if wk else {}).get("odds", {})
    run_alerts(prev_hist[-1] if prev_hist else None, snap)
    hist = save_history(prev_hist, snap)
    json.dump(hist, open(os.path.join(OUT, "history.json"), "w"), separators=(",", ":"))
    open(os.path.join(OUT, "index.html"), "w").write(render(d))
    open(os.path.join(OUT, "manifest.webmanifest"), "w").write(json.dumps({
        "name": "System Health", "short_name": "Sys Health", "start_url": "./", "display": "standalone",
        "background_color": "#0f1a14", "theme_color": "#0f1a14", "icons": [{"src": "icon.png", "sizes": "180x180", "type": "image/png"}]}))
    write_icon(os.path.join(OUT, "icon.png"))
    slim = {"updated": NOW.isoformat(), "regime": regime, "regime_month_ago": calc_prev, "liquidity": lstate,
            "recession_watch": [[n_, bool(l_), r_] for n_, l_, r_ in rwatch],
            "bottoms": [{k: v for k, v in b_.items() if k != "checks"} | {"checks": [[c[0], bool(c[1])] for c in b_["checks"]]} for b_ in bottoms],
            "crisis_core": [{k: v for k, v in l.items()} for l in core],
            "gauges": [{k: v for k, v in g.items() if k != "spark"} for g in gauges],
            "main_street": [{k: v for k, v in r.items() if k != "spark"} for r in ms],
            "watchlist": [{k: v for k, v in w.items() if k != "spark"} for w in watch]}
    json.dump(slim, open(os.path.join(OUT, "data.json"), "w"), default=str, indent=1)
    json.dump({"fetched": dj.get("fetched"), "ok": dj.get("ok"), "ests": dj.get("ests", {}),
               "entries": [(str(d_), n_, j_) for d_, n_, j_ in dj.get("entries", [])]}, open(os.path.join(OUT, "djc.json"), "w"))
    print(f"Built: regime {regime} (month ago {calc_prev}), {reds} red / {yels} yellow lights, {len(watch)} tickers")

# =========================================================================== push alerts (ntfy)
# Compares this run with the previous one and sends only important changes. Each alert type has a 24-hour cooldown.

def site_url():
    if REPO and "/" in REPO:
        owner, repo = REPO.split("/", 1)
        return f"https://{owner.lower()}.github.io/{repo}/"
    return ""

def find_alerts(prev, now):
    """Returns list of (key, priority 1-5, title, body)."""
    if not prev: return []
    A = []
    RK = {"GREEN": 0, "YELLOW": 1, "RED": 2}
    pr, nr = prev.get("regime"), now["regime"]
    if pr in RK and nr in RK and pr != nr:
        worse = RK[nr] > RK[pr]
        A.append((f"regime-{nr}", 5 if nr == "RED" else 4, f"Regime {'worsened' if worse else 'improved'}: {pr} \u2192 {nr}",
                  f"{sum(1 for v in now['lights'].values() if v == 'RED')} of 6 crisis lights red."))
    for k, st in now["lights"].items():
        was = prev.get("lights", {}).get(k)
        if st == "RED" and was in ("GREEN", "YELLOW"):
            A.append((f"light-{k}", 4, f"Crisis light red: {k}", f"Now {now.get('lightv', {}).get(k, '')}. Was {was.lower()}."))
    pw, nw = prev.get("rw"), now["rw"]
    if pw is not None and nw > pw and nw >= 2:
        new = [x for x in now["rw_lit"] if x not in prev.get("rw_lit", [])]
        A.append((f"rw-{nw}", 5 if nw >= 4 else 4, f"Recession watch: {pw} \u2192 {nw} of 8 lit", ("New: " + ", ".join(new)) if new else ""))
    pl, nl = prev.get("liq"), now["liq"]
    if pl and nl and pl != nl and nl in ("Turning up", "Falling", "Rising"):
        A.append((f"liq-{nl}", 4 if nl == "Turning up" else 3, f"Liquidity: {pl} \u2192 {nl}",
                  "Howell's lead suggests Bitcoin follows in roughly 13 weeks." if nl in ("Turning up", "Rising") else "Liquidity draining. Hard-asset bottoms less likely to hold."))
    for nm, (n, st) in now["bottoms"].items():
        was = prev.get("bottoms", {}).get(nm, [0, ""])
        if st == "GREEN" and was[1] != "GREEN":
            A.append((f"bottom-{nm}", 5, f"BUY SIGNAL: {nm} bottom confirmed", f"Score {n} of 6, liquidity rising, all bull-trap guards clear."))
    if (prev.get("cap") or 0) < 3 <= now["cap"]:
        A.append(("capitulation", 5, "Capitulation signal ON", "Blood in the streets: start buying in 1/3 tranches even if the regime is red."))
    if (prev.get("piv") or 0) < 2 <= now["piv"]:
        A.append(("pivot", 4, f"Fed/Treasury pivot forming: {now['piv']} of 5", "Consider a first tranche of dry powder."))
    po, no = prev.get("odds", {}), now.get("odds", {})
    if no.get("fed0:date") == po.get("fed0:date"):
        a_, b_ = po.get("fed0:hike_big", 0), no.get("fed0:hike_big", 0)
        if b_ >= 0.30 > a_:
            A.append(("odds-bighike", 4, f"Odds of a 50 bp hike jump to {b_*100:.0f}%", f"Kalshi, Fed meeting {no.get('fed0:date')}. Policy-error risk rising."))
    for k, v in no.items():
        if k.startswith("poly:") and "recession" in k.lower() and isinstance(v, float):
            pv = po.get(k)
            for th in (0.30, 0.50):
                if isinstance(pv, float) and pv < th <= v:
                    A.append((f"odds-rec-{int(th*100)}", 4, f"Market recession odds cross {int(th*100)}%", f"Polymarket now {v*100:.0f}%, was {pv*100:.0f}%."))
    newg = [g for g in now.get("guards_fail", []) if g not in prev.get("guards_fail", [])]
    if newg:
        A.append(("guards-" + "-".join(sorted(newg)), 3, f"Bull-trap warning: {', '.join(FAIL_LABEL.get(g, g) for g in newg)}", "Full-size hard-asset buys are blocked until it clears."))
    return A

def ntfy_send(title, body, priority=3, tags="chart_with_downwards_trend"):
    if MOCK or not NTFY_TOPIC:
        print(f"[alert p{priority}] {title} | {body}"); return
    try:
        payload = {"topic": NTFY_TOPIC, "title": title, "message": body or " ", "priority": int(priority),
                   "tags": [tags], "click": site_url() or None}
        req = urllib.request.Request("https://ntfy.sh/", data=json.dumps({k: v for k, v in payload.items() if v is not None}).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json", "User-Agent": UA["User-Agent"]})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        print(f"ntfy failed: {e}")

def run_alerts(prev, snap):
    sent = dict(prev.get("alerts", {})) if prev else {}
    snap["alerts"] = sent
    if not NTFY_TOPIC and not MOCK: return
    if prev and not prev.get("alerts_on"):
        ntfy_send("Dashboard alerts connected", f"You'll get a push when something important changes. Regime now {snap['regime']}.", 3, "white_check_mark")
    snap["alerts_on"] = True
    for key, pri, title, body in find_alerts(prev, snap):
        last_t = sent.get(key)
        if last_t and (NOW - dt.datetime.fromisoformat(last_t)).total_seconds() < 24 * 3600: continue
        tag = "rotating_light" if pri >= 5 else "warning" if "worse" in title.lower() or "red" in title.lower() else "bell"
        ntfy_send(title, body, pri, tag); sent[key] = NOW.isoformat(timespec="seconds")
    # Monday digest: first run after 12:00 UTC on Mondays
    wk = NOW.strftime("%G-W%V")
    if NOW.weekday() == 0 and NOW.hour >= 12 and sent.get("digest") != wk:
        lights = ", ".join(f"{k.split()[0]} {v.lower()}" for k, v in snap["lights"].items())
        bt = ", ".join(f"{k} {v[0]}/6" for k, v in snap["bottoms"].items())
        ntfy_send(f"Weekly: regime {snap['regime']}, recession {snap['rw']}/8, liquidity {snap['liq'].lower()}",
                  f"Lights: {lights}. Bottom scores: {bt}.", 2, "calendar")
        sent["digest"] = wk
    snap["alerts"] = {k: v for k, v in sent.items() if k == "digest" or (NOW - dt.datetime.fromisoformat(v)).days < 7}

# =========================================================================== snapshot for "what changed"

LIQ_RANK = {"Rising": 0, "Turning up": 1, "Rolling over": 2, "Falling": 3, "No data": None}

def make_snapshot(d):
    rw_lit = [n for n, lit, _ in d["rwatch"] if lit]
    sts = lambda rows: {r["name"]: r["st"] for r in rows}
    return {
        "t": NOW.isoformat(timespec="seconds"),
        "regime": d["regime"],
        "lights": {l["name"]: l["st"] for l in d["core"]},
        "lightv": {l["name"]: l["value"] for l in d["core"]},
        "rw": len(rw_lit), "rw_lit": rw_lit,
        "liq": d["lstate"],
        "guards_fail": [nm for nm, ok, _, _ in d["guards"] if not ok],
        "bottoms": {b["name"]: [b["n"], b["st"]] for b in d["bottoms"]},
        "cap": d["ncap"], "piv": d["npiv"],
        "fed": d["fed_light"]["st"],
        "gauges": {g["name"]: [g["st"], fmt(g["v"], g["unit"]) if not g.get("vtext") else g["vtext"]] for g in d["gauges"]},
        "jobs": sts(d["jb"]), "main": sts(d["ms"]), "build": sts(d["bu"]),
        "odds": odds_flat(d),
    }

def odds_flat(d):
    o = {}
    for i, m in enumerate(d.get("fedodds", [])):
        hikes = m["odds"].get("hike25", 0) + m["odds"].get("hike_big", 0); cuts = m["odds"].get("cut25", 0) + m["odds"].get("cut_big", 0)
        o[f"fed{i}:hike"] = round(hikes, 4); o[f"fed{i}:cut"] = round(cuts, 4); o[f"fed{i}:hike_big"] = round(m["odds"].get("hike_big", 0), 4)
        o[f"fed{i}:date"] = str(m["date"])
    for pm in d.get("polys", []):
        o["poly:" + pm["label"]] = round(pm["p"], 4)
    return o

def mock_history(snap):
    """Sample data only: fake older snapshots so the panel can be tested."""
    out = []
    for k, tweak in [(30, 2), (8, 1), (3, 1), (1, 0)]:
        s = json.loads(json.dumps(snap))
        s["t"] = (NOW - dt.timedelta(days=k)).isoformat(timespec="seconds")
        if tweak >= 1:
            s["regime"] = "YELLOW"; first = next(iter(s["lights"])); s["lights"][first] = "RED"
            s["rw"] = snap["rw"] + 1; s["rw_lit"] = snap["rw_lit"] + ["Jobless claims up 15%+"]
            for i, name in enumerate(list(s["gauges"])[:6]):
                s["gauges"][name][0] = {"GREEN": "YELLOW", "YELLOW": "RED", "RED": "YELLOW"}.get(s["gauges"][name][0], "GREEN") if i % 2 == 0 else s["gauges"][name][0]
        if "odds" in s:
            s["odds"] = {k: (round(max(0, min(1, v - 0.05 * tweak)), 4) if isinstance(v, float) else v) for k, v in s["odds"].items()}
        if tweak == 2:
            s["liq"] = "Falling"; s["guards_fail"] = snap["guards_fail"] + ["Credit spreads not widening"]
        out.append(s)
    return out

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

    # ---------------- recession watch
    rw, rwp = d["rwatch"], d["rwatch_prev"]
    nrw, nrwp = sum(1 for x in rw if x[1]), sum(1 for x in rwp if x[1])
    rw_tone = "red" if nrw >= 4 else "yellow" if nrw >= 2 else "green"
    rw_word = "High recession risk: signals clustering" if nrw >= 4 else "Watch: some warning signs" if nrw >= 2 else "Low recession risk"
    rw_trend = f"&#9650; up from {nrwp} a month ago" if nrw > nrwp else f"&#9660; down from {nrwp} a month ago" if nrw < nrwp else "same as a month ago"
    rw_cls = "worse" if nrw > nrwp else "better" if nrw < nrwp else "flat"
    rw_items = "".join(f'''<li class="{"lit" if lit else ""}"><span class="rl"></span><div><b>{E(n)}</b><small>{E(r)}</small></div></li>''' for n, lit, r in rw)

    # ---------------- market odds
    ow = d.get("odds_week", {}) or {}
    def wchg(key, v, bad_up=True):
        pv = ow.get(key)
        if not isinstance(pv, float) or not num(v): return ""
        dd = v - pv
        if abs(dd) < 0.005: return '<span class="arr flat">&#9644; flat vs 1 wk</span>'
        worse = (dd > 0) if bad_up else (dd < 0)
        return f'<span class="arr {"worse" if worse else "better"}">{"&#9650;" if dd > 0 else "&#9660;"} {abs(dd)*100:.0f} pts vs 1 wk</span>'
    odds_html = ""
    fo = d.get("fedodds") or []
    if fo:
        m0 = fo[0]; order = [("hike_big", "Hike more than 25 bp", "red"), ("hike25", "Hike 25 bp", "yellow"), ("hold", "Hold", "grey"),
                             ("cut25", "Cut 25 bp", "green"), ("cut_big", "Cut more than 25 bp", "green")]
        bars = "".join(f'''<li class="s-{tone}"><span>{E(nm)}</span><div class="track"><i style="width:{max(1, m0["odds"].get(k, 0)*100):.1f}%"></i></div><b>{m0["odds"].get(k, 0)*100:.0f}%</b></li>'''
                       for k, nm, tone in order if m0["odds"].get(k, 0) >= 0.005 or k in ("hike25", "hold", "cut25"))
        later = "".join(f'''<li><b>{E(m["label"])}</b><span>hike {(m["odds"].get("hike25",0)+m["odds"].get("hike_big",0))*100:.0f}% &middot; hold {m["odds"].get("hold",0)*100:.0f}% &middot; cut {(m["odds"].get("cut25",0)+m["odds"].get("cut_big",0))*100:.0f}%</span></li>''' for m in fo[1:])
        hk = m0["odds"].get("hike25", 0) + m0["odds"].get("hike_big", 0)
        odds_html += f'''<div class="odd"><div class="djh"><b>Next Fed meeting: {E(m0["label"])}</b>{wchg("fed0:hike", hk)}</div>
<ul class="obars">{bars}</ul>{f'<ul class="olater">{later}</ul>' if later else ''}
<p class="muted fine">Kalshi (CFTC-regulated), mid of bid and ask. A rising chance of a hike is colored as worse.</p></div>'''
    GOODW = ("normal", "reopen", "ceasefire", "peace", "deal", "agreement", "avoid")
    def pm_row(pm):
        q = (pm["question"] or "").lower(); yes_good = any(w in q for w in GOODW); pv = pm["p"]
        bad = (1 - pv) if yes_good else pv
        tone = "red" if bad >= 0.5 else "yellow" if bad >= 0.25 else "grey"
        return f'''<li><div><b>{E(pm["label"])}</b><small>{E(pm["question"] or "")}{(" &middot; ends " + E(pm["end"])) if pm.get("end") else ""}</small></div>
<div class="oy"><span class="pill s-{tone}">{pv*100:.0f}%</span>{wchg("poly:" + pm["label"], pv, not yes_good)}</div></li>'''
    pm_rows = "".join(pm_row(pm) for pm in d.get("polys", []))
    if pm_rows:
        odds_html += f'''<ul class="pml">{pm_rows}</ul><p class="muted fine">Polymarket, chance of "Yes". The code picks the most-traded open contract matching each topic and rolls to new ones as old ones expire.</p>'''

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
    ] + ([frow(f"Odds of a 50 bp hike, {d['fedodds'][0]['label']}", f"{d['fedodds'][0]['odds'].get('hike_big', 0)*100:.0f}%",
               d['fedodds'][0]['odds'].get('hike_big', 0) >= 0.30, "Kalshi prediction market. 30%+ = an outsized hike is a live risk")] if d.get("fedodds") else []))
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

    # ---------------- liquidity and bottoms
    bt = d["bottoms"]
    all_conf = len(bt) == 3 and d["nb_conf"] == 3
    blocked_any = any(b_["blocked"] and b_["n"] >= 5 for b_ in bt)
    if all_conf: lverd, lvt = "BUY IN SIZE: Bitcoin, gold and silver bottoms confirmed, liquidity turning up, no bull-trap warnings", "green"
    elif d["nb_conf"] >= 1: lverd, lvt = f"{d['nb_conf']} of 3 confirmed bottoms. Buy those in size; build the rest in thirds", "green"
    elif blocked_any: lverd, lvt = "Bottom patterns showing, but bull-trap guards are failing. 1/3 positions at most", "yellow"
    elif d["nb_bot"] >= 2: lverd, lvt = (f"{d['nb_bot']} of 3 bottoming. Build 1/3 positions; size up as each confirms" if d["liq_ok"] else f"{d['nb_bot']} of 3 bottoming. Build 1/3 positions; wait for liquidity to turn"), "yellow"
    else: lverd, lvt = "No hard-asset bottom yet. Hold dry powder", "red" if d["ltone"] == R else "grey"
    cards = []
    for b_ in bt:
        dots = "".join(f'<i class="{"on" if c[1] else ""}"></i>' for c in b_["checks"])
        blk = f'<span class="blk">Blocked: {E(", ".join(b_["blocked"]))}</span>' if b_["blocked"] and b_["n"] >= 3 else ""
        cards.append(f'''<div class="bcard s-{TONE[b_["st"]]}"><div class="bh"><b>{E(b_["name"])}</b><span>{money(b_["price"])}</span></div>
<div class="bs"><span class="bn">{b_["n"]}<small>/6</small></span><span class="dots">{dots}</span></div><span class="pill">{E(b_["tier"])}</span>{blk}</div>''')
    matrix = ""
    if bt:
        head = "".join(f"<th>{E(b_['name'])}</th>" for b_ in bt)
        body_ = ""
        for i, c in enumerate(bt[0]["checks"]):
            cells = "".join(f'<td class="{"y" if b_["checks"][i][1] else "n"}">{"&#10003;" if b_["checks"][i][1] else "&#10005;"}</td>' for b_ in bt)
            body_ += f'<tr><th scope="row">{E(c[0])}<small>{E(re.sub(r" [(]needs.*", "", c[2]) if c[0] != "Washout" else "fell hard from a recent peak")}</small></th>{cells}</tr>'
        washes = ", ".join(f"{E(b_['name'])} {BOTTOM_CFG[b_['sym']][1]*100:.0f}% (now {pct(b_['mdd'], False).lstrip('-')})" for b_ in bt)
        matrix = f'''<table class="bmx"><thead><tr><th></th>{head}</tr></thead><tbody>{body_}</tbody></table>
<p class="muted fine">Washout means a drop from a peak in the last 4 months of at least: {washes}.</p>'''
    gd = d["guards"]; gfail = [x for x in gd if not x[1]]
    gl = "".join(f'''<li class="{"ok" if ok else "bad"}"><span class="gk">{"&#10003;" if ok else "&#10005;"}</span><div><b>{E(nm)}</b><small>{E(rd)}{" &middot; applies to Bitcoin and silver" if who else ""}</small></div></li>''' for nm, ok, rd, who in gd)
    gverd = (f"All {len(gd)} guards clear: a bottom signal here is not a bull trap by these measures", "green") if not gfail else \
            (f"{len(gfail)} of {len(gd)} guards failing: 'buy in size' is blocked until they clear", "red" if len(gfail) >= 3 else "yellow")
    hw = "".join(f'<li class="{"hit" if hit else ""}"><span class="dot"></span><div><b>{E(n)}</b><small>{E(r)}</small></div><span class="yn">{"YES" if hit else "no"}</span></li>' for n, r, hit in d["howell"])
    liq_html = "".join(mrow(r["name"], r["value"], r["st"], r["delta"], r["worse"], r["spark"], r["asof"], r["meaning"], r["rule"]) for r in d["liq_rows"])
    lead_svg = ""
    ld = d["lead"]
    if ld and ld["l"] and ld["b"]:
        W, H = 340, 150; x0, x1 = TODAY - days(365), TODAY + days(91)
        X = lambda dd: (dd - x0).days / (x1 - x0).days * W
        sl = max(abs(v) for _, v in ld["l"]) or 1; sb = max(abs(v) for _, v in ld["b"]) or 1
        Yl = lambda v: H / 2 - v / sl * (H / 2 - 12); Yb = lambda v: H / 2 - v / sb * (H / 2 - 12)
        lp = [(X(dd), Yl(v)) for dd, v in ld["l"] if x0 <= dd <= x1]
        bp = [(X(dd), Yb(v)) for dd, v in ld["b"] if x0 <= dd <= x1]
        area = f"M{lp[0][0]:.1f},{H/2:.1f} " + " ".join(f"L{x:.1f},{y:.1f}" for x, y in lp) + f" L{lp[-1][0]:.1f},{H/2:.1f} Z" if lp else ""
        bl = " ".join(f"{x:.1f},{y:.1f}" for x, y in bp)
        tx = X(TODAY)
        lead_svg = f'''<div class="lead"><div class="djh"><b>Liquidity leads Bitcoin by ~13 weeks</b></div>
<svg viewBox="0 0 {W} {H}" class="leadc" role="img" aria-label="Liquidity momentum shifted 13 weeks ahead vs Bitcoin momentum">
<rect x="{tx:.1f}" y="0" width="{W-tx:.1f}" height="{H}" class="fut"/><line x1="0" x2="{W}" y1="{H/2}" y2="{H/2}" class="zero"/>
<path d="{area}" class="la"/><polyline points="{bl}" class="bl"/><line x1="{tx:.1f}" x2="{tx:.1f}" y1="0" y2="{H}" class="now"/>
<text x="{tx+4:.1f}" y="12" class="ax">next 13 wks</text><text x="2" y="{H-3}" class="ax">{x0.strftime("%b %Y")}</text></svg>
<p class="leg"><span><i class="k1"></i>Liquidity, 6-wk change, shifted 13 wks ahead</span><span><i class="k2"></i>Bitcoin, 6-wk change</span></p>
<p class="muted fine">Both scaled to fit. The shaded area past today is where liquidity points Bitcoin next, per Howell's lead. A guide, not a promise: Howell reports an R-squared of about 0.3.</p></div>'''

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
<div class="tf">{E(h["trend"])}{' &middot; ' if h["trend"] else ''}{pct(h["off_high"])} from high{f' &middot; bottom score {h["bscore"]}/6' if h.get("bscore") is not None else ''}</div></div>''' for h in d["hard"])
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
    if len(d["bottoms"]) == 3 and d["nb_conf"] == 3:
        cap_note += '<p class="note on">BUY IN SIZE: Bitcoin, gold and silver bottoms confirmed with liquidity turning. See "Liquidity".</p>'
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
<div><b>{nrw} of 8</b><small>recession signals lit ({nrwp} a month ago)</small></div>
<div><b>{E(d["lstate"])}</b><small>liquidity (a month ago: {E(d["lstate_prev"].lower())})</small></div>
<div><b>{d["nb_conf"]} of 3</b><small>hard-asset bottoms confirmed ({d["nb_bot"]} bottoming)</small></div>
</div>{cap_note}{ov_note}
<div class="stale" id="stale">Data is more than 3 hours old. The hourly update may have paused.</div>
</div></header>
<section class="wrap chg" id="changes"><div class="chh"><h2>What changed</h2>
<div class="chips" role="tablist"><button data-w="last" class="on">Since last visit</button><button data-w="1">1 day</button><button data-w="7">1 week</button><button data-w="30">1 month</button></div></div>
<div id="chsum" class="verdict s-grey">Loading change history&hellip;</div><ul id="chlist" class="chl"></ul>
<details id="chmore" hidden><summary>Show all changes</summary><ul id="chall" class="chl"></ul></details>
<script type="application/json" id="snapnow">{json.dumps(d["snap"]).replace("</", "<\\/")}</script></section>
<nav class="jump wrap"><a href="#core">Crisis core</a><a href="#recession">Recession</a><a href="#odds">Odds</a><a href="#liq">Liquidity</a><a href="#blood">Blood</a><a href="#pivot">Pivot</a><a href="#fed">Fed</a><a href="#jobs">Jobs</a><a href="#main">Main St</a><a href="#buildup">Build-up</a><a href="#duel">Dale/Karsan</a><a href="#hard">Hard assets</a><a href="#gauges">Gauges</a><a href="#watch">Watchlist</a></nav>
<div class="legend wrap"><span><i class="s-green"></i>Healthy</span><span><i class="s-yellow"></i>Caution</span><span><i class="s-red"></i>Stress</span><span class="arr better">&#9660; better</span><span class="arr worse">&#9650; worse</span></div>
<main class="wrap">
<section id="core"><h2>Crisis core</h2><p class="lede">The six channels behind past breaks. They alone set the regime: 4+ red = RED; 2+ red, with yellows counting half = YELLOW. Tap a light for its trigger.</p>
<div class="lights">{"".join(core)}</div></section>

<section id="recession"><h2>Recession watch</h2><div class="verdict s-{rw_tone}"><b>{nrw} of 8 lit</b> &middot; {E(rw_word)} <span class="arr {rw_cls}">{rw_trend}</span></div>
<p class="lede">The classic leading signals. One alone means little; watch for clusters and acceleration. Crisis lights measure financial stress; this measures recession risk. 2022 had stress without a recession.</p>
<ul class="rwl">{rw_items}</ul></section>

<section id="odds"><h2>Market odds</h2><p class="lede">What real-money prediction markets expect. Context only: these don't set the regime, and thin markets can be noisy.</p>{odds_html or '<p class="muted">Prediction-market data unavailable this hour.</p>'}</section>

<section id="liq"><h2>Liquidity and hard-asset bottoms</h2><div class="verdict s-{lvt}"><b>Liquidity: {E(d["lstate"])}</b> &middot; {E(lverd)}</div>
<p class="lede">Following Michael Howell: liquidity drives Bitcoin (about 8x sensitive) and gold and silver (about 2x). Each asset gets a 6-point bottom score. "Buy in size" needs 5+ points including a washout, a base above the 50-day and rising liquidity, and every bull-trap guard clear.</p>
<div class="bcards">{"".join(cards)}</div>{matrix}
<h3 id="guards">Bull-trap guards</h3><div class="verdict s-{gverd[1]}">{E(gverd[0])}</div>
<p class="lede">A rebound inside a building credit or funding crisis is a trap. Any failing guard downgrades "buy in size" to 1/3 at most. The crisis and panic guards lift once the capitulation signal is on, because real bottoms come after the panic.</p>
<ul class="guards">{gl}</ul>{lead_svg}
<h3>Howell's own change-of-view tests</h3><ul class="chk">{hw}</ul>
<h3>Liquidity gauges</h3>{liq_html}</section>

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
<script>%%CHANGES_JS%%</script>
<script>{{const el=document.getElementById('upd');const t=new Date(el.dataset.t);const m=Math.round((Date.now()-t)/60000);
el.textContent='Updated '+(m<1?'just now':m<60?m+' min ago':m<1440?Math.round(m/60)+' hr ago':Math.round(m/1440)+' days ago');
if(m>180)document.getElementById('stale').style.display='block';}}</script>'''
    return HEAD + body.replace("%%CHANGES_JS%%", CHANGES_JS) + "\n</body></html>"


CHANGES_JS = r"""
(function(){
  const RANK={GREEN:0,YELLOW:1,RED:2,NODATA:null,"NO DATA":null};
  const LIQ={"Rising":0,"Turning up":1,"Rolling over":2,"Falling":3};
  const WORD={GREEN:"green",YELLOW:"yellow",RED:"red",NODATA:"no data"};
  const now=JSON.parse(document.getElementById('snapnow').textContent);
  const sum=document.getElementById('chsum'), list=document.getElementById('chlist'), more=document.getElementById('chmore'), all=document.getElementById('chall');
  let hist=[], mode='last', prevVisit=null;
  try{const cur=+localStorage.getItem('sh_cur')||0, prv=+localStorage.getItem('sh_prev')||0, t=Date.now();
      if(!cur||t-cur>30*60*1000){ if(cur) localStorage.setItem('sh_prev',cur); localStorage.setItem('sh_cur',t); prevVisit=cur||null; }
      else { prevVisit=prv||null; }}catch(e){}
  function ago(ms){const h=ms/36e5; if(h<1) return 'under an hour ago'; if(h<36) return Math.round(h)+' hours ago'; return Math.round(h/24)+' days ago';}
  function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
  function diff(b){
    const out=[]; // [weight, dir(+1 worse/-1 better), text]
    const st=(label,a,c,w,extra)=>{const ra=RANK[a],rc=RANK[c]; if(ra==null||rc==null||ra===rc) return;
      out.push([w,rc>ra?1:-1,label+': '+WORD[a]+' → '+WORD[c]+(extra?' ('+extra+')':'')]);};
    if(b.regime!==now.regime && RANK[b.regime]!=null && RANK[now.regime]!=null) out.push([5,RANK[now.regime]>RANK[b.regime]?1:-1,'Regime: '+b.regime+' → '+now.regime]);
    for(const k in now.lights) st(k,(b.lights||{})[k],now.lights[k],3,(now.lightv||{})[k]);
    if(b.rw!=null && b.rw!==now.rw){const added=now.rw_lit.filter(x=>!(b.rw_lit||[]).includes(x)), gone=(b.rw_lit||[]).filter(x=>!now.rw_lit.includes(x));
      out.push([3,now.rw>b.rw?1:-1,'Recession signals: '+b.rw+' → '+now.rw+' of 8'+(added.length?' (new: '+added.join(', ')+')':'')+(gone.length?' (cleared: '+gone.join(', ')+')':'')]);}
    if(b.liq && LIQ[b.liq]!=null && LIQ[now.liq]!=null && b.liq!==now.liq) out.push([3,LIQ[now.liq]>LIQ[b.liq]?1:-1,'Liquidity: '+b.liq+' → '+now.liq]);
    const gf=(b.guards_fail||[]).length, gn=(now.guards_fail||[]).length;
    if(gf!==gn) out.push([2,gn>gf?1:-1,'Bull-trap guards failing: '+gf+' → '+gn]);
    for(const grp of [['jobs','Jobs'],['main','Main Street'],['build','Build-up']]){const B=b[grp[0]]||{},C=now[grp[0]]||{}; for(const k in C) st(grp[1]+' · '+k,B[k],C[k],1.5);}
    const BG=b.gauges||{}; for(const k in now.gauges){const c=now.gauges[k], a=BG[k]; if(a) st(k,a[0],c[0],1,(a[1]&&c[1])?a[1]+' → '+c[1]:'');}
    const opp=[]; const BB=b.bottoms||{};
    for(const k in now.bottoms){const a=BB[k],c=now.bottoms[k]; if(a&&a[0]!==c[0]) opp.push(k+' bottom score: '+a[0]+' → '+c[0]+' of 6');}
    if(b.cap!=null&&b.cap!==now.cap) opp.push('Capitulation signals: '+b.cap+' → '+now.cap+' of 5');
    if(b.piv!=null&&b.piv!==now.piv) opp.push('Pivot signals: '+b.piv+' → '+now.piv+' of 5');
    return {out,opp};
  }
  function pick(){
    if(!hist.length) return null;
    let target;
    if(mode==='last'){ if(!prevVisit) return {first:true}; target=prevVisit; }
    else target=Date.now()-(+mode)*864e5;
    let best=null; for(const h of hist){const t=Date.parse(h.t); if(t<=target) best=h;}
    if(!best) return {early:hist[0]};
    return best;
  }
  function render(){
    document.querySelectorAll('.chips button').forEach(x=>x.classList.toggle('on',x.dataset.w===mode));
    list.innerHTML=''; all.innerHTML=''; more.hidden=true;
    const b=pick();
    if(!b){sum.className='verdict s-grey'; sum.textContent='Change history is still building. Check back after a few hourly updates.'; return;}
    if(b.first){sum.className='verdict s-grey'; sum.textContent='First visit on this device. From your next visit, this shows what changed since you last looked.'; return;}
    if(b.early){sum.className='verdict s-grey'; sum.textContent='History only goes back to '+new Date(b.early.t).toLocaleDateString()+'. Try a shorter window.'; return;}
    const {out,opp}=diff(b);
    const worse=out.filter(x=>x[1]>0), better=out.filter(x=>x[1]<0);
    const score=out.reduce((s,x)=>s+x[0]*x[1],0);
    const when=ago(Date.now()-Date.parse(b.t));
    const tone=score>=3?'red':score<=-3?'green':'grey';
    const dir=score>=3?'Getting worse':score<=-3?'Getting better':'Little net change';
    sum.className='verdict s-'+tone;
    sum.innerHTML='<b>'+dir+'</b> since '+esc(when)+' <span class="cc"><span class="arr worse">&#9650; '+worse.length+' worse</span> <span class="arr better">&#9660; '+better.length+' better</span></span>';
    const sorted=out.slice().sort((x,y)=>(y[1]-x[1])||(y[0]-x[0]));
    const li=x=>'<li class="'+(x[1]>0?'w':'b')+'"><span class="arr '+(x[1]>0?'worse':'better')+'">'+(x[1]>0?'&#9650;':'&#9660;')+'</span>'+esc(x[2])+'</li>';
    const top=sorted.filter(x=>x[0]>=1.5).slice(0,8);
    list.innerHTML=(top.length?top.map(li).join(''):'<li class="n">No change in the regime, crisis lights, recession watch, liquidity or panels.</li>')
      +opp.map(t=>'<li class="o"><span class="arr flat">&#9679;</span>'+esc(t)+'</li>').join('');
    if(sorted.length>top.length){more.hidden=false; all.innerHTML=sorted.map(li).join('');}
  }
  document.querySelectorAll('.chips button').forEach(btn=>btn.addEventListener('click',()=>{mode=btn.dataset.w; render();}));
  fetch('history.json',{cache:'no-store'}).then(r=>r.ok?r.json():[]).then(h=>{hist=Array.isArray(h)?h:[]; if(!prevVisit) mode='1'; render();})
    .catch(()=>{sum.textContent='Change history could not load.';});
})();
"""

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
.mv{display:flex;flex-direction:column;align-items:flex-end;gap:3px;max-width:44vw}
.mv .arr{white-space:normal;text-align:right;line-height:1.25}
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
/* what changed */
.chg{padding-top:16px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.chh{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-bottom:10px}
.chh h2{margin:0}
.chips{display:flex;gap:4px;background:var(--card);border-radius:999px;padding:3px;border:1px solid var(--line)}
.chips button{font:600 13px Inter,system-ui,sans-serif;border:0;background:none;color:var(--mut);padding:8px 10px;border-radius:999px;min-height:36px;cursor:pointer}
.chips button.on{background:var(--ink);color:var(--bg)}
.chips button:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.cc{display:block;margin-top:2px;font-size:13px}.cc .arr{font-size:13px;margin-right:10px}
.chl{list-style:none;padding:0;margin:0 0 8px}
.chl li{display:grid;grid-template-columns:20px minmax(0,1fr);gap:6px;padding:7px 0;border-top:1px solid var(--line);font-size:14px;line-height:1.35}
.chl li.n{display:block;color:var(--mut)}
#chmore summary{font-size:14px;font-weight:600;padding:8px 0;color:var(--mut)}
/* odds */
.odd{background:var(--card);border-radius:14px;padding:12px;margin:0 0 10px}
.obars,.olater,.pml{list-style:none;padding:0;margin:6px 0 0}
.obars li{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,1fr) 44px;gap:8px;align-items:center;padding:5px 0;font-size:14px}
.obars b{text-align:right;font-variant-numeric:tabular-nums}
.olater li{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;padding:6px 0;border-top:1px solid var(--line);font-size:13.5px}
.olater span{color:var(--mut)}
.pml li{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:10px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}
.pml small{display:block;color:var(--mut);font-size:12px;line-height:1.3;margin-top:2px}
.oy{display:flex;flex-direction:column;align-items:flex-end;gap:3px}.oy .pill{font-size:15px;min-width:52px}
/* recession watch */
.rwl{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.rwl li{display:grid;grid-template-columns:14px minmax(0,1fr);gap:8px;align-items:start;padding:10px;border-radius:12px;background:var(--card);font-size:14px;line-height:1.25}
.rwl small{display:block;color:var(--mut);font-size:12px;margin-top:3px}
.rl{width:12px;height:12px;border-radius:50%;border:2px solid var(--n);margin-top:2px}
.rwl li.lit{background:var(--rbg)}.rwl li.lit .rl{background:var(--r);border-color:var(--r)}.rwl li.lit b{color:var(--r)}
.verdict .arr{margin-left:6px}
/* liquidity */
.bcards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-bottom:12px}
.bcard{background:var(--cb);border-radius:14px;padding:10px;border:1px solid color-mix(in srgb,var(--c) 25%,transparent)}
.bh{display:flex;justify-content:space-between;align-items:baseline;gap:6px;flex-wrap:wrap;font-size:14px}.bh b{color:var(--c)}.bh span{font-size:12px;color:var(--mut);font-variant-numeric:tabular-nums}
.bs{display:flex;align-items:center;gap:8px;margin:4px 0 6px;flex-wrap:wrap}
.bn{font-family:'Inter Tight',sans-serif;font-weight:800;font-size:30px;line-height:1}.bn small{font-size:14px;color:var(--mut);font-weight:600}
.dots{display:grid;grid-template-columns:repeat(3,9px);gap:3px}.dots i{width:9px;height:9px;border-radius:50%;border:1.5px solid var(--c)}.dots i.on{background:var(--c)}
.bcard .pill{display:block;font-size:12px;white-space:normal;line-height:1.25;padding:5px 6px;background:color-mix(in srgb,var(--c) 16%,var(--bg))}
.blk{display:block;font-size:11.5px;line-height:1.3;margin-top:6px;color:var(--r);font-weight:600}
.guards{list-style:none;padding:0;margin:0 0 14px}
.guards li{display:grid;grid-template-columns:22px minmax(0,1fr);gap:6px;padding:8px 0;border-top:1px solid var(--line);font-size:14.5px}
.guards small{display:block;color:var(--mut);font-size:12.5px}
.gk{font-weight:800}.guards .ok .gk{color:var(--g)}.guards .bad .gk{color:var(--r)}.guards .bad b{color:var(--r)}
.bmx{width:100%;border-collapse:collapse;font-size:14px;margin:0 0 4px}
.bmx th,.bmx td{padding:7px 4px;border-top:1px solid var(--line);text-align:center}
.bmx thead th{font-size:12px;color:var(--mut);font-weight:600;border-top:0}
.bmx tbody th{text-align:left;font-weight:600;width:46%}.bmx tbody th small{display:block;font-weight:400;font-size:11.5px;color:var(--mut);line-height:1.3}
.bmx td.y{color:var(--g);background:color-mix(in srgb,var(--g) 10%,transparent);font-weight:800}
.bmx td.n{color:var(--r);background:color-mix(in srgb,var(--r) 7%,transparent);font-weight:800}
.lead{background:var(--card);border-radius:14px;padding:12px;margin:0 0 6px}
.leadc{width:100%;height:auto;display:block}
.leadc .fut{fill:var(--ink);opacity:.06}.leadc .zero{stroke:var(--mut);stroke-width:1;opacity:.5}
.leadc .la{fill:var(--ink);opacity:.22}.leadc .bl{fill:none;stroke:var(--dale);stroke-width:2}
.leadc .now{stroke:var(--ink);stroke-width:1;stroke-dasharray:3 3}.leadc .ax{font-size:10px;fill:var(--mut)}
.leg{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;margin:6px 0 0}.leg span{display:flex;align-items:center;gap:6px}
.leg i{width:14px;height:8px;border-radius:2px}.leg .k1{background:var(--ink);opacity:.3}.leg .k2{background:var(--dale);height:3px}
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
