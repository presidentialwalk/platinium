"""
PLATINIUM LIVE
Real-time terminal view of the engine brain — price, indicators,
on-chain data, pattern memory, and MAS discoveries.
Run: python live.py
Exit: Ctrl+C
"""

import asyncio
import time
import sys
import collections
from datetime import datetime, timezone

import aiohttp

from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.console import Console
from rich import box

import engine as eng
from engine import (
    PlatiniumEngine, BitgetExecutor,
    get_astro, get_readings, update_onchain, fetch_klines
)
from mas import MASBrain

console = Console()

# ── LIVE LOG ──────────────────────────────────────────────────────
_log: collections.deque = collections.deque(maxlen=14)

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    _log.append(f"[dim]{ts}[/dim]  {msg}")


# ══════════════════════════════════════════════════════════════════
# PANEL BUILDERS
# ══════════════════════════════════════════════════════════════════

def panel_header(e: PlatiniumEngine) -> Panel:
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%S UTC")
    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="center")
    grid.add_column(justify="right")
    grid.add_row(
        "[bold bright_magenta]🔱  PLATINIUM  LIVE[/bold bright_magenta]",
        f"[bold green]▶  RUNNING[/bold green]   tick [bold]{e.tick_count}[/bold]",
        f"[dim]{now}[/dim]",
    )
    return Panel(grid, border_style="bright_magenta", padding=(0, 2))


