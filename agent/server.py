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
import re
import shutil
import subprocess
import time
from pathlib import Path

from flask import Flask, Response, abort, render_template_string, request, send_from_directory

from . import safeguard, watchdog
from .memory import Memory, utc_iso
from .safeguard import integrity_status
from . import tools
from .tools import system_stats

log = logging.getLogger("agent.server")
_PRIVATE = ("127.0.0.1", "localhost", "::1", "")

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{{ name }} console</title>
<style>
 :root{--bg:#070b10;--card:#0d141d;--card2:#101a25;--mut:#637789;--fg:#d6e3ee;
   --ac:#33e6a4;--ac2:#43b8ff;--ok:#33e6a4;--bad:#ff5d6c;--warn:#f5b53f;
   --ac-rgb:51,230,164;--ac2-rgb:67,184,255;
   --bd:#172231;--bd2:#26384a;
   --mono:ui-monospace,"JetBrains Mono","Cascadia Code","SF Mono",Menlo,Consolas,monospace}
 *{box-sizing:border-box}
 body{margin:0;color:var(--fg);min-height:100vh;font:14.5px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif;
   background:radial-gradient(1100px 560px at 82% -12%,rgba(var(--ac-rgb),.07),transparent 60%),
              radial-gradient(900px 480px at -5% 2%,rgba(var(--ac2-rgb),.06),transparent 55%),var(--bg);
   background-attachment:fixed}
 body::before{content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
   background-image:linear-gradient(rgba(90,130,160,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(90,130,160,.045) 1px,transparent 1px);
   background-size:42px 42px;-webkit-mask-image:radial-gradient(circle at 50% 25%,#000,transparent 92%);mask-image:radial-gradient(circle at 50% 25%,#000,transparent 92%)}
 ::-webkit-scrollbar{width:10px;height:10px} ::-webkit-scrollbar-track{background:transparent}
 ::-webkit-scrollbar-thumb{background:var(--bd2);border-radius:6px} ::-webkit-scrollbar-thumb:hover{background:var(--ac)}
 code{font:12px var(--mono);background:rgba(var(--ac2-rgb),.1);color:var(--ac2);padding:1px 5px;border-radius:4px}
 header{position:sticky;top:0;z-index:5;display:flex;gap:11px;align-items:center;flex-wrap:wrap;
   padding:12px 20px;background:rgba(8,12,18,.72);backdrop-filter:blur(11px);-webkit-backdrop-filter:blur(11px);
   border-bottom:1px solid var(--bd);box-shadow:0 1px 0 rgba(var(--ac-rgb),.18),0 6px 22px rgba(0,0,0,.35)}
 .brand{margin:0;display:flex;align-items:center;gap:9px;font:700 18px/1 var(--mono);letter-spacing:1.5px;color:var(--fg)}
 .brand .logo{width:13px;height:13px;border-radius:3px;background:linear-gradient(135deg,var(--ac),var(--ac2));box-shadow:0 0 13px rgba(var(--ac-rgb),.7)}
 .brand .caret{width:9px;height:16px;background:var(--ac);box-shadow:0 0 9px var(--ac);animation:blink 1.1s steps(1) infinite}
 @keyframes blink{50%{opacity:0}}
 .pill{font:11px/1 var(--mono);letter-spacing:.03em;padding:5px 10px;border-radius:6px;border:1px solid var(--bd2);color:var(--mut);background:rgba(255,255,255,.02);display:inline-flex;align-items:center;gap:6px}
 .pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor;box-shadow:0 0 8px currentColor}
 .pill.ok{color:var(--ok);border-color:rgba(var(--ac-rgb),.35)} .pill.ok::before{animation:pulse 2s ease-in-out infinite}
 .pill.bad{color:var(--bad);border-color:rgba(255,93,108,.4)} .pill.warn{color:var(--warn);border-color:rgba(245,181,63,.4)}
 @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
 nav{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
 nav button{font:600 12px/1 var(--mono);letter-spacing:.06em;text-transform:uppercase;background:rgba(255,255,255,.02);color:var(--mut);border:1px solid var(--bd);border-radius:7px;padding:8px 13px;cursor:pointer;transition:.15s}
 nav button:hover{color:var(--fg);border-color:var(--ac2)}
 nav button.on{background:linear-gradient(180deg,rgba(var(--ac-rgb),.18),rgba(var(--ac-rgb),.05));color:var(--ac);border-color:var(--ac);box-shadow:0 0 14px rgba(var(--ac-rgb),.25),inset 0 0 12px rgba(var(--ac-rgb),.07)}
 main{position:relative;z-index:1;max-width:1120px;margin:0 auto;padding:22px 20px 44px}
 .tab{display:none} .tab.on{display:block;animation:fade .26s ease}
 @keyframes fade{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
 .card{position:relative;background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--bd);border-radius:12px;padding:15px 17px;margin:12px 0;transition:border-color .18s,box-shadow .18s}
 .card:hover{border-color:var(--bd2);box-shadow:0 8px 26px rgba(0,0,0,.4)}
 .card h3{margin:0 0 5px;font:600 15px/1.35 var(--mono);color:var(--fg)} .meta{color:var(--mut);font:12px/1.5 var(--mono)}
 a{color:var(--ac2);text-decoration:none} a:hover{text-decoration:underline;text-shadow:0 0 8px rgba(var(--ac2-rgb),.4)}
 .art{display:inline-block;margin:5px 8px 0 0;font:12.5px var(--mono)}
 h2{font:600 12px/1 var(--mono);color:var(--ac);text-transform:uppercase;letter-spacing:.18em;margin:30px 0 11px;display:flex;align-items:center;gap:9px}
 h2::before{content:"//";color:var(--mut)}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
 .grid img{width:100%;height:150px;object-fit:cover;border-radius:10px;border:1px solid var(--bd);transition:.18s}
 .grid a:hover img{border-color:var(--ac);box-shadow:0 0 16px rgba(var(--ac-rgb),.3)}
 .stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}
 .stat{position:relative;overflow:hidden;background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--bd);border-radius:12px;padding:13px 15px}
 .stat::after{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--ac);opacity:.45}
 .stat .v{font:600 23px/1.2 var(--mono);color:var(--fg);margin-top:3px} .stat .k{color:var(--mut);font:11px var(--mono);text-transform:uppercase;letter-spacing:.1em}
 .bar{height:5px;border-radius:3px;background:#05090d;margin-top:9px;overflow:hidden;border:1px solid var(--bd)}.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--ac2),var(--ac));box-shadow:0 0 10px rgba(var(--ac-rgb),.5)}
 .chip{display:inline-block;font:11px var(--mono);padding:3px 9px;border-radius:20px;border:1px solid var(--bd2);color:var(--mut);margin:0 5px 5px 0;background:rgba(255,255,255,.02)}
 .chip.fix{color:var(--bad);border-color:rgba(255,93,108,.45);background:rgba(255,93,108,.08)}
 .act{display:inline-block;font:600 12px var(--mono);background:rgba(255,255,255,.03);color:var(--fg);border:1px solid var(--bd2);border-radius:8px;padding:6px 12px;cursor:pointer;margin:6px 6px 0 0;transition:.15s;text-decoration:none}
 .act:hover{border-color:var(--ac);color:var(--ac);box-shadow:0 0 12px rgba(var(--ac-rgb),.2);text-decoration:none} .danger:hover{border-color:var(--bad);color:var(--bad);box-shadow:0 0 12px rgba(255,93,108,.2)}
 .big{font-size:13.5px;padding:11px 18px;margin:6px 10px 6px 0;border-radius:9px}
 .runbtn{font:11px var(--mono);padding:2px 8px;margin-left:5px;background:rgba(var(--ac-rgb),.08);color:var(--ok);border:1px solid rgba(var(--ac-rgb),.4);border-radius:6px;cursor:pointer}
 .runbtn:hover{background:rgba(var(--ac-rgb),.18);box-shadow:0 0 10px rgba(var(--ac-rgb),.3)}
 .set h3{margin:18px 0 6px;font:600 13px var(--mono);color:var(--ac2);text-transform:uppercase;letter-spacing:.08em}
 .set label{display:block;margin:8px 0;font:12.5px var(--mono);color:var(--mut)}
 .set input,.set select,.set textarea{display:block;width:100%;max-width:560px;background:#05090d;color:var(--fg);border:1px solid var(--bd2);border-radius:7px;padding:7px 9px;font:13px var(--mono);margin-top:3px;transition:.15s}
 .set input:focus,.set select:focus,.set textarea:focus,#suggbox:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 3px rgba(var(--ac-rgb),.13)}
 .set textarea{line-height:1.5;resize:vertical}
 .set .prow input{max-width:none}
 .set input[type=checkbox]{display:inline-block;width:auto;margin-right:7px;accent-color:var(--ac)}
 .prow{display:flex;gap:8px;align-items:center;margin:7px 0;flex-wrap:wrap}
 .prow input{flex:1;min-width:120px}
 .homewrap{display:flex;gap:18px;align-items:flex-start}
 .homemain{flex:1;min-width:0} .homeside{width:264px;flex:none;position:sticky;top:80px}
 .homeside .row{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid var(--bd);font:12.5px var(--mono)}
 .homeside .row span{color:var(--mut)} .homeside .row b{font-weight:600;color:var(--fg)} .homeside .row:last-child{border:0}
 .usaget{width:100%;border-collapse:collapse;font:11.5px var(--mono)}
 .usaget th{color:var(--mut);text-align:left;font-weight:500;padding-bottom:4px;text-transform:uppercase;letter-spacing:.05em}
 .usaget td{border-top:1px solid var(--bd);padding:4px 0;color:var(--fg)}
 .usaget th:not(:first-child),.usaget td:not(:first-child){text-align:right}
 #suggbox{width:100%;background:#05090d;color:var(--fg);border:1px solid var(--bd2);border-radius:8px;padding:8px 10px;font:13px/1.5 var(--mono);resize:vertical}
 @media (max-width:760px){ .homewrap{flex-direction:column} .homeside{width:100%;position:static} header{padding:10px 14px} main{padding:16px 14px} }
 #toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(8px);background:linear-gradient(180deg,#0e1a16,#0a130f);color:var(--ac);border:1px solid var(--ac);padding:10px 18px;border-radius:9px;font:600 12.5px var(--mono);opacity:0;transition:.22s;pointer-events:none;z-index:30;box-shadow:0 0 22px rgba(var(--ac-rgb),.3)}
 #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
 .modal{position:fixed;inset:0;background:rgba(2,5,9,.72);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;padding:20px;z-index:20}
 .modal.show{display:flex}
 .modal .box{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--bd2);border-radius:14px;max-width:820px;width:100%;max-height:84vh;overflow:auto;padding:18px;box-shadow:0 20px 60px rgba(0,0,0,.6),0 0 0 1px rgba(var(--ac-rgb),.08)}
 .modal .box h3{font:600 15px var(--mono);margin:0 0 8px}
 .modal pre{white-space:pre-wrap;word-break:break-word;font:12.5px/1.55 var(--mono);background:#03060a;padding:12px;border-radius:8px;margin:0;border:1px solid var(--bd);color:#bfe9d6}
 .lightbox{position:fixed;inset:0;z-index:25;background:rgba(2,5,9,.88);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);display:none;flex-direction:column;align-items:center;justify-content:center;gap:14px;padding:24px}
 .lightbox.show{display:flex}
 .lightbox img{max-width:92vw;max-height:76vh;object-fit:contain;border:1px solid var(--bd2);border-radius:10px;box-shadow:0 0 50px rgba(var(--ac-rgb),.18)}
 .lbcap{font:12.5px var(--mono);color:var(--ac);text-shadow:0 0 8px rgba(var(--ac-rgb),.4);max-width:92vw;word-break:break-all;text-align:center}
 .lbbtns{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:center}
 #gallerygrid .grid{margin-top:4px}
 .gal-empty{color:var(--mut);font:12px var(--mono)}
 .swrow{display:flex;align-items:center;gap:13px;margin:12px 0}
 .swrow .lbl{font:13px var(--mono);color:var(--fg)} .swrow .sub{color:var(--mut);font:11.5px var(--mono)}
 .sw{position:relative;display:inline-block;width:44px;height:24px;flex:none}
 .sw input{opacity:0;width:0;height:0;position:absolute}
 .sw .sl{position:absolute;inset:0;background:#05090d;border:1px solid var(--bd2);border-radius:20px;cursor:pointer;transition:.2s}
 .sw .sl::before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;border-radius:50%;background:var(--mut);transition:.2s}
 .sw input:checked + .sl{border-color:var(--ac);box-shadow:0 0 10px rgba(var(--ac-rgb),.25)}
 .sw input:checked + .sl::before{transform:translateX(20px);background:var(--ac);box-shadow:0 0 8px var(--ac)}
 #hwbody .row{display:flex;justify-content:space-between;gap:10px;padding:4px 0;border-bottom:1px solid var(--bd);font:12.5px var(--mono)}
 #hwbody .row span{color:var(--mut);flex:none} #hwbody .row b{color:var(--fg);text-align:right;word-break:break-all}
 #hwbody .hd{color:var(--ac);font:11px var(--mono);text-transform:uppercase;letter-spacing:.1em;margin:10px 0 2px}
 .nowcard{border-color:var(--ac);box-shadow:0 0 22px rgba(var(--ac-rgb),.16),inset 0 0 30px rgba(var(--ac-rgb),.05)}
 .nowlbl{font:11px var(--mono);text-transform:uppercase;letter-spacing:.14em;color:var(--ac)}
 .nowtitle{font:600 18px/1.3 var(--mono);color:var(--fg);margin:3px 0}
 .evlist{display:flex;flex-direction:column}
 .ev{display:flex;align-items:center;gap:9px;padding:6px 0;border-bottom:1px solid var(--bd);font:12.5px var(--mono)}
 .ev:last-child{border:0}
 .evd{width:7px;height:7px;border-radius:50%;flex:none;background:var(--ok);box-shadow:0 0 7px var(--ok)}
 .evd.bad{background:var(--warn);box-shadow:0 0 7px var(--warn)}
 .evt{flex:1;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .ev .meta{flex:none}
 .panel{border:1px solid var(--bd);border-radius:12px;margin:12px 0;background:linear-gradient(180deg,var(--card2),var(--card))}
 .phead{width:100%;text-align:left;background:transparent;border:0;color:var(--fg);font:600 13px var(--mono);text-transform:uppercase;letter-spacing:.08em;padding:14px 16px;cursor:pointer;display:flex;justify-content:space-between;align-items:center}
 .phead:hover{color:var(--ac)} .phead .chev{color:var(--mut);transition:transform .2s}
 .panel.open .phead .chev{transform:rotate(180deg);color:var(--ac)}
 .pbody{display:none;padding:0 16px 16px} .panel.open .pbody{display:block}
 .themes{display:flex;gap:7px;align-items:center}
 .swatch{width:16px;height:16px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0;transition:.15s}
 .swatch:hover{transform:scale(1.15)} .swatch.on{border-color:var(--fg)}
 .sparks{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
 .spark{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--bd);border-radius:12px;padding:11px 13px}
 .spark .k{color:var(--mut);font:11px var(--mono);text-transform:uppercase;letter-spacing:.08em}
 .spark .v{font:600 18px var(--mono);color:var(--fg)} .spark svg{display:block;width:100%;height:34px;margin-top:5px}
 .think{background:#03060a;border:1px solid var(--bd2);border-radius:10px;padding:10px 12px;height:172px;overflow:auto;font:12px/1.55 var(--mono);white-space:pre-wrap;word-break:break-word}
 .think .tl{padding:1px 0} .think .t-think{color:var(--mut)} .think .t-tool{color:var(--ac2)} .think .t-ok{color:var(--ac)} .think .t-warn{color:var(--warn)} .think .t-info{color:var(--fg)}
</style></head><body>
<header>
 <h1 class=brand><span class=logo></span>{{ name }}<span class=caret></span></h1>
 <span id=p_status class="pill">booting</span>
 <span id=p_alive class="pill {{ 'ok' if alive else 'bad' }}">{{ 'alive '+hb if alive else 'no heartbeat' }}</span>
 {% if safe %}<span class="pill warn">SAFE MODE</span>{% endif %}
 <span class="pill {{ 'ok' if integ_ok else 'bad' }}">guard {{ 'ok' if integ_ok else 'CHECK' }}</span>
 <nav>
  <button class="on" data-t=home>Home</button>
  <button data-t=projects>Projects</button>
  <button data-t=gallery>Gallery</button>
  <button data-t=control>Control</button>
 </nav>
 <div class=themes id=themes></div>
</header>
<main>

 <section id=home class="tab on"><div class=homewrap>
  <div class=homemain>
   <div id=workingon>{% if working_on %}<div class="card nowcard"><div class=nowlbl>▶ Working on</div>
     <div class=nowtitle>{{ working_on.title }}</div>
     <span class=meta>{{ working_on.type }} · attempt {{ working_on.attempt }}</span></div>{% endif %}</div>
   <h2>Live thinking</h2>
   <div class=card><div id=think class=think>idle…</div></div>
   <h2>System</h2>
   <div class="stats" id=sysgrid><div class=stat><div class=k>loading…</div></div></div>
   <p class="meta" id=sysmodel></p>

   <h2>Vitals <span class=meta>· recent history</span></h2>
   <div class=sparks id=sparks><p class=meta>collecting…</p></div>

   <h2>Recent activity</h2>
   <div class=card><div id=homelist class=evlist>
   {% for j in journal %}
    <div class=ev><span class="evd{{ '' if j.ok else ' bad' }}"></span><span class=evt>{{ j.title }}</span><span class=meta>{{ j.task_type or j.kind }} · <span class=ts data-ts="{{ j.ts }}">{{ j.when }}</span></span></div>
   {% else %}<p class="meta">Nothing yet — give it a little time.</p>{% endfor %}
   </div></div>
  </div>
  <aside class=homeside>
   <div class=card>
    <h3 style="margin-top:0">LLM usage today</h3>
    <table class=usaget id=usagetbl>
     <tr><th>provider</th><th>today</th><th>total</th><th>cooldown</th></tr>
     {% for u in usage %}<tr><td>{{ u.provider }}</td><td>{{ u.day_count }}</td><td>{{ u.total }}</td><td>{{ u.cool or '—' }}</td></tr>{% else %}<tr><td colspan=4 class=meta>no calls yet</td></tr>{% endfor %}
    </table>
   </div>
  </aside>
 </div></section>

 <section id=projects class="tab">
  <div class="card">
    <h3 style="margin-top:0">💡 Suggest the next project</h3>
    <p class="meta">Tell {{ name }} what to build next. It finishes anything in
      progress first, then takes your suggestion on before inventing its own idea.</p>
    <textarea id=suggbox rows=2 placeholder="e.g. a Pong clone where the paddles speed up with CPU temperature"></textarea>
    <div style="margin-top:8px">
      <button class="act big" onclick="sendSuggest()">Send suggestion</button>
      <span class="meta" id=suggcur>{% if suggestion %}Queued: {{ suggestion }}{% endif %}</span>
    </div>
  </div>
  <h2>Projects it has built — open, run, tag, or flag broken ones for a fix</h2>
  <div id=projlist>
  {% for j in projects %}
   <div class="card" data-id="{{ j.id }}">
     <h3>{{ j.title }} {% if not j.ok %}<span class="pill bad">unfinished</span>{% endif %}</h3>
     <div class="meta"><span class=ts data-ts="{{ j.ts }}">{{ j.when }}</span>{% if j.provider %} · via {{ j.provider }}{% endif %}</div>
     <p>{{ j.body }}</p>
     {% for a in j.arts %}<span style="white-space:nowrap">{% if a.view %}<a class="art" href="#" onclick='viewfile({{ a.path|tojson }});return false'>▸ {{ a.label }}</a>{% else %}<a class="art" href="/file/{{ a.path }}" target=_blank>▸ {{ a.label }}</a>{% endif %}{% if a.path.endswith('.py') and allow_run %}<button class="runbtn" onclick='runpy({{ a.path|tojson }},{{ j.id }})'>▶ run</button>{% endif %}</span> {% endfor %}
     <div class="chips" id="chips-{{ j.id }}" style="margin-top:8px">
       {% for t in j.tags %}<span class="chip {{ 'fix' if t=='needs-fix' else '' }}">{{ t }}</span>{% endfor %}
     </div>
     <button class="act" onclick="rate({{ j.id }},'loved')" title="more like this">⭐</button>
     <button class="act" onclick="rate({{ j.id }},'meh')" title="less like this">👎</button>
     <button class="act danger" onclick="fixit({{ j.id }})">🔧 Fix this</button>
     <button class="act" onclick="addtag({{ j.id }})">+ tag</button>
     <button class="act danger" onclick="delproj({{ j.id }},this)">🗑 Delete</button>
   </div>
  {% else %}<p class="meta">No finished projects yet.</p>{% endfor %}
  </div>
 </section>

 <section id=gallery class="tab">
  <h2>Gallery — images it has generated <span class=meta id=galcount></span></h2>
  <div id=gallerygrid>
   {% if images %}<div class="grid">{% for im in images %}<a href="#" onclick='openLightbox({{ loop.index0 }});return false'><img loading=lazy src="/file/images/{{ im }}" alt="{{ im }}"></a>{% endfor %}</div>
   {% else %}<p class="meta">No images yet — it fills this in as it makes creative_image projects.</p>{% endif %}
  </div>
 </section>

 <section id=control class="tab">
  <div class="panel open" data-panel=ctl>
   <button class=phead onclick="togglePanel(this)">Agent controls<span class=chev>▾</span></button>
   <div class=pbody>
    <button class="act big" onclick="ctl('run')">▶ Run a cycle now</button>
    <button class="act big" onclick="ctl('pause')">⏸ Pause</button>
    <button class="act big" onclick="ctl('resume')">⏵ Resume</button>
    <button class="act big" onclick="ctl('stop')">⏹ Stop (dormant)</button>
    <button class="act big danger" onclick="if(confirm('Restart the agent?'))ctl('restart')">⟳ Restart</button>
    <p class="meta">Pause/Stop just idle it (removable). Restart relaunches via systemd.
      Flagged fixes are worked before new projects.</p>
    <p class="meta" id=fixq></p>
   </div>
  </div>

  <div class="panel open" data-panel=alerts>
   <button class=phead onclick="togglePanel(this)">Discord alerts <span class=chev>▾</span></button>
   <div class=pbody>
    <div class=swrow>
     <label class=sw><input type=checkbox id=al_agent {{ 'checked' if alerts_agent_on }} onchange="toggleAlerts('agent',this.checked)"><span class=sl></span></label>
     <div><div class=lbl>Agent alerts</div><div class=sub>“project complete” + problems (and the LED, if wired). Turn off to stop the per-project spam.</div></div>
    </div>
    <div class=swrow>
     <label class=sw><input type=checkbox id=al_observer {{ 'checked' if alerts_observer_on }} onchange="toggleAlerts('observer',this.checked)"><span class=sl></span></label>
     <div><div class=lbl>Observer alerts</div><div class=sub>crash-loops, rollbacks &amp; host-health from the root watchdog/updater. Best left on.</div></div>
    </div>
    <p class="meta">Both take effect immediately — only notifications are silenced.</p>
   </div>
  </div>

  <div class="panel" data-panel=providers>
   <button class=phead onclick="togglePanel(this)">LLM providers<span class=chev>▾</span></button>
   <div class=pbody>
    {% for p in sv.providers %}
    <div class=swrow>
     <label class=sw><input type=checkbox id="prov_{{ p.name }}" {{ 'checked' if p.name not in providers_off }} onchange="toggleProvider('{{ p.name }}',this.checked)"><span class=sl></span></label>
     <div><div class=lbl>{{ p.name }} <span class=meta>{{ p.model }}</span></div>
       <div class=sub>{% if p.key_env and not p.key_set %}no key set — {% endif %}off = the router skips it until you flip it back on</div></div>
    </div>
    {% endfor %}
    <p class="meta">Use this when a provider is exhausted but still erroring (a free daily quota the bot can't see). Instant, survives restarts; keys and models untouched.</p>
   </div>
  </div>

  <div class="panel" data-panel=hardware>
   <button class=phead onclick="togglePanel(this)">Hardware <span class=chev>▾</span></button>
   <div class=pbody>
    <button class="act big" id=hwbtn onclick="scanHW()">⟳ Scan for hardware</button>
    <span class=meta id=hwts></span>
    <p class="meta">Probes USB, I²C (addresses), SPI, 1-wire, cameras &amp; GPIO. Anything newly attached becomes the agent's next project idea.</p>
    <div id=hwbody class=meta>loading…</div>
   </div>
  </div>

  <div class="panel" data-panel=settings>
   <button class=phead onclick="togglePanel(this)">Settings <span class=chev>▾</span></button>
   <div class="pbody set">
   <p class="meta" style="margin-top:0">Stored on the agent; “Save &amp; Restart” to apply.</p>
   <h3>Personality &amp; interests</h3>
   <label>Persona — how it behaves and talks<textarea id=s_persona rows=5>{{ sv.persona }}</textarea></label>
   <label>Interests — one per line; it picks its projects from these (add / edit / remove freely)<textarea id=s_interests rows=7>{{ sv.interests_text }}</textarea></label>

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
  </div>
 </section>
</main>
<div id=toast></div>
<div class=modal id=modal onclick="if(event.target===this)this.classList.remove('show')">
  <div class=box><h3 id=modaltitle style="margin-top:0"></h3><pre id=modalout></pre>
    <button class="act danger" id=fixbtn style="display:none" onclick="fixFromRun()">🔧 Fix this with the error above</button>
    <button class="act" onclick="document.getElementById('modal').classList.remove('show')">close</button></div>
</div>
<div class=lightbox id=lightbox onclick="if(event.target===this)closeLightbox()">
  <div class=lbcap id=lbcap></div>
  <img id=lbimg alt="">
  <div class=lbbtns>
    <button class="act" onclick="lbNav(-1)">◀ prev</button>
    <span class=meta id=lbpos></span>
    <button class="act" onclick="lbNav(1)">next ▶</button>
    <a class="act" id=lbopen target=_blank href="#">open ↗</a>
    <button class="act" onclick="closeLightbox()">close ✕</button>
  </div>
</div>
<script>
 const $=s=>document.querySelector(s);
 function showTab(t){
   document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x.dataset.t===t));
   document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',x.id===t));
   history.replaceState(null,'','#'+t);
   if(t==='control')loadHW();
 }
 function togglePanel(h){const p=h.closest('.panel');const open=p.classList.toggle('open');
   try{localStorage.setItem('panel:'+p.dataset.panel,open?'1':'0');}catch(e){}}
 function restorePanels(){document.querySelectorAll('.panel').forEach(p=>{
   let v=null;try{v=localStorage.getItem('panel:'+p.dataset.panel);}catch(e){}
   if(v==='1')p.classList.add('open');else if(v==='0')p.classList.remove('open');});}
 const THEMES={
   mint:{'--ac':'#33e6a4','--ac-rgb':'51,230,164','--ac2':'#43b8ff','--ac2-rgb':'67,184,255','--ok':'#33e6a4','--bg':'#070b10','--fg':'#d6e3ee','--mut':'#637789','--card':'#0d141d','--card2':'#101a25','--bd':'#172231','--bd2':'#26384a'},
   amber:{'--ac':'#ffb347','--ac-rgb':'255,179,71','--ac2':'#ff7a45','--ac2-rgb':'255,122,69','--ok':'#ffb347','--bg':'#0c0a06','--fg':'#f0e7d6','--mut':'#8c7c60','--card':'#15110a','--card2':'#1c160d','--bd':'#2a2113','--bd2':'#3b2e1a'},
   synth:{'--ac':'#ff5dd8','--ac-rgb':'255,93,216','--ac2':'#36e3ff','--ac2-rgb':'54,227,255','--ok':'#56e39f','--bg':'#0b0716','--fg':'#ece2ff','--mut':'#7d6ca8','--card':'#140f28','--card2':'#1b1535','--bd':'#241c44','--bd2':'#352a63'},
   ice:{'--ac':'#5bd6ff','--ac-rgb':'91,214,255','--ac2':'#8a7bff','--ac2-rgb':'138,123,255','--ok':'#5bd6ff','--bg':'#060a0f','--fg':'#dcebf5','--mut':'#5f7184','--card':'#0b131c','--card2':'#0f1a26','--bd':'#152232','--bd2':'#223547'}};
 const THEME_ORDER=['mint','amber','synth','ice'];
 function applyTheme(name){const t=THEMES[name]||THEMES.mint;
   for(const k in t)document.documentElement.style.setProperty(k,t[k]);
   try{localStorage.setItem('theme',name);}catch(e){}
   document.querySelectorAll('.swatch').forEach(s=>s.classList.toggle('on',s.dataset.t===name));}
 function initThemes(){const host=$('#themes');if(host){THEME_ORDER.forEach(n=>{
     const b=document.createElement('button');b.className='swatch';b.dataset.t=n;b.title=n;
     b.style.background='linear-gradient(135deg,'+THEMES[n]['--ac']+','+THEMES[n]['--ac2']+')';
     b.onclick=()=>applyTheme(n);host.appendChild(b);});}
   let saved=null;try{saved=localStorage.getItem('theme');}catch(e){}
   applyTheme(saved&&THEMES[saved]?saved:'mint');}
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
 function rate(id,r){fetch('/control/rate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,rating:r})})
   .then(x=>x.json()).then(d=>{if(d.ok){toast(r==='loved'?'⭐ loved — it\\'ll make more like this':'👎 noted — less like this');addchip(id,r);}else toast(d.error||'failed');});}
 function sendSuggest(){const t=gv('suggbox');if(!t){toast('type a suggestion first');return;}
   fetch('/control/suggest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})})
   .then(r=>r.json()).then(d=>{if(d.ok){$('#suggbox').value='';
     $('#suggcur').textContent='Queued: '+t;toast('Suggestion queued for next ✓');}
     else toast(d.error||'failed');});}
 function toggleAlerts(target,on){
   fetch('/control/alerts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target,on})})
   .then(r=>r.json()).then(d=>{toast(d.ok?((target==='agent'?'Agent':'Observer')+' alerts '+(on?'ON ✓':'OFF ✓')):(d.error||'failed'));});}
 function toggleProvider(name,on){
   fetch('/control/provider',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,on})})
   .then(r=>r.json()).then(d=>{toast(d.ok?(name+' '+(on?'ON ✓':'OFF ✓')):(d.error||'failed'));});}
 function hwRow(k,v){const r=document.createElement('div');r.className='row';
   const a=document.createElement('span');a.textContent=k;const b=document.createElement('b');b.textContent=v;
   r.appendChild(a);r.appendChild(b);return r;}
 function hwHead(t){const d=document.createElement('div');d.className='hd';d.textContent=t;return d;}
 function hwRel(ts){const s=Math.max(0,Math.round(Date.now()/1000-ts));
   return s<60?s+'s ago':s<3600?Math.round(s/60)+'m ago':Math.round(s/3600)+'h ago';}
 function renderHW(info){const el=$('#hwbody');if(!el)return;el.replaceChildren();
   if(!info||!info.model){el.textContent='No scan yet — hit “Scan for hardware”.';return;}
   el.appendChild(hwRow('model',info.model||'—'));
   el.appendChild(hwRow('cameras',(info.cameras||[]).join(', ')||'none'));
   el.appendChild(hwRow('SPI',(info.spi||[]).join(', ')||'none'));
   el.appendChild(hwRow('1-wire',(info.onewire||[]).join(', ')||'none'));
   el.appendChild(hwRow('GPIO',(info.gpiochips||[]).join('  ')||'none'));
   el.appendChild(hwRow('thermals',(info.thermals||[]).join('   ')||'none'));
   if((info.i2c||[]).length){el.appendChild(hwHead('I²C buses'));
     info.i2c.forEach(b=>el.appendChild(hwRow(b.bus,(b.addrs||[]).join('  ')||'(no devices)')));}
   if((info.usb||[]).length){el.appendChild(hwHead('USB'));
     info.usb.forEach(u=>el.appendChild(hwRow(u.id,u.name)));}
   const ts=$('#hwts'); if(ts)ts.textContent=info.ts?('· scanned '+hwRel(info.ts)):'';}
 function loadHW(){fetch('/api/hardware').then(r=>r.json()).then(d=>renderHW(d.info)).catch(()=>{});}
 function scanHW(){const b=$('#hwbtn');if(b){b.disabled=true;b.textContent='⟳ scanning…';}
   toast('scanning hardware…');
   fetch('/control/scan',{method:'POST'}).then(r=>r.json()).then(d=>{
     if(d.ok){renderHW(d.info);toast(d.new&&d.new.length?('found '+d.new.length+' new device(s) ✓'):'scan complete ✓');}
     else toast(d.error||'scan failed');})
   .catch(e=>toast('scan failed')).finally(()=>{if(b){b.disabled=false;b.textContent='⟳ Scan for hardware';}});}
 function viewfile(path){$('#modaltitle').textContent='📄 '+path;$('#modalout').textContent='loading…';
   $('#fixbtn').style.display='none';$('#modal').classList.add('show');
   fetch('/file/'+path.split('/').map(encodeURIComponent).join('/'))
   .then(r=>r.ok?r.text():Promise.reject(r.status))
   .then(t=>{$('#modalout').textContent=(t||'(empty file)').slice(0,200000);})
   .catch(e=>{$('#modalout').textContent='ERROR: could not read file ('+e+')';});}
 function delproj(id,btn){if(!confirm('Delete this project and its files? This cannot be undone.'))return;
   fetch('/control/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})
   .then(r=>r.json()).then(d=>{if(d.ok){const c=btn.closest('.card');if(c)c.remove();
     toast('Deleted '+(d.removed&&d.removed.length?d.removed.length+' file(s) ✓':'✓'));}
     else toast(d.error||'failed');});}
 let _runid=null;
 function runpy(path,id){_runid=id; toast('running '+path.split('/').pop()+'…');
   $('#modaltitle').textContent='▶ '+path; $('#modalout').textContent='running…';
   $('#fixbtn').style.display='none'; $('#modal').classList.add('show');
   fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})})
   .then(r=>r.json()).then(d=>{
     const failed=!d.ok||d.rc!==0;
     $('#modalout').textContent=d.ok?('(exit '+d.rc+')\\n'+(d.out||'(no output)')):('ERROR: '+(d.error||'failed'));
     $('#fixbtn').style.display=(failed&&_runid!=null)?'inline-block':'none';});}
 function fixFromRun(){const note='Run failed:\\n'+($('#modalout').textContent||'').slice(0,1400);
   fetch('/control/fix',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:_runid,note})})
   .then(r=>r.json()).then(d=>{toast(d.ok?'Queued for fixing ✓':'failed');$('#modal').classList.remove('show');});}
 const PKEY={{ pkey_json|safe }};
 const ALLOW_RUN={{ allow_run|tojson }};
 let lastSig={{ jsig|tojson }};
 let _images={{ images|tojson }},_lbi=0;
 const imgURL=n=>'/file/images/'+encodeURIComponent(n);
 function renderGallery(images){_images=images||[];
   const wrap=$('#gallerygrid'); const cnt=$('#galcount'); if(cnt)cnt.textContent=_images.length?('· '+_images.length):'';
   if(!wrap)return; wrap.replaceChildren();
   if(!_images.length){const p=document.createElement('p');p.className='gal-empty';p.textContent='No images yet — it fills this in as it makes creative_image projects.';wrap.appendChild(p);return;}
   const grid=document.createElement('div');grid.className='grid';
   _images.forEach((im,i)=>{const a=document.createElement('a');a.href='#';
     a.addEventListener('click',e=>{e.preventDefault();openLightbox(i);});
     const img=document.createElement('img');img.loading='lazy';img.src=imgURL(im);img.alt=im;a.appendChild(img);grid.appendChild(a);});
   wrap.appendChild(grid);}
 function openLightbox(i){if(!_images.length)return;_lbi=(i+_images.length)%_images.length;
   const n=_images[_lbi];$('#lbimg').src=imgURL(n);$('#lbcap').textContent=n;
   $('#lbpos').textContent=(_lbi+1)+' / '+_images.length;$('#lbopen').href=imgURL(n);
   $('#lightbox').classList.add('show');}
 function lbNav(d){openLightbox(_lbi+d);}
 function closeLightbox(){$('#lightbox').classList.remove('show');$('#lbimg').src='';}
 const esc=s=>(s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const fmtLocal=ts=>{if(!ts)return '';try{return new Date(ts*1000).toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});}catch(e){return '';}};
 function localizeTimes(){document.querySelectorAll('.ts[data-ts]').forEach(el=>{const t=fmtLocal(+el.dataset.ts);if(t)el.textContent=t;});}
 const fileURL=p=>'/file/'+p.split('/').map(encodeURIComponent).join('/');
 function mkArtLink(a){const l=document.createElement('a');l.className='art';l.textContent='▸ '+a.label;
   if(a.view){l.href='#';l.addEventListener('click',e=>{e.preventDefault();viewfile(a.path);});}
   else{l.href=fileURL(a.path);l.target='_blank';} return l;}
 function mkHomeCard(j){const r=document.createElement('div');r.className='ev';
   const dot=document.createElement('span');dot.className='evd'+(j.ok?'':' bad');
   const t=document.createElement('span');t.className='evt';t.textContent=j.title||'(untitled)';
   const m=document.createElement('span');m.className='meta';m.textContent=(j.task_type||j.kind||'')+' · '+(fmtLocal(j.ts)||j.when);
   r.appendChild(dot);r.appendChild(t);r.appendChild(m);return r;}
 function mkProjCard(j){const c=document.createElement('div');c.className='card';c.dataset.id=j.id;
   const h=document.createElement('h3');h.textContent=j.title+' ';
   if(!j.ok){const pill=document.createElement('span');pill.className='pill bad';pill.textContent='unfinished';h.appendChild(pill);}
   c.appendChild(h);
   const m=document.createElement('div');m.className='meta';m.textContent=(fmtLocal(j.ts)||j.when)+(j.provider?' · via '+j.provider:'');c.appendChild(m);
   const p=document.createElement('p');p.textContent=j.body||'';c.appendChild(p);
   (j.arts||[]).forEach(a=>{const span=document.createElement('span');span.style.whiteSpace='nowrap';
     span.appendChild(mkArtLink(a));
     if(a.path.endsWith('.py')&&ALLOW_RUN){const b=document.createElement('button');b.className='runbtn';b.textContent='▶ run';
       b.addEventListener('click',()=>runpy(a.path,j.id));span.appendChild(b);}
     c.appendChild(span);c.appendChild(document.createTextNode(' '));});
   const chips=document.createElement('div');chips.className='chips';chips.id='chips-'+j.id;chips.style.marginTop='8px';
   (j.tags||[]).forEach(t=>{const s=document.createElement('span');s.className='chip'+(t==='needs-fix'?' fix':'');s.textContent=t;chips.appendChild(s);});
   c.appendChild(chips);
   const mk=(cls,txt,fn)=>{const b=document.createElement('button');b.className=cls;b.textContent=txt;b.addEventListener('click',fn);return b;};
   c.appendChild(mk('act','⭐',()=>rate(j.id,'loved')));
   c.appendChild(mk('act','👎',()=>rate(j.id,'meh')));
   c.appendChild(mk('act danger','🔧 Fix this',()=>fixit(j.id)));
   c.appendChild(mk('act','+ tag',()=>addtag(j.id)));
   c.appendChild(mk('act danger','🗑 Delete',e=>delproj(j.id,e.currentTarget)));
   return c;}
 function renderHome(journal){
   const list=$('#homelist'); if(!list)return; list.replaceChildren();
   if(journal.length)journal.forEach(j=>list.appendChild(mkHomeCard(j)));
   else{const p=document.createElement('p');p.className='meta';p.textContent='Nothing yet — give it a little time.';list.appendChild(p);}}
 function renderProjects(journal){const list=$('#projlist'); if(!list)return; list.replaceChildren();
   const projs=journal.filter(j=>j.kind==='cycle');
   if(projs.length)projs.forEach(j=>list.appendChild(mkProjCard(j)));
   else{const p=document.createElement('p');p.className='meta';p.textContent='No finished projects yet.';list.appendChild(p);}}
 function renderWorking(w){const el=$('#workingon'); if(!el)return; el.replaceChildren();
   if(w){const c=document.createElement('div');c.className='card nowcard';
     const l=document.createElement('div');l.className='nowlbl';l.textContent='▶ Working on';c.appendChild(l);
     const t=document.createElement('div');t.className='nowtitle';t.textContent=w.title||'(untitled)';c.appendChild(t);
     const m=document.createElement('span');m.className='meta';m.textContent=(w.type||'')+' · attempt '+w.attempt;c.appendChild(m);el.appendChild(c);}}
 function renderUsage(usage){const t=$('#usagetbl'); if(!t)return;
   let h='<tr><th>provider</th><th>today</th><th>total</th><th>cooldown</th></tr>';
   if(usage&&usage.length)usage.forEach(u=>{h+='<tr><td>'+esc(u.provider)+'</td><td>'+(u.day_count||0)+'</td><td>'+(u.total||0)+'</td><td>'+esc(u.cool||'—')+'</td></tr>';});
   else h+='<tr><td colspan=4 class=meta>no calls yet</td></tr>';
   t.innerHTML=h;}
 function spk(arr,max){const w=200,h=34;const vals=arr.filter(v=>v!=null);
   if(vals.length<2)return '<svg viewBox="0 0 200 34" preserveAspectRatio=none></svg>';
   const mx=max||(Math.max.apply(null,vals)*1.15)||1;const n=arr.length;const step=w/(n-1);let d='';let on=false;
   arr.forEach((v,i)=>{if(v==null)return;const x=(i*step).toFixed(1);
     const y=(h-Math.max(0,Math.min(1,v/mx))*h).toFixed(1);d+=(on?'L':'M')+x+' '+y+' ';on=true;});
   return '<svg viewBox="0 0 '+w+' '+h+'" preserveAspectRatio=none><path d="'+d+'" fill=none stroke="var(--ac)" stroke-width=1.5/></svg>';}
 function sparkCard(k,cur,svg){return '<div class=spark><div class=k>'+k+'</div><div class=v>'+cur+'</div>'+svg+'</div>';}
 function renderSparks(v){const el=$('#sparks');if(!el)return;
   if(!v||v.length<2){el.innerHTML='<p class=meta>collecting…</p>';return;}
   const last=v[v.length-1];
   el.innerHTML=sparkCard('CPU',last.cpu!=null?last.cpu+'%':'—',spk(v.map(x=>x.cpu),100))
     +sparkCard('Memory',last.mem!=null?last.mem+'%':'—',spk(v.map(x=>x.mem),100))
     +sparkCard('Temp',last.temp!=null?last.temp+'°C':'—',spk(v.map(x=>x.temp),null));}
 function renderThink(steps){const el=$('#think');if(!el)return;
   const atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<40;
   el.replaceChildren();
   if(!steps||!steps.length){el.textContent='idle — waiting for the next cycle.';return;}
   steps.forEach(s=>{const d=document.createElement('div');d.className='tl t-'+(s.k||'info');
     d.textContent=s.txt||'';el.appendChild(d);});
   if(atBottom)el.scrollTop=el.scrollHeight;}
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
   const interests=(gv('s_interests')||'').split('\\n').map(x=>x.trim()).filter(Boolean);
   const s={loop,llm,alerts:{notify_every_cycle:gc('s_notify'),
     ntfy:{topic:gv('s_ntfy'),enabled:!!gv('s_ntfy')},led:{enabled:!!ll}},env,interests};
   const persona=gv('s_persona'); if(persona)s.identity={persona};
   fetch('/settings',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({settings:s,restart})}).then(r=>r.json())
     .then(d=>toast(d.ok?(restart?'Saved — restarting…':'Saved ✓ (restart to apply)'):('failed: '+(d.error||''))));
 }
 function pct(v){return v==null?'—':v+'%';}
 function refresh(){fetch('/api/system').then(r=>r.json()).then(d=>{
   const s=d.stats||{};
   const status=(d.status||'?')+(d.next_cycle_in!=null&&d.status=='sleeping'?' · next in '+d.next_cycle_in+'s':'');
   $('#p_status').textContent=status;
   const now=new Date();
   const ltime=now.toLocaleTimeString([],{hour12:false});
   const ldate=now.toLocaleDateString([],{weekday:'short',day:'numeric',month:'short',year:'numeric'});
   const cells=[
     ['status',d.status||'?'],['time',ltime],['uptime',s.uptime||'—'],
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
   $('#sysmodel').textContent=(s.model||'')+'  ·  '+ldate;
   $('#fixq').textContent=(d.fix_queue||0)+' project(s) queued for fixing.';
   if(d.usage)renderUsage(d.usage);
   renderWorking(d.working_on);
   renderSparks(d.vitals); renderThink(d.steps);
   const sc=$('#suggcur'); if(sc)sc.textContent=d.suggestion?('Queued: '+d.suggestion):'';
   if(d.journal_sig!==undefined&&d.journal_sig!==lastSig){
     lastSig=d.journal_sig;
     fetch('/api/journal').then(r=>r.json()).then(jd=>{
       renderHome(jd.journal||[]); renderProjects(jd.journal||[]); renderGallery(jd.images||[]);}).catch(()=>{});
   }
 }).catch(()=>{});}
 initThemes(); renderGallery(_images); restorePanels(); localizeTimes(); loadHW(); refresh(); setInterval(refresh,4000);
 document.addEventListener('keydown',e=>{
   if(e.key==='Escape'){closeLightbox();const m=$('#modal');if(m)m.classList.remove('show');}
   else if($('#lightbox').classList.contains('show')){
     if(e.key==='ArrowLeft')lbNav(-1); else if(e.key==='ArrowRight')lbNav(1);}
 });
</script>
</body></html>"""


# Files we show inline in the dashboard modal rather than opening in a new tab.
# (.html is intentionally excluded — those open in a new tab so they render.)
TEXT_EXTS = (".py", ".js", ".sh", ".md", ".txt", ".json", ".css", ".cfg",
             ".ini", ".yaml", ".yml", ".toml", ".c", ".h", ".cpp", ".asm",
             ".z80", ".s", ".log", ".csv")


def _parse_tags(raw):
    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


def _hw_view(info):
    """Tidy the raw collect_hardware() dict into something the dashboard renders."""
    info = info or {}
    i2c = []
    for bus in info.get("i2c_buses", []) or []:
        addrs = sorted(tools._i2c_addresses((info.get("i2c_scan") or {}).get(bus, "")))
        i2c.append({"bus": bus, "addrs": ["0x" + a for a in addrs]})
    usb = []
    for line in (info.get("usb") or "").splitlines():
        m = re.search(r"\bID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})\s*(.*)$", line.strip())
        if m:
            usb.append({"id": m.group(1), "name": m.group(2).strip() or "device"})
    thermals = [f"{t.get('type') or t.get('zone')} {t.get('celsius')}°C"
                for t in info.get("thermals", []) or []]
    gpio = [l for l in (info.get("gpiochips") or "").splitlines() if l.strip()]
    return {"model": info.get("model") or "unknown", "usb": usb,
            "cameras": info.get("video_devices", []) or [], "i2c": i2c,
            "spi": info.get("spi_devices", []) or [], "onewire": info.get("onewire", []) or [],
            "gpiochips": gpio, "thermals": thermals, "ts": info.get("ts")}


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

    def _dedupe_arts(arts):
        # Old journal rows were stored with one entry per write_file call, so a
        # file edited several times shows up repeatedly. Collapse to one per path
        # at render time (keeps the last label) so existing rows display clean too.
        seen = {}
        for a in arts:
            if isinstance(a, dict) and "path" in a:
                a["view"] = str(a["path"]).lower().endswith(TEXT_EXTS)
                seen[a["path"]] = a
        return list(seen.values())

    def _journal(limit):
        rows = []
        for j in mem.recent_journal(limit):
            rows.append({"id": j["id"], "title": j["title"] or "", "kind": j["kind"],
                         "task_type": j["task_type"], "body": j["body"] or "",
                         "provider": j["provider"], "ok": bool(j["ok"]),
                         "when": utc_iso(j["ts"]), "ts": j["ts"],
                         "arts": _dedupe_arts(json.loads(j["artifacts"] or "[]")),
                         "tags": _parse_tags(j["tags"] if "tags" in j.keys() else "")})
        return rows

    def _journal_sig(rows):
        # Cheap fingerprint so the client only re-renders the lists when something
        # actually changed (new project, ok-flip, tag add, delete).
        return ";".join(f"{r['id']}.{int(r['ok'])}.{len(r['tags'])}" for r in rows)

    def _usage_view():
        out = []
        for u in mem.usage_summary():
            cu = u["cooldown_until"]
            cool = f"{int(cu - time.time())}s" if cu and cu > time.time() else ""
            out.append({"provider": u["provider"], "day_count": u["day_count"],
                        "total": u["total"], "cool": cool})
        return out

    def _sample_vitals(stats):
        # Rolling CPU/mem/temp history for the Home sparklines. Appended at most
        # once per 20s (so multiple open tabs don't over-sample); ~30 min kept.
        v = mem.recall("vitals")
        v = v if isinstance(v, list) else []
        now = time.time()
        if not v or now - (v[-1].get("t") or 0) >= 20:
            temps = stats.get("temps") or []
            v.append({"t": round(now), "cpu": stats.get("cpu_pct"),
                      "mem": stats.get("mem_pct"),
                      "temp": (temps[0].get("c") if temps else None)})
            v = v[-90:]
            mem.remember("vitals", v)
        return v

    @app.route("/")
    def index():
        rows = _journal(60)
        age = watchdog.heartbeat_age(cfg)
        integ = integrity_status()
        running_root = getattr(os, "geteuid", lambda: -1)() == 0
        integ_ok = integ["hash_ok"] and (running_root or not integ["writable_by_me"])
        sv, pkey = _settings_view(cfg, mem)
        return render_template_string(
            PAGE, name=name, journal=rows,
            projects=[r for r in rows if r["kind"] == "cycle"],
            images=_ls(cfg.images, (".png", ".jpg", ".jpeg")),
            usage=_usage_view(), allow_run=allow_run,
            sv=sv, pkey_json=json.dumps(pkey),
            alive=age is not None and age < 1800,
            hb=(f"{int(age)}s ago" if age is not None else ""),
            safe=bool(mem.recall("safe_mode")),
            working_on=mem.recall("working_on"),
            suggestion=mem.get_suggestion(),
            jsig=_journal_sig(rows),
            alerts_agent_on=not (ws / "AGENT_ALERTS_OFF").exists(),
            alerts_observer_on=not (ws / "OBSERVER_ALERTS_OFF").exists(),
            providers_off=mem.providers_off(),
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
        stats = system_stats()
        return {
            "stats": stats,
            "status": mem.recall("status") or "starting",
            "working_on": mem.recall("working_on"),
            "heartbeat_age": age,
            "alive": age is not None and age < 1800,
            "next_cycle_in": max(0, int(nxt - time.time())) if nxt else None,
            "safe_mode": bool(mem.recall("safe_mode")),
            "fix_queue": len(mem.fix_queue()),
            "usage": _usage_view(),                 # live so cooldowns tick
            "suggestion": mem.get_suggestion(),
            "journal_sig": _journal_sig(_journal(60)),
            "vitals": _sample_vitals(stats),
            "steps": mem.recall("live_steps") or [],
        }

    @app.route("/api/journal")
    def api_journal():
        # The heavier payload (cards + gallery) — the client only fetches this
        # when journal_sig from /api/system changes, so new projects pop in live.
        return {"journal": _journal(60),
                "images": _ls(cfg.images, (".png", ".jpg", ".jpeg"))}

    @app.route("/api/hardware")
    def api_hardware():
        return {"ok": True, "info": _hw_view(mem.recall("hardware"))}

    @app.route("/control/scan", methods=["POST"])
    def control_scan():
        # Run a full hardware scan on demand (probes i2c etc.), store it, and
        # surface any newly-attached devices to ideation — same path the agent
        # uses on its timer, just forced now.
        try:
            info, new = tools.scan_and_diff_hardware(mem, cfg, force=True)
        except Exception as e:
            return {"ok": False, "error": str(e)}, 500
        log.info("dashboard hardware scan: %d new", len(new))
        return {"ok": True, "new": new, "info": _hw_view(info or {})}

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

    @app.route("/control/rate", methods=["POST"])
    def control_rate():
        # ⭐/👎 feedback → loved/meh tags (mutually exclusive). ideate() reads these
        # to build more of what you like and less of what you don't.
        d = request.get_json(silent=True) or {}
        try:
            jid = int(d.get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad id"}, 400
        r = (d.get("rating") or "").strip()
        if r not in ("loved", "meh", "none"):
            return {"ok": False, "error": "bad rating"}, 400
        mem.tag_entry(jid, "loved", on=(r == "loved"))
        tags = mem.tag_entry(jid, "meh", on=(r == "meh"))
        return {"ok": True, "id": jid, "rating": r, "tags": tags}

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
        mem.add_fix({"id": jid, "title": j["title"], "note": (d.get("note") or "")[:1500],
                     "artifacts": [a["path"] for a in arts]})
        mem.tag_entry(jid, "needs-fix", on=True)
        log.info("flagged for fixing: %s", j["title"])
        return {"ok": True}

    @app.route("/control/delete", methods=["POST"])
    def control_delete():
        d = request.get_json(silent=True) or {}
        try:
            jid = int(d.get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "bad id"}, 400
        match = [j for j in mem.recent_journal(500) if j["id"] == jid]
        if not match:
            return {"ok": False, "error": "entry not found"}, 404
        j = match[0]
        arts = json.loads(j["artifacts"] or "[]")
        root = os.path.realpath(str(ws))
        proj_root = os.path.realpath(str(cfg.projects))
        removed, proj_dirs = [], set()
        for a in arts:
            try:
                full = os.path.realpath(safeguard.safe_join(str(ws), a["path"]))
            except Exception:
                continue
            if full != root and not full.startswith(root + os.sep):
                continue  # never touch anything outside the workspace
            # A file under projects/<name>/... means delete that whole project dir.
            if full.startswith(proj_root + os.sep):
                rel = os.path.relpath(full, proj_root).replace(os.sep, "/")
                top = rel.split("/", 1)[0]
                if top and top not in (".", ".."):
                    proj_dirs.add(os.path.join(proj_root, top))
                    continue
            if os.path.isfile(full):  # loose file (dashboards/, images/, ...)
                try:
                    os.remove(full)
                    removed.append(a["path"])
                except OSError:
                    pass
        for pd in proj_dirs:
            rp = os.path.realpath(pd)
            if rp.startswith(proj_root + os.sep) and os.path.isdir(rp):
                try:
                    shutil.rmtree(rp)
                    removed.append(os.path.relpath(rp, root).replace(os.sep, "/") + "/")
                except OSError:
                    pass
        mem.delete_journal(jid)
        mem.remove_fix(jid)
        log.info("deleted project: %s (%d path(s))", j["title"], len(removed))
        return {"ok": True, "removed": removed}

    @app.route("/control/suggest", methods=["POST"])
    def control_suggest():
        d = request.get_json(silent=True) or {}
        text = (d.get("text") or "").strip()[:500]
        if not text:
            return {"ok": False, "error": "empty suggestion"}, 400
        mem.set_suggestion(text)
        mem.remember("run_now", True)   # wake it so an idle agent picks this up soon
        log.info("human suggestion queued: %s", text[:120])
        return {"ok": True}

    @app.route("/control/alerts", methods=["POST"])
    def control_alerts():
        # Toggle Discord/LED notifications per source via a workspace flag file,
        # which the agent's Alerter and the root observer/updater each check
        # before sending. Instant, no restart, no privilege escalation.
        d = request.get_json(silent=True) or {}
        on = bool(d.get("on"))
        fname = {"agent": "AGENT_ALERTS_OFF",
                 "observer": "OBSERVER_ALERTS_OFF"}.get(d.get("target"))
        if not fname:
            return {"ok": False, "error": "bad target"}, 400
        f = ws / fname
        try:
            if on:
                f.unlink(missing_ok=True)            # alerts ON  = remove the off-flag
            else:
                f.write_text(f"off via dashboard {int(time.time())}\n", encoding="utf-8")
        except Exception as e:
            return {"ok": False, "error": str(e)}, 500
        log.info("alerts toggle: %s -> %s", d.get("target"), "on" if on else "off")
        return {"ok": True, "target": d.get("target"), "on": on}

    @app.route("/control/provider", methods=["POST"])
    def control_provider():
        # Manually enable/disable an LLM provider live — the Router reads this
        # list before every call, so it takes effect with no restart. Keys/models
        # are untouched; the provider is simply skipped while off.
        d = request.get_json(silent=True) or {}
        name = (d.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "no provider name"}, 400
        on = bool(d.get("on"))
        off = mem.set_provider_enabled(name, on)
        log.info("provider toggle: %s -> %s", name, "on" if on else "off")
        return {"ok": True, "name": name, "on": on, "off": off}

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

    @app.route("/data/<path:relpath>")
    def run_data(relpath):
        # Per-request "backend" for the agent's DYNAMIC dashboards: runs a small
        # projects/ .py and returns its stdout verbatim (JSON), so a static HTML+JS
        # page can fetch() live data without anyone running a standalone server.
        # Same safety envelope as /run: projects/ only, .py only, venv, sandboxed,
        # short timeout, unprivileged. GET so client JS can poll it directly.
        if not allow_run:
            abort(403)
        if not relpath.endswith(".py"):
            abort(400)
        try:
            full = safeguard.safe_join(str(ws), relpath)
        except Exception:
            abort(400)
        if not os.path.isfile(full) or "/projects/" not in full.replace(os.sep, "/"):
            abort(404)
        venv_py = os.path.join(str(cfg.project_venv), "bin", "python")
        py = venv_py if os.path.exists(venv_py) else "python3"
        env = dict(os.environ, VIRTUAL_ENV=str(cfg.project_venv),
                   PATH=os.path.join(str(cfg.project_venv), "bin") + os.pathsep + os.environ.get("PATH", ""))
        try:
            p = subprocess.run([py, full], cwd=str(ws), capture_output=True,
                               text=True, timeout=20, env=env,
                               preexec_fn=safeguard.posix_limits(mem_mb=300, cpu_seconds=15))
        except subprocess.TimeoutExpired:
            return Response('{"error":"data script timed out"}', status=504,
                            mimetype="application/json")
        except Exception as e:
            return Response(json.dumps({"error": str(e)}), status=500,
                            mimetype="application/json")
        out = p.stdout or ""
        if p.returncode != 0:
            return Response(json.dumps({"error": "data script exited %d" % p.returncode,
                                        "stderr": (p.stderr or "")[:1000]}),
                            status=502, mimetype="application/json")
        ct = "application/json" if out.lstrip().startswith(("{", "[")) else "text/plain; charset=utf-8"
        return Response(out, mimetype=ct)

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
    ident_db = s.get("identity") or {}
    interests = s.get("interests") if isinstance(s.get("interests"), list) else (cfg.get("interests", default=[]) or [])

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
        "persona": ident_db.get("persona") or cfg.get("identity", "persona", default=""),
        "interests_text": "\n".join(interests),
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
