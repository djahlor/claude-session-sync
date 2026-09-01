# Claude Session Sync

Claude Session Sync keeps Claude Desktop Code chats, pins, and custom groups
available when you change accounts on one Mac. The normal setup uses one Claude
app and one local data root. A separate Personal app remains an optional,
advanced mode.

It plans chat changes first, uses a single writer, and keeps byte-for-byte
recovery journals. Sidebar sync changes only three tested Local Storage records.
It never copies Claude's whole login database.

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

The base config starts in safe mode. The recommended installer choice then
enables automatic account discovery and sidebar sync inside the standard Claude
data root:

```json
{
  "version": 1,
  "approved_targets": [],
  "target_policy": "approved-only",
  "acknowledge_cross_profile_copy": false,
  "acknowledge_cross_account_copy": false,
  "sync_sidebar_layout": false
}
```

Automatic mode accepts future account and workspace IDs only under data roots
already present in the private config. It does not trust new filesystem roots.
Malformed chats and real revision conflicts still stop chat writes. A malformed
sidebar record skips only the sidebar update, so a completed chat sync stays
completed. Machine-readable output and logs contain aggregate counts, not raw
account IDs, session IDs, paths, titles, or chat content.

## Requirements and compatibility

- macOS with Claude Desktop installed at a configured executable path.
- Python 3.9 or newer; runtime dependencies are Python standard library only.
- Xcode Command Line Tools for the event-driven watcher and bundled sidebar
  helper. The installer compiles both locally.
- The currently observed Claude Code-session registry layout. This is an
  undocumented compatibility surface, not a stable API.

The standard profile launches Claude normally with no `--user-data-dir`.
Advanced two-profile mode adds a second data root and two generated launchers.

## Install

For the easiest install, download the repository, double-click
`Install Claude Session Sync.command`, and choose option 1. It uses the normal
Claude app, trusts future accounts inside that app's existing data root, and
syncs chats, pins, and groups after Claude quits. It uses the macOS system
Python and does not install Python packages globally.

The same setup can run from Terminal:

```sh
./install.sh --automatic-targets --sync-layout --disable-personal
```

From this directory:

```sh
python3 -m pip install .
claude-session-sync install --dry-run
claude-session-sync install --apply
```

The per-user installer creates:

- `~/.config/claude-session-sync/config.json` with mode `0600`;
- no replacement Claude app in the normal one-profile setup;
- Work and Personal launchers only in advanced two-profile mode;
- a private system-Python-compatible runtime and compiled watcher under
  `~/Library/Application Support/ClaudeSessionSync`;
- `~/Library/LaunchAgents/com.claude-session-sync.watcher.plist`.

The installer never modifies `/Applications/Claude.app`. Generated plist files are
checked with `plutil -lint`; changed generated artifacts are backed up before
replacement, unchanged installs are no-ops, and the LaunchAgent is bootstrapped
or reloaded through `launchctl` after installation.

Safe mode pins only the targets that exist now:

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

To use the advanced separate Personal app:

```sh
claude-session-sync configure --automatic-targets --sync-layout --enable-personal --apply
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

The normal workflow has three steps:

1. Sign out and sign in to another account in Claude when needed.
2. Quit Claude once.
3. Wait for `claude-session-sync status` to report `ready`, then open Claude
   normally. The watcher syncs chats, pins, and groups while Claude is closed.

If Claude reopens before the watcher can write, no data is changed. Quit it
again and let the watcher finish the pending sync.

Manual commands remain available:

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

Advanced two-profile mode requires one sign-in per profile:

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

Claude stores chat history and sidebar layout separately. The layout adapter
reads and writes only the group-scope record, pin record, and matching dframe
store record. It checks their shapes and requires the two group records to
agree before writing.

On first use, existing group names are combined and added to every discovered
account/workspace scope. Unambiguous chat assignments are copied by group name.
If the same chat has different group assignments in two scopes, each existing
assignment stays in place and no assignment is guessed for a new scope. A
private snapshot restores pins and groups if a new account starts with empty
sidebar state.

## Failure handling

`plan`, `auto`, and `switch` return `next_action=run-doctor` for malformed chat
data or conflicts. Exact-target safe mode returns
`next_action=approve-targets-or-enable-automatic-targets` when a routine new
target is the only blocker. Automatic mode removes that routine approval step,
but never converts data corruption or an ambiguous revision into an overwrite.

Sidebar errors are separate. A malformed or changed sidebar format returns
`layout={'state': 'skipped', 'reason': 'unsafe-layout'}` after the chat result.
No sidebar record is written. If verification fails after a write, the tool
restores the three exact preimages. A failed restore creates
`RECOVERY_REQUIRED` state and stops later sidebar writes.

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

Tests use temporary profile, config, state, and synthetic sidebar records plus
fake process, launch, compiler, and plist-lint adapters. They do not write to
live Claude, Applications, Library, or LaunchAgents locations.

Release verification also builds the wheel, installs it into an isolated
directory, and runs `install --dry-run` without the source checkout. The Swift
watcher and third-party LevelDB/Snappy source ship as package data so wheel
installs remain self-contained. LevelDB keeps its BSD license and Snappy keeps
its COPYING notice under `src/claude_session_sync/vendor`.

## License

MIT. See [LICENSE](LICENSE).
