#!/bin/sh
set -eu
umask 077

source_dir="${PROJECT_SNOW_HOST_SECRETS:-/run/host-secrets}"
runtime_dir="${PROJECT_SNOW_RUNTIME_SECRETS:-/run/project-snow-secrets}"

install -o snow -g snow -m 0700 -d "$runtime_dir"

for name in \
  public_database_url \
  turnstile_secret \
  public_credential_key \
  public_state_hmac_key \
  public_ip_hmac_key \
  public_qq_key \
  public_admin_token \
  neo4j_password \
  qdrant_api_key
do
  source_file="$source_dir/$name"
  if [ -e "$source_file" ]; then
    if [ ! -f "$source_file" ]; then
      echo "Configured secret source is not a regular file: $name" >&2
      exit 78
    fi
    install -o snow -g snow -m 0400 "$source_file" "$runtime_dir/$name"
  fi
done

exec gosu snow "$@"
