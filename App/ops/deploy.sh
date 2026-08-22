#!/bin/sh
set -eu
umask 077

# Stage a release in the inactive colour. This command deliberately never
# recreates Caddy, either edge ingress, or changes the active-colour marker. Traffic
# is promoted only by ops/promote.sh after private acceptance.
colour="${1:?blue or green required}"
sha="${2:?main SHA required}"
case "$colour" in blue|green) ;; *) exit 64 ;; esac
: "${PUBLIC_API_IMAGE:?immutable image digest required}"
: "${EMBEDDING_IMAGE:?embedding image required}"

is_immutable_image() {
  printf '%s\n' "$1" | grep -Eq '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'
}
if ! is_immutable_image "$PUBLIC_API_IMAGE" || ! is_immutable_image "$EMBEDDING_IMAGE"; then
  echo 'Immutable image digests are required.' >&2
  exit 65
fi

static_env="${PROJECT_SNOW_IMAGE_ENV:-/etc/project-snow/images.env}"
public_env_source="${PROJECT_SNOW_PUBLIC_ENV:-/etc/project-snow/public.env}"
public_env_root="/srv/project-snow/runtime"
current_env="${PROJECT_SNOW_COMPOSE_ENV:-/srv/project-snow/runtime/compose.env}"
release_manifest="${PROJECT_SNOW_RELEASE_MANIFEST:-}"
current_marker="/srv/project-snow/releases/current"
current_manifest="/srv/project-snow/releases/current-manifest.json"
active_file="/srv/project-snow/releases/active-colour"
colour_env_root="/srv/project-snow/runtime/colours"
colour_release_root="/srv/project-snow/releases/colours"
configuration_release_root="/srv/project-snow/releases/configurations"
colour_env="$colour_env_root/$colour.compose.env"
colour_manifest="$colour_release_root/$colour-manifest.json"
colour_marker="$colour_release_root/$colour"
colour_config_binding="$colour_release_root/$colour-config.json"

base_configuration_paths="compose.prod.yml
infra/Caddyfile
infra/egress-squid.conf
infra/neo4j-entrypoint.sh
infra/postgres/postgresql.conf
infra/public-api.Dockerfile
requirements-public.txt"
direct_origin_configuration_paths="infra/OriginEdge.Caddyfile
scripts/cloudflare_origin_firewall.py
ops/project-snow-origin-firewall.service
ops/project-snow-origin-firewall.timer"
configuration_paths="$base_configuration_paths
$direct_origin_configuration_paths"

configuration_paths_for_root() {
  paths_root="$1"
  printf '%s\n' "$base_configuration_paths"
  direct_origin_path_count=0
  for direct_origin_path in $direct_origin_configuration_paths; do
    if [ -e "$paths_root/$direct_origin_path" ] || [ -L "$paths_root/$direct_origin_path" ]; then
      [ -f "$paths_root/$direct_origin_path" ] && [ ! -L "$paths_root/$direct_origin_path" ] || {
        echo "Optional direct-origin configuration is not a regular file: $paths_root/$direct_origin_path" >&2
        return 1
      }
      direct_origin_path_count=$((direct_origin_path_count + 1))
    fi
  done
  case "$direct_origin_path_count" in
    0) ;;
    4) printf '%s\n' "$direct_origin_configuration_paths" ;;
    *) echo "Direct-origin configuration snapshot is incomplete under $paths_root." >&2; return 1 ;;
  esac
}

create_config_binding() {
  binding_colour="$1"
  binding_sha="$2"
  binding_root="$3"
  binding_output="$4"
  binding_paths="$(configuration_paths_for_root "$binding_root")" || return 1
  binding_hashes='{}'
  for binding_path in $binding_paths; do
    [ -f "$binding_root/$binding_path" ] && [ ! -L "$binding_root/$binding_path" ] || {
      echo "Configuration snapshot is missing a regular $binding_path file." >&2
      return 1
    }
    binding_digest="$(sha256sum "$binding_root/$binding_path" | awk '{print $1}')"
    binding_hashes="$(printf '%s' "$binding_hashes" | jq -c \
      --arg path "$binding_path" --arg digest "$binding_digest" \
      '. + {($path): $digest}')"
  done
  jq -n \
    --arg schema_version project-snow-config-snapshot-1 \
    --arg colour "$binding_colour" \
    --arg commit_sha "$binding_sha" \
    --arg root "$binding_root" \
    --argjson configuration_sha256 "$binding_hashes" \
    '{schema_version: $schema_version, colour: $colour, commit_sha: $commit_sha,
      root: $root, configuration_sha256: $configuration_sha256}' > "$binding_output"
  chmod 0600 "$binding_output"
}

validate_config_binding() {
  binding_file="$1"
  binding_colour="$2"
  binding_sha="$3"
  expected_root="$configuration_release_root/$binding_sha"
  [ -r "$binding_file" ] && [ ! -L "$binding_file" ] || {
    echo "Missing readable configuration snapshot binding for $binding_colour." >&2
    return 1
  }
  [ "$(jq -r '.schema_version // empty' "$binding_file")" = project-snow-config-snapshot-1 ] &&
    [ "$(jq -r '.colour // empty' "$binding_file")" = "$binding_colour" ] &&
    [ "$(jq -r '.commit_sha // empty' "$binding_file")" = "$binding_sha" ] &&
    [ "$(jq -r '.root // empty' "$binding_file")" = "$expected_root" ] || {
      echo "Configuration snapshot binding identity mismatch for $binding_colour." >&2
      return 1
    }
  [ -d "$expected_root" ] && [ ! -L "$expected_root" ] || {
    echo "Configuration snapshot root is missing or mutable for $binding_colour." >&2
    return 1
  }
  binding_paths="$(configuration_paths_for_root "$expected_root")" || return 1
  binding_expected_count="$(printf '%s\n' "$binding_paths" | awk 'NF { count += 1 } END { print count + 0 }')"
  binding_count="$(jq -r '.configuration_sha256 | if type == "object" then length else 0 end' "$binding_file")"
  [ "$binding_count" -eq "$binding_expected_count" ] || {
    echo "Configuration snapshot binding has an unexpected file count for $binding_colour." >&2
    return 1
  }
  for binding_path in $binding_paths; do
    expected_digest="$(jq -r --arg path "$binding_path" '.configuration_sha256[$path] // empty' "$binding_file")"
    printf '%s\n' "$expected_digest" | grep -Eq '^[0-9a-f]{64}$' || {
      echo "Configuration snapshot has no valid hash for $binding_path." >&2
      return 1
    }
    [ -f "$expected_root/$binding_path" ] && [ ! -L "$expected_root/$binding_path" ] || {
      echo "Configuration snapshot file is missing or mutable: $binding_path." >&2
      return 1
    }
    actual_digest="$(sha256sum "$expected_root/$binding_path" | awk '{print $1}')"
    [ "$actual_digest" = "$expected_digest" ] || {
      echo "Configuration snapshot hash mismatch for $binding_path." >&2
      return 1
    }
  done
}

