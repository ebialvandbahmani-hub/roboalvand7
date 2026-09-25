# -*- coding: utf-8 -*-
from __future__ import annotations
import os, time, asyncio, logging
from dataclasses import dataclass, field
try:
    import aiohttp
    _HAS_AIOHTTP = True
except Exception:
    _HAS_AIOHTTP = False

log = logging.getLogger("robo7.data")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
YAHOO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
BINANCE_BASE = "https://api.binance.com"
YAHOO_BASE = "https://query1.finance.yahoo.com"
TD_BASE = "https://api.twelvedata.com"

METALS = {"XAU", "XAG", "XPT", "XPD"}
FIAT = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "CNY", "CNH", "TRY", "SEK", "NOK", "DKK", "PLN", "ZAR", "MXN", "SGD", "HKD"}
CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "TUSD", "BTC", "ETH", "BNB")

@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

@dataclass
class MarketData:
    symbol: str
    market: str
    price: float
    candles: list = field(default_factory=list)
    source: str = ""

def normalize(symbol: str) -> str:
    return symbol.upper().replace("/", "").replace("=X", "").replace(" ", "").strip()

def detect_market(symbol: str) -> str:
    s = normalize(symbol)
    if len(s) == 6 and s.isalpha():
        base, quote = s[:3], s[3:]
        if base in METALS:
            return "forex"
        if base in FIAT and quote in FIAT:
            return "forex"
    if s[:3] in METALS:
        return "forex"
    return "crypto"

def to_binance(symbol: str) -> str:
    s = normalize(symbol)
    return s if s.endswith(CRYPTO_QUOTES) else s + "USDT"

def to_yahoo(symbol: str) -> str:
    return normalize(symbol) + "=X"

def to_twelvedata(symbol: str) -> str:
    s = normalize(symbol)
    if s[:3] in METALS or (len(s) == 6 and s.isalpha()):
        return f"{s[:3]}/{s[3:]}"
    return s

class _TTLCache:
    def __init__(self, ttl: float = 60.0):
        self.ttl = ttl
        self._d = {}
    def get(self, key):
        item = self._d.get(key)
        if item and (time.time() - item[0]) < self.ttl:
            return item[1]
        return None
    def set(self, key, value):
        self._d[key] = (time.time(), value)

_cache = _TTLCache(ttl=60.0)

async def _get_json(session, url, params=None, headers=None):
    last = None
    for attempt in range(3):
        try:
            async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    return await r.json()
                last = f"HTTP {r.status}"
                if r.status in (429, 418, 503):
                    await asyncio.sleep(1.2 * (attempt + 1))
                    continue
                return None
        except Exception as e:
            last = repr(e)
            await asyncio.sleep(0.7 * (attempt + 1))
    log.warning("request failed url=%s err=%s", url, last)
    return None

async def _binance_klines(session, symbol, interval, limit):
    sym = to_binance(symbol)
    data = await _get_json(session, f"{BINANCE_BASE}/api/v3/klines",
                           {"symbol": sym, "interval": interval, "limit": limit})
    if not isinstance(data, list) or not data:
        return None, sym
    candles = [Candle(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in data]
    return candles, sym

async def _yahoo_klines(session, symbol, interval, limit):
    sym = to_yahoo(symbol)
    rng = {"1m": "1d", "5m": "5d", "15m": "5d", "30m": "1mo", "1h": "3mo", "1d": "1y", "1wk": "5y"}.get(interval, "3mo")
    data = await _get_json(session, f"{YAHOO_BASE}/v8/finance/chart/{sym}",
                           {"range": rng, "interval": interval},
                           {"User-Agent": YAHOO_UA, "Accept": "application/json"})
    try:
        res = data["chart"]["result"][0]
        meta = res.get("meta", {})
        price = meta.get("regularMarketPrice")
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
        candles = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            vol = (q.get("volume") or [None] * len(ts))[i] or 0
            candles.append(Candle(int(t) * 1000, float(o), float(h), float(l), float(c), float(vol)))
        if not candles:
            return None, sym
        if price is None:
            price = candles[-1].close
        return candles[-limit:], sym
    except Exception as e:
        log.warning("yahoo parse error %s: %r", sym, e)
        return None, sym

async def _td_klines(session, symbol, interval, limit):
    if not TWELVE_DATA_API_KEY:
        return None, to_twelvedata(symbol)
    sym = to_twelvedata(symbol)
    data = await _get_json(session, f"{TD_BASE}/time_series",
                           {"symbol": sym, "interval": interval, "outputsize": limit, "apikey": TWELVE_DATA_API_KEY})
    try:
        if not data or data.get("status") == "error" or "values" not in data:
            return None, sym
        vals = list(reversed(data["values"]))
        candles = []
        for v in vals:
            candles.append(Candle(0, float(v["open"]), float(v["high"]), float(v["low"]), float(v["close"]), float(v.get("volume") or 0)))
        return candles, sym
    except Exception as e:
        log.warning("twelvedata parse error %s: %r", sym, e)
        return None, sym

async def get_market_data(symbol: str, interval: str = "1h", limit: int = 200):
    if not _HAS_AIOHTTP:
        return None
    market = detect_market(symbol)
    key = f"{market}:{normalize(symbol)}:{interval}:{limit}"
    cached = _cache.get(key)
    if cached is not None:
        return cached
    async with aiohttp.ClientSession() as session:
        candles = None
        sym = normalize(symbol)
        source = ""
        if market == "crypto":
            candles, sym = await _binance_klines(session, symbol, interval, limit)
            source = "binance"
        if candles is None and TWELVE_DATA_API_KEY and market == "forex":
            candles, sym = await _td_klines(session, symbol, interval, limit)
            source = "twelvedata"
        if candles is None:
            candles, sym = await _yahoo_klines(session, symbol, interval, limit)
            source = "yahoo"
    if not candles:
        return None
    md = MarketData(symbol=sym, market=market, price=candles[-1].close, candles=candles, source=source)
    _cache.set(key, md)
    return md

async def get_price(symbol: str):
    md = await get_market_data(symbol, interval="1h", limit=2)
    return md.price if md else None
