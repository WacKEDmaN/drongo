"""In-agent watchdog: heartbeats, systemd notify, and crash-loop self-defence.

This is the *innermost* of DRONGO's self-preservation layers. It does three
things, all from inside the agent process:

  * heartbeat()    — touch a file + ping systemd's WATCHDOG so the external
                     observer and systemd can tell "alive" from "wedged".
  * register_start() — on boot, look at how often we've restarted recently. If
                     we're crash-looping, return safe_mode=True so the agent
                     throttles right down instead of thrashing the box.
  * mark_clean_exit() — record that the last shutdown was orderly, so the next
                     boot can distinguish a crash from a planned restart.

The *outer* layers (systemd WatchdogSec, the root observer, the SoC hardware
watchdog) are what actually reboot/rollback; this layer makes the agent a
cooperative, honest participant in that scheme.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from pathlib import Path

log = logging.getLogger("agent.watchdog")

_last_ping = 0.0


def _heartbeat_file(cfg) -> Path:
    return Path(cfg.state_dir) / "heartbeat"


def _starts_file(cfg) -> Path:
    return Path(cfg.state_dir) / "starts.log"


def _clean_exit_file(cfg) -> Path:
    return Path(cfg.state_dir) / "clean_exit"


def sd_notify(state: str) -> bool:
    """Send a datagram to systemd's notify socket (no external deps)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    try:
        if addr.startswith("@"):          # abstract namespace
            addr = "\0" + addr[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.connect(addr)
        sock.sendall(state.encode("utf-8"))
        sock.close()
        return True
    except Exception:
        return False


def notify_ready():
    sd_notify("READY=1")


def heartbeat(cfg, force: bool = False) -> None:
    """Update the heartbeat file and ping systemd's watchdog.

    Called frequently from the main loop and during sleeps. Rate-limited so we
    don't hammer the disk, but always pings systemd when due.
    """
    global _last_ping
    now = time.time()
    interval = cfg.get("watchdog", "ping_interval_seconds", default=60)
    if not force and (now - _last_ping) < min(interval, 30):
        return
    _last_ping = now
    try:
        hb = _heartbeat_file(cfg)
        # Write+rename so the root observer never reads a half-written file.
        tmp = hb.with_name("heartbeat.tmp")
        tmp.write_text(f"{now!r}\n", encoding="utf-8")
        os.replace(tmp, hb)
    except Exception as e:
        log.debug("heartbeat write failed: %s", e)
    sd_notify("WATCHDOG=1")


def heartbeat_age(cfg) -> float | None:
    try:
        return time.time() - float(_heartbeat_file(cfg).read_text().strip())
    except Exception:
        return None


def register_start(cfg) -> dict:
    """Record this boot and decide whether to enter safe mode.

    Returns a dict: {safe_mode: bool, recent_starts: int, crashed_last: bool, reason: str}
    """
    sm_cfg = cfg.get("safe_mode", default={}) or {}
    window = sm_cfg.get("crash_window_seconds", 600)
    max_starts = sm_cfg.get("max_starts_in_window", 5)
    enabled = sm_cfg.get("enabled", True)

    now = time.time()
    sf = _starts_file(cfg)
    starts = []
    if sf.exists():
        try:
            starts = [float(x) for x in sf.read_text().split() if x.strip()]
        except Exception:
            starts = []
    starts = [t for t in starts if now - t < max(window * 4, 3600)]
    starts.append(now)
    try:
        sf.write_text(" ".join(f"{t:.0f}" for t in starts), encoding="utf-8")
    except Exception:
        pass

    recent = sum(1 for t in starts if now - t < window)

    # Was the previous run a clean shutdown?
    ce = _clean_exit_file(cfg)
    crashed_last = not ce.exists()
    try:
        ce.unlink()                       # consume the marker for this run
    except Exception:
        pass

    safe = bool(enabled and recent > max_starts)
    reason = ""
    if safe:
        reason = (f"crash-loop guard: {recent} starts in {window}s "
                  f"(limit {max_starts}) — entering SAFE MODE")
        log.error(reason)
    elif crashed_last:
        reason = "previous run did not exit cleanly (possible crash)"
        log.warning(reason)

    return {"safe_mode": safe, "recent_starts": recent,
            "crashed_last": crashed_last, "reason": reason}


def mark_clean_exit(cfg) -> None:
    """Drop the clean-exit marker so the next boot knows we shut down nicely."""
    try:
        _clean_exit_file(cfg).write_text(f"{time.time():.0f}\n", encoding="utf-8")
    except Exception:
        pass


def sleep_with_heartbeat(cfg, seconds: float) -> None:
    """Sleep in small slices, pinging the watchdog so a long nap isn't a hang."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        heartbeat(cfg, force=True)
        time.sleep(min(30, max(1, deadline - time.time())))
