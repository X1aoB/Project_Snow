#!/bin/sh
set -eu
exec /usr/bin/python3 "$(dirname "$0")/install_maintenance.py" "$@"
