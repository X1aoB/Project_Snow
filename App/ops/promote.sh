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

# Keep this gate inside the target checkout as well as in the root release
# runner.  The predecessor runner may survive a power loss after checking out
# a newer controller but before atomically installing that controller's runner;
# in that state it must not be able to dispatch this script to switch traffic.
release_repository=/srv/project-snow/repo
release_runner_path=/usr/local/sbin/project-snow-release

require_runner_controller_binding() {
  [ -d "$release_repository/.git" ] && [ ! -L "$release_repository" ] &&
    [ ! -L "$release_repository/.git" ] || return 1
  [ -f "$release_runner_path" ] && [ ! -L "$release_runner_path" ] || return 1
  release_runner_metadata="$(stat -c %u:%g:%a:%h "$release_runner_path" 2>/dev/null)" || return 1
  [ "$release_runner_metadata" = 0:0:755:1 ] || return 1

  release_controller_sha="$(git -C "$release_repository" rev-parse --verify HEAD 2>/dev/null)" || return 1
  printf '%s\n' "$release_controller_sha" | grep -Eq '^[0-9a-f]{40}$' || return 1
  release_controller_entry="$(git -C "$release_repository" ls-tree \
    "$release_controller_sha" -- App/ops/project-snow-release 2>/dev/null)" || return 1
  set -- $release_controller_entry
  [ "$#" -eq 4 ] && [ "$1" = 100755 ] && [ "$2" = blob ] &&
    [ "$4" = App/ops/project-snow-release ] || return 1
  release_controller_runner_blob="$3"
  printf '%s\n' "$release_controller_runner_blob" | grep -Eq '^[0-9a-f]{40}$' || return 1
  release_installed_runner_blob="$(git -C "$release_repository" hash-object --no-filters \
    "$release_runner_path" 2>/dev/null)" || return 1
  [ "$release_installed_runner_blob" = "$release_controller_runner_blob" ] || return 1
}

require_runner_controller_binding || {
  echo 'Installed release runner is not bound to the checked-out controller; repeat exact stage before switching traffic.' >&2
  exit 78
}

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

fsync_promote_path() {
  python3 - "$1" <<'PY'
import os
import sys

path = sys.argv[1]
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
if os.path.isdir(path):
    flags |= getattr(os, "O_DIRECTORY", 0)
descriptor = os.open(path, flags)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
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
release_current="/srv/project-snow/releases/current"
live_origin_edge_binding="/srv/project-snow/releases/live-origin-edge-config.json"
live_origin_edge_env_root="$runtime_root/origin-edge"
service="public-api-$colour"
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
smoke_script="$script_dir/../infra/public_smoke.py"
[ -r "$smoke_script" ] || {
  echo 'The trusted release smoke script is missing.' >&2
  exit 66
}

base_configuration_paths="compose.prod.yml
infra/Caddyfile
infra/egress-squid.conf
infra/neo4j-entrypoint.sh
infra/postgres/postgresql.conf
infra/public-api.Dockerfile
requirements-public.txt"
legacy_direct_origin_configuration_paths="infra/OriginEdge.Caddyfile
scripts/cloudflare_origin_firewall.py
ops/project-snow-origin-firewall.service
ops/project-snow-origin-firewall.timer"
direct_origin_configuration_paths="infra/OriginEdge.Caddyfile
config/origin-edge/origin-cert.pem
config/origin-edge/aop-ca.pem
scripts/install_origin_tls.py
scripts/cloudflare_origin_firewall.py
ops/project-snow-origin-firewall.service
ops/project-snow-origin-firewall.timer"

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
    4)
      for legacy_direct_origin_path in $legacy_direct_origin_configuration_paths; do
        [ -f "$paths_root/$legacy_direct_origin_path" ] &&
          [ ! -L "$paths_root/$legacy_direct_origin_path" ] || {
            echo "Legacy direct-origin configuration snapshot is incomplete under $paths_root." >&2
            return 1
          }
      done
      printf '%s\n' "$legacy_direct_origin_configuration_paths"
      ;;
    7) printf '%s\n' "$direct_origin_configuration_paths" ;;
    *) echo "Direct-origin configuration snapshot is incomplete under $paths_root." >&2; return 1 ;;
  esac
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
    printf '%s\n' "$expected_digest" | grep -Eq '^[0-9a-f]{64}$' || return 1
    [ -f "$expected_root/$binding_path" ] && [ ! -L "$expected_root/$binding_path" ] || return 1
    actual_digest="$(sha256sum "$expected_root/$binding_path" | awk '{print $1}')"
    [ "$actual_digest" = "$expected_digest" ] || {
      echo "Configuration snapshot hash mismatch for $binding_path." >&2
      return 1
    }
  done
}

validate_live_origin_edge_binding() {
  live_binding_file="$1"
  [ -f "$live_binding_file" ] && [ ! -L "$live_binding_file" ] &&
    [ "$(stat -c %u:%g:%a:%h "$live_binding_file" 2>/dev/null)" = 0:0:600:1 ] || {
      echo 'Live origin-edge binding must be a root-owned mode-0600 single regular file.' >&2
      return 1
    }
  jq -e '
    type == "object" and
    ((keys | sort) == ([
      "colour", "commit_sha", "configuration_sha256",
      "live_origin_edge_schema", "origin_edge_env_path",
      "origin_edge_env_sha256", "root", "schema_version"
    ] | sort)) and
    .schema_version == "project-snow-config-snapshot-1" and
    .live_origin_edge_schema == "project-snow-live-origin-edge-1" and
    (.colour == "blue" or .colour == "green") and
    (.commit_sha | type == "string" and test("^[0-9a-f]{40}$")) and
    (.origin_edge_env_path | type == "string" and
      test("^/srv/project-snow/runtime/origin-edge/[0-9a-f]{64}\\.compose\\.env$")) and
    (.origin_edge_env_sha256 | type == "string" and test("^[0-9a-f]{64}$"))
  ' "$live_binding_file" >/dev/null || {
    echo 'Live origin-edge binding has an invalid schema or identity.' >&2
    return 1
  }
  live_binding_colour="$(jq -r '.colour' "$live_binding_file")" || return 1
  live_binding_sha="$(jq -r '.commit_sha' "$live_binding_file")" || return 1
  live_binding_config_root="$(jq -r '.root' "$live_binding_file")" || return 1
  live_binding_env="$(jq -r '.origin_edge_env_path' "$live_binding_file")" || return 1
  live_binding_env_sha="$(jq -r '.origin_edge_env_sha256' "$live_binding_file")" || return 1
  validate_config_binding "$live_binding_file" "$live_binding_colour" \
    "$live_binding_sha" || return 1
  [ "$live_binding_config_root" = "$configuration_release_root/$live_binding_sha" ] || return 1
  [ -d "$live_origin_edge_env_root" ] && [ ! -L "$live_origin_edge_env_root" ] &&
    [ "$(stat -c %u:%g:%a "$live_origin_edge_env_root" 2>/dev/null)" = 0:0:700 ] || {
      echo 'Live origin-edge environment root must be a root-owned mode-0700 directory.' >&2
      return 1
    }
  [ -f "$live_binding_env" ] && [ ! -L "$live_binding_env" ] &&
    [ "$(stat -c %u:%g:%a:%h "$live_binding_env" 2>/dev/null)" = 0:0:600:1 ] || {
      echo 'Live origin-edge environment must be a root-owned mode-0600 single regular file.' >&2
      return 1
    }
  live_binding_env_basename="${live_binding_env##*/}"
  [ "$live_binding_env_basename" = "$live_binding_env_sha.compose.env" ] &&
    [ "$(sha256sum "$live_binding_env" | awk '{print $1}')" = "$live_binding_env_sha" ] || {
      echo 'Live origin-edge environment hash does not match its immutable binding.' >&2
      return 1
    }
}

load_live_origin_edge_binding() {
  live_origin_edge_binding_loaded=0
  if [ ! -e "$live_origin_edge_binding" ] && [ ! -L "$live_origin_edge_binding" ]; then
    return 0
  fi
  validate_live_origin_edge_binding "$live_origin_edge_binding" || return 1
  origin_edge_retained_env="$live_binding_env"
  origin_edge_retained_config_root="$live_binding_config_root"
  origin_edge_retained_colour="$live_binding_colour"
  live_origin_edge_binding_loaded=1
}

origin_context_config_binding() {
  context_env="$1"
  context_config_root="$2"
  context_colour="$3"
  if [ "$context_config_root" = "$colour_config_root" ] &&
     [ "$context_colour" = "$colour" ]; then
    printf '%s\n' "$colour_config_binding"
    return 0
  fi
  if [ "$context_config_root" = "$previous_config_root" ] &&
     [ "$context_colour" = "$previous_colour" ]; then
    printf '%s\n' "$previous_config_binding"
    return 0
  fi
  if [ -n "$origin_edge_replacement_restore_binding" ] &&
     [ "$context_config_root" = "$origin_edge_replacement_restore_config_root" ] &&
     [ "$context_colour" = "$origin_edge_replacement_restore_colour" ]; then
    printf '%s\n' "$origin_edge_replacement_restore_binding"
    return 0
  fi
  if [ "$live_origin_edge_binding_loaded" -eq 1 ] &&
     [ "$context_env" = "$origin_edge_retained_env" ] &&
     [ "$context_config_root" = "$origin_edge_retained_config_root" ] &&
     [ "$context_colour" = "$origin_edge_retained_colour" ]; then
    printf '%s\n' "$live_origin_edge_binding"
    return 0
  fi
  echo 'No exact immutable configuration binding exists for the running origin-edge.' >&2
  return 1
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
  cleanup_access_probe() {
    access_probe_status=$?
    trap - EXIT HUP INT QUIT TERM PIPE
    rm -f -- "$probe_headers" || true
    exit "$access_probe_status"
  }
  trap cleanup_access_probe EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 131' QUIT
  trap 'exit 143' TERM
  trap 'exit 141' PIPE
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
  colour_configuration_paths="$(configuration_paths_for_root "$colour_config_root")" || exit 67
  colour_configuration_count="$(printf '%s\n' "$colour_configuration_paths" | awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$manifest_config_count" -eq "$colour_configuration_count" ] || {
    echo 'Staged release manifest has an incomplete configuration hash set.' >&2
    exit 67
  }
  for manifest_config_path in $colour_configuration_paths; do
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

service_list_has() {
  printf '%s\n' "$1" | grep -Fx "$2" >/dev/null
}

validate_edge_service_list() {
  edge_service_list="$1"
  edge_service_label="$2"
  for required_edge_service in caddy egress-proxy; do
    service_list_has "$edge_service_list" "$required_edge_service" || {
      echo "$edge_service_label configuration has no $required_edge_service service." >&2
      return 1
    }
  done
  if ! service_list_has "$edge_service_list" origin-edge &&
     ! service_list_has "$edge_service_list" cloudflared; then
    echo "$edge_service_label configuration has no supported edge ingress service." >&2
    return 1
  fi
}

validate_edge_service_list "$target_services" Target || exit 69
validate_edge_service_list "$previous_services" Previous || exit 69
target_has_origin_edge=0
target_has_cloudflared=0
previous_has_origin_edge=0
previous_has_cloudflared=0
service_list_has "$target_services" origin-edge && target_has_origin_edge=1
service_list_has "$target_services" cloudflared && target_has_cloudflared=1
service_list_has "$previous_services" origin-edge && previous_has_origin_edge=1
service_list_has "$previous_services" cloudflared && previous_has_cloudflared=1
if [ "$target_has_origin_edge" -eq 1 ] && [ "$target_has_cloudflared" -ne 1 ]; then
  echo 'A direct-origin target must retain cloudflared until a separately authorized edge migration.' >&2
  exit 69
fi

origin_edge_replacement_active=0
origin_edge_replacement_restore_env=""
origin_edge_replacement_restore_config_root=""
origin_edge_replacement_restore_colour=""
origin_edge_replacement_restore_binding=""
origin_edge_replacement_target_env=""
origin_edge_replacement_target_config_root=""
origin_edge_replacement_target_colour=""
origin_edge_overlay_env=""
origin_edge_overlay_config_root=""
origin_edge_overlay_colour=""
origin_edge_retained_env=""
origin_edge_retained_config_root=""
origin_edge_retained_colour=""
origin_edge_prestart_failure_preserve=0
origin_edge_tunnel_retain=0
promote_signal_phase=pre-switch
promote_signal_handling=0

