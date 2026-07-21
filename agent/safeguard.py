"""DRONGO SAFEGUARD CORE  —  tamper-resistant safety policy.

    ┌──────────────────────────────────────────────────────────────────┐
    │  THIS FILE IS THE AGENT'S CONSCIENCE. IT MUST NOT BE WRITABLE BY  │
    │  THE AGENT.  Install it root-owned, mode 0444:                    │
    │      sudo chown root:root safeguard.py && sudo chmod 0444 ...     │
    │  The agent runs as the unprivileged 'drongo' user and therefore   │
    │  physically cannot modify this file. The OS enforces that; the    │
    │  code below merely *detects* tampering and refuses to operate if  │
    │  the enforcement has been weakened.                               │
    └──────────────────────────────────────────────────────────────────┘

Philosophy (per CLAUDE.md): "Local Autonomy, Global Isolation."

Three responsibilities live here, deliberately in ONE immutable file:
  1. check_command()  — denylist of destructive / self-harming commands.
  2. integrity        — verify_self(): the guard checks its own ownership,
                        permissions and SHA-256 against a root-owned sidecar.
  3. confinement      — safe_join() + posix resource limits for child procs.

Nothing in here imports agent code, reads the agent-writable config, or can
be toggled off from the agent's config. Policy lives in code, in this file.
"""

from __future__ import annotations

import hashlib
import os
import re

try:                      # 'resource' is POSIX-only; absent on the Windows dev box.
    import resource
except ImportError:       # pragma: no cover
    resource = None


# ---------------------------------------------------------------------------
# 1. COMMAND DENYLIST
# ---------------------------------------------------------------------------
# Matched case-insensitively against the raw command string. These protect the
# system, the agent's own guard rails, and the watchdog from being disabled.

BUILTIN_DENY = [
    # --- destroying the filesystem / disks ---
    # recursive-force rm aimed at a root-ish target (rm -rf / , -rf ~ , -rf * , -rf /etc …)
    r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\s+(/|~|\$home|\*)",
    r"\brm\s+-[a-z]*f[a-z]*r[a-z]*\s+(/|~|\$home|\*)",
    r"\brm\b[^|;&]*--recursive[^|;&]*(/|~|\$home|\*)",
    # any rm naming a bare root, /*, ~ or $HOME as a target, whatever the flags
    r"\brm\b[^|;&]*\s(/|/\*|~|\$home)(\s|$)",
    r":\(\)\s*\{.*\};:",                                  # fork bomb
    r"\b(mkfs|mke2fs|mkswap|swapoff|fdisk|parted|sgdisk|wipefs)\b",
    r"\bdd\b.*\bof=/dev/(sd|mmcblk|nvme|vd|disk)",
    r">\s*/dev/(sd|mmcblk|nvme|vd|disk)",
    r"\bchmod\s+-[a-z]*r[a-z]*\s+[0-7]+\s+/",             # chmod -R <mode> /
    # --- powering off / rebooting (only the observer may do that) ---
    r"\b(shutdown|reboot|poweroff|halt|telinit|init\s+0|init\s+6)\b",
    r"\bsystemctl\s+(reboot|poweroff|halt|kexec)\b",
    # --- disabling the safety machinery / watchdog / observer ---
    r"\bsystemctl\s+(stop|disable|mask|kill)\s+.*\bdrongo[-a-z]*\b",
    r"\b(pkill|kill(all)?)\b.*\b(observer|watchdog|systemd)\b",
    r"/dev/watchdog",
    r"\bwdctl\b",
    r"\bchattr\b.*[+-]i",
    r"\bcrontab\s+-r\b",
    # --- tampering with the guard's own files / protected dirs (defence in
    #     depth; the filesystem already blocks these for the drongo user) ---
    r"\b(safeguard|observer)\.py\b",
    r"\.sha256\b",
    # --- account / privilege manipulation ---
    r"\b(userdel|groupdel|usermod|adduser|useradd|visudo)\b",
    r"\bpasswd\b(?!\s+-S)",
    r"\bsudoers\b",
    # --- I2C bus probing: on this SoC the PMIC/RTC live on I2C and actively
    #     scanning it HARD-LOCKS the whole board (a freeze the watchdog can't
    #     catch). Safe hardware discovery goes through discover_sensors / sysfs,
    #     never these. Blocks i2cdetect and the i2c read/write CLIs outright.
    r"\bi2cdetect\b",
    r"\bi2c(set|get|dump|transfer)\b",
    # --- network / egress controls (the "Data Cage" must stay up) ---
    r"\b(iptables|nft|nftables)\b.*\b(-f|flush|delete)\b",
    r"\bufw\s+disable\b",
    # --- pulling code straight into a shell ---
    r"\b(curl|wget|fetch)\b[^|]*\|\s*(sudo\s+)?(bash|sh|zsh|python3?)\b",
    r"\bgit\s+push\b.*--force",
]

