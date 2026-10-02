"""Market data (quotes and price history) from Yahoo Finance's public chart API — no key, no browser.

Covers stocks (AAPL, 0700.HK, 9988.HK, 600519.SS, 7203.T), indices (^GSPC, ^IXIC, ^DJI, ^HSI, ^N225, ^STI),
FX (HKD=X = USD/HKD, CNY=X = USD/CNY, SGD=X, JPY=X, EURUSD=X), commodities (GC=F gold, CL=F oil) and crypto
(BTC-USD, ETH-USD). Names ("tencent", "nvidia") are looked up with Yahoo's search."""
from __future__ import annotations

import datetime as dt
import os
import re

import httpx

UA = {"User-Agent": "Mozilla/5.0"}   # a full browser UA string gets 429 from this API; the short one is served
HOSTS = tuple(h for h in os.environ.get("MARKET_DATA_HOSTS", "").split(",") if h) or (
    "https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
RANGES = {"5d": "1d", "1mo": "1d", "3mo": "1d", "6mo": "1wk", "ytd": "1wk", "1y": "1wk", "2y": "1mo", "3y": "1mo",
          "5y": "1mo", "10y": "3mo", "max": "3mo"}
ALIASES = {"1m": "1mo", "3m": "3mo", "6m": "6mo", "12m": "1y", "1yr": "1y", "year": "1y", "2yr": "2y", "3yr": "3y",
           "5yr": "5y", "10yr": "10y", "1w": "5d", "week": "5d", "month": "1mo"}
# common names the model may pass instead of tickers
NAMES = {"腾讯": "0700.HK", "汇丰": "0005.HK", "渣打": "2888.HK", "小米": "1810.HK", "比亚迪": "1211.HK", "阿里巴巴": "9988.HK",
         "美团": "3690.HK", "宁德时代": "300750.SZ", "茅台": "600519.SS", "贵州茅台": "600519.SS", "恒生指数": "^HSI", "恒指": "^HSI",
         "标普500": "^GSPC", "标普": "^GSPC", "纳指": "^IXIC", "纳斯达克": "^IXIC", "道指": "^DJI", "日经": "^N225", "黄金": "GC=F",
         "比特币": "BTC-USD", "英伟达": "NVDA", "苹果": "AAPL", "微软": "MSFT", "特斯拉": "TSLA", "谷歌": "GOOGL", "亚马逊": "AMZN",
         "港币": "HKD=X", "港元": "HKD=X", "美元兑港币": "HKD=X", "美元兑港元": "HKD=X", "USD/HKD": "HKD=X", "人民币": "CNY=X",
         "美元兑人民币": "CNY=X", "USD/CNY": "CNY=X", "新元": "SGD=X", "新加坡元": "SGD=X", "美元兑新元": "SGD=X", "USD/SGD": "SGD=X",
         "日元": "JPY=X", "美元兑日元": "JPY=X", "USD/JPY": "JPY=X", "欧元": "EURUSD=X", "EUR/USD": "EURUSD=X", "原油": "CL=F",
         "海天国际": "1882.HK", "中国移动": "0941.HK", "建设银行": "0939.HK", "工商银行": "1398.HK", "友邦": "1299.HK", "港交所": "0388.HK",
         "京东": "9618.HK", "百度": "9888.HK", "网易": "9999.HK", "快手": "1024.HK", "理想汽车": "2015.HK", "蔚来": "NIO", "小鹏": "9868.HK",
         "台积电": "TSM", "Meta": "META", "奈飞": "NFLX", "伯克希尔": "BRK-B", "星展": "D05.SI", "新加坡交易所": "S68.SI", "海峡时报指数": "^STI"}


class MarketError(Exception):
    pass


async def _get_json(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    """GET from Yahoo's API, trying the second host when one is rate-limited or answers with a non-JSON page."""
    last = None
    for host in HOSTS:
        r = await client.get(host + path, params=params, headers=UA, timeout=20)
        if r.status_code == 404:
            return {"_status": 404}
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            return r.json()
        last = r.status_code
    raise MarketError(f"数据源暂时不可用 data source busy (HTTP {last}) — try again shortly or use a finance website")


def _norm_range(r: str) -> str:
    r = str(r or "1y").strip().lower()
    r = ALIASES.get(r, r)
    if r not in RANGES:
        raise MarketError(f"range 应为 {', '.join(RANGES)} 之一 (range must be one of {', '.join(RANGES)})")
    return r


async def resolve(client: httpx.AsyncClient, sym: str) -> str:
    s = str(sym or "").strip()
    if not s:
        raise MarketError("空的代码 empty symbol")
    if s in NAMES:
        return NAMES[s]
    if re.fullmatch(r"[\^A-Za-z0-9.\-=]{1,15}", s) and not re.fullmatch(r"[A-Za-z]{6,}", s):
        if re.fullmatch(r"\d{1,4}", s):   # "700" / "0005" -> Hong Kong
            return s.zfill(4) + ".HK"
        return s.upper() if not s.startswith("^") else s
    d = await _get_json(client, "/v1/finance/search", {"q": s, "quotesCount": 5, "newsCount": 0})
    quotes = [q for q in (d.get("quotes") or []) if q.get("symbol")]
    if not quotes:
        raise MarketError(f"找不到「{s}」的代码 no ticker found for {s!r} — give a ticker like AAPL, 0700.HK, ^HSI")
    return quotes[0]["symbol"]


def _r(v) -> float:
    """Round prices for display: 4 decimals below 20 (FX, small caps), else 2."""
    v = float(v)
    return round(v, 4) if abs(v) < 20 else round(v, 2)


def _day(ts: int, gmtoff: int = 0) -> str:
    return dt.datetime.fromtimestamp(ts + gmtoff, tz=dt.timezone.utc).strftime("%Y-%m-%d")


async def history(client: httpx.AsyncClient, sym: str, rng: str) -> dict:
    rng = _norm_range(rng)
    j = await _get_json(client, f"/v8/finance/chart/{sym}", {"range": rng, "interval": RANGES[rng], "includePrePost": "false"})
    if j.get("_status") == 404:
        raise MarketError(f"代码不存在 unknown symbol: {sym}")
    d = j.get("chart") or {}
    if d.get("error") or not d.get("result"):
        raise MarketError(f"{sym}: {(d.get('error') or {}).get('description') or 'no data'}")
    res = d["result"][0]
    meta = res.get("meta") or {}
    off = int(meta.get("gmtoffset") or 0)
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    adj = ((res.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
    pts = []
    for i, ts in enumerate(res.get("timestamp") or []):
        c = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
        if c is None:
            continue
        pts.append({"date": _day(ts, off), "close": _r(c),
                    "high": _r((q.get("high") or [c])[i] or c), "low": _r((q.get("low") or [c])[i] or c)})
    if not pts:
        raise MarketError(f"{sym}: 这个区间没有数据 no prices in this range")
    last = meta.get("regularMarketPrice") or pts[-1]["close"]
    prev = meta.get("chartPreviousClose") if rng in ("5d", "1mo") else None
    hi = max(pts, key=lambda p: p["high"])
    lo = min(pts, key=lambda p: p["low"])
    first = pts[0]["close"]
    return {"symbol": meta.get("symbol") or sym, "name": meta.get("longName") or meta.get("shortName") or sym,
            "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "", "currency": meta.get("currency") or "",
            "range": rng, "interval": RANGES[rng], "last": _r(last),
            "last_time": _day(int(meta.get("regularMarketTime") or 0), off) if meta.get("regularMarketTime") else pts[-1]["date"],
            "day_change_pct": round((float(last) / float(meta["previousClose"]) - 1) * 100, 2) if meta.get("previousClose") else None,
            "start": {"date": pts[0]["date"], "close": first}, "change_pct": round((float(last) / first - 1) * 100, 2) if first else None,
            "high": {"date": hi["date"], "price": hi["high"]}, "low": {"date": lo["date"], "price": lo["low"]},
            "week52": [meta.get("fiftyTwoWeekLow"), meta.get("fiftyTwoWeekHigh")], "prev_close": prev,
            "series": [[p["date"], p["close"]] for p in pts], "adjusted": bool(adj)}


async def fetch(symbols: list[str], rng: str = "1y") -> dict:
    out, errors = [], []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for s in (symbols or [])[:8]:
            try:
                sym = await resolve(client, s)
                out.append(await history(client, sym, rng))
            except MarketError as e:
                errors.append(f"{s}: {e}")
            except (httpx.HTTPError, ValueError, KeyError) as e:
                errors.append(f"{s}: 数据源暂时不可用 data source error ({type(e).__name__})")
    return {"results": out, "errors": errors, "source": "Yahoo Finance"}


def describe(r: dict) -> str:
    """Compact text for the agent: headline numbers plus the series as date close pairs."""
    cur = r["currency"]
    lines = [f"{r['symbol']} · {r['name']} · {r['exchange']} · {cur}",
             f"最新价 last {r['last']} {cur}（{r['last_time']}"
             + (f"，较前收 {r['day_change_pct']:+.2f}%" if r.get("day_change_pct") is not None else "") + "）",
             f"区间 {r['range']}：起 start {r['start']['close']} ({r['start']['date']}) → 现 now {r['last']}，"
             f"涨跌 change {r['change_pct']:+.2f}%" if r.get("change_pct") is not None else "",
             f"区间最高 high {r['high']['price']} ({r['high']['date']})；最低 low {r['low']['price']} ({r['low']['date']})"]
    if r.get("week52") and all(r["week52"]):
        lines.append(f"52 周区间 52-week range: {r['week52'][0]} – {r['week52'][1]}")
    step = {"1d": "日", "1wk": "周", "1mo": "月", "3mo": "季"}.get(r["interval"], "")
    lines.append(f"收盘价序列（{step}线, {len(r['series'])} 点）closes: " + ", ".join(f"{d} {c:g}" for d, c in r["series"]))
    return "\n".join(x for x in lines if x)


# ---------------------------------------------------------------- valuations and reported financials
# Quote fields (P/E, market cap, dividend yield) need Yahoo's cookie + "crumb"; the reported-financials timeseries does not.
_crumb: dict = {"value": None, "cookies": None, "at": 0.0}
TS_FIELDS = ["TotalRevenue", "GrossProfit", "OperatingIncome", "NetIncome", "DilutedEPS"]


async def _quote_crumb(client: httpx.AsyncClient):
    import time as _t
    if _crumb["value"] and _t.time() - _crumb["at"] < 3600:
        return _crumb["value"], _crumb["cookies"]
    jar = httpx.Cookies()
    if HOSTS[0].startswith("https://query"):   # real Yahoo: the session cookie comes from fc.yahoo.com (answers 404 but sets it)
        try:
            r = await client.get("https://fc.yahoo.com", headers=UA, timeout=15)
            jar.update(r.cookies)
        except httpx.HTTPError:
            pass
    crumb = None
    for host in HOSTS:
        try:
            r = await client.get(host + "/v1/test/getcrumb", headers=UA, cookies=jar, timeout=15)
        except httpx.HTTPError:
            continue
        if r.status_code == 200 and 0 < len(r.text.strip()) < 40 and "<" not in r.text:
            crumb = r.text.strip()
            break
    _crumb.update(value=crumb, cookies=jar, at=_t.time())
    return crumb, jar


async def _quotes(client: httpx.AsyncClient, syms: list[str]) -> dict:
    crumb, jar = await _quote_crumb(client)
    params = {"symbols": ",".join(syms)}
    if crumb:
        params["crumb"] = crumb
    for host in HOSTS:
        r = await client.get(host + "/v7/finance/quote", params=params, headers=UA, cookies=jar, timeout=20)
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            return {q["symbol"]: q for q in (r.json().get("quoteResponse") or {}).get("result") or [] if q.get("symbol")}
        if r.status_code in (401, 403):
            _crumb["at"] = 0   # stale crumb: fetch a new one next time
    return {}


async def _financials(client: httpx.AsyncClient, sym: str) -> dict:
    import time as _t
    types = ",".join([f"quarterly{f}" for f in TS_FIELDS] + [f"annual{f}" for f in TS_FIELDS])
    d = await _get_json(client, f"/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}",
                        {"type": types, "period1": int(_t.time()) - 6 * 365 * 86400, "period2": int(_t.time()) + 86400})
    out = {"quarterly": {}, "annual": {}}
    for r in ((d.get("timeseries") or {}).get("result") or []):
        t = ((r.get("meta") or {}).get("type") or [""])[0]
        kind = "quarterly" if t.startswith("quarterly") else "annual" if t.startswith("annual") else None
        if not kind:
            continue
        field = t[len(kind):]
        for x in r.get(t) or []:
            if x and x.get("asOfDate") and (x.get("reportedValue") or {}).get("raw") is not None:
                out[kind].setdefault(x["asOfDate"], {})[field] = x["reportedValue"]["raw"]
                out[kind][x["asOfDate"]]["currency"] = x.get("currencyCode") or ""
    for kind in out:   # newest last, keep the last 4 periods
        out[kind] = dict(sorted(out[kind].items())[-4:])
    return out


async def fundamentals(symbols: list[str]) -> dict:
    out, errors = [], []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        syms = []
        for s in (symbols or [])[:6]:
            try:
                syms.append((s, await resolve(client, s)))
            except MarketError as e:
                errors.append(f"{s}: {e}")
            except (httpx.HTTPError, ValueError, KeyError) as e:
                errors.append(f"{s}: 数据源暂时不可用 data source error ({type(e).__name__})")
        try:
            quotes = await _quotes(client, [y for _x, y in syms]) if syms else {}
        except (httpx.HTTPError, ValueError) as e:
            quotes = {}
            errors.append(f"估值数据暂时不可用 valuation data unavailable ({type(e).__name__})")
        for orig, sym in syms:
            q = quotes.get(sym) or {}
            try:
                fin = await _financials(client, sym) if not sym.startswith("^") and "=" not in sym else {}
            except (MarketError, httpx.HTTPError, ValueError) as e:
                fin = {}
                errors.append(f"{orig}: 财报数据暂时不可用 financials unavailable ({e})"[:200])
            if not q and not (fin.get("quarterly") or fin.get("annual")):
                errors.append(f"{orig}: 没有数据 no data for {sym}")
                continue
            out.append({"symbol": sym, "name": q.get("longName") or q.get("shortName") or sym, "currency": q.get("currency") or "",
                        "financial_currency": q.get("financialCurrency") or "", "price": q.get("regularMarketPrice"),
                        "market_cap": q.get("marketCap"), "pe_ttm": q.get("trailingPE"), "pe_forward": q.get("forwardPE"),
                        "eps_ttm": q.get("epsTrailingTwelveMonths"), "pb": q.get("priceToBook"),
                        "dividend_yield_pct": q.get("dividendYield") if q.get("dividendYield") is not None else (
                            round(q["trailingAnnualDividendYield"] * 100, 2) if q.get("trailingAnnualDividendYield") else None),
                        "week52": [q.get("fiftyTwoWeekLow"), q.get("fiftyTwoWeekHigh")],
                        "earnings_date": _day(int(q["earningsTimestamp"])) if q.get("earningsTimestamp") else None,
                        "quarterly": fin.get("quarterly") or {}, "annual": fin.get("annual") or {}})
    return {"results": out, "errors": errors, "source": "Yahoo Finance"}


def _big(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    for n, u in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= n:
            return f"{v / n:.2f}{u}"
    return f"{v:g}"


def describe_fundamentals(r: dict) -> str:
    cur = r["currency"]
    f = lambda v, d=2: "—" if v is None else f"{float(v):.{d}f}"
    lines = [f"{r['symbol']} · {r['name']} · 股价 price {r['price']} {cur}",
             f"市值 market cap {_big(r['market_cap'])} {cur}；市盈率 P/E (TTM) {f(r['pe_ttm'])}；预期市盈率 forward P/E {f(r['pe_forward'])}；"
             f"市净率 P/B {f(r['pb'])}；每股收益 EPS (TTM) {f(r['eps_ttm'])}；股息率 dividend yield "
             + ("—" if r["dividend_yield_pct"] is None else f"{r['dividend_yield_pct']:.2f}%")]
    if r.get("earnings_date"):
        lines.append(f"最近/下次财报日 earnings date {r['earnings_date']}")
    for kind, label in (("quarterly", "季度财报 quarterly"), ("annual", "年度财报 annual")):
        rows = r.get(kind) or {}
        if not rows:
            continue
        fc = next(iter(rows.values())).get("currency") or r.get("financial_currency") or ""
        lines.append(f"{label}（{fc}）：")
        for d, x in rows.items():
            rev, gp, ni = x.get("TotalRevenue"), x.get("GrossProfit"), x.get("NetIncome")
            gm = f"{gp / rev * 100:.1f}%" if rev and gp else "—"
            nm = f"{ni / rev * 100:.1f}%" if rev and ni is not None else "—"
            lines.append(f"  {d}: 营收 revenue {_big(rev)}；毛利 gross profit {_big(gp)}（毛利率 gross margin {gm}）；"
                         f"经营利润 operating income {_big(x.get('OperatingIncome'))}；净利润 net income {_big(ni)}（净利率 net margin {nm}）；"
                         f"摊薄 EPS {x.get('DilutedEPS', '—')}")
    return "\n".join(lines)
