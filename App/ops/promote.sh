#!/bin/sh
set -eu
umask 077

# Promote a previously staged colour after private acceptance. The candidate
# must already be running and passing its direct health/smoke checks.
colour="${1:?blue or green required}"
expected_sha="${2:-}"
case "$colour" in blue|green) ;; *) exit 64 ;; esac
rollback_mode="${PROJECT_SNOW_ROLLBACK_MODE:-0}"
case "$rollback_mode" in 0|1) ;; *) echo 'Invalid internal rollback mode.' >&2; exit 64 ;; esac
access_policy_file=/etc/project-snow/access-denied-status
access_denied_status=''
if [ -e "$access_policy_file" ]; then
  [ -f "$access_policy_file" ] && [ ! -L "$access_policy_file" ] &&
    [ -r "$access_policy_file" ] && [ "$(stat -c %u "$access_policy_file")" = 0 ] &&
    [ "$(stat -c %a "$access_policy_file")" = 600 ] || {
      echo 'Access denial status policy must be a root-owned mode-0600 regular file.' >&2
      exit 64
    }
  access_denied_status="$(cat "$access_policy_file")"
  [ "$access_denied_status" = 403 ] || {
    echo 'Access denial status policy may contain only 403.' >&2
    exit 64
  }
fi

is_immutable_image() {
  printf '%s\n' "$1" | grep -Eq '^[^[:space:]@]+@sha256:[0-9a-f]{64}$'
}

runtime_root="/srv/project-snow/runtime"
colour_env="$runtime_root/colours/$colour.compose.env"
colour_release_root="/srv/project-snow/releases/colours"
configuration_release_root="/srv/project-snow/releases/configurations"
colour_marker="$colour_release_root/$colour"
colour_manifest="$colour_release_root/$colour-manifest.json"
colour_config_binding="$colour_release_root/$colour-config.json"
current_env="${PROJECT_SNOW_COMPOSE_ENV:-$runtime_root/compose.env}"
current_manifest="/srv/project-snow/releases/current-manifest.json"
current_config_binding="/srv/project-snow/releases/current-config.json"
active_file="/srv/project-snow/releases/active-colour"
service="public-api-$colour"
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
smoke_script="$script_dir/../infra/public_smoke.py"
[ -r "$smoke_script" ] || {
  echo 'The trusted release smoke script is missing.' >&2
  exit 66
}

configuration_paths="compose.prod.yml
infra/Caddyfile
infra/egress-squid.conf
infra/neo4j-entrypoint.sh
infra/postgres/postgresql.conf
infra/public-api.Dockerfile
requirements-public.txt"

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
  binding_count="$(jq -r '.configuration_sha256 | if type == "object" then length else 0 end' "$binding_file")"
  [ "$binding_count" -eq 7 ] || {
    echo "Configuration snapshot binding has an unexpected file count for $binding_colour." >&2
    return 1
  }
  for binding_path in $configuration_paths; do
    expected_digest="$(jq -r --arg path "$binding_path" '.configuration_sha256[$path] // empty' "$binding_file")"
    printf '%s\n' "$expected_digest" | grep -Eq '^[0-9a-f]{64}$' || return 1
    [ -f "$expected_root/$binding_path" ] && [ ! -L "$expected_root/$binding_path" ] || return 1
    actual_digest="$(sha256sum "$expected_root/$binding_path" | awk '{print $1}')"
    [ "$actual_digest" = "$expected_digest" ] || {
      echo "Configuration snapshot hash mismatch for $binding_path." >&2
      return 1
    }
  done
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

