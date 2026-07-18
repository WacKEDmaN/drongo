"""The agent's tools. Each tool is a plain function returning a string
observation that gets fed back to the model. Tools are registered with a
short description and an argument hint so the loop can advertise them to the
LLM in a single system prompt.
"""

from __future__ import annotations

import glob
import html
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import safeguard


# Tools that are too risky to run while the agent is in crash-loop SAFE MODE.
DANGEROUS_IN_SAFE_MODE = {"shell", "self_update"}


@dataclass
class ToolContext:
    cfg: object
    mem: object
    router: object
    alerter: object
    log: object
    safe_mode: bool = False
    artifacts: list = field(default_factory=list)  # collected per cycle
    project_dir: str = ""    # e.g. "projects/snake-game" — bare writes land here

    @property
    def workspace(self):
        return str(self.cfg.workspace)

    def add_artifact(self, rel_path, label):
        for a in self.artifacts:           # dedupe: a file written N times = one artifact
            if a["path"] == rel_path:
                a["label"] = label
                return
        self.artifacts.append({"path": rel_path, "label": label})


@dataclass
class Tool:
    name: str
    description: str
    args: str          # human-readable arg hint
    func: object


REGISTRY: dict[str, Tool] = {}


def tool(name, description, args=""):
    def deco(fn):
        REGISTRY[name] = Tool(name, description, args, fn)
        return fn
    return deco


def build_registry(ctx: ToolContext) -> dict[str, Tool]:
    """Return the enabled tools given the config toggles."""
    t = ctx.cfg.get("tools", default={}) or {}
    enabled = {}
    for nm, tl in REGISTRY.items():
        if ctx.safe_mode and nm in DANGEROUS_IN_SAFE_MODE:
            continue
        section = {
            "shell": "shell", "write_file": "files", "read_file": "files",
            "list_dir": "files", "delete_path": "files", "web_search": "web", "web_fetch": "web",
            "generate_image": "images", "discover_sensors": "sensors",
            "make_dashboard": "dashboard", "send_alert": "alerts",
            "remember": "files", "recall": "files",
            "save_skill": "files", "recall_skill": "files",
            "save_note": "files", "recall_notes": "files", "request_package": "files",
            "recall_knowledge": "files",
        }.get(nm, nm)
        if t.get(section, {}).get("enabled", True):
            enabled[nm] = tl
    return enabled


def tools_prompt(tools: dict[str, Tool]) -> str:
    lines = []
    for tl in tools.values():
        lines.append(f"- {tl.name}({tl.args}): {tl.description}")
    return "\n".join(lines)


# Secrets the agent's shell must never see (so a prompt-injected command can't
# exfiltrate them). The agent's projects don't need any of these.
_SECRET_ENV = ("GROQ_API_KEY", "CEREBRAS_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY",
               "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "NVIDIA_API_KEY",
               "OLLAMA_API_KEY", "TOGETHER_API_KEY",
               "DISCORD_WEBHOOK_URL", "DRONGO_DISCORD_WEBHOOK", "TELEGRAM_BOT_TOKEN",
               "DRONGO_WEB_PASSWORD")


def _project_env(cfg):
    """Subprocess env with the agent's writable project venv activated, so
    `pip install X` and `python`/`python3` work (the system ones are read-only
    and Debian blocks system-wide pip). Secrets are stripped."""
    env = dict(os.environ)
    for k in _SECRET_ENV:
        env.pop(k, None)
    binp = os.path.join(str(cfg.project_venv), "bin")
    env["PATH"] = binp + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(cfg.project_venv)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env.pop("PYTHONHOME", None)
    # Retro Z80 toolchain (if system/retro-toolchain.sh has been run): put z88dk's
    # zcc on PATH and point ZCCCFG / CPCT_PATH at it. sdcc + pasmo are in /usr/bin.
    if os.path.isdir("/opt/retro/z88dk/bin"):
        env["PATH"] = "/opt/retro/z88dk/bin" + os.pathsep + env["PATH"]
        if os.path.isdir("/opt/retro/z88dk/lib/config"):
            env["ZCCCFG"] = "/opt/retro/z88dk/lib/config"
    if os.path.isdir("/opt/retro/cpctelera"):
        env["CPCT_PATH"] = "/opt/retro/cpctelera"
    return env


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


# ----------------------------------------------------------------------
# Shell + filesystem
# ----------------------------------------------------------------------
@tool("shell", "Run a shell command inside the workspace. Returns stdout+stderr.",
      "command: str")
