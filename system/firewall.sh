#!/usr/bin/env bash
# ===========================================================================
#  OPTIONAL inbound firewall for the DRONGO box (nftables).
#  Run it ONLY if you want to lock the Pi down further:   sudo ./system/firewall.sh
#
#  What it does:
#    * default-DROP all incoming connections (so nothing is reachable from the
#      internet even if your router forwards a port by mistake)
#    * ALLOW: loopback, already-established replies, and ping
#    * ALLOW: SSH and the dashboard ONLY from private LAN ranges
#    * ALLOW: all OUTGOING traffic (the agent still reaches its LLM providers)
#
#  It is SSH-safe: SSH from your LAN keeps working. If you SSH in from OUTSIDE
#  your LAN, add your source IP first:  SSH_ALLOW_EXTRA="1.2.3.4" sudo ./system/firewall.sh
#
#  Undo:  sudo cp /etc/nftables.conf.drongo-backup /etc/nftables.conf && sudo nft -f /etc/nftables.conf
#         (or just: sudo systemctl disable --now nftables && sudo nft flush ruleset)
# ===========================================================================
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo:  sudo ./system/firewall.sh"; exit 1; }

DASH_PORT="${DASH_PORT:-8080}"
# Detect the SSH port (default 22) so we never lock you out.
SSH_PORT="${SSH_PORT:-$(grep -iE '^[[:space:]]*Port[[:space:]]+[0-9]+' /etc/ssh/sshd_config 2>/dev/null | awk '{print $2}' | head -1)}"
SSH_PORT="${SSH_PORT:-22}"
SSH_ALLOW_EXTRA="${SSH_ALLOW_EXTRA:-}"   # optional extra source IP/CIDR for SSH

echo "==> installing nftables (if needed)"
command -v nft >/dev/null 2>&1 || { apt-get update -y && apt-get install -y nftables; }

echo "==> backing up current ruleset to /etc/nftables.conf.drongo-backup"
[ -f /etc/nftables.conf ] && cp -n /etc/nftables.conf /etc/nftables.conf.drongo-backup || true

extra_rule=""
[ -n "$SSH_ALLOW_EXTRA" ] && extra_rule="ip saddr $SSH_ALLOW_EXTRA tcp dport $SSH_PORT accept"

echo "==> writing ruleset (SSH on $SSH_PORT, dashboard on $DASH_PORT, LAN-only)"
cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
# Managed by DRONGO system/firewall.sh - edit with care.
flush ruleset

define LAN4 = { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16 }

table inet drongo {
  chain input {
    type filter hook input priority 0; policy drop;

    iif "lo" accept
    ct state established,related accept
    ct state invalid drop

    # ping (handy for diagnostics)
    ip protocol icmp accept
    ip6 nexthdr ipv6-icmp accept

    # SSH + dashboard from the local network only
    ip saddr \$LAN4 tcp dport $SSH_PORT accept
    ip saddr \$LAN4 tcp dport $DASH_PORT accept
    $extra_rule

    # everything else inbound is dropped by the policy above
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output  { type filter hook output  priority 0; policy accept; }
}
EOF

echo "==> applying now"
nft -f /etc/nftables.conf
systemctl enable --now nftables >/dev/null 2>&1 || true

cat <<EOF

Firewall active. Inbound is default-DROP; only loopback, established replies,
ping, and (SSH $SSH_PORT + dashboard $DASH_PORT) from the LAN are allowed.

  Check:  sudo nft list ruleset
  Undo:   sudo cp /etc/nftables.conf.drongo-backup /etc/nftables.conf 2>/dev/null; \\
          sudo nft -f /etc/nftables.conf 2>/dev/null || sudo nft flush ruleset

If you lost SSH, you're connecting from outside the LAN - reboot to clear the
in-memory rules (they only persist after 'systemctl enable nftables', which we
did), or re-run with SSH_ALLOW_EXTRA="<your-ip>".
EOF
