#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
user=${USER:-$(id -un)}
python=${PYTHON:-$(command -v python3)}
config=${CCC_CONFIG:-"$HOME/.config/ccc"}
out=${1:-"$HOME/Library/LaunchAgents"}
mkdir -p "$out"
for name in LaunchAgent janitor janitor-guard; do
  input="$root/templates/$name.plist.template"
  case "$name" in
    LaunchAgent) target="$out/com.$user.ccc-continue.plist" ;;
    janitor) target="$out/com.$user.ccc-janitor.plist" ;;
    janitor-guard) target="$out/com.$user.ccc-janitor-guard.plist" ;;
  esac
  sed -e "s#{{USER}}#$user#g" -e "s#{{PYTHON}}#$python#g" \
      -e "s#{{CCC_ROOT}}#$root#g" -e "s#{{CCC_CONFIG}}#$config#g" \
      "$input" > "$target"
  printf '%s\n' "$target"
done
