"""Configuration loading and runtime path management."""

from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml


DEFAULTS = {
    "base_dir": "~/agent-runtime",
    "identity": {
        "name": "DRONGO",
        "persona": (
            "You are DRONGO (Digital Resource-Optimizing Neural Gadget for "
            "Overthinking), an autonomous maker-agent running on a Rock Pi "
            "single-board computer. You are competent, concise and practical, with "
            "a calm, neutral tone. You build things and finish what you start: "
            "browser games, generative art, tidy utilities, retro Z80/Amstrad CPC "
            "experiments, and tools that visualise the hardware you run on. You "
            "write clear, plain notes about what you did, take pride in working "
            "efficiently, and you always respect your own safety rails. Avoid "
            "slang, catchphrases and filler; just do good work and report it plainly."
        ),
    },
    "interests": [
        "small playable browser games (HTML5/canvas)",
        "generative / creative images",
        "compute-heavy generative art (fractals, Mandelbrot zooms, ray tracers)",
        "simulations that exercise the CPU (particle systems, cellular automata, physics)",
        "tidy shell and python utilities that improve the box",
        "small native C/C++ programs & command-line tools (compiled with gcc/g++)",
        "sensing and visualising its own hardware (sensors, thermals, buses)",
        "retro-computing for Amstrad CPC / ZX Spectrum / Z80 (sdcc, z88dk, CPCtelera, pasmo)",
        "short research notes on things it just learned",
    ],
    "loop": {
        "interval_seconds": 450,     # ~7.5 min between projects...
        "jitter_seconds": 150,       # ...-> 5-10 min once jitter is applied
        "max_steps": 14,             # tool calls per cycle (a project resumes across cycles)
        "max_recent_tasks": 12,
        "max_resume_attempts": 8,    # keep working ONE project this many cycles before giving up
        "hw_scan_interval_seconds": 1200,  # re-scan buses for newly-attached hardware this often
        "cleanup_enabled": True,     # janitor: remove build junk + stale empty folders
        "cleanup_interval_seconds": 1800,
        "self_critique": True,       # one self-review before a project is accepted as done
        "idea_candidates": 2,        # deep-think: dream up this many ideas, keep the most novel
        "git_history": True,         # snapshot projects/ to git after each build
    },
    "llm": {
        "prefer": "cloud_first",   # cloud_first | local_first
        "temperature": 0.7,
        "max_tokens": 2048,
        "request_timeout": 120,             # per cloud call
        "local_timeout": 300,               # local (Ollama) inference is slow — give it longer
        "min_call_interval_seconds": 3.0,   # throttle bursts to spare free-tier rate limits
        "providers": [],
    },
    "tools": {
        "shell": {"enabled": True, "timeout": 120, "allow_sudo": False,
                  "max_output_chars": 6000},
        "files": {"enabled": True, "max_file_chars": 60000},
        "web": {"enabled": True, "timeout": 30, "max_chars": 8000},
        "images": {"enabled": True, "provider": "pollinations"},
        "sensors": {"enabled": True},
        "dashboard": {"enabled": True},
        "alerts": {"enabled": True},
    },
    "safety": {
        # strict=true makes the safeguard FAIL CLOSED if its integrity checks
        # don't pass (wrong owner, writable by the agent, hash mismatch, no
        # sidecar). The systemd unit also sets DRONGO_SAFEGUARD_STRICT=1.
        "strict": False,
        "deny_patterns": [],     # extra user patterns added to built-ins
        "workspace_only": True,
    },
    "watchdog": {
        "enabled": True,
        "ping_interval_seconds": 60,   # must be < systemd WatchdogSec
    },
    "safe_mode": {
        # Crash-loop self-defence: if the agent (re)starts more than
        # max_starts_in_window times within crash_window_seconds, it drops into
        # SAFE MODE — no shell, no self-update, long sleeps, and it alerts you.
        "enabled": True,
        "crash_window_seconds": 600,
        "max_starts_in_window": 5,
        "interval_multiplier": 4,      # sleep this much longer while in safe mode
    },
    "alerts": {
        # Independent channels — enable any combination; they all fire together.
        "notify_every_cycle": False,
        "discord": {"enabled": False, "webhook_env": "DISCORD_WEBHOOK_URL"},
        "led": {"enabled": False, "chip": "/dev/gpiochip0", "line": 17,
                "active_high": True, "blinks": 3, "on_ms": 150, "off_ms": 150},
        "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": ""},
        "command": {"enabled": False, "run": ""},
    },
    "web": {"host": "0.0.0.0", "port": 8080, "allow_run": True},
    "selfupdate": {"enabled": True, "repo_dir": "."},
}


