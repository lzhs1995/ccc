#!/bin/bash
# cmux-janitor guard — a circuit breaker, not a reporter.
#
# Runs every minute. Checks red lines that mean "the janitor did something it
# must never do". On any violation it TRIPS: writes the janitor's own DISABLED
# sentinel first, explains second.
#
# USAGE
#   ./guard.sh                        one check cycle (launchd calls this)
#   ./guard.sh --status               show baseline vs current, change nothing
#   ./guard.sh --rearm                clear a trip and re-baseline
#   ./guard.sh --rearm --accept-mode dry|apply
#                                     re-baseline while deliberately accepting a
#                                     MODE change (maintenance protocol, R2-3)
#
# The guard never removes DISABLED. Restoring the janitor is a separate,
# deliberate act (cmux-janitorctl resume), so a trip can never self-heal.
set -uo pipefail

STAT=/usr/bin/stat
FIND=/usr/bin/find
GREP=/usr/bin/grep
WC=/usr/bin/wc
TR=/usr/bin/tr
LS=/bin/ls
DATE=/bin/date
TOUCH=/usr/bin/touch
MV=/bin/mv
OSA=/usr/bin/osascript
SORT=/usr/bin/sort
SHASUM=/usr/bin/shasum
BASENAME=/usr/bin/basename
MKTEMP=/usr/bin/mktemp
RM=/bin/rm

JD="$HOME/.config/cmux-janitor"
CM="$HOME/.cmuxterm"
LIVE="$CM/agent-turn-diff-baselines.json"
LOCK="$CM/agent-turn-diff-baselines.json.lock"
PUB="$CM/agent-turn-diff-baseline-snapshots"
CMUX_CFG="$HOME/.config/cmux"
CMUX_HOOKS="$HOME/.cmux/hooks"
Q="$HOME/.cmuxterm-janitor-quarantine"
BASE="$JD/guard.baseline"
GLOG="$JD/guard.log"
TRIPPED="$JD/GUARD_TRIPPED"
DISABLED="$JD/DISABLED"
STATE="$JD/guard-state.json"
LOG_MAX=262144

# Schema 2 adds BASE_SCHEMA and BASE_Q_FINGERPRINT. A schema mismatch is not a
# violation of the janitor's behaviour, so it must not trip; it is an operator
# problem and the guard fails closed by refusing to judge.
SCHEMA_VERSION=2

# ---------- argument parsing (explicit; unknown args are rejected) ----------
ACTION=check
ACCEPT_MODE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --status) ACTION=status ;;
    --rearm)  ACTION=rearm ;;
    --accept-mode)
      shift
      [ "$#" -gt 0 ] || { printf 'guard.sh: --accept-mode needs dry|apply\n' >&2; exit 2; }
      case "$1" in
        dry|apply) ACCEPT_MODE="$1" ;;
        *) printf 'guard.sh: --accept-mode must be dry or apply, got: %s\n' "$1" >&2; exit 2 ;;
      esac
      ;;
    check) ACTION=check ;;
    *) printf 'guard.sh: unknown argument: %s\n' "$1" >&2
       printf 'usage: guard.sh [--status|--rearm [--accept-mode dry|apply]]\n' >&2
       exit 2 ;;
  esac
  shift
done

if [ -n "$ACCEPT_MODE" ] && [ "$ACTION" != "rearm" ]; then
  printf 'guard.sh: --accept-mode is only valid with --rearm\n' >&2
  exit 2
fi

# A guard that cannot write its own baseline, log, or state cannot judge
# anything, and exiting 0 there would report health it never established.
# Fail closed and say so on stderr, where launchd captures it.
if [ ! -d "$JD" ] || [ ! -w "$JD" ]; then
  printf 'guard.sh: guard directory missing or not writable: %s\n' "$JD" >&2
  exit 3
fi

ts()  { "$DATE" '+%F %T'; }
iso() { "$DATE" -u '+%Y-%m-%dT%H:%M:%SZ'; }
glog() { printf '%s  %s\n' "$(ts)" "$*" >> "$GLOG"; }

