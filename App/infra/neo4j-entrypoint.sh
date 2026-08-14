#!/bin/bash
set -euo pipefail

source_file=/run/host-secrets/neo4j_auth
runtime_dir=/run/project-snow-secrets

if [[ ! -f "$source_file" ]]; then
  echo "Missing Neo4j authentication secret." >&2
  exit 78
fi

# Keep the host secret root-only while giving the Neo4j process its own
# read-only copy inside the container. The official entrypoint drops from
# root to uid 7474 before reading NEO4J_AUTH_FILE.
install -o neo4j -g neo4j -m 0700 -d "$runtime_dir"
install -o neo4j -g neo4j -m 0400 "$source_file" "$runtime_dir/neo4j_auth"
export NEO4J_AUTH_FILE="$runtime_dir/neo4j_auth"

exec tini -g -- /startup/docker-entrypoint.sh "$@"
