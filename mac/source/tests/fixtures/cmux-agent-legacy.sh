#!/usr/bin/env bash
set -euo pipefail

CMUX_BIN="${CMUX_BIN:-}"
if [[ -z "$CMUX_BIN" ]]; then
  if command -v cmux >/dev/null 2>&1; then
    CMUX_BIN="$(command -v cmux)"
  else
    CMUX_BIN="/Applications/cmux.app/Contents/Resources/bin/cmux"
  fi
fi

if [[ ! -x "$CMUX_BIN" ]]; then
  echo "cmux-agent: cmux binary not found. Set CMUX_BIN=/path/to/cmux." >&2
  exit 127
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "cmux-agent: jq is required." >&2
  exit 127
fi

usage() {
  cat <<'USAGE'
cmux-agent - lightweight coordination helper for agents running inside cmux

Usage:
  cmux-agent self
  cmux-agent list
  cmux-agent reconcile <surface-ref>  (read-only pending delivery check)
  cmux-agent read <surface-ref> [lines]
  cmux-agent ask <surface-ref> <message...>    (tracked submission, not consumption)
  cmux-agent send <surface-ref> <message...>   (raw submit, no marker confirmation)
  cmux-agent broadcast <message...>
  cmux-agent status <key> <value> [icon] [color]
  cmux-agent clear-status <key>
  cmux-agent log <message...>
  cmux-agent notify <title> <body...>
  cmux-agent feed
  cmux-agent events [event-name]
  cmux-agent start-codex <workspace-name> [cwd]
  cmux-agent protocol

Message protocol:
  Ask workers to reply with exactly one leading marker:
  STATUS: short current state
  DONE: concise result and changed files
  BLOCKED: blocker and required decision/input

Examples:
  cmux-agent list
  cmux-agent ask surface:7 "STATUS: report your current task and blocker, if any."
  cmux-agent read surface:7 80
  cmux-agent broadcast "STATUS: sync point. Reply with current state."
USAGE
}

cmux() {
  case "${1:-}" in
    identify|tree|read-screen|send|send-key)
      /opt/homebrew/bin/python3 -c '
import os,subprocess,sys
seconds=max(0.1,min(30,float(os.environ.get("CMUX_AGENT_CALL_TIMEOUT", "10"))))
try:
    r=subprocess.run(sys.argv[1:],timeout=seconds)
    raise SystemExit(r.returncode)
except subprocess.TimeoutExpired:
    print("cmux-agent: CMUX_CALL_TIMEOUT (remote delivery may be uncertain; do not resend)",file=sys.stderr)
    raise SystemExit(75)
' "$CMUX_BIN" "$@" ;;
    *) "$CMUX_BIN" "$@" ;;
  esac
}

canonical_surface() {
  local ref
  # Target lookup may need a workspace different from the sender's workspace.
  # Never change the caller environment or current_surface identity.
  if ! ref="$(cmux identify --surface "$1" --json | jq -er '.caller.surface_ref')"; then
    local workspace
    workspace="$(cmux tree --all --json | jq -er --arg target "$1" '
      [.windows[].workspaces[] as $ws | $ws.panes[].surfaces[]
       | select(.ref == $target) | $ws.ref]
      | if length == 1 then .[0] else error("target workspace is not unique") end
    ')" || return 75
    ref="$(cmux identify --workspace "$workspace" --surface "$1" --json |
      jq -er --arg target "$1" '.caller.surface_ref | select(. == $target)')" || return 75
  fi
  [[ "$ref" == surface:* ]] || return 75
  printf '%s\n' "$ref"
}

require_args() {
  local need="$1"
  local got="$2"
  local usage_line="$3"
  if (( got < need )); then
    echo "cmux-agent: missing arguments" >&2
    echo "usage: $usage_line" >&2
    exit 2
  fi
}

current_surface() {
  cmux identify --json | jq -r '.caller.surface_ref // .focused.surface_ref // empty'
}

list_surfaces_json() {
  cmux tree --all --json
}

