#!/usr/bin/env python3
"""DRONGO scoped package installer — a ROOT helper (sibling of updater.py).

Why this exists: the agent runs as the unprivileged 'drongo' user inside a
systemd sandbox (NoNewPrivileges=true, ProtectSystem=strict), so it CANNOT
install system packages itself — and giving it sudo wouldn't help, because apt
can't write /usr inside that sandbox anyway. Instead the agent REQUESTS a
package (dashboard/ request_package) and this root helper, run from a systemd
timer, fulfils the request under tight control.

Security model — this is the whole point, read it:
  * It NEVER runs an arbitrary command. It only ever runs `apt-get install` of
    package NAMES that pass a strict regex (lowercase Debian names; no options,
    no paths, no local .deb, no shell metacharacters). Names are passed as
    argv after a literal `--`, so nothing can be smuggled in as an apt option
    (which is the classic sudo-apt root-exec bypass). Worst case a confused
    agent installs a real Debian package — never arbitrary root code.
  * It reads the agent DB READ-ONLY (never writes it, so no ownership mess),
    and only touches the request queue + the install policy.
  * Policy (set from the dashboard) decides WHICH requested packages are
    allowed. Default is manual + empty allowlist => it installs NOTHING until
    a human allows something. 'auto' installs any validly-named request.

Note on the trust boundary: the dashboard and the agent run as the same user
(drongo) and share the DB, so the allowlist is a convenience/safety control,
not a hard wall against a rogue agent — the HARD guarantee is the name check
above (no arbitrary root). For a hard allowlist, keep mode=manual and curate it.

Stdlib only, so it runs even if the agent venv is broken.
"""

import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

RUNTIME = os.environ.get("DRONGO_RUNTIME", "/var/lib/drongo/runtime")
DB = os.environ.get("DRONGO_DB", f"{RUNTIME}/state/agent.db")
AGENT_USER = os.environ.get("DRONGO_USER", "drongo")
ETC = os.environ.get("DRONGO_ETC", "/etc/drongo")
# Root-owned HARD allow-list: the agent user can't edit this (it lives in /etc,
# root-owned), so these grants are tamper-proof — unlike the dashboard list.
ROOT_ALLOW = Path(ETC) / "pkg-allow.conf"
DONE_DIR = Path(RUNTIME) / "workspace" / ".pkg-installed"
STATE = Path(os.environ.get("DRONGO_OBS_STATE", "/var/lib/drongo/observer")) / "pkg-installer.json"

# Debian package names: start alnum, then alnum / + / - / . — nothing else.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,60}$")
APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C",
           "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}


def log(m):
    print(f"[pkg {time.strftime('%H:%M:%S')}] {m}", flush=True)


def valid_pkg(name: str) -> bool:
    return bool(name) and bool(NAME_RE.match(name)) and ".." not in name and not name.startswith("-")


def read_db_ro():
    """Return (requested_names, policy) from the agent DB, opened READ-ONLY."""
    import sqlite3
    reqs, policy = [], {"mode": "manual", "allow": []}
    if not os.path.exists(DB):
        return reqs, policy
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        rows = {k: v for k, v in con.execute(
            "SELECT key,value FROM kv WHERE key IN ('pkg_requests','pkg_policy')")}
        con.close()
    except Exception as e:
        log(f"db read failed: {e}")
        return reqs, policy
    try:
        raw = json.loads(rows.get("pkg_requests", "[]"))
        reqs = [p.get("name", "") for p in raw if isinstance(p, dict) and p.get("name")]
    except Exception:
        pass
    try:
        p = json.loads(rows.get("pkg_policy", "") or "{}")
        if isinstance(p, dict):
            policy = {"mode": "auto" if p.get("mode") == "auto" else "manual",
                      "allow": [str(x) for x in (p.get("allow") or []) if x]}
    except Exception:
        pass
    return reqs, policy


def read_root_allow():
    """Parse the root-owned hard allow-list: one apt name/glob per line, # comments."""
    pats = []
    try:
        for ln in ROOT_ALLOW.read_text().splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ln:
                pats.append(ln)
    except Exception:
        pass
    return pats


def permitted(name: str, policy: dict, root_allow=()) -> bool:
    # Tamper-proof root list wins first; then the (soft, dashboard-editable) policy.
    if any(fnmatch.fnmatch(name, p) for p in root_allow):
        return True
    if policy["mode"] == "auto":
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in policy["allow"])


def apt(args, timeout=600):
    p = subprocess.run(["apt-get", *args], env=APT_ENV,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def mark_done(name: str, ok: bool, detail: str = ""):
    """Drop a drongo-owned marker so the agent learns the outcome and clears the
    request. We only ever create files (never touch the DB) to avoid ownership
    problems."""
    try:
        import pwd                                       # POSIX-only; lazy so tests import on any OS
        DONE_DIR.mkdir(parents=True, exist_ok=True)
        (DONE_DIR / name).write_text(json.dumps(
            {"ok": ok, "ts": time.time(), "detail": detail[:300]}))
        u = pwd.getpwnam(AGENT_USER)
        os.chown(DONE_DIR, u.pw_uid, u.pw_gid)
        os.chown(DONE_DIR / name, u.pw_uid, u.pw_gid)
    except Exception as e:
        log(f"mark_done failed for {name}: {e}")


def main():
    reqs, policy = read_db_ro()
    if not reqs:
        return
    root_allow = read_root_allow()
    seen = list(dict.fromkeys(reqs))                     # de-dup, keep order
    for n in seen:
        if not valid_pkg(n):
            log(f"REJECTED invalid package name: {n!r}")
    todo = [n for n in seen if valid_pkg(n) and permitted(n, policy, root_allow)]
    if not todo:
        log(f"{len(seen)} request(s); none permitted by policy (mode={policy['mode']}).")
        return
    log(f"policy={policy['mode']}; installing: {', '.join(todo)}")
    apt(["update", "-y"], timeout=300)                   # refresh once per run
    for name in todo:
        rc, out = apt(["install", "-y", "--no-install-recommends", "--", name])
        if rc == 0:
            log(f"installed {name}")
            mark_done(name, True)
        elif "unable to locate package" in out.lower() or "has no installation candidate" in out.lower():
            log(f"no such package: {name}")
            mark_done(name, False, "package not found")
        else:                                            # transient (network etc.) — retry next tick
            log(f"install failed for {name} (rc={rc}); will retry. {out[-200:]}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"pkg-installer error: {e}")
        sys.exit(0)                                      # never fail the unit; try again next tick
