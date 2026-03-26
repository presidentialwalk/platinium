"""
PLATINIUM DASHBOARD
Live web interface — MAS discoveries, pattern memory, brainstorm board
Runs on PORT (Railway) or DASHBOARD_PORT inside the same asyncio process as the bot.
"""

import asyncio
import json
import logging
import time
from aiohttp import web

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# HTML  (single-file SPA, no CDN dependencies)
# ══════════════════════════════════════════════════════════════════

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PLATINIUM BRAIN</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#050508;--bg2:#0d0d14;--bg3:#13131f;--border:#1a1a2e;
  --text:#e2e8f0;--muted:#4a5568;
  --purple:#7c3aed;--pl:#a78bfa;
  --green:#10b981;--red:#ef4444;--yellow:#f59e0b;--blue:#3b82f6;
}
body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:13px;min-height:100vh;overflow-x:hidden}

/* ── HEADER ── */
header{
  background:var(--bg2);border-bottom:1px solid var(--border);
  padding:10px 20px;display:flex;align-items:center;gap:16px;
  position:sticky;top:0;z-index:100;flex-wrap:wrap;
}
.logo{font-size:16px;font-weight:700;color:var(--pl);letter-spacing:3px;white-space:nowrap}
.logo span{color:var(--text);opacity:.4}
.pill{
  display:flex;align-items:center;gap:5px;background:var(--bg3);
  border:1px solid var(--border);border-radius:5px;padding:3px 9px;font-size:11px;
}
.pill .lbl{color:var(--muted)}
.pill .val{font-weight:600}
.g{color:var(--green)}.r{color:var(--red)}.p{color:var(--pl)}.y{color:var(--yellow)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--red);animation:blink 2s infinite;flex-shrink:0}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
.conn{margin-left:auto;font-size:11px;color:var(--muted);display:flex;align-items:center;gap:5px}

/* ── LAYOUT ── */
.grid{
  display:grid;grid-template-columns:260px 1fr 290px;
  gap:1px;background:var(--border);min-height:calc(100vh - 46px);
}
.panel{background:var(--bg);padding:14px;overflow-y:auto;max-height:calc(100vh - 46px)}
.pt{
  font-size:10px;letter-spacing:2px;text-transform:uppercase;
  color:var(--muted);margin-bottom:10px;padding-bottom:7px;
  border-bottom:1px solid var(--border);display:flex;align-items:center;gap:6px;
}

/* ── LEFT: STATS ── */
.big{font-size:26px;font-weight:700;line-height:1;margin-bottom:4px}
.bar-wrap{background:var(--bg3);border-radius:3px;height:6px;margin:6px 0;overflow:hidden}
.bar-fill{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--blue),var(--purple),var(--green));transition:width 1.2s ease}
.srow{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:11px}
.srow:last-child{border-bottom:none}
.srow .k{color:var(--muted)}
svg#spark{width:100%;height:36px;display:block;margin:8px 0}

