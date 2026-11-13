#!/usr/bin/env python3
"""Cash Yields engine.

Fetches money-fund, deposit, Treasury-bill and bill-ETF yields, ranks them by net yield
after each entity's taxes, works out today's order cutoffs and cash-available dates, and
writes docs/index.html, docs/data.json, history.json and changelog.json.

Needs: Python 3.9+, requests, pdftotext (poppler-utils).  Settings: config/*.json and an
optional .env (alerts).  Never invents a yield: an instrument with no fetched value is
shown as "no data" and left out of the ranking.

Test switches (environment):
  CY_BREAK=merrill,federated   force the named fetches to fail
  CY_NOW=2026-11-13T09:00      pretend the run happens at this Eastern time
"""
import datetime as dt
import html as htmllib
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config")
DOCS = os.path.join(HERE, "docs")
TEMPLATE = os.path.join(HERE, "template.html")
STATE = os.path.join(HERE, "state.json")
HISTORY = os.path.join(HERE, "history.json")
CHANGELOG = os.path.join(HERE, "changelog.json")
CALENDAR = os.path.join(CONFIG, "calendar.json")
MANUAL_MERRILL = os.path.join(HERE, "manual", "ICCRateSheet.pdf")
ET = ZoneInfo("America/New_York")
UA = "cash-yields/1.0 (personal rate dashboard; python-requests)"
BREAK = {s.strip() for s in os.environ.get("CY_BREAK", "").split(",") if s.strip()}

ENTITIES = [("sc_partner", "SC partners"), ("sc_trust", "SC trust"), ("fl_trust", "FL trust"), ("foundation", "Foundation")]
COLUMNS = [("same", "Same day", "Needed any day"), ("next", "Next day, no date", "T+1 liquidity, no maturity to match"),
           ("w13", "Dated inside 13 weeks", "Payment date within 13 weeks"), ("w52", "Dated inside 52 weeks", "Payment date within 52 weeks")]
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
MON3 = {m[:3]: i for m, i in MONTHS.items()}


# ---------------------------------------------------------------- small helpers
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
        f.write("\n")


def load_env():
    try:
        for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def now_et():
    forced = os.environ.get("CY_NOW")
    if forced:
        return dt.datetime.fromisoformat(forced).replace(tzinfo=ET)
    return dt.datetime.now(ET)


def http_get(url, kind):
    if kind in BREAK:
        raise RuntimeError(f"fetch disabled by CY_BREAK={kind}")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r


def pdf_text(content):
    if not content.startswith(b"%PDF"):
        raise RuntimeError("not a PDF")
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(content)
        f.flush()
        return subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "replace")


def strip_tags(raw):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", t)))


def iso(d):
    return d.isoformat() if d else None


def parse_iso(s):
    return dt.date.fromisoformat(s[:10]) if s else None


def md(d):
    """Fri 11/13"""
    return f"{d:%a} {d.month}/{d.day}"


def mdy(d):
    return f"{d.month}/{d.day}/{d.year}"


def parse_clock(s):
    """'2:30pm', '5pm', '01:00 p.m.', '11:00 AM' -> 'HH:MM' (24h) or None."""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m", s or "", flags=re.I)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return f"{h:02d}:{int(m.group(2) or 0):02d}"


def clock(hhmm):
    """'14:30' -> '2:30 p.m. ET'"""
    h, m = map(int, hhmm.split(":"))
    return f"{(h - 1) % 12 + 1}:{m:02d} {'a.m.' if h < 12 else 'p.m.'} ET"


# ---------------------------------------------------------------- calendar (SIFMA)
class Cal:
    def __init__(self, data):
        self.holidays = {parse_iso(h["date"]): h["name"] for h in data.get("holidays", [])}
        self.early = {parse_iso(e["date"]): e for e in data.get("early_closes", [])}

    def is_bd(self, d):
        return d.weekday() < 5 and d not in self.holidays

    def next_bd(self, d):
        d += dt.timedelta(days=1)
        while not self.is_bd(d):
            d += dt.timedelta(days=1)
        return d

    def bd_between(self, a, b):
        """Business days after a, up to and including b."""
        n, d = 0, a
        while d < b:
            d += dt.timedelta(days=1)
            n += self.is_bd(d)
        return n

    def add_bd(self, d, n):
        for _ in range(n):
            d = self.next_bd(d)
        return d


