#!/usr/bin/env bash
set -euo pipefail

# Source checkout / local development. Production uses the signed bootstrap.
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"
docker compose config --quiet
docker compose up -d --build
docker compose ps
