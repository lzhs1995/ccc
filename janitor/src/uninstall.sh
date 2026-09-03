#!/bin/bash
# cmux-janitor uninstaller (L3) — full removal, restores cmux to stock behaviour.
#
#   ./uninstall.sh --dry-run   show exactly what would happen, change nothing
#   ./uninstall.sh             do it
#
# Order matters: quarantined items are put BACK where they came from before the
# janitor's own files are deleted, so nothing is stranded.
set -uo pipefail

LABEL="com.example.cmux-janitor"
GUARD_LABEL="com.example.cmux-janitor-guard"
JANITOR_DIR="$HOME/.config/cmux-janitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUARD_PLIST="$HOME/Library/LaunchAgents/$GUARD_LABEL.plist"
CM="$HOME/.cmuxterm"
STAGING="$CM/agent-turn-diff-baseline-snapshots-staging"
QUARANTINE_DIR="$HOME/.cmuxterm-janitor-quarantine"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

say() { printf '%s\n' "$*"; }
act() {
  if [ "$DRY" = "1" ]; then
    say "  WOULD: $*"
  else
    say "  DO:    $*"
  fi
}

# A launchd label is machine-global. `launchctl bootout gui/<uid>/<label>` takes
# its scope from the label plus the real uid, and neither is derived from $HOME,
# so overriding HOME does NOT sandbox it. A run under a throwaway HOME (the test
# sandbox at tests/test_janitor.py, or a second install) therefore rips the
# PRODUCTION agent out of launchd while believing it only touched its own copy.
# That is not hypothetical: on 2026-08-29T06:39:50Z a sandboxed test run booted
# out both live labels and left the Mac unprotected until it was noticed.
#
# Guard: only bootout when the service launchd actually has loaded was installed
# from THIS HOME. `launchctl print` echoes the plist path and ProgramArguments,
# so a substring test against $JANITOR_DIR distinguishes "our install" from
# "somebody else's". Nothing else in this script needs the guard, because every
# other mutation goes through a $HOME-derived path.
label_is_ours() {
  local out rc
  out=$(/bin/launchctl print "gui/$(/usr/bin/id -u)/$1" 2>/dev/null)
  rc=$?
  [ "$rc" = "0" ] || return 1
  case "$out" in
    *"$JANITOR_DIR/"*) return 0 ;;
    *) return 1 ;;
  esac
}
label_is_loaded() {
  /bin/launchctl print "gui/$(/usr/bin/id -u)/$1" >/dev/null 2>&1
}

say ""
if [ "$DRY" = "1" ]; then
  say "=== cmux-janitor uninstall — DRY RUN (nothing will change) ==="
else
  say "=== cmux-janitor uninstall ==="
fi
say ""

# ---------- 1. stop the guard FIRST ----------
# The guard watches the janitor. If it kept running while the janitor's files
# are deleted, it would trip and fire a bogus alert. It goes first.
say "[1/5] guard schedule"
if label_is_ours "$GUARD_LABEL"; then
  act "launchctl bootout gui/$(/usr/bin/id -u)/$GUARD_LABEL"
  [ "$DRY" = "0" ] && /bin/launchctl bootout "gui/$(/usr/bin/id -u)/$GUARD_LABEL" 2>/dev/null
elif label_is_loaded "$GUARD_LABEL"; then
  say "  SKIP-FOREIGN-LABEL $GUARD_LABEL is loaded but was not installed from"
  say "                     $JANITOR_DIR — refusing to bootout (see header note)"
else
  say "  (guard not loaded)"
fi
if [ -f "$GUARD_PLIST" ]; then
  act "rm $GUARD_PLIST"
  [ "$DRY" = "0" ] && /bin/rm -f "$GUARD_PLIST"
else
  say "  (no guard plist)"
fi
say ""

# ---------- 2. stop the janitor schedule ----------
say "[2/5] janitor schedule"
if label_is_ours "$LABEL"; then
  act "launchctl bootout gui/$(/usr/bin/id -u)/$LABEL"
  [ "$DRY" = "0" ] && /bin/launchctl bootout "gui/$(/usr/bin/id -u)/$LABEL" 2>/dev/null
elif label_is_loaded "$LABEL"; then
  say "  SKIP-FOREIGN-LABEL $LABEL is loaded but was not installed from"
  say "                     $JANITOR_DIR — refusing to bootout (see header note)"
else
  say "  (not loaded)"
fi
if [ -f "$PLIST" ]; then
  act "rm $PLIST"
  [ "$DRY" = "0" ] && /bin/rm -f "$PLIST"
else
  say "  (no plist)"
fi
say ""

