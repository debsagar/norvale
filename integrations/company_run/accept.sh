#!/usr/bin/env bash
# Schedule independent scans; run the same engine again after the deliberate cut.
set -euo pipefail
umask 077
run=$(realpath "${1:?prepared demo runtime}")
cd "$run/release"
export PYTHONPATH=".:native:integrations/lake:${PYTHONPATH:-}" NORVALE_OFFLINE=1
export RAYON_NUM_THREADS=1 TOKIO_WORKER_THREADS=1 ARROW_NUM_THREADS=1
if [[ ! -f $run/analytics.json ]]; then python3 -m integrations.company_run.configure "$run"; fi
python3 -m integrations.company_run.lake "$run/analytics.json" "$run/lake" --run "$run/business" --interval 60 >>"$run/lake.log" 2>&1 &
worker=$!
trap 'kill "$worker" 2>/dev/null || true; wait "$worker" 2>/dev/null || true' EXIT
if [[ ! -f $run/interruption-confirmed ]]; then
    touch "$run/business/interrupt-after-release"
    if python3 -m integrations.company_run "$run/business" >"$run/first-process.log" 2>&1; then
        echo 'Expected a mid-handoff interruption' >&2; exit 1
    fi
    grep -F 'InterruptedError: Acceptance interruption after native QMS release, before ERP sync' "$run/first-process.log"
    touch "$run/interruption-confirmed"
fi
python3 -m integrations.company_run "$run/business"
python3 -m integrations.company_run.lake "$run/analytics.json" "$run/lake" --run "$run/business"
python3 -m integrations.company_run.verify "$run"
