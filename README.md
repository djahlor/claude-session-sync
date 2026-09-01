# Claude Session Sync

Claude Session Sync copies Claude Desktop Code-session registry replicas between
explicitly configured local profiles on one Mac. It plans every change first,
blocks ambiguous data, applies copies through a single-writer transaction, and
keeps byte-for-byte recovery journals.

> [!WARNING]
> This is experimental software built against Claude Desktop's undocumented,
> private on-disk storage. Anthropic can change that layout without notice.
> Stop using the tool after a Claude update if `doctor` or `plan` reports an
> unknown layout. This project is not affiliated with or supported by
> Anthropic.

## Privacy boundary

Synchronization can move conversation-session metadata across Claude profiles
and across account namespaces. That can expose work history to a personal
profile, or personal history to a work profile. Review your employer's policy
before enabling it.

The generated config deliberately enables only Work, leaves the exact target
allowlist empty, keeps automatic target discovery off, and sets both consent
switches to `false`:

```json
{
  "version": 1,
  "approved_targets": [],
  "target_policy": "approved-only",
  "acknowledge_cross_profile_copy": false,
  "acknowledge_cross_account_copy": false
}
```

Set each switch to `true` only after reviewing the configured data roots and
accepting that boundary, then set the generated Personal profile's `enabled`
field to `true`. Every discovered profile/account/workspace target must also
match `approved_targets`; a future login blocks sync until separately approved.
Automatic mode changes `target_policy` to `all-configured-profiles`, which
accepts future account and workspace IDs only under roots already present in
the private config. Malformed data, conflicts, unknown layouts, symlinks, and
unexpected profile roots still block. Machine-readable output and logs contain aggregate counts, byte
totals, durations, plan IDs, and run IDs—not raw account IDs, session IDs,
paths, titles, or chat content.

## Requirements and compatibility

- macOS with Claude Desktop installed at a configured executable path.
- Python 3.9 or newer; runtime dependencies are Python standard library only.
- Xcode Command Line Tools (`xcrun swiftc`) for the event-driven watcher.
- The currently observed Claude Code-session registry layout. This is an
  undocumented compatibility surface, not a stable API.

The standard Work profile may launch Claude normally with no
`--user-data-dir`. The Personal profile ultimately runs the same Claude
executable with a distinct `--user-data-dir` argument. Process detection keeps
that executable path separate from wrapper launch commands and matches exact,
tokenized process arguments.

## Install

For the easiest install, download the repository, double-click
`Install Claude Session Sync.command`, and choose a mode. The two-profile mode
keeps Work and Personal signed in separately and enables automatic target
discovery inside those two configured profile roots. This path uses the macOS
system Python and does not install Python packages globally.

The same setup can run from Terminal:

```sh
./install.sh --automatic-targets --enable-personal
```

From this directory:

```sh
python3 -m pip install .
claude-session-sync install --dry-run
claude-session-sync install --apply
```

The per-user installer creates:

- `~/.config/claude-session-sync/config.json` with mode `0600`;
- `~/Applications/Claude Work.app`;
- `~/Applications/Claude Personal Synced.app` after Personal is enabled;
- a private system-Python-compatible runtime and compiled watcher under
  `~/Library/Application Support/ClaudeSessionSync`;
- `~/Library/LaunchAgents/com.claude-session-sync.watcher.plist`.

Enabled-profile wrappers call `switch Work` or `switch Personal`. The
installer never modifies `/Applications/Claude.app`. Generated plist files are
checked with `plutil -lint`; changed generated artifacts are backed up before
replacement, unchanged installs are no-ops, and the LaunchAgent is bootstrapped
or reloaded through `launchctl` after installation.

Edit the private config, verify every profile root and launch command (the
generated Personal root is `~/Library/Application Support/Claude-Personal`),
set the explicit `is_default` process identity, change the consent switches,
and enable Personal. Pin exactly the targets that exist now, then rerun install
to expose the Personal wrapper:

```sh
claude-session-sync approve-current-targets --dry-run
claude-session-sync approve-current-targets --apply
claude-session-sync install --apply
```

To stop routine account or workspace ID changes from blocking a configured
profile, enable automatic targets:

```sh
claude-session-sync configure --automatic-targets --dry-run
claude-session-sync configure --automatic-targets --apply
```

To enable both generated profile launchers in the same operation:

```sh
claude-session-sync configure --automatic-targets --enable-personal --apply
claude-session-sync install --apply
```

Validate before the first write:

```sh
claude-session-sync doctor
claude-session-sync status
claude-session-sync plan
claude-session-sync plan --json
```

## Use

Close every managed Claude window and process before synchronizing:

```sh
claude-session-sync sync
claude-session-sync sync --json
claude-session-sync switch Work
claude-session-sync switch Personal
claude-session-sync switch Personal --no-launch
claude-session-sync switch Work --wait-for-exit 15
```

