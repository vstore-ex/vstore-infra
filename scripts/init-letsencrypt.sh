#!/usr/bin/env bash
# =============================================================================
# Automated Let's Encrypt Certificate Bootstrap Script for VStore
# Acquires initial TLS certificates via Certbot HTTP-01 webroot challenge
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

# Source environment variables if .env is present
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a
  source .env
  set +a
fi

FRONTEND_DOMAIN="${FRONTEND_DOMAIN:-vstore.local}"
ADMIN_DOMAIN="${ADMIN_DOMAIN:-admin.vstore.local}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@vstore.local}"
CERTBOT_STAGING="${CERTBOT_STAGING:-0}"

echo "================================================================="
echo "VStore Let's Encrypt TLS Bootstrap"
echo "Frontend Domain: ${FRONTEND_DOMAIN}"
echo "Admin Domain:    ${ADMIN_DOMAIN}"
echo "Contact Email:   ${CERTBOT_EMAIL}"
echo "Staging Mode:    ${CERTBOT_STAGING}"
echo "================================================================="

# Ensure edge proxy is up to serve /.well-known/acme-challenge/ on port 80
echo "==> Ensuring Edge Reverse Proxy is running..."
docker compose up -d proxy

# Build domain arguments for Certbot
DOMAIN_ARGS=(-d "${FRONTEND_DOMAIN}")
if [ "${FRONTEND_DOMAIN}" != "${ADMIN_DOMAIN}" ]; then
  DOMAIN_ARGS+=(-d "${ADMIN_DOMAIN}")
fi

# Build staging arguments if requested
STAGING_ARG=()
if [ "${CERTBOT_STAGING}" != "0" ]; then
  echo "==> Enabling Let's Encrypt Staging environment (--staging)..."
  STAGING_ARG=(--staging)
fi

echo "==> Requesting TLS certificate from Let's Encrypt via HTTP-01 webroot challenge..."
docker compose run --rm --entrypoint "certbot certonly --webroot -w /var/www/certbot \
  ${STAGING_ARG[*]} \
  ${DOMAIN_ARGS[*]} \
  --email ${CERTBOT_EMAIL} \
  --rsa-key-size 4096 \
  --agree-tos \
  --no-eff-email \
  --force-renewal" certbot

echo "==> Reloading Nginx edge proxy configuration to apply new certificates..."
docker compose exec proxy nginx -s reload

echo "==> Let's Encrypt TLS bootstrap completed successfully."
