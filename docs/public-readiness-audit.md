# Release verification

Updated: 2026-09-07

## Decision

Keep the repository private. Code verification and live account-switch proof
are separate requirements. A copied-data replay is useful evidence, but does
not prove that the installed watcher recovered the user's live installation.

The original September 5 audit is retained in Git history. Historical upstream
sources remain in [Claude Code change-risk research](claude-code-change-risk.md).
That research does not establish compatibility with future Desktop versions.

## Resolved code findings

| Finding | Current implementation |
| --- | --- |
| Invalid chats stopped other features | Directory discovery is separate from chat validation; routines and sidebar updates run independently. |
| Health checks used stale receipts | `doctor` probes current routines and a disposable sidebar database copy, with the Desktop build in its result. |
| Duplicate adapter recovery | Routines and sidebar share `RecordJournal`, exact preimages, compare-before-write, rollback checks, and retention. |
| Oversized command module | Configuration, adapter execution, health, and progress have separate modules. |
| Routine runtime state crossed accounts | Only definition fields sync; permissions, execution history, and unknown metadata remain destination-local. |
| Unchecked routine paths | Instruction paths must be exact children of the shared scheduled-task root, with no symlinked ancestors. |
| Nonstandard JSON passed validation | A shared strict decoder rejects duplicate keys, NaN, infinities, and overflowing numbers. |
| Unclear completion | Progress records distinguish waiting, syncing, finished, and needs attention; success time is not invented. |
| Interrupted backup poisoned later sync | A complete journal is published only after its backups and signed manifest are durable. |
| Failed preparations accumulated silently | Ordinary failures remove unfinished copies; crash residue appears in health and status. |
| Installation changed settings in separate invocations | `setup` receives the desired validated config and installs it with the owned artifacts. |

Chat-file transactions retain their authenticated streaming journal. The shared
record journal serves small routine manifests and atomic LevelDB batches.
These physical backends differ; forcing every file copy into a serialized
in-memory record would increase memory and journal size. They share strict JSON,
filesystem primitives, locks, stopped-app checks, and explicit terminal states.

## Observed compatibility

The inspected Desktop build was `1.46388.4`. A disposable copy of the configured
data completed chat, routine, and sidebar synchronization, followed by a no-op
second run. The first replay changed 676 chat records in about 68 seconds; the
second took about 14 seconds. These are local measurements, not a speed promise.

The replay reported 3 routines, 17 groups, and 27 pins. It preserved 16 ambiguous
assignments instead of guessing a different group for them. Those counts do not
prove that the Claude UI displayed the resulting state.

## Live recovery boundary

The live state contains a legacy run without its manifest. Its completed
preimages match current replicas byte for byte, and an empty temporary backup
file supports the interrupted-preparation diagnosis. The missing manifest still
prevents proof of the original transaction phase.

Automatic sync must not ignore that run. A one-off repair must retain the
original bytes and a verified current-data snapshot, validate the exact known
inventory, hold the writer locks, and prove Claude has stopped before archiving
the run and replanning. No fabricated manifest or automatic deletion is allowed.

The agent's current process-inspection permission is insufficient for live
writes. The legacy run and live data remain unchanged by this repair turn.
Live installation, recovery, and visible account-switch proof remain pending.

## Release gates

- Passed: installer rollback, uncaught process exit, interrupted-upgrade recovery, and overlapping-run lock tests.
- Passed: packaged install with real macOS helpers in an isolated home, using simulated launchctl.
- Passed: independent review, with no unresolved code-level safety finding in the reviewed scope.
- Passed: [GitHub checks on the pushed code revision](https://github.com/popcorn-so/claude-session-sync/actions/runs/34127484263), `a1ffa7a6e248c53c77b7cc12aece7dceae600d56`, with Python 3.9, 3.12, and 3.13.
- Not completed: real service activation and live account-switch proof; both remain required before a public release claim.

The local 184-test suite passes with Python 3.9 and 3.12, with three explicitly
gated integration tests skipped. The real compiler and full artifact lifecycle
tests also passed separately against the built wheel. Actual LaunchAgent
activation was attempted using a unique temporary test service, but macOS
rejected bootstrap with exit 5. No production watcher was replaced by that test.

Python classifiers are limited to 3.9, 3.12, and 3.13, matching the CI matrix.
The implementation still depends on undocumented Claude Desktop formats.
Future changes can require adapter updates even when the public Claude Code
changelog contains no matching storage announcement.
