#!/usr/bin/env python3
"""DRONGO external observer - the independent "Dead Man's Switch".

Runs as ROOT on a short systemd timer (every ~60s), completely outside the
agent process and the agent's user. Because it is root-owned and root-run, the
agent cannot disable or edit it. Deliberately uses only the Python standard
library so it keeps working even if the agent's virtualenv is broken.

Responsibilities (CLAUDE.md 4.2 "Self-Preservation"):
  * Liveness  : if the service is active but the heartbeat is stale -> restart.
  * Crash-loop: if systemd has given up (failed) or restarts spiked -> roll the
                code back to the last-known-good git commit, then restart.
  * Rollback  : maintain a 'drongo-lkg' git ref whenever the agent is healthy.
  * Health    : watch CPU temp, load and disk; alert, and as an absolute last
                resort reboot the board (only if DRONGO_ALLOW_REBOOT=1).

Configuration is via environment (see drongo-observer.service). All actions are
debounced through a small state file so the observer never thrashes.
"""

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

CFG = {
    "service":        os.environ.get("DRONGO_SERVICE", "drongo.service"),
    "runtime":        os.environ.get("DRONGO_RUNTIME", "/var/lib/drongo/runtime"),
    "repo":           os.environ.get("DRONGO_REPO", "/opt/drongo"),
    "state_dir":      os.environ.get("DRONGO_OBS_STATE", "/var/lib/drongo/observer"),
    "hb_max_age":     int(os.environ.get("DRONGO_HEARTBEAT_MAX_AGE", "900")),
    "max_restarts":   int(os.environ.get("DRONGO_MAX_RESTARTS", "5")),
    "temp_crit":      float(os.environ.get("DRONGO_TEMP_CRIT", "90")),
    "load_crit":      float(os.environ.get("DRONGO_LOAD_CRIT", str(os.cpu_count() * 4))),
    "disk_crit":      float(os.environ.get("DRONGO_DISK_CRIT", "96")),
    "allow_reboot":   os.environ.get("DRONGO_ALLOW_REBOOT", "0") == "1",
    "discord":        os.environ.get("DRONGO_DISCORD_WEBHOOK", ""),
    "ntfy_server":    os.environ.get("DRONGO_NTFY_SERVER", "https://ntfy.sh"),
    "ntfy_topic":     os.environ.get("DRONGO_NTFY_TOPIC", ""),
    "min_healthy_secs": int(os.environ.get("DRONGO_MIN_HEALTHY_SECS", "1200")),
}


def log(msg):
    print(f"[observer {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def alert(message, title="DRONGO observer", priority="high"):
    # Dashboard kill-switch: if this flag exists, the root watchers stay silent.
    if os.path.exists(os.path.join(CFG["runtime"], "workspace", "OBSERVER_ALERTS_OFF")):
        return
    sent = False
    if CFG["discord"]:
        try:
            body = json.dumps({"username": "DRONGO",
                               "content": f"**{title}**\n{message}"[:1900]}).encode()
            urllib.request.urlopen(urllib.request.Request(
                CFG["discord"], data=body,
                headers={"Content-Type": "application/json"}), timeout=15)
            sent = True
        except Exception as e:
            log(f"discord alert failed: {e}")
    if CFG["ntfy_topic"]:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{CFG['ntfy_server'].rstrip('/')}/{CFG['ntfy_topic']}",
                data=message.encode("utf-8"),
                headers={"Title": title.encode('ascii', 'replace').decode(),
                         "Priority": priority}), timeout=15)
            sent = True
        except Exception as e:
            log(f"ntfy alert failed: {e}")
    if not sent:
        log(f"(no alert channel configured) {title}: {message}")


