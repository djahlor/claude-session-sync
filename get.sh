#!/bin/zsh
# Install or update Claude Session Sync with one Terminal command:
#
#   zsh -c "$(curl -fsSL https://raw.githubusercontent.com/djahlor/claude-session-sync/main/get.sh)"
#
# `zsh -c "$(curl ...)"` keeps the keyboard attached, so the installer can ask
# which account is the main one. Piping curl into zsh would not.

set -euo pipefail

repository="${CLAUDE_SESSION_SYNC_REPOSITORY:-djahlor/claude-session-sync}"
ref="${CLAUDE_SESSION_SYNC_REF:-main}"
archive="${CLAUDE_SESSION_SYNC_ARCHIVE:-https://github.com/$repository/archive/$ref.tar.gz}"
claude_app="${CLAUDE_SESSION_SYNC_CLAUDE_APP:-/Applications/Claude.app}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Claude Session Sync works on macOS only." >&2
  exit 1
fi

if [[ ! -d "$claude_app" ]]; then
  echo "Claude Desktop is not installed. Get it from https://claude.ai/download, then run this again." >&2
  exit 1
fi

if ! xcode-select -p >/dev/null 2>&1 || ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "Claude Session Sync needs Apple's Command Line Tools to build its helpers."
  echo "macOS will now ask to install them. Run this command again when that finishes."
  xcode-select --install >/dev/null 2>&1 || true
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "Downloading Claude Session Sync..."
curl -fsSL "$archive" | tar -xz -C "$work" --strip-components 1

"$work/install.sh" --automatic-targets --sync-layout --sync-routines
