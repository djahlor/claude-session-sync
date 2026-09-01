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
11. Session registry files are the only synchronized store. Sidebar pins,
    custom groups, account tokens, and other Electron Local Storage values are
    outside the current adapter and must never be copied as an opaque database.

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
budget.
