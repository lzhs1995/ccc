#!/bin/bash
# Scheduled expiry shares all gates and the sweep mutex; never delete by glob.
set -euo pipefail
exec /opt/homebrew/bin/python3 -B "$HOME/.config/cmux-janitor/cmux-janitorctl" expire