list_surfaces() {
  list_surfaces_json | jq -r '
    .windows[] as $w
    | $w.workspaces[] as $ws
    | $ws.panes[] as $pane
    | $pane.surfaces[]
    | [
        .ref,
        $ws.ref,
        $pane.ref,
        .type,
        (.title // ""),
        (if .here then "here" elif .focused then "focused" elif .active then "active" else "" end)
      ]
    | @tsv
  ' | awk -F '\t' 'BEGIN {
      printf "%-12s %-12s %-10s %-14s %-9s %s\n", "SURFACE", "WORKSPACE", "PANE", "TYPE", "STATE", "TITLE"
    }
    {
      printf "%-12s %-12s %-10s %-14s %-9s %s\n", $1, $2, $3, $4, $6, $5
    }'
}

terminal_surfaces_except_self() {
  local self
  self="$(current_surface || true)"
  list_surfaces_json | jq -r --arg self "$self" '
    .windows[].workspaces[].panes[].surfaces[]
    | select(.type == "terminal" or .type == "agent-session")
    | select(.ref != $self)
    | .ref
  '
}

read_surface() {
  local surface="$1"
  local lines="${2:-80}"
  cmux read-screen --surface "$surface" --lines "$lines"
}

new_delivery_id() {
  printf 'cmux-%s-%s-%s' "$(date -u '+%Y%m%dT%H%M%SZ')" "$$" "$RANDOM"
}

# Inspect the final input block, never unrelated historical activity.
# A cleared later prompt proves submission, not consumption by the model.
delivery_state() {
  local screen="$1" marker="$2" line block="" seen=0 final_seen=0 queued=0 queue_marker=0 in_user=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == *"Queued follow-up inputs"* ]] && queued=1
    if (( queued )) && [[ "$line" == *"$marker"* ]]; then queue_marker=1; fi
    if [[ "$line" =~ ^[[:space:]]*(❯|›)([[:space:]]|$) ]]; then
      queued=0
      block="$line"
      final_seen=$seen
      in_user=1
    else
      block+=$'\n'"$line"
    fi
    if [[ "$line" =~ ^[[:space:]]*(⏺|•|✻|✢|✳|✶|✽)[[:space:]] ]]; then in_user=0; fi
    if (( in_user )) && [[ "$line" == *"$marker"* ]]; then seen=1; fi
  done <<< "$screen"
  if [[ "$block" == *"$marker"* ]]; then
    if printf '%s\n' "$block" | grep -Eq '^[[:space:]]*(⏺|•|✻|✢|✳|✶|✽|◐|◑|◒|◓)[[:space:]]'; then
      echo UNCONFIRMED
      return
    fi
    # Do not mistake queued UI for an editable compose buffer.
    if [[ "$block" == *"Press up to edit queued messages"* ]]; then
      echo QUEUED
    else
      echo COMPOSE_PENDING
    fi
  elif (( queue_marker )); then
    echo QUEUED
  elif (( seen && final_seen )); then
    echo SUBMITTED
  else
    echo UNCONFIRMED
  fi
}

normalize_claude_frame() {
  /opt/homebrew/bin/python3 -c '
import re,sys
s=sys.argv[1]; lines=s.splitlines(); starts=[i for i,l in enumerate(lines) if re.match(r"^\s*❯(?:\s|$)",l)]
if starts:
 i=starts[-1]
 rules=[j for j in range(i+1,len(lines)) if re.fullmatch(r"\s*─{8,}\s*",lines[j])]
 if i>0 and re.fullmatch(r"\s*─{8,}\s*",lines[i-1]) and rules:
  j=rules[0]; footer=[l.strip() for l in lines[j+1:] if l.strip()]
  patterns=[r"\[(?:Opus|Sonnet|Haiku) [^\]]+\] │ .+", r"⏱️?  .+", r"上下文 [░▒▓█ ]+\d+%", r"\d+ CLAUDE\.md \| \d+ MCPs \| \d+ 钩子", r"⏵⏵ (?:bypass permissions on|accept edits on) \(shift\+tab to cycle\)(?: · ← for agents)?"]
  if len(footer)==5 and all(re.fullmatch(p,l) for p,l in zip(patterns,footer)):
   # Only the final proven frame is normalized; history and draft bytes survive.
   lines=lines[:j]
   lines[i]=re.sub(r"^(\s*❯)\s",r"\1 ",lines[i],count=1)
   s="\n".join(lines)
print(s)
' "$1"
}

