#!/usr/bin/env bash

set -euo pipefail

deployment_root="${DEPLOY_ROOT:?DEPLOY_ROOT is required}"
expected_runner="${UAT_RUNNER_NAME:?UAT_RUNNER_NAME is required}"
swr_registry="${SWR_REGISTRY:?SWR_REGISTRY is required}"
agentos_image="${AGENTOS_IMAGE:?AGENTOS_IMAGE is required}"
runtime_gid="10002"

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1: $2" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || fail os "expected Linux"
[ "$(uname -m)" = x86_64 ] || fail architecture "expected x86_64"
[ "$(id -un)" = tidewise-deploy ] || fail deploy-user "expected tidewise-deploy"
[ "${RUNNER_NAME:-}" = "$expected_runner" ] || fail runner-name "expected $expected_runner"
pass runtime-identity

for command in docker curl python3 sha256sum flock ss; do
  command -v "$command" >/dev/null || fail dependency "$command is missing"
done
docker info >/dev/null || fail docker-engine "docker info failed"
docker compose version >/dev/null || fail docker-compose "Docker Compose v2 is unavailable"
docker network inspect tidewise-uat >/dev/null || fail docker-network "external network tidewise-uat is missing"
pass docker-runtime-and-network

for directory in "$deployment_root" "$deployment_root/state" "$deployment_root/data"; do
  [ -d "$directory" ] || fail deployment-directory "$directory is missing"
done
[ -w "$deployment_root/state" ] || fail state-directory "state directory is not writable"
[ -d /opt/tidewise/uat ] && [ -w /opt/tidewise/uat ] \
  || fail shared-deployment-lock "Tidewise AI UAT deployment root is not writable"
[ "$(stat -c '%g' "$deployment_root/data")" = "$runtime_gid" ] || fail data-directory "data GID must be 10002"
[ "$(stat -c '%a' "$deployment_root/data")" = 2770 ] || fail data-directory "data mode must be 2770"
[ -s "$deployment_root/jwt-jwks.json" ] || fail jwks "JWT JWKS file is missing or empty"
[ "$(stat -c '%g' "$deployment_root/jwt-jwks.json")" = "$runtime_gid" ] || fail jwks "JWT JWKS GID must be 10002"
[ "$(stat -c '%a' "$deployment_root/jwt-jwks.json")" = 640 ] || fail jwks "JWT JWKS mode must be 640"
pass deployment-storage

available_kb="$(df -Pk "$deployment_root" | awk 'NR == 2 {print $4}')"
[ "$available_kb" -ge 5242880 ] || fail disk-space "at least 5 GiB is required"
pass disk-space

swr_status="$(curl --silent --show-error --connect-timeout 5 --max-time 15 --output /dev/null --write-out '%{http_code}' "https://${swr_registry}/v2/")"
case "$swr_status" in
  200|401) pass swr-registry-endpoint ;;
  *) fail swr-registry-endpoint "unexpected HTTP status $swr_status" ;;
esac

[ "${DB_SSLMODE:-}" = require ] || fail rds-tls "DB_SSLMODE=require is mandatory"
[ "${DB_DATABASE:-}" = agent_os_uat ] || fail rds-database "expected agent_os_uat"
[ "${DB_USER:-}" = agent_os_uat_runtime ] || fail rds-role "expected agent_os_uat_runtime"
python3 - <<'PY'
import os
import socket
from urllib.parse import urlparse

with socket.create_connection((os.environ["DB_HOST"], int(os.environ.get("DB_PORT", "5432"))), timeout=10):
    pass
external = urlparse(os.environ["AGENTOS_EXTERNAL_URL"])
if external.scheme not in {"http", "https"} or not external.hostname:
    raise SystemExit("FAIL external-url: absolute HTTP(S) URL required")
if external.port != 9081 or external.path not in {"", "/"} or external.query or external.fragment:
    raise SystemExit("FAIL external-url: current UAT contract requires an origin URL on fixed port 9081")
PY
pass rds-private-tcp-and-external-url

docker run --rm --network tidewise-uat --entrypoint curl "$agentos_image" \
  -fsS --connect-timeout 5 --max-time 15 http://data:9011/readyz >/dev/null \
  || fail data-service "http://data:9011/readyz is unavailable"
pass internal-data-service

container_ids="$(docker ps --filter 'publish=9081' --format '{{.ID}}')"
while read -r container_id; do
  [ -z "$container_id" ] && continue
  project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$container_id")"
  [ "$project" = tidewise-agentos-uat ] || fail port-9081 "published by a container outside tidewise-agentos-uat"
done <<< "$container_ids"
if [ -z "$container_ids" ] && [ -n "$(ss -H -ltn 'sport = :9081')" ]; then
  fail port-9081 "occupied by a non-Docker listener"
fi
pass port-9081
