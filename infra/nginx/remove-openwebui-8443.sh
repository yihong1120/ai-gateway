#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "請使用 sudo 執行此腳本。" >&2
    exit 1
fi

domain=ai.changdar-server.mooo.com
source_dir=/home/changdar/Documents/ai-gateway/infra/nginx
available=/etc/nginx/sites-available/openwebui-8443
enabled=/etc/nginx/sites-enabled/openwebui-8443
backup="${available}.disabled-$(date +%Y%m%d-%H%M%S)"
default_site=/etc/nginx/sites-enabled/default
default_backup="/etc/nginx/site-backups/default.before-ai-redirect-$(date +%Y%m%d-%H%M%S)"

curl --fail --silent --show-error "https://${domain}/health" >/dev/null

install -d -m 0755 /etc/nginx/site-backups
cp -a "$default_site" "$default_backup"
install -m 0644 "$source_dir/openwebui.conf" \
    /etc/nginx/snippets/openwebui-ai-redirect.conf
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
install -m 0755 "$source_dir/reload-after-certificate-renewal.sh" \
    /etc/letsencrypt/renewal-hooks/deploy/reload-nginx
python3 - "$default_site" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
directive = '    include /etc/nginx/snippets/openwebui-ai-redirect.conf;\n'
if directive not in text:
    anchor = (
        '    include /home/changdar/Documents/Construction-Hazard-Detection/'
        'deploy/keycloak/nginx.keycloak.conf;\n'
    )
    if anchor not in text:
        raise SystemExit('找不到主 HTTPS server 的 Keycloak include，未修改。')
    path.write_text(text.replace(anchor, anchor + '\n' + directive, 1))
PY

if [[ -e "$available" ]]; then
    cp -a "$available" "$backup"
fi
if [[ -L "$enabled" ]]; then
    unlink "$enabled"
fi
if [[ -e "$available" ]]; then
    unlink "$available"
fi

if ! nginx -t; then
    if [[ -e "$backup" ]]; then
        cp -a "$backup" "$available"
        ln -sfn "$available" "$enabled"
    fi
    cp -a "$default_backup" "$default_site"
    exit 1
fi

systemctl reload nginx
curl --fail --silent --show-error "https://${domain}/health" >/dev/null

for _ in $(seq 1 20); do
    if ! ss -ltnH 'sport = :8443' | grep -q .; then
        break
    fi
    sleep 1
done
if ss -ltnH 'sport = :8443' | grep -q .; then
    echo "8443 仍有其他服務監聽，未達成關閉。" >&2
    exit 1
fi

echo "8443 已停止監聽；Open WebUI 僅使用 https://${domain}/"
