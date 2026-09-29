#!/bin/bash
# Double-click to STOP cmux-janitor. Takes effect immediately.
# cmux returns to its stock behaviour; nothing needs to be undone.
#
# Pausing is always allowed, so this stays a thin wrapper: it delegates to
# cmux-janitorctl for one audit trail, but falls back to writing the sentinel
# directly.  Refusing to pause because a helper is missing would be the wrong
# failure direction for a stop button.
D="$HOME/.config/cmux-janitor"
CTL="$D/cmux-janitorctl"

printf '\n'
if [ -x "$CTL" ]; then
  "$CTL" pause 2>&1 | /usr/bin/sed 's/^/  /'
else
  /usr/bin/touch "$D/DISABLED"
  printf '  cmux-janitor 已停用 (直接写入哨兵)\n'
fi
printf '\n'
printf '  cmux 恢复默认行为，孤儿文件不再被清理。\n'
printf '  重新启用：双击 ENABLE.command\n\n'
printf '  哨兵文件: %s\n\n' "$D/DISABLED"
printf '  (按任意键关闭)'
if [ -t 0 ]; then read -r -n 1 -s; else printf "\n"; fi
