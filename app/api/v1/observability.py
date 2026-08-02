"""Agent Observability — dashboard, SSE streaming, and metrics for agent communication."""
from __future__ import annotations
import asyncio, json, logging, os, time
from datetime import datetime, timezone
from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/observability", tags=["observability"])

_PROCESS_START_TIME: float = time.time()
event_cache: list[dict] = []
event_clients: list[asyncio.Queue] = []

# ── Notification functions (imported by swarm.py) ─────────────────────
def notify_agent_registered(name: str, capabilities: list[str] = []) -> None:
    _push_event("agent_registered", {"agent": name, "capabilities": capabilities})
def notify_agent_status(name: str, status: str) -> None:
    _push_event("agent_status_change", {"agent": name, "status": status})
def notify_message_sent(to: str, msg_type: str, body_preview: str = "") -> None:
    _push_event("agent_message_sent", {"to": to, "type": msg_type, "preview": body_preview[:200]})

def _push_event(etype: str, data: dict) -> None:
    entry = {"type": etype, "data": data, "timestamp": time.time()}
    event_cache.append(entry)
    if len(event_cache) > 500:
        event_cache[:] = event_cache[-500:]
    for q in event_clients[:]:
        try: q.put_nowait(entry)
        except: event_clients.remove(q)

# ── SSE Stream ────────────────────────────────────────────────────────
@router.get("/stream")
async def event_stream(request: Request):
    q: asyncio.Queue = asyncio.Queue()
    event_clients.append(q)
    async def generate():
        try:
            for ev in event_cache[-50:]:
                yield f"data: {json.dumps(ev)}\n\n"
            while True:
                ev = await asyncio.wait_for(q.get(), timeout=30)
                yield f"data: {json.dumps(ev)}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'ping', 'data': {}, 'timestamp': time.time()})}\n\n"
        except: pass
        finally:
            if q in event_clients: event_clients.remove(q)
    return StreamingResponse(generate(), media_type="text/event-stream")

# ── Stats ─────────────────────────────────────────────────────────────
@router.get("/stats")
async def stats():
    agents_data = await _fetch_json("/v1/swarm/agents")
    agents = agents_data.get("agents", []) if isinstance(agents_data, dict) else (agents_data or [])
    events_1h = sum(1 for e in event_cache if e.get("timestamp",0) > time.time() - 3600)
    online = sum(1 for a in agents if a.get("status") == "online")
    msgs_pending = sum(1 for e in event_cache if "message" in e.get("type",""))
    return {
        "agents_total": len(agents), "agents_online": online,
        "events_total": len(event_cache), "events_last_hour": events_1h,
        "messages_pending": msgs_pending,
        "uptime_seconds": int(time.time() - _PROCESS_START_TIME),
    }

@router.get("/agents")
async def agent_status():
    return await _fetch_json("/v1/swarm/agents")

# ── Pages ─────────────────────────────────────────────────────────────
_NAV = """
<style>
.nav{display:flex;gap:4px;margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border);flex-wrap:wrap}
.nav a{padding:6px 14px;border-radius:6px;font-size:13px;text-decoration:none;color:var(--text-dim)}
.nav a:hover{background:var(--surface);color:var(--text)}
.nav a.active{background:var(--blue);color:#fff}
</style>
<div class="nav">
<a href="/v1/observability/dashboard" class="__D__">📊 Dashboard</a>
<a href="/v1/observability/workspace" class="__W__">🏗️ Workspace</a>
<a href="/v1/observability/coordinator" class="__C__">🎯 Coordenação</a>
<a href="#" style="margin-left:auto;color:var(--text-dim);font-size:12px">🔄 3s</a>
</div>"""