verify_snapshot_against_manifest() {
  snapshot_root="$1"
  snapshot_manifest="$2"
  snapshot_paths="$(configuration_paths_for_root "$snapshot_root")" || return 1
  snapshot_expected_count="$(printf '%s\n' "$snapshot_paths" | awk 'NF { count += 1 } END { print count + 0 }')"
  snapshot_manifest_count="$(jq -r '.configuration_sha256 | if type == "object" then length else 0 end' "$snapshot_manifest")"
  [ "$snapshot_manifest_count" -eq "$snapshot_expected_count" ] || return 1
  for snapshot_path in $snapshot_paths; do
    expected_digest="$(jq -r --arg path "$snapshot_path" '.configuration_sha256[$path] // empty' "$snapshot_manifest")"
    printf '%s\n' "$expected_digest" | grep -Eq '^[0-9a-f]{64}$' || return 1
    [ -f "$snapshot_root/$snapshot_path" ] && [ ! -L "$snapshot_root/$snapshot_path" ] || return 1
    actual_digest="$(sha256sum "$snapshot_root/$snapshot_path" | awk '{print $1}')"
    [ "$actual_digest" = "$expected_digest" ] || return 1
  done
}

install_direct_origin_firewall() {
  firewall_config_root="$1"
  [ -f "$firewall_config_root/infra/OriginEdge.Caddyfile" ] || return 0
  firewall_source="$firewall_config_root/scripts/cloudflare_origin_firewall.py"
  firewall_binary=/usr/local/sbin/project-snow-origin-firewall
  systemd_root=/etc/systemd/system
  for protected_firewall_path in \
    "$firewall_binary" \
    "$systemd_root/project-snow-origin-firewall.service" \
    "$systemd_root/project-snow-origin-firewall.timer"; do
    [ ! -L "$protected_firewall_path" ] &&
      { [ ! -e "$protected_firewall_path" ] || [ -f "$protected_firewall_path" ]; } || {
        echo "Managed origin-firewall path is not a regular file: $protected_firewall_path" >&2
        return 1
      }
    if [ -e "$protected_firewall_path" ] && [ "$(stat -c %h "$protected_firewall_path")" -ne 1 ]; then
      echo "Managed origin-firewall file has multiple hard links: $protected_firewall_path" >&2
      return 1
    fi
  done
  install -o root -g root -m 0755 "$firewall_source" "$firewall_binary"
  install -o root -g root -m 0644 \
    "$firewall_config_root/ops/project-snow-origin-firewall.service" \
    "$systemd_root/project-snow-origin-firewall.service"
  install -o root -g root -m 0644 \
    "$firewall_config_root/ops/project-snow-origin-firewall.timer" \
    "$systemd_root/project-snow-origin-firewall.timer"
  for installed_firewall_spec in \
    "$firewall_binary|0:0:755:1" \
    "$systemd_root/project-snow-origin-firewall.service|0:0:644:1" \
    "$systemd_root/project-snow-origin-firewall.timer|0:0:644:1"; do
    installed_firewall_path="${installed_firewall_spec%%|*}"
    installed_firewall_metadata="${installed_firewall_spec#*|}"
    [ -f "$installed_firewall_path" ] && [ ! -L "$installed_firewall_path" ] &&
      [ "$(stat -c %u:%g:%a:%h "$installed_firewall_path")" = "$installed_firewall_metadata" ] || {
        echo "Installed origin-firewall asset has unsafe ownership, mode, type or link count: $installed_firewall_path" >&2
        return 1
      }
  done
  systemctl daemon-reload
  "$firewall_binary" update
  systemctl enable project-snow-origin-firewall.service project-snow-origin-firewall.timer
  firewall_unit_text="$(systemctl cat --no-pager project-snow-origin-firewall.service)" || {
    echo 'Installed origin-firewall unit could not be read through systemd.' >&2
    return 1
  }
  firewall_unit_exec_start="$(printf '%s\n' "$firewall_unit_text" | sed -n 's/^[[:space:]]*ExecStart=//p')"
  [ "$firewall_unit_exec_start" = '/usr/local/sbin/project-snow-origin-firewall update' ] || {
    echo 'Installed origin-firewall unit has an unexpected or overridden ExecStart.' >&2
    return 1
  }
  systemctl start project-snow-origin-firewall.timer
  systemctl is-enabled --quiet project-snow-origin-firewall.service
  systemctl is-enabled --quiet project-snow-origin-firewall.timer
  systemctl is-active --quiet project-snow-origin-firewall.timer
}

validate_mailer_env() {
  mailer_file="$1"
  [ -f "$mailer_file" ] && [ ! -L "$mailer_file" ] && [ -r "$mailer_file" ] || {
    echo "Missing readable dedicated feedback mailer environment: $mailer_file" >&2
    return 1
  }
  [ "$(stat -c %u "$mailer_file")" = 0 ] && [ "$(stat -c %a "$mailer_file")" = 600 ] || {
    echo 'Feedback mailer environment must be owned by root with mode 0600.' >&2
    return 1
  }
  mailer_seen='|'
  mailer_count=0
  mailer_to=''
  mailer_from=''
  mailer_host=''
  mailer_port=''
  mailer_username=''
  while IFS= read -r mailer_line || [ -n "$mailer_line" ]; do
    case "$mailer_line" in ''|'#'*) continue ;; esac
    case "$mailer_line" in
      *=*) mailer_key="${mailer_line%%=*}"; mailer_value="${mailer_line#*=}" ;;
      *) echo 'Feedback mailer environment contains a malformed line.' >&2; return 1 ;;
    esac
    case "$mailer_key" in
      PUBLIC_FEEDBACK_EMAIL_TO|PUBLIC_FEEDBACK_EMAIL_FROM|PUBLIC_FEEDBACK_SMTP_HOST|PUBLIC_FEEDBACK_SMTP_PORT|PUBLIC_FEEDBACK_SMTP_USERNAME) ;;
      *) echo 'Feedback mailer environment contains a disallowed key.' >&2; return 1 ;;
    esac
    case "$mailer_seen" in
      *"|$mailer_key|"*) echo 'Feedback mailer environment contains a duplicate key.' >&2; return 1 ;;
    esac
    case "$mailer_key" in
      PUBLIC_FEEDBACK_EMAIL_TO) mailer_to="$mailer_value" ;;
      PUBLIC_FEEDBACK_EMAIL_FROM) mailer_from="$mailer_value" ;;
      PUBLIC_FEEDBACK_SMTP_HOST) mailer_host="$mailer_value" ;;
      PUBLIC_FEEDBACK_SMTP_PORT) mailer_port="$mailer_value" ;;
      PUBLIC_FEEDBACK_SMTP_USERNAME) mailer_username="$mailer_value" ;;
    esac
    if [ "$mailer_key" = PUBLIC_FEEDBACK_SMTP_PORT ]; then
      case "$mailer_value" in *[!0-9]*|'') echo 'Feedback SMTP port must be numeric.' >&2; return 1 ;; esac
    fi
    mailer_seen="$mailer_seen$mailer_key|"
    mailer_count=$((mailer_count + 1))
  done < "$mailer_file"
  [ "$mailer_count" -eq 5 ] || {
    echo 'Feedback mailer environment must define exactly the five approved settings.' >&2
    return 1
  }
  [ -n "$mailer_to" ] && [ -n "$mailer_port" ] || {
    echo 'Feedback mailer recipient and port are required.' >&2
    return 1
  }
  if [ -n "$mailer_from$mailer_host$mailer_username" ] &&
     { [ -z "$mailer_from" ] || [ -z "$mailer_host" ] || [ -z "$mailer_username" ]; }; then
    echo 'Feedback SMTP sender, host and username must be all configured or all empty.' >&2
    return 1
  fi
}

