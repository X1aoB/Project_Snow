#!/bin/sh
set -eu
umask 077

# Stage a release in the inactive colour. This command deliberately never
# recreates Caddy, cloudflared, or changes the active-colour marker. Traffic
# is promoted only by ops/promote.sh after private acceptance.
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
active_file="/srv/project-snow/releases/active-colour"
colour_env_root="/srv/project-snow/runtime/colours"
colour_release_root="/srv/project-snow/releases/colours"
colour_env="$colour_env_root/$colour.compose.env"
colour_manifest="$colour_release_root/$colour-manifest.json"
colour_marker="$colour_release_root/$colour"

if [ ! -r "$static_env" ]; then
  echo "Missing readable static image environment: $static_env" >&2
  exit 66
fi

active_colour=""
if [ -r "$active_file" ]; then
  active_colour="$(tr -d '[:space:]' < "$active_file")"
  case "$active_colour" in blue|green|'') ;; *) echo 'Invalid active-colour marker.' >&2; exit 67 ;; esac
fi
if [ "$active_colour" = "$colour" ]; then
  echo "Refusing to stage active colour '$colour'; choose the inactive colour." >&2
  exit 70
fi

install -d -m 0700 "$colour_env_root" "$colour_release_root"
# Bootstrap the active colour's durable environment before the first staged
# release. This preserves a usable rollback target on older installations.
if [ -n "$active_colour" ] && [ ! -r "/srv/project-snow/runtime/colours/$active_colour.compose.env" ] && [ -r "$current_env" ]; then
  cp "$current_env" "/srv/project-snow/runtime/colours/$active_colour.compose.env"
  chmod 0600 "/srv/project-snow/runtime/colours/$active_colour.compose.env"
fi

candidate_env="$(mktemp "$colour_env.candidate.XXXXXX")"
candidate_manifest="$(mktemp "$colour_manifest.candidate.XXXXXX")"
candidate_marker="$(mktemp "$colour_marker.candidate.XXXXXX")"
cleanup() {
  [ -z "${candidate_env:-}" ] || rm -f "$candidate_env"
  [ -z "${candidate_manifest:-}" ] || rm -f "$candidate_manifest"
  [ -z "${candidate_marker:-}" ] || rm -f "$candidate_marker"
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
candidate_media_version=""
if [ -n "$release_manifest" ]; then
  candidate_data_version="$(jq -r '.data_version // empty' "$release_manifest")"
  case "$candidate_data_version" in '') echo 'Release manifest has no data version.' >&2; exit 71 ;; esac
  candidate_media_version="$(jq -r '.media_version // empty' "$release_manifest")"
  case "$candidate_media_version" in '') echo 'Release manifest has no media version.' >&2; exit 71 ;; esac
  active_media_version=""
  if [ -r /srv/project-snow/media/current/manifest.json ]; then
    active_media_version="$(jq -r '.media_version // empty' /srv/project-snow/media/current/manifest.json)"
  fi
  if [ "$candidate_media_version" != "$active_media_version" ]; then
    echo "Media $candidate_media_version must be downloaded, verified and promoted before application staging (active: ${active_media_version:-none})." >&2
    exit 72
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

service="public-api-$colour"
compose pull "$service"
if [ "$embedding_changed" -eq 1 ]; then
  compose pull embedding
else
  echo 'Embedding digest unchanged; skipping image pull.'
fi
compose run --rm --no-deps "$service" \
  python -m backend.snow_app.data_loader --verify-only
compose run --rm "$service" alembic upgrade head
compose up -d postgres qdrant neo4j embedding egress-proxy
if [ "$data_changed" -eq 1 ]; then
  compose run --rm --no-deps "$service" python -m backend.snow_app.data_loader
else
  echo "Data version $candidate_data_version unchanged; skipping Qdrant and Neo4j load."
fi
# Only the inactive API is started. Caddy and cloudflared keep serving the
# current colour until promote.sh is explicitly invoked.
compose up -d "$service"
ready=0
attempt=0
while [ "$attempt" -lt 30 ]; do
  if compose exec -T "$service" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/public/v1/health/ready', timeout=5).read()" >/dev/null 2>&1; then
    ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo 'Staged public API did not become ready within 60 seconds.' >&2
  compose logs --tail=100 "$service" >&2 || true
  exit 73
fi
compose exec -T "$service" python /app/public_smoke.py http://127.0.0.1:8000

printf '%s\n' "$colour $sha $PUBLIC_API_IMAGE $EMBEDDING_IMAGE" > "$candidate_marker"
chmod 0600 "$candidate_marker"
if [ -n "$release_manifest" ]; then
  cp "$release_manifest" "$candidate_manifest"
  chmod 0600 "$candidate_manifest"
fi
mv -f "$candidate_env" "$colour_env"
candidate_env=""
if [ -n "$release_manifest" ]; then
  mv -f "$candidate_manifest" "$colour_manifest"
  candidate_manifest=""
fi
mv -f "$candidate_marker" "$colour_marker"
candidate_marker=""

printf '%s\n' "Staged $colour $sha without changing active traffic."
printf '%s\n' "Private acceptance endpoint: http://127.0.0.1:$([ "$colour" = blue ] && echo 18081 || echo 18082)"
printf '%s\n' 'Promote only after acceptance with: ./ops/promote.sh <colour>'