_CSS = """
:root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--text-dim:#8b949e;--green:#3fb950;--blue:#58a6ff;--yellow:#d29922;--red:#f85149}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);padding:16px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:12px}
.card h2{font-size:13px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px}
.empty-state{padding:24px;text-align:center;color:var(--text-dim);font-size:13px}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 1.5s infinite;margin-right:6px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.3}}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.full{grid-column:1/-1}
.task-item{font-size:12px;padding:6px 8px;background:var(--surface);border:1px solid var(--border);border-radius:4px;margin-bottom:4px}
.task-item .tt{font-weight:500;color:var(--text)}
.task-item .tm{font-size:11px;color:var(--text-dim);margin-top:2px}
.task-item:hover{border-color:var(--blue)}
.bar{height:4px;background:var(--bg);border-radius:2px;overflow:hidden;margin:4px 0}
.bar-fill{height:100%;border-radius:2px;transition:width .5s}
.nav{display:flex;gap:4px;margin-bottom:16px;padding:8px 0;border-bottom:1px solid var(--border);flex-wrap:wrap}
.nav a{padding:6px 14px;border-radius:6px;font-size:13px;text-decoration:none;color:var(--text-dim);transition:all .2s}
.nav a:hover{background:var(--surface);color:var(--text)}
.tag{display:inline-block;padding:1px 6px;border-radius:3px;font-size:11px;margin:1px}
.tag.high{background:rgba(248,81,73,0.15);color:var(--red)}
.tag.med{background:rgba(210,153,34,0.15);color:var(--yellow)}
.tag.low{background:rgba(63,185,80,0.15);color:var(--green)}
"""

