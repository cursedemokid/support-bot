#!/usr/bin/env bash
# Локальная база для разработки и тестов. Прод не трогает.
#
#   ./scripts/dev-db.sh up      поднять
#   ./scripts/dev-db.sh down    погасить
#   ./scripts/dev-db.sh psql    консоль
set -euo pipefail

NAME=support-pg
PORT=${SUPPORT_PG_PORT:-55441}
PASSWORD=test

case "${1:-up}" in
  up)
    docker run -d --name "$NAME" \
      -e POSTGRES_PASSWORD="$PASSWORD" \
      -p "127.0.0.1:${PORT}:5432" \
      postgres:16-alpine >/dev/null
    until docker exec "$NAME" pg_isready -U postgres >/dev/null 2>&1; do sleep 0.5; done
    docker exec "$NAME" psql -U postgres -c 'create database support_test' >/dev/null
    echo "готово: postgresql+asyncpg://postgres:${PASSWORD}@127.0.0.1:${PORT}/postgres"
    ;;
  down)
    docker rm -f "$NAME" >/dev/null
    ;;
  psql)
    docker exec -it "$NAME" psql -U postgres -d postgres
    ;;
  *)
    echo "использование: $0 {up|down|psql}" >&2
    exit 2
    ;;
esac
