#!/usr/bin/env bash
# ===========================================================================
#  DRONGO installer for Debian on the Rock Pi 4C+ (RK3399).
#
#  Beginner steps:
#    1. Flash Debian to the Pi, boot it, and SSH in.
#    2. Get this code onto the Pi (git clone <your fork>  OR  unzip it).
#    3. Run:   sudo ./install.sh --strip-desktop
#    4. Follow the printed "NEXT" steps. That's it.
#
#  What it sets up ("Local Autonomy, Global Isolation"):
#    * code at /opt/drongo       - root-owned, READ-ONLY to the agent
#    * runtime at /var/lib/drongo - the only place the agent can write
#    * safeguard.py locked 0444 root:root + sha256-sealed
#    * unprivileged 'drongo' user, sandboxed systemd unit with cgroup caps
#    * SoC hardware watchdog armed (reboots the board if the kernel wedges)
#    * external root observer + privileged updater (the "Dead Man's Switch")
#
#  Flags:  --strip-desktop   remove the GUI to free RAM (recommended, headless)
#          --model NAME       force a specific Ollama model (default: auto by RAM)
# ===========================================================================
set -euo pipefail

INSTALL=/opt/drongo
RUNTIME=/var/lib/drongo/runtime
ETC=/etc/drongo
AGENT_USER=drongo
MODEL=""              # empty => auto-pick from RAM below
STRIP_DESKTOP=0

# --- friendly failure: tell the beginner exactly where it broke ------------
trap 'rc=$?; [ $rc -ne 0 ] && printf "\n\033[1;31m[install] FAILED (exit %s) near line %s.\033[0m\nRead the message just above, fix it, then re-run:  sudo ./install.sh\n" "$rc" "$LINENO"' EXIT

while [ $# -gt 0 ]; do
  case "$1" in
    --strip-desktop) STRIP_DESKTOP=1 ;;
    --model) [ $# -ge 2 ] || { echo "--model needs a value, e.g. --model qwen2.5:1.5b-instruct"; exit 1; }
             MODEL="$2"; shift ;;
    -h|--help) sed -n '2,25p' "$0"; trap - EXIT; exit 0 ;;
    *) echo "unknown flag: $1  (try --help)"; exit 1 ;;
  esac
  shift
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m    ! %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
say "0/10  Pre-flight checks"
[ "$(id -u)" -eq 0 ] || { echo "Please run me with sudo:  sudo ./install.sh"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

# Internet (we must download packages + the model)
if ! curl -fsS --max-time 10 https://ollama.com >/dev/null 2>&1; then
  warn "Couldn't reach the internet. The installer needs it to download packages"
  warn "and the local model. Check the Pi's network and try again."
  exit 1
fi

# Free disk space (venv + a small model need a few GB)
avail_mb=$(df -Pm / | awk 'NR==2{print $4}')
[ "${avail_mb:-0}" -ge 3000 ] || warn "Only ${avail_mb}MB free on / - a model may not fit. ~3GB+ recommended."

# Pick a model that fits this board's RAM (4C+ ships as 1/2/4 GB)
ram_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo 2>/dev/null || echo 0)
if [ -z "$MODEL" ]; then
  if   [ "$ram_mb" -ge 3000 ]; then MODEL="qwen2.5:3b-instruct"
  elif [ "$ram_mb" -ge 1500 ]; then MODEL="qwen2.5:1.5b-instruct"
  else                              MODEL="qwen2.5:0.5b-instruct"; fi
fi
echo "    RAM: ${ram_mb}MB  ->  local model: $MODEL   (override with --model)"

# ---------------------------------------------------------------------------
say "1/10  Base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip git curl ca-certificates rsync \
  i2c-tools gpiod usbutils lm-sensors util-linux

if [ "$STRIP_DESKTOP" -eq 1 ]; then
  say "1b/10 Removing desktop environment (freeing RAM)"
  systemctl set-default multi-user.target || true
  apt-get purge -y 'task-*-desktop' lightdm gdm3 'xserver-xorg*' 2>/dev/null || true
  apt-get autoremove -y || true
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
  # Point the local provider at the model we're actually pulling.
  sed -i "s#qwen2.5:3b-instruct#$MODEL#g" "$ETC/config.yaml"
fi
[ -f "$ETC/drongo.env" ]   || cp "$INSTALL/system/drongo.env.example"   "$ETC/drongo.env"
[ -f "$ETC/observer.env" ] || cp "$INSTALL/system/observer.env.example" "$ETC/observer.env"
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
# The runtime tree is the ONLY thing the agent may write.
chown -R "$AGENT_USER:$AGENT_USER" /var/lib/drongo
chmod 700 "$RUNTIME"

# ---------------------------------------------------------------------------
say "8/10  Ollama + local model ($MODEL)"
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
say "    pulling $MODEL (first time can take several minutes)..."
ollama pull "$MODEL" || warn "model pull failed - run 'ollama pull $MODEL' later, then 'systemctl restart drongo'"

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
                       drongo-observer.timer drongo-update.timer

# ---------------------------------------------------------------------------
# Self-check so you immediately know whether it's healthy.
say "Health check"
sleep 3
( cd "$INSTALL" && "$INSTALL/.venv/bin/python" -m agent -c "$ETC/config.yaml" doctor --quick ) || true

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
trap - EXIT
printf '\n\033[1;32mDRONGO is installed and running.\033[0m\n'
cat <<EOF

  IT ALREADY WORKS on the local model. To make it smarter, add free API keys:

    1. Edit your keys:        sudo nano $ETC/drongo.env
         (CEREBRAS / GROQ / GEMINI / MISTRAL / OPENROUTER - ANTHROPIC_API_KEY is optional/paid)
    2. Want alerts? Two easy options (Discord is on by default):
         Discord: make a channel webhook (Server Settings -> Integrations ->
           Webhooks -> Copy URL), then  sudo nano $ETC/drongo.env  -> DISCORD_WEBHOOK_URL
           (paste the same URL into $ETC/observer.env -> DRONGO_DISCORD_WEBHOOK too).
         LED:     wire an LED to a GPIO pin, find it with 'gpioinfo', then set
           alerts.led (enabled/chip/line) in $ETC/config.yaml.
    3. Apply changes:         sudo systemctl restart drongo drongo-web

  HOW DO I KNOW IT'S WORKING?
    sudo $INSTALL/.venv/bin/python -m agent -c $ETC/config.yaml doctor   # should say READY
    journalctl -u drongo -f                                              # watch it think
    Dashboard:  http://${IP:-<pi-ip>}:8080/
       login: ANY username   password: $(grep -m1 '^DRONGO_WEB_PASSWORD=' "$ETC/drongo.env" | cut -d= -f2-)
       (LAN-only + password-protected. Change it in $ETC/drongo.env, then restart drongo-web.)

  CONTROLS  (just create these files):
    touch $RUNTIME/workspace/PAUSE   # finish current job, then idle   (delete to resume)
    touch $RUNTIME/workspace/STOP    # stand down cleanly

  To remove everything later:   sudo $INSTALL/uninstall.sh
EOF
