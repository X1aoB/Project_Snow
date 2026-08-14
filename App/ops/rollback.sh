#!/bin/sh
set -eu
colour="${1:?blue or green required}"
case "$colour" in blue|green) ;; *) exit 64 ;; esac
compose_env="${PROJECT_SNOW_COMPOSE_ENV:-/srv/project-snow/runtime/compose.env}"
export SNOW_UPSTREAM="public-api-$colour:8000"
docker compose --env-file "$compose_env" -f compose.prod.yml --profile "$colour" up -d --no-deps --force-recreate caddy
unset SNOW_UPSTREAM
printf '%s\n' "$colour" > /srv/project-snow/releases/active-colour
