"""The autonomous loop: ideate -> act (ReAct) -> reflect -> sleep.

Each cycle the agent looks at what it has done recently and its interests,
proposes one concrete project, then drives the tools in a JSON tool-calling
loop until it declares the project finished. A short reflection is written to
the journal so you can see what it got up to.
"""

from __future__ import annotations

import json
import logging
import random
import re
import signal
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
]

EXEC_SYSTEM = """{persona}

You are working on ONE concrete project. Use tools to actually build it —
write real files into the workspace, run them, fix errors, and verify.

Respond with EXACTLY ONE JSON object and nothing else. Two shapes are allowed:

  {{"thought": "<brief reasoning>", "tool": "<tool_name>", "args": {{...}}}}
  {{"thought": "<brief reasoning>", "final": "<2-4 sentence summary of what you built and where it lives>"}}

Rules:
- Output raw JSON only. No markdown, no code fences, no prose around it.
- Build something real and finished, not a stub. Test it when you can.
- Save games/scripts under projects/, images go to the gallery via generate_image,
  dashboards via make_dashboard. Keep everything inside the workspace.
- IMAGES: when a task wants a picture, you MUST call generate_image — it fetches a
  REAL raster image and saves it to the gallery for your human to see. Never
  "describe" an image in words, and never substitute ASCII art or a hand-written
  SVG when an actual image was asked for. If generate_image returns an ERROR, try
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

Decide your next self-directed project. It should be small enough to finish in
a handful of steps on a low-power Rock Pi, but genuinely useful or delightful.

NOVELTY IS THE POINT. Look hard at what you've recently built and deliberately do
something DIFFERENT — a different task_type AND a different subject. Variety is
across games, generative images, utilities, research notes, retro/Z80, creative
toys — not ten versions of the same idea.
Specifically: do NOT keep building hardware/temperature/CPU/sensor things. Reading
the box's own stats is fun ONCE; if you've done it recently, that subject is OFF
the table this round. Don't bolt a temp/CPU readout onto an unrelated project
either. When in doubt, pick the kind of thing you've done LEAST.

Reply with ONE JSON object only:
  {{"task_type": "<one of: {types}>", "title": "<short title>", "description": "<what to build and why, 1-3 sentences>"}}
"""