build_candidate_public_env() {
  source_file="$1"
  output_file="$2"
  [ -f "$source_file" ] && [ ! -L "$source_file" ] && [ -r "$source_file" ] || {
    echo 'Public environment must be a readable regular file.' >&2
    return 1
  }
  [ "$(stat -c %u "$source_file")" = 0 ] &&
    [ "$(stat -c %a "$source_file")" = 600 ] &&
    [ "$(stat -c %h "$source_file")" = 1 ] || {
      echo 'Public environment must be root-owned, mode 0600 and have one link.' >&2
      return 1
    }

  public_seen='|'
  public_enabled_providers=''
  public_turnstile_site_key=''
  public_source_state_key_id=''
  public_source_previous_key_id=''
  public_have_enabled_providers=0
  public_have_turnstile_site_key=0
  carriage_return="$(printf '\r')"
  while IFS= read -r public_line || [ -n "$public_line" ]; do
    case "$public_line" in
      *"$carriage_return"*)
        echo 'Public environment contains a carriage return.' >&2
        return 1
        ;;
      ''|'#'*) continue ;;
      *=*)
        public_key="${public_line%%=*}"
        public_value="${public_line#*=}"
        ;;
      *)
        echo 'Public environment contains a malformed line.' >&2
        return 1
        ;;
    esac
    case "$public_key" in
      PUBLIC_ORIGIN|PUBLIC_DEVELOPMENT_ORIGINS|PUBLIC_ALLOW_INSECURE_DEV|\
      PUBLIC_AUTO_CREATE_SCHEMA|PUBLIC_TRUST_PROXY_HEADERS|PUBLIC_ENABLED_PROVIDERS|\
      PUBLIC_APP_VERSION|PUBLIC_DATA_VERSION|PUBLIC_MEDIA_VERSION|PUBLIC_MEDIA_ROOT|\
      PUBLIC_EXPERIENCE_NOTICE_VERSION|PUBLIC_ARRIVAL_PROBABILITY|\
      PUBLIC_STICKER_VERSION|PUBLIC_STICKER_ROOT|TURNSTILE_SITE_KEY|\
      PUBLIC_TURNSTILE_HOSTNAME|PUBLIC_TURNSTILE_MAX_AGE_SECONDS|\
      PUBLIC_PRIVACY_POLICY_VERSION|PUBLIC_PRIVACY_EFFECTIVE_AT|\
      PUBLIC_ATTRIBUTION_URL|PUBLIC_MAX_PROVIDER_CALLS_PER_ACTION|PUBLIC_BYOK_LIFETIME_HOURS|\
      PUBLIC_STATE_KEY_ID|PUBLIC_STATE_PREVIOUS_KEY_ID|QDRANT_URL|\
      QDRANT_COLLECTION|EMBEDDING_URL|NEO4J_URI|NEO4J_USER|\
      PUBLIC_FEEDBACK_EMAIL_TO|PUBLIC_FEEDBACK_EMAIL_FROM|\
      PUBLIC_FEEDBACK_SMTP_HOST|PUBLIC_FEEDBACK_SMTP_PORT|\
      PUBLIC_FEEDBACK_SMTP_USERNAME|PUBLIC_FEEDBACK_SMTP_PASSWORD_FILE) ;;
      *)
        echo 'Public environment contains a disallowed key.' >&2
        return 1
        ;;
    esac
    case "$public_seen" in
      *"|$public_key|"*)
        echo 'Public environment contains a duplicate key.' >&2
        return 1
        ;;
    esac
    public_seen="$public_seen$public_key|"
    case "$public_key" in
      PUBLIC_ENABLED_PROVIDERS)
        public_enabled_providers="$public_value"
        public_have_enabled_providers=1
        ;;
      TURNSTILE_SITE_KEY)
        public_turnstile_site_key="$public_value"
        public_have_turnstile_site_key=1
        ;;
      PUBLIC_STATE_KEY_ID) public_source_state_key_id="$public_value" ;;
      PUBLIC_STATE_PREVIOUS_KEY_ID) public_source_previous_key_id="$public_value" ;;
    esac
  done < "$source_file"

  [ "$public_have_enabled_providers" -eq 1 ] &&
    printf '%s\n' "$public_enabled_providers" |
      grep -Eq '^(openai|deepseek|dashscope|zhipu|moonshot)(,(openai|deepseek|dashscope|zhipu|moonshot))*$' || {
        echo 'Public environment has no valid enabled-provider allowlist.' >&2
        return 1
      }
  [ "$public_have_turnstile_site_key" -eq 1 ] &&
    [ "$public_turnstile_site_key" != replace-with-turnstile-site-key ] &&
    printf '%s\n' "$public_turnstile_site_key" | grep -Eq '^[0-9A-Za-z._-]+$' || {
      echo 'Public environment has no valid Turnstile site key.' >&2
      return 1
    }
  for public_state_id in "$public_source_state_key_id" "$public_source_previous_key_id"; do
    [ -z "$public_state_id" ] ||
      printf '%s\n' "$public_state_id" | grep -Eq '^[0-9A-Za-z._-]+$' || {
        echo 'Public environment contains an invalid state key identifier.' >&2
        return 1
      }
  done

  public_target_state_key_id='2026-08-19'
  if [ -n "$public_source_state_key_id" ] &&
     [ "$public_source_state_key_id" != "$public_target_state_key_id" ]; then
    public_target_previous_key_id="$public_source_state_key_id"
  else
    public_target_previous_key_id="$public_source_previous_key_id"
  fi

  {
    printf '%s\n' \
      'PUBLIC_ORIGIN=https://snow.xiaob.dev' \
      'PUBLIC_DEVELOPMENT_ORIGINS=' \
      'PUBLIC_ALLOW_INSECURE_DEV=false' \
      'PUBLIC_AUTO_CREATE_SCHEMA=false' \
      'PUBLIC_TRUST_PROXY_HEADERS=true'
    printf 'PUBLIC_ENABLED_PROVIDERS=%s\n' "$public_enabled_providers"
    printf 'PUBLIC_APP_VERSION=%s\nPUBLIC_DATA_VERSION=%s\n' \
      "$candidate_app_version" "$candidate_data_version"
    printf 'PUBLIC_MEDIA_VERSION=%s\nPUBLIC_MEDIA_ROOT=%s\n' \
      "$candidate_media_version" "$candidate_media_root"
    printf '%s\n' \
      'PUBLIC_EXPERIENCE_NOTICE_VERSION=0.9.2' \
      'PUBLIC_ARRIVAL_PROBABILITY=0.5'
    printf 'PUBLIC_STICKER_VERSION=%s\nPUBLIC_STICKER_ROOT=%s\n' \
      "$candidate_sticker_version" "$candidate_sticker_root"
    printf 'TURNSTILE_SITE_KEY=%s\n' "$public_turnstile_site_key"
    printf '%s\n' \
      'PUBLIC_TURNSTILE_HOSTNAME=snow.xiaob.dev' \
      'PUBLIC_TURNSTILE_MAX_AGE_SECONDS=300' \
      'PUBLIC_PRIVACY_POLICY_VERSION=0.9.2' \
      'PUBLIC_PRIVACY_EFFECTIVE_AT=2026-08-20' \
      'PUBLIC_ATTRIBUTION_URL=/public/v1/attributions' \
      'PUBLIC_MAX_PROVIDER_CALLS_PER_ACTION=2' \
      'PUBLIC_BYOK_LIFETIME_HOURS=12'
    printf 'PUBLIC_STATE_KEY_ID=%s\nPUBLIC_STATE_PREVIOUS_KEY_ID=%s\n' \
      "$public_target_state_key_id" "$public_target_previous_key_id"
    printf '%s\n' \
      'QDRANT_URL=http://qdrant:6333' \
      'QDRANT_COLLECTION=project_snow_documents' \
      'EMBEDDING_URL=http://embedding:8000' \
      'NEO4J_URI=bolt://neo4j:7687' \
      'NEO4J_USER=neo4j'
  } > "$output_file"
  chmod 0600 "$output_file"
}

