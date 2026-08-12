#!/usr/bin/env bash

set -euo pipefail

runtime_env="${RUNTIME_ENV:?RUNTIME_ENV is required}"
images_env="${CANDIDATE_IMAGES:?CANDIDATE_IMAGES is required}"
compose_file="${COMPOSE_FILE:?COMPOSE_FILE is required}"

redact() {
  sed -E \
    -e 's/(Authorization: Bearer )[A-Za-z0-9._-]+/\1[REDACTED]/g' \
    -e 's#(postgresql[^:]*://[^:]+:)[^@]+@#\1[REDACTED]@#g' \
    -e 's/(API_KEY|TOKEN|SECRET|DB_PASS)=([^[:space:]]+)/\1=[REDACTED]/gI'
}

compose=(docker compose --env-file "$runtime_env" --env-file "$images_env" -f "$compose_file")
echo "===== docker compose ps ====="
"${compose[@]}" ps 2>&1 | redact
echo "===== agentos logs ====="
"${compose[@]}" logs --no-color --tail 300 agentos 2>&1 | redact
