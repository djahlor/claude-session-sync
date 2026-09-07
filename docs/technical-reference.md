# Technical reference

For the standard setup and daily use, see the [README](../README.md).
This page covers commands, data handling, recovery, and development.

Commands below use the short name `claude-session-sync`, available after a pip
install. With the double-click installer, replace that name with the quoted path
`"$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync"`.

It plans chat changes first, uses a single writer, and keeps byte-for-byte
recovery journals. Sidebar sync changes only three tested Local Storage records.
Routine sync changes only Claude Code's scheduled-task manifests. It never
copies Claude's whole login database.

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
before enabling it. Routine definitions include local paths. Existing permission
grants, execution history, and unknown fields stay local to each destination.

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
  "sync_code_routines": false,
  "sync_sidebar_layout": false
}
```

Automatic mode accepts future account and workspace IDs only under data roots
already present in the private config. It does not trust new filesystem roots.
Malformed chats and real revision conflicts still stop chat writes. A malformed
routine or sidebar record skips only that separate update, so a completed chat
sync stays completed. Invalid chats do not stop valid routine or sidebar updates.
Receipts use aggregate counts; diagnostic error details can include local paths.

## Requirements and compatibility

- macOS with Claude Desktop installed at a configured executable path.
- Python 3.9 or newer; runtime dependencies are Python standard library only.
- Xcode Command Line Tools for the event-driven watcher and bundled sidebar
  helper. The installer compiles both locally.
- The currently observed Claude Code-session and routine-manifest layouts.
  These are undocumented compatibility surfaces, not stable APIs.

The standard setup launches Claude normally with no `--user-data-dir`.

## Install

For the easiest install, download the repository, double-click
`Install Claude Session Sync.command`, and choose option 1. It uses the normal
Claude app, trusts future accounts inside that app's existing data root, and
syncs chats, Code routines, pins, and groups after Claude quits. It uses the
macOS system Python and does not install Python packages globally.

The same setup can run from Terminal:

```sh
./install.sh --automatic-targets --sync-layout --sync-routines --disable-personal
```

From this directory:

```sh
python3 -m pip install .
claude-session-sync setup --automatic-targets --sync-layout --sync-routines --disable-personal --dry-run
claude-session-sync setup --automatic-targets --sync-layout --sync-routines --disable-personal --apply
```

The per-user installer creates:

- `~/.config/claude-session-sync/config.json` with mode `0600`;
- no replacement Claude app in the normal one-profile setup;
- a private system-Python-compatible runtime and compiled watcher under
  `~/Library/Application Support/ClaudeSessionSync`;
- `~/Library/LaunchAgents/com.claude-session-sync.watcher.plist`.

The installer never modifies `/Applications/Claude.app`. Generated plist files are
checked with `plutil -lint`; changed generated artifacts are backed up before
replacement, unchanged installs are no-ops, and the LaunchAgent is bootstrapped
or reloaded through `launchctl` after installation.

`setup` validates the requested config and stages the helpers before changing
installed artifacts. It holds the installation and sync locks, stops the old
watcher, and activates the new one last. A durable recovery snapshot restores
the previous files and service after failure. An interrupted setup is recovered
before the next applying setup or uninstall. If recovery cannot be verified,
the snapshot stays in the private support directory and the command reports its
location. Dry-run never applies a pending recovery.

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
3. Wait for the “Sync finished” notification, then open Claude normally.
   `claude-session-sync status` also reports `progress=finished`.

Status reports waiting for Claude, syncing, finished, or needs attention. It also
shows the last successful sync time. A missing or interrupted run is never
reported as finished. macOS notification settings can suppress banners, so use
the status command if no banner appears.

If Claude reopens before the watcher can write, no data is changed. Quit it
again and let the watcher finish the pending sync.

Manual commands remain available:

```sh
claude-session-sync sync
claude-session-sync sync --json
```

### Existing separate-profile installations

The installer no longer offers separate Work and Personal apps. Existing
profiles and these commands remain supported; this change deletes no data.
For normal account switching, use the workflow above.

```sh
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
success with an explicit `skipped` state. The watcher retries temporary shutdown
and writer-busy conditions for up to 30 seconds, without changing open app data.

### Automatic sync and launch recovery

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
notifications and runs one `auto` check at load. It coalesces termination bursts,
uses bounded retries during shutdown, and persists a private aggregate status
receipt. Completion and attention notifications contain no chat data.
`status` surfaces watcher failure; `doctor` probes current routine data and a
private copy of the sidebar database while Claude is closed. Successful adapter
checks record the installed Desktop build without imposing a version allowlist.
Planning uses cached content hashes, unchanged syncs are no-ops, and copies are
staged and atomically replaced. In a copy-only check on the development Mac,
catching up 676 chat records took about 68 seconds; a following unchanged sync
took about 14 seconds. These measurements are not a live completion guarantee.
Actual time depends on the number and size of records and the filesystem.