compose_text() {
  local normalized
  normalized="$(normalize_claude_frame "$1")"
  printf '%s\n' "$normalized" | awk '
    /^[[:space:]]*(❯|›)([[:space:]]|$)/ {b=$0; sub(/^[[:space:]]*(❯|›)[[:space:]]*/, "", b); found=1; footer=0; next}
    found && /^[[:space:]]*GPT-[^ ]+ .*·/ {footer=1; next}
    found && footer && /^[[:space:]]*← for agents[[:space:]]+·[[:space:]]+(\? for shortcuts|tab to queue message)([[:space:]]+⚠[[:space:]]+[0-9]+[[:space:]]+warnings?[[:space:]]+·[[:space:]]+f2 to view)?[[:space:]]*$/ {next}
    found && footer && /^[[:space:]]*⚠[[:space:]]+[0-9]+[[:space:]]+warnings?[[:space:]]+·[[:space:]]+f2 to view[[:space:]]*$/ {next}
    found && /^[[:space:]]*(GPT-[^ ]+ .*·|\? for shortcuts|tab to queue message|[0-9]+% context left)/ {next}
    found {b=b " " $0}
    END {if (!found) {print "__NO_COMPOSER__"; exit}; gsub(/[[:space:]]+/, " ", b); sub(/^ /,"",b); sub(/ $/,"",b); print b}
  '
}

# Provider evidence must belong to the current input block, not scrollback.
composer_provider() {
  printf '%s\n' "$1" | awk '
    /^[[:space:]]*(❯|›)([[:space:]]|$)/ {b=""; next}
    {b=b "\n" $0}
    END {if (b ~ /GPT-[^ ]+ .*·/) print "codex"; else if (b ~ /Claude|bypass permissions|accept edits/) print "claude"; else print "unknown"}
  '
}
compose_key() {
  printf '%s\n' "$1" | awk '
    /^[[:space:]]*(❯|›)([[:space:]]|$)/ {b=""; next}
    {b=b "\n" $0}
    END {if (b ~ /tab to queue message/) print "tab"; else print "enter"}
  '
}

# Compare wrapped visual lines against the original, permitting only line-boundary
# whitespace introduced by the terminal. Interior characters/spaces must match.
compose_match_state() {
  local normalized
  normalized="$(normalize_claude_frame "$1")"
  /opt/homebrew/bin/python3 -c '
import re,sys
def mismatch(): print("MISMATCH"); sys.exit(0)
screen,expected=sys.argv[1:]; lines=screen.splitlines(); start=None
for i,line in enumerate(lines):
 if re.match(r"^\s*[❯›](?:\s|$)",line): start=i
if start is None: mismatch()
expected=re.sub(r"\s+"," ",expected).strip(); matched=False; footer=False
for i,line in enumerate(lines[start:]):
 if i==0: line=re.sub(r"^\s*[❯›]\s*","",line)
 elif re.match(r"^\s*GPT-[^ ]+ .*·",line): footer=True; continue
 elif footer and re.fullmatch(r"\s*← for agents\s+·\s+(?:\? for shortcuts|tab to queue message)(?:\s+⚠\s+\d+\s+warnings?\s+·\s+f2 to view)?\s*",line): continue
 elif footer and re.fullmatch(r"\s*⚠\s+\d+\s+warnings?\s+·\s+f2 to view\s*",line): continue
 elif re.match(r"^\s*(GPT-[^ ]+ .*·|\? for shortcuts|tab to queue message|[0-9]+% context left)",line): continue
 chunk=re.sub(r"\s+"," ",line).strip()
 if not chunk: continue
 if expected.startswith(" "): expected=expected[1:]
 if not expected.startswith(chunk): mismatch()
 matched=True
 expected=expected[len(chunk):]
print("FULL" if not expected.strip() else "PREFIX" if matched else "MISMATCH")
' "$normalized" "$2"
}

compose_matches() {
  [[ "$(compose_match_state "$1" "$2")" == FULL ]]
}

