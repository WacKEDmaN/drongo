"""Local web dashboard + control panel for DRONGO.

Tabs: Home (status + live stats sidebar + activity + gallery), System (full live
host stats), Projects (everything it built — open/run/tag, flag broken ones for
fixing), and Control (pause / resume / run-now / restart).

Runs as the unprivileged 'drongo' user, LAN-locked + password-protected. Controls
work via files / DB flags the agent watches — no systemctl, no root. The one
exception is "Run" (executes a generated .py): it runs as the same unprivileged
user with a timeout + memory cap, only on files under projects/, and can be
turned off with web.allow_run: false.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from flask import Flask, Response, abort, render_template_string, request, send_from_directory

from . import safeguard, watchdog
from .memory import Memory, utc_iso
from .safeguard import integrity_status
from .tools import system_stats

log = logging.getLogger("agent.server")
_PRIVATE = ("127.0.0.1", "localhost", "::1", "")

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{{ name }} console</title>
<style>
 :root{--bg:#0e1116;--card:#171c24;--mut:#8a93a3;--fg:#e6edf3;--ac:#4cc2ff;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--bd:#232a34}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 ui-sans-serif,system-ui,sans-serif}
 header{padding:14px 20px;border-bottom:1px solid var(--bd);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 h1{font-size:19px;margin:0}
 .pill{font-size:12px;padding:3px 9px;border-radius:20px;border:1px solid var(--bd);color:var(--mut)}
 .pill.ok{color:var(--ok);border-color:#1c3} .pill.bad{color:var(--bad);border-color:#622} .pill.warn{color:var(--warn);border-color:#640}
 nav{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
 nav button{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:6px 14px;cursor:pointer;font-size:14px}
 nav button.on{background:var(--ac);color:#02121f;border-color:var(--ac);font-weight:600}
 main{max-width:1080px;margin:0 auto;padding:20px}
 .tab{display:none} .tab.on{display:block}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin:12px 0}
 .card h3{margin:0 0 4px;font-size:15px} .meta{color:var(--mut);font-size:12.5px}
 a{color:var(--ac);text-decoration:none} a:hover{text-decoration:underline}
 .art{display:inline-block;margin:4px 8px 0 0;font-size:13px}
 h2{font-size:14px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin:26px 0 8px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
 .grid img{width:100%;height:150px;object-fit:cover;border-radius:8px;border:1px solid var(--bd)}
 .stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
 .stat{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px}
 .stat .v{font-size:22px;font-weight:600} .stat .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
 .bar{height:6px;border-radius:4px;background:#0a0d12;margin-top:6px;overflow:hidden}.bar>i{display:block;height:100%;background:var(--ac)}
 .chip{display:inline-block;font-size:11px;padding:2px 8px;border-radius:12px;border:1px solid var(--bd);color:var(--mut);margin:0 4px 4px 0}
 .chip.fix{color:var(--bad);border-color:#622}
 button.act{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:5px 11px;cursor:pointer;font-size:12.5px;margin:4px 6px 0 0}
 button.act:hover{border-color:var(--ac)} button.danger:hover{border-color:var(--bad)}
 .big{font-size:15px;padding:10px 18px;margin:6px 10px 6px 0}
 .runbtn{font-size:11px;padding:1px 7px;margin-left:4px;background:#0a0d12;color:var(--ok);border:1px solid #1c3;border-radius:6px;cursor:pointer}
 .set h3{margin:16px 0 4px;font-size:14px}
 .set label{display:block;margin:7px 0;font-size:13px;color:var(--mut)}
 .set input,.set select{display:block;width:100%;max-width:360px;background:#0a0d12;color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:5px 8px;font-size:13px;margin-top:2px}
 .set .prow input{max-width:none}
 .set input[type=checkbox]{display:inline-block;width:auto;margin-right:6px}
 .prow{display:flex;gap:8px;align-items:center;margin:6px 0;flex-wrap:wrap}
 .prow input{flex:1;min-width:120px}
 .homewrap{display:flex;gap:16px;align-items:flex-start}
 .homemain{flex:1;min-width:0} .homeside{width:240px;flex:none;position:sticky;top:14px}
 .homeside .row{display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-bottom:1px solid var(--bd);font-size:13px}
 .homeside .row b{font-weight:600} .homeside .row:last-child{border:0}
 @media(max-width:760px){.homewrap{flex-direction:column}.homeside{width:100%;position:static}}
 #toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--ac);color:#02121f;padding:8px 16px;border-radius:8px;font-weight:600;opacity:0;transition:.2s;pointer-events:none;z-index:20}
 #toast.show{opacity:1}
 .modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:20px;z-index:10}
 .modal.show{display:flex}
 .modal .box{background:var(--card);border:1px solid var(--bd);border-radius:10px;max-width:780px;width:100%;max-height:82vh;overflow:auto;padding:16px}
 .modal pre{white-space:pre-wrap;word-break:break-word;font-size:12.5px;background:#0a0d12;padding:10px;border-radius:6px;margin:0}
</style></head><body>
<header>
 <h1>{{ name }}</h1>
 <span id=p_status class="pill">…</span>
 <span id=p_alive class="pill {{ 'ok' if alive else 'bad' }}">{{ 'alive '+hb if alive else 'no heartbeat' }}</span>
 {% if safe %}<span class="pill warn">SAFE MODE</span>{% endif %}
 <span class="pill {{ 'ok' if integ_ok else 'bad' }}">guard {{ 'ok' if integ_ok else 'CHECK' }}</span>
 <nav>
  <button class="on" data-t=home>Home</button>
  <button data-t=system>System</button>
  <button data-t=projects>Projects</button>
  <button data-t=control>Control</button>
 </nav>
</header>
<main>

 <section id=home class="tab on"><div class=homewrap>
  <div class=homemain>
   {% if working_on %}<div class="card"><b>⏳ Working on:</b> {{ working_on.title }}
     <span class=meta>({{ working_on.type }} · attempt {{ working_on.attempt }})</span></div>{% endif %}
   <h2>Recent activity</h2>
   {% for j in journal %}
    <div class="card">
      <h3>{{ j.title }} <span class="meta">· {{ j.kind }}{% if j.task_type %} · {{ j.task_type }}{% endif %}</span></h3>
      <div class="meta">{{ j.when }}{% if j.provider %} · via {{ j.provider }}{% endif %}{% if not j.ok %} · ⚠ unfinished{% endif %}</div>
      <p>{{ j.body }}</p>
      {% for a in j.arts %}<a class="art" href="/file/{{ a.path }}" target=_blank>▸ {{ a.label }}</a>{% endfor %}
    </div>
   {% else %}<p class="meta">Nothing yet — give it a little time.</p>{% endfor %}
   {% if images %}<h2>Gallery</h2><div class="grid">
     {% for im in images %}<a href="/file/images/{{ im }}" target=_blank><img loading=lazy src="/file/images/{{ im }}"></a>{% endfor %}
   </div>{% endif %}
  </div>
  <aside class=homeside><div class=card>
    <h3 style="margin-top:0">Live</h3>
    <div id=homestats class=meta>loading…</div>
  </div></aside>
 </div></section>

 <section id=system class="tab">
  <h2>Host</h2>
  <div class="stats" id=sysgrid><div class=stat><div class=k>loading…</div></div></div>
  <p class="meta" id=sysmodel></p>
 </section>

 <section id=projects class="tab">
  <h2>Projects it has built — open, run, tag, or flag broken ones for a fix</h2>
  {% for j in projects %}
   <div class="card" data-id="{{ j.id }}">
     <h3>{{ j.title }} {% if not j.ok %}<span class="pill bad">unfinished</span>{% endif %}</h3>
     <div class="meta">{{ j.when }}{% if j.provider %} · via {{ j.provider }}{% endif %}</div>
     <p>{{ j.body }}</p>
     {% for a in j.arts %}<span style="white-space:nowrap"><a class="art" href="/file/{{ a.path }}" target=_blank>▸ {{ a.label }}</a>{% if a.path.endswith('.py') and allow_run %}<button class="runbtn" onclick="runpy('{{ a.path }}')">▶ run</button>{% endif %}</span> {% endfor %}
     <div class="chips" id="chips-{{ j.id }}" style="margin-top:8px">
       {% for t in j.tags %}<span class="chip {{ 'fix' if t=='needs-fix' else '' }}">{{ t }}</span>{% endfor %}
     </div>
     <button class="act danger" onclick="fixit({{ j.id }})">🔧 Fix this</button>
     <button class="act" onclick="addtag({{ j.id }})">+ tag</button>
   </div>
  {% else %}<p class="meta">No finished projects yet.</p>{% endfor %}
 </section>

 <section id=control class="tab">
  <h2>Controls</h2>
  <div class="card">
   <button class="act big" onclick="ctl('run')">▶ Run a cycle now</button>
   <button class="act big" onclick="ctl('pause')">⏸ Pause</button>
   <button class="act big" onclick="ctl('resume')">⏵ Resume</button>
   <button class="act big" onclick="ctl('stop')">⏹ Stop (dormant)</button>
   <button class="act big danger" onclick="if(confirm('Restart the agent?'))ctl('restart')">⟳ Restart</button>
   <p class="meta">Pause/Stop just idle it (removable). Restart relaunches via systemd.
     Flagged fixes are worked before new projects.</p>
   <p class="meta" id=fixq></p>
  </div>

  <h2>Settings <span class="meta">— stored on the agent; “Save &amp; Restart” to apply</span></h2>
  <div class="card set">
   <h3>Cooldowns &amp; loop</h3>
   <label>Seconds between projects (cycle gap)<input id=s_interval value="{{ sv.loop.interval_seconds }}"></label>
   <label>Jitter (± seconds)<input id=s_jitter value="{{ sv.loop.jitter_seconds }}"></label>
   <label>Tool steps per cycle<input id=s_steps value="{{ sv.loop.max_steps }}"></label>
   <label>Resume attempts before giving up on a project<input id=s_attempts value="{{ sv.loop.max_resume_attempts }}"></label>
   <label>Min seconds between LLM calls (throttle)<input id=s_minc value="{{ sv.min_call }}"></label>
   <label>Provider order<select id=s_prefer>
     <option value=cloud_first {{ 'selected' if sv.prefer=='cloud_first' }}>cloud first</option>
     <option value=local_first {{ 'selected' if sv.prefer=='local_first' }}>local first</option></select></label>

   <h3>Providers &amp; API keys</h3>
   {% for p in sv.providers %}
    <div class=prow data-name="{{ p.name }}">
      <label style="flex:0 0 auto;margin:0"><input type=checkbox id="pe_{{ p.name }}" {{ 'checked' if p.enabled }}> {{ p.name }}</label>
      <input id="pm_{{ p.name }}" value="{{ p.model }}" placeholder="model">
      {% if p.key_env %}<input id="pk_{{ p.name }}" type=password autocomplete=off placeholder="{{ 'key set — blank keeps it' if p.key_set else 'paste '+p.key_env }}">{% endif %}
    </div>
   {% endfor %}

   <h3>Alerts</h3>
   <label><input type=checkbox id=s_notify {{ 'checked' if sv.notify }}> Alert on every cycle (not just completions)</label>
   <label>Discord webhook URL<input id=s_discord type=password autocomplete=off placeholder="{{ 'set — blank keeps it' if sv.discord_set else 'paste webhook URL' }}"></label>
   <label>ntfy topic (optional)<input id=s_ntfy value="{{ sv.ntfy }}"></label>
   <label>LED gpiochip<input id=s_ledchip value="{{ sv.led_chip }}"></label>
   <label>LED line offset (blank = LED off)<input id=s_ledline value="{{ sv.led_line }}"></label>

   <div style="margin-top:12px">
     <button class="act big" onclick="saveSettings(false)">Save</button>
     <button class="act big danger" onclick="saveSettings(true)">Save &amp; Restart</button>
   </div>
  </div>

  <h2>LLM usage today</h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
   <tr style="color:var(--mut);text-align:left"><th>provider</th><th>today</th><th>min</th><th>total</th><th>cooldown</th></tr>
   {% for u in usage %}<tr style="border-top:1px solid var(--bd)"><td>{{ u.provider }}</td><td>{{ u.day_count }}</td><td>{{ u.minute_count }}</td><td>{{ u.total }}</td><td>{{ u.cool }}</td></tr>{% endfor %}
  </table>
 </section>
</main>
<div id=toast></div>
<div class=modal id=modal onclick="if(event.target===this)this.classList.remove('show')">
  <div class=box><h3 id=modaltitle style="margin-top:0"></h3><pre id=modalout></pre>
    <button class="act" onclick="document.getElementById('modal').classList.remove('show')">close</button></div>
</div>
<script>
 const $=s=>document.querySelector(s);
 function showTab(t){
   document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x.dataset.t===t));
   document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x.id===t));
   history.replaceState(null,'','#'+t);
 }
 document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>showTab(b.dataset.t));
 if(location.hash){const t=location.hash.slice(1); if($('#'+t)) showTab(t);}
 function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1900);}
 function ctl(a){fetch('/control/'+a,{method:'POST'}).then(r=>r.json()).then(d=>{toast(a+(d.ok?' ✓':' — '+(d.error||'failed')));refresh();});}
 function addchip(id,text,cls){const c=$('#chips-'+id);if(!c)return;
   if([...c.children].some(x=>x.textContent===text))return;
   const s=document.createElement('span');s.className='chip '+(cls||'');s.textContent=text;c.appendChild(s);}
 function fixit(id){const n=prompt("What's wrong / what should it fix? (optional)");if(n===null)return;
   fetch('/control/fix',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,note:n})})
   .then(r=>r.json()).then(d=>{if(d.ok){addchip(id,'needs-fix','fix');toast('Queued for fixing ✓');}else toast(d.error||'failed');});}
 function addtag(id){const t=prompt('Tag (e.g. favourite, idea, wip):');if(!t)return;
   fetch('/control/tag',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,tag:t})})
   .then(r=>r.json()).then(d=>{if(d.ok)addchip(id,t);});}
 function runpy(path){toast('running '+path.split('/').pop()+'…');
   $('#modaltitle').textContent='▶ '+path; $('#modalout').textContent='running…'; $('#modal').classList.add('show');
   fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})})
   .then(r=>r.json()).then(d=>{$('#modalout').textContent=d.ok?('(exit '+d.rc+')\\n'+(d.out||'(no output)')):('ERROR: '+(d.error||'failed'));});}
 const PKEY={{ pkey_json|safe }};
 const gv=id=>{const e=document.getElementById(id);return e?e.value.trim():'';};
 const gc=id=>{const e=document.getElementById(id);return e?e.checked:false;};
 const gn=id=>{const v=parseInt(gv(id),10);return isNaN(v)?null:v;};
 function saveSettings(restart){
   const provs={},env={};
   document.querySelectorAll('.prow').forEach(r=>{const n=r.dataset.name;
     provs[n]={enabled:gc('pe_'+n)}; const m=gv('pm_'+n); if(m)provs[n].model=m;
     const k=gv('pk_'+n); if(k&&PKEY[n])env[PKEY[n]]=k;});
   const dw=gv('s_discord'); if(dw)env.DISCORD_WEBHOOK_URL=dw;
   const ll=gv('s_ledline'); env.DRONGO_LED_CHIP=gv('s_ledchip'); if(ll)env.DRONGO_LED_LINE=ll;
   const loop={};[['interval_seconds','s_interval'],['jitter_seconds','s_jitter'],
     ['max_steps','s_steps'],['max_resume_attempts','s_attempts']].forEach(([k,id])=>{
     const v=gn(id); if(v!=null)loop[k]=v;});
   const llm={prefer:gv('s_prefer'),providers:provs}; const mc=gn('s_minc'); if(mc!=null)llm.min_call_interval_seconds=mc;
   const s={loop,llm,alerts:{notify_every_cycle:gc('s_notify'),
     ntfy:{topic:gv('s_ntfy'),enabled:!!gv('s_ntfy')},led:{enabled:!!ll}},env};
   fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({settings:s,restart})}).then(r=>r.json())
     .then(d=>toast(d.ok?(restart?'Saved — restarting…':'Saved ✓ (restart to apply)'):('failed: '+(d.error||''))));
 }
 function pct(v){return v==null?'—':v+'%';}
 function refresh(){fetch('/api/system').then(r=>r.json()).then(d=>{
   const s=d.stats||{};
   const status=(d.status||'?')+(d.next_cycle_in!=null&&d.status=='sleeping'?' · next in '+d.next_cycle_in+'s':'');
   $('#p_status').textContent=status;
   const cells=[
     ['status',d.status||'?'],['time',s.time||'—'],['uptime',s.uptime||'—'],
     ['cpu',pct(s.cpu_pct)],['memory',s.mem_pct!=null?s.mem_used_mb+'/'+s.mem_total_mb+'MB':'—'],
     ['disk',s.disk_pct!=null?s.disk_used_gb+'/'+s.disk_total_gb+'GB':'—'],
     ['load',(s.load||[]).join(' ')||'—'],
     ['temps',(s.temps||[]).map(t=>t.label.replace('-thermal','')+' '+t.c+'°C').join('  ')||'—'],
     ['fix queue',d.fix_queue||0],
   ];
   $('#sysgrid').innerHTML=cells.map(([k,v])=>{
     let bar='';const p={cpu:s.cpu_pct,memory:s.mem_pct,disk:s.disk_pct}[k];
     if(p!=null)bar='<div class=bar><i style="width:'+Math.min(100,p)+'%;background:'+(p>90?'var(--bad)':p>70?'var(--warn)':'var(--ac)')+'"></i></div>';
     return '<div class=stat><div class=k>'+k+'</div><div class=v>'+v+'</div>'+bar+'</div>';
   }).join('');
   $('#sysmodel').textContent=(s.model||'')+(s.date?'  ·  '+s.date:'');
   const t0=(s.temps&&s.temps[0])?(s.temps[0].c+'°C'):'—';
   const rows=[['status',status],['time',s.time||'—'],['date',s.date||''],['uptime',s.uptime||'—'],
     ['cpu',pct(s.cpu_pct)],['mem',pct(s.mem_pct)],['disk',pct(s.disk_pct)],['temp',t0],
     ['load',(s.load||[]).join(' ')||'—']];
   $('#homestats').innerHTML=rows.map(([k,v])=>'<div class=row><span>'+k+'</span><b>'+v+'</b></div>').join('');
   $('#fixq').textContent=(d.fix_queue||0)+' project(s) queued for fixing.';
 }).catch(()=>{});}
 refresh(); setInterval(refresh,4000);
</script>
</body></html>"""


