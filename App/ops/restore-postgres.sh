#!/bin/sh
set -eu
dump="${1:?path to verified pg_dump required}"
checksum="${2:?verified SHA-256 from recovery.json required}"
exec /usr/bin/python3 /usr/local/libexec/project-snow/maintenance.py restore-postgres \
  --dump "$dump" --sha256 "$checksum"
