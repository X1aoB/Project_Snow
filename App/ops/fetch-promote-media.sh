#!/bin/sh
set -eu
umask 027

version="${1:?media version required}"
: "${R2_MEDIA_REMOTE:?rclone remote path required, for example r2:bucket/public-content/project-snow/media}"
mode="${2:-promote}"

case "$mode" in
  promote|stage-only) ;;
  *) echo 'Media mode must be promote or stage-only.' >&2; exit 64 ;;
esac

case "$version" in
  *[!0-9A-Za-z._-]*|'') echo 'Unsafe media version.' >&2; exit 64 ;;
esac

media_root=/srv/project-snow/media
release_root="$media_root/releases"
staging_root="$media_root/staging"
target="$release_root/$version"
install -d -m 0755 "$release_root" "$staging_root"
staging="$(mktemp -d "$staging_root/$version.XXXXXX")"
cleanup() {
  case "${staging:-}" in "$staging_root"/*) rm -rf -- "$staging" ;; esac
}
trap cleanup EXIT HUP INT TERM

# Cloudflare R2 with the server's rclone 1.60 build does not implement the
# post-upload HEAD/checksum metadata requests.  The release SHA256SUMS file
# remains the authoritative integrity check after the copy, so use the R2
# compatible read path and verify every file locally below.
rclone copy "$R2_MEDIA_REMOTE/$version" "$staging" \
  --size-only \
  --immutable \
  --s3-no-check-bucket \
  --s3-no-head \
  --s3-disable-checksum \
  --s3-no-system-metadata
test -r "$staging/manifest.json"
test -r "$staging/SHA256SUMS"
manifest_version="$(jq -r '.media_version // empty' "$staging/manifest.json")"
character_count="$(jq -r '.character_count // 0' "$staging/manifest.json")"
test "$manifest_version" = "$version"
test "$character_count" -eq 22
analyst_asset="$(jq -r '.analyst.asset_id // empty' "$staging/manifest.json")"
test "$analyst_asset" = "analyst-default"
analyst_license_status="$(jq -r '.analyst.license_status // empty' "$staging/manifest.json")"
test "$analyst_license_status" = "verified_site_policy_no_page_exception" \
  || test "$analyst_license_status" = "verified_explicit" \
  || test "$analyst_license_status" = "verified" \
  || { echo 'Analyst avatar license review is incomplete.' >&2; exit 66; }
test "$(jq -r '.analyst.license // empty' "$staging/manifest.json")" = "CC BY-NC-SA 4.0"
test "$(jq -r '.analyst.license_version // empty' "$staging/manifest.json")" = "4.0"
test -n "$(jq -r '.analyst.license_source_page // empty' "$staging/manifest.json")"
test -n "$(jq -r '.analyst.license_source_url // empty' "$staging/manifest.json")"
test -n "$(jq -r '.analyst.license_source_revision_id // empty' "$staging/manifest.json")"
(
  cd "$staging"
  sha256sum -c SHA256SUMS
)
test "$(find "$staging/avatars" -maxdepth 1 -type f -name '*-96.webp' | wc -l)" -eq 22
test "$(find "$staging/avatars" -maxdepth 1 -type f -name '*-200.webp' | wc -l)" -eq 22
test "$(find "$staging/analyst" -maxdepth 1 -type f -name '*-96.webp' | wc -l)" -eq 1
test "$(find "$staging/analyst" -maxdepth 1 -type f -name '*-200.webp' | wc -l)" -eq 1

# mktemp creates the staging directory as 0700. The public API intentionally
# runs as the unprivileged `snow` user, so normalize this verified public
# media package before it is moved behind a read-only release path.
find "$staging" -type d -exec chmod 0755 {} +
find "$staging" -type f -exec chmod 0644 {} +

if [ -e "$target" ]; then
  echo "Media release already exists: $target" >&2
  exit 65
fi
mv -- "$staging" "$target"
staging=""

if [ "$mode" = "stage-only" ]; then
  echo "Staged media $version without changing the current symlink."
  exit 0
fi

previous=""
if [ -L "$media_root/current" ]; then
  previous="$(readlink "$media_root/current")"
fi
ln -s "$target" "$media_root/current.next"
mv -Tf "$media_root/current.next" "$media_root/current"
if [ -n "$previous" ] && [ "$previous" != "$target" ]; then
  ln -sfn "$previous" "$media_root/previous"
fi
printf '%s\n' "$version" > "$media_root/current-version"
echo "Promoted media $version; previous=${previous:-none}"
