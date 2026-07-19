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
import struct
import subprocess
import time
import zlib
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
 main{position:relative;z-index:1;max-width:1600px;margin:0 auto;padding:18px 22px 44px}
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
 #projlist{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:14px;align-items:start}
 #projlist .card{display:flex;flex-direction:column;height:300px;overflow:hidden}
 #projlist .card.expanded{height:auto}
 .pcontent{flex:1 1 auto;min-height:0;overflow:auto;margin:2px 0 8px}
 .pactions{flex:none;display:flex;flex-wrap:wrap;align-items:center}
 .pnum{color:var(--ac);font-weight:600}
 .pill-ok{font:10px var(--mono);color:var(--ok);border:1px solid rgba(var(--ac-rgb),.4);border-radius:10px;padding:1px 6px;margin-left:6px}
 .pill-bad{font:10px var(--mono);color:var(--bad);border:1px solid rgba(255,93,108,.45);border-radius:10px;padding:1px 6px;margin-left:6px}
 .fbbar{font:12.5px var(--mono);margin-bottom:10px;color:var(--mut)} .fbbar a{color:var(--ac2)}
 .fbrow{display:flex;justify-content:space-between;gap:10px;padding:5px 2px;border-bottom:1px solid var(--bd);font:13px var(--mono)}
 .fbrow:last-child{border:0} .fbrow a{color:var(--fg);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis} .fbrow a:hover{color:var(--ac)} .fbrow .meta{flex:none}
 .pkgrow{display:flex;align-items:center;gap:9px;padding:6px 0;border-bottom:1px solid var(--bd);font:13px var(--mono)} .pkgrow:last-child{border:0}
 .pkgrow input[type=checkbox]{accent-color:var(--ac)} .pkgname{color:var(--ac);font-weight:600;flex:none} .pkgrow .meta{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
 .dash{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;align-items:stretch}
 .dash>#workingon:empty{display:none}
 .tile{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--bd);border-radius:12px;padding:13px 15px;min-width:0}
 .tile .th{font:600 12px var(--mono);text-transform:uppercase;letter-spacing:.13em;color:var(--ac);margin:0 0 10px;display:flex;gap:9px;align-items:center}
 .tile .th::before{content:"//";color:var(--mut)}
 .c4{grid-column:span 4}.c5{grid-column:span 5}.c6{grid-column:span 6}.c7{grid-column:span 7}.c8{grid-column:span 8}.c12{grid-column:span 12}
 @media (max-width:1000px){ .c4,.c5,.c6,.c7,.c8{grid-column:span 12} }
 .usaget{width:100%;border-collapse:collapse;font:11.5px var(--mono)}
 .usaget th{color:var(--mut);text-align:left;font-weight:500;padding-bottom:4px;text-transform:uppercase;letter-spacing:.05em}
 .usaget td{border-top:1px solid var(--bd);padding:4px 0;color:var(--fg)}
 .usaget th:not(:first-child),.usaget td:not(:first-child){text-align:right}
 #suggbox,#missionbox{width:100%;background:#05090d;color:var(--fg);border:1px solid var(--bd2);border-radius:8px;padding:8px 10px;font:13px/1.5 var(--mono);resize:vertical}
 #missionbox:focus{outline:none;border-color:var(--ac);box-shadow:0 0 0 3px rgba(var(--ac-rgb),.13)}
 @media (max-width:760px){ header{padding:10px 14px} main{padding:16px 14px} }
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
 .help h3{font:600 13px var(--mono);color:var(--ac2);margin:15px 0 4px}
 .help pre{background:#03060a;border:1px solid var(--bd);border-radius:7px;padding:9px 11px;font:12.5px/1.5 var(--mono);color:#bfe9d6;overflow:auto;margin:0 0 4px;white-space:pre-wrap;word-break:break-word}
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
 .panel.open > .phead .chev{transform:rotate(180deg);color:var(--ac)}
 .pbody{display:none;padding:0 16px 16px} .panel.open > .pbody{display:block}
 .panel .panel{background:rgba(255,255,255,.018);border-color:var(--bd);border-radius:9px;margin:8px 0}
 .panel .panel .phead{font-size:12px;padding:11px 13px;letter-spacing:.06em;color:var(--mut)}
 .panel .panel.open > .phead{color:var(--ac)} .panel .panel .pbody{padding:0 13px 12px}
 .themes{display:flex;gap:7px;align-items:center}
 .swatch{width:16px;height:16px;border-radius:50%;border:2px solid transparent;cursor:pointer;padding:0;transition:.15s}
 .swatch:hover{transform:scale(1.15)} .swatch.on{border-color:var(--fg)}
 .think{background:#03060a;border:1px solid var(--bd2);border-radius:10px;padding:10px 12px;height:344px;overflow:auto;font:12px/1.55 var(--mono);white-space:pre-wrap;word-break:break-word}
 .think .tl{padding:1px 0} .think .t-think{color:var(--mut)} .think .t-tool{color:var(--ac2)} .think .t-ok{color:var(--ac)} .think .t-warn{color:var(--warn)} .think .t-info{color:var(--fg)}
 .tokchart{display:flex;flex-direction:column;gap:7px;padding:4px 0}
 .tokrow{display:flex;align-items:center;gap:9px;font:12px var(--mono)}
 .toklab{width:96px;color:var(--mut);text-align:right;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .tokbarwrap{flex:1;display:flex;height:14px;background:#05090d;border:1px solid var(--bd);border-radius:5px;overflow:hidden}
 .tokbarwrap i{display:block;height:100%}
 .tokin{background:linear-gradient(90deg,var(--ac2),rgba(var(--ac2-rgb),.4))}
 .tokout{background:linear-gradient(90deg,rgba(var(--ac-rgb),.5),var(--ac))}
 .tokval{width:60px;color:var(--fg);flex:none}
 .chatlog{max-height:54vh;min-height:200px;overflow:auto;display:flex;flex-direction:column;gap:8px;padding:4px 2px 12px}
 .cmsg{max-width:82%;padding:8px 12px;border-radius:12px;font:13px/1.55 var(--mono);white-space:pre-wrap;word-break:break-word}
 .cmsg.user{align-self:flex-end;background:linear-gradient(180deg,rgba(var(--ac-rgb),.18),rgba(var(--ac-rgb),.06));border:1px solid rgba(var(--ac-rgb),.35);color:var(--fg)}
 .cmsg.bot{align-self:flex-start;background:#05090d;border:1px solid var(--bd2);color:#cfe6db}
 .chatbar{display:flex;gap:8px;align-items:flex-end;margin-top:8px}
 .chatbar textarea{flex:1;background:#05090d;color:var(--fg);border:1px solid var(--bd2);border-radius:9px;padding:9px 11px;font:13px var(--mono);resize:vertical}
 .cmsg .ccode{background:#03060a;border:1px solid var(--bd);border-radius:7px;padding:8px 10px;margin:6px 0;overflow-x:auto;white-space:pre;font:12px/1.5 var(--mono);color:#bfe9d6}
 .cmsg code{font:12px var(--mono);background:rgba(var(--ac2-rgb),.12);color:var(--ac2);padding:1px 5px;border-radius:4px}
 .cfoot{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-top:6px}
 .cbtn{font:10.5px var(--mono);background:transparent;color:var(--mut);border:1px solid var(--bd2);border-radius:5px;padding:2px 7px;cursor:pointer}
 .cbtn:hover{color:var(--ac);border-color:var(--ac)}
 .memrow{display:flex;align-items:center;gap:9px;padding:5px 0;border-bottom:1px solid var(--bd);font:12.5px var(--mono)}
 .memrow:last-child{border:0} .memrow .mk{color:var(--ac2);cursor:pointer;flex:none;min-width:140px} .memrow .mk:hover{color:var(--ac)}
 .memrow .mp{flex:1;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
</style></head><body>
<header>
 <h1 class=brand><span class=logo></span>{{ name }}<span class=caret></span></h1>
 <span id=p_status class="pill">booting</span>
 <span id=p_alive class="pill {{ 'ok' if alive else 'bad' }}">{{ 'alive '+hb if alive else 'no heartbeat' }}</span>
 {% if safe %}<span class="pill warn">SAFE MODE</span>{% endif %}
 <span class="pill {{ 'ok' if integ_ok else 'bad' }}">guard {{ 'ok' if integ_ok else 'CHECK' }}</span>
 <nav>
  <button class="on" data-t=home>Home</button>
  <button data-t=chat>Chat</button>
  <button data-t=projects>Projects</button>
  <button data-t=gallery>Gallery</button>
  <button data-t=files>Files</button>
  <button data-t=brain>Brain</button>
  <button data-t=control>Control</button>
  <button data-t=help>Help</button>
 </nav>
 <div class=themes id=themes></div>
</header>
<main>

 <section id=home class="tab on">
  <div class=dash>
   <div id=workingon class=c12>{% if working_on %}<div class="card nowcard"><div class=nowlbl>▶ Working on</div>
     <div class=nowtitle>{{ working_on.title }}</div>
     <span class=meta>{{ working_on.type }} · attempt {{ working_on.attempt }}</span></div>{% endif %}</div>

   <div class="tile c8"><div class=th>System <span class=meta id=sysmodel></span></div>
     <div class="stats" id=sysgrid><div class=stat><div class=k>loading…</div></div></div></div>

   <div class="tile c4"><div class=th>LLM usage today</div>
     <table class=usaget id=usagetbl>
      <tr><th>provider</th><th>today</th><th>tok</th><th>total</th><th>cd</th></tr>
      {% for u in usage %}<tr><td>{{ u.provider }}</td><td>{{ u.day_count }}</td><td>{{ u.day_tokens }}</td><td>{{ u.total }}</td><td>{{ u.cool or '—' }}</td></tr>{% else %}<tr><td colspan=5 class=meta>no calls yet</td></tr>{% endfor %}
     </table></div>

   <div class="tile c12"><div class=th>Token usage by provider <span class=meta id=toktotal></span><span class=meta style="float:right">▮ in · ▮ out</span></div>
     <div id=tokchart class=tokchart>loading…</div></div>

   <div class="tile c12"><div class=th>Recent activity</div>
     <div id=homelist class=evlist>
     {% for j in journal %}
      <div class=ev><span class="evd{{ '' if j.ok else ' bad' }}"></span><span class=evt>{{ j.title }}</span><span class=meta>{{ j.task_type or j.kind }} · <span class=ts data-ts="{{ j.ts }}">{{ j.when }}</span></span></div>
     {% else %}<p class="meta">Nothing yet — give it a little time.</p>{% endfor %}
     </div></div>
  </div>
 </section>

 <section id=chat class="tab">
  <h2>💬 Chat — ask DRONGO anything, or steer it</h2>
  <div class="card">
   <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
    <span class=meta>answer with:</span>
    <select id=chatprov class=set style="width:auto;max-width:180px;display:inline-block;margin:0"><option value="">auto (router order)</option></select>
    <button class=act onclick="regenChat()" title="re-ask the last question">↻ regenerate</button>
    <button class="act danger" onclick="clearChat()" title="wipe the conversation">🗑 clear</button>
   </div>
   <div id=chatlog class=chatlog>loading…</div>
   <div class=chatbar>
    <textarea id=chatmsg rows=2 placeholder="Ask a question, or tell it what to build next… (Enter to send, Shift+Enter for a newline)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat();}"></textarea>
    <button class="act big" id=chatsend onclick="sendChat()">Send</button>
   </div>
   <p class=meta>Answered here instantly — even while it's building. Tell it "build X next" or "focus on Y" and it queues that for its loop; teach it a fact and it remembers.</p>
  </div>
  <div class="card"><div class=th>🧠 Live thinking — what the agent is doing right now <span class=meta id=lastllm style="float:right"></span></div>
   <div id=think class=think>idle…</div></div>
 </section>

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
  <div class="card">
    <h3 style="margin-top:0">🎯 Standing mission</h3>
    <p class="meta">A persistent theme that biases EVERY new project it dreams up (on top of its
      interests). Unlike a one-off suggestion this sticks until you change it. Blank = no mission.</p>
    <textarea id=missionbox rows=2 placeholder="e.g. focus on retro Amstrad / Z80 projects this month">{{ mission }}</textarea>
    <div style="margin-top:8px"><button class="act big" onclick="saveMission()">Save mission</button></div>
  </div>
  <h2>Projects it has built — open, run, tag, or flag broken ones for a fix</h2>
  <div id=projlist>
  {% for j in projects %}
   <div class="card" data-id="{{ j.id }}">
     <h3><span class=pnum>#{{ j.id }}</span> {{ j.title }} {% if not j.ok %}<span class="pill bad">unfinished</span>{% endif %}</h3>
     <div class="meta"><span class=ts data-ts="{{ j.ts }}">{{ j.when }}</span>{% if j.provider %} · via {{ j.provider }}{% endif %}</div>
     <div class=pcontent>
       <p>{{ j.body }}</p>
       {% for a in j.arts %}<span style="white-space:nowrap">{% if a.view %}<a class="art" href="#" onclick='viewfile({{ a.path|tojson }});return false'>▸ {{ a.label }}</a>{% else %}<a class="art" href="/file/{{ a.path }}" target=_blank>▸ {{ a.label }}</a>{% endif %}{% if a.path.endswith(('.py', '.sh')) and allow_run %}<button class="runbtn" onclick='runpy({{ a.path|tojson }},{{ j.id }})'>▶ run</button>{% endif %}</span> {% endfor %}
       <div class="chips" id="chips-{{ j.id }}" style="margin-top:8px">
         {% for t in j.tags %}<span class="chip {{ 'fix' if t=='needs-fix' else '' }}">{{ t }}</span>{% endfor %}
       </div>
     </div>
     <div class=pactions>
       <button class="act" onclick="rate({{ j.id }},'loved')" title="more like this">⭐</button>
       <button class="act" onclick="rate({{ j.id }},'meh')" title="less like this">👎</button>
       <button class="act danger" onclick="fixit({{ j.id }})">🔧 Fix</button>
       <button class="act" onclick="addtag({{ j.id }})">+ tag</button>
       <button class="act danger" onclick="delproj({{ j.id }},this)">🗑</button>
       <button class="act" onclick="toggleExpand(this)" title="expand / collapse">⤢</button>
     </div>
   </div>
  {% else %}<p class="meta">No finished projects yet.</p>{% endfor %}
  </div>
 </section>

 <section id=files class="tab">
  <h2>Files — browse the agent's workspace</h2>
  <div class="card"><div id=fbwrap class=meta>loading…</div></div>
  <h2>📦 Package requests <span class=meta>— system (apt) packages the agent has asked for</span></h2>
  <div class="card"><div id=pkgwrap class=meta>loading…</div></div>
  <div class="card">
   <div class=th>Install policy <span class=meta>— what the agent may auto-install (a root helper does the install)</span></div>
   <p class=meta><b>manual</b>: only packages/globs you allow below. <b>auto</b>: any valid Debian package it requests. Either way it can only ever <code>apt-get install</code> real package names — never run arbitrary commands.</p>
   <div id=pkgpolwrap class=meta>loading…</div>
  </div>
 </section>

 <section id=gallery class="tab">
  <h2>Gallery — images it has generated <span class=meta id=galcount></span></h2>
  <div id=gallerygrid>
   {% if images %}<div class="grid">{% for im in images %}<a href="#" onclick='openLightbox({{ loop.index0 }});return false'><img loading=lazy src="/img/{{ im }}" alt="{{ im }}"></a>{% endfor %}</div>
   {% else %}<p class="meta">No images yet — it fills this in as it makes creative_image projects.</p>{% endif %}
  </div>
 </section>

 <section id=brain class="tab">
  <h2>🧠 Brain — what DRONGO has learned</h2>
  <div class="card"><div id=kbsummary class=meta>loading…</div></div>
  <div class="card">
   <div class=th>Import a skill</div>
   <p class=meta>Paste a skill as JSON <code>{"name","description","code"}</code> (or a pack <code>{"skills":[…]}</code>), or give a public URL to download from. Imported code is stored, never auto-run.</p>
   <input id=skillurl class=set placeholder="https://…/skills.json (public URL)">
   <button class=act onclick="dlSkill()">⬇ Download from URL</button>
   <textarea id=skilljson class=set placeholder='{"name":"my-skill","description":"what it does + when to reuse","code":"def f(): ..."}' style="min-height:88px"></textarea>
   <button class=act onclick="importSkill()">+ Add skill</button>
  </div>
  <h2>Skills <span class=meta id=skillcount></span></h2>
  <div class="card"><div id=skillwrap class=meta>loading…</div></div>
  <h2>Notes <span class=meta id=notecount></span></h2>
  <div class="card"><div id=notewrap class=meta>loading…</div></div>
  <h2>Lessons learned</h2>
  <div class="card"><div id=lessonwrap class=meta>loading…</div></div>
  <h2>Raw memory <span class=meta id=memcount>— every key in its long-term store</span></h2>
  <div class="card">
   <p class=meta>Everything in the agent's key/value memory (state, settings, learned data). Click a key to inspect the full value; delete what you want it to forget. <b>settings</b> is protected here — edit it via Control → Settings.</p>
   <div id=memwrap class=meta>loading…</div>
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
    <div class=swrow style="margin-top:10px">
     <label class=sw><input type=checkbox id=turbo {{ 'checked' if turbo }} onchange="toggleTurbo(this.checked)"><span class=sl></span></label>
     <div><div class=lbl>⚡ Turbo mode</div><div class=sub>Work back-to-back (~20-40s between projects) instead of the normal gap — keeps it (and the CPU) busy. Cloud limits will fall back to the local model.</div></div>
    </div>
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
     <div><div class=lbl>{{ p.name }} <span class=meta>{{ p.model }}</span>
       {% if p.usable %}<span class=pill-ok>active</span>{% else %}<span class=pill-bad>{{ 'no key — inactive' if p.key_env and not p.key_set else 'inactive' }}</span>{% endif %}</div>
       <div class=sub>{% if p.key_env and not p.key_set %}⚠ the agent will SKIP this until a key is set (add it under Providers &amp; API keys, then Save &amp; Restart). {% endif %}off = router skips it until flipped back on</div></div>
    </div>
    {% endfor %}
    <p class="meta">“active” = the agent actually loads it. A cloud provider with no API key is skipped. Toggle to mute one that's exhausted but still erroring.</p>
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

   <div class="panel" data-panel=set-persona>
    <button class=phead onclick="togglePanel(this)">Personality &amp; interests<span class=chev>▾</span></button>
    <div class=pbody>
     <label>Persona — how it behaves and talks<textarea id=s_persona rows=5>{{ sv.persona }}</textarea></label>
     <label>Interests — one per line; it picks its projects from these<textarea id=s_interests rows=7>{{ sv.interests_text }}</textarea></label>
    </div>
   </div>

   <div class="panel" data-panel=set-loop>
    <button class=phead onclick="togglePanel(this)">Loop &amp; cadence<span class=chev>▾</span></button>
    <div class=pbody>
     <label>Seconds between projects (cycle gap)<input id=s_interval value="{{ sv.loop.interval_seconds }}"></label>
     <label>Jitter (± seconds)<input id=s_jitter value="{{ sv.loop.jitter_seconds }}"></label>
     <label>Tool steps per cycle<input id=s_steps value="{{ sv.loop.max_steps }}"></label>
     <label>Resume attempts before giving up<input id=s_attempts value="{{ sv.loop.max_resume_attempts }}"></label>
     <label>Idea candidates per project (deep-think)<input id=s_ideas value="{{ sv.idea_candidates }}"></label>
     <label>Hardware re-scan interval (seconds)<input id=s_hwscan value="{{ sv.hw_scan }}"></label>
     <label>Cleanup janitor interval (seconds)<input id=s_cleanint value="{{ sv.cleanup_int }}"></label>
     <label>Min seconds between LLM calls (throttle)<input id=s_minc value="{{ sv.min_call }}"></label>
     <label>Provider order<select id=s_prefer>
       <option value=cloud_first {{ 'selected' if sv.prefer=='cloud_first' }}>cloud first</option>
       <option value=local_first {{ 'selected' if sv.prefer=='local_first' }}>local first</option></select></label>
    </div>
   </div>

   <div class="panel" data-panel=set-behaviour>
    <button class=phead onclick="togglePanel(this)">Behaviour<span class=chev>▾</span></button>
    <div class=pbody>
     <label><input type=checkbox id=s_critique {{ 'checked' if sv.self_critique }}> Self-review before finishing a project</label>
     <label><input type=checkbox id=s_git {{ 'checked' if sv.git_history }}> Keep per-project git history</label>
     <label><input type=checkbox id=s_cleanup {{ 'checked' if sv.cleanup_enabled }}> Auto-clean junk &amp; empty folders</label>
    </div>
   </div>

   <div class="panel" data-panel=set-llm>
    <button class=phead onclick="togglePanel(this)">LLM tuning<span class=chev>▾</span></button>
    <div class=pbody>
     <label>Temperature (0–1.5)<input id=s_temp value="{{ sv.temperature }}"></label>
     <label>Max tokens per reply<input id=s_maxtok value="{{ sv.max_tokens }}"></label>
     <label>Cloud request timeout (seconds)<input id=s_timeout value="{{ sv.req_timeout }}"></label>
     <label>Local model timeout (seconds) — raise if local replies time out<input id=s_localtimeout value="{{ sv.local_timeout }}"></label>
    </div>
   </div>

   <div class="panel" data-panel=set-images>
    <button class=phead onclick="togglePanel(this)">Image generation<span class=chev>▾</span></button>
    <div class=pbody>
     <label>Provider<select id=s_imgprov>
       <option value=pollinations {{ 'selected' if sv.img_provider=='pollinations' }}>pollinations (free cloud — fast)</option>
       <option value=local {{ 'selected' if sv.img_provider=='local' }}>local (offline — slow on this box)</option></select></label>
     <label>Local command — {prompt} and {out} are filled in (shell-quoted)<input id=s_imgcmd value="{{ sv.img_cmd }}" placeholder="/opt/imggen/sd --turbo --models-path /opt/imggen/models/&lt;dir&gt; --steps 1 --prompt {prompt} --output {out}"></label>
     <p class="meta">Run <code>sudo /opt/drongo/system/image-gen.sh</code> to build a local generator, then switch the provider to “local”.</p>
    </div>
   </div>

   <div class="panel" data-panel=set-providers>
    <button class=phead onclick="togglePanel(this)">Providers &amp; API keys<span class=chev>▾</span></button>
    <div class=pbody>
    {% for p in sv.providers %}
     <div class=prow data-name="{{ p.name }}">
       <label style="flex:0 0 auto;margin:0"><input type=checkbox id="pe_{{ p.name }}" {{ 'checked' if p.enabled }}> {{ p.name }}{% if p.custom %} <span class=meta>(custom)</span>{% endif %}</label>
       <input id="pm_{{ p.name }}" value="{{ p.model }}" placeholder="model">
       {% if p.key_env %}<input id="pk_{{ p.name }}" type=password autocomplete=off placeholder="{{ 'key set — blank keeps it' if p.key_set else 'paste '+p.key_env }}">{% endif %}
       <button class="act" onclick="moveProvider(this,-1)" title="try earlier">▲</button>
       <button class="act" onclick="moveProvider(this,1)" title="try later">▼</button>
       {% if p.custom %}<button class="act danger" onclick="removeProvider('{{ p.name }}')" title="remove this provider">✕</button>{% endif %}
     </div>
    {% endfor %}
    <h3 style="margin-top:14px">Add a provider</h3>
    <label>Preset<select id=ap_preset onchange="apPreset()">
      <option value=custom>Custom (OpenAI-compatible)</option>
      <option value=ollama>Ollama Cloud (free — gpt-oss, qwen, deepseek)</option>
      <option value=nemotron>Ollama Cloud — Nemotron (NVIDIA, no NVIDIA key)</option>
      <option value=nvidia>NVIDIA NIM (direct — needs NVIDIA key)</option>
      <option value=github>GitHub Models (free w/ PAT)</option>
      <option value=cerebras>Cerebras (free)</option>
      <option value=groq>Groq (free)</option>
      <option value=gemini>Google Gemini (free)</option>
      <option value=mistral>Mistral (free)</option>
      <option value=together>Together AI (free tier)</option>
      <option value=openrouter>OpenRouter (free models)</option></select></label>
    <label>Name<input id=ap_name placeholder="e.g. github"></label>
    <label>Base URL<input id=ap_url placeholder="https://…/v1"></label>
    <label>Model<input id=ap_model placeholder="provider/model-name"></label>
    <label>API-key env var<input id=ap_keyenv placeholder="GITHUB_TOKEN"></label>
    <label>API key (optional — paste to set)<input id=ap_key type=password autocomplete=off></label>
    <button class="act big" onclick="addProvider()">+ Add provider</button>
    <p class="meta">Added providers take effect after a restart. Use the live on/off
      switches above the Settings panel to toggle any provider instantly.</p>
    </div>
   </div>

   <div class="panel" data-panel=set-alerts>
    <button class=phead onclick="togglePanel(this)">Alerts &amp; LED<span class=chev>▾</span></button>
    <div class=pbody>
     <label><input type=checkbox id=s_notify {{ 'checked' if sv.notify }}> Alert on every cycle (not just completions)</label>
     <label>Discord webhook URL<input id=s_discord type=password autocomplete=off placeholder="{{ 'set — blank keeps it' if sv.discord_set else 'paste webhook URL' }}"></label>
     <label>ntfy topic (optional)<input id=s_ntfy value="{{ sv.ntfy }}"></label>
     <label>LED gpiochip<input id=s_ledchip value="{{ sv.led_chip }}"></label>
     <label>LED line offset (blank = LED off)<input id=s_ledline value="{{ sv.led_line }}"></label>
    </div>
   </div>

   <div style="margin-top:12px">
     <button class="act big" onclick="saveSettings(false)">Save</button>
     <button class="act big danger" onclick="saveSettings(true)">Save &amp; Restart</button>
   </div>
   </div>
  </div>
 </section>

 <section id=help class="tab">
  <h2>Cheat sheet — SSH &amp; admin</h2>
  <div class="card help">
   <h3>✏️ Edit your API keys / secrets</h3>
   <pre>sudo nano {{ hp.env }}</pre>
   <p class=meta>API keys, Discord webhook &amp; the dashboard password live here. (Or just use Control → Settings.) After editing, restart.</p>
   <h3>⚙️ Edit config (model, limits, etc.)</h3>
   <pre>sudo nano {{ hp.cfg }}</pre>
   <h3>🔄 Restart after a change</h3>
   <pre>sudo systemctl restart drongo drongo-web</pre>
   <h3>⬆️ Pull &amp; deploy the latest code</h3>
   <pre>cd ~/drongo &amp;&amp; git pull &amp;&amp; sudo ./update.sh</pre>
   <h3>🩺 Health check</h3>
   <pre>sudo drongo doctor</pre>
   <h3>📜 Watch it live</h3>
   <pre>journalctl -u drongo -f</pre>
   <h3>♻️ Wipe all projects (keeps settings)</h3>
   <pre>sudo drongo reset</pre>
   <h3>📦 Install requested packages</h3>
   <pre>sudo bash {{ hp.base }}/pkg-installer.sh</pre>
   <h3>🕹️ Z80 / Amstrad toolchain · 🖼️ local image gen</h3>
   <pre>sudo {{ hp.code }}/system/retro-toolchain.sh
sudo {{ hp.code }}/system/image-gen.sh</pre>
   <h3>📁 Where things live</h3>
   <table class=usaget>
    <tr><th>what</th><th>path</th></tr>
    <tr><td>secrets / API keys</td><td>{{ hp.env }}</td></tr>
    <tr><td>config</td><td>{{ hp.cfg }}</td></tr>
    <tr><td>code (read-only)</td><td>{{ hp.code }}</td></tr>
    <tr><td>workspace (projects, images)</td><td>{{ hp.ws }}</td></tr>
    <tr><td>runtime / state</td><td>{{ hp.base }}</td></tr>
   </table>
   <p class=meta>Pause/Stop/Restart and all settings are also on the Control tab — no SSH needed for most of it.</p>
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
   if(t==='files'){loadFiles('');loadPkgs();loadPolicy();}
   if(t==='brain')loadBrain();
   if(t==='chat'){loadChat();const m=$('#chatmsg');if(m)m.focus();}
   if(t==='home')loadUsageGraph();
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
 function saveMission(){const t=$('#missionbox').value;
   fetch('/control/mission',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})})
   .then(r=>r.json()).then(d=>toast(d.ok?(t.trim()?'Mission set ✓':'Mission cleared ✓'):'failed'));}
 function toggleAlerts(target,on){
   fetch('/control/alerts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target,on})})
   .then(r=>r.json()).then(d=>{toast(d.ok?((target==='agent'?'Agent':'Observer')+' alerts '+(on?'ON ✓':'OFF ✓')):(d.error||'failed'));});}
 function toggleProvider(name,on){
   fetch('/control/provider',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,on})})
   .then(r=>r.json()).then(d=>{toast(d.ok?(name+' '+(on?'ON ✓':'OFF ✓')):(d.error||'failed'));});}
 const AP_PRESETS={
   ollama:{name:'ollama-cloud',url:'https://ollama.com/v1',model:'gpt-oss:120b',keyenv:'OLLAMA_API_KEY'},
   nemotron:{name:'nemotron',url:'https://ollama.com/v1',model:'nemotron',keyenv:'OLLAMA_API_KEY'},
   nvidia:{name:'nvidia',url:'https://integrate.api.nvidia.com/v1',model:'meta/llama-3.1-8b-instruct',keyenv:'NVIDIA_API_KEY'},
   github:{name:'github',url:'https://models.github.ai/inference',model:'openai/gpt-4o-mini',keyenv:'GITHUB_TOKEN'},
   cerebras:{name:'cerebras2',url:'https://api.cerebras.ai/v1',model:'gpt-oss-120b',keyenv:'CEREBRAS_API_KEY'},
   groq:{name:'groq2',url:'https://api.groq.com/openai/v1',model:'llama-3.3-70b-versatile',keyenv:'GROQ_API_KEY'},
   gemini:{name:'gemini2',url:'https://generativelanguage.googleapis.com/v1beta/openai',model:'gemini-flash-latest',keyenv:'GEMINI_API_KEY'},
   pollinations:{name:'pollinations',url:'https://gen.pollinations.ai/v1',model:'openai',keyenv:'POLLINATIONS_API_KEY'},
   mistral:{name:'mistral2',url:'https://api.mistral.ai/v1',model:'mistral-small-latest',keyenv:'MISTRAL_API_KEY'},
   together:{name:'together',url:'https://api.together.xyz/v1',model:'meta-llama/Llama-3.3-70B-Instruct-Turbo-Free',keyenv:'TOGETHER_API_KEY'},
   openrouter:{name:'openrouter2',url:'https://openrouter.ai/api/v1',model:'meta-llama/llama-3.1-8b-instruct:free',keyenv:'OPENROUTER_API_KEY'}};
 function apPreset(){const p=AP_PRESETS[$('#ap_preset').value];if(!p)return;
   $('#ap_name').value=p.name;$('#ap_url').value=p.url;$('#ap_model').value=p.model;$('#ap_keyenv').value=p.keyenv;}
 function addProvider(){const body={name:gv('ap_name'),base_url:gv('ap_url'),model:gv('ap_model'),api_key_env:gv('ap_keyenv'),key:gv('ap_key')};
   if(!body.name||!body.base_url||!body.model){toast('need name, base_url and model');return;}
   fetch('/control/add_provider',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
   .then(r=>r.json()).then(d=>{if(d.ok){toast('added '+d.name+' ✓ — restart to activate');setTimeout(()=>location.reload(),900);}else toast(d.error||'failed');});}
 function removeProvider(name){if(!confirm('Remove provider '+name+'?'))return;
   fetch('/control/remove_provider',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})})
   .then(r=>r.json()).then(d=>{if(d.ok){toast('removed — restart to apply');setTimeout(()=>location.reload(),700);}else toast(d.error||'failed');});}
 function moveProvider(btn,dir){const row=btn.closest('.prow');if(!row)return;
   const sib=dir<0?row.previousElementSibling:row.nextElementSibling;
   if(sib&&sib.classList.contains('prow')){
     if(dir<0)row.parentNode.insertBefore(row,sib);else row.parentNode.insertBefore(sib,row);
     saveProviderOrder();}}
 function saveProviderOrder(){const order=[...document.querySelectorAll('.prow')].map(r=>r.dataset.name);
   fetch('/control/provider_order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order})})
   .then(r=>r.json()).then(d=>toast(d.ok?'order saved — restart to apply':(d.error||'failed')));}
 function toggleTurbo(on){fetch('/control/turbo',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on})})
   .then(r=>r.json()).then(d=>toast(d.ok?('⚡ Turbo '+(on?'ON — going hard':'off')):(d.error||'failed')));}
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
 function fmtSize(n){return n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':(n/1048576).toFixed(1)+' MB';}
 function loadFiles(path){const w=$('#fbwrap');if(!w)return;
   fetch('/api/files?path='+encodeURIComponent(path||'')).then(r=>r.json()).then(d=>{
     if(!d.ok){w.textContent=d.error||'error';return;} renderFiles(d.path||'',d.entries||[]);}).catch(()=>{w.textContent='error';});}
 function fbCrumb(path){const bar=document.createElement('div');bar.className='fbbar';
   const mk=(label,p)=>{const a=document.createElement('a');a.href='#';a.textContent=label;a.onclick=e=>{e.preventDefault();loadFiles(p);};return a;};
   bar.appendChild(mk('workspace',''));let acc='';
   (path?path.split('/'):[]).forEach(part=>{bar.appendChild(document.createTextNode(' / '));acc=acc?acc+'/'+part:part;bar.appendChild(mk(part,acc));});
   return bar;}
 function fbRow(label,onclick,size){const r=document.createElement('div');r.className='fbrow';
   const a=document.createElement('a');a.href='#';a.textContent=label;a.onclick=e=>{e.preventDefault();onclick();};r.appendChild(a);
   if(size!=null){const s=document.createElement('span');s.className='meta';s.textContent=fmtSize(size);r.appendChild(s);}return r;}
 function renderFiles(path,entries){const w=$('#fbwrap');w.replaceChildren();w.appendChild(fbCrumb(path));
   const list=document.createElement('div');
   if(path)list.appendChild(fbRow('📁 ..',()=>loadFiles(path.split('/').slice(0,-1).join('/'))));
   if(!entries.length){const p=document.createElement('div');p.className='meta';p.textContent='(empty)';list.appendChild(p);}
   entries.forEach(e=>{
     const row=document.createElement('div');row.className='fbrow';
     const a=document.createElement('a');a.href='#';a.textContent=(e.dir?'📁 ':(e.img?'🖼 ':'📄 '))+e.name;
     a.onclick=ev=>{ev.preventDefault();e.dir?loadFiles(e.path):openFile(e);};
     row.appendChild(a);
     if(!e.dir){
       if(ALLOW_RUN&&(e.path.endsWith('.py')||e.path.endsWith('.sh'))&&e.path.indexOf('projects/')===0){
         const b=document.createElement('button');b.className='runbtn';b.textContent='▶ run';
         b.onclick=()=>runpy(e.path,null);row.appendChild(b);}
       const s=document.createElement('span');s.className='meta';s.textContent=fmtSize(e.size);row.appendChild(s);}
     list.appendChild(row);});
   w.appendChild(list);}
 function openFile(e){
   if(e.img){_images=[e.path];openLightbox(0);return;}   // view images in the lightbox
   if(e.view){viewfile(e.path);return;}                  // text in the modal
   window.open('/file/'+e.path.split('/').map(encodeURIComponent).join('/'),'_blank');}
 function loadPkgs(){const w=$('#pkgwrap');if(!w)return;
   fetch('/api/pkgs').then(r=>r.json()).then(d=>renderPkgs(d.requests||[],d.installed||[])).catch(()=>{});}
 function renderPkgs(reqs,installed){const w=$('#pkgwrap');w.replaceChildren();
   if(!reqs.length){const p=document.createElement('div');p.className='meta';p.textContent='Nothing requested — the agent calls request_package when it needs an apt package.';w.appendChild(p);}
   else{reqs.forEach(p=>{const r=document.createElement('div');r.className='pkgrow';
       const cb=document.createElement('input');cb.type='checkbox';cb.className='pkgck';cb.value=p.name;
       const nm=document.createElement('span');nm.className='pkgname';nm.textContent=p.name;
       const wy=document.createElement('span');wy.className='meta';wy.textContent=p.reason||'';
       const ins=document.createElement('button');ins.className='act';ins.textContent='installed ✓';ins.onclick=()=>pkgResolve(p.name,'installed');
       const dis=document.createElement('button');dis.className='act danger';dis.textContent='dismiss';dis.onclick=()=>pkgResolve(p.name,'dismiss');
       r.append(cb,nm,wy,ins,dis);w.appendChild(r);});
     const gen=document.createElement('button');gen.className='act big';gen.style.marginTop='10px';
     gen.textContent='⬇ Generate pkg-installer.sh (checked)';gen.onclick=genInstaller;w.appendChild(gen);}
   if(installed.length){const e=document.createElement('div');e.className='meta';e.style.marginTop='10px';
     e.textContent='Installed & available to the agent: '+installed.join(', ');w.appendChild(e);}}
 function pkgResolve(name,action){fetch('/control/pkg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,name})})
   .then(r=>r.json()).then(d=>{toast(d.ok?(action==='installed'?name+' marked installed ✓':name+' dismissed'):'failed');loadPkgs();});}
 function loadPolicy(){const w=$('#pkgpolwrap');if(!w)return;
   fetch('/api/pkg_policy').then(r=>r.json()).then(renderPolicy).catch(()=>{});}
 function renderPolicy(p){const w=$('#pkgpolwrap');if(!w)return;w.replaceChildren();
   const modeRow=document.createElement('div');modeRow.style.marginBottom='10px';
   ['manual','auto'].forEach(m=>{const b=document.createElement('button');b.className='act';
     b.textContent=(p.mode===m?'● ':'○ ')+m;if(p.mode===m)b.style.borderColor='var(--ac)';
     b.onclick=()=>setPolicy({mode:m});modeRow.appendChild(b);});
   w.appendChild(modeRow);
   // Root-owned hard allow-list — read-only here (edit over SSH as root).
   const hard=document.createElement('div');hard.style.marginBottom='10px';
   const hl=document.createElement('div');hl.className='meta';hl.style.marginBottom='4px';
   hl.textContent='🔒 hard-allowed (root — edit '+(p.root_path||'/etc/drongo/pkg-allow.conf')+' over SSH):';
   hard.appendChild(hl);
   if((p.root_allow||[]).length){(p.root_allow).forEach(a=>{const c=document.createElement('span');
     c.className='chip';c.textContent=a;hard.appendChild(c);});}
   else{const s=document.createElement('span');s.className='meta';s.textContent='(none set)';hard.appendChild(s);}
   w.appendChild(hard);
   const dl=document.createElement('div');dl.className='meta';dl.style.marginBottom='4px';
   dl.textContent='dashboard-allowed (editable here):';w.appendChild(dl);
   const chips=document.createElement('div');
   (p.allow||[]).forEach(a=>{const c=document.createElement('span');c.className='chip';c.style.cursor='pointer';
     c.textContent=a+' ✕';c.title='remove';c.onclick=()=>setPolicy({remove:a});chips.appendChild(c);});
   if(!(p.allow||[]).length){const s=document.createElement('span');s.className='meta';
     s.textContent=p.mode==='auto'?'auto mode — every valid package it requests is installed':'nothing allowed yet — add a package/glob below';chips.appendChild(s);}
   w.appendChild(chips);
   const row=document.createElement('div');row.style.marginTop='8px';
   const inp=document.createElement('input');inp.className='set';inp.placeholder='package or glob (e.g. build-essential, libboost-*)';
   inp.style.maxWidth='340px';inp.style.display='inline-block';inp.style.marginRight='8px';
   const add=document.createElement('button');add.className='act';add.textContent='+ allow';
   add.onclick=()=>{const v=inp.value.trim();if(v){setPolicy({add:v});inp.value='';}};
   inp.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();add.onclick();}};
   row.append(inp,add);w.appendChild(row);}
 function setPolicy(body){fetch('/control/pkg_policy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
   .then(r=>r.json()).then(p=>{toast('policy saved');renderPolicy(p);});}
 function genInstaller(){const names=[...document.querySelectorAll('.pkgck:checked')].map(c=>c.value);
   if(!names.length){toast('tick some packages first');return;}
   fetch('/control/pkg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'installer',names})})
   .then(r=>r.json()).then(d=>{if(!d.ok){toast(d.error||'failed');return;}
     $('#modaltitle').textContent='📦 pkg-installer.sh ('+d.count+' package'+(d.count>1?'s':'')+')';
     $('#modalout').textContent='Run this on the Pi:\\n\\n  sudo bash '+d.path+'\\n\\n--- script ---\\n'+d.script;
     $('#fixbtn').style.display='none';$('#modal').classList.add('show');});}
 function loadBrain(){const w=$('#skillwrap');if(!w)return;
   fetch('/api/knowledge').then(r=>r.json()).then(renderBrain).catch(()=>{w.textContent='error';});
   loadMemory();}
 function loadMemory(){const w=$('#memwrap');if(!w)return;
   fetch('/api/memory').then(r=>r.json()).then(d=>renderMemory(d.keys||[])).catch(()=>{w.textContent='error';});}
 function renderMemory(keys){const w=$('#memwrap');if(!w)return;w.replaceChildren();
   $('#memcount').textContent='('+keys.length+' keys)';
   if(!keys.length){w.textContent='(empty)';return;}
   keys.forEach(k=>{const row=document.createElement('div');row.className='memrow';
     const mk=document.createElement('span');mk.className='mk';mk.textContent=k.key;mk.title='view value';
     mk.onclick=()=>viewMemory(k.key);
     const mp=document.createElement('span');mp.className='mp';mp.textContent=k.preview||'';
     const sz=document.createElement('span');sz.className='meta';sz.textContent=fmtSize(k.size||0);
     const del=document.createElement('button');del.className='cbtn';del.textContent='✕';del.title='delete this key';
     if(k.key==='settings'){del.disabled=true;del.title='protected — edit via Control → Settings';}
     else del.onclick=()=>{if(confirm('Forget \"'+k.key+'\"? The agent loses this state/knowledge.'))
       fetch('/control/memory_delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k.key})})
       .then(r=>r.json()).then(d=>{toast(d.ok?'forgotten':'failed: '+(d.error||''));loadMemory();});};
     row.append(mk,mp,sz,del);w.appendChild(row);});}
 function viewMemory(key){$('#modaltitle').textContent='🧠 memory: '+key;$('#modalout').textContent='loading…';
   $('#fixbtn').style.display='none';$('#modal').classList.add('show');
   fetch('/api/memory/'+encodeURIComponent(key)).then(r=>r.json())
   .then(d=>{$('#modalout').textContent=d.ok?d.value:(d.error||'error');}).catch(()=>{$('#modalout').textContent='error';});}
 function renderBrain(d){
   const kb=$('#kbsummary');
   if(kb){kb.replaceChildren();
     const s=document.createElement('span');
     s.textContent='📚 Knowledge base: '+(d.repo_files||0)+' repo files indexed · '+((d.skills||[]).length)+' skills · '+((d.notes||[]).length)+' notes · '+((d.lessons||[]).length)+' lessons · '+(d.dataset_examples||0)+' training examples. ';
     kb.appendChild(s);
     if(d.dataset_examples){const a=document.createElement('a');a.href='/file/dataset/train.jsonl';a.target='_blank';a.className='act';a.textContent='⬇ dataset (JSONL)';kb.appendChild(a);}}
   const sk=$('#skillwrap');sk.replaceChildren();
   const skills=d.skills||[];$('#skillcount').textContent='('+skills.length+')';
   if(!skills.length){const p=document.createElement('div');p.className='meta';p.textContent='No skills yet — DRONGO harvests one from each finished project, or import your own above.';sk.appendChild(p);}
   skills.forEach(s=>{const row=document.createElement('div');row.className='pkgrow';
     const nm=document.createElement('span');nm.className='pkgname';nm.textContent=s.name;
     const wy=document.createElement('span');wy.className='meta';wy.style.flex='1';wy.textContent=s.desc||'';
     const view=document.createElement('button');view.className='act';view.textContent='view';
     view.onclick=()=>{$('#modaltitle').textContent='🧠 '+s.name;$('#modalout').textContent=(s.desc||'')+'\\n\\n'+(s.code||'');$('#fixbtn').style.display='none';$('#modal').classList.add('show');};
     const del=document.createElement('button');del.className='act danger';del.textContent='delete';
     del.onclick=()=>{if(confirm('Delete skill '+s.name+'?'))delSkill(s.name);};
     row.append(nm,wy,view,del);sk.appendChild(row);});
   const nw=$('#notewrap');nw.replaceChildren();const notes=d.notes||[];
   $('#notecount').textContent='('+notes.length+')';
   if(!notes.length){const p=document.createElement('div');p.className='meta';p.textContent='No notes yet — the agent saves research findings with save_note.';nw.appendChild(p);}
   notes.slice().reverse().forEach(n=>{const row=document.createElement('div');row.className='fbrow';
     const a=document.createElement('span');a.textContent=(n.topic||'(note)')+' — '+(n.content||'').slice(0,140);a.style.flex='1';
     row.appendChild(a);nw.appendChild(row);});
   const lw=$('#lessonwrap');lw.replaceChildren();const lessons=d.lessons||[];
   if(!lessons.length){const p=document.createElement('div');p.className='meta';p.textContent='No lessons yet.';lw.appendChild(p);}
   lessons.slice().reverse().forEach(t=>{const row=document.createElement('div');row.className='fbrow';
     const a=document.createElement('span');a.textContent='• '+t;row.appendChild(a);lw.appendChild(row);});}
 function importSkill(){const raw=($('#skilljson').value||'').trim();if(!raw){toast('paste a skill JSON first');return;}
   fetch('/control/skill_import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({json:raw})})
   .then(r=>r.json()).then(d=>{toast(d.ok?('added: '+(d.saved||[]).join(', ')):(d.error||'failed'));if(d.ok){$('#skilljson').value='';loadBrain();}});}
 function dlSkill(){const url=($('#skillurl').value||'').trim();if(!url){toast('enter a URL first');return;}
   toast('downloading…');
   fetch('/control/skill_import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})})
   .then(r=>r.json()).then(d=>{toast(d.ok?('added: '+(d.saved||[]).join(', ')):(d.error||'failed'));if(d.ok){$('#skillurl').value='';loadBrain();}});}
 function delSkill(name){fetch('/control/skill_delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})})
   .then(r=>r.json()).then(d=>{toast(d.ok?'deleted':'failed');loadBrain();});}
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
 const imgURL=n=>'/img/'+String(n).split('/').map(encodeURIComponent).join('/');
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
   const h=document.createElement('h3');
   const num=document.createElement('span');num.className='pnum';num.textContent='#'+j.id;h.appendChild(num);
   h.appendChild(document.createTextNode(' '+j.title+' '));
   if(!j.ok){const pill=document.createElement('span');pill.className='pill bad';pill.textContent='unfinished';h.appendChild(pill);}
   c.appendChild(h);
   const m=document.createElement('div');m.className='meta';m.textContent=(fmtLocal(j.ts)||j.when)+(j.provider?' · via '+j.provider:'');c.appendChild(m);
   const pc=document.createElement('div');pc.className='pcontent';
   const p=document.createElement('p');p.textContent=j.body||'';pc.appendChild(p);
   (j.arts||[]).forEach(a=>{const span=document.createElement('span');span.style.whiteSpace='nowrap';
     span.appendChild(mkArtLink(a));
     if((a.path.endsWith('.py')||a.path.endsWith('.sh'))&&ALLOW_RUN){const b=document.createElement('button');b.className='runbtn';b.textContent='▶ run';
       b.addEventListener('click',()=>runpy(a.path,j.id));span.appendChild(b);}
     pc.appendChild(span);pc.appendChild(document.createTextNode(' '));});
   const chips=document.createElement('div');chips.className='chips';chips.id='chips-'+j.id;chips.style.marginTop='8px';
   (j.tags||[]).forEach(t=>{const s=document.createElement('span');s.className='chip'+(t==='needs-fix'?' fix':'');s.textContent=t;chips.appendChild(s);});
   pc.appendChild(chips); c.appendChild(pc);
   const mk=(cls,txt,fn)=>{const b=document.createElement('button');b.className=cls;b.textContent=txt;b.addEventListener('click',fn);return b;};
   const act=document.createElement('div');act.className='pactions';
   act.appendChild(mk('act','⭐',()=>rate(j.id,'loved')));
   act.appendChild(mk('act','👎',()=>rate(j.id,'meh')));
   act.appendChild(mk('act danger','🔧 Fix',()=>fixit(j.id)));
   act.appendChild(mk('act','+ tag',()=>addtag(j.id)));
   act.appendChild(mk('act danger','🗑',e=>delproj(j.id,e.currentTarget)));
   act.appendChild(mk('act','⤢',e=>toggleExpand(e.currentTarget)));
   c.appendChild(act);
   return c;}
 function toggleExpand(btn){const c=btn.closest('.card');if(c)c.classList.toggle('expanded');}
 function renderHome(journal){
   const list=$('#homelist'); if(!list)return; list.replaceChildren();
   if(journal.length)journal.forEach(j=>list.appendChild(mkHomeCard(j)));
   else{const p=document.createElement('p');p.className='meta';p.textContent='Nothing yet — give it a little time.';list.appendChild(p);}}
 function renderProjects(projs){const list=$('#projlist'); if(!list)return; list.replaceChildren();
   projs=(projs||[]).filter(j=>j.kind==='cycle');
   if(projs.length)projs.forEach(j=>list.appendChild(mkProjCard(j)));
   else{const p=document.createElement('p');p.className='meta';p.textContent='No finished projects yet.';list.appendChild(p);}}
 function renderWorking(w){const el=$('#workingon'); if(!el)return; el.replaceChildren();
   if(w){const c=document.createElement('div');c.className='card nowcard';
     const l=document.createElement('div');l.className='nowlbl';l.textContent='▶ Working on';c.appendChild(l);
     const t=document.createElement('div');t.className='nowtitle';t.textContent=w.title||'(untitled)';c.appendChild(t);
     const m=document.createElement('span');m.className='meta';m.textContent=(w.type||'')+' · attempt '+w.attempt;c.appendChild(m);el.appendChild(c);}}
 function fmtTok(n){n=n||0;return n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':(''+n);}
 function renderUsage(usage){const t=$('#usagetbl'); if(!t)return;
   let h='<tr><th>provider</th><th>today</th><th>tok</th><th>total</th><th>cd</th></tr>';
   if(usage&&usage.length)usage.forEach(u=>{h+='<tr><td>'+esc(u.provider)+'</td><td>'+(u.day_count||0)+'</td><td>'+fmtTok(u.day_tokens)+'</td><td>'+(u.total||0)+'</td><td>'+esc(u.cool||'—')+'</td></tr>';});
   else h+='<tr><td colspan=5 class=meta>no calls yet</td></tr>';
   t.innerHTML=h;}
 function loadUsageGraph(){const w=$('#tokchart');if(!w)return;
   fetch('/api/usage_graph').then(r=>r.json()).then(d=>drawTok(d.totals||[])).catch(()=>{});}
 function drawTok(totals){const w=$('#tokchart');if(!w)return;
   const rows=totals.map(t=>({p:t.provider,inn:t.tokens_in||0,out:t.tokens_out||0,tot:(t.tokens_in||0)+(t.tokens_out||0)})).filter(r=>r.tot>0).sort((a,b)=>b.tot-a.tot);
   const grand=rows.reduce((s,r)=>s+r.tot,0);
   const tt=$('#toktotal');if(tt)tt.textContent=grand?('— '+fmtTok(grand)+' total'):'';
   w.replaceChildren();
   if(!rows.length){w.textContent='no token data yet — providers report usage as they answer.';return;}
   const max=Math.max.apply(null,rows.map(r=>r.tot));
   rows.forEach(r=>{const row=document.createElement('div');row.className='tokrow';
     const lab=document.createElement('span');lab.className='toklab';lab.textContent=r.p;
     const bar=document.createElement('div');bar.className='tokbarwrap';
     const bin=document.createElement('i');bin.className='tokin';bin.style.width=(r.inn/max*100)+'%';bin.title=r.inn+' in';
     const bout=document.createElement('i');bout.className='tokout';bout.style.width=(r.out/max*100)+'%';bout.title=r.out+' out';
     bar.append(bin,bout);
     const val=document.createElement('span');val.className='tokval';val.textContent=fmtTok(r.tot);
     row.append(lab,bar,val);w.appendChild(row);});}
 // Markdown-lite for bot replies. SAFE: escape ALL html first, then re-insert a
 // whitelist of tags (code fences, inline code, bold) — nothing else survives.
 function mdlite(s){let x=esc(s||'');
   x=x.replace(/```([a-z0-9+-]*)\\n?([\\s\\S]*?)```/g,(m,l,c)=>'<pre class=ccode>'+c.replace(/\\n$/,'')+'</pre>');
   x=x.replace(/`([^`\\n]+)`/g,'<code>$1</code>');
   x=x.replace(/\\*\\*([^*\\n]+)\\*\\*/g,'<b>$1</b>');
   return x.replace(/\\n/g,'<br>');}
 function chatCaption(m){const bits=[];
   if(m.provider)bits.push(m.provider);
   if(m.tin||m.tout)bits.push(fmtTok(m.tin)+'→'+fmtTok(m.tout)+' tok');
   return bits.join(' · ');}
 function botBubble(content,cap){const wrap=document.createElement('div');wrap.className='cmsg bot';
   const body=document.createElement('div');body.innerHTML=mdlite(content);wrap.appendChild(body);
   const foot=document.createElement('div');foot.className='cfoot';
   const capEl=document.createElement('span');capEl.className='meta';capEl.textContent=cap||'';foot.appendChild(capEl);
   const cp=document.createElement('button');cp.className='cbtn';cp.textContent='⧉ copy';cp.title='copy reply';
   cp.onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText(content).then(()=>toast('copied'));};
   foot.appendChild(cp);wrap.appendChild(foot);return wrap;}
 let _lastUserMsg='';
 function loadChat(){const w=$('#chatlog');if(!w)return;
   fetch('/api/chat').then(r=>r.json()).then(d=>{renderChat(d.history||[]);
     const sel=$('#chatprov');if(sel&&d.providers){const cur=sel.value;sel.replaceChildren();
       const auto=document.createElement('option');auto.value='';auto.textContent='auto (router order)';sel.appendChild(auto);
       d.providers.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;sel.appendChild(o);});
       sel.value=cur&&d.providers.includes(cur)?cur:'';}
   }).catch(()=>{w.textContent='error';});}
 function renderChat(h){const w=$('#chatlog');if(!w)return;w.replaceChildren();
   if(!h.length){const p=document.createElement('div');p.className='meta';p.textContent='Say hi 👋 — ask it what it is doing, or tell it what to build next.';w.appendChild(p);}
   h.forEach(m=>{if(m.role==='user'){_lastUserMsg=m.content;
       const b=document.createElement('div');b.className='cmsg user';b.textContent=m.content;w.appendChild(b);}
     else w.appendChild(botBubble(m.content,chatCaption(m)));});
   w.scrollTop=w.scrollHeight;}
 function sendChat(msgOverride){const t=$('#chatmsg');const msg=(msgOverride||t.value||'').trim();if(!msg)return;
   _lastUserMsg=msg;
   const w=$('#chatlog');const ub=document.createElement('div');ub.className='cmsg user';ub.textContent=msg;w.appendChild(ub);
   const pend=document.createElement('div');pend.className='cmsg bot';pend.textContent='…';w.appendChild(pend);w.scrollTop=w.scrollHeight;
   if(!msgOverride)t.value='';const btn=$('#chatsend');btn.disabled=true;
   const prov=($('#chatprov')||{}).value||'';
   fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg,provider:prov})})
   .then(r=>r.json()).then(d=>{
     w.replaceChild(botBubble(d.reply||d.error||'(no reply)',chatCaption(d)),pend);
     if(d.applied&&d.applied.length){const n=document.createElement('div');n.className='meta';n.style.alignSelf='flex-start';n.textContent='✓ '+d.applied.join('; ');w.appendChild(n);}
     w.scrollTop=w.scrollHeight;}).catch(()=>{pend.textContent='(error reaching the agent)';}).finally(()=>{btn.disabled=false;});}
 function regenChat(){if(!_lastUserMsg){toast('nothing to regenerate');return;}sendChat(_lastUserMsg);}
 function clearChat(){if(!confirm('Wipe the whole conversation?'))return;
   fetch('/control/chat_clear',{method:'POST'}).then(r=>r.json()).then(()=>{toast('chat cleared');loadChat();});}
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
 const gf=id=>{const v=parseFloat(gv(id));return isNaN(v)?null:v;};
 function saveSettings(restart){
   const provs={},env={};
   document.querySelectorAll('.prow').forEach(r=>{const n=r.dataset.name;
     provs[n]={enabled:gc('pe_'+n)}; const m=gv('pm_'+n); if(m)provs[n].model=m;
     const k=gv('pk_'+n); if(k&&PKEY[n])env[PKEY[n]]=k;});
   const dw=gv('s_discord'); if(dw)env.DISCORD_WEBHOOK_URL=dw;
   const ll=gv('s_ledline'); env.DRONGO_LED_CHIP=gv('s_ledchip'); if(ll)env.DRONGO_LED_LINE=ll;
   const loop={};[['interval_seconds','s_interval'],['jitter_seconds','s_jitter'],
     ['max_steps','s_steps'],['max_resume_attempts','s_attempts'],['idea_candidates','s_ideas'],
     ['hw_scan_interval_seconds','s_hwscan'],['cleanup_interval_seconds','s_cleanint']].forEach(([k,id])=>{
     const v=gn(id); if(v!=null)loop[k]=v;});
   loop.self_critique=gc('s_critique'); loop.git_history=gc('s_git'); loop.cleanup_enabled=gc('s_cleanup');
   const llm={prefer:gv('s_prefer'),providers:provs}; const mc=gn('s_minc'); if(mc!=null)llm.min_call_interval_seconds=mc;
   const tp=gf('s_temp'); if(tp!=null)llm.temperature=tp;
   const mt=gn('s_maxtok'); if(mt!=null)llm.max_tokens=mt;
   const rt=gn('s_timeout'); if(rt!=null)llm.request_timeout=rt;
   const lt=gn('s_localtimeout'); if(lt!=null)llm.local_timeout=lt;
   const interests=(gv('s_interests')||'').split('\\n').map(x=>x.trim()).filter(Boolean);
   const images={provider:gv('s_imgprov')}; const ic=gv('s_imgcmd'); if(ic)images.local_cmd=ic;
   const s={loop,llm,images,alerts:{notify_every_cycle:gc('s_notify'),
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
     ['projects',d.projects||0],
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
   const ll=$('#lastllm'); if(ll)ll.textContent=d.last_llm?('last: '+d.last_llm.provider+' '+fmtTok(d.last_llm.in)+'→'+fmtTok(d.last_llm.out)+' tok'):'';
   renderWorking(d.working_on);
   renderThink(d.steps);
   const sc=$('#suggcur'); if(sc)sc.textContent=d.suggestion?('Queued: '+d.suggestion):'';
   if(d.journal_sig!==undefined&&d.journal_sig!==lastSig){
     lastSig=d.journal_sig;
     fetch('/api/journal').then(r=>r.json()).then(jd=>{
       renderHome(jd.journal||[]); renderProjects(jd.projects||[]); renderGallery(jd.images||[]);}).catch(()=>{});
   }
 }).catch(()=>{});}
 initThemes(); renderGallery(_images); restorePanels(); localizeTimes(); loadHW(); refresh(); setInterval(refresh,4000);
 loadUsageGraph(); setInterval(loadUsageGraph,15000);
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
        devs = (info.get("i2c_devices") or {}).get(bus)
        if devs is None:   # legacy blob from before the sysfs-only scan
            devs = ["0x" + a for a in sorted(tools._i2c_addresses(
                (info.get("i2c_scan") or {}).get(bus, "")))]
        i2c.append({"bus": bus, "addrs": devs})
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

    # A router the DASHBOARD uses to CHAT with the human — it lives in the web
    # process, separate from the agent loop, so chat answers instantly even while
    # the agent is mid-project. Best-effort: a provider/config issue here must
    # never break the (read-only) dashboard views.
    try:
        from .llm import Router
        chat_router = Router(cfg, mem)
    except Exception as e:
        chat_router = None
        log.warning("chat router unavailable: %s", e)

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

    @app.after_request
    def _sec_headers(resp):
        # Cheap defence-in-depth: no MIME sniffing, no clickjacking, no referrer
        # leakage. (setdefault so a route can still override if it ever needs to.)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

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

    def _journal(limit, kind=None):
        rows = []
        for j in mem.recent_journal(limit, kind=kind):
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
        used = {u["provider"]: u for u in mem.usage_summary()}
        # List ALL configured providers (built-in + dashboard-added) so a brand-new
        # one shows here with 0s, not just providers that have been called.
        names = [p.get("name") for p in (cfg.get("llm", "providers", default=[]) or []) if p.get("name")]
        for c in ((mem.recall("settings") or {}).get("llm") or {}).get("custom_providers") or []:
            if c.get("name") and c["name"] not in names:
                names.append(c["name"])
        for n in used:                              # plus any used-but-now-unconfigured ones
            if n not in names:
                names.append(n)
        now, out = time.time(), []
        for n in names:
            u = used.get(n, {})
            cu = u.get("cooldown_until")
            cool = f"{int(cu - now)}s" if cu and cu > now else ""
            out.append({"provider": n, "day_count": u.get("day_count", 0),
                        "total": u.get("total", 0), "cool": cool,
                        "tokens_in": u.get("tokens_in", 0), "tokens_out": u.get("tokens_out", 0),
                        "day_tokens": u.get("day_tokens", 0)})
        return out

    @app.route("/")
    def index():
        rows = _journal(60)
        age = watchdog.heartbeat_age(cfg)
        integ = integrity_status()
        running_root = getattr(os, "geteuid", lambda: -1)() == 0
        integ_ok = integ["hash_ok"] and (running_root or not integ["writable_by_me"])
        sv, pkey = _settings_view(cfg, mem)
        cfgp = cfg.source_path or "/etc/drongo/config.yaml"
        hp = {"cfg": cfgp, "env": os.path.join(os.path.dirname(cfgp), "drongo.env"),
              "code": "/opt/drongo", "ws": str(cfg.workspace), "base": str(cfg.base_dir)}
        return render_template_string(
            PAGE, name=name, journal=rows,
            projects=_journal(500, kind="cycle"),     # ALL projects, not just recent journal
            images=_gallery_images(cfg),
            usage=_usage_view(), allow_run=allow_run,
            sv=sv, pkey_json=json.dumps(pkey), hp=hp,
            alive=age is not None and age < 1800,
            hb=(f"{int(age)}s ago" if age is not None else ""),
            safe=bool(mem.recall("safe_mode")),
            working_on=mem.recall("working_on"),
            suggestion=mem.get_suggestion(),
            mission=mem.get_mission(),
            turbo=bool(mem.recall("turbo")),
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
        # Preserve settings the form doesn't carry, so a Save & Restart can't wipe
        # dashboard-added providers or the chosen order.
        prev_llm = cur.get("llm") or {}
        s.setdefault("llm", {})
        for keep in ("custom_providers", "order"):
            if keep in prev_llm and keep not in s["llm"]:
                s["llm"][keep] = prev_llm[keep]
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
            "projects": mem.count_projects(),
            "usage": _usage_view(),                 # live so cooldowns tick
            "last_llm": mem.recall("last_llm"),     # provider + tokens of the latest call
            "suggestion": mem.get_suggestion(),
            "journal_sig": _journal_sig(_journal(60)),
            "steps": mem.recall("live_steps") or [],
        }

    @app.route("/api/journal")
    def api_journal():
        # The heavier payload (cards + gallery) — the client only fetches this
        # when journal_sig from /api/system changes, so new projects pop in live.
        return {"journal": _journal(60), "projects": _journal(500, kind="cycle"),
                "images": _gallery_images(cfg)}

    @app.route("/api/chat")
    def api_chat():
        provs = chat_router.provider_names() if chat_router else []
        return {"ok": True, "history": mem.chat_history(40), "providers": provs}

    @app.route("/control/chat_clear", methods=["POST"])
    def control_chat_clear():
        mem.remember("chat", [])
        return {"ok": True}

    @app.route("/api/memory")
    def api_memory():
        out = []
        for e in mem.all_kv():
            val = e.get("value")
            try:
                preview = val if isinstance(val, str) else json.dumps(val)
            except Exception:
                preview = str(val)
            out.append({"key": e["key"], "preview": (preview or "")[:160],
                        "size": len(preview or ""), "ts": e.get("ts")})
        return {"ok": True, "keys": out}

    @app.route("/api/memory/<path:key>")
    def api_memory_key(key):
        val = mem.recall(key)
        if val is None:
            return {"ok": False, "error": "no such key"}, 404
        # Don't hand secrets to the browser: settings.env holds API keys.
        if key == "settings" and isinstance(val, dict):
            val = dict(val)
            val["env"] = {k: "•••" for k in (val.get("env") or {})}
        try:
            body = json.dumps(val, indent=2, default=str, ensure_ascii=False)
        except Exception:
            body = str(val)
        return {"ok": True, "key": key, "value": body[:20000]}

    @app.route("/control/memory_delete", methods=["POST"])
    def control_memory_delete():
        key = ((request.get_json(silent=True) or {}).get("key") or "").strip()
        if not key:
            return {"ok": False, "error": "no key"}, 400
        if key == "settings":
            return {"ok": False, "error": "settings is protected — change it in Control → Settings"}, 400
        mem.forget(key)
        log.info("memory key '%s' deleted via dashboard", key)
        return {"ok": True}

    @app.route("/chat", methods=["POST"])
    def chat():
        # Talk to DRONGO and STEER it — answered here in the web process, so it
        # works even while the agent loop is mid-project. The model may also set a
        # next project / mission / a learned note, which the loop then picks up.
        d = request.get_json(silent=True) or {}
        msg = (d.get("message") or "").strip()[:2000]
        pin = (d.get("provider") or "").strip() or None      # picker: force one provider
        if not msg:
            return {"ok": False, "error": "empty message"}, 400
        prior = mem.chat_history(12)                          # BEFORE adding this msg
        mem.add_chat("user", msg)
        if chat_router is None or not chat_router.provider_names():
            reply = "I've no LLM provider configured yet — add a key in Control → Settings."
            mem.add_chat("assistant", reply)
            return {"ok": True, "reply": reply, "applied": []}
        wo = mem.recall("working_on")
        recent = "; ".join(j["title"] for j in mem.recent_journal(8, kind="cycle"))
        kb = mem.relevant_knowledge(msg, k=4)
        persona = cfg.get("identity", "persona", default="You are DRONGO, an autonomous maker-agent.")
        system = (persona + "\n\nYou are chatting with your human in your dashboard. Answer "
                  "helpfully and briefly. You can be STEERED: if they ask you to build or "
                  "prioritise something next, put it in next_project; if they set a standing "
                  "direction/preference, put it in mission; if they teach you a durable fact "
                  "worth remembering, put it in learned. Reply with ONE JSON object only:\n"
                  '{"reply":"<your message>","next_project":"<optional>","mission":"<optional>",'
                  '"learned":"<optional>"}')
        lines = []
        if isinstance(wo, dict) and wo.get("title"):
            lines.append(f"(You are currently working on: {wo.get('title')} — attempt {wo.get('attempt')}.)")
        if recent:
            lines.append(f"(Recently built: {recent}.)")
        if kb:
            lines.append("(Relevant from your knowledge base:\n"
                         + "\n".join(f"- [{x['kind']}] {x['title']}: {x['text']}" for x in kb) + ")")
        user = ("\n".join(lines) + "\n\n" if lines else "") + "Human: " + msg
        # PROPER multi-turn: replay the recent conversation so it remembers what
        # you talked about, not just the latest message.
        messages = [{"role": "system", "content": system}]
        for m in prior:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                messages.append({"role": m["role"], "content": m["content"][:1500]})
        messages.append({"role": "user", "content": user})
        try:
            from .loop import extract_json
            text, prov = chat_router.chat(messages, temperature=0.5, max_tokens=700, only=pin)
            obj = extract_json(text) or {}
        except Exception as e:
            reply = f"(couldn't reach a model right now: {e})"
            mem.add_chat("assistant", reply)
            return {"ok": True, "reply": reply, "applied": []}
        usage = getattr(chat_router, "last_usage", None) or {}
        tin, tout = usage.get("in", 0), usage.get("out", 0)
        reply = (obj.get("reply") or text or "").strip() or "(no reply)"
        applied = []
        if obj.get("next_project"):
            mem.set_suggestion(str(obj["next_project"])[:400]); applied.append("queued that as my next project")
        if obj.get("mission"):
            mem.set_mission(str(obj["mission"])[:400]); applied.append("updated my standing mission")
        if obj.get("learned"):
            mem.add_note("from chat", str(obj["learned"])[:1000]); applied.append("saved that to memory")
        mem.add_chat("assistant", reply + (("\n\n_(" + "; ".join(applied) + ")_") if applied else ""),
                     provider=prov, tin=tin, tout=tout)
        return {"ok": True, "reply": reply, "applied": applied,
                "provider": prov, "tin": tin, "tout": tout}

    @app.route("/api/usage_graph")
    def api_usage_graph():
        totals = []
        for u in mem.usage_summary():
            totals.append({"provider": u["provider"], "calls": u.get("total", 0),
                           "tokens_in": u.get("tokens_in", 0), "tokens_out": u.get("tokens_out", 0)})
        return {"ok": True, "totals": totals, "daily": mem.usage_daily_series(14)}

    @app.route("/api/hardware")
    def api_hardware():
        return {"ok": True, "info": _hw_view(mem.recall("hardware"))}

    @app.route("/api/files")
    def api_files():
        rel = (request.args.get("path") or "").strip().lstrip("/")
        root = os.path.realpath(str(ws))
        try:
            full = os.path.realpath(safeguard.safe_join(str(ws), rel)) if rel else root
        except Exception:
            return {"ok": False, "error": "bad path"}, 400
        if full != root and not full.startswith(root + os.sep):
            return {"ok": False, "error": "outside workspace"}, 403
        if not os.path.isdir(full):
            return {"ok": False, "error": "not a folder"}, 404
        img_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
                    ".ppm", ".pgm", ".pbm")
        entries = []
        for name in sorted(os.listdir(full)):
            p = os.path.join(full, name)
            isd = os.path.isdir(p)
            low = name.lower()
            entries.append({"name": name,
                            "path": os.path.relpath(p, root).replace(os.sep, "/"),
                            "dir": isd,
                            "size": 0 if isd else (os.path.getsize(p) if os.path.isfile(p) else 0),
                            "view": (not isd) and low.endswith(TEXT_EXTS),
                            "img": (not isd) and low.endswith(img_exts)})
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return {"ok": True, "path": rel, "entries": entries}

    @app.route("/api/pkgs")
    def api_pkgs():
        return {"ok": True, "requests": mem.pkg_requests(), "installed": mem.installed_extras()}

    @app.route("/api/knowledge")
    def api_knowledge():
        ds = cfg.workspace / "dataset" / "train.jsonl"
        n_ds = 0
        try:
            if ds.exists():
                with open(ds, "r", encoding="utf-8", errors="replace") as fh:
                    n_ds = sum(1 for _ in fh)
        except Exception:
            pass
        return {"ok": True, "skills": mem.skills(), "notes": mem.notes(),
                "lessons": mem.recent_lessons(limit=25),
                "repo_files": len(mem.recall("repo_index") or []),
                "dataset_examples": n_ds}

    @app.route("/control/skill_import", methods=["POST"])
    def control_skill_import():
        # Import skill(s) either from pasted JSON or by downloading from a PUBLIC
        # URL (SSRF-guarded). Code is STORED as a skill, never executed here — the
        # agent must choose to recall + run it, which still goes through the sandbox.
        d = request.get_json(silent=True) or {}
        url = (d.get("url") or "").strip()
        if url:
            body, err = tools.fetch_public_text(cfg, url)
            if err:
                return {"ok": False, "error": f"download rejected: {err}"}, 400
            payload = body
        else:
            payload = d.get("json")
            if not payload:
                return {"ok": False, "error": "paste skill JSON or give a URL"}, 400
        saved, why = tools.import_skills(mem, payload)
        if not saved:
            return {"ok": False, "error": why}, 400
        log.info("imported %d skill(s) via dashboard: %s", len(saved), ", ".join(saved))
        return {"ok": True, "saved": saved}

    @app.route("/control/skill_delete", methods=["POST"])
    def control_skill_delete():
        name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
        return {"ok": mem.delete_skill(name)}

    def _root_allow():
        """The root-owned hard allow-list (read-only here — /etc/drongo is
        root-owned, so the drongo dashboard can display it but not edit it)."""
        etc = os.path.dirname(cfg.source_path or "/etc/drongo/config.yaml")
        path = os.path.join(etc, "pkg-allow.conf")
        pats = []
        try:
            with open(path, encoding="utf-8") as fh:
                for ln in fh:
                    ln = ln.split("#", 1)[0].strip()
                    if ln:
                        pats.append(ln)
        except Exception:
            pass
        return pats, path

    @app.route("/api/pkg_policy")
    def api_pkg_policy():
        root_allow, path = _root_allow()
        return {"ok": True, **mem.pkg_policy(), "root_allow": root_allow, "root_path": path}

    @app.route("/control/pkg_policy", methods=["POST"])
    def control_pkg_policy():
        # Governs the root pkg-installer: which requested apt packages it may
        # install. manual+allowlist (default) or auto. Stored in the DB.
        d = request.get_json(silent=True) or {}
        pol = mem.pkg_policy()
        allow = list(pol["allow"])
        if d.get("add"):
            allow.append(str(d["add"]))
        if d.get("remove"):
            allow = [a for a in allow if a != d["remove"]]
        mode = d.get("mode") if d.get("mode") in ("auto", "manual") else None
        pol = mem.set_pkg_policy(mode=mode, allow=allow)
        log.info("pkg policy: mode=%s allow=%s", pol["mode"], pol["allow"])
        return {"ok": True, **pol}

    @app.route("/control/pkg", methods=["POST"])
    def control_pkg():
        d = request.get_json(silent=True) or {}
        action = d.get("action")
        if action == "installed":
            mem.resolve_package((d.get("name") or "").strip(), installed=True)
            return {"ok": True}
        if action == "dismiss":
            mem.resolve_package((d.get("name") or "").strip(), installed=False)
            return {"ok": True}
        if action == "installer":
            # Sanitise to valid apt package names so the generated script can't be
            # injected into (the agent proposes these; a crafted name must not run).
            names = [n for n in (d.get("names") or [])
                     if isinstance(n, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._:-]*", n)]
            if not names:
                return {"ok": False, "error": "no valid package names selected"}, 400
            script = ("#!/usr/bin/env bash\n"
                      "# Generated by the DRONGO dashboard. Review, then run:\n"
                      "#   sudo bash " + str(cfg.base_dir / "pkg-installer.sh") + "\n"
                      "set -e\nsudo apt-get update\nsudo apt-get install -y " + " ".join(names) + "\n")
            path = cfg.base_dir / "pkg-installer.sh"
            try:
                path.write_text(script, encoding="utf-8")
                os.chmod(path, 0o755)
            except Exception as e:
                return {"ok": False, "error": str(e)}, 500
            log.info("wrote pkg-installer.sh for: %s", " ".join(names))
            return {"ok": True, "path": str(path), "count": len(names), "script": script}
        return {"ok": False, "error": "unknown action"}, 400

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

    @app.route("/control/turbo", methods=["POST"])
    def control_turbo():
        on = bool((request.get_json(silent=True) or {}).get("on"))
        mem.remember("turbo", on)
        if on:
            mem.remember("run_now", True)   # start working immediately
        log.info("turbo mode: %s", "ON" if on else "off")
        return {"ok": True, "on": on}

    @app.route("/control/mission", methods=["POST"])
    def control_mission():
        d = request.get_json(silent=True) or {}
        mem.set_mission((d.get("text") or "")[:400])
        log.info("standing mission updated")
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

    @app.route("/control/add_provider", methods=["POST"])
    def control_add_provider():
        # Add a custom LLM provider from the dashboard. Stored in settings; takes
        # effect on restart (apply_overrides appends it to the router).
        d = request.get_json(silent=True) or {}
        name = re.sub(r"[^a-z0-9_-]", "", (d.get("name") or "").strip().lower())[:30]
        base_url = (d.get("base_url") or "").strip()
        model = (d.get("model") or "").strip()
        if not name or not model or not base_url.startswith(("http://", "https://")):
            return {"ok": False, "error": "need a name, an http(s) base_url and a model"}, 400
        key_env = (re.sub(r"[^A-Z0-9_]", "", (d.get("api_key_env") or "").strip().upper())[:40]
                   or name.upper().replace("-", "_") + "_API_KEY")
        spec = {"name": name, "base_url": base_url, "model": model,
                "api_key_env": key_env, "enabled": True,
                "rpm_limit": int(d.get("rpm_limit") or 0) or None,
                "daily_limit": int(d.get("daily_limit") or 0) or None}
        if (d.get("type") or "").strip() == "anthropic":
            spec["type"] = "anthropic"
        cur = mem.recall("settings") or {}
        llm = cur.setdefault("llm", {})
        llm["custom_providers"] = [c for c in (llm.get("custom_providers") or [])
                                   if c.get("name") != name] + [spec]
        key = (d.get("key") or "").strip()
        if key:
            cur.setdefault("env", {})[key_env] = key
        mem.remember("settings", cur)
        log.info("added custom provider '%s' (%s)", name, base_url)
        return {"ok": True, "name": name}

    @app.route("/control/provider_order", methods=["POST"])
    def control_provider_order():
        order = (request.get_json(silent=True) or {}).get("order")
        if not isinstance(order, list):
            return {"ok": False, "error": "bad order"}, 400
        cur = mem.recall("settings") or {}
        cur.setdefault("llm", {})["order"] = [str(n) for n in order if n]
        mem.remember("settings", cur)
        log.info("provider order set: %s", " > ".join(str(n) for n in order))
        return {"ok": True}

    @app.route("/control/remove_provider", methods=["POST"])
    def control_remove_provider():
        name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
        cur = mem.recall("settings") or {}
        if isinstance(cur.get("llm"), dict):
            cur["llm"]["custom_providers"] = [c for c in (cur["llm"].get("custom_providers") or [])
                                              if c.get("name") != name]
            mem.remember("settings", cur)
        log.info("removed custom provider '%s'", name)
        return {"ok": True}

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
        if not rel.endswith((".py", ".sh")):
            return {"ok": False, "error": "only .py or .sh files can be run"}, 400
        try:
            full = safeguard.safe_join(str(ws), rel)
        except Exception:
            return {"ok": False, "error": "path escapes the workspace"}, 400
        if not os.path.isfile(full) or "/projects/" not in full.replace(os.sep, "/"):
            return {"ok": False, "error": "only scripts under projects/ can be run"}, 404
        venv_py = os.path.join(str(cfg.project_venv), "bin", "python")
        py = venv_py if os.path.exists(venv_py) else "python3"
        env = tools._project_env(cfg)   # venv on PATH + SECRETS STRIPPED (no key leak)
        # .sh runs via bash (lets compiled C/C++ projects build+run from a run.sh);
        # .py via the project venv. Both as the unprivileged drongo user, sandboxed.
        cmd = ["bash", full] if rel.endswith(".sh") else [py, full]
        cwd = os.path.dirname(full) if rel.endswith(".sh") else str(ws)
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True,
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
        env = tools._project_env(cfg)   # venv on PATH + SECRETS STRIPPED (no key leak)
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

    @app.route("/img/<path:relpath>")
    def serve_img(relpath):
        # Like /file, but transcodes netpbm (.ppm/.pgm/.pbm — what C renderers emit)
        # to PNG so the browser can show it. Other image types pass straight through.
        root = os.path.realpath(str(ws))
        full = os.path.realpath(os.path.join(root, relpath))
        if full != root and not full.startswith(root + os.sep):
            abort(403)
        if not os.path.isfile(full):
            abort(404)
        if relpath.lower().endswith(_NETPBM_EXTS):
            try:
                with open(full, "rb") as fh:
                    png = _netpbm_to_png(fh.read(20_000_000))   # read cap (memory)
                if png:
                    return Response(png, mimetype="image/png")
            except Exception as e:
                log.warning("ppm->png failed for %s: %s", relpath, e)
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

    def loopval(k, d):
        return loop_db.get(k, cfg.get("loop", k, default=d))

    sv = {
        "loop": {k: loop_db.get(k, cfg.get("loop", k, default="")) for k in
                 ("interval_seconds", "jitter_seconds", "max_steps", "max_resume_attempts")},
        "idea_candidates": loopval("idea_candidates", 2),
        "hw_scan": loopval("hw_scan_interval_seconds", 1200),
        "cleanup_int": loopval("cleanup_interval_seconds", 1800),
        "self_critique": bool(loopval("self_critique", True)),
        "git_history": bool(loopval("git_history", True)),
        "cleanup_enabled": bool(loopval("cleanup_enabled", True)),
        "temperature": llm_db.get("temperature", cfg.get("llm", "temperature", default=0.7)),
        "max_tokens": llm_db.get("max_tokens", cfg.get("llm", "max_tokens", default=2048)),
        "req_timeout": llm_db.get("request_timeout", cfg.get("llm", "request_timeout", default=120)),
        "local_timeout": llm_db.get("local_timeout", cfg.get("llm", "local_timeout", default=300)),
        "img_provider": (s.get("images") or {}).get("provider", cfg.get("tools", "images", "provider", default="pollinations")),
        "img_cmd": (s.get("images") or {}).get("local_cmd", cfg.get("tools", "images", "local_cmd", default="")),
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
    custom_names = {c.get("name") for c in (llm_db.get("custom_providers") or [])}
    pkey = {}
    def _is_local(spec):
        b = spec.get("base_url") or ""
        return bool(spec.get("local")) or "localhost" in b or "127.0.0.1" in b

    for p in cfg.get("llm", "providers", default=[]) or []:
        name, o = p.get("name"), pov.get(p.get("name")) or {}
        enabled = bool(o.get("enabled", p.get("enabled", True)))
        ks = keyset(p.get("api_key_env"))
        sv["providers"].append({
            "name": name,
            "enabled": enabled,
            "model": o.get("model") or p.get("model", ""),
            "key_env": p.get("api_key_env"),
            "key_set": ks,
            "custom": name in custom_names,
            "usable": enabled and (_is_local(p) or ks),   # what the router will actually load
        })
        if p.get("api_key_env"):
            pkey[name] = p["api_key_env"]
    # Dashboard-added providers live in the DB settings and only land in cfg after
    # a restart of THIS (web) process — so surface them straight from settings too,
    # otherwise a just-added provider seems to vanish until the web service restarts.
    seen = {pp["name"] for pp in sv["providers"]}
    for c in (llm_db.get("custom_providers") or []):
        nm = c.get("name")
        if not nm or nm in seen:
            continue
        o = pov.get(nm) or {}
        enabled = bool(o.get("enabled", c.get("enabled", True)))
        ks = keyset(c.get("api_key_env"))
        sv["providers"].append({
            "name": nm,
            "enabled": enabled,
            "model": o.get("model") or c.get("model", ""),
            "key_env": c.get("api_key_env"),
            "key_set": ks,
            "custom": True,
            "usable": enabled and (_is_local(c) or ks),
        })
        if c.get("api_key_env"):
            pkey[nm] = c["api_key_env"]
    order = llm_db.get("order")
    if isinstance(order, list) and order:                 # reflect chosen try-order
        idx = {n: i for i, n in enumerate(order)}
        sv["providers"].sort(key=lambda p: idx.get(p["name"], len(idx) + 1))
    return sv, pkey


def _ls(directory, exts):
    p = Path(directory)
    if not p.exists():
        return []
    files = [f.name for f in p.iterdir() if f.suffix.lower() in exts]
    files.sort(key=lambda n: (p / n).stat().st_mtime, reverse=True)
    return files


_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ppm", ".pgm", ".pbm")
_NETPBM_EXTS = (".ppm", ".pgm", ".pbm")


def _png_encode(width, height, rgb):
    """Minimal stdlib PNG encoder (8-bit RGB). rgb = width*height*3 bytes."""
    def chunk(typ, body):
        return (struct.pack(">I", len(body)) + typ + body
                + struct.pack(">I", zlib.crc32(typ + body) & 0xffffffff))
    stride = width * 3
    raw = bytearray()
    for y in range(height):                 # PNG wants a filter byte per scanline
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _netpbm_to_png(data):
    """Convert a P2/P3/P5/P6 netpbm (PPM/PGM) to PNG bytes, or None. Capped at 4MP
    to bound memory (the dashboard has a small RAM cgroup)."""
    if data[:1] != b"P" or data[1:2] not in b"2356":
        return None
    magic = data[:2]
    pos = 2

    def tok(pos):
        while pos < len(data):
            c = data[pos:pos + 1]
            if c in b" \t\r\n":
                pos += 1
            elif c == b"#":
                while pos < len(data) and data[pos:pos + 1] != b"\n":
                    pos += 1
            else:
                break
        start = pos
        while pos < len(data) and data[pos:pos + 1] not in b" \t\r\n":
            pos += 1
        return data[start:pos], pos
    try:
        w, pos = tok(pos); h, pos = tok(pos); mx, pos = tok(pos)
        width, height, maxval = int(w), int(h), int(mx)
    except Exception:
        return None
    if not (0 < width and 0 < height and width * height <= 4_000_000 and 0 < maxval <= 65535):
        return None
    gray = magic in (b"P2", b"P5")
    npix = width * height
    nsamp = npix if gray else npix * 3
    if magic in (b"P3", b"P2"):                       # ASCII samples
        nums = data[pos:].split()
        if len(nums) < nsamp:
            return None
        sc = (lambda v: v) if maxval == 255 else (lambda v: v * 255 // maxval)
        s = [sc(int(x)) for x in nums[:nsamp]]
        rgb = bytes(s) if not gray else bytes(v for v in s for _ in range(3))
    else:                                             # binary (P5/P6)
        pos += 1                                      # single whitespace after maxval
        bpp = 1 if maxval < 256 else 2
        body = data[pos:pos + nsamp * bpp]
        if len(body) < nsamp * bpp:
            return None
        if bpp == 2:
            body = bytes(((body[2 * k] << 8 | body[2 * k + 1]) * 255 // maxval) for k in range(nsamp))
        elif maxval != 255:
            body = bytes(v * 255 // maxval for v in body)
        rgb = body if not gray else bytes(v for v in body for _ in range(3))
    if len(rgb) != npix * 3:
        return None
    return _png_encode(width, height, rgb)


def _gallery_images(cfg):
    """Every image the agent has made: the images/ gallery PLUS any images it
    saved inside its projects/ (fractals, generative art, etc.). Newest first,
    workspace-relative paths so /file/<path> serves them."""
    root = Path(cfg.workspace)
    found = []
    for base in (Path(cfg.images), Path(cfg.projects)):
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.is_file() and f.suffix.lower() in _IMG_EXTS:
                found.append(f)
    found.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [str(f.relative_to(root)).replace(os.sep, "/") for f in found[:200]]


def serve(cfg, mem):
    app = create_app(cfg, mem)
    host = cfg.get("web", "host", default="127.0.0.1")
    port = cfg.get("web", "port", default=8080)
    if host not in _PRIVATE and not os.environ.get("DRONGO_WEB_PASSWORD"):
        log.warning("No DRONGO_WEB_PASSWORD set — binding the dashboard to localhost "
                    "only. Set a password to reach it over the LAN (ssh -L 8080:localhost:%s).", port)
        host = "127.0.0.1"
    app.run(host=host, port=port, threaded=True)
