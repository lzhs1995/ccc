#!/bin/bash
# Double-click to RE-ENABLE cmux-janitor after using DISABLE.command.
#
# This delegates to cmux-janitorctl instead of removing the sentinel itself.
# Removing DISABLED directly was the real gap: it never looked at
# GUARD_TRIPPED, so double-clicking this file after a trip would resurrect a
# janitor the guard had deliberately stopped.  `ctl resume` refuses in that
# case, and also when config.env's MODE no longer matches the authorised
# baseline, or when the guard's own state has gone stale.
D="$HOME/.config/cmux-janitor"
CTL="$D/cmux-janitorctl"

printf '\n'
if [ ! -x "$CTL" ]; then
  printf '  无法启用: 控制器缺失或不可执行\n'
  printf '  %s\n\n' "$CTL"
  printf '  (按任意键关闭)'
  if [ -t 0 ]; then read -r -n 1 -s; else printf "\n"; fi
  exit 3
fi

OUT=$("$CTL" resume 2>&1)
RC=$?
printf '%s\n' "$OUT" | /usr/bin/sed 's/^/  /'
printf '\n'
if [ "$RC" -eq 0 ]; then
  printf '  下次运行: 30 分钟内\n'
  printf '  停用: 双击 DISABLE.command\n'
else
  printf '  janitor 仍处于停用状态 (未做任何改动)\n'
fi
printf '\n  (按任意键关闭)'
if [ -t 0 ]; then read -r -n 1 -s; else printf "\n"; fi
exit "$RC"
