#!/usr/bin/env bash
# Freeze the application code for a new isolated acceptance run.
set -euo pipefail
umask 077
runtime=${1:?new absolute directory}
source=$(cd "$(dirname "$0")/../.." && pwd -P)
runtime=$(realpath -m "$runtime")
[[ "$runtime" != "$source" && "$runtime" != "$source/"* ]]
mkdir -m 700 "$runtime"
mkdir "$runtime/release"
tar -C "$source" --exclude=.git --exclude=engine_build --exclude=__pycache__ -cf - . |
    tar -C "$runtime/release" -xpf -
cp -a "$runtime/release/integrations/revisions/odoo_addons/." "$runtime/release/integrations/mes/addons/"