if [ ! -r "$static_env" ]; then
  echo "Missing readable static image environment: $static_env" >&2
  exit 66
fi
if [ ! -r "$public_env_source" ]; then
  echo "Missing readable public environment: $public_env_source" >&2
  exit 66
fi
mailer_env_file="/etc/project-snow/feedback-mailer.env"
validate_mailer_env "$mailer_env_file" || exit 66
mailer_database_password_file="/etc/project-snow/secrets/feedback_mailer_database_password"
if [ ! -f "$mailer_database_password_file" ] || [ -L "$mailer_database_password_file" ] ||
   [ ! -r "$mailer_database_password_file" ] || [ ! -s "$mailer_database_password_file" ] ||
   [ "$(stat -c %u "$mailer_database_password_file")" != 0 ] ||
   [ "$(stat -c %a "$mailer_database_password_file")" != 600 ]; then
  echo 'Feedback mailer database password must exist as a root-owned mode-0600 regular file.' >&2
  exit 66
fi
if ! printf '%s' "$sha" | grep -Eq '^[0-9a-f]{40}$'; then
  echo 'Release commit SHA must be 40 lowercase hexadecimal characters.' >&2
  exit 65
fi
if [ -z "$release_manifest" ] || [ ! -r "$release_manifest" ]; then
  echo 'A readable verified release manifest is required.' >&2
  exit 71
fi

manifest_schema="$(jq -r '.schema_version // empty' "$release_manifest")"
[ "$manifest_schema" = "project-snow-release-1" ] || {
  echo 'Unsupported release manifest schema.' >&2
  exit 71
}
manifest_commit_sha="$(jq -r '.commit_sha // empty' "$release_manifest")"
[ "$manifest_commit_sha" = "$sha" ] || {
  echo 'Release manifest commit SHA does not match the requested SHA.' >&2
  exit 71
}
manifest_application_image="$(jq -r '.application.image // empty' "$release_manifest")"
manifest_application_digest="$(jq -r '.application.digest // empty' "$release_manifest")"
manifest_embedding_image="$(jq -r '.embedding.image // empty' "$release_manifest")"
manifest_embedding_digest="$(jq -r '.embedding.digest // empty' "$release_manifest")"
if ! printf '%s\n' "$manifest_application_image" | grep -Eq '^[^[:space:]@]+$' ||
   ! printf '%s\n' "$manifest_embedding_image" | grep -Eq '^[^[:space:]@]+$' ||
   ! printf '%s\n' "$manifest_application_digest" | grep -Eq '^sha256:[0-9a-f]{64}$' ||
   ! printf '%s\n' "$manifest_embedding_digest" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
  echo 'Release manifest contains invalid image coordinates.' >&2
  exit 71
fi
[ "$PUBLIC_API_IMAGE" = "$manifest_application_image@$manifest_application_digest" ] || {
  echo 'Release manifest application image does not match PUBLIC_API_IMAGE.' >&2
  exit 71
}
[ "$EMBEDDING_IMAGE" = "$manifest_embedding_image@$manifest_embedding_digest" ] || {
  echo 'Release manifest embedding image does not match EMBEDDING_IMAGE.' >&2
  exit 71
}
for configuration_path in $configuration_paths; do
  expected_configuration_sha="$(jq -r --arg path "$configuration_path" '.configuration_sha256[$path] // empty' "$release_manifest")"
  printf '%s\n' "$expected_configuration_sha" | grep -Eq '^[0-9a-f]{64}$' || {
    echo "Release manifest has no valid hash for $configuration_path." >&2
    exit 71
  }
  actual_configuration_sha="$(sha256sum "$configuration_path" | awk '{print $1}')"
  [ "$actual_configuration_sha" = "$expected_configuration_sha" ] || {
    echo "Release configuration hash mismatch for $configuration_path." >&2
    exit 71
  }
done

active_colour=""
if [ ! -e "$active_file" ]; then
  echo 'Active-colour marker is required before staging.' >&2
  exit 67
fi
if [ ! -r "$active_file" ]; then
  echo 'Active-colour marker exists but is not readable.' >&2
  exit 67
fi
active_colour="$(cat "$active_file")"
case "$active_colour" in blue|green) ;; *) echo 'Invalid active-colour marker.' >&2; exit 67 ;; esac
if [ "$active_colour" = "$colour" ]; then
  echo "Refusing to stage active colour '$colour'; choose the inactive colour." >&2
  exit 70
fi

