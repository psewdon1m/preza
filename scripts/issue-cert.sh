#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env.production}"
COMPOSE="docker compose --env-file ${ENV_FILE}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: env file not found: ${ENV_FILE}"
  exit 1
fi

DOMAIN="$(grep '^SITE_DOMAIN=' "${ENV_FILE}" | cut -d= -f2-)"
EMAIL="$(grep '^LETSENCRYPT_EMAIL=' "${ENV_FILE}" | cut -d= -f2-)"

if [[ -z "${DOMAIN}" || -z "${EMAIL}" ]]; then
  echo "ERROR: SITE_DOMAIN and LETSENCRYPT_EMAIL must be set in ${ENV_FILE}"
  exit 1
fi

echo "Issuing certificate for ${DOMAIN}"
echo "Make sure DNS A-record points to this server and port 80 is reachable."

if ! ${COMPOSE} ps --status running proxy | grep -q "proxy"; then
  echo "ERROR: proxy container is not running."
  echo "Run deployment first: ./scripts/deploy.sh"
  exit 1
fi

# If nginx started with a temporary self-signed cert, remove placeholder lineage
# before the first Certbot issuance so Certbot can create its own live/archive links.
${COMPOSE} run --rm --entrypoint sh certbot -lc "
  if [ ! -f /etc/letsencrypt/renewal/${DOMAIN}.conf ]; then
    rm -rf /etc/letsencrypt/live/${DOMAIN} /etc/letsencrypt/archive/${DOMAIN}
  fi
"

${COMPOSE} run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d "${DOMAIN}" \
  --email "${EMAIL}" \
  --agree-tos \
  --no-eff-email

${COMPOSE} exec proxy nginx -s reload

echo "Certificate issued and nginx reloaded."
