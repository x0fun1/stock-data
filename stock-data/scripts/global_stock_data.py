"""Stock data fallback functions and JSON command line interface.

Adapted from global-stock-data by Simon Lin:
https://github.com/simonlin1212/global-stock-data
Network requests are performed only by explicit function calls.
"""
from __future__ import annotations

import argparse
import csv
import inspect
import io
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


class ConfigurationError(RuntimeError):
    """The caller must supply required provider configuration."""


class AuthorizationRequired(RuntimeError):
    """Provider authorization has not been recorded in the environment."""


class ProviderError(RuntimeError):
    """The provider reported an error or an invalid response."""


def _nonempty_text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string.")
    return value.strip()


def _positive_int(value, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _cik(value) -> str:
    value = _nonempty_text(value, "cik")
    if not value.isascii() or not value.isdigit() or len(value) > 10:
        raise ValueError("cik must contain 1 to 10 digits.")
    return value.zfill(10)


def _date(value, name: str, format_: str = "%Y-%m-%d") -> str:
    value = _nonempty_text(value, name)
    try:
        parsed = datetime.strptime(value, format_)
    except ValueError as exc:
        raise ValueError(f"{name} has an invalid date format or value.") from exc
    if parsed.strftime(format_) != value:
        raise ValueError(f"{name} has an invalid date format.")
    return value


def _candle_sort_key(candle: dict, index: int) -> tuple[str, float]:
    """Use the most precise provided key without the host's local timezone."""
    if "timestamp_epoch" in candle:
        value = candle["timestamp_epoch"]
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(value)):
            raise ValueError(f"Candle {index} has invalid timestamp_epoch.")
        return "timestamp_epoch", value
    field = "timestamp_utc" if "timestamp_utc" in candle else "date"
    value = candle.get(field)
    if not isinstance(value, str):
        raise ValueError(f"Candle {index} has invalid {field}.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Candle {index} has invalid {field}.") from exc
    if field == "timestamp_utc":
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValueError(f"Candle {index} timestamp_utc must contain a UTC timezone.")
    elif parsed.tzinfo is None:
        # Legacy date labels are calendar keys. UTC is an explicit ordering
        # convention here; it does not assert the source exchange's timezone.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return field, parsed.timestamp()


def _validate_klines(klines, fields: tuple[str, ...] = ("close",)) -> None:
    if not isinstance(klines, list):
        raise ValueError("klines must be a list of candles.")
    key_field = None
    previous = None
    for index, candle in enumerate(klines):
        if not isinstance(candle, dict) or not candle.get("date"):
            raise ValueError(f"Candle {index} must contain a date.")
        for field in fields:
            value = candle.get(field)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value)):
                raise ValueError(f"Candle {index} has missing or invalid {field}.")
        if "high" in fields and candle["high"] < candle["low"]:
            raise ValueError(f"Candle {index} has high below low.")
        field, current = _candle_sort_key(candle, index)
        if key_field is not None and key_field != field:
            raise ValueError("Candles must use the same timestamp field throughout the series.")
        if previous is not None and current <= previous:
            raise ValueError(
                "Candles must be strictly ascending with no duplicates. "
                "Same-day intraday candles require explicit timestamp_epoch or timestamp_utc.")
        key_field, previous = field, current


def _periods(values, name: str = "periods") -> None:
    if not isinstance(values, list) or not values:
        raise ValueError(f"{name} must be a nonempty list of positive integers.")
    for value in values:
        _positive_int(value, name)


def _bootstrap_yahoo(session: requests.Session) -> None:
    # The cookie bootstrap endpoint can return 404 while setting valid cookies.
    # That documented handshake status is accepted; all other errors propagate.
    response = session.get("https://fc.yahoo.com", timeout=10)
    if response.status_code != 404:
        response.raise_for_status()


_yahoo_session = None

def get_yahoo_session() -> requests.Session:
    """获取带 crumb 的 Yahoo Finance session（自动缓存）"""
    global _yahoo_session
    if _yahoo_session and hasattr(_yahoo_session, '_crumb'):
        return _yahoo_session

    s = requests.Session()
    s.headers['User-Agent'] = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'

    # Step 1: 获取 cookie
    _bootstrap_yahoo(s)

    # Step 2: 获取 crumb
    r = s.get('https://query2.finance.yahoo.com/v1/test/getcrumb', timeout=10)
    r.raise_for_status()
    if not r.text.strip() or "<" in r.text or "\n" in r.text:
        raise ProviderError("Yahoo returned an invalid crumb.")
    s._crumb = r.text

    _yahoo_session = s
    return s

def yahoo_quote_summary(symbol: str, modules: list[str]) -> dict:
    """Yahoo quoteSummary 统一查询"""
    _nonempty_text(symbol, "symbol")
    if (not isinstance(modules, list) or not modules
            or any(not isinstance(m, str) or not m.strip() for m in modules)):
        raise ValueError("modules must be a nonempty list of module names.")
    s = get_yahoo_session()
    r = s.get(f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}', params={
        'modules': ','.join(modules),
        'crumb': s._crumb,
    }, timeout=15)
    r.raise_for_status()
    payload = r.json().get("quoteSummary") or {}
    if payload.get("error"):
        raise ProviderError("Yahoo quoteSummary returned a provider error.")
    results = payload.get("result") or []
    return results[0] if results else {}


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

def eastmoney_datacenter(report_name: str, columns: str = "ALL",
                          filter_str: str = "", page_size: int = 50,
                          sort_columns: str = "", sort_types: str = "-1") -> list[dict]:
    """东财数据中心统一查询"""
    _nonempty_text(report_name, "report_name")
    _positive_int(page_size, "page_size")
    params = {
        "reportName": report_name, "columns": columns,
        "filter": filter_str, "pageNumber": "1", "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    r = requests.get(DATACENTER_URL, params=params, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("success") is False:
        raise ProviderError("Eastmoney datacenter returned a provider error.")
    if d.get("result") and d["result"].get("data"):
        return d["result"]["data"]
    return []



# SEC requires a real contact identity in User-Agent; configure it outside code.
# Read on each request so callers can configure the environment after import.


class DataNotAvailable(RuntimeError):
    """该日/该标的确实没有数据（如非交易日、文件尚未发布）——可安全回退到下一个候选日。

    与配置错误、网络错误区分开：后者必须立刻抛给调用方，
    否则「SEC_CONTACT 没配」会被日期回退循环吞掉，最后伪装成「没找到数据」。
    """


class _RateLimiter:
    """线程安全的最小间隔节流器（用锁，避免并发下被击穿）"""

    def __init__(self, max_per_sec: float):
        self._interval = 1.0 / float(max_per_sec)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


# 各源限速：SEC 官方硬上限 10/s，此处取 8/s 留余量；其余为自律保护值
_LIMITS = {
    "sec.gov": _RateLimiter(8),
    "finra.org": _RateLimiter(4),
    "cboe.com": _RateLimiter(4),
    "nasdaq.com": _RateLimiter(2),
    "_default": _RateLimiter(5),
}


def _limiter_for(url: str) -> _RateLimiter:
    host = (urlparse(url).hostname or "").lower()
    for domain, limiter in _LIMITS.items():
        if host == domain or host.endswith("." + domain):
            return limiter
    return _LIMITS["_default"]


def _is_object_missing(resp) -> bool:
    """Only a 404 or explicit missing-object error permits date fallback.

    XML AccessDenied (403) does not establish that a resource is absent.
    """
    if resp.status_code == 404:
        return True
    if resp.status_code not in (400, 403):
        return False
    from xml.etree import ElementTree
    try:
        root = ElementTree.fromstring(resp.text or "")
    except ElementTree.ParseError:
        return False
    code = next((item.text for item in root.iter()
                 if item.tag.rsplit("}", 1)[-1] == "Code"), None)
    return code in {"NoSuchKey", "NoSuchObject"}


def official_get(url: str, params: dict = None, headers: dict = None,
                 timeout: int = 30, as_json: bool = False):
    """Rate limited HTTP; only confirmed absent resources become DataNotAvailable."""
    host = (urlparse(url).hostname or "").lower()
    is_sec = host == "sec.gov" or host.endswith(".sec.gov")
    is_cboe = host == "cboe.com" or host.endswith(".cboe.com")
    if is_cboe and os.environ.get("CBOE_AUTHORIZED") != "1":
        raise AuthorizationRequired(
            "CBOE retrieval requires existing authorization and CBOE_AUTHORIZED=1.")
    if is_sec:
        contact = os.environ.get("SEC_CONTACT", "").strip()
        placeholder = any(value in contact.lower() for value in (
            "example.com", "example.org", "example.net", "domain.com",
            "your-email", "your name", "your company"))
        if not re.fullmatch(r".+\s+[^\s@]+@[^\s@]+\.[^\s@]+", contact) or placeholder:
            raise ConfigurationError(
                "Set SEC_CONTACT to a real company/name and contact email before SEC requests.")
        h = {"User-Agent": contact, "Accept-Encoding": "gzip, deflate"}
    else:
        h = {"User-Agent": UA}
    h.update(headers or {})
    if is_sec:
        h["User-Agent"] = contact

    _limiter_for(url).wait()
    response = requests.get(url, params=params, headers=h, timeout=timeout)
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        if _is_object_missing(response):
            raise DataNotAvailable(
                f"HTTP {response.status_code}: requested resource is not available.") from exc
        raise
    return response.json() if as_json else response.text


def assert_us_ticker(ticker: str) -> str:
    """Normalize a US ticker; Hong Kong codes must use HK endpoints."""
    value = _nonempty_text(ticker, "ticker").upper()
    if value.endswith(".HK") or value.isdigit():
        raise ValueError("This function supports US tickers only.")
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]*", value):
        raise ValueError("ticker has an invalid format.")
    return value


