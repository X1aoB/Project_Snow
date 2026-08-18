#!/bin/sh
set -eu
umask 027

version="${1:?sticker media version required}"
: "${R2_STICKER_REMOTE:?rclone remote path required, for example r2:bucket/private-content/project-snow/stickers}"
mode="${2:-promote}"

case "$mode" in
  promote|stage-only) ;;
  *) echo 'Sticker media mode must be promote or stage-only.' >&2; exit 64 ;;
esac

case "$version" in
  *[!0-9A-Za-z._-]*|'') echo 'Unsafe sticker media version.' >&2; exit 64 ;;
esac

media_root=/srv/project-snow/media/stickers
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
rclone copy "$R2_STICKER_REMOTE/$version" "$staging" \
  --size-only \
  --immutable \
  --s3-no-check-bucket \
  --s3-no-head \
  --s3-disable-checksum \
  --s3-no-system-metadata
test -r "$staging/manifest.json"
test -r "$staging/SHA256SUMS"
manifest_version="$(jq -r '.media_version // empty' "$staging/manifest.json")"
sticker_count="$(jq -r '.count // 0' "$staging/manifest.json")"
test "$manifest_version" = "$version"
test "$sticker_count" -eq 363
jq -e '
  .schema_version == "project-snow-sticker-1" and
  .private_candidate == false and
  .license_review_status == "verified_public_release" and
  ((.license_policy // "") | length > 0) and
  (.stickers | length == 363) and
  all(.stickers[];
    ((.asset_id // "") | test("^[A-Za-z0-9][A-Za-z0-9_-]{5,63}$")) and
    (.character_ids | type == "array") and
    (all(.character_ids[]; test("^[0-9a-f]{12}$"))) and
    (.emotion_tags | type == "array" and length > 0) and
    (.candidate_scope == (if (.character_ids | length) > 0 then "character" else "generic" end)) and
    ((.file_page_url // "") | startswith("https://")) and
    ((.source_page_url // "") | startswith("https://")) and
    ((.source_image_url // "") | startswith("https://")) and
    ((.license // "") | contains("CC BY-NC-SA 4.0")) and
    .license_version == "4.0" and
    .license_status == "verified" and
    ((.attribution // "") | length > 0) and
    .content_hash == .sha256
  )
' "$staging/manifest.json" >/dev/null
(
  cd "$staging"
  sha256sum -c SHA256SUMS
)
test "$(find "$staging/stickers" -maxdepth 1 -type f | wc -l)" -eq 363
test "$(find "$staging/thumbnails" -maxdepth 1 -type f -name '*.webp' | wc -l)" -eq 363

# The API container runs as the unprivileged snow user and mounts this tree
# read-only, so normalize permissions only after all hashes have passed.
find "$staging" -type d -exec chmod 0755 {} +
find "$staging" -type f -exec chmod 0644 {} +

if [ -e "$target" ]; then
  echo "Sticker media release already exists: $target" >&2
  exit 65
fi
mv -- "$staging" "$target"
staging=""

if [ "$mode" = "stage-only" ]; then
  echo "Staged sticker media $version without changing the current symlink."
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
echo "Promoted sticker media $version; previous=${previous:-none}"
