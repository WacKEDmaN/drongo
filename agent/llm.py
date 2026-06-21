"""LLM router.

Most providers (cloud free tiers and the local Ollama server) speak the
OpenAI-compatible chat-completions protocol, so one `Provider` class handles
them all — only the base_url, key and model differ. Claude is the exception:
it gets a native `AnthropicProvider` (different request shape, no temperature).

The router tries providers in the configured order (cloud-first by default),
honours per-minute / per-day budgets, applies a short circuit breaker on
failures, and skips any provider whose API key is unset. List the local Ollama
provider last so it acts as the never-fail fallback when everything else is
rate-limited or unreachable.
"""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("agent.llm")


class AllProvidersFailed(Exception):
    pass


class Provider:
    def __init__(self, spec: dict):
        self.name = spec.get("name", "unknown")
        self.base_url = spec["base_url"].rstrip("/")
        self.model = spec["model"]
        self.api_key = None
        key_env = spec.get("api_key_env")
        if key_env:
            self.api_key = os.environ.get(key_env)
        self.rpm_limit = spec.get("rpm_limit")
        self.daily_limit = spec.get("daily_limit")
        self.enabled = spec.get("enabled", True)
        self.is_local = spec.get("local", False) or "localhost" in self.base_url or "127.0.0.1" in self.base_url
        self.extra_headers = spec.get("headers", {}) or {}
        # A cloud provider with no key configured is effectively disabled.
        self.usable = self.enabled and (self.is_local or bool(self.api_key))

    def chat(self, messages, temperature, max_tokens, timeout):
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if resp.status_code == 429:
            raise RateLimited(resp.text[:300])
        if resp.status_code >= 400:
            # Surface the API's reason (e.g. wrong model id) instead of a bare code.
            raise ProviderError(resp.status_code, resp.text[:300])
        data = resp.json()
        return data["choices"][0]["message"]["content"]


class RateLimited(Exception):
    pass


class ProviderError(Exception):
    """A non-429 HTTP error from a provider (carries the body so you can see why)."""
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


class AnthropicProvider:
    """Native Claude provider via the official `anthropic` SDK.

    Claude does NOT speak the OpenAI shape we use for everyone else, so it gets
    its own adapter: `system` is a top-level parameter (not a message), and
    current Opus/Sonnet/Haiku 4.x models reject `temperature` — so we omit it.
    The `anthropic` package is an optional dependency, imported lazily; install
    it only if you actually enable Claude (`pip install anthropic`).
    """

    def __init__(self, spec: dict):
        self.name = spec.get("name", "anthropic")
        self.model = spec["model"]
        self.rpm_limit = spec.get("rpm_limit")
        self.daily_limit = spec.get("daily_limit")
        self.enabled = spec.get("enabled", True)
        self.is_local = False
        key_env = spec.get("api_key_env", "ANTHROPIC_API_KEY")
        self.api_key = os.environ.get(key_env) if key_env else None
        # Optional: "adaptive" turns on adaptive thinking (better quality, more
        # tokens/cost). Off by default to keep this paid fallback cheap.
        self.thinking = spec.get("thinking")
        self._client = None
        self.usable = self.enabled and bool(self.api_key)

    def _client_or_raise(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:
                raise RuntimeError(
                    "the 'anthropic' package is required for the Claude provider "
                    "(pip install anthropic)") from e
            # max_retries=0 so the router can fail over fast instead of the SDK
            # silently retrying a 429 for minutes.
            self._client = anthropic.Anthropic(api_key=self.api_key, max_retries=0)
        return self._client

    def chat(self, messages, temperature, max_tokens, timeout):
        import anthropic  # for the typed exceptions
        client = self._client_or_raise()
        system = "\n\n".join(m["content"] for m in messages
                             if m.get("role") == "system").strip()
        convo = [{"role": m["role"], "content": m["content"]}
                 for m in messages if m.get("role") in ("user", "assistant")]
        if not convo or convo[0]["role"] != "user":
            convo = [{"role": "user", "content": system or "Continue."}] + convo
        kwargs = {"model": self.model, "max_tokens": max_tokens, "messages": convo}
        if system:
            kwargs["system"] = system
        if self.thinking == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}
        # NOTE: temperature is deliberately NOT sent — Claude Opus/Sonnet 4.x 400 on it.
        try:
            resp = client.with_options(timeout=timeout).messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise RateLimited(str(e))
        if getattr(resp, "stop_reason", None) == "refusal":
            raise RuntimeError("claude declined the request")
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def _make_provider(spec: dict):
    if spec.get("type") == "anthropic":
        return AnthropicProvider(spec)
    return Provider(spec)


class Router:
    def __init__(self, cfg, memory):
        self.cfg = cfg
        self.mem = memory
        self.temperature = cfg.get("llm", "temperature", default=0.7)
        self.max_tokens = cfg.get("llm", "max_tokens", default=2048)
        self.timeout = cfg.get("llm", "request_timeout", default=120)
        prefer = cfg.get("llm", "prefer", default="cloud_first")
        specs = cfg.get("llm", "providers", default=[]) or []
        providers = []
        for s in specs:
            try:
                providers.append(_make_provider(s))
            except Exception as e:   # missing base_url/model etc — skip, don't crash
                log.warning("skipping malformed provider %s: %s", s.get("name", "?"), e)
        providers = [p for p in providers if p.usable]
        if prefer == "local_first":
            providers.sort(key=lambda p: 0 if p.is_local else 1)
        # cloud_first keeps the configured order (locals usually listed last).
        self.providers = providers
        if not providers:
            log.warning("No usable LLM providers configured!")

    def provider_names(self):
        return [p.name for p in self.providers]

    def chat(self, messages, *, temperature=None, max_tokens=None):
        """Return (text, provider_name). Raises AllProvidersFailed."""
        temperature = self.temperature if temperature is None else temperature
        max_tokens = max_tokens or self.max_tokens
        errors = []
        for p in self.providers:
            if not p.is_local and not self.mem.can_use(p.name, p.rpm_limit, p.daily_limit):
                continue
            try:
                text = p.chat(messages, temperature, max_tokens, self.timeout)
                if not text or not text.strip():
                    raise ValueError("empty response")
                self.mem.record_use(p.name)
                return text, p.name
            except RateLimited:
                log.info("%s rate limited; cooling down", p.name)
                self.mem.set_cooldown(p.name, 90)
                errors.append(f"{p.name}: 429")
            except ProviderError as e:
                # A 4xx (bad model id, bad key, bad request) won't fix itself on
                # retry — back off for an hour so it doesn't spam every cycle.
                # 5xx is transient, short cooldown.
                cd = 3600 if 400 <= e.status < 500 else 60
                log.warning("%s HTTP %s (%ds cooldown): %s", p.name, e.status, cd, e.body)
                if not p.is_local:
                    self.mem.set_cooldown(p.name, cd)
                errors.append(f"{p.name}: HTTP {e.status}")
            except Exception as e:  # network, parse, etc.
                log.warning("%s failed: %s", p.name, e)
                if not p.is_local:
                    self.mem.set_cooldown(p.name, 60)
                errors.append(f"{p.name}: {e}")
        raise AllProvidersFailed("; ".join(errors) or "no providers available")

    def complete(self, system, user, **kw):
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        return self.chat(messages, **kw)
