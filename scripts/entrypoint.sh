#!/bin/bash

set -Eeuo pipefail

############################################################################
#
#    Agno Container Entrypoint
#
############################################################################

# Colors
ORANGE='\033[38;5;208m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${ORANGE}"
cat << 'BANNER'
     █████╗  ██████╗ ███╗   ██╗ ██████╗
    ██╔══██╗██╔════╝ ████╗  ██║██╔═══██╗
    ███████║██║  ███╗██╔██╗ ██║██║   ██║
    ██╔══██║██║   ██║██║╚██╗██║██║   ██║
    ██║  ██║╚██████╔╝██║ ╚████║╚██████╔╝
    ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝
BANNER
echo -e "${NC}"

wait_for_db="${WAIT_FOR_DB:-False}"
if [[ "$wait_for_db" = true || "$wait_for_db" = True ]]; then
    : "${DB_HOST:?DB_HOST is required when WAIT_FOR_DB is true}"
    : "${DB_PORT:?DB_PORT is required when WAIT_FOR_DB is true}"
    echo -e "    ${DIM}Waiting for database at ${DB_HOST}:${DB_PORT}...${NC}"
    python - "$DB_HOST" "$DB_PORT" <<'PY'
import socket
import sys
import time

host = sys.argv[1]
port = int(sys.argv[2])
deadline = time.monotonic() + 300
last_error: OSError | None = None

while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=5):
            break
    except OSError as exc:
        last_error = exc
        time.sleep(1)
else:
    raise SystemExit(f"Database unavailable after 300s: {host}:{port}: {last_error}")
PY
    echo -e "    ${BOLD}Database ready.${NC}"
    echo ""
fi

case "$1" in
    chill)
        echo -e "    ${DIM}Mode: chill${NC}"
        echo -e "    ${BOLD}Container running.${NC}"
        echo ""
        while true; do sleep 18000; done
        ;;
    *)
        echo -e "    ${DIM}> $@${NC}"
        echo ""
        exec "$@"
        ;;
esac
