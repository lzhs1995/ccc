#!/bin/bash
# cmux-janitor — reclaim orphaned cmux diff-baseline artifacts under ~/.cmuxterm
#
# WHAT IT TOUCHES
#   ~/.cmuxterm/*.sb-*                                             interrupted atomic-write temp copies
#   ~/.cmuxterm/agent-turn-diff-baseline-snapshots-staging/<UUID>/  never-published snapshot dirs
#
# WHAT IT NEVER TOUCHES
#   agent-turn-diff-baselines.json          live store
#   agent-turn-diff-baselines.json.lock     lock
#   agent-turn-diff-baseline-snapshots/     published snapshots
#   ~/.config/cmux/                         cmux's own config
#   anything the live store still references
#
# It does not patch, configure, or signal cmux. cmux is unaware it exists.
# Removing this script restores stock cmux behaviour with nothing to undo.
#
# KILL SWITCH:  touch ~/.config/cmux-janitor/DISABLED
#
# Absolute tool paths throughout: launchd runs with a minimal PATH, and an
# interactive shell may resolve `find` to bfs, which rejects some BSD flags.
set -uo pipefail

FIND=/usr/bin/find
STAT=/usr/bin/stat
LSOF=/usr/sbin/lsof
MV=/bin/mv
RM=/bin/rm
MKDIR=/bin/mkdir
RMDIR=/bin/rmdir
DATE=/bin/date
WC=/usr/bin/wc
SORT=/usr/bin/sort
GREP=/usr/bin/grep
AWK=/usr/bin/awk
BASENAME=/usr/bin/basename
MKTEMP=/usr/bin/mktemp
CP=/bin/cp
SLEEP=/bin/sleep
TR=/usr/bin/tr
HEAD=/usr/bin/head
PS=/bin/ps
DU=/usr/bin/du
CUT=/usr/bin/cut
TOUCH=/usr/bin/touch
SED=/usr/bin/sed

JANITOR_DIR="$HOME/.config/cmux-janitor"
CM="$HOME/.cmuxterm"
STAGING="$CM/agent-turn-diff-baseline-snapshots-staging"
LIVE="$CM/agent-turn-diff-baselines.json"
LOG="$JANITOR_DIR/janitor.log"
DISABLED="$JANITOR_DIR/DISABLED"
MUTEX="$JANITOR_DIR/.janitor.mutex"
MUTEX_OWNER="$MUTEX/owner"
STATE="$JANITOR_DIR/janitor-state.json"
METRICS="$JANITOR_DIR/metrics.jsonl"

# Batch metadata filename. R2-4: uninstall.sh must skip this name explicitly,
# because `find -mindepth 2 -maxdepth 2` does enumerate dot files and the
# non-UUID catch-all would otherwise restore it into ~/.cmuxterm.
BATCH_META=".janitor-batch.json"
BATCH_SCHEMA=1
STATE_SCHEMA=1

# ---------- defaults, overridden by config.env ----------
MODE=dry
SB_MIN_AGE_MIN=10
STAGING_MIN_AGE_MIN=60
MAX_ITEMS_PER_RUN=500
USE_QUARANTINE=1
QUARANTINE_DIR="$HOME/.cmuxterm-janitor-quarantine"
QUARANTINE_KEEP_HOURS=3
LOG_MAX_BYTES=1048576
# Canonical name, matching config.env. The old METRICS_MAX_LINES was an
# internal-only name, so a configured METRICS_KEEP_LINES was silently ignored --
# the same shape of defect as QUARANTINE_RETAIN_HOURS vs QUARANTINE_KEEP_HOURS.
METRICS_KEEP_LINES=2048
METRICS_KEEP_DAYS=14
MUTEX_ORPHAN_GRACE_MIN=5
VERBOSE=0

# TRIGGER is the only run type besides scheduled. There is deliberately no
# `inspect`: DISABLED must stay an absolute kill switch, so nothing may run
# behind GATE 0 just to refresh a display.
TRIGGER=scheduled
DRAIN_MANIFEST=""
MAINTENANCE="$JANITOR_DIR/janitor_maintenance.py"
PYTHON=/opt/homebrew/bin/python3
PREVIEW_MODE=0
PREVIEW_VERBOSE=0
PREVIEW_JSON=0
PREVIEW_REASON=ready
PREVIEW_SCANNED=false

while [ $# -gt 0 ]; do
  case "$1" in
    --manual)    TRIGGER=manual; shift ;;
    --drain-manifest) DRAIN_MANIFEST="${2:?manifest required}"; shift 2 ;;
    --scheduled) TRIGGER=scheduled; shift ;;
    --preview)   PREVIEW_MODE=1; TRIGGER=manual; shift ;;
    --verbose)   PREVIEW_VERBOSE=1; shift ;;
    --json)      PREVIEW_JSON=1; shift ;;
    "")          break ;;
    *)
      printf 'usage: %s [--scheduled|--manual|--preview] [--verbose] [--json]\n' "$0" >&2
      exit 2 ;;
  esac
done

# --verbose only valid with --preview
if [ "$PREVIEW_MODE" = "0" ] && { [ "$PREVIEW_VERBOSE" = "1" ] || [ "$PREVIEW_JSON" = "1" ]; }; then
  printf 'error: --verbose and --json require --preview\n' >&2
  exit 2
fi

CONFIG_ERROR=""
if [ -r "$JANITOR_DIR/config.env" ]; then
  # Fail closed on every retired name rather than ignoring it. Two of these
  # have already caused silent misconfiguration: the old script read
  # QUARANTINE_RETAIN_HOURS while config.env declared QUARANTINE_KEEP_HOURS
  # (so the documented 48h was never in effect), and METRICS_MAX_LINES was an
  # internal-only name while config.env declared METRICS_KEEP_LINES (so a
  # configured retention was silently ignored). A retired key present in
  # config.env is an ambiguity, and ambiguity about retention is a data
  # question -- refuse instead of guessing which name the operator meant.
  for retired in QUARANTINE_RETAIN_HOURS:QUARANTINE_KEEP_HOURS \
                 METRICS_MAX_LINES:METRICS_KEEP_LINES; do
    old="${retired%%:*}"; new="${retired##*:}"
    if LC_ALL=C "$GREP" -qE "^[[:space:]]*${old}=" "$JANITOR_DIR/config.env" 2>/dev/null; then
      CONFIG_ERROR="retired key $old present; use $new only"
      break
    fi
  done
  if [ -z "$CONFIG_ERROR" ]; then
    # A key defined twice is the same ambiguity by a different route: `.` would
    # take the last one, which is not necessarily the one being read.
    for unique in QUARANTINE_KEEP_HOURS METRICS_KEEP_LINES METRICS_KEEP_DAYS; do
      dupes=$(LC_ALL=C "$GREP" -cE "^[[:space:]]*${unique}=" "$JANITOR_DIR/config.env" 2>/dev/null || echo 0)
      if [ "$dupes" -gt 1 ] 2>/dev/null; then
        CONFIG_ERROR="$unique defined $dupes times"
        break
      fi
    done
  fi
  [ -z "$CONFIG_ERROR" ] && . "$JANITOR_DIR/config.env"
