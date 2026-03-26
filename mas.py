"""
PLATINIUM MAS BRAIN
Multi-Agent System — continuous research, discovery, and pattern learning
Runs as background async tasks alongside the bot.
"""

import asyncio
import json
import logging
import os
import time
import collections
from dataclasses import dataclass, field
from typing import Optional
import aiohttp

log = logging.getLogger(__name__)

IDEAS_FILE = os.getenv("IDEAS_FILE", "platinium_ideas.json")


# ═══════════════════════════════════════════════
# DISCOVERY
# ═══════════════════════════════════════════════

@dataclass
class Discovery:
    agent:      str
    title:      str
    body:       str
    bias:       str   # BULL | BEAR | NEUTRAL | PATTERN
    confidence: int   # 0-100
    ts:         float = field(default_factory=time.time)

    def age_str(self) -> str:
        s = int(time.time() - self.ts)
        if s < 60:   return f"{s}s ago"
        if s < 3600: return f"{s // 60}m ago"
        return f"{s // 3600}h ago"

    def to_dict(self) -> dict:
        return {
            "agent":      self.agent,
            "title":      self.title,
            "body":       self.body,
            "bias":       self.bias,
            "confidence": self.confidence,
            "ts":         self.ts,
            "age":        self.age_str(),
        }


# ═══════════════════════════════════════════════
# MAS BRAIN
# ═══════════════════════════════════════════════

