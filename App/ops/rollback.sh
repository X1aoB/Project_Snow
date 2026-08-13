#!/bin/sh
set -eu
colour="${1:?blue or green required}"
case "$colour" in blue|green) ;; *) exit 64 ;; esac
SNOW_UPSTREAM="public-api-$colour:8000" docker compose -f compose.prod.yml up -d --no-deps --force-recreate caddy
printf '%s\n' "$colour" > /srv/project-snow/releases/active-colour
