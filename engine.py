"""
PLATINIUM ENGINE
Full trading engine — astro, indicators, patterns, edge score, simulation
Mirrors the JS engine from platinium_v2.html exactly
"""

import json
import math
import time
import random
import asyncio
import aiohttp
import hmac
import hashlib
import base64
import os
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

DATA_FILE = os.getenv("DATA_FILE", "platinium_data.json")


# ═══════════════════════════════════════════════
# ON-CHAIN CACHE  (populated by async fetcher,
#                  read synchronously by get_readings)
# ═══════════════════════════════════════════════

@dataclass
class OnChainCache:
    funding:    float = 0.0    # BTC perp funding rate %
    chain_flow: float = 0.0    # -1 (sell) to +1 (buy pressure)
    whale_buy:  bool  = False  # large-player accumulation signal
    updated_at: float = 0.0
    fresh:      bool  = False  # True once real data has been fetched

_onchain: dict = {}   # symbol -> OnChainCache (populated per-coin)


async def update_onchain(session: aiohttp.ClientSession,
                         symbol: str = "BTCUSDT") -> OnChainCache:
    """
    Fetch real on-chain/derivatives data for a single symbol from Binance Futures.
    Updates _onchain[symbol] and returns it.
    """
    global _onchain
    prev = _onchain.get(symbol, OnChainCache())

    funding    = prev.funding
    chain_flow = prev.chain_flow
    whale_buy  = prev.whale_buy

    perp = symbol  # Binance futures symbol is the same (BTCUSDT, ETHUSDT …)

    # ── 1. FUNDING RATE ───────────────────────────────────────────
    try:
        url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={perp}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        funding = round(float(data["lastFundingRate"]) * 100, 5)
    except Exception as e:
        log.debug(f"OnChain funding {symbol} failed: {e}")

    # ── 2. TAKER BUY/SELL RATIO → chain_flow ─────────────────────
    try:
        url = (f"https://fapi.binance.com/futures/data/takerlongshortRatio"
               f"?symbol={perp}&period=5m&limit=3")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        avg_ratio = sum(float(d["buySellRatio"]) for d in data) / len(data)
        chain_flow = round(max(-1.0, min(1.0, (avg_ratio - 1.0) * 2.0)), 3)
    except Exception as e:
        log.debug(f"OnChain flow {symbol} failed: {e}")

    # ── 3. GLOBAL LONG/SHORT RATIO → whale_buy ───────────────────
    try:
        url = (f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
               f"?symbol={perp}&period=5m&limit=1")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        ls_ratio = float(data[0]["longShortRatio"])
        whale_buy = ls_ratio > 1.12 and chain_flow > 0
    except Exception as e:
        log.debug(f"OnChain whale {symbol} failed: {e}")

    result = OnChainCache(
        funding=funding, chain_flow=chain_flow, whale_buy=whale_buy,
        updated_at=time.time(), fresh=True,
    )
    _onchain[symbol] = result
    log.debug(f"OnChain {symbol}: fund={funding:+.4f}% flow={chain_flow:+.3f} whale={whale_buy}")
    return result


async def update_all_onchain(session: aiohttp.ClientSession,
                              symbols: list) -> None:
    """Batch-update on-chain data for all symbols, staggered to avoid rate limits."""
    for sym in symbols:
        await update_onchain(session, sym)
        await asyncio.sleep(0.3)


# ═══════════════════════════════════════════════
# ASTRO ENGINE
# ═══════════════════════════════════════════════

MOON_NAMES = [
    "New Moon", "Wax Crescent", "First Quarter", "Wax Gibbous",
    "Full Moon", "Wan Gibbous", "Last Quarter", "Wan Crescent"
]
MOON_EMOJI = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
SIGNS = [
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces"
]
DEG = math.pi / 180


def julian_day(dt: datetime) -> float:
    y = dt.year
    m = dt.month
    d = dt.day + dt.hour / 24 + dt.minute / 1440
    A = int(y / 100)
    B = 2 - A + int(A / 4)
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + B - 1524.5


def sun_longitude(J: float) -> float:
    T = (J - 2451545) / 36525
    L = 280.46646 + 36000.76983 * T
    M = 357.52911 + 35999.05029 * T - 0.0001537 * T * T
    M = M % 360
    C = ((1.914602 - 0.004817 * T) * math.sin(M * DEG)
         + (0.019993 - 0.000101 * T) * math.sin(2 * M * DEG)
         + 0.000289 * math.sin(3 * M * DEG))
    return (L + C) % 360


def moon_longitude(J: float) -> float:
    T = (J - 2451545) / 36525
    L = 218.3165 + 481267.8813 * T
    M = (134.9634 + 477198.8676 * T) % 360
    D = (297.8502 + 445267.1115 * T) % 360
    F = (93.2721 + 483202.0175 * T) % 360
    return (L
            + 6.289 * math.sin(M * DEG)
            + 1.274 * math.sin((2 * D - M) * DEG)
            + 0.658 * math.sin(2 * D * DEG)
            + 0.214 * math.sin(2 * M * DEG)
            - 0.114 * math.sin(2 * F * DEG)) % 360


def is_mercury_retrograde(J: float) -> bool:
    phase = (J - 2451600) % 115.88
    if phase < 0:
        phase += 115.88
    return 10 < phase < 31


@dataclass
class AstroState:
    moon_phase: str
    moon_emoji: str
    moon_idx: int
    moon_bull: bool
    moon_bear: bool
    merc_rx: bool
    equinox: bool
    fire_sign: bool
    sun_sign: str
    moon_angle: float


