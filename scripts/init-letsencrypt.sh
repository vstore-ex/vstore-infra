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
CERTBOT_FORCE_RENEWAL="${CERTBOT_FORCE_RENEWAL:-0}"
CERTBOT_INCLUDE_ADMIN_DOMAIN="${CERTBOT_INCLUDE_ADMIN_DOMAIN:-1}"

echo "================================================================="
echo "VStore Let's Encrypt TLS Bootstrap"
echo "Frontend Domain: ${FRONTEND_DOMAIN}"
echo "Admin Domain:    ${ADMIN_DOMAIN}"
echo "Contact Email:   ${CERTBOT_EMAIL}"
echo "Staging Mode:    ${CERTBOT_STAGING}"
echo "Force Renewal:   ${CERTBOT_FORCE_RENEWAL}"
echo "================================================================="

# Ensure edge proxy is up to serve /.well-known/acme-challenge/ on port 80
echo "==> Ensuring Edge Reverse Proxy is running..."
bash scripts/compose.sh up -d proxy

# Build certbot command arguments as a proper bash array.
# Each element stays a discrete argument through "${array[@]}" expansion,
# avoiding the word-splitting pitfalls of interpolating into a single string.
CERTBOT_ARGS=(
  certonly
  --webroot
  -w /var/www/certbot
  --email "${CERTBOT_EMAIL}"
  --rsa-key-size 4096
  --agree-tos
  --no-eff-email
)

# Append domain arguments
CERTBOT_ARGS+=(-d "${FRONTEND_DOMAIN}")
if [ "${CERTBOT_INCLUDE_ADMIN_DOMAIN}" != "0" ] && [ "${FRONTEND_DOMAIN}" != "${ADMIN_DOMAIN}" ]; then
  CERTBOT_ARGS+=(-d "${ADMIN_DOMAIN}")
fi

# Append staging flag if requested
if [ "${CERTBOT_STAGING}" != "0" ]; then
  echo "==> Enabling Let's Encrypt Staging environment (--staging)..."
  CERTBOT_ARGS+=(--staging)
fi

# Append --force-renewal only if explicitly requested.
# Without it, certbot skips issuance when a valid cert already exists,
# preventing accidental Let's Encrypt rate-limit consumption on re-runs.
if [ "${CERTBOT_FORCE_RENEWAL}" != "0" ]; then
  echo "==> Force renewal enabled — existing certificates will be replaced."
  CERTBOT_ARGS+=(--force-renewal)
fi

echo "==> Requesting TLS certificate from Let's Encrypt via HTTP-01 webroot challenge..."
# Use exec-form entrypoint (single binary) with arguments passed as CMD.
# This avoids shell-form entrypoint fragility where bash scripts/compose.sh run
# --entrypoint may interpret a multi-word string as a single executable name.
bash scripts/compose.sh run --rm --entrypoint certbot certbot "${CERTBOT_ARGS[@]}"

echo "==> Reloading Nginx edge proxy configuration to apply new certificates..."
bash scripts/compose.sh exec proxy nginx -s reload

echo "==> Let's Encrypt TLS bootstrap completed successfully."