atomic_paste() {
  /opt/homebrew/bin/python3 - "$CMUX_BIN" "$1" "$2" "$3" <<'PYATOMIC'
import json,os,subprocess,sys,pathlib,time
binary,surface,text,folder=sys.argv[1:]
folder=pathlib.Path(folder)
def save(name,data):
    (folder/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
def call(*args):
    result=subprocess.run([binary,*args],capture_output=True,text=True,timeout=max(.1,min(30,float(os.environ.get("CMUX_AGENT_CALL_TIMEOUT","10")))))
    if result.returncode: raise RuntimeError("CMUX_RETURN_"+str(result.returncode)+": "+result.stderr[:300])
    return json.loads(result.stdout)
try:
    if any(ord(ch)<32 and ch not in "\n\r\t" for ch in text): raise ValueError("UNSUPPORTED_CONTROL_BYTE")
    caps=call("capabilities")
    if "terminal.paste" not in caps.get("methods",[]): raise ValueError("ATOMIC_PASTE_UNSUPPORTED_NO_FALLBACK")
    target=call("--id-format","both","identify","--surface",surface,"--json")["caller"]
    if target is None:
        tree=call("tree","--all","--json")
        matches=[ws["ref"] for win in tree.get("windows",[]) for ws in win.get("workspaces",[]) for pane in ws.get("panes",[]) for item in pane.get("surfaces",[]) if item.get("ref")==surface]
        if len(matches)!=1: raise ValueError("ATOMIC_WORKSPACE_NOT_UNIQUE")
        target=call("--id-format","both","identify","--workspace",matches[0],"--surface",surface,"--json")["caller"]
    if not target or target.get("surface_type")!="terminal" or target.get("surface_ref")!=surface: raise ValueError("ATOMIC_TARGET_MISMATCH")
    params={"surface_id":target["surface_id"],"workspace_id":target["workspace_id"],"text":text,"submit_key":"none"}
    save("atomic-paste-intent.json",{"at":time.time(),"params":params})
    result=call("rpc","terminal.paste",json.dumps(params,ensure_ascii=False))
    save("atomic-paste-return.json",{"at":time.time(),"result":result})
except Exception as exc:
    save("atomic-paste-error.json",{"at":time.time(),"error":str(exc),"no_fallback":True})
    print("DISPATCH_UNCONFIRMED atomic paste: "+str(exc),file=sys.stderr)
    sys.exit(75)
PYATOMIC
}

submit_text() (
  local surface text="$2" marker="${3:-}" screen state attempt
  surface="$(canonical_surface "$1")" || return 75
  local root="${CMUX_AGENT_STATE_DIR:-$HOME/.local/state/cmux-agent}"
  local key="${surface//[^a-zA-Z0-9_-]/_}" lock record
  umask 077
  mkdir -p "$root"
  lock="$root/$key.lock"
  # Persistent inode OS lock; kernel releases it after process death.
  # Preserve legacy directory locks: their ownership is unknown.
  [[ ! -d "$lock" ]] || { echo "DISPATCH_BUSY legacy_lock=$lock" >&2; return 75; }
  exec 9>>"$lock.flock"
  /opt/homebrew/bin/python3 -c 'import fcntl,sys
try: fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError: sys.exit(75)' || return 75
  record="$root/${marker:-raw-$$-$RANDOM}"
  mkdir "$record"
  printf '%s\n' "$surface" > "$record/target"
  printf '%s\n' "$text" > "$record/message"
  screen="$(cmux read-screen --surface "$surface" --lines "${CMUX_AGENT_CONFIRM_LINES:-200}")" || return 75
  printf '%s\n' "$screen" > "$record/before.txt"
  local prompt
  prompt="$(printf '%s\n' "$screen" | grep -E '^[[:space:]]*(❯|›)([[:space:]]|$)' | tail -1 || true)"
  # Accept an empty prompt or the CLI's known placeholder, not someone else's draft.
  local compose
  [[ "$(composer_provider "$screen")" != unknown ]] || { echo "DISPATCH_UNCONFIRMED (no current provider evidence; no input sent)" >&2; return 75; }
  compose="$(compose_text "$screen")"
  if [[ -n "$compose" && "$compose" != "Ask Codex to do anything" ]]; then
    echo PREFLIGHT_UNCERTAIN > "$record/state"
    echo "DISPATCH_UNCONFIRMED surface=$surface (input not proven empty; no input sent) ledger=$record" >&2
    return 75
  fi

  # A prior unconfirmed transaction must be reconciled before new input.
  if [[ -e "$root/$key.pending" ]]; then
    echo "DISPATCH_PENDING surface=$surface ledger=$root/$key.pending (no input sent)" >&2
    return 75
  fi
  printf '%s\n' "$record" > "$root/$key.pending"
  printf '%s\n' SENDING > "$record/state"
  atomic_paste "$surface" "$text" "$record" || return 75
  sleep "${CMUX_AGENT_SUBMIT_DELAY:-0.25}"
  # Revalidate after paste: the application or a human draft may have changed.
  screen="$(cmux read-screen --surface "$surface" --lines "${CMUX_AGENT_CONFIRM_LINES:-200}")" || return 75
  printf '%s\n' "$screen" > "$record/pasted.txt"
  local expected
  expected="$(printf '%s\n' "$text" | awk '{printf "%s ",$0}' | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//')"
  # Wait only for an exact prefix of our paste; never re-paste or key altered input.
  local observed render_try
  for render_try in 1 2 3 4 5; do
    observed="$(compose_match_state "$screen" "$expected")"
    [[ "$(composer_provider "$screen")" != unknown && "$observed" == PREFIX ]] || break
    sleep 0.2
    screen="$(cmux read-screen --surface "$surface" --lines "${CMUX_AGENT_CONFIRM_LINES:-200}")" || return 75
    printf '%s\n' "$screen" > "$record/render-$render_try.txt"
  done
  if [[ "$(composer_provider "$screen")" == unknown ]] || ! compose_matches "$screen" "$expected"; then
    echo PASTE_UNCONFIRMED > "$record/state"
    echo "DISPATCH_UNCONFIRMED surface=$surface ledger=$record (paste differs or application changed; no key sent)" >&2
    return 75
  fi
  local submit_key
  submit_key="$(compose_key "$screen")"
  cmux send-key --surface "$surface" "$submit_key" || return 75
  sleep "${CMUX_AGENT_POST_SUBMIT_DELAY:-0.75}"
  if [[ -z "$marker" ]]; then
    echo RAW_UNVERIFIED > "$record/state"
    rm "$root/$key.pending"
    return 0
  fi
  for attempt in 0 1; do
    screen="$(cmux read-screen --surface "$surface" --lines "${CMUX_AGENT_CONFIRM_LINES:-200}")" || return 75
    printf '%s\n' "$screen" > "$record/after-$attempt.txt"
    state="$(delivery_state "$screen" "$marker")"
    printf '%s\n' "$state" > "$record/state"
    case "$state" in
      SUBMITTED|QUEUED)
        rm "$root/$key.pending"
        echo "DISPATCH_$state marker=$marker surface=$surface ledger=$record (consumption not confirmed)" >&2
        return 0 ;;
      COMPOSE_PENDING)
        if (( attempt == 0 )) && [[ "${CMUX_AGENT_NO_FORCE_COMPOSE:-0}" != 1 ]]; then
          # Re-read before recovery; only submit the unchanged marker-bearing buffer.
          local fresh
          fresh="$(cmux read-screen --surface "$surface" --lines "${CMUX_AGENT_CONFIRM_LINES:-200}")" || return 75
          if [[ "$fresh" == "$screen" ]]; then
            cmux send-key --surface "$surface" "$submit_key" || return 75
            sleep "${CMUX_AGENT_POST_SUBMIT_DELAY:-0.75}"
            continue
          fi
        fi ;;
    esac
    break
  done
  echo "DISPATCH_UNCONFIRMED state=$state marker=$marker surface=$surface ledger=$record (do not resend; inspect original delivery)" >&2
  return 75
)