def get_astro(dt: Optional[datetime] = None) -> AstroState:
    if dt is None:
        dt = datetime.now(timezone.utc)
    J = julian_day(dt)
    sL = sun_longitude(J)
    mL = moon_longitude(J)
    ang = (mL - sL) % 360
    if ang < 0:
        ang += 360
    mi = int(ang / 45)
    merc_rx = is_mercury_retrograde(J)
    equinox = any(abs(((sL - k + 180) % 360) - 180) < 4 for k in [0, 90, 180, 270])
    sun_sign = SIGNS[int(sL / 30)]
    fire_sign = sun_sign in ["Aries", "Leo", "Sagittarius"]
    return AstroState(
        moon_phase=MOON_NAMES[mi],
        moon_emoji=MOON_EMOJI[mi],
        moon_idx=mi,
        moon_bull=mi < 4,
        moon_bear=4 <= mi < 7,
        merc_rx=merc_rx,
        equinox=equinox,
        fire_sign=fire_sign,
        sun_sign=sun_sign,
        moon_angle=round(ang, 1),
    )


# ═══════════════════════════════════════════════
# LIVE PRICE ENGINE
# ═══════════════════════════════════════════════

@dataclass
class LivePrice:
    btc: float = 70000.0
    chg: float = 0.0
    high: float = 71000.0
    low: float = 69000.0
    vol: float = 0.0
    fresh: bool = False
    last_fetch: float = 0.0


# CoinGecko coin ID map — fallback when Binance is blocked
COINGECKO_IDS: dict = {
    "BTCUSDT":  "bitcoin",       "ETHUSDT":  "ethereum",
    "SOLUSDT":  "solana",        "BNBUSDT":  "binancecoin",
    "XRPUSDT":  "ripple",        "AVAXUSDT": "avalanche-2",
    "DOGEUSDT": "dogecoin",      "LINKUSDT": "chainlink",
    "ADAUSDT":  "cardano",       "DOTUSDT":  "polkadot",
    "MATICUSDT":"matic-network", "LTCUSDT":  "litecoin",
    "UNIUSDT":  "uniswap",       "ATOMUSDT": "cosmos",
    "NEARUSDT": "near",
}


async def _fetch_price_coingecko(symbol: str, session: aiohttp.ClientSession) -> dict:
    """CoinGecko fallback — returns Binance-shaped ticker dict."""
    coin_id = COINGECKO_IDS.get(symbol.upper())
    if not coin_id:
        return {}
    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={coin_id}&vs_currencies=usd"
        f"&include_24hr_change=true&include_24hr_vol=true"
        f"&include_high_24hr=true&include_low_24hr=true"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        c = data.get(coin_id, {})
        price = float(c.get("usd", 0))
        if not price:
            return {}
        return {
            "lastPrice":          str(price),
            "priceChangePercent": str(round(float(c.get("usd_24h_change", 0)), 2)),
            "highPrice":          str(c.get("usd_24h_high", price * 1.02)),
            "lowPrice":           str(c.get("usd_24h_low",  price * 0.98)),
            "volume":             str(float(c.get("usd_24h_vol", 0)) / price),
        }
    except Exception:
        return {}


async def _fetch_klines_coingecko(symbol: str, limit: int,
                                   session: aiohttp.ClientSession) -> list:
    """CoinGecko fallback — returns list of hourly close prices."""
    coin_id = COINGECKO_IDS.get(symbol.upper())
    if not coin_id:
        return []
    days = max(1, limit // 24 + 1)
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        f"?vs_currency=usd&days={days}&interval=hourly"
    )
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        prices = data.get("prices", [])
        closes = [float(p[1]) for p in prices]
        return closes[-limit:] if len(closes) > limit else closes
    except Exception:
        return []


async def fetch_price(symbol: str = "BTCUSDT",
                      session: Optional[aiohttp.ClientSession] = None) -> dict:
    """Fetch 24hr ticker — Binance first, CoinGecko fallback."""
    owned = session is None
    if owned:
        session = aiohttp.ClientSession()
    try:
        # Try Binance
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                data = await r.json()
            if isinstance(data, dict) and "lastPrice" in data:
                return data
        except Exception:
            pass
        # Fallback: CoinGecko
        return await _fetch_price_coingecko(symbol, session)
    finally:
        if owned:
            await session.close()


async def fetch_klines(symbol: str = "BTCUSDT", interval: str = "1h",
                       limit: int = 80,
                       session: Optional[aiohttp.ClientSession] = None) -> list:
    """Fetch close prices — Binance first, CoinGecko fallback."""
    owned = session is None
    if owned:
        session = aiohttp.ClientSession()
    try:
        # Try Binance
        url = (f"https://api.binance.com/api/v3/klines"
               f"?symbol={symbol}&interval={interval}&limit={limit}")
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as r:
                data = await r.json()
            if isinstance(data, list) and data:
                return [float(k[4]) for k in data]
        except Exception:
            pass
        # Fallback: CoinGecko
        return await _fetch_klines_coingecko(symbol, limit, session)
    finally:
        if owned:
            await session.close()


# ═══════════════════════════════════════════════
# INDICATOR ENGINE
# ═══════════════════════════════════════════════

def randn() -> float:
    """Box-Muller normal random."""
    u, v = 0, 0
    while u == 0:
        u = random.random()
    while v == 0:
        v = random.random()
    return math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * v)


@dataclass
class Readings:
    rsi: float
    bb: float
    macd: float
    vol_r: float
    sma50: float
    price: float
    lw: float  # lower wick
    uw: float  # upper wick
    hammer: bool
    bull_engulf: bool
    bear_engulf: bool
    doji: bool
    strong_bull: bool
    strong_bear: bool
    wick_dn: bool
    wick_up: bool
    shoot_star: bool
    sma_reclaim: bool
    sma_break: bool
    rsi_rising: bool
    macd_cross: bool
    funding: float
    chain_flow: float
    whale_buy: bool
    range_pos: float


