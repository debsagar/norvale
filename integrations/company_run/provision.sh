#!/usr/bin/env bash
# Fresh run only. prepare.sh assembles the release first.
set -euo pipefail
umask 077
run=$(realpath "${1:?prepared runtime}")
cd "$run/release"
[[ -d integrations/company_run && ! -e "$run/services.json" ]]
df -h "$run"
export PYTHONPATH=".:native:integrations/lake${PYTHONPATH:+:$PYTHONPATH}" NORVALE_OFFLINE=1
: "${NORVALE_CLOCK_LIB:?release libfaketime directory required}"
suffix=$(openssl rand -hex 6)
mes_project=nvmes-company-$suffix
plm_project=nvplm-company-$suffix
port(){ python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
export PLM_PORT=$(port)
for key in publisher reader engineering approver mes-password mes-rework-password mes-analytics-password mes-db; do openssl rand -hex 24 > "$run/$key"; done
export PLM_PUBLISHER_PASSWORD=$(cat "$run/publisher") PLM_READER_PASSWORD=$(cat "$run/reader")
export PLM_ENGINEERING_PASSWORD=$(cat "$run/engineering") PLM_APPROVER_PASSWORD=$(cat "$run/approver")
export NORVALE_PLM_CHANGE_GUARD=${NORVALE_COMPANY_OPERATING:-0}
pc(){ docker compose -p "$plm_project" -f integrations/plm/compose.yaml "$@"; }
pc run --rm --no-deps plm python3 src/backend/InvenTree/manage.py migrate --noinput
pc up -d
curl -fsS --retry 90 --retry-all-errors --retry-delay 1 "http://127.0.0.1:$PLM_PORT/api/" >/dev/null
pc exec -T -e PLM_PUBLISHER_PASSWORD -e PLM_READER_PASSWORD -e PLM_ENGINEERING_PASSWORD -e PLM_APPROVER_PASSWORD plm python3 src/backend/InvenTree/manage.py shell < integrations/plm/provision.py
export MES_PORT=$(port) MES_TEST_PASSWORD=$(cat "$run/mes-password") MES_DB_PASSWORD=$(cat "$run/mes-db")
export MES_REWORK_PASSWORD=$(cat "$run/mes-rework-password")
export MES_ANALYTICS_PASSWORD=$(cat "$run/mes-analytics-password")
mc(){ docker compose -p "$mes_project" -f integrations/mes/compose.yaml "$@"; }
mc run --rm -T odoo --database=norvale_mes --init=norvale_mes,norvale_revision_receiver --without-demo=all --stop-after-init --no-http
mc run --rm -T -e MES_TEST_PASSWORD -e MES_REWORK_PASSWORD -e MES_ANALYTICS_PASSWORD odoo odoo shell -d norvale_mes --no-http < integrations/mes/fixture.py >"$run/mes-fixture.json"
mc run --rm -T odoo odoo shell -d norvale_mes --no-http < integrations/company_run/mes_precision.py
mc up -d
curl -fsS --retry 90 --retry-all-errors --retry-delay 1 "http://127.0.0.1:$MES_PORT/web/health"
python3 - "$run/services.json" "$mes_project" "$MES_PORT" "$plm_project" "$PLM_PORT" <<'PY'
import json, sys
from pathlib import Path
path, mes, mp, plm, pp = sys.argv[1:]
Path(path).write_text(json.dumps(dict(mes_project=mes, mes_url='http://127.0.0.1:'+mp,
                                     plm_project=plm, plm_url='http://127.0.0.1:'+pp)))
PY
for action in up configure fixture; do bash integrations/qms/provision.sh "$action" "$run/qms"; done
NORVALE_ERP_PORT=$(port) python3 - "$run/business" <<'PY'
import os, sys
from pathlib import Path
from native.company import initialize, DEFAULT_CONFIG
from integrations.company_run.timing import DEFAULT_TIMING
scenario = Path(sys.argv[1]).parent / "scenario.json"
settings = {}
if scenario.exists():
    from simulate import load_scenario
    cfg = load_scenario(scenario)
    settings = {k: cfg[k] for k in ("days", "seed", "opening_orders")}
config = dict(DEFAULT_CONFIG, company_authority='integrations.company_run.authority:Authority',
    company_timing=DEFAULT_TIMING, days=8, orders_per_day=1, report_every=2,
    **(dict(company_operating=True, supplier_quality=True) if os.environ.get('NORVALE_COMPANY_OPERATING') == '1'
       else dict(maintenance_units=24, maintenance_days=1)))
config.update(settings)
e = initialize(sys.argv[1], config)
e.step('seed')
PY
sample=()
if [[ ${NORVALE_COMPANY_OPERATING:-0} == 1 ]]; then
    bash integrations/qms/provision.sh sample-writer-only "$run/qms"
    sample=(--sample-writer)
fi
python3 -m integrations.company_run.configure "$run" --scoped-receipts "${sample[@]}"
