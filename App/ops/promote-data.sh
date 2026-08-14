#!/bin/sh
set -eu

version="${1:?data version required}"
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
printf '%s\n' "Promoted Project Snow data release: $version"
