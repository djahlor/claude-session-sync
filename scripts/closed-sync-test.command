#!/bin/zsh
# Double-click this in Finder. It opens Terminal and runs closed-sync-test.sh,
# which quits Claude, syncs, and opens Claude again.

exec "$(cd "$(dirname "$0")" && pwd)/closed-sync-test.sh" "$@"
