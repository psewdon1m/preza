#!/usr/bin/env bash
set -euo pipefail

HTTP_PORT="${HTTP_PORT:-80}"
HTTPS_PORT="${HTTPS_PORT:-443}"
RESERVED_NOTE_PORT="${RESERVED_NOTE_PORT:-62050}"

echo "Checking required ports for deployment..."
echo "Required: ${HTTP_PORT}/tcp, ${HTTPS_PORT}/tcp"
echo "Existing service to keep intact: ${RESERVED_NOTE_PORT}/tcp"
echo

if ! command -v ss >/dev/null 2>&1; then
  echo "ERROR: 'ss' command not found. Install iproute2 package."
  exit 1
fi

echo "Current listeners (tcp):"
ss -tulnp | grep -E "LISTEN|Local Address:Port" || true
echo

check_port() {
  local port="$1"
  local busy
  busy="$(ss -tulnp | awk -v p=":${port}" '$1=="tcp" && $2=="LISTEN" && $5 ~ p"$" {print}')"
  if [[ -n "${busy}" ]]; then
    echo "PORT ${port}: BUSY"
    echo "${busy}"
    return 1
  fi
  echo "PORT ${port}: free"
}

status=0
check_port "${HTTP_PORT}" || status=1
check_port "${HTTPS_PORT}" || status=1
echo

if ss -tulnp | awk -v p=":${RESERVED_NOTE_PORT}" '$1=="tcp" && $2=="LISTEN" && $5 ~ p"$" {found=1} END {exit !found}'; then
  echo "Port ${RESERVED_NOTE_PORT} is listening (expected existing proxy node)."
else
  echo "WARNING: Port ${RESERVED_NOTE_PORT} is not listening right now."
fi

if [[ "${status}" -ne 0 ]]; then
  echo
  echo "Deployment would conflict with existing listeners on required ports."
  exit 1
fi

echo
echo "No conflicts detected for required deployment ports."

