#!/bin/sh
set -eu
umask 077

colour="${1:?blue or green required}"
sha="${2:?main SHA required}"
case "$colour" in blue|green) ;; *) exit 64 ;; esac
: "${PUBLIC_API_IMAGE:?immutable image digest required}"
: "${EMBEDDING_IMAGE:?embedding image required}"

case "$PUBLIC_API_IMAGE:$EMBEDDING_IMAGE" in *@sha256:*@sha256:*) ;; *) echo 'Immutable image digests are required.' >&2; exit 65 ;; esac

static_env="${PROJECT_SNOW_IMAGE_ENV:-/etc/project-snow/images.env}"
current_env="${PROJECT_SNOW_COMPOSE_ENV:-/srv/project-snow/runtime/compose.env}"
if [ ! -r "$static_env" ]; then
  echo "Missing readable static image environment: $static_env" >&2
  exit 66
fi
candidate_env="$(mktemp "${current_env}.candidate.XXXXXX")"
cleanup() {
  [ -z "${candidate_env:-}" ] || rm -f "$candidate_env"
}
trap cleanup EXIT HUP INT TERM
cat "$static_env" > "$candidate_env"
printf '\nPUBLIC_API_IMAGE=%s\nEMBEDDING_IMAGE=%s\n' "$PUBLIC_API_IMAGE" "$EMBEDDING_IMAGE" >> "$candidate_env"
chmod 0600 "$candidate_env"

compose() {
  docker compose --env-file "$candidate_env" -f compose.prod.yml --profile "$colour" "$@"
}

compose pull "public-api-$colour" embedding
compose run --rm "public-api-$colour" alembic upgrade head
compose up -d "public-api-$colour" postgres qdrant neo4j embedding egress-proxy caddy cloudflared
compose exec -T "public-api-$colour" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/public/v1/health/ready', timeout=10).read()"

export SNOW_UPSTREAM="public-api-$colour:8000"
compose up -d --no-deps --force-recreate caddy
compose run --rm --no-deps --entrypoint python "public-api-$colour" /app/public_smoke.py http://caddy:8080
unset SNOW_UPSTREAM

mv -f "$candidate_env" "$current_env"
candidate_env=""
printf '%s\n' "$colour $sha $PUBLIC_API_IMAGE $EMBEDDING_IMAGE" > /srv/project-snow/releases/current
printf '%s\n' 'Private acceptance remains behind Cloudflare Access; this script never removes Access or changes MyWebsite.'