def parse_sifma(raw):
    """U.S. full closes and early closes from the SIFMA holiday-schedule page (HTML + embedded page data)."""
    raw = htmllib.unescape(raw.replace('\\"', '"'))
    toks = []
    for m in re.finditer(r'<h3[^>]*>([^<]{3,60})</h3>|"h3",null,\{"children":"([^"]{3,60})"', raw):
        toks.append((m.start(), "name", (m.group(1) or m.group(2)).strip()))
    early_spans = []
    for m in re.finditer(r"Early Close(?: Only)? \((\d{1,2}):(\d{2}) ([ap])\.m\. Eastern Time\):\s*\w+day, (\w+) (\d{1,2}), (\d{4})", raw):
        if m.group(4) in MONTHS:
            h = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
            toks.append((m.start(), "early", (dt.date(int(m.group(6)), MONTHS[m.group(4)], int(m.group(5))), f"{h:02d}:{m.group(2)}")))
            early_spans.append((m.start(), m.end()))
    for m in re.finditer(r"(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, (\w+) (\d{1,2}), (\d{4})", raw):
        if m.group(1) in MONTHS and not any(a <= m.start() < b for a, b in early_spans) and "Early Close" not in raw[max(0, m.start() - 60):m.start()]:
            toks.append((m.start(), "full", dt.date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))))
    toks.sort(key=lambda t: t[0])
    # The page repeats the schedule for the U.S., U.K. and Japan (and the 2027 lists arrive as scattered page-data
    # chunks), so work entry by entry: keep entries whose holiday name is a U.S. one.  The U.K. and Japan lists show the
    # U.S. holidays on the same dates; their own holidays and "(Substitute)" days are dropped by name.  Early closes are
    # only taken when stated in Eastern Time, and a day with an Eastern early close is not a full close (Good Friday 2026).
    us = re.compile(r"^(New Year|Martin Luther King|Presidents|Good Friday|Memorial Day|Juneteenth|U\.S\. Independence|Independence Day|Labor Day|Columbus|Veterans|Thanksgiving Day|Christmas)")
    fixed = {"New Year": (1, 1), "Juneteenth": (6, 19), "U.S. Independence": (7, 4), "Independence Day": (7, 4), "Veterans": (11, 11), "Christmas": (12, 25)}
    floating = {"Martin Luther King": 1, "Presidents": 2, "Good Friday": (3, 4), "Memorial Day": 5, "Labor Day": 9, "Columbus": 10, "Thanksgiving Day": 11}

    def plausible(n, d):
        """Guards against a date picked up under the wrong heading: fixed holidays within a day of their date, others in their month."""
        for k, (mo, dy) in fixed.items():
            if n.startswith(k):
                return any(abs((d - dt.date(y, mo, dy)).days) <= 1 for y in (d.year, d.year + 1))
        for k, mo in floating.items():
            if n.startswith(k):
                return d.month in (mo if isinstance(mo, tuple) else (mo,))
        return False

    holidays, early, name, name_pos = {}, {}, None, 0
    for pos, kind, val in toks:
        if kind == "name":
            name, name_pos = val, pos
        elif name and us.match(name) and "Substitute" not in name and pos - name_pos < 1500:
            if kind == "full":
                if plausible(name, val) and val.weekday() < 5:
                    holidays[val] = name.strip()
            else:
                early[val[0]] = {"date": iso(val[0]), "time": val[1], "name": name.strip()}
    holidays = {d: n for d, n in holidays.items() if d not in early}
    if not (any(n.startswith("Thanksgiving") for n in holidays.values()) and any(n.startswith("Christmas") for n in holidays.values())):
        raise RuntimeError("SIFMA parse found no Thanksgiving / Christmas")
    if len(holidays) < 8:
        raise RuntimeError(f"SIFMA parse found only {len(holidays)} U.S. holidays")
    return {"holidays": [{"date": iso(d), "name": n} for d, n in sorted(holidays.items())],
            "early_closes": [early[d] for d in sorted(early)]}


def get_calendar(sources, today, feeds):
    cached = load(CALENDAR, None)
    age = (today - parse_iso(cached["fetched"])).days if cached and cached.get("fetched") else None
    if cached and age is not None and 0 <= age < 7 and "sifma" not in BREAK:
        feeds.append({"name": "SIFMA calendar", "ok": True, "asof": cached["fetched"], "note": "cached (refetched weekly)"})
        return cached
    try:
        data = parse_sifma(http_get(sources["sifma_calendar"], "sifma").text)
        data["fetched"] = iso(today)
        data["source"] = sources["sifma_calendar"]
        save(CALENDAR, data)
        feeds.append({"name": "SIFMA calendar", "ok": True, "asof": iso(today), "note": "fetched today"})
        return data
    except Exception as ex:
        print(f"SIFMA calendar fetch failed: {ex}")
        if cached:
            feeds.append({"name": "SIFMA calendar", "ok": False, "asof": cached.get("fetched"), "note": "fetch failed — using cached file"})
            return cached
        feeds.append({"name": "SIFMA calendar", "ok": False, "asof": None, "note": "no calendar — weekends only; holidays not known"})
        return {"holidays": [], "early_closes": [], "fetched": None}


# ---------------------------------------------------------------- Merrill sheet
FUND_RE = re.compile(r"^(.*?)\s{2,}([A-Z]{5})\s+(?:\d\.\d{2}%\s+)?(\d\.\d{2})%\s+(Brokerage, IAP|Brokerage|IAP)", re.M)   # tax-exempt rows carry a taxable-equivalent yield first
SWEEP_RE = re.compile(r"^(.*?)\s{2,}([A-Z]{5})\s+(\d\.\d{2})%\s*$", re.M)                                                    # sweep-only funds have no eligibility column


def _fund_name(s):
    return re.sub(r"\s+", " ", re.sub(r"\s*(---|–)\s*", " – ", s)).strip().rstrip("*").strip()
DATE_RE = r"(\d{1,2})/(\d{1,2})/(\d{4})"