class MASBrain:
    def __init__(self, engine):
        self.engine = engine
        self.discoveries: collections.deque = collections.deque(maxlen=500)
        self.ideas: list = []
        self._sse_queues: list = []
        self._load_ideas()

    # ── PERSISTENCE ─────────────────────────────

    def _save_ideas(self):
        try:
            tmp = IDEAS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.ideas, f)
            os.replace(tmp, IDEAS_FILE)
        except Exception as e:
            log.warning(f"Ideas save failed: {e}")

    def _load_ideas(self):
        if not os.path.exists(IDEAS_FILE):
            return
        try:
            with open(IDEAS_FILE) as f:
                self.ideas = json.load(f)
            log.info(f"Loaded {len(self.ideas)} brainstorm ideas")
        except Exception as e:
            log.warning(f"Ideas load failed: {e}")

    # ── SSE ─────────────────────────────────────

    def push(self, d: Discovery):
        self.discoveries.appendleft(d)
        log.info(f"[MAS:{d.agent}] {d.title} [{d.bias} {d.confidence}%]")
        dead = []
        for q in self._sse_queues:
            try:
                q.put_nowait(d)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._sse_queues.remove(q)

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=50)
        self._sse_queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._sse_queues.remove(q)
        except ValueError:
            pass

    # ── AGENT: FEAR & GREED ─────────────────────

    async def _agent_fear_greed(self, session: aiohttp.ClientSession):
        try:
            url = "https://api.alternative.me/fng/?limit=2"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json(content_type=None)
            vals  = data["data"]
            now   = int(vals[0]["value"])
            prev  = int(vals[1]["value"]) if len(vals) > 1 else now
            label = vals[0]["value_classification"]
            delta = now - prev
            d_str = ("+" if delta >= 0 else "") + str(delta)

            if now <= 20:
                bias, conf = "BULL", min(88, 60 + (25 - now))
                title = f"Extreme Fear ({now}) — contrarian LONG zone"
                body  = (f"Fear & Greed at {now} ({label}). "
                         f"Historically precedes 3-7% recoveries within 48-72h. "
                         f"Delta vs yesterday: {d_str}. Watch for bullish pattern confirmation.")
            elif now >= 80:
                bias, conf = "BEAR", min(88, 60 + (now - 75))
                title = f"Extreme Greed ({now}) — reversal risk HIGH"
                body  = (f"Fear & Greed at {now} ({label}). "
                         f"Overbought crowd sentiment. Tighten stops on longs. "
                         f"Delta vs yesterday: {d_str}. Consider shorts on signal.")
            elif now <= 35:
                bias, conf = "BULL", 56
                title = f"Fear zone ({now}) — watch for entries"
                body  = (f"Market sentiment fearful ({label}, {now}). "
                         f"Smart money historically accumulates in this range. "
                         f"Confirm with technicals before entry.")
            elif now >= 65:
                bias, conf = "NEUTRAL", 48
                title = f"Greed zone ({now}) — be selective"
                body  = (f"Fear & Greed at {now} ({label}). "
                         f"Market euphoria building. Quality setups only, "
                         f"avoid chasing breakouts.")
            else:
                bias, conf = "NEUTRAL", 42
                title = f"Sentiment neutral ({now} — {label})"
                body  = f"Fear & Greed at {now}. No strong contrarian signal. Follow technicals."

            self.push(Discovery("FearGreed", title, body, bias, conf))
        except Exception as e:
            log.debug(f"FearGreed agent: {e}")

    # ── AGENT: FUNDING RATE ──────────────────────

    async def _agent_funding(self, session: aiohttp.ClientSession):
        try:
            url = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
            rate = float(data["lastFundingRate"]) * 100
            mark = float(data["markPrice"])

            if rate > 0.08:
                bias, conf = "BEAR", min(85, 55 + int((rate - 0.08) * 200))
                title = f"Funding HIGH ({rate:.4f}%) — longs overextended"
                body  = (f"BTC perpetual funding at {rate:.4f}%. "
                         f"Longs paying heavily. When funding > 0.08% shorts become cheap. "
                         f"Expect long flush if price stalls. Mark: ${mark:,.0f}")
            elif rate < -0.03:
                bias, conf = "BULL", min(85, 55 + int(abs(rate + 0.03) * 400))
                title = f"Funding NEGATIVE ({rate:.4f}%) — short squeeze loading"
                body  = (f"BTC funding at {rate:.4f}%. Shorts paying to stay in. "
                         f"Negative funding + any bullish catalyst = violent squeeze. "
                         f"Mark: ${mark:,.0f}")
            elif rate > 0.05:
                bias, conf = "BEAR", 54
                title = f"Funding elevated ({rate:.4f}%) — mild bearish lean"
                body  = f"Longs paying above average. Not extreme but trend exhaustion risk is higher. Mark: ${mark:,.0f}"
            else:
                bias, conf = "NEUTRAL", 40
                title = f"Funding balanced ({rate:.4f}%)"
                body  = f"BTC funding rate neutral. No directional funding pressure. Mark: ${mark:,.0f}"

            self.push(Discovery("Funding", title, body, bias, conf))
        except Exception as e:
            log.debug(f"Funding agent: {e}")

    # ── AGENT: BTC DOMINANCE ─────────────────────

    async def _agent_dominance(self, session: aiohttp.ClientSession):
        try:
            url = "https://api.coingecko.com/api/v3/global"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                data = await r.json()
            d        = data["data"]
            dom      = round(d["market_cap_percentage"]["btc"], 1)
            chg_24h  = round(d["market_cap_change_percentage_24h_usd"], 2)
            sign     = "+" if chg_24h >= 0 else ""

            if dom > 58:
                bias, conf = "BULL", 64
                title = f"BTC Dominance HIGH ({dom}%) — Bitcoin season"
                body  = (f"Capital rotating INTO Bitcoin. Alts underperforming. "
                         f"Pure BTC long setups are highest probability. "
                         f"Total market 24h: {sign}{chg_24h}%")
            elif dom < 42:
                bias, conf = "BULL", 58
                title = f"BTC Dominance LOW ({dom}%) — altcoin momentum"
                body  = (f"Dominance at {dom}% — capital dispersing into alts. "
                         f"Diversified setups across ETH/SOL/etc valid. "
                         f"Total market 24h: {sign}{chg_24h}%")
            else:
                bias, conf = "NEUTRAL", 44
                title = f"Dominance neutral ({dom}%)"
                body  = (f"BTC dominance at {dom}% — no strong rotation signal. "
                         f"Total market 24h: {sign}{chg_24h}%")

            self.push(Discovery("Dominance", title, body, bias, conf))
        except Exception as e:
            log.debug(f"Dominance agent: {e}")

    # ── AGENT: TRENDING COINS ────────────────────

    async def _agent_trending(self, session: aiohttp.ClientSession):
        try:
            url = "https://api.coingecko.com/api/v3/search/trending"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                data = await r.json()
            coins = data.get("coins", [])[:7]
            names = [c["item"]["name"] for c in coins]
            syms  = [c["item"]["symbol"].upper() for c in coins]

            self.push(Discovery(
                "TrendScanner",
                f"Trending: {', '.join(syms[:4])}",
                (f"Top trending on CoinGecko right now: {', '.join(names)}. "
                 f"Trending assets often lead 24-72h momentum moves. "
                 f"Scan {', '.join(syms[:3])} for pattern setups."),
                "BULL", 54
            ))
        except Exception as e:
            log.debug(f"Trending agent: {e}")

    # ── AGENT: PATTERN CORRELATOR ────────────────

    async def _agent_pattern_correlator(self):
        try:
            trades = self.engine.sim.trades
            if len(trades) < 8:
                return

            total = len(trades)
            wins  = sum(1 for t in trades if t.get("win"))
            wr    = round(wins / total * 100, 1)

            # Best and worst patterns in last 50 trades
            pat_stats: dict = {}
            for t in trades[:50]:
                p = t.get("pattern", "?")
                if p not in pat_stats:
                    pat_stats[p] = {"w": 0, "l": 0}
                if t.get("win"):
                    pat_stats[p]["w"] += 1
                else:
                    pat_stats[p]["l"] += 1

            ranked = sorted(
                pat_stats.items(),
                key=lambda x: x[1]["w"] / max(1, x[1]["w"] + x[1]["l"]),
                reverse=True
            )
            best_name, best_d = ranked[0]
            best_acc = round(best_d["w"] / max(1, best_d["w"] + best_d["l"]) * 100)

            # Current losing streak
            streak = 0
            for t in trades:
                if not t.get("win"):
                    streak += 1
                else:
                    break

            if streak >= 4:
                bias, conf = "NEUTRAL", 72
                title = f"WARNING: {streak}-trade losing streak"
                body  = (f"Last {streak} trades all closed at a loss. "
                         f"Pattern DB recalibrating. Reducing sim exposure. "
                         f"Overall win rate still {wr}% over {total} trades.")
            elif best_acc >= 72 and (best_d["w"] + best_d["l"]) >= 3:
                bias, conf = "BULL", 68
                title = f"Hot pattern: '{best_name}' — {best_acc}% recently"
                body  = (f"'{best_name}' outperforming: {best_d['w']}W / {best_d['l']}L "
                         f"in last 50 trades. Edge signal is sharpening. "
                         f"Overall: {wr}% WR across {total} trades.")
            else:
                bias, conf = "NEUTRAL", 48
                title = f"Pattern DB: {wr}% win rate — {total} trades logged"
                body  = (f"Database nominal. Best recent: '{best_name}' at {best_acc}%. "
                         f"Keep sampling — accuracy compounds with more data.")

            self.push(Discovery("PatternMiner", title, body, bias, conf))
        except Exception as e:
            log.debug(f"PatternCorrelator agent: {e}")

    # ── AGENT: EDGE ANALYST ──────────────────────

    async def _agent_edge_analyst(self):
        try:
            records     = list(self.engine.db.records.values())
            elite       = [r for r in records if r.acc >= 70 and r.occ >= 5]
            underperform= [r for r in records if r.acc < 40 and r.occ >= 5]
            total_obs   = sum(r.occ for r in records)
            avg_acc     = round(sum(r.acc for r in records) / len(records), 1) if records else 0

            if elite:
                top = max(elite, key=lambda r: r.score)
                title = f"Elite edge: '{top.pattern.name}' — {top.acc}% acc"
                body  = (f"Top performer: '{top.pattern.name}' "
                         f"({top.acc}% acc, EV={top.ev:+.2f}%, {top.occ} occurrences, score={top.score:.0f}). "
                         f"{len(elite)} elite patterns / {len(underperform)} underperforming. "
                         f"Total observations: {total_obs}. Avg accuracy: {avg_acc}%.")
                bias, conf = "BULL", 72
            else:
                title = f"Edge building: {total_obs} observations — avg {avg_acc}%"
                body  = (f"No elite patterns yet (need ≥70% acc + 5 occurrences). "
                         f"Sampling {len(records)} patterns. "
                         f"Edge sharpens exponentially — keep the engine running.")
                bias, conf = "NEUTRAL", 52

            self.push(Discovery("EdgeAnalyst", title, body, bias, conf))
        except Exception as e:
            log.debug(f"EdgeAnalyst agent: {e}")

    # ── AGENT: OPEN INTEREST ─────────────────────

    async def _agent_open_interest(self, session: aiohttp.ClientSession):
        try:
            url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
            oi = float(data["openInterest"])

            # Get current price for context
            price = self.engine.live_price.btc or 65000
            oi_usd = oi * price

            if oi_usd > 15_000_000_000:
                bias, conf = "BEAR", 58
                title = f"Open Interest HIGH (${oi_usd/1e9:.1f}B) — crowded market"
                body  = (f"BTC open interest at ${oi_usd/1e9:.1f}B. "
                         f"Crowded positioning increases liquidation cascade risk. "
                         f"Any sharp move will amplify quickly.")
            elif oi_usd < 8_000_000_000:
                bias, conf = "BULL", 55
                title = f"Open Interest LOW (${oi_usd/1e9:.1f}B) — room to run"
                body  = (f"OI at ${oi_usd/1e9:.1f}B — low leverage, less cascade risk. "
                         f"Moves tend to be more organic and sustained at low OI.")
            else:
                bias, conf = "NEUTRAL", 42
                title = f"Open Interest neutral (${oi_usd/1e9:.1f}B)"
                body  = f"BTC OI at ${oi_usd/1e9:.1f}B. Normal market conditions."

            self.push(Discovery("OpenInterest", title, body, bias, conf))
        except Exception as e:
            log.debug(f"OpenInterest agent: {e}")

    # ── MAIN LOOP ────────────────────────────────

    async def run_loop(self, session: aiohttp.ClientSession):
        log.info("MAS Brain starting — all agents online")

        # (interval_seconds, coroutine_factory)
        SCHEDULE = [
            (300,  lambda: self._agent_fear_greed(session)),
            (180,  lambda: self._agent_funding(session)),
            (900,  lambda: self._agent_dominance(session)),
            (600,  lambda: self._agent_pattern_correlator()),
            (1800, lambda: self._agent_trending(session)),
            (1200, lambda: self._agent_edge_analyst()),
            (360,  lambda: self._agent_open_interest(session)),
        ]

        last_run = [0.0] * len(SCHEDULE)

        # Fire all agents once at startup (staggered to avoid rate limits)
        for i, (_, factory) in enumerate(SCHEDULE):
            try:
                await factory()
                last_run[i] = time.time()
            except Exception as e:
                log.warning(f"MAS startup agent {i}: {e}")
            await asyncio.sleep(1.5)

        # Main loop
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for i, (interval, factory) in enumerate(SCHEDULE):
                if now - last_run[i] >= interval:
                    try:
                        await factory()
                        last_run[i] = now
                    except Exception as e:
                        log.warning(f"MAS agent {i}: {e}")
