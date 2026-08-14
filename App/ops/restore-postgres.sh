#!/bin/sh
set -eu
dump="${1:?path to verified pg_dump required}"
docker compose -f /srv/project-snow/app/compose.prod.yml exec -T postgres pg_restore --clean --if-exists -U project_snow -d project_snow < "$dump"
docker compose -f /srv/project-snow/app/compose.prod.yml --profile admin run --rm admin python -c "from backend.snow_app.config import PublicSettings; from backend.snow_app.public_store import PublicStore; s=PublicSettings.from_environment(); print(PublicStore(s.database_url).cleanup())"