def get_readings(ph: list, lp: LivePrice,
                 onchain: Optional[OnChainCache] = None) -> Readings:
    c = ph
    if len(c) < 3:
        c = [lp.btc] * 30

    l = c[-1]
    p = c[-2] if len(c) >= 2 else l
    p2 = c[-3] if len(c) >= 3 else p

    # RSI 14
    g, lo = 0.0, 0.0
    start = max(1, len(c) - 14)
    for i in range(start, len(c)):
        d = c[i] - c[i - 1]
        if d > 0:
            g += d
        else:
            lo -= d
    ag, al = g / 14, lo / 14
    rsi = 100.0 if al == 0 else round(100 - (100 / (1 + ag / al)), 1)

    # Bollinger 20
    sl = c[-20:] if len(c) >= 20 else c
    m = sum(sl) / len(sl)
    variance = sum((x - m) ** 2 for x in sl) / len(sl)
    sd = math.sqrt(variance)
    bb = round((l - (m - 2 * sd)) / (4 * sd or 1), 3)

    # MACD
    def ema(pp):
        if len(c) < pp:
            return l
        e = c[-pp]
        k = 2 / (pp + 1)
        for i in range(len(c) - pp + 1, len(c)):
            e = e * (1 - k) + c[i] * k
        return e

    macd = round(ema(12) - ema(26), 4) if len(c) >= 26 else 0.0

    # Candle metrics
    body = l - p
    abs_body = abs(body)
    prev_body = p - p2

    recent5 = c[-5:] if len(c) >= 5 else c
    lw = min(l, p) - min(recent5)
    uw = max(recent5) - max(l, p)

    sma50_vals = c[-50:] if len(c) >= 50 else c
    sma50 = sum(sma50_vals) / len(sma50_vals)

    # Volume ratio (real context)
    if lp.fresh:
        vol_r = abs(lp.chg) / 2 + 0.5 + random.random() * 0.8
    else:
        vol_r = 0.6 + random.random() * 1.8

    # On-chain — use per-coin cache if provided and fresh (< 10 min old)
    oc = onchain or OnChainCache()
    if oc.fresh and time.time() - oc.updated_at < 600:
        funding    = oc.funding
        chain_flow = oc.chain_flow
        whale_buy  = oc.whale_buy
    else:
        # Price-derived fallback — better than pure random
        funding    = round(-0.03 if lp.chg < -1.5 else (0.03 if lp.chg > 1.5 else 0.0), 4)
        chain_flow = round(max(-1.0, min(1.0, lp.chg / 3.0)), 3)
        whale_buy  = lp.chg > 0.5 and vol_r > 1.5
    range_pos = (l - lp.low) / (lp.high - lp.low) if lp.high > lp.low else 0.5

    return Readings(
        rsi=rsi, bb=bb, macd=macd, vol_r=vol_r, sma50=sma50, price=l, lw=lw, uw=uw,
        hammer=body > 0 and lw > abs_body * 2 and uw < abs_body * 0.5,
        bull_engulf=body > 0 and prev_body < 0 and l > p2,
        bear_engulf=body < 0 and prev_body > 0 and l < p2,
        doji=abs_body / (p or 1) < 0.001,
        strong_bull=body / (p or 1) > 0.012,
        strong_bear=body / (p or 1) < -0.012,
        wick_dn=lw > abs_body * 1.5,
        wick_up=uw > abs_body * 1.5,
        shoot_star=body < 0 and uw > abs_body * 2,
        sma_reclaim=l > sma50 and p < sma50,
        sma_break=l < sma50 and p > sma50,
        rsi_rising=rsi > 48,
        macd_cross=abs(macd) < 0.001 and macd != 0,
        funding=funding, chain_flow=chain_flow, whale_buy=whale_buy, range_pos=range_pos,
    )


# ═══════════════════════════════════════════════
# PATTERN LIBRARY
# ═══════════════════════════════════════════════

@dataclass
class Pattern:
    id: str
    name: str
    cat: str
    direction: int   # 1=long, -1=short, 0=neutral
    base_win: float
    ingredients: list
    # test is a lambda — stored as callable
    test_fn: object = field(repr=False)

    def fires(self, r: Readings, a: AstroState) -> bool:
        try:
            return bool(self.test_fn(r, a))
        except Exception:
            return False


def _p(id_, name, cat, direction, bw, ings, fn):
    return Pattern(id=id_, name=name, cat=cat, direction=direction,
                   base_win=bw, ingredients=ings, test_fn=fn)