fi

ts()   { "$DATE" '+%F %T'; }
iso()  { "$DATE" -u '+%Y-%m-%dT%H:%M:%SZ'; }
log()  { [ "$PREVIEW_MODE" = "1" ] && return 0; printf '%s  %s\n' "$(ts)" "$*" >> "$LOG"; }
vlog() { [ "$VERBOSE" = "1" ] && log "$*"; return 0; }

is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac; }

validate_config() {
  [ -n "$CONFIG_ERROR" ] && return 1
  is_uint "$QUARANTINE_KEEP_HOURS" || { CONFIG_ERROR="QUARANTINE_KEEP_HOURS not a non-negative integer: $QUARANTINE_KEEP_HOURS"; return 1; }
  [ "$QUARANTINE_KEEP_HOURS" -gt 0 ] || { CONFIG_ERROR="QUARANTINE_KEEP_HOURS must be > 0"; return 1; }
  is_uint "$MAX_ITEMS_PER_RUN"      || { CONFIG_ERROR="MAX_ITEMS_PER_RUN not a non-negative integer: $MAX_ITEMS_PER_RUN"; return 1; }
  [ "$MAX_ITEMS_PER_RUN" -gt 0 ]    || { CONFIG_ERROR="MAX_ITEMS_PER_RUN must be > 0"; return 1; }
  is_uint "$SB_MIN_AGE_MIN"         || { CONFIG_ERROR="SB_MIN_AGE_MIN invalid: $SB_MIN_AGE_MIN"; return 1; }
  is_uint "$STAGING_MIN_AGE_MIN"    || { CONFIG_ERROR="STAGING_MIN_AGE_MIN invalid: $STAGING_MIN_AGE_MIN"; return 1; }
  # Retention bounds are validated like every other number: an unusable value
  # must not fall back to a default silently, because the whole point of the
  # key is that the operator chose it.
  is_uint "$METRICS_KEEP_LINES"     || { CONFIG_ERROR="METRICS_KEEP_LINES not a non-negative integer: $METRICS_KEEP_LINES"; return 1; }
  [ "$METRICS_KEEP_LINES" -gt 0 ]   || { CONFIG_ERROR="METRICS_KEEP_LINES must be > 0"; return 1; }
  is_uint "$METRICS_KEEP_DAYS"      || { CONFIG_ERROR="METRICS_KEEP_DAYS not a non-negative integer: $METRICS_KEEP_DAYS"; return 1; }
  [ "$METRICS_KEEP_DAYS" -gt 0 ]    || { CONFIG_ERROR="METRICS_KEEP_DAYS must be > 0"; return 1; }
  case "$MODE" in
    dry|apply) ;;
    *) CONFIG_ERROR="MODE must be dry or apply, got: $MODE"; return 1 ;;
  esac
  # J2 (2026-09-01): every scheduled/manual disposal must be reversible.
  # USE_QUARANTINE=0 (direct rm -rf) is retired: a value other than exactly 1
  # fails closed here, so no run can reach a destructive path with the
  # quarantine off. Read-only checks live in cmux-janitorctl status/inspect.
  case "$USE_QUARANTINE" in
    1) ;;
    0) CONFIG_ERROR="USE_QUARANTINE=0 (direct delete) is retired; disposal requires the quarantine (USE_QUARANTINE=1)"; return 1 ;;
    *) CONFIG_ERROR="USE_QUARANTINE must be exactly 1, got: $USE_QUARANTINE"; return 1 ;;
  esac
  return 0
}

rotate_log() {
  [ "$PREVIEW_MODE" = "1" ] && return 0
  [ -f "$LOG" ] || return 0
  local sz
  sz=$("$STAT" -f%z "$LOG" 2>/dev/null || echo 0)
  if [ "$sz" -gt "$LOG_MAX_BYTES" ]; then
    "$MV" -f "$LOG" "$LOG.1" 2>/dev/null
    : > "$LOG"
    log "log rotated (previous ${sz}B -> janitor.log.1)"
  fi
}

# ---------- preview formatting functions ----------
format_bytes() {
  local bytes="$1"
  if [ "$bytes" -lt 1024 ]; then
    printf '%d B' "$bytes"
  elif [ "$bytes" -lt 1048576 ]; then
    awk "BEGIN {printf \"%.1f KB\", $bytes/1024}"
  elif [ "$bytes" -lt 1073741824 ]; then
    awk "BEGIN {printf \"%.1f MB\", $bytes/1048576}"
  else
    awk "BEGIN {printf \"%.1f GB\", $bytes/1073741824}"
  fi
}

format_age() {
  local minutes="$1"
  if [ "$minutes" -lt 60 ]; then
    printf '%d min' "$minutes"
  elif [ "$minutes" -lt 1440 ]; then
    printf '%d hours' "$((minutes / 60))"
  else
    printf '%d days' "$((minutes / 1440))"
  fi
}

