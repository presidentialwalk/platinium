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
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional


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


async def fetch_price(symbol: str = "BTCUSDT", session: Optional[aiohttp.ClientSession] = None) -> dict:
    """Fetch 24hr ticker from Binance."""
    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
    try:
        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        if close_session:
            await session.close()
        return data
    except Exception:
        return {}


async def fetch_klines(symbol: str = "BTCUSDT", interval: str = "1h", limit: int = 80,
                       session: Optional[aiohttp.ClientSession] = None) -> list:
    """Fetch klines (OHLCV) from Binance."""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        close_session = False
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
        if close_session:
            await session.close()
        return [float(k[4]) for k in data]  # close prices
    except Exception:
        return []


# ═══════════════════════════════════════════════
# POLYMARKET ENGINE
# ═══════════════════════════════════════════════

POLYMARKET_API = "https://gamma-api.polymarket.com/markets"
POLY_KEYWORDS_BULL = ["above", "reach", "hit", "exceed", "over", "higher", "bull", "surpass"]
POLY_KEYWORDS_BEAR = ["below", "drop", "crash", "fall", "bear", "under", "lose"]


@dataclass
class PolymarketState:
    bull_pct: float = 50.0
    markets: list = field(default_factory=list)  # [{question, yes_pct, volume}]
    fresh: bool = False
    last_fetch: float = 0.0


async def fetch_polymarket(session: Optional[aiohttp.ClientSession] = None) -> PolymarketState:
    """Fetch BTC/crypto prediction markets from Polymarket Gamma API."""
    url = POLYMARKET_API + "?search=bitcoin&active=true&closed=false&limit=10"
    close_session = False
    try:
        if session is None:
            session = aiohttp.ClientSession()
            close_session = True
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
        if close_session:
            await session.close()
            close_session = False

        markets = []
        bull_scores = []

        for m in data:
            if not m.get("active") or m.get("closed"):
                continue
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
                if len(prices) < 2:
                    continue
                yes_pct = round(float(prices[0]) * 100, 1)
                vol = float(m.get("volume", 0))
                q = m.get("question", "")
                markets.append({"question": q[:70], "yes_pct": yes_pct, "volume": vol})
                q_lower = q.lower()
                if any(w in q_lower for w in POLY_KEYWORDS_BEAR):
                    bull_scores.append((100 - yes_pct, vol))
                else:
                    bull_scores.append((yes_pct, vol))
            except Exception:
                continue

        if not bull_scores:
            return PolymarketState()

        total_vol = sum(v for _, v in bull_scores) or 1.0
        bull_pct = round(sum(p * v for p, v in bull_scores) / total_vol, 1)
        return PolymarketState(
            bull_pct=bull_pct,
            markets=markets[:5],
            fresh=True,
            last_fetch=time.time(),
        )
    except Exception:
        if close_session and session:
            try:
                await session.close()
            except Exception:
                pass
        return PolymarketState()


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
    poly_bull: float = 50.0


def get_readings(ph: list, lp: LivePrice, poly_bull: float = 50.0) -> Readings:
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

    # On-chain (context-biased)
    funding = (-0.05 + random.random() * 0.02) if lp.chg < -1 else (random.random() - 0.5) * 0.06
    chain_flow = -1.0 if lp.chg < -1 else (1.0 if lp.chg > 1 else (-1.0 if random.random() > 0.5 else 1.0))
    whale_buy = random.random() > (0.45 if lp.chg < -2 else 0.75)
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
        poly_bull=poly_bull,
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
    # POLYMARKET
    _p("poly_bull",  "Polymarket Bullish",    "MACRO",   1,   0.64, ["MACRO"],         lambda r,a: r.poly_bull > 60),
    _p("poly_bear",  "Polymarket Bearish",    "MACRO",  -1,   0.63, ["MACRO"],         lambda r,a: r.poly_bull < 40),
    _p("poly_combo", "Poly Bull + Oversold",  "COMBO",   1,   0.69, ["MACRO","RSI"],   lambda r,a: r.poly_bull > 62 and r.rsi < 38),
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


def calc_edge(db: Database, sim_wins: int, sim_total: int) -> EdgeScore:
    pats = [r for r in db.records.values() if r.occ >= 2]
    if not pats:
        return EdgeScore(0, "BLURRY", 0, 0, 0, 0)
    avg = sum(r.acc for r in pats) / len(pats)
    elite = sum(1 for r in pats if r.acc >= 70)
    prof = sum(1 for r in pats if r.ev > 0)
    obs = sum(r.occ for r in pats)
    cons = round(sim_wins / sim_total * 100) if sim_total > 0 else 0
    score = min(100, round(
        avg * 0.35 +
        (elite / max(len(pats), 1)) * 30 +
        (prof / max(len(pats), 1)) * 20 +
        min(obs / 300, 1) * 15
    ))
    if score >= 85:
        status = "ELITE"
    elif score >= 70:
        status = "SHARP"
    elif score >= 50:
        status = "FORMING"
    else:
        status = "BLURRY"
    return EdgeScore(score, status, len(pats), obs, elite, cons)