def shell(ctx: ToolContext, command: str = "", **_):
    scfg = ctx.cfg.get("tools", "shell", default={})
    allow_sudo = scfg.get("allow_sudo", False)
    timeout = scfg.get("timeout", 120)
    max_out = scfg.get("max_output_chars", 6000)
    extra = ctx.cfg.get("safety", "deny_patterns", default=[]) or []
    # Re-verify the guard hasn't been tampered with before every shell call.
    ok, problems = safeguard.verify_self()
    if not ok and ctx.cfg.get("safety", "strict", default=False):
        return f"REJECTED: safeguard integrity check failed: {problems}"
    try:
        safeguard.check_command(command, allow_sudo=allow_sudo, extra_deny=extra)
    except safeguard.CommandRejected as e:
        return f"REJECTED: {e}"
    try:
        proc = subprocess.run(
            command, shell=True, cwd=ctx.workspace, capture_output=True,
            text=True, timeout=timeout, env=_project_env(ctx.cfg),
            preexec_fn=safeguard.posix_limits(cpu_seconds=timeout),
        )
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return _truncate(f"(exit {proc.returncode})\n{out}".strip(), max_out)
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout}s"
    except Exception as e:
        return f"ERROR: {e}"


_OUTPUT_DIRS = ("projects/", "dashboards/", "images/")


def project_slug(text: str, fallback: str = "project") -> str:
    """A filesystem-safe folder name derived from a project title. Stable across
    cycles so a resumed project keeps writing to the SAME folder."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40].strip("-") or fallback


def _rooted(ctx: "ToolContext", path: str) -> str:
    """Keep a project's files together. A path that isn't already under a known
    output dir (projects/ dashboards/ images/) is placed inside THIS project's
    folder, so the agent can't scatter loose files across the workspace root."""
    p = (path or "").replace("\\", "/").strip().lstrip("/")
    if not p or not ctx.project_dir:
        return path
    if p.lower().startswith(_OUTPUT_DIRS):
        return p
    return f"{ctx.project_dir}/{p}"


@tool("write_file", "Create or overwrite a file in the workspace.",
      "path: str, content: str")
def write_file(ctx: ToolContext, path: str = "", content: str = "", **_):
    try:
        path = _rooted(ctx, path)
        full = safeguard.safe_join(ctx.workspace, path)
        Path(full).parent.mkdir(parents=True, exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        rel = os.path.relpath(full, ctx.workspace)
        ctx.add_artifact(rel, f"file: {rel}")
        msg = f"wrote {len(content)} chars to {rel}"
        warn = _syntax_check(rel, content)   # immediate feedback so it self-fixes
        return msg + ("\n" + warn if warn else "")
    except Exception as e:
        return f"ERROR: {e}"


def _syntax_check(rel: str, content: str) -> str:
    """Stdlib-only sanity check on what was just written, so the model sees an
    error in the SAME observation and fixes it before moving on."""
    low = rel.lower()
    try:
        if low.endswith(".py"):
            compile(content, rel, "exec")
        elif low.endswith(".json"):
            json.loads(content)
    except SyntaxError as e:
        return f"⚠ SyntaxError at line {e.lineno}: {e.msg} — FIX this before continuing."
    except Exception as e:
        return f"⚠ invalid JSON: {e} — FIX this before continuing."
    return ""


@tool("read_file", "Read a file from the workspace.", "path: str")
def read_file(ctx: ToolContext, path: str = "", **_):
    limit = ctx.cfg.get("tools", "files", "max_file_chars", default=60000)
    try:
        full = safeguard.safe_join(ctx.workspace, path)
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            return _truncate(fh.read(), limit)
    except Exception as e:
        return f"ERROR: {e}"


@tool("list_dir", "List files in a workspace directory (default '.').", "path: str = '.'")
def list_dir(ctx: ToolContext, path: str = ".", **_):
    try:
        full = safeguard.safe_join(ctx.workspace, path)
        entries = []
        for name in sorted(os.listdir(full)):
            p = os.path.join(full, name)
            kind = "dir" if os.path.isdir(p) else f"{os.path.getsize(p)}B"
            entries.append(f"{name} ({kind})")
        return "\n".join(entries) or "(empty)"
    except Exception as e:
        return f"ERROR: {e}"


@tool("delete_path",
      "Delete a file or folder you no longer need (only under projects/, "
      "dashboards/ or images/). Use to tidy up scratch/temp files.", "path: str")
def delete_path(ctx: ToolContext, path: str = "", **_):
    try:
        full = os.path.realpath(safeguard.safe_join(ctx.workspace, path))
    except Exception:
        return "ERROR: path escapes the workspace"
    ws = os.path.realpath(ctx.workspace)
    allowed = tuple(os.path.join(ws, d) + os.sep for d in ("projects", "dashboards", "images"))
    if not full.startswith(allowed):
        return "ERROR: can only delete things under projects/, dashboards/ or images/"
    if not os.path.exists(full):
        return f"already gone: {path}"
    try:
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
            return f"deleted folder {path}"
        os.remove(full)
        return f"deleted {path}"
    except OSError as e:
        return f"ERROR: {e}"


# ----------------------------------------------------------------------
# Web
# ----------------------------------------------------------------------
@tool("web_search", "Search the web (DuckDuckGo). Returns top results.", "query: str")
def web_search(ctx: ToolContext, query: str = "", **_):
    try:
        r = requests.post("https://html.duckduckgo.com/html/",
                          data={"q": query},
                          headers={"User-Agent": "Mozilla/5.0 (agent)"},
                          timeout=ctx.cfg.get("tools", "web", "timeout", default=30))
        r.raise_for_status()
        results = []
        for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                             r.text, re.S):
            href, title = m.group(1), re.sub("<.*?>", "", m.group(2))
            q = urllib.parse.urlparse(href)
            params = urllib.parse.parse_qs(q.query)
            if "uddg" in params:
                href = params["uddg"][0]
            results.append(f"- {html.unescape(title).strip()}\n  {href}")
            if len(results) >= 6:
                break
        return "\n".join(results) or "no results"
    except Exception as e:
        return f"ERROR: {e}"


