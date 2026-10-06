#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "請使用 sudo 執行此腳本。" >&2
    exit 1
fi

domain=ai.changdar-server.mooo.com
source_dir=/home/changdar/Documents/ai-gateway/infra/nginx
available=/etc/nginx/sites-available
enabled=/etc/nginx/sites-enabled

getent ahosts "$domain" >/dev/null
curl --fail --silent --show-error \
    "http://$domain/.well-known/acme-challenge/not-present" \
    --output /dev/null || [[ $? -eq 22 ]]

certbot certonly \
    --non-interactive \
    --agree-tos \
    --keep-until-expiring \
    --webroot \
    --webroot-path /var/www/html \
    --domain "$domain"

install -m 0644 "$source_dir/openwebui-ai.conf" "$available/openwebui-ai"
ln -sfn "$available/openwebui-ai" "$enabled/openwebui-ai"
"$source_dir/remove-openwebui-8443.sh"
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
install -m 0755 "$source_dir/reload-after-certificate-renewal.sh" \
    /etc/letsencrypt/renewal-hooks/deploy/reload-nginx

nginx -t
systemctl reload nginx

curl --fail --silent --show-error "https://$domain/health"
echo
echo "Nginx 與 TLS 已啟用：https://$domain/；舊 8443 listener 已停用。"