def _date_after(text, header):
    m = re.search(header + r"[^\d]{0,120}" + DATE_RE, text, flags=re.S)
    return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def parse_merrill(text):
    funds = {m.group(2): {"y": float(m.group(3)), "name": _fund_name(m.group(1)), "eligibility": m.group(4)} for m in FUND_RE.finditer(text)}
    for m in SWEEP_RE.finditer(text):
        funds.setdefault(m.group(2), {"y": float(m.group(3)), "name": _fund_name(m.group(1)), "eligibility": "Sweep only"})
    if len(funds) < 5:
        raise RuntimeError(f"Merrill sheet parse found only {len(funds)} fund rows")
    fund_asof = _date_after(text, "Seven-Day Yield") or _date_after(text, "Annual Percentage Yield as of")
    dep_asof = _date_after(text, "Annual Percentage Yield as of") or fund_asof
    if not fund_asof:
        raise RuntimeError("Merrill sheet as-of date not found")
    deps = {}
    i = text.find("$100,000,000 and above")
    if i >= 0:
        win = text[max(0, i - 1500):i + 1500]
        m = re.search(r"\$100,000,000 and above\s+(\d\.\d{2})%", win)
        if m:
            deps["PD-100M"] = float(m.group(1))
        low = None
        for m in re.finditer(r"\$([\d,]+) - \$[\d,]+\s+(\d\.\d{2})%", win):
            lo, y = int(m.group(1).replace(",", "")), float(m.group(2))
            if lo >= 10_000_000:
                deps["PD-10M"] = y
            elif lo >= 5_000_000:
                deps["PD-5M"] = y
            elif lo >= 2_500_000:
                deps["PD-2.5M"] = y
            elif low is None or lo > low[0]:
                low = (lo, y)
        if low:
            deps["PD-<2.5M"] = low[1]
    m = re.search(r"Insured Savings Account[^\n%]*?(\d\.\d{2})%", text)
    if m:
        deps["ISA"] = float(m.group(1))
    for m in re.finditer(r"Merrill Lynch Bank Deposit Program\W+Tier\s*(\d)[^\n%]{0,80}?(\d\.\d{2})%", text):
        key = {"1": "MLBDP-1", "4": "MLBDP-4", "5": "MLBDP-5"}.get(m.group(1))
        if key:
            deps.setdefault(key, float(m.group(2)))
    return {"asof": iso(fund_asof), "deposit_asof": iso(dep_asof), "funds": funds, "deposits": deps}


def get_merrill(sources, state, today, cal, feeds, banners):
    last = state.get("merrill")
    got, how, err = None, None, None
    try:
        got, how = parse_merrill(pdf_text(http_get(sources["merrill_pdf"], "merrill").content)), "fetched today"
    except Exception as ex:
        err = str(ex)
        print(f"Merrill sheet fetch failed: {ex}")
        if os.path.exists(MANUAL_MERRILL) and "merrill" not in BREAK:
            try:
                got = parse_merrill(pdf_text(open(MANUAL_MERRILL, "rb").read()))
                how = "read from manual/ICCRateSheet.pdf (download failed)"
            except Exception as ex2:
                print(f"manual Merrill sheet unreadable: {ex2}")
    if got and (not last or got["asof"] >= last.get("asof", "")):
        got["fetched"] = iso(today)
        state["merrill"] = last = got
    if not last:
        banners.append({"level": "amber", "text": "Merrill sheet could not be fetched and no earlier copy is stored — Merrill-only rows show “no data” and are not ranked. Nothing is estimated."})
        feeds.append({"name": "Merrill Yields at a Glance", "ok": False, "asof": None, "note": f"fetch failed ({err})"})
        return None
    asof = parse_iso(last["asof"])
    age = cal.bd_between(asof, today)
    if not got:
        banners.append({"level": "amber", "text": f"Merrill sheet is stale — today's download failed; ranking on the last good values, as of {mdy(asof)}."})
    elif age > 10:
        banners.append({"level": "amber", "text": f"Merrill sheet is stale — its as-of date is {mdy(asof)} ({age} business days old); ranking on those values."})
    feeds.append({"name": "Merrill Yields at a Glance", "ok": bool(got), "asof": last["asof"], "note": how or f"fetch failed — last good copy from {last.get('fetched')}"})
    return last


# ---------------------------------------------------------------- Treasury
def get_bills(sources, state, today, feeds, banners):
    months = [today.replace(day=1)]
    months.append((months[0] - dt.timedelta(days=1)).replace(day=1))
    if today.day <= 2:
        months.reverse()
    got = None
    for mth in months:
        try:
            xml = http_get(sources["treasury_bills_xml"].format(yyyymm=f"{mth:%Y%m}"), "treasury").text
            entries = xml.split("<entry>")[1:]
            if not entries:
                continue
            f = dict(re.findall(r"<d:(\w+)[^>]*>([^<]*)<", entries[-1]))
            ylds = {}
            for w in (4, 6, 8, 13, 17, 26, 52):
                v = f.get(f"ROUND_B1_YIELD_{w}WK_2")
                if v:
                    ylds[f"BILL-{w}W"] = {"y": float(v), "maturity": (f.get(f"MATURITY_DATE_{w}WK") or "")[:10] or None, "cusip": f.get(f"CUSIP_{w}WK") or None}
            cand = {"asof": f["INDEX_DATE"][:10], "yields": ylds}
            if not got or cand["asof"] > got["asof"]:
                got = cand
            if got and mth == months[0] and today.day > 2:
                break
        except Exception as ex:
            print(f"Treasury feed failed ({mth:%Y-%m}): {ex}")
    last = state.get("treasury")
    if got and (not last or got["asof"] >= last["asof"]):
        state["treasury"] = last = got
    if not got:
        banners.append({"level": "amber", "text": "Treasury bill feed failed today" + (f" — bills ranked on the last good values, as of {mdy(parse_iso(last['asof']))}." if last else " — bills show “no data”.")})
    feeds.append({"name": "Treasury bill rates", "ok": bool(got), "asof": last["asof"] if last else None, "note": "fetched today" if got else "fetch failed"})
    return last


