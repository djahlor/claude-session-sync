#!/bin/zsh

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"

echo "Claude Session Sync"
echo
echo "Use the regular Claude app."
echo "1. Automatic sync (recommended): include new accounts after you sign in"
echo "2. Manual approval: approve each new account before syncing"
echo
printf "Choose 1 or 2: "
read -r choice

case "$choice" in
  1)
    exec "$script_dir/install.sh" --automatic-targets --sync-layout --sync-routines --disable-personal
    ;;
  2)
    exec "$script_dir/install.sh" --sync-layout --sync-routines --disable-personal
    ;;
  *)
    echo "No changes made. Run the installer again and choose 1 or 2." >&2
    exit 2
    ;;
esac
