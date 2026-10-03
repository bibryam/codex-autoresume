from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>codex-resume</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{color-scheme:light dark;--fg:#1d1d1f;--bg:#f5f5f7;--card:#fff;--muted:#6e6e73;--bar:#e5e5ea;--ok:#34c759;--warn:#ff9f0a;--bad:#ff3b30;--accent:#0a84ff}
@media(prefers-color-scheme:dark){:root{--fg:#f5f5f7;--bg:#1c1c1e;--card:#2c2c2e;--muted:#98989d;--bar:#3a3a3c}}
body{margin:0;padding:24px;font:14px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
h1{font-size:20px;margin:0 0 4px}h2{font-size:14px;margin:24px 0 8px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.sub{color:var(--muted);margin-bottom:16px}.card{background:var(--card);border-radius:12px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,.08)}
.gauge{display:grid;grid-template-columns:90px 1fr 300px;gap:12px;align-items:center;margin:8px 0}
.bar{height:10px;background:var(--bar);border-radius:5px;overflow:hidden}.bar i{display:block;height:100%;background:var(--ok)}
.bar.warn i{background:var(--warn)}.bar.bad i{background:var(--bad)}
table{width:100%;border-collapse:collapse;table-layout:fixed;min-width:1000px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--bar);vertical-align:top;overflow-wrap:anywhere}
th{color:var(--muted);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.03em}#threads{overflow-x:auto}
.ask{color:var(--muted);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.4em;max-height:2.8em}
.yes,.no,.run,.wait{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;margin-bottom:3px}
.yes{background:#d6f5de;color:#0b6b2a}.run{background:#d1e9ff;color:#0a4a8a}.wait{background:#ffe8c2;color:#7a4a00}.no{background:var(--bar);color:var(--muted)}
.when{font-weight:600}.reason{color:var(--muted);font-size:12px}button:disabled{opacity:.4;cursor:default}
.state{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:var(--bar)}
.state.scheduled{background:#d1e9ff;color:#0a4a8a}.state.running{background:#d6f5de;color:#0b6b2a}.state.skipped,.state.max-attempts,.state.weekly-guard,.state.cooldown,.state.automation{opacity:.7}
.state.pending-in-app{background:#ffe8c2;color:#7a4a00}
button{font:inherit;font-size:12px;padding:4px 10px;border-radius:8px;border:1px solid var(--bar);background:var(--card);color:var(--fg);cursor:pointer;margin-right:4px}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}button.danger{background:var(--bad);color:#fff;border-color:var(--bad)}
.log{font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;color:var(--muted);max-height:260px;overflow:auto}
.empty{color:var(--muted);padding:12px 0}
.editrow td{background:rgba(10,132,255,.06)}.editform{display:grid;grid-template-columns:1fr 200px 140px auto;gap:10px;align-items:end}
.editform label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px}.editform textarea,.editform input,.editform select{width:100%;box-sizing:border-box;font:inherit;padding:6px 8px;border-radius:8px;border:1px solid var(--bar);background:var(--card);color:var(--fg)}
.editform textarea{min-height:56px;resize:vertical}
</style></head><body>
<h1>codex-resume</h1><div class="sub" id="sub">loading…</div>
<div class="card" id="limits"></div>
<h2>Blocked threads</h2><div class="card" id="threads"></div>
<h2>History</h2><div class="card log" id="history"></div>
<h2>Log</h2><div class="card log" id="log"></div>
<script>
const fmtT=s=>s?new Date(s*1000).toLocaleString([], {weekday:'short',hour:'2-digit',minute:'2-digit'}):'';
const rel=(s,now)=>{if(!s)return'';const d=s-now;const a=Math.abs(d);const m=Math.round(a/60);const t=m<60?m+' min':(Math.round(a/360)/10)+' h';return d>=0?'in '+t:t+' ago'};
const esc=t=>(t||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function post(p,b){await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):null});load()}
function gauge(name,w,now){if(!w)return`<div class="gauge"><b>${name}</b><div class="bar"><i style="width:0"></i></div><span>unknown</span></div>`;
 const c=w.used_percent>=100?'bad':(w.used_percent>=80?'warn':'');return`<div class="gauge"><b>${name}</b><div class="bar ${c}"><i style="width:${Math.min(100,w.used_percent)}%"></i></div><span>${w.used_percent}% used · resets ${fmtT(w.resets_at)} (${rel(w.resets_at,now)})</span></div>`}
function resumeCell(e,now){
 switch(e.state){
  case 'running': return `<span class="run">Running now</span>`;
  case 'scheduled': return e.position===1
    ? `<span class="yes">Yes · #1</span><div class="when">${fmtT(e.resume_at)} <small>(${rel(e.resume_at,now)})</small></div><div class="reason">${esc(e.reason)}</div>`
    : `<span class="yes">Yes · #${e.position}</span><div class="when">after #${e.position-1} finishes</div><div class="reason">${esc(e.reason)}</div>`;
  case 'cooldown': case 'waiting-reset': return `<span class="wait">Yes · waiting</span><div class="reason">${esc(e.reason)}</div>`;
  case 'pending-in-app': return `<span class="wait">Queued in the app</span><div class="reason">${esc(e.reason)}</div>`;
  case 'skipped': return `<span class="no">No · skipped</span><div class="reason">press Schedule to put it back in the queue</div>`;
  case 'automation': return `<span class="no">No · automation thread</span><div class="reason">${esc(e.reason)}. Press Schedule if you want it resumed anyway.</div>`;
  case 'max-attempts': return `<span class="no">No · attempts used</span><div class="reason">${esc(e.reason)}. Press Schedule to allow one more.</div>`;
  case 'weekly-guard': return `<span class="no">No · weekly guard</span><div class="reason">${esc(e.reason)}. Press Schedule to resume anyway.</div>`;
  default: return `<span class="no">${esc(e.state)}</span><div class="reason">${esc(e.reason)}</div>`;
 }}
function row(e,now){const parked=['skipped','automation','max-attempts','weekly-guard'].includes(e.state);const id=esc(JSON.stringify(e.thread_id));
 return`<tr><td><b>${esc(e.cwd.split('/').pop())}</b><div class="ask" title="${esc(e.title)}">${esc(e.title)}</div></td>
 <td><div class="ask" title="${esc(e.last_user_message)}">${esc(e.last_user_message)}</div></td>
 <td>${fmtT(e.blocked_at)}<div class="reason">${rel(e.blocked_at,now)}</div></td>
 <td>${resumeCell(e,now)}${e.last_result?`<div class="reason" title="${esc(e.last_result)}">last run: ${esc(e.last_result)}</div>`:''}</td>
 <td><code>${esc(e.message)}</code><div class="reason">${esc(e.model||'thread default')} · ${esc(e.effort||'default effort')}</div></td>
 <td style="white-space:nowrap">${parked
   ? `<button class="primary" onclick="post('/api/threads/${e.thread_id}/schedule')">Schedule</button><button disabled title="already not resuming">Skip</button>`
   : `<button class="danger" onclick="post('/api/threads/${e.thread_id}/skip')">Skip</button>`}
 <button onclick="post('/api/threads/${e.thread_id}/resume-now')" title="resume immediately, ignoring the window and guards">Now</button>
 <button onclick="startEdit(${id})">${editing===e.thread_id?'Close':'Edit'}</button></td></tr>${editing===e.thread_id?editRow(e):''}`}
let editing=null;
function startEdit(id){editing=(editing===id)?null:id;load(true)}
let MODELS=[];
function modelOptions(sel){const ids=MODELS.map(m=>m.id);const extra=sel&&!ids.includes(sel)?[{id:sel,display:sel+' (from thread)',efforts:[]}]:[];
 return [...extra,...MODELS].map(m=>`<option value="${esc(m.id)}" ${m.id===sel?'selected':''}>${esc(m.display)}</option>`).join('')}
function effortOptions(modelId,sel){const m=MODELS.find(x=>x.id===modelId);let efforts=m&&m.efforts.length?m.efforts.slice():['low','medium','high','xhigh','max','ultra'];
 if(sel&&!efforts.includes(sel))efforts.unshift(sel);return efforts.map(x=>`<option value="${esc(x)}" ${x===sel?'selected':''}>${esc(x)}</option>`).join('')}
function onModelChange(selEl){const form=selEl.form;const m=MODELS.find(x=>x.id===selEl.value);form.effort.innerHTML=effortOptions(selEl.value,m?m.default_effort:form.effort.value)}
function editRow(e){return`<tr class="editrow"><td colspan="6"><form class="editform" onsubmit="return saveEdit(event,${esc(JSON.stringify(e.thread_id))})">
 <div><label>Message sent when resuming</label><textarea name="message" required>${esc(e.message)}</textarea></div>
 <div><label>Model (thread: ${esc(e.model||'default')})</label><select name="model" onchange="onModelChange(this)">${modelOptions(e.model)}</select></div>
 <div><label>Reasoning effort (thread: ${esc(e.effort||'default')})</label><select name="effort">${effortOptions(e.model,e.effort)}</select></div>
 <div><button class="primary" type="submit">Save</button><button type="button" onclick="startEdit(${esc(JSON.stringify(e.thread_id))})">Cancel</button></div></form></td></tr>`}
async function saveEdit(ev,id){ev.preventDefault();const f=new FormData(ev.target);editing=null;
 await post('/api/threads/'+id+'/message',{message:f.get('message'),model:(f.get('model')||'').trim()||null,effort:(f.get('effort')||'').trim()||null});return false}
async function load(force){if(editing&&!force)return;try{const s=await(await fetch('/api/state')).json();const now=s.now;MODELS=s.models||[];
 document.getElementById('sub').textContent=`engine: ${s.engine} · updated ${new Date().toLocaleTimeString()} · ${s.config.approvals} approvals · weekly guard ${s.config.weekly_guard}% · max ${s.config.max_attempts}/day${s.note?' · '+s.note:''}`;
 const L=s.limits||{};document.getElementById('limits').innerHTML=gauge('5h window',L.primary,now)+gauge('weekly',L.secondary,now)+`<div class="sub" style="margin:6px 0 0">usage allowed: ${L.allowed===true?'yes':(L.allowed===false?'no':'unknown')} · plan ${L.plan||'?'} · read ${fmtT(L.fetched_at)}${s.limits_stale?' · STALE (engine down)':''}</div>`;
 document.getElementById('threads').innerHTML=s.entries.length?`<table><colgroup><col style="width:19%"><col style="width:20%"><col style="width:9%"><col style="width:23%"><col style="width:12%"><col style="width:17%"></colgroup><tr><th>Project / thread</th><th>Last ask</th><th>Blocked</th><th>Will it resume?</th><th>Message · model</th><th>Actions</th></tr>${s.entries.map(e=>row(e,now)).join('')}</table>`:'<div class="empty">No threads are blocked on the usage limit.</div>';
 document.getElementById('history').textContent=s.history.map(h=>`${fmtT(h.ts)}  ${(h.thread_id||'').slice(0,13).padEnd(13)}  ${h.event.padEnd(18)} ${h.detail||''}`).join('\n')||'nothing yet';
 document.getElementById('log').textContent=(s.log||[]).join('\n');}catch(e){document.getElementById('sub').textContent='daemon unreachable: '+e}}
load();setInterval(load,10000);
</script></body></html>"""


def make_server(scheduler, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "codex-resume"

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
            if host not in ("127.0.0.1", "localhost", "::1"):
                self._json(403, {"error": "forbidden host"})
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(200, scheduler.snapshot())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                return self._json(415, {"error": "application/json required"})
            parts = self.path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "threads"]:
                return self._json(404, {"error": "not found"})
            tid, action = parts[2], parts[3]
            known = getattr(scheduler, "known_threads", lambda: None)()
            if known is not None and tid not in known:
                return self._json(404, {"error": "unknown thread"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > 65536:
                return self._json(400, {"error": "bad content length"})
            raw = self.rfile.read(length) if length else b""
            if action == "skip":
                scheduler.set_skip(tid, True)
            elif action == "unskip":
                scheduler.set_skip(tid, False)
            elif action == "schedule":
                scheduler.schedule(tid)
            elif action == "resume-now":
                scheduler.resume_now(tid)
            elif action == "message":
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = None
                if not isinstance(body, dict) or not isinstance(body.get("message"), str) or not body["message"].strip():
                    return self._json(400, {"error": "message required"})
                scheduler.set_message(tid, body["message"], body.get("model") or None, body.get("effort") or None)
            else:
                return self._json(404, {"error": "unknown action"})
            self._json(200, {"ok": True})

    return ThreadingHTTPServer((host, port), Handler)
