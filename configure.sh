#!/usr/bin/env bash
# DRONGO interactive setup — alerts (Discord / LED) + API keys, then restart.
# Run any time:   sudo /opt/drongo/configure.sh   (or: sudo drongo configure)
# It only edits /etc/drongo/*.env (your config.yaml is left untouched).
cd /opt/drongo 2>/dev/null || { echo "DRONGO not installed at /opt/drongo"; exit 1; }
PY=/opt/drongo/.venv/bin/python
[ -x "$PY" ] || PY=python3
if [ "$(id -u)" -ne 0 ]; then
  exec sudo "$PY" -m agent -c /etc/drongo/config.yaml configure "$@"
fi
exec "$PY" -m agent -c /etc/drongo/config.yaml configure "$@"