The watcher receives an event only after Claude has really terminated. A quit
request can return while Electron is still shutting down. The watcher waits for
the remaining processes during its retry window. The finished notification and
the status receipt show when sync has completed; they do not make Claude shut
down faster.

The installer detects the legacy `com.djahlor.claude-session-sync` LaunchAgent,
stops it, and moves its plist into the backup area before activating the new
watcher. If activation fails, it restores and restarts that legacy agent.

## Pins and custom groups

Claude stores chat history and sidebar layout separately. The layout adapter
reads and writes only the group-scope record, pin record, and matching dframe
store record. It checks their shapes and safely merges a group that appears in
only one record during Claude shutdown. Conflicting group IDs, names, or
assignments still stop the layout write.

On first use, existing group names are combined and added to every discovered
account/workspace scope. Unambiguous chat assignments are copied by group name.
If the same chat has different group assignments in two scopes, each existing
assignment stays in place and no assignment is guessed for a new scope. A
private snapshot restores pins and groups if a new account starts with empty
sidebar state.

## Claude Code routines

Claude Code stores routines in `scheduled-tasks.json`, separate from chat files.
The task instructions remain in Claude's shared `~/.claude/scheduled-tasks`
directory, so account switching needs only the small manifest copied.

The adapter merges routines by task ID across approved account and organization
targets. On first sync it combines unique tasks and keeps the newest manifest
when the same task differs. A private snapshot then tracks additions, edits, and
deletions so a deleted routine does not return from a stale account. New empty
accounts receive the current snapshot instead of deleting it.

Each multi-file update has exact preimages, post-write verification, automatic
rollback, and crash recovery. Manifests and their comparison snapshot share one
transaction. Definitions sync; permission grants, execution history, and unknown
metadata remain local to each destination. Routine errors are reported separately
and never change a completed chat result, but the overall command returns a
nonzero exit code with `progress=needs-attention`. This tool syncs Claude Code routines only;
Cowork routines use different space-specific context and are left untouched.

## Failure handling

`plan`, `auto`, and `switch` return `next_action=run-doctor` for malformed chat
data or conflicts. Exact-target safe mode returns
`next_action=approve-targets-or-enable-automatic-targets` when a new target is
the only blocker. Automatic mode removes that approval step,
but never converts data corruption or an ambiguous revision into an overwrite.

Routine and sidebar errors are separate. A malformed routine manifest returns
`routines={'state': 'skipped', 'reason': 'unsafe-routines', 'detail': '...'}`.
A malformed or changed sidebar format returns
`layout={'state': 'skipped', 'reason': 'unsafe-layout', 'detail': '...'}` after
the chat result. Details name the failed safety check without exposing chat or
account IDs. No affected record is written. If verification fails after a
write, the tool restores exact preimages. A failed restore creates
`RECOVERY_REQUIRED` state and stops later writes for that adapter.

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

During the next sync while Claude is closed, sidebar recovery recognizes an interrupted run whose
entire payload still matches its before or after state. It ignores only valid
wrapper timestamps and `collapsedGroups`, which Claude can update on reopen.
It keeps the current bytes, retains the original journal, records the observed
hashes, and checks the data again before closing that recovery. The completed
journal follows the same configured retention limit as other completed runs.
Reopening Claude is not required for recovery. Normal sync then
continues. Changed pins, assignments, names, unknown fields, and mixed payloads
with metadata changes still need attention; this exception never authorizes
overwriting them. Normal commits and partial rollback keep exact-byte checks.

Chat backups are now prepared outside the published `runs` directory. An
ordinary backup failure removes only its unfinished copies, before any live
write. A complete signed journal becomes visible in `runs` through one rename.
After a hard process crash, `doctor` and `status` report any leftover preparations
with `next_action=inspect-preparations`. These are separate from published runs.
Older manifestless runs still need individual inspection; the tool does not
guess whether a missing manifest was never written or was later deleted.

If process inspection is denied, sync stops with
`reason=process-inspection-unavailable`. Run it in a normal local Terminal with
access to the Mac process table. Do not disable the stopped-app check.

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
directory, and runs `setup --dry-run` without the source checkout. The Swift
watcher and third-party LevelDB/Snappy source ship as package data so wheel
installs remain self-contained. LevelDB keeps its BSD license and Snappy keeps
its COPYING notice under `src/claude_session_sync/vendor`.

Opt-in macOS integration tests compile the real helpers and exercise an isolated
install, config upgrade, and uninstall with a simulated service controller:

```sh
RUN_MACOS_INSTALLER_INTEGRATION=1 PYTHONPATH=src python3 -m unittest discover -s tests -p test_installer_integration.py -v
```

`RUN_LAUNCHCTL_INTEGRATION=1` additionally registers and removes a uniquely named
test service in the current GUI session. It never targets the production watcher.

## License

MIT. See [LICENSE](../LICENSE).
