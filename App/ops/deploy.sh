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
release_manifest="${PROJECT_SNOW_RELEASE_MANIFEST:-}"
current_manifest="/srv/project-snow/releases/current-manifest.json"
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

embedding_changed=1
if [ -r /srv/project-snow/releases/current ]; then
  current_embedding="$(awk '{print $4}' /srv/project-snow/releases/current)"
  if [ "$current_embedding" = "$EMBEDDING_IMAGE" ]; then
    embedding_changed=0
  fi
fi

data_changed=1
candidate_data_version=""
if [ -n "$release_manifest" ]; then
  candidate_data_version="$(jq -r '.data_version // empty' "$release_manifest")"
  case "$candidate_data_version" in '') echo 'Release manifest has no data version.' >&2; exit 67 ;; esac
  candidate_media_version="$(jq -r '.media_version // empty' "$release_manifest")"
  case "$candidate_media_version" in '') echo 'Release manifest has no media version.' >&2; exit 67 ;; esac
  active_media_version=""
  if [ -r /srv/project-snow/media/current/manifest.json ]; then
    active_media_version="$(jq -r '.media_version // empty' /srv/project-snow/media/current/manifest.json)"
  fi
  if [ "$candidate_media_version" != "$active_media_version" ]; then
    echo "Media $candidate_media_version must be downloaded, verified and promoted before application deployment (active: ${active_media_version:-none})." >&2
    exit 68
  fi
  current_data_version=""
  if [ -r "$current_manifest" ]; then
    current_data_version="$(jq -r '.data_version // empty' "$current_manifest")"
  elif [ -r /srv/project-snow/data/current/manifest.json ]; then
    current_data_version="$(jq -r '.data_version // empty' /srv/project-snow/data/current/manifest.json)"
  fi
  if [ "$candidate_data_version" = "$current_data_version" ]; then
    data_changed=0
  fi
fi

compose pull "public-api-$colour"
if [ "$embedding_changed" -eq 1 ]; then
  compose pull embedding
else
  echo 'Embedding digest unchanged; skipping image pull.'
fi
compose run --rm --no-deps "public-api-$colour" \
  python -m backend.snow_app.data_loader --verify-only
compose run --rm "public-api-$colour" alembic upgrade head
compose up -d postgres qdrant neo4j embedding egress-proxy
if [ "$data_changed" -eq 1 ]; then
  compose run --rm --no-deps "public-api-$colour" python -m backend.snow_app.data_loader
else
  echo "Data version $candidate_data_version unchanged; skipping Qdrant and Neo4j load."
fi
compose up -d "public-api-$colour" caddy cloudflared
ready=0
attempt=0
while [ "$attempt" -lt 30 ]; do
  if compose exec -T "public-api-$colour" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/public/v1/health/ready', timeout=5).read()" >/dev/null 2>&1; then
    ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo 'Public API did not become ready within 60 seconds.' >&2
  compose logs --tail=100 "public-api-$colour" caddy >&2 || true
  exit 69
fi

export SNOW_UPSTREAM="public-api-$colour:8000"
compose up -d --no-deps --force-recreate caddy
compose run --rm --no-deps --entrypoint python "public-api-$colour" /app/public_smoke.py http://caddy:8080
unset SNOW_UPSTREAM

mv -f "$candidate_env" "$current_env"
candidate_env=""
printf '%s\n' "$colour $sha $PUBLIC_API_IMAGE $EMBEDDING_IMAGE" > /srv/project-snow/releases/current
if [ -n "$release_manifest" ]; then
  cp "$release_manifest" "$current_manifest"
  chmod 0600 "$current_manifest"
fi
printf '%s\n' 'Private acceptance remains behind Cloudflare Access; this script never removes Access or changes MyWebsite.'