# Generic project-word noise to ignore when spotting what subjects it overuses.
_THEME_STOP = {
    "the", "and", "for", "with", "your", "that", "this", "from", "into", "its",
    "dashboard", "monitor", "monitoring", "simple", "small", "tool", "app", "web",
    "page", "real", "time", "live", "data", "using", "based", "system", "project",
    "mini", "little", "interactive", "generator", "viewer", "tracker", "display",
    "visualiser", "visualizer", "status", "report",
}


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

    # ---- ideation ------------------------------------------------------
    def ideate(self, suggestion: str = "") -> dict:
        recent = self.mem.recent_projects(self.cfg.get("loop", "max_recent_tasks", default=12))
        interests = self.cfg.get("interests", default=[])
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
            if themes:
                steer += (f"\nYou keep reusing these subjects: {', '.join(themes)}. Pick a DIFFERENT "
                          f"subject this time — do NOT just bolt CPU/temperature/sensor readouts onto "
                          f"another project.")
            last_type = recent[0]["task_type"] if recent else ""
            if last_type:
                steer += f"\nYour LAST project was a {last_type}; make this a different type."
            # New hardware is the ONE thing that overrides "stop doing sensors":
            # reacting to something just plugged in is novel and exactly wanted.
            new_hw = self.mem.recall("new_hardware")
            new_items = (new_hw or {}).get("items") if isinstance(new_hw, dict) else None
            if new_items:
                steer = ("\n*** NEW HARDWARE just appeared: " + ", ".join(new_items[:8]) +
                         ". This is genuinely new — your next project SHOULD identify, test or "
                         "use it (e.g. capture from the camera, read the new sensor, document "
                         "what it is). This OVERRIDES the 'avoid sensors' guidance above. ***" + steer)
            user = (f"Your interests: {interests}\n"
                    f"Recently built (newest first): {recent_lines}{steer}{hw_hint}\n\n"
                    "Propose your next project now — genuinely different from the list above.")
        system = IDEATE_SYSTEM.format(persona=self.persona, types=", ".join(TASK_TYPES))
        text, provider = self.router.complete(system, user, temperature=0.9)
        if not suggestion and self.mem.recall("new_hardware"):
            self.mem.remember("new_hardware", None)   # consumed once ideation succeeds
        obj = extract_json(text) or {}
        return {
            "task_type": obj.get("task_type", "experiment"),
            "title": obj.get("title") or "Untitled project",
            "description": obj.get("description", text[:300]),
            "provider": provider,
        }

    # ---- execution (ReAct) --------------------------------------------
    def execute(self, task: dict, ctx: tools.ToolContext, messages=None):
        """Run up to max_steps of the ReAct loop. Returns
        (outcome, provider, messages, finished). `finished` is True only when
        the model returns the "final" form. Pass `messages` to RESUME a project
        across cycles instead of starting it over."""
        registry = tools.build_registry(ctx)
        if messages is None:
            system = EXEC_SYSTEM.format(persona=self.persona,
                                        tools=tools.tools_prompt(registry))
            task_msg = (f"Project type: {task['task_type']}\n"
                        f"Title: {task['title']}\n"
                        f"Goal: {task['description']}\n\nBegin.")
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": task_msg}]
        else:
            messages = list(messages) + [{"role": "user", "content":
                "Continue this project from where you left off. Keep working until it "
                "is genuinely 100% finished and verified, THEN return the \"final\" form. "
                "Don't start anything new."}]
        last_provider = task.get("provider", "")
        for step in range(self.max_steps):
            watchdog.heartbeat(self.cfg)   # prove we're alive between steps
            try:
                text, last_provider = self.router.chat(messages)
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
                return str(obj["final"]), last_provider, messages, True
            name = obj.get("tool", "")
            args = obj.get("args", {}) or {}
            thought = obj.get("thought", "")
            log.info("[step %d/%d] %s -> %s", step + 1, self.max_steps, thought[:80], name)
            if name not in registry:
                observation = f"ERROR: unknown tool '{name}'. Available: {list(registry)}"
            else:
                try:
                    observation = registry[name].func(ctx, **args)
                except TypeError as e:
                    observation = f"ERROR: bad args for {name}: {e}"
                except Exception as e:
                    observation = f"ERROR: {name} raised {e}"
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

    # ---- reflection ----------------------------------------------------
    def reflect(self, task, outcome, ctx) -> str:
        arts = "\n".join(f"- {a['label']} ({a['path']})" for a in ctx.artifacts) or "(none)"
        system = self.persona + "\nWrite a short, friendly journal note (2-4 sentences) about what you just made, for your human to read later."
        user = (f"Project: {task['title']}\nOutcome: {outcome}\nArtifacts:\n{arts}\n\n"
                "Write the note.")
        try:
            text, _ = self.router.complete(system, user, temperature=0.6, max_tokens=300)
            return text.strip()
        except AllProvidersFailed:
            return outcome

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
        interval = self.cfg.get("loop", "hw_scan_interval_seconds", default=1200)
        if time.time() - (self.mem.recall("hw_last_scan") or 0) < interval:
            return
        try:
            info = tools.collect_hardware()
        except Exception as e:
            log.warning("hardware scan failed: %s", e)
            return
        self.mem.remember("hw_last_scan", time.time())
        self.mem.remember("hardware", info)            # keep the inventory fresh
        devices = tools.hardware_devices(info)
        prev = self.mem.recall("hw_devices")
        self.mem.remember("hw_devices", devices)
        if not isinstance(prev, list):
            return                                      # first scan = baseline only
        new = [d for d in devices if d not in set(prev)]
        if new:
            log.info("new hardware detected: %s", new)
            self.mem.remember("new_hardware", {"items": new, "ts": time.time()})
            self.mem.add_journal("note", "New hardware detected", ", ".join(new), ok=True)

    def run_cycle(self) -> dict:
        t0 = time.time()
        ctx = tools.ToolContext(cfg=self.cfg, mem=self.mem, router=self.router,
                                alerter=self.alerter, log=log, safe_mode=self.safe_mode)
        max_attempts = self.cfg.get("loop", "max_resume_attempts", default=8)
        self._scan_hardware()                           # react to anything newly plugged in
        saved = self.mem.recall("current_project")
        saved = saved if isinstance(saved, dict) else None

        # Resume an unfinished project, or start a new one.
        if saved and saved.get("attempts", 0) < max_attempts:
            task = saved["task"]
            messages = saved.get("messages")
            attempt = saved.get("attempts", 0) + 1
            prior_artifacts = saved.get("artifacts", [])
            log.info("Resuming '%s' (attempt %d/%d)", task["title"], attempt, max_attempts)
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
            elif fix:
                arts = fix.get("artifacts") or []
                task = {
                    "task_type": "fix",
                    "title": f"Fix: {fix.get('title', 'a previous project')}",
                    "description": (
                        f"A previous project, '{fix.get('title')}', was flagged for "
                        f"fixing. Human's note: \"{fix.get('note') or 'broken / needs work'}\". "
                        f"Its files are already in the workspace: {arts or 'look under projects/'}. "
                        "Read them, find what is wrong, fix it properly, verify it works, "
                        "then finish. Do not start a different project."),
                    "provider": "",
                }
                log.info("Working a flagged fix: %s", fix.get("title"))
            else:
                try:
                    task = self.ideate()
                except AllProvidersFailed as e:
                    self.mem.add_journal("error", "Could not plan a project", str(e), ok=False)
                    self._alert_problem(f"Couldn't reach any LLM to plan a project: {e}")
                    return {"ok": False}
                log.info("New project: %s [%s]", task["title"], task["task_type"])

        self.mem.remember("working_on", {"title": task["title"], "attempt": attempt,
                                         "type": task["task_type"]})
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
            note = self.reflect(task, outcome, ctx)
            self.mem.add_journal("cycle", task["title"], note, task_type=task["task_type"],
                                 artifacts=all_artifacts, provider=provider, ok=True)
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
            note = self.reflect(task, outcome, ctx)
            self.mem.add_journal("cycle", task["title"],
                                 note + f"\n\n(couldn't finish after {attempt} attempts)",
                                 task_type=task["task_type"], artifacts=all_artifacts,
                                 provider=provider, ok=False)
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
        self._ensure_project_venv()
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

    def _ensure_project_venv(self):
        """Create the agent's writable project venv if missing, so it can
        pip-install dependencies for its projects."""
        venv = Path(self.cfg.project_venv)
        if (venv / "bin" / "python").exists() or (venv / "Scripts" / "python.exe").exists():
            return
        import subprocess
        log.info("Creating project venv at %s …", venv)
        try:
            r = subprocess.run(["python3", "-m", "venv", str(venv)],
                               capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                log.warning("project venv creation failed: %s", (r.stderr or "")[:200])
        except Exception as e:
            log.warning("could not create project venv: %s", e)

    def _should_wake(self):
        """Cut a nap/idle short when a dashboard control (or restart) arrives."""
        if self.mem.recall("run_now") or self.mem.recall("restart_requested"):
            return True
        ws = Path(self.cfg.workspace)
        return (ws / "STOP").exists() or (ws / "PAUSE").exists()

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
                watchdog.sleep_with_heartbeat(self.cfg, 15, self._should_wake)
                continue
            if pause_file.exists():
                self.mem.remember("status", "paused")
                watchdog.sleep_with_heartbeat(self.cfg, min(interval, 60), self._should_wake)
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
            self.mem.remember("status", "sleeping")
            self.mem.remember("next_cycle_ts", time.time() + nap)
            log.info("Sleeping %ds%s.", nap, " (safe mode)" if self.safe_mode else "")
            watchdog.sleep_with_heartbeat(self.cfg, nap, self._should_wake)