def get_auctions(sources, state, now, feeds):
    try:
        recs = http_get(sources["auctions_json"], "auctions").json()
        out = {}
        for r in recs:
            if r.get("securityType") != "Bill" or r.get("cashManagementBill") == "Yes":
                continue
            m = re.match(r"(\d+)-Week", r.get("securityTerm", ""))
            nc_date = (r.get("noncompetitiveAuctionCloseDate") or r.get("auctionDate") or "")[:10] or None
            if not m or not r.get("issueDate"):
                continue
            out.setdefault(f"BILL-{m.group(1)}W", []).append({
                "close_date": nc_date, "nc_time": parse_clock(r.get("noncompetitiveAuctionCloseTime")) or "11:00",
                "c_time": parse_clock(r.get("competitiveAuctionCloseTime")) or "11:30",
                "issue": r["issueDate"][:10], "maturity": (r.get("maturityDate") or "")[:10] or None,
                "cusip": r.get("cusip"), "announced": (r.get("announcementDate") or "")[:10] or None})
        for k in out:
            out[k].sort(key=lambda a: (a["issue"], a["nc_time"]))
        state["auctions"] = {"fetched": iso(now.date()), "by_term": out}
        feeds.append({"name": "TreasuryDirect auction calendar", "ok": True, "asof": iso(now.date()), "note": "fetched today"})
    except Exception as ex:
        print(f"auction calendar failed: {ex}")
        feeds.append({"name": "TreasuryDirect auction calendar", "ok": False, "asof": (state.get("auctions") or {}).get("fetched"), "note": "fetch failed — showing last stored auctions"})
    stamp = now.strftime("%Y-%m-%d %H:%M")
    return {k: [a for a in v if (f"{a['close_date']} {a['nc_time']}" > stamp if a["close_date"] else a["issue"] > stamp[:10])] for k, v in (state.get("auctions") or {}).get("by_term", {}).items()}


# ---------------------------------------------------------------- sponsor yields
def get_sponsor_yields(instruments, sources, state, today, feeds):
    store = state.setdefault("sponsor", {})
    ok = {"BlackRock daily yields": None, "Federated Hermes daily yields": None, "ETF sponsor pages": None}

    def put(sym, y, asof, source, **extra):
        if y is None or not asof:
            return
        if sym not in store or asof >= store[sym]["asof"]:
            store[sym] = dict({"y": round(float(y), 2), "asof": asof, "source": source, "fetched": iso(today)}, **extra)

    br = [i for i in instruments if (i.get("sponsor") or {}).get("kind") == "blackrock"]
    if br:
        try:
            rows = http_get(sources["blackrock_screener"], "blackrock").json()
            by = {v.get("localExchangeTicker"): v for v in rows.values() if isinstance(v, dict)}
            for i in br:
                v = by.get(i["symbol"])
                if v and v.get("oneWeekSecYield") and v.get("oneWeekSecYieldAsOf"):
                    d = str(v["oneWeekSecYieldAsOf"]["r"])
                    put(i["symbol"], v["oneWeekSecYield"]["r"], f"{d[:4]}-{d[4:6]}-{d[6:]}", "BlackRock (7-day SEC yield)")
            ok["BlackRock daily yields"] = True
        except Exception as ex:
            print(f"BlackRock yields failed: {ex}")
            ok["BlackRock daily yields"] = False
    for i in instruments:
        sp = i.get("sponsor") or {}
        try:
            if sp.get("kind") == "federated":
                ok["Federated Hermes daily yields"] = ok["Federated Hermes daily yields"] or False
                d = http_get(sources["federated_api"].format(id=sp["product_id"]), "federated").json()["data"]
                s = str(d["sevenDayNetYieldDate"])
                put(i["symbol"], d["sevenDayNetYield"], f"{s[:4]}-{s[4:6]}-{s[6:]}", "Federated Hermes (7-day net yield)")
                ok["Federated Hermes daily yields"] = True
            elif sp.get("kind") == "ishares":
                ok["ETF sponsor pages"] = ok["ETF sponsor pages"] or False
                t = strip_tags(http_get(sp["url"], "ishares").text)
                m = re.search(r"30 Day SEC Yield as of (\w{3}) (\d{1,2}), (\d{4}) (\d+\.\d+)%", t)
                y = re.search(r"Average Yield to Maturity (\d+\.\d+)%", t)
                if m:
                    put(i["symbol"], m.group(4), iso(dt.date(int(m.group(3)), MON3[m.group(1)], int(m.group(2)))), "iShares (30-day SEC yield)", ytm=float(y.group(1)) if y else None)
                    ok["ETF sponsor pages"] = True
            elif sp.get("kind") == "ssga":
                ok["ETF sponsor pages"] = ok["ETF sponsor pages"] or False
                raw = http_get(sp["url"], "ssga").text
                m = re.search(r'30 Day SEC Yield\s*<.*?</th>\s*<td class="data">([\d.]+)%', raw, flags=re.S)
                y = re.search(r'Yield to Maturity\s*</th>\s*<td class="data">([\d.]+)%', raw)
                a = re.search(r"Yields as of (\w{3}) (\d{1,2}) (\d{4})", strip_tags(raw))
                if m and a:
                    put(i["symbol"], m.group(1), iso(dt.date(int(a.group(3)), MON3[a.group(1)], int(a.group(2)))), "State Street (30-day SEC yield)", ytm=float(y.group(1)) if y else None)
                    ok["ETF sponsor pages"] = True
        except Exception as ex:
            print(f"sponsor yield failed for {i['symbol']}: {ex}")
    for name, v in ok.items():
        if v is not None:
            feeds.append({"name": name, "ok": v, "asof": iso(today) if v else None, "note": "fetched today" if v else "fetch failed — last good values kept"})
    return store