print_preview() {
  if [ "$PREVIEW_JSON" = "1" ]; then
    # Only fixed reason codes and validated counters cross the JSON boundary.
    printf '{"schema_version":1,"read_only":true,"reason":"%s","observed_at":"%s","scanned":%s,"safety_complete":%s,' \
      "$PREVIEW_REASON" "$(iso)" "$PREVIEW_SCANNED" "$SAFETY_COMPLETE"
    if [ "$PREVIEW_SCANNED" != "true" ]; then
      printf '"counts":null,"quarantine":null,"limit":null}\n'
      return 0
    fi
    printf '"counts":{"raw_sb":%s,"raw_staging":%s,"protected_directories":%s,"eligible":%s,"selected":%s,"would_dispose":%s,"would_dispose_sb":%s,"would_dispose_staging":%s,"skipped_moving":%s,"skipped_fresh":%s,"skipped_held":%s,"skipped_outside":%s},' \
      "$C_RAW_SB" "$C_RAW_STAGING" "$C_PROTECTED_DIRS" "$C_ELIGIBLE" "$C_SELECTED" \
      "$C_WOULD_DISPOSE" "$C_WOULD_SB" "$C_WOULD_STAGING" \
      "$C_SKIP_MOVING" "$C_SKIP_FRESH" "$C_SKIP_HELD" "$C_SKIP_OUTSIDE"
    printf '"quarantine":{"batch_count":%s,"would_expire_batches":%s,"would_expire_items":%s},"limit":%s}\n' \
      "$Q_BATCHES" "$C_WOULD_EXP_B" "$C_WOULD_EXP_I" "$MAX_ITEMS_PER_RUN"
    return 0
  fi
  printf '\n=== Janitor Preview ===\n'
  printf 'Status: %s (read-only snapshot; apply rechecks every gate)\n' "$PREVIEW_REASON"
  if [ "$PREVIEW_SCANNED" != "true" ]; then
    printf 'Candidates: unknown (scan not performed)\n'
    return 0
  fi
  printf 'Run ID: %s\n' "$RUN_ID"
  printf 'Config: MODE=%s, SB_MIN_AGE_MIN=%s, STAGING_MIN_AGE_MIN=%s\n\n' \
    "$MODE" "$SB_MIN_AGE_MIN" "$STAGING_MIN_AGE_MIN"

  if [ "$PREVIEW_VERBOSE" = "1" ]; then
    if [ "${#PREVIEW_SB_PATHS[@]}" -gt 0 ]; then
      printf '[SB Temp Files]\n'
      local limit=10 i=0
      for path in "${PREVIEW_SB_PATHS[@]}"; do
        [ "$i" -ge "$limit" ] && break
        printf '  Age: %s | Size: %s | %s\n' \
          "$(format_age "${PREVIEW_SB_AGES[$i]}")" \
          "$(format_bytes "${PREVIEW_SB_SIZES[$i]}")" \
          "$path"
        i=$((i + 1))
      done
      if [ "${#PREVIEW_SB_PATHS[@]}" -gt "$limit" ]; then
        printf '  ... (truncated, showing first %d of %d)\n' "$limit" "${#PREVIEW_SB_PATHS[@]}"
      fi
      printf '\n'
    fi

    if [ "${#PREVIEW_STAGING_PATHS[@]}" -gt 0 ]; then
      printf '[Staging Directories]\n'
      local limit=10 i=0
      for path in "${PREVIEW_STAGING_PATHS[@]}"; do
        [ "$i" -ge "$limit" ] && break
        printf '  Age: %s | Size: %s | %s\n' \
          "$(format_age "${PREVIEW_STAGING_AGES[$i]}")" \
          "$(format_bytes "${PREVIEW_STAGING_SIZES[$i]}")" \
          "$path"
        i=$((i + 1))
      done
      if [ "${#PREVIEW_STAGING_PATHS[@]}" -gt "$limit" ]; then
        printf '  ... (truncated, showing first %d of %d)\n' "$limit" "${#PREVIEW_STAGING_PATHS[@]}"
      fi
      printf '\n'
    fi

    if [ "${Q_BATCHES:-0}" -gt 0 ]; then
      printf '[Quarantine Batches]\n'
      printf '  Total batches: %d | Total size: %s\n' "$Q_BATCHES" "$(format_bytes "$Q_BYTES")"
      if [ "${Q_OLDEST_JSON:-null}" != "null" ]; then
        printf '  Oldest sealed: %s\n' "$Q_OLDEST_JSON"
      fi
      if [ "${Q_NEXT_JSON:-null}" != "null" ]; then
        printf '  Next to expire: %s\n' "$Q_NEXT_JSON"
      fi
      printf '\n'
    fi
  fi

  printf '=== Summary ===\n'
  printf '%-20s | %5s | %10s | %13s\n' 'Category' 'Count' 'Total Size' 'Would Dispose'
  printf '%s\n' '------------------------------------------------------------'
  printf '%-20s | %5d | %10s | %13d\n' \
    'SB Temp Files' "$C_RAW_SB" "$(format_bytes "$B_SB_TOTAL")" "$C_WOULD_SB"
  printf '%-20s | %5d | %10s | %13d\n' \
    'Staging Directories' "$C_RAW_STAGING" "$(format_bytes "$B_STAGING_TOTAL")" "$C_WOULD_STAGING"
  printf '%s\n' '------------------------------------------------------------'
  local total_count=$((C_RAW_SB + C_RAW_STAGING))
  local total_bytes=$((B_SB_TOTAL + B_STAGING_TOTAL))
  printf '%-20s | %5d | %10s | %13d\n' \
    'Total' "$total_count" "$(format_bytes "$total_bytes")" "$C_WOULD_DISPOSE"
  printf 'Protected directories: %s | Eligible: %s | Selected: %s (cap %s)\n' \
    "$C_PROTECTED_DIRS" "$C_ELIGIBLE" "$C_SELECTED" "$MAX_ITEMS_PER_RUN"
  printf 'Quarantine: %s batches | Would expire: %s batches / %s items\n' \
    "$Q_BATCHES" "$C_WOULD_EXP_B" "$C_WOULD_EXP_I"

  if [ "${C_SKIP_FRESH:-0}" -gt 0 ] || [ "${C_SKIP_HELD:-0}" -gt 0 ] || [ "${C_PROTECTED:-0}" -gt 0 ]; then
    printf '\nSkipped:\n'
    [ "${C_SKIP_FRESH:-0}" -gt 0 ] && printf '  - %d items too fresh\n' "$C_SKIP_FRESH"
    [ "${C_SKIP_HELD:-0}" -gt 0 ] && printf '  - %d items held by process\n' "$C_SKIP_HELD"
    [ "$C_PROTECTED_DIRS" -gt 0 ] && printf '  - %d directories protected by live store\n' "$C_PROTECTED_DIRS"
  fi

  printf '\nNext Steps:\n'
  if [ "$MODE" = "dry" ]; then
    printf '  To apply: Set MODE=apply in %s/config.env\n' "$JANITOR_DIR"
    printf '  Then run: %s --manual\n' "$0"
  else
    printf '  Current MODE=apply. Run --manual to execute cleanup.\n'
  fi
}

