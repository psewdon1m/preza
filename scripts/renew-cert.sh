#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env.production}"
COMPOSE="docker compose --env-file ${ENV_FILE}"

${COMPOSE} run --rm certbot renew --webroot -w /var/www/certbot
${COMPOSE} exec proxy nginx -s reload

echo "Renew completed and nginx reloaded."

