"""
PLATINIUM TELEGRAM BOT
Production-ready bot — signals, edge, equity, astro alerts, daily summary
Run: python bot.py
"""

import os
import asyncio
import logging
import time
from datetime import datetime, timezone, time as dtime
from typing import Optional

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, ApplicationBuilder
)
from telegram.constants import ParseMode

from engine import (
    PlatiniumEngine, fetch_price, fetch_klines, get_astro,
    get_readings, PATTERNS, calc_edge, LivePrice,
    BitgetExecutor, LiveTradeState
)

# ── CONFIG ──────────────────────────────────────────────────────
TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_TOKEN_HERE")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Bitget execution
BITGET_API_KEY    = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")
PAPER_MODE         = os.getenv("PAPER_MODE", "true").lower() != "false"
TRADE_CAPITAL_USDT = float(os.getenv("TRADE_CAPITAL_USDT", "1"))   # editable via /settrade
DEMO_BALANCE_USDT  = float(os.getenv("DEMO_BALANCE_USDT", "100"))  # starting demo wallet

# Alert thresholds
SIGNAL_MIN_CONF   = int(os.getenv("MIN_CONFIDENCE", "62"))   # minimum confidence to alert
SIGNAL_MIN_ACC    = int(os.getenv("MIN_ACCURACY", "60"))     # minimum accuracy to alert
EDGE_ALERT_LEVELS = [50, 70, 85]                             # alert when edge crosses these
EQUITY_ALERT_PCT  = float(os.getenv("EQUITY_ALERT_PCT", "5"))# alert every X% balance change

# Intervals (seconds)
TICK_INTERVAL  = int(os.getenv("TICK_INTERVAL", "30"))   # engine tick every 30s
PRICE_INTERVAL = int(os.getenv("PRICE_INTERVAL", "15"))  # price fetch every 15s
DAILY_HOUR     = int(os.getenv("DAILY_HOUR", "8"))        # daily summary at 08:00 UTC

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── GLOBALS ──────────────────────────────────────────────────────
_executor = BitgetExecutor(
    api_key=BITGET_API_KEY,
    secret=BITGET_API_SECRET,
    passphrase=BITGET_PASSPHRASE,
    paper=PAPER_MODE,
)
engine = PlatiniumEngine(
    executor=_executor,
    trade_size_usdt=TRADE_CAPITAL_USDT,
    demo_balance=DEMO_BALANCE_USDT,
)
last_signal_id: Optional[str] = None
last_edge_level: int = 0
last_equity_alert: float = 100.0
last_astro_flags: dict = {"merc_rx": False, "equinox": False, "moon_idx": -1}
subscribed_chats: set = set()
session: Optional[aiohttp.ClientSession] = None


# ── EMOJI / FORMAT HELPERS ───────────────────────────────────────

def dir_emoji(direction: str) -> str:
    return "🟢" if direction == "LONG" else "🔴" if direction == "SHORT" else "⚪"

def edge_emoji(status: str) -> str:
    return {"BLURRY":"🔵","FORMING":"🟡","SHARP":"🟠","ELITE":"🟢"}.get(status,"⚫")

def pnl_emoji(pct: float) -> str:
    if pct > 10: return "🚀"
    if pct > 0:  return "📈"
    if pct < -10:return "🔥"
    return "📉"

def fmt_num(n: float, prefix: str = "$") -> str:
    if abs(n) >= 1000:
        return f"{prefix}{n:,.0f}"
    if abs(n) >= 1:
        return f"{prefix}{n:.2f}"
    return f"{prefix}{n:.4f}"

def escape(txt: str) -> str:
    """Escape for MarkdownV2."""
    chars = r'_*[]()~`>#+-=|{}.!'
    for c in chars:
        txt = txt.replace(c, f'\\{c}')
    return txt


# ── MESSAGE FORMATTERS ───────────────────────────────────────────

