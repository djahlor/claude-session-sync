# Finish checklist

## Scope and starting state

The live watcher reports a chat-sync failure, and the health check reports a
recovery problem. The current tests pass, but that does not prove live recovery.
Routine and sidebar sync have completed independently. The installed fixes are
local changes; the private GitHub repository still has the earlier revision.

Finish the sync repair, installation recovery, verification, and delivery to the
existing private repository. Keep repository visibility unchanged and do not
post messages or release announcements. Preserve live chats, credentials,
account permissions, recovery records, and unrelated work.

## Checks

- [x] Reproduce the live recovery failure through a read-only or disposable-copy test.
- [x] Fix the cause and add a regression test at the same boundary.
- [x] Make setup, upgrade, and uninstall recover safely from partial failure.
- [x] Test the packaged install on an isolated home with real macOS helpers.
- [x] Complete independent code review and resolve concrete safety findings.
- [ ] Install the tested runtime and verify live sync while Claude is stopped.
- [ ] Verify the resulting account data and record exact remaining limitations.
- [ ] Commit and push the source, tests, and current documentation.
- [ ] Verify GitHub checks pass on the pushed revision.

## Evidence rules

Use aggregate results in public-ready files. Keep real session bodies, account
identifiers, manifests, database files, and secrets out of the repository and
tool output. Do not discard an invalid journal or bypass an authentication check
to make sync appear successful. Back up any approved repair and verify its exact
effect before continuing.

## Verification results

- The 184-test suite passed on Python 3.9 and 3.12, with three opt-in integrations skipped.
- Real helpers compiled, and the isolated install, config upgrade, and uninstall passed with simulated launchctl.
- The same real-artifact lifecycle passed after importing the built wheel from an isolated package directory.
- An uncaught subprocess exit recovered on the next installer invocation; an overlapping installer was rejected by the operating-system lock.
- A real uniquely named LaunchAgent activation was attempted but rejected with exit 5; activation remains unverified.
- The copied-data replay completed 676 chat operations, followed by a no-op run; routines and sidebar updates also passed.
- Every completed preimage in the exact legacy orphan still matches a current chat replica byte for byte.
- A separate private one-off recovery command passed five synthetic tests, including its full apply sequence with injected system boundaries.
- Live installation and recovery remain paused because this agent cannot inspect Mac processes; no live repair was applied.
