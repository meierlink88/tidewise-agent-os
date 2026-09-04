#!/usr/bin/env bash

set -euo pipefail

deployment_root="${DEPLOY_ROOT:?DEPLOY_ROOT is required}"
expected_runner="${UAT_RUNNER_NAME:?UAT_RUNNER_NAME is required}"
release_sha="${RELEASE_SHA:?RELEASE_SHA is required}"
runtime_gid="10002"

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1: $2" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || fail os "expected Linux"
[ "$(uname -m)" = aarch64 ] || fail architecture "expected aarch64"
[ "$(id -un)" = tidewise-deploy ] || fail deploy-user "expected tidewise-deploy"
[ "${RUNNER_NAME:-}" = "$expected_runner" ] || fail runner-name "expected $expected_runner"
[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]] || fail release-sha "expected a full lowercase Git commit SHA"
pass dgx-runtime-identity

for command in docker curl python3 sha256sum flock ss ip; do
  command -v "$command" >/dev/null || fail dependency "$command is missing"
done
docker info >/dev/null || fail docker-engine "docker info failed"
docker compose version >/dev/null || fail docker-compose "Docker Compose is unavailable"
pass docker-runtime

for directory in "$deployment_root" "$deployment_root/state" "$deployment_root/data" "$deployment_root/backups"; do
  [ -d "$directory" ] || fail deployment-directory "$directory is missing"
done
[ -w "$deployment_root/state" ] || fail state-directory "state directory is not writable"
[ -w "$deployment_root/backups" ] || fail backup-directory "backup directory is not writable"
[ "$(stat -c '%g' "$deployment_root/data")" = "$runtime_gid" ] || fail data-directory "data GID must be 10002"
[ "$(stat -c '%a' "$deployment_root/data")" = 2770 ] || fail data-directory "data mode must be 2770"
[ -s "$deployment_root/jwt-jwks.json" ] || fail jwks "JWT JWKS file is missing or empty"
[ "$(stat -c '%g' "$deployment_root/jwt-jwks.json")" = "$runtime_gid" ] || fail jwks "JWT JWKS GID must be 10002"
[ "$(stat -c '%a' "$deployment_root/jwt-jwks.json")" = 640 ] || fail jwks "JWT JWKS mode must be 640"
available_kb="$(df -Pk "$deployment_root" | awk 'NR == 2 {print $4}')"
[ "$available_kb" -ge 20971520 ] || fail disk-space "at least 20 GiB is required"
pass deployment-storage