def fmt_signal(sig: dict, lp_btc: float) -> str:
    d  = sig["direction"]
    de = dir_emoji(d)
    ev_sign = "+" if sig['ev'] > 0 else ""
    lines = [
        f"{de} *{escape(d)} — {escape(sig['pattern'])}*",
        f"",
        f"📊 *Category:* {escape(sig['category'])}",
        f"🎯 *Accuracy:* {sig['accuracy']}%",
        f"⚡ *Confidence:* {sig['confidence']}/100",
        f"📈 *Avg EV:* {escape(f'{ev_sign}{sig[\"ev\"]}%')}",
        f"👁 *Seen:* {sig['observations']} times",
        f"",
        f"💰 *Entry:* {escape(fmt_num(sig['entry']))}",
        f"🛑 *Stop Loss:* {escape(fmt_num(sig['stop']))}",
        f"🎯 *Target:* {escape(fmt_num(sig['target']))}",
        f"⚖️ *R:R:* {escape(str(sig['rr']))}:1",
        f"🔢 *Leverage:* {sig['leverage']}×",
    ]
    if sig.get("why"):
        lines += ["", "🧠 *Why:*"]
        for w in sig["why"]:
            lines.append(f"  • {escape(w)}")
    if sig.get("merc_rx"):
        lines += ["", "⚠️ *Mercury Retrograde active — reduce size 40%*"]
    lines += ["", f"₿ BTC: {escape(fmt_num(lp_btc))}"]
    return "\n".join(lines)