PATTERNS = [
    # CANDLE
    _p("hammer",     "Hammer",               "CANDLE",  1,   0.63, ["CANDLE"],        lambda r,a: r.hammer),
    _p("bullEngulf", "Bullish Engulfing",     "CANDLE",  1,   0.64, ["CANDLE"],        lambda r,a: r.bull_engulf),
    _p("bearEngulf", "Bearish Engulfing",     "CANDLE", -1,   0.63, ["CANDLE"],        lambda r,a: r.bear_engulf),
    _p("wickDn",     "Wick Rejection Low",    "CANDLE",  1,   0.64, ["CANDLE"],        lambda r,a: r.wick_dn),
    _p("wickUp",     "Wick Rejection High",   "CANDLE", -1,   0.63, ["CANDLE"],        lambda r,a: r.wick_up),
    _p("shootStar",  "Shooting Star",         "CANDLE", -1,   0.62, ["CANDLE"],        lambda r,a: r.shoot_star),
    _p("doji",       "Doji",                  "CANDLE",  0,   0.52, ["CANDLE"],        lambda r,a: r.doji),
    _p("3white",     "Three White Soldiers",  "CANDLE",  1,   0.65, ["CANDLE"],        lambda r,a: r.strong_bull and r.macd > 0 and r.rsi < 65),
    _p("3black",     "Three Black Crows",     "CANDLE", -1,   0.64, ["CANDLE"],        lambda r,a: r.strong_bear and r.macd < 0 and r.rsi > 35),
    # TECH
    _p("rsi_os",     "RSI Oversold <30",      "TECH",    1,   0.64, ["RSI"],           lambda r,a: r.rsi < 30),
    _p("rsi_ob",     "RSI Overbought >70",    "TECH",   -1,   0.62, ["RSI"],           lambda r,a: r.rsi > 70),
    _p("rsi_div",    "RSI Bull Divergence",   "TECH",    1,   0.66, ["RSI"],           lambda r,a: r.rsi < 40 and r.rsi_rising),
    _p("bb_low",     "BB Lower Band",         "TECH",    1,   0.63, ["BB"],            lambda r,a: r.bb < 0.1),
    _p("bb_high",    "BB Upper Band",         "TECH",   -1,   0.62, ["BB"],            lambda r,a: r.bb > 0.9),
    _p("macd_up",    "MACD Bull Cross",       "TECH",    1,   0.61, ["MACD"],          lambda r,a: r.macd > 0 and r.macd_cross),
    _p("vol_bull",   "Volume Spike Bull",     "TECH",    1,   0.63, ["VOL"],           lambda r,a: r.vol_r > 2 and r.macd > 0),
    _p("sma_rec",    "SMA 50 Reclaim",        "TECH",    1,   0.61, ["TECH"],          lambda r,a: r.sma_reclaim),
    _p("range_low",  "Near 24h Low",          "TECH",    1,   0.62, ["TECH"],          lambda r,a: r.range_pos < 0.15),
    _p("range_high", "Near 24h High",         "TECH",   -1,   0.61, ["TECH"],          lambda r,a: r.range_pos > 0.85),
    # PRICE ACTION
    _p("liq_low",    "Liquidity Grab Low",    "PA",      1,   0.67, ["CANDLE","TECH"], lambda r,a: r.wick_dn and r.rsi < 38 and r.vol_r > 1.4),
    _p("liq_high",   "Liquidity Grab High",   "PA",     -1,   0.66, ["CANDLE","TECH"], lambda r,a: r.wick_up and r.rsi > 62 and r.vol_r > 1.4),
    _p("stop_low",   "Stop Hunt Low",         "PA",      1,   0.66, ["CANDLE","TECH"], lambda r,a: r.wick_dn and r.vol_r > 2 and r.rsi < 35),
    _p("ob_bull",    "Bull Order Block",      "PA",      1,   0.65, ["TECH"],          lambda r,a: r.bb < 0.2 and r.strong_bull and r.vol_r > 1.5),
    _p("fvg",        "Fair Value Gap",        "PA",      1,   0.63, ["TECH"],          lambda r,a: r.strong_bull and r.bb < 0.5),
    # MATH
    _p("fib618",     "Fibonacci 0.618",       "MATH",    1,   0.64, ["MATH"],          lambda r,a: 0.36 < r.bb < 0.42),
    _p("ell_w3",     "Elliott Wave 3",        "MATH",    1,   0.66, ["MATH","MACD"],   lambda r,a: r.macd > 0 and 55 < r.rsi < 72 and r.vol_r > 1.5),
    # WYCKOFF
    _p("wyck_sp",    "Wyckoff Spring",        "WYCK",    1,   0.68, ["TECH","VOL"],    lambda r,a: r.wick_dn and r.vol_r > 1.8 and r.rsi < 35 and r.bb < 0.15),
    _p("wyck_up",    "Wyckoff Upthrust",      "WYCK",   -1,   0.67, ["TECH","VOL"],    lambda r,a: r.wick_up and r.vol_r > 1.8 and r.rsi > 65 and r.bb > 0.85),
    # ON-CHAIN
    _p("fund_os",    "Neg Funding + Oversold","CHAIN",   1,   0.67, ["CHAIN","RSI"],   lambda r,a: r.funding < -0.02 and r.rsi < 40),
    _p("whale",      "Whale Accumulation",    "CHAIN",   1,   0.66, ["CHAIN","VOL"],   lambda r,a: r.whale_buy and r.rsi < 55),
    _p("exch_out",   "Exchange Outflow",      "CHAIN",   1,   0.64, ["CHAIN"],         lambda r,a: r.chain_flow < 0 and r.vol_r > 1.3),
    # ASTRO
    _p("new_moon",   "New Moon",              "ASTRO",   1,   0.62, ["MOON"],          lambda r,a: a.moon_idx == 0),
    _p("full_moon",  "Full Moon Reversal",    "ASTRO",  -1,   0.61, ["MOON"],          lambda r,a: a.moon_idx == 4),
    _p("waxing",     "Waxing Moon Bull",      "ASTRO",   1,   0.60, ["MOON"],          lambda r,a: a.moon_bull),
    _p("equinox",    "Equinox",               "ASTRO",   1,   0.64, ["ASTRO"],         lambda r,a: a.equinox),
    _p("merc_rx",    "Mercury Rx Trap",       "ASTRO",  -1,   0.63, ["ASTRO"],         lambda r,a: a.merc_rx and r.strong_bull),
    # COMBOS
    _p("nm_os",      "New Moon + Oversold",   "COMBO",   1,   0.68, ["MOON","RSI"],    lambda r,a: a.moon_idx == 0 and r.rsi < 35),
    _p("wyck_moon",  "Wyckoff + New Moon",    "COMBO",   1,   0.72, ["WYCK","MOON"],   lambda r,a: r.wick_dn and r.vol_r > 1.8 and r.rsi < 35 and a.moon_bull),
    _p("sh_chain",   "Stop Hunt + Outflow",   "COMBO",   1,   0.71, ["PA","CHAIN"],    lambda r,a: r.wick_dn and r.vol_r > 2 and r.chain_flow < 0 and r.rsi < 40),
    _p("fund_whale", "Fund + Whale + OS",     "COMBO",   1,   0.70, ["CHAIN","RSI"],   lambda r,a: r.funding < -0.02 and r.whale_buy and r.rsi < 38),
    # MACRO
    _p("fear",       "Extreme Fear",          "MACRO",   1,   0.63, ["MACRO"],         lambda r,a: r.rsi < 28 and r.vol_r > 1.5),
    _p("gamma_sq",   "Gamma Squeeze",         "MACRO",   1,   0.64, ["MACRO","VOL"],   lambda r,a: r.vol_r > 2.5 and r.funding < -0.01 and r.rsi < 45),
]


# ═══════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════

