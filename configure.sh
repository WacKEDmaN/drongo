#!/usr/bin/env bash
# DRONGO interactive setup — alerts (Discord / LED) + API keys, then restart.
# Run any time:   sudo /opt/drongo/configure.sh
# It only edits /etc/drongo/*.env (your config.yaml is left untouched).
PY=/opt/drongo/.venv/bin/python
CFG=/etc/drongo/config.yaml
[ -x "$PY" ] || PY=python3
if [ "$(id -u)" -ne 0 ]; then
  exec sudo env PYTHONPATH=/opt/drongo "$PY" -m agent -c "$CFG" configure "$@"
fi
exec env PYTHONPATH=/opt/drongo "$PY" -m agent -c "$CFG" configure "$@"