def fmt_edge(edge) -> str:
    bar_filled = round(edge.score / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    return "\n".join([
        f"{edge_emoji(edge.status)} *Edge Score: {edge.score}/100 — {escape(edge.status)}*",
        f"",
        f"`{bar}` {edge.score}%",
        f"",
        f"📦 Patterns in DB: *{edge.db_size}*",
        f"👁 Total observations: *{edge.total_obs}*",
        f"🏆 Elite patterns \\(≥70%\\): *{edge.elite}*",
        f"✅ Sim win rate: *{edge.consistency}%*",
    ])


def fmt_equity(sim_summary: dict) -> str:
    s      = sim_summary
    ep     = pnl_emoji(s["pnl_pct"])
    tag    = "📄 Demo" if PAPER_MODE else "💸 Live"
    pnl_u  = s.get("pnl_usdt", 0)
    sign_p = "+" if s["pnl_pct"] >= 0 else ""
    sign_u = "+" if pnl_u >= 0 else ""
    # escape all floats — unescaped "." breaks MarkdownV2
    bal    = escape(fmt_num(s["balance"]))
    start  = escape(fmt_num(s["start_balance"]))
    peak   = escape(fmt_num(s["peak"]))
    pct    = escape(f"{sign_p}{s['pnl_pct']}%")
    usdt   = escape(f"{sign_u}{pnl_u} USDT")
    dd     = escape(f"{s['drawdown']}%")
    wr     = escape(f"{s['wr']}%")
    lines  = [
        f"{ep} *{escape(tag)}*",
        "",
        f"💰 Balance: *{bal}*  \\(start: {start}\\)",
        f"📈 P&L: *{pct}*  \\({usdt}\\)",
        f"🏔 Peak: *{peak}*",
        f"📉 Drawdown: *{dd}*",
        f"🎯 Win Rate: *{wr}*",
        f"📊 Trades: *{s['total']}*  \\({s['wins']}W / {s['losses']}L\\)",
    ]
    if s["cons_win"] > 1:
        lines.append(f"🔥 Streak: *{s['cons_win']}W* winning")
    elif s["cons_loss"] > 1:
        lines.append(f"❄️ Streak: *{s['cons_loss']}L* losing")
    return "\n".join(lines)


def fmt_astro(a) -> str:
    return "\n".join([
        f"🌌 *Astro Update*",
        f"",
        f"{a.moon_emoji} *{escape(a.moon_phase)}*",
        f"  Phase angle: {a.moon_angle}°",
        f"  Bull bias: {'✅' if a.moon_bull else '❌'}",
        f"",
        f"☿ *Mercury: {'RETROGRADE ⚠️' if a.merc_rx else 'DIRECT ✅'}*",
        f"{'  Reduce position size 40%' if a.merc_rx else '  Clear signal conditions'}",
        f"",
        f"☉ *Sun in {escape(a.sun_sign)}*",
        f"  Fire sign energy: {'🔥' if a.fire_sign else '—'}",
        f"{'' if not a.equinox else chr(10)+'⚡ *Equinox active — inflection window*'}",
    ])


def fmt_live_trade(lt: LiveTradeState, btc: float) -> str:
    mode = "📄 PAPER" if lt.paper else "💸 LIVE"
    d = "🟢 LONG" if lt.direction == 1 else "🔴 SHORT"
    dur = round((time.time() - lt.opened_at) / 60, 1)
    dist_tp = round(abs(btc - lt.target) / lt.entry * 100, 2)
    dist_sl = round(abs(btc - lt.stop) / lt.entry * 100, 2)
    return "\n".join([
        f"⚡ *Open Trade — {escape(mode)}*",
        f"",
        f"{d} — {escape(lt.pattern)}",
        f"",
        f"📥 *Entry:*  {escape(fmt_num(lt.entry))}",
        f"₿ *Now:*   {escape(fmt_num(btc))}",
        f"🎯 *Target:* {escape(fmt_num(lt.target))} \\({escape(str(dist_tp))}% away\\)",
        f"🛑 *Stop:*   {escape(fmt_num(lt.stop))} \\({escape(str(dist_sl))}% away\\)",
        f"💼 *Size:*   {escape(fmt_num(lt.size_usdt))}",
        f"⏱ *Open:*  {escape(str(dur))}m",
    ])


def fmt_trade_close(ev: dict) -> str:
    e    = "✅" if ev["win"] else "❌"
    mode = "📄 DEMO" if ev["paper"] else "💸 LIVE"
    pnl  = escape(f"{'+'  if ev['pnl_pct']  >= 0 else ''}{ev['pnl_pct']}%")
    usdt = escape(f"{'+'  if ev['pnl_usdt'] >= 0 else ''}{ev['pnl_usdt']} USDT")
    lines = [
        f"{e} *Trade Closed — {escape(mode)}*",
        f"",
        f"{'🟢' if ev['direction']=='LONG' else '🔴'} {escape(ev['direction'])} — {escape(ev['pattern'])}",
        f"",
        f"📥 Entry:    {escape(fmt_num(ev['entry']))}",
        f"📤 Exit:     {escape(fmt_num(ev['exit']))}",
        f"💰 P&L:      *{pnl}*  \\({usdt}\\)",
        f"⏱ Duration: {ev['duration']}m",
    ]
    if ev.get("demo_balance") is not None:
        lines.append(f"💼 Demo wallet: *{escape(fmt_num(ev['demo_balance']))}*")
    return "\n".join(lines)


def fmt_daily(summary: dict) -> str:
    s = summary
    a = s["astro"]
    top_pat = None
    best_acc = 0
    for rec in engine.db.records.values():
        if rec.occ >= 3 and rec.acc > best_acc:
            best_acc = rec.acc
            top_pat = rec
    return "\n".join([
        f"📋 *PLATINIUM Daily Summary*",
        f"_{escape(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}_",
        f"",
        f"💰 *Balance:* {escape(fmt_num(s['balance']))} \\({escape(('+' if s['pnl_pct']>=0 else '')+str(s['pnl_pct'])+'%')}\\)",
        f"🏔 *Peak:* {escape(fmt_num(s['peak']))}",
        f"📉 *Max DD:* {escape(str(s['drawdown'])+'%')}",
        f"🎯 *Win Rate:* {escape(str(s['wr'])+'%')} \\({s['wins']}W/{s['losses']}L\\)",
        f"📊 *Trades:* {s['total']}",
        f"",
        f"🔬 *Edge Score:* {s['edge_score']}/100 — {escape(s['edge_status'])}",
        f"📦 *DB Patterns:* {s['db_size']}/{len(PATTERNS)}",
        f"🏆 *Elite Patterns:* {s['elite']}",
        f"",
        f"🏅 *Best Pattern:* {escape(top_pat.pattern.name) if top_pat else '—'} \\({best_acc}% acc\\)",
        f"",
        f"{a.moon_emoji} *{escape(a.moon_phase)}*",
        f"☿ Mercury: {'⚠️ RETROGRADE' if a.merc_rx else '✅ DIRECT'}",
        f"₿ BTC: {escape(fmt_num(s['btc']))} \\({escape(('+' if s['btc_chg']>=0 else '')+str(s['btc_chg'])+'%')}\\)",
    ])


# ── KEYBOARD HELPERS ─────────────────────────────────────────────

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 Signal",  callback_data="signal"),
         InlineKeyboardButton("⚡ Trades",  callback_data="trades")],
        [InlineKeyboardButton("💰 Wallet",  callback_data="equity"),
         InlineKeyboardButton("🔬 Edge",    callback_data="edge")],
        [InlineKeyboardButton("🔍 Search",  callback_data="search_prompt"),
         InlineKeyboardButton("🌌 Astro",   callback_data="astro")],
        [InlineKeyboardButton("📋 Summary", callback_data="summary"),
         InlineKeyboardButton("⚙️ Status",  callback_data="status")],
    ])