reconcile_surface() (
  local surface root="${CMUX_AGENT_STATE_DIR:-$HOME/.local/state/cmux-agent}"
  surface="$(canonical_surface "$1")" || return 75
  local key="${surface//[^a-zA-Z0-9_-]/_}" record marker screen state
  [[ -f "$root/$key.pending" ]] || { echo "NO_PENDING surface=$surface"; return; }
  [[ ! -d "$root/$key.lock" ]] || return 75
  exec 9>>"$root/$key.lock.flock"
  /opt/homebrew/bin/python3 -c 'import fcntl,sys
try: fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError: sys.exit(75)' || return 75
  record="$(cat "$root/$key.pending")"
  marker="${record##*/}"
  screen="$(cmux read-screen --surface "$surface" --lines "${CMUX_AGENT_CONFIRM_LINES:-200}")" || return 75
  printf '%s\n' "$screen" > "$record/reconcile.txt"
  state="$(delivery_state "$screen" "$marker")"
  printf '%s\n' "$state" > "$record/state"
  case "$state" in
    SUBMITTED|QUEUED) rm "$root/$key.pending" ;;
    *) echo "DISPATCH_UNCONFIRMED state=$state ledger=$record (no keys sent)"; return 75 ;;
  esac
  echo "DISPATCH_$state ledger=$record (consumption not confirmed; no keys sent)"
)

send_text() {
  local surface="$1"
  local text="$2"
  cmux send --surface "$surface" "$text"
}