# ---------- state publication ----------
# R2-1: safety_complete is the ONLY gate on disposal. Byte totals carry their
# own precision and are display-only, so an estimated size can never stop a
# run whose safety gates all passed exactly.
# R2-2: this is only ever written from inside a real run. GATE 0 exits before
# it, so a paused janitor leaves the state to go stale on purpose; ctl
# composes liveness from the sentinels instead.
# R2-6: bounded. No paths, no file names, no per-batch list.
publish_state() {
  [ "$PREVIEW_MODE" = "1" ] && return 0
  local phase="$1" err="$2"
  local tmp
  tmp=$("$MKTEMP" "$JANITOR_DIR/.state.XXXXXX") || return 0
  {
    printf '{\n'
    printf '  "schema_version": %s,\n' "$STATE_SCHEMA"
    printf '  "observed_at": "%s",\n' "$(iso)"
    printf '  "run_id": "%s",\n' "$RUN_ID"
    printf '  "trigger": "%s",\n' "$TRIGGER"
    if [ -n "$DRAIN_MANIFEST" ]; then
      "$PYTHON" -B -c 'import json,sys; print("  \"drain_manifest\": " + json.dumps(sys.argv[1]) + ",")' "$DRAIN_MANIFEST"
    fi
    printf '  "phase": "%s",\n' "$phase"
    printf '  "mode": "%s",\n' "$MODE"
    printf '  "safety_complete": %s,\n' "$SAFETY_COMPLETE"
    printf '  "counts": {\n'
    printf '    "raw_sb": %s,\n'            "${C_RAW_SB:-0}"
    printf '    "raw_staging": %s,\n'       "${C_RAW_STAGING:-0}"
    printf '    "eligible": %s,\n'          "${C_ELIGIBLE:-0}"
    printf '    "selected": %s,\n'          "${C_SELECTED:-0}"
    printf '    "would_dispose": %s,\n'     "${C_WOULD_DISPOSE:-0}"
    printf '    "disposed": %s,\n'          "${C_DISPOSED:-0}"
    printf '    "would_expire_batches": %s,\n' "${C_WOULD_EXP_B:-0}"
    printf '    "would_expire_items": %s,\n'   "${C_WOULD_EXP_I:-0}"
    printf '    "expired_batches": %s,\n'   "${C_EXP_B:-0}"
    printf '    "expired_items": %s,\n'     "${C_EXP_I:-0}"
    printf '    "skipped_moving": %s,\n'    "${C_SKIP_MOVING:-0}"
    printf '    "skipped_fresh": %s,\n'     "${C_SKIP_FRESH:-0}"
    printf '    "skipped_held": %s,\n'      "${C_SKIP_HELD:-0}"
    printf '    "skipped_outside": %s,\n'   "${C_SKIP_OUTSIDE:-0}"
    printf '    "protected_ids": %s\n'      "${C_PROTECTED:-0}"
    printf '  },\n'
    printf '  "selected_bytes": {"value": %s, "precision": "%s"},\n' \
           "${B_SELECTED:-0}" "${P_SELECTED:-unknown}"
    printf '  "expired_bytes": {"value": %s, "precision": "%s"},\n' \
           "${B_EXPIRED:-0}" "${P_EXPIRED:-unknown}"
    printf '  "quarantine": {\n'
    printf '    "batch_count": %s,\n'  "${Q_BATCHES:-0}"
    printf '    "bytes": {"value": %s, "precision": "%s"},\n' "${Q_BYTES:-0}" "${Q_PRECISION:-unknown}"
    printf '    "oldest_sealed_at": %s,\n' "${Q_OLDEST_JSON:-null}"
    printf '    "next_expiry_at": %s,\n'   "${Q_NEXT_JSON:-null}"
    printf '    "keep_hours": %s\n'        "$QUARANTINE_KEEP_HOURS"
    printf '  },\n'
    printf '  "limit": %s,\n' "$MAX_ITEMS_PER_RUN"
    printf '  "duration_ms": %s,\n' "$(( ($("$DATE" +%s) - RUN_START_EPOCH) * 1000 ))"
    if [ -n "$err" ]; then
      printf '  "error": "%s"\n' "$err"
    else
      printf '  "error": null\n'
    fi
    printf '}\n'
  } > "$tmp" 2>/dev/null
  "$MV" -f "$tmp" "$STATE" 2>/dev/null || "$RM" -f "$tmp" 2>/dev/null
}

# R2-7: bounded metrics, no paths or file names.
append_metrics() {
  [ "$PREVIEW_MODE" = "1" ] && return 0
  local phase="$1"
  printf '{"observed_at":"%s","run_id":"%s","trigger":"%s","mode":"%s","phase":"%s","safety_complete":%s,"eligible":%s,"selected":%s,"disposed":%s,"would_dispose":%s,"expired_batches":%s,"expired_items":%s,"duration_ms":%s}\n' \
    "$(iso)" "$RUN_ID" "$TRIGGER" "$MODE" "$phase" "$SAFETY_COMPLETE" \
    "${C_ELIGIBLE:-0}" "${C_SELECTED:-0}" "${C_DISPOSED:-0}" "${C_WOULD_DISPOSE:-0}" \
    "${C_EXP_B:-0}" "${C_EXP_I:-0}" \
    "$(( ($("$DATE" +%s) - RUN_START_EPOCH) * 1000 ))" >> "$METRICS" 2>/dev/null || return 0
  local lines
  lines=$("$WC" -l < "$METRICS" 2>/dev/null | "$TR" -d ' ')
  if is_uint "$lines" && [ "$lines" -gt "$METRICS_KEEP_LINES" ]; then
    local tmp
    tmp=$("$MKTEMP" "$JANITOR_DIR/.metrics.XXXXXX") || return 0
    /usr/bin/tail -n "$METRICS_KEEP_LINES" "$METRICS" > "$tmp" 2>/dev/null \
      && "$MV" -f "$tmp" "$METRICS" 2>/dev/null || "$RM" -f "$tmp" 2>/dev/null
  fi
}

RUN_START_EPOCH=$("$DATE" +%s)
RUN_ID="$("$DATE" '+%Y%m%d-%H%M%S')-$$"
SAFETY_COMPLETE=false
C_RAW_SB=0; C_RAW_STAGING=0; C_ELIGIBLE=0; C_SELECTED=0
C_WOULD_DISPOSE=0; C_DISPOSED=0
C_WOULD_SB=0; C_WOULD_STAGING=0; C_PROTECTED_DIRS=0
C_WOULD_EXP_B=0; C_WOULD_EXP_I=0; C_EXP_B=0; C_EXP_I=0
C_SKIP_MOVING=0; C_SKIP_FRESH=0; C_SKIP_HELD=0; C_SKIP_OUTSIDE=0; C_PROTECTED=0
B_SELECTED=0; P_SELECTED=unknown
B_EXPIRED=0; P_EXPIRED=unknown

# Preview data arrays (Bash 4+)
declare -a PREVIEW_SB_PATHS=()
declare -a PREVIEW_SB_AGES=()
declare -a PREVIEW_SB_SIZES=()
declare -a PREVIEW_STAGING_PATHS=()
declare -a PREVIEW_STAGING_AGES=()
declare -a PREVIEW_STAGING_SIZES=()
B_SB_TOTAL=0
B_STAGING_TOTAL=0
Q_BATCHES=0; Q_BYTES=0; Q_PRECISION=unknown
Q_OLDEST_JSON=null; Q_NEXT_JSON=null

# ---------- GATE 0: kill switch, before anything else ----------
# Absolute. Both scheduled and manual exit here; nothing is scanned, no mutex
# is taken, and no state is published. A paused janitor is a silent janitor.
if [ -e "$DISABLED" ]; then
  if [ "$PREVIEW_MODE" = "1" ]; then PREVIEW_REASON=disabled; print_preview; exit 0; fi
  rotate_log
  log "SKIP disabled-by-user (DISABLED sentinel present) trigger=$TRIGGER"
  exit 0
fi
if [ -e "$JANITOR_DIR/GUARD_TRIPPED" ] || [ -e "$JANITOR_DIR/GUARD_UNAVAILABLE" ]; then
  if [ "$PREVIEW_MODE" = "1" ]; then PREVIEW_REASON=guard_unavailable; print_preview; exit 0; fi
  log "SKIP guard tripped or measurement unavailable"
  exit 0
fi

if [ ! -d "$CM" ]; then
  if [ "$PREVIEW_MODE" = "1" ]; then PREVIEW_REASON=store_missing; print_preview; fi
  exit 0
fi
[ "$PREVIEW_MODE" = "1" ] || "$MKDIR" -p "$JANITOR_DIR" 2>/dev/null

if ! validate_config; then
  if [ "$PREVIEW_MODE" = "1" ]; then PREVIEW_REASON=invalid_config; print_preview; exit 1; fi
  rotate_log
  log "ABORT config invalid: $CONFIG_ERROR"
  publish_state error "$CONFIG_ERROR"
  exit 1
fi