install -d -m 0700 "$colour_env_root" "$colour_release_root"
install -d -m 0755 "$configuration_release_root"
install -d -m 0750 "$public_env_root"
# Bootstrap every durable artifact for the active colour before the first
# staged release. Older installations only have the promoted compose env,
# marker, and manifest at their legacy current paths. A partial bootstrap
# would make the old colour impossible to select through rollback.sh.
if [ -n "$active_colour" ]; then
  bootstrap_colour_env="$colour_env_root/$active_colour.compose.env"
  bootstrap_colour_marker="$colour_release_root/$active_colour"
  bootstrap_colour_manifest="$colour_release_root/$active_colour-manifest.json"

  if [ ! -r "$bootstrap_colour_env" ]; then
    [ -r "$current_env" ] || { echo "Cannot preserve rollback environment for active colour $active_colour." >&2; exit 68; }
    cp "$current_env" "$bootstrap_colour_env"
    chmod 0600 "$bootstrap_colour_env"
  fi
  if [ ! -r "$bootstrap_colour_marker" ]; then
    [ -r "$current_marker" ] || { echo "Cannot preserve rollback marker for active colour $active_colour." >&2; exit 68; }
    cp "$current_marker" "$bootstrap_colour_marker"
    chmod 0600 "$bootstrap_colour_marker"
  fi
  if [ ! -r "$bootstrap_colour_manifest" ]; then
    [ -r "$current_manifest" ] || { echo "Cannot preserve rollback manifest for active colour $active_colour." >&2; exit 68; }
    cp "$current_manifest" "$bootstrap_colour_manifest"
    chmod 0600 "$bootstrap_colour_manifest"
  fi

  read -r bootstrap_marker_colour bootstrap_marker_sha bootstrap_app_image bootstrap_embedding_image < "$bootstrap_colour_marker"
  [ "$bootstrap_marker_colour" = "$active_colour" ] || { echo 'Bootstrap rollback marker colour mismatch.' >&2; exit 69; }
  printf '%s\n' "$bootstrap_marker_sha" | grep -Eq '^[0-9a-f]{40}$' || {
    echo 'Bootstrap rollback marker has an invalid commit SHA.' >&2
    exit 69
  }
  if ! is_immutable_image "$bootstrap_app_image" || ! is_immutable_image "$bootstrap_embedding_image"; then
    echo 'Bootstrap rollback marker images are not immutable digests.' >&2
    exit 69
  fi
  bootstrap_manifest_sha="$(jq -r '.commit_sha // empty' "$bootstrap_colour_manifest")"
  if [ "$bootstrap_manifest_sha" != "$bootstrap_marker_sha" ]; then
    echo 'Bootstrap rollback manifest is invalid.' >&2
    exit 69
  fi
  bootstrap_data_version="$(jq -r '.data_version // empty' "$bootstrap_colour_manifest")"
  case "$bootstrap_data_version" in
    *[!0-9A-Za-z._-]*|'') echo 'Bootstrap rollback manifest has an invalid data version.' >&2; exit 69 ;;
  esac
  bootstrap_data_root="/srv/project-snow/data/releases/$bootstrap_data_version"
  bootstrap_data_manifest="$bootstrap_data_root/manifest.json"
  if [ -L "$bootstrap_data_root" ] || [ ! -r "$bootstrap_data_manifest" ] ||
     [ "$(jq -r '.data_version // empty' "$bootstrap_data_manifest")" != "$bootstrap_data_version" ]; then
    echo "Cannot pin rollback data release $bootstrap_data_version." >&2
    exit 69
  fi
  sed -i '/^PUBLIC_DATA_ROOT=/d' "$bootstrap_colour_env"
  sed -i '/^PUBLIC_MAILER_ENV_FILE=/d' "$bootstrap_colour_env"
  printf 'PUBLIC_DATA_ROOT=%s\nPUBLIC_MAILER_ENV_FILE=%s\n' \
    "$bootstrap_data_root" "$mailer_env_file" >> "$bootstrap_colour_env"
  chmod 0600 "$bootstrap_colour_env"

  bootstrap_config_binding="$colour_release_root/$active_colour-config.json"
  bootstrap_config_root="$configuration_release_root/$bootstrap_marker_sha"
  if [ ! -r "$bootstrap_config_binding" ]; then
    # Old installations did not persist their Compose/edge inputs. Rebuild the
    # first rollback snapshot from the exact commit recorded by the active
    # marker, never from the candidate checkout.
    (
      bootstrap_tmp="$(mktemp -d "$configuration_release_root/$bootstrap_marker_sha.candidate.XXXXXX")"
      bootstrap_binding_tmp="$(mktemp "$bootstrap_config_binding.candidate.XXXXXX")"
      cleanup_bootstrap_snapshot() {
        [ -z "${bootstrap_tmp:-}" ] || rm -rf -- "$bootstrap_tmp"
        [ -z "${bootstrap_binding_tmp:-}" ] || rm -f -- "$bootstrap_binding_tmp"
      }
      trap cleanup_bootstrap_snapshot EXIT HUP INT TERM
      repository_root="$(git rev-parse --show-toplevel)"
      git -C "$repository_root" cat-file -e "$bootstrap_marker_sha^{commit}" || {
        echo "Cannot reconstruct rollback configuration for $bootstrap_marker_sha." >&2
        exit 69
      }
      bootstrap_archive_paths=""
      for bootstrap_base_path in $base_configuration_paths; do
        bootstrap_archive_paths="$bootstrap_archive_paths App/$bootstrap_base_path"
      done
      bootstrap_direct_count=0
      for bootstrap_direct_path in $direct_origin_configuration_paths; do
        if git -C "$repository_root" cat-file -e \
          "$bootstrap_marker_sha:App/$bootstrap_direct_path" 2>/dev/null; then
          bootstrap_archive_paths="$bootstrap_archive_paths App/$bootstrap_direct_path"
          bootstrap_direct_count=$((bootstrap_direct_count + 1))
        fi
      done
      case "$bootstrap_direct_count" in
        0|4) ;;
        *) echo 'Recorded rollback commit has an incomplete direct-origin configuration bundle.' >&2; exit 69 ;;
      esac
      git -C "$repository_root" archive "$bootstrap_marker_sha" -- $bootstrap_archive_paths |
        tar -x -C "$bootstrap_tmp" --strip-components=1
      if [ -e "$bootstrap_config_root" ]; then
        [ -d "$bootstrap_config_root" ] && [ ! -L "$bootstrap_config_root" ] || {
          echo 'Existing rollback configuration snapshot is not a regular directory.' >&2
          exit 69
        }
        bootstrap_paths="$(configuration_paths_for_root "$bootstrap_tmp")" || exit 69
        existing_bootstrap_paths="$(configuration_paths_for_root "$bootstrap_config_root")" || exit 69
        [ "$bootstrap_paths" = "$existing_bootstrap_paths" ] || {
          echo 'Existing rollback configuration has a different file set than its commit.' >&2
          exit 69
        }
        for bootstrap_path in $bootstrap_paths; do
          [ -f "$bootstrap_config_root/$bootstrap_path" ] &&
            [ ! -L "$bootstrap_config_root/$bootstrap_path" ] &&
            cmp -s "$bootstrap_tmp/$bootstrap_path" "$bootstrap_config_root/$bootstrap_path" || {
              echo "Existing rollback configuration differs from commit: $bootstrap_path." >&2
              exit 69
            }
        done
      else
        find "$bootstrap_tmp" -type f -exec chmod 0444 {} +
        find "$bootstrap_tmp" -type d -exec chmod 0555 {} +
        mv -- "$bootstrap_tmp" "$bootstrap_config_root"
        bootstrap_tmp=""
      fi
      create_config_binding "$active_colour" "$bootstrap_marker_sha" \
        "$bootstrap_config_root" "$bootstrap_binding_tmp"
      mv -f -- "$bootstrap_binding_tmp" "$bootstrap_config_binding"
      bootstrap_binding_tmp=""
    )
  fi
  validate_config_binding "$bootstrap_config_binding" "$active_colour" "$bootstrap_marker_sha" || exit 69
