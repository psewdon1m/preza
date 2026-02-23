#!/bin/sh
set -eu

: "${SITE_DOMAIN:=localhost}"

cert_dir="/etc/letsencrypt/live/${SITE_DOMAIN}"
fullchain="${cert_dir}/fullchain.pem"
privkey="${cert_dir}/privkey.pem"

mkdir -p "${cert_dir}"

if [ ! -s "${fullchain}" ] || [ ! -s "${privkey}" ]; then
  echo "Generating temporary self-signed certificate for ${SITE_DOMAIN}"
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout "${privkey}" \
    -out "${fullchain}" \
    -subj "/CN=${SITE_DOMAIN}"
fi
