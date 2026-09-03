#!/usr/bin/env bash

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "bootstrap-dgx.sh must be run manually as root" >&2
  exit 1
fi
if [ "$(uname -s)" != Linux ] || [ "$(uname -m)" != aarch64 ]; then
  echo "DGX UAT bootstrap requires Linux aarch64" >&2
  exit 1
fi

deploy_user="${TIDEWISE_DEPLOY_USER:-tidewise-deploy}"
deploy_root="${AGENTOS_DEPLOY_ROOT:-/opt/tidewise/agentos-uat}"
runner_root="${AGENTOS_RUNNER_ROOT:-/opt/tidewise/agentos-actions-runner}"
runtime_group="tidewise-agentos"
runtime_gid="10002"
runner_name="${UAT_RUNNER_NAME:?UAT_RUNNER_NAME is required}"
repository_url="${GITHUB_REPOSITORY_URL:?GITHUB_REPOSITORY_URL is required}"
registration_token="${GITHUB_RUNNER_REGISTRATION_TOKEN:?GITHUB_RUNNER_REGISTRATION_TOKEN is required}"
runner_archive="${ACTIONS_RUNNER_ARCHIVE:?ACTIONS_RUNNER_ARCHIVE is required}"
runner_archive_sha256="${ACTIONS_RUNNER_ARCHIVE_SHA256:?ACTIONS_RUNNER_ARCHIVE_SHA256 is required}"

# DGX Docker is NVIDIA-managed host infrastructure. Bootstrap validates it and
# never replaces or upgrades it from distribution packages.
for command in docker curl git python3 sha256sum flock ss openssl gzip tar; do
  command -v "$command" >/dev/null || { echo "missing required command: $command" >&2; exit 1; }
done
docker info >/dev/null
docker compose version >/dev/null

if ! id "$deploy_user" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$deploy_user"
fi
if group_entry="$(getent group "$runtime_group")"; then
  existing_gid="$(printf '%s\n' "$group_entry" | cut -d: -f3)"
  [ "$existing_gid" = "$runtime_gid" ] || {
    echo "${runtime_group} must use GID ${runtime_gid}, found ${existing_gid}" >&2
    exit 1
  }
elif getent group "$runtime_gid" >/dev/null; then
  echo "GID ${runtime_gid} is already assigned to another group" >&2
  exit 1
else
  groupadd --gid "$runtime_gid" "$runtime_group"
fi
usermod -aG docker "$deploy_user"
usermod -aG "$runtime_group" "$deploy_user"

install -d -m 0750 -o "$deploy_user" -g "$deploy_user" "$deploy_root" "$deploy_root/state" "$deploy_root/backups"
install -d -m 2770 -o "$deploy_user" -g "$runtime_group" "$deploy_root/data"
install -m 0640 -o "$deploy_user" -g "$runtime_group" /dev/null "$deploy_root/jwt-jwks.json"

printf '%s  %s\n' "$runner_archive_sha256" "$runner_archive" | sha256sum --check --status
if [ ! -x "$runner_root/config.sh" ]; then
  install -d -m 0750 -o "$deploy_user" -g "$deploy_user" "$runner_root"
  tar -xzf "$runner_archive" -C "$runner_root"
  chown -R "$deploy_user:$deploy_user" "$runner_root"
fi
if [ ! -f "$runner_root/.runner" ]; then
  (
    cd "$runner_root"
    runuser -u "$deploy_user" -- ./config.sh \
      --url "$repository_url" \
      --token "$registration_token" \
      --name "$runner_name" \
      --labels tidewise-agentos-uat-dgx \
      --unattended \
      --replace
  )
fi
(
  cd "$runner_root"
  if [ ! -f .service ]; then
    ./svc.sh install "$deploy_user"
  fi
  ./svc.sh start
)

echo "AgentOS UAT DGX bootstrap complete. Re-login before using new group memberships."
