#!/usr/bin/env bash
set -euo pipefail
umask 077
here=$(cd "$(dirname "$0")" && pwd)
mode=${1:?missing action}
runtime=${2:?missing absolute runtime directory}
[[ $runtime = /* ]] || { echo "Runtime path must be absolute" >&2; exit 2; }
shift 2
if [[ $mode == up && ! -d $runtime ]]; then
    mkdir -m 700 "$runtime"
fi
exec 9>"$runtime/lock"
flock 9
if [[ $mode == up && ! -e $runtime/docker.env ]]; then
    {
        printf 'COMPOSE_PROJECT_NAME=norvale-qms-%s\n' "$(openssl rand -hex 8)"
        printf 'QMS_DB_PASSWORD=%s\n' "$(openssl rand -hex 32)"
        printf 'QMS_ADMIN_PASSWORD=%s\n' "$(openssl rand -hex 32)"
        printf 'QMS_PORT=%s\n' "$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
        printf 'QMS_IMAGE=%s\n' "${QMS_IMAGE:-frappe/erpnext:v15.124.1}"
    } > "$runtime/docker.env"
fi
set -a
source "$runtime/docker.env"
set +a
compose() {
    docker compose --env-file "$runtime/docker.env" -p "$COMPOSE_PROJECT_NAME" -f "$here/compose.yml" "$@"
}
case "$mode" in
    up)
        compose up -d --wait db redis
        if [[ ! -e $runtime/site-created ]]; then
            # Check the same network origin used by Bench. Socket health can pass during initialization.
            if ! compose run -T --rm --no-deps --entrypoint sh db -ec '
                export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"
                for attempt in $(seq 1 30); do
                    mariadb --connect-timeout=2 --protocol=tcp -hdb -uroot -e "SELECT 1" >/dev/null && exit 0
                    sleep 2
                done
                exit 1
            ' > "$runtime/bootstrap-login.log" 2>&1; then
                echo "QMS MariaDB bootstrap login from the Compose network failed; inspect $runtime/bootstrap-login.log and database grants" >&2
                exit 1
            fi
            # Follow norvale/environment/stack.py's Bench bootstrap with isolated services.
            if ! compose run -T --rm --no-deps -e QMS_ADMIN_PASSWORD -e QMS_DB_PASSWORD qms bash -ec '
                ls -1 apps > sites/apps.txt
                bench set-config -gp server_script_enabled 1
                bench set-config -g db_host db
                bench set-config -gp db_port 3306
                for key in redis_cache redis_queue redis_socketio; do
                    bench set-config -g "$key" redis://redis:6379
                done
                bench new-site --mariadb-user-host-login-scope="%" --admin-password="$QMS_ADMIN_PASSWORD" \
                    --db-root-username=root --db-root-password="$QMS_DB_PASSWORD" --install-app erpnext \
                    --set-default qms.localhost
            ' > "$runtime/new-site.log" 2>&1; then
                echo "bench new-site failed. Private diagnostics in $runtime/new-site.log" >&2
                exit 1
            fi
            touch "$runtime/site-created"
        fi
        compose up -d qms
        ;;
    restart) compose restart db redis qms ;;
    configure|fixture|lease-only|withdrawal-only|sample-writer-only)
        script=configure
        output=users
        option=()
        if [[ $mode == lease-only ]]; then
            option=(lease-only)
            output=dispatcher
        fi
        if [[ $mode == withdrawal-only ]]; then
            option=(withdrawal-only)
            output=withdrawal
        fi
        if [[ $mode == sample-writer-only ]]; then
            option=(sample-writer-only)
            output=sample-writer
        fi
        if [[ $mode == fixture ]]; then
            script=fixture
            output=fixture
        fi
        compose exec -T qms bash -ec '
            script_dir=$(mktemp -d)
            cleanup() { rm -rf "$script_dir"; }
            trap cleanup EXIT
            tar -xf - -C "$script_dir"
            cd sites
            ../env/bin/python "$script_dir/$1.py" "${@:2}"
        ' bash "$script" "${option[@]}" < <(tar -cf - -C "$here" configure.py fixture.py dispatch_lease.py disposition_history.py) > "$runtime/$output.tmp"
        mv "$runtime/$output.tmp" "$runtime/$output.json"
        ;;
    stop) compose stop ;;
    compose) compose "$@" ;;
    *) exit 2 ;;
esac
if [[ $mode == up || $mode == restart ]]; then
    url="http://$(compose port qms 8000)"
    for ((attempt=0; attempt<120; attempt++)); do
        if curl -fsS -H 'X-Frappe-Site-Name: qms.localhost' "$url/api/method/ping" >/dev/null 2>&1; then
            printf '%s\n' "$url" > "$runtime/url"
            exit 0
        fi
        sleep 1
    done
    echo "QMS failed readiness at $url" >&2
    exit 1
fi