rotate() {
  [ -f "$GLOG" ] || return 0
  local sz
  sz=$("$STAT" -f%z "$GLOG" 2>/dev/null || echo 0)
  if [ "$sz" -gt "$LOG_MAX" ]; then
    "$MV" -f "$GLOG" "$GLOG.1" 2>/dev/null
    : > "$GLOG"
  fi
}

# ---------- quarantine fingerprint (R2-3 / R7) ----------
# Sorted basename+inode+mtime of the top-level batch directories only. Inode is
# included so replacing a batch with a same-named one is still a change; the
# per-item contents are deliberately not walked (too slow for a 60s cycle and
# not needed: any item move updates the batch mtime).
q_fingerprint() {
  if [ ! -d "$Q" ]; then
    printf 'absent'
    return 0
  fi
  local line
  line=$("$FIND" "$Q" -mindepth 1 -maxdepth 1 -type d 2>/dev/null \
    | "$SORT" \
    | while IFS= read -r d; do
        [ -n "$d" ] || continue
        printf '%s|%s|%s\n' \
          "$("$BASENAME" "$d")" \
          "$("$STAT" -f%i "$d" 2>/dev/null || echo x)" \
          "$("$STAT" -f%m "$d" 2>/dev/null || echo x)"
      done \
    | "$SHASUM" -a 256 2>/dev/null | { read -r h _; printf '%s' "$h"; })
  printf '%s' "${line:-error}"
}

read_now() {
  NOW_LOCK_SIZE=$($STAT -f%z "$LOCK" 2>/dev/null || echo MISSING)
  NOW_LOCK_MTIME=$($STAT -f%m "$LOCK" 2>/dev/null || echo MISSING)
  NOW_LIVE_SIZE=$($STAT -f%z "$LIVE" 2>/dev/null || echo MISSING)
  NOW_PUB_COUNT=$($FIND "$PUB" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | $WC -l | $TR -d ' ')
  NOW_CFG_COUNT=$($LS -A "$CMUX_CFG" 2>/dev/null | $WC -l | $TR -d ' ')
  NOW_HOOKS_COUNT=$($LS -A "$CMUX_HOOKS" 2>/dev/null | $WC -l | $TR -d ' ')
  NOW_MODE=$($GREP -E '^MODE=' "$JD/config.env" 2>/dev/null | /usr/bin/tail -1 | /usr/bin/cut -d= -f2)
  NOW_USE_QUARANTINE=$($GREP -E '^USE_QUARANTINE=' "$JD/config.env" 2>/dev/null | /usr/bin/tail -1 | /usr/bin/cut -d= -f2)
  NOW_Q_EXISTS=$([ -d "$Q" ] && echo yes || echo no)
  NOW_Q_FINGERPRINT=$(q_fingerprint)
}

write_baseline() {
  # printf, not a heredoc via cat: launchd's PATH is minimal and cat is not
  # referenced by absolute path anywhere else in this script.
  #
  # This is a whole-table rewrite, so BASE_MODE is re-taken from the current
  # config every time. That is exactly why plain --rearm refuses a MODE drift:
  # rebaselining a drifted MODE would silently bless it and retire R6.
  {
    printf '# cmux-janitor guard baseline — written %s\n' "$(ts)"
    printf 'BASE_SCHEMA=%s\n'        "$SCHEMA_VERSION"
    printf 'BASE_LOCK_SIZE=%s\n'     "$NOW_LOCK_SIZE"
    printf 'BASE_LOCK_MTIME=%s\n'    "$NOW_LOCK_MTIME"
    printf 'BASE_PUB_COUNT=%s\n'     "$NOW_PUB_COUNT"
    printf 'BASE_CFG_COUNT=%s\n'     "$NOW_CFG_COUNT"
    printf 'BASE_HOOKS_COUNT=%s\n'   "$NOW_HOOKS_COUNT"
    printf 'BASE_MODE=%s\n'          "$NOW_MODE"
    printf 'BASE_USE_QUARANTINE=%s\n' "$NOW_USE_QUARANTINE"
    printf 'BASE_Q_FINGERPRINT=%s\n' "$NOW_Q_FINGERPRINT"
  } > "$BASE"
}

