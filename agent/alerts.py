"""Outbound notifications so the agent can ping you about notable results.

Default is ntfy.sh: install the ntfy app on your phone, subscribe to a
private topic, and the agent POSTs to it. No account or key required.
Telegram is also supported if you'd rather use a bot.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("agent.alerts")


class Alerter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.provider = (cfg.get("alerts", "provider", default="none") or "none").lower()
        self.notify_every_cycle = cfg.get("alerts", "notify_every_cycle", default=False)

    def enabled(self) -> bool:
        return self.provider in ("ntfy", "telegram")

    def send(self, message: str, title: str = "Agent", priority: str = "default",
             link: str | None = None) -> bool:
        try:
            if self.provider == "ntfy":
                return self._ntfy(message, title, priority, link)
            if self.provider == "telegram":
                return self._telegram(message, title, link)
        except Exception as e:
            log.warning("alert failed: %s", e)
        return False

    def _ntfy(self, message, title, priority, link):
        server = self.cfg.get("alerts", "ntfy", "server", default="https://ntfy.sh")
        topic = self.cfg.get("alerts", "ntfy", "topic", default="")
        if not topic:
            return False
        # HTTP headers must be latin-1; the body (below) keeps full UTF-8.
        safe_title = title.encode("ascii", "replace").decode("ascii")
        headers = {"Title": safe_title, "Priority": priority}
        if link:
            headers["Click"] = link
        r = requests.post(f"{server.rstrip('/')}/{topic}",
                          data=message.encode("utf-8"), headers=headers, timeout=20)
        return r.ok

    def _telegram(self, message, title, link):
        token_env = self.cfg.get("alerts", "telegram", "bot_token_env",
                                 default="TELEGRAM_BOT_TOKEN")
        token = os.environ.get(token_env, "")
        chat_id = self.cfg.get("alerts", "telegram", "chat_id", default="")
        if not token or not chat_id:
            return False
        text = f"*{title}*\n{message}"
        if link:
            text += f"\n{link}"
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=20,
        )
        return r.ok