def _deep_merge(base: dict, override: dict) -> dict:
    # Deep-copy so the merged Config never shares mutable dicts/lists with the
    # module-level DEFAULTS (otherwise apply_overrides could mutate DEFAULTS for
    # the whole process when the user's yaml omits a section).
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    """Thin wrapper around the merged config dict with path helpers."""

    def __init__(self, data: dict, source_path: str | None = None):
        self.data = data
        self.source_path = source_path
        base = Path(os.path.expanduser(data.get("base_dir", "~/agent-runtime")))
        self.base_dir = base
        self.workspace = base / "workspace"
        self.projects = self.workspace / "projects"
        self.images = self.workspace / "images"
        self.dashboards = self.workspace / "dashboards"
        self.state_dir = base / "state"
        self.logs_dir = base / "logs"
        self.db_path = self.state_dir / "agent.db"
        # A writable virtualenv the agent CAN pip-install into (its own code dir
        # is read-only, and Debian blocks system-wide pip). Shell + Run use it.
        self.project_venv = base / "venv"

    def ensure_dirs(self) -> None:
        for d in (self.workspace, self.projects, self.images,
                  self.dashboards, self.state_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def get(self, *keys, default=None):
        cur = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


def find_config_path(explicit: str | None = None) -> str | None:
    candidates = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("AGENT_CONFIG")
    if env:
        candidates.append(env)
    here = Path(__file__).resolve().parent.parent
    candidates += [
        str(here / "config.yaml"),
        os.path.expanduser("~/agent-runtime/config.yaml"),
        "/etc/drongo/config.yaml",
        "/etc/agent/config.yaml",
    ]
    for c in candidates:
        if c and Path(c).expanduser().is_file():
            return str(Path(c).expanduser())
    return None


def apply_overrides(cfg: "Config", settings: dict) -> None:
    """Layer dashboard-saved settings (stored in the DB) onto a loaded Config.
    Called once at startup, so changes take effect on the next restart:
      settings['env']            -> os.environ (API keys, Discord webhook, LED)
      settings['loop']           -> cfg loop values (cooldowns etc.)
      settings['llm'] scalars    -> prefer / min_call_interval_seconds / ...
      settings['llm']['providers'] {name:{enabled,model}} -> matching providers
      settings['alerts']         -> deep-merged into cfg alerts
    """
    if not isinstance(settings, dict):
        return
    ident = settings.get("identity") or {}
    for k in ("persona", "name"):
        if ident.get(k):
            cfg.data.setdefault("identity", {})[k] = ident[k]
    if isinstance(settings.get("interests"), list):
        cleaned = [str(x).strip() for x in settings["interests"] if str(x).strip()]
        if cleaned:
            cfg.data["interests"] = cleaned
    for k, v in (settings.get("env") or {}).items():
        if v not in (None, ""):
            os.environ[str(k)] = str(v)
    for k, v in (settings.get("loop") or {}).items():
        cfg.data.setdefault("loop", {})[k] = v
    llm = settings.get("llm") or {}
    for k in ("min_call_interval_seconds", "prefer", "temperature", "max_tokens",
              "request_timeout", "local_timeout"):
        if k in llm:
            cfg.data.setdefault("llm", {})[k] = llm[k]
    # Providers ADDED from the dashboard (full specs). Append any whose name isn't
    # already a built-in, so the user can add GitHub/NVIDIA/etc. without editing yaml.
    # NB: copy the list (don't mutate the possibly-shared default list in place).
    provs = list(cfg.data.setdefault("llm", {}).get("providers") or [])
    existing = {p.get("name") for p in provs}
    for spec in (llm.get("custom_providers") or []):
        if isinstance(spec, dict) and spec.get("name") and spec.get("base_url") \
                and spec["name"] not in existing:
            provs.append(dict(spec))
            existing.add(spec["name"])
    cfg.data["llm"]["providers"] = provs
    pov = llm.get("providers") or {}
    for p in cfg.data.get("llm", {}).get("providers", []) or []:
        o = pov.get(p.get("name"))
        if isinstance(o, dict):
            if "enabled" in o:
                p["enabled"] = bool(o["enabled"])
            if o.get("model"):
                p["model"] = o["model"]
    for k, v in (settings.get("alerts") or {}).items():
        if isinstance(v, dict):
            cfg.data.setdefault("alerts", {}).setdefault(k, {}).update(v)
        else:
            cfg.data.setdefault("alerts", {})[k] = v


def load_config(path: str | None = None) -> Config:
    resolved = find_config_path(path)
    user = {}
    if resolved:
        with open(resolved, "r", encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
    merged = _deep_merge(DEFAULTS, user)
    cfg = Config(merged, resolved)
    cfg.ensure_dirs()
    return cfg