/* ── MIDDLE: FEED ── */
.feed{display:flex;flex-direction:column;gap:7px}
.card{
  background:var(--bg2);border:1px solid var(--border);border-radius:7px;
  padding:10px 12px 10px 14px;animation:slid .3s ease;position:relative;overflow:hidden;
}
.card::before{content:"";position:absolute;left:0;top:0;bottom:0;width:2px}
.card.BULL::before{background:var(--green)}
.card.BEAR::before{background:var(--red)}
.card.NEUTRAL::before{background:var(--muted)}
.card.PATTERN::before{background:var(--purple)}
@keyframes slid{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
.ch{display:flex;justify-content:space-between;align-items:flex-start;gap:6px;margin-bottom:4px}
.ct{font-size:12px;font-weight:600;line-height:1.35}
.cm{display:flex;gap:5px;align-items:center;flex-shrink:0}
.badge{font-size:9px;padding:2px 5px;border-radius:3px;font-weight:700;letter-spacing:.5px}
.badge.BULL{background:rgba(16,185,129,.15);color:var(--green)}
.badge.BEAR{background:rgba(239,68,68,.15);color:var(--red)}
.badge.NEUTRAL{background:rgba(74,85,104,.2);color:var(--muted)}
.badge.PATTERN{background:rgba(124,58,237,.2);color:var(--pl)}
.cb{font-size:11px;color:var(--muted);line-height:1.5}
.cf{display:flex;justify-content:space-between;margin-top:5px;font-size:10px;color:var(--muted);opacity:.5}

/* ── RIGHT: PATTERNS ── */
.prow{display:flex;align-items:center;gap:6px;padding:6px 0;border-bottom:1px solid var(--border);font-size:11px}
.prow:last-child{border-bottom:none}
.prank{width:16px;text-align:right;color:var(--muted);flex-shrink:0}
.pname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pdir{font-size:9px;padding:1px 4px;border-radius:2px;flex-shrink:0}
.pdir.long{background:rgba(16,185,129,.12);color:var(--green)}
.pdir.short{background:rgba(239,68,68,.12);color:var(--red)}
.pbw{width:44px;height:3px;background:var(--bg3);border-radius:2px;overflow:hidden;flex-shrink:0}
.pbf{height:100%;border-radius:2px}
.pacc{width:28px;text-align:right;flex-shrink:0;font-size:11px}

/* ── BOTTOM: BRAINSTORM ── */
.bottom{grid-column:1/-1;background:var(--bg);border-top:1px solid var(--border);padding:14px 20px}
.ideas-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;margin-top:8px}
.idea{background:var(--bg2);border:1px solid var(--border);border-radius:7px;padding:10px 12px;position:relative}
.idea-text{font-size:12px;color:var(--text);line-height:1.5;padding-right:16px;margin-bottom:3px}
.idea-ts{font-size:10px;color:var(--muted);opacity:.4}
.idea-del{position:absolute;top:7px;right:8px;background:none;border:none;color:var(--muted);cursor:pointer;font-size:11px;opacity:.3;transition:opacity .2s}
.idea-del:hover{opacity:1;color:var(--red)}
.add-row{display:flex;gap:7px;margin-top:10px}
.add-row input{flex:1;background:var(--bg3);border:1px solid var(--border);border-radius:5px;padding:7px 11px;color:var(--text);font-family:inherit;font-size:12px;outline:none;transition:border-color .2s}
.add-row input:focus{border-color:var(--purple)}
.add-row button{background:var(--purple);border:none;border-radius:5px;padding:7px 14px;color:#fff;font-family:inherit;font-size:12px;cursor:pointer;font-weight:600;transition:background .2s;white-space:nowrap}
.add-row button:hover{background:var(--pl)}

::-webkit-scrollbar{width:3px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

@media(max-width:860px){.grid{grid-template-columns:1fr}.panel{max-height:none}}
</style>
</head>
<body>

<header>
  <div class="logo">🔱 PLATINIUM <span>BRAIN</span></div>
  <div class="pill"><span class="lbl">Balance</span><span class="val g" id="h-bal">$—</span></div>
  <div class="pill"><span class="lbl">P&L</span><span class="val" id="h-pnl">—</span></div>
  <div class="pill"><span class="lbl">Edge</span><span class="val p" id="h-edge">—</span></div>
  <div class="pill"><span class="lbl">BTC</span><span class="val" id="h-btc">—</span></div>
  <div class="pill"><span class="lbl">WR</span><span class="val g" id="h-wr">—</span></div>
  <div class="conn"><div class="dot" id="cdot"></div><span id="ctxt">Connecting…</span></div>
</header>

<div class="grid">

  <!-- LEFT -->
  <div class="panel">
    <div class="pt">⚡ Live Stats</div>
    <div style="margin-bottom:10px">
      <div style="font-size:10px;color:var(--muted);margin-bottom:2px">DEMO WALLET</div>
      <div class="big g" id="s-bal">$—</div>
    </div>
    <div class="bar-wrap"><div class="bar-fill" id="ebar" style="width:0%"></div></div>
    <div style="font-size:10px;color:var(--muted);margin-bottom:10px">
      Edge <span id="s-escore">—</span>/100 — <span id="s-estat">—</span>
    </div>
    <svg id="spark" viewBox="0 0 200 36" preserveAspectRatio="none">
      <polyline id="sline" fill="none" stroke="var(--purple)" stroke-width="1.5" points=""/>
    </svg>
    <div style="margin-top:12px">
      <div class="srow"><span class="k">Trades</span><span id="s-total">—</span></div>
      <div class="srow"><span class="k">Win Rate</span><span class="g" id="s-wr">—</span></div>
      <div class="srow"><span class="k">P&L</span><span id="s-pnl">—</span></div>
      <div class="srow"><span class="k">BTC</span><span id="s-btc">—</span></div>
      <div class="srow"><span class="k">BTC 24h</span><span id="s-btcchg">—</span></div>
      <div class="srow"><span class="k">Discoveries</span><span class="p" id="s-disc">0</span></div>
    </div>
  </div>

  <!-- MIDDLE: MAS FEED -->
  <div class="panel">
    <div class="pt">
      🧠 MAS Brain Feed
      <span style="margin-left:auto;font-size:9px" id="fc">0 signals</span>
    </div>
    <div class="feed" id="feed"></div>
  </div>

  <!-- RIGHT: PATTERN MEMORY -->
  <div class="panel">
    <div class="pt">🏆 Pattern Memory</div>
    <div id="plist"></div>
  </div>

  <!-- BOTTOM: BRAINSTORM -->
  <div class="bottom">
    <div class="pt">💡 Brainstorm — Ideas &amp; Research Directions</div>
    <div class="ideas-grid" id="igrid"></div>
    <div class="add-row">
      <input id="iinput" type="text" placeholder="New idea, pattern hypothesis, data source to explore…" maxlength="500">
      <button onclick="addIdea()">+ Add</button>
    </div>
  </div>

</div>

<script>
let disc=[], ideas=[], t0=Date.now();

// ── LOAD ──
async function load(){
  try{
    const s=await(await fetch('/state')).json();
    upStats(s);upPatterns(s.patterns);upSpark(s.curve);
    disc=s.discoveries||[];ideas=s.ideas||[];
    renderFeed();renderIdeas();
  }catch(e){console.warn(e)}
}

// ── SSE ──
function sse(){
  const ev=new EventSource('/events');
  ev.onopen=()=>{
    document.getElementById('cdot').style.background='var(--green)';
    document.getElementById('ctxt').textContent='Live';
  };
  ev.onmessage=e=>{
    const d=JSON.parse(e.data);
    if(d.type==='discovery'){
      disc.unshift(d);if(disc.length>300)disc.pop();
      prependCard(d);
      document.getElementById('fc').textContent=disc.length+' signals';
      document.getElementById('s-disc').textContent=disc.length;
    } else if(d.type==='tick') upStats(d);
  };
  ev.onerror=()=>{
    document.getElementById('cdot').style.background='var(--red)';
    document.getElementById('ctxt').textContent='Reconnecting…';
    ev.close();setTimeout(sse,3000);
  };
}

// ── STATS ──
function fmt(n){return n>=1000?'$'+n.toLocaleString():'$'+n.toFixed(2)}
function upStats(s){
  if(s.balance!=null){
    const b=fmt(s.balance);
    document.getElementById('h-bal').textContent=b;
    document.getElementById('s-bal').textContent=b;
    const g=s.pnl_pct>=0;
    document.getElementById('s-bal').className='big '+(g?'g':'r');
    document.getElementById('h-bal').className='val '+(g?'g':'r');
  }
  if(s.pnl_pct!=null){
    const sg=s.pnl_pct>=0?'+':'',str=sg+s.pnl_pct+'%';
    ['h-pnl','s-pnl'].forEach(id=>{
      const el=document.getElementById(id);el.textContent=str;
      el.className=(id==='h-pnl'?'val ':'')+(s.pnl_pct>=0?'g':'r');
    });
  }
  if(s.edge_score!=null){
    document.getElementById('h-edge').textContent=s.edge_score+' '+s.edge_status;
    document.getElementById('s-escore').textContent=s.edge_score;
    document.getElementById('s-estat').textContent=s.edge_status||'';
    document.getElementById('ebar').style.width=s.edge_score+'%';
  }
  if(s.btc!=null){
    const b='$'+s.btc.toLocaleString();
    document.getElementById('h-btc').textContent=b;
    document.getElementById('s-btc').textContent=b;
  }
  if(s.btc_chg!=null){
    const el=document.getElementById('s-btcchg');
    el.textContent=(s.btc_chg>=0?'+':'')+s.btc_chg+'%';
    el.className=s.btc_chg>=0?'g':'r';
  }
  if(s.total!=null) document.getElementById('s-total').textContent=s.total;
  if(s.wr!=null){
    document.getElementById('s-wr').textContent=s.wr+'%';
    document.getElementById('h-wr').textContent=s.wr+'%';
  }
}

// ── FEED ──
function renderFeed(){
  const f=document.getElementById('feed');f.innerHTML='';
  disc.forEach(d=>f.appendChild(mkCard(d)));
  document.getElementById('fc').textContent=disc.length+' signals';
  document.getElementById('s-disc').textContent=disc.length;
}
function prependCard(d){
  const f=document.getElementById('feed');
  f.insertBefore(mkCard(d),f.firstChild);
}
function mkCard(d){
  const div=document.createElement('div');div.className='card '+d.bias;
  const cc=d.confidence>=70?'var(--green)':d.confidence>=55?'var(--yellow)':'var(--muted)';
  div.innerHTML=`
    <div class="ch">
      <div class="ct">${esc(d.title)}</div>
      <div class="cm">
        <span class="badge ${d.bias}">${d.bias}</span>
        <span style="font-size:10px;color:${cc}">${d.confidence}%</span>
      </div>
    </div>
    <div class="cb">${esc(d.body)}</div>
    <div class="cf"><span>${esc(d.agent)}</span><span>${d.age||'just now'}</span></div>`;
  return div;
}

// ── PATTERNS ──
function upPatterns(pats){
  if(!pats)return;
  const list=document.getElementById('plist');list.innerHTML='';
  pats.forEach((p,i)=>{
    const c=p.acc>=70?'#10b981':p.acc>=55?'#f59e0b':'#4a5568';
    const dir=p.direction==='LONG'?'long':'short';
    const row=document.createElement('div');row.className='prow';
    row.innerHTML=`
      <span class="prank">${i+1}</span>
      <span class="pname" title="${esc(p.name)}">${esc(p.name)}</span>
      <span class="pdir ${dir}">${p.direction==='LONG'?'▲':'▼'}</span>
      <div class="pbw"><div class="pbf" style="width:${p.acc}%;background:${c}"></div></div>
      <span class="pacc" style="color:${c}">${p.acc}%</span>`;
    list.appendChild(row);
  });
}

// ── SPARKLINE ──
function upSpark(curve){
  if(!curve||curve.length<2)return;
  const mn=Math.min(...curve),mx=Math.max(...curve),rng=mx-mn||1;
  const W=200,H=36,P=2;
  const pts=curve.map((v,i)=>{
    const x=P+(i/(curve.length-1))*(W-P*2);
    const y=H-P-((v-mn)/rng)*(H-P*2);
    return x.toFixed(1)+','+y.toFixed(1);
  }).join(' ');
  const ln=document.getElementById('sline');
  ln.setAttribute('points',pts);
  ln.setAttribute('stroke',curve[curve.length-1]>=curve[0]?'var(--green)':'var(--red)');
}

// ── IDEAS ──
function renderIdeas(){
  const g=document.getElementById('igrid');g.innerHTML='';
  if(!ideas.length){
    g.innerHTML='<div style="color:var(--muted);font-size:12px;padding:6px 0">No ideas yet — what patterns, data sources, or strategies should we explore?</div>';
    return;
  }
  ideas.forEach((idea,idx)=>{
    const d=document.createElement('div');d.className='idea';
    const ts=new Date((idea.ts||0)*1000).toLocaleDateString();
    d.innerHTML=`
      <div class="idea-text">${esc(idea.text)}</div>
      <div class="idea-ts">${ts}</div>
      <button class="idea-del" onclick="delIdea(${idx})">✕</button>`;
    g.appendChild(d);
  });
}
async function addIdea(){
  const inp=document.getElementById('iinput');
  const text=inp.value.trim();if(!text)return;
  await fetch('/idea',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({idea:text})});
  inp.value='';ideas.push({text,ts:Date.now()/1000});renderIdeas();
}
async function delIdea(idx){
  await fetch('/idea/'+idx,{method:'DELETE'});
  ideas.splice(idx,1);renderIdeas();
}
document.getElementById('iinput').addEventListener('keydown',e=>{if(e.key==='Enter')addIdea()});

// ── UTILS ──
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

// ── REFRESH LOOP ──
setInterval(async()=>{
  try{
    const s=await(await fetch('/state')).json();
    upPatterns(s.patterns);upSpark(s.curve);ideas=s.ideas||[];renderIdeas();
  }catch(e){}
},90000);

load();sse();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════
# DASHBOARD SERVER
# ══════════════════════════════════════════════════════════════════

class Dashboard:
    def __init__(self, engine, mas):
        self.engine = engine
        self.mas    = mas
        self.app    = web.Application()
        self.app.router.add_get("/",            self._index)
        self.app.router.add_get("/state",       self._state)
        self.app.router.add_get("/events",      self._events)
        self.app.router.add_post("/idea",       self._add_idea)
        self.app.router.add_delete("/idea/{i}", self._del_idea)

    async def _index(self, _req):
        return web.Response(text=HTML, content_type="text/html")

    async def _state(self, _req):
        e    = self.engine
        s    = e.get_summary()
        edge = e.get_edge()

        records = sorted(
            [r for r in e.db.records.values() if r.occ >= 2],
            key=lambda r: r.score, reverse=True
        )[:20]

        return web.json_response({
            "balance":      round(s["balance"], 2),
            "start_balance":round(s["start_balance"], 2),
            "pnl_pct":      s["pnl_pct"],
            "wr":           s["wr"],
            "total":        s["total"],
            "edge_score":   edge.score,
            "edge_status":  edge.status,
            "btc":          round(s["btc"], 2),
            "btc_chg":      s["btc_chg"],
            "patterns": [{
                "name":      r.pattern.name,
                "acc":       r.acc,
                "ev":        r.ev,
                "occ":       r.occ,
                "score":     round(r.score, 1),
                "direction": "LONG" if r.pattern.direction == 1 else "SHORT",
            } for r in records],
            "discoveries": [d.to_dict() for d in list(self.mas.discoveries)[:60]],
            "ideas":        self.mas.ideas,
            "curve":        e.sim.curve[-120:],
        })

    async def _events(self, req):
        resp = web.StreamResponse(headers={
            "Content-Type":                "text/event-stream",
            "Cache-Control":               "no-cache",
            "Access-Control-Allow-Origin": "*",
            "X-Accel-Buffering":           "no",
        })
        await resp.prepare(req)
        await resp.write(b'data: {"type":"connected"}\n\n')

        q = self.mas.subscribe()
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=14.0)
                    data = json.dumps({"type": "discovery", **item.to_dict()})
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.TimeoutError:
                    # heartbeat with live stats
                    s    = self.engine.get_summary()
                    edge = self.engine.get_edge()
                    hb   = json.dumps({
                        "type":        "tick",
                        "balance":     round(s["balance"], 2),
                        "pnl_pct":     s["pnl_pct"],
                        "total":       s["total"],
                        "wr":          s["wr"],
                        "edge_score":  edge.score,
                        "edge_status": edge.status,
                        "btc":         round(s["btc"], 2),
                        "btc_chg":     s["btc_chg"],
                    })
                    await resp.write(f"data: {hb}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self.mas.unsubscribe(q)
        return resp

    async def _add_idea(self, req):
        body = await req.json()
        text = str(body.get("idea", "")).strip()[:500]
        if text:
            self.mas.ideas.append({"text": text, "ts": time.time()})
            self.mas._save_ideas()
        return web.json_response({"ok": True})

    async def _del_idea(self, req):
        try:
            idx = int(req.match_info["i"])
            if 0 <= idx < len(self.mas.ideas):
                self.mas.ideas.pop(idx)
                self.mas._save_ideas()
        except (ValueError, IndexError):
            pass
        return web.json_response({"ok": True})

    async def start(self, port: int = 8080):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info(f"Dashboard live → http://0.0.0.0:{port}")
