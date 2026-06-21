"""Local web dashboard — your window into what DRONGO has been up to.

Read-only Flask app sharing the SQLite DB and workspace with the agent. Shows
the activity journal, a gallery of generated images, links to games/scripts/
dashboards the agent built, LLM-provider usage, and live health (heartbeat,
safe-mode). Bind it to your LAN and check in whenever.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import time
from pathlib import Path

from flask import Flask, Response, abort, render_template_string, request, send_from_directory

from . import watchdog
from .memory import Memory, utc_iso
from .safeguard import integrity_status

log = logging.getLogger("agent.server")

_PRIVATE = ("127.0.0.1", "localhost", "::1", "")

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{{ name }} · console</title>
<style>
 :root{--bg:#0e1116;--card:#171c24;--mut:#8a93a3;--fg:#e6edf3;--ac:#4cc2ff;--ok:#3fb950;--bad:#f85149;--bd:#232a34}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 ui-sans-serif,system-ui,sans-serif}
 header{padding:18px 22px;border-bottom:1px solid var(--bd);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 h1{font-size:20px;margin:0} .pill{font-size:12px;padding:3px 9px;border-radius:20px;border:1px solid var(--bd);color:var(--mut)}
 .pill.ok{color:var(--ok);border-color:#1c3} .pill.bad{color:var(--bad);border-color:#622}
 main{max-width:1000px;margin:0 auto;padding:22px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
 .grid img{width:100%;height:150px;object-fit:cover;border-radius:8px;border:1px solid var(--bd)}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin:12px 0}
 .card h3{margin:0 0 4px} .meta{color:var(--mut);font-size:12.5px}
 a{color:var(--ac);text-decoration:none} a:hover{text-decoration:underline}
 .art{display:inline-block;margin:4px 8px 0 0;font-size:13px}
 h2{font-size:15px;color:var(--mut);text-transform:uppercase;letter-spacing:.06em;margin-top:30px}
 table{width:100%;border-collapse:collapse;font-size:13.5px} td,th{padding:6px 8px;border-bottom:1px solid var(--bd);text-align:left}
</style></head><body>
<header>
 <h1>{{ name }}</h1>
 <span class="pill {{ 'ok' if alive else 'bad' }}">{{ 'alive · ' + hb if alive else 'no heartbeat' }}</span>
 {% if safe %}<span class="pill bad">SAFE MODE</span>{% endif %}
 <span class="pill {{ 'ok' if integ_ok else 'bad' }}">guard {{ 'ok' if integ_ok else 'CHECK' }}</span>
 <span class="pill">providers: {{ providers }}</span>
</header>
<main>
 {% if working_on %}<p class="meta">⏳ Currently working on: <b>{{ working_on.title }}</b>
   <span style="opacity:.6">({{ working_on.type }} · attempt {{ working_on.attempt }})</span></p>{% endif %}
 <h2>Recent activity</h2>
 {% for j in journal %}
  <div class="card">
    <h3>{{ j.title }} <span class="meta">· {{ j.kind }}{% if j.task_type %} · {{ j.task_type }}{% endif %}</span></h3>
    <div class="meta">{{ j.when }}{% if j.provider %} · via {{ j.provider }}{% endif %}{% if not j.ok %} · ⚠ unfinished{% endif %}</div>
    <p>{{ j.body }}</p>
    {% for a in j.arts %}<a class="art" href="/file/{{ a.path }}">▸ {{ a.label }}</a>{% endfor %}
  </div>
 {% else %}<p class="meta">Nothing yet — give it time, mate.</p>{% endfor %}

 {% if images %}<h2>Gallery</h2><div class="grid">
   {% for im in images %}<a href="/file/images/{{ im }}"><img loading=lazy src="/file/images/{{ im }}"></a>{% endfor %}
 </div>{% endif %}

 {% if dashboards %}<h2>Dashboards & games</h2>
   {% for d in dashboards %}<div class="art">▸ <a href="/file/dashboards/{{ d }}">{{ d }}</a></div>{% endfor %}
 {% endif %}

 <h2>LLM usage today</h2>
 <table><tr><th>provider</th><th>today</th><th>this minute</th><th>total</th><th>cooldown</th></tr>
 {% for u in usage %}<tr><td>{{ u.provider }}</td><td>{{ u.day_count }}</td><td>{{ u.minute_count }}</td><td>{{ u.total }}</td>
   <td>{{ u.cool }}</td></tr>{% endfor %}</table>
 <p class="meta">Guard: {{ integ.mode }} · owner uid {{ integ.owner_uid }} · hash {{ 'ok' if integ.hash_ok else 'MISMATCH' }}</p>
</main></body></html>"""


