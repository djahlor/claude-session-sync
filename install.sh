#!/bin/zsh

set -euo pipefail

automatic_targets=false
enable_personal=false
disable_personal=false
sync_layout=false
sync_routines=false

for argument in "$@"; do
  case "$argument" in
    --automatic-targets)
      automatic_targets=true
      ;;
    --enable-personal)
      enable_personal=true
      ;;
    --disable-personal)
      disable_personal=true
      ;;
    --sync-layout)
      sync_layout=true
      ;;
    --sync-routines)
      sync_routines=true
      ;;
    *)
      echo "Unknown option: $argument" >&2
      echo "Use: ./install.sh [--automatic-targets] [--sync-layout] [--sync-routines] [--enable-personal|--disable-personal]" >&2
      exit 2
      ;;
  esac
done

if [[ "$enable_personal" == true && "$disable_personal" == true ]]; then
  echo "Choose either --enable-personal or --disable-personal, not both." >&2
  exit 2
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Claude Session Sync currently supports macOS only." >&2
  exit 1
fi

if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x /usr/bin/python3 ]]; then
  python_bin=/usr/bin/python3
else
  python_bin=python3
fi
if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "Python 3.9 or newer is required." >&2
  exit 1
fi
if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))'; then
  echo "Python 3.9 or newer is required." >&2
  exit 1
fi

script_dir="$(cd "$(dirname "$0")" && pwd)"
support_dir="$HOME/Library/Application Support/ClaudeSessionSync"
source_root="$script_dir/src"

trap 'echo "Installation stopped at line $LINENO. Nothing in /Applications/Claude.app was changed." >&2' ERR

mkdir -p "$support_dir"
chmod 700 "$support_dir"
run_source_cli() {
  PYTHONPATH="$source_root" "$python_bin" -c \
    'from claude_session_sync.cli import main; raise SystemExit(main())' "$@"
}

setup_args=()
if [[ "$automatic_targets" == true ]]; then
  setup_args+=(--automatic-targets)
fi
if [[ "$enable_personal" == true ]]; then
  setup_args+=(--enable-personal)
fi
if [[ "$disable_personal" == true ]]; then
  setup_args+=(--disable-personal)
fi
if [[ "$sync_layout" == true ]]; then
  # On a terminal, setup asks which account's pins and groups the others copy.
  setup_args+=(--sync-layout --ask-main-account)
fi
if [[ "$sync_routines" == true ]]; then
  setup_args+=(--sync-routines)
fi
run_source_cli setup "${setup_args[@]}" --apply

echo
echo "Claude Session Sync is installed."
if [[ "$enable_personal" == true ]]; then
  echo "Open Claude Work or Claude Personal Synced from ~/Applications."
  echo "Open Claude Personal Synced once and sign in to the second account."
else
  echo "Open Claude normally. No separate profile app is needed."
fi
if [[ "$automatic_targets" == true ]]; then
  echo "Chats sync when you switch accounts and when you quit Claude."
  echo "After a switch, Claude closes, syncs, and opens again by itself."
  echo "A new account joins after you chat in it and then switch accounts or quit Claude."
else
  echo "Safe mode is active. New account and workspace IDs need explicit approval."
  echo "Chats sync when you quit Claude."
fi
echo "Nothing syncs while Claude is open."
if [[ "$sync_layout" == true ]]; then
  echo "Pins and custom groups sync whenever Claude closes or restarts."
fi
if [[ "$sync_routines" == true ]]; then
  echo "Claude Code routines sync whenever Claude closes or restarts."
fi
