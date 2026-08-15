#!/bin/sh
set -eu

# Rollback is the same guarded operation as a promotion: it validates the
# durable colour release, switches Caddy, and only then updates markers.
colour="${1:?blue or green required}"
expected_sha="${2:-}"
case "$colour" in blue|green) ;; *) exit 64 ;; esac
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec "$script_dir/promote.sh" "$colour" "$expected_sha"