verify_cloudflare_access_restored() (
  probe_headers="$(mktemp /tmp/project-snow-access-probe.XXXXXX)"
  trap 'rm -f -- "$probe_headers"' EXIT HUP INT TERM
  probe_status="$(/usr/bin/curl -q --silent --show-error \
    --connect-timeout 5 --max-time 15 --max-redirs 0 --noproxy '*' \
    --proto '=https' --tlsv1.2 \
    --header 'Cookie:' --header 'Authorization:' \
    --dump-header "$probe_headers" --output /dev/null \
    --write-out '%{http_code}' https://snow.xiaob.dev/)" || {
      echo 'Could not verify Cloudflare Access before legacy rollback.' >&2
      exit 1
    }
  probe_location="$(awk '
    tolower($1) == "location:" {
      sub(/^[^:]*:[[:space:]]*/, "")
      sub(/\r$/, "")
      value = $0
    }
    END { print value }
  ' "$probe_headers")"
  case "$probe_status" in
    302)
      case "$probe_location" in
        https://snow.xiaob.dev/cdn-cgi/access/*|https://*.cloudflareaccess.com/cdn-cgi/access/*) ;;
        *) echo 'Cloudflare Access probe returned an untrusted redirect.' >&2; exit 1 ;;
      esac
      ;;
    403)
      [ "$access_denied_status" = 403 ] || {
        echo 'Cloudflare Access 403 is not enabled by the root policy.' >&2
        exit 1
      }
      ;;
    2??)
      echo 'Legacy rollback refused because the public site is not behind Access.' >&2
      exit 1
      ;;
    *)
      echo "Legacy rollback Access probe returned unexpected HTTP status $probe_status." >&2
      exit 1
      ;;
  esac
)

mailer_env_file="/etc/project-snow/feedback-mailer.env"
validate_mailer_env "$mailer_env_file" || exit 66

if [ ! -r "$colour_env" ] || [ ! -r "$colour_marker" ] ||
   [ ! -r "$colour_manifest" ] || [ ! -r "$colour_config_binding" ]; then
  echo "No staged release exists for $colour." >&2
  exit 66
fi
read -r marker_colour marker_sha marker_app_image marker_embedding_image < "$colour_marker"
[ "$marker_colour" = "$colour" ] || { echo 'Staged marker colour mismatch.' >&2; exit 67; }
printf '%s\n' "$marker_sha" | grep -Eq '^[0-9a-f]{40}$' || { echo 'Invalid staged commit SHA.' >&2; exit 67; }
if ! is_immutable_image "$marker_app_image" || ! is_immutable_image "$marker_embedding_image"; then
  echo 'Staged images are not immutable digests.' >&2
  exit 67
fi
if [ -n "$expected_sha" ] && [ "$expected_sha" != "$marker_sha" ]; then
  echo "Staged SHA $marker_sha does not match requested SHA $expected_sha." >&2
  exit 68
fi
manifest_sha="$(jq -r '.commit_sha // empty' "$colour_manifest")"
manifest_app_version="$(jq -r '.app_version // empty' "$colour_manifest")"
manifest_app_ref="$(jq -r '(.application.image // "") + "@" + (.application.digest // "")' "$colour_manifest")"
manifest_embedding_ref="$(jq -r '(.embedding.image // "") + "@" + (.embedding.digest // "")' "$colour_manifest")"
if [ "$manifest_sha" != "$marker_sha" ] || [ "$manifest_app_ref" != "$marker_app_image" ] ||
   [ "$manifest_embedding_ref" != "$marker_embedding_image" ]; then
  echo 'Staged marker and release manifest identities do not match.' >&2
  exit 67
fi
legacy_rollback_compat=0
if [ "$rollback_mode" = 1 ]; then
  case "$manifest_app_version" in 0.8.*) legacy_rollback_compat=1 ;; esac
