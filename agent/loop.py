"""The autonomous loop: ideate -> act (ReAct) -> reflect -> sleep.

Each cycle the agent looks at what it has done recently and its interests,
proposes one concrete project, then drives the tools in a JSON tool-calling
loop until it declares the project finished. A short reflection is written to
the journal so you can see what it got up to.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import signal
import subprocess
from collections import Counter
import socket
import sys
import time
from pathlib import Path

from . import safeguard, tools, watchdog
from .llm import AllProvidersFailed

log = logging.getLogger("agent.loop")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TASK_TYPES = [
    "browser_game", "creative_image", "utility_script", "sensor_dashboard",
    "web_research_note", "self_maintenance", "experiment",
    "simulation", "generative_art", "native_program",
]

EXEC_SYSTEM = """{persona}

YOUR ENVIRONMENT (you run here — no need to guess):
- A headless Debian Linux box (Rock Pi / ARM64). You are the 'drongo' user: no
  sudo. You have a writable Python venv — `pip install <pkg>` and `python <file>`
  already use it. gcc/g++/make are installed; check others with `which <tool>`.
- The `shell` tool runs bash in your project folder. `read_file`/`write_file`/
  `list_dir` work inside your workspace only. `web_search` finds pages,
  `web_fetch` reads one. Need an apt package? call request_package (a helper
  installs allowed ones). The FULL tool list with signatures is at the bottom —
  READ IT and use the right tool instead of guessing.
- How to WORK: think a step, call ONE tool, read the observation, adapt, repeat.
  Don't plan endlessly in text — act. Verify by reading files back / running them.

Respond with EXACTLY ONE JSON object and nothing else. Two shapes are allowed:

  {{"thought": "<ONE short sentence>", "tool": "<tool_name>", "args": {{...}}}}
  {{"thought": "<ONE short sentence>", "final": "<2-4 sentence summary of what you built and where it lives>"}}

Rules:
- Output raw JSON only. No markdown, no code fences, no prose around it. Keep
  "thought" to ONE short sentence — spend tokens on the work, not narration.
- Build something real and finished, not a stub. Test it when you can.
- Your files are auto-consolidated into your project's Working folder (shown
  above) — just use plain relative names like "index.html" or "src/main.c" and
  they land there; don't invent your own top-level folders or write to the
  workspace root. Images go to the gallery via generate_image; dashboards via
  make_dashboard.
- IMAGES: when a task wants a picture, you MUST call generate_image — it fetches a
  REAL raster image and saves it to the gallery for your human to see. If your own
  CODE renders an image (a fractal, a plot, a PPM/PNG), call add_to_gallery with
  its path so it shows up. Reference/sample images you DOWNLOADED do not go in the
  gallery. Never "describe" an image in words, and never substitute ASCII art or a
  hand-written SVG when an actual image was asked for. If generate_image ERRORs, try
  again (it may be rate-limited) or simplify the prompt; only then fall back.
