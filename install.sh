#!/bin/zsh

set -euo pipefail

automatic_targets=false
enable_personal=false
disable_personal=false
sync_layout=false

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
    *)
      echo "Unknown option: $argument" >&2
      echo "Use: ./install.sh [--automatic-targets] [--sync-layout] [--enable-personal|--disable-personal]" >&2
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

run_source_cli install --apply

configure_args=()
if [[ "$automatic_targets" == true ]]; then
  configure_args+=(--automatic-targets)
fi
if [[ "$enable_personal" == true ]]; then
  configure_args+=(--enable-personal)
fi
if [[ "$disable_personal" == true ]]; then
  configure_args+=(--disable-personal)
fi
if [[ "$sync_layout" == true ]]; then
  configure_args+=(--sync-layout)
fi
if (( ${#configure_args[@]} > 0 )); then
  run_source_cli configure "${configure_args[@]}" --apply
  run_source_cli install --apply
fi

echo
echo "Claude Session Sync is installed."
if [[ "$enable_personal" == true ]]; then
  echo "Open Claude Work or Claude Personal Synced from ~/Applications."
  echo "Open Claude Personal Synced once and sign in to the second account."
else
  echo "Open Claude normally. No separate profile app is needed."
fi
if [[ "$automatic_targets" == true ]]; then
  echo "Future account and workspace IDs inside configured profiles will sync after Claude quits."
else
  echo "Safe mode is active. New account and workspace IDs need explicit approval."
fi
if [[ "$sync_layout" == true ]]; then
  echo "Pins and custom groups will sync after Claude quits."
fi