# ---------- GATE 1: single instance (mkdir mutex; macOS has no flock(1)) ----------
# R2-5 full race semantics:
#   live owner (pid alive AND start stamp matches) -> busy forever, however old
#   dead owner or mismatched start stamp           -> stale, may reclaim
#   missing/corrupt owner                          -> fail closed under the
#     grace window, reclaim after it; covers a crash between mkdir and the
#     owner write
# `ps -o lstart=` pads its output, and the stamp is compared as a string, so it
# has to be normalised the same way every time: collapse runs of spaces, then
# strip the leading and trailing ones. Piping through `sort` (as this once did)
# collapses nothing and leaves the trailing space in place, so an owner file
# written by anything other than this exact pipeline never matched.
proc_start_of() {
  "$PS" -o lstart= -p "$1" 2>/dev/null | "$TR" -s ' ' | "$SED" 's/^ *//;s/ *$//'
}

# Set alongside the verdict so the log can name the situation. Sharing one
# message between "a run is working" and "nobody claimed this lock yet" would
# leave an operator unable to tell a healthy skip from a crash remnant.
MUTEX_STALE_REASON=""
MUTEX_BUSY_REASON=""

mutex_is_stale() {
  [ -d "$MUTEX" ] || { MUTEX_BUSY_REASON="mutex vanished"; return 1; }
  if [ ! -f "$MUTEX_OWNER" ]; then
    # Nobody recorded ownership. Could be a crash in the tiny window after
    # mkdir, so only reclaim once the orphan grace window has passed.
    if [ -z "$("$FIND" "$MUTEX" -maxdepth 0 -mmin "-$MUTEX_ORPHAN_GRACE_MIN" 2>/dev/null)" ]; then
      MUTEX_STALE_REASON="owner unknown, older than ${MUTEX_ORPHAN_GRACE_MIN}min grace"
      return 0
    fi
    MUTEX_BUSY_REASON="owner unknown, inside ${MUTEX_ORPHAN_GRACE_MIN}min grace"
    return 1
  fi
  local opid ostart
  opid=$(LC_ALL=C "$GREP" -o '"pid"[[:space:]]*:[[:space:]]*[0-9]*' "$MUTEX_OWNER" 2>/dev/null | "$GREP" -o '[0-9]*$')
  ostart=$(LC_ALL=C "$GREP" -o '"process_start"[[:space:]]*:[[:space:]]*"[^"]*"' "$MUTEX_OWNER" 2>/dev/null | "$CUT" -d'"' -f4)
  if ! is_uint "$opid"; then
    if [ -z "$("$FIND" "$MUTEX" -maxdepth 0 -mmin "-$MUTEX_ORPHAN_GRACE_MIN" 2>/dev/null)" ]; then
      MUTEX_STALE_REASON="owner corrupt, older than ${MUTEX_ORPHAN_GRACE_MIN}min grace"
      return 0
    fi
    MUTEX_BUSY_REASON="owner corrupt, inside ${MUTEX_ORPHAN_GRACE_MIN}min grace"
    return 1
  fi
  local now_start
  now_start=$(proc_start_of "$opid")
  if [ -n "$now_start" ] && [ "$now_start" = "$ostart" ]; then
    MUTEX_BUSY_REASON="live owner pid=$opid"
    return 1   # genuinely alive: never steal, no matter how old
  fi
  if [ -n "$now_start" ]; then
    MUTEX_STALE_REASON="pid=$opid reused by a different process"
  else
    MUTEX_STALE_REASON="owner pid=$opid is dead"
  fi
  return 0     # dead pid, or pid reused by a different process
}

write_mutex_owner() {
  local tmp
  tmp=$("$MKTEMP" "$JANITOR_DIR/.owner.XXXXXX") || return 1
  printf '{"pid": %s, "process_start": "%s", "run_id": "%s"}\n' \
    "$$" "$(proc_start_of $$)" "$RUN_ID" > "$tmp" 2>/dev/null
  "$MV" -f "$tmp" "$MUTEX_OWNER" 2>/dev/null || { "$RM" -f "$tmp" 2>/dev/null; return 1; }
  return 0
}

owner_run_id() {
  [ -f "$MUTEX_OWNER" ] || return 1
  LC_ALL=C "$GREP" -o '"run_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$MUTEX_OWNER" 2>/dev/null | "$CUT" -d'"' -f4
}

if [ "$PREVIEW_MODE" = "1" ]; then
  if [ -e "$MUTEX" ] || [ -L "$MUTEX" ]; then
    PREVIEW_REASON=busy; print_preview; exit 0
  fi
  RUN_TMP=$("$MKTEMP" -d "/tmp/cmux-janitor.XXXXXX") || {
    PREVIEW_REASON=scratch_unavailable; print_preview; exit 1;
  }
  trap '"$RM" -rf "$RUN_TMP" 2>/dev/null' EXIT
else
if ! "$MKDIR" "$MUTEX" 2>/dev/null; then
  if mutex_is_stale; then
    "$RM" -rf "$MUTEX" 2>/dev/null
    if ! "$MKDIR" "$MUTEX" 2>/dev/null; then
      rotate_log; log "SKIP another run took the mutex during recovery"; exit 0
    fi
    rotate_log; log "WARN cleared stale mutex ($MUTEX_STALE_REASON)"
  else
    # Two very different situations share this branch, and an operator reading
    # the log needs to tell them apart: a run really is working, versus a lock
    # nobody claimed that we are still too early to steal.
    rotate_log; log "SKIP another run in progress ($MUTEX_BUSY_REASON)"; exit 0
  fi
fi
write_mutex_owner || { "$RM" -rf "$MUTEX" 2>/dev/null; exit 1; }

RUN_TMP=$("$MKTEMP" -d "/tmp/cmux-janitor.XXXXXX") || { "$RM" -rf "$MUTEX" 2>/dev/null; exit 0; }

# EXIT trap only releases a mutex this run still owns. Without the run_id
# check a slow exit could delete a successor's freshly taken lock.
release_mutex() {
  local held
  held=$(owner_run_id 2>/dev/null || true)
  if [ "$held" = "$RUN_ID" ]; then
    "$RM" -rf "$MUTEX" 2>/dev/null
  fi
}
trap '"$RM" -rf "$RUN_TMP" 2>/dev/null; release_mutex' EXIT
fi

rotate_log

SB_CAND="$RUN_TMP/sb.txt"
ST_AGED="$RUN_TMP/staging_aged.txt"
ST_CAND="$RUN_TMP/staging_cand.txt"
PROT="$RUN_TMP/protected.txt"
SEL="$RUN_TMP/selected.txt"
: > "$SB_CAND"; : > "$ST_AGED"; : > "$ST_CAND"; : > "$PROT"; : > "$SEL"

# ---------- collect .sb-* orphans (top level, files only) ----------
# The live store's own name has no ".sb-" infix, so it cannot match this glob.
CANDIDATE_SCAN_OK=true
"$FIND" "$CM" -mindepth 1 -maxdepth 1 -type f -name '*.sb-*' -mmin "+$SB_MIN_AGE_MIN" \
  2>/dev/null | "$SORT" > "$SB_CAND" || CANDIDATE_SCAN_OK=false