def _url_is_external(url: str):
    """SSRF guard: allow only http/https to a PUBLIC IP. Blocks localhost, the
    LAN, link-local (incl. cloud metadata 169.254.x), and reserved ranges so a
    prompt-injected page can't pivot the agent into your network."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        return False, f"scheme '{p.scheme}' not allowed (http/https only)"
    host = p.hostname
    if not host:
        return False, "no host in url"
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:
        return False, f"dns lookup failed: {e}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False, f"refusing internal address {ip} (SSRF guard)"
    return True, ""


@tool("web_fetch", "Fetch a public http/https URL and return readable text.", "url: str")
def web_fetch(ctx: ToolContext, url: str = "", **_):
    limit = ctx.cfg.get("tools", "web", "max_chars", default=8000)
    timeout = ctx.cfg.get("tools", "web", "timeout", default=30)
    try:
        cur = url
        for _hop in range(4):                       # validate every redirect hop
            ok, why = _url_is_external(cur)
            if not ok:
                return f"REJECTED: {why}"
            r = requests.get(cur, headers={"User-Agent": "Mozilla/5.0 (agent)"},
                             timeout=timeout, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                cur = urllib.parse.urljoin(cur, r.headers["Location"])
                continue
            r.raise_for_status()
            text = r.text
            text = re.sub(r"(?is)<(script|style|head|nav|footer).*?</\1>", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            text = html.unescape(re.sub(r"\s+", " ", text)).strip()
            return _truncate(text, limit)
        return "ERROR: too many redirects"
    except Exception as e:
        return f"ERROR: {e}"


# ----------------------------------------------------------------------
# Image generation (free, keyless via Pollinations)
# ----------------------------------------------------------------------
@tool("generate_image",
      "Create an image from a text prompt and save it to the gallery.",
      "prompt: str, filename: str = 'art.png'")
def generate_image(ctx: ToolContext, prompt: str = "", filename: str = "", **_):
    if not filename:
        filename = f"art-{int(time.time())}.png"
    if not filename.lower().endswith((".png", ".jpg", ".jpeg")):
        filename += ".png"
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    if not prompt.strip():
        return "ERROR: generate_image needs a prompt describing the picture to draw."
    provider = ctx.cfg.get("tools", "images", "provider", default="pollinations")
    out_path = Path(ctx.cfg.images) / filename
    try:
        if provider == "local":
            return _generate_image_local(ctx, prompt, filename, out_path)
        if provider != "pollinations":
            return f"ERROR: unknown image provider '{provider}'"
        enc = urllib.parse.quote(prompt[:600])
        url = (f"https://image.pollinations.ai/prompt/{enc}"
               f"?width=768&height=768&nologo=true&seed={int(time.time())%100000}")
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        data = r.content
        # Make sure we actually got a picture, not an error page / rate-limit
        # notice — otherwise we'd save HTML as a broken .png and claim success.
        ctype = r.headers.get("Content-Type", "").lower()
        real_ext = _image_ext(data)
        if not (real_ext or ctype.startswith("image/")) or len(data) < 512:
            snippet = data[:120].decode("utf-8", "replace").strip()
            return (f"ERROR: image service returned {ctype or 'no content-type'} "
                    f"({len(data)} bytes), not an image — try again (maybe rate-limited) "
                    f"or simplify the prompt. First bytes: {snippet!r}")
        # Match the saved extension to the real bytes (pollinations returns JPEG
        # even when asked for .png) so browsers + the gallery render it correctly.
        if real_ext and not filename.lower().endswith(real_ext):
            filename = re.sub(r"\.(png|jpg|jpeg|gif|webp)$", "", filename, flags=re.I) + real_ext
            out_path = Path(ctx.cfg.images) / filename
        out_path.write_bytes(data)
        rel = f"images/{filename}"
        ctx.add_artifact(rel, f"image: {prompt[:60]}")
        return (f"saved a real image to {rel} ({len(data)} bytes). It is now in the "
                f"gallery — the task is done; do NOT also describe the image in text.")
    except Exception as e:
        return f"ERROR: {e}"


def _image_ext(data: bytes):
    return (".jpg" if data[:3] == b"\xff\xd8\xff"
            else ".png" if data[:8] == b"\x89PNG\r\n\x1a\n"
            else ".gif" if data[:6] in (b"GIF87a", b"GIF89a")
            else ".webp" if data[:4] == b"RIFF"
            else None)


def _generate_image_local(ctx, prompt, filename, out_path):
    """Run a LOCAL image generator (e.g. OnnxStream) via the configured
    images.local_cmd template. {prompt} and {out} are shell-quoted (injection-safe)."""
    tmpl = ctx.cfg.get("tools", "images", "local_cmd", default="")
    if not tmpl:
        return ("ERROR: images.provider is 'local' but tools.images.local_cmd is not set. "
                "Run system/image-gen.sh or point local_cmd at your generator.")
    cmd = tmpl.replace("{prompt}", shlex.quote(prompt[:600])).replace("{out}", shlex.quote(str(out_path)))
    to = int(ctx.cfg.get("tools", "images", "timeout", default=600))
    try:
        proc = subprocess.run(cmd, shell=True, cwd=ctx.workspace, capture_output=True,
                              text=True, timeout=to, env=_project_env(ctx.cfg),
                              preexec_fn=safeguard.posix_limits(cpu_seconds=to + 30))
    except subprocess.TimeoutExpired:
        return f"ERROR: local image generation timed out after {to}s (it's slow on this box)."
    if not out_path.exists() or out_path.stat().st_size < 512:
        return f"ERROR: local generator produced no image. stderr: {(proc.stderr or '')[:300]}"
    if not _image_ext(out_path.read_bytes()[:8]):
        return f"ERROR: local generator output isn't a recognised image. stderr: {(proc.stderr or '')[:200]}"
    rel = f"images/{filename}"
    ctx.add_artifact(rel, f"image: {prompt[:60]}")
    return (f"saved a real image to {rel} via the local generator. It is now in the "
            f"gallery — the task is done; do NOT also describe the image in text.")


# ----------------------------------------------------------------------
# Sensors / hardware discovery
# ----------------------------------------------------------------------
def _read_first(path):
    try:
        with open(path) as fh:
            # device-tree strings carry a trailing NUL; drop it.
            return fh.read().replace("\x00", "").strip()
    except Exception:
        return None


def _run(cmd):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return (p.stdout or p.stderr).strip()
    except Exception:
        return ""


def collect_hardware() -> dict:
    """Best-effort inventory of the machine's sensors and buses."""
    info = {"ts": time.time()}

    # CPU / memory / model
    info["model"] = _read_first("/proc/device-tree/model") or _read_first("/sys/firmware/devicetree/base/model")
    info["uname"] = _run("uname -a")
    info["mem"] = _run("free -h")
    info["uptime"] = _run("uptime -p")

    # Thermal zones
    thermals = []
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        t = _read_first(f"{zone}/temp")
        ztype = _read_first(f"{zone}/type")
        if t and t.lstrip("-").isdigit():
            thermals.append({"zone": os.path.basename(zone), "type": ztype,
                             "celsius": round(int(t) / 1000.0, 1)})
    info["thermals"] = thermals

    # hwmon (voltages, temps, fans)
    hwmon = []
    for chip in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = _read_first(f"{chip}/name")
        readings = {}
        for f in glob.glob(f"{chip}/*_input"):
            val = _read_first(f)
            if val is not None:
                readings[os.path.basename(f)] = val
        hwmon.append({"name": name, "readings": readings})
    info["hwmon"] = hwmon

    # Buses & peripherals
    info["i2c_buses"] = sorted(os.path.basename(p) for p in glob.glob("/dev/i2c-*"))
    info["spi_devices"] = sorted(os.path.basename(p) for p in glob.glob("/dev/spidev*"))
    info["onewire"] = sorted(os.path.basename(p) for p in glob.glob("/sys/bus/w1/devices/*")
                             if not os.path.basename(p).startswith("w1_bus"))
    info["video_devices"] = sorted(os.path.basename(p) for p in glob.glob("/dev/video*"))
    info["gpiochips"] = _run("gpiodetect")  # from libgpiod if present
    info["usb"] = _run("lsusb")
    info["block"] = _run("lsblk -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null")
    info["net"] = _run("ip -brief addr 2>/dev/null")

    # i2c scan (only if i2c-tools present and buses exist)
    i2c_scan = {}
    if info["i2c_buses"] and _run("which i2cdetect"):
        for bus in info["i2c_buses"]:
            num = bus.replace("i2c-", "")
            i2c_scan[bus] = _run(f"i2cdetect -y {num} 2>/dev/null")
    info["i2c_scan"] = i2c_scan
    return info