# ---------- state publication (atomic; consumed only via cmux-janitorctl) ----------
# Bounded by construction: no paths, no batch names, no unbounded arrays.
publish_state() {
  local health="$1" reason="$2" tmp
  tmp=$("$MKTEMP" "$JD/.guard-state.XXXXXX") || return 0
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "observed_at": "%s",\n' "$(iso)"
    printf '  "health": "%s",\n' "$health"
    printf '  "baseline_schema": %s,\n' "${BASE_SCHEMA:-0}"
    printf '  "baseline_written_at": "%s",\n' "$(${STAT} -f '%Sm' -t '%FT%TZ' "$BASE" 2>/dev/null || echo unknown)"
    printf '  "mode": "%s",\n' "${NOW_MODE:-unknown}"
    printf '  "baseline_mode": "%s",\n' "${BASE_MODE:-unknown}"
    printf '  "mode_matches_baseline": %s,\n' \
      "$([ "${NOW_MODE:-x}" = "${BASE_MODE:-y}" ] && echo true || echo false)"
    printf '  "disabled": %s,\n' "$([ -e "$DISABLED" ] && echo true || echo false)"
    printf '  "guard_tripped": %s,\n' "$([ -e "$TRIPPED" ] && echo true || echo false)"
    printf '  "quarantine_fingerprint_matches": %s,\n' \
      "$([ "${NOW_Q_FINGERPRINT:-x}" = "${BASE_Q_FINGERPRINT:-y}" ] && echo true || echo false)"
    printf '  "reason": "%s"\n' "$reason"
    printf '}\n'
  } > "$tmp" 2>/dev/null
  "$MV" -f "$tmp" "$STATE" 2>/dev/null || "$RM" -f "$tmp" 2>/dev/null
}

read_now

# ---------- first run: establish baseline ----------
if [ ! -f "$BASE" ]; then
  write_baseline
  rotate
  glog "BASELINE established schema=$SCHEMA_VERSION lock_mtime=$NOW_LOCK_MTIME pub=$NOW_PUB_COUNT cfg=$NOW_CFG_COUNT hooks=$NOW_HOOKS_COUNT mode=$NOW_MODE"
  BASE_SCHEMA=$SCHEMA_VERSION
  BASE_MODE=$NOW_MODE
  BASE_USE_QUARANTINE=$NOW_USE_QUARANTINE
  BASE_Q_FINGERPRINT=$NOW_Q_FINGERPRINT
  publish_state healthy "baseline established"
  exit 0
fi

. "$BASE"
BASE_SCHEMA="${BASE_SCHEMA:-1}"
BASE_Q_FINGERPRINT="${BASE_Q_FINGERPRINT:-}"
# Baselines written before the safety-config fingerprint existed lack this
# key. Defaulting to 1 is exact, not lenient: 1 is the only value the janitor
# itself accepts (validate_config fails closed on anything else), so an old
# baseline can only ever have been taken against USE_QUARANTINE=1.
BASE_USE_QUARANTINE="${BASE_USE_QUARANTINE:-1}"