# ---------- collect aged, UUID-shaped staging dirs ----------
if [ -d "$STAGING" ]; then
  "$FIND" "$STAGING" -mindepth 1 -maxdepth 1 -type d -mmin "+$STAGING_MIN_AGE_MIN" \
    -regex '.*/[0-9A-Fa-f]\{8\}-[0-9A-Fa-f]\{4\}-[0-9A-Fa-f]\{4\}-[0-9A-Fa-f]\{4\}-[0-9A-Fa-f]\{12\}$' \
    2>/dev/null | "$SORT" > "$ST_AGED" || CANDIDATE_SCAN_OK=false
fi
if [ "$PREVIEW_MODE" = "1" ] && [ "$CANDIDATE_SCAN_OK" != "true" ]; then
  PREVIEW_REASON=candidate_scan_unverified; print_preview; exit 1
fi

C_RAW_SB=$("$WC" -l < "$SB_CAND" | "$TR" -d ' ')
C_RAW_STAGING=$("$WC" -l < "$ST_AGED" | "$TR" -d ' ')
PREVIEW_SCANNED=true

# ---------- preview data collection ----------
if [ "$PREVIEW_MODE" = "1" ]; then
  NOW=$("$DATE" +%s)
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    age_min=$(( (NOW - $("$STAT" -f%m "$f" 2>/dev/null || echo "$NOW")) / 60 ))
    size=$("$STAT" -f%z "$f" 2>/dev/null || echo 0)
    PREVIEW_SB_PATHS+=("$f")
    PREVIEW_SB_AGES+=("$age_min")
    PREVIEW_SB_SIZES+=("$size")
    B_SB_TOTAL=$((B_SB_TOTAL + size))
  done < "$SB_CAND"

  while IFS= read -r d; do
    [ -n "$d" ] || continue
    age_min=$(( (NOW - $("$STAT" -f%m "$d" 2>/dev/null || echo "$NOW")) / 60 ))
    kb=$("$DU" -sk "$d" 2>/dev/null | "$AWK" '{print $1}')
    size=$((kb * 1024))
    PREVIEW_STAGING_PATHS+=("$d")
    PREVIEW_STAGING_AGES+=("$age_min")
    PREVIEW_STAGING_SIZES+=("$size")
    B_STAGING_TOTAL=$((B_STAGING_TOTAL + size))
  done < "$ST_AGED"
fi

# ---------- quarantine inventory (display only; never a disposal input) ----------
# R2-6: aggregate counts plus oldest/next timestamps. No batch names.
survey_quarantine() {
  Q_BATCHES=0; Q_BYTES=0; Q_PRECISION=unknown
  Q_OLDEST_JSON=null; Q_NEXT_JSON=null
  [ "$USE_QUARANTINE" = "1" ] || return 0
  [ -d "$QUARANTINE_DIR" ] || { Q_PRECISION=exact; return 0; }
  local oldest="" b sealed
  while IFS= read -r b; do
    [ -n "$b" ] || continue
    case "$("$BASENAME" "$b")" in .incomplete-*) continue ;; esac
    Q_BATCHES=$((Q_BATCHES+1))
    sealed=$(batch_sealed_epoch "$b")
    if [ -n "$sealed" ]; then
      if [ -z "$oldest" ] || [ "$sealed" -lt "$oldest" ]; then oldest="$sealed"; fi
    fi
  done < <("$FIND" "$QUARANTINE_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
  if [ -n "$oldest" ]; then
    Q_OLDEST_JSON="\"$("$DATE" -u -r "$oldest" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)\""
    Q_NEXT_JSON="\"$("$DATE" -u -r "$((oldest + QUARANTINE_KEEP_HOURS*3600))" '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null)\""
  fi
  local kb
  kb=$("$DU" -sk "$QUARANTINE_DIR" 2>/dev/null | "$AWK" '{print $1}')
  if is_uint "$kb"; then
    Q_BYTES=$((kb * 1024)); Q_PRECISION=exact
  else
    Q_BYTES=0; Q_PRECISION=unknown
  fi
  return 0
}

# sealed_at is the ONLY expiry anchor. R2-4 / consensus: never min(birth,mtime)
# — mv into the batch keeps bumping the directory mtime, so mtime approximates
# "last item moved in" and birth would expire the newest items early.
batch_sealed_epoch() {
  local b="$1" meta="$1/$BATCH_META" v=""
  if [ -f "$meta" ]; then
    v=$(LC_ALL=C "$GREP" -o '"sealed_at_epoch"[[:space:]]*:[[:space:]]*[0-9]*' "$meta" 2>/dev/null | "$GREP" -o '[0-9]*$')
    is_uint "$v" && { printf '%s' "$v"; return 0; }
    return 1          # metadata present but unreadable -> caller fails closed
  fi
  printf ''           # no metadata at all
  return 1
}

write_batch_meta() {
  local dir="$1" items="$2" sealed
  sealed=$("$DATE" +%s)
  local tmp="$dir/.meta.tmp.$$"
  printf '{"schema_version": %s, "run_id": "%s", "created_at_epoch": %s, "sealed_at_epoch": %s, "sealed_at": "%s", "item_count": %s}\n' \
    "$BATCH_SCHEMA" "$RUN_ID" "$BATCH_CREATED_EPOCH" "$sealed" \
    "$("$DATE" -u -r "$sealed" '+%Y-%m-%dT%H:%M:%SZ')" "$items" > "$tmp" 2>/dev/null || return 1
  if [ -n "$DRAIN_MANIFEST" ]; then
    "$PYTHON" -B -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["drain_id"]=json.load(open(sys.argv[2]))["drain_id"]; open(p,"w").write(json.dumps(d)+"\n")' "$tmp" "$DRAIN_MANIFEST" || return 1
  fi
  "$MV" -f "$tmp" "$dir/$BATCH_META" 2>/dev/null || return 1
  "$TOUCH" "$dir" 2>/dev/null
  return 0
}

# ---------- expiry ----------
# R2-1 / P0: dry never mutates the target tree OR the quarantine. The old
# version called this from three exits with no MODE check, so "roll back to
# dry" would have destroyed the very batches kept for rollback.
# Only batches with verifiable metadata expire; missing or corrupt metadata
# fails closed and the batch is left alone.
expire_quarantine() {
  local result
  local args=(expire --shell)
  if [ "$PREVIEW_MODE" = "1" ]; then
    args+=(--preview)
  else
    args+=(--parent-run "$RUN_ID")
  fi
  if [ -n "$DRAIN_MANIFEST" ] && "$PYTHON" -B -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("purge_now") is True else 1)' "$DRAIN_MANIFEST"; then
    args+=(--immediate-manifest "$DRAIN_MANIFEST")
  fi
  if result=$("$PYTHON" -B "$MAINTENANCE" "${args[@]}"); then
    IFS="$(printf '\t')" read -r C_WOULD_EXP_B C_WOULD_EXP_I C_EXP_B C_EXP_I B_EXPIRED <<< "$result"
    P_EXPIRED=exact
  else
    P_EXPIRED=unknown
    log "WARN quarantine expiry refused; see expiry-state.json"
  fi
  return 0
}

