#!/usr/bin/env bash
# ===========================================================================
#  Fast redeploy of code changes to the running agent.  sudo ./update.sh
#
#  Why this exists: the agent runs from /opt/drongo (root-owned), NOT from this
#  checkout. `git pull` here only updates your clone — this script copies the
#  new code into /opt/drongo, re-seals the safeguard, and restarts. Much faster
#  than re-running install.sh (skips apt + the Ollama model pull).
#
#  Normal update flow:   git pull   &&   sudo ./update.sh
# ===========================================================================
set -euo pipefail
INSTALL=/opt/drongo
[ "$(id -u)" -eq 0 ] || { echo "Run me with sudo:  sudo ./update.sh"; exit 1; }
[ -d "$INSTALL/.venv" ] || { echo "$INSTALL not installed yet — run sudo ./install.sh first."; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "==> syncing code -> $INSTALL"
rsync -a --delete --exclude '.venv' --exclude '.smoketest' --exclude '__pycache__' \
      "$SRC"/ "$INSTALL"/

echo "==> python deps"
"$INSTALL/.venv/bin/pip" install -q -r "$INSTALL/requirements.txt" || true

echo "==> re-sealing safeguard (root:root 0444)"
chown -R root:root "$INSTALL"
( cd "$INSTALL" && "$INSTALL/.venv/bin/python" -m agent seal ) \
  || ( cd "$INSTALL" && python3 -c "from agent import safeguard; safeguard.self_seal()" )
chmod 0444 "$INSTALL/agent/safeguard.py" "$INSTALL/agent/safeguard.py.sha256"
[ -d "$INSTALL/.git" ] && git -C "$INSTALL" tag -f drongo-lkg HEAD >/dev/null 2>&1 || true

echo "==> refreshing the 'drongo' CLI wrapper"
cp "$INSTALL/system/drongo" /usr/local/bin/drongo && chmod 0755 /usr/local/bin/drongo

echo "==> fixing runtime ownership"
chown -R drongo:drongo /var/lib/drongo

echo "==> restarting services"
systemctl restart drongo drongo-web

REV="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo '?')"
echo
echo "Deployed $REV. Check it:"
echo "  sudo $INSTALL/.venv/bin/python -m agent -c /etc/drongo/config.yaml doctor"
echo "  journalctl -u drongo -f"