# ---------------------------------------------------------------- cutoffs
def get_cutoffs(instruments, state, today, feeds, log_items):
    store = state.setdefault("cutoffs", {})
    pages, out = {}, {}
    status = {}
    for i in instruments:
        feed = i.get("cutoff_feed")
        sym = i["symbol"]
        base = {"time": i.get("cutoff_time"), "buy": None, "sell": None, "early_time": i.get("early_close_time"),
                "source": i.get("cutoff_verified") or ("config (not verified)" if i.get("cutoff_time") else None), "refreshed_today": None}
        if not feed:
            out[sym] = base
            continue
        label = "BlackRock fact sheets (cutoffs)" if feed["kind"] == "blackrock_factsheet" else "Federated Hermes pages (cutoffs)"
        try:
            url = feed["url"]
            if url not in pages:
                r = http_get(url, "blackrock" if feed["kind"] == "blackrock_factsheet" else "federated")
                pages[url] = pdf_text(r.content) if feed["kind"] == "blackrock_factsheet" else r.text
            txt = pages[url]
            if feed["kind"] == "blackrock_factsheet":
                m = re.search(r"Trading deadline\s+(\S+)\s*\(ET\)", txt)
                p = re.search(r"Purchase deadline\s+(\S+)\s*\(ET\)", txt)
                s = re.search(r"Redemption deadline\s+(\S+)\s*\(ET\)", txt)
                buy = parse_clock(m.group(1)) if m else (parse_clock(p.group(1)) if p else None)
                sell = parse_clock(m.group(1)) if m else (parse_clock(s.group(1)) if s else None)
                a = re.search(r"information provided is as of (\w+) \d{1,2}, (\d{4})", txt)
                src = f"BlackRock fact sheet {a.group(1)[:3]} {a.group(2)}" if a else "BlackRock fact sheet"
                t1 = None
            else:
                pairs = re.findall(r'"cmsStatisticValue":\["Purchases: ([^"]+)","Redemptions: ([^"]+)"\]', txt)
                buy, sell = (parse_clock(pairs[0][0]), parse_clock(pairs[0][1])) if pairs else (None, None)
                t1 = parse_clock(pairs[1][0]) if len(pairs) > 1 else None
                src = f"Federated page {mdy(today)}"
            if not (buy and sell):
                raise RuntimeError("cutoff not found in the sponsor document")
            new = {"time": min(buy, sell), "buy": buy, "sell": sell, "t1_time": t1, "source": src, "verified": iso(today)}
            prev_time = (store.get(sym) or {}).get("time") or i.get("cutoff_time")
            if prev_time and prev_time != new["time"]:
                log_items.append({"kind": "cutoff", "label": sym, "text": f"{sym} same-day cutoff changed: {clock(prev_time)} → {clock(new['time'])} ({src})"})
            store[sym] = new
            out[sym] = dict(base, **new, refreshed_today=True)
            status[label] = status.get(label, True)
        except Exception as ex:
            print(f"cutoff fetch failed for {sym}: {ex}")
            status[label] = False
            old = store.get(sym)
            if old:
                out[sym] = dict(base, **old, refreshed_today=False)
                out[sym]["source"] = f"{old['source']} — not refreshed today"
            else:
                out[sym] = dict(base, refreshed_today=False, source="config value — not refreshed today")
    for label, v in status.items():
        feeds.append({"name": label, "ok": v, "asof": iso(today) if v else None, "note": "fetched today" if v else "fetch failed — stored / config cutoffs used, marked “not refreshed today”"})
    return out


# ---------------------------------------------------------------- tax and ranking
def net_factor(entity, inst, tax):
    f, s = inst["fed_taxable_share"], inst["sc_taxable_share"]
    if entity == "sc_partner":
        return 1 - f * (tax["fed_rate_partner"] + tax["niit_partner"]) - s * tax["sc_rate"]
    if entity == "sc_trust":
        return 1 - f * (tax["fed_rate_trust"] + tax["niit_trust"]) - s * tax["sc_rate"]
    if entity == "fl_trust":
        return 1 - f * (tax["fed_rate_trust"] + tax["niit_trust"])
    return 1 - tax["foundation_excise"] * f