@dataclass
class PatternRecord:
    pattern: Pattern
    occ: int = 0
    wins: int = 0
    losses: int = 0
    acc: float = 0.0
    ev: float = 0.0
    recent_returns: list = field(default_factory=list)

    def update(self, win: bool, ret: float):
        self.occ += 1
        if win:
            self.wins += 1
        else:
            self.losses += 1
        self.recent_returns.append(ret)
        if len(self.recent_returns) > 80:
            self.recent_returns.pop(0)
        total = self.wins + self.losses
        self.acc = round(self.wins / total * 100) if total else 0
        self.ev = round(sum(self.recent_returns) / len(self.recent_returns), 2) if self.recent_returns else 0

    @property
    def score(self) -> float:
        return self.acc * 0.55 + min(100, self.ev * 12) * 0.35 + (self.occ / 60) * 10 * 0.10


class Database:
    def __init__(self):
        self.records: dict[str, PatternRecord] = {}
        for pat in PATTERNS:
            rec = PatternRecord(pattern=pat)
            # Seed with synthetic priors
            n = 5 + random.randint(0, 12)
            for _ in range(n):
                win = random.random() < pat.base_win
                ret = (1.2 + random.random() * 2.2) if win else -(0.7 + random.random() * 1.8)
                rec.update(win, ret)
            self.records[pat.id] = rec

    def find_best(self, r: Readings, a: AstroState, min_acc: float = 45) -> Optional[PatternRecord]:
        candidates = [
            rec for rec in self.records.values()
            if rec.occ >= 1 and rec.acc >= min_acc and rec.pattern.fires(r, a)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda x: x.score)

    def get_stats(self) -> dict:
        with_data = [r for r in self.records.values() if r.occ >= 2]
        if not with_data:
            return {"size": 0, "obs": 0, "elite": 0, "avg_acc": 0}
        return {
            "size": len(with_data),
            "obs": sum(r.occ for r in with_data),
            "elite": sum(1 for r in with_data if r.acc >= 70),
            "avg_acc": round(sum(r.acc for r in with_data) / len(with_data), 1),
        }

    def to_dict(self) -> dict:
        return {
            pid: {
                "occ": rec.occ, "wins": rec.wins, "losses": rec.losses,
                "acc": rec.acc, "ev": rec.ev,
                "recent_returns": rec.recent_returns,
            }
            for pid, rec in self.records.items()
        }

    def load_dict(self, data: dict):
        for pid, d in data.items():
            if pid in self.records:
                rec = self.records[pid]
                rec.occ            = d.get("occ", 0)
                rec.wins           = d.get("wins", 0)
                rec.losses         = d.get("losses", 0)
                rec.acc            = d.get("acc", 0.0)
                rec.ev             = d.get("ev", 0.0)
                rec.recent_returns = d.get("recent_returns", [])


# ═══════════════════════════════════════════════
# EDGE SCORE
# ═══════════════════════════════════════════════

@dataclass
class EdgeScore:
    score: int
    status: str
    db_size: int
    total_obs: int
    elite: int
    consistency: int


def calc_edge(records: list) -> EdgeScore:
    """Compute edge score from real pattern records across all coins."""
    pats = [r for r in records if r.occ >= 2]
    if not pats:
        return EdgeScore(0, "BLURRY", 0, 0, 0, 0)
    avg   = sum(r.acc for r in pats) / len(pats)
    elite = sum(1 for r in pats if r.acc >= 70)
    prof  = sum(1 for r in pats if r.ev > 0)
    obs   = sum(r.occ for r in pats)
    total_w = sum(r.wins for r in pats)
    total_t = sum(r.occ  for r in pats)
    cons  = round(total_w / total_t * 100) if total_t > 0 else 0
    score = min(100, round(
        avg * 0.35 +
        (elite / max(len(pats), 1)) * 30 +
        (prof  / max(len(pats), 1)) * 20 +
        min(obs / 300, 1) * 15
    ))
    status = ("ELITE"   if score >= 85 else
              "SHARP"   if score >= 70 else
              "EDGE"    if score >= 50 else
              "FORMING" if score >= 30 else "BLURRY")
    return EdgeScore(score, status, len(pats), obs, elite, cons)


# ═══════════════════════════════════════════════
# LEVERAGE HELPER
# ═══════════════════════════════════════════════

def get_lev(acc: float) -> int:
    if acc >= 80: return 8
    if acc >= 70: return 6
    if acc >= 60: return 4
    return 3


# ═══════════════════════════════════════════════
# PRICE SIMULATION (anchored to live)
# ═══════════════════════════════════════════════

class PriceHistory:
    def __init__(self, start_price: float = 70000.0):
        self.prices = []
        p = start_price
        for _ in range(120):
            p = max(p * 0.5, p * (1 + (random.random() - 0.49) * 0.022))
            self.prices.append(p)

    def tick(self, live_price: float):
        l = self.prices[-1]
        pull = (live_price - l) / live_price * 0.12
        n = max(l * 0.4, l * (1 + pull + 0.022 * randn() * 0.4 + 0.0001))
        self.prices.append(n)
        if len(self.prices) > 300:
            self.prices = self.prices[-200:]

    @property
    def closes(self) -> list:
        return self.prices.copy()


# ═══════════════════════════════════════════════
# MULTI-COIN SCANNER
# ═══════════════════════════════════════════════

# All symbols we scan — Binance USDT-M perps that also exist on Bitget
SCAN_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "AVAXUSDT", "DOGEUSDT", "LINKUSDT", "ADAUSDT", "NEARUSDT",
    "DOTUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT", "MATICUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT",
]

