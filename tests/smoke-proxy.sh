#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
name="vstore-proxy-smoke-$$"
image="${1:-vstore-proxy-smoke:$$}"
owned_image=false
if [ "$#" -eq 0 ]; then
  owned_image=true
fi
cleanup() {
  docker logs "$name" 2>&1 || true
  docker rm -f "$name" >/dev/null 2>&1 || true
  if [ "$owned_image" = true ]; then
    docker image rm "$image" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
if [ "$owned_image" = true ]; then
  docker build --pull --no-cache -t "$image" config/nginx
fi
docker run -d --name "$name" --network none \
  --cap-drop ALL --cap-add NET_BIND_SERVICE --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges:true \
  --add-host backend:127.0.0.1 --add-host frontend:127.0.0.1 --add-host admin:127.0.0.1 \
  -e FRONTEND_DOMAIN=vstore.local -e ADMIN_DOMAIN=admin.vstore.local -e ADMIN_CERT_NAME=vstore.local \
  -e 'NGINX_ENVSUBST_FILTER=FRONTEND_DOMAIN|ADMIN_DOMAIN|ADMIN_CERT_NAME' \
  --tmpfs /etc/letsencrypt --tmpfs /var/www/certbot \
  -v "$PWD/config/nginx/templates:/etc/nginx/templates:ro" \
  -v "$PWD/config/nginx/entrypoint/10-init-dummy-certs.sh:/docker-entrypoint.d/10-init-dummy-certs.sh:ro" \
  -v "$PWD/config/nginx/entrypoint/20-periodic-reload.sh:/docker-entrypoint.d/30-periodic-reload.sh:ro" \
  "$image"
for attempt in {1..30}; do
  if docker exec "$name" wget -qO- http://127.0.0.1/healthz; then
    docker exec "$name" nginx -t
    docker exec "$name" openssl x509 -in /etc/letsencrypt/live/vstore.local/fullchain.pem -noout -subject
    for domain in vstore.local admin.vstore.local; do
      docker exec "$name" sh -c 'openssl s_client -connect 127.0.0.1:443 -servername "$1" -showcerts </dev/null 2>/dev/null | openssl x509 -noout -subject' sh "$domain"
    done
    docker exec "$name" sh -c '! grep -F "\${FRONTEND_DOMAIN}" /etc/nginx/conf.d/default.conf && grep -F '\''$host'\'' /etc/nginx/conf.d/default.conf'
    exit 0
  fi
  [ "$(docker inspect --format '{{.State.Running}}' "$name")" = true ] || break
  sleep 1
done
echo 'Proxy bootstrap failed' >&2
exit 1
