"""Minimal MCP (Model Context Protocol) CLIENT.

Lets DRONGO use external MCP tool servers as first-class tools. JSON-RPC 2.0 over
either stdio (spawn a server subprocess) or streamable HTTP. Kept dependency-light
(subprocess + threads + requests) so it runs on the Pi.

Security: the HUMAN configures servers from the dashboard. A stdio server is
launched inside the AGENT's OWN systemd sandbox (User=drongo, no sudo,
ProtectSystem=strict, writable only under /var/lib/drongo) — so an MCP server is
no more privileged than the agent's existing `shell` tool. MCP servers get a
CLEAN environment (PATH/HOME + only the env vars their config lists), so DRONGO's
LLM keys are never leaked to them.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time

import requests

PROTO = "2025-06-18"           # MCP protocol version we advertise
_CLIENT_INFO = {"name": "drongo", "version": "1.0"}


class MCPError(Exception):
    pass


def _clean_env(extra):
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8"}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v not in (None, ""):
                env[str(k)] = str(v)
    return env


# ---------------------------------------------------------------------------
class _StdioConn:
    """JSON-RPC over a server subprocess's stdin/stdout (newline-delimited)."""

    def __init__(self, command, args, env=None, cwd=None, timeout=30):
        self.timeout = timeout
        self._id = 0
        self.proc = subprocess.Popen(
            [command] + [str(a) for a in (args or [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=_clean_env(env), cwd=cwd or None)
        self._q = queue.Queue()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        try:
            for line in self.proc.stdout:                # blocks; daemon thread
                line = line.strip()
                if line:
                    try:
                        self._q.put(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass

    def _send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=None):
        self._id += 1
        rid = self._id
        m = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            try:
                msg = self._q.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise MCPError("server process exited")
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise MCPError(str(msg["error"]))
                return msg.get("result", {})
            # otherwise a notification / log / other id — ignore
        raise MCPError(f"timeout waiting for {method}")

    def notify(self, method, params=None):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        self._send(m)

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
class _HttpConn:
    """JSON-RPC over MCP streamable HTTP (best-effort; handles JSON or one SSE msg)."""

    def __init__(self, url, headers=None, timeout=30):
        self.url = url
        self.timeout = timeout
        self._id = 0
        self.session = None
        self.headers = {"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"}
        if isinstance(headers, dict):
            self.headers.update({str(k): str(v) for k, v in headers.items()})

    def _post(self, msg):
        h = dict(self.headers)
        if self.session:
            h["Mcp-Session-Id"] = self.session
        r = requests.post(self.url, data=json.dumps(msg), headers=h, timeout=self.timeout)
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}: {r.text[:200]}")
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            self.session = sid
        return r

    def request(self, method, params=None, timeout=None):
        self._id += 1
        m = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            m["params"] = params
        r = self._post(m)
        ctype = r.headers.get("Content-Type", "")
        if "text/event-stream" in ctype:                # take the first data: {json}
            for line in r.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        obj = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if obj.get("id") == m["id"]:
                        if "error" in obj:
                            raise MCPError(str(obj["error"]))
                        return obj.get("result", {})
            raise MCPError("no result in SSE stream")
        obj = r.json()
        if "error" in obj:
            raise MCPError(str(obj["error"]))
        return obj.get("result", {})

    def notify(self, method, params=None):
        m = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            m["params"] = params
        try:
            self._post(m)
        except Exception:
            pass

    def close(self):
        pass


# ---------------------------------------------------------------------------
class MCPServer:
    def __init__(self, spec: dict):
        self.name = str(spec.get("name") or "mcp")
        self.transport = "http" if spec.get("transport") == "http" else "stdio"
        self.spec = spec
        self.conn = None
        self.tools = []
        self.error = ""

    def connect(self, timeout=None) -> bool:
        try:
            self.close()
            if self.transport == "http":
                if not self.spec.get("url"):
                    raise MCPError("http server needs a url")
                self.conn = _HttpConn(self.spec["url"], self.spec.get("headers"),
                                      timeout=self.spec.get("timeout", 30))
            else:
                if not self.spec.get("command"):
                    raise MCPError("stdio server needs a command")
                self.conn = _StdioConn(self.spec["command"], self.spec.get("args"),
                                       env=self.spec.get("env"), cwd=self.spec.get("cwd"),
                                       timeout=self.spec.get("timeout", 30))
            self.conn.request("initialize", {"protocolVersion": PROTO, "capabilities": {},
                                             "clientInfo": _CLIENT_INFO}, timeout=timeout or 30)
            self.conn.notify("notifications/initialized")
            res = self.conn.request("tools/list", {})
            self.tools = res.get("tools", []) or []
            self.error = ""
            return True
        except Exception as e:
            self.error = str(e)[:300]
            self.close()
            return False

    def call(self, tool, arguments):
        if self.conn is None and not self.connect():
            return f"ERROR: {self.name} not connected: {self.error}"
        for attempt in (1, 2):
            try:
                res = self.conn.request("tools/call", {"name": tool, "arguments": arguments or {}})
                break
            except Exception as e:
                if attempt == 2 or not self.connect():   # one reconnect
                    return f"ERROR: MCP {self.name}.{tool}: {e}"
        parts = []
        for c in (res.get("content") or []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif c.get("type") == "resource":
                parts.append(str((c.get("resource") or {}).get("text") or c)[:1000])
            else:
                parts.append(json.dumps(c)[:500])
        body = "\n".join(p for p in parts if p) or "(no content)"
        return ("ERROR: " + body) if res.get("isError") else body

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


# ---------------------------------------------------------------------------
class MCPManager:
    """Holds the configured MCP servers, connects them, exposes their tools."""

    def __init__(self, specs):
        self.servers = [MCPServer(s) for s in (specs or [])
                        if isinstance(s, dict) and s.get("name") and s.get("enabled", True)]

    def connect_all(self, log=None):
        for s in self.servers:
            ok = s.connect()
            if log:
                (log.info if ok else log.warning)(
                    "MCP %s: %s", s.name, f"{len(s.tools)} tool(s)" if ok else s.error)

    def tools(self):
        """[{full, server, tool, description, schema}] across all connected servers."""
        out = []
        for s in self.servers:
            for t in s.tools:
                nm = t.get("name")
                if nm:
                    out.append({"full": f"mcp__{s.name}__{nm}", "server": s.name, "tool": nm,
                                "description": t.get("description", ""),
                                "schema": t.get("inputSchema") or {}})
        return out

    def call(self, full_name, arguments):
        for s in self.servers:
            for t in s.tools:
                if f"mcp__{s.name}__{t.get('name')}" == full_name:
                    return s.call(t.get("name"), arguments)
        return f"ERROR: unknown MCP tool '{full_name}'"

    def status(self):
        return [{"name": s.name, "transport": s.transport,
                 "connected": s.conn is not None, "tools": len(s.tools),
                 "error": s.error} for s in self.servers]

    def close(self):
        for s in self.servers:
            s.close()


def probe_server(spec: dict) -> dict:
    """Connect once and report tools/error — the dashboard's 'test' button."""
    s = MCPServer(spec)
    ok = s.connect()
    result = {"ok": ok, "error": s.error,
              "tools": [{"name": t.get("name"), "description": t.get("description", "")}
                        for t in s.tools]}
    s.close()
    return result
