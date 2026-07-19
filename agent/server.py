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

PAGE = (Path(__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")


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

    try:
        from .docs import DocStore
        docstore = DocStore(cfg.docs_db)
    except Exception as e:
        docstore = None
        log.warning("doc store unavailable: %s", e)
    app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024   # cap uploads (spare the SD)

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

    def _clean_note(body):
        """Older journal rows can hold the model's raw JSON instead of the note
        (a reflect() parse failure). Show the human-readable part instead."""
        t = (body or "").strip()
        if not t.startswith("{"):
            return body or ""
        try:
            obj = json.loads(t)
        except Exception:
            m = re.search(r'"(?:note|reply|final)"\s*:\s*"((?:[^"\\]|\\.)*)"?', t)
            if m:
                try:
                    return json.loads('"' + m.group(1) + '"')
                except Exception:
                    return m.group(1)
            return body or ""
        if isinstance(obj, dict):
            for k in ("note", "reply", "final", "description", "summary"):
                if isinstance(obj.get(k), str) and obj[k].strip():
                    return obj[k].strip()
        return body or ""

    def _journal(limit, kind=None):
        rows = []
        for j in mem.recent_journal(limit, kind=kind):
            rows.append({"id": j["id"], "title": j["title"] or "", "kind": j["kind"],
                         "task_type": j["task_type"], "body": _clean_note(j["body"]),
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

    @app.route("/api/docs")
    def api_docs():
        if not docstore:
            return {"ok": True, "documents": [], "count": {"documents": 0, "chunks": 0}, "fts": False}
        return {"ok": True, "documents": docstore.documents(), "count": docstore.count(),
                "fts": docstore.fts}

    @app.route("/control/doc_upload", methods=["POST"])
    def control_doc_upload():
        if not docstore:
            return {"ok": False, "error": "doc store unavailable"}, 500
        files = request.files.getlist("files")
        if not files:
            return {"ok": False, "error": "no files"}, 400
        from .docs import TEXT_EXTS
        docdir = Path(cfg.docs_dir); docdir.mkdir(parents=True, exist_ok=True)
        saved, skipped, chunks = [], [], 0
        for f in files:
            name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(f.filename or ""))[:120]
            if not name:
                continue
            if os.path.splitext(name)[1].lower() not in TEXT_EXTS:
                skipped.append(name); continue
            dest = docdir / name
            f.save(str(dest))
            chunks += docstore.add_file(dest, source=name)
            saved.append(name)
        log.info("docs uploaded: %s (%d passages)", ", ".join(saved) or "-", chunks)
        return {"ok": True, "saved": saved, "skipped": skipped, "chunks": chunks}

    @app.route("/control/doc_delete", methods=["POST"])
    def control_doc_delete():
        src = ((request.get_json(silent=True) or {}).get("source") or "").strip()
        if not src:
            return {"ok": False, "error": "no source"}, 400
        if docstore:
            docstore.delete(src)
        try:
            f = safeguard.safe_join(str(cfg.docs_dir), src)
            if os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass
        return {"ok": True}

    @app.route("/api/doc_search")
    def api_doc_search():
        q = (request.args.get("q") or "").strip()
        return {"ok": True, "hits": (docstore.search(q, k=6) if (docstore and q) else [])}

    @app.route("/api/mcp")
    def api_mcp():
        return {"ok": True, "servers": mem.recall("mcp_servers") or []}

    @app.route("/control/mcp_add", methods=["POST"])
    def control_mcp_add():
        # Configure an external MCP tool server. It's launched inside the agent's
        # OWN sandbox (no sudo, ProtectSystem) — no more privileged than `shell`.
        d = request.get_json(silent=True) or {}
        name = re.sub(r"[^a-z0-9_-]", "", (d.get("name") or "").strip().lower())[:30]
        if not name:
            return {"ok": False, "error": "need a name (a-z 0-9 _ -)"}, 400
        transport = "http" if d.get("transport") == "http" else "stdio"
        spec = {"name": name, "transport": transport, "enabled": True}
        if transport == "http":
            url = (d.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                return {"ok": False, "error": "http server needs an http(s) url"}, 400
            spec["url"] = url
        else:
            cmd = (d.get("command") or "").strip()
            if not cmd:
                return {"ok": False, "error": "stdio server needs a command (e.g. npx)"}, 400
            spec["command"] = cmd
            a = d.get("args")
            spec["args"] = ([x for x in a.split() if x] if isinstance(a, str)
                            else [str(x) for x in (a or [])])
        if isinstance(d.get("env"), dict):
            spec["env"] = {str(k): str(v) for k, v in d["env"].items() if v}
        servers = [s for s in (mem.recall("mcp_servers") or []) if s.get("name") != name]
        servers.append(spec)
        mem.remember("mcp_servers", servers)
        log.info("MCP server configured: %s (%s)", name, transport)
        return {"ok": True, "name": name}

    @app.route("/control/mcp_remove", methods=["POST"])
    def control_mcp_remove():
        name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
        mem.remember("mcp_servers", [s for s in (mem.recall("mcp_servers") or [])
                                     if s.get("name") != name])
        return {"ok": True}

    @app.route("/control/mcp_toggle", methods=["POST"])
    def control_mcp_toggle():
        d = request.get_json(silent=True) or {}
        name, on = (d.get("name") or "").strip(), bool(d.get("on"))
        servers = mem.recall("mcp_servers") or []
        for s in servers:
            if s.get("name") == name:
                s["enabled"] = on
        mem.remember("mcp_servers", servers)
        return {"ok": True}

    @app.route("/control/mcp_test", methods=["POST"])
    def control_mcp_test():
        # Connect once and report the tools it exposes (or the error).
        name = ((request.get_json(silent=True) or {}).get("name") or "").strip()
        spec = next((s for s in (mem.recall("mcp_servers") or []) if s.get("name") == name), None)
        if not spec:
            return {"ok": False, "error": "no such server"}, 404
        from .mcp_client import probe_server
        return probe_server(dict(spec, enabled=True))

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
            text, prov = chat_router.chat(messages, temperature=0.5, max_tokens=700, only=pin, purpose="chat")
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

    @app.route("/api/requests")
    def api_requests():
        try:
            limit = min(max(int(request.args.get("limit", 120)), 1), 500)
        except (TypeError, ValueError):
            limit = 120
        return {"ok": True, "requests": mem.recent_requests(limit),
                "summary": mem.requests_summary()}

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
            elif action == "skip":
                # abandon whatever it's stuck on and move to a fresh project now
                mem.remember("current_project", None)
                mem.remember("working_on", None)
                mem.remember("run_now", True)
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
        # If the agent is currently working on / resuming THIS project, drop that
        # in-progress state too — otherwise it keeps trying to continue a project
        # whose files we just removed and won't move on to anything new.
        cur = mem.recall("current_project")
        if isinstance(cur, dict):
            task = cur.get("task") or {}
            title = task.get("title") or ""
            cur_paths = [a.get("path") for a in (cur.get("artifacts") or [])
                         if isinstance(a, dict) and a.get("path")]
            matches = (
                (j["title"] and j["title"] == title) or ("Fix #%d:" % jid) in title or
                any(cp == rp or (rp.endswith("/") and cp.startswith(rp))
                    for cp in cur_paths for rp in removed))
            if matches:
                mem.remember("current_project", None)
                mem.remember("working_on", None)
                log.info("cleared in-progress state for deleted project '%s'", j["title"])
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
    """The curated gallery: ONLY the images/ folder. The agent puts images it
    MAKES here (generate_image writes here; for images it renders itself it calls
    add_to_gallery). We deliberately do NOT scan projects/ — that picked up
    reference/sample images the agent downloaded, which don't belong in a gallery
    of its own work. Newest first, workspace-relative for /file/<path>."""
    root = Path(cfg.workspace)
    base = Path(cfg.images)
    if not base.exists():
        return []
    found = [f for f in base.rglob("*") if f.is_file() and f.suffix.lower() in _IMG_EXTS]
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