def _i2c_addresses(grid: str) -> set:
    """Occupied addresses from an `i2cdetect -y N` grid (cells are the address in
    hex; '--' empty, 'UU' busy/driver-claimed). Row labels like '70:' are skipped."""
    addrs = set()
    for line in (grid or "").splitlines():
        label, sep, rest = line.partition(":")
        if not sep or not re.fullmatch(r"\s*[0-9a-fA-F]{2}\s*", label):
            continue
        for c in rest.split():
            if re.fullmatch(r"[0-9a-fA-F]{2}", c):   # a real address (UU/-- excluded)
                addrs.add(c.lower())
    return addrs


def hardware_devices(info: dict) -> list:
    """Stable identifiers for the devices currently attached, derived from
    collect_hardware(). Excludes volatile readings (temps/mem/uptime) so two
    scans can be diffed to spot genuinely NEW hardware (camera, sensor, etc.)."""
    out = set()
    for line in (info.get("usb") or "").splitlines():
        m = re.search(r"\bID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4}.*)$", line.strip())
        if m:
            out.add("usb:" + m.group(1).strip())
    for v in info.get("video_devices") or []:
        out.add("video:" + v)
    for b in info.get("i2c_buses") or []:
        out.add("i2c-bus:" + b)
    for s in info.get("spi_devices") or []:
        out.add("spi:" + s)
    for w in info.get("onewire") or []:
        out.add("1wire:" + w)
    for bus, grid in (info.get("i2c_scan") or {}).items():
        for addr in _i2c_addresses(grid):
            out.add(f"i2c:{bus}@0x{addr}")
    return sorted(out)