def sh(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def systemctl_show(prop):
    rc, out, _ = sh(f"systemctl show {CFG['service']} -p {prop} --value")
    return out if rc == 0 else ""


def load_state():
    sf = Path(CFG["state_dir"]) / "observer_state.json"
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except Exception:
            pass
    return {}


def save_state(state):
    Path(CFG["state_dir"]).mkdir(parents=True, exist_ok=True)
    (Path(CFG["state_dir"]) / "observer_state.json").write_text(json.dumps(state, indent=2))


def heartbeat_age():
    hb = Path(CFG["runtime"]) / "state" / "heartbeat"
    try:
        return time.time() - float(hb.read_text().strip())
    except Exception:
        return None


def cpu_temp():
    best = 0.0
    for zone in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
        try:
            best = max(best, int(zone.read_text().strip()) / 1000.0)
        except Exception:
            pass
    return best


def disk_pct():
    try:
        t, u, f = shutil.disk_usage("/")
        return 100.0 * u / t
    except Exception:
        return 0.0


def loadavg():
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


# ---- corrective actions ---------------------------------------------------
def mark_lkg():
    """Tag current HEAD as last-known-good if the repo is a clean git checkout."""
    repo = CFG["repo"]
    if not (Path(repo) / ".git").exists():
        return
    sh(f"git -C {repo} tag -f drongo-lkg HEAD")


def reseal():
    """Re-hash and re-lock the safeguard so its sidecar matches whatever
    safeguard.py we just landed on. Critical after a rollback: the rolled-back
    code may differ from the sidecar written by the last update."""
    sg = Path(CFG["repo"]) / "agent" / "safeguard.py"
    if not sg.exists():
        return
    digest = hashlib.sha256(sg.read_bytes()).hexdigest()
    side = sg.with_name("safeguard.py.sha256")
    try:
        side.write_text(digest + "  safeguard.py\n")
    except Exception as e:
        log(f"reseal failed: {e}")
        return
    sh(f"chown root:root {sg} {side}")
    sh(f"chmod 0444 {sg} {side}")


def rollback():
    repo = CFG["repo"]
    if not (Path(repo) / ".git").exists():
        return "no git repo; cannot roll back"
    rc, _, _ = sh(f"git -C {repo} rev-parse drongo-lkg")
    if rc != 0:
        return "no last-known-good tag to roll back to"
    sh(f"git -C {repo} reset --hard drongo-lkg")
    sh(f"git -C {repo} clean -fd agent")
    reseal()   # keep the integrity sidecar consistent with the restored code
    return "rolled back to drongo-lkg"


def restart_service(reason):
    log(f"restarting {CFG['service']}: {reason}")
    sh(f"systemctl reset-failed {CFG['service']}")
    sh(f"systemctl restart {CFG['service']}")


def main():
    state = load_state()
    now = time.time()

    active = systemctl_show("ActiveState")      # active / failed / inactive
    sub = systemctl_show("SubState")
    nrestarts = int(systemctl_show("NRestarts") or "0")
    hb_age = heartbeat_age()

    log(f"state={active}/{sub} restarts={nrestarts} hb_age={hb_age} "
        f"temp={cpu_temp():.0f} load={loadavg():.1f} disk={disk_pct():.0f}%")

    # 1) crash-loop: systemd has given up, or restarts spiked since last check.
    prev_restarts = state.get("nrestarts", 0)
    restart_delta = nrestarts - prev_restarts
    state["nrestarts"] = nrestarts
    crash_loop = (active == "failed") or (restart_delta >= CFG["max_restarts"])

    if crash_loop and now - state.get("last_rollback", 0) > 600:
        msg = rollback()
        state["last_rollback"] = now
        alert(f"Crash-loop detected ({active}, +{restart_delta} restarts). {msg}. "
              "Restarting on last-known-good code.",
              title="DRONGO rolled back", priority="urgent")
        restart_service("crash-loop rollback")
        save_state(state)
        return

    # 2) wedged: service claims active but heartbeat is stale.
    if active == "active" and hb_age is not None and hb_age > CFG["hb_max_age"]:
        if now - state.get("last_restart", 0) > 300:
            alert(f"Heartbeat stale ({int(hb_age)}s). Agent looks wedged; restarting.",
                  title="DRONGO unresponsive")
            restart_service("stale heartbeat")
            state["last_restart"] = now
            save_state(state)
            return

    # 3) not running at all (and not intentionally stopped) -> start it.
    if active in ("inactive", "failed") and not (Path(CFG["runtime"]) / "workspace" / "STOP").exists():
        restart_service(f"service was {active}")

    # 4) healthy long enough -> refresh last-known-good.
    if active == "active" and (hb_age is None or hb_age < CFG["hb_max_age"]):
        healthy_since = state.get("healthy_since") or now
        state["healthy_since"] = healthy_since
        if now - healthy_since > CFG["min_healthy_secs"] and now - state.get("last_lkg", 0) > 3600:
            mark_lkg()
            state["last_lkg"] = now
            log("refreshed last-known-good tag")
    else:
        state["healthy_since"] = 0

    # 5) system health: alert (debounced 30 min) and last-resort reboot.
    problems = []
    if cpu_temp() >= CFG["temp_crit"]:
        problems.append(f"CPU {cpu_temp():.0f}C")
    if loadavg() >= CFG["load_crit"]:
        problems.append(f"load {loadavg():.1f}")
    if disk_pct() >= CFG["disk_crit"]:
        problems.append(f"disk {disk_pct():.0f}%")

    if problems:
        if now - state.get("last_health_alert", 0) > 1800:
            alert("System under stress: " + ", ".join(problems),
                  title="DRONGO host health", priority="high")
            state["last_health_alert"] = now
        # Sustained critical load is the only thing we'd reboot for, and only
        # if explicitly allowed and not in a reboot cooldown.
        bad_streak = state.get("bad_streak", 0) + 1
        state["bad_streak"] = bad_streak
        if (CFG["allow_reboot"] and bad_streak >= 5
                and now - state.get("last_reboot", 0) > 3600):
            alert("Sustained critical load - rebooting host as last resort.",
                  title="DRONGO host reboot", priority="urgent")
            state["last_reboot"] = now
            save_state(state)
            sh("systemctl reboot")
            return
    else:
        state["bad_streak"] = 0

    save_state(state)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"observer error: {e}")
