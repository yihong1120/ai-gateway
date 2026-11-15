#!/usr/bin/env bash
# Restore the existing private proxy without changing its listeners or ACLs.
set -euo pipefail

if (( EUID != 0 )); then
  printf '%s\n' 'Run this repair with sudo on the OCR host A.' >&2
  exit 1
fi

repair_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repair_source="$repair_dir/nginx-tailscale.conf"
repair_target=/etc/systemd/system/nginx.service.d/20-ai-gateway-tailscale.conf
repair_backup="/var/backups/ai-gateway/nginx-tailscale-$(date +%Y%m%dT%H%M%S)-$$"

test -f "$repair_source"
systemctl cat tailscaled.service >/dev/null
/usr/sbin/nginx -t
umask 077
mkdir -p -- "$repair_backup"
systemctl cat nginx.service > "$repair_backup/nginx-unit-before.txt"
if [[ -f "$repair_target" ]]; then
  cp -a -- "$repair_target" "$repair_backup/previous-drop-in.conf"
fi
install -d -m 0755 -- "$(dirname -- "$repair_target")"
install -D -m 0644 -- "$repair_source" "$repair_target"
systemctl daemon-reload
systemctl start nginx.service
systemctl is-active --quiet nginx.service
systemctl show nginx.service --no-pager -p ActiveState -p After -p Restart -p RestartUSec -p StartLimitIntervalUSec
printf 'Private backup: %s\n' "$repair_backup"