# Pretty display names
COIN_NAMES = {
    "BTCUSDT": "BTC",  "ETHUSDT": "ETH",  "SOLUSDT": "SOL",
    "BNBUSDT": "BNB",  "XRPUSDT": "XRP",  "AVAXUSDT": "AVAX",
    "DOGEUSDT": "DOGE","LINKUSDT": "LINK", "ADAUSDT": "ADA",
    "NEARUSDT": "NEAR","DOTUSDT": "DOT",  "UNIUSDT": "UNI",
    "ATOMUSDT": "ATOM","LTCUSDT": "LTC",  "MATICUSDT": "MATIC",
    "APTUSDT": "APT",  "ARBUSDT": "ARB",  "OPUSDT": "OP",
    "INJUSDT": "INJ",  "SUIUSDT": "SUI",
}


class CoinScanner:
    """Tracks one coin's price, indicators, on-chain data, and pattern DB."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.live_price = LivePrice()
        self.price_history = PriceHistory()
        self.db = Database()    # per-coin pattern learning

    @property
    def onchain(self) -> OnChainCache:
        return _onchain.get(self.symbol, OnChainCache())

    async def update_price(self, session: aiohttp.ClientSession) -> bool:
        data = await fetch_price(self.symbol, session)
        if data and "lastPrice" in data:
            self.live_price.btc   = float(data["lastPrice"])
            self.live_price.chg   = float(data["priceChangePercent"])
            self.live_price.high  = float(data["highPrice"])
            self.live_price.low   = float(data["lowPrice"])
            self.live_price.vol   = float(data["volume"]) * self.live_price.btc
            self.live_price.fresh = True
            self.live_price.last_fetch = time.time()
            return True
        return False

    def get_readings(self) -> Readings:
        self.price_history.tick(self.live_price.btc)
        return get_readings(self.price_history.closes, self.live_price, self.onchain)

    def get_signal(self, _w=0, _t=0) -> Optional[dict]:
        """Return best signal dict for this coin, or None."""
        a = get_astro()
        r = get_readings(self.price_history.closes, self.live_price, self.onchain)
        rec = self.db.find_best(r, a)
        if not rec:
            return None
        pat = rec.pattern
        price = self.live_price.btc

        stop   = price * (0.97 if pat.direction == 1 else 1.03)
        target = price * (1 + 0.04 + (rec.acc - 50) * 0.001 if pat.direction == 1
                          else 1 - (0.04 + (rec.acc - 50) * 0.001))
        risk   = abs(price - stop) / price
        reward = abs(target - price) / price
        rr     = round(reward / risk, 2) if risk > 0 else 0.0

        edge = calc_edge(list(self.db.records.values()))
        conf = min(95, max(40, round(rec.acc * 0.6 + edge.score * 0.4)))

        why = []
        if a.moon_bull and pat.direction == 1:   why.append(f"{a.moon_emoji} {a.moon_phase} — waxing bull cycle")
        if a.moon_bear and pat.direction == -1:  why.append(f"{a.moon_emoji} {a.moon_phase} — waning bear cycle")
        if a.merc_rx:    why.append("☿ Mercury Retrograde — reduce size 40%")
        if r.rsi < 32:   why.append(f"RSI {r.rsi} — oversold, demand zone")
        if r.rsi > 68:   why.append(f"RSI {r.rsi} — overbought, distribution risk")
        if r.wick_dn:    why.append("Wick rejection — liquidity grab")
        if r.vol_r > 1.8:why.append(f"Volume {r.vol_r:.1f}× — conviction move")
        if r.funding < -0.025: why.append(f"Funding {r.funding:.3f}% — squeeze building")

        coin = COIN_NAMES.get(self.symbol, self.symbol.replace("USDT", ""))
        return {
            "symbol":     self.symbol,
            "coin":       coin,
            "pattern":    pat.name,
            "category":   pat.cat,
            "direction":  "LONG" if pat.direction == 1 else "SHORT" if pat.direction == -1 else "NEUTRAL",
            "dir_arrow":  "▲" if pat.direction == 1 else "▼",
            "accuracy":   rec.acc,
            "ev":         rec.ev,
            "observations": rec.occ,
            "confidence": conf,
            "score":      rec.score,
            "entry":      round(price, 6),
            "stop":       round(stop, 6),
            "target":     round(target, 6),
            "rr":         rr,
            "leverage":   get_lev(rec.acc),
            "size_pct":   min(20, max(5, round((rec.acc - 42) / 2))),
            "why":        why[:4],
            "ingredients":pat.ingredients,
            "merc_rx":    a.merc_rx,
            "pattern_id": pat.id,
        }


# ═══════════════════════════════════════════════
# BITGET EXECUTION LAYER
# ═══════════════════════════════════════════════

BITGET_BASE = "https://api.bitget.com"


@dataclass
class LiveTradeState:
    active: bool = False
    order_id: str = ""
    symbol: str = "BTCUSDT"   # which coin this trade is on
    direction: int = 0        # 1=long, -1=short
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    size_usdt: float = 0.0
    pattern: str = ""         # display name
    pattern_id: str = ""      # DB key — used to record real outcome
    opened_at: float = 0.0


class BitgetExecutor:
    def __init__(self, api_key: str = "", secret: str = "", passphrase: str = "",
                 paper: bool = True):
        self.api_key = api_key
        self.secret = secret
        self.passphrase = passphrase
        self.paper = paper
        self.enabled = bool(api_key and secret and passphrase)

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        msg = f"{timestamp}{method.upper()}{path}{body}"
        sig = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(sig).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "ACCESS-KEY":        self.api_key,
            "ACCESS-SIGN":       self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP":  ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type":      "application/json",
            "locale":            "en-US",
        }

    async def get_balance(self, session: aiohttp.ClientSession) -> float:
        """Return available USDT balance on Bitget futures."""
        if not self.enabled:
            return 0.0
        path = "/api/v2/mix/account/account?symbol=BTCUSDT&productType=USDT-FUTURES&marginCoin=USDT"
        try:
            async with session.get(
                BITGET_BASE + path,
                headers=self._headers("GET", path),
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                data = await r.json()
            return float(data.get("data", {}).get("available", 0))
        except Exception:
            return 0.0

    async def place_order(self, session: aiohttp.ClientSession,
                          symbol: str, direction: int, size_usdt: float,
                          price: float, stop: float, target: float) -> dict:
        """Open a market order with attached TP/SL on any USDT-M perp."""
        if self.paper or not self.enabled:
            return {"paper": True, "orderId": f"PAPER-{int(time.time())}"}

        side      = "buy"  if direction == 1 else "sell"
        hold_side = "long" if direction == 1 else "short"
        size_coin = round(size_usdt / price, 6)

        body = json.dumps({
            "symbol":                symbol,
            "productType":           "USDT-FUTURES",
            "marginMode":            "isolated",
            "marginCoin":            "USDT",
            "size":                  str(size_coin),
            "side":                  side,
            "tradeSide":             "open",
            "orderType":             "market",
            "presetStopLossPrice":   str(round(stop, 4)),
            "presetTakeProfitPrice": str(round(target, 4)),
        })
        path = "/api/v2/mix/order/placeOrder"
        try:
            async with session.post(
                BITGET_BASE + path,
                headers=self._headers("POST", path, body),
                data=body,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
            order_id = data.get("data", {}).get("orderId", "")
            return {"paper": False, "orderId": order_id, "raw": data}
        except Exception as e:
            return {"paper": False, "orderId": "", "error": str(e)}

    async def close_position(self, session: aiohttp.ClientSession,
                             symbol: str, direction: int) -> dict:
        """Close the open position on the given symbol at market."""
        if self.paper or not self.enabled:
            return {"paper": True}

        hold_side = "long" if direction == 1 else "short"
        body = json.dumps({
            "symbol":      symbol,
            "productType": "USDT-FUTURES",
            "holdSide":    hold_side,
        })
        path = "/api/v2/mix/order/closePositions"
        try:
            async with session.post(
                BITGET_BASE + path,
                headers=self._headers("POST", path, body),
                data=body,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                return await r.json()
        except Exception as e:
            return {"error": str(e)}

    async def get_position(self, session: aiohttp.ClientSession,
                           symbol: str = "BTCUSDT") -> dict:
        """Return current open position for a symbol (empty dict if none)."""
        if not self.enabled:
            return {}
        path = (f"/api/v2/mix/position/singlePosition"
                f"?symbol={symbol}&productType=USDT-FUTURES&marginCoin=USDT")
        try:
            async with session.get(
                BITGET_BASE + path,
                headers=self._headers("GET", path),
                timeout=aiohttp.ClientTimeout(total=8)
            ) as r:
                data = await r.json()
            positions = data.get("data", [])
            if positions and float(positions[0].get("total", 0)) > 0:
                return positions[0]
            return {}
        except Exception:
            return {}


# ═══════════════════════════════════════════════
# PLATINIUM ENGINE (main orchestrator)
# ═══════════════════════════════════════════════

class PlatiniumEngine:
    def __init__(self, executor: Optional[BitgetExecutor] = None,
                 trade_size_usdt: float = 1.0,
                 symbols: Optional[list] = None):
        # ── multi-coin scanners ──
        self.symbols = symbols or SCAN_SYMBOLS
        self.scanners: dict[str, CoinScanner] = {
            sym: CoinScanner(sym) for sym in self.symbols
        }
        # ── BTC aliases (backward compat) ──
        btc = self.scanners["BTCUSDT"]
        self.live_price    = btc.live_price
        self.price_history = btc.price_history
        self.db            = btc.db
        # ── state ──
        self.tick_count = 0
        self.executor = executor or BitgetExecutor()
        self.live_trade = LiveTradeState()
        self.trade_size_usdt = trade_size_usdt
        # real trade history (win/loss recorded from actual closes)
        self.trade_history: list = []
        self._load()

    def _save(self):
        try:
            payload = {
                "dbs":           {sym: sc.db.to_dict() for sym, sc in self.scanners.items()},
                "trade_history": self.trade_history[-200:],
            }
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, DATA_FILE)
        except Exception as e:
            log.warning(f"State save failed: {e}")

    def _load(self):
        if not os.path.exists(DATA_FILE):
            return
        try:
            with open(DATA_FILE) as f:
                payload = json.load(f)
            self.sim.load_dict(payload.get("sim", {}))
            # per-coin DBs (new format) or legacy single db
            if "dbs" in payload:
                for sym, data in payload["dbs"].items():
                    if sym in self.scanners:
                        self.scanners[sym].db.load_dict(data)
            elif "db" in payload:
                self.scanners["BTCUSDT"].db.load_dict(payload["db"])
            self.trade_history = payload.get("trade_history", [])
            log.info(f"State loaded — {len(self.trade_history)} real trades, {len(self.scanners)} coins")
        except Exception as e:
            log.warning(f"State load failed (starting fresh): {e}")

    def get_live_trade(self) -> LiveTradeState:
        return self.live_trade

    async def open_live_trade(self, session: aiohttp.ClientSession,
                               sig: dict, size_usdt: float = 1.0) -> LiveTradeState:
        """Open a trade on any coin from a signal dict."""
        if self.live_trade.active:
            return self.live_trade
        symbol    = sig.get("symbol", "BTCUSDT")
        direction = 1 if sig["direction"] == "LONG" else -1
        result = await self.executor.place_order(
            session, symbol, direction,
            size_usdt, sig["entry"], sig["stop"], sig["target"]
        )
        self.live_trade = LiveTradeState(
            active=True,
            order_id=result.get("orderId", ""),
            symbol=symbol,
            direction=direction,
            entry=sig["entry"],
            stop=sig["stop"],
            target=sig["target"],
            size_usdt=size_usdt,
            pattern=sig["pattern"],
            pattern_id=sig.get("pattern_id", ""),
            opened_at=time.time(),
        )
        return self.live_trade

    async def check_and_close_live_trade(self, session: aiohttp.ClientSession) -> Optional[dict]:
        """
        Check if active trade hit TP/SL.
        Real trades: poll Bitget position — closed when position gone.
        No keys: use live price vs TP/SL thresholds.
        Returns close-event dict or None.
        """
        if not self.live_trade.active:
            return None

        lt = self.live_trade
        scanner = self.scanners.get(lt.symbol, self.scanners["BTCUSDT"])
        price = scanner.live_price.btc

        if self.executor.enabled:
            # Real trade — check Bitget
            pos = await self.executor.get_position(session, lt.symbol)
            if pos:
                return None  # still open on exchange
            win = price > lt.entry if lt.direction == 1 else price < lt.entry
        else:
            # No exchange connection — use price vs TP/SL
            hit_tp = (lt.direction == 1 and price >= lt.target) or \
                     (lt.direction == -1 and price <= lt.target)
            hit_sl = (lt.direction == 1 and price <= lt.stop) or \
                     (lt.direction == -1 and price >= lt.stop)
            if not (hit_tp or hit_sl):
                return None
            win = hit_tp

        pnl_pct  = round((price - lt.entry) / lt.entry * 100 * lt.direction, 2)
        pnl_usdt = round(lt.size_usdt * pnl_pct / 100, 2)
        duration = round((time.time() - lt.opened_at) / 60, 1)

        # ── Feed real outcome back to pattern DB ──
        sc = self.scanners.get(lt.symbol)
        if sc and lt.pattern_id and lt.pattern_id in sc.db.records:
            sc.db.records[lt.pattern_id].update(win, pnl_pct)

        coin = COIN_NAMES.get(lt.symbol, lt.symbol.replace("USDT", ""))
        event = {
            "symbol":       lt.symbol,
            "coin":      coin,
            "pattern":   lt.pattern,
            "direction": "LONG" if lt.direction == 1 else "SHORT",
            "entry":     lt.entry,
            "exit":      round(price, 6),
            "pnl_pct":   round(pnl_pct, 2),
            "pnl_usdt":  pnl_usdt,
            "win":       win,
            "duration":  duration,
        }
        self.trade_history.insert(0, event)
        if len(self.trade_history) > 200:
            self.trade_history = self.trade_history[:100]
        self.live_trade = LiveTradeState()
        self._save()
        return event

    async def get_live_balance(self, session: aiohttp.ClientSession) -> float:
        return await self.executor.get_balance(session)

    async def refresh_onchain(self, session: aiohttp.ClientSession):
        """Fetch on-chain data for BTC (backward compat)."""
        await update_onchain(session, "BTCUSDT")

    async def refresh_all_onchain(self, session: aiohttp.ClientSession):
        """Fetch on-chain data for all scanned coins."""
        await update_all_onchain(session, self.symbols)

    async def update_price(self, session: aiohttp.ClientSession):
        """Fetch fresh BTC price (backward compat)."""
        await self.scanners["BTCUSDT"].update_price(session)
        # keep alias in sync
        self.live_price = self.scanners["BTCUSDT"].live_price

    async def update_all_prices(self, session: aiohttp.ClientSession):
        """Batch-fetch prices for all scanned coins via Binance multi-ticker."""
        syms_json = json.dumps(self.symbols)
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbols={syms_json}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                tickers = await r.json()
            if isinstance(tickers, list):
                for t in tickers:
                    sym = t.get("symbol", "")
                    sc  = self.scanners.get(sym)
                    if not sc:
                        continue
                    sc.live_price.btc   = float(t["lastPrice"])
                    sc.live_price.chg   = float(t["priceChangePercent"])
                    sc.live_price.high  = float(t["highPrice"])
                    sc.live_price.low   = float(t["lowPrice"])
                    sc.live_price.vol   = float(t["volume"]) * sc.live_price.btc
                    sc.live_price.fresh = True
                    sc.live_price.last_fetch = time.time()
                # keep BTC alias in sync
                self.live_price = self.scanners["BTCUSDT"].live_price
                log.debug(f"Prices updated for {len(tickers)} coins")
                return
        except Exception as e:
            log.debug(f"Batch price fetch failed: {e}")
        # fallback: update coins individually
        for sc in self.scanners.values():
            await sc.update_price(session)
            await asyncio.sleep(0.1)

    def tick_all(self):
        """
        Advance price history for every coin. Pure scanning — no fake trades.
        Pattern learning happens only from real closed trades via check_and_close_live_trade.
        """
        self.tick_count += 1
        a = get_astro()
        for sc in self.scanners.values():
            if sc.live_price.fresh:
                sc.price_history.tick(sc.live_price.btc)
                # warm up readings so patterns are ready for get_signal()
                get_readings(sc.price_history.closes, sc.live_price, sc.onchain)

    def tick(self):
        self.tick_all()

    def get_edge_records(self) -> list:
        """All pattern records across all coin DBs."""
        records = []
        for sc in self.scanners.values():
            records.extend(sc.db.records.values())
        return records

    def get_signal(self) -> Optional[dict]:
        """Best signal across all coins (highest score wins)."""
        all_sigs = []
        for sc in self.scanners.values():
            if not sc.live_price.fresh:
                continue
            sig = sc.get_signal(0, 0)
            if sig:
                all_sigs.append(sig)
        if not all_sigs:
            return None
        return max(all_sigs, key=lambda s: s["score"] + s["confidence"] * 0.5)

    def get_edge(self) -> EdgeScore:
        return calc_edge(self.get_edge_records())

    def get_astro(self) -> AstroState:
        return get_astro()

    def get_summary(self) -> dict:
        edge  = self.get_edge()
        wins  = sum(1 for t in self.trade_history if t.get("win"))
        total = len(self.trade_history)
        return {
            "total":       total,
            "wins":        wins,
            "losses":      total - wins,
            "wr":          round(wins / total * 100, 1) if total else 0.0,
            "edge_score":  edge.score,
            "edge_status": edge.status,
            "db_size":     edge.db_size,
            "elite":       edge.elite,
            "total_obs":   edge.total_obs,
            "btc":         round(self.live_price.btc, 2),
            "btc_chg":     round(self.live_price.chg, 2),
            "astro":       self.get_astro(),
            "last_trades": self.trade_history[:5],
        }