def coin_keyboard():
    coins = ["BTC","ETH","SOL","BNB","XRP","AVAX","DOGE","LINK","ADA","DOT"]
    rows = []
    for i in range(0, len(coins), 5):
        rows.append([InlineKeyboardButton(c, callback_data=f"coin_{c}USDT_{c}") for c in coins[i:i+5]])
    rows.append([InlineKeyboardButton("◀ Back", callback_data="back")])
    return InlineKeyboardMarkup(rows)


# ── COMMAND HANDLERS ─────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    subscribed_chats.add(chat_id)
    txt = "\n".join([
        "🔱 *PLATINIUM* — Live Trading Intelligence",
        "",
        f"The lens is running\\. I scan {len(PATTERNS)} patterns across:",
        "candlesticks, price action, Wyckoff, on\\-chain,",
        "Fibonacci, Elliott, astrology, and macro\\.",
        "",
        "I send you alerts when:",
        "• 🔥 A high\\-confidence signal fires",
        "• 📊 Edge score crosses a new level",
        "• 🌌 Major astro event begins",
        "• 📋 Daily P\\&L summary \\(08:00 UTC\\)",
        "",
        "Use the buttons below or type /help",
    ])
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2,
                                     reply_markup=main_keyboard())


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = "\n".join([
        "🔱 *PLATINIUM Commands*",
        "",
        "/signal — Best signal right now",
        "/trades — Open trade status",
        "/equity — Demo wallet P&L",
        "/edge — Pattern learning stats",
        "/astro — Moon \\& Mercury conditions",
        "/search — Scan any coin",
        "/summary — Full daily report",
        "/status — Bot health",
        "/settrade — Set \\$ per trade",
        "/start — Subscribe to alerts",
        "/stop — Unsubscribe",
        "",
        "Or use the buttons below\\.",
    ])
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2,
                                     reply_markup=main_keyboard())


async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sig = engine.get_signal()
    if not sig:
        await update.message.reply_text(
            "🔵 *Lens scanning\\.\\.\\.*\n\nNo high\\-confidence pattern right now\\.\nThe bot is learning — patterns will fire when the edge is sharp enough\\.",
            parse_mode=ParseMode.MARKDOWN_V2)
        return
    await update.message.reply_text(
        fmt_signal(sig, engine.live_price.btc),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard())