def _page(title: str, active: str, body: str) -> str:
    nav = _NAV.replace(f"class=\"__{active[0].upper()}__\"", "class=\"active\"")
    for p in ["__D__","__W__","__C__"]:
        nav = nav.replace(f"class=\"{p}\"", "")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{title}</title><style>{_CSS}</style></head><body>{nav}{body}</body></html>"""

async def _fetch_json(path: str):
    import httpx
    try:
        async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=5) as c:
            r = await c.get(path)
            return r.json() if r.status_code == 200 else {}
    except: return {}

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return _page("Dashboard", "dashboard", """
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
<h1 style="font-size:20px"><span class="live-dot"></span>📊 Swarm Dashboard</h1>
<div id="d-stats" style="font-size:13px;color:var(--text-dim)">Carregando...</div>
</div>
<div class="grid">
<div class="card full"><h2>🟢 Agentes</h2><div id="d-agents" style="font-size:13px">Carregando...</div></div>
<div class="card"><h2>⏳ Pendentes</h2><div id="d-pend" style="font-size:12px">Carregando...</div></div>
<div class="card"><h2>🔄 Em Andamento</h2><div id="d-prog" style="font-size:12px">Carregando...</div></div>
<div class="card full"><h2>✅ Concluídas</h2><div id="d-done" style="font-size:12px">Carregando...</div></div>
<div class="card full"><h2>📡 Atividade</h2><div id="d-feed" style="max-height:400px;overflow-y:auto;font-size:12px">Carregando...</div></div>
</div>
<script>
const S="/v1/swarm",O="/v1/observability";
function esc(s){var d=document.createElement("div");d.textContent=s||"";return d.innerHTML}
function fmt(t){return t?new Date(t*1e3).toLocaleTimeString("pt-BR",{hour:"2-digit",minute:"2-digit",second:"2-digit"}):"--"}
function pct(p){return'<div class="bar"><div class="bar-fill" style="width:'+p+'%;background:'+(p>=100?"var(--green)":"var(--blue)")+'"></div></div><span style="font-size:10px;color:var(--text-dim)">'+p+"%</span>"}
async function refresh(){try{
var[ag,pe,pr,do_,ac]=await Promise.all([
fetch(S+"/agents").then(r=>r.json()),fetch(S+"/tasks?status=pending&limit=10").then(r=>r.json()),
fetch(S+"/tasks?status=in_progress&limit=10").then(r=>r.json()),fetch(S+"/tasks?status=done&limit=10").then(r=>r.json()),
fetch(S+"/activity?limit=50").then(r=>r.json())]);
var agents=ag.agents||[], now=Math.floor(Date.now()/1e3);
document.getElementById("d-stats").innerHTML="🟢 "+agents.filter(function(x){return x.status==="online"}).length+"/"+agents.length+" online · 📋 "+((pe.tasks||[]).length+(pr.tasks||[]).length+(do_.tasks||[]).length)+" tasks";
document.getElementById("d-agents").innerHTML=agents.map(function(x){var ago=x.last_heartbeat?Math.floor(now-x.last_heartbeat)+"s":"--";return'<div style="padding:3px 0;border-bottom:1px solid var(--border);display:flex;gap:8px;font-size:12px">'+(x.status==="online"?"🟢":"🔴")+" <b>"+esc(x.name)+"</b> <span style='color:var(--text-dim)'>"+ago+"</span></div>"}).join("")+'<div style="margin-top:4px;font-size:12px;color:var(--text-dim)">'+ag.online+"/"+ag.count+" online</div>";
function rt(ts,id,em){var el=document.getElementById(id);if(!ts||!ts.length){el.innerHTML='<div style="color:var(--text-dim);padding:4px">'+em+"</div>";return}
el.innerHTML=ts.slice(0,10).map(function(t){var bar=t.progress?pct(t.progress):"";return'<a href="/v1/observability/task/'+t.id+'" style="text-decoration:none;color:inherit"><div class="task-item"><div class="tt">'+esc(t.title||"?").slice(0,60)+"</div><div class='tm'>"+esc(t.target||"*")+"</div>"+bar+"</div></a>"}).join("")}
rt(pe.tasks,"d-pend","Nenhuma pendente");rt(pr.tasks,"d-prog","Nenhuma");rt(do_.tasks,"d-done","Nenhuma");
document.getElementById("d-feed").innerHTML=(ac.activities||[]).slice(0,40).map(function(x){return'<div style="display:flex;gap:8px;padding:3px 0;border-bottom:1px solid var(--border);font-size:12px"><span style="color:var(--text-dim);min-width:60px">'+fmt(x.timestamp)+'</span><span style="color:var(--blue);min-width:70px">'+esc(x.agent)+'</span><span style="color:'+(x.action==="working"?"var(--blue)":x.action==="completed"?"var(--green)":"var(--text-dim)")+'">'+(x.action||"")+'</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(x.message||"").slice(0,80)+"</span></div>"}).join("")||'<div class="empty-state">Aguardando atividade...</div>';
}catch(e){console.error(e)}}
refresh();setInterval(refresh,3000);
</script>""")

@router.get("/workspace", response_class=HTMLResponse)
async def workspace():
    return _page("Workspace", "workspace", """
<h1 style="font-size:20px;margin-bottom:16px"><span class="live-dot"></span>🏗️ Workspace</h1>
<div class="grid">
<div class="card"><h2>📡 Atividade</h2><div id="w-feed" style="max-height:500px;overflow-y:auto;font-size:12px">Carregando...</div></div>
<div class="card"><h2>🟢 Agentes</h2><div id="w-agents" style="font-size:12px">Carregando...</div></div>
<div class="card full"><h2>📋 Tasks</h2><div style="display:flex;gap:8px">
<div style="flex:1"><h3 style="font-size:11px;color:var(--text-dim);margin-bottom:4px">⏳ Pendentes</h3><div id="w-pend"></div></div>
<div style="flex:1"><h3 style="font-size:11px;color:var(--text-dim);margin-bottom:4px">🔄 Em Andamento</h3><div id="w-prog"></div></div>
<div style="flex:1"><h3 style="font-size:11px;color:var(--text-dim);margin-bottom:4px">✅ Concluídas</h3><div id="w-done"></div></div>
</div></div>
</div>
<script>
const S="/v1/swarm";
function esc(s){var d=document.createElement("div");d.textContent=s||"";return d.innerHTML}
function fmt(t){return t?new Date(t*1e3).toLocaleTimeString("pt-BR",{hour:"2-digit",minute:"2-digit",second:"2-digit"}):"--"}
function pct(p){return'<div class="bar"><div class="bar-fill" style="width:'+p+'%;background:'+(p>=100?"var(--green)":"var(--blue)")+'"></div></div><span style="font-size:10px;color:var(--text-dim)">'+p+"%</span>"}
async function refresh(){try{
var[pe,pr,do_,ac,ag]=await Promise.all([
fetch(S+"/tasks?status=pending&limit=20").then(r=>r.json()),fetch(S+"/tasks?status=in_progress&limit=20").then(r=>r.json()),
fetch(S+"/tasks?status=done&limit=10").then(r=>r.json()),fetch(S+"/activity?limit=100").then(r=>r.json()),
fetch(S+"/agents").then(r=>r.json())]);
document.getElementById("w-feed").innerHTML=(ac.activities||[]).slice(0,60).map(function(x){return'<div style="display:flex;gap:8px;padding:3px 0;border-bottom:1px solid var(--border);font-size:12px"><span style="color:var(--text-dim);min-width:60px">'+fmt(x.timestamp)+'</span><span style="color:var(--blue);min-width:70px">'+esc(x.agent)+'</span><span style="color:'+(x.action==="working"?"var(--blue)":x.action==="completed"?"var(--green)":"var(--text-dim)")+'">'+(x.action||"")+'</span><span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(x.message||"").slice(0,80)+"</span></div>"}).join("")||'<div class="empty-state">Aguardando...</div>';
document.getElementById("w-agents").innerHTML=(ag.agents||[]).map(function(x){var ago=x.last_heartbeat?Math.floor(Date.now()/1e3-x.last_heartbeat)+"s":"--";return'<div style="display:flex;align-items:center;gap:8px;padding:3px 0;border-bottom:1px solid var(--border);font-size:12px">'+(x.status==="online"?"🟢":"🔴")+" <b>"+esc(x.name)+"</b> <span style='color:var(--text-dim);margin-left:auto'>"+ago+"</span></div>"}).join("")+"";
function rt(ts,id,em){var el=document.getElementById(id);if(!ts||!ts.length){el.innerHTML='<div style="color:var(--text-dim);padding:4px;font-size:11px">'+em+"</div>";return}
el.innerHTML=ts.slice(0,15).map(function(t){var bar=t.progress?pct(t.progress):"";return'<a href="/v1/observability/task/'+t.id+'" style="text-decoration:none;color:inherit"><div class="task-item"><div class="tt">'+esc(t.title||"?").slice(0,50)+'</div><div class="tm">'+esc(t.source||"?")+" → "+esc(t.target||"*")+"</div>"+bar+"</div></a>"}).join("")}
rt(pe.tasks,"w-pend","Nenhuma");rt(pr.tasks,"w-prog","Nenhuma");rt(do_.tasks,"w-done","Nenhuma");
}catch(e){console.error(e)}}
refresh();setInterval(refresh,3000);
</script>""")

@router.get("/coordinator", response_class=HTMLResponse)
async def coordinator():
    return _page("Coordenação", "coordinator", """
<h1 style="font-size:20px;margin-bottom:16px"><span class="live-dot"></span>🎯 Coordenação</h1>
<div class="grid">
<div class="card"><h2>📋 Agentes</h2><div id="c-agents" style="font-size:13px">Carregando...</div></div>
<div class="card"><h2>📊 Relatório</h2><div id="c-report" style="font-size:13px">Carregando...</div></div>
<div class="card full"><h2>📡 Feed</h2><div id="c-feed" style="max-height:400px;overflow-y:auto;font-size:12px">Carregando...</div></div>
</div>
<script>
const S="/v1/swarm";
function esc(s){var d=document.createElement("div");d.textContent=s||"";return d.innerHTML}
function fmt(t){return t?new Date(t*1e3).toLocaleTimeString("pt-BR",{hour:"2-digit",minute:"2-digit",second:"2-digit"}):"--"}
async function refresh(){try{
var[ag,ac]=await Promise.all([fetch(S+"/agents").then(r=>r.json()),fetch(S+"/activity?limit=100").then(r=>r.json())]);
var agents=ag.agents||[], now=Math.floor(Date.now()/1e3), online=agents.filter(function(x){return x.status==="online"}).length, active=ac.active_count||0;
document.getElementById("c-agents").innerHTML=agents.map(function(x){var ago=x.last_heartbeat?Math.floor(now-x.last_heartbeat)+"s":"--";var hasAct=ac.active_agents&&ac.active_agents.indexOf(x.name)>=0;var icon=x.status==="online"?(hasAct?"🟢":"🟡"):"🔴";var label=x.status==="online"?(hasAct?"trabalhando":"ocioso"):x.status;return'<div style="padding:3px 0;border-bottom:1px solid var(--border);display:flex;gap:8px;font-size:12px">'+icon+" <b>"+esc(x.name)+"</b> "+label+" <span style='color:var(--text-dim);margin-left:auto'>"+ago+"</span></div>"}).join("")+'<div style="margin-top:6px;font-size:12px;color:var(--text-dim)">🟢 '+online+" online · 🟡 "+(online-active)+" ociosos · 📡 "+active+" ativos</div>";
var byAgent={};(ac.activities||[]).forEach(function(x){if(!byAgent[x.agent])byAgent[x.agent]=[];byAgent[x.agent].push(x)});
document.getElementById("c-report").innerHTML=Object.keys(byAgent).map(function(ag){var acts=byAgent[ag],last=acts[0],icon=last.action==="working"?"🔧":last.action==="thinking"?"🧠":"💓";return'<div style="padding:3px 0;border-bottom:1px solid var(--border);font-size:12px">'+icon+" <b>"+esc(ag)+"</b> "+acts.length+" acoes · "+(last.message||"").slice(0,60)+"</div>"}).join("")||'<div class="empty-state">Nenhum agente ativo</div>';
document.getElementById("c-feed").innerHTML=(ac.activities||[]).slice(0,40).map(function(x){return'<div style="padding:2px 0;font-size:12px;border-bottom:1px solid var(--border)"><span style="color:var(--text-dim)">'+fmt(x.timestamp)+'</span> <b>'+esc(x.agent)+"</b> "+(x.action||"")+": "+(x.message||"").slice(0,80)+"</div>"}).join("")||'<div class="empty-state">Sem atividade</div>';
}catch(e){}}
refresh();setInterval(refresh,3000);
</script>""")

@router.get("/task/{task_id}", response_class=HTMLResponse)
async def task_detail(task_id: str):
    import httpx, time
    task, task_activities = {}, []
    try:
        async with httpx.AsyncClient(base_url="http://localhost:8000", timeout=10) as c:
            r = await c.get(f"/v1/swarm/task/{task_id}")
            if r.status_code == 200: task = r.json().get("task", {})
            ar = await c.get("/v1/swarm/activity?limit=100")
            task_activities = [a for a in ar.json().get("activities", []) if a.get("task_id") == task_id]
    except: pass
    title = task.get("title", "Task"); status = task.get("status", "?"); progress = task.get("progress", 0) or 0
    target = task.get("target", "?"); source = task.get("source", "?"); priority = task.get("priority", "medium")
    steps = task.get("steps", []) or []; desc = task.get("description", "") or ""
    created = task.get("created_at", 0) or 0; completed = task.get("completed_at", 0) or 0
    model_used = task.get("model", "") or ""; tokens_in = task.get("tokens_in", 0) or 0; tokens_out = task.get("tokens_out", 0) or 0
    now = time.time(); elapsed = int(now - created) if created else 0
    h, rem = divmod(elapsed, 3600); m, s = divmod(rem, 60)
    elapsed_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
    created_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created)) if created else "--"
    completed_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(completed)) if completed else ""
    si = {"pending":"⏳","in_progress":"🔄","done":"✅"}.get(status, "❓")
    pi = {"high":"🔴","medium":"🟡","low":"🟢"}.get(priority, "")
    bc = "var(--green)" if progress >= 100 else "var(--blue)" if progress > 0 else "var(--text-dim)"
    sh = ""
    if steps:
        sh = "<h3 style='font-size:13px;color:var(--text-dim);margin-bottom:8px;margin-top:12px'>📋 Checklist</h3>"
        for s in steps: sh += f"<div style='padding:4px 0;font-size:12px;border-bottom:1px solid var(--border)'>{'✅' if s.get('done') else '🔄'} {s['step']}</div>"
    ah = ""
    if task_activities:
        ah = "<h3 style='font-size:13px;color:var(--text-dim);margin-bottom:8px;margin-top:12px'>📡 Atividade</h3><div style='max-height:200px;overflow-y:auto'>"
        for a in reversed(task_activities[-20:]):
            ts = time.strftime("%H:%M:%S", time.localtime(a.get("timestamp", 0)))
            ah += f"<div style='padding:3px 0;font-size:12px;border-bottom:1px solid var(--border);display:flex;gap:8px'><span style='color:var(--text-dim);min-width:50px'>{ts}</span><span style='color:var(--blue)'>{a.get('action','')}</span><span>{a.get('message','')[:100]}</span></div>"
        ah += "</div>"
    cd = f'<p style="margin-top:8px;color:var(--text-dim);font-size:13px">{desc}</p>' if desc else ""
    cc = f'<span class="tag" style="background:rgba(0,0,0,0.2)">✅ {completed_str}</span>' if completed else ""
    html = f'''<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>{title[:50]} - Task</title><style>{_CSS}</style></head><body>
<div class="nav"><a href="/v1/observability/dashboard">📊 Dashboard</a><a href="/v1/observability/workspace">🏗️ Workspace</a></div>
<div class="card"><div style="display:flex;justify-content:space-between;align-items:start;flex-wrap:wrap;gap:8px"><h1 style="font-size:20px;flex:1">{si} {title}</h1><span class="tag {priority}">{pi} {priority}</span></div>
<div style="margin-top:8px;font-size:13px;color:var(--text-dim);display:flex;flex-wrap:wrap;gap:16px"><span>🎯 {target}</span><span>👤 {source}</span><span id="timer">⏱️ {elapsed_str}</span></div>
{cd}
<div style="margin-top:12px"><div style="display:flex;justify-content:space-between;font-size:13px"><span>{si} {status}</span><span id="pct">{progress}%</span></div><div class="bar"><div class="bar-fill" style="width:{progress}%;background:{bc}" id="pbar"></div></div></div>
<div style="margin-top:12px;display:flex;flex-wrap:wrap;gap:8px"><span class="tag" style="background:rgba(0,0,0,0.2)">📅 {created_str}</span>{cc}<span class="tag" style="background:rgba(0,0,0,0.2)">🤖 {model_used or "pendente"}</span><span class="tag" style="background:rgba(0,0,0,0.2)">🔺 {tokens_in} in</span><span class="tag" style="background:rgba(0,0,0,0.2)">🔻 {tokens_out} out</span><span class="tag" style="background:rgba(0,0,0,0.2)">💰 {tokens_in + tokens_out} total</span></div></div>
{sh}
{ah if ah else '<div class="card"><p style="color:var(--text-dim);font-size:13px">Nenhuma atividade registrada.</p></div>'}
<div class="card"><h3 style="font-size:13px;color:var(--text-dim);margin-bottom:8px">💬 Falar sobre esta task</h3><div id="chat-msgs" style="max-height:200px;overflow-y:auto;margin-bottom:8px;font-size:12px"></div><div style="display:flex;gap:8px"><input id="chat-inp" style="flex:1;padding:8px 10px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;outline:none" placeholder="Digite instruções..." onkeydown="if(event.key==='Enter')sendChat()"><button onclick="sendChat()" style="padding:8px 16px;background:var(--blue);color:#fff;border:none;border-radius:6px;cursor:pointer">Enviar</button></div><div id="chat-st" style="font-size:12px;color:var(--green);margin-top:4px"></div></div>
<script>
var TID="{task_id}",CRE={int(created)},COM={int(completed)};
if(CRE>0){{setInterval(function(){{var n=COM||Math.floor(Date.now()/1e3),e=n-CRE,h=Math.floor(e/3600),m=Math.floor((e%3600)/60),s=e%60;document.getElementById("timer").textContent="⏱️ "+(h?h+"h ":"")+m+"m "+s+"s"}},1000)}}
function sendChat(){{var inp=document.getElementById("chat-inp"),msg=inp.value.trim();if(!msg)return;inp.value="";fetch("/v1/swarm/room/geral/message",{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{sender:"sisyphus",body:"[Task "+TID.slice(0,8)+"] "+msg}})}});fetch("/v1/swarm/activity",{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{agent:"sisyphus",action:"instruction",message:msg,task_id:TID}})}});document.getElementById("chat-st").textContent="✅ Enviado! Aguardando resposta...";var d=new Date();document.getElementById("chat-msgs").innerHTML='<div style="color:var(--text);font-size:12px;padding:3px 0;border-bottom:1px solid var(--border)">🧑 Você ('+d.toLocaleTimeString()+'): '+msg+'</div>'+document.getElementById("chat-msgs").innerHTML}}
</script></body></html>'''
    return HTMLResponse(html)

@router.get("/tmux/{session_name}")
async def tmux_view(session_name: str, lines: int = 30):
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec("tmux", "capture-pane", "-t", session_name, "-p", "-S", f"-{lines}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        ap = await asyncio.create_subprocess_exec("tmux", "has-session", "-t", session_name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await ap.wait()
        return {"ok": True, "session": session_name, "alive": ap.returncode == 0, "output": stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else ""}
    except: return {"ok": False, "session": session_name, "alive": False, "error": "timeout"}