def _parse_tags(raw):
    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


def create_app(cfg, mem: Memory) -> Flask:
    app = Flask(__name__)
    name = cfg.get("identity", "name", default="DRONGO")
    ws = Path(cfg.workspace)
    allow_run = bool(cfg.get("web", "allow_run", default=True))

    password = os.environ.get("DRONGO_WEB_PASSWORD", "")
    nets = []
    for c in os.environ.get("DRONGO_WEB_ALLOW", "").split(","):
        c = c.strip()
        if c:
            try:
                nets.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                log.warning("ignoring bad DRONGO_WEB_ALLOW entry: %s", c)

    @app.before_request
    def _gate():
        if nets:
            try:
                ip = ipaddress.ip_address((request.remote_addr or "").split("%")[0])
            except ValueError:
                return Response("forbidden\n", 403)
            if not any(ip in n for n in nets):
                return Response("forbidden\n", 403)
        if password:
            a = request.authorization
            if not a or not a.password or not hmac.compare_digest(a.password, password):
                return Response("authentication required\n", 401,
                                {"WWW-Authenticate": 'Basic realm="DRONGO"'})
        return None

    def _journal(limit):
        rows = []
        for j in mem.recent_journal(limit):
            rows.append({"id": j["id"], "title": j["title"] or "", "kind": j["kind"],
                         "task_type": j["task_type"], "body": j["body"] or "",
                         "provider": j["provider"], "ok": bool(j["ok"]),
                         "when": utc_iso(j["ts"]),
                         "arts": json.loads(j["artifacts"] or "[]"),
                         "tags": _parse_tags(j["tags"] if "tags" in j.keys() else "")})
        return rows

    @app.route("/")
    def index():
        rows = _journal(60)
        usage = []
        for u in mem.usage_summary():
            cool = f"{int(u['cooldown_until'] - time.time())}s" if u["cooldown_until"] and u["cooldown_until"] > time.time() else ""
            usage.append({**u, "cool": cool})
        age = watchdog.heartbeat_age(cfg)
        integ = integrity_status()
        running_root = getattr(os, "geteuid", lambda: -1)() == 0
        integ_ok = integ["hash_ok"] and (running_root or not integ["writable_by_me"])
        sv, pkey = _settings_view(cfg, mem)
        return render_template_string(
            PAGE, name=name, journal=rows,
            projects=[r for r in rows if r["kind"] == "cycle"],
            images=_ls(cfg.images, (".png", ".jpg", ".jpeg")),
            usage=usage, allow_run=allow_run, sv=sv, pkey_json=json.dumps(pkey),
            alive=age is not None and age < 1800,
            hb=(f"{int(age)}s ago" if age is not None else ""),
            safe=bool(mem.recall("safe_mode")),
            working_on=mem.recall("working_on"),
            integ_ok=integ_ok)

    @app.route("/settings", methods=["POST"])
    def save_settings():
        d = request.get_json(silent=True) or {}
        s = d.get("settings")
        if not isinstance(s, dict):
            return {"ok": False, "error": "bad settings"}, 400
        cur = mem.recall("settings") or {}
        env = dict(cur.get("env") or {})
        env.update({k: v for k, v in (s.get("env") or {}).items() if v})
        s["env"] = env                       # keep existing keys when fields left blank
        mem.remember("settings", s)
        if d.get("restart"):
            mem.remember("restart_requested", True)
        log.info("settings saved via dashboard (restart=%s)", bool(d.get("restart")))
        return {"ok": True}

    @app.route("/api/system")
    def api_system():
        age = watchdog.heartbeat_age(cfg)
        nxt = mem.recall("next_cycle_ts")
        return {
            "stats": system_stats(),
            "status": mem.recall("status") or "starting",
            "working_on": mem.recall("working_on"),
            "heartbeat_age": age,
            "alive": age is not None and age < 1800,
            "next_cycle_in": max(0, int(nxt - time.time())) if nxt else None,
            "safe_mode": bool(mem.recall("safe_mode")),
            "fix_queue": len(mem.fix_queue()),
        }

    @app.route("/api/status")
    def api_status():
        age = watchdog.heartbeat_age(cfg)
        return {"name": name, "status": mem.recall("status"),
                "heartbeat_age": age, "alive": age is not None and age < 1800,
                "integrity": integrity_status(), "usage": mem.usage_summary()}

    @app.route("/control/<action>", methods=["POST"])
    def control(action):
        pause, stop = ws / "PAUSE", ws / "STOP"
        try:
            if action == "pause":
                pause.touch()
            elif action == "resume":
                pause.unlink(missing_ok=True); stop.unlink(missing_ok=True)
                mem.remember("run_now", True)
            elif action == "run":
                pause.unlink(missing_ok=True); mem.remember("run_now", True)
            elif action == "stop":
                stop.touch()
            elif action == "restart":
                mem.remember("restart_requested", True)
            else:
                return {"ok": False, "error": "unknown action"}, 400
        except Exception as e:
            return {"ok": False, "error": str(e)}, 500
        log.info("dashboard control: %s", action)
        return {"ok": True, "action": action}

    @app.route("/control/tag", methods=["POST"])
    def control_tag():
        d = request.get_json(silent=True) or {}
        try:
            jid = int(d.get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad id"}, 400
        tag = (d.get("tag") or "").strip()[:30]
        if not tag:
            return {"ok": False, "error": "empty tag"}, 400
        return {"ok": True, "tags": mem.tag_entry(jid, tag, on=bool(d.get("on", True)))}

    @app.route("/control/fix", methods=["POST"])
    def control_fix():
        d = request.get_json(silent=True) or {}
        try:
            jid = int(d.get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad id"}, 400
        match = [j for j in mem.recent_journal(300) if j["id"] == jid]
        if not match:
            return {"ok": False, "error": "entry not found"}, 404
        j = match[0]
        arts = json.loads(j["artifacts"] or "[]")
        mem.add_fix({"id": jid, "title": j["title"], "note": (d.get("note") or "")[:200],
                     "artifacts": [a["path"] for a in arts]})
        mem.tag_entry(jid, "needs-fix", on=True)
        log.info("flagged for fixing: %s", j["title"])
        return {"ok": True}

    @app.route("/run", methods=["POST"])
    def run_py():
        if not allow_run:
            return {"ok": False, "error": "running is disabled (web.allow_run: false)"}, 403
        rel = ((request.get_json(silent=True) or {}).get("path") or "").strip()
        if not rel.endswith(".py"):
            return {"ok": False, "error": "only .py files can be run"}, 400
        try:
            full = safeguard.safe_join(str(ws), rel)
        except Exception:
            return {"ok": False, "error": "path escapes the workspace"}, 400
        if not os.path.isfile(full) or "/projects/" not in full.replace(os.sep, "/"):
            return {"ok": False, "error": "only scripts under projects/ can be run"}, 404
        venv_py = os.path.join(str(cfg.project_venv), "bin", "python")
        py = venv_py if os.path.exists(venv_py) else "python3"
        env = dict(os.environ, VIRTUAL_ENV=str(cfg.project_venv),
                   PATH=os.path.join(str(cfg.project_venv), "bin") + os.pathsep + os.environ.get("PATH", ""))
        try:
            p = subprocess.run([py, full], cwd=str(ws), capture_output=True,
                               text=True, timeout=30, env=env,
                               preexec_fn=safeguard.posix_limits(mem_mb=300, cpu_seconds=25))
            out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
            return {"ok": True, "rc": p.returncode, "out": out[:4000] or "(no output)"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timed out after 30s"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.route("/file/<path:relpath>")
    def serve_file(relpath):
        root = os.path.realpath(str(ws))
        full = os.path.realpath(os.path.join(root, relpath))
        if full != root and not full.startswith(root + os.sep):
            abort(403)
        if not os.path.isfile(full):
            abort(404)
        return send_from_directory(root, relpath)

    return app


def _settings_view(cfg, mem):
    """Current effective settings for the form (DB overrides over config.yaml).
    Keys are never sent to the browser — only whether each is set."""
    s = mem.recall("settings") or {}
    loop_db, llm_db, al_db = s.get("loop") or {}, s.get("llm") or {}, s.get("alerts") or {}
    env_db, pov = s.get("env") or {}, (s.get("llm") or {}).get("providers") or {}

    def keyset(name):
        return bool(name and (env_db.get(name) or os.environ.get(name)))

    sv = {
        "loop": {k: loop_db.get(k, cfg.get("loop", k, default="")) for k in
                 ("interval_seconds", "jitter_seconds", "max_steps", "max_resume_attempts")},
        "min_call": llm_db.get("min_call_interval_seconds", cfg.get("llm", "min_call_interval_seconds", default=3)),
        "prefer": llm_db.get("prefer", cfg.get("llm", "prefer", default="cloud_first")),
        "notify": al_db.get("notify_every_cycle", cfg.get("alerts", "notify_every_cycle", default=False)),
        "ntfy": (al_db.get("ntfy") or {}).get("topic", cfg.get("alerts", "ntfy", "topic", default="")),
        "discord_set": keyset("DISCORD_WEBHOOK_URL"),
        "led_chip": env_db.get("DRONGO_LED_CHIP") or os.environ.get("DRONGO_LED_CHIP") or cfg.get("alerts", "led", "chip", default="/dev/gpiochip0"),
        "led_line": env_db.get("DRONGO_LED_LINE") or os.environ.get("DRONGO_LED_LINE") or "",
        "providers": [],
    }
    pkey = {}
    for p in cfg.get("llm", "providers", default=[]) or []:
        name, o = p.get("name"), pov.get(p.get("name")) or {}
        sv["providers"].append({
            "name": name,
            "enabled": o.get("enabled", p.get("enabled", True)),
            "model": o.get("model") or p.get("model", ""),
            "key_env": p.get("api_key_env"),
            "key_set": keyset(p.get("api_key_env")),
        })
        if p.get("api_key_env"):
            pkey[name] = p["api_key_env"]
    return sv, pkey


def _ls(directory, exts):
    p = Path(directory)
    if not p.exists():
        return []
    files = [f.name for f in p.iterdir() if f.suffix.lower() in exts]
    files.sort(key=lambda n: (p / n).stat().st_mtime, reverse=True)
    return files


def serve(cfg, mem):
    app = create_app(cfg, mem)
    host = cfg.get("web", "host", default="127.0.0.1")
    port = cfg.get("web", "port", default=8080)
    if host not in _PRIVATE and not os.environ.get("DRONGO_WEB_PASSWORD"):
        log.warning("No DRONGO_WEB_PASSWORD set — binding the dashboard to localhost "
                    "only. Set a password to reach it over the LAN (ssh -L 8080:localhost:%s).", port)
        host = "127.0.0.1"
    app.run(host=host, port=port, threaded=True)