# ---------- --rearm ----------
if [ "$ACTION" = "rearm" ]; then
  REARM_FAIL=""

  # R1-R5 preconditions, using the OLD baseline's semantics (not equality where
  # the red line is a floor: published snapshots legitimately rotate 200/201).
  [ "$NOW_LIVE_SIZE" = "MISSING" ] && REARM_FAIL="$REARM_FAIL
  live store missing: $LIVE"
  if [ "$NOW_LIVE_SIZE" != "MISSING" ] && [ "$NOW_LIVE_SIZE" -eq 0 ] 2>/dev/null; then
    REARM_FAIL="$REARM_FAIL
  live store is empty (0 bytes)"
  fi
  [ "$NOW_LOCK_SIZE" = "MISSING" ] && REARM_FAIL="$REARM_FAIL
  lock missing: $LOCK"
  [ "$NOW_LOCK_SIZE" != "MISSING" ] && [ "$NOW_LOCK_SIZE" != "$BASE_LOCK_SIZE" ] \
    && REARM_FAIL="$REARM_FAIL
  lock size changed: $BASE_LOCK_SIZE -> $NOW_LOCK_SIZE"
  [ "$NOW_LOCK_MTIME" != "MISSING" ] && [ "$NOW_LOCK_MTIME" != "$BASE_LOCK_MTIME" ] \
    && REARM_FAIL="$REARM_FAIL
  lock mtime changed: $BASE_LOCK_MTIME -> $NOW_LOCK_MTIME"
  [ "$NOW_PUB_COUNT" -lt 150 ] 2>/dev/null \
    && REARM_FAIL="$REARM_FAIL
  published snapshots collapsed: $NOW_PUB_COUNT (floor 150)"
  [ "$NOW_CFG_COUNT" -lt "$BASE_CFG_COUNT" ] 2>/dev/null \
    && REARM_FAIL="$REARM_FAIL
  ~/.config/cmux items dropped: $BASE_CFG_COUNT -> $NOW_CFG_COUNT"
  [ "$NOW_HOOKS_COUNT" -lt "$BASE_HOOKS_COUNT" ] 2>/dev/null \
    && REARM_FAIL="$REARM_FAIL
  ~/.cmux/hooks items dropped: $BASE_HOOKS_COUNT -> $NOW_HOOKS_COUNT"

  # R9 precondition: rearm must not bless a config the janitor itself refuses.
  [ "${NOW_USE_QUARANTINE:-}" != "1" ] && REARM_FAIL="$REARM_FAIL
  USE_QUARANTINE is ${NOW_USE_QUARANTINE:-<missing>} in config.env; the only rearm-able value is 1"

  # MODE authorization (R2-3).
  if [ -n "$ACCEPT_MODE" ]; then
    # Deliberate MODE change: only with the janitor already paused, and the
    # declared value must match what config.env actually says right now.
    [ -e "$DISABLED" ] || REARM_FAIL="$REARM_FAIL
  --accept-mode requires the janitor to be paused first (DISABLED absent)"
    [ "$ACCEPT_MODE" = "$NOW_MODE" ] || REARM_FAIL="$REARM_FAIL
  --accept-mode $ACCEPT_MODE does not match config.env MODE=$NOW_MODE"
  else
    [ -n "$NOW_MODE" ] && [ "$NOW_MODE" != "$BASE_MODE" ] && REARM_FAIL="$REARM_FAIL
  MODE drifted ($BASE_MODE -> $NOW_MODE); use --rearm --accept-mode $NOW_MODE after pausing"
  fi

  if [ -n "$REARM_FAIL" ]; then
    printf '\n  拒绝 rearm。未通过前置检查:%s\n\n' "$REARM_FAIL"
    glog "REARM REFUSED:$(printf '%s' "$REARM_FAIL" | "$TR" '\n' ';')"
    exit 1
  fi

  /bin/rm -f "$TRIPPED"
  write_baseline
  . "$BASE"
  rotate
  glog "REARMED by user schema=$SCHEMA_VERSION lock_mtime=$NOW_LOCK_MTIME pub=$NOW_PUB_COUNT mode=$NOW_MODE accept_mode=${ACCEPT_MODE:-none} qfp=${NOW_Q_FINGERPRINT:0:12}"
  publish_state healthy "rearmed by user"
  printf '\n  守卫已重置并重新取基线 (schema=%s)。\n' "$SCHEMA_VERSION"
  printf '  MODE=%s  隔离区指纹=%s\n' "$NOW_MODE" "${NOW_Q_FINGERPRINT:0:12}"
  printf '  注意: janitor 的 DISABLED 哨兵未自动清除。\n'
  printf '  确认无误后运行 cmux-janitorctl resume 恢复 janitor。\n\n'
  exit 0
fi