ask_surface() {
  local surface="$1"
  local text="$2"
  local from
  from="$(current_surface || true)"
  local stamp
  stamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  local delivery_id
  delivery_id="$(new_delivery_id)"
  submit_text "$surface" "[CMUX-AGENT][delivery:${delivery_id}][$stamp][from:${from:-unknown}]
TASK:
$text

Reply with one leading marker: STATUS:, DONE:, or BLOCKED:." "$delivery_id"
}

broadcast() {
  local text="$1"
  local target
  local count=0
  while IFS= read -r target; do
    [[ -z "$target" ]] && continue
    ask_surface "$target" "$text"
    count=$((count + 1))
  done < <(terminal_surfaces_except_self)
  echo "cmux-agent: broadcast sent to $count surface(s)"
}

protocol() {
  cat <<'PROTOCOL'
cmux agent collaboration protocol

Discovery:
  cmux-agent self
  cmux-agent list

Task messages:
  cmux-agent ask <surface-ref> "<task>"
  cmux-agent broadcast "<sync request>"

Delivery:
  `ask` adds a unique delivery id, validates the composer and exact paste,
  and uses Tab for a running Codex or Enter for idle input. It records
  DISPATCH_QUEUED or DISPATCH_SUBMITTED; neither proves consumption.
  `reconcile` checks the original pending delivery without sending keys.
  `DISPATCH_UNCONFIRMED` is a hard stop, not permission to send a
  duplicate. `send` is reserved for raw terminal controls.

Responses:
  STATUS: short current state
  DONE: concise result and changed files
  BLOCKED: blocker and required decision/input

Review:
  cmux-agent read <surface-ref> 120

Rules:
  Do not modify another agent's terminal unless the user or coordinator asked you to.
  Prefer ask/read/status/log over ad hoc terminal control. `ask` submits with a
  unique delivery id, state-appropriate key, a render delay, and post-submit screen
  confirmation; `DISPATCH_UNCONFIRMED` is not delivery and must not be retried
  blindly.
  Keep cross-agent messages short and actionable.
PROTOCOL
}

cmd="${1:-help}"
shift || true

case "$cmd" in
  help|-h|--help)
    usage
    ;;
  self)
    cmux identify --json
    ;;
  list|ls)
    list_surfaces
    ;;
  reconcile)
    require_args 1 "$#" "cmux-agent reconcile <surface-ref>"
    reconcile_surface "$1"
    ;;
  read)
    require_args 1 "$#" "cmux-agent read <surface-ref> [lines]"
    read_surface "$1" "${2:-80}"
    ;;
  send)
    require_args 2 "$#" "cmux-agent send <surface-ref> <message...>"
    surface="$1"
    shift
    submit_text "$surface" "$*"
    ;;
  ask)
    require_args 2 "$#" "cmux-agent ask <surface-ref> <message...>"
    surface="$1"
    shift
    if [[ "${1:-}" == "--no-force-compose" ]]; then
      export CMUX_AGENT_NO_FORCE_COMPOSE=1
      shift
    fi
    ask_surface "$surface" "$*"
    ;;
  broadcast)
    require_args 1 "$#" "cmux-agent broadcast <message...>"
    broadcast "$*"
    ;;
  status)
    require_args 2 "$#" "cmux-agent status <key> <value> [icon] [color]"
    key="$1"
    value="$2"
    icon="${3:-message.circle.fill}"
    color="${4:-#0A84FF}"
    cmux set-status "$key" "$value" --icon "$icon" --color "$color"
    ;;
  clear-status)
    require_args 1 "$#" "cmux-agent clear-status <key>"
    cmux clear-status "$1"
    ;;
  log)
    require_args 1 "$#" "cmux-agent log <message...>"
    cmux log --source cmux-agent "$*"
    ;;
  notify)
    require_args 2 "$#" "cmux-agent notify <title> <body...>"
    title="$1"
    shift
    cmux notify --title "$title" --body "$*"
    ;;
  feed)
    cmux feed tui
    ;;
  events)
    if (($# > 0)); then
      cmux events --name "$1" --reconnect
    else
      cmux events --reconnect
    fi
    ;;
  start-codex)
    require_args 1 "$#" "cmux-agent start-codex <workspace-name> [cwd]"
    name="$1"
    cwd="${2:-$PWD}"
    cmux new-workspace --name "$name" --cwd "$cwd" --command "codex"
    ;;
  protocol)
    protocol
    ;;
  *)
    echo "cmux-agent: unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