fi
validate_config_binding "$colour_config_binding" "$colour" "$marker_sha" || exit 67
colour_config_root="$(jq -r '.root' "$colour_config_binding")"
manifest_config_count="$(jq -r '.configuration_sha256 | if type == "object" then length else 0 end' "$colour_manifest")"
if [ "$manifest_config_count" -gt 0 ]; then
  [ "$manifest_config_count" -eq 7 ] || {
    echo 'Staged release manifest has an incomplete configuration hash set.' >&2
    exit 67
  }
  for manifest_config_path in $configuration_paths; do
    manifest_config_digest="$(jq -r --arg path "$manifest_config_path" '.configuration_sha256[$path] // empty' "$colour_manifest")"
    binding_config_digest="$(jq -r --arg path "$manifest_config_path" '.configuration_sha256[$path] // empty' "$colour_config_binding")"
    [ "$manifest_config_digest" = "$binding_config_digest" ] || {
      echo "Staged configuration binding differs from its release manifest: $manifest_config_path." >&2
      exit 67
    }
  done
fi
manifest_data_version="$(jq -r '.data_version // empty' "$colour_manifest")"
case "$manifest_data_version" in
  *[!0-9A-Za-z._-]*|'') echo 'Staged manifest has an invalid data version.' >&2; exit 67 ;;
esac
expected_data_root="/srv/project-snow/data/releases/$manifest_data_version"
staged_data_root="$(sed -n 's/^PUBLIC_DATA_ROOT=//p' "$colour_env" | tail -n 1)"
staged_mailer_env="$(sed -n 's/^PUBLIC_MAILER_ENV_FILE=//p' "$colour_env" | tail -n 1)"
if [ "$staged_mailer_env" != "$mailer_env_file" ]; then
  echo 'Staged colour does not pin the dedicated feedback mailer environment.' >&2
  exit 67
fi
if [ "$staged_data_root" != "$expected_data_root" ] || [ -L "$staged_data_root" ] ||
   [ ! -r "$staged_data_root/manifest.json" ] ||
   [ "$(jq -r '.data_version // empty' "$staged_data_root/manifest.json")" != "$manifest_data_version" ]; then
  echo 'Staged colour data root does not match its verified release manifest.' >&2
  exit 67
fi

active_colour=""
if [ ! -e "$active_file" ]; then
  echo 'Active-colour marker is required before promotion.' >&2
  exit 69
fi
if [ ! -r "$active_file" ]; then
  echo 'Active-colour marker exists but is not readable.' >&2
  exit 69
fi
active_colour="$(cat "$active_file")"
case "$active_colour" in blue|green) ;; *) echo 'Invalid active-colour marker.' >&2; exit 69 ;; esac
if [ "$active_colour" = "$colour" ]; then
  echo "Refusing to promote already-active colour '$colour'." >&2
  exit 69
fi
if [ "$legacy_rollback_compat" -eq 1 ]; then
  # Prove the external Access gate before even starting the legacy image,
  # whose historical secret/network boundaries are intentionally weaker.
  verify_cloudflare_access_restored || exit 70
fi

compose() {
  docker compose --env-file "$colour_env" -f "$colour_config_root/compose.prod.yml" --profile "$colour" "$@"
}

# A successfully promoted colour is stopped once it becomes inactive. Start
# the exact target service from its snapshot before readiness checks so both a
# normal promotion and an emergency rollback can use the same guarded path.
compose up -d --no-deps "$service"
ready=0
attempt=0
while [ "$attempt" -lt 15 ]; do
  if compose exec -T "$service" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/public/v1/health/ready', timeout=5).read()" >/dev/null 2>&1; then
    ready=1
    break
  fi
  attempt=$((attempt + 1))
  sleep 2
done
if [ "$ready" -ne 1 ]; then
  echo "Staged $service is not ready; traffic was not changed." >&2
  exit 70
fi
if ! compose exec -T "$service" python - http://127.0.0.1:8000 --mode internal < "$smoke_script"; then
  echo "Staged $service failed its direct smoke test; traffic was not changed." >&2
  exit 71
fi

