#!/bin/sh
set -eu

colour="${1:?blue or green required}"
sha="${2:?main SHA required}"
case "$colour" in blue|green) ;; *) exit 64 ;; esac
: "${PUBLIC_API_IMAGE:?immutable image digest required}"
: "${EMBEDDING_IMAGE:?embedding image required}"

compose="docker compose -f compose.prod.yml --profile $colour"
$compose pull "public-api-$colour"
docker pull "$EMBEDDING_IMAGE"
case "$PUBLIC_API_IMAGE:$EMBEDDING_IMAGE" in *@sha256:*@sha256:*) ;; *) echo 'Immutable image digests are required.' >&2; exit 65 ;; esac
PUBLIC_API_IMAGE="$PUBLIC_API_IMAGE" $compose run --rm "public-api-$colour" alembic upgrade head
PUBLIC_API_IMAGE="$PUBLIC_API_IMAGE" $compose up -d "public-api-$colour" postgres qdrant neo4j embedding caddy cloudflared
docker compose -f compose.prod.yml exec -T "public-api-$colour" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/public/v1/health/ready', timeout=10).read()"

SNOW_UPSTREAM="public-api-$colour:8000" docker compose -f compose.prod.yml up -d --no-deps --force-recreate caddy
docker compose -f compose.prod.yml --profile "$colour" run --rm --no-deps --entrypoint python "public-api-$colour" /app/public_smoke.py http://caddy:8080
printf '%s\n' "$colour $sha $PUBLIC_API_IMAGE $EMBEDDING_IMAGE" > /srv/project-snow/releases/current
printf '%s\n' 'Private acceptance remains behind Cloudflare Access; this script never removes Access or changes MyWebsite.'