# ---------- 3. restore anything still in quarantine ----------
# The restore loop enumerates depth 2 of the quarantine tree, and find does not
# exclude dotfiles.  Two classes of entry there are the janitor's own bookkeeping
# and must never be treated as a reclaimed cmux artifact:
#
#   .janitor-batch.json   sealed-batch metadata, written by the janitor
#   .incomplete-*/        a batch that was still being filled when we stopped
#
# Without the skips below, `.janitor-batch.json` falls through to the `*)` arm
# and lands in ~/.cmuxterm as a stray file the janitor would then have to
# explain.  An .incomplete-* batch has no verifiable sealed_at, so its contents
# stay put rather than being restored from a half-written manifest.
say "[3/5] quarantine restore"
RESTORED=0
STRANDED=0
SKIPPED_META=0
SKIPPED_INCOMPLETE=0
if [ -d "$QUARANTINE_DIR" ]; then
  while IFS= read -r item; do
    [ -n "$item" ] || continue
    base=$(/usr/bin/basename "$item")
    parent=$(/usr/bin/basename "$(/usr/bin/dirname "$item")")

    # Janitor bookkeeping, not a cmux artifact.
    if [ "$base" = ".janitor-batch.json" ]; then
      say "  SKIP (batch metadata): $parent/$base"
      SKIPPED_META=$((SKIPPED_META+1))
      continue
    fi
    # An unsealed batch: leave the whole thing alone.
    case "$parent" in
      .incomplete-*)
        say "  SKIP (incomplete batch): $parent/$base"
        SKIPPED_INCOMPLETE=$((SKIPPED_INCOMPLETE+1))
        continue ;;
    esac

    case "$base" in
      # UUID-shaped -> came from the staging directory
      [0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f]-*)
        dest="$STAGING/$base" ;;
      # everything else was a top-level ~/.cmuxterm file
      *)
        dest="$CM/$base" ;;
    esac
    if [ -e "$dest" ]; then
      say "  SKIP (target exists): $base"
      STRANDED=$((STRANDED+1))
      continue
    fi
    act "mv $item -> $dest"
    if [ "$DRY" = "0" ]; then
      /bin/mkdir -p "$(/usr/bin/dirname "$dest")" 2>/dev/null
      if /bin/mv -f -- "$item" "$dest" 2>/dev/null; then
        RESTORED=$((RESTORED+1))
      else
        say "  WARN restore failed: $base"
        STRANDED=$((STRANDED+1))
      fi
    else
      RESTORED=$((RESTORED+1))
    fi
  done < <(/usr/bin/find "$QUARANTINE_DIR" -mindepth 2 -maxdepth 2 2>/dev/null)
  say "  restored=$RESTORED stranded=$STRANDED skipped_meta=$SKIPPED_META skipped_incomplete=$SKIPPED_INCOMPLETE"
  # Metadata alone is ours to drop, but an .incomplete-* batch still holds real
  # cmux artifacts that were deliberately not restored.  Deleting the tree then
  # would destroy exactly what the skip was protecting, so both counters gate it.
  if [ "$STRANDED" -eq 0 ] && [ "$SKIPPED_INCOMPLETE" -eq 0 ]; then
    act "rm -rf $QUARANTINE_DIR"
    [ "$DRY" = "0" ] && /bin/rm -rf "$QUARANTINE_DIR"
  elif [ "$SKIPPED_INCOMPLETE" -gt 0 ]; then
    say "  keeping $QUARANTINE_DIR ($SKIPPED_INCOMPLETE unsealed item(s) left in place)"
  else
    say "  keeping $QUARANTINE_DIR (had $STRANDED item(s) that could not be restored)"
  fi
else
  say "  (no quarantine directory)"
fi
say ""

# ---------- 3. keep a copy of the log, then remove the janitor ----------
say "[4/5] janitor files"
if [ -f "$JANITOR_DIR/janitor.log" ]; then
  KEEP="$HOME/cmux-janitor-final-log-$(/bin/date '+%Y%m%d-%H%M%S').txt"
  act "cp janitor.log -> $KEEP"
  [ "$DRY" = "0" ] && /bin/cp "$JANITOR_DIR/janitor.log" "$KEEP" 2>/dev/null
fi
if [ -d "$JANITOR_DIR" ]; then
  act "rm -rf $JANITOR_DIR"
  [ "$DRY" = "0" ] && /bin/rm -rf "$JANITOR_DIR"
fi
say ""

# ---------- 4. confirm cmux is untouched ----------
say "[5/5] cmux integrity (janitor never modified these)"
for p in "$CM/agent-turn-diff-baselines.json" \
         "$CM/agent-turn-diff-baselines.json.lock" \
         "$CM/agent-turn-diff-baseline-snapshots" \
         "$HOME/.config/cmux"; do
  if [ -e "$p" ]; then
    say "  present: $p"
  else
    say "  absent:  $p"
  fi
done
say ""
if [ "$DRY" = "1" ]; then
  say "DRY RUN complete — nothing changed."
else
  say "Uninstalled. cmux is back to stock behaviour (it was never patched)."
  say "The only lasting effect: orphans are no longer swept."
fi
say ""
