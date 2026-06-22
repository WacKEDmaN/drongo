"""Outbound notifications - how DRONGO tells you it did something.

Channels are independent and fire together; enable any combination in the
config. No phone required:

  * discord  - POST to a channel webhook (no bot, no account API). Easiest.
  * led      - blink an LED wired to a GPIO pin (ambient, glanceable).
  * ntfy     - optional; has a desktop/web client too.
  * command  - run any command on an alert (power-user escape hatch).

Each channel implements .send(message, title, priority, link) -> bool and is
best-effort: a failure in one never breaks the others or the agent.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

import requests

log = logging.getLogger("agent.alerts")


def _ascii(s: str) -> str:
    # HTTP headers must be latin-1; keep titles safe for ntfy.
    return (s or "").encode("ascii", "replace").decode("ascii")


# ---------------------------------------------------------------------------
class DiscordChannel:
    name = "discord"

    def __init__(self, cfg):
        env = cfg.get("alerts", "discord", "webhook_env", default="DISCORD_WEBHOOK_URL")
        self.webhook = os.environ.get(env, "")
        self.usable = bool(self.webhook)

    def send(self, message, title, priority, link):
        if not self.usable:
            return False
        content = f"**{title}**\n{message}"
        if link:
            content += f"\n{link}"
        r = requests.post(self.webhook, json={"username": "DRONGO",
                                              "content": content[:1900]}, timeout=20)
        return r.ok


# ---------------------------------------------------------------------------
class LedChannel:
    """Blink an LED on a GPIO line. Uses python-periphery (one stable API across
    kernels); imported lazily so it's only needed if you enable the LED."""
    name = "led"

    def __init__(self, cfg):
        # Env vars win over config so the (zero-YAML-edit) configurator can set
        # the LED entirely via drongo.env.
        chip = os.environ.get("DRONGO_LED_CHIP") or str(cfg.get("alerts", "led", "chip", default="/dev/gpiochip0"))
        self.chip = chip if chip.startswith("/") else f"/dev/{chip}"
        line_env = os.environ.get("DRONGO_LED_LINE", "")
        self.line = int(line_env) if line_env.strip() else int(cfg.get("alerts", "led", "line", default=17))
        ah_env = os.environ.get("DRONGO_LED_ACTIVE_HIGH", "")
        self.active_high = (ah_env.strip().lower() in ("1", "true", "yes", "y", "on")) \
            if ah_env.strip() else bool(cfg.get("alerts", "led", "active_high", default=True))
        self.blinks = int(cfg.get("alerts", "led", "blinks", default=3))
        self.on_ms = int(cfg.get("alerts", "led", "on_ms", default=150))
        self.off_ms = int(cfg.get("alerts", "led", "off_ms", default=150))
        self._lock = threading.Lock()
        self._warned = False
        self.usable = os.path.exists(self.chip)
        if not self.usable:
            log.warning("LED channel: %s not found - is the LED's gpiochip right?", self.chip)

    def send(self, message, title, priority, link):
        if not self.usable:
            return False
        # Non-blocking: blink in a background thread so the agent never waits on it.
        threading.Thread(target=self._blink, args=(priority,), daemon=True).start()
        return True

    def _blink(self, priority):
        if not self._lock.acquire(blocking=False):
            return  # a blink is already running; don't stack them
        try:
            try:
                from periphery import GPIO
            except ImportError:
                if not self._warned:
                    log.warning("LED channel needs python-periphery (pip install python-periphery)")
                    self._warned = True
                return
            n = self.blinks * (2 if priority in ("high", "urgent") else 1)
            gpio = GPIO(self.chip, self.line, "out")
            try:
                for _ in range(n):
                    gpio.write(self.active_high)
                    time.sleep(self.on_ms / 1000.0)
                    gpio.write(not self.active_high)
                    time.sleep(self.off_ms / 1000.0)
            finally:
                gpio.write(not self.active_high)   # leave it off
                gpio.close()
        except Exception as e:
            log.warning("LED blink failed: %s", e)
        finally:
            self._lock.release()


# ---------------------------------------------------------------------------
class NtfyChannel:
    name = "ntfy"

    def __init__(self, cfg):
        self.server = cfg.get("alerts", "ntfy", "server", default="https://ntfy.sh")
        self.topic = cfg.get("alerts", "ntfy", "topic", default="")
        self.usable = bool(self.topic)

    def send(self, message, title, priority, link):
        if not self.usable:
            return False
        headers = {"Title": _ascii(title), "Priority": priority}
        if link:
            headers["Click"] = link
        r = requests.post(f"{self.server.rstrip('/')}/{self.topic}",
                          data=message.encode("utf-8"), headers=headers, timeout=20)
        return r.ok


# ---------------------------------------------------------------------------
class CommandChannel:
    """Run an operator-defined command on each alert. The command is set in the
    root-owned config (not by the agent), and receives the alert in env vars."""
    name = "command"

    def __init__(self, cfg):
        self.run = cfg.get("alerts", "command", "run", default="")
        self.usable = bool(self.run)

    def send(self, message, title, priority, link):
        if not self.usable:
            return False
        env = dict(os.environ, DRONGO_ALERT_TITLE=title, DRONGO_ALERT_MESSAGE=message,
                   DRONGO_ALERT_PRIORITY=priority, DRONGO_ALERT_LINK=link or "")
        subprocess.run(self.run, shell=True, env=env, timeout=20,
                       capture_output=True)
        return True


_CHANNELS = {"discord": DiscordChannel, "led": LedChannel,
             "ntfy": NtfyChannel, "command": CommandChannel}


def _auto_enabled(key, cfg) -> bool:
    """Turn a channel on just by setting its env var — no config edit needed."""
    if key == "discord":
        env = cfg.get("alerts", "discord", "webhook_env", default="DISCORD_WEBHOOK_URL")
        return bool(os.environ.get(env, "").strip())
    if key == "led":
        return bool(os.environ.get("DRONGO_LED_LINE", "").strip())
    return False


class Alerter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.notify_every_cycle = cfg.get("alerts", "notify_every_cycle", default=False)
        self.channels = []
        for key, cls in _CHANNELS.items():
            if cfg.get("alerts", key, "enabled", default=False) or _auto_enabled(key, cfg):
                try:
                    self.channels.append(cls(cfg))
                except Exception as e:
                    log.warning("alert channel '%s' failed to init: %s", key, e)

    def enabled(self) -> bool:
        return any(getattr(c, "usable", False) for c in self.channels)

    def send(self, message: str, title: str = "DRONGO", priority: str = "default",
             link: str | None = None) -> bool:
        # Dashboard kill-switch (no restart needed): if this flag file exists the
        # agent stays quiet on every channel. The web UI creates/removes it.
        if os.path.exists(os.path.join(str(self.cfg.workspace), "AGENT_ALERTS_OFF")):
            log.info("agent alerts muted (AGENT_ALERTS_OFF); skipping: %s", title)
            return False
        sent = False
        for ch in self.channels:
            try:
                sent = bool(ch.send(message, title, priority, link)) or sent
            except Exception as e:
                log.warning("alert channel '%s' failed: %s", ch.name, e)
        return sent