previous_colour="$active_colour"
previous_service="public-api-$previous_colour"
previous_env="$runtime_root/colours/$previous_colour.compose.env"
previous_marker="$colour_release_root/$previous_colour"
previous_config_binding="$colour_release_root/$previous_colour-config.json"
if [ ! -r "$previous_env" ] || [ ! -r "$previous_marker" ] || [ ! -r "$previous_config_binding" ]; then
  echo "The active colour $previous_colour has no complete rollback snapshot." >&2
  exit 69
fi
read -r previous_marker_colour previous_marker_sha previous_app_image previous_embedding_image < "$previous_marker"
[ "$previous_marker_colour" = "$previous_colour" ] || {
  echo 'Active rollback marker colour mismatch.' >&2
  exit 69
}
printf '%s\n' "$previous_marker_sha" | grep -Eq '^[0-9a-f]{40}$' || {
  echo 'Active rollback marker has an invalid commit SHA.' >&2
  exit 69
}
if ! is_immutable_image "$previous_app_image" || ! is_immutable_image "$previous_embedding_image"; then
  echo 'Active rollback marker images are not immutable digests.' >&2
  exit 69
fi
validate_config_binding "$previous_config_binding" "$previous_colour" "$previous_marker_sha" || exit 69
previous_config_root="$(jq -r '.root' "$previous_config_binding")"

previous_compose() {
  docker compose --env-file "$previous_env" -f "$previous_config_root/compose.prod.yml" \
    --profile "$previous_colour" "$@"
}

target_has_mailer=0
target_services="$(compose config --services)" || {
  echo 'Target configuration snapshot could not be rendered.' >&2
  exit 69
}
if printf '%s\n' "$target_services" | grep -Fx feedback-mailer >/dev/null; then
  target_has_mailer=1
fi
previous_has_mailer=0
previous_services="$(previous_compose config --services)" || {
  echo 'Previous configuration snapshot could not be rendered.' >&2
  exit 69
}
if printf '%s\n' "$previous_services" | grep -Fx feedback-mailer >/dev/null; then
  previous_has_mailer=1
fi
switch_edge() {
  edge_env="$1"
  edge_config_root="$2"
  target_colour="$3"
  SNOW_UPSTREAM="public-api-$target_colour:8000" \
    docker compose --env-file "$edge_env" -f "$edge_config_root/compose.prod.yml" --profile "$target_colour" \
    up -d --no-deps --force-recreate caddy cloudflared egress-proxy
}

# Prepare all marker files before changing traffic. The final renames are
# same-filesystem operations and leave the previous colour available.
state_tmp="$(mktemp "$runtime_root/compose.env.promote.XXXXXX")"
active_tmp="$(mktemp "$active_file.promote.XXXXXX")"
manifest_tmp="$(mktemp "$current_manifest.promote.XXXXXX")"
config_tmp="$(mktemp "$current_config_binding.promote.XXXXXX")"
cleanup() {
  rm -f "${state_tmp:-}" "${active_tmp:-}" "${manifest_tmp:-}" "${config_tmp:-}"
}
trap cleanup EXIT HUP INT TERM
cp "$colour_env" "$state_tmp"
chmod 0600 "$state_tmp"
printf '%s\n' "$colour" > "$active_tmp"
chmod 0600 "$active_tmp"
if [ -r "$colour_manifest" ]; then
  cp "$colour_manifest" "$manifest_tmp"
  chmod 0600 "$manifest_tmp"
else
  rm -f "$manifest_tmp"
  manifest_tmp=""
fi
cp "$colour_config_binding" "$config_tmp"
chmod 0600 "$config_tmp"