# ---------- EARLY EXIT: never read the 1.5GB store when there is nothing to do ----------
if [ "$C_RAW_SB" -eq 0 ] && [ "$C_RAW_STAGING" -eq 0 ]; then
  SAFETY_COMPLETE=true
  expire_quarantine
  survey_quarantine
  if [ "$PREVIEW_MODE" = "1" ]; then
    C_WOULD_DISPOSE=0
    print_preview
    exit 0
  fi
  log "run mode=$MODE trigger=$TRIGGER nothing-to-do expired_batches=$C_EXP_B expired_items=$C_EXP_I would_expire_batches=$C_WOULD_EXP_B"
  publish_state idle ""
  append_metrics idle
  exit 0
fi

# ---------- GATE 2: exclude snapshot IDs the live store still references ----------
# Streamed with grep -o. The 1.5GB store is never parsed as a whole.
LIVE_SCAN_FAILED=0
if [ "$C_RAW_STAGING" -gt 0 ] && [ -f "$LIVE" ] && [ -r "$LIVE" ] && [ -s "$LIVE" ]; then
  LC_ALL=C "$GREP" -o '"untrackedSnapshotId"[[:space:]]*:[[:space:]]*"[0-9A-Fa-f-]\{36\}"' "$LIVE" 2>/dev/null \
    | LC_ALL=C "$GREP" -o '[0-9A-Fa-f-]\{36\}' | "$SORT" -u > "$PROT" || LIVE_SCAN_FAILED=1
fi
C_PROTECTED=$("$WC" -l < "$PROT" | "$TR" -d ' ')

STAGING_SAFE=true
if [ "$C_RAW_STAGING" -gt 0 ]; then
  if [ ! -f "$LIVE" ]; then
    # J1 (2026-09-01): a MISSING live store is not the same claim as an EMPTY
    # one. Missing means we cannot know what is still referenced (wrong mount,
    # migration in flight, store moved) -> fail closed, no staging work.
    # An existing 0-byte store IS a positive statement: nothing referenced.
    log "ABORT-STAGING live store missing at $LIVE; skipping staging (fail closed)"
    : > "$ST_CAND"
    STAGING_SAFE=false
    PREVIEW_REASON=live_store_missing
  elif [ ! -r "$LIVE" ]; then
    log "ABORT-STAGING live store unreadable; skipping staging"
    STAGING_SAFE=false
    PREVIEW_REASON=live_store_unreadable
  elif [ "$LIVE_SCAN_FAILED" = "1" ]; then
    log "ABORT-STAGING extracted 0 snapshot ids or live store scan failed; skipping staging"
    STAGING_SAFE=false
    PREVIEW_REASON=live_store_unverified
  elif [ "$C_PROTECTED" -gt 0 ]; then
    "$AWK" 'NR==FNR{p[$0]=1;next}{n=$0;sub(/.*\//,"",n); if(!(n in p)) print $0}' \
      "$PROT" "$ST_AGED" > "$ST_CAND"
  elif [ -s "$LIVE" ]; then
    # Non-empty store but zero IDs extracted => parse gap, not an empty store.
    # Fail closed: skip all staging work this run.
    log "ABORT-STAGING extracted 0 snapshot ids from a non-empty store; skipping staging"
    : > "$ST_CAND"
    STAGING_SAFE=false
    PREVIEW_REASON=live_store_unverified
  else
    # Store exists and is 0 bytes: a verifiable "no active references".
    "$CP" "$ST_AGED" "$ST_CAND"
  fi
fi
if [ "$STAGING_SAFE" = "true" ]; then
  C_PROTECTED_DIRS=$((C_RAW_STAGING - $("$WC" -l < "$ST_CAND" | "$TR" -d ' ')))
fi

# ---------- global selection: one shared cap, oldest first ----------
# The old script gave .sb-* and staging a 500 cap EACH, so a run could touch
# 1000 items (production logged candidates=514). One queue, ordered by mtime so
# neither class starves — plain `sort` was lexicographic by path.
if ! "$PYTHON" -B "$MAINTENANCE" rank-candidates "$SB_CAND" "$ST_CAND" > "$RUN_TMP/all_cand.txt"; then
  log "ABORT candidate timestamp scan failed"
  exit 1
fi

if [ -n "$DRAIN_MANIFEST" ]; then
  if ! "$PYTHON" -B "$MAINTENANCE" select "$DRAIN_MANIFEST" < "$RUN_TMP/all_cand.txt" > "$RUN_TMP/scoped.txt"; then
    log "ABORT invalid drain manifest"; exit 1
  fi
  "$MV" "$RUN_TMP/scoped.txt" "$RUN_TMP/all_cand.txt"
fi

C_ELIGIBLE=$("$WC" -l < "$RUN_TMP/all_cand.txt" | "$TR" -d ' ')
"$HEAD" -n "$MAX_ITEMS_PER_RUN" "$RUN_TMP/all_cand.txt" > "$SEL"
C_SELECTED=$("$WC" -l < "$SEL" | "$TR" -d ' ')

if [ "$C_SELECTED" -eq 0 ]; then
  SAFETY_COMPLETE=$STAGING_SAFE
  expire_quarantine
  survey_quarantine
  if [ "$PREVIEW_MODE" = "1" ]; then
    C_WOULD_DISPOSE=0
    print_preview
    exit 0
  fi
  log "run mode=$MODE trigger=$TRIGGER raw_sb=$C_RAW_SB raw_staging=$C_RAW_STAGING protected=$C_PROTECTED eligible=0 selected=0 expired_batches=$C_EXP_B"
  publish_state idle ""
  append_metrics idle
  exit 0
fi

# Byte total for the selected set: exact when cheap, estimated when the set is
# large. R2-1: this figure is display-only and never gates disposal.
measure_selected() {
  local budget=64 n=0 total=0 kb line p
  while IFS= read -r line; do
    p="${line#*$(printf '\t')}"
    n=$((n+1))
    [ "$n" -gt "$budget" ] && break
    kb=$("$DU" -sk "$p" 2>/dev/null | "$AWK" '{print $1}')
    is_uint "$kb" && total=$((total + kb*1024))
  done < "$SEL"
  if [ "$C_SELECTED" -le "$budget" ]; then
    B_SELECTED=$total; P_SELECTED=exact
  else
    B_SELECTED=$(( total / budget * C_SELECTED )); P_SELECTED=estimated
  fi
}
measure_selected

# A real capture writes continuously and holds handles. Re-sampling after a pause
# plus an lsof check removes anything still in flight.
#
# The settle window is 3s in production. The test suite shortens it because it
# constructs candidates whose mtimes it controls outright; anything other than a
# non-negative integer falls back to the production value, so a stray or hostile
# value cannot silently remove the wait.
SETTLE_SEC=3
case "${CMUX_JANITOR_TEST_SETTLE_SEC:-}" in
  ''|*[!0-9]*) ;;
  *) SETTLE_SEC="$CMUX_JANITOR_TEST_SETTLE_SEC" ;;
esac
"$SLEEP" "$SETTLE_SEC"