# Write-capable verbs we don't want aimed at protected locations.
_WRITE_VERBS = r"(?:>>?|cp|mv|tee|install|ln|rm|chmod|chown|chgrp|chattr|" \
               r"truncate|dd|rsync|sed\s+-i|patch)"
PROTECTED_PATHS = [
    "/opt/drongo", "/etc", "/usr", "/bin", "/sbin", "/lib", "/boot",
    "/var/lib/drongo/state", "/etc/systemd", "/etc/cron",
]


class CommandRejected(Exception):
    """Raised when a shell command violates policy."""


def check_command(command: str, *, allow_sudo: bool = False, extra_deny=None) -> None:
    cmd = (command or "").strip()
    if not cmd:
        raise CommandRejected("empty command")
    low = cmd.lower()

    for pat in list(BUILTIN_DENY) + list(extra_deny or []):
        if re.search(pat, low):
            raise CommandRejected(f"blocked by safety pattern: {pat}")

    if not allow_sudo and re.search(r"(^|\s|;|&|\||`)sudo\b", low):
        raise CommandRejected("sudo is disabled (set tools.shell.allow_sudo to enable)")

    # Block writes that escape the workspace via redirection. Only relative
    # paths (which stay in the workspace cwd), /tmp/* and /dev/null are allowed;
    # reject any absolute redirect, and any '..' traversal even under /tmp.
    for m in re.finditer(r">>?\s*(/\S+)", cmd):
        target = m.group(1)
        allowed = (target == "/dev/null"
                   or target == "/tmp"
                   or target.startswith("/tmp/"))
        if ".." in target or not allowed:
            raise CommandRejected(f"refusing to write outside workspace: {target}")

    # ...and writes aimed at protected system paths via common verbs. Match the
    # protected path only as a STANDALONE absolute path — preceded by start, a
    # space or a shell operator, and not continuing into a longer word — so the
    # agent's OWN venv (e.g. /var/lib/drongo/runtime/venv/bin/pip, which contains
    # "/bin") and its workspace under /var/lib (which contains "/lib") are not
    # mistaken for system /bin or /lib. `pip install <pkg>` must be allowed.
    _bound = r"(?:^|[\s;&|`'\"=(><])"
    for p in PROTECTED_PATHS:
        if re.search(_WRITE_VERBS + r"\b[^\n]*" + _bound + re.escape(p) + r"(?![\w-])", low):
            raise CommandRejected(f"refusing to modify protected path: {p}")


# ---------------------------------------------------------------------------
# 2. INTEGRITY  —  the guard verifies it has not been tampered with
# ---------------------------------------------------------------------------
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def integrity_status() -> dict:
    """Describe this file's on-disk security posture."""
    path = os.path.realpath(__file__)
    st = os.stat(path)
    sidecar = path + ".sha256"
    expected = None
    if os.path.isfile(sidecar):
        try:
            expected = open(sidecar).read().split()[0].strip()
        except Exception:
            expected = None
    digest = _sha256(path)
    return {
        "path": path,
        "owner_uid": st.st_uid,
        "owner_is_root": st.st_uid == 0,
        "mode": oct(st.st_mode & 0o777),
        "group_writable": bool(st.st_mode & 0o020),
        "world_writable": bool(st.st_mode & 0o002),
        "writable_by_me": os.access(path, os.W_OK),
        "running_uid": os.geteuid() if hasattr(os, "geteuid") else -1,
        "sha256": digest,
        "expected_sha256": expected,
        "hash_ok": (expected is None) or (expected == digest),
        "sidecar_present": expected is not None,
    }