agentos_image="${AGENTOS_IMAGE:-}"
[[ "$agentos_image" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || fail immutable-image "AGENTOS_IMAGE must be a local image ID"
docker image inspect "$agentos_image" >/dev/null || fail image-present "AGENTOS_IMAGE is not built locally"
[ "$(docker image inspect --format '{{.Architecture}}' "$agentos_image")" = arm64 ] \
  || fail image-architecture "AGENTOS_IMAGE is not arm64"
[ "$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$agentos_image")" = "$release_sha" ] \
  || fail image-revision "AGENTOS_IMAGE does not match RELEASE_SHA"

for image_var in POSTGRES_IMAGE NEO4J_IMAGE MINIO_IMAGE; do
  image="${!image_var:-}"
  [[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] || fail immutable-image "$image_var must use a digest"
  docker image inspect "$image" >/dev/null || fail image-present "$image_var is not pulled"
  [ "$(docker image inspect --format '{{.Architecture}}' "$image")" = arm64 ] \
    || fail image-architecture "$image_var is not arm64"
done
pass immutable-local-agentos-and-arm64-dependencies

python3 - <<'PY'
import ipaddress
import os
import socket
from urllib.parse import urlparse

external = urlparse(os.environ["AGENTOS_EXTERNAL_URL"])
if external.scheme != "https" or not external.hostname:
    raise SystemExit("FAIL external-url: absolute HTTPS URL required for MCP OAuth")
if external.port not in {None, 443} or external.path.rstrip("/") != "/agentos":
    raise SystemExit("FAIL external-url: expected /agentos on HTTPS port 443")
if external.query or external.fragment:
    raise SystemExit("FAIL external-url: query and fragment are not allowed")

data = urlparse(os.environ["DATA_SERVICE_BASE_URL"])
if data.scheme != "https" or not data.hostname or data.port not in {None, 443}:
    raise SystemExit("FAIL data-service-url: public HTTPS endpoint on port 443 is required")
if data.username or data.password or data.query or data.fragment:
    raise SystemExit("FAIL data-service-url: credentials, query and fragment are not allowed")
for _family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(data.hostname, 443):
    address = ipaddress.ip_address(sockaddr[0])
    if address.is_private or address.is_loopback or address.is_link_local:
        raise SystemExit(f"FAIL data-service-url: {data.hostname} resolved to non-public address")

lan_address = ipaddress.ip_address(os.environ["UAT_LAN_BIND_ADDRESS"])
if (
    lan_address.version != 4
    or not lan_address.is_private
    or lan_address.is_loopback
    or lan_address.is_link_local
    or lan_address.is_unspecified
):
    raise SystemExit("FAIL uat-lan-bind: a specific private IPv4 address is required")

ports = []
for name in (
    "POSTGRES_LAN_PORT",
    "NEO4J_HTTP_LAN_PORT",
    "NEO4J_BOLT_LAN_PORT",
    "MINIO_LAN_PORT",
    "MINIO_CONSOLE_PORT",
):
    try:
        port = int(os.environ[name])
    except ValueError as exc:
        raise SystemExit(f"FAIL uat-lan-port: {name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"FAIL uat-lan-port: {name} is outside 1..65535")
    ports.append(port)
if len(set(ports)) != len(ports) or 9081 in ports:
    raise SystemExit("FAIL uat-lan-port: dependency ports must be unique and must not use 9081")

raw_base = urlparse(os.environ["RAW_EVIDENCE_PUBLIC_BASE_URL"])
if (
    raw_base.scheme != "http"
    or raw_base.hostname != str(lan_address)
    or raw_base.port != int(os.environ["MINIO_LAN_PORT"])
    or raw_base.path.rstrip("/")
    or raw_base.username
    or raw_base.password
    or raw_base.query
    or raw_base.fragment
):
    raise SystemExit("FAIL raw-evidence-public-url: expected protected-LAN MinIO API base URL")
PY
pass public-https-and-lan-contracts

ip -o -4 addr show | awk '{split($4, address, "/"); print address[1]}' \
  | grep -Fqx "$UAT_LAN_BIND_ADDRESS" \
  || fail uat-lan-bind "$UAT_LAN_BIND_ADDRESS is not assigned to this DGX host"

check_dependency_port_owner() {
  local port="$1"
  local expected_service="$2"
  local container_ids
  local project
  local service
  container_ids="$(docker ps --filter "publish=${port}" --format '{{.ID}}')"
  while read -r container_id; do
    [ -z "$container_id" ] && continue
    project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$container_id")"
    service="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "$container_id")"
    [ "$project" = tidewise-agentos-uat ] && [ "$service" = "$expected_service" ] \
      || fail "port-${port}" "published by a container outside tidewise-agentos-uat/${expected_service}"
  done <<< "$container_ids"
  if [ -z "$container_ids" ] && [ -n "$(ss -H -ltn "sport = :${port}")" ]; then
    fail "port-${port}" "occupied by a non-Docker listener"
  fi
}

check_dependency_port_owner "$POSTGRES_LAN_PORT" postgres
check_dependency_port_owner "$NEO4J_HTTP_LAN_PORT" neo4j
check_dependency_port_owner "$NEO4J_BOLT_LAN_PORT" neo4j
check_dependency_port_owner "$MINIO_LAN_PORT" minio
check_dependency_port_owner "$MINIO_CONSOLE_PORT" minio
pass protected-lan-dependency-ports

: "${DATA_SERVICE_TOKEN:?DATA_SERVICE_TOKEN is required}"
docker run --rm \
  -e DATA_SERVICE_BASE_URL -e DATA_SERVICE_TOKEN \
  --entrypoint python "$AGENTOS_IMAGE" -c '
from capabilities.collection import load_active_source_snapshot
load_active_source_snapshot()
' >/dev/null || fail source-snapshot "authenticated public Data Service Source Snapshot is unavailable or invalid"
pass public-data-service-source-snapshot

: "${DEEPSEEK_API_KEY:?DEEPSEEK_API_KEY is required}"
: "${NEO4J_PASSWORD:?NEO4J_PASSWORD is required}"
: "${MINIO_ACCESS_KEY:?MINIO_ACCESS_KEY is required}"
: "${MINIO_SECRET_KEY:?MINIO_SECRET_KEY is required}"
: "${RAW_EVIDENCE_BUCKET:?RAW_EVIDENCE_BUCKET is required}"
[[ "$RAW_EVIDENCE_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] \
  || fail raw-evidence-bucket "must be a valid lowercase S3 bucket name"
[ "${#MINIO_ACCESS_KEY}" -ge 3 ] || fail minio-access-key "must contain at least 3 characters"
[ "${#MINIO_SECRET_KEY}" -ge 8 ] || fail minio-secret-key "must contain at least 8 characters"
: "${GRAPHITI_EMBEDDING_API_KEY:?GRAPHITI_EMBEDDING_API_KEY is required}"
: "${GRAPHITI_EMBEDDING_BASE_URL:?GRAPHITI_EMBEDDING_BASE_URL is required}"
: "${GRAPHITI_EMBEDDING_MODEL:?GRAPHITI_EMBEDDING_MODEL is required}"
: "${GRAPHITI_EMBEDDING_DIM:?GRAPHITI_EMBEDDING_DIM is required}"

container_ids="$(docker ps --filter 'publish=9081' --format '{{.ID}}')"
while read -r container_id; do
  [ -z "$container_id" ] && continue
  project="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$container_id")"
  [ "$project" = tidewise-agentos-uat ] || fail port-9081 "published by a container outside tidewise-agentos-uat"
done <<< "$container_ids"
if [ -z "$container_ids" ] && [ -n "$(ss -H -ltn 'sport = :9081')" ]; then
  fail port-9081 "occupied by a non-Docker listener"
fi
pass loopback-port-9081