# ═══════════════════════════════════════════════
# SIMULATION ENGINE (real conditions)
# ═══════════════════════════════════════════════

MAKER_FEE = 0.0002
TAKER_FEE = 0.0005
SLIPPAGE   = 0.0003
ROUND_TRIP = (MAKER_FEE + TAKER_FEE + SLIPPAGE) * 2
MIN_BALANCE = 2.0
MAX_LEVERAGE = 10


@dataclass
class SimState:
    balance: float = 100.0
    peak: float = 100.0
    wins: int = 0
    losses: int = 0
    total: int = 0
    cons_loss: int = 0
    cons_win: int = 0
    curve: list = field(default_factory=lambda: [100.0])
    trades: list = field(default_factory=list)

    @property
    def wr(self) -> float:
        return round(self.wins / self.total * 100, 1) if self.total else 0.0

    @property
    def drawdown(self) -> float:
        return round((self.peak - self.balance) / self.peak * 100, 1) if self.peak > 0 else 0.0

    @property
    def pnl_pct(self) -> float:
        return round((self.balance - 100) / 100 * 100, 1)


def get_lev(acc: float) -> int:
    if acc >= 80: return 8
    if acc >= 70: return 6
    if acc >= 60: return 4
    return 3


def pos_size(sim: SimState, acc: float) -> float:
    f = 0.08 + (acc - 50) / 1000
    f = min(f, 0.20)
    if sim.cons_loss >= 2: f *= 0.6
    if sim.cons_loss >= 4: f *= 0.4
    if sim.balance < sim.peak * 0.7: f *= 0.5  # drawdown protection
    return max(1.0, sim.balance * f)


def execute_trade(sim: SimState, db: Database, rec: PatternRecord,
                  r: Readings, a: AstroState) -> Optional[dict]:
    if sim.balance < MIN_BALANCE:
        return None

    pat = rec.pattern
    acc = rec.acc or 50
    size = pos_size(sim, acc)
    lev = get_lev(acc)

    # Win probability — real factors
    wp = pat.base_win
    if a.moon_bull and pat.direction == 1:  wp += 0.04
    if a.moon_bear and pat.direction == -1: wp += 0.03
    if a.merc_rx and pat.id != "merc_rx":  wp -= 0.14
    if a.equinox:                           wp += 0.03
    if r.range_pos < 0.1 and pat.direction == 1:  wp += 0.04
    if r.range_pos > 0.9 and pat.direction == -1: wp += 0.04
    wp = max(0.2, min(0.85, wp))

    win = random.random() < wp

    # P&L with real costs
    if win:
        gross = (0.004 + random.random() * 0.035) * lev
    else:
        gross = -(0.003 + random.random() * 0.022) * lev

    net_ret = gross - ROUND_TRIP * lev
    pnl = size * net_ret
    new_bal = max(0.0, sim.balance + pnl)

    # Update database
    rec.update(win, net_ret * 100)

    # Update sim
    sim.balance = new_bal
    sim.peak = max(sim.peak, new_bal)
    sim.total += 1
    if win:
        sim.wins += 1
        sim.cons_loss = 0
        sim.cons_win += 1
    else:
        sim.losses += 1
        sim.cons_loss += 1
        sim.cons_win = 0
    sim.curve.append(round(new_bal, 2))
    if len(sim.curve) > 500:
        sim.curve = sim.curve[-300:]

    trade = {
        "pattern": pat.name,
        "direction": "LONG" if pat.direction == 1 else "SHORT",
        "win": win,
        "pnl": round(pnl, 2),
        "balance": round(new_bal, 2),
        "acc": acc,
        "lev": lev,
        "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
    }
    sim.trades.insert(0, trade)
    if len(sim.trades) > 200:
        sim.trades = sim.trades[:100]

    return trade


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
# BITGET EXECUTION LAYER
# ═══════════════════════════════════════════════

BITGET_BASE = "https://api.bitget.com"