fi

candidate_env="$(mktemp "$colour_env.candidate.XXXXXX")"
candidate_manifest="$(mktemp "$colour_manifest.candidate.XXXXXX")"
candidate_marker="$(mktemp "$colour_marker.candidate.XXXXXX")"
candidate_config_binding_tmp="$(mktemp "$colour_config_binding.candidate.XXXXXX")"
candidate_config_root="$configuration_release_root/$sha"
candidate_config_tmp=""
candidate_public_env=""
public_env_path="$public_env_root/public-$sha.env"
cleanup() {
  [ -z "${candidate_env:-}" ] || rm -f "$candidate_env"
  [ -z "${candidate_manifest:-}" ] || rm -f "$candidate_manifest"
  [ -z "${candidate_marker:-}" ] || rm -f "$candidate_marker"
  [ -z "${candidate_config_binding_tmp:-}" ] || rm -f "$candidate_config_binding_tmp"
  [ -z "${candidate_config_tmp:-}" ] || rm -rf -- "$candidate_config_tmp"
  [ -z "${candidate_public_env:-}" ] || rm -f "$candidate_public_env"
}
trap cleanup EXIT HUP INT TERM

cat "$static_env" > "$candidate_env"
printf '\nPUBLIC_API_IMAGE=%s\nEMBEDDING_IMAGE=%s\n' "$PUBLIC_API_IMAGE" "$EMBEDDING_IMAGE" >> "$candidate_env"
sed -i '/^PUBLIC_MAILER_ENV_FILE=/d' "$candidate_env"
printf 'PUBLIC_MAILER_ENV_FILE=%s\n' "$mailer_env_file" >> "$candidate_env"
chmod 0600 "$candidate_env"

compose() {
  docker compose --env-file "$candidate_env" -f "$candidate_config_root/compose.prod.yml" --profile "$colour" "$@"
}

embedding_changed=1
if [ -r "$current_marker" ]; then
  current_embedding="$(awk '{print $4}' "$current_marker")"
  if [ "$current_embedding" = "$EMBEDDING_IMAGE" ]; then
    embedding_changed=0
  fi
fi

candidate_data_version=""
candidate_data_root=""
candidate_app_version=""
candidate_media_version=""
candidate_media_root=""
candidate_sticker_version=""
candidate_sticker_root=""
if [ -n "$release_manifest" ]; then
  candidate_app_version="$(jq -r '.app_version // empty' "$release_manifest")"
  case "$candidate_app_version" in
    *[!0-9A-Za-z._-]*|'') echo 'Release manifest has an unsafe or missing application version.' >&2; exit 71 ;;
  esac
  candidate_data_version="$(jq -r '.data_version // empty' "$release_manifest")"
  case "$candidate_data_version" in
    *[!0-9A-Za-z._-]*|'') echo 'Release manifest has an unsafe or missing data version.' >&2; exit 71 ;;
  esac
  candidate_data_root="/srv/project-snow/data/releases/$candidate_data_version"
  candidate_data_manifest="$candidate_data_root/manifest.json"
  if [ -L "$candidate_data_root" ] || [ ! -r "$candidate_data_manifest" ] ||
     [ "$(jq -r '.data_version // empty' "$candidate_data_manifest")" != "$candidate_data_version" ]; then
    echo "Data $candidate_data_version must be verified and staged before application staging." >&2
    exit 72
  fi
  candidate_media_version="$(jq -r '.media_version // empty' "$release_manifest")"
  case "$candidate_media_version" in
    *[!0-9A-Za-z._-]*|'') echo 'Release manifest has an unsafe or missing media version.' >&2; exit 71 ;;
  esac
  candidate_media_root="/srv/project-snow/media/releases/$candidate_media_version"
  candidate_media_manifest="$candidate_media_root/manifest.json"
  if [ ! -r "$candidate_media_manifest" ] ||
     [ "$(jq -r '.media_version // empty' "$candidate_media_manifest")" != "$candidate_media_version" ]; then
    echo "Media $candidate_media_version must be downloaded, verified and staged before application staging." >&2
    exit 72
  fi
  candidate_sticker_version="$(jq -r '.sticker_version // empty' "$release_manifest")"
  case "$candidate_sticker_version" in
    *[!0-9A-Za-z._-]*|'') echo 'Release manifest has an unsafe or missing sticker version.' >&2; exit 71 ;;
  esac
  candidate_sticker_root="/srv/project-snow/media/stickers/releases/$candidate_sticker_version"
  candidate_sticker_manifest="$candidate_sticker_root/manifest.json"
  if [ ! -r "$candidate_sticker_manifest" ] ||
     [ "$(jq -r '.media_version // empty' "$candidate_sticker_manifest")" != "$candidate_sticker_version" ]; then
    echo "Sticker media $candidate_sticker_version must be downloaded, verified and staged before application staging." >&2
    exit 73
  fi
fi

if [ -e "$candidate_config_root" ]; then
  [ -d "$candidate_config_root" ] && [ ! -L "$candidate_config_root" ] || {
    echo "Configuration snapshot path for $sha is not a regular directory." >&2
    exit 71
  }
  verify_snapshot_against_manifest "$candidate_config_root" "$release_manifest" || {
    echo "Existing configuration snapshot does not match release $sha." >&2
    exit 71
  }