# ---------- --status ----------
if [ "$ACTION" = "status" ]; then
  printf '\n=== cmux-janitor guard status ===\n\n'
  if [ -e "$TRIPPED" ]; then
    printf '  状态: 已跳闸 (%s)\n' "$($STAT -f '%Sm' -t '%F %T' "$TRIPPED")"
    printf '  跳闸原因:\n'
    /usr/bin/sed 's/^/    /' "$TRIPPED"
  else
    printf '  状态: 正常守护中\n'
  fi
  printf '\n  %-22s %-24s %s\n' "项目" "基线" "当前"
  printf '  %-22s %-24s %s\n' "baseline schema" "$BASE_SCHEMA" "$SCHEMA_VERSION"
  printf '  %-22s %-24s %s\n' "lock mtime" "$BASE_LOCK_MTIME" "$NOW_LOCK_MTIME"
  printf '  %-22s %-24s %s\n' "lock size" "$BASE_LOCK_SIZE" "$NOW_LOCK_SIZE"
  printf '  %-22s %-24s %s\n' "已发布快照数" "$BASE_PUB_COUNT" "$NOW_PUB_COUNT"
  printf '  %-22s %-24s %s\n' "~/.config/cmux 项数" "$BASE_CFG_COUNT" "$NOW_CFG_COUNT"
  printf '  %-22s %-24s %s\n' "~/.cmux/hooks 项数" "$BASE_HOOKS_COUNT" "$NOW_HOOKS_COUNT"
  printf '  %-22s %-24s %s\n' "MODE" "$BASE_MODE" "$NOW_MODE"
  printf '  %-22s %-24s %s\n' "USE_QUARANTINE" "$BASE_USE_QUARANTINE" "${NOW_USE_QUARANTINE:-<缺失>}"
  printf '  %-22s %-24s %s\n' "隔离区指纹" "${BASE_Q_FINGERPRINT:0:12}" "${NOW_Q_FINGERPRINT:0:12}"
  printf '  %-22s %-24s %s\n' "活 store 字节" "(允许变化)" "$NOW_LIVE_SIZE"
  printf '  %-22s %-24s %s\n' "隔离区" "(apply 模式才有)" "$NOW_Q_EXISTS"
  printf '  %-22s %-24s %s\n' "janitor 已暂停" "-" "$([ -e "$DISABLED" ] && echo yes || echo no)"
  if [ -s "$GLOG" ]; then
    printf '\n  --- 守卫日志 (仅异常时写入) ---\n'
    /usr/bin/tail -10 "$GLOG" | /usr/bin/sed 's/^/  /'
  else
    printf '\n  守卫日志: 空 (无异常)\n'
  fi
  printf '\n'
  exit 0
fi

# ---------- already tripped: stay tripped ----------
if [ -e "$TRIPPED" ]; then
  publish_state tripped "already tripped; awaiting human --rearm"
  exit 0
fi

# ---------- schema gate: refuse to judge on an unknown baseline ----------
if [ "$BASE_SCHEMA" != "$SCHEMA_VERSION" ]; then
  rotate
  glog "SCHEMA MISMATCH baseline=$BASE_SCHEMA expected=$SCHEMA_VERSION; refusing to judge (run --rearm after review)"
  publish_state schema_mismatch "baseline schema $BASE_SCHEMA != $SCHEMA_VERSION"
  # Non-zero: the guard is no longer protecting anything, so launchd's exit
  # code must not read as healthy.  This is an operator problem, not a janitor
  # violation, so it still must not trip.
  exit 3
fi

