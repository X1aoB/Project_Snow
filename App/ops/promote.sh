#!/bin/sh
set -eu
umask 077

# Promote a previously staged colour after private acceptance. The candidate
# must already be running and passing its direct health/smoke checks.
colour="${1:?blue or green required}"
expected_sha="${2:-}"
case "$colour" in blue|green) ;; *) exit 64 ;; esac

runtime_root="/srv/project-snow/runtime"
colour_env="$runtime_root/colours/$colour.compose.env"
colour_release_root="/srv/project-snow/releases/colours"
colour_marker="$colour_release_root/$colour"
colour_manifest="$colour_release_root/$colour-manifest.json"
current_env="${PROJECT_SNOW_COMPOSE_ENV:-$runtime_root/compose.env}"
current_manifest="/srv/project-snow/releases/current-manifest.json"
active_file="/srv/project-snow/releases/active-colour"
service="public-api-$colour"

if [ ! -r "$colour_env" ] || [ ! -r "$colour_marker" ]; then
  echo "No staged release exists for $colour." >&2
  exit 66
fi
read -r marker_colour marker_sha marker_app_image marker_embedding_image < "$colour_marker"
[ "$marker_colour" = "$colour" ] || { echo 'Staged marker colour mismatch.' >&2; exit 67; }
case "$marker_sha" in [0-9a-f][0-9a-f]*) ;; *) echo 'Invalid staged commit SHA.' >&2; exit 67 ;; esac
case "$marker_app_image:$marker_embedding_image" in *@sha256:*@sha256:*) ;; *) echo 'Staged images are not immutable digests.' >&2; exit 67 ;; esac
if [ -n "$expected_sha" ] && [ "$expected_sha" != "$marker_sha" ]; then
  echo "Staged SHA $marker_sha does not match requested SHA $expected_sha." >&2
  exit 68
fi

active_colour=""
if [ -r "$active_file" ]; then
  active_colour="$(tr -d '[:space:]' < "$active_file")"
  case "$active_colour" in blue|green|'') ;; *) echo 'Invalid active-colour marker.' >&2; exit 69 ;; esac
fi
if [ "$active_colour" = "$colour" ]; then
  echo "$colour is already the active colour; refreshing the promotion markers."
fi

compose() {
  docker compose --env-file "$colour_env" -f compose.prod.yml --profile "$colour" "$@"
}

ready=0
attempt=0
while [ "$attempt" -lt 15 ]; do
  if compose exec -T "$service" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/public/v1/health/ready', timeout=5).read()" >/dev/null 2>&1; then
    ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo "Staged $service is not ready; traffic was not changed." >&2
  exit 70
fi
if ! compose exec -T "$service" python /app/public_smoke.py http://127.0.0.1:8000; then
  echo "Staged $service failed its direct smoke test; traffic was not changed." >&2
  exit 71
fi

previous_colour="$active_colour"
previous_env="$current_env"
if [ -n "$previous_colour" ] && [ -r "$runtime_root/colours/$previous_colour.compose.env" ]; then
  previous_env="$runtime_root/colours/$previous_colour.compose.env"
fi

switch_caddy() {
  caddy_env="$1"
  target_colour="$2"
  SNOW_UPSTREAM="public-api-$target_colour:8000" \
    docker compose --env-file "$caddy_env" -f compose.prod.yml --profile "$target_colour" \
    up -d --no-deps --force-recreate caddy
}

# Prepare all marker files before changing traffic. The final renames are
# same-filesystem operations and leave the previous colour available.
state_tmp="$(mktemp "$runtime_root/compose.env.promote.XXXXXX")"
active_tmp="$(mktemp "$active_file.promote.XXXXXX")"
manifest_tmp="$(mktemp "$current_manifest.promote.XXXXXX")"
cleanup() {
  rm -f "${state_tmp:-}" "${active_tmp:-}" "${manifest_tmp:-}"
}
trap cleanup EXIT HUP INT TERM
cp "$colour_env" "$state_tmp"
chmod 0600 "$state_tmp"
printf '%s\n' "$colour" > "$active_tmp"
chmod 0600 "$active_tmp"
if [ -r "$colour_manifest" ]; then
  cp "$colour_manifest" "$manifest_tmp"
  chmod 0600 "$manifest_tmp"
else
  rm -f "$manifest_tmp"
  manifest_tmp=""
fi

switch_caddy "$colour_env" "$colour"
if ! compose exec -T "$service" python /app/public_smoke.py http://caddy:8080; then
  echo 'Post-switch smoke failed; restoring the previous Caddy upstream.' >&2
  if [ -n "$previous_colour" ] && [ -r "$previous_env" ]; then
    switch_caddy "$previous_env" "$previous_colour" || true
  fi
  exit 72
fi

mv -f "$state_tmp" "$current_env"
state_tmp=""
mv -f "$active_tmp" "$active_file"
active_tmp=""
if [ -n "$manifest_tmp" ]; then
  mv -f "$manifest_tmp" "$current_manifest"
  manifest_tmp=""
fi
printf '%s\n' "$colour $marker_sha $marker_app_image $marker_embedding_image" > /srv/project-snow/releases/current
chmod 0600 /srv/project-snow/releases/current

printf '%s\n' "Promoted $colour $marker_sha. Cloudflare Access and MyWebsite settings were not changed."
