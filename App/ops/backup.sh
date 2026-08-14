#!/bin/sh
set -eu
umask 077
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
work="/srv/project-snow/backups/staging/$stamp"
mkdir -p "$work"
docker compose -f /srv/project-snow/app/compose.prod.yml exec -T postgres pg_dump -U project_snow -d project_snow -Fc > "$work/postgres.dump"
cp /srv/project-snow/releases/current "$work/current-release"
restic backup "$work" --tag project-snow-production
restic forget --keep-daily 7 --prune
rm -f "$work/postgres.dump" "$work/current-release"
rmdir "$work"
