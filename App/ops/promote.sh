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

validate_origin_edge_material() {
  for origin_material in \
    /etc/project-snow/origin-edge/origin-cert.pem \
    /etc/project-snow/origin-edge/origin-key.pem \
    /etc/project-snow/origin-edge/aop-ca.pem; do
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
  backend_origin_ids="$(docker ps --all \
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
    --argjson expected_running "$inspected_running" '
      length == 1 and
      .[0].State.Running == $expected_running and
      .[0].State.Paused == false and
      .[0].Config.Labels["com.docker.compose.project"] == $project and
      .[0].Config.Labels["com.docker.compose.service"] == "origin-edge" and
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
      ((.[0].HostConfig.SecurityOpt // []) | index("no-new-privileges:true")) != null
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
  probe_nonce="$(printf '%s' "$expected_sha" | cut -c 1-12)-$$"
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
    --network "$origin_edge_network" \
    --dns 127.0.0.1 \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 32 --memory 64m --cpus 0.25 \
    --tmpfs /tmp:rw,nosuid,nodev,size=4m \
    --entrypoint python "$probe_image_id" \
    -c '
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
    ' project-snow-origin-netprobe "$probe_external_name" "$probe_gateway_ipv4" \
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

prepare_or_retain_origin_edge() {
  prepare_env="$1"
  prepare_config_root="$2"
  prepare_colour="$3"
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
      ensure_caddy_origin_backend "$prepare_env" "$prepare_config_root" "$prepare_colour" || return 1
      validate_origin_edge_container "$prepare_env" "$prepare_config_root" "$prepare_colour" true || return 1
      validate_running_origin_edge_caddy "$prepare_env" "$prepare_config_root" "$prepare_colour" || return 1
      origin_edge_prestart_mode=retain
      return 0
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
  validate_origin_edge_container "$prepared_env" "$prepared_config_root" "$prepared_colour" false
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
  set -- caddy
  if service_list_has "$edge_service_list" origin-edge && [ "$edge_allow_origin" -eq 1 ]; then
    validate_origin_edge_material || return 1
    case "$edge_origin_mode" in
      start)
        validate_origin_edge_container "$edge_env" "$edge_config_root" "$edge_colour" false || return 1
        edge_start_origin=1
        ;;
      retain)
        validate_origin_edge_container "$edge_env" "$edge_config_root" "$edge_colour" true || return 1
        edge_retain_origin=1
        ;;
      *)
        echo 'Origin-edge has no validated pre-start or retained-runtime state.' >&2
        return 1
        ;;
    esac
  fi
  if service_list_has "$edge_service_list" cloudflared; then
    set -- "$@" cloudflared
  fi
  [ "$edge_start_origin" -eq 1 ] || [ "$edge_retain_origin" -eq 1 ] || [ "$#" -gt 1 ] || {
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
  if [ "$edge_start_origin" -eq 1 ]; then
    validate_origin_edge_container "$edge_env" "$edge_config_root" "$edge_colour" false || return 1
    SNOW_UPSTREAM="public-api-$edge_colour:8000" \
      docker compose --env-file "$edge_env" -f "$edge_config_root/compose.prod.yml" --profile "$edge_colour" \
        start origin-edge || return 1
    validate_origin_edge_container "$edge_env" "$edge_config_root" "$edge_colour" true || return 1
    validate_running_origin_edge_caddy "$edge_env" "$edge_config_root" "$edge_colour"
  elif [ "$edge_retain_origin" -eq 1 ]; then
    validate_origin_edge_container "$edge_env" "$edge_config_root" "$edge_colour" true || return 1
    validate_running_origin_edge_caddy "$edge_env" "$edge_config_root" "$edge_colour"
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
state_tmp="$(mktemp "$runtime_root/compose.env.promote.XXXXXX")"
active_tmp="$(mktemp "$active_file.promote.XXXXXX")"
manifest_tmp="$(mktemp "$current_manifest.promote.XXXXXX")"
config_tmp="$(mktemp "$current_config_binding.promote.XXXXXX")"
cleanup() {
  if [ -n "${origin_probe_container:-}" ]; then
    docker rm -f "$origin_probe_container" >/dev/null 2>&1 || true
  fi
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
  previous_edge_switched=0
  previous_allow_origin=1
  previous_origin_mode=none
  if [ "$previous_has_origin_edge" -eq 1 ]; then
    origin_edge_prestart_mode=none
    if ! validate_origin_edge_material ||
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

target_allow_origin=1
target_origin_mode=none
if [ "$target_has_origin_edge" -eq 1 ]; then
  origin_edge_prestart_mode=none
  if ! validate_origin_edge_material ||
     ! prepare_or_retain_origin_edge "$colour_env" "$colour_config_root" "$colour"; then
    echo 'Target origin-edge could not pass its retained-runtime or stopped pre-start gate; public 443 was not started.' >&2
    target_allow_origin=0
    if ! stop_known_origin_edge; then
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
