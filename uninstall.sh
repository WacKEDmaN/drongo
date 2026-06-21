#!/usr/bin/env bash
# ===========================================================================
#  DRONGO uninstaller.   sudo ./uninstall.sh
#
#  By default it stops & removes the services and the code, but KEEPS your
#  data and config (so you can reinstall and pick up where you left off):
#      /var/lib/drongo   (workspace, journal, generated art/games)
#      /etc/drongo       (config.yaml + your API keys)
#
#  Flags:
#    --purge    also delete /var/lib/drongo, /etc/drongo and the 'drongo' user
#    --yes      don't ask for confirmation
# ===========================================================================
set -euo pipefail

PURGE=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    *) echo "unknown flag: $1"; exit 1 ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || { echo "Please run me with sudo:  sudo ./uninstall.sh"; exit 1; }

if [ "$ASSUME_YES" -ne 1 ]; then
  echo "This will stop and remove DRONGO."
  [ "$PURGE" -eq 1 ] && echo "  --purge: ALSO deletes /var/lib/drongo, /etc/drongo and the drongo user."
  printf "Continue? [y/N] "
  read -r ans
  case "$ans" in y|Y|yes|YES) ;; *) echo "Aborted."; exit 0 ;; esac
fi

echo "==> stopping + disabling services"
for unit in drongo.service drongo-web.service drongo-observer.timer \
            drongo-update.timer drongo-observer.service drongo-update.service; do
  systemctl disable --now "$unit" 2>/dev/null || true
done

echo "==> removing systemd units"
rm -f /etc/systemd/system/drongo*.service /etc/systemd/system/drongo*.timer
systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

echo "==> removing the hardware-watchdog lines we added"
sed -i '/# --- DRONGO: hardware watchdog ---/,+2d' /etc/systemd/system.conf 2>/dev/null || true

echo "==> removing code at /opt/drongo"
rm -rf /opt/drongo

if [ "$PURGE" -eq 1 ]; then
  echo "==> PURGE: removing data, config and user"
  rm -rf /var/lib/drongo /etc/drongo
  id drongo >/dev/null 2>&1 && userdel drongo 2>/dev/null || true
else
  echo "==> kept your data:   /var/lib/drongo"
  echo "==> kept your config: /etc/drongo   (delete by hand, or re-run with --purge)"
fi

echo
echo "Done. (Ollama and the desktop, if removed, were left as-is.)"
echo "Note: the hardware watchdog change takes effect on next reboot."