def panel_engine(e: PlatiniumEngine) -> Panel:
    lp   = e.live_price
    a    = get_astro()
    r    = get_readings(e.price_history.closes, lp)
    s    = e.get_summary()
    edge = e.get_edge()
    oc   = eng._onchain

    # colours
    chg_c   = "green" if lp.chg >= 0 else "red"
    rsi_c   = "red" if r.rsi > 70 else ("green" if r.rsi < 30 else "white")
    fund_c  = "green" if r.funding < -0.02 else ("red" if r.funding > 0.05 else "yellow")
    flow_c  = "green" if r.chain_flow > 0.2 else ("red" if r.chain_flow < -0.2 else "yellow")
    bal_c   = "green" if s["pnl_pct"] >= 0 else "red"
    src     = "[green]LIVE[/green]" if (oc.fresh and time.time() - oc.updated_at < 600) else "[yellow]FALLBACK[/yellow]"

    rsi_bar = "█" * int(r.rsi // 10) + "░" * (10 - int(r.rsi // 10))

    g = Table.grid(padding=(0, 1))
    g.add_column(style="dim", width=12)
    g.add_column(min_width=22)

    def row(k, v): g.add_row(k, v)

    row("BTC",      f"[bold]${lp.btc:>10,.2f}[/bold]  [{chg_c}]{lp.chg:+.2f}%[/]")
    row("High/Low", f"${lp.high:,.0f}  /  ${lp.low:,.0f}")
    row("", "")
    row("RSI",      f"[{rsi_c}]{r.rsi:>5.1f}[/]  [dim]{rsi_bar}[/]")
    row("BB pos",   f"{r.bb * 100:>5.1f}%")
    row("MACD",     f"{r.macd:>+9.5f}")
    row("Volume",   f"{r.vol_r:>5.2f}×")
    row("", "")
    row("Funding",  f"[{fund_c}]{r.funding:>+8.4f}%[/]   {src}")
    row("Flow",     f"[{flow_c}]{r.chain_flow:>+7.3f}[/]")
    row("Whale",    "[green]✅  accumulating[/green]" if r.whale_buy else "[dim]⚪  neutral[/dim]")
    row("", "")
    row("Balance",  f"[{bal_c}]${s['balance']:>8.4f}[/]")
    row("P&L",      f"[{bal_c}]{s['pnl_pct']:>+.1f}%   ${s.get('pnl_usdt', 0):>+.4f}[/]")
    row("Trades",   f"{s['total']}   ({s['wins']}W / {s['losses']}L)")
    row("Win Rate", f"[green]{s['wr']}%[/green]")
    row("Drawdown", f"[red]{s['drawdown']}%[/red]")
    row("", "")
    row("Edge",     f"[bright_magenta]{edge.score}/100  {edge.status}[/bright_magenta]")
    row("Moon",     f"{a.moon_emoji}  {a.moon_phase}")
    row("Mercury",  "[red]⚠ RETROGRADE[/red]" if a.merc_rx else "[green]✅ DIRECT[/green]")

    return Panel(g, title="[bold bright_magenta]⚡ ENGINE[/bold bright_magenta]",
                 border_style="bright_magenta", padding=(0, 1))


def panel_patterns(e: PlatiniumEngine) -> Panel:
    lp      = e.live_price
    a       = get_astro()
    r       = get_readings(e.price_history.closes, lp)
    firing  = e.db.find_best(r, a)

    records = sorted(
        [rec for rec in e.db.records.values() if rec.occ >= 2],
        key=lambda rec: rec.score, reverse=True
    )[:20]

    t = Table(show_header=True, header_style="bold dim", box=box.SIMPLE,
              padding=(0, 1), expand=True)
    t.add_column("#",       width=3,  style="dim", justify="right")
    t.add_column("Pattern", width=24, no_wrap=True)
    t.add_column("Dir",     width=3,  justify="center")
    t.add_column("Acc",     width=12)
    t.add_column("EV",      width=7,  justify="right")
    t.add_column("n",       width=4,  style="dim", justify="right")
    t.add_column("🔥",      width=3,  justify="center")

    for i, rec in enumerate(records, 1):
        acc    = rec.acc
        col    = "green" if acc >= 70 else ("yellow" if acc >= 55 else "dim")
        bar    = "█" * int(acc // 10) + "░" * (10 - int(acc // 10))
        dirstr = "[green]▲[/green]" if rec.pattern.direction == 1 else "[red]▼[/red]"
        ev_c   = "green" if rec.ev > 0 else "red"
        hot    = "⚡" if firing and firing.pattern.id == rec.pattern.id else ""
        t.add_row(
            str(i),
            f"[{col}]{rec.pattern.name}[/]",
            dirstr,
            f"[{col}]{acc}%[/] [dim]{bar[:5]}[/]",
            f"[{ev_c}]{rec.ev:+.2f}%[/]",
            str(rec.occ),
            hot,
        )

    title = f"[bold yellow]🏆 PATTERN MEMORY[/bold yellow]  [dim]{len(records)} active[/dim]"
    return Panel(t, title=title, border_style="yellow", padding=(0, 1))


def panel_mas(brain: MASBrain) -> Panel:
    disc = list(brain.discoveries)[:16]

    if not disc:
        body = Text("\n  Warming up… agents fire in ~2 min\n", style="dim")
        return Panel(body, title="[bold cyan]🧠 MAS BRAIN[/bold cyan]",
                     border_style="cyan", padding=(0, 1))

    bias_col = {"BULL": "green", "BEAR": "red", "NEUTRAL": "dim", "PATTERN": "bright_magenta"}

    g = Table.grid(padding=(0, 1))
    g.add_column(width=2)
    g.add_column()

    for d in disc:
        col = bias_col.get(d.bias, "white")
        g.add_row(
            f"[{col}]●[/]",
            f"[bold {col}]{d.title[:52]}[/]\n"
            f"  [dim]{d.agent}  ·  {d.confidence}%  ·  {d.age_str()}[/]\n"
        )

    agents = len(set(d.agent for d in disc))
    title  = f"[bold cyan]🧠 MAS BRAIN[/bold cyan]  [dim]{len(disc)} signals · {agents} agents[/dim]"
    return Panel(g, title=title, border_style="cyan", padding=(0, 1))


def panel_log() -> Panel:
    g = Table.grid()
    g.add_column()
    for line in _log:
        g.add_row(line)
    return Panel(g, title="[dim]LIVE LOG[/dim]", border_style="dim", padding=(0, 1))


# ══════════════════════════════════════════════════════════════════
# LAYOUT
# ══════════════════════════════════════════════════════════════════

def build(e: PlatiniumEngine, brain: MASBrain) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header",   size=3),
        Layout(name="main"),
        Layout(name="log",      size=16),
    )
    layout["main"].split_row(
        Layout(name="engine",   ratio=3),
        Layout(name="patterns", ratio=4),
        Layout(name="mas",      ratio=4),
    )
    layout["header"].update(panel_header(e))
    layout["engine"].update(panel_engine(e))
    layout["patterns"].update(panel_patterns(e))
    layout["mas"].update(panel_mas(brain))
    layout["log"].update(panel_log())
    return layout


# ══════════════════════════════════════════════════════════════════
# ASYNC LOOPS
# ══════════════════════════════════════════════════════════════════

async def loop_price(e: PlatiniumEngine, session: aiohttp.ClientSession):
    # Warm up: fetch 80 candles of history so RSI/MACD/BB are real
    try:
        closes = await fetch_klines("BTCUSDT", "1h", 80, session)
        if closes:
            for c in closes:
                e.price_history.tick(c)
            log(f"[green]Price history loaded — {len(closes)} candles[/green]")
    except Exception as ex:
        log(f"[yellow]Klines fallback: {ex}[/yellow]")

    while True:
        try:
            await e.update_price(session)
            lp = e.live_price
            chg_c = "green" if lp.chg >= 0 else "red"
            log(f"₿ [bold]${lp.btc:,.2f}[/bold]  [{chg_c}]{lp.chg:+.2f}%[/]  "
                f"H:{lp.high:,.0f}  L:{lp.low:,.0f}")
        except Exception as ex:
            log(f"[red]Price: {ex}[/red]")
        await asyncio.sleep(15)


async def loop_onchain(e: PlatiniumEngine, session: aiohttp.ClientSession):
    await asyncio.sleep(8)
    while True:
        try:
            oc = await update_onchain(session)
            fund_c = "green" if oc.funding < -0.02 else ("red" if oc.funding > 0.05 else "yellow")
            flow_c = "green" if oc.chain_flow > 0.2 else ("red" if oc.chain_flow < -0.2 else "yellow")
            log(f"🔗 funding=[{fund_c}]{oc.funding:+.4f}%[/]  "
                f"flow=[{flow_c}]{oc.chain_flow:+.3f}[/]  "
                f"whale={'[green]YES ✅[/green]' if oc.whale_buy else '[dim]no[/dim]'}")
        except Exception as ex:
            log(f"[red]OnChain: {ex}[/red]")
        await asyncio.sleep(300)


async def loop_tick(e: PlatiniumEngine):
    await asyncio.sleep(12)  # let price + onchain load first
    while True:
        try:
            trade = e.tick()
            if trade:
                col = "green" if trade["win"] else "red"
                log(f"[{col}]{'✅' if trade['win'] else '❌'}  "
                    f"{trade['direction']}  {trade['pattern']}  "
                    f"pnl=[bold]{trade['pnl']:+.4f}[/bold]  "
                    f"bal=[bold]${e.sim.balance:.4f}[/bold][/]")
            else:
                edge = e.get_edge()
                log(f"[dim]tick #{e.tick_count}  "
                    f"edge={edge.score} {edge.status}  "
                    f"bal=${e.sim.balance:.4f}[/dim]")
        except Exception as ex:
            log(f"[red]Tick: {ex}[/red]")
        await asyncio.sleep(30)


async def loop_mas(brain: MASBrain, session: aiohttp.ClientSession):
    """Fire each MAS agent on its own schedule."""
    await asyncio.sleep(5)
    log("[cyan]MAS agents starting…[/cyan]")

    # Staggered first run
    agents = [
        (brain._agent_fear_greed,       [session], 300),
        (brain._agent_funding,          [session], 180),
        (brain._agent_open_interest,    [session], 360),
        (brain._agent_dominance,        [session], 900),
        (brain._agent_edge_analyst,     [],         600),
        (brain._agent_pattern_correlator, [],       600),
        (brain._agent_trending,         [session], 1800),
    ]

    last = [0.0] * len(agents)

    for i, (fn, args, _) in enumerate(agents):
        try:
            await fn(*args)
            last[i] = time.time()
            d = list(brain.discoveries)[0] if brain.discoveries else None
            if d:
                col = {"BULL":"green","BEAR":"red","NEUTRAL":"dim","PATTERN":"bright_magenta"}.get(d.bias,"white")
                log(f"[{col}]🧠 {d.agent}: {d.title[:55]}[/]")
        except Exception as ex:
            log(f"[red]MAS {fn.__name__}: {ex}[/red]")
        await asyncio.sleep(1.5)

    while True:
        await asyncio.sleep(30)
        now = time.time()
        for i, (fn, args, interval) in enumerate(agents):
            if now - last[i] >= interval:
                try:
                    await fn(*args)
                    last[i] = now
                    d = list(brain.discoveries)[0] if brain.discoveries else None
                    if d:
                        col = {"BULL":"green","BEAR":"red","NEUTRAL":"dim","PATTERN":"bright_magenta"}.get(d.bias,"white")
                        log(f"[{col}]🧠 {d.agent}: {d.title[:55]}[/]")
                except Exception as ex:
                    log(f"[red]MAS {fn.__name__}: {ex}[/red]")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

async def main():
    console.clear()
    console.print("\n[bold bright_magenta]🔱  PLATINIUM LIVE[/bold bright_magenta]  [dim]initialising…[/dim]\n")

    e     = PlatiniumEngine(executor=BitgetExecutor(paper=True),
                            trade_size_usdt=1.0, demo_balance=100.0)
    brain = MASBrain(e)

    log(f"[green]Engine ready — {len(e.db.records)} patterns loaded[/green]")
    log(f"[dim]Balance: ${e.sim.balance:.2f}  Trades: {e.sim.total}[/dim]")

    session = aiohttp.ClientSession()
    tasks   = []

    try:
        with Live(build(e, brain), refresh_per_second=2,
                  screen=True, console=console) as live:

            tasks = [
                asyncio.create_task(loop_price(e, session)),
                asyncio.create_task(loop_onchain(e, session)),
                asyncio.create_task(loop_tick(e)),
                asyncio.create_task(loop_mas(brain, session)),
            ]

            while True:
                live.update(build(e, brain))
                await asyncio.sleep(0.5)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await session.close()
        console.print(
            "\n[bold bright_magenta]🔱 PLATINIUM LIVE[/bold bright_magenta]"
            " [dim]stopped[/dim]\n"
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
