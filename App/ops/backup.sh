#!/bin/sh
set -eu
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
work="/srv/project-snow/backups/staging/$stamp"
compose_env="${PROJECT_SNOW_COMPOSE_ENV:-/srv/project-snow/runtime/compose.env}"
compose() {
  docker compose --env-file "$compose_env" -f /srv/project-snow/app/compose.prod.yml "$@"
}
mkdir -p "$work"
compose exec -T postgres pg_dump -U project_snow -d project_snow -Fc > "$work/postgres.dump"
cp /srv/project-snow/releases/current "$work/current-release"
restic backup "$work" --tag project-snow-production
restic forget --keep-daily 7 --prune
rm -f "$work/postgres.dump" "$work/current-release"
rmdir "$work"
