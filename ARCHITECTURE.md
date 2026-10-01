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

Every sync runs with Claude closed. `auto` and `sync` check the managed Claude
processes first, and while one runs they write nothing and report that they are
waiting. The planner decides per session from content and a private record of
the last agreed version (rules.py, ported from vinlim/claude-desktop-sync under
0BSD).

`switch(profile)` is a thin macOS adapter. It can wait a bounded time for every
managed Claude process to exit, applies the current plan, and runs the selected
profile's launch command. Because the termination watcher may start the same
work, the adapter waits briefly for the single writer and replans after a
concurrent commit. A distinct nonblocking handoff lock spans the final process
check, synchronization, and launch, preventing two simultaneous profile
launches. It runs the launch command even when the sync failed, and still
reports the failure. A launch fails only when the launch command cannot start
or exits with an error. It adds no synchronization policy.

In automatic target mode, the native watcher observes the default profile's
account UUID marker. A stable account change asks Claude to quit normally, runs
`switch(profile)` with Claude closed, and reopens Claude. A quit runs `auto`.
Saving a chat does not trigger a sync. At start the watcher runs `auto` once,
which waits if Claude is open. A persistent status menu and a non-activating
window display progress without depending on Notification Center.

## Domain language

- **Profile**: one Claude Electron data directory and launch command.
- **Target**: one `<account>/<workspace>` session directory inside a profile.
- **Replica**: one `local_<session-id>.json` file in a target.
- **Revision**: validated JSON content identified by SHA-256, size, and mtime.
- **Plan**: the create, replace, and retire steps of one run, and the chats
  it leaves alone.
- **Run**: the single-writer consistency boundary for one application or
  rollback transaction.

## Invariants

1. Cross-profile copying is disabled unless the configuration explicitly
   acknowledges it. Target discovery uses the private exact-target allowlist,
   the `logins` policy (approved targets plus folders Claude wrote a chat to
   after their account logged in on this Mac), or an explicit
   `all-configured-profiles` policy, always restricted to
   the configured profile roots.
2. A plan with invalid targets cannot be applied. A session the rules leave
   alone (tied, lost, unreadable, or future) is reported and never blocks
   another session.
3. One exclusive file lock covers revalidation, journal creation, staging,
   commit, verification, and receipt persistence.
4. Existing destinations are journaled before mutation. New destinations are
   recorded so rollback can remove only the exact content the run introduced.
5. Files change only by same-directory atomic replacement, by a hard-link
   create that never overwrites, or by a journaled removal. A write needs every
   managed Claude process stopped. The engine checks before journaling and
   again before the first write.
6. A rollback verifies every preimage before it changes live data and never
   suppresses restore failures. A chat run that a killed process left open
   mid-apply is closed by the next chat sync. It keeps what it wrote, and its
   journal keeps every file it replaced or removed. A rollback that was cut
   short or failed keeps blocking chat sync until it is finished.
7. File times never decide. Unknown layouts and symlinks block the run;
   malformed records and equal-activity divergent copies freeze only their
   session instead of being guessed through.
8. Logs contain run IDs, phases, counts, bytes, and profile labels, never chat
   titles, contents, or raw account identifiers.
9. Default-profile process identity is explicit configuration, never inferred
   from the profile's launch command.
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
15. Chat state is saved before a run's first write and after it settles. An
    unreadable chat state stops chat writes rather than forgetting what was
    agreed or seen.

## Sidebar layout adapter

Claude stores chats and sidebar layout in separate stores. After chat sync
commits, the optional layout adapter derives account and workspace scopes from
the validated session registry. One scope is the source, and a private snapshot
records which one. With none recorded, the first sync adopts the scope chosen
at install, else the signed-in scope if it has groups, else the only scope with
groups. After that the signed-in scope is the source, except right after an
account switch, when the scope just left is. The adapter copies the source's
groups, assignments, and group order into every other scope. Pins are one
shared list and stay as they are. When only the user can say which scope to
keep, the adapter stops and asks. `doctor` runs the same plan on a disposable
copy of the database.

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
  -> NOOP | BLOCKED_APP | BLOCKED_INVALID
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
