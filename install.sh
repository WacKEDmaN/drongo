#!/usr/bin/env bash
# ===========================================================================
#  DRONGO installer for Debian on the Rock Pi 4C+ (RK3399).
#
#  Beginner steps:
#    1. Flash Debian to the Pi, boot it, and SSH in.
#    2. Get this code onto the Pi (git clone <your fork>  OR  unzip it).
#    3. Run:   sudo ./install.sh
#    4. Answer the few setup questions, then follow the printed "NEXT" steps.
#
#  It's INTERACTIVE by default: it asks about the desktop, the local model, the
#  retro toolchain and the image generator. Every question has a sensible default
#  (just press Enter). Pass a flag to pre-answer, or --yes to accept all defaults
#  without prompting (handy for scripted installs).
#
#  What it sets up ("Local Autonomy, Global Isolation"):
#    * code at /opt/drongo       - root-owned, READ-ONLY to the agent
#    * runtime at /var/lib/drongo - the only place the agent can write
#    * safeguard.py locked 0444 root:root + sha256-sealed
#    * unprivileged 'drongo' user, sandboxed systemd unit with cgroup caps
#    * SoC hardware watchdog armed (reboots the board if the kernel wedges)
#    * external root observer + privileged updater (the "Dead Man's Switch")
#
#  The brain is the FREE CLOUD providers by default (add keys in the wizard). A
#  local Ollama model is an optional never-fail fallback, OFF unless you ask for it.
#
#  Flags (all optional — skip the matching question):
#    --local           also install a local fallback model (Ollama); off by default
#    --model NAME      install a local fallback using this Ollama model (implies --local)
#    --strip-desktop   stop the GUI to free RAM (reversible; nothing uninstalled)
#    --retro           install the Z80/Amstrad toolchain (sdcc/z88dk/CPCtelera)
#    --imggen          build the local image generator (OnnxStream — slow on a Pi)
#    --yes, -y         accept all defaults, don't prompt (non-interactive)
# ===========================================================================
set -euo pipefail

INSTALL=/opt/drongo
RUNTIME=/var/lib/drongo/runtime
ETC=/etc/drongo
AGENT_USER=drongo
# Empty = "not chosen yet" -> ask interactively (or fall back to a default when
# there's no terminal). A flag pre-answers the matching question.
MODEL=""
WANT_LOCAL=""         # install a local Ollama fallback model? off by default (cloud-first)
STRIP_DESKTOP=""
RETRO=""
IMGGEN=""
ASSUME_YES=0

# --- friendly failure: tell the beginner exactly where it broke ------------
trap 'rc=$?; [ $rc -ne 0 ] && printf "\n\033[1;31m[install] FAILED (exit %s) near line %s.\033[0m\nRead the message just above, fix it, then re-run:  sudo ./install.sh\n" "$rc" "$LINENO"' EXIT

while [ $# -gt 0 ]; do
  case "$1" in
    --strip-desktop) STRIP_DESKTOP=1 ;;
    --retro) RETRO=1 ;;
    --imggen) IMGGEN=1 ;;
    --local) WANT_LOCAL=1 ;;
    --model) [ $# -ge 2 ] || { echo "--model needs a value, e.g. --model qwen2.5:1.5b-instruct"; exit 1; }
             MODEL="$2"; WANT_LOCAL=1; shift ;;     # asking for a model means you want local
    --yes|-y) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,30p' "$0"; trap - EXIT; exit 0 ;;
    *) echo "unknown flag: $1  (try --help)"; exit 1 ;;
  esac
  shift
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }

# ASCII-only banner (no Unicode, so it survives a broken locale / dumb terminal).
banner() {
  printf '\033[1;36m'
  cat <<'ART'

    ____   ____    ___   _   _   ____    ___
   |  _ \ |  _ \  / _ \ | \ | | / ___|  / _ \
   | | | || |_) || | | ||  \| || |  _  | | | |
   | |_| ||  _ < | |_| || |\  || |_| | | |_| |
   |____/ |_| \_\ \___/ |_| \_| \____|  \___/
ART
  printf '\033[0m\033[1;90m   Digital Resource-Optimizing Neural Gadget for Overthinking\033[0m\n'
  printf '\033[1;90m   autonomous maker-agent  ~  Local Autonomy, Global Isolation\033[0m\n'
}