def in_column(col, r):
    c = r["category"]
    if col == "same":
        return c in ("fund", "deposit") and r["same_day"]
    if c in ("fund", "deposit", "etf"):
        return True
    return c == "bill" and r["term_weeks"] <= (13 if col == "w13" else 52) and col != "next"


def caveat(i):
    t = i["type"]
    if t.startswith("Prime"):
        return "Prime fund: floating NAV, liquidity fees possible — above the recommended risk tier."
    if t.startswith("Municipal"):
        return "Municipal fund: floating NAV, fees possible, check AMT paper; only wins when the muni test is amber."
    if i["symbol"].startswith("PD-"):
        return "Rate applies only to an account whose own balance is in this tier; FDIC insurance to $250,000."
    return None


# ---------------------------------------------------------------- timing (for the run date; the page recomputes live)
def timing(r, now, cal):
    today = now.date()
    open_today = cal.is_bd(today)
    nbd = cal.next_bd(today)
    trade = today if open_today else nbd
    day = "today" if open_today else md(trade)
    early = cal.early.get(trade)
    cat, cut = r["category"], r["cutoff"]
    if cat == "bill":
        a = (r.get("auctions") or [None])[0]
        if a and not a["close_date"]:
            ann = f"to be announced {md(parse_iso(a['announced']))}" if a.get("announced") else "not yet announced"
            return {"cutoff": f"next auction {ann}; secondary: during trading hours",
                    "cash": f"next auction settles {md(parse_iso(a['issue']))}; secondary buy or sell settles T+1 = {md(cal.next_bd(trade))}"}
        if a:
            return {"cutoff": f"next auction closes {md(parse_iso(a['close_date']))} {clock(a['nc_time'])} (noncompetitive); secondary: during trading hours",
                    "cash": f"auction settles {md(parse_iso(a['issue']))}; secondary buy or sell settles T+1 = {md(cal.next_bd(trade))}"}
        return {"cutoff": "no auction announced; secondary: during trading hours", "cash": f"secondary buy or sell settles T+1 = {md(cal.next_bd(trade))}"}
    if cat == "etf":
        return {"cutoff": "market hours 9:30 a.m.–4:00 p.m. ET" + (" (early close — confirm)" if early else ""), "cash": f"T+1 = {md(cal.next_bd(trade))}"}
    if cat == "deposit":
        if r["same_day"]:
            return {"cutoff": "automatic sweep", "cash": "same day (sweep)"}
        return {"cutoff": "by instruction", "cash": f"posts {md(cal.next_bd(trade))}"}
    if "sweep" in r["cutoff_buy"]:
        return {"cutoff": "automatic sweep", "cash": "same day (sweep)"}
    t = cut.get("time")
    if not t:
        return {"cutoff": "not verified — ask Merrill", "cash": f"same day only if Merrill confirms the cutoff; otherwise {md(cal.next_bd(trade))}"}
    flag = ""
    if early:
        if cut.get("early_time"):
            t, flag = min(t, cut["early_time"]), " (early close)"
        else:
            flag = " (early close — confirm)"
    c = f"{clock(t)} {day}{flag}"
    after = md(cal.next_bd(trade))
    if r["same_day"]:
        return {"cutoff": c, "cash": f"{day} if ordered by {clock(t)}; otherwise {after}"}
    return {"cutoff": c, "cash": f"same day only if ordered by {clock(t)}; otherwise {after}"}


# ---------------------------------------------------------------- alerts (same as tripwires)
def send_email(subject, body):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com"); port = int(os.environ.get("SMTP_PORT", "465"))
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS"); to = os.environ.get("ALERT_EMAIL", user)
    if not (user and pw and to):
        return False
    msg = MIMEText(body); msg["Subject"], msg["From"], msg["To"] = subject, user, to
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context()) as s:
        s.login(user, pw); s.sendmail(user, [to], msg.as_string())
    return True


def send_pushover(title, body):
    token, user = os.environ.get("PUSHOVER_TOKEN"), os.environ.get("PUSHOVER_USER")
    if not (token and user):
        return False
    data = urllib.parse.urlencode({"token": token, "user": user, "title": title, "message": body[:1024]}).encode()
    urllib.request.urlopen(urllib.request.Request("https://api.pushover.net/1/messages.json", data=data), timeout=30)
    return True


def alert(subject, body):
    sent = []
    for fn, name in ((send_email, "email"), (send_pushover, "pushover")):
        try:
            if fn(subject, body):
                sent.append(name)
        except Exception as ex:
            print(f"{name} failed: {ex}")
    return sent