held() {
  local output rc
  if [ -d "$1" ]; then
    output=$("$LSOF" -nP +D "$1" 2>&1); rc=$?
  else
    output=$("$LSOF" -nP -- "$1" 2>&1); rc=$?
  fi
  # Only a successful empty query means no handle. Errors are not clearance.
  [ "$rc" -ne 1 ] || [ -n "$output" ]
}
stamp_of() { "$STAT" -f%m "$1" 2>/dev/null || echo x; }

QDEST=""
BATCH_CREATED_EPOCH=$("$DATE" +%s)
if [ "$PREVIEW_MODE" = "0" ] && [ "$USE_QUARANTINE" = "1" ] && [ "$MODE" = "apply" ]; then
  # R2-4 two-phase seal: fill `.incomplete-<run_id>` first, write controlled
  # metadata, then atomically rename to the final name. An interrupted run
  # leaves an .incomplete-* directory that the expirer and uninstall both skip.
  QSTAGE="$QUARANTINE_DIR/.incomplete-$RUN_ID"
  "$MKDIR" -p "$QSTAGE" 2>/dev/null || QSTAGE=""
  QDEST="$QSTAGE"
fi

dispose() {
  local p="$1" base
  base=$("$BASENAME" "$p")
  if [ "$PREVIEW_MODE" = "1" ] || [ "$MODE" != "apply" ]; then
    # Truthful accounting: the old dry branch returned 0 and the caller then
    # incremented the disposed counter, so logs claimed disposed=514 while
    # nothing moved.
    C_WOULD_DISPOSE=$((C_WOULD_DISPOSE+1))
    if [ -d "$p" ]; then
      C_WOULD_STAGING=$((C_WOULD_STAGING+1))
    else
      C_WOULD_SB=$((C_WOULD_SB+1))
    fi
    log "DRY would-remove $p"
    return 1
  fi
  if [ "$USE_QUARANTINE" = "1" ]; then
    if [ -z "$QDEST" ]; then
      log "WARN quarantine dir unavailable, leaving in place: $p"
      return 1
    fi
    if "$MV" -f -- "$p" "$QDEST/$base" 2>>"$LOG"; then
      return 0
    fi
    log "WARN quarantine move failed, leaving in place: $p"
    return 1
  fi
  # J2 (2026-09-01): the direct rm -rf branch is gone. validate_config already
  # refuses USE_QUARANTINE != 1, so reaching this line means the gate was
  # bypassed -- refuse again rather than delete without a rollback path.
  log "REFUSE-DELETE quarantine disabled, leaving in place: $p"
  return 1
}

# ---------- GATE 3: mtime static + no open handle + no fresh content ----------
while IFS= read -r line; do
  [ -n "$line" ] || continue
  if [ -e "$DISABLED" ] || [ -e "$JANITOR_DIR/GUARD_TRIPPED" ] || [ -e "$JANITOR_DIR/GUARD_UNAVAILABLE" ]; then
    log "STOP pause or guard trip during sweep"; break
  fi
  was="${line%%$(printf '\t')*}"
  p="${line#*$(printf '\t')}"
  if [ -f "$p" ]; then
    case "$p" in "$CM"/*) ;; *) log "SKIP-OUTSIDE $p"; C_SKIP_OUTSIDE=$((C_SKIP_OUTSIDE+1)); continue ;; esac
    case $("$BASENAME" "$p") in
      agent-turn-diff-baselines.json|agent-turn-diff-baselines.json.lock)
        log "SKIP-PROTECTED-NAME $p"; continue ;;
    esac
    if [ "$(stamp_of "$p")" != "$was" ]; then
      log "SKIP-MOVING $p"; C_SKIP_MOVING=$((C_SKIP_MOVING+1)); continue
    fi
    if held "$p"; then log "SKIP-HELD $p"; C_SKIP_HELD=$((C_SKIP_HELD+1)); continue; fi
    if [ "$(stamp_of "$p")" != "$was" ]; then C_SKIP_MOVING=$((C_SKIP_MOVING+1)); continue; fi
    dispose "$p" && C_DISPOSED=$((C_DISPOSED+1))
  elif [ -d "$p" ]; then
    case "$p" in "$STAGING"/*) ;; *) log "SKIP-OUTSIDE $p"; C_SKIP_OUTSIDE=$((C_SKIP_OUTSIDE+1)); continue ;; esac
    if [ "$(stamp_of "$p")" != "$was" ]; then
      log "SKIP-MOVING $p"; C_SKIP_MOVING=$((C_SKIP_MOVING+1)); continue
    fi
    # Any file inside modified within the age window means the capture is live.
    # -mmin is POSIX-portable; -newermt with a relative arg is not.
    if [ -n "$("$FIND" "$p" -type f -mmin "-$STAGING_MIN_AGE_MIN" -print -quit 2>/dev/null)" ]; then
      log "SKIP-FRESH-CONTENT $p"; C_SKIP_FRESH=$((C_SKIP_FRESH+1)); continue
    fi
    if held "$p"; then log "SKIP-HELD $p"; C_SKIP_HELD=$((C_SKIP_HELD+1)); continue; fi
    if [ "$(stamp_of "$p")" != "$was" ]; then C_SKIP_MOVING=$((C_SKIP_MOVING+1)); continue; fi
    dispose "$p" && C_DISPOSED=$((C_DISPOSED+1))
  fi
done < "$SEL"

# Seal the batch: metadata first, then atomic rename into the final name.
if [ -n "$QDEST" ] && [ -d "$QDEST" ]; then
  if [ "$C_DISPOSED" -gt 0 ]; then
    if write_batch_meta "$QDEST" "$C_DISPOSED"; then
      QFINAL="$QUARANTINE_DIR/$("$DATE" '+%Y%m%d-%H%M%S')-$$"
      if "$MV" -f "$QDEST" "$QFINAL" 2>/dev/null; then
        log "SEALED batch=$("$BASENAME" "$QFINAL") items=$C_DISPOSED"
      else
        log "WARN batch seal rename failed; left as $("$BASENAME" "$QDEST")"
      fi
    else
      log "WARN batch metadata write failed; left as $("$BASENAME" "$QDEST")"
    fi
  else
    "$RMDIR" "$QDEST" 2>/dev/null
  fi
fi

# safety_complete: candidate enumeration and every disposal gate ran to
# completion. Byte precision is deliberately NOT part of this.
SAFETY_COMPLETE=$STAGING_SAFE

expire_quarantine
survey_quarantine
if [ "$PREVIEW_MODE" = "1" ]; then print_preview; exit 0; fi

log "run mode=$MODE trigger=$TRIGGER raw_sb=$C_RAW_SB raw_staging=$C_RAW_STAGING protected=$C_PROTECTED eligible=$C_ELIGIBLE selected=$C_SELECTED would_dispose=$C_WOULD_DISPOSE disposed=$C_DISPOSED would_expire_batches=$C_WOULD_EXP_B would_expire_items=$C_WOULD_EXP_I expired_batches=$C_EXP_B expired_items=$C_EXP_I skip_moving=$C_SKIP_MOVING skip_fresh=$C_SKIP_FRESH skip_held=$C_SKIP_HELD skip_outside=$C_SKIP_OUTSIDE safety_complete=$SAFETY_COMPLETE"
publish_state idle ""
append_metrics idle

exit 0
