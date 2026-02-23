#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env.production}"
COMPOSE="docker compose --env-file ${ENV_FILE}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: env file not found: ${ENV_FILE}"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed"
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon is not running"
  exit 1
fi

echo "Using env file: ${ENV_FILE}"
echo "Step 1/4: Validate compose config"
${COMPOSE} config >/dev/null

echo "Step 2/4: Check host ports"
export HTTP_PORT HTTPS_PORT
HTTP_PORT="$(grep '^HTTP_PORT=' "${ENV_FILE}" | cut -d= -f2- || echo 80)"
HTTPS_PORT="$(grep '^HTTPS_PORT=' "${ENV_FILE}" | cut -d= -f2- || echo 443)"
if ${COMPOSE} ps --status running proxy | grep -q "proxy"; then
  echo "Proxy container is already running; skipping strict port pre-check."
else
  "${ROOT_DIR}/scripts/check-ports.sh"
fi

echo "Step 3/4: Build and start services"
${COMPOSE} up -d --build

echo "Step 4/4: Show service status"
${COMPOSE} ps

echo
echo "Deployment completed."
echo "Next (first time only): issue Let's Encrypt certificate with scripts/issue-cert.sh"
