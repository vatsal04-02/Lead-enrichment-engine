#!/usr/bin/env bash
# Start a throwaway local PostgreSQL cluster for development/tests/benchmarks.
# Usage: scripts/dev_postgres.sh [start|stop|status]   (default: start)
# Creates databases "enrich" and "enrich_test" with trust auth on 127.0.0.1:${PGPORT:-55432}.
# Connection string: postgresql://enrich@127.0.0.1:55432/enrich
set -euo pipefail
PGBIN="${PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
PGDATA="${PGDATA:-/var/tmp/leadengine-pg}"
PGPORT="${PGPORT:-55432}"
RUNAS=()
if [ "$(id -u)" = "0" ]; then RUNAS=(su postgres -c); fi

run() { # run a shell command string as the unprivileged pg user
  if [ ${#RUNAS[@]} -gt 0 ]; then su postgres -s /bin/bash -c "$1"; else bash -c "$1"; fi
}

cmd="${1:-start}"
case "$cmd" in
  start)
    if [ ! -d "$PGDATA/base" ]; then
      mkdir -p "$PGDATA"; [ "$(id -u)" = "0" ] && chown postgres "$PGDATA"
      run "$PGBIN/initdb -D '$PGDATA' -A trust -U enrich --no-sync >/dev/null"
    fi
    if ! run "$PGBIN/pg_ctl -D '$PGDATA' status >/dev/null 2>&1"; then
      run "$PGBIN/pg_ctl -D '$PGDATA' -o '-p $PGPORT -c listen_addresses=127.0.0.1 -c fsync=off -c unix_socket_directories=/tmp' -l '$PGDATA/server.log' -w start >/dev/null"
    fi
    for db in enrich enrich_test; do   # enrich: app/benchmarks; enrich_test: pytest (tests TRUNCATE everything)
      run "$PGBIN/psql -h 127.0.0.1 -p $PGPORT -U enrich -d postgres -tc \"select 1 from pg_database where datname='$db'\" | grep -q 1 || $PGBIN/createdb -h 127.0.0.1 -p $PGPORT -U enrich $db"
    done
    echo "postgresql://enrich@127.0.0.1:$PGPORT/enrich"
    ;;
  stop) run "$PGBIN/pg_ctl -D '$PGDATA' -m fast stop" ;;
  status) run "$PGBIN/pg_ctl -D '$PGDATA' status" ;;
  *) echo "usage: $0 [start|stop|status]"; exit 2 ;;
esac
