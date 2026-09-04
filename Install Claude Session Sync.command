#!/bin/zsh

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"

echo "Claude Session Sync"
echo
echo "1. Recommended: one Claude app; sync chats, routines, pins, and groups automatically"
echo "2. Safe mode: one Claude app; approve each new account"
echo "3. Advanced: separate Work and Personal apps"
echo
printf "Choose 1, 2, or 3: "
read -r choice

case "$choice" in
  1)
    exec "$script_dir/install.sh" --automatic-targets --sync-layout --sync-routines --disable-personal
    ;;
  2)
    exec "$script_dir/install.sh" --sync-layout --sync-routines --disable-personal
    ;;
  3)
    exec "$script_dir/install.sh" --automatic-targets --sync-layout --sync-routines --enable-personal
    ;;
  *)
    echo "No changes made. Run the installer again and choose 1, 2, or 3." >&2
    exit 2
    ;;
esac
