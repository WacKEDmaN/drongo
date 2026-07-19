#!/usr/bin/env python3
"""DRONGO privileged updater - the ONLY thing allowed to change the code.

Runs as ROOT from a systemd timer (and on demand). The agent can REQUEST an
update (it drops a marker file) but can never write its own code, because its
install dir is root-owned and read-only to it. This script performs the update
under controlled conditions and rolls back on any failure:

  1. fetch + ff-only pull from the trusted remote
  2. byte-compile the package (syntax gate)
  3. re-seal the safeguard (.sha256) and re-lock its permissions (0444 root)
  4. restart the agent; if it fails to come up clean, reset to drongo-lkg

Stdlib only, so it works even if the agent venv is broken.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

def _env(name, default=""):
    """Read an env var, tolerating an inline '# comment' after the value (systemd
    EnvironmentFile keeps those) and surrounding whitespace — otherwise int() on
    a commented line crashes the updater at import."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = raw.split("#", 1)[0].strip()
    return raw if raw != "" else default


def _envint(name, default):
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


REPO = _env("DRONGO_REPO", "/opt/drongo")
RUNTIME = _env("DRONGO_RUNTIME", "/var/lib/drongo/runtime")
SERVICE = _env("DRONGO_SERVICE", "drongo.service")
VENV_PY = _env("DRONGO_PYTHON", f"{REPO}/.venv/bin/python")
DISCORD = _env("DRONGO_DISCORD_WEBHOOK", "")
NTFY_SERVER = _env("DRONGO_NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = _env("DRONGO_NTFY_TOPIC", "")
# How often to do a routine pull even without an explicit request (seconds).
ROUTINE_INTERVAL = _envint("DRONGO_UPDATE_INTERVAL", 86400)
MARKER = Path(RUNTIME) / "workspace" / "UPDATE_REQUESTED"
STATE = Path(_env("DRONGO_OBS_STATE", "/var/lib/drongo/observer")) / "updater.json"


def log(m):
    print(f"[updater {time.strftime('%H:%M:%S')}] {m}", flush=True)


def alert(message, title="DRONGO updater", priority="default"):
    # Shares the observer's dashboard kill-switch flag.
    if os.path.exists(os.path.join(RUNTIME, "workspace", "OBSERVER_ALERTS_OFF")):
        return
    if DISCORD:
        try:
            body = json.dumps({"username": "DRONGO",
                               "content": f"**{title}**\n{message}"[:1900]}).encode()
            urllib.request.urlopen(urllib.request.Request(
                DISCORD, data=body, headers={"Content-Type": "application/json"}), timeout=15)
        except Exception:
            pass
    if NTFY_TOPIC:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}", data=message.encode(),
                headers={"Title": title.encode('ascii', 'replace').decode(),
                         "Priority": priority}), timeout=15)
        except Exception:
            pass


def sh(cmd, timeout=180):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def git(args, timeout=180):
    return sh(f"git -C {REPO} {args}", timeout)


def lock_safeguard():
    """Regenerate the hash sidecar and make the guard immutable again."""
    # Must run from $REPO so `python -m agent` imports the installed package.
    py = VENV_PY if Path(VENV_PY).exists() else "python3"
    sh(f"cd {REPO} && {py} -m agent seal")
    for f in ("agent/safeguard.py", "agent/safeguard.py.sha256"):
        sh(f"chown root:root {REPO}/{f}")
        sh(f"chmod 0444 {REPO}/{f}")


def _last_routine():
    try:
        return json.loads(STATE.read_text()).get("last_routine", 0)
    except Exception:
        return 0


def _save_routine(ts):
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"last_routine": ts}))
    except Exception:
        pass


def main():
    forced = "--force" in sys.argv
    requested = MARKER.exists()
    due = (time.time() - _last_routine()) >= ROUTINE_INTERVAL
    if not (requested or forced or due):
        log("no update requested and routine pull not yet due; nothing to do.")
        return
    if requested:
        log("update requested by the agent.")
        try:
            MARKER.unlink()
        except Exception:
            pass
    if due or forced:
        _save_routine(time.time())

    if not (Path(REPO) / ".git").exists():
        log(f"{REPO} is not a git checkout; cannot self-update.")
        return

    rc, before, _ = git("rev-parse HEAD")
    git("tag -f drongo-prev HEAD")            # safety net for this update
    git("fetch --all --prune", timeout=240)
    rc, out, err = git("pull --ff-only", timeout=240)
    rc2, after, _ = git("rev-parse HEAD")

    if before == after:
        log("already up to date.")
        lock_safeguard()                      # keep perms/hash correct anyway
        return

    log(f"updated {before[:7]} -> {after[:7]}; syntax-checking...")
    chk_rc, chk_out, chk_err = sh(f"cd {REPO} && {VENV_PY} -m compileall -q agent"
                                  if Path(VENV_PY).exists()
                                  else f"cd {REPO} && python3 -m compileall -q agent")
    if chk_rc != 0:
        log("syntax check FAILED - rolling back.")
        git(f"reset --hard {before}")
        lock_safeguard()
        alert(f"Update {after[:7]} failed syntax check; rolled back to {before[:7]}.",
              title="DRONGO update rolled back", priority="high")
        return

    lock_safeguard()
    log("restarting agent on new code.")
    sh(f"systemctl reset-failed {SERVICE}")
    sh(f"systemctl restart {SERVICE}")

    # Give it a moment; if it didn't come up, roll back.
    time.sleep(20)
    rc, state, _ = sh(f"systemctl is-active {SERVICE}")
    if state.strip() != "active":
        log("agent did not come up; rolling back to previous commit.")
        git(f"reset --hard {before}")
        lock_safeguard()
        sh(f"systemctl reset-failed {SERVICE}")
        sh(f"systemctl restart {SERVICE}")
        alert(f"New code {after[:7]} wouldn't start; rolled back to {before[:7]}.",
              title="DRONGO update rolled back", priority="high")
    else:
        alert(f"Updated to {after[:7]} and restarted cleanly.\n{out}",
              title="DRONGO updated")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"updater error: {e}")