@dataclass
class LiveTradeState:
    active: bool = False
    order_id: str = ""
    direction: int = 0       # 1=long, -1=short
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    size_usdt: float = 0.0
    pattern: str = ""
    opened_at: float = 0.0
    paper: bool = True        # True = simulated, False = real money


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
                          direction: int, size_usdt: float,
                          price: float, stop: float, target: float) -> dict:
        """Open a market order with attached TP/SL. Returns order info dict."""
        if self.paper or not self.enabled:
            return {"paper": True, "orderId": f"PAPER-{int(time.time())}"}

        side      = "buy"  if direction == 1 else "sell"
        hold_side = "long" if direction == 1 else "short"
        size_btc  = round(size_usdt / price, 4)

        body = json.dumps({
            "symbol":                "BTCUSDT",
            "productType":           "USDT-FUTURES",
            "marginMode":            "isolated",
            "marginCoin":            "USDT",
            "size":                  str(size_btc),
            "side":                  side,
            "tradeSide":             "open",
            "orderType":             "market",
            "presetStopLossPrice":   str(round(stop, 2)),
            "presetTakeProfitPrice": str(round(target, 2)),
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

    async def close_position(self, session: aiohttp.ClientSession, direction: int) -> dict:
        """Close the open long or short position at market."""
        if self.paper or not self.enabled:
            return {"paper": True}

        hold_side = "long" if direction == 1 else "short"
        body = json.dumps({
            "symbol":      "BTCUSDT",
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

    async def get_position(self, session: aiohttp.ClientSession) -> dict:
        """Return current open BTC position (empty dict if none)."""
        if not self.enabled:
            return {}
        path = "/api/v2/mix/position/singlePosition?symbol=BTCUSDT&productType=USDT-FUTURES&marginCoin=USDT"
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
    def __init__(self, executor: Optional[BitgetExecutor] = None):
        self.live_price = LivePrice()
        self.price_history = PriceHistory()
        self.db = Database()
        self.sim = SimState()
        self.tick_count = 0
        self.polymarket = PolymarketState()
        self.executor = executor or BitgetExecutor()
        self.live_trade = LiveTradeState()

    async def update_polymarket(self, session: aiohttp.ClientSession):
        """Fetch fresh Polymarket sentiment."""
        self.polymarket = await fetch_polymarket(session)

    def get_polymarket(self) -> PolymarketState:
        return self.polymarket

    def get_live_trade(self) -> LiveTradeState:
        return self.live_trade

    async def open_live_trade(self, session: aiohttp.ClientSession,
                               sig: dict, size_usdt: float = 1.0) -> LiveTradeState:
        """Open a real or paper trade from a signal dict."""
        if self.live_trade.active:
            return self.live_trade
        direction = 1 if sig["direction"] == "LONG" else -1
        result = await self.executor.place_order(
            session, direction,
            size_usdt, sig["entry"], sig["stop"], sig["target"]
        )
        self.live_trade = LiveTradeState(
            active=True,
            order_id=result.get("orderId", ""),
            direction=direction,
            entry=sig["entry"],
            stop=sig["stop"],
            target=sig["target"],
            size_usdt=size_usdt,
            pattern=sig["pattern"],
            opened_at=time.time(),
            paper=self.executor.paper,
        )
        return self.live_trade

    async def check_and_close_live_trade(self, session: aiohttp.ClientSession) -> Optional[dict]:
        """
        Check if active trade hit TP/SL. Returns close-event dict or None.
        For paper trades uses live price; for real trades checks Bitget position.
        """
        if not self.live_trade.active:
            return None

        lt = self.live_trade
        price = self.live_price.btc

        if lt.paper:
            hit_tp = (lt.direction == 1 and price >= lt.target) or \
                     (lt.direction == -1 and price <= lt.target)
            hit_sl = (lt.direction == 1 and price <= lt.stop) or \
                     (lt.direction == -1 and price >= lt.stop)
            if not (hit_tp or hit_sl):
                return None
            win = hit_tp
            pnl_pct = ((price - lt.entry) / lt.entry * 100 * lt.direction)
        else:
            pos = await self.executor.get_position(session)
            if pos:
                return None  # still open
            win = price > lt.entry if lt.direction == 1 else price < lt.entry
            pnl_pct = round((price - lt.entry) / lt.entry * 100 * lt.direction, 2)

        pnl_usdt = round(lt.size_usdt * pnl_pct / 100, 2)
        duration = round((time.time() - lt.opened_at) / 60, 1)
        event = {
            "pattern":   lt.pattern,
            "direction": "LONG" if lt.direction == 1 else "SHORT",
            "entry":     lt.entry,
            "exit":      round(price, 2),
            "pnl_pct":   round(pnl_pct, 2),
            "pnl_usdt":  pnl_usdt,
            "win":       win,
            "duration":  duration,
            "paper":     lt.paper,
        }
        self.live_trade = LiveTradeState()  # reset
        return event

    async def get_live_balance(self, session: aiohttp.ClientSession) -> float:
        return await self.executor.get_balance(session)

    async def update_price(self, session: aiohttp.ClientSession):
        """Fetch fresh BTC price from Binance."""
        data = await fetch_price("BTCUSDT", session)
        if data and "lastPrice" in data:
            self.live_price.btc   = float(data["lastPrice"])
            self.live_price.chg   = float(data["priceChangePercent"])
            self.live_price.high  = float(data["highPrice"])
            self.live_price.low   = float(data["lowPrice"])
            self.live_price.vol   = float(data["volume"]) * self.live_price.btc
            self.live_price.fresh = True
            self.live_price.last_fetch = time.time()

    def tick(self) -> Optional[dict]:
        """
        One engine tick. Returns trade dict if a trade was executed, else None.
        """
        self.tick_count += 1
        self.price_history.tick(self.live_price.btc)

        a = get_astro()
        r = get_readings(self.price_history.closes, self.live_price, self.polymarket.bull_pct)

        rec = self.db.find_best(r, a)
        trade = None
        if rec and self.sim.balance > MIN_BALANCE:
            trade = execute_trade(self.sim, self.db, rec, r, a)

        return trade

    def get_signal(self) -> Optional[dict]:
        """Get the current best signal without executing a trade."""
        a = get_astro()
        r = get_readings(self.price_history.closes, self.live_price, self.polymarket.bull_pct)
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

        edge = calc_edge(self.db, self.sim.wins, self.sim.total)
        conf = min(95, max(40, round(rec.acc * 0.6 + edge.score * 0.4)))

        why = []
        a2 = a  # already computed
        if a2.moon_bull and pat.direction == 1:  why.append(f"{a2.moon_emoji} {a2.moon_phase} — waxing bull cycle")
        if a2.moon_bear and pat.direction == -1: why.append(f"{a2.moon_emoji} {a2.moon_phase} — waning bear cycle")
        if a2.merc_rx:   why.append("☿ Mercury Retrograde — reduce size 40%")
        if a2.equinox:   why.append("⚡ Equinox active — inflection window")
        if r.rsi < 32:   why.append(f"RSI {r.rsi} — oversold, demand zone")
        if r.rsi > 68:   why.append(f"RSI {r.rsi} — overbought, distribution risk")
        if r.wick_dn:    why.append("Wick rejection — liquidity grab")
        if r.vol_r > 1.8:why.append(f"Volume {r.vol_r:.1f}× — conviction move")
        if r.funding < -0.025: why.append(f"Funding {r.funding:.3f}% — squeeze building")
        if self.polymarket.fresh and r.poly_bull > 62: why.append(f"Polymarket crowd {r.poly_bull}% bull")
        if self.polymarket.fresh and r.poly_bull < 38: why.append(f"Polymarket crowd {r.poly_bull}% bull — bearish")

        return {
            "pattern":    pat.name,
            "category":   pat.cat,
            "direction":  "LONG" if pat.direction == 1 else "SHORT" if pat.direction == -1 else "NEUTRAL",
            "dir_arrow":  "▲" if pat.direction == 1 else "▼",
            "accuracy":   rec.acc,
            "ev":         rec.ev,
            "observations": rec.occ,
            "confidence": conf,
            "entry":      round(price, 2),
            "stop":       round(stop, 2),
            "target":     round(target, 2),
            "rr":         rr,
            "leverage":   get_lev(rec.acc),
            "size_pct":   min(20, max(5, round((rec.acc - 42) / 2))),
            "why":        why[:4],
            "ingredients":pat.ingredients,
            "merc_rx":    a2.merc_rx,
        }

    def get_edge(self) -> EdgeScore:
        return calc_edge(self.db, self.sim.wins, self.sim.total)

    def get_astro(self) -> AstroState:
        return get_astro()

    def get_summary(self) -> dict:
        edge = self.get_edge()
        return {
            "balance":    round(self.sim.balance, 2),
            "peak":       round(self.sim.peak, 2),
            "pnl_pct":    self.sim.pnl_pct,
            "drawdown":   self.sim.drawdown,
            "wr":         self.sim.wr,
            "total":      self.sim.total,
            "wins":       self.sim.wins,
            "losses":     self.sim.losses,
            "cons_loss":  self.sim.cons_loss,
            "cons_win":   self.sim.cons_win,
            "edge_score": edge.score,
            "edge_status":edge.status,
            "db_size":    edge.db_size,
            "elite":      edge.elite,
            "btc":        round(self.live_price.btc, 2),
            "btc_chg":    round(self.live_price.chg, 2),
            "astro":      self.get_astro(),
            "last_trades":self.sim.trades[:5],
        }
