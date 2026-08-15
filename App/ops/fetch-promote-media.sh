#!/bin/sh
set -eu
umask 027

version="${1:?media version required}"
: "${R2_MEDIA_REMOTE:?rclone remote path required, for example r2:bucket/public-content/project-snow/media}"

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

rclone copy "$R2_MEDIA_REMOTE/$version" "$staging" --checksum --immutable
test -r "$staging/manifest.json"
test -r "$staging/SHA256SUMS"
manifest_version="$(jq -r '.media_version // empty' "$staging/manifest.json")"
character_count="$(jq -r '.character_count // 0' "$staging/manifest.json")"
test "$manifest_version" = "$version"
test "$character_count" -eq 22
(
  cd "$staging"
  sha256sum -c SHA256SUMS
)
test "$(find "$staging/avatars" -maxdepth 1 -type f -name '*-96.webp' | wc -l)" -eq 22
test "$(find "$staging/avatars" -maxdepth 1 -type f -name '*-200.webp' | wc -l)" -eq 22

# mktemp creates the staging directory as 0700. The public API intentionally
# runs as the unprivileged `snow` user, so normalize this verified public
# media package before it is moved behind the read-only current symlink.
find "$staging" -type d -exec chmod 0755 {} +
find "$staging" -type f -exec chmod 0644 {} +

if [ -e "$target" ]; then
  echo "Media release already exists: $target" >&2
  exit 65
fi
mv -- "$staging" "$target"
staging=""
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
