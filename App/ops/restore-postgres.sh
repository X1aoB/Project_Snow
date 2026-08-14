#!/bin/sh
set -eu
dump="${1:?path to verified pg_dump required}"
compose_env="${PROJECT_SNOW_COMPOSE_ENV:-/srv/project-snow/runtime/compose.env}"
compose() {
  docker compose --env-file "$compose_env" -f /srv/project-snow/app/compose.prod.yml "$@"
}
compose exec -T postgres pg_restore --clean --if-exists -U project_snow -d project_snow < "$dump"
compose --profile admin run --rm admin python -c "from backend.snow_app.config import PublicSettings; from backend.snow_app.public_store import PublicStore; s=PublicSettings.from_environment(); print(PublicStore(s.database_url).cleanup())"