async def cmd_edge(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    edge = engine.get_edge()
    await update.message.reply_text(
        fmt_edge(edge), parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard())


async def cmd_equity(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    summary = engine.get_summary()
    await update.message.reply_text(
        fmt_equity(summary), parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard())


async def cmd_astro(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    a = engine.get_astro()
    await update.message.reply_text(
        fmt_astro(a), parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard())


async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    summary = engine.get_summary()
    await update.message.reply_text(
        fmt_daily(summary), parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard())


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "🔍 *Search a coin*\n\nUsage: `/search BTC` or `/search ETH`\n\nOr tap a quick button:",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=coin_keyboard())
        return
    symbol = args[0].upper().replace("USDT","") + "USDT"
    await do_coin_search(update.message.reply_text, symbol, symbol.replace("USDT",""))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    subscribed_chats.discard(update.effective_chat.id)
    await update.message.reply_text(
        "✅ Unsubscribed from PLATINIUM alerts\\.\nType /start to re\\-subscribe\\.",
        parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uptime = time.time() - start_time
    h = int(uptime // 3600)
    m = int((uptime % 3600) // 60)
    s = engine.get_summary()
    lt = engine.get_live_trade()
    trade_line = f"⚡ Trade: *{'OPEN — ' + escape(lt.pattern) if lt.active else 'none'}*"
    mode_line  = f"💸 Mode: *{'PAPER' if PAPER_MODE else 'LIVE — REAL MONEY'}*"
    bitget_line = f"🔑 Bitget: *{'✅ ' + ('paper' if PAPER_MODE else 'live') if _executor.enabled else '⚠️ keys not set'}*"
    txt = "\n".join([
        "✅ *PLATINIUM Status*",
        "",
        f"⏱ Uptime: *{h}h {m}m*",
        f"🎯 Tick count: *{engine.tick_count}*",
        f"💰 Sim Balance: *{escape(fmt_num(s['balance']))}*",
        f"₿ BTC: *{escape(fmt_num(s['btc']))}* \\({escape(('+' if s['btc_chg']>=0 else '')+str(s['btc_chg'])+'%')}\\)",
        f"📦 DB: *{s['db_size']}/{len(PATTERNS)} patterns*",
        f"🔬 Edge: *{s['edge_score']} — {escape(s['edge_status'])}*",
        f"📡 Live price: *{'✅' if engine.live_price.fresh else '⚠️ simulated'}*",
        f"👥 Subscribers: *{len(subscribed_chats)}*",
        f"",
        trade_line,
        mode_line,
        bitget_line,
    ])
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_settrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global TRADE_CAPITAL_USDT
    user_id = update.effective_user.id
    if ADMIN_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text(
            f"💼 *Current trade size: {escape(fmt_num(TRADE_CAPITAL_USDT))}*\n\n"
            f"Usage: `/settrade 5` to set \\$5 per trade",
            parse_mode=ParseMode.MARKDOWN_V2)
        return
    try:
        val = float(args[0])
        if val <= 0:
            raise ValueError
        TRADE_CAPITAL_USDT = val
        engine.trade_size_usdt = val
        log.info(f"Trade capital set to ${val} by user {user_id}")
        await update.message.reply_text(
            f"✅ *Trade size set to {escape(fmt_num(TRADE_CAPITAL_USDT))} per trade*",
            parse_mode=ParseMode.MARKDOWN_V2)
    except ValueError:
        await update.message.reply_text("❌ Invalid amount\\. Example: `/settrade 5`",
                                        parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lt = engine.get_live_trade()
    if not lt.active:
        mode = "PAPER" if PAPER_MODE else "LIVE"
        bitget_ok = "✅ Connected" if _executor.enabled else "⚠️ No keys set"
        await update.message.reply_text(
            f"📭 *No open trade right now*\n\n"
            f"Mode: *{escape(mode)}*\n"
            f"Bitget: *{escape(bitget_ok)}*\n"
            f"Capital per trade: *{escape(fmt_num(TRADE_CAPITAL_USDT))}*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_keyboard())
        return
    await update.message.reply_text(
        fmt_live_trade(lt, engine.live_price.btc),
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=main_keyboard())


# ── CALLBACK QUERY HANDLER ───────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    async def edit(text: str, keyboard=None):
        """Edit the existing message in-place — no new message spam."""
        kb = keyboard if keyboard is not None else main_keyboard()
        try:
            await query.message.edit_text(
                text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb,
            )
        except Exception as exc:
            err = str(exc)
            if "message is not modified" in err:
                return  # identical content — silently ignore
            # Real error (e.g. parse error, flood limit) — log it and send plain text fallback
            log.error(f"edit_text failed [{data}]: {exc}")
            try:
                # Strip markdown and send as plain text so user sees something
                plain = text.replace("*", "").replace("_", "").replace("\\", "").replace("`", "")
                await query.message.edit_text(plain, reply_markup=kb)
            except Exception:
                pass  # if even plain text fails, nothing we can do

    if data == "signal":
        sig = engine.get_signal()
        if not sig:
            await edit("🔵 *Scanning\\.\\.\\.*\n\nNo high\\-confidence pattern right now\\.\nCheck back soon\\.")
        else:
            await edit(fmt_signal(sig, engine.live_price.btc))

    elif data == "trades":
        lt = engine.get_live_trade()
        if lt.active:
            await edit(fmt_live_trade(lt, engine.live_price.btc))
        else:
            mode    = "PAPER" if PAPER_MODE else "LIVE"
            bitget  = "✅ Connected" if _executor.enabled else "⚠️ Keys not set"
            await edit(
                f"📭 *No open trade*\n\n"
                f"Mode: *{escape(mode)}*\n"
                f"Bitget: *{escape(bitget)}*\n"
                f"Size per trade: *{escape(fmt_num(TRADE_CAPITAL_USDT))}*"
            )

    elif data == "equity":
        await edit(fmt_equity(engine.get_summary()))

    elif data == "edge":
        await edit(fmt_edge(engine.get_edge()))

    elif data == "search_prompt":
        await edit("🔍 *Tap a coin to scan:*", keyboard=coin_keyboard())

    elif data == "astro":
        await edit(fmt_astro(engine.get_astro()))

    elif data == "summary":
        await edit(fmt_daily(engine.get_summary()))

    elif data == "status":
        uptime = time.time() - start_time
        h = int(uptime // 3600)
        m = int((uptime % 3600) // 60)
        s = engine.get_summary()
        lt = engine.get_live_trade()
        await edit("\n".join([
            "⚙️ *PLATINIUM Status*",
            "",
            f"⏱ Uptime: *{h}h {m}m*",
            f"🎯 Ticks: *{engine.tick_count}*",
            f"💰 Wallet: *{escape(fmt_num(s['balance']))}*",
            f"₿ BTC: *{escape(fmt_num(s['btc']))}* \\({escape(('+' if s['btc_chg']>=0 else '')+str(s['btc_chg'])+'%')}\\)",
            f"📦 DB: *{s['db_size']}/{len(PATTERNS)} patterns*",
            f"🔬 Edge: *{s['edge_score']} — {escape(s['edge_status'])}*",
            f"📡 Price feed: *{'✅ live' if engine.live_price.fresh else '⚠️ simulated'}*",
            f"👥 Subscribers: *{len(subscribed_chats)}*",
            "",
            f"⚡ Trade: *{('OPEN — ' + escape(lt.pattern)) if lt.active else 'none'}*",
            f"💸 Mode: *{'PAPER' if PAPER_MODE else 'LIVE'}*",
            f"🔑 Bitget: *{'✅' if _executor.enabled else '⚠️ keys not set'}*",
        ]))

    elif data == "back":
        await edit("🔱 *PLATINIUM* — What do you need?")

    elif data.startswith("coin_"):
        _, sym, name = data.split("_", 2)
        await do_coin_search(query.message.reply_text, sym, name)


# ── COIN SEARCH ──────────────────────────────────────────────────

async def do_coin_search(reply_fn, symbol: str, name: str):
    await reply_fn(f"🔍 *Scanning {escape(name)}\\.\\.\\.*",
                   parse_mode=ParseMode.MARKDOWN_V2)
    try:
        async with aiohttp.ClientSession() as sess:
            ticker, closes = await asyncio.gather(
                fetch_price(symbol, sess),
                fetch_klines(symbol, "1h", 80, sess),
            )

        if not ticker or "lastPrice" not in ticker:
            raise ValueError("not found")

        price = float(ticker["lastPrice"])
        chg   = float(ticker["priceChangePercent"])
        high  = float(ticker["highPrice"])
        low   = float(ticker["lowPrice"])

        fake_lp = LivePrice(btc=price, chg=chg, high=high, low=low, fresh=True)
        r = get_readings(closes or [price]*30, fake_lp)
        r.range_pos = (price - low) / (high - low) if high > low else 0.5

        a = get_astro()
        rec = engine.db.find_best(r, a)

        price_str = fmt_num(price)
        chg_str   = f"{'▲' if chg >= 0 else '▼'} {abs(chg):.2f}%"
        dir_str   = "—"
        sig_lines = []

        if rec:
            pat = rec.pattern
            d   = "LONG ▲" if pat.direction == 1 else "SHORT ▼"
            acc = rec.acc
            conf = min(95, max(40, round(acc * 0.6 + engine.get_edge().score * 0.4)))
            pstop  = round(price * (0.97 if pat.direction == 1 else 1.03), 4 if price < 1 else 2)
            ptarget= round(price * (1.04 + (acc-50)*0.001 if pat.direction == 1 else 0.96 - (acc-50)*0.001), 4 if price < 1 else 2)
            risk   = abs(price - pstop) / price
            reward = abs(ptarget - price) / price
            rr     = round(reward / risk, 2) if risk > 0 else 0
            dir_str = d

            sig_lines = [
                f"",
                f"{'🟢' if pat.direction==1 else '🔴'} *Next Move: {escape(d)}*",
                f"Pattern: {escape(pat.name)}",
                f"",
                f"Entry: `{fmt_num(price)}`",
                f"Stop:  `{fmt_num(pstop)}`",
                f"Target:`{fmt_num(ptarget)}`",
                f"R:R: *{rr}:1*",
                f"Accuracy: *{acc}%*  Confidence: *{conf}/100*",
            ]
            if a.merc_rx:
                sig_lines.append("⚠️ Mercury RX — reduce size 40%")

        rsi_bar = "▓" * round(r.rsi / 10) + "░" * (10 - round(r.rsi / 10))
        bb_pct  = round(r.bb * 100)

        lines = [
            f"*{escape(name)}/USDT*",
            f"₿ {escape(price_str)}  {escape(chg_str)}",
            f"",
            f"📊 *Indicators*",
            f"RSI 14:  `{r.rsi}` `{rsi_bar}`",
            f"BB pos:  `{bb_pct}%`",
            f"MACD:    `{'+' if r.macd >= 0 else ''}{r.macd:.3f}`",
            f"24h pos: `{round(r.range_pos*100)}%`",
            f"",
            f"{a.moon_emoji} {escape(a.moon_phase)}  {'☿⚠️' if a.merc_rx else '☿✅'}",
        ] + sig_lines

        if not rec:
            lines += ["", "⚪ *No clear signal* — wait for setup"]

        await reply_fn("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as e:
        await reply_fn(f"❌ *{escape(name)}* not found or API error\\.\nTry: BTC, ETH, SOL, etc\\.",
                       parse_mode=ParseMode.MARKDOWN_V2)
        log.error(f"Search error for {symbol}: {e}")


# ── BACKGROUND TASKS ─────────────────────────────────────────────

async def broadcast(app: Application, text: str, keyboard=None):
    """Send to all subscribed chats."""
    if not subscribed_chats:
        return
    # Also always send to configured CHAT_ID
    targets = set(subscribed_chats)
    if CHAT_ID:
        targets.add(int(CHAT_ID))
    for cid in targets:
        try:
            await app.bot.send_message(
                chat_id=cid, text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard or main_keyboard()
            )
        except Exception as e:
            log.warning(f"Broadcast failed for {cid}: {e}")


async def engine_loop(app: Application):
    """Main engine loop — ticks every TICK_INTERVAL seconds."""
    global last_signal_id, last_edge_level, last_equity_alert, last_astro_flags
    global session

    session = aiohttp.ClientSession()
    log.info("Engine loop started")

    price_tick = 0

    while True:
        try:
            # Fetch price every PRICE_INTERVAL seconds
            price_tick += TICK_INTERVAL
            if price_tick >= PRICE_INTERVAL:
                await engine.update_price(session)
                price_tick = 0

            # ── CHECK OPEN TRADE ──────────────────────
            close_ev = await engine.check_and_close_live_trade(session)
            if close_ev:
                log.info(f"Trade closed: {close_ev['direction']} {close_ev['pattern']} pnl={close_ev['pnl_pct']}%")
                await broadcast(app, fmt_trade_close(close_ev))

            # Tick the engine
            trade = engine.tick()

            # ── SIGNAL ALERT + TRADE OPEN ─────────────
            sig = engine.get_signal()
            if sig and sig["pattern"] != last_signal_id:
                if sig["confidence"] >= SIGNAL_MIN_CONF and sig["accuracy"] >= SIGNAL_MIN_ACC:
                    last_signal_id = sig["pattern"]
                    log.info(f"Signal fired: {sig['direction']} {sig['pattern']} conf={sig['confidence']}")
                    await broadcast(app, fmt_signal(sig, engine.live_price.btc))
                    # Open live / paper trade
                    if not engine.live_trade.active:
                        lt = await engine.open_live_trade(session, sig, TRADE_CAPITAL_USDT)
                        mode = "PAPER" if lt.paper else "LIVE"
                        log.info(f"Trade opened [{mode}] ${TRADE_CAPITAL_USDT}: {lt.pattern} order={lt.order_id}")
            elif not sig:
                last_signal_id = None

            # ── EDGE SCORE ALERT ──────────────────────
            edge = engine.get_edge()
            for level in EDGE_ALERT_LEVELS:
                if edge.score >= level > last_edge_level:
                    last_edge_level = level
                    log.info(f"Edge crossed {level}: {edge.status}")
                    txt = f"📊 *Edge score crossed {level}\\!*\n\n" + fmt_edge(edge)
                    await broadcast(app, txt)
                    break

            # ── EQUITY ALERT ──────────────────────────
            bal = engine.sim.balance
            pct_change = abs(bal - last_equity_alert) / last_equity_alert * 100
            if pct_change >= EQUITY_ALERT_PCT and engine.sim.total > 0:
                last_equity_alert = bal
                summary = engine.get_summary()
                log.info(f"Equity update: ${bal:.2f}")
                await broadcast(app, fmt_equity(summary))

            # ── ASTRO ALERTS ──────────────────────────
            a = engine.get_astro()
            # Mercury RX start/end
            if a.merc_rx != last_astro_flags["merc_rx"]:
                last_astro_flags["merc_rx"] = a.merc_rx
                if a.merc_rx:
                    txt = "⚠️ *Mercury Retrograde begins\\!*\n\nSignal quality reduced\\. Reduce all position sizes by 40%\\. Avoid FOMO entries\\."
                else:
                    txt = "✅ *Mercury Retrograde ends\\!*\n\nSignal quality restored\\. Normal position sizing resumes\\."
                await broadcast(app, txt)

            # Equinox
            if a.equinox and not last_astro_flags.get("equinox"):
                last_astro_flags["equinox"] = True
                txt = "⚡ *Equinox window active\\!*\n\n" + fmt_astro(a)
                await broadcast(app, txt)
            elif not a.equinox:
                last_astro_flags["equinox"] = False

            # New Moon / Full Moon
            if a.moon_idx != last_astro_flags["moon_idx"]:
                last_astro_flags["moon_idx"] = a.moon_idx
                if a.moon_idx == 0:
                    txt = f"🌑 *New Moon\\!*\n\n{escape(a.moon_phase)} — historically bullish cycle start\\. Watch for long setups\\."
                    await broadcast(app, txt)
                elif a.moon_idx == 4:
                    txt = f"🌕 *Full Moon\\!*\n\n{escape(a.moon_phase)} — reversal risk elevated\\. Tighten stops\\."
                    await broadcast(app, txt)

        except Exception as e:
            log.error(f"Engine loop error: {e}", exc_info=True)

        await asyncio.sleep(TICK_INTERVAL)


async def daily_summary_loop(app: Application):
    """Send daily summary at DAILY_HOUR UTC."""
    log.info(f"Daily summary loop started — sends at {DAILY_HOUR:02d}:00 UTC")
    last_day = -1
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == DAILY_HOUR and now.day != last_day and engine.sim.total > 0:
            last_day = now.day
            summary = engine.get_summary()
            log.info("Sending daily summary")
            await broadcast(app, fmt_daily(summary))
        await asyncio.sleep(60)


# ── MAIN ─────────────────────────────────────────────────────────

start_time = time.time()


async def post_init(app: Application):
    """Called after bot is initialized."""
    log.info("PLATINIUM bot starting...")
    # Start background tasks
    asyncio.create_task(engine_loop(app))
    asyncio.create_task(daily_summary_loop(app))
    # Send startup message to configured chat
    if CHAT_ID:
        try:
            await app.bot.send_message(
                chat_id=int(CHAT_ID),
                text="🔱 *PLATINIUM is live*\n\nEngine running\\. Patterns scanning\\. I'll alert you when signals fire\\.\n\nUse /help to see commands\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=main_keyboard()
            )
        except Exception as e:
            log.warning(f"Could not send startup message: {e}")
    log.info("PLATINIUM bot ready")


def main():
    if TOKEN == "YOUR_TOKEN_HERE":
        print("=" * 60)
        print("ERROR: No bot token found!")
        print("Set TELEGRAM_BOT_TOKEN in your .env file")
        print("Get a token from @BotFather on Telegram")
        print("=" * 60)
        return

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("edge",    cmd_edge))
    app.add_handler(CommandHandler("equity",  cmd_equity))
    app.add_handler(CommandHandler("astro",   cmd_astro))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("search",  cmd_search))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("trades",   cmd_trades))
    app.add_handler(CommandHandler("settrade", cmd_settrade))

    # Callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info(f"Starting PLATINIUM bot (token: ...{TOKEN[-8:]})")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