def scan_and_diff_hardware(mem, cfg, force=False):
    """Shared by the agent loop and the dashboard's Scan button: run a hardware
    scan (skipped if within the throttle window unless force=True), store the
    inventory + device fingerprint, and if a device appeared since last time,
    record a 'new_hardware' nudge for ideation + a journal note. Returns
    (info, new_devices)."""
    interval = cfg.get("loop", "hw_scan_interval_seconds", default=1200)
    if not force and time.time() - (mem.recall("hw_last_scan") or 0) < interval:
        return mem.recall("hardware"), []
    info = collect_hardware()
    mem.remember("hw_last_scan", time.time())
    mem.remember("hardware", info)
    devices = hardware_devices(info)
    prev = mem.recall("hw_devices")
    mem.remember("hw_devices", devices)
    new = [] if not isinstance(prev, list) else [d for d in devices if d not in set(prev)]
    if new:
        mem.remember("new_hardware", {"items": new, "ts": time.time()})
        mem.add_journal("note", "New hardware detected", ", ".join(new), ok=True)
    return info, new


_JUNK_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ipynb_checkpoints",
               ".DS_Store", "Thumbs.db"}
_JUNK_EXTS = (".pyc", ".pyo", ".tmp", ".temp", ".swp", ".swo", ".bak", ".orig")


