#!/usr/bin/env bash
# Run from any directory. Keep evidence in a fresh private directory.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
: "${MES_TEST_SOURCE:?choose a fresh Compose project name beginning nvmes-}"
: "${MES_PORT:?choose an unused loopback port}"
[[ "$MES_TEST_SOURCE" =~ ^nvmes-[a-z0-9-]+$ ]]
export MES_TEST_SOURCE MES_PORT
export MES_DB_PASSWORD="$(openssl rand -hex 24)"
export MES_TEST_PASSWORD="$(openssl rand -hex 24)"
export MES_ANALYTICS_PASSWORD="$(openssl rand -hex 24)"
export MES_REWORK_PASSWORD="$(openssl rand -hex 24)"
export MES_TEST_URL="http://127.0.0.1:$MES_PORT"
if [[ -z ${MES_REWORK_QMS_RUNTIME:-} ]]; then python3 -c 'import deltalake, pyarrow'; fi
umask 077
evidence=$(mktemp -d "${TMPDIR:-/tmp}/${MES_TEST_SOURCE}.XXXXXX")
echo "Evidence directory $evidence"
compose=(docker compose -p "$MES_TEST_SOURCE")
if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$MES_TEST_SOURCE")" ]] ||
   [[ -n "$(docker volume ls -q --filter "label=com.docker.compose.project=$MES_TEST_SOURCE")" ]]; then
    echo "Compose project already exists" >&2
    exit 1
fi
trap '"${compose[@]}" stop odoo db >/dev/null 2>&1 || true' EXIT
free -h
df -h "$(docker info --format '{{.DockerRootDir}}')"
python3 - <<'PY'
import os
import socket
with socket.socket() as port:
    port.bind(("127.0.0.1", int(os.environ["MES_PORT"])))
PY
"${compose[@]}" run --rm -T odoo --database=norvale_mes --init=norvale_mes --without-demo=all \
    --stop-after-init --no-http >"$evidence/init.log" 2>&1
"${compose[@]}" run --rm -T -e MES_TEST_PASSWORD -e MES_REWORK_PASSWORD -e MES_ANALYTICS_PASSWORD odoo odoo shell -d norvale_mes --no-http \
    <fixture.py >"$evidence/fixture.json" 2>"$evidence/fixture.log"
"${compose[@]}" up -d odoo

ready() {
    for attempt in {1..45}; do
        if curl --silent --fail --max-time 3 "$MES_TEST_URL/web/health" >/dev/null; then
            return
        fi
        sleep 1
    done
    return 1
}

check() {
    (cd ../.. && PYTHONPATH=".:integrations/lake:${PYTHONPATH:-}" python3 -m integrations.mes.test_live "$1" "$evidence/fixture.json" "$evidence/checkpoint.json")
}

ready
if [[ -n ${MES_REWORK_QMS_RUNTIME:-} ]]; then
    (cd ../.. && python3 -m integrations.mes.check_rework "$evidence/fixture.json" \
        "$MES_REWORK_QMS_RUNTIME" "$evidence/controller") | tee "$evidence/rework.json"
    exit
fi
check before | tee "$evidence/before.txt"
"${compose[@]}" stop odoo
"${compose[@]}" restart db
"${compose[@]}" up -d odoo
ready
check after | tee "$evidence/after.txt"
check interrupt | tee "$evidence/interrupt.txt"