# Interactive prompts. Open the controlling terminal ONCE on fd 3 (so sequential
# questions read sequential answers, and it works even under `curl | sudo bash`);
# with no terminal — or --yes — every question falls back to its default.
_TTY=0
if [ "$ASSUME_YES" -ne 1 ] && [ -r /dev/tty ]; then exec 3</dev/tty && _TTY=1; fi
confirm() {  # confirm "Question?" Y|N   -> exit 0 = yes, 1 = no
  local q="$1" def="${2:-N}" ans hint
  [ "$def" = "Y" ] && hint="[Y/n]" || hint="[y/N]"
  if [ "$_TTY" -ne 1 ]; then [ "$def" = "Y" ]; return; fi
  read -rp "    $q $hint " ans <&3 || ans=""
  ans="${ans:-$def}"
  case "$ans" in [Yy]*) return 0;; *) return 1;; esac
}
ask() {      # ask "Prompt" "default"   -> echoes the answer
  local q="$1" def="$2" ans
  if [ "$_TTY" -ne 1 ]; then printf '%s' "$def"; return; fi
  read -rp "    $q [$def]: " ans <&3 || ans=""
  printf '%s' "${ans:-$def}"
}

banner

# ---------------------------------------------------------------------------
say "0/10  Pre-flight checks"
[ "$(id -u)" -eq 0 ] || { echo "Please run me with sudo:  sudo ./install.sh"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

# A long install over SSH is KILLED if the connection drops (common on SD boards /
# flaky wifi). Running inside tmux/screen makes it survive a disconnect — strongly
# recommended, especially with --retro (source builds take a long time).
if [ "$_TTY" -eq 1 ] && [ -z "${TMUX:-}${STY:-}" ]; then
  warn "TIP: if your SSH drops mid-install it gets killed. Run it inside tmux so it survives:"
  warn "       tmux new -s drongo      # then: sudo ./install.sh   (Ctrl-b d detaches)"
  warn "     Reconnect later with:  tmux attach -t drongo"
fi

# Internet (we must download packages + the model)
if ! curl -fsS --max-time 10 https://ollama.com >/dev/null 2>&1; then
  warn "Couldn't reach the internet. The installer needs it to download packages"
  warn "and the local model. Check the Pi's network and try again."
  exit 1
fi

# Free disk space (venv + a small model need a few GB)
avail_mb=$(df -Pm / | awk 'NR==2{print $4}')
[ "${avail_mb:-0}" -ge 3000 ] || warn "Only ${avail_mb}MB free on / - a model may not fit. ~3GB+ recommended."

# Suggest a model that fits this board's RAM (4C+ ships as 1/2/4 GB)
ram_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if   [ "$ram_mb" -ge 3000 ]; then DEF_MODEL="qwen2.5:3b-instruct"
elif [ "$ram_mb" -ge 1500 ]; then DEF_MODEL="qwen2.5:1.5b-instruct"
else                              DEF_MODEL="qwen2.5:0.5b-instruct"; fi
echo "    RAM: ${ram_mb}MB  ->  suggested local model: $DEF_MODEL"

# ---------------------------------------------------------------------------
# Interactive choices. Each is skipped if a flag already answered it; with no
# terminal (or --yes) every unanswered question falls back to its default.
if [ "$_TTY" -eq 1 ]; then
  say "Setup — a few quick choices (press Enter to take the default)"
  if [ -z "$WANT_LOCAL" ]; then
    if confirm 'Install a LOCAL fallback model (Ollama)? ~2GB RAM+disk; the free cloud providers are the default brain.' N; then WANT_LOCAL=1; else WANT_LOCAL=0; fi
  fi
  if [ "$WANT_LOCAL" = 1 ] && [ -z "$MODEL" ]; then MODEL="$(ask 'Local Ollama model' "$DEF_MODEL")"; fi
  if [ -z "$STRIP_DESKTOP" ]; then
    if confirm 'Disable the desktop GUI to free RAM (recommended if headless)?' N; then STRIP_DESKTOP=1; else STRIP_DESKTOP=0; fi
  fi
  if [ -z "$RETRO" ]; then
    if confirm 'Install the retro Z80/Amstrad toolchain (sdcc/z88dk/CPCtelera - heavy source builds)?' N; then RETRO=1; else RETRO=0; fi
  fi
  if [ -z "$IMGGEN" ]; then
    if confirm 'Build the local image generator (OnnxStream - slow on a Pi)?' N; then IMGGEN=1; else IMGGEN=0; fi
  fi
fi
# Resolve anything still unanswered (non-interactive / --yes / flag absent).
WANT_LOCAL="${WANT_LOCAL:-0}"
MODEL="${MODEL:-$DEF_MODEL}"
STRIP_DESKTOP="${STRIP_DESKTOP:-0}"
RETRO="${RETRO:-0}"
IMGGEN="${IMGGEN:-0}"
echo "    chosen: local=$WANT_LOCAL  model=$MODEL  strip-desktop=$STRIP_DESKTOP  retro=$RETRO  imggen=$IMGGEN"

# ---------------------------------------------------------------------------
# Memory headroom via ZRAM (compressed swap that lives in RAM) — deliberately NOT
# a disk swapfile. Writing gigabytes to a slow SD card, and then swapping to it,
# is itself enough to saturate the card, make the board unresponsive and drop
# your SSH. zram adds headroom with ZERO disk I/O and zero SD wear. Best-effort;
# skipped cleanly if the module isn't available.
if ! grep -q '^/' /proc/swaps 2>/dev/null; then          # no swap yet
  say "Memory headroom (zram compressed swap — no disk writes, SD-friendly)"
  if modprobe zram 2>/dev/null && [ -e /sys/block/zram0 ]; then
    echo lz4 > /sys/block/zram0/comp_algorithm 2>/dev/null || true
    echo $((2048*1024*1024)) > /sys/block/zram0/disksize 2>/dev/null || true
    if mkswap /dev/zram0 >/dev/null 2>&1 && swapon -p 100 /dev/zram0 2>/dev/null; then
      echo "    zram swap on (up to 2GB, compressed in RAM)."
    else
      warn "couldn't enable zram — continuing (a headless cloud-only box rarely needs swap)."
    fi
  else
    warn "zram unavailable — continuing without extra swap (no disk swapfile on SD)."
  fi
fi

# ---------------------------------------------------------------------------
say "1/10  Base packages"
export DEBIAN_FRONTEND=noninteractive
# Stop the distro's background apt (apt-daily / unattended-upgrades) so it can't
# hold the dpkg lock mid-install and stall us (re-enabled by a reboot), and wait
# (up to 5 min) for any in-progress one to release the lock rather than hanging.
systemctl stop apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl stop apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
APT="apt-get -o DPkg::Lock::Timeout=300"
$APT update -y
$APT install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev git curl ca-certificates rsync \
  i2c-tools gpiod usbutils lm-sensors util-linux \
  build-essential libffi-dev libssl-dev pkg-config   # lets the agent pip-compile native deps

if [ "$STRIP_DESKTOP" -eq 1 ]; then
  say "1b/10 Disabling the desktop (frees RAM; nothing is uninstalled)"
  # IMPORTANT: we do NOT purge/autoremove packages. On a vendor SBC image that
  # can drag out hardware/firmware/overlay packages you actually need. Instead
  # we just stop the GUI from starting — it frees the same RAM and is fully
  # reversible. Sensors/GPIO/I2C come from the kernel + device tree, not the DE.
  systemctl set-default multi-user.target || true
  systemctl stop display-manager 2>/dev/null || true   # free the RAM now, no reboot needed
  warn "Desktop is OFF (still installed). Re-enable any time:"
  warn "  sudo systemctl set-default graphical.target && sudo reboot"
fi

# ---------------------------------------------------------------------------
say "2/10  Dedicated unprivileged user '$AGENT_USER'"
if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/drongo \
          --shell /usr/sbin/nologin "$AGENT_USER"
fi
# Let the agent READ its own sensors (i2c/gpio/spi groups), nothing more.
for grp in i2c gpio spi dialout video; do
  getent group "$grp" >/dev/null 2>&1 && usermod -aG "$grp" "$AGENT_USER" || true
done

# ---------------------------------------------------------------------------
say "3/10  Directories"
mkdir -p "$INSTALL" "$RUNTIME"/{workspace,state,logs} \
         /var/lib/drongo/observer "$ETC"

# ---------------------------------------------------------------------------
say "4/10  Install code to $INSTALL (root-owned)"
rsync -a --delete --exclude '.smoketest' --exclude '__pycache__' \
      "$SRC"/ "$INSTALL"/
# Rollback needs a git repo. If you cloned, the real one (with a remote) is kept.
# If you unzipped (no .git), make a local one so the observer can still roll back.
if [ ! -d "$INSTALL/.git" ]; then
  echo "    no git history found - initialising a local repo so rollback works"
  git -C "$INSTALL" init -q
  git -C "$INSTALL" add -A
  git -C "$INSTALL" -c user.email=drongo@localhost -c user.name=DRONGO \
      commit -qm "baseline install" || true
  warn "Self-UPDATE needs a real git remote; without one the agent can still"
  warn "run and roll back, but won't fetch new code. (Clone instead of unzip to enable it.)"
fi

# ---------------------------------------------------------------------------
say "5/10  Python virtualenv + deps"
python3 -m venv "$INSTALL/.venv"
"$INSTALL/.venv/bin/pip" install --upgrade pip >/dev/null
"$INSTALL/.venv/bin/pip" install -r "$INSTALL/requirements.txt"

# ---------------------------------------------------------------------------
say "6/10  Config + secrets (kept if they already exist)"
if [ ! -f "$ETC/config.yaml" ]; then
  cp "$INSTALL/config.example.yaml" "$ETC/config.yaml"
  sed -i 's#^base_dir:.*#base_dir: "/var/lib/drongo/runtime"#' "$ETC/config.yaml"
  # Local model is OFF by default (cloud-first). Only if you asked for one, point
  # it at the model we're pulling and flip the local provider on.
  if [ "$WANT_LOCAL" -eq 1 ]; then
    sed -i "s#qwen2.5:3b-instruct#$MODEL#g" "$ETC/config.yaml"
    sed -i 's/enabled: false\( *# LOCAL_ENABLED\)/enabled: true\1/' "$ETC/config.yaml"
  fi
fi
[ -f "$ETC/drongo.env" ]   || cp "$INSTALL/system/drongo.env.example"   "$ETC/drongo.env"
[ -f "$ETC/observer.env" ] || cp "$INSTALL/system/observer.env.example" "$ETC/observer.env"
# Root-owned HARD package allow-list. Lives in /etc (root-owned) so the agent
# user can NEVER edit it — only you, over SSH. Packages/globs here are always
# installable by the agent, on top of whatever you allow live on the dashboard.
if [ ! -f "$ETC/pkg-allow.conf" ]; then
  cat > "$ETC/pkg-allow.conf" <<'PKGALLOW'
# DRONGO hard package allow-list (root-owned; the agent CANNOT edit this file).
# One apt package name or glob per line; '#' starts a comment. Packages matching
# a line here are ALWAYS installable by the agent, in addition to anything you
# allow live on the dashboard (Files -> Install policy).
# Uncomment / add what you trust it to install unattended, e.g.:
# build-essential
# sdcc
# pasmo
# z88dk
# libboost-*
PKGALLOW
fi
chmod 644 "$ETC/pkg-allow.conf"
# Auto-generate a strong dashboard password so the LAN UI is protected out of the
# box (kept if you already set one). Without it the dashboard is localhost-only.
if grep -q '^DRONGO_WEB_PASSWORD=$' "$ETC/drongo.env" 2>/dev/null; then
  WEBPW="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-20)"
  sed -i "s#^DRONGO_WEB_PASSWORD=\$#DRONGO_WEB_PASSWORD=$WEBPW#" "$ETC/drongo.env"
fi
chmod 600 "$ETC"/*.env
chmod 644 "$ETC/config.yaml"

# ---------------------------------------------------------------------------
say "7/10  Lock down the safeguard (seal hash, root:root 0444)"
chown -R root:root "$INSTALL"
# Seal from INSIDE $INSTALL so `python -m agent` resolves the installed package.
( cd "$INSTALL" && "$INSTALL/.venv/bin/python" -m agent seal ) \
  || ( cd "$INSTALL" && python3 -c "from agent import safeguard; safeguard.self_seal()" )
chmod 0444 "$INSTALL/agent/safeguard.py" "$INSTALL/agent/safeguard.py.sha256"
chown root:root "$INSTALL/agent/safeguard.py" "$INSTALL/agent/safeguard.py.sha256"
# Baseline "last known good" for the observer to roll back to from day one.
[ -d "$INSTALL/.git" ] && git -C "$INSTALL" tag -f drongo-lkg HEAD >/dev/null 2>&1 || true
# A writable venv the agent can pip-install its project deps into (its own code
# dir is read-only and Debian blocks system-wide pip).
python3 -m venv "$RUNTIME/venv" 2>/dev/null || true
# The runtime tree is the ONLY thing the agent may write.
chown -R "$AGENT_USER:$AGENT_USER" /var/lib/drongo
chmod 700 "$RUNTIME"

# ---------------------------------------------------------------------------
say "8/10  Local model (optional)"
if [ "$WANT_LOCAL" -eq 1 ]; then
  echo "    installing Ollama + pulling $MODEL"
  if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  systemctl enable --now ollama 2>/dev/null || true
  mkdir -p /etc/systemd/system/ollama.service.d
  cat > /etc/systemd/system/ollama.service.d/lowmem.conf <<'EOF'
[Service]
Environment=OLLAMA_NUM_PARALLEL=1
Environment=OLLAMA_MAX_LOADED_MODELS=1
Environment=OLLAMA_KEEP_ALIVE=10m
# Keep the model server bound to localhost only - nothing on the LAN should reach it.
Environment=OLLAMA_HOST=127.0.0.1
EOF
  systemctl daemon-reload
  systemctl restart ollama 2>/dev/null || true
  echo "    pulling $MODEL (first time can take several minutes)..."
  ollama pull "$MODEL" || warn "model pull failed - run 'ollama pull $MODEL' later, then 'systemctl restart drongo'"
else
  echo "    cloud-only — no local model installed (that's the default)."
  echo "    Add one anytime:  sudo ./install.sh --local"
  warn "You MUST add at least one provider key in the wizard (or /etc/drongo/drongo.env),"
  warn "or the agent has no brain until you do."
fi

# ---------------------------------------------------------------------------
say "9/10  Arm the SoC hardware watchdog (reboots board if the kernel wedges)"
if ! grep -q '^RuntimeWatchdogSec=' /etc/systemd/system.conf; then
  cat >> /etc/systemd/system.conf <<'EOF'

# --- DRONGO: hardware watchdog ---
RuntimeWatchdogSec=20s
RebootWatchdogSec=2min
EOF
fi

# ---------------------------------------------------------------------------
say "10/10  systemd units"
cp "$INSTALL"/systemd/*.service "$INSTALL"/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now drongo.service drongo-web.service \
                       drongo-observer.timer drongo-update.timer drongo-pkg.timer

# Convenience CLI: `sudo drongo doctor|configure|...` always targets /opt/drongo.
cp "$INSTALL/system/drongo" /usr/local/bin/drongo && chmod 0755 /usr/local/bin/drongo

# nice/ionice keeps these heavy source builds from starving the box — so your
# SSH session stays responsive instead of dropping while they compile.
# best-effort LOW priority (class 2, prio 7) — deprioritised but NOT starved. (An
# idle class -c3 can get zero I/O on a busy SD and stall apt/downloads entirely.)
LOAD="nice -n 15"; command -v ionice >/dev/null 2>&1 && LOAD="ionice -c2 -n7 nice -n 15"

# Optional Z80/Amstrad cross-dev toolchain (sdcc, z88dk, CPCtelera, pasmo).
if [ "$RETRO" -eq 1 ]; then
  say "Retro toolchain: sdcc / z88dk / CPCtelera / pasmo (low-priority build; be patient)"
  $LOAD bash "$INSTALL/system/retro-toolchain.sh" || warn "retro toolchain had problems (see above) — re-run sudo $INSTALL/system/retro-toolchain.sh"
fi

# Optional local image generator (OnnxStream). Slow on a Pi; cloud stays default.
if [ "$IMGGEN" -eq 1 ]; then
  say "Local image generator: OnnxStream (low-priority build; this takes a while)"
  $LOAD bash "$INSTALL/system/image-gen.sh" || warn "image generator had problems (see above) — re-run sudo $INSTALL/system/image-gen.sh"
fi

# ---------------------------------------------------------------------------
# Self-check so you immediately know whether it's healthy.
say "Health check"
sleep 3
( cd "$INSTALL" && "$INSTALL/.venv/bin/python" -m agent -c "$ETC/config.yaml" doctor --quick ) || true

# Interactive setup wizard (alerts + keys) — only when run on a real terminal.
if [ -t 0 ]; then
  ( cd "$INSTALL" && "$INSTALL/.venv/bin/python" -m agent -c "$ETC/config.yaml" configure ) || true
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
trap - EXIT
if [ "$WANT_LOCAL" -eq 1 ]; then
  BRAIN="It works on the LOCAL model out of the box, plus any cloud keys you add in the wizard."
else
  BRAIN="Brain = FREE CLOUD providers — add at least one key in the wizard (next) or it idles until you do."
fi
printf '\n\033[1;32mDRONGO is installed and running.\033[0m\n'
cat <<EOF

  $BRAIN

    Re-run the setup wizard any time:   sudo $INSTALL/configure.sh
    (Discord webhook, LED pin, API keys — it restarts DRONGO for you.)

  HOW DO I KNOW IT'S WORKING?
    sudo drongo doctor          # should say READY (run from anywhere)
    journalctl -u drongo -f     # watch it think
    Dashboard:  http://${IP:-<pi-ip>}:8080/
       login: ANY username   password: $(grep -m1 '^DRONGO_WEB_PASSWORD=' "$ETC/drongo.env" | cut -d= -f2-)
       (LAN-only + password-protected. Change it in $ETC/drongo.env, then restart drongo-web.)

  CONTROLS  (just create these files):
    touch $RUNTIME/workspace/PAUSE   # finish current job, then idle   (delete to resume)
    touch $RUNTIME/workspace/STOP    # stand down cleanly

  To remove everything later:   sudo $INSTALL/uninstall.sh
EOF