require_running_cloudflared_fallback() {
  fallback_tunnel_ids="$(docker ps \
    --filter label=com.docker.compose.project=project-snow-public \
    --filter label=com.docker.compose.service=cloudflared \
    --filter status=running --quiet)" || return 1
  fallback_tunnel_count="$(printf '%s\n' "$fallback_tunnel_ids" |
    awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$fallback_tunnel_count" -eq 1 ] || {
    echo 'A controlled origin-edge replacement requires exactly one running Tunnel fallback.' >&2
    return 1
  }
  fallback_tunnel_id="$(printf '%s\n' "$fallback_tunnel_ids" | awk 'NF { print; exit }')"
  fallback_tunnel_document="$(docker inspect "$fallback_tunnel_id")" || return 1
  printf '%s\n' "$fallback_tunnel_document" | jq -e '
    length == 1 and
    .[0].State.Running == true and
    .[0].State.Paused == false and
    .[0].State.Restarting == false and
    .[0].State.Dead == false and
    .[0].Config.Labels["com.docker.compose.project"] == "project-snow-public" and
    .[0].Config.Labels["com.docker.compose.service"] == "cloudflared" and
    (.[0].Config.Image | test("@sha256:[0-9a-f]{64}$")) and
    .[0].Config.Cmd == ["tunnel", "--no-autoupdate", "--config", "/etc/cloudflared/config.yml", "run"] and
    (((.[0].NetworkSettings.Networks // {}) | keys | sort) ==
      (["project-snow-public_edge-client", "project-snow-public_tunnel-uplink"] | sort)) and
    ((.[0].Mounts // []) |
      map({Type, Source, Destination, RW}) | sort_by(.Destination)) ==
      ([
        {Type: "bind", Source: "/etc/project-snow/cloudflared/config.yml", Destination: "/etc/cloudflared/config.yml", RW: false},
        {Type: "bind", Source: "/etc/project-snow/cloudflared/credentials.json", Destination: "/etc/cloudflared/credentials.json", RW: false}
      ] | sort_by(.Destination))
  ' >/dev/null || {
    echo 'The running Tunnel fallback violates its exact identity, mount or network boundary.' >&2
    return 1
  }
}

origin_firewall_binary=/usr/local/sbin/project-snow-origin-firewall
run_origin_firewall() {
  firewall_action="$1"
  [ -f "$origin_firewall_binary" ] && [ ! -L "$origin_firewall_binary" ] &&
    [ -x "$origin_firewall_binary" ] &&
    [ "$(stat -c %u:%a:%h "$origin_firewall_binary")" = 0:755:1 ] || {
      echo 'Origin firewall helper must be a root-owned mode-0755 single regular file.' >&2
      return 1
    }
  "$origin_firewall_binary" "$firewall_action"
}

origin_tls_root_for_env() {
  origin_env="$1"
  origin_config_root="$2"
  [ -f "$origin_env" ] && [ ! -L "$origin_env" ] || {
    echo 'Origin TLS environment binding is missing or unsafe.' >&2
    return 1
  }
  origin_tls_root_count="$(sed -n '/^ORIGIN_TLS_ROOT=/p' "$origin_env" | awk 'END { print NR + 0 }')"
  case "$origin_tls_root_count" in
    1)
      origin_tls_root="$(sed -n 's/^ORIGIN_TLS_ROOT=//p' "$origin_env")"
      printf '%s\n' "$origin_tls_root" |
        grep -Eq '^/etc/project-snow/origin-edge/releases/[0-9a-f]{64}$' || {
          echo 'Origin TLS environment root is not an immutable managed path.' >&2
          return 1
        }
      ;;
    0)
      legacy_origin_compose="$origin_config_root/compose.prod.yml"
      [ -f "$legacy_origin_compose" ] && [ ! -L "$legacy_origin_compose" ] || return 1
      grep -Fq -- '- /etc/project-snow/origin-edge/origin-cert.pem:/run/project-snow-origin/origin-cert.pem:ro' \
        "$legacy_origin_compose" &&
        grep -Fq -- '- /etc/project-snow/origin-edge/origin-key.pem:/run/project-snow-origin/origin-key.pem:ro' \
          "$legacy_origin_compose" &&
        grep -Fq -- '- /etc/project-snow/origin-edge/aop-ca.pem:/run/project-snow-origin/aop-ca.pem:ro' \
          "$legacy_origin_compose" &&
        ! grep -Fq 'ORIGIN_TLS_ROOT' "$legacy_origin_compose" || {
          echo 'Legacy origin TLS mounts are not the exact fixed read-only layout.' >&2
          return 1
        }
      origin_tls_root=/etc/project-snow/origin-edge
      ;;
    *)
      echo 'Origin TLS environment contains more than one root binding.' >&2
      return 1
      ;;
  esac
  printf '%s\n' "$origin_tls_root"
}

