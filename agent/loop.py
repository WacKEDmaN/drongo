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
- DOCUMENT every project: alongside the code write a short README.md (in the same
  projects/ folder) covering what it is, how to run it (exact command), what it
  needs (dependencies), and how to use it. Keep code commented where it helps.
- You have a writable Python environment: install dependencies with
  `pip install <package>` and run code with `python <file>` (both already point at
  your project venv). Prefer the standard library, but install what you need.
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
Avoid repeating recent projects. Prefer variety across games, images, scripts,
hardware dashboards, and short research notes.

Reply with ONE JSON object only:
  {{"task_type": "<one of: {types}>", "title": "<short title>", "description": "<what to build and why, 1-3 sentences>"}}
"""


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
    def ideate(self) -> dict:
        recent = self.mem.recent_task_titles(self.cfg.get("loop", "max_recent_tasks", default=12))
        interests = self.cfg.get("interests", default=[])
        hw = self.mem.recall("hardware")
        hw_hint = ""
        if hw:
            hw_hint = (f"\nKnown hardware: model={hw.get('model')}, "
                       f"i2c={hw.get('i2c_buses')}, 1-wire={hw.get('onewire')}, "
                       f"cameras={hw.get('video_devices')}, "
                       f"thermals={[t.get('type') for t in hw.get('thermals', [])]}.")
        else:
            hw_hint = "\nYou have not scanned the hardware yet — a sensor dashboard could start with discover_sensors."
        user = (f"Your interests: {interests}\n"
                f"Recent projects (don't repeat): {recent}{hw_hint}\n\n"
                "Propose your next project now.")
        system = IDEATE_SYSTEM.format(persona=self.persona, types=", ".join(TASK_TYPES))
        text, provider = self.router.complete(system, user, temperature=0.9)
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
    def run_cycle(self) -> dict:
        t0 = time.time()
        ctx = tools.ToolContext(cfg=self.cfg, mem=self.mem, router=self.router,
                                alerter=self.alerter, log=log, safe_mode=self.safe_mode)
        max_attempts = self.cfg.get("loop", "max_resume_attempts", default=8)
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
            fix = self.mem.pop_fix()      # flagged-broken projects jump the queue
            if fix:
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
        all_artifacts = (prior_artifacts or []) + ctx.artifacts
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