restore_previous_runtime() {
  restore_failed=0
  if ! previous_compose up -d --no-deps "$previous_service"; then
    echo 'CRITICAL: failed to restart the previous public API.' >&2
    restore_failed=1
  fi
  previous_ready=0
  restore_attempt=0
  while [ "$restore_failed" -eq 0 ] && [ "$restore_attempt" -lt 15 ]; do
    if previous_compose exec -T "$previous_service" python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/public/v1/health/ready', timeout=5).read()" \
      >/dev/null 2>&1; then
      previous_ready=1
      break
    fi
    restore_attempt=$((restore_attempt + 1))
    sleep 2
  done
  if [ "$previous_ready" -ne 1 ]; then
    echo 'CRITICAL: previous public API did not become ready for restoration.' >&2
    restore_failed=1
  elif ! switch_edge "$previous_env" "$previous_config_root" "$previous_colour"; then
    echo 'CRITICAL: failed to restore the previous edge configuration snapshot.' >&2
    restore_failed=1
  fi
  if [ "$previous_has_mailer" -eq 1 ]; then
    if ! previous_compose up -d --no-deps feedback-mailer; then
      echo 'CRITICAL: failed to restore the previous feedback mailer.' >&2
      restore_failed=1
    fi
  elif [ "$target_has_mailer" -eq 1 ]; then
    if ! compose rm -sf feedback-mailer >/dev/null; then
      echo 'CRITICAL: failed to remove the target-only feedback mailer.' >&2
      restore_failed=1
    fi
  fi
  [ "$restore_failed" -eq 0 ]
}

if ! switch_edge "$colour_env" "$colour_config_root" "$colour"; then
  echo 'Edge switch failed; restoring the previous edge configuration snapshot.' >&2
  restore_previous_runtime || exit 74
  exit 72
fi
post_switch_smoke_ok=1
if [ "$legacy_rollback_compat" -eq 1 ]; then
  compose exec -T "$service" python - http://caddy:8080 --mode public \
    --allow-private-health < "$smoke_script" || post_switch_smoke_ok=0
else
  compose exec -T "$service" python - http://caddy:8080 --mode public \
    < "$smoke_script" || post_switch_smoke_ok=0
fi
if [ "$post_switch_smoke_ok" -ne 1 ]; then
  echo 'Post-switch smoke failed; restoring the previous edge configuration snapshot.' >&2
  restore_previous_runtime || exit 74
  exit 72
fi

# Receipt-only email delivery is deliberately isolated from the public API.
# Reconcile it only after post-switch smoke. Older rollback snapshots may not
# define the service; in that case stop and remove the newer worker rather than
# making an otherwise valid application rollback fail on an unknown service.
mailer_transition_ok=1
if [ "$target_has_mailer" -eq 1 ]; then
  compose up -d --no-deps feedback-mailer || mailer_transition_ok=0
elif [ "$previous_has_mailer" -eq 1 ]; then
  previous_compose stop feedback-mailer || mailer_transition_ok=0
  if [ "$mailer_transition_ok" -eq 1 ]; then
    previous_compose rm -f feedback-mailer || mailer_transition_ok=0
  fi
fi
if [ "$mailer_transition_ok" -ne 1 ]; then
  echo 'Feedback mailer transition failed; restoring the previous release runtime.' >&2
  restore_previous_runtime || exit 74
  exit 73
fi

# The inactive API is stopped after every target gate passes. This is
# especially important for the one legacy rollback colour, whose historical
# Compose snapshot joined a directly routed outbound bridge. A future rollback
# restarts it above before any traffic can be switched.
if ! previous_compose stop "$previous_service"; then
  echo 'Failed to stop the previous public API; restoring the previous release runtime.' >&2
  restore_previous_runtime || exit 74
  exit 73
fi

mv -f "$state_tmp" "$current_env"
state_tmp=""
mv -f "$active_tmp" "$active_file"
active_tmp=""
if [ -n "$manifest_tmp" ]; then
  mv -f "$manifest_tmp" "$current_manifest"
  manifest_tmp=""
fi
mv -f "$config_tmp" "$current_config_binding"
config_tmp=""
printf '%s\n' "$colour $marker_sha $marker_app_image $marker_embedding_image" > /srv/project-snow/releases/current
chmod 0600 /srv/project-snow/releases/current

printf '%s\n' "Promoted $colour $marker_sha. Cloudflare Access and MyWebsite settings were not changed."
