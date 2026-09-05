# Public-readiness audit

Date: 2026-09-05

## Verdict

Claude Session Sync is suitable for a private team beta. It is not ready for a
public release yet. The safety controls and tests are strong, but undocumented
Claude Desktop storage can change independently of the public Claude Code
changelog. The current implementation also has three orchestration paths for
transactions and recovery.

Public release should wait for the blockers below. A team beta can continue if
participants understand that the repository is alpha software, keep the private
state directory, and stop after an adapter reports an unknown format.

Historical evidence and source links are in
[Claude Code change-risk research](claude-code-change-risk.md).

## Verified strengths

- Chat writes use planning, revalidation, staging, checksums, a single-writer
  lock, durable journals, post-write verification, and exact rollback.
- Sidebar writes touch three allowlisted LevelDB records in one atomic batch.
- Routine writes are limited to discovered `scheduled-tasks.json` targets and
  restore exact file preimages after a failed multi-file update.
- Routine and sidebar failures do not relabel a committed chat result.
- The current suite passes 109 tests with the active and macOS system Python.
- Ruff and both shell syntax checks pass.
- The package metadata labels the project as alpha and the README warns that the
  storage formats are undocumented.

## Release blockers

### P1: adapter isolation is incomplete

`RoutineSynchronizer._targets()` calls the chat-oriented `SessionStore.discover()`
and rejects every `invalid_replica`. A malformed chat therefore prevents routine
sync even when every routine manifest is valid. This conflicts with the stated
feature-isolation model.

Evidence: `src/claude_session_sync/routines.py:392` and
`src/claude_session_sync/routines.py:398`.

Fix: split structural target discovery from chat-replica validation. Routines
should depend on safe account/workspace directories, while the chat adapter alone
should depend on valid chat replicas.

### P1: compatibility checks are stale after an update

`doctor` validates chat discovery and then reads the last saved routine and
layout status. It does not probe the current routine manifest or sidebar record
shape. After a Claude Desktop update, an old successful status can still produce
`healthy` before the first new sync attempt.

Evidence: `src/claude_session_sync/cli.py:761` and
`src/claude_session_sync/cli.py:807`.

Fix: every adapter needs a read-only `probe()` operation. When the Claude Desktop
version changes, run probes before any write and save a compatibility receipt for
that exact application build.

### P1: recovery logic has three standards

Chats use `RunJournal` and `TransactionEngine`. Sidebar and routine adapters each
implement another journal format, commit loop, rollback path, pruning path, and
crash-recovery parser. The routine recovery function has cyclomatic complexity 23
and cognitive complexity 59.

Evidence: `src/claude_session_sync/transaction.py:83`,
`src/claude_session_sync/layout.py:657`, and
`src/claude_session_sync/routines.py:477`.

Fix: use one journal envelope and one state machine. Keep storage-specific
read/write callbacks for files and LevelDB, but centralize state transitions,
preimage validation, recovery decisions, retention, and receipts.

### P1: the command layer is a god module

`cli.py` is 1,218 lines. Its `run()` function is 207 lines with cyclomatic
complexity 27 and cognitive complexity 67. Layout and routine summary, status,
and failure functions are near copies.

Evidence: `src/claude_session_sync/cli.py:252`,
`src/claude_session_sync/cli.py:297`, and
`src/claude_session_sync/cli.py:1004`.

Fix: make the CLI parse and route only. A single adapter runner should execute
enabled adapters, normalize errors, persist status, and build aggregate output.

### P1: scheduler state is merged without a policy

Routine merging preserves every unknown top-level key and merges each whole
metadata value by manifest modification time. Known keys include
`recordedSkips` and `runRetries`, which appear to be runtime state rather than
routine definitions. Copying them may change which account retries or skips a
run. This consequence is not tested or documented.

Evidence: `src/claude_session_sync/routines.py:147` and
`src/claude_session_sync/routines.py:240`.

Fix: identify definition fields and scheduler-runtime fields from observed
fixtures. Sync definitions only unless a field has an explicit cross-account
policy.

## Important hardening

### P2: claimed routine path boundary is not enforced

