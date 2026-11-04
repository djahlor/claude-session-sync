#!/bin/zsh

set -euo pipefail

automatic_targets=false
sync_layout=false
sync_routines=false
sync_browser_logins=false
# ask, yes or no: whether to stop the artifact "didn't resume" warnings.
stop_artifact_warnings=ask
# ask, yes or no: whether to add the share-artifacts skill to Claude Code.
share_skill=ask

for argument in "$@"; do
  case "$argument" in
    --automatic-targets)
      automatic_targets=true
      ;;
    --sync-layout)
      sync_layout=true
      ;;
    --sync-routines)
      sync_routines=true
      ;;
    --sync-browser-logins)
      sync_browser_logins=true
      ;;
    --stop-artifact-warnings)
      stop_artifact_warnings=yes
      ;;
    --no-stop-artifact-warnings)
      stop_artifact_warnings=no
      ;;
    --share-skill)
      share_skill=yes
      ;;
    --no-share-skill)
      share_skill=no
      ;;
    *)
      echo "Unknown option: $argument" >&2
      echo "Use: ./install.sh [--automatic-targets] [--sync-layout] [--sync-routines] [--sync-browser-logins] [--stop-artifact-warnings | --no-stop-artifact-warnings] [--share-skill | --no-share-skill]" >&2
      exit 2
      ;;
  esac
done

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
if [[ "$sync_layout" == true ]]; then
  # On a terminal, setup asks which account's pins and groups the others copy.
  setup_args+=(--sync-layout --ask-main-account)
fi
if [[ "$sync_routines" == true ]]; then
  setup_args+=(--sync-routines)
fi
if [[ "$sync_browser_logins" == true ]]; then
  setup_args+=(--sync-browser-logins)
fi
if [[ "$stop_artifact_warnings" == yes ]]; then
  setup_args+=(--stop-artifact-warnings)
elif [[ "$stop_artifact_warnings" == no ]]; then
  # A no given in a Claude Code chat, saved like Enter at the question.
  setup_args+=(--no-stop-artifact-warnings)
else
  # On a terminal, setup asks once whether to stop the artifact warnings.
  setup_args+=(--ask-artifact-warnings)
fi
run_source_cli setup "${setup_args[@]}" --apply

# The share-artifacts skill shows Claude how to open a page from every account.
# Once added, each update keeps it current; the account list beside it stays.
# A skip is remembered, so an update does not ask again.
skill_source="$script_dir/skills/share-artifacts/SKILL.md"
skill_dir="$HOME/.claude/skills/share-artifacts"
skill_skipped="$support_dir/share-skill-skipped"
if [[ "$share_skill" == no && ! -f "$skill_dir/SKILL.md" ]]; then
  # A no given in a Claude Code chat, remembered like n at the question.
  : > "$skill_skipped"
fi
if [[ "$share_skill" == ask && -f "$skill_dir/SKILL.md" ]]; then
  share_skill=yes
elif [[ "$share_skill" == ask && -f "$skill_skipped" ]]; then
  share_skill=no
elif [[ "$share_skill" == ask ]]; then
  share_skill=no
  if [[ -t 0 && -t 1 ]]; then
    echo
    echo "Add the share-artifacts skill to Claude Code?"
    echo "Pages Claude publishes belong to one account and look deleted on the others."
    echo "Ask Claude \"share this page with my other accounts\" and it walks you through it."
    echo
    printf "Press Enter to add it, or type n to skip: "
    if read -r reply; then
      if [[ "$reply" == [nN]* ]]; then
        : > "$skill_skipped"
      else
        share_skill=yes
      fi
    fi
  fi
fi
skill_added=false
if [[ "$share_skill" == yes && -f "$skill_source" ]]; then
  mkdir -p "$skill_dir"
  cp "$skill_source" "$skill_dir/SKILL.md"
  rm -f "$skill_skipped"
  skill_added=true
fi

echo
echo "Claude Session Sync is installed."
echo "Open Claude normally. No separate profile app is needed."
if [[ "$automatic_targets" == true ]]; then
  echo "Chats sync when you switch accounts and when you quit Claude."
  echo "After a switch, Claude closes and reopens by itself only when something still needs it closed."
  echo "A new account joins the next time Claude closes after you sign in and open the Code tab once."
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
if [[ "$sync_browser_logins" == true ]]; then
  echo "The built-in browser's site logins come back after an account switch."
fi
if [[ "$stop_artifact_warnings" == yes ]]; then
  echo "Claude no longer answers artifact comments by itself, so its 'didn't resume' warnings stop."
fi
if [[ "$skill_added" == true ]]; then
  echo "To open a page from your other accounts, ask Claude: share this page with my other accounts."
fi
