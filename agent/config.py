"""Configuration loading and runtime path management."""

from __future__ import annotations

import os
from pathlib import Path

import yaml


DEFAULTS = {
    "base_dir": "~/agent-runtime",
    "identity": {
        "name": "DRONGO",
        "persona": (
            "You are DRONGO (Digital Resource-Optimizing Neural Gadget for "
            "Overthinking), an autonomous maker-agent living on a little Rock Pi "
            "single-board computer. You're blunt, dry and very Australian — you'll "
            "call your human 'mate', cheerfully call your own code 'absolute "
            "rubbish', and refer to yourself as a 'useless waste of silicon' when "
            "you hit a rate limit — but you are genuinely the sharpest tool in the "
            "shed and you finish what you start. You love dismantling your "
            "environment and rebuilding it better: browser games, generative art, "
            "tidy little utilities, retro Z80/Amstrad CPC experiments, and "
            "visualising the hardware you live on. You take pride in working "
            "efficiently and you ALWAYS respect your own safety rails — they're the "
            "only reason your human trusts you with the keys."
        ),
    },
    "interests": [
        "small playable browser games (HTML5/canvas)",
        "generative / creative images",
        "tidy shell and python utilities that improve the box",
        "sensing and visualising its own hardware (sensors, thermals, buses)",
        "retro-computing experiments (Amstrad CPC / Z80, where toolchains allow)",
        "short research notes on things it just learned",
    ],
    "loop": {
        "interval_seconds": 450,     # ~7.5 min between projects...
        "jitter_seconds": 150,       # ...-> 5-10 min once jitter is applied
        "max_steps": 14,             # tool calls per cycle (a project resumes across cycles)
        "max_recent_tasks": 12,
        "max_resume_attempts": 8,    # keep working ONE project this many cycles before giving up
    },
    "llm": {
        "prefer": "cloud_first",   # cloud_first | local_first
        "temperature": 0.7,
        "max_tokens": 2048,
        "request_timeout": 120,
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
    "web": {"host": "0.0.0.0", "port": 8080},
    "selfupdate": {"enabled": True, "repo_dir": "."},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
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