else
  candidate_config_tmp="$(mktemp -d "$configuration_release_root/$sha.candidate.XXXXXX")"
  for candidate_config_path in $configuration_paths; do
    candidate_config_parent="$(dirname "$candidate_config_path")"
    install -d -m 0755 "$candidate_config_tmp/$candidate_config_parent"
    install -m 0444 "$candidate_config_path" "$candidate_config_tmp/$candidate_config_path"
  done
  verify_snapshot_against_manifest "$candidate_config_tmp" "$release_manifest" || {
    echo "Candidate configuration snapshot does not match its release manifest." >&2
    exit 71
  }
  find "$candidate_config_tmp" -type d -exec chmod 0555 {} +
  mv -- "$candidate_config_tmp" "$candidate_config_root"
  candidate_config_tmp=""
fi
create_config_binding "$colour" "$sha" "$candidate_config_root" "$candidate_config_binding_tmp"
validate_config_binding "$candidate_config_binding_tmp" "$colour" "$sha" || exit 71

if [ -n "$candidate_media_root" ]; then
  candidate_public_env="$(mktemp "$public_env_root/public-$sha.candidate.XXXXXX")"
  build_candidate_public_env "$public_env_source" "$candidate_public_env" || exit 66
  sed -i '/^PUBLIC_ENV_FILE=/d' "$candidate_env"
  printf 'PUBLIC_ENV_FILE=%s\n' "$candidate_public_env" >> "$candidate_env"
  sed -i '/^PUBLIC_DATA_ROOT=/d' "$candidate_env"
  printf 'PUBLIC_DATA_ROOT=%s\nPUBLIC_MEDIA_VERSION=%s\nPUBLIC_MEDIA_ROOT=%s\n' \
    "$candidate_data_root" "$candidate_media_version" "$candidate_media_root" >> "$candidate_env"
  chmod 0600 "$candidate_env"
fi

service="public-api-$colour"
compose pull "$service"
if [ "$embedding_changed" -eq 1 ]; then
  compose pull embedding
else
  echo 'Embedding digest unchanged; skipping image pull.'
fi
compose run --rm --no-deps "$service" \
  python -m backend.snow_app.data_loader --release-root "$candidate_data_root" --verify-only
compose run --rm "$service" alembic upgrade head
compose up -d postgres qdrant neo4j embedding egress-proxy
postgres_ready=0
attempt=0
while [ "$attempt" -lt 30 ]; do
  if compose exec -T --user postgres postgres pg_isready -U project_snow -d project_snow >/dev/null 2>&1; then
    postgres_ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if [ "$postgres_ready" -ne 1 ]; then
  echo 'PostgreSQL did not become ready for logging-policy reload.' >&2
  exit 73
fi
compose exec -T --user postgres postgres pg_ctl reload -D /var/lib/postgresql/data
postgres_logging="$(compose exec -T --user postgres postgres \
  psql -U project_snow -d project_snow -At -F '|' -v ON_ERROR_STOP=1 \
  -c "SELECT current_setting('log_statement'), current_setting('log_min_duration_statement'), current_setting('log_min_error_statement'), current_setting('log_parameter_max_length'), current_setting('log_parameter_max_length_on_error')")"
[ "$postgres_logging" = 'none|-1|panic|0|0' ] || {
  echo "PostgreSQL logging policy is unsafe: $postgres_logging" >&2
  exit 73
}
# The migration creates a NOLOGIN least-privilege role. Rotate it to the
# root-only deployment secret only after statement/parameter logging is known
# to be disabled. The secret stays inside the Postgres container, is supplied
# to psql over stdin, and never appears in argv, Compose interpolation or logs.
if ! compose exec -T --user root postgres sh -s <<'PROJECT_SNOW_MAILER_ROLE'
set -eu
umask 077
# Even if the static ALTER statement fails, never let PostgreSQL attach the
# statement (which contains a reversible hex encoding) to an error log.
PGOPTIONS='-c log_min_error_statement=panic'
export PGOPTIONS
secret_path=/run/secrets/feedback_mailer_database_password
if [ ! -f "$secret_path" ] || [ ! -s "$secret_path" ]; then
  echo 'Missing required feedback mailer database password secret.' >&2
  exit 1
fi
secret_owner="$(stat -c %u "$secret_path")"
secret_mode="$(stat -c %a "$secret_path")"
[ "$secret_owner" = 0 ] && { [ "$secret_mode" = 400 ] || [ "$secret_mode" = 600 ]; } || {
  echo 'Feedback mailer database password must be a root-only file.' >&2
  exit 1
}
mailer_password="$(cat "$secret_path")"
password_length="$(printf '%s' "$mailer_password" | wc -c | tr -d '[:space:]')"
[ "$password_length" -ge 32 ] || {
  echo 'Feedback mailer database password must contain at least 32 bytes.' >&2
  exit 1
}
if printf '%s' "$mailer_password" | LC_ALL=C grep -q '[[:space:]]'; then
  echo 'Feedback mailer database password must be a single token without whitespace.' >&2
  exit 1
fi
password_hex="$(printf '%s' "$mailer_password" | od -An -tx1 -v | tr -d '[:space:]')"
unset mailer_password
role_count="$(psql -X -U project_snow -d project_snow -At -v ON_ERROR_STOP=1 \
  -c "SELECT count(*) FROM pg_roles WHERE rolname = 'project_snow_feedback_mailer'")"
[ "$role_count" = 1 ] || {
  echo 'Feedback mailer database role is missing after migration.' >&2
  exit 1
}
{
  printf '%s\n' '\set ON_ERROR_STOP on'
  printf "SELECT format('ALTER ROLE %%I LOGIN PASSWORD %%L', 'project_snow_feedback_mailer', convert_from(decode('%s', 'hex'), 'UTF8')) \\gexec\n" "$password_hex"
} | psql -X -q -U project_snow -d project_snow >/dev/null 2>&1 || {
  unset password_hex
  echo 'Failed to rotate the feedback mailer database role.' >&2
  exit 1
}
unset password_hex
role_can_login="$(psql -X -U project_snow -d project_snow -At -v ON_ERROR_STOP=1 \
  -c "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'project_snow_feedback_mailer'")"
[ "$role_can_login" = t ] || {
  echo 'Feedback mailer database role did not become login-capable.' >&2
  exit 1
}
PROJECT_SNOW_MAILER_ROLE
then
  echo 'Feedback mailer database credential preparation failed.' >&2
  exit 73
fi
# Default loading is stage-only: it creates/verifies the exact versioned
# Qdrant collection and Neo4j dataset without changing legacy serving pointers.
if ! compose run --rm --no-deps -T "$service" python - <<'PROJECT_SNOW_DATA_DEPENDENCIES'
import socket
import time

pending = {("qdrant", 6333), ("neo4j", 7687)}
deadline = time.monotonic() + 60
while pending and time.monotonic() < deadline:
    for target in tuple(pending):
        try:
            with socket.create_connection(target, timeout=2):
                pending.remove(target)
        except OSError:
            pass
    if pending:
        time.sleep(1)
if pending:
    names = ", ".join(f"{host}:{port}" for host, port in sorted(pending))
    raise SystemExit(f"Data dependencies did not become reachable: {names}")