def housekeep(cfg, empty_dir_age=3600) -> str:
    """Janitor: remove build junk (__pycache__ / *.pyc / editor temp files) and
    stale EMPTY folders under the generated dirs. Deliberately conservative —
    only touches projects/, dashboards/ and images/, never the workspace root
    (control markers), state, logs or the venv, and only deletes empty dirs that
    have sat unchanged for a while (so it won't nuke a project mid-build).
    Returns a one-line summary of what was removed, or ''."""
    removed = []
    for root in (cfg.projects, cfg.dashboards, cfg.images):
        root = str(root)
        if not os.path.isdir(root):
            continue
        for base, dirs, files in os.walk(root, topdown=False):
            if ".git" in base.split(os.sep):       # never touch git internals
                continue
            for f in files:
                if f in _JUNK_NAMES or f.lower().endswith(_JUNK_EXTS):
                    try:
                        os.remove(os.path.join(base, f)); removed.append(f)
                    except OSError:
                        pass
            for dname in dirs:
                if dname in _JUNK_NAMES:
                    p = os.path.join(base, dname)
                    if not os.path.islink(p):
                        try:
                            shutil.rmtree(p); removed.append(dname + "/")
                        except OSError:
                            pass
            if base != root:                       # never remove the root dirs themselves
                try:
                    if not os.listdir(base) and (time.time() - os.path.getmtime(base)) > empty_dir_age:
                        os.rmdir(base); removed.append(os.path.basename(base) + "/")
                except OSError:
                    pass
    # Also sweep build junk from the workspace ROOT — ONLY _JUNK_ items, so the
    # control markers (PAUSE/STOP/UPDATE_REQUESTED), hardware.json and the
    # projects/dashboards/images/state/logs/venv dirs are all left untouched.
    ws = str(cfg.workspace)
    if os.path.isdir(ws):
        for name in os.listdir(ws):
            p = os.path.join(ws, name)
            if os.path.isdir(p) and name in _JUNK_NAMES and not os.path.islink(p):
                try:
                    shutil.rmtree(p); removed.append(name + "/")
                except OSError:
                    pass
            elif os.path.isfile(p) and (name in _JUNK_NAMES or name.lower().endswith(_JUNK_EXTS)):
                try:
                    os.remove(p); removed.append(name)
                except OSError:
                    pass
    if not removed:
        return ""
    return f"cleaned {len(removed)} item(s): " + ", ".join(removed[:8]) + ("…" if len(removed) > 8 else "")


def hardware_summary() -> dict:
    """Fast, non-intrusive snapshot of what's wired up — lists buses/devices and
    reads thermals, but does NOT probe i2c (so it never disturbs a device).
    Used by `doctor` so you can see your sensors right after install."""
    def temps():
        out = []
        for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
            t = _read_first(f"{z}/temp")
            ty = _read_first(f"{z}/type")
            if t and t.lstrip("-").isdigit():
                out.append(f"{ty or os.path.basename(z)}={int(t)/1000.0:.1f}C")
        return out

    # Only accept real gpiodetect output (lines like "gpiochip0 [gpio0] ..."),
    # so an error message from a missing/denied gpiodetect doesn't show as noise.
    chips = [ln.split()[0] for ln in _run("gpiodetect").splitlines()
             if ln.strip().startswith("gpiochip")]
    return {
        "model": _read_first("/proc/device-tree/model") or "unknown",
        "thermals": temps(),
        "i2c": sorted(os.path.basename(p) for p in glob.glob("/dev/i2c-*")),
        "spi": sorted(os.path.basename(p) for p in glob.glob("/dev/spidev*")),
        "onewire": sorted(os.path.basename(p) for p in glob.glob("/sys/bus/w1/devices/*")
                          if not os.path.basename(p).startswith("w1_bus")),
        "cameras": sorted(os.path.basename(p) for p in glob.glob("/dev/video*")),
        "gpiochips": chips,
    }