# ---------------------------------------------------------------- run
def run():
    load_env()
    now = now_et()
    today = now.date()
    cfg = load(os.path.join(CONFIG, "instruments.json"), None)
    tax = load(os.path.join(CONFIG, "tax.json"), None)
    sources = load(os.path.join(CONFIG, "sources.json"), None)
    if not (cfg and tax and sources):
        sys.exit("config/instruments.json, config/tax.json and config/sources.json are required")
    instruments = cfg["instruments"]
    state = load(STATE, {})
    feeds, banners, log_items = [], [], []

    cal_data = get_calendar(sources, today, feeds)
    cal = Cal(cal_data)
    merrill = get_merrill(sources, state, today, cal, feeds, banners)
    bills = get_bills(sources, state, today, feeds, banners)
    auctions = get_auctions(sources, state, now, feeds)
    sponsor = get_sponsor_yields(instruments, sources, state, today, feeds)
    cutoffs = get_cutoffs(instruments, state, today, feeds, log_items)

    rows = []
    for i in instruments:
        sym, cat = i["symbol"], i["category"]
        r = {k: i.get(k) for k in ("symbol", "name", "category", "type", "availability", "risk_tier", "fed_taxable_share", "sc_taxable_share",
                                   "same_day", "cutoff_buy", "cutoff_sell", "nav_strike", "settlement_note", "term_weeks", "expense_ratio")}
        ys = {}
        if merrill and cat == "fund" and sym in merrill["funds"]:
            m = merrill["funds"][sym]
            ys["merrill"] = {"y": m["y"], "asof": merrill["asof"], "source": "Merrill Yields at a Glance (7-day yield)"}
            r["name"] = r["name"] or m["name"]
            r["merrill_eligibility"] = m["eligibility"]
        if merrill and cat == "deposit" and sym in merrill["deposits"]:
            ys["merrill"] = {"y": merrill["deposits"][sym], "asof": merrill.get("deposit_asof") or merrill["asof"], "source": "Merrill Yields at a Glance (APY)"}
        if sym in sponsor:
            ys["sponsor"] = {k: sponsor[sym][k] for k in ("y", "asof", "source")}
            if sponsor[sym].get("ytm") is not None:
                r["ytm"] = sponsor[sym]["ytm"]
        if cat == "bill" and bills and sym in bills["yields"]:
            b = bills["yields"][sym]
            ys["treasury"] = {"y": b["y"], "asof": bills["asof"], "source": "Treasury daily bill rates (coupon-equivalent)"}
            r["bill_maturity"], r["bill_cusip"] = b.get("maturity"), b.get("cusip")
        if cat == "bill":
            r["auctions"] = auctions.get(sym, [])
        best = max(ys.items(), key=lambda kv: (kv[1]["asof"], kv[0] == "merrill"), default=None)   # newest wins; Merrill on a tie
        r["yields"] = ys
        r["gross"], r["gross_asof"], r["gross_source"] = (best[1]["y"], best[1]["asof"], best[1]["source"]) if best else (None, None, None)
        r["stale"] = bool(best) and cal.bd_between(parse_iso(best[1]["asof"]), today) > 10
        r["name"] = r["name"] or f"{sym} ({i['type']})"
        r["cutoff"] = cutoffs.get(sym, {})
        r["caveat"] = caveat(i)
        if i["availability"] == "TMA sweep only":
            r["status"], r["status_note"] = "excluded", "TMA sweep only — bank-trustee / retirement accounts; never ranked"
        elif i["availability"] == "IAP only" and not tax.get("iap_accounts"):
            r["status"], r["status_note"] = "excluded", "IAP only — advisory-program accounts (iap_accounts is off)"
        elif r["gross"] is None:
            r["status"], r["status_note"] = "no_data", "no data — no yield fetched; not ranked"
        else:
            r["status"], r["status_note"] = "ranked", None
        r["net"] = {e: (round(r["gross"] * net_factor(e, i, tax), 4) if r["gross"] is not None else None) for e, _ in ENTITIES}
        r["timing"] = timing(r, now, cal)
        rows.append(r)
    by = {r["symbol"]: r for r in rows}

    ranking, picks = {}, {}
    for e, _ in ENTITIES:
        ranking[e], picks[e] = {}, {}
        for col, _, _ in COLUMNS:
            members = [r for r in rows if in_column(col, r)]
            ranked = sorted((r for r in members if r["status"] == "ranked"), key=lambda r: (-r["net"][e], r["risk_tier"], r["symbol"]))
            rec = next((r for r in ranked if r["risk_tier"] <= tax["max_risk_tier_recommended"]), None)
            ranking[e][col] = {"ranked": [r["symbol"] for r in ranked], "unranked": [r["symbol"] for r in members if r["status"] != "ranked"],
                               "recommended": rec["symbol"] if rec else None}
            picks[e][col] = {"symbol": rec["symbol"], "net": rec["net"][e]} if rec else None

    # checks
    g = lambda s: by[s]["gross"] if s in by else None
    fed = tax["fed_rate_trust"] + tax["niit_trust"]
    checks = {"muni": None, "repo": None, "bill_premium": None}
    if g("FFTXX") and g("TTTXX"):
        ratio = g("FFTXX") / g("TTTXX")
        be_sc, be_fl = (1 - fed) / (1 - tax["sc_rate"]), 1 - fed
        checks["muni"] = {"ratio": round(ratio, 4), "be_sc": round(be_sc, 4), "be_fl": round(be_fl, 4), "fftxx": g("FFTXX"), "tttxx": g("TTTXX"),
                          "sc": "amber" if ratio > be_sc else "green", "fl": "amber" if ratio > be_fl else "green"}
    if g("TSTXX") and g("TTTXX"):
        bp = round((g("TSTXX") - g("TTTXX")) * 100)
        checks["repo"] = {"bp": bp, "status": "green" if bp <= 10 else "amber" if bp <= 25 else "red"}
    if g("BILL-13W") and g("TTTXX"):
        checks["bill_premium"] = {"bp": round((g("BILL-13W") - g("TTTXX")) * 100)}

    # change log
    ename, cname = dict(ENTITIES), {c: n for c, n, _ in COLUMNS}
    prev = state.get("picks")
    alerts = []
    if prev:
        for e, _ in ENTITIES:
            for col, _, _ in COLUMNS:
                o, n = (prev.get(e) or {}).get(col), picks[e][col]
                if (o or {}).get("symbol") != (n or {}).get("symbol"):
                    f = lambda p: f"{p['symbol']} ({p['net']:.2f}% net)" if p else "none"
                    log_items.append({"kind": "pick", "label": ename[e], "entity": e, "column": col, "old": (o or {}).get("symbol"), "new": (n or {}).get("symbol"),
                                      "old_net": (o or {}).get("net"), "new_net": (n or {}).get("net"), "text": f"{ename[e]} · {cname[col]}: {f(o)} → {f(n)}"})
                    alerts.append(log_items[-1]["text"])
    pm = state.get("muni")
    if checks["muni"] and pm:
        for k, lab in (("sc", "SC"), ("fl", "FL")):
            if pm.get(k) and pm[k] != checks["muni"][k]:
                txt = f"Muni test ({lab}) flipped: {'FFTXX wins for sitting cash' if checks['muni'][k] == 'amber' else 'Treasury-only wins'} (ratio {checks['muni']['ratio']:.1%})"
                log_items.append({"kind": "muni", "label": "Muni test", "text": txt})
                alerts.append(txt)
    horizon = cal.add_bd(today, 10)
    seen = set(state.get("early_seen", []))
    upcoming = [{"date": iso(d), "kind": "holiday", "name": n} for d, n in cal.holidays.items() if today <= d <= horizon]
    upcoming += [{"date": e["date"], "kind": "early", "name": e["name"], "time": e["time"]} for d, e in cal.early.items() if today <= d <= horizon]
    upcoming.sort(key=lambda u: u["date"])
    for u in upcoming:
        if u["kind"] == "early" and u["date"] not in seen:
            log_items.append({"kind": "early_close", "label": "Calendar", "text": f"SIFMA early close ({clock(u['time'])}) on {md(parse_iso(u['date']))} is now inside the next 10 business days ({u['name']})"})
            seen.add(u["date"])
    state["early_seen"] = sorted(d for d in seen if d >= iso(today))
    state["picks"] = picks
    if checks["muni"]:
        state["muni"] = {"sc": checks["muni"]["sc"], "fl": checks["muni"]["fl"]}

    changelog = load(CHANGELOG, [])
    changelog = [c for c in changelog if c.get("date") != iso(today)]
    if log_items:
        changelog.insert(0, {"date": iso(today), "summary": f"{len(log_items)} change{'s' if len(log_items) != 1 else ''} against the previous run", "items": log_items})
    cutoff_date = iso(today - dt.timedelta(days=365))
    changelog = [c for c in changelog if c["date"] >= cutoff_date]
    save(CHANGELOG, changelog)

    history = [h for h in load(HISTORY, []) if h.get("date") != iso(today)]
    history.append({"date": iso(today), "gross": {r["symbol"]: r["gross"] for r in rows if r["gross"] is not None},
                    "picks": {e: {c: (p or {}).get("symbol") for c, p in cols.items()} for e, cols in picks.items()}})
    save(HISTORY, history[-520:])
    save(STATE, state)

    data = {
        "updated": now.strftime("%a %-m/%-d/%Y %-I:%M %p ET"), "run_date": iso(today), "first_run": prev is None,
        "entities": [{"key": k, "name": n} for k, n in ENTITIES], "columns": [{"key": k, "name": n, "sub": s} for k, n, s in COLUMNS],
        "tax": tax, "rows": rows, "ranking": ranking, "watch": cfg.get("watch", []), "checks": checks, "banners": banners, "feeds": feeds,
        "merrill_asof": merrill["asof"] if merrill else None, "treasury_asof": bills["asof"] if bills else None,
        "calendar": {"holidays": [h for h in cal_data["holidays"] if h["date"] >= iso(today - dt.timedelta(days=7))],
                     "early_closes": [e for e in cal_data["early_closes"] if e["date"] >= iso(today - dt.timedelta(days=7))], "upcoming": upcoming,
                     "fetched": cal_data.get("fetched")},
        "since_last_run": log_items, "changelog": changelog[:120],
    }
    os.makedirs(DOCS, exist_ok=True)
    save(os.path.join(DOCS, "data.json"), data)
    tpl = open(TEMPLATE, encoding="utf-8").read()
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(tpl.replace("/*CASH_DATA*/{}", payload))

    if alerts:
        site = os.environ.get("SITE_URL", "")
        sent = alert("Cash yields: " + (alerts[0] if len(alerts) == 1 else f"{len(alerts)} changes"), "\n".join(alerts) + (f"\n\n{site}" if site else ""))
        print("alerts sent:", ", ".join(sent) or "none configured")
    for e, n in ENTITIES:
        print(n.ljust(12), " | ".join(f"{cname[c]}: {p['symbol']} {p['net']:.2f}%" if p else f"{cname[c]}: none" for c, p in picks[e].items()))
    for f in feeds:
        print(("ok   " if f["ok"] else "FAIL ") + f["name"], "-", f["note"], f"(as of {f['asof']})" if f["asof"] else "")
    for b in banners:
        print("BANNER:", b["text"])


if __name__ == "__main__":
    run()