Routine validation requires an absolute `SKILL.md` whose parent matches the task
ID. It does not require the file to live under Claude's scheduled-task root,
despite the documentation describing that boundary.

Evidence: `src/claude_session_sync/routines.py:431`.

Fix: resolve the expected scheduled-task root once, reject symlinked ancestors,
and require every definition path to be an exact child of that root.

### P2: JSON acceptance is not uniformly strict

Python's JSON decoder accepts nonstandard `NaN` and infinity constants by
default, and the encoder can emit them. A corrupted numeric value can therefore
pass the parser and be written back as invalid JSON for JavaScript.

Evidence: `src/claude_session_sync/routines.py:297` and
`src/claude_session_sync/routines.py:305`.

Fix: use one strict JSON loader and encoder across config, snapshots, journals,
sessions, routines, and status receipts.

### P2: diagnostics lack current compatibility context

Status reports counts and a coarse state, but not the last attempt time, last
success time, application build, adapter phase, or a direct next action. This
makes shutdown delay and skipped work hard to distinguish.

Evidence: `src/claude_session_sync/cli.py:1156`.

Fix: use one typed status receipt with timestamps, current phase, Claude build,
adapter results, safe error codes, and `next_action`.

### P2: installation is not one transaction

The shell installer applies the base installation, changes configuration, then
applies installation again. If the later step fails, the first installation may
remain even though the final message only reports a stopped line.

Evidence: `install.sh:66` and `install.sh:75`.

Fix: add one `setup` command that validates prerequisites and desired settings
before committing a single install plan. Report the exact rollback command when
a later operating-system step fails.

### P2: the support matrix is wider than CI

Package metadata claims Python 3.9 through 3.14, while CI tests two versions on
one macOS runner. Public claims should match tested combinations.

Evidence: `pyproject.toml:13` and `.github/workflows/ci.yml:12`.

Fix: expand CI or narrow the classifiers. Add a clean-user installation test,
watcher activation test, and an upgrade test from the previous release.

## One architecture

Use one pipeline while keeping format-specific logic separate:

```text
Claude build change
  -> CompatibilityGate.probe(adapters)
  -> TargetDiscovery.discover_directories()
  -> ChatAdapter | RoutineAdapter | SidebarAdapter
  -> AdapterRunner.plan_and_apply()
  -> MutationJournal + backend callbacks
  -> AggregateStatusReceipt
```

Each adapter should expose the same contract:

```python
class SyncAdapter(Protocol):
    name: str

    def probe(self) -> CompatibilityResult: ...
    def plan(self, targets: Sequence[Target]) -> AdapterPlan: ...
    def apply(self, plan: AdapterPlan) -> AdapterReceipt: ...
```

The shared runner should own process checks, lock acquisition, status files,
error classification, and aggregate output. Adapter modules should own only
format validation, merge policy, and storage callbacks.

This structure preserves safe feature-level failure. Unknown routine data can
skip routines while compatible chats and sidebar state continue. A real conflict
still stops writes for the affected adapter.

## Change-resilience plan

1. Record the Claude Desktop build and adapter schema fingerprints after every
   successful probe.
2. When the build changes, probe all adapters before writes and disable only the
   incompatible adapter.
3. Keep redacted structural fixtures for each observed build and run them in CI.
4. Monitor official Claude Code releases as an early signal, not as proof of
   Claude Desktop storage compatibility.
5. Publish a compatibility table with tested Claude Desktop builds and tool
   releases.

## Public launch path

1. Keep the repository private for the team beta while the P1 items are fixed.
2. Test installation and account switching with fresh macOS users and synthetic
   Claude data.
3. Publish a versioned GitHub release with checksums, release notes, and a
   rollback-tested installer.
4. Enable GitHub private vulnerability reporting and replace the vague security
   contact with a real private route.
5. Make the repository public only after the compatibility table has at least one
   release-to-release upgrade result.

The product story is strong enough for LinkedIn and Twitter after that gate: it
solves local continuity across Claude accounts without copying authentication
state. Marketing should describe it as an independent macOS alpha, not a stable
Anthropic integration.
