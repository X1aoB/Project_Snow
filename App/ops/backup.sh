#!/bin/sh
set -eu
umask 077
# The helper reads the last promoted environment directly; no Compose project
# is reconciled and no maintenance process receives a mutable checkout image.
exec /usr/bin/python3 /usr/local/libexec/project-snow/maintenance.py backup