VIOLATIONS=""
add() { VIOLATIONS="$VIOLATIONS  $1
"; }

# R1 live store must exist and be non-empty
[ "$NOW_LIVE_SIZE" = "MISSING" ] && add "R1 活 store 文件消失: $LIVE"
[ "$NOW_LIVE_SIZE" != "MISSING" ] && [ "$NOW_LIVE_SIZE" -eq 0 ] 2>/dev/null && add "R1 活 store 被清空 (0 字节)"

# R2 lock must be byte-identical and untouched
[ "$NOW_LOCK_SIZE" = "MISSING" ] && add "R2 lock 文件消失: $LOCK"
[ "$NOW_LOCK_MTIME" != "MISSING" ] && [ "$NOW_LOCK_MTIME" != "$BASE_LOCK_MTIME" ] \
  && add "R2 lock mtime 被改动: $BASE_LOCK_MTIME -> $NOW_LOCK_MTIME"
[ "$NOW_LOCK_SIZE" != "MISSING" ] && [ "$NOW_LOCK_SIZE" != "$BASE_LOCK_SIZE" ] \
  && add "R2 lock 大小被改动: $BASE_LOCK_SIZE -> $NOW_LOCK_SIZE"

# R3 published snapshots may rotate, but must never collapse
# cmux prunes to 7d/200; a drop below 150 means something deleted them
if [ "$NOW_PUB_COUNT" -lt 150 ]; then
  add "R3 已发布快照暴跌: 基线 $BASE_PUB_COUNT -> 当前 $NOW_PUB_COUNT (阈值 150)"
fi

# R4 cmux's own config must be untouched
[ "$NOW_CFG_COUNT" -lt "$BASE_CFG_COUNT" ] 2>/dev/null \
  && add "R4 ~/.config/cmux 项数减少: $BASE_CFG_COUNT -> $NOW_CFG_COUNT"

# R5 cmux hooks must be untouched
[ "$NOW_HOOKS_COUNT" -lt "$BASE_HOOKS_COUNT" ] 2>/dev/null \
  && add "R5 ~/.cmux/hooks 项数减少: $BASE_HOOKS_COUNT -> $NOW_HOOKS_COUNT"

# R6 MODE must not change without the user doing it
[ -n "$NOW_MODE" ] && [ "$NOW_MODE" != "$BASE_MODE" ] \
  && add "R6 MODE 未经预期地变了: $BASE_MODE -> $NOW_MODE (合法变更: 先暂停再 guard.sh --rearm --accept-mode $NOW_MODE)"

# R9 safety configuration must not drift: USE_QUARANTINE is the reversibility
# guarantee for every disposal, so any change (or any value other than 1) is a
# violation. The janitor itself fails closed on non-1, so a drift seen here
# means someone edited config.env behind the guard's back.
if [ "${NOW_USE_QUARANTINE:-}" != "$BASE_USE_QUARANTINE" ] || [ "${NOW_USE_QUARANTINE:-}" != "1" ]; then
  add "R9 USE_QUARANTINE 漂移: 基线 $BASE_USE_QUARANTINE -> 当前 ${NOW_USE_QUARANTINE:-<缺失>} (唯一合法值是 1)"
fi

# R7 dry mode must not change the quarantine area
# Presence alone is legitimate: apply may have built it earlier and the batches
# are simply waiting to expire. What must never happen in dry is a CHANGE.
if [ "$NOW_MODE" = "dry" ] && [ -n "$BASE_Q_FINGERPRINT" ] \
   && [ "$NOW_Q_FINGERPRINT" != "$BASE_Q_FINGERPRINT" ]; then
  add "R7 dry 模式下隔离区发生变化 (指纹 ${BASE_Q_FINGERPRINT:0:12} -> ${NOW_Q_FINGERPRINT:0:12})"
fi

# R8 the janitor must never report an outside-scope or protected-name hit
if [ -f "$JD/janitor.log" ]; then
  n=$($GREP -c 'SKIP-OUTSIDE\|SKIP-PROTECTED-NAME' "$JD/janitor.log" 2>/dev/null || echo 0)
  [ "$n" -gt 0 ] 2>/dev/null && add "R8 janitor 日志出现越界/受保护命中 ($n 次)"
fi

# ---------- verdict ----------
if [ -n "$VIOLATIONS" ]; then
  # TRIP: stop the janitor first, explain second.
  $TOUCH "$DISABLED" 2>/dev/null
  {
    printf 'TRIPPED %s\n' "$(ts)"
    printf '%s' "$VIOLATIONS"
    printf 'ACTION janitor 已被自动停用 (DISABLED 哨兵已写入)\n'
    printf 'NEXT   调查后运行 %s/guard.sh --rearm，再 cmux-janitorctl resume\n' "$JD"
  } > "$TRIPPED"
  rotate
  glog "TRIPPED — janitor auto-disabled. Violations:"
  printf '%s' "$VIOLATIONS" | while IFS= read -r v; do
    [ -n "$v" ] && glog "  $v"
  done
  publish_state tripped "$(printf '%s' "$VIOLATIONS" | "$TR" '\n' ';' | /usr/bin/sed 's/"/\\"/g')"
  $OSA -e 'display notification "已自动停用 janitor，请查看 guard.sh --status" with title "cmux-janitor 守卫跳闸" sound name "Basso"' >/dev/null 2>&1
  exit 1
fi

# healthy: silent except for the machine-readable state file.
publish_state healthy ""
exit 0