def system_stats() -> dict:
    """Live host stats for the dashboard (stdlib only; Linux, safe elsewhere)."""
    import shutil
    out = {"time": time.strftime("%H:%M:%S"), "date": time.strftime("%a %d %b %Y")}
    try:
        with open("/proc/uptime") as fh:
            up = int(float(fh.read().split()[0]))
        dd, r = divmod(up, 86400); hh, r = divmod(r, 3600); mm = r // 60
        out["uptime"] = (f"{dd}d " if dd else "") + f"{hh}h {mm}m"
    except Exception:
        out["uptime"] = "?"
    try:
        def _snap():
            with open("/proc/stat") as fh:
                v = list(map(int, fh.readline().split()[1:8]))
            return sum(v), v[3] + v[4]            # total, idle+iowait
        a, ai = _snap(); time.sleep(0.2); b, bi = _snap()
        out["cpu_pct"] = round(100 * (1 - (bi - ai) / (b - a)), 1) if b > a else 0.0
    except Exception:
        out["cpu_pct"] = None
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        out["load"] = None
    try:
        mi = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                p = line.split()
                mi[p[0].rstrip(":")] = int(p[1])  # kB
        tot, avail = mi.get("MemTotal", 0), mi.get("MemAvailable", 0)
        out["mem_used_mb"] = round((tot - avail) / 1024)
        out["mem_total_mb"] = round(tot / 1024)
        out["mem_pct"] = round(100 * (tot - avail) / tot, 1) if tot else None
    except Exception:
        out["mem_used_mb"] = out["mem_total_mb"] = out["mem_pct"] = None
    try:
        t, u, _ = shutil.disk_usage("/")
        out["disk_used_gb"] = round(u / 1e9, 1)
        out["disk_total_gb"] = round(t / 1e9, 1)
        out["disk_pct"] = round(100 * u / t, 1)
    except Exception:
        out["disk_used_gb"] = out["disk_total_gb"] = out["disk_pct"] = None
    temps = []
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        tv = _read_first(f"{z}/temp")
        if tv and tv.lstrip("-").isdigit():
            temps.append({"label": _read_first(f"{z}/type") or os.path.basename(z),
                          "c": round(int(tv) / 1000.0, 1)})
    out["temps"] = temps
    out["model"] = _read_first("/proc/device-tree/model") or "unknown"
    return out


@tool("discover_sensors",
      "Scan the machine for sensors, buses (i2c/spi/1-wire), cameras, thermals and USB devices.",
      "")
def discover_sensors(ctx: ToolContext, **_):
    info = collect_hardware()
    ctx.mem.remember("hardware", info)
    # Persist a readable copy too.
    out = Path(ctx.cfg.workspace) / "hardware.json"
    out.write_text(json.dumps(info, indent=2), encoding="utf-8")
    ctx.add_artifact("hardware.json", "hardware inventory")
    summary = {
        "model": info.get("model"),
        "thermals": info.get("thermals"),
        "i2c_buses": info.get("i2c_buses"),
        "spi": info.get("spi_devices"),
        "onewire": info.get("onewire"),
        "cameras": info.get("video_devices"),
        "hwmon": [h["name"] for h in info.get("hwmon", [])],
    }
    return json.dumps(summary, indent=2)


# ----------------------------------------------------------------------
# Dashboards
# ----------------------------------------------------------------------
@tool("make_dashboard",
      "Save an HTML page to the dashboards folder (served by the web UI).",
      "title: str, html: str, filename: str = 'dashboard.html'")
def make_dashboard(ctx: ToolContext, title: str = "Dashboard", html: str = "",
                   filename: str = "", **_):
    if not filename:
        filename = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") + ".html"
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    if not filename.endswith(".html"):
        filename += ".html"
    try:
        out = Path(ctx.cfg.dashboards) / filename
        out.write_text(html, encoding="utf-8")
        rel = f"dashboards/{filename}"
        ctx.add_artifact(rel, f"dashboard: {title}")
        return f"saved dashboard to {rel}"
    except Exception as e:
        return f"ERROR: {e}"


# ----------------------------------------------------------------------
# Alerts + memory
# ----------------------------------------------------------------------
@tool("send_alert", "Send a push notification to your human. Use sparingly.",
      "message: str, title: str = 'Agent'")
def send_alert(ctx: ToolContext, message: str = "", title: str = "Agent", **_):
    ok = ctx.alerter.send(message, title=title)
    ctx.mem.add_journal("alert", title, message, ok=ok)
    return "alert sent" if ok else "alert not sent (check alerts config)"