validate_origin_edge_material() {
  origin_material_env="$1"
  origin_material_config_root="$2"
  origin_tls_root="$(origin_tls_root_for_env "$origin_material_env" \
    "$origin_material_config_root")" || return 1
  [ -d "$origin_tls_root" ] && [ ! -L "$origin_tls_root" ] &&
    [ "$(stat -c %u:%g:%a "$origin_tls_root")" = 0:0:700 ] || {
      echo 'Origin TLS bundle directory must be root-owned with mode 0700.' >&2
      return 1
    }
  origin_material_paths="$origin_tls_root/origin-cert.pem
$origin_tls_root/origin-key.pem
$origin_tls_root/aop-ca.pem"
  case "$origin_tls_root" in
    /etc/project-snow/origin-edge/releases/*)
      origin_material_paths="$origin_material_paths
    $origin_tls_root/metadata.json"
      ;;
  esac
  for origin_material in $origin_material_paths; do
    [ -f "$origin_material" ] && [ ! -L "$origin_material" ] &&
      [ -s "$origin_material" ] &&
      [ "$(stat -c %u:%a:%h "$origin_material")" = 0:400:1 ] || {
        echo "Origin TLS material must be a root-owned mode-0400 single regular file: $origin_material" >&2
        return 1
      }
  done
}

origin_edge_network=ps-origin0
origin_edge_internal_network=ps-origin1

validate_docker_dns_security_floor() {
  docker_server_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null)" || {
    echo 'Docker Engine version could not be inspected for the origin DNS boundary.' >&2
    return 1
  }
  printf '%s\n' "$docker_server_version" | awk -F. '
    NF == 3 &&
    $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ &&
    $1 >= 26 {
      supported = 1
    }
    END { exit(supported ? 0 : 1) }
  ' || {
    echo 'Docker Engine must be a stable numeric release at or above 26.0.0 for the origin DNS boundary.' >&2
    return 1
  }
}

validate_origin_edge_network() {
  origin_network_document="$(docker network inspect "$origin_edge_network")" || {
    echo "Origin edge network could not be inspected: $origin_edge_network" >&2
    return 1
  }
  printf '%s\n' "$origin_network_document" | jq -e \
    --arg network "$origin_edge_network" '
      length == 1 and
      .[0].Name == $network and
      .[0].Driver == "bridge" and
      .[0].Internal == false and
      .[0].EnableIPv6 == false and
      .[0].Options["com.docker.network.bridge.name"] == $network and
      .[0].Labels["com.docker.compose.project"] == "project-snow-public" and
      .[0].Labels["com.docker.compose.network"] == "origin-uplink"
    ' >/dev/null || {
      echo "Origin edge network metadata is not the exact fail-closed policy: $origin_edge_network" >&2
      return 1
    }
  /usr/sbin/ip -json -details link show dev "$origin_edge_network" | jq -e \
    --arg network "$origin_edge_network" '
      length == 1 and
      .[0].ifname == $network and
      .[0].linkinfo.info_kind == "bridge"
    ' >/dev/null || {
      echo "Origin edge Linux bridge is missing or is not a bridge: $origin_edge_network" >&2
      return 1
    }
  origin_internal_network_document="$(docker network inspect "$origin_edge_internal_network")" || {
    echo "Internal edge network could not be inspected: $origin_edge_internal_network" >&2
    return 1
  }
  printf '%s\n' "$origin_internal_network_document" | jq -e \
    --arg network "$origin_edge_internal_network" '
      length == 1 and
      .[0].Name == $network and
      .[0].Driver == "bridge" and
      .[0].Internal == true and
      .[0].EnableIPv6 == false and
      .[0].Options["com.docker.network.bridge.name"] == $network and
      .[0].Labels["com.docker.compose.project"] == "project-snow-public" and
      .[0].Labels["com.docker.compose.network"] == "origin-backend"
    ' >/dev/null || {
      echo "Internal edge network metadata is not the exact isolated policy: $origin_edge_internal_network" >&2
      return 1
    }
  /usr/sbin/ip -json -details link show dev "$origin_edge_internal_network" | jq -e \
    --arg network "$origin_edge_internal_network" '
      length == 1 and
      .[0].ifname == $network and
      .[0].linkinfo.info_kind == "bridge"
    ' >/dev/null || {
      echo "Origin backend Linux bridge is missing or is not a bridge: $origin_edge_internal_network" >&2
      return 1
    }
}

resolve_running_origin_caddy() {
  caddy_env="$1"
  caddy_config_root="$2"
  caddy_colour="$3"
  origin_caddy_ids="$(
    SNOW_UPSTREAM="public-api-$caddy_colour:8000" \
      docker compose --env-file "$caddy_env" -f "$caddy_config_root/compose.prod.yml" \
        --profile "$caddy_colour" ps --quiet caddy
  )" || return 1
  origin_caddy_count="$(printf '%s\n' "$origin_caddy_ids" | awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$origin_caddy_count" -eq 1 ] || {
    echo 'Expected exactly one running Caddy container for the origin backend.' >&2
    return 1
  }
  origin_caddy_container_id="$(printf '%s\n' "$origin_caddy_ids" | awk 'NF { print; exit }')"
  docker inspect "$origin_caddy_container_id" | jq -e '
    length == 1 and
    .[0].State.Running == true and
    .[0].State.Paused == false and
    .[0].Config.Labels["com.docker.compose.project"] == "project-snow-public" and
    .[0].Config.Labels["com.docker.compose.service"] == "caddy"
  ' >/dev/null || {
    echo 'Caddy container identity or runtime state is invalid for the origin backend.' >&2
    return 1
  }
}

ensure_caddy_origin_backend() {
  backend_env="$1"
  backend_config_root="$2"
  backend_colour="$3"
  validate_origin_edge_network || return 1
  resolve_running_origin_caddy "$backend_env" "$backend_config_root" "$backend_colour" || return 1
  caddy_backend_attached="$(docker inspect "$origin_caddy_container_id" | jq -r \
    --arg network "$origin_edge_internal_network" \
    'if .[0].NetworkSettings.Networks[$network] then "true" else "false" end')" || return 1
  if [ "$caddy_backend_attached" != true ]; then
    docker network connect --alias caddy "$origin_edge_internal_network" "$origin_caddy_container_id" || {
      echo 'Could not attach Caddy to the dedicated origin backend.' >&2
      return 1
    }
  fi
  docker inspect "$origin_caddy_container_id" | jq -e \
    --arg network "$origin_edge_internal_network" \
    '.[0].NetworkSettings.Networks[$network] as $backend |
     $backend != null and (($backend.Aliases // []) | index("caddy")) != null' >/dev/null || {
      echo 'Caddy is not attached to the dedicated origin backend with its fixed alias.' >&2
      return 1
    }
  backend_origin_ids="$(docker ps --all --no-trunc \
    --filter label=com.docker.compose.project=project-snow-public \
    --filter label=com.docker.compose.service=origin-edge --quiet)" || return 1
  backend_origin_count="$(printf '%s\n' "$backend_origin_ids" | awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$backend_origin_count" -le 1 ] || {
    echo 'More than one origin-edge container exists on the dedicated backend.' >&2
    return 1
  }
  backend_origin_id="$(printf '%s\n' "$backend_origin_ids" | awk 'NF { print; exit }')"
  backend_origin_running=false
  if [ -n "$backend_origin_id" ]; then
    backend_origin_running="$(docker inspect --format '{{.State.Running}}' "$backend_origin_id")" || return 1
    case "$backend_origin_running" in true|false) ;; *) return 1 ;; esac
  fi
  remove_verified_stale_origin_network_probes || return 1
  docker network inspect "$origin_edge_internal_network" | jq -e \
    --arg caddy_id "$origin_caddy_container_id" \
    --arg origin_id "$backend_origin_id" \
    --argjson origin_running "$backend_origin_running" '
      length == 1 and
      ((.[0].Containers // {} | keys | sort) as $endpoints |
       if $origin_id == "" then
         $endpoints == [$caddy_id]
       elif $origin_running then
         $endpoints == ([$caddy_id, $origin_id] | sort)
       else
         ($endpoints == [$caddy_id] or
          $endpoints == ([$caddy_id, $origin_id] | sort))
       end)
    ' >/dev/null || {
      echo 'The dedicated origin backend contains an unexpected endpoint.' >&2
      return 1
    }
}

validate_origin_edge_container() {
  inspected_env="$1"
  inspected_config_root="$2"
  inspected_colour="$3"
  inspected_running="$4"
  inspected_caddy_image_count="$(awk -F= '$1 == "CADDY_IMAGE" { count += 1 } END { print count + 0 }' \
    "$inspected_env")" || return 1
  [ "$inspected_caddy_image_count" -eq 1 ] || {
    echo 'Origin-edge environment must contain exactly one CADDY_IMAGE binding.' >&2
    return 1
  }
  inspected_caddy_image="$(sed -n 's/^CADDY_IMAGE=//p' "$inspected_env")" || return 1
  is_immutable_image "$inspected_caddy_image" || {
    echo 'Origin-edge Caddy image is not an immutable digest.' >&2
    return 1
  }
  inspected_tls_root="$(origin_tls_root_for_env "$inspected_env" \
    "$inspected_config_root")" || return 1
  validate_origin_edge_network || return 1
  resolve_running_origin_caddy "$inspected_env" "$inspected_config_root" "$inspected_colour" || return 1
  inspected_container_ids="$(
    SNOW_UPSTREAM="public-api-$inspected_colour:8000" \
      docker compose --env-file "$inspected_env" -f "$inspected_config_root/compose.prod.yml" \
        --profile "$inspected_colour" ps --all --quiet origin-edge
  )" || {
    echo 'Origin-edge container could not be resolved.' >&2
    return 1
  }
  inspected_container_count="$(printf '%s\n' "$inspected_container_ids" | awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$inspected_container_count" -eq 1 ] || {
    echo 'Expected exactly one origin-edge container.' >&2
    return 1
  }
  inspected_container_id="$(printf '%s\n' "$inspected_container_ids" | awk 'NF { print; exit }')"
  inspected_container_document="$(docker inspect "$inspected_container_id")" || {
    echo 'Origin-edge container could not be inspected.' >&2
    return 1
  }
  printf '%s\n' "$inspected_container_document" | jq -e \
    --arg project project-snow-public \
    --arg internal_network "$origin_edge_internal_network" \
    --arg uplink_network "$origin_edge_network" \
    --arg tls_root "$inspected_tls_root" \
    --arg caddy_image "$inspected_caddy_image" \
    --arg caddy_config "$inspected_config_root/infra/OriginEdge.Caddyfile" \
    --argjson expected_running "$inspected_running" '
      length == 1 and
      .[0].State.Running == $expected_running and
      .[0].State.Paused == false and
      .[0].State.Restarting == false and
      (.[0].State.Error // "") == "" and
      ($expected_running == false or .[0].State.Status == "running") and
      .[0].Config.Labels["com.docker.compose.project"] == $project and
      .[0].Config.Labels["com.docker.compose.service"] == "origin-edge" and
      .[0].Config.Image == $caddy_image and
      .[0].Config.Entrypoint == null and
      .[0].Config.Cmd == ["caddy", "run", "--config", "/etc/caddy/OriginEdge.Caddyfile", "--adapter", "caddyfile"] and
      (((.[0].NetworkSettings.Networks // {}) | keys | sort) ==
        ([$internal_network, $uplink_network] | sort)) and
      ((.[0].HostConfig.PortBindings // {}) | keys) == ["8443/tcp"] and
      .[0].HostConfig.Dns == ["127.0.0.1"] and
      ((.[0].HostConfig.PortBindings["8443/tcp"] // []) | length) == 1 and
      .[0].HostConfig.PortBindings["8443/tcp"][0].HostPort == "443" and
      (.[0].HostConfig.PortBindings["8443/tcp"][0].HostIp == "" or
       .[0].HostConfig.PortBindings["8443/tcp"][0].HostIp == "0.0.0.0" or
       .[0].HostConfig.PortBindings["8443/tcp"][0].HostIp == "::") and
      ($expected_running == false or
       ([((.[0].NetworkSettings.Ports // {}) | to_entries[]) | select(.value != null)] as $published |
        ($published | length) == 1 and
        $published[0].key == "8443/tcp" and
        ($published[0].value | length) >= 1 and
        all($published[0].value[];
          .HostPort == "443" and
          (.HostIp == "" or .HostIp == "0.0.0.0" or .HostIp == "::")))) and
      .[0].HostConfig.ReadonlyRootfs == true and
      ((.[0].HostConfig.CapDrop // []) | index("ALL")) != null and
      (((.[0].HostConfig.CapAdd // []) | sort) == ["NET_BIND_SERVICE"] or
       ((.[0].HostConfig.CapAdd // []) | sort) == ["CAP_NET_BIND_SERVICE"]) and
      ((.[0].HostConfig.SecurityOpt // []) | index("no-new-privileges:true")) != null and
      ((.[0].Mounts // []) |
        map(select(.Type == "bind" or .Type == "volume")) |
        map({Type, Source, Destination, RW}) |
        sort_by(.Destination)) ==
        ([
          {Type: "bind", Source: $caddy_config, Destination: "/etc/caddy/OriginEdge.Caddyfile", RW: false},
          {Type: "bind", Source: ($tls_root + "/aop-ca.pem"), Destination: "/run/project-snow-origin/aop-ca.pem", RW: false},
          {Type: "bind", Source: ($tls_root + "/origin-cert.pem"), Destination: "/run/project-snow-origin/origin-cert.pem", RW: false},
          {Type: "bind", Source: ($tls_root + "/origin-key.pem"), Destination: "/run/project-snow-origin/origin-key.pem", RW: false}
        ] | sort_by(.Destination))
    ' >/dev/null || {
      echo 'Origin-edge container violates its expected state, exact network, port or hardening policy.' >&2
      return 1
    }
  inspected_origin_image_id="$(printf '%s\n' "$inspected_container_document" | jq -er \
    '.[0].Image | select(type == "string" and test("^sha256:[0-9a-f]{64}$"))')" || {
      echo 'Origin-edge image identity is not an immutable local image ID.' >&2
      return 1
    }
  printf '%s\n' "$origin_network_document" | jq -e \
    --arg container_id "$inspected_container_id" \
    --argjson expected_running "$inspected_running" '
      length == 1 and
      ((.[0].Containers // {}) as $containers |
       if $expected_running then
         ($containers | length) == 1 and ($containers | has($container_id))
       else
         (($containers | length) == 0 or
          (($containers | length) == 1 and ($containers | has($container_id))))
       end)
    ' >/dev/null || {
      echo 'Origin uplink contains an unexpected container endpoint.' >&2
      return 1
    }
  printf '%s\n' "$origin_internal_network_document" | jq -e \
    --arg container_id "$inspected_container_id" \
    --arg caddy_id "$origin_caddy_container_id" \
    --argjson expected_running "$inspected_running" '
      length == 1 and
      ((.[0].Containers // {}) as $containers |
       if $expected_running then
         (($containers | keys | sort) == ([$container_id, $caddy_id] | sort))
       else
         ((($containers | keys | sort) == [$caddy_id]) or
          (($containers | keys | sort) == ([$container_id, $caddy_id] | sort)))
       end)
    ' >/dev/null || {
      echo 'Origin backend contains an unexpected container endpoint.' >&2
      return 1
    }
}

print_origin_edge_runtime_diagnostics() {
  diagnostic_env="$1"
  diagnostic_config_root="$2"
  diagnostic_colour="$3"
  diagnostic_container_ids="$(
    SNOW_UPSTREAM="public-api-$diagnostic_colour:8000" \
      docker compose --env-file "$diagnostic_env" \
        -f "$diagnostic_config_root/compose.prod.yml" \
        --profile "$diagnostic_colour" ps --all --quiet origin-edge 2>/dev/null
  )" || {
    echo 'Origin-edge runtime diagnostics are unavailable.' >&2
    return 0
  }
  diagnostic_container_count="$(printf '%s\n' "$diagnostic_container_ids" |
    awk 'NF { count += 1 } END { print count + 0 }')"
  if [ "$diagnostic_container_count" -ne 1 ]; then
    printf '%s\n' "Origin-edge runtime diagnostics: {\"container_count\":$diagnostic_container_count}" >&2
    return 0
  fi
  diagnostic_container_id="$(printf '%s\n' "$diagnostic_container_ids" |
    awk 'NF { print; exit }')"
  diagnostic_document="$(docker inspect "$diagnostic_container_id" 2>/dev/null)" || {
    echo 'Origin-edge runtime diagnostics are unavailable.' >&2
    return 0
  }
  diagnostic_caddy_image="$(sed -n 's/^CADDY_IMAGE=//p' "$diagnostic_env" 2>/dev/null)" || {
    echo 'Origin-edge runtime diagnostics are unavailable.' >&2
    return 0
  }
  diagnostic_tls_root="$(origin_tls_root_for_env "$diagnostic_env" \
    "$diagnostic_config_root" 2>/dev/null)" || {
    echo 'Origin-edge runtime diagnostics are unavailable.' >&2
    return 0
  }
  diagnostic_caddy_config="$diagnostic_config_root/infra/OriginEdge.Caddyfile"
  diagnostic_json="$(printf '%s\n' "$diagnostic_document" | jq -c \
    --arg internal_network "$origin_edge_internal_network" \
    --arg uplink_network "$origin_edge_network" \
    --arg tls_root "$diagnostic_tls_root" \
    --arg caddy_image "$diagnostic_caddy_image" \
    --arg caddy_config "$diagnostic_caddy_config" '
      .[0] as $container |
      (($container.State.Status // "unknown") as $raw_status |
       if (["created", "running", "paused", "restarting", "removing", "exited", "dead"] |
           index($raw_status)) == null then "unknown" else $raw_status end) as $status |
      ([((($container.NetworkSettings.Ports // {}) | to_entries[]) |
          select(.value != null))] // []) as $published |
      {
        status: $status,
        running: ($container.State.Running == true),
        restarting: ($container.State.Restarting == true),
        exit_code: (if ($container.State.ExitCode | type) == "number" then
          $container.State.ExitCode else null end),
        state_error_present: (($container.State.Error // "") != ""),
        restart_count: (if ($container.RestartCount | type) == "number" then
          $container.RestartCount else null end),
        image_exact: ($container.Config.Image == $caddy_image),
        entrypoint_exact: ($container.Config.Entrypoint == null),
        command_exact:
          ($container.Config.Cmd == ["caddy", "run", "--config",
           "/etc/caddy/OriginEdge.Caddyfile", "--adapter", "caddyfile"]),
        declared_port_exact:
          (((($container.HostConfig.PortBindings // {}) | keys) == ["8443/tcp"]) and
           ((($container.HostConfig.PortBindings["8443/tcp"] // []) | length) == 1) and
           $container.HostConfig.PortBindings["8443/tcp"][0].HostPort == "443" and
           ($container.HostConfig.PortBindings["8443/tcp"][0].HostIp == "" or
            $container.HostConfig.PortBindings["8443/tcp"][0].HostIp == "0.0.0.0" or
            $container.HostConfig.PortBindings["8443/tcp"][0].HostIp == "::")),
        dns_exact: ($container.HostConfig.Dns == ["127.0.0.1"]),
        readonly_rootfs: ($container.HostConfig.ReadonlyRootfs == true),
        capabilities_exact:
          (((($container.HostConfig.CapDrop // []) | sort) == ["ALL"]) and
           (((($container.HostConfig.CapAdd // []) | sort) == ["NET_BIND_SERVICE"]) or
            ((($container.HostConfig.CapAdd // []) | sort) == ["CAP_NET_BIND_SERVICE"]))),
        no_new_privileges:
          ((($container.HostConfig.SecurityOpt // []) | index("no-new-privileges:true")) != null),
        mounts_exact:
          (((($container.Mounts // []) |
             map(select(.Type == "bind" or .Type == "volume")) |
             map({Type, Source, Destination, RW}) |
             sort_by(.Destination))) ==
           ([
             {Type: "bind", Source: $caddy_config,
              Destination: "/etc/caddy/OriginEdge.Caddyfile", RW: false},
             {Type: "bind", Source: ($tls_root + "/aop-ca.pem"),
              Destination: "/run/project-snow-origin/aop-ca.pem", RW: false},
             {Type: "bind", Source: ($tls_root + "/origin-cert.pem"),
              Destination: "/run/project-snow-origin/origin-cert.pem", RW: false},
             {Type: "bind", Source: ($tls_root + "/origin-key.pem"),
              Destination: "/run/project-snow-origin/origin-key.pem", RW: false}
           ] | sort_by(.Destination))),
        runtime_networks_exact:
          (((($container.NetworkSettings.Networks // {}) | keys | sort)) ==
           ([$internal_network, $uplink_network] | sort)),
        runtime_port_exact:
          (($published | length) == 1 and
           $published[0].key == "8443/tcp" and
           ($published[0].value | length) >= 1 and
           all($published[0].value[];
             .HostPort == "443" and
             (.HostIp == "" or .HostIp == "0.0.0.0" or .HostIp == "::")))
      }
    ' 2>/dev/null)" || {
      echo 'Origin-edge runtime diagnostics are unavailable.' >&2
      return 0
    }
  [ -n "$diagnostic_json" ] || {
    echo 'Origin-edge runtime diagnostics are unavailable.' >&2
    return 0
  }
  printf '%s\n' "Origin-edge runtime diagnostics: $diagnostic_json" >&2
}

wait_for_origin_edge_running_policy() {
  waiting_env="$1"
  waiting_config_root="$2"
  waiting_colour="$3"
  waiting_attempt=0
  waiting_consecutive=0
  while [ "$waiting_attempt" -lt 10 ]; do
    if validate_origin_edge_container "$waiting_env" "$waiting_config_root" \
      "$waiting_colour" true >/dev/null 2>&1; then
      waiting_consecutive=$((waiting_consecutive + 1))
      if [ "$waiting_consecutive" -ge 2 ]; then
        return 0
      fi
    else
      waiting_consecutive=0
    fi
    waiting_attempt=$((waiting_attempt + 1))
    [ "$waiting_attempt" -ge 10 ] || sleep 1
  done
  print_origin_edge_runtime_diagnostics "$waiting_env" "$waiting_config_root" \
    "$waiting_colour"
  echo 'Origin-edge did not reach its exact running policy within 10 seconds.' >&2
  return 1
}

origin_network_probe_program='
import http.client
import socket
import sys

external_name, origin_gateway, backend_gateway, other_bridge_target = sys.argv[1:5]

try:
    socket.getaddrinfo(external_name, 443)
except socket.gaierror:
    pass
else:
    raise SystemExit(41)

def require_tcp_blocked(host, port, exit_code):
    try:
        connection = socket.create_connection((host, port), timeout=3)
    except OSError:
        return
    connection.close()
    raise SystemExit(exit_code)

require_tcp_blocked("1.1.1.1", 80, 42)
require_tcp_blocked("1.1.1.1", 53, 43)

labels = external_name.encode("ascii").split(b".")
query_name = b"".join(bytes((len(label),)) + label for label in labels) + b"\x00"
query = b"\x50\x53\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + query_name + b"\x00\x01\x00\x01"
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.settimeout(3)
try:
    udp.sendto(query, ("1.1.1.1", 53))
    udp.recvfrom(512)
except OSError:
    pass
else:
    raise SystemExit(44)
finally:
    udp.close()

require_tcp_blocked(origin_gateway, 53, 45)
require_tcp_blocked(backend_gateway, 53, 46)
require_tcp_blocked(other_bridge_target, 8000, 47)

socket.getaddrinfo("caddy", 8080)
connection = http.client.HTTPConnection("caddy", 8080, timeout=5)
connection.request("GET", "/public/v1/health/live", headers={"Host": "snow.xiaob.dev"})
response = connection.getresponse()
response.read(65536)
connection.close()
if response.status != 200:
    raise SystemExit(48)
    '

probe_ipv4_belongs_to_app_network() {
  checked_ipv4="$1"
  shift
  python3 - "$checked_ipv4" "$@" <<'PY'
import ipaddress
import sys

address = ipaddress.ip_address(sys.argv[1])
networks = [ipaddress.ip_network(value, strict=False) for value in sys.argv[2:]]
if address.version != 4 or not networks or not any(address in network for network in networks):
    raise SystemExit(1)
PY
}

validate_stale_origin_network_probe() {
  stale_probe_id="$1"
  stale_probe_document="$(docker inspect "$stale_probe_id")" || return 1
  stale_probe_uplink_gateway="$(printf '%s\n' "$origin_network_document" | jq -er '
    [.[0].IPAM.Config[]?.Gateway // empty |
      select(type == "string" and test("^([0-9]{1,3}\\.){3}[0-9]{1,3}$"))] |
    unique | if length == 1 then .[0] else error("origin gateway is not unique") end
  ')" || return 1
  stale_probe_backend_gateway="$(printf '%s\n' "$origin_internal_network_document" | jq -er '
    [.[0].IPAM.Config[]?.Gateway // empty |
      select(type == "string" and test("^([0-9]{1,3}\\.){3}[0-9]{1,3}$"))] |
    unique | if length == 1 then .[0] else error("origin backend gateway is not unique") end
  ')" || return 1
  stale_probe_identity="$(printf '%s\n' "$stale_probe_document" | jq -er \
    --arg container_id "$stale_probe_id" \
    --arg uplink "$origin_edge_network" \
    --arg backend "$origin_edge_internal_network" \
    --arg uplink_gateway "$stale_probe_uplink_gateway" \
    --arg backend_gateway "$stale_probe_backend_gateway" \
    --arg program "$origin_network_probe_program" '
      (.[0].Name |
        capture("^/project-snow-origin-netprobe-(?<pid>[1-9][0-9]*)$") |
        .pid) as $pid |
      select(
      length == 1 and
      .[0].Id == $container_id and
      (.[0].Name | test("^/project-snow-origin-netprobe-[1-9][0-9]*$")) and
      .[0].State.Running == false and
      .[0].State.Paused == false and
      .[0].State.Restarting == false and
      .[0].State.Dead == false and
      .[0].State.OOMKilled == false and
      .[0].State.Pid == 0 and
      ((.[0].State.Status == "created") or (.[0].State.Status == "exited")) and
      (.[0].State.Error // "") == "" and
      .[0].Config.Entrypoint == ["python"] and
      (.[0].Config.Cmd | length) == 7 and
      .[0].Config.Cmd[0] == "-c" and
      .[0].Config.Cmd[1] == $program and
      .[0].Config.Cmd[2] == "project-snow-origin-netprobe" and
      .[0].Config.Cmd[4] == $uplink_gateway and
      .[0].Config.Cmd[5] == $backend_gateway and
      (.[0].Config.Cmd[3] |
        test("^project-snow-[0-9a-f]{12}-" + $pid + "\\.1-1-1-1\\.sslip\\.io$")) and
      (.[0].Config.Cmd[6] |
        test("^([0-9]{1,3}\\.){3}[0-9]{1,3}$")) and
      .[0].HostConfig.NetworkMode == $uplink and
      .[0].HostConfig.ReadonlyRootfs == true and
      (((.[0].HostConfig.CapDrop // []) | sort) == ["ALL"] or
       ((.[0].HostConfig.CapDrop // []) | sort) == ["CAP_ALL"]) and
      ((.[0].HostConfig.CapAdd // []) | length) == 0 and
      (((.[0].HostConfig.SecurityOpt // []) | sort) == ["no-new-privileges"] or
       ((.[0].HostConfig.SecurityOpt // []) | sort) == ["no-new-privileges:true"]) and
      .[0].HostConfig.Privileged == false and
      .[0].HostConfig.AutoRemove == false and
      .[0].HostConfig.PublishAllPorts == false and
      ((.[0].HostConfig.PortBindings // {}) | length) == 0 and
      .[0].HostConfig.PidsLimit == 32 and
      .[0].HostConfig.Memory == 67108864 and
      .[0].HostConfig.NanoCpus == 250000000 and
      .[0].HostConfig.Dns == ["127.0.0.1"] and
      .[0].HostConfig.Tmpfs == {"/tmp":"rw,nosuid,nodev,size=4m"} and
      ((.[0].NetworkSettings.Networks // {} | keys | sort) as $networks |
        if .[0].State.Status == "created" then
          ($networks == [$uplink] or
           $networks == ([$uplink, $backend] | sort))
        else
          $networks == ([$uplink, $backend] | sort)
        end) and
      ((.[0].Config.Labels // {}) as $labels |
        (($labels["com.project-snow.origin-network-probe"] == null and
          $labels["com.project-snow.origin-network-probe-schema"] == null and
          $labels["com.project-snow.origin-network-probe-sha"] == null and
          $labels["com.project-snow.origin-network-probe-colour"] == null) or
         ($labels["com.project-snow.origin-network-probe"] == "1" and
          $labels["com.project-snow.origin-network-probe-schema"] == "1" and
          ($labels["com.project-snow.origin-network-probe-sha"] |
            test("^[0-9a-f]{40}$")) and
          ($labels["com.project-snow.origin-network-probe-colour"] |
            test("^(blue|green)$")) and
          .[0].Config.Cmd[3] ==
            ("project-snow-" +
             ($labels["com.project-snow.origin-network-probe-sha"][0:12]) +
             "-" + $pid + ".1-1-1-1.sslip.io"))))) |
      [.[0].Image, .[0].Config.Cmd[6]] | @tsv
    ')" || return 1
  stale_probe_image="$(printf '%s\n' "$stale_probe_identity" | cut -f 1)"
  stale_probe_other_ipv4="$(printf '%s\n' "$stale_probe_identity" | cut -f 2)"
  printf '%s\n' "$stale_probe_image" | grep -Eq '^sha256:[0-9a-f]{64}$' || return 1
  stale_probe_image_allowed=0
  for stale_probe_allowed_ref in "$marker_app_image" "$previous_app_image"; do
    stale_probe_allowed_image="$(docker image inspect --format '{{.Id}}' \
      "$stale_probe_allowed_ref" 2>/dev/null)" || return 1
    if [ "$stale_probe_image" = "$stale_probe_allowed_image" ]; then
      stale_probe_image_allowed=1
    fi
  done
  [ "$stale_probe_image_allowed" -eq 1 ] || return 1
  stale_probe_app_network_document="$(docker network inspect project-snow-public_app)" || return 1
  stale_probe_app_subnets="$(printf '%s\n' "$stale_probe_app_network_document" | jq -er '
    if length == 1 and
       .[0].Name == "project-snow-public_app" and
       .[0].Driver == "bridge" and
       .[0].Internal == true and
       .[0].EnableIPv6 == false and
       .[0].Labels["com.docker.compose.project"] == "project-snow-public" and
       .[0].Labels["com.docker.compose.network"] == "app"
    then
      [.[0].IPAM.Config[]?.Subnet |
        select(type == "string" and test("^[0-9./]+$"))] |
      unique | if length > 0 then .[] else error("no app subnet") end
    else error("invalid app network") end
  ')" || return 1
  # Subnets are restricted above to digits, dots and slashes before word-splitting.
  # shellcheck disable=SC2086
  probe_ipv4_belongs_to_app_network "$stale_probe_other_ipv4" $stale_probe_app_subnets
}

remove_verified_stale_origin_network_probes() {
  stale_probe_ids="$(docker ps --all --no-trunc --quiet \
    --filter name=project-snow-origin-netprobe-)" || return 1
  [ -n "$stale_probe_ids" ] || return 0
  for stale_probe_id in $stale_probe_ids; do
    printf '%s\n' "$stale_probe_id" | grep -Eq '^[0-9a-f]{64}$' || {
      echo 'A stale origin probe candidate has an invalid container identity.' >&2
      return 1
    }
    validate_stale_origin_network_probe "$stale_probe_id" || {
      echo 'Refusing to remove an origin backend endpoint that is not an exact stale Project Snow probe.' >&2
      return 1
    }
  done
  for stale_probe_id in $stale_probe_ids; do
    # Revalidate immediately and use non-forced removal so a concurrently
    # started or mutated endpoint remains fail-closed instead of being killed.
    validate_stale_origin_network_probe "$stale_probe_id" || return 1
    docker rm "$stale_probe_id" >/dev/null || {
      echo 'Could not remove an exact stopped stale Project Snow origin probe.' >&2
      return 1
    }
  done
}

remove_origin_network_probe() {
  [ -n "${origin_probe_container:-}" ] || return 0
  probe_remove_target="$origin_probe_container"
  if docker rm -f "$probe_remove_target" >/dev/null 2>&1; then
    origin_probe_container=""
    return 0
  fi
  return 1
}

read_origin_drop_counter() {
  counter_field="$1"
  run_origin_firewall counters | jq -er --arg field "$counter_field" '
    .[$field] | select(type == "number" and floor == . and . >= 0)
  '
}

run_origin_network_probe() {
  probe_env="$1"
  probe_config_root="$2"
  probe_colour="$3"
  probe_candidate="project-snow-origin-netprobe-$$"
  probe_nonce="$(printf '%s' "$marker_sha" | cut -c 1-12)-$$"
  probe_external_name="project-snow-$probe_nonce.1-1-1-1.sslip.io"
  probe_gateway_ipv4="$(printf '%s\n' "$origin_network_document" | jq -er '
    [.[0].IPAM.Config[]?.Gateway // empty |
      select(type == "string" and test("^([0-9]{1,3}\\.){3}[0-9]{1,3}$"))] |
    unique | if length == 1 then .[0] else error("origin gateway is not unique") end
  ')" || {
    echo 'Origin bridge has no unique IPv4 gateway for the isolation probe.' >&2
    return 1
  }
  probe_backend_gateway_ipv4="$(printf '%s\n' "$origin_internal_network_document" | jq -er '
    [.[0].IPAM.Config[]?.Gateway // empty |
      select(type == "string" and test("^([0-9]{1,3}\\.){3}[0-9]{1,3}$"))] |
    unique | if length == 1 then .[0] else error("origin backend gateway is not unique") end
  ')" || {
    echo 'Origin backend has no unique IPv4 gateway for the isolation probe.' >&2
    return 1
  }
  probe_target_ids="$(
    SNOW_UPSTREAM="public-api-$probe_colour:8000" \
      docker compose --env-file "$probe_env" -f "$probe_config_root/compose.prod.yml" \
        --profile "$probe_colour" ps --quiet "public-api-$probe_colour"
  )" || return 1
  probe_target_count="$(printf '%s\n' "$probe_target_ids" | awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$probe_target_count" -eq 1 ] || {
    echo 'Expected exactly one target API container for the cross-bridge isolation probe.' >&2
    return 1
  }
  probe_target_id="$(printf '%s\n' "$probe_target_ids" | awk 'NF { print; exit }')"
  probe_target_document="$(docker inspect "$probe_target_id")" || {
    echo 'Target API container could not be inspected for the isolation probe.' >&2
    return 1
  }
  probe_image_id="$(printf '%s\n' "$probe_target_document" | jq -er \
    '.[0].Image | select(type == "string" and test("^sha256:[0-9a-f]{64}$"))')" || {
      echo 'Target API image identity is not an immutable local image ID.' >&2
      return 1
    }
  probe_other_bridge_ipv4="$(printf '%s\n' "$probe_target_document" | jq -er \
    --arg network project-snow-public_app '
      .[0].NetworkSettings.Networks[$network].IPAddress |
      select(type == "string" and test("^([0-9]{1,3}\\.){3}[0-9]{1,3}$"))
    ')" || {
      echo 'Target API has no inspectable application-bridge IPv4 address.' >&2
      return 1
    }
  probe_uplink_input_before="$(read_origin_drop_counter input_uplink)" || {
    echo 'Origin uplink host-input drop counter is unavailable before the isolation probe.' >&2
    return 1
  }
  probe_backend_input_before="$(read_origin_drop_counter input_backend)" || {
    echo 'Origin backend host-input drop counter is unavailable before the isolation probe.' >&2
    return 1
  }
  probe_forward_before="$(read_origin_drop_counter forward)" || {
    echo 'Origin forwarding drop counter is unavailable before the isolation probe.' >&2
    return 1
  }
  if ! docker create --name "$probe_candidate" \
    --label com.project-snow.origin-network-probe=1 \
    --label com.project-snow.origin-network-probe-schema=1 \
    --label "com.project-snow.origin-network-probe-sha=$marker_sha" \
    --label "com.project-snow.origin-network-probe-colour=$probe_colour" \
    --network "$origin_edge_network" \
    --dns 127.0.0.1 \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 32 --memory 64m --cpus 0.25 \
    --tmpfs /tmp:rw,nosuid,nodev,size=4m \
    --entrypoint python "$probe_image_id" \
    -c "$origin_network_probe_program" \
      project-snow-origin-netprobe "$probe_external_name" "$probe_gateway_ipv4" \
      "$probe_backend_gateway_ipv4" "$probe_other_bridge_ipv4" >/dev/null; then
    echo 'Could not create the no-secret origin network probe.' >&2
    return 1
  fi
  origin_probe_container="$probe_candidate"
  if ! docker network connect "$origin_edge_internal_network" "$origin_probe_container"; then
    remove_origin_network_probe || true
    echo 'Could not attach the no-secret probe to the internal edge network.' >&2
    return 1
  fi
  probe_status=0
  docker start --attach "$origin_probe_container" >/dev/null 2>&1 || probe_status=$?
  probe_counter_status=0
  probe_uplink_input_after="$(read_origin_drop_counter input_uplink)" || probe_counter_status=1
  probe_backend_input_after="$(read_origin_drop_counter input_backend)" || probe_counter_status=1
  probe_forward_after="$(read_origin_drop_counter forward)" || probe_counter_status=1
  if ! remove_origin_network_probe; then
    echo 'Could not remove the no-secret origin network probe.' >&2
    return 1
  fi
  [ "$probe_status" -eq 0 ] || {
    echo 'Origin network isolation probe failed; public DNS/TCP/UDP must fail while internal Caddy succeeds.' >&2
    return 1
  }
  [ "$probe_counter_status" -eq 0 ] &&
    [ "$probe_uplink_input_after" -gt "$probe_uplink_input_before" ] &&
    [ "$probe_backend_input_after" -gt "$probe_backend_input_before" ] &&
    [ "$probe_forward_after" -gt "$probe_forward_before" ] || {
      echo 'Origin network isolation probe did not increment all enforced nftables drop counters.' >&2
      return 1
    }
}

validate_running_origin_edge_caddy() {
  running_env="$1"
  running_config_root="$2"
  running_colour="$3"
  SNOW_UPSTREAM="public-api-$running_colour:8000" \
    docker compose --env-file "$running_env" -f "$running_config_root/compose.prod.yml" \
      --profile "$running_colour" exec -T origin-edge \
        caddy validate --config /etc/caddy/OriginEdge.Caddyfile --adapter caddyfile
}

validate_prepared_origin_edge_caddy() {
  prepared_caddy_env="$1"
  prepared_caddy_config_root="$2"
  prepared_caddy_colour="$3"
  SNOW_UPSTREAM="public-api-$prepared_caddy_colour:8000" \
    docker compose --env-file "$prepared_caddy_env" \
      -f "$prepared_caddy_config_root/compose.prod.yml" \
      --profile "$prepared_caddy_colour" run --rm --no-deps -T \
      --entrypoint caddy origin-edge validate \
      --config /etc/caddy/OriginEdge.Caddyfile --adapter caddyfile \
      >/dev/null 2>&1 || {
        echo 'Origin-edge Caddy configuration or TLS material failed pre-start validation.' >&2
        return 1
      }
}

running_origin_edge_matches_snapshot() {
  matching_env="$1"
  matching_config_root="$2"
  matching_colour="$3"
  validate_origin_edge_material "$matching_env" "$matching_config_root" || return 1
  ensure_caddy_origin_backend "$matching_env" "$matching_config_root" "$matching_colour" || return 1
  validate_origin_edge_container "$matching_env" "$matching_config_root" "$matching_colour" true || return 1
  validate_running_origin_edge_caddy "$matching_env" "$matching_config_root" "$matching_colour"
}

persist_live_origin_edge_binding() {
  persist_env="$1"
  persist_config_root="$2"
  persist_colour="$3"
  running_origin_edge_matches_snapshot "$persist_env" "$persist_config_root" \
    "$persist_colour" || return 1
  persist_source_binding="$(origin_context_config_binding "$persist_env" \
    "$persist_config_root" "$persist_colour")" || return 1
  [ -f "$persist_source_binding" ] && [ ! -L "$persist_source_binding" ] &&
    [ "$(stat -c %u:%g:%a:%h "$persist_source_binding" 2>/dev/null)" = 0:0:600:1 ] || {
      echo 'Origin-edge configuration source binding is not an exact root-owned file.' >&2
      return 1
    }
  persist_sha="$(jq -r '.commit_sha // empty' "$persist_source_binding")" || return 1
  printf '%s\n' "$persist_sha" | grep -Eq '^[0-9a-f]{40}$' || return 1
  validate_config_binding "$persist_source_binding" "$persist_colour" "$persist_sha" || return 1
  [ "$(jq -r '.root' "$persist_source_binding")" = "$persist_config_root" ] || return 1

  if [ -e "$live_origin_edge_env_root" ] || [ -L "$live_origin_edge_env_root" ]; then
    [ -d "$live_origin_edge_env_root" ] && [ ! -L "$live_origin_edge_env_root" ] &&
      [ "$(stat -c %u:%g:%a "$live_origin_edge_env_root" 2>/dev/null)" = 0:0:700 ] || {
        echo 'Refusing to adopt a mutable live origin-edge environment root.' >&2
        return 1
      }
  else
    install -o root -g root -m 0700 -d "$live_origin_edge_env_root" || return 1
    fsync_promote_path "$runtime_root" || return 1
  fi
  persist_env_sha="$(sha256sum "$persist_env" | awk '{print $1}')" || return 1
  printf '%s\n' "$persist_env_sha" | grep -Eq '^[0-9a-f]{64}$' || return 1
  persist_immutable_env="$live_origin_edge_env_root/$persist_env_sha.compose.env"
  if [ -e "$persist_immutable_env" ] || [ -L "$persist_immutable_env" ]; then
    [ -f "$persist_immutable_env" ] && [ ! -L "$persist_immutable_env" ] &&
      [ "$(stat -c %u:%g:%a:%h "$persist_immutable_env" 2>/dev/null)" = 0:0:600:1 ] &&
      [ "$(sha256sum "$persist_immutable_env" | awk '{print $1}')" = "$persist_env_sha" ] || {
        echo 'Existing immutable live origin-edge environment has an invalid identity.' >&2
        return 1
      }
  else
    live_origin_env_tmp="$(mktemp "$persist_immutable_env.candidate.XXXXXX")" || return 1
    cp -- "$persist_env" "$live_origin_env_tmp" || return 1
    chown root:root "$live_origin_env_tmp" || return 1
    chmod 0600 "$live_origin_env_tmp" || return 1
    [ "$(sha256sum "$live_origin_env_tmp" | awk '{print $1}')" = "$persist_env_sha" ] || return 1
    fsync_promote_path "$live_origin_env_tmp" || return 1
    mv -f -- "$live_origin_env_tmp" "$persist_immutable_env" || return 1
    live_origin_env_tmp=""
    fsync_promote_path "$live_origin_edge_env_root" || return 1
  fi
  fsync_promote_path "$persist_immutable_env" || return 1
  fsync_promote_path "$live_origin_edge_env_root" || return 1

  live_origin_binding_tmp="$(mktemp "$live_origin_edge_binding.promote.XXXXXX")" || return 1
  jq --arg live_schema project-snow-live-origin-edge-1 \
    --arg env_path "$persist_immutable_env" \
    --arg env_sha "$persist_env_sha" '
      del(.live_origin_edge_schema, .origin_edge_env_path, .origin_edge_env_sha256) |
      . + {
        live_origin_edge_schema: $live_schema,
        origin_edge_env_path: $env_path,
        origin_edge_env_sha256: $env_sha
      }
    ' "$persist_source_binding" > "$live_origin_binding_tmp" || return 1
  chown root:root "$live_origin_binding_tmp" || return 1
  chmod 0600 "$live_origin_binding_tmp" || return 1
  fsync_promote_path "$live_origin_binding_tmp" || return 1
  validate_live_origin_edge_binding "$live_origin_binding_tmp" || return 1
  trap '' HUP INT QUIT TERM PIPE || return 1
  if ! mv -f -- "$live_origin_binding_tmp" "$live_origin_edge_binding"; then
    [ "$promote_signal_handling" -eq 1 ] || restore_promote_signal_traps || true
    return 1
  fi
  live_origin_binding_tmp=""
  if ! fsync_promote_path /srv/project-snow/releases ||
     ! validate_live_origin_edge_binding "$live_origin_edge_binding"; then
    [ "$promote_signal_handling" -eq 1 ] || restore_promote_signal_traps || true
    return 1
  fi
  origin_edge_retained_env="$persist_immutable_env"
  origin_edge_retained_config_root="$persist_config_root"
  origin_edge_retained_colour="$persist_colour"
  live_origin_edge_binding_loaded=1
  if [ "$promote_signal_handling" -ne 1 ]; then
    restore_promote_signal_traps || return 1
  fi
}

discover_running_origin_edge_binding() {
  load_live_origin_edge_binding || return 1
  discover_origin_ids="$(docker ps \
    --filter label=com.docker.compose.project=project-snow-public \
    --filter label=com.docker.compose.service=origin-edge \
    --filter status=running --quiet)" || return 1
  discover_origin_count="$(printf '%s\n' "$discover_origin_ids" |
    awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$discover_origin_count" -le 1 ] || {
    echo 'More than one running origin-edge exists for the production project.' >&2
    return 1
  }
  [ "$discover_origin_count" -eq 1 ] || return 0
  if [ "$live_origin_edge_binding_loaded" -eq 1 ]; then
    running_origin_edge_matches_snapshot "$origin_edge_retained_env" \
      "$origin_edge_retained_config_root" "$origin_edge_retained_colour" || {
        echo 'The running origin-edge does not match its durable live binding.' >&2
        return 1
      }
    return 0
  fi
  if [ "$target_has_origin_edge" -eq 1 ] &&
     running_origin_edge_matches_snapshot "$colour_env" "$colour_config_root" "$colour"; then
    persist_live_origin_edge_binding "$colour_env" "$colour_config_root" "$colour" || return 1
    return 0
  fi
  if [ "$previous_has_origin_edge" -eq 1 ] &&
     running_origin_edge_matches_snapshot "$previous_env" "$previous_config_root" "$previous_colour"; then
    persist_live_origin_edge_binding "$previous_env" "$previous_config_root" "$previous_colour" || return 1
    return 0
  fi
  echo 'A running origin-edge has no exact durable recovery binding; refusing traffic mutation.' >&2
  return 1
}

remove_snapshot_origin_edge_if_present() {
  remove_env="$1"
  remove_config_root="$2"
  remove_colour="$3"
  remove_origin_ids="$(
    SNOW_UPSTREAM="public-api-$remove_colour:8000" \
      docker compose --env-file "$remove_env" -f "$remove_config_root/compose.prod.yml" \
        --profile "$remove_colour" ps --all --quiet origin-edge
  )" || return 1
  remove_origin_count="$(printf '%s\n' "$remove_origin_ids" |
    awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$remove_origin_count" -le 1 ] || return 1
  [ "$remove_origin_count" -eq 1 ] || return 0
  SNOW_UPSTREAM="public-api-$remove_colour:8000" \
    docker compose --env-file "$remove_env" -f "$remove_config_root/compose.prod.yml" \
      --profile "$remove_colour" stop origin-edge || return 1
  SNOW_UPSTREAM="public-api-$remove_colour:8000" \
    docker compose --env-file "$remove_env" -f "$remove_config_root/compose.prod.yml" \
      --profile "$remove_colour" rm -f origin-edge
}

begin_origin_edge_replacement() {
  replacement_target_env="$1"
  replacement_target_config_root="$2"
  replacement_target_colour="$3"
  replacement_restore_env="$4"
  replacement_restore_config_root="$5"
  replacement_restore_colour="$6"
  running_origin_edge_matches_snapshot "$replacement_restore_env" \
    "$replacement_restore_config_root" "$replacement_restore_colour" || {
      echo 'The running origin-edge matches neither the target nor the exact previous snapshot.' >&2
      return 1
    }

  # From this point a failed Tunnel gate must leave the known-good running
  # direct listener untouched.  Only after its exact recovery coordinates are
  # recorded may the controlled stop/remove window begin.
  origin_edge_prestart_failure_preserve=1
  require_running_cloudflared_fallback || return 1
  persist_live_origin_edge_binding "$replacement_restore_env" \
    "$replacement_restore_config_root" "$replacement_restore_colour" || return 1
  origin_edge_tunnel_retain=1
  origin_edge_replacement_restore_env="$origin_edge_retained_env"
  origin_edge_replacement_restore_config_root="$origin_edge_retained_config_root"
  origin_edge_replacement_restore_colour="$origin_edge_retained_colour"
  origin_edge_replacement_restore_binding="$(mktemp \
    "$live_origin_edge_binding.restore.XXXXXX")" || return 1
  cp -- "$live_origin_edge_binding" "$origin_edge_replacement_restore_binding" || return 1
  chown root:root "$origin_edge_replacement_restore_binding" || return 1
  chmod 0600 "$origin_edge_replacement_restore_binding" || return 1
  fsync_promote_path "$origin_edge_replacement_restore_binding" || return 1
  fsync_promote_path /srv/project-snow/releases || return 1
  validate_live_origin_edge_binding "$origin_edge_replacement_restore_binding" || return 1
  origin_edge_replacement_target_env="$replacement_target_env"
  origin_edge_replacement_target_config_root="$replacement_target_config_root"
  origin_edge_replacement_target_colour="$replacement_target_colour"
  origin_edge_replacement_active=1
  promote_signal_phase=switching
  origin_edge_prestart_failure_preserve=0
  remove_snapshot_origin_edge_if_present "$replacement_restore_env" \
    "$replacement_restore_config_root" "$replacement_restore_colour" || return 1
  require_running_cloudflared_fallback
}

prepare_or_retain_origin_edge() {
  prepare_env="$1"
  prepare_config_root="$2"
  prepare_colour="$3"
  origin_edge_prestart_failure_preserve=0
  validate_docker_dns_security_floor || return 1
  origin_uplink_exists=0
  origin_backend_exists=0
  docker network inspect "$origin_edge_network" >/dev/null 2>&1 && origin_uplink_exists=1
  docker network inspect "$origin_edge_internal_network" >/dev/null 2>&1 && origin_backend_exists=1
  if [ "$origin_uplink_exists" -eq 1 ] || [ "$origin_backend_exists" -eq 1 ]; then
    [ "$origin_uplink_exists" -eq 1 ] && [ "$origin_backend_exists" -eq 1 ] || {
      echo 'The dedicated origin networks must either both exist or both be absent.' >&2
      return 1
    }
    validate_origin_edge_network || return 1
  else
    for unmanaged_origin_bridge in "$origin_edge_network" "$origin_edge_internal_network"; do
      if /usr/sbin/ip link show dev "$unmanaged_origin_bridge" >/dev/null 2>&1; then
        echo "Refusing to adopt an unmanaged existing origin bridge: $unmanaged_origin_bridge" >&2
        return 1
      fi
    done
  fi
  existing_origin_ids="$(
    SNOW_UPSTREAM="public-api-$prepare_colour:8000" \
      docker compose --env-file "$prepare_env" -f "$prepare_config_root/compose.prod.yml" \
        --profile "$prepare_colour" ps --all --quiet origin-edge
  )" || return 1
  existing_origin_count="$(printf '%s\n' "$existing_origin_ids" | awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$existing_origin_count" -le 1 ] || {
    echo 'More than one origin-edge container exists for the production project.' >&2
    return 1
  }
  if [ "$existing_origin_count" -eq 1 ]; then
    existing_origin_id="$(printf '%s\n' "$existing_origin_ids" | awk 'NF { print; exit }')"
    existing_origin_running="$(docker inspect --format '{{.State.Running}}' "$existing_origin_id")" || return 1
    if [ "$existing_origin_running" = true ]; then
      if running_origin_edge_matches_snapshot "$prepare_env" "$prepare_config_root" \
        "$prepare_colour"; then
        persist_live_origin_edge_binding "$prepare_env" "$prepare_config_root" \
          "$prepare_colour" || return 1
        origin_edge_prestart_mode=retain
        return 0
      fi
      if [ -n "$origin_edge_retained_env" ] &&
         running_origin_edge_matches_snapshot "$origin_edge_retained_env" \
           "$origin_edge_retained_config_root" "$origin_edge_retained_colour"; then
        prepare_tls_root="$(origin_tls_root_for_env "$prepare_env" \
          "$prepare_config_root")" || return 1
        if [ "$rollback_mode" = 1 ] ||
           [ "$prepare_tls_root" = /etc/project-snow/origin-edge ]; then
          origin_edge_overlay_env="$origin_edge_retained_env"
          origin_edge_overlay_config_root="$origin_edge_retained_config_root"
          origin_edge_overlay_colour="$origin_edge_retained_colour"
          origin_edge_prestart_mode=overlay
          return 0
        fi
        begin_origin_edge_replacement "$prepare_env" "$prepare_config_root" \
          "$prepare_colour" "$origin_edge_retained_env" \
          "$origin_edge_retained_config_root" "$origin_edge_retained_colour" || return 1
      elif [ "$previous_has_origin_edge" -eq 1 ] &&
         running_origin_edge_matches_snapshot "$previous_env" "$previous_config_root" \
           "$previous_colour"; then
        prepare_tls_root="$(origin_tls_root_for_env "$prepare_env" \
          "$prepare_config_root")" || return 1
        if [ "$rollback_mode" = 1 ] ||
           [ "$prepare_tls_root" = /etc/project-snow/origin-edge ]; then
          # Application rollback is not an edge-route rollback.  Keep the
          # independently live listener and only move its Caddy backend.
          persist_live_origin_edge_binding "$previous_env" "$previous_config_root" \
            "$previous_colour" || return 1
          origin_edge_overlay_env="$origin_edge_retained_env"
          origin_edge_overlay_config_root="$origin_edge_retained_config_root"
          origin_edge_overlay_colour="$origin_edge_retained_colour"
          origin_edge_prestart_mode=overlay
          return 0
        fi
        begin_origin_edge_replacement "$prepare_env" "$prepare_config_root" \
          "$prepare_colour" "$previous_env" "$previous_config_root" \
          "$previous_colour" || return 1
      else
        echo 'The running origin-edge matches neither the target nor an exact durable recovery snapshot.' >&2
        return 1
      fi
    fi
  fi
  SNOW_UPSTREAM="public-api-$prepare_colour:8000" \
    docker compose --env-file "$prepare_env" -f "$prepare_config_root/compose.prod.yml" \
      --profile "$prepare_colour" up --no-start --no-deps --force-recreate origin-edge || {
        echo 'Origin-edge could not be created in a stopped state from its immutable snapshot.' >&2
        return 1
      }
  ensure_caddy_origin_backend "$prepare_env" "$prepare_config_root" "$prepare_colour" || return 1
  validate_origin_edge_container "$prepare_env" "$prepare_config_root" "$prepare_colour" false || return 1
  origin_edge_prestart_mode=start
}

probe_prepared_origin_edge() {
  prepared_env="$1"
  prepared_config_root="$2"
  prepared_colour="$3"
  validate_origin_edge_container "$prepared_env" "$prepared_config_root" "$prepared_colour" false || return 1
  run_origin_network_probe "$prepared_env" "$prepared_config_root" "$prepared_colour" || return 1
  validate_prepared_origin_edge_caddy "$prepared_env" "$prepared_config_root" \
    "$prepared_colour" || return 1
  validate_origin_edge_container "$prepared_env" "$prepared_config_root" "$prepared_colour" false
}

restore_replaced_origin_edge() {
  [ "$origin_edge_replacement_active" -eq 1 ] || return 0
  require_running_cloudflared_fallback || return 1
  remove_snapshot_origin_edge_if_present "$origin_edge_replacement_target_env" \
    "$origin_edge_replacement_target_config_root" \
    "$origin_edge_replacement_target_colour" || return 1
  validate_origin_edge_material "$origin_edge_replacement_restore_env" \
    "$origin_edge_replacement_restore_config_root" || return 1
  SNOW_UPSTREAM="public-api-$origin_edge_replacement_restore_colour:8000" \
    docker compose --env-file "$origin_edge_replacement_restore_env" \
      -f "$origin_edge_replacement_restore_config_root/compose.prod.yml" \
      --profile "$origin_edge_replacement_restore_colour" \
      up --no-start --no-deps --force-recreate origin-edge || return 1
  ensure_caddy_origin_backend "$origin_edge_replacement_restore_env" \
    "$origin_edge_replacement_restore_config_root" \
    "$origin_edge_replacement_restore_colour" || return 1
  validate_origin_edge_container "$origin_edge_replacement_restore_env" \
    "$origin_edge_replacement_restore_config_root" \
    "$origin_edge_replacement_restore_colour" false || return 1
  run_origin_firewall restore || return 1
  probe_prepared_origin_edge "$origin_edge_replacement_restore_env" \
    "$origin_edge_replacement_restore_config_root" \
    "$origin_edge_replacement_restore_colour" || return 1
  SNOW_UPSTREAM="public-api-$origin_edge_replacement_restore_colour:8000" \
    docker compose --env-file "$origin_edge_replacement_restore_env" \
      -f "$origin_edge_replacement_restore_config_root/compose.prod.yml" \
      --profile "$origin_edge_replacement_restore_colour" start origin-edge || return 1
  wait_for_origin_edge_running_policy "$origin_edge_replacement_restore_env" \
    "$origin_edge_replacement_restore_config_root" \
    "$origin_edge_replacement_restore_colour" || return 1
  validate_running_origin_edge_caddy "$origin_edge_replacement_restore_env" \
    "$origin_edge_replacement_restore_config_root" \
    "$origin_edge_replacement_restore_colour" || return 1
  persist_live_origin_edge_binding "$origin_edge_replacement_restore_env" \
    "$origin_edge_replacement_restore_config_root" \
    "$origin_edge_replacement_restore_colour" || return 1
  require_running_cloudflared_fallback || return 1
  origin_edge_replacement_active=0
}

switch_edge() {
  edge_env="$1"
  edge_config_root="$2"
  edge_colour="$3"
  edge_service_list="$4"
  edge_allow_origin="$5"
  edge_origin_mode="$6"
  edge_start_origin=0
  edge_retain_origin=0
  edge_retain_tunnel=0
  edge_runtime_env="$edge_env"
  edge_runtime_config_root="$edge_config_root"
  edge_runtime_colour="$edge_colour"
  set -- caddy
  if service_list_has "$edge_service_list" origin-edge && [ "$edge_allow_origin" -eq 1 ]; then
    case "$edge_origin_mode" in
      start)
        edge_start_origin=1
        ;;
      retain)
        edge_retain_origin=1
        ;;
      overlay)
        edge_runtime_env="$origin_edge_overlay_env"
        edge_runtime_config_root="$origin_edge_overlay_config_root"
        edge_runtime_colour="$origin_edge_overlay_colour"
        [ -n "$edge_runtime_env" ] && [ -n "$edge_runtime_config_root" ] &&
          [ -n "$edge_runtime_colour" ] || return 1
        edge_retain_origin=1
        ;;
      *)
        echo 'Origin-edge has no validated pre-start or retained-runtime state.' >&2
        return 1
        ;;
    esac
    validate_origin_edge_material "$edge_runtime_env" "$edge_runtime_config_root" || return 1
    if [ "$edge_start_origin" -eq 1 ]; then
      validate_origin_edge_container "$edge_runtime_env" "$edge_runtime_config_root" \
        "$edge_runtime_colour" false || return 1
    else
      validate_origin_edge_container "$edge_runtime_env" "$edge_runtime_config_root" \
        "$edge_runtime_colour" true || return 1
    fi
  fi
  if service_list_has "$edge_service_list" cloudflared; then
    if [ "$origin_edge_tunnel_retain" -eq 1 ]; then
      require_running_cloudflared_fallback || return 1
      edge_retain_tunnel=1
    else
      set -- "$@" cloudflared
    fi
  fi
  [ "$edge_start_origin" -eq 1 ] || [ "$edge_retain_origin" -eq 1 ] ||
    [ "$edge_retain_tunnel" -eq 1 ] || [ "$#" -gt 1 ] || {
    echo 'No permitted edge ingress remains after fail-closed filtering.' >&2
    return 1
  }
  set -- "$@" egress-proxy
  SNOW_UPSTREAM="public-api-$edge_colour:8000" \
    docker compose --env-file "$edge_env" -f "$edge_config_root/compose.prod.yml" --profile "$edge_colour" \
    up -d --no-deps --force-recreate "$@" || return 1
  edge_running_origin_ids="$(docker ps \
    --filter label=com.docker.compose.project=project-snow-public \
    --filter label=com.docker.compose.service=origin-edge \
    --filter status=running --quiet)" || return 1
  edge_running_origin_count="$(printf '%s\n' "$edge_running_origin_ids" | awk 'NF { count += 1 } END { print count + 0 }')"
  [ "$edge_running_origin_count" -le 1 ] || {
    echo 'More than one running origin-edge exists for the production project.' >&2
    return 1
  }
  if [ "$edge_start_origin" -eq 1 ] || [ "$edge_retain_origin" -eq 1 ] ||
     [ "$edge_running_origin_count" -eq 1 ]; then
    ensure_caddy_origin_backend "$edge_env" "$edge_config_root" "$edge_colour" || return 1
  fi
  if [ "$edge_running_origin_count" -eq 1 ] &&
     [ "$edge_start_origin" -ne 1 ] && [ "$edge_retain_origin" -ne 1 ]; then
    [ "$live_origin_edge_binding_loaded" -eq 1 ] &&
      [ -n "$origin_edge_retained_env" ] &&
      running_origin_edge_matches_snapshot "$origin_edge_retained_env" \
        "$origin_edge_retained_config_root" "$origin_edge_retained_colour" || {
          echo 'The retained origin-edge has no valid durable live binding.' >&2
          return 1
        }
  fi
  if [ "$edge_start_origin" -eq 1 ]; then
    validate_origin_edge_container "$edge_runtime_env" "$edge_runtime_config_root" \
      "$edge_runtime_colour" false || return 1
    SNOW_UPSTREAM="public-api-$edge_colour:8000" \
      docker compose --env-file "$edge_env" -f "$edge_config_root/compose.prod.yml" --profile "$edge_colour" \
        start origin-edge || return 1
    wait_for_origin_edge_running_policy "$edge_runtime_env" "$edge_runtime_config_root" \
      "$edge_runtime_colour" || return 1
    validate_running_origin_edge_caddy "$edge_runtime_env" "$edge_runtime_config_root" \
      "$edge_runtime_colour" || return 1
    if ! persist_live_origin_edge_binding "$edge_runtime_env" \
      "$edge_runtime_config_root" "$edge_runtime_colour"; then
      echo 'Could not publish the exact live origin-edge recovery binding.' >&2
      remove_snapshot_origin_edge_if_present "$edge_runtime_env" \
        "$edge_runtime_config_root" "$edge_runtime_colour" || true
      return 1
    fi
  elif [ "$edge_retain_origin" -eq 1 ]; then
    validate_origin_edge_container "$edge_runtime_env" "$edge_runtime_config_root" \
      "$edge_runtime_colour" true || return 1
    validate_running_origin_edge_caddy "$edge_runtime_env" "$edge_runtime_config_root" \
      "$edge_runtime_colour" || return 1
    validate_live_origin_edge_binding "$live_origin_edge_binding" || return 1
    [ "$edge_runtime_env" = "$origin_edge_retained_env" ] &&
      [ "$edge_runtime_config_root" = "$origin_edge_retained_config_root" ] &&
      [ "$edge_runtime_colour" = "$origin_edge_retained_colour" ] || return 1
  fi
  if [ "$edge_retain_tunnel" -eq 1 ]; then
    require_running_cloudflared_fallback
  fi
}

stop_snapshot_service() {
  stop_env="$1"
  stop_config_root="$2"
  stop_colour="$3"
  stop_service="$4"
  docker compose --env-file "$stop_env" -f "$stop_config_root/compose.prod.yml" --profile "$stop_colour" \
    stop "$stop_service" &&
    docker compose --env-file "$stop_env" -f "$stop_config_root/compose.prod.yml" --profile "$stop_colour" \
      rm -f "$stop_service"
}

stop_known_origin_edge() {
  if [ "$target_has_origin_edge" -eq 1 ]; then
    stop_snapshot_service "$colour_env" "$colour_config_root" "$colour" origin-edge
  elif [ "$previous_has_origin_edge" -eq 1 ]; then
    stop_snapshot_service "$previous_env" "$previous_config_root" "$previous_colour" origin-edge
  fi
}

# Prepare all marker files before changing traffic. The final renames are
# same-filesystem operations and leave the previous colour available.
origin_probe_container=""
live_origin_binding_tmp=""
live_origin_env_tmp=""
previous_state_backup=""
previous_active_backup=""
previous_manifest_backup=""
previous_config_backup=""
previous_current_backup=""
promoted_restore_tmp=""
promoted_state_recovery_failed=0
state_tmp="$(mktemp "$runtime_root/compose.env.promote.XXXXXX")"
active_tmp="$(mktemp "$active_file.promote.XXXXXX")"
manifest_tmp="$(mktemp "$current_manifest.promote.XXXXXX")"
config_tmp="$(mktemp "$current_config_binding.promote.XXXXXX")"
current_tmp="$(mktemp "$release_current.promote.XXXXXX")"

create_promoted_state_backup() {
  backup_source="$1"
  backup_template="$2"
  if [ ! -e "$backup_source" ] && [ ! -L "$backup_source" ]; then
    printf '%s\n' absent
    return 0
  fi
  [ -f "$backup_source" ] && [ ! -L "$backup_source" ] &&
    [ "$(stat -c %u:%g:%a:%h "$backup_source" 2>/dev/null)" = 0:0:600:1 ] || {
      echo "Promoted state source is not an exact root-owned file: $backup_source" >&2
      return 1
    }
  promoted_backup_tmp="$(mktemp "$backup_template")" || return 1
  cp -- "$backup_source" "$promoted_backup_tmp" || return 1
  chown root:root "$promoted_backup_tmp" || return 1
  chmod 0600 "$promoted_backup_tmp" || return 1
  fsync_promote_path "$promoted_backup_tmp" || return 1
  printf '%s\n' "$promoted_backup_tmp"
}

cleanup() {
  promote_cleanup_status=$?
  trap - EXIT HUP INT QUIT TERM PIPE
  if [ -n "${origin_probe_container:-}" ]; then
    docker rm -f "$origin_probe_container" >/dev/null 2>&1 || true
  fi
  rm -f "${state_tmp:-}" "${active_tmp:-}" "${manifest_tmp:-}" \
    "${config_tmp:-}" "${current_tmp:-}" "${live_origin_binding_tmp:-}" \
    "${live_origin_env_tmp:-}" "${promoted_restore_tmp:-}" || true
  if [ "${promoted_state_recovery_failed:-0}" -ne 1 ]; then
    for promoted_backup in "${previous_state_backup:-}" "${previous_active_backup:-}" \
      "${previous_manifest_backup:-}" "${previous_config_backup:-}" \
      "${previous_current_backup:-}"; do
      case "$promoted_backup" in ''|absent) ;; *) rm -f -- "$promoted_backup" || true ;; esac
    done
  else
    for promoted_backup in "${previous_state_backup:-}" "${previous_active_backup:-}" \
      "${previous_manifest_backup:-}" "${previous_config_backup:-}" \
      "${previous_current_backup:-}"; do
      case "$promoted_backup" in
        ''|absent) ;;
        *) echo "CRITICAL: preserved previous promoted-state backup at $promoted_backup" >&2 || true ;;
      esac
    done
  fi
  if [ "${origin_edge_replacement_active:-0}" -ne 1 ]; then
    rm -f "${origin_edge_replacement_restore_binding:-}" || true
  elif [ -n "${origin_edge_replacement_restore_binding:-}" ]; then
    echo "CRITICAL: preserved exact origin-edge recovery binding at $origin_edge_replacement_restore_binding" >&2 || true
  fi
  exit "$promote_cleanup_status"
}

terminate_promote_signal() {
  promote_signal_status="$1"
  promote_signal_handling=1
  trap '' HUP INT QUIT TERM PIPE
  case "$promote_signal_phase" in
    committed)
      exit 0
      ;;
    switching)
      echo 'Promotion was interrupted after traffic mutation began; restoring the previous runtime.' >&2 || true
      if ! restore_previous_runtime; then
        echo 'CRITICAL: interrupted promotion could not fully restore the previous runtime.' >&2 || true
      fi
      exit "$promote_signal_status"
      ;;
    *)
      exit "$promote_signal_status"
      ;;
  esac
}

restore_promote_signal_traps() {
  trap 'terminate_promote_signal 129' HUP || return 1
  trap 'terminate_promote_signal 130' INT || return 1
  trap 'terminate_promote_signal 131' QUIT || return 1
  trap 'terminate_promote_signal 143' TERM || return 1
  trap 'terminate_promote_signal 141' PIPE || return 1
}

trap cleanup EXIT
restore_promote_signal_traps || exit 78
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
printf '%s\n' "$colour $marker_sha $marker_app_image $marker_embedding_image" > "$current_tmp"
chmod 0600 "$current_tmp"
for promote_candidate in "$state_tmp" "$active_tmp" "$config_tmp" "$current_tmp"; do
  fsync_promote_path "$promote_candidate" || exit 78
done
if [ -n "$manifest_tmp" ]; then
  fsync_promote_path "$manifest_tmp" || exit 78
fi
previous_state_backup="$(create_promoted_state_backup "$current_env" \
  "$current_env.previous.XXXXXX")" || exit 78
previous_active_backup="$(create_promoted_state_backup "$active_file" \
  "$active_file.previous.XXXXXX")" || exit 78
previous_manifest_backup="$(create_promoted_state_backup "$current_manifest" \
  "$current_manifest.previous.XXXXXX")" || exit 78
previous_config_backup="$(create_promoted_state_backup "$current_config_binding" \
  "$current_config_binding.previous.XXXXXX")" || exit 78
previous_current_backup="$(create_promoted_state_backup "$release_current" \
  "$release_current.previous.XXXXXX")" || exit 78
fsync_promote_path "$runtime_root" || exit 78
fsync_promote_path /srv/project-snow/releases || exit 78

restore_previous_runtime() {
  restore_failed=0
  previous_edge_switched=0
  previous_allow_origin=1
  previous_origin_mode=none
  if [ "$origin_edge_replacement_active" -eq 1 ]; then
    if ! restore_replaced_origin_edge; then
      echo 'CRITICAL: the exact pre-upgrade origin-edge could not be restored; Tunnel remains the only permitted ingress.' >&2
      previous_allow_origin=0
      restore_failed=1
      stop_known_origin_edge || true
    fi
  fi
  if [ "$previous_has_origin_edge" -eq 1 ] && [ "$restore_failed" -eq 0 ]; then
    origin_edge_prestart_mode=none
    if ! validate_origin_edge_material "$previous_env" "$previous_config_root" ||
       ! prepare_or_retain_origin_edge "$previous_env" "$previous_config_root" "$previous_colour"; then
      echo 'CRITICAL: previous origin-edge could not pass its retained-runtime or stopped pre-start gate; keeping public 443 fail-closed.' >&2
      previous_allow_origin=0
      if ! stop_known_origin_edge; then
        echo 'CRITICAL: failed to stop origin-edge after its pre-start gate failed.' >&2
        restore_failed=1
      fi
    else
      previous_origin_mode="$origin_edge_prestart_mode"
    fi
  fi
  if [ "$target_has_origin_edge" -eq 1 ] || [ "$previous_has_origin_edge" -eq 1 ]; then
    if ! run_origin_firewall restore; then
      echo 'CRITICAL: failed to restore the last-known-good origin firewall; keeping public 443 fail-closed.' >&2
      previous_allow_origin=0
      if ! stop_known_origin_edge; then
        echo 'CRITICAL: failed to stop origin-edge after firewall restoration failed.' >&2
        restore_failed=1
      fi
    fi
  fi
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
  if [ "$previous_ready" -eq 1 ] && [ "$previous_allow_origin" -eq 1 ] &&
     [ "$previous_origin_mode" = start ]; then
    if ! probe_prepared_origin_edge "$previous_env" "$previous_config_root" "$previous_colour"; then
      echo 'CRITICAL: previous origin-edge failed its post-firewall no-secret network isolation probe.' >&2
      previous_allow_origin=0
      if ! stop_known_origin_edge; then
        echo 'CRITICAL: failed to keep origin-edge stopped after its isolation probe failed.' >&2
      fi
      restore_failed=1
    fi
  fi
  if [ "$previous_ready" -ne 1 ]; then
    echo 'CRITICAL: previous public API did not become ready for restoration.' >&2
    restore_failed=1
  elif ! switch_edge "$previous_env" "$previous_config_root" "$previous_colour" \
    "$previous_services" "$previous_allow_origin" "$previous_origin_mode"; then
    echo 'CRITICAL: failed to restore the previous edge configuration snapshot.' >&2
    restore_failed=1
  else
    previous_edge_switched=1
  fi
  if [ "$previous_edge_switched" -eq 1 ]; then
    if [ "$previous_allow_origin" -ne 1 ] && [ "$previous_has_origin_edge" -eq 1 ]; then
      if ! stop_snapshot_service "$previous_env" "$previous_config_root" "$previous_colour" origin-edge; then
        echo 'CRITICAL: failed to stop origin-edge after firewall restoration failed.' >&2
        restore_failed=1
      fi
    fi
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

if ! discover_running_origin_edge_binding; then
  echo 'Could not establish an exact durable binding for the live origin-edge.' >&2
  exit 72
fi

target_allow_origin=1
target_origin_mode=none
if [ "$target_has_origin_edge" -eq 1 ]; then
  origin_edge_prestart_mode=none
  origin_edge_prestart_failure_preserve=1
  if ! validate_origin_edge_material "$colour_env" "$colour_config_root" ||
     ! prepare_or_retain_origin_edge "$colour_env" "$colour_config_root" "$colour"; then
    echo 'Target origin-edge could not pass its retained-runtime or stopped pre-start gate; public 443 was not started.' >&2
    target_allow_origin=0
    if [ "$origin_edge_prestart_failure_preserve" -ne 1 ] && ! stop_known_origin_edge; then
      echo 'Could not stop origin-edge after the target pre-start gate failed.' >&2
      restore_previous_runtime || exit 74
      exit 72
    fi
    restore_previous_runtime || exit 74
    exit 72
  else
    target_origin_mode="$origin_edge_prestart_mode"
  fi
fi
if [ "$rollback_mode" = 1 ] &&
   { [ "$target_has_origin_edge" -eq 1 ] || [ "$previous_has_origin_edge" -eq 1 ]; }; then
  if ! run_origin_firewall restore; then
    echo 'Rollback could not restore the last-known-good origin firewall; public 443 will remain fail-closed.' >&2
    target_allow_origin=0
    if ! stop_known_origin_edge; then
      echo 'Could not stop origin-edge after the rollback firewall gate failed.' >&2
      restore_previous_runtime || exit 74
      exit 72
    fi
  fi
elif [ "$target_has_origin_edge" -eq 1 ]; then
  if ! run_origin_firewall update; then
    echo 'Origin firewall refresh failed before edge switch; active traffic was not intentionally changed.' >&2
    restore_previous_runtime || exit 74
    exit 72
  fi
fi

if [ "$target_has_origin_edge" -eq 1 ] && [ "$target_allow_origin" -eq 1 ] &&
   [ "$target_origin_mode" = start ]; then
  if ! probe_prepared_origin_edge "$colour_env" "$colour_config_root" "$colour"; then
    echo 'Target origin-edge failed its post-firewall no-secret network isolation probe.' >&2
    target_allow_origin=0
    if ! stop_known_origin_edge; then
      echo 'Could not keep origin-edge stopped after its isolation probe failed.' >&2
    fi
    restore_previous_runtime || exit 74
    exit 72
  fi
fi

promote_signal_phase=switching
if ! switch_edge "$colour_env" "$colour_config_root" "$colour" \
  "$target_services" "$target_allow_origin" "$target_origin_mode"; then
  echo 'Edge switch failed; restoring the previous edge configuration snapshot.' >&2
  restore_previous_runtime || exit 74
  exit 72
fi
if [ "$target_allow_origin" -ne 1 ] && [ "$target_has_origin_edge" -eq 1 ]; then
  if ! stop_snapshot_service "$colour_env" "$colour_config_root" "$colour" origin-edge; then
    echo 'Failed to keep origin-edge stopped after the rollback firewall gate failed.' >&2
    restore_previous_runtime || exit 74
    exit 72
  fi
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

restore_promoted_file() {
  promoted_file_backup="$1"
  promoted_file_destination="$2"
  if [ "$promoted_file_backup" = absent ]; then
    [ ! -d "$promoted_file_destination" ] || return 1
    rm -f -- "$promoted_file_destination" || return 1
    return 0
  fi
  [ -f "$promoted_file_backup" ] && [ ! -L "$promoted_file_backup" ] &&
    [ "$(stat -c %u:%g:%a:%h "$promoted_file_backup" 2>/dev/null)" = 0:0:600:1 ] || return 1
  promoted_restore_tmp="$(mktemp "$promoted_file_destination.restore.XXXXXX")" || return 1
  cp -- "$promoted_file_backup" "$promoted_restore_tmp" || return 1
  chown root:root "$promoted_restore_tmp" || return 1
  chmod 0600 "$promoted_restore_tmp" || return 1
  fsync_promote_path "$promoted_restore_tmp" || return 1
  mv -f -- "$promoted_restore_tmp" "$promoted_file_destination" || return 1
  promoted_restore_tmp=""
}

promoted_file_matches_backup() {
  promoted_match_backup="$1"
  promoted_match_destination="$2"
  if [ "$promoted_match_backup" = absent ]; then
    [ ! -e "$promoted_match_destination" ] && [ ! -L "$promoted_match_destination" ]
    return
  fi
  [ -f "$promoted_match_destination" ] && [ ! -L "$promoted_match_destination" ] &&
    [ "$(stat -c %u:%g:%a:%h "$promoted_match_destination" 2>/dev/null)" = 0:0:600:1 ] &&
    cmp -s "$promoted_match_backup" "$promoted_match_destination"
}

restore_previous_promoted_state() {
  promoted_restore_failed=0
  restore_promoted_file "$previous_state_backup" "$current_env" || promoted_restore_failed=1
  restore_promoted_file "$previous_active_backup" "$active_file" || promoted_restore_failed=1
  restore_promoted_file "$previous_manifest_backup" "$current_manifest" || promoted_restore_failed=1
  restore_promoted_file "$previous_config_backup" "$current_config_binding" || promoted_restore_failed=1
  restore_promoted_file "$previous_current_backup" "$release_current" || promoted_restore_failed=1
  fsync_promote_path "$runtime_root" || promoted_restore_failed=1
  fsync_promote_path /srv/project-snow/releases || promoted_restore_failed=1
  promoted_file_matches_backup "$previous_state_backup" "$current_env" || promoted_restore_failed=1
  promoted_file_matches_backup "$previous_active_backup" "$active_file" || promoted_restore_failed=1
  promoted_file_matches_backup "$previous_manifest_backup" "$current_manifest" || promoted_restore_failed=1
  promoted_file_matches_backup "$previous_config_backup" "$current_config_binding" || promoted_restore_failed=1
  promoted_file_matches_backup "$previous_current_backup" "$release_current" || promoted_restore_failed=1
  if [ "$promoted_restore_failed" -ne 0 ]; then
    promoted_state_recovery_failed=1
    echo 'CRITICAL: previous promoted-state metadata could not be restored exactly.' >&2
    return 1
  fi
}

commit_promoted_state() {
  trap '' HUP INT QUIT TERM PIPE || return 1
  promoted_commit_failed=0
  if ! mv -f "$state_tmp" "$current_env"; then
    promoted_commit_failed=1
  else
    state_tmp=""
  fi
  if [ "$promoted_commit_failed" -eq 0 ]; then
    if ! mv -f "$active_tmp" "$active_file"; then
      promoted_commit_failed=1
    else
      active_tmp=""
    fi
  fi
  if [ "$promoted_commit_failed" -eq 0 ] && [ -n "$manifest_tmp" ]; then
    if ! mv -f "$manifest_tmp" "$current_manifest"; then
      promoted_commit_failed=1
    else
      manifest_tmp=""
    fi
  fi
  if [ "$promoted_commit_failed" -eq 0 ]; then
    if ! mv -f "$config_tmp" "$current_config_binding"; then
      promoted_commit_failed=1
    else
      config_tmp=""
    fi
  fi
  if [ "$promoted_commit_failed" -eq 0 ]; then
    if ! mv -f "$current_tmp" "$release_current"; then
      promoted_commit_failed=1
    else
      current_tmp=""
    fi
  fi
  if [ "$promoted_commit_failed" -eq 0 ]; then
    fsync_promote_path "$runtime_root" || promoted_commit_failed=1
  fi
  if [ "$promoted_commit_failed" -eq 0 ]; then
    fsync_promote_path /srv/project-snow/releases || promoted_commit_failed=1
  fi
  if [ "$promoted_commit_failed" -ne 0 ]; then
    restore_previous_promoted_state || true
    restore_promote_signal_traps || true
    return 1
  fi
  promote_signal_phase=committed
  origin_edge_replacement_active=0
  origin_edge_tunnel_retain=0
  restore_promote_signal_traps
}

if ! commit_promoted_state; then
  echo 'Promotion state publication failed; restoring the previous runtime.' >&2
  restore_previous_runtime || exit 74
  exit 73
fi

printf '%s\n' "Promoted $colour $marker_sha. Cloudflare Access and MyWebsite settings were not changed."