def create_app(cfg, mem: Memory) -> Flask:
    app = Flask(__name__)
    name = cfg.get("identity", "name", default="DRONGO")

    # --- access control ----------------------------------------------------
    # Password (HTTP Basic auth) — the installer auto-generates one.
    password = os.environ.get("DRONGO_WEB_PASSWORD", "")
    # Optional IP allowlist — e.g. DRONGO_WEB_ALLOW="192.168.1.50,192.168.1.0/24"
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
        # 1) optional source-IP allowlist
        if nets:
            try:
                ip = ipaddress.ip_address((request.remote_addr or "").split("%")[0])
            except ValueError:
                return Response("forbidden\n", 403)
            if not any(ip in n for n in nets):
                return Response("forbidden\n", 403)
        # 2) password (constant-time compare; any username accepted)
        if password:
            a = request.authorization
            if not a or not a.password or not hmac.compare_digest(a.password, password):
                return Response("authentication required\n", 401,
                                {"WWW-Authenticate": 'Basic realm="DRONGO"'})
        return None

    @app.route("/")
    def index():
        journal = []
        for j in mem.recent_journal(40):
            journal.append({
                # Jinja autoescapes on render, so pass raw text (no manual escape).
                "title": j["title"] or "",
                "kind": j["kind"], "task_type": j["task_type"],
                "body": j["body"] or "",
                "provider": j["provider"], "ok": bool(j["ok"]),
                "when": utc_iso(j["ts"]),
                "arts": json.loads(j["artifacts"] or "[]"),
            })
        images = _ls(cfg.images, (".png", ".jpg", ".jpeg"))
        dashboards = _ls(cfg.dashboards, (".html",))
        usage = []
        for u in mem.usage_summary():
            cool = ""
            if u["cooldown_until"] and u["cooldown_until"] > time.time():
                cool = f"{int(u['cooldown_until'] - time.time())}s"
            usage.append({**u, "cool": cool})
        age = watchdog.heartbeat_age(cfg)
        integ = integrity_status()
        running_root = getattr(os, "geteuid", lambda: -1)() == 0
        integ_ok = integ["hash_ok"] and (running_root or not integ["writable_by_me"])
        return render_template_string(
            PAGE, name=name, journal=journal, images=images, dashboards=dashboards,
            usage=usage, providers=", ".join(p["provider"] for p in mem.usage_summary()) or "—",
            alive=age is not None and age < 1800,
            hb=(f"{int(age)}s ago" if age is not None else "—"),
            safe=bool(mem.recall("safe_mode")),
            working_on=mem.recall("working_on"),
            integ_ok=integ_ok, integ=integ)

    @app.route("/file/<path:relpath>")
    def serve_file(relpath):
        root = os.path.realpath(str(cfg.workspace))
        full = os.path.realpath(os.path.join(root, relpath))
        if full != root and not full.startswith(root + os.sep):
            abort(403)
        if not os.path.isfile(full):
            abort(404)
        return send_from_directory(root, relpath)

    @app.route("/api/status")
    def status():
        age = watchdog.heartbeat_age(cfg)
        return {
            "name": name,
            "heartbeat_age": age,
            "alive": age is not None and age < 1800,
            "integrity": integrity_status(),
            "usage": mem.usage_summary(),
            "recent": [j["title"] for j in mem.recent_journal(10)],
        }

    return app


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
    # Safe default: never expose the dashboard to the LAN without a password.
    # No password set + a public bind -> fall back to localhost only.
    if host not in _PRIVATE and not os.environ.get("DRONGO_WEB_PASSWORD"):
        log.warning("No DRONGO_WEB_PASSWORD set — binding the dashboard to localhost "
                    "only. Set a password to reach it over the LAN (SSH-tunnel to view: "
                    "ssh -L 8080:localhost:%s <pi>).", port)
        host = "127.0.0.1"
    app.run(host=host, port=port, threaded=True)