@tool("self_update",
      "Request a code self-update. A privileged, root-only updater pulls from the "
      "trusted git remote, syntax-checks, re-seals the safeguard and restarts — "
      "the agent itself is NOT allowed to write its own code.", "")
def self_update(ctx: ToolContext, **_):
    if not ctx.cfg.get("selfupdate", "enabled", default=True):
        return "self-update disabled in config"
    # By design the agent runs unprivileged with a read-only code dir, so it
    # cannot (and must not) modify itself directly. It drops a request marker
    # that the root `drongo-update` service validates and applies safely.
    try:
        marker = Path(ctx.cfg.workspace) / "UPDATE_REQUESTED"
        marker.write_text(f"requested {int(time.time())}\n", encoding="utf-8")
        return ("update requested. The privileged updater will fetch the trusted "
                "remote, run a syntax check, re-seal the safeguard and restart me "
                "on its next run (rolling back automatically if the new code is "
                "broken). I can't touch my own code directly — that's the point.")
    except Exception as e:
        return f"ERROR: {e}"


@tool("save_skill",
      "Save a reusable code snippet/pattern you got working, so you (and future "
      "projects) can reuse it instead of reinventing it.",
      "name: str, description: str, code: str")
def save_skill(ctx: ToolContext, name: str = "", description: str = "", code: str = "", **_):
    return (f"saved skill '{name}'" if ctx.mem.add_skill(name, description, code)
            else "ERROR: a skill needs a name and some code")


@tool("recall_skill",
      "Get a saved skill's code by name. Call with no name to list what you have.",
      "name: str = ''")
def recall_skill(ctx: ToolContext, name: str = "", **_):
    if not name:
        names = [s["name"] for s in ctx.mem.skills()]
        return "saved skills: " + (", ".join(names) if names else "(none yet)")
    s = ctx.mem.get_skill(name)
    return f"# {s['name']} — {s['desc']}\n{s['code']}" if s else f"no skill named '{name}'"


@tool("request_package",
      "Ask your human to apt-install a SYSTEM package you can't get via pip (a "
      "compiler, a C library, a CLI tool). You can't sudo; they review it on the "
      "dashboard. For Python libraries just use `pip install` instead.",
      "name: str, reason: str")
def request_package(ctx: ToolContext, name: str = "", reason: str = "", **_):
    return (f"requested '{name}' — your human will review it on the dashboard's Files tab"
            if ctx.mem.request_package(name, reason) else "ERROR: need a package name")


@tool("save_note",
      "Save a durable research finding or fact you learned (an API shape, how a "
      "sensor works, a useful URL), to recall in future projects.",
      "topic: str, content: str")
def save_note(ctx: ToolContext, topic: str = "", content: str = "", **_):
    return (f"noted '{topic}'" if ctx.mem.add_note(topic, content)
            else "ERROR: a note needs some content")


@tool("recall_notes",
      "Search your saved research notes by keyword (blank = your most recent notes).",
      "query: str = ''")
def recall_notes(ctx: ToolContext, query: str = "", **_):
    hits = ctx.mem.search_notes(query)
    return "\n\n".join(f"# {n.get('topic') or '(note)'}\n{n['content'][:600]}"
                       for n in hits) or "no matching notes"


@tool("recall_knowledge",
      "Search EVERYTHING you've learned (saved skills, notes and lessons) for what's "
      "most relevant to a query. Use it when starting or stuck on a project, to reuse "
      "past work instead of redoing it.",
      "query: str")
def recall_knowledge(ctx: ToolContext, query: str = "", **_):
    items = ctx.mem.relevant_knowledge(query, k=6)
    if not items:
        return "no relevant knowledge saved yet"
    return "\n".join(f"[{d['kind']}] {d['title']}: {d['text']}".replace(": \n", "\n").rstrip(": ")
                     for d in items)


@tool("remember", "Store a fact in long-term memory.", "key: str, value: str")
def remember(ctx: ToolContext, key: str = "", value: str = "", **_):
    ctx.mem.remember(key, value)
    return f"remembered '{key}'"


@tool("recall", "Retrieve a fact from long-term memory.", "key: str")
def recall(ctx: ToolContext, key: str = "", **_):
    val = ctx.mem.recall(key)
    return json.dumps(val) if val is not None else "(nothing stored)"
