#!/bin/sh
set -eu

# Target directory for the primary domain certificate
CERT_DIR="/etc/letsencrypt/live/${FRONTEND_DOMAIN:-vstore.local}"
FULLCHAIN="${CERT_DIR}/fullchain.pem"
PRIVKEY="${CERT_DIR}/privkey.pem"

if [ ! -f "${FULLCHAIN}" ] || [ ! -f "${PRIVKEY}" ]; then
    echo "==> [nginx-init] No existing certificate found for ${FRONTEND_DOMAIN:-vstore.local}."
    echo "==> [nginx-init] Generating fallback self-signed dummy certificate..."
    mkdir -p "${CERT_DIR}"
    openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
        -keyout "${PRIVKEY}" \
        -out "${FULLCHAIN}" \
        -subj "/CN=${FRONTEND_DOMAIN:-vstore.local}" \
        2>/dev/null
    echo "==> [nginx-init] Dummy certificate generated successfully."
else
    echo "==> [nginx-init] Certificate already exists at ${FULLCHAIN}."
fi