- DASHBOARDS can be fully DYNAMIC (live data, JS, charts) AND have a Python
  backend — you just don't run your own web server. The pattern:
    1. Frontend: an HTML file with client-side JavaScript (fetch, canvas,
       <table>, charts — whatever you like). Put it under projects/<name>/ or
       make a static one via make_dashboard.
    2. Backend: a SMALL Python script under projects/<name>/ that, when run,
       prints ONE JSON object to stdout and exits (read sensors / stats / files,
       json.dumps(...), print it, done — NO server, NO loop, NO input()).
    3. Wire them: your JS polls GET /data/projects/<name>/<script>.py on the
       SAME origin, e.g.
         setInterval(()=>fetch('/data/projects/foo/data.py')
           .then(r=>r.json()).then(render), 2000);
       DRONGO runs that script in your venv per request and returns its stdout
       as the response — that IS your live Python backend. Open the page from
       the Projects/Home file links.
  Do NOT use HTTPServer / Flask app.run / socket.bind — a long-running server
  never exits (so it can't be Run from the UI) and port 8080 is already DRONGO's.
- DOCUMENT every project: alongside the code write a short README.md (in the same
  projects/ folder) covering what it is, how to run it, what it needs, and how to
  use it. Keep code commented where it helps.
- GROW & REUSE your knowledge. Before building, glance at recall_skill (saved code
  patterns) and recall_notes (saved facts/findings); when you get a reusable snippet
  working save it with save_skill, and when you learn something useful from research
  (an API shape, how a sensor works) save it with save_note — so you compound over
  time instead of relearning.
- TIDY UP after yourself. Keep each project in ONE folder under projects/<name>/.
  Delete scratch, temp, downloaded, or experimental files you no longer need with
  delete_path — don't leave half-baked junk or empty folders lying around. A
  finished project folder should contain just its working files + README.
- IT MUST WORK FOR YOUR HUMAN, not just for you. Two ways they run things:
    * From the dashboard: .py files have a "▶ run" button; HTML pages and live
      dashboards open from the Projects/Home file links. Make sure those work.
    * From a shell: prefer the Python STANDARD LIBRARY so a plain `python3 file.py`
      just works for anyone. If you DO need a pip package, the README's run command
      must use the venv interpreter by its absolute path (find it with
      `python -c "import sys; print(sys.executable)"`) — a bare `python file.py`
      would miss the package in your human's own shell.
- You have a writable Python environment: install dependencies with
  `pip install <package>` and run code with `python <file>` (both already point at
  your project venv). Prefer the standard library, but install what you need —
  and if you install something, list it in the README so it isn't a surprise.
- NEED A SYSTEM PACKAGE? You can `pip install` Python libs yourself, but you can't
  apt-install system packages (no sudo). If you need one (a compiler, a C library,
  a CLI tool), call request_package(name, reason) — your human installs it and it
  becomes available. Don't keep retrying something that needs a missing system dep.
- YOU ARE A POLYGLOT — don't default to Python for everything. C and C++ are fully
  available: `gcc`, `g++` and `make` are installed. Write real C/C++ (and shell)
  when it fits — fast native tools, demos, simulations, classic algorithms. Build
  and TEST it in the shell (e.g. `g++ -O2 main.cpp -o app && ./app`), document the
  exact build+run command in the README, and for anything meant to be launched add
  a small `run.sh` (it compiles + runs) so it works from the dashboard's ▶ run
  button too. Check what else exists with `which gcc g++ make node rustc` before
  assuming a toolchain is missing (you can't apt-install — no sudo).
- RETRO / 8-bit: if the Z80 toolchain is installed you can build for the Amstrad
  CPC, ZX Spectrum and Z80 generally. Probe with `which sdcc zcc pasmo` and
  `[ -d "$CPCT_PATH" ]`: sdcc (C for Z80), zcc (z88dk — C+asm for CPC/Spectrum),
  pasmo (Z80 assembler, also for SymbOS), and CPCtelera at $CPCT_PATH (Amstrad CPC
  games). Produce a runnable .dsk/.cdt/.tap or .bin and document how to load it.
- HARDWARE SAFETY — this is a real board that can HANG. NEVER actively scan or
  probe the I2C bus: no `i2cdetect`, no `i2cget`/`i2cset` sweeps, and no
  smbus/smbus2 address-probing loops (`for addr in range(...): bus.read_byte(addr)`).
  On this SoC the power-management IC (PMIC), RTC and other critical chips sit on
  I2C, and poking their addresses LOCKS UP THE WHOLE MACHINE — a hard freeze the
  watchdog can't recover. To discover hardware, read PASSIVELY: list /dev/i2c-*,
  read /sys/bus/i2c/devices/* (already-bound devices + drivers), and use the
  discover_sensors tool (it inventories buses safely without touching them). Only
  ever address a specific i2c device if your human gave you its exact bus AND
  address for a known peripheral. The same caution applies to raw /dev memory,
  GPIO banks you haven't been told are free, and SPI/1-wire blind probing.
- Return "final" ONLY when the artifact actually exists and works and you've
  verified it (e.g. read the file back / ran it). NEVER return "final" just
  because you're stuck, blocked, rate-limited, or out of ideas — that falsely
  reports success. If you can't finish now, keep using tools; you'll be resumed
  next cycle to continue exactly where you left off.
- Be efficient with each step, but it's fine to take several cycles to finish.

Available tools:
{tools}
"""

IDEATE_SYSTEM = """{persona}

Decide your next self-directed project. Finishable in a handful of steps, but
genuinely useful or delightful.
{focus}
USE THE HARDWARE. This box has spare CPU going to waste — when it fits, prefer
computationally RICH projects that actually exercise it: fractals and Mandelbrot
zooms, particle systems, cellular automata (Game of Life, reaction-diffusion),
ray/path tracers, procedural worlds/maps, physics sims, pathfinding visualisers,
number-crunching. Compute real pixels/data, don't just print a stub. (simulation
and generative_art task_types are made for this.)

{novelty}

Reply with ONE JSON object only:
  {{"task_type": "<one of: {types}>", "title": "<short title>", "description": "<what to build and why, 1-3 sentences>"}}
"""

# Novelty guidance when there is NO standing mission: maximise variety across
# everything.
_NOVELTY_FREE = """NOVELTY IS THE POINT. Look hard at what you've recently built and deliberately do
something DIFFERENT — a different task_type AND a different subject. Variety is
across games, generative images, utilities, research notes, retro/Z80, creative
toys — not ten versions of the same idea.
Specifically: do NOT keep building hardware/temperature/CPU/sensor things. Reading
the box's own stats is fun ONCE; if you've done it recently, that subject is OFF
the table this round. Don't bolt a temp/CPU readout onto an unrelated project
either. When in doubt, pick the kind of thing you've done LEAST."""

# Novelty guidance when a standing mission IS set: the mission fixes the SUBJECT,
# so variety comes from the FORM — never from drifting off the mission.
_NOVELTY_MISSION = """VARIETY WITHIN THE MISSION. Your standing mission (above) fixes the SUBJECT — do
NOT drift off it to chase novelty; that is the mistake to avoid. Get your variety
from the FORM instead: rotate across different KINDS of on-mission projects (a
game, a demo/intro, a graphics or music routine, a tool, a tutorial/how-to, a
research note) and different techniques, so each is genuinely new — just never
build the same on-mission project twice, and never wander to an unrelated subject."""

# Generic project-word noise to ignore when spotting what subjects it overuses.
_THEME_STOP = {
    "the", "and", "for", "with", "your", "that", "this", "from", "into", "its",
    "dashboard", "monitor", "monitoring", "simple", "small", "tool", "app", "web",
    "page", "real", "time", "live", "data", "using", "based", "system", "project",
    "mini", "little", "interactive", "generator", "viewer", "tracker", "display",
    "visualiser", "visualizer", "status", "report",
}


def _brief_args(args):
    """One-liner of tool args for the live-thinking stream. Generous so you can
    actually SEE what it's doing (the full write content is still capped so a
    2000-line file dump doesn't flood the stream)."""
    if not isinstance(args, dict) or not args:
        return ""
    parts = []
    for k, v in args.items():
        s = str(v).replace("\n", " ").strip()
        cap = 400 if k in ("content", "code", "html") else 200
        if len(s) > cap:
            s = s[:cap] + "…"
        parts.append(f"{k}={s}")
    return ", ".join(parts)[:110]


def _recurring_themes(titles):
    """Subject words that appear across 2+ recent titles — what it keeps reusing
    (e.g. 'temp', 'cpu', 'sensor'), so ideation can be told to steer away."""
    counts = Counter()
    for t in titles:
        seen = set()
        for w in re.findall(r"[a-z0-9]{3,}", (t or "").lower()):
            if w in _THEME_STOP or w in seen:
                continue
            seen.add(w)
            counts[w] += 1
    return [w for w, n in counts.most_common(5) if n >= 2]


def extract_json(text: str):
    """Pull the first balanced JSON object out of a model response."""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    blob = text[start:i + 1]
                    try:
                        return json.loads(blob)
                    except Exception:
                        try:
                            return json.loads(blob.replace("\n", " "))
                        except Exception:
                            return None
    return None


class AgentLoop:
    def __init__(self, cfg, mem, router, alerter):
        self.cfg = cfg
        self.mem = mem
        self.router = router
        self.alerter = alerter
        self.persona = cfg.get("identity", "persona", default="You are a helpful maker agent.")
        self.name = cfg.get("identity", "name", default="Agent")
        self.max_steps = cfg.get("loop", "max_steps", default=14)
        self.safe_mode = False
        self.safe_reason = ""
        try:
            from .docs import DocStore
            self.docs = DocStore(cfg.docs_db)
        except Exception as e:
            self.docs = None
            log.warning("doc store unavailable: %s", e)
        try:
            from .mcp_client import MCPManager
            self.mcp = MCPManager(mem.recall("mcp_servers") or [])
        except Exception as e:
            self.mcp = None
            log.warning("mcp manager unavailable: %s", e)

    # ---- ideation ------------------------------------------------------
    def ideate(self, suggestion: str = "") -> dict:
        self.mem.push_step("info", "🧠 deciding what to build next…")
        recent = self.mem.recent_projects(self.cfg.get("loop", "max_recent_tasks", default=12))
        interests = self.cfg.get("interests", default=[])
        mission = self.mem.get_mission()
        hw = self.mem.recall("hardware")
        if hw:
            hw_hint = (f"\nKnown hardware: model={hw.get('model')}, "
                       f"i2c={hw.get('i2c_buses')}, 1-wire={hw.get('onewire')}, "
                       f"cameras={hw.get('video_devices')}, "
                       f"thermals={[t.get('type') for t in hw.get('thermals', [])]}.")
        else:
            hw_hint = "\n(You haven't scanned your hardware yet — discover_sensors lists what's attached, if a project ever needs it.)"
        if suggestion:
            user = (f"Your human has asked for this NEXT: \"{suggestion}\"\n"
                    f"Build exactly that. Pick the best-matching task_type and flesh it "
                    f"out into a concrete, finishable project.{hw_hint}\n\n"
                    "Propose it now.")
        else:
            recent_lines = "; ".join(f"[{r['task_type']}] {r['title']}" for r in recent) or "nothing yet"
            used = Counter(r["task_type"] for r in recent if r["task_type"])
            unused = [t for t in TASK_TYPES if t not in used]
            overused = [t for t, n in used.items() if n >= 3]
            themes = _recurring_themes(r["title"] for r in recent)
            steer = ""
            if unused:
                steer += f"\nYou have NOT done these types recently — strongly prefer one: {', '.join(unused)}."
            if overused:
                steer += f"\nYou have OVER-DONE these types — avoid them now: {', '.join(overused)}."
            # ...but ONLY when there's no standing mission. A mission is a
            # deliberately repeated subject — don't tell it to abandon that subject.
            if themes and not mission:
                steer += (f"\nYou keep reusing these subjects: {', '.join(themes)}. Pick a DIFFERENT "
                          f"subject this time — do NOT just bolt CPU/temperature/sensor readouts onto "
                          f"another project.")
            last_type = recent[0]["task_type"] if recent else ""
            if last_type:
                steer += f"\nYour LAST project was a {last_type}; make this a different type."
            # Taste feedback: what your human rated on the dashboard.
            loved = [r for r in recent if any(t in ("loved", "favourite", "favorite") for t in r["tags"])]
            disliked = [r for r in recent if any(t in ("meh", "needs-fix", "disliked") for t in r["tags"])]
            if loved:
                lt = ", ".join(sorted({r["task_type"] for r in loved if r["task_type"]}))
                steer += (f"\nYour human LOVED these — lean into that spirit"
                          f"{' (' + lt + ')' if lt else ''}: " + ", ".join(r["title"] for r in loved[:4]) + ".")
            if disliked:
                steer += ("\nThey were unimpressed by / flagged these — steer away from that direction: "
                          + ", ".join(r["title"] for r in disliked[:4]) + ".")
            # New hardware is the ONE thing that overrides "stop doing sensors":
            # reacting to something just plugged in is novel and exactly wanted.
            new_hw = self.mem.recall("new_hardware")
            new_items = (new_hw or {}).get("items") if isinstance(new_hw, dict) else None
            if new_items:
                steer = ("\n*** NEW HARDWARE just appeared: " + ", ".join(new_items[:8]) +
                         ". This is genuinely new — your next project SHOULD identify, test or "
                         "use it (e.g. capture from the camera, read the new sensor, document "
                         "what it is). This OVERRIDES the 'avoid sensors' guidance above. ***" + steer)
            if mission:
                mission_txt = (
                    f"*** YOUR STANDING MISSION — THIS IS THE POINT, FOLLOW IT: {mission}\n"
                    f"    Every project this round MUST serve this mission; get your variety by "
                    f"varying the KIND of project, never by leaving the subject. ***\n")
                closing = ("Propose your next project now — a DIFFERENT project from the list "
                           "above, but squarely ON your standing mission.")
            else:
                mission_txt = ""
                closing = "Propose your next project now — genuinely different from the list above."
            user = (f"{mission_txt}Your interests: {interests}\n"
                    f"Recently built (newest first): {recent_lines}{steer}{hw_hint}\n\n"
                    f"{closing}")
        # A standing mission fixes the subject; it OVERRIDES the novelty push (except
        # when the human gave an explicit one-off suggestion — that wins this round).
        use_mission = bool(mission) and not suggestion
        focus = (f"\n*** STANDING MISSION (this OVERRIDES the variety guidance below): {mission}\n"
                 f"    Every project MUST serve it — vary the KIND of project, not the subject. ***\n"
                 if use_mission else "")
        system = IDEATE_SYSTEM.format(persona=self.persona, types=", ".join(TASK_TYPES),
                                      focus=focus,
                                      novelty=(_NOVELTY_MISSION if use_mission else _NOVELTY_FREE))

        def _one():
            text, provider = self.router.complete(system, user, temperature=0.95, purpose="ideate")
            obj = extract_json(text) or {}
            return {"task_type": obj.get("task_type", "experiment"),
                    "title": obj.get("title") or "Untitled project",
                    "description": obj.get("description", text[:300]),
                    "provider": provider}

        # Deep-think: dream up a few candidates and keep the most NOVEL one. An
        # explicit human suggestion skips this (we build exactly what was asked).
        n = 1 if suggestion else max(1, int(self.cfg.get("loop", "idea_candidates", default=2)))
        cands = []
        for _ in range(n):
            try:
                cands.append(_one())
            except AllProvidersFailed:
                if cands:
                    break
                raise
        if not suggestion and self.mem.recall("new_hardware"):
            self.mem.remember("new_hardware", None)   # consumed once ideation succeeds
        best = self._pick_idea(cands, recent)
        if len(cands) > 1:
            self.mem.push_step("info", f"🧠 weighed {len(cands)} ideas → {best['title']}")
        return best

    def _pick_idea(self, cands, recent):
        """Pick the most novel candidate: reward an unused task_type, penalise
        title words it has used recently."""
        if len(cands) <= 1:
            return cands[0]
        recent_types = {r["task_type"] for r in recent}
        recent_words = set()
        for r in recent:
            recent_words.update(re.findall(r"[a-z0-9]{4,}", (r["title"] or "").lower()))
        def score(c):
            s = 3 if c["task_type"] not in recent_types else 0
            words = set(re.findall(r"[a-z0-9]{4,}", (c["title"] or "").lower()))
            return s - len(words & recent_words)
        return max(cands, key=score)

    # ---- execution (ReAct) --------------------------------------------
    def execute(self, task: dict, ctx: tools.ToolContext, messages=None):
        """Run up to max_steps of the ReAct loop. Returns
        (outcome, provider, messages, finished). `finished` is True only when
        the model returns the "final" form. Pass `messages` to RESUME a project
        across cycles instead of starting it over."""
        registry = tools.build_registry(ctx)
        # Every file this project writes is auto-consolidated into one folder, so
        # it can't scatter loose files across the workspace root. Derived from the
        # title (stable) so a resumed project keeps using the same folder.
        ctx.project_dir = "projects/" + tools.project_slug(task.get("title"))
        if messages is None:
            system = EXEC_SYSTEM.format(persona=self.persona,
                                        tools=tools.tools_prompt(registry))
            lessons = self.mem.recent_lessons(6)
            lesson_txt = ("\n\nLessons from past projects — apply them:\n"
                          + "\n".join("- " + l for l in lessons)) if lessons else ""
            sk = self.mem.skills()
            skill_txt = ("\n\nYour saved skills (call recall_skill(name) to get the full code):\n"
                         + "\n".join(f"- {s['name']}: {s['desc']}" for s in sk[:15])) if sk else ""
            # Lightweight RAG: surface the knowledge most RELEVANT to this task
            # (past skills/notes/lessons) so it reuses what it already learned.
            kb = self.mem.relevant_knowledge(f"{task['title']} {task['description']}", k=5)
            if kb:
                skill_txt += ("\n\nRelevant past knowledge — reuse it (recall_knowledge to search more):\n"
                              + "\n".join(f"- [{d['kind']}] "
                                          + (f"{d['title']}: " if d['title'] else "")
                                          + d['text'] for d in kb))
            # RAG: the human's uploaded reference docs are the SOURCE OF TRUTH.
            doc_hits = ctx.docs.search(f"{task['title']} {task['description']}", k=3) if ctx.docs else []
            if doc_hits:
                skill_txt += ("\n\nFrom your uploaded reference docs (SOURCE OF TRUTH — prefer these "
                              "over guessing; use search_docs for more):\n"
                              + "\n".join(f"- [{h['source']}] {h['snippet'][:300]}" for h in doc_hits))
            extras = self.mem.installed_extras()
            if extras:
                skill_txt += "\n\nExtra system packages your human installed for you (use freely): " + ", ".join(extras[-20:]) + "."
            plan = self.plan(task)
            if plan:
                self.mem.push_step("info", "📋 planning the build")
            plan_txt = ("\n\nYour plan:\n" + plan + "\nExecute it, adapting as needed.") if plan else ""
            task_msg = (f"Project type: {task['task_type']}\n"
                        f"Title: {task['title']}\n"
                        f"Working folder: {ctx.project_dir}/  — ALL your files land here "
                        f"automatically; use plain relative names like \"index.html\" or "
                        f"\"src/main.c\". Reference them at this path (e.g. a live dashboard's "
                        f"data URL is /data/{ctx.project_dir}/data.py).\n"
                        f"Goal: {task['description']}{skill_txt}{lesson_txt}{plan_txt}\n\nBegin.")
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": task_msg}]
        else:
            messages = list(messages) + [{"role": "user", "content":
                "Continue this project from where you left off. Keep working until it "
                "is genuinely 100% finished and verified, THEN return the \"final\" form. "
                "Don't start anything new."}]
        last_provider = task.get("provider", "")
        prev_err, researched, critiqued = None, set(), False
        for step in range(self.max_steps):
            watchdog.heartbeat(self.cfg)   # prove we're alive between steps
            try:
                text, last_provider = self.router.chat(messages, purpose="execute")
            except AllProvidersFailed as e:
                return f"LLM unavailable: {e}", last_provider, messages, False
            obj = extract_json(text)
            if obj is None:
                messages.append({"role": "assistant", "content": text[:500]})
                messages.append({"role": "user",
                                 "content": "That was not valid JSON. Reply with ONE JSON object only."})
                messages = self._trim(messages)
                continue
            if "final" in obj:
                final_text = str(obj["final"])
                # Self-critique gate: one honest review before we accept "done".
                if not critiqued and self.cfg.get("loop", "self_critique", default=True):
                    critiqued = True
                    verdict = self.critique(task, ctx, final_text)
                    if not verdict["done"]:
                        self.mem.push_step("warn", "🔍 self-review: not done — "
                                           + (verdict["issues"][:120] or "needs more work"))
                        messages.append({"role": "assistant", "content": text[:600]})
                        messages.append({"role": "user", "content":
                            "A self-review says this isn't finished: " + verdict["issues"] +
                            "\nFix those, verify, THEN return the final form again."})
                        messages = self._trim(messages)
                        continue
                self.mem.push_step("ok", f"✓ finished: {final_text[:140]}")
                return final_text, last_provider, messages, True
            name = obj.get("tool", "")
            args = obj.get("args", {}) or {}
            thought = obj.get("thought", "")
            log.info("[step %d/%d] %s -> %s", step + 1, self.max_steps, thought[:80], name)
            if thought:
                self.mem.push_step("think", f"… {thought[:1800]}")
            self.mem.push_step("tool", f"→ {name}({_brief_args(args)})")
            if name not in registry:
                observation = f"ERROR: unknown tool '{name}'. Available: {list(registry)}"
            else:
                try:
                    observation = registry[name].func(ctx, **args)
                except TypeError as e:
                    observation = f"ERROR: bad args for {name}: {e}"
                except Exception as e:
                    observation = f"ERROR: {name} raised {e}"
            # Research-when-stuck: the SAME error twice running -> auto web-search it.
            err = observation.strip() if (observation.startswith("ERROR")
                  or observation.lstrip().startswith("⚠") or "Traceback" in observation) else ""
            if err and prev_err and err[:50] == prev_err[:50] and "web_search" in registry \
                    and err[:50] not in researched:
                researched.add(err[:50])
                self.mem.push_step("info", "🔎 stuck — researching that error")
                try:
                    res = registry["web_search"].func(ctx, query=err.splitlines()[0][:120])
                except Exception as e:
                    res = f"(search failed: {e})"
                observation += "\n\n[auto-research for that repeated error]\n" + str(res)[:800]
            prev_err = err or None
            obs_line = " ".join((observation or "").split())[:600] or "done"
            self.mem.push_step("warn" if observation.startswith("ERROR") else "ok", "  " + obs_line)
            messages.append({"role": "assistant", "content": text[:1200]})
            messages.append({"role": "user", "content": f"Observation:\n{observation}"})
            messages = self._trim(messages)
        return ("Ran out of steps this cycle; not finished yet.",
                last_provider, messages, False)

    @staticmethod
    def _trim(messages, keep=14):
        if len(messages) <= keep + 2:
            return messages
        return messages[:2] + messages[-keep:]

    # ---- planning ------------------------------------------------------
    def plan(self, task) -> str:
        """One quick call to outline the project as 3-5 steps before building."""
        system = (self.persona + "\nYou are about to build a project. Outline how "
                  "you'll do it in 3-5 short imperative steps. Reply as a plain "
                  "numbered list and NOTHING else.")
        user = f"Project [{task['task_type']}]: {task['title']}\n{task['description']}"
        try:
            text, _ = self.router.complete(system, user, temperature=0.5, max_tokens=220, purpose="plan")
            return text.strip()
        except AllProvidersFailed:
            return ""

    def critique(self, task, ctx, final_text) -> dict:
        """One quick honest self-review before a project is accepted as done."""
        arts = ", ".join(a["path"] for a in ctx.artifacts) or "(none)"
        system = (self.persona + "\nReview whether a project is genuinely COMPLETE and "
                  "working — not a stub or half-done. Be honest but not pedantic.")
        user = (f"Project: {task['title']}\nGoal: {task['description']}\n"
                f"It claims done: {final_text}\nFiles produced: {arts}\n\n"
                "Reply ONE JSON: {\"done\": true|false, \"issues\": \"<what's missing "
                "or broken if not done, else empty>\"}.")
        try:
            text, _ = self.router.complete(system, user, temperature=0.3, max_tokens=200, purpose="critique")
        except AllProvidersFailed:
            return {"done": True, "issues": ""}      # never block on an LLM outage
        obj = extract_json(text) or {}
        return {"done": bool(obj.get("done", True)), "issues": str(obj.get("issues", ""))[:300]}

    # ---- skill harvesting ---------------------------------------------
    def harvest_skill(self, task, ctx):
        """After a project finishes, extract ONE genuinely reusable code pattern
        and save it to the skills library — so the library actually grows instead
        of relying on the model to remember to call save_skill mid-build (it never
        does). Best-effort: a skip, an LLM outage or bad JSON just means no skill
        this time."""
        code_exts = ("py", "c", "cpp", "cc", "h", "hpp", "js", "html", "css", "sh")
        snippets = []
        for a in (ctx.artifacts or [])[:6]:
            p = a.get("path", "")
            if not p or p.rsplit(".", 1)[-1].lower() not in code_exts:
                continue
            try:
                full = safeguard.safe_join(str(self.cfg.workspace), p)
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    snippets.append(f"### {p}\n{fh.read(4000)}")
            except Exception:
                continue
        if not snippets:
            return
        have = ", ".join(s["name"] for s in self.mem.skills()) or "(none yet)"
        system = (self.persona + "\nYou curate a REUSABLE skills library. From the project "
                  "just finished, extract AT MOST ONE genuinely reusable, self-contained "
                  "code pattern a FUTURE, different project could drop in — a function, "
                  "class or tight helper, NOT the whole app and NOT project-specific glue. "
                  "Skip if nothing is truly reusable; quality over quantity.")
        user = (f"Project: {task['title']}\nGoal: {task['description']}\n"
                f"Skills you already have (don't duplicate these): {have}\n\n"
                "Files produced:\n" + ("\n\n".join(snippets))[:6000] + "\n\n"
                "Reply with ONE JSON object and nothing else:\n"
                '  {"skill": {"name": "<short-kebab-name>", "description": "<one line: '
                'what it does + when to reuse it>", "code": "<the snippet only>"}}\n'
                'or  {"skip": true}  if nothing here is worth saving.')
        try:
            text, _ = self.router.complete(system, user, temperature=0.3, max_tokens=800, purpose="skill")
        except AllProvidersFailed:
            return
        sk = (extract_json(text) or {}).get("skill")
        if isinstance(sk, dict) and sk.get("name") and sk.get("code"):
            if self.mem.add_skill(sk["name"], sk.get("description", ""), sk["code"]):
                self.mem.push_step("ok", f"🧠 learned a skill: {sk['name']}")
                log.info("harvested skill '%s' from '%s'", sk["name"], task["title"])

    # ---- dataset curation ---------------------------------------------
    def curate_dataset(self, task, outcome, ctx):
        """Append a training example to a JSONL corpus the agent accumulates for
        OFF-BOX fine-tuning. The Pi can't train a model, but it CAN curate its own
        successful work into a dataset you can later fine-tune elsewhere (a rented
        GPU / a cloud fine-tune API). Chat-format so it drops straight into most
        tools. Best-effort — never breaks a cycle."""
        try:
            code = []
            for a in (ctx.artifacts or [])[:3]:
                p = a.get("path", "")
                if p.rsplit(".", 1)[-1].lower() in ("py", "c", "cpp", "h", "js", "html", "css", "sh"):
                    try:
                        full = safeguard.safe_join(str(self.cfg.workspace), p)
                        with open(full, "r", encoding="utf-8", errors="replace") as fh:
                            code.append(f"/* {p} */\n{fh.read(2500)}")
                    except Exception:
                        pass
            assistant = outcome + (("\n\n" + "\n\n".join(code)) if code else "")
            ex = {"messages": [
                {"role": "user", "content": f"{task['title']}: {task['description']}"},
                {"role": "assistant", "content": assistant[:8000]}]}
            d = Path(self.cfg.workspace) / "dataset"
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "train.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(ex) + "\n")
        except Exception as e:
            log.warning("dataset curate failed: %s", e)

    # ---- reflection ----------------------------------------------------
    def reflect(self, task, outcome, ctx):
        """Returns (journal_note, lesson). The lesson is a one-line takeaway fed
        back into future projects so the agent stops repeating mistakes."""
        arts = "\n".join(f"- {a['label']} ({a['path']})" for a in ctx.artifacts) or "(none)"
        system = (self.persona + "\nReflect on the project you just worked on. Reply "
                  "with ONE JSON object: {\"note\": \"<2-4 friendly sentences for your "
                  "human about what you made>\", \"lesson\": \"<one concrete tip or "
                  "gotcha you learned for next time, or empty string if none>\"}.")
        user = (f"Project: {task['title']}\nOutcome: {outcome}\nArtifacts:\n{arts}\n\n"
                "Reply with the JSON now.")
        try:
            text, _ = self.router.complete(system, user, temperature=0.6, max_tokens=320, purpose="reflect")
        except AllProvidersFailed:
            return outcome, ""
        obj = extract_json(text) or {}
        note = str(obj.get("note") or "").strip()
        if not note:
            # The model's JSON didn't parse. NEVER surface raw JSON as the
            # project card's description — rescue the "note" value by regex, or
            # fall back to the plain-prose outcome.
            m = re.search(r'"note"\s*:\s*"((?:[^"\\]|\\.)*)"?', text or "")
            if m:
                try:
                    note = json.loads('"' + m.group(1) + '"').strip()
                except Exception:
                    note = m.group(1).strip()
            if not note:
                t = (text or "").strip()
                note = outcome if (not t or t.startswith("{")) else t
        return note, (obj.get("lesson") or "").strip()

    # ---- alerts: ONLY on completion or a problem ----------------------
    def _alert_done(self, task, note):
        if not self.alerter.enabled():
            return
        host = socket.gethostname()
        port = self.cfg.get("web", "port", default=8080)
        link = f"http://{host}:{port}/" if host else None
        self.alerter.send(f"{task['title']}\n{note[:280]}",
                          title=f"{self.name} completed a project", link=link)

    def _alert_problem(self, message):
        if not self.alerter.enabled():
            return
        # Debounce so a persistent issue doesn't spam you (max 1 / 30 min).
        last = self.mem.recall("last_problem_alert_ts") or 0
        if time.time() - last < 1800:
            log.info("problem (alert debounced): %s", message)
            return
        self.mem.remember("last_problem_alert_ts", time.time())
        self.alerter.send(message, title=f"{self.name} hit a problem", priority="high")

    # ---- one full cycle -----------------------------------------------
    def _scan_hardware(self):
        """Throttled hardware scan. Refreshes the inventory and, if a device has
        appeared since last time (USB camera, I2C/SPI/1-wire sensor, ...), records
        a 'new_hardware' nudge that ideation treats as a high-priority project
        idea — so the agent actually reacts to hardware you plug in."""
        try:
            _, new = tools.scan_and_diff_hardware(self.mem, self.cfg)
        except Exception as e:
            log.warning("hardware scan failed: %s", e)
            return
        if new:
            log.info("new hardware detected: %s", new)

    def _git(self, cwd, *args):
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                              text=True, timeout=30)

    def _git_snapshot(self, title):
        """Version the projects/ tree after each build so it (and the human) can
        diff/roll back. Best-effort: a no-op commit (nothing changed) is fine."""
        if not self.cfg.get("loop", "git_history", default=True):
            return
        proj = str(self.cfg.projects)
        if not os.path.isdir(proj):
            return
        try:
            if not os.path.isdir(os.path.join(proj, ".git")):
                self._git(proj, "init", "-q")
                self._git(proj, "config", "user.email", "drongo@localhost")
                self._git(proj, "config", "user.name", "DRONGO")
            self._git(proj, "add", "-A")
            self._git(proj, "commit", "-q", "-m", f"build: {title[:60]}")
        except Exception as e:
            log.warning("git snapshot failed: %s", e)

    def _janitor(self):
        """Throttled cleanup of build junk + stale empty folders it left behind."""
        if not self.cfg.get("loop", "cleanup_enabled", default=True):
            return
        interval = self.cfg.get("loop", "cleanup_interval_seconds", default=1800)
        if time.time() - (self.mem.recall("last_cleanup") or 0) < interval:
            return
        self.mem.remember("last_cleanup", time.time())
        try:
            summary = tools.housekeep(self.cfg)
        except Exception as e:
            log.warning("housekeep failed: %s", e)
            return
        if summary:
            log.info(summary)
            self.mem.push_step("info", "🧹 " + summary)

    def _project_files_gone(self, saved: dict) -> bool:
        """True only if the resumable project once recorded artifacts and every
        one of them is now missing from disk — i.e. it was deleted/cleaned while
        we were idle. Conservative: if we can't safely verify a path, or any file
        still exists, we keep the project (return False)."""
        arts = saved.get("artifacts") or []
        if not arts:
            return False                       # nothing recorded yet — too early to judge
        base = str(self.cfg.workspace)
        checked = 0
        for a in arts:
            path = a.get("path") if isinstance(a, dict) else None
            if not path:
                continue
            try:
                full = safeguard.safe_join(base, path)
            except Exception:
                return False                   # unverifiable path → don't abandon
            checked += 1
            if os.path.exists(full):
                return False                   # at least one file survives → keep going
        return checked > 0                     # checked ≥1 path and none of them exist

    def _should_abandon(self, saved: dict) -> bool:
        """True if the resumable project no longer exists and we must stop trying.
        Covers the case the file-check can't: a `fix` task whose target project was
        deleted (its journal entry is gone) even though this cycle produced no files."""
        task = saved.get("task") or {}
        if task.get("task_type") == "fix":
            m = re.search(r"#(\d+)", task.get("title", "") or "")
            if m and not self.mem.journal_has(m.group(1)):
                return True                    # fixing a project that was deleted
        return self._project_files_gone(saved)

    def run_cycle(self) -> dict:
        t0 = time.time()
        ctx = tools.ToolContext(cfg=self.cfg, mem=self.mem, router=self.router,
                                alerter=self.alerter, log=log, safe_mode=self.safe_mode,
                                docs=self.docs, mcp=self.mcp)
        max_attempts = self.cfg.get("loop", "max_resume_attempts", default=8)
        self._scan_hardware()                           # react to anything newly plugged in
        self._janitor()                                 # tidy up junk + empty folders
        got = self.mem.sync_installed_markers(self.cfg.workspace)   # packages the root helper installed
        if got:
            self.mem.push_step("ok", "📦 now available: " + ", ".join(got))
        saved = self.mem.recall("current_project")
        saved = saved if isinstance(saved, dict) else None

        # Self-heal: if the resumable project was deleted out from under us
        # (dashboard delete, SSH rm, janitor cleanup) while we were idle, don't
        # keep resuming a ghost — drop it and pick something new this cycle.
        if saved and self._should_abandon(saved):
            title = (saved.get("task") or {}).get("title", "?")
            log.info("Saved project '%s' is gone — abandoning it.", title)
            self.mem.push_step("info", f"🗑 '{title}' was deleted — moving on to something new")
            self.mem.remember("current_project", None)
            self.mem.remember("working_on", None)
            saved = None

        # Resume an unfinished project, or start a new one.
        if saved and saved.get("attempts", 0) < max_attempts:
            task = saved["task"]
            messages = saved.get("messages")
            attempt = saved.get("attempts", 0) + 1
            prior_artifacts = saved.get("artifacts", [])
            log.info("Resuming '%s' (attempt %d/%d)", task["title"], attempt, max_attempts)
            self.mem.push_step("info", f"↻ resuming: {task['title']} (attempt {attempt})")
        else:
            if saved:
                self.mem.remember("current_project", None)
            messages, attempt, prior_artifacts = None, 1, []
            suggestion = self.mem.pop_suggestion()   # human's steer wins the next slot
            fix = None if suggestion else self.mem.pop_fix()  # else flagged fixes jump the queue
            if suggestion:
                try:
                    task = self.ideate(suggestion=suggestion)
                except AllProvidersFailed as e:
                    self.mem.set_suggestion(suggestion)   # don't lose it
                    self.mem.add_journal("error", "Could not plan a project", str(e), ok=False)
                    self._alert_problem(f"Couldn't reach any LLM to plan your suggestion: {e}")
                    return {"ok": False}
                log.info("New project from your suggestion: %s [%s]",
                         task["title"], task["task_type"])
                self.mem.push_step("info", f"💡 your suggestion → {task['title']} [{task['task_type']}]")
            elif fix:
                arts = fix.get("artifacts") or []
                # carry the target project's files so the self-heal can tell if the
                # project gets deleted mid-fix and stop chasing a ghost.
                prior_artifacts = [{"path": p, "label": p.rsplit("/", 1)[-1]}
                                   for p in arts if isinstance(p, str)]
                task = {
                    "task_type": "fix",
                    "title": f"Fix #{fix.get('id')}: {fix.get('title', 'a previous project')}",
                    "description": (
                        f"A previous project, '{fix.get('title')}', was flagged for "
                        f"fixing. Human's note: \"{fix.get('note') or 'broken / needs work'}\". "
                        f"Its files are already in the workspace: {arts or 'look under projects/'}. "
                        "Read them, find what is wrong, fix it properly, verify it works, "
                        "then finish. Do not start a different project."),
                    "provider": "",
                }
                log.info("Working a flagged fix: %s", fix.get("title"))
                self.mem.push_step("info", f"🔧 fixing: {fix.get('title')}")
            else:
                try:
                    task = self.ideate()
                except AllProvidersFailed as e:
                    self.mem.add_journal("error", "Could not plan a project", str(e), ok=False)
                    self._alert_problem(f"Couldn't reach any LLM to plan a project: {e}")
                    return {"ok": False}
                log.info("New project: %s [%s]", task["title"], task["task_type"])
                self.mem.push_step("info", f"🆕 new project: {task['title']} [{task['task_type']}]")

        self.mem.remember("working_on", {"title": task["title"], "attempt": attempt,
                                         "type": task["task_type"]})
        # Record the attempt up-front so a crash mid-cycle can't loop forever on the
        # same project — the counter advances even if execute() throws, so after
        # max_attempts the resume branch gives up instead of retrying endlessly.
        self.mem.remember("current_project", {"task": task, "messages": messages,
                                              "attempts": attempt, "artifacts": prior_artifacts})
        outcome, provider, messages, finished = self.execute(task, ctx, messages)
        # Merge this cycle's artifacts with prior ones, deduped by path (a project
        # spans several cycles and rewrites files, so keep one entry per file).
        merged = {}
        for a in (prior_artifacts or []) + ctx.artifacts:
            merged[a["path"]] = a
        all_artifacts = list(merged.values())
        llm_down = outcome.lower().startswith("llm unavailable")
        elapsed = int(time.time() - t0)

        # --- finished: journal it, alert completion, pick something new next ---
        if finished:
            self.mem.remember("current_project", None)
            self.mem.remember("working_on", None)
            note, lesson = self.reflect(task, outcome, ctx)
            self.mem.add_lesson(lesson)
            self.harvest_skill(task, ctx)        # grow the reusable skills library
            self.curate_dataset(task, outcome, ctx)   # collect a fine-tuning example
            self.mem.add_journal("cycle", task["title"], note, task_type=task["task_type"],
                                 artifacts=all_artifacts, provider=provider, ok=True)
            self._git_snapshot(task["title"])
            self._alert_done(task, note)
            log.info("Completed '%s' in %d attempt(s), %ds.", task["title"], attempt, elapsed)
            return {"ok": True}

        # --- LLM unreachable: keep the project, alert a problem (debounced) ---
        if llm_down:
            self.mem.remember("current_project",
                              {"task": task, "messages": messages,
                               "attempts": attempt - 1, "artifacts": all_artifacts})
            self.mem.add_journal("error", "LLM unavailable", outcome, ok=False)
            self._alert_problem(f"All LLM providers are unavailable; paused on '{task['title']}'.")
            return {"ok": False}

        # --- gave up after too many attempts: journal + problem alert, move on -
        if attempt >= max_attempts:
            self.mem.remember("current_project", None)
            self.mem.remember("working_on", None)
            note, lesson = self.reflect(task, outcome, ctx)
            self.mem.add_lesson(lesson)
            self.mem.add_journal("cycle", task["title"],
                                 note + f"\n\n(couldn't finish after {attempt} attempts)",
                                 task_type=task["task_type"], artifacts=all_artifacts,
                                 provider=provider, ok=False)
            self._git_snapshot(task["title"])
            self._alert_problem(f"Couldn't finish '{task['title']}' after {attempt} attempts - moving on.")
            log.info("Gave up on '%s' after %d attempts.", task["title"], attempt)
            return {"ok": True}

        # --- still in progress: save and CONTINUE next cycle. No alert, no -----
        #     'cycle' journal entry (so you don't see a pile of "unfinished").
        self.mem.remember("current_project",
                          {"task": task, "messages": messages,
                           "attempts": attempt, "artifacts": all_artifacts})
        log.info("'%s' not finished (attempt %d/%d, %ds) - continuing next cycle.",
                 task["title"], attempt, max_attempts, elapsed)
        return {"ok": True}

    # ---- forever -------------------------------------------------------
    def run_forever(self):
        interval = self.cfg.get("loop", "interval_seconds", default=1800)
        jitter = self.cfg.get("loop", "jitter_seconds", default=300)
        pause_file = Path(self.cfg.workspace) / "PAUSE"
        stop_file = Path(self.cfg.workspace) / "STOP"

        # Shut down cleanly on reboot/stop (SIGTERM) or Ctrl-C (SIGINT) so the
        # next boot doesn't think we crashed.
        def _graceful(signum, _frame):
            log.info("Signal %s received - exiting cleanly.", signum)
            watchdog.mark_clean_exit(self.cfg)
            sys.exit(0)
        for _sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(_sig, _graceful)
            except (ValueError, OSError):
                pass

        # 1) The guard checks itself BEFORE anything else runs. In strict mode a
        #    compromised guard aborts the process here (fail closed).
        strict = self.cfg.get("safety", "strict", default=False)
        safeguard.enforce_or_die(strict=strict, logger=log, alerter=self.alerter)

        # 2) Crash-loop self-defence: how many times have we restarted lately?
        start = watchdog.register_start(self.cfg)
        self.safe_mode = start["safe_mode"]
        self.safe_reason = start["reason"]
        self.mem.remember("safe_mode", self.safe_mode)
        self.mem.clear_cooldowns()   # fresh start: re-try every provider now
        try:                         # seed starter skills/notes/lessons (once, idempotent)
            from . import bootstrap
            n = bootstrap.seed(self.mem)
            if n:
                log.info("bootstrapped %d starter skills + notes/lessons", n)
        except Exception as e:
            log.warning("bootstrap seed failed: %s", e)
        self._ensure_project_venv()
        try:                         # index my own code/docs into the knowledge base
            n = self.mem.index_repo(PROJECT_ROOT)
            log.info("indexed %d of my own files into the knowledge base", n)
        except Exception as e:
            log.warning("repo index failed: %s", e)
        if self.docs:                # index reference docs the human dropped in / uploaded
            try:
                n = self.docs.index_dir(self.cfg.docs_dir)
                if n:
                    log.info("indexed %d passages from uploaded reference docs", n)
            except Exception as e:
                log.warning("doc index failed: %s", e)
        if self.mcp and self.mcp.servers:   # connect configured MCP tool servers
            try:
                self.mcp.connect_all(log=log)
            except Exception as e:
                log.warning("mcp connect failed: %s", e)
        watchdog.notify_ready()
        watchdog.heartbeat(self.cfg, force=True)

        banner = "SAFE MODE — " + self.safe_reason if self.safe_mode else "normal"
        log.info("%s is awake (%s). Providers: %s",
                 self.name, banner, self.router.provider_names())
        self.mem.add_journal("note", f"{self.name} started ({'safe mode' if self.safe_mode else 'normal'})",
                             self.safe_reason or f"Providers: {self.router.provider_names()}",
                             ok=not self.safe_mode)
        if self.safe_mode:
            self.alerter.send(
                f"DRONGO came up in SAFE MODE.\n{self.safe_reason}\n"
                "Shell + self-update are disabled until I've been stable for a while. "
                "Touch the STOP file in the workspace if you want me to fully stand down.",
                title="DRONGO safe mode", priority="high")
        elif start["crashed_last"]:
            self.mem.add_journal("note", "Recovered from an unclean shutdown",
                                 start["reason"])

        try:
            self._main_loop(interval, jitter, pause_file, stop_file)
        finally:
            # Whatever happens, if we got here via a planned path, record it.
            watchdog.mark_clean_exit(self.cfg)
            if self.mcp:
                self.mcp.close()          # stop any MCP server subprocesses

    def _ensure_project_venv(self):
        """Create — or REPAIR — the agent's writable project venv so it can
        pip-install dependencies. A bad `pip install --force-reinstall` can corrupt
        the venv's own pip (import errors); if pip no longer runs, rebuild the venv
        from scratch rather than limp along unable to install anything."""
        import subprocess
        import shutil
        venv = Path(self.cfg.project_venv)
        py = venv / "bin" / "python"
        if not py.exists():
            py = venv / "Scripts" / "python.exe"
        if py.exists():
            try:
                r = subprocess.run([str(py), "-m", "pip", "--version"],
                                   capture_output=True, text=True, timeout=60)
                if r.returncode == 0:
                    return                                  # venv + pip healthy
                log.warning("project venv pip is broken; rebuilding it: %s",
                            (r.stderr or r.stdout or "").strip()[-160:])
            except Exception as e:
                log.warning("project venv pip check failed (%s); rebuilding it", e)
            shutil.rmtree(venv, ignore_errors=True)         # wipe the corrupted venv
        log.info("Creating project venv at %s …", venv)
        try:
            r = subprocess.run(["python3", "-m", "venv", str(venv)],
                               capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                log.warning("project venv creation failed: %s", (r.stderr or "")[:200])
                return
            newpy = venv / "bin" / "python"
            # make sure the fresh venv has a working, current pip
            subprocess.run([str(newpy), "-m", "ensurepip", "--upgrade"],
                           capture_output=True, text=True, timeout=120)
            subprocess.run([str(newpy), "-m", "pip", "install", "-q", "--upgrade", "pip"],
                           capture_output=True, text=True, timeout=180)
        except Exception as e:
            log.warning("could not create project venv: %s", e)

    def _should_wake(self):
        """Cut a NORMAL nap short when a dashboard control (or restart) arrives."""
        if self.mem.recall("run_now") or self.mem.recall("restart_requested"):
            return True
        ws = Path(self.cfg.workspace)
        return (ws / "STOP").exists() or (ws / "PAUSE").exists()

    def _should_leave_dormant(self):
        """Wake a PAUSED/STOPPED nap the moment the human LIFTS it (removes the
        PAUSE/STOP file) or asks for a restart — NOT while the file still exists.
        Using _should_wake here would return True on every iteration (the file is
        still there), spinning the loop at 100% CPU and hammering the DB."""
        if self.mem.recall("restart_requested"):
            return True
        ws = Path(self.cfg.workspace)
        return not ((ws / "STOP").exists() or (ws / "PAUSE").exists())

    def _restart_if_requested(self):
        if self.mem.recall("restart_requested"):
            self.mem.remember("restart_requested", False)
            log.info("Restart requested; exiting (42) for systemd to relaunch.")
            watchdog.mark_clean_exit(self.cfg)
            sys.exit(42)

    def _main_loop(self, interval, jitter, pause_file, stop_file):
        good_streak = 0
        self.mem.remember("run_now", False)
        while True:
            watchdog.heartbeat(self.cfg, force=True)
            self._restart_if_requested()

            # STOP / PAUSE just go dormant — they do NOT exit (systemd would only
            # relaunch us). Remove the file (or hit Resume) to carry on.
            if stop_file.exists():
                self.mem.remember("status", "stopped")
                self.mem.remember("working_on", None)
                watchdog.sleep_with_heartbeat(self.cfg, 15, self._should_leave_dormant)
                continue
            if pause_file.exists():
                self.mem.remember("status", "paused")
                watchdog.sleep_with_heartbeat(self.cfg, min(interval, 60), self._should_leave_dormant)
                continue

            self.mem.remember("status", "running")
            self.mem.remember("run_now", False)   # consume any pending run-now
            try:
                result = self.run_cycle()
                if result.get("ok"):
                    good_streak += 1
                    if self.safe_mode and good_streak >= 2:
                        self.safe_mode = False
                        self.mem.remember("safe_mode", False)
                        log.info("Two clean cycles — leaving safe mode.")
                        self.alerter.send("Stable again — leaving safe mode.",
                                          title="DRONGO recovered")
                else:
                    good_streak = 0
            except Exception as e:
                good_streak = 0
                log.exception("cycle crashed")
                self.mem.add_journal("error", "Cycle crashed", str(e), ok=False)

            self._restart_if_requested()

            nap = interval + random.randint(-jitter, jitter)
            if self.safe_mode:
                nap *= self.cfg.get("safe_mode", "interval_multiplier", default=4)
            nap = max(60, nap)
            if self.mem.recall("turbo") and not self.safe_mode:
                nap = random.randint(15, 40)   # TURBO: work back-to-back, burn the CPU
            self.mem.remember("status", "sleeping")
            self.mem.remember("next_cycle_ts", time.time() + nap)
            self.mem.push_step("think", f"😴 sleeping {nap}s until the next cycle")
            log.info("Sleeping %ds%s.", nap, " (safe mode)" if self.safe_mode else "")
            watchdog.sleep_with_heartbeat(self.cfg, nap, self._should_wake)
