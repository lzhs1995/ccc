#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "${PYTHON:-$(command -v python3)}" "$root/scripts/render_launchagents.py" "$@"
