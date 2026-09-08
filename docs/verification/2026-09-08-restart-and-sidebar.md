# Account restart and sidebar verification

The installed local fix recovered from a transient process-check timeout and
restored consistent chat folders. This is live local evidence, not a promise
that future Claude versions keep the same private storage format.

## Bugs reproduced and fixed

- A process-inspection timeout ended automatic restart before Claude quit.
  Bounded read-only retries now recover without another sign-in.
- A queued account check could run after retry exhaustion. A pending-check guard
  and a second phase check prevent the extra retry.
- Folder differences were always treated as conflicts. The merge now compares
  explicit placements with the last successful snapshot, so one ordinary move
  propagates regardless of which account is active.
- Existing unresolved placements can be reconciled explicitly with
  `sync --prefer-current-sidebar`, using the normal journal and safety checks.

## Live checks

On 8 September 2026, a temporary test bundle ran the production watcher source
against the real Claude application and installed sync command. Only its account
marker and status files were isolated. The test injected one failed preflight,
then delegated subsequent checks and sync to the installed command.

The real app remained open during the failed check, then automatically quit,
synced, and reopened. Process IDs confirmed the restart, without a manual launch
during the observation. The visible completion window said
"Sync finished. Claude reopened." The full replay took about 21 seconds.

This was a simulated account-change signal against the real application, not a
fresh Google sign-in. The requested second Google account was rejected, so that
provider round-trip remains unverified. No authentication data was changed by
the replay. The temporary watcher was stopped and the normal LaunchAgent restored.

A subsequent journaled sidebar repair reconciled 16 old placement conflicts.
All 18 groups and 104 shared placements matched across 14 saved account/workspace
targets. Read-back found zero placement mismatches or remaining conflicts, and
another layout transform was a no-op. After reopening Claude, all 18 groups and
both visible pins remained. All 57 chats visible before repair kept their visible
placements. The installed runtime matched source, automatic restart remained
enabled, and status reported zero watcher, layout, or routine failures.

## Automated checks

The 227-test suite passed with three optional macOS installer tests skipped in
the default invocation. The three optional compiler, installer, and LaunchAgent
checks were run separately with their integration flags enabled and passed. Ruff and
`git diff --check` passed. Five consecutive native retry-exhaustion tests also
confirmed exactly three checks and no restart loop.
