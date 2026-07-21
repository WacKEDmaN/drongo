"""DRONGO command-line entry point.

    python -m agent run        # the autonomous loop (what systemd runs)
    python -m agent web        # the dashboard
    python -m agent once       # run a single cycle and print the result
    python -m agent discover   # scan + print the hardware inventory
    python -m agent doctor     # check providers, paths and guard integrity
    python -m agent verify     # check safeguard integrity (exit 0 = ok)
    python -m agent seal       # (run as ROOT) write the safeguard .sha256 sidecar
    python -m agent reset      # WIPE ALL PROJECTS + history (keeps your settings)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import safeguard, watchdog
from .alerts import Alerter
from .config import load_config
from .llm import Router
from .loop import AgentLoop
from .memory import Memory


def _setup_logging(cfg, level=logging.INFO, basename="agent"):
    from logging.handlers import RotatingFileHandler
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        # ROTATE — a plain FileHandler grows forever and will eventually fill the
        # SD card and wedge the whole Pi (esp. the dashboard, which logs a line per
        # HTTP request and polls itself constantly). 5MB × 3 = 15MB hard ceiling.
        # The agent and the dashboard log to SEPARATE files so their rotations
        # never race over one inode.
        handlers.append(RotatingFileHandler(
            cfg.logs_dir / f"{basename}.log", maxBytes=5_000_000,
            backupCount=3, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=level, handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # The dashboard polls itself every few seconds; Werkzeug's per-request INFO
    # lines would otherwise swamp the log and journald. Only warnings+ from it.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _build(args):
    cfg = load_config(args.config)
    _setup_logging(cfg, logging.DEBUG if args.verbose else logging.INFO,
                   basename="web" if getattr(args, "cmd", None) == "web" else "agent")
    mem = Memory(cfg.db_path)
    from .config import apply_overrides
    # Dashboard-saved settings are applied best-effort: a corrupt/partial blob
    # must never stop the agent from booting (that would otherwise need a manual
    # DB wipe). On any failure, fall back to the on-disk config for this boot.
    try:
        apply_overrides(cfg, mem.recall("settings") or {})
    except Exception:
        logging.getLogger("agent").exception(
            "apply_overrides failed — ignoring dashboard settings this boot")
    router = Router(cfg, mem)
    alerter = Alerter(cfg)
    return cfg, mem, router, alerter


def cmd_run(args):
    cfg, mem, router, alerter = _build(args)
    AgentLoop(cfg, mem, router, alerter).run_forever()


def cmd_once(args):
    cfg, mem, router, alerter = _build(args)
    safeguard.enforce_or_die(strict=cfg.get("safety", "strict", default=False),
                             logger=logging.getLogger("agent"), alerter=alerter)
    result = AgentLoop(cfg, mem, router, alerter).run_cycle()
    print(json.dumps({k: v for k, v in result.items() if k != "task"}, indent=2, default=str))


def cmd_web(args):
    cfg, mem, _, _ = _build(args)
    from .server import serve
    print(f"Dashboard on http://{cfg.get('web','host')}:{cfg.get('web','port')}/")
    serve(cfg, mem)


def cmd_discover(args):
    cfg, mem, _, _ = _build(args)
    from .tools import collect_hardware
    info = collect_hardware()
    mem.remember("hardware", info)
    print(json.dumps(info, indent=2))


def cmd_doctor(args):
    from .llm import AllProvidersFailed
    cfg, mem, router, alerter = _build(args)
    strict = cfg.get("safety", "strict", default=False)
    issues = []   # plain-English things a human must fix

    # Footgun guard: if you run this from a git clone, `python -m agent` loads
    # the clone's code, NOT the installed agent. Say so loudly.
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir("/opt/drongo") and pkg_root != "/opt/drongo":
        print(f"NOTE: inspecting code at {pkg_root}, not the installed /opt/drongo.")
        print("      Run 'sudo drongo doctor' (or cd /opt/drongo first) for the real agent.\n")

    import subprocess as _sp
    from . import __version__
    try:
        gh = _sp.run(f"git -C '{pkg_root}' rev-parse --short HEAD", shell=True,
                     capture_output=True, text=True, timeout=10).stdout.strip() or "n/a"
    except Exception:
        gh = "n/a"                       # never let doctor hang on a wedged git
    print(f"Version:     {__version__}  (git {gh})  from {pkg_root}")
    print(f"Config:      {cfg.source_path or '(defaults only)'}")
    print(f"Base dir:    {cfg.base_dir}")
    print(f"DB:          {cfg.db_path}")

    provs = router.provider_names()
    print(f"Providers:   {provs or 'NONE'}")
    if not provs:
        issues.append("No usable LLM providers. Add API keys to /etc/drongo/drongo.env, "
                      "or make sure Ollama is running with a model pulled.")

    chans = ", ".join(f"{c.name}{'' if getattr(c, 'usable', False) else ' (not configured)'}"
                      for c in alerter.channels) or "none"
    print(f"Alerts:      {chans}")
    age = watchdog.heartbeat_age(cfg)
    print(f"Heartbeat:   {('%ds ago' % age) if age is not None else 'none yet (agent may not have run)'}")

    ok, problems = safeguard.verify_self(strict=strict)
    st = safeguard.integrity_status()
    print(f"Safeguard:   {st['mode']} owner_uid={st['owner_uid']} "
          f"hash_ok={st['hash_ok']} sidecar={st['sidecar_present']}")
    print(f"  integrity: {'OK' if ok else 'PROBLEMS: ' + '; '.join(problems)}")
    if not ok and strict:
        issues.append("Safeguard integrity check failed (see above). On the Pi this should be "
                      "0444 root-owned + sealed; re-run the installer if it isn't.")

    # Hardware DRONGO can see (kernel + device tree — works headless).
    from .tools import hardware_summary
    hw = hardware_summary()
    print("Hardware:")
    print(f"  model:    {hw['model']}")
    print(f"  thermals: {', '.join(hw['thermals']) or 'none'}")
    print(f"  i2c:      {hw['i2c'] or 'none'}    spi: {hw['spi'] or 'none'}    "
          f"1-wire: {hw['onewire'] or 'none'}")
    print(f"  cameras:  {hw['cameras'] or 'none'}")
    print(f"  gpio:     {', '.join(hw['gpiochips']) or 'none (is gpiod installed + drongo in the gpio group?)'}")

    if not getattr(args, "quick", False):
        print("LLM check:   testing - first run loads the model, can take ~a minute...")
        try:
            text, who = router.complete("You are a connectivity test.",
                                        "Reply with exactly: OK", max_tokens=5, purpose="test")
            print(f"             OK - a model replied via '{who}'")
        except AllProvidersFailed as e:
            print(f"             FAILED - no model answered ({e})")
            issues.append("No LLM answered. Cloud? check keys/network. "
                          "Local? run 'ollama pull <model>' then 'systemctl restart ollama'.")

    print()
    if issues:
        print("VERDICT: NOT READY - fix these:")
        for i in issues:
            print(f"  - {i}")
        sys.exit(1)
    print("VERDICT: READY - DRONGO is good to go.")


def cmd_verify(args):
    cfg, *_ = _build(args)
    ok, problems = safeguard.verify_self(strict=cfg.get("safety", "strict", default=False))
    if ok:
        print("safeguard integrity: OK")
        sys.exit(0)
    print("safeguard integrity: FAILED")
    for p in problems:
        print(f"  - {p}")
    sys.exit(1)


def cmd_configure(args):
    """Friendly interactive setup. Edits only the env files (so your config.yaml
    comments stay intact) and restarts the services. Press Enter to skip items."""
    import shutil
    import subprocess

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("Please run with sudo:  sudo /opt/drongo/configure.sh")
        sys.exit(1)

    etc = os.path.dirname(os.path.abspath(args.config)) if args.config else "/etc/drongo"
    envf = os.path.join(etc, "drongo.env")
    obsf = os.path.join(etc, "observer.env")

    def set_env(path, key, value):
        lines = []
        if os.path.exists(path):
            lines = open(path, encoding="utf-8").read().splitlines()
        out, done = [], False
        for ln in lines:
            if ln.startswith(key + "="):
                out.append(f"{key}={value}"); done = True
            else:
                out.append(ln)
        if not done:
            out.append(f"{key}={value}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")

    def ask(prompt, default=""):
        try:
            return input(prompt).strip() or default
        except EOFError:
            return default

    print("\n=== DRONGO quick setup ===   (press Enter to skip anything)\n")

    wh = ask("Discord webhook URL (Enter to skip): ")
    if wh:
        set_env(envf, "DISCORD_WEBHOOK_URL", wh)
        set_env(obsf, "DRONGO_DISCORD_WEBHOOK", wh)
        print("  -> Discord alerts ON.\n")

    if ask("Set up an LED on a GPIO pin? [y/N]: ").lower().startswith("y"):
        print("\n  Your GPIO chips (each has its own line offsets - see `gpioinfo`):")
        if shutil.which("gpiodetect"):
            subprocess.run("gpiodetect", shell=True)
        chip = ask("  gpiochip [/dev/gpiochip0]: ", "/dev/gpiochip0")
        line = ask("  line offset (a number): ")
        if line.isdigit():
            ah = ask("  is the LED active-high? [Y/n]: ", "y").lower().startswith("y")
            set_env(envf, "DRONGO_LED_CHIP", chip)
            set_env(envf, "DRONGO_LED_LINE", line)
            set_env(envf, "DRONGO_LED_ACTIVE_HIGH", "true" if ah else "false")
            print("  -> LED alerts ON.\n")
        else:
            print("  (not a number — skipping the LED)\n")

    print("Optional free LLM API keys (Enter to skip each; it works without them):")
    for var, where in [("CEREBRAS_API_KEY", "cloud.cerebras.ai"),
                       ("GROQ_API_KEY", "console.groq.com"),
                       ("GEMINI_API_KEY", "aistudio.google.com/apikey"),
                       ("MISTRAL_API_KEY", "console.mistral.ai"),
                       ("OPENROUTER_API_KEY", "openrouter.ai/keys"),
                       ("ANTHROPIC_API_KEY", "console.anthropic.com  (PAID)")]:
        v = ask(f"  {var}  [{where}]: ")
        if v:
            set_env(envf, var, v)

    print("\nWeb search — how it researches online (Enter to skip):")
    print("  Best option is self-hosted SearXNG: open-source, no API key, no cloud.")
    print("  (docker run -d --name searxng -p 8888:8080 searxng/searxng, then enable")
    print("   JSON in its settings.yml — search.formats: [html, json].)")
    sx = ask("  SEARXNG_URL  [e.g. http://127.0.0.1:8888]: ")
    if sx:
        set_env(envf, "SEARXNG_URL", sx.rstrip("/"))
        print("  -> SearXNG web search ON.\n")
    else:
        print("  (or a hosted search key instead — Enter to skip each:)")
        for var, where in [("BRAVE_API_KEY", "brave.com/search/api — 2000 free/mo"),
                           ("TAVILY_API_KEY", "tavily.com"),
                           ("SERPER_API_KEY", "serper.dev")]:
            v = ask(f"    {var}  [{where}]: ")
            if v:
                set_env(envf, var, v)
                break

    os.chmod(envf, 0o600)
    if os.path.exists(obsf):
        os.chmod(obsf, 0o600)

    if shutil.which("systemctl"):
        print("\nApplying (restarting DRONGO)...")
        subprocess.run("systemctl restart drongo drongo-web", shell=True)
    print("\nDone. Re-run anytime:  sudo /opt/drongo/configure.sh\n")


def cmd_reset(args):
    """Factory-reset DRONGO's RUNTIME: delete every project, the whole journal,
    the gallery/dashboards, the fix queue and cooldowns. Your settings (API keys,
    Discord webhook, persona, interests) are KEPT. The code is untouched."""
    import shutil as _sh
    import subprocess as _sp

    cfg = load_config(args.config)
    _setup_logging(cfg)
    mem = Memory(cfg.db_path)

    projects = sorted(p.name for p in cfg.projects.glob("*")) if cfg.projects.exists() else []
    n_journal = mem.count_journal()

    print("\n*** DRONGO RESET — THIS PERMANENTLY WIPES ALL PROJECTS ***\n")
    print(f"  Projects to DELETE ({len(projects)}): " + (", ".join(projects) or "(none)"))
    print(f"  Journal entries to delete:  {n_journal}")
    print("  Also cleared:  dashboards, generated images, fix queue, cooldowns, working state.")
    print("  KEPT:          your settings — API keys, Discord webhook, persona, interests.")
    print("  Your code and OS are untouched.\n")

    if not args.yes:
        try:
            ans = input("This cannot be undone. Type 'wipe' to confirm: ").strip()
        except EOFError:
            ans = ""
        if ans != "wipe":
            print("Aborted — nothing was changed.")
            return

    posix = hasattr(os, "geteuid")
    is_root = posix and os.geteuid() == 0
    # Remember who owns the DB so we can hand everything back after touching it
    # as root (a root-written WAL would otherwise lock the agent out).
    owner = None
    if posix and cfg.db_path.exists():
        st = cfg.db_path.stat()
        owner = (st.st_uid, st.st_gid)

    # Stop the agent so it isn't writing while we wipe (best-effort).
    have_systemctl = bool(_sh.which("systemctl"))
    if have_systemctl:
        _sp.run("systemctl stop drongo drongo-web", shell=True)

    n = mem.reset_runtime(keep_keys=("settings",))

    for d in (cfg.projects, cfg.dashboards, cfg.images):
        if not d.exists():
            continue
        for child in d.iterdir():
            try:
                if child.is_dir() and not child.is_symlink():
                    _sh.rmtree(child)
                else:
                    child.unlink()
            except OSError as e:
                print(f"  ! could not remove {child}: {e}")
    cfg.ensure_dirs()

    # Restore ownership of anything we may have touched as root (DB + WAL/SHM,
    # recreated dirs) so the unprivileged agent can read/write it again.
    if is_root and owner:
        for base, dirs, files in os.walk(cfg.base_dir):
            for nm in (*dirs, *files):
                try:
                    os.chown(os.path.join(base, nm), *owner)
                except OSError:
                    pass
            try:
                os.chown(base, *owner)
            except OSError:
                pass

    if have_systemctl:
        _sp.run("systemctl start drongo drongo-web", shell=True)
        print(f"\nReset complete — wiped {len(projects)} project(s) and {n} journal "
              f"entr{'y' if n == 1 else 'ies'}. DRONGO restarted on a clean slate.")
    else:
        print(f"\nReset complete — wiped {len(projects)} project(s) and {n} journal "
              f"entr{'y' if n == 1 else 'ies'}.")
        print("NOTE: systemctl not found — if the agent is running, restart it to pick "
              "up the clean state.")


def cmd_seal(args):
    # No config/logging needed; just (re)write the sidecar next to safeguard.py.
    digest = safeguard.self_seal()
    print(f"Wrote safeguard.py.sha256 = {digest}")
    print("Now lock it down (run as root):")
    print("  chown root:root agent/safeguard.py agent/safeguard.py.sha256")
    print("  chmod 0444 agent/safeguard.py agent/safeguard.py.sha256")


def main(argv=None):
    # Never let an odd locale (C/POSIX) crash us when printing unicode the LLM
    # produced. Render what we can, replace the rest.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="agent", description="DRONGO autonomous agent")
    p.add_argument("-c", "--config", help="path to config.yaml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    parsers = {}
    for name, fn, help_ in [
        ("run", cmd_run, "run the autonomous loop"),
        ("once", cmd_once, "run a single cycle"),
        ("web", cmd_web, "serve the dashboard"),
        ("discover", cmd_discover, "scan hardware"),
        ("configure", cmd_configure, "interactive setup (alerts + API keys)"),
        ("doctor", cmd_doctor, "diagnostics + READY/NOT-READY verdict"),
        ("verify", cmd_verify, "check safeguard integrity"),
        ("seal", cmd_seal, "write safeguard hash sidecar (root)"),
        ("reset", cmd_reset, "WIPE ALL PROJECTS + history (keeps settings)"),
    ]:
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(func=fn)
        parsers[name] = sp
    parsers["doctor"].add_argument("--quick", action="store_true",
                                   help="skip the live LLM connectivity test")
    parsers["reset"].add_argument("--yes", action="store_true",
                                  help="skip the confirmation prompt (DANGER)")
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
