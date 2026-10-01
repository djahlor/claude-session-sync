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

Automatic mode uses the `logins` target policy. Only sidebar folders of real
logins take part: the approved targets, plus any folder whose account logged in
on this Mac and that holds a chat Claude saved after that login. Leftover
folders from another Mac never had a login here and are ignored. It does not
trust new filesystem roots. A malformed chat, or two copies that changed to different
states with equal activity, is left alone and reported; every other chat still
syncs. A malformed routine or sidebar record skips only that separate update.
Receipts use aggregate counts; diagnostic error details can include local paths.

## Requirements and compatibility

- macOS with Claude Desktop installed at a configured executable path.
- Python 3.9 or newer; runtime dependencies are Python standard library only.
- Xcode Command Line Tools for the event-driven watcher and bundled sidebar
  helper. The installer compiles both locally.
- The currently observed Claude Code-session and routine-manifest layouts.
  These are undocumented compatibility surfaces, not stable APIs.

The standard setup launches Claude normally with no `--user-data-dir`.

## Limits

Sync reads and writes Claude Desktop's private files. Anthropic documents none
of them and can change any of them in an update. These changes would break
sync:

- A new key or record shape for pins and groups in Claude's Local Storage
  database. This is the most likely break, because sync writes three exact
  records there.
- A new field, version, or location for routines in `scheduled-tasks.json`.
- A new folder layout or file name under
  `claude-code-sessions/<account>/<workspace>/`, or a new shape inside
  `local_<session>.json`.
- A new way of naming account and workspace folders, or a new sign-in line in
  Claude's `main.log`. A new account would then not join sync.

Sync never guesses at a format it does not know. It stops the affected part
and writes nothing there. An unreadable chat is left alone and reported. A
folder layout it does not know stops chat sync with `state=blocked_invalid`.
An unknown pins, groups, or routines record skips that update with
`reason=unsafe-layout` or `reason=unsafe-routines`. The other parts still
sync, `status` reports `needs-attention`, and `doctor` shows which part failed.
The last full check on a copy of real data used Claude Desktop 1.46388.4, in
September 2026.

## Install

For the easiest install, download the repository, double-click
`Install Claude Session Sync.command`, and choose option 1. It uses the normal
Claude app, trusts future logins inside that app's existing data root, and
syncs chats, Code routines, pins, and groups when the account changes or Claude
quits. It uses the macOS system Python and does not install Python packages
globally.

If two or more accounts already have custom groups, the installer then asks
which account is the main one. The other accounts copy its pins and groups the
next time Claude closes. `setup --ask-main-account` asks only in a terminal and
only before any account was adopted. It asks before the watcher starts, so no
sync can run first.

The same setup can run from Terminal:

```sh
./install.sh --automatic-targets --sync-layout --sync-routines
```

From this directory:

