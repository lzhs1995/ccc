#!/bin/bash
# Show what cmux-janitor is doing right now. Read-only.
#
# Everything published here comes from `cmux-janitorctl status`, which is the
# single public contract. The old version parsed config.env directly and printed
# QUARANTINE_KEEP_HOURS as the retention promise while the janitor script read a
# different key entirely -- so it told the user "48 hours" during a period when
# batches were actually being deleted after 24. A status page must never be able
# to disagree with the executor about what will happen.
set -uo pipefail
D="$HOME/.config/cmux-janitor"
CTL="$D/cmux-janitorctl"

printf '\n=== cmux-janitor status ===\n\n'

if [ ! -x "$CTL" ]; then
  printf '  控制器缺失: %s\n' "$CTL"
  printf '  无法报告状态（fail closed，不猜测）。\n\n'
  exit 3
fi

"$CTL" status
rc=$?
printf '\n'

if [ "$rc" -ne 0 ]; then
  printf '  控制器返回 %s（状态不可信，按 fail closed 处理）。\n\n' "$rc"
  exit "$rc"
fi

printf '  完整 JSON:  %s status --json\n' "$CTL"
printf '  手动清扫:   %s run --manual\n' "$CTL"
printf '  暂停/恢复:  %s pause | %s resume\n' "$CTL" "$CTL"
printf '  还原全部:   %s/uninstall.sh\n\n' "$D"