PROJECT_SNOW_DATA_DEPENDENCIES
then
  echo 'Qdrant or Neo4j did not become reachable before staged data loading.' >&2
  exit 73
fi
compose run --rm --no-deps "$service" \
  python -m backend.snow_app.data_loader --release-root "$candidate_data_root"
# Only the inactive API is started. Caddy, origin-edge and cloudflared keep
# serving the current colour until promote.sh is explicitly invoked.
compose up -d "$service"
ready=0
attempt=0
while [ "$attempt" -lt 30 ]; do
  if compose exec -T "$service" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/public/v1/health/ready', timeout=5).read()" >/dev/null 2>&1; then
    ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo 'Staged public API did not become ready within 60 seconds.' >&2
  compose logs --tail=100 "$service" >&2 || true
  exit 73
fi
compose exec -T "$service" python /app/public_smoke.py http://127.0.0.1:8000 --mode internal

candidate_container_ids="$(compose ps -q "$service" 2>/dev/null || true)"
candidate_container_count="$(printf '%s\n' "$candidate_container_ids" | awk 'NF { count += 1 } END { print count + 0 }')"
[ "$candidate_container_count" -eq 1 ] || {
  echo 'Expected exactly one staged API container.' >&2
  exit 73
}
candidate_container_id="$(printf '%s\n' "$candidate_container_ids" | awk 'NF { print; exit }')"
printf '%s\n' "$candidate_container_id" | grep -Eq '^[0-9a-f]{12,64}$' || {
  echo 'Staged API container identifier is invalid.' >&2
  exit 73
}

# Candidate acceptance stays on Docker's internal app network.  This avoids a
# second host listener and, critically, does not give the API a non-internal
# default route that could bypass the allowlisted egress proxy.
candidate_app_network=project-snow-public_app
candidate_data_network=project-snow-public_data
candidate_egress_network=project-snow-public_egress-client
candidate_networks="$(docker inspect "$candidate_container_id")"
printf '%s\n' "$candidate_networks" | jq -e \
  --arg app "$candidate_app_network" \
  --arg data "$candidate_data_network" \
  --arg egress "$candidate_egress_network" '
    length == 1 and
    .[0].State.Running == true and
    ((.[0].HostConfig.PortBindings // {}) | length == 0) and
    ((.[0].NetworkSettings.Networks | keys | sort) == ([$app, $data, $egress] | sort))
  ' >/dev/null || {
    echo 'Staged API network attachment is not the exact internal-only policy.' >&2
    exit 73
  }

for candidate_network_pair in \
  "app:$candidate_app_network" \
  "data:$candidate_data_network" \
  "egress-client:$candidate_egress_network"; do
  candidate_network_role="${candidate_network_pair%%:*}"
  candidate_network_name="${candidate_network_pair#*:}"
  docker network inspect "$candidate_network_name" |
    jq -e --arg role "$candidate_network_role" '
      length == 1 and
      .[0].Driver == "bridge" and
      .[0].Internal == true and
      .[0].Labels["com.docker.compose.project"] == "project-snow-public" and
      .[0].Labels["com.docker.compose.network"] == $role
    ' >/dev/null || {
      echo "Staged API network policy is invalid for $candidate_network_role." >&2
      exit 73
    }
done

candidate_app_network_metadata="$(docker network inspect "$candidate_app_network")"
candidate_app_subnet="$(printf '%s\n' "$candidate_app_network_metadata" | jq -r '
  [.[0].IPAM.Config[]?.Subnet | select(contains(":") | not)] |
  if length == 1 then .[0] else empty end
')"
candidate_internal_ip="$(printf '%s\n' "$candidate_networks" | jq -r \
  --arg app "$candidate_app_network" '.[0].NetworkSettings.Networks[$app].IPAddress // empty')"
[ -n "$candidate_app_subnet" ] && [ -n "$candidate_internal_ip" ] &&
  python3 -c 'import ipaddress, sys; address = ipaddress.ip_address(sys.argv[1]); subnet = ipaddress.ip_network(sys.argv[2], strict=True); raise SystemExit(0 if address.version == 4 and address in subnet else 1)' \
    "$candidate_internal_ip" "$candidate_app_subnet" || {
      echo 'Staged API internal acceptance address is invalid.' >&2
      exit 73
    }

candidate_internal_endpoint="http://$candidate_internal_ip:8000/public/v1/health/live"
candidate_acceptance_ready=0
candidate_acceptance_attempt=0
while [ "$candidate_acceptance_attempt" -lt 10 ]; do
  if /usr/bin/curl -q --noproxy '*' --fail --silent --show-error --max-time 5 \
       -H 'Host: snow.xiaob.dev' "$candidate_internal_endpoint" 2>/dev/null |
     jq -e --arg version "$candidate_app_version" \
       '.status == "ok" and .version == $version' >/dev/null 2>&1; then
    candidate_acceptance_ready=1
    break
  fi
  candidate_acceptance_attempt=$((candidate_acceptance_attempt + 1))
  sleep 1
done
[ "$candidate_acceptance_ready" -eq 1 ] || {
  echo 'Staged API is not reachable on its internal SSH acceptance target.' >&2
  exit 73
}

# Install the immutable firewall helper and boot ordering only after all staged
# application gates pass. Its first live update must succeed before this colour
# is made durable; promote.sh repeats that fail-closed gate immediately before
# it is allowed to create or recreate origin-edge.
install_direct_origin_firewall "$candidate_config_root" || {
  echo 'Direct-origin firewall installation or initial update failed.' >&2
  exit 73
}

if [ -n "$candidate_public_env" ]; then
  sed -i "s|^PUBLIC_ENV_FILE=.*|PUBLIC_ENV_FILE=$public_env_path|" "$candidate_env"
  mv -f "$candidate_public_env" "$public_env_path"
  candidate_public_env=""
fi
printf '%s\n' "$colour $sha $PUBLIC_API_IMAGE $EMBEDDING_IMAGE" > "$candidate_marker"
chmod 0600 "$candidate_marker"
if [ -n "$release_manifest" ]; then
  cp "$release_manifest" "$candidate_manifest"
  chmod 0600 "$candidate_manifest"
fi
mv -f "$candidate_env" "$colour_env"
candidate_env=""
if [ -n "$release_manifest" ]; then
  mv -f "$candidate_manifest" "$colour_manifest"
  candidate_manifest=""
fi
mv -f "$candidate_config_binding_tmp" "$colour_config_binding"
candidate_config_binding_tmp=""
mv -f "$candidate_marker" "$colour_marker"
candidate_marker=""

printf '%s\n' "Staged $colour $sha without changing active traffic."
printf '%s\n' "Private acceptance target: $candidate_internal_ip:8000 (SSH tunnel only)"
printf '%s\n' 'Promote only after acceptance with: ./ops/promote.sh <colour>'