Without `--wait-for-exit`, `switch` refuses to proceed if any managed Claude
process is open. Installed app wrappers use a bounded 15-second wait so they can
be clicked while the previous account is still shutting down. After exit,
`switch` safely waits for an automatic writer and replans if that writer commits
first; a separate handoff lock ensures simultaneous wrapper clicks cannot
launch two profiles. The wait uses a wall-clock deadline and a bounded process
probe, so slow process inspection cannot silently extend the advertised limit.
It launches the selected profile only after synchronization succeeds.
`auto` is intended for the watcher; app-running or writer-busy conditions return
success with an explicit `skipped` state so another termination event can retry
safely.

The recommended two-profile workflow requires one sign-in per profile:

1. Open `Claude Work` and sign in to the Work account once.
2. Quit Claude.
3. Open `Claude Personal Synced` and sign in to the Personal account once.
4. Later switches need only Quit, then open the other launcher.

After Claude terminates, the watcher discovers configured targets, skips
unchanged content through its hash cache, applies new copies with a recovery
journal, and writes an aggregate status receipt. It does not sync at sign-out
because Claude is still running and may still be writing its private stores.

Before launch, `switch` writes a private durable guard and removes it only after
the selected profile is observed. A timeout or the wrong profile returns
`launch_unconfirmed` and blocks later switches. After independently confirming
that no delayed Claude process can still appear, recover explicitly:

```sh
claude-session-sync clear-launch-guard --dry-run
claude-session-sync clear-launch-guard --apply
```

The Swift watcher subscribes to macOS workspace application-termination
notifications and runs one `auto` check at load. It does not poll every second,
coalesces termination bursts, and persists a private aggregate status receipt.
`status` and `doctor` surface watcher failure.
Planning uses cached content hashes, unchanged syncs are no-ops, and copies are
staged and atomically replaced. On the development Mac, a durable synthetic
700-copy transaction (4.2 MB) takes about 0.3–0.5 seconds, while the 5,000-file
planner test remains well under its three-second warm-cache budget. Actual
performance still depends on filesystem and profile size.

The watcher receives an event only after Claude has really terminated. A quit
request can return while Electron is still shutting down; observed shutdowns on
the development Mac ranged from about 1.2 to 6.0 seconds. The hot no-op sync
itself normally takes about 0.2–0.4 seconds. The wrapper removes the need to
guess or retry during that shutdown gap, but it cannot make Claude terminate
faster. Heavy system load can increase both numbers.

The installer detects the legacy `com.djahlor.claude-session-sync` LaunchAgent,
stops it, and moves its plist into the backup area before activating the new
watcher. If activation fails, it restores and restarts that legacy agent.

## Pins and custom groups

Claude stores session history separately from sidebar layout. Pins, custom
groups, collapsed sections, and grouping mode are held in Electron Local
Storage rather than the Code-session registry synchronized by this release.
Copying the whole Local Storage database would also copy unrelated account and
application state, so this tool does not do that.

A future layout adapter can selectively synchronize a tested allowlist of
sidebar keys with its own journal and compatibility check. Until that adapter
exists, session history syncs but pins and custom groups remain profile-local.

## Failure handling

`plan`, `auto`, and `switch` return `next_action=run-doctor` for malformed data
or conflicts. Exact-target safe mode returns
`next_action=approve-targets-or-enable-automatic-targets` when a routine new
target is the only blocker. Automatic mode removes that routine approval step,
but never converts data corruption or an ambiguous revision into an overwrite.

## Recovery

Every committed mutation has a run ID. Keep the state directory and installer
backups until the synchronized profiles have been verified.

```sh
claude-session-sync rollback RUN_ID
claude-session-sync rollback RUN_ID --json
claude-session-sync status
claude-session-sync doctor
claude-session-sync clear-launch-guard --dry-run
```

Rollback revalidates live data before restoring journaled preimages. If it
reports `RECOVERY_REQUIRED`, stop launching Claude and preserve the state
directory for manual inspection. Never delete a journal to silence an error.

Preview or apply removal with:

```sh
claude-session-sync uninstall --dry-run
claude-session-sync uninstall --apply
```

Uninstall removes only generated wrappers, watcher, and LaunchAgent by moving
them into the tool's backup area. It preserves the private config, transaction
state, and the original Claude application for recovery.

## Development

Run the standard-library test suite with both the active Python and macOS system
Python:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests
```

Tests use temporary profile, config, and state roots plus fake process, launch,
compiler, and plist-lint adapters. They do not run a live sync or write to
Claude, Applications, Library, or LaunchAgents locations.

Release verification also builds the wheel, installs it into an isolated
directory, and runs `install --dry-run` without the source checkout. The Swift
watcher source ships as package data so wheel installs remain self-contained.

## License

MIT. See [LICENSE](LICENSE).
