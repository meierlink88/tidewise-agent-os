#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_env="${1:-${repository_root}/../tidewise-reason/.runtime/graphiti.env}"
target_env="${2:-${repository_root}/.env}"

if [[ ! -f "${source_env}" ]]; then
    echo "Graphiti runtime source does not exist: ${source_env}" >&2
    exit 1
fi

if [[ ! -f "${target_env}" ]]; then
    echo "AgentOS runtime environment does not exist: ${target_env}" >&2
    exit 1
fi

keys=(
    NEO4J_USER
    NEO4J_PASSWORD
    NEO4J_HTTP_PORT
    NEO4J_BOLT_PORT
    GRAPHITI_LLM_API_KEY
    GRAPHITI_LLM_BASE_URL
    GRAPHITI_LLM_MODEL
    GRAPHITI_EMBEDDING_API_KEY
    GRAPHITI_EMBEDDING_BASE_URL
    GRAPHITI_EMBEDDING_MODEL
    GRAPHITI_EMBEDDING_DIM
)

temporary_env="$(mktemp "${target_env}.XXXXXX")"
trap 'rm -f "${temporary_env}"' EXIT
cp "${target_env}" "${temporary_env}"

added=0
for key in "${keys[@]}"; do
    if grep -q "^${key}=" "${target_env}"; then
        continue
    fi

    line="$(grep -m 1 "^${key}=" "${source_env}" || true)"
    if [[ -z "${line}" ]]; then
        echo "Required Graphiti runtime key is missing: ${key}" >&2
        exit 1
    fi

    printf '%s\n' "${line}" >>"${temporary_env}"
    added=$((added + 1))
done

chmod 600 "${temporary_env}"
mv "${temporary_env}" "${target_env}"
trap - EXIT

echo "Graphiti runtime configuration is ready (${added} keys added; secret values were not printed)."
