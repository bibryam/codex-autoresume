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
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--bar);vertical-align:top}
th{color:var(--muted);font-weight:500}#threads{overflow-x:auto}.ask{max-width:300px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.state{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;background:var(--bar)}
.state.scheduled{background:#d1e9ff;color:#0a4a8a}.state.running{background:#d6f5de;color:#0b6b2a}.state.skipped,.state.max-attempts,.state.weekly-guard,.state.cooldown,.state.automation{opacity:.7}
.state.pending-in-app{background:#ffe8c2;color:#7a4a00}
button{font:inherit;font-size:12px;padding:4px 10px;border-radius:8px;border:1px solid var(--bar);background:var(--card);color:var(--fg);cursor:pointer;margin-right:4px}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
.log{font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;color:var(--muted);max-height:260px;overflow:auto}
.empty{color:var(--muted);padding:12px 0}
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
function row(e,now){const when=e.state==='scheduled'?(e.position===1?fmtT(e.resume_at)+' <small>('+rel(e.resume_at,now)+')</small>':'after #'+(e.position-1)):(e.state==='running'?'running now':'');
 return`<tr><td><b>${esc(e.cwd.split('/').pop())}</b><div class="ask" title="${esc(e.title)}">${esc(e.title)}</div></td>
 <td class="ask" title="${esc(e.last_user_message)}">${esc(e.last_user_message)}</td><td>${fmtT(e.blocked_at)}</td><td>${when}</td>
 <td><span class="state ${e.state}">${e.state}${e.position&&e.state==='scheduled'?' #'+e.position:''}</span><div class="ask" title="${esc(e.reason)}">${esc(e.reason)}</div>${e.last_result?`<div class="ask" title="${esc(e.last_result)}">last: ${esc(e.last_result)}</div>`:''}</td>
 <td><code>${esc(e.message)}</code><div class="ask">${esc(e.model||'')} ${esc(e.effort||'')}</div></td>
 <td style="white-space:nowrap">${e.state==='skipped'?`<button onclick="post('/api/threads/${e.thread_id}/unskip')">Unskip</button>`:`<button onclick="post('/api/threads/${e.thread_id}/skip')">Skip</button>`}
 <button class="primary" onclick="post('/api/threads/${e.thread_id}/resume-now')">Now</button>
 <button onclick="editMsg(${esc(JSON.stringify([e.thread_id,e.message,e.model||'',e.effort||'']))})">Edit</button></td></tr>`}
function editMsg(args){const [id,msg,model,effort]=args;const m=prompt('Message to send when resuming:',msg);if(m===null)return;
 const me=prompt('Model/effort (blank keeps thread defaults), e.g. gpt-6-astra/xhigh:',model?model+'/'+(effort||''):'');if(me===null)return;
 const [mo,ef]=me.split('/');post('/api/threads/'+id+'/message',{message:m,model:mo||null,effort:ef||null})}
async function load(){try{const s=await(await fetch('/api/state')).json();const now=s.now;
 document.getElementById('sub').textContent=`engine: ${s.engine} · updated ${new Date().toLocaleTimeString()} · ${s.config.approvals} approvals · weekly guard ${s.config.weekly_guard}% · max ${s.config.max_attempts}/day${s.note?' · '+s.note:''}`;
 const L=s.limits||{};document.getElementById('limits').innerHTML=gauge('5h window',L.primary,now)+gauge('weekly',L.secondary,now)+`<div class="sub" style="margin:6px 0 0">usage allowed: ${L.allowed===true?'yes':(L.allowed===false?'no':'unknown')} · plan ${L.plan||'?'} · read ${fmtT(L.fetched_at)}${s.limits_stale?' · STALE (engine down)':''}</div>`;
 document.getElementById('threads').innerHTML=s.entries.length?`<table><tr><th>Project / thread</th><th>Last ask</th><th>Blocked</th><th>Resume at</th><th>State</th><th>Message</th><th></th></tr>${s.entries.map(e=>row(e,now)).join('')}</table>`:'<div class="empty">No threads are blocked on the usage limit.</div>';
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