def _num(value) -> float | None:
    """Parse a numeric source value; missing placeholders remain None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).strip())
    except (ValueError, TypeError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int_or_none(value) -> int | None:
    parsed = _num(value)
    return int(parsed) if parsed is not None else None


def us_stock_quote_sina(ticker: str) -> dict:
    """
    新浪美股行情 — 36字段
    ticker: 纯字母，如 "AAPL", "TSLA", "BABA"
    """
    ticker = assert_us_ticker(ticker)
    url = f"https://hq.sinajs.cn/list=gb_{ticker.lower()}"
    r = requests.get(url, headers={
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": UA,
    }, timeout=10)
    r.raise_for_status()
    r.encoding = "gbk"
    text = r.text

    m = re.search(r'"(.+)"', text)
    if not m:
        return {}

    fields = m.group(1).split(",")
    if len(fields) < 30:
        return {}

    return {
        "name": fields[0],           # 中文名
        "price": _num(fields[1]),    # 最新价
        "change_pct": _num(fields[2]),  # 涨跌幅 %
        "timestamp": fields[3],       # 时间
        "prev_close": _num(fields[26]),  # 昨收
        "open": _num(fields[5]),     # 开盘
        "high": _num(fields[6]),     # 最高
        "low": _num(fields[7]),      # 最低
        "volume": _num(fields[10]),  # 成交量
        "high_52w": _num(fields[8]),  # 52周最高
        "low_52w": _num(fields[9]),   # 52周最低
        "market_cap": _num(fields[12]),  # 市值
        "eps": _num(fields[13]),  # EPS
        "pe": _num(fields[14]),   # PE
    }


def us_stock_quote_tencent(ticker: str) -> dict:
    """
    腾讯美股行情 — 71字段
    ticker: 纯字母，如 "AAPL"
    """
    ticker = assert_us_ticker(ticker)
    url = f"https://qt.gtimg.cn/q=us{ticker.upper()}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    r.encoding = "gbk"
    text = r.text

    m = re.search(r'"(.+)"', text)
    if not m:
        return {}

    fields = m.group(1).split("~")
    if len(fields) < 52:   # 需读到 fields[51](PB)，美股正常返回 71 个
        return {}

    # ⚠️ 下标以实测为准，勿照抄港股那套（两市布局不同，见本节末「腾讯行情字段对照表」）
    return {
        "name": fields[1],           # 中文名
        "name_en": fields[46],       # 英文名，如 "Apple Inc."
        "price": _num(fields[3]),
        "prev_close": _num(fields[4]),
        "open": _num(fields[5]),
        "volume": _int_or_none(fields[6]),
        "high": _num(fields[33]),
        "low": _num(fields[34]),
        "high_52w": _num(fields[48]),
        "low_52w": _num(fields[49]),
        "change_pct": _num(fields[32]),
        "float_market_cap": _num(fields[44]),  # 流通市值，亿美元
        "market_cap": _num(fields[45]),        # 总市值，亿美元
        "eps": _num(fields[47]),
        "pe": _num(fields[39]),
        "pb": _num(fields[51]),
        "currency": fields[35],      # "USD"
        "timestamp": fields[30],
    }


def hk_stock_quote_tencent(code: str) -> dict:
    """
    腾讯港股行情 — 78字段（最全）
    code: 五位数字，如 "00700", "09988"
    """
    code = _nonempty_text(code, "code")
    if not code.isascii() or not code.isdigit() or len(code) not in (4, 5):
        raise ValueError("code must contain 4 or 5 Hong Kong code digits.")
    code = code.zfill(5)
    url = f"https://qt.gtimg.cn/q=r_hk{code}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    r.encoding = "gbk"
    text = r.text

    m = re.search(r'"(.+)"', text)
    if not m:
        return {}

    fields = m.group(1).split("~")
    if len(fields) < 76:   # 需读到 fields[75](币种)，港股正常返回 78 个
        return {}

    # ⚠️ 下标以实测为准，与美股那套不同（见本节末「腾讯行情字段对照表」）
    return {
        "name": fields[1],           # 中文名
        "code": fields[2],           # 五位代码，如 "00700"（旧版误当英文名）
        "name_en": fields[46],       # 英文名，如 "TENCENT"
        "price": _num(fields[3]),
        "prev_close": _num(fields[4]),
        "open": _num(fields[5]),
        "high": _num(fields[33]),
        "low": _num(fields[34]),
        "volume": _int_or_none(fields[6]),  # 成交量(股)
        "amount": _num(fields[37]),     # 成交额
        "change_pct": _num(fields[32]),
        "pe": _num(fields[39]),
        "pb": _num(fields[58]),
        "high_52w": _num(fields[48]),
        "low_52w": _num(fields[49]),
        "float_market_cap": _num(fields[44]),  # 流通市值，亿港元
        "market_cap": _num(fields[45]),        # 总市值，亿港元
        "currency": fields[75],      # "HKD"
        "timestamp": fields[30],
    }


def hk_stock_quote_sina(code: str) -> dict:
    """
    新浪港股行情 — 25字段
    code: 五位数字，如 "00700"
    """
    code = _nonempty_text(code, "code")
    if not code.isascii() or not code.isdigit() or len(code) not in (4, 5):
        raise ValueError("code must contain 4 or 5 Hong Kong code digits.")
    code = code.zfill(5)
    url = f"https://hq.sinajs.cn/list=rt_hk{code}"
    r = requests.get(url, headers={
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": UA,
    }, timeout=10)
    r.raise_for_status()
    r.encoding = "gbk"
    text = r.text

    m = re.search(r'"(.+)"', text)
    if not m:
        return {}

    fields = m.group(1).split(",")
    if len(fields) < 15:
        return {}

    return {
        "name_en": fields[0],
        "name": fields[1],           # 中文名
        "open": _num(fields[2]),
        "prev_close": _num(fields[3]),
        "high": _num(fields[4]),
        "low": _num(fields[5]),
        "price": _num(fields[6]),
        "change": _num(fields[7]),
        "change_pct": _num(fields[8]),
        "volume": _num(fields[12]),
        "amount": _num(fields[11]),
    }


def stock_quote_eastmoney(ticker_or_code: str, secid_prefix: int = 105) -> dict:
    """
    东财 push2 实时行情 — 美股+港股统一接口
    美股: stock_quote_eastmoney("AAPL", 105)  # NASDAQ
          stock_quote_eastmoney("BABA", 106)  # NYSE
    港股: stock_quote_eastmoney("00700", 116)
    返回: 最新价/开高低收/成交量/成交额/换手率/涨跌幅/中文名

    secid_prefix 说明: 105=NASDAQ, 106=NYSE, 107=US_ETF, 116=港股
    如不确定前缀，先调 stock_search() 获取 mkt_num
    """
    _nonempty_text(ticker_or_code, "ticker_or_code")
    if secid_prefix not in (105, 106, 107, 116):
        raise ValueError("secid_prefix must be 105, 106, 107 or 116.")
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": f"{secid_prefix}.{ticker_or_code}",
        "fields": "f43,f44,f45,f46,f47,f48,f55,f57,f58,f59,f60,f170",
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    d = r.json().get("data")
    if not d:
        return {}

    # f59 = 小数位数, 价格字段需除以 10^f59 还原真实值
    dec = _int_or_none(d.get("f59"))
    if dec is None or not 0 <= dec <= 12:
        raise ProviderError("Eastmoney quote lacks a valid price precision.")
    divisor = 10 ** dec

    def _p(key):
        v = _num(d.get(key))
        if v is None:
            return None
        return round(v / divisor, dec)

    return {
        "code": d.get("f57"),           # 股票代码
        "name": d.get("f58"),           # 中文名
        "price": _p("f43"),             # 最新价
        "high": _p("f44"),              # 最高
        "low": _p("f45"),               # 最低
        "open": _p("f46"),              # 开盘
        "volume": _int_or_none(d.get("f47")),         # 成交量(股)
        "amount": _num(d.get("f48")),         # 成交额
        "turnover_rate": _num(d.get("f55")),  # 换手率(%)
        "prev_close": _p("f60"),        # 昨收
        "change_pct": _num(d["f170"]) / 100 if _num(d.get("f170")) is not None else None,  # 涨跌幅(%)
    }


def us_stock_kline_sina(ticker: str, num: int = 120) -> list[dict]:
    """
    新浪美股日K — 可回溯到1984年
    ticker: 如 "AAPL"
    返回: [{date, open, high, low, close, volume}, ...]
    """
    _positive_int(num, "num")
    ticker = assert_us_ticker(ticker)
    url = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK"
    params = {"symbol": ticker.upper(), "num": num}
    r = requests.get(url, params=params, headers={"Referer": "https://finance.sina.com.cn/"}, timeout=15)
    r.raise_for_status()
    text = r.text

    # 解析 JSONP: var=([{...},...])
    import json
    m = re.search(r'\((\[.+\])\)', text)
    if not m:
        return []

    items = json.loads(m.group(1))
    result = []
    for item in items:
        result.append({
            "date": item.get("d"),
            "open": _num(item.get("o")),
            "high": _num(item.get("h")),
            "low": _num(item.get("l")),
            "close": _num(item.get("c")),
            "volume": _int_or_none(item.get("v")),
        })
    return result


def stock_kline_yahoo(symbol: str, interval: str = "1d",
                       range_: str = "6mo") -> list[dict]:
    """Yahoo OHLCV with unchanged precision, source epoch and UTC timestamps.

    date is the UTC calendar date. Use timestamp_utc for intraday ordering;
    no field uses the execution host's local timezone.
    Null or absent fields remain None for callers to assess.
    """
    _nonempty_text(symbol, "symbol")
    if interval not in {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"}:
        raise ValueError("Unsupported Yahoo candle interval.")
    if range_ not in {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}:
        raise ValueError("Unsupported Yahoo candle range.")
    response = requests.get(
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"interval": interval, "range": range_},
        headers={"User-Agent": UA}, timeout=15)
    response.raise_for_status()
    payload = response.json().get("chart") or {}
    if payload.get("error"):
        raise ProviderError("Yahoo chart returned a provider error.")
    results = payload.get("result") or []
    if not results:
        return []
    chart = results[0] or {}
    quotes = (chart.get("indicators") or {}).get("quote") or []
    quote = quotes[0] if quotes else {}
    result = []
    for index, epoch in enumerate(chart.get("timestamp") or []):
        if _num(epoch) is None:
            raise ProviderError("Yahoo chart returned an invalid candle timestamp.")
        timestamp = datetime.fromtimestamp(epoch, timezone.utc)
        candle = {
            "date": timestamp.strftime("%Y-%m-%d"),
            "timestamp_epoch": epoch,
            "timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
        }
        for field in ("open", "high", "low", "close", "volume"):
            values = quote.get(field) or []
            candle[field] = _num(values[index]) if index < len(values) else None
        result.append(candle)
    return result


def _ema(values: list[float], period: int) -> list[float]:
    """EMA 指数移动平均（内部辅助）"""
    result = [values[0]]
    k = 2 / (period + 1)
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def calc_ma(klines: list[dict], periods: list[int] = None) -> list[dict]:
    """
    移动平均线 MA + EMA
    klines: K线数据 [{date, open, high, low, close, volume}, ...]
    periods: 周期列表，默认 [5, 10, 20, 60]
    返回: [{date, close, ma5, ma10, ma20, ma60, ema12, ema26}, ...]
    """
    _validate_klines(klines)
    if periods is None:
        periods = [5, 10, 20, 60]
    _periods(periods)
    if not klines:
        return []
    closes = [k["close"] for k in klines]

    # EMA 12/26（MACD 常用）
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)

    result = []
    for i, k in enumerate(klines):
        row = {"date": k["date"], "close": k["close"]}
        for p in periods:
            if i >= p - 1:
                row[f"ma{p}"] = round(sum(closes[i - p + 1:i + 1]) / p, 4)
            else:
                row[f"ma{p}"] = None
        row["ema12"] = round(ema12[i], 4)
        row["ema26"] = round(ema26[i], 4)
        result.append(row)
    return result


def calc_macd(klines: list[dict], fast: int = 12, slow: int = 26,
              signal: int = 9) -> list[dict]:
    """
    MACD (Moving Average Convergence Divergence)
    klines: K线数据
    fast/slow/signal: 快线/慢线/信号线周期（默认 12/26/9）
    返回: [{date, close, dif, dea, macd_hist}, ...]

    dif = EMA(fast) - EMA(slow)        金叉/死叉看 dif 穿越 dea
    dea = EMA(signal) of dif           信号线
    macd_hist = (dif - dea) * 2        柱状图（红涨绿跌）
    """
    _validate_klines(klines)
    for name, value in (("fast", fast), ("slow", slow), ("signal", signal)):
        _positive_int(value, name)
    if not klines:
        return []
    closes = [k["close"] for k in klines]
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)

    dif = [round(f - s, 4) for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)

    result = []
    for i, k in enumerate(klines):
        result.append({
            "date": k["date"],
            "close": k["close"],
            "dif": round(dif[i], 4),
            "dea": round(dea[i], 4),
            "macd_hist": round((dif[i] - dea[i]) * 2, 4),
        })
    return result


def calc_rsi(klines: list[dict],
             periods: list[int] = None) -> list[dict]:
    """
    RSI (Relative Strength Index)，保留原 simple-average 版本。
    涨跌幅使用最近 N 期的简单平均，未使用 Wilder 平滑。
    klines: K线数据
    periods: 周期列表（默认 [6, 12, 24]）
    返回: [{date, close, rsi6, rsi12, rsi24}, ...]

    RSI > 70 / < 30 为常见分析阈值，本计算不预测价格变化。
    """
    _validate_klines(klines)
    if periods is None:
        periods = [6, 12, 24]
    _periods(periods)
    closes = [k["close"] for k in klines]

    # 涨跌额序列
    changes = [0.0] + [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes]
    losses = [max(-c, 0) for c in changes]

    result = []
    for i, k in enumerate(klines):
        row = {"date": k["date"], "close": k["close"]}
        for p in periods:
            if i < p:
                row[f"rsi{p}"] = None
                continue
            avg_gain = sum(gains[i - p + 1:i + 1]) / p
            avg_loss = sum(losses[i - p + 1:i + 1]) / p
            if avg_loss == 0:
                row[f"rsi{p}"] = 100.0
            else:
                rs = avg_gain / avg_loss
                row[f"rsi{p}"] = round(100 - 100 / (1 + rs), 2)
        result.append(row)
    return result


def calc_kdj(klines: list[dict], n: int = 9,
             m1: int = 3, m2: int = 3) -> list[dict]:
    """
    KDJ 随机指标
    klines: K线数据
    n: RSV 周期（默认9）
    m1/m2: K/D 平滑系数（默认3/3）
    返回: [{date, close, k, d, j}, ...]

    K/D > 80 超买，K/D < 20 超卖
    J > 100 或 J < 0 为极端信号
    金叉: K 上穿 D；死叉: K 下穿 D
    """
    _validate_klines(klines, ("close", "high", "low"))
    for name, value in (("n", n), ("m1", m1), ("m2", m2)):
        _positive_int(value, name)
    k_val, d_val = 50.0, 50.0
    result = []

    for i, kline in enumerate(klines):
        if i < n - 1:
            result.append({"date": kline["date"], "close": kline["close"],
                           "k": None, "d": None, "j": None})
            continue

        window = klines[i - n + 1:i + 1]
        high_n = max(w["high"] for w in window)
        low_n = min(w["low"] for w in window)

        rsv = (kline["close"] - low_n) / (high_n - low_n) * 100 if high_n != low_n else 50.0
        k_val = (1 / m1) * rsv + (1 - 1 / m1) * k_val
        d_val = (1 / m2) * k_val + (1 - 1 / m2) * d_val
        j_val = 3 * k_val - 2 * d_val

        result.append({
            "date": kline["date"],
            "close": kline["close"],
            "k": round(k_val, 2),
            "d": round(d_val, 2),
            "j": round(j_val, 2),
        })
    return result


def calc_boll(klines: list[dict], period: int = 20,
              num_std: float = 2.0) -> list[dict]:
    """
    布林带 (Bollinger Bands)
    klines: K线数据
    period: 中轨 MA 周期（默认20）
    num_std: 标准差倍数（默认2）
    返回: [{date, close, upper, middle, lower, bandwidth}, ...]

    bandwidth 描述波动带宽度；收窄不确定未来价格方向或变化时点。
    """
    _validate_klines(klines)
    _positive_int(period, "period")
    if _num(num_std) is None or num_std < 0:
        raise ValueError("num_std must be a finite nonnegative number.")
    closes = [k["close"] for k in klines]
    result = []

    for i, k in enumerate(klines):
        if i < period - 1:
            result.append({"date": k["date"], "close": k["close"],
                           "upper": None, "middle": None, "lower": None,
                           "bandwidth": None})
            continue

        window = closes[i - period + 1:i + 1]
        ma = sum(window) / period
        std = (sum((x - ma) ** 2 for x in window) / period) ** 0.5
        upper = ma + num_std * std
        lower = ma - num_std * std

        result.append({
            "date": k["date"],
            "close": k["close"],
            "upper": round(upper, 4),
            "middle": round(ma, 4),
            "lower": round(lower, 4),
            "bandwidth": round((upper - lower) / ma * 100, 2) if ma else None,
        })
    return result


def financial_statements_eastmoney(secucode: str, statement: str = "balance",
                                     page_size: int = 200) -> list[dict]:
    """
    东财 datacenter 财报三表
    secucode: "AAPL.O" (NASDAQ) / "BABA.N" (NYSE) / "00700.HK" (港股)
    statement: "balance" / "income" / "cashflow"
    返回: [{ITEM_NAME, AMOUNT, YOY_RATIO, REPORT, REPORT_DATE, ...}, ...]

    注意: 数据按科目行展开，每行一个科目（如"流动资产合计"、"营业收入"等），
    同一期报告有多行。用 REPORT_DATE 分组可还原整张报表。
    """
    _nonempty_text(secucode, "secucode")
    if statement not in ("balance", "income", "cashflow"):
        raise ValueError("statement must be balance, income or cashflow.")
    # 报表名映射（注意命名不统一：balance/income 用 F10，cashflow 用 SK）
    report_map = {
        "balance": {"us": "RPT_USF10_FN_BALANCE", "hk": "RPT_HKF10_FN_BALANCE"},
        "income":  {"us": "RPT_USF10_FN_INCOME",  "hk": "RPT_HKF10_FN_INCOME"},
        "cashflow": {"us": "RPT_USSK_FN_CASHFLOW", "hk": "RPT_HKSK_FN_CASHFLOW"},
    }

    market = "hk" if secucode.endswith(".HK") else "us"
    report_name = report_map[statement][market]

    return eastmoney_datacenter(
        report_name=report_name,
        filter_str=f'(SECUCODE="{secucode}")',
        page_size=page_size,
        sort_columns="REPORT_DATE",
        sort_types="-1",
    )
    # 每行字段:
    # SECUCODE, SECURITY_CODE, SECURITY_NAME_ABBR, REPORT_DATE,
    # STD_ITEM_CODE, ITEM_NAME (科目名), AMOUNT (金额),
    # YOY_RATIO (同比%), REPORT (如 "2026/Q2"), REPORT_TYPE,
    # ACCOUNT_STANDARD (如 "美国会计准则"/"国际会计准则"),
    # CURRENCY (如 "美元"/"人民币")


def key_indicators_eastmoney(secucode: str, page_size: int = 4) -> list[dict]:
    """
    东财 GMAININDICATOR 关键财务指标（中文）
    secucode: "AAPL.O" (NASDAQ) / "BABA.N" (NYSE) / "00700.HK" (港股)
    page_size: 返回最近几期报告（默认4期=一年）
    返回: [{REPORT_DATE, OPERATE_INCOME, BASIC_EPS, ROE_AVG, ROA, ...}, ...]

    美股核心字段(49): OPERATE_INCOME(营收), GROSS_PROFIT(毛利), GROSS_PROFIT_RATIO(毛利率%),
      PARENT_HOLDER_NETPROFIT(归母净利), NET_PROFIT_RATIO(净利率%), BASIC_EPS, DILUTED_EPS,
      ROE_AVG(平均ROE%), ROA(%), CURRENT_RATIO(流动比率), DEBT_ASSET_RATIO(资产负债率%),
      OPERATE_INCOME_YOY(营收同比%), BASIC_EPS_YOY(EPS同比%)

    港股额外字段(75): BPS(每股净资产), ROIC(投入资本回报率), EQUITY_RATIO(产权比率),
      HOLDER_PROFIT(股东应占溢利), OCF_SALES(经营现金流/营收%), DPS_HKD(每股股息),
      DIVI_RATIO(股息率%), PER_NETCASH_OPERATE(每股经营现金流)
    """
    _nonempty_text(secucode, "secucode")
    market = "hk" if secucode.endswith(".HK") else "us"
    report_name = f"RPT_{'HK' if market == 'hk' else 'US'}F10_FN_GMAININDICATOR"

    return eastmoney_datacenter(
        report_name=report_name,
        filter_str=f'(SECUCODE="{secucode}")',
        page_size=page_size,
        sort_columns="REPORT_DATE",
        sort_types="-1",
    )


def key_statistics(symbol: str) -> dict:
    """
    Yahoo 关键财务指标
    symbol: "AAPL" (美股) 或 "0700.HK" (港股)
    返回: PE/PB/EV/EBITDA/利润率/目标价/ROE/Beta 等
    """
    data = yahoo_quote_summary(symbol, ["financialData", "defaultKeyStatistics", "summaryDetail"])

    fd = data.get("financialData", {})
    ks = data.get("defaultKeyStatistics", {})
    sd = data.get("summaryDetail", {})

    def _val(d, key):
        v = d.get(key, {})
        return v.get("raw") if isinstance(v, dict) else v

    return {
        # 价格相关
        "current_price": _val(fd, "currentPrice"),
        "target_high": _val(fd, "targetHighPrice"),
        "target_low": _val(fd, "targetLowPrice"),
        "target_mean": _val(fd, "targetMeanPrice"),
        "recommendation": fd.get("recommendationKey"),  # buy/hold/sell

        # 估值指标
        "trailing_pe": _val(sd, "trailingPE"),
        "forward_pe": _val(ks, "forwardPE"),
        "peg_ratio": _val(ks, "pegRatio"),
        "price_to_book": _val(ks, "priceToBook"),
        "enterprise_value": _val(ks, "enterpriseValue"),
        "ev_to_ebitda": _val(ks, "enterpriseToEbitda"),
        "ev_to_revenue": _val(ks, "enterpriseToRevenue"),

        # 盈利能力
        "profit_margin": _val(ks, "profitMargins"),
        "operating_margin": _val(fd, "operatingMargins"),
        "gross_margin": _val(fd, "grossMargins"),
        "return_on_equity": _val(fd, "returnOnEquity"),
        "return_on_assets": _val(fd, "returnOnAssets"),

        # 成长性
        "earnings_growth": _val(fd, "earningsGrowth"),
        "revenue_growth": _val(fd, "revenueGrowth"),

        # 风险
        "beta": _val(ks, "beta"),
        "short_ratio": _val(ks, "shortRatio"),

        # 股息
        "dividend_yield": _val(sd, "dividendYield"),
        "payout_ratio": _val(ks, "payoutRatio"),

        # 规模
        "market_cap": _val(sd, "marketCap"),
        "total_revenue": _val(fd, "totalRevenue"),
        "total_cash": _val(fd, "totalCash"),
        "total_debt": _val(fd, "totalDebt"),
    }


def analyst_estimates(symbol: str) -> dict:
    """
    Yahoo 分析师预期 — EPS预测/评级趋势/升降级历史
    symbol: "AAPL" 或 "0700.HK"
    """
    data = yahoo_quote_summary(symbol, [
        "earningsTrend", "recommendationTrend", "upgradeDowngradeHistory",
        "earnings", "earningsHistory",
    ])

    # EPS 趋势
    et = data.get("earningsTrend", {}).get("trend", [])
    eps_trend = []
    for t in et:
        eps_trend.append({
            "period": t.get("period"),
            "end_date": t.get("endDate"),
            "eps_estimate": t.get("earningsEstimate", {}).get("avg", {}).get("raw"),
            "eps_high": t.get("earningsEstimate", {}).get("high", {}).get("raw"),
            "eps_low": t.get("earningsEstimate", {}).get("low", {}).get("raw"),
            "revenue_estimate": t.get("revenueEstimate", {}).get("avg", {}).get("raw"),
            "num_analysts": t.get("earningsEstimate", {}).get("numberOfAnalysts", {}).get("raw"),
        })

    # 评级趋势 (最近4个月)
    rt = data.get("recommendationTrend", {}).get("trend", [])
    rating_trend = []
    for r_ in rt:
        rating_trend.append({
            "period": r_.get("period"),
            "strong_buy": r_.get("strongBuy"),
            "buy": r_.get("buy"),
            "hold": r_.get("hold"),
            "sell": r_.get("sell"),
            "strong_sell": r_.get("strongSell"),
        })

    # 升降级历史 (最近20条)
    udh = data.get("upgradeDowngradeHistory", {}).get("history", [])[:20]
    upgrades = []
    for u in udh:
        upgrades.append({
            "date": u.get("epochGradeDate"),
            "firm": u.get("firm"),
            "to_grade": u.get("toGrade"),
            "from_grade": u.get("fromGrade"),
            "action": u.get("action"),  # up/down/main/init
        })

    return {
        "eps_trend": eps_trend,
        "rating_trend": rating_trend,
        "upgrade_downgrade": upgrades,
    }


def institutional_holders(symbol: str) -> dict:
    """
    Yahoo 机构持仓 — 前10大机构 + 内部人持股比例
    symbol: "AAPL" 或 "0700.HK"
    """
    data = yahoo_quote_summary(symbol, ["institutionOwnership", "majorHoldersBreakdown"])

    # 持股比例总览
    mhb = data.get("majorHoldersBreakdown", {})
    def _val(d, key):
        v = d.get(key, {})
        return v.get("raw") if isinstance(v, dict) else v

    overview = {
        "insiders_pct": _val(mhb, "insidersPercentHeld"),
        "institutions_pct": _val(mhb, "institutionsPercentHeld"),
        "institutions_float_pct": _val(mhb, "institutionsFloatPercentHeld"),
        "institutions_count": _val(mhb, "institutionsCount"),
    }

    # 前10大机构
    io = data.get("institutionOwnership", {}).get("ownershipList", [])
    top_holders = []
    for h in io[:10]:
        top_holders.append({
            "name": h.get("organization"),
            "shares": _val(h, "position"),
            "value": _val(h, "value"),
            "pct_held": _val(h, "pctHeld"),
            "report_date": h.get("reportDate", {}).get("fmt") if isinstance(h.get("reportDate"), dict) else None,
        })

    return {"overview": overview, "top_holders": top_holders}


def financial_statements_yahoo(symbol: str,
                                 quarterly: bool = False) -> dict:
    """
    Yahoo 财报三表 — 结构化完整报表
    symbol: "AAPL" 或 "0700.HK"
    quarterly: False=年度, True=季度
    返回: {"income": [...], "balance": [...], "cashflow": [...]}
    """
    suffix = "Quarterly" if quarterly else ""
    data = yahoo_quote_summary(symbol, [
        f"incomeStatementHistory{suffix}",
        f"balanceSheetHistory{suffix}",
        f"cashflowStatementHistory{suffix}",
    ])

    def _extract(statements):
        result = []
        for stmt in statements:
            row = {}
            for k, v in stmt.items():
                if isinstance(v, dict) and "raw" in v:
                    row[k] = v["raw"]
                elif isinstance(v, dict) and "fmt" in v:
                    row[k] = v["fmt"]
                else:
                    row[k] = v
            result.append(row)
        return result

    income_key = f"incomeStatementHistory{suffix}"
    balance_key = f"balanceSheetHistory{suffix}"
    cashflow_key = f"cashflowStatementHistory{suffix}"

    return {
        "income": _extract(data.get(income_key, {}).get("incomeStatementHistory", [])),
        "balance": _extract(data.get(balance_key, {}).get("balanceSheetStatements", [])),
        "cashflow": _extract(data.get(cashflow_key, {}).get("cashflowStatements", [])),
    }


def fund_flow_daily(ticker_or_code: str, secid_prefix: int = 105,
                      limit: int = 100) -> list[dict]:
    """
    东财 push2his 日级资金流 — 主力/大单/中单/小单净流入
    美股: fund_flow_daily("AAPL", 105)  # NASDAQ
          fund_flow_daily("BABA", 106)  # NYSE
    港股: fund_flow_daily("00700", 116)
    返回: [{date, main_net, big_net, mid_net, small_net, main_pct, ...}, ...]
    """
    _positive_int(limit, "limit")
    _nonempty_text(ticker_or_code, "ticker_or_code")
    if secid_prefix not in (105, 106, 107, 116):
        raise ValueError("secid_prefix must be 105, 106, 107 or 116.")
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": f"{secid_prefix}.{ticker_or_code}",
        "klt": 101,
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "lmt": limit,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    d = r.json()
    data = d.get("data")
    if not data or not data.get("klines"):
        return []

    result = []
    for line in data["klines"]:
        parts = line.split(",")
        # f51=日期, f52=主力净流入, f53=小单净流入, f54=中单净流入, f55=大单净流入, f56=超大单净流入
        result.append({
            "date": parts[0],
            "main_net": float(parts[1]),       # 主力净流入（元）
            "small_net": float(parts[2]),       # 小单净流入
            "mid_net": float(parts[3]),         # 中单净流入
            "big_net": float(parts[4]),         # 大单净流入
            "super_big_net": float(parts[5]),   # 超大单净流入
            "main_pct": float(parts[6]) if len(parts) > 6 and parts[6] else 0,  # 主力净占比%
        })
    return result


# 依赖「官方源统一出口」的 official_get / assert_us_ticker

CBOE_BASE = "https://cdn.cboe.com/api/global/delayed_quotes"
# OCC 合约代码: 标的 + YYMMDD + C/P + 8位行权价(千分之一美元)
# root 允许含数字：拆股/分拆等公司行为会产生调整后合约（如 NVDA1、BRKB1）。
# 后面全是定宽组（6+1+8=15 字符），正则回溯能正确对齐，标准合约解析结果不变。
_OSI = re.compile(r"^(?P<root>[A-Z][A-Z0-9]*)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})"
                  r"(?P<cp>[CP])(?P<strike>\d{8})$")


def parse_osi(symbol: str) -> dict:
    """解析 OCC 合约代码 → {expiry, type, strike}；无法解析返回 {}"""
    m = _OSI.match(symbol)
    if not m:
        return {}
    g = m.groupdict()
    return {"expiry": f"20{g['y']}-{g['m']}-{g['d']}",
            "type": "call" if g["cp"] == "C" else "put",
            "strike": int(g["strike"]) / 1000.0}


def options_chain_cboe(ticker: str) -> dict:
    """
    CBOE 官方延时期权全链（仅美股）。
    返回 {"ticker","timestamp","spot","contracts":[{symbol,expiry,type,strike,bid,ask,
          volume,open_interest,iv,delta,gamma,vega,theta,rho,last_trade_price}]}
    """
    ticker = assert_us_ticker(ticker)
    raw = official_get(f"{CBOE_BASE}/options/{ticker}.json", as_json=True)
    data = raw.get("data") or {}
    contracts = []
    for o in data.get("options") or []:
        meta = parse_osi(o.get("option", ""))
        if not meta:
            continue
        contracts.append({
            "symbol": o["option"], **meta,
            "bid": o.get("bid"), "ask": o.get("ask"),
            "volume": _num(o.get("volume")),
            "open_interest": _num(o.get("open_interest")),
            "iv": o.get("iv"), "delta": o.get("delta"), "gamma": o.get("gamma"),
            "vega": o.get("vega"), "theta": o.get("theta"), "rho": o.get("rho"),
            "last_trade_price": o.get("last_trade_price"),
        })
    if not contracts:
        raise DataNotAvailable(f"{ticker} 未返回任何期权合约 —— 该标的可能无期权，"
                               f"或不在 CBOE 覆盖范围（CBOE 仅覆盖美股）")
    return {"ticker": ticker, "timestamp": raw.get("timestamp"),
            "spot": data.get("current_price"), "contracts": contracts}


try:
    from zoneinfo import ZoneInfo
    _ET_TZ = ZoneInfo("America/New_York")
except Exception:      # Windows 上 zoneinfo 可能缺 tzdata
    _ET_TZ = None


def _et_today() -> str:
    """
    美东今日 YYYY-MM-DD，用于 0DTE 判定。

    ⚠️ 必须区分 EDT(UTC-4) 与 EST(UTC-5)：硬编码 UTC-4 会让冬令时
    UTC 04:00–05:00 这一小时算成次日，导致 0DTE 选错到期日。
    """
    now = datetime.now(timezone.utc)
    if _ET_TZ is not None:
        return now.astimezone(_ET_TZ).strftime("%Y-%m-%d")
    # 无 tzdata 时的回退：按美国 DST 规则（3月第2个周日 ~ 11月第1个周日）自算
    y = now.year
    # 美国 DST 在**当地时间 2:00** 切换，换算成 UTC：
    #   开始 = 3月第2个周日 02:00 EST = 07:00 UTC
    #   结束 = 11月第1个周日 02:00 EDT = 06:00 UTC
    # 用 00:00 UTC 当切换点会在切换日凌晨那几小时取错偏移。
    mar8 = datetime(y, 3, 8, tzinfo=timezone.utc)
    dst_start = (mar8 + timedelta(days=(6 - mar8.weekday()) % 7)
                 ).replace(hour=7)
    nov1 = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = (nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
               ).replace(hour=6)
    offset = 4 if dst_start <= now < dst_end else 5
    return (now - timedelta(hours=offset)).strftime("%Y-%m-%d")


def filter_expiry(chain: dict, expiry: str = None, dte_max: int = None) -> list[dict]:
    """按到期日筛选。expiry='0DTE' 取当日到期；dte_max 取 N 天内到期"""
    if expiry is not None and expiry != "0DTE":
        _date(expiry, "expiry")
    if dte_max is not None and (not isinstance(dte_max, int) or isinstance(dte_max, bool) or dte_max < 0):
        raise ValueError("dte_max must be a nonnegative integer.")
    cs = chain["contracts"]
    if expiry == "0DTE":
        return [c for c in cs if c["expiry"] == _et_today()]
    if expiry:
        return [c for c in cs if c["expiry"] == expiry]
    if dte_max is not None:
        today = datetime.strptime(_et_today(), "%Y-%m-%d")
        return [c for c in cs
                if 0 <= (datetime.strptime(c["expiry"], "%Y-%m-%d") - today).days <= dte_max]
    return cs


def unusual_activity(contracts: list[dict], min_volume: int = 500,
                     vol_oi_min: float = 1.0) -> list[dict]:
    """Screen high volume relative to reported OI.

    The ratio cannot establish opening/closing trades or trade direction.
    Missing or zero OI gives an undefined ratio and is excluded.
    """
    _positive_int(min_volume, "min_volume")
    if _num(vol_oi_min) is None or vol_oi_min < 0:
        raise ValueError("vol_oi_min must be a finite nonnegative number.")
    result = []
    for contract in contracts:
        volume = _num(contract.get("volume"))
        interest = _num(contract.get("open_interest"))
        if volume is None or interest is None or interest <= 0 or volume < min_volume:
            continue
        ratio = volume / interest
        if ratio >= vol_oi_min:
            result.append({**contract, "vol_oi_ratio": ratio})
    return sorted(result, key=lambda item: -item["volume"])


def chain_summary(contracts: list[dict]) -> dict:
    """Aggregate contract activity. The delta value is a volume proxy.

    volume_weighted_delta_proxy_shares assumes a standard 100-share multiplier;
    it is neither directional trade flow nor an investor/dealer net exposure.
    Adjusted contracts may use a different multiplier not supplied here.
    Missing volumes/OI/IV/delta are excluded and counted.
    """
    calls = [c for c in contracts if c.get("type") == "call"]
    puts = [c for c in contracts if c.get("type") == "put"]
    def total(rows, field):
        values = [_num(c.get(field)) for c in rows]
        present = [v for v in values if v is not None]
        return sum(present) if present else None
    cv, pv = total(calls, "volume"), total(puts, "volume")
    coi, poi = total(calls, "open_interest"), total(puts, "open_interest")
    traded = [c for c in contracts if (_num(c.get("volume")) or 0) > 0]
    iv_rows = [c for c in traded if _num(c.get("iv")) is not None]
    iv_volume = sum(_num(c["volume"]) for c in iv_rows)
    vwiv = (sum(_num(c["iv"]) * _num(c["volume"]) for c in iv_rows) / iv_volume
            if iv_volume else None)
    delta_rows = [c for c in contracts if _num(c.get("delta")) is not None
                  and _num(c.get("volume")) is not None]
    delta_proxy = (sum(_num(c["delta"]) * _num(c["volume"]) * 100 for c in delta_rows)
                   if delta_rows else None)
    return {
        "call_volume": cv, "put_volume": pv,
        "put_call_volume_ratio": pv / cv if cv and pv is not None else None,
        "call_oi": coi, "put_oi": poi,
        "put_call_oi_ratio": poi / coi if coi and poi is not None else None,
        "volume_weighted_iv": vwiv,
        "volume_weighted_delta_proxy_shares": delta_proxy,
        "delta_proxy_contract_multiplier": 100,
        "delta_proxy_note": "Volume proxy; trade direction and position ownership are unknown.",
        "contracts_total": len(contracts), "contracts_traded": len(traded),
        "contracts_missing_volume": sum(_num(c.get("volume")) is None for c in contracts),
        "contracts_missing_open_interest": sum(_num(c.get("open_interest")) is None for c in contracts),
        "contracts_missing_iv": sum(_num(c.get("iv")) is None for c in traded),
        "contracts_missing_delta": sum(_num(c.get("delta")) is None for c in contracts),
    }


def cboe_quote(ticker: str) -> dict:
    """CBOE 个股快照（含现价，可与期权链配合定 ATM）"""
    return official_get(f"{CBOE_BASE}/quotes/{assert_us_ticker(ticker)}.json",
                        as_json=True)["data"]


def options_chain(symbol: str, expiration: int = None) -> dict:
    """
    Yahoo 期权链 — calls + puts 完整数据（仅美股）
    symbol: "AAPL", "TSLA" 等美股 ticker
    港股代码会在发起请求前被拒绝。
    expiration: Unix timestamp (不传则返回最近到期日 + 所有到期日列表)
    返回: {"expiration_dates": [...], "calls": [...], "puts": [...]}
    """
    symbol = assert_us_ticker(symbol)
    if expiration is not None:
        _positive_int(expiration, "expiration")
    s = get_yahoo_session()
    params = {"crumb": s._crumb}
    if expiration:
        params["date"] = expiration

    r = s.get(f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}",
              params=params, timeout=15)
    r.raise_for_status()

    payload = r.json().get("optionChain") or {}
    if payload.get("error"):
        raise ProviderError("Yahoo options returned a provider error.")
    results = payload.get("result") or []
    oc = results[0] if results else {}

    exp_dates = oc.get("expirationDates", [])
    options = oc.get("options", [{}])[0] if oc.get("options") else {}

    def _parse_options(opts):
        result = []
        for o in opts:
            def _val(key):
                v = o.get(key, {})
                return v.get("raw") if isinstance(v, dict) else v
            result.append({
                "strike": _val("strike"),
                "last_price": _val("lastPrice"),
                "bid": _val("bid"),
                "ask": _val("ask"),
                "volume": _val("volume"),
                "open_interest": _val("openInterest"),
                "implied_volatility": _val("impliedVolatility"),
                "in_the_money": o.get("inTheMoney"),
                "expiration": o.get("expiration", {}).get("fmt") if isinstance(o.get("expiration"), dict) else None,
                "contract_symbol": o.get("contractSymbol"),
            })
        return result

    return {
        "expiration_dates": exp_dates,  # Unix timestamps, 可依次传入获取各期
        "calls": _parse_options(options.get("calls", [])),
        "puts": _parse_options(options.get("puts", [])),
        "underlying_price": oc.get("quote", {}).get("regularMarketPrice"),
    }


# SEC 请求统一使用 official_get（SEC_CONTACT、User-Agent 与限速在上方配置）。

def sec_filings(cik: str, form_type: str = None) -> dict:
    """
    SEC EDGAR Filing 列表
    cik: CIK号（10位补零），如 "0000320193" (Apple)
         可通过 ticker_to_cik() 从 ticker 转换
    form_type: 筛选类型，如 "10-K", "10-Q", "8-K"（不传返回全部）
    返回: {"company_name": ..., "filings": [{form, date, accession_number, primary_document}, ...]}
    """
    cik = _cik(cik)
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = official_get(url, timeout=15, as_json=True)
    recent = data.get("filings", {}).get("recent", {})

    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accessions = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    filings = []
    for i in range(len(forms)):
        if form_type and forms[i] != form_type:
            continue
        filings.append({
            "form": forms[i],
            "date": dates[i],
            "accession_number": accessions[i],
            "primary_document": primary_docs[i] if i < len(primary_docs) else "",
            "description": descriptions[i] if i < len(descriptions) else "",
            "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accessions[i].replace('-', '')}/{primary_docs[i]}" if i < len(primary_docs) and primary_docs[i] else "",
        })

    return {
        "company_name": data.get("name"),
        "cik": cik,
        "ticker": data.get("tickers", [""])[0] if data.get("tickers") else "",
        "filings": filings[:50],  # 最近50条
    }


def sec_xbrl_facts(cik: str, metrics: list[str] = None) -> dict:
    """SEC GAAP facts; records retain units, period and filing identities.

    Results include 10-K/10-Q facts in every unit, sorted newest filing first.
    A fiscal quarter label alone is insufficient to distinguish quarter/YTD/
    annual durations; use start/end/frame and never merge solely on fp/end.
    """
    cik = _cik(cik)
    if metrics is not None and (
            not isinstance(metrics, list) or any(not isinstance(m, str) or not m for m in metrics)):
        raise ValueError("metrics must be a list of nonempty XBRL names.")
    facts = official_get(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
        timeout=15, as_json=True)
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    if not metrics:
        available = [{"name": name, "label": metric.get("label", name),
                      "units": list((metric.get("units") or {}).keys())}
                     for name, metric in gaap.items()]
        return {"company": facts.get("entityName"),
                "total_metrics": len(available), "available_metrics": available}
    result = {}
    fields = ("start", "end", "val", "form", "frame", "accn", "fy", "fp", "filed")
    for name in metrics:
        rows = []
        for unit, entries in (gaap.get(name, {}).get("units") or {}).items():
            for entry in entries:
                if entry.get("form") in ("10-K", "10-Q"):
                    rows.append({**{field: entry.get(field) for field in fields},
                                 "unit": unit})
        result[name] = sorted(rows, key=lambda row: (
            row.get("filed") or "", row.get("end") or "",
            row.get("start") or "", row.get("accn") or ""), reverse=True)
    return {"company": facts.get("entityName"), "metrics": result}


def stock_search(keyword: str, count: int = 10) -> list[dict]:
    """
    东财股票搜索 — 支持中英文，返回代码+市场+中文名
    keyword: "AAPL" / "苹果" / "Tencent" / "00700" / "特斯拉"
    返回: [{code, name, mkt_num, market_name, security_type}, ...]

    mkt_num 即 push2/push2his 的 secid 前缀:
    105=NASDAQ, 106=NYSE, 107=美股ETF, 116=港股
    """
    _nonempty_text(keyword, "keyword")
    _positive_int(count, "count")
    url = "https://searchapi.eastmoney.com/api/suggest/get"
    params = {
        "input": keyword,
        "type": 14,  # 14=全球市场
        "token": "D43BF722C8E33BDC906FB84D85E326E8",
        "count": count,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    d = r.json()

    suggestions = d.get("QuotationCodeTable", {}).get("Data", [])
    result = []
    for s in suggestions:
        mkt = s.get("MktNum", "")
        # 只保留美股和港股
        if str(mkt) not in ("105", "106", "107", "116"):
            continue

        market_map = {"105": "NASDAQ", "106": "NYSE", "107": "US_OTHER", "116": "HK"}
        result.append({
            "code": s.get("Code"),
            "name": s.get("Name"),
            "mkt_num": int(mkt),
            "market_name": market_map.get(str(mkt), str(mkt)),
            "security_type": s.get("SecurityTypeName"),
        })
    return result


def stock_news(keyword: str, count: int = 10) -> list[dict]:
    """
    Yahoo Finance 新闻搜索
    keyword: 股票代码或关键词，如 "AAPL", "Tesla", "0700.HK"
    返回: [{title, publisher, link, publish_time, thumbnail}, ...]
    注意: 需要先获取 Yahoo cookie 才能调用，否则返回 400
    """
    _nonempty_text(keyword, "keyword")
    _positive_int(count, "count")
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    _bootstrap_yahoo(s)

    url = "https://query2.finance.yahoo.com/v1/finance/search"
    params = {"q": keyword, "quotesCount": 0, "newsCount": count}
    r = s.get(url, params=params, timeout=10)
    r.raise_for_status()

    news = r.json().get("news", [])
    result = []
    for n in news:
        result.append({
            "title": n.get("title"),
            "publisher": n.get("publisher"),
            "link": n.get("link"),
            "publish_time": n.get("providerPublishTime"),
            "thumbnail": n.get("thumbnail", {}).get("resolutions", [{}])[0].get("url") if n.get("thumbnail") else None,
        })
    return result


_cik_cache = None

def ticker_to_cik(ticker: str) -> dict:
    """
    SEC EDGAR ticker → CIK 映射
    ticker: 如 "AAPL", "TSLA", "MSFT"
    返回: {"ticker": "AAPL", "cik": "0000320193", "company": "Apple Inc."}

    首次调用下载完整映射表(~10KB JSON, 10000+公司)并缓存。
    """
    ticker = assert_us_ticker(ticker)
    global _cik_cache
    if not _cik_cache:
        _cik_cache = official_get("https://www.sec.gov/files/company_tickers.json", timeout=15, as_json=True)

    ticker_upper = ticker.upper()
    for _, v in _cik_cache.items():
        if v.get("ticker") == ticker_upper:
            cik_str = str(v["cik_str"]).zfill(10)
            return {
                "ticker": ticker_upper,
                "cik": cik_str,
                "company": v.get("title"),
            }
    return {}


def market_stock_list(market: str = "us_nasdaq", sort_field: str = "f3",
                       sort_desc: bool = True, page: int = 1,
                       page_size: int = 20) -> dict:
    """
    东财 push2 全市场股票列表 — 涨跌幅/成交量/成交额排名
    market: "us_nasdaq" (m:105), "us_nyse" (m:106), "hk" (m:116)
    sort_field: 排序字段
      f3=涨跌幅, f5=成交量, f6=成交额, f2=最新价, f7=振幅, f15=最高, f16=最低
    sort_desc: True=降序(默认), False=升序
    page/page_size: 分页（默认第1页，每页20条）
    返回股票原始价格及来源 precision；未经确认的缩放规则不推导真实价格。

    典型用途:
    - 今日涨幅 TOP 20: market_stock_list("us_nasdaq", "f3", True)
    - 今日跌幅 TOP 20: market_stock_list("us_nasdaq", "f3", False)
    - 成交量 TOP 20: market_stock_list("hk", "f5", True)
    - 遍历全市场: 循环 page=1..N, 每页100条做筛选
    """
    _positive_int(page, "page")
    _positive_int(page_size, "page_size")
    market_map = {"us_nasdaq": "m:105", "us_nyse": "m:106", "us_etf": "m:107", "hk": "m:116"}
    fs = market_map.get(market, market)

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "fs": fs,
        "fields": "f2,f3,f4,f5,f6,f7,f12,f14,f15,f16,f17,f18,f152",
        "pn": page,
        "pz": page_size,
        "fid": sort_field,
        "po": 1 if sort_desc else 0,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    d = r.json()
    data = d.get("data") or {}

    total = data.get("total", 0)
    diff = data.get("diff", [])
    # 东财 push2 的 diff 有时是 list、有时是按序号为键的 dict（如 {"0":{...},"1":{...}}）。
    # 直接 for item in diff 遇到 dict 会拿到字符串键 → AttributeError，统一成列表。
    if isinstance(diff, dict):
        diff = list(diff.values())

    stocks = []
    for item in diff:
        stocks.append({
            "code": item.get("f12"),         # 股票代码
            "name": item.get("f14"),         # 中文名
            "precision": _int_or_none(item.get("f152")),
            "raw_price": _num(item.get("f2")),
            "raw_change_amount": _num(item.get("f4")),
            "raw_high": _num(item.get("f15")),
            "raw_low": _num(item.get("f16")),
            "raw_open": _num(item.get("f17")),
            "raw_prev_close": _num(item.get("f18")),
            "price": None, "change_amount": None,
            "high": None, "low": None, "open": None, "prev_close": None,
            "normalization_note": (
                "Raw clist price scaling has not been verified; use stock_quote_eastmoney "
                "for normalized quotes. precision is retained without assuming a divisor."),
            "raw_change_pct": _num(item.get("f3")),
            "change_pct": _num(item["f3"]) / 100 if _num(item.get("f3")) is not None else None,
            "volume": _int_or_none(item.get("f5")),
            "amount": _num(item.get("f6")),
            "raw_amplitude": _num(item.get("f7")),
            "amplitude": _num(item["f7"]) / 100 if _num(item.get("f7")) is not None else None,
        })

    return {"total": total, "stocks": stocks}


# 依赖「官方源统一出口」的 official_get


def _recent_weekdays(days_back: int = 7) -> list[str]:
    d, out = datetime.strptime(_et_today(), "%Y-%m-%d"), []
    while len(out) < days_back:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return out


def short_volume_all(date: str = None, market: str = "CNMS") -> dict:
    """
    FINRA 全市场每日空头成交量。
    market: CNMS(合并全市场) / FNSQ(Nasdaq) / FNYX(NYSE) / FNRA(TRF)
    date: YYYYMMDD；不传则自动回退找最近有数据的交易日
    返回 {"date","market","count","data":{SYMBOL:{short,short_exempt,total,ratio}}}
    """
    if date is not None:
        _date(date, "date", "%Y%m%d")
    if market not in ("CNMS", "FNSQ", "FNYX", "FNRA"):
        raise ValueError("Unknown FINRA market.")
    for d in ([date] if date else _recent_weekdays(7)):
        try:
            raw = official_get(
                f"https://cdn.finra.org/equity/regsho/daily/{market}shvol{d}.txt")
        except DataNotAvailable:
            continue   # 该日无文件（非交易日/尚未发布），回退下一日
        # 其余异常（网络/限流/配置）直接抛出，不伪装成「没数据」
        rows = {}
        for line in raw.splitlines()[1:]:
            p = line.split("|")
            if len(p) < 5 or not p[1]:
                continue
            try:
                sv, se, tv = float(p[2]), float(p[3]), float(p[4])
            except ValueError:
                continue
            rows[p[1]] = {"short": sv, "short_exempt": se, "total": tv,
                          "ratio": round(sv / tv, 4) if tv else None}
        if rows:
            return {"date": d, "market": market, "count": len(rows), "data": rows}
    # 抛 DataNotAvailable 而非 RuntimeError：指定日期无数据时，
    # 调用方（如 short_volume_symbol 的多日循环）要能捕获并跳过这一天
    raise DataNotAvailable(f"未找到 {market} "
                           f"{'该日' if date else '近 7 个工作日'}的 Reg SHO 数据")


def short_volume_symbol(symbol: str, days: int = 5, market: str = "CNMS") -> list[dict]:
    """单只股票近 N 个交易日的空头成交占比时间序列"""
    symbol = assert_us_ticker(symbol)
    _positive_int(days, "days")
    out = []
    for d in _recent_weekdays(days * 2):
        if len(out) >= days:
            break
        try:
            snap = short_volume_all(date=d, market=market)
        except DataNotAvailable:
            continue
        rec = snap["data"].get(symbol.upper())
        if rec:
            out.append({"date": d, **rec})
    return out


def short_volume_ranking(snapshot: dict, min_total: float = 1_000_000,
                         top: int = 20) -> list[dict]:
    """空头占比排行（先按最小成交量过滤，避免小票噪音）"""
    rows = [{"symbol": s, **v} for s, v in snapshot["data"].items()
            if v["total"] >= min_total and v["ratio"] is not None]
    return sorted(rows, key=lambda x: -x["ratio"])[:top]



_FORM_LABEL = {"4": "内部人交易", "8-K": "重大事件", "13F-HR": "机构持仓",
               "144": "限售股拟出售", "10-K": "年报", "10-Q": "季报",
               "SC 13D": "举牌(主动)", "SC 13G": "举牌(被动)", "S-1": "IPO注册"}


def daily_filings(date: str = None, forms: list[str] = None) -> dict:
    """
    EDGAR 每日申报流。date=YYYYMMDD，不传自动回退找最近有数据的日子。
    forms: 只保留这些表单类型，如 ["4","8-K","13F-HR"]；None=全部
    返回 {"date","total","by_form":{...},"filings":[{form,form_label,company,cik,date,url}]}
    """
    if date is not None:
        _date(date, "date", "%Y%m%d")
    if forms is not None and (not isinstance(forms, list) or any(not isinstance(f, str) for f in forms)):
        raise ValueError("forms must be a list of form names.")
    for d in ([date] if date else _recent_weekdays(7)):
        dt = datetime.strptime(d, "%Y%m%d")
        url = (f"https://www.sec.gov/Archives/edgar/daily-index/"
               f"{dt.year}/QTR{(dt.month - 1) // 3 + 1}/form.{d}.idx")
        try:
            raw = official_get(url)
        except DataNotAvailable:
            continue   # 该日无索引文件，回退下一日
        # 配置错误（SEC_CONTACT 未配置）与网络错误在此直接抛出——
        # 否则会被 7 次循环吞掉，最终误报成「未找到 EDGAR 每日索引」
        lines = raw.splitlines()
        start = next((i + 1 for i, L in enumerate(lines) if L.startswith("---")), 11)
        filings, by_form = [], {}
        for L in lines[start:]:
            # ⚠️ 不要按固定列宽切片：实测该 .idx 的列宽会随表单类型/公司名长度漂移
            # （2026-09-18 复核 form.20260916.idx：日期实际始于第 91 列，而非假设的 86 列），
            # 固定切片把日期截成 "2026091"、把文件路径整体错位，产出的 url 直接不可用。
            # 各列之间以 2 个以上空格分隔，而表单类型（如 "1-A POS"）与公司名内部只有单个空格，
            # 因此按「空白段落」切分是稳定且自适应的。
            parts = re.split(r"\s{2,}", L.strip())
            if len(parts) < 5:
                continue
            form, company, cik, filed, path = parts[0], parts[1], parts[2], parts[3], parts[4]
            if not form:
                continue
            by_form[form] = by_form.get(form, 0) + 1
            if forms and form not in forms:
                continue
            filings.append({"form": form, "form_label": _FORM_LABEL.get(form, ""),
                            "company": company, "cik": cik, "date": filed,
                            "url": f"https://www.sec.gov/Archives/{path}" if path else None})
        if by_form:
            return {"date": d, "total": sum(by_form.values()),
                    "by_form": dict(sorted(by_form.items(), key=lambda x: -x[1])),
                    "filings": filings}
    raise DataNotAvailable("未找到近 7 个工作日的 EDGAR 每日索引")


def fulltext_search(query: str, forms: str = None, date_from: str = None,
                    date_to: str = None, limit: int = 20) -> dict:
    """
    query: 加引号为精确短语，如 '"HBM4"'
    forms: "8-K" / "10-K" 等；date_from/to: YYYY-MM-DD
    """
    _nonempty_text(query, "query")
    _positive_int(limit, "limit")
    if date_from is not None:
        _date(date_from, "date_from")
    if date_to is not None:
        _date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must not be after date_to.")
    p = {"q": query, "from": 0, "size": limit}
    if forms:
        p["forms"] = forms
    if date_from:
        p["dateRange"], p["startdt"] = "custom", date_from
    if date_to:
        p["dateRange"], p["enddt"] = "custom", date_to
    j = official_get("https://efts.sec.gov/LATEST/search-index", params=p, as_json=True)
    hits = (j.get("hits") or {}).get("hits") or []
    return {"total": ((j.get("hits") or {}).get("total") or {}).get("value", 0),
            "results": [{"form": (h.get("_source") or {}).get("root_form"),
                         "company": ((h.get("_source") or {}).get("display_names") or [None])[0],
                         "filed": (h.get("_source") or {}).get("file_date"),
                         "id": h.get("_id")} for h in hits]}


XBRL_TAGS = {
    "营业收入": "Revenues",
    "营业收入(合同)": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "净利润": "NetIncomeLoss",
    "研发费用": "ResearchAndDevelopmentExpense",
    "毛利": "GrossProfit",
    "经营利润": "OperatingIncomeLoss",
    "总资产": "Assets",
    "股东权益": "StockholdersEquity",
    "现金及等价物": "CashAndCashEquivalentsAtCarryingValue",
    "经营现金流": "NetCashProvidedByUsedInOperatingActivities",
    "资本开支": "PaymentsToAcquirePropertyPlantAndEquipment",
    "长期负债": "LongTermDebtNoncurrent",
    "稀释EPS": "EarningsPerShareDiluted",
}

# ⚠️ 时点(instant)概念 —— 资产负债表科目描述的是「某一时刻的余额」，而非一段期间的发生额。
# SEC Frames 对这类概念**要求周期带 I 后缀**，且**没有纯年度周期**：
#   Assets/CY2025Q1  -> 404      Assets/CY2025Q1I -> 200 (5643 家)
#   Assets/CY2024    -> 404      Assets/CY2024Q4I -> 200 (6248 家)
# 期间(duration)概念(营收/净利/现金流等)则相反，用 CY2025Q1 / CY2024。
# 以上均为 2026-07-26 逐个实测结果。
_INSTANT_TAGS = {
    "Assets",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "LongTermDebtNoncurrent",
}


def _frame_period(year: int, quarter, instant: bool) -> str:
    """时点概念没有纯年度周期，年度请求落到 Q4I。"""
    if instant:
        return f"CY{year}Q{quarter}I" if quarter else f"CY{year}Q4I"
    return f"CY{year}Q{quarter}" if quarter else f"CY{year}"


def market_frame(tag: str, year: int, quarter: int = None, unit: str = "USD",
                 instant=None) -> dict:
    """
    全市场横截面。tag 可用 XBRL_TAGS 的中文键或原始 XBRL 标签。
    quarter: 1-4 季度；None 为年度
    instant: 是否为时点(资产负债表)概念。None=自动判定。

    自动判定逻辑：先按 _INSTANT_TAGS 猜一种周期形式，404 再换另一种重试。
    这样**任意原始 XBRL 标签**（Liabilities / InventoryNet / AssetsCurrent …）
    都能正确取到数，而不必把所有时点概念都枚举进 _INSTANT_TAGS。
    已知类型时显式传 instant=True/False 可省掉一次探测请求。
    """
    _nonempty_text(tag, "tag")
    _positive_int(year, "year")
    if quarter is not None and (not isinstance(quarter, int) or isinstance(quarter, bool) or quarter not in (1, 2, 3, 4)):
        raise ValueError("quarter must be 1, 2, 3, 4 or null.")
    if instant is not None and not isinstance(instant, bool):
        raise ValueError("instant must be true, false or null.")
    tag = XBRL_TAGS.get(tag, tag)
    guess = (tag in _INSTANT_TAGS) if instant is None else instant
    attempts = [guess] if instant is not None else [guess, not guess]

    last_err = None
    for is_instant in attempts:
        period = _frame_period(year, quarter, is_instant)
        try:
            j = official_get(
                f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit}/{period}.json",
                timeout=45, as_json=True)
        except DataNotAvailable as e:      # 周期形式不对时 SEC 返回 404
            last_err = e
            continue
        rows = [{"cik": d.get("cik"), "entity": d.get("entityName"),
                 "value": d.get("val"), "end": d.get("end")} for d in j.get("data", [])]
        return {"tag": tag, "period": period, "unit": unit,
                "instant": is_instant, "count": len(rows), "data": rows}
    raise last_err


def frame_ranking(frame: dict, top: int = 20, ascending: bool = False) -> list[dict]:
    return sorted(frame["data"], key=lambda x: x["value"], reverse=not ascending)[:top]


def frame_screen(frame: dict, min_value: float = None,
                 max_value: float = None) -> list[dict]:
    """按数值区间筛选全市场公司"""
    out = frame["data"]
    if min_value is not None:
        out = [r for r in out if r["value"] >= min_value]
    if max_value is not None:
        out = [r for r in out if r["value"] <= max_value]
    return out




def treasury_yield_curve(year: int = None) -> list[dict]:
    """美国国债收益率曲线（每日，1M~30Y）。政府数据，S 级。返回 [0] 为最新一日"""
    if year is not None:
        _positive_int(year, "year")
    year = year or datetime.now(timezone.utc).year
    url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
           f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
           f"&field_tdr_date_value={year}&page&_format=csv")
    return list(csv.DictReader(io.StringIO(official_get(url))))


def cftc_cot(limit: int = 20, market_contains: str = None) -> list[dict]:
    """CFTC 持仓报告(COT)。政府数据，S 级"""
    _positive_int(limit, "limit")
    q = {"$limit": limit, "$order": "report_date_as_yyyy_mm_dd DESC"}
    if market_contains:
        q["$where"] = f"upper(contract_market_name) like upper('%{market_contains.replace(chr(39), chr(39) * 2)}%')"
    return official_get("https://publicreporting.cftc.gov/resource/6dca-aqww.json",
                        params=q, as_json=True)


def earnings_calendar(date: str = None) -> dict:
    """Nasdaq 财报日历。date=YYYY-MM-DD，不传取今天"""
    if date is not None:
        _date(date, "date")
    date = date or _et_today()
    j = official_get("https://api.nasdaq.com/api/calendar/earnings",
                     params={"date": date}, headers={"Accept": "application/json"},
                     as_json=True)
    rows = ((j.get("data") or {}).get("rows")) or []
    return {"date": date, "count": len(rows),
            "rows": [{"symbol": r.get("symbol"), "name": r.get("name"),
                      "time": r.get("time"), "eps_forecast": r.get("epsForecast"),
                      "market_cap": r.get("marketCap")} for r in rows]}


# Explicit data/analysis entry points. Low-level HTTP and session helpers are
# importable library functions, but are intentionally unavailable to the CLI.
FUNCTION_SOURCES = {
    "yahoo_quote_summary": "Yahoo Finance",
    "us_stock_quote_sina": "Sina",
    "us_stock_quote_tencent": "Tencent",
    "hk_stock_quote_tencent": "Tencent",
    "hk_stock_quote_sina": "Sina",
    "stock_quote_eastmoney": "Eastmoney push2",
    "us_stock_kline_sina": "Sina",
    "stock_kline_yahoo": "Yahoo Finance",
    "calc_ma": "local",
    "calc_macd": "local",
    "calc_rsi": "local",
    "calc_kdj": "local",
    "calc_boll": "local",
    "financial_statements_eastmoney": "Eastmoney datacenter",
    "key_indicators_eastmoney": "Eastmoney datacenter",
    "key_statistics": "Yahoo Finance",
    "analyst_estimates": "Yahoo Finance",
    "institutional_holders": "Yahoo Finance",
    "financial_statements_yahoo": "Yahoo Finance",
    "fund_flow_daily": "Eastmoney push2his",
    "parse_osi": "local",
    "options_chain_cboe": "CBOE",
    "filter_expiry": "local",
    "unusual_activity": "local",
    "chain_summary": "local",
    "cboe_quote": "CBOE",
    "options_chain": "Yahoo Finance",
    "sec_filings": "SEC EDGAR",
    "sec_xbrl_facts": "SEC EDGAR",
    "stock_search": "Eastmoney search",
    "stock_news": "Yahoo Finance",
    "ticker_to_cik": "SEC EDGAR",
    "market_stock_list": "Eastmoney push2",
    "short_volume_all": "FINRA Reg SHO",
    "short_volume_symbol": "FINRA Reg SHO",
    "short_volume_ranking": "local",
    "daily_filings": "SEC EDGAR",
    "fulltext_search": "SEC EDGAR",
    "market_frame": "SEC EDGAR",
    "frame_ranking": "local",
    "frame_screen": "local",
    "treasury_yield_curve": "US Treasury",
    "cftc_cot": "CFTC",
    "earnings_calendar": "Nasdaq",
}
FUNCTIONS = {name: globals()[name] for name in FUNCTION_SOURCES}


def _function_list() -> list[dict]:
    result = []
    for name, function in FUNCTIONS.items():
        parameters = []
        for parameter in inspect.signature(function).parameters.values():
            item = {
                "name": parameter.name,
                "required": parameter.default is inspect.Parameter.empty,
            }
            if parameter.annotation is not inspect.Parameter.empty:
                item["type"] = str(parameter.annotation)
            if parameter.default is not inspect.Parameter.empty:
                item["default"] = parameter.default
            parameters.append(item)
        result.append({"function": name, "source": FUNCTION_SOURCES[name],
                       "parameters": parameters})
    return result


def _validate_parameters(function, parameters: dict) -> None:
    """Check JSON argument types and function binding before any network I/O."""
    signature = inspect.signature(function)
    try:
        bound = signature.bind(**parameters)
    except TypeError as exc:
        raise ValueError(f"Invalid function parameters: {exc}") from exc
    for name, value in bound.arguments.items():
        parameter = signature.parameters[name]
        if value is None and parameter.default is None:
            continue
        annotation = str(parameter.annotation)
        kind = annotation.split("[", 1)[0]
        if kind == "str":
            valid = isinstance(value, str)
        elif kind == "int":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "float":
            valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value))
        elif kind == "bool":
            valid = isinstance(value, bool)
        elif kind == "dict":
            valid = isinstance(value, dict)
        elif kind == "list":
            valid = isinstance(value, list)
        else:
            continue
        if not valid:
            raise ValueError(f"Parameter {name} must have type {annotation}.")


def _reject_json_constant(value: str):
    raise ValueError(f"Nonfinite JSON constant {value} is not supported.")


def _load_parameters(args) -> dict:
    if args.params_json is not None:
        text = args.params_json
    elif args.params_file is not None:
        text = (sys.stdin.read() if args.params_file == "-"
                else Path(args.params_file).read_text(encoding="utf-8-sig"))
    else:
        return {}
    value = json.loads(text, parse_constant=_reject_json_constant)
    if not isinstance(value, dict):
        raise ValueError("Parameters must be a JSON object.")
    return value


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def _error_message(error: Exception) -> str:
    # The contact identity must never appear in structured error output.
    message = str(error)
    contact = os.environ.get("SEC_CONTACT", "").strip()
    if contact:
        message = message.replace(contact, "[redacted]")
    crumb = getattr(_yahoo_session, "_crumb", None)
    if crumb:
        message = message.replace(crumb, "[redacted]")
    return message


def main(argv: list[str] | None = None) -> int:
    """Emit a single JSON envelope; return 0 on success/empty, 1 on error."""
    parser = _JsonArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--list", action="store_true", help="List allowed functions and parameters.")
    selector.add_argument("--function", help="Run a function from --list.")
    parameters = parser.add_mutually_exclusive_group()
    parameters.add_argument("--params-json", help="JSON object of keyword arguments.")
    parameters.add_argument("--params-file", help="UTF-8 JSON file, or - for stdin.")
    function_name = None
    source = None
    try:
        args = parser.parse_args(argv)
        function_name = "list" if args.list else args.function
        source = "local" if args.list else FUNCTION_SOURCES.get(function_name)
        if args.list:
            if args.params_json is not None or args.params_file is not None:
                raise ValueError("--list does not accept parameters.")
            data = _function_list()
        else:
            function = FUNCTIONS.get(function_name)
            if function is None:
                raise ValueError("Function is not in the allowed list; use --list.")
            params = _load_parameters(args)
            _validate_parameters(function, params)
            data = function(**params)
        envelope = {
            "status": "empty" if data is None or data == [] or data == {} else "success",
            "source": source, "function": function_name,
            # Retrieval/execution time, never the quote/candle/filing event time.
            "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "data": data,
        }
        output = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        exit_code = 0
    except Exception as exc:
        envelope = {
            "status": "error", "source": source, "function": function_name,
            "fetched_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "error_type": type(exc).__name__, "error_message": _error_message(exc),
        }
        output = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        exit_code = 1
    print(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
