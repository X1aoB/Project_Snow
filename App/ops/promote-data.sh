#!/bin/sh
set -eu

version="${1:?data version required}"
activation="${2:-}"
case "$activation" in
  ''|--activate) ;;
  *) echo 'Usage: promote-data.sh <version> [--activate]' >&2; exit 64 ;;
esac
data_root=/srv/project-snow/data
releases_root="$data_root/releases"
release_root="$releases_root/$version"
resolved_release="$(readlink -f "$release_root")"
case "$resolved_release" in
  "$releases_root"/*) ;;
  *) echo 'Release path escapes the production data root.' >&2; exit 65 ;;
esac

python3 /srv/project-snow/app/scripts/verify_data_release.py \
  "$resolved_release" --expected-version "$version"

if [ "$activation" = "--activate" ]; then
  active_file=/srv/project-snow/releases/active-colour
  [ -r "$active_file" ] || { echo 'A readable active-colour marker is required.' >&2; exit 66; }
  active_colour="$(cat "$active_file")"
  case "$active_colour" in blue|green) ;; *) echo 'Invalid active-colour marker.' >&2; exit 66 ;; esac
  colour_env="/srv/project-snow/runtime/colours/$active_colour.compose.env"
  [ -r "$colour_env" ] || { echo 'The active colour environment is missing.' >&2; exit 66; }
  pinned_root="$(sed -n 's/^PUBLIC_DATA_ROOT=//p' "$colour_env" | tail -n 1)"
  [ "$pinned_root" = "$resolved_release" ] || {
    echo 'Manual pointer activation is allowed only for the active colour data release.' >&2
    exit 66
  }
  app_root="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
  docker compose --env-file "$colour_env" -f "$app_root/compose.prod.yml" \
    --profile "$active_colour" run --rm --no-deps "public-api-$active_colour" \
    python -m backend.snow_app.data_loader \
      --release-root "$resolved_release" --activate
fi

current_link="$data_root/current"
previous_link="$data_root/previous"
temporary_current="$data_root/.current.$$"
temporary_previous="$data_root/.previous.$$"
cleanup() {
  rm -f "$temporary_current" "$temporary_previous"
}
trap cleanup EXIT HUP INT TERM

if [ -L "$current_link" ]; then
  old_release="$(readlink -f "$current_link")"
  ln -s "$old_release" "$temporary_previous"
  mv -Tf "$temporary_previous" "$previous_link"
fi
ln -s "$resolved_release" "$temporary_current"
mv -Tf "$temporary_current" "$current_link"
trap - EXIT HUP INT TERM
printf '%s\n' "Promoted Project Snow data release: $version; legacy pointers activated=$([ "$activation" = "--activate" ] && echo yes || echo no)"