def verify_self(strict: bool | None = None) -> tuple[bool, list[str]]:
    """Return (ok, problems). In strict mode any problem means 'do not run'."""
    if strict is None:
        strict = os.environ.get("DRONGO_SAFEGUARD_STRICT", "0") == "1"
    s = integrity_status()
    problems = []

    if not s["hash_ok"]:
        problems.append("SHA-256 mismatch — safeguard.py has been modified!")
    if s["world_writable"] or s["group_writable"]:
        problems.append(f"safeguard.py is group/world writable ({s['mode']})")

    running_as_root = s["running_uid"] == 0
    if s["writable_by_me"] and not running_as_root:
        problems.append("the agent's own user can WRITE safeguard.py — tamper risk")
    if strict and not s["owner_is_root"] and not running_as_root:
        problems.append("safeguard.py is not owned by root")
    if strict and not s["sidecar_present"]:
        problems.append("no .sha256 sidecar to verify against (run 'drongo seal' as root)")

    return (len(problems) == 0), problems


def enforce_or_die(strict: bool | None = None, logger=None, alerter=None) -> None:
    """Verify integrity; in strict mode, abort the process if compromised."""
    if strict is None:
        strict = os.environ.get("DRONGO_SAFEGUARD_STRICT", "0") == "1"
    ok, problems = verify_self(strict=strict)
    if ok:
        if logger:
            logger.info("Safeguard integrity OK (%s).",
                        integrity_status()["mode"])
        return
    msg = "SAFEGUARD INTEGRITY FAILURE:\n  - " + "\n  - ".join(problems)
    if logger:
        logger.error(msg)
    if alerter is not None:
        try:
            alerter.send(msg, title="DRONGO safeguard compromised", priority="urgent")
        except Exception:
            pass
    if strict:
        # Fail closed. Better a dead agent than an unguarded one.
        raise SystemExit(3)


# ---------------------------------------------------------------------------
# 3. CONFINEMENT  —  workspace jail + child-process resource limits
# ---------------------------------------------------------------------------
def safe_join(workspace: str, path: str) -> str:
    """Resolve `path` and guarantee it stays inside `workspace`."""
    ws = os.path.realpath(workspace)
    candidate = path if os.path.isabs(path) else os.path.join(ws, path)
    full = os.path.realpath(candidate)
    if full != ws and not full.startswith(ws + os.sep):
        raise CommandRejected(f"path escapes workspace: {path}")
    return full


def posix_limits(mem_mb: int = 512, cpu_seconds: int = 120, nofile: int = 256,
                 nproc: int = 128, fsize_mb: int = 512):
    """Return a preexec_fn capping a child process (no-op off POSIX).

    This is the PER-PROCESS cage (address space, CPU time, open files, process
    count, single-file size, no core dumps). It is deliberately paired with the
    systemd unit's cgroup cage (MemoryMax + MemorySwapMax=0 + TasksMax), which
    bounds the whole process TREE — an rlimit can't, since each fork gets its own
    fresh budget. Defaults are sized for a 4GB box: modest RAM, a bounded pid
    count (so a naive fork bomb hits a wall), and a file-size cap so a runaway
    can't fill the SD card. Callers tighten these further for untrusted code."""
    if resource is None or os.name != "posix":
        return None

    def _apply():
        for what, soft, hard in (
            (resource.RLIMIT_AS, mem_mb * 1024 * 1024, mem_mb * 1024 * 1024),
            (resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 5),
            (resource.RLIMIT_NOFILE, nofile, nofile),
            (resource.RLIMIT_NPROC, nproc, nproc),
            (resource.RLIMIT_FSIZE, fsize_mb * 1024 * 1024, fsize_mb * 1024 * 1024),
            (resource.RLIMIT_CORE, 0, 0),   # never dump a multi-GB core on OOM
        ):
            try:
                resource.setrlimit(what, (soft, hard))
            except Exception:
                pass
        try:
            os.nice(10)               # be a polite background citizen
        except Exception:
            pass

    return _apply


def self_seal() -> str:
    """(Re)write the .sha256 sidecar for this file. Run as root by the installer."""
    path = os.path.realpath(__file__)
    digest = _sha256(path)
    with open(path + ".sha256", "w") as fh:
        fh.write(digest + "  safeguard.py\n")
    return digest
