# Architecture

Claude Session Sync treats Claude Desktop's private session registry as a
foreign model. The synchronization core never assumes that one Electron data
directory is universal; configured profile adapters translate each store into
the same local model.

## External interface

The deep `SessionSync` module exposes three operations:

```text
plan(request) -> Plan
apply(plan) -> RunReceipt
rollback(run_id) -> RecoveryReceipt
```

`switch(profile)` is a thin macOS adapter: it can wait a bounded time for every
managed Claude process to exit, applies the current plan, and launches the
selected profile. Because the termination watcher may start the same work, the
adapter waits briefly for the single writer and replans after a concurrent
commit. A distinct nonblocking handoff lock spans the final process check,
synchronization, launch, and bounded launch confirmation, preventing two
simultaneous profile launches. A durable guard written before launch remains
fail-closed until the selected profile is confirmed or the user explicitly
clears a known failed launch. It adds no synchronization policy.

In automatic target mode, the native watcher observes the default profile's
account UUID marker. A stable change requests one normal quit, then reuses
`switch(profile)` for sync and confirmed launch. A private account fingerprint
and phase receipt is saved before requesting quit, preventing helper restarts
from repeating a failed quit. Manual quits still trigger ordinary sync without
reopening the app. A persistent status menu and a non-activating window display
progress without depending on Notification Center.

## Domain language

- **Profile**: one Claude Electron data directory and launch command.
- **Target**: one `<account>/<workspace>` session directory inside a profile.
- **Replica**: one `local_<session-id>.json` file in a target.
- **Revision**: validated JSON content identified by SHA-256, size, and mtime.
- **Plan**: a canonical, deterministic set of copy operations or blocking
  conflicts.
- **Run**: the single-writer consistency boundary for one application or
  rollback transaction.

## Invariants

1. Cross-profile copying is disabled unless the configuration explicitly
   acknowledges it. Target discovery uses either the private exact-target
   allowlist or an explicit `all-configured-profiles` policy restricted to the
   configured profile roots.
2. A plan with conflicts or invalid replicas cannot be applied.
3. One exclusive file lock covers revalidation, journal creation, staging,
   commit, verification, and receipt persistence.
4. Existing destinations are journaled before mutation. New destinations are
   recorded so rollback can remove only the exact content the run introduced.
5. Live destinations are changed only by same-directory atomic replacement.
6. A rollback verifies every preimage before it changes live data and never
   suppresses restore failures.
7. Unknown layouts, malformed JSON, symlinks, and equal-mtime divergent
   revisions block instead of being guessed through.
8. Logs contain run IDs, phases, counts, bytes, and profile labels, never chat
   titles, contents, or raw account identifiers.
9. Default-profile process identity is explicit configuration, never inferred
   from a wrapper's launch command.
10. Process waits use monotonic wall-clock deadlines and subprocess timeouts;
    probe runtime cannot silently extend a configured handoff limit.
11. Chat sync changes only validated session registry files. Sidebar sync is a
    separate, optional adapter that changes only the three allowlisted Local
    Storage records needed for pins and custom groups plus the account-scoped
    group migration marker when required. It never copies an opaque
    Electron database, account tokens, cookies, or login state.
12. Sidebar writes use one atomic LevelDB batch, checksum-verified reads,
    exact preimage journals, post-write verification, and automatic rollback.
    A sidebar failure cannot undo or relabel an already committed chat sync.
13. Code routine sync is a separate, optional adapter. It writes only approved
    `scheduled-tasks.json` targets and cannot undo or relabel a chat sync.
14. Routine additions, edits, and deletions use a private three-way snapshot.
    Equal-time divergent edits stop only the routine adapter.

## Sidebar layout adapter

Claude stores chats and sidebar layout in separate stores. After chat sync
commits, the optional layout adapter derives account and workspace scopes from
the validated session registry. It then unions group names and pin order across
those scopes. Group assignments copy by group name only when the source scopes
agree. Ambiguous assignments remain unchanged in their existing scope and are
not guessed for new scopes.

The native helper is compiled locally from bundled LevelDB and Snappy sources.
It exposes only exact-key reads and an atomic write batch. Unknown record shapes,
missing records, internal record disagreement, checksum errors, and post-write
verification failures stop the layout pass. Chat receipts remain committed and
the aggregate status reports the layout failure separately.

## Code routine adapter

Claude Code initializes one scheduled-task manifest for the active account and
organization. The routine adapter discovers the same approved targets as chat
sync and merges task records by ID after Claude terminates. A first run unions
unique tasks and uses manifest modification time for differing copies. Later
runs compare every target with a private snapshot, which makes deletions and new
empty accounts unambiguous.

The task instruction files stay in Claude's shared scheduled-task directory.
Before any manifest write, the adapter confirms every selected instruction file
is a regular `SKILL.md` under its task ID. Multi-file writes have private exact
preimages, verification, rollback, and crash recovery. Cowork routine manifests
are excluded because their space context has different semantics.

## State machine

```text
IDLE -> DISCOVERING -> PLANNED
  -> NOOP | BLOCKED_APP | BLOCKED_CONFLICT
  -> LOCKED -> REVALIDATING -> JOURNALING -> STAGING
  -> COMMITTING -> VERIFYING -> COMMITTED
  -> ABORTING -> ROLLED_BACK | RECOVERY_REQUIRED
```

## Test seams

Tests exercise the external interface and CLI against temporary profile roots
and injected process/launch adapters. Required behaviors include deterministic
planning, explicit privacy scope, two-profile discovery, malformed and tied
revision blocking, one-writer concurrency, interruption recovery, app-reopen
abort, idempotent apply, byte-perfect rollback, and a 5,000-replica performance
budget. Routine tests cover union, newest-edit selection, deletion propagation,
new empty targets, malformed manifests, and missing task instructions.
