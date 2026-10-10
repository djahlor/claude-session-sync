#!/bin/zsh
# Test one sync with Claude closed, and keep the log.
#
# Start it by double-clicking closed-sync-test.command. It quits Claude, which
# ends every open chat, including chats that are still working. Pick a moment
# when no chat is working.
#
# Steps: quit Claude, wait until `pgrep -a -x Claude` finds nothing, run
# `sync`, read `status`, open Claude again, read `status` once more. The log
# is saved in logs/ in this repository. Once Claude has quit, it is opened
# again whatever happens next.

set -u

script_dir="$(cd "$(dirname "$0")" && pwd)"
tool="${CLOSED_SYNC_TOOL:-$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync}"
log_dir="${CLOSED_SYNC_LOG_DIR:-$script_dir/../logs}"
# Seconds between checks, and how many checks before giving up.
pause="${CLOSED_SYNC_PAUSE:-2}"
quit_checks="${CLOSED_SYNC_QUIT_CHECKS:-60}"
sync_tries="${CLOSED_SYNC_TRIES:-30}"

mkdir -p "$log_dir" || exit 1
log="$(cd "$log_dir" && pwd)/closed-sync-$(date +%Y%m%d-%H%M%S).log"

say() {
  print -r -- "$*" 2>/dev/null
  print -r -- "$*" >> "$log"
}

# -x matches the exact name, so the watcher and Claude's helpers do not count.
# -a counts pgrep's own parent processes, which it leaves out by default.
claude_is_open() {
  pgrep -a -x Claude >/dev/null 2>&1
}

# A script started from a Claude chat stops when Claude quits, before the sync.
started_inside_claude() {
  local pid=$$ command
  while [[ -n "$pid" && "$pid" -gt 1 ]]; do
    command="$(ps -o comm= -p "$pid" 2>/dev/null)"
    [[ "$command" == */Claude.app/Contents/* ]] && return 0
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
  done
  return 1
}

reopen_claude() {
  [[ "$claude_must_reopen" == 1 ]] || return 0
  claude_must_reopen=0
  say "Opening Claude..."
  open -a Claude >> "$log" 2>&1 || say "Claude did not open. Open it yourself."
}

say "Closed-Claude sync test, $(date '+%d-%m-%Y %H:%M:%S %Z')"

if [[ ! -x "$tool" ]]; then
  say "Claude Session Sync is not installed at $tool. Nothing was changed."
  exit 1
fi
if started_inside_claude; then
  say "Start this by double-click in Finder, not from a Claude chat. Nothing was changed."
  exit 1
fi

say "Claude Session Sync $("$tool" --version 2>&1)"
say "Claude $(defaults read /Applications/Claude.app/Contents/Info.plist CFBundleShortVersionString 2>/dev/null || echo "version unknown")"

print "This quits Claude. Every open chat ends, including chats that are still working."
print -n "Type yes and press Return to go on: "
read -r answer || answer=""
if [[ "$answer" != "yes" ]]; then
  say "Stopped before quitting Claude. Nothing was changed."
  exit 1
fi

claude_must_reopen=0
trap reopen_claude EXIT
trap 'exit 130' INT TERM
# Closing the Terminal window must not leave Claude closed.
trap '' HUP

if claude_is_open; then
  say "Quitting Claude..."
  # From here a stop of this script may find Claude already quitting.
  claude_must_reopen=1
  if ! osascript -e 'tell application "Claude" to quit' >> "$log" 2>&1; then
    say "macOS did not pass the quit on. Quit Claude yourself with Cmd+Q and this goes on."
  fi
  checks=0
  while claude_is_open; do
    if (( checks >= quit_checks )); then
      say "Claude is still open, so nothing was synced. Answer any question Claude is showing, then start this again."
      claude_must_reopen=0
      exit 1
    fi
    (( checks += 1 ))
    sleep "$pause"
  done
  say "Claude is closed."
else
  say "Claude was already closed."
fi
claude_must_reopen=1

# The watcher starts its own sync when Claude quits, and Claude's helpers can
# take a moment to stop. Both make this sync wait, and neither is a failure.
try=1
while true; do
  say "Running sync, try $try..."
  sync_output="$("$tool" sync --json 2>&1)"
  sync_exit=$?
  say "$sync_output"
  say "sync exit code: $sync_exit"
  if (( sync_exit == 0 || try >= sync_tries )); then
    break
  fi
  if ! print -r -- "$sync_output" | grep -Eq '"reason": *"(busy|claude-open)"'; then
    break
  fi
  (( try += 1 ))
  sleep "$pause"
done
if print -r -- "$sync_output" | grep -q "references an unknown group"; then
  say "The installed build is older than 0.5.2, which fixes this. Run the installer again, then repeat this test."
fi

# Status can say finished only while Claude is closed. With Claude open it
# always says waiting-for-Claude. So the proof is read before Claude opens.
say "Running status with Claude closed..."
check=1
while true; do
  closed_status="$("$tool" status --json 2>&1)"
  if (( check >= sync_tries )); then
    break
  fi
  if ! print -r -- "$closed_status" | grep -Eq '"progress": *"syncing"'; then
    break
  fi
  (( check += 1 ))
  sleep "$pause"
done
say "$closed_status"

reopen_claude

say "Running status with Claude open..."
say "$("$tool" status 2>&1)"

if (( sync_exit == 0 )) \
  && print -r -- "$closed_status" | grep -Eq '"progress": *"finished"' \
  && print -r -- "$closed_status" | grep -Eq '"layout_failures": *0[,}]'; then
  say "RESULT: passed. The sync finished with Claude closed, and pins and groups report no failure."
  say "Now look at the sidebar in Claude. If a group looks empty, check the sidebar filter first."
  result=0
else
  say "RESULT: needs attention. Read the sync and status lines above."
  result=1
fi
say "Log: $log"
exit $result