```sh
python3 -m pip install .
claude-session-sync setup --automatic-targets --sync-layout --sync-routines --dry-run
claude-session-sync setup --automatic-targets --sync-layout --sync-routines --apply
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

In automatic mode, chats sync when the account changes and when Claude quits.
Nothing syncs while Claude is open, and saving a chat never starts a sync.

1. Sign out and sign in to another account in Claude when needed.
2. The helper quits Claude, syncs, and opens Claude again.
3. Keep working. The next switch or quit syncs again.

A new account joins after you chat in it and then switch accounts or quit
Claude. `claude-session-sync restart-claude` asks the helper to quit Claude,
sync, and reopen it now. Status reports waiting, syncing, finished, or needs
attention, and the last successful sync time. A missing or interrupted run is never reported as finished. A
menu-bar status item and a non-activating status window show progress without
depending on macOS notification permissions.

Safe mode still syncs after a manual quit.

Manual commands remain available:

```sh
claude-session-sync sync
claude-session-sync sync --json
```

### Switch from the command line

Older versions offered separate Work and Personal apps. `setup` moves those
apps into the backup folder and removes the Personal profile from the config.
The watcher runs `switch` after an account change. It also runs by hand:

```sh
claude-session-sync switch Work
claude-session-sync switch Work --no-launch
claude-session-sync switch Work --wait-for-exit 15
```

Without `--wait-for-exit`, `switch` refuses to proceed if any managed Claude
process is open. With it, `switch` waits that many seconds for Claude to finish
quitting. After exit, `switch` waits up to 15 seconds for another writer to
finish, then plans again and syncs. A separate handoff lock stops two switches
at once from both launching Claude. The wait uses a wall-clock deadline and a
bounded process probe, so slow process inspection cannot extend the limit.
`switch` launches Claude after the sync, even when the sync failed.
`auto` is intended for the watcher. While Claude is open it writes nothing and
returns success with `state=waiting` and `reason=claude-open`. If Claude opens
again after the chats synced but before pins, groups, or routines did, those
report `deferred` and the run reports `progress=waiting-for-Claude`, not
finished. The next quit syncs them. A busy writer
returns success with `state=skipped` and `reason=busy`. `sync` reports the same
states but exits nonzero. The watcher retries a busy writer, and a Claude that
is still shutting down after a quit, for up to 30 seconds, without changing open
app data.

### Automatic sync and launch

After Claude terminates, the watcher discovers configured targets, skips
unchanged content through its hash cache, applies new copies with a recovery
journal, and writes an aggregate status receipt. It does not sync at sign-out
because Claude is still running and may still be writing its private stores.

After the sync, `switch` runs the profile's launch command, even when the sync
failed, so a switch never leaves Claude closed. A failed sync still exits
nonzero with its result and `launch=started`. `switch` returns
`state=launch_failed` only when the launch command cannot start or exits with
an error, or is still running after 10 seconds. The launch command is `open`,
which returns once macOS has started Claude. A failed or slow launch leaves
nothing behind that blocks the next switch. Older versions could leave a
`launch-pending.json` file after a slow launch. `setup` removes it.

The Swift watcher subscribes to macOS workspace application-termination
notifications and runs one `auto` check at load. If Claude is open then, that
check writes nothing and the next quit or switch syncs. It coalesces termination bursts,
uses bounded retries during shutdown, and persists a private aggregate status
receipt. Completion and attention notifications contain no chat data.
`status` surfaces watcher failure; `doctor` probes current routine data and,
while Claude is closed, plans the sidebar sync that runs when Claude closes on
a private copy of the sidebar database, with the same signed-in account and
pending choice. Successful adapter
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

## Pins and custom groups

Claude stores chat history and sidebar layout separately. The layout adapter
reads and writes only the group-scope record, pin record, and matching dframe
store record. It checks their shapes and stops on anything it does not know.
Pins are one list shared by every account, keyed by chat ID, so they stay as
they are. Custom groups belong to one account and workspace each, and sync
copies one account's groups, chat assignments, and group order to the others.
[Pins and groups across accounts](#pins-and-groups-across-accounts) says which
account is the source.

Claude also syncs the group list through its account settings. Copying only
the local records is not enough. On startup, the server's older list replaces
copied groups and drops their local chat assignments. When a sync copies groups
into the signed-in account, the adapter sets Claude's account-scoped
`ccd-sync-pending:ccd/dframe-store` upload marker. Claude then uploads the copy
as that account's own groups. Group names therefore also reach that signed-in
Claude account; this step does not upload local chat messages.

The marker is committed and backed up with the layout records. The adapter
requires an exact account-owner match and refuses a quarantined or differently
scoped pending update, including when only an inactive scope changes. If a
copy would change a scope with pending user edits, it leaves the payload and
snapshot unchanged until Claude sends those edits. Crash recovery can recognize
a consumed marker without rolling back a completed write, but only when the
other records match the completed transaction. It never changes identity
markers, credentials, or other account settings. Clients without enabled
account settings sync keep the local path. This is a private Claude protocol,
verified against the installed Desktop build, not a supported public API or a
guarantee against future changes.

## How chat sync decides

Claude keeps one sidebar record per chat in each account's folder. Sync compares
what each record says, never file times, because a click rewrites a record. It
ignores fields each account or click rewrites on its own: `lastFocusedAt`,
`errorAt`, the connector lists `remoteMcpServersConfig` and `enabledMcpTools`,
`transcriptUnavailable`, `promptSuggestion`, `promptAppendSnapshot`, and
`toolSurfaceSnapshot`.

- **Last agreed version.** A private state file remembers the version all
  folders last shared. A copy that still matches it is unchanged.
- **One side changed.** The changed copy wins, unless its activity is older.
- **Both changed.** The copy with the later `lastActivityAt` wins. Claude moves
  that only on real work: a message, a turn, or a permission answer.
- **Tie.** Equal activity with different content is left alone and reported.
  `claude-session-sync sync --prefer ACCOUNT/WORKSPACE` settles it, and
  `--session ID` limits that to one chat.
- **Deletes.** Claude writes a `deleted_<id>` marker. Unless the chat was used
  after the delete, the other copies are retired and the marker travels.
- **Lost chats.** A chat that vanished where it was seen, with no marker, is not
  put back. `claude-session-sync forget-lost ID` lets the next sync restore it.
- **Unreadable chats.** A record Claude would not accept freezes only that chat.

`claude-session-sync plan --report` writes every planned action and every chat
left alone to `plan-report.json` in the private state folder, with session IDs
and short folder labels only.

`claude-session-sync seed-state --from-unenrolled --dry-run` (then `--apply`)
starts a new state from folders outside sync, such as an old Mac's leftovers.
Where they all hold the same version of a chat, that version becomes the last
agreed one, so a rename or archive since then is recognised as the change.

## Account switches and new accounts

Claude holds the signed-in account's chats in memory and writes them back from
memory, so sync never writes while Claude runs. Every write first checks that
no managed Claude process runs, once before the run's journal is written and
again before the first file changes.

The watcher reads only `lastKnownAccountUuid` from Claude's `config.json` and
keeps a SHA-256 fingerprint of it. It never reads cookies, copies credentials,
or writes Claude's account config. A switch asks Claude to quit normally, runs
`switch <profile> --after-account-switch --json` with Claude closed, and
reopens Claude. Claude quitting runs `auto`. Saving a chat does not start a
sync. A failed sync still reopens Claude and reports why. A failed restart
check leaves Claude open. It never force-kills Claude.

Each sync reads every login line at the end of Claude's `main.log` and records
each account's first login on this Mac in the private chat state, so a rotated
log does not lose it. A signed-in account the log does not name counts from the
first time sync sees it. A folder outside sync joins once its account has a
recorded login and the folder holds a chat Claude saved at least 5 seconds
after that login. So a new account joins after you chat in it and then switch
accounts or quit Claude. A folder copied from another Mac has no login here and
stays out.

Every write goes through the journal. Each run keeps the files it replaced or
removed. The newest runs always stay; older ones stay for 30 days while all
kept runs fit in 500 MB.

The watcher owns a persistent menu-bar status item and a non-activating status
window. It does not rely on AppleScript or Notification Center delivery.

## Pins and groups across accounts

Claude keeps pins and custom groups in its Local Storage database, which it
locks while open, and each account's group list also lives in that account's
settings on Anthropic's servers. At sign-in Claude replaces the local list with
the server list. So pins and groups can only be carried between accounts with
Claude closed, and the new account must then upload the copy.

- **One source of truth.** One account's groups are the source, and the other
  accounts copy them. A private snapshot records which account that was.
- **The first sync.** It copies the account chosen at install. With no choice,
  it copies the signed-in account if it has groups, or else the only account
  that has groups. If no account has groups yet, nothing changes and the
  layout result says `reason=no-groups-yet`.
- **The main account at install.** When two or more accounts have groups, the
  installer lists them and asks which one is the main one. Press Enter to keep
  the account you are signed into.
- **After that.** The signed-in account wins when Claude closes normally.
- **Account switches.** The watcher restarts Claude once after every switch
  and runs `switch <profile> --after-account-switch`.
  The account just left holds the newest organization, so it is copied into the
  new account, which is then marked for an upload to its servers.
- **Deletes stick.** A deleted group is gone from every account after the next
  sync; nothing is merged back from older copies. An account with no groups is
  never copied over another, so when the main account deletes its last group,
  nothing changes and the layout result says `reason=main-account-has-no-groups`.
  The next group it gets is copied as usual.
- **A choice when unclear.** If you signed in to another account without the
  switch restart, and that account has groups, sync stops with
  `reason=choose-main-account`. The same happens on a first sync when the
  signed-in account is empty and two other accounts have groups.
- **Accounts not yet synced** are left alone.

To see the accounts and choose the main one yourself, run:

```sh
claude-session-sync keep-sidebar --dry-run
claude-session-sync keep-sidebar --account 2 --apply
claude-session-sync restart-claude
```

`--dry-run` prints one numbered row per account the next sync uses, with
whether it is signed in and its group, pin, and chat counts. Accounts have no
names on disk, so a row shows an 8-character ID prefix only when two rows would
otherwise look the same. It reads a private copy of Claude's database, so it
works while Claude is open. `--apply` makes that row the source the next time
Claude closes, and without `--account` it keeps the signed-in account.
`restart-claude` quits Claude, syncs, and reopens it now.

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

`auto` and `sync` report chats left alone in `counts` (`tied`, `lost`,
`unreadable`, `future`) and return `next_action=run-plan-report` when one needs
a choice. They never convert data corruption or an ambiguous revision into an
overwrite. An unreadable chat state file stops chat writes with
`reason=state-unusable`. Exact-target safe mode returns
`next_action=approve-targets-or-enable-automatic-targets` when a new target is
the only blocker.

Routine and sidebar errors are separate. A malformed routine manifest returns
`routines={'state': 'skipped', 'reason': 'unsafe-routines', 'detail': '...'}`.
A malformed or changed sidebar format returns
`layout={'state': 'skipped', 'reason': 'unsafe-layout', 'detail': '...'}` after
the chat result. When only you can say which account's groups to keep, it
returns `reason=choose-main-account` and the detail names `keep-sidebar`. Details name the failed safety check without exposing chat or
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
```

Rollback revalidates live data before restoring journaled preimages. If it
reports `RECOVERY_REQUIRED`, stop launching Claude and preserve the state
directory for manual inspection. Never delete a journal to silence an error.

A chat run that a killed process left open in the middle of applying, for
example by a shutdown right after you quit Claude, is closed by the next chat
sync. That sync counts it as `recovered_runs`. The closed run keeps what it
wrote, and its journal keeps every file it replaced or removed, so
`claude-session-sync rollback RUN_ID` can still undo it. A run that was rolling
back, or whose rollback failed (`RECOVERY_REQUIRED`), stays open. Chat sync
then stops with `reason=recovery-pending` until `rollback RUN_ID` finishes it.
The run ID is the folder name under `runs` in the state directory.

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

Uninstall removes only the generated runtime, watcher, sidebar helper, and
LaunchAgent by moving them into the tool's backup area. It preserves the
private config, transaction state, and the original Claude application for
recovery.

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
