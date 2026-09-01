#!/bin/zsh

set -euo pipefail

script_dir="$(cd "$(dirname "$0")" && pwd)"

echo "Claude Session Sync"
echo
echo "1. Safe mode: one Claude profile, approve each new account"
echo "2. Automatic mode: one Claude profile, trust future accounts in that profile"
echo "3. Two-profile mode: stay signed in to Work and Personal, trust both profiles"
echo
printf "Choose 1, 2, or 3: "
read -r choice

case "$choice" in
  1)
    exec "$script_dir/install.sh"
    ;;
  2)
    exec "$script_dir/install.sh" --automatic-targets
    ;;
  3)
    exec "$script_dir/install.sh" --automatic-targets --enable-personal
    ;;
  *)
    echo "No changes made. Run the installer again and choose 1, 2, or 3." >&2
    exit 2
    ;;
esac

