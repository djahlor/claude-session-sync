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

Synchronization can move conversation-session metadata across Claude accounts
and workspaces. That can expose work history to a personal account, or
personal history to a work account. Review your employer's policy
before enabling it. Routine definitions include local paths and permission
settings. Execution history and unknown fields stay local to each destination.

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
on this Mac and that holds a chat Claude saved after that login, or that Claude
created after that login and that holds no older chat. Leftover
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
September 2026. The last live sync with Claude closed used Claude Desktop
2.31226.1, on 10 October 2026.

### Tested live

On 10 October 2026 one sync ran on real Claude folders while Claude Desktop
2.31226.1 was closed. Run `820d696c` copied 6834 files, 1.7 GB, into six
account folders that were empty: 1134 chats and 5 delete markers each. One of
the six belonged to a new account, which went from 0 to 1134 chats. Routines
synced in the same run. Pins and groups did not. That run, and the next one
nine minutes later, skipped them with `reason=unsafe-layout`. The run used the
code of [pull request 12](https://github.com/djahlor/claude-session-sync/pull/12),
not an installed release. Its log sat in a temporary folder and is gone, so
these numbers come from the run record in the state folder.

The pins and groups fix in 0.5.2 was then checked on a private copy of the same
sidebar database, with no write to Claude. 0.5.1 stops there with the error
below. 0.5.2 plans the copy: 16 groups, 217 chats in groups, and 25 pins, to
all 13 synced account folders. A live run of 0.5.2 with Claude closed has not
happened yet.

### Limits found in that test

- **An empty new-account folder never joined.** In 0.5.1 a folder joined only
  after Claude saved a chat in it. A new account has no chat, so its folder
  stayed empty and never got the other accounts' chats. 0.5.2 also takes a
  folder Claude created after the account's first login here, as long as it
  holds no older chat
  ([pull request 12](https://github.com/djahlor/claude-session-sync/pull/12)).
  One limit stays: at the very first sign-in the folder does not exist yet, so
  the restart after that switch cannot fill it. Open the Code tab once, then
  quit Claude.
- **One stale group pointer stopped pins and groups for every account.**
  Claude can save a chat under a group ID its account no longer has, and it
  shows that chat as ungrouped. 0.5.1 read this as an unknown format in any
  account's records and skipped pins and groups everywhere, with the detail
  `custom group assignment references an unknown group`. 0.5.2 reads the chat
  as ungrouped, the way Claude does. It never copies the stale pointer to
  another account, and it leaves the account that holds it as Claude saved it.
  Run the installer again to get 0.5.2.
- **How to check that Claude is closed.** Use `pgrep -x Claude`. It matches
  only the process named exactly `Claude`. `pgrep -f` on Claude's path also
  matches the watcher, which carries that path as an argument and keeps
  running, so it never comes back empty. One more catch: `pgrep` leaves out
  its own parent processes. Run from inside a Claude chat, `pgrep -x Claude`
  finds nothing while Claude is open. `pgrep -a -x Claude` is right in both
  places.
- **`status` says `finished` only while Claude is closed.** With Claude open
  and nothing failed, it says `waiting-for-Claude`, because nothing syncs
  while Claude runs. Read `status` before reopening Claude to prove a sync.
- **The watcher syncs first.** When Claude quits, the watcher starts its own
  sync. A manual `sync` in the same seconds can answer `reason=busy` or
  `reason=claude-open`. Run it again after a moment.
- **Quitting Claude ends every open chat**, including chats that are still
  working. Quit when no chat is working.

### Run the test yourself

Double-click `scripts/closed-sync-test.command` in Finder. It asks you to type
`yes`, quits Claude, waits until `pgrep -a -x Claude` finds nothing, runs
`sync`, reads `status`, opens Claude again, and reads `status` once more. It
opens Claude again even when the sync fails. It tests the installed build, so
update first. The log is saved in `logs/` in the repository. Git
ignores that folder, because a log can name local paths. The script refuses to
start from inside a Claude chat, where it would stop when Claude quits.

## Install

For the easiest install, download the repository, double-click
`Install Claude Session Sync.command`, and choose option 1. It uses the normal
Claude app, trusts future logins inside that app's existing data root, and
syncs chats, Code routines, pins, and groups when the account changes or Claude
quits. It uses the macOS system Python and does not install Python packages
globally.

If your accounts already have different custom groups, the installer then asks
which account is the main one. The other accounts copy its pins and groups the
next time Claude closes. `setup --ask-main-account` asks only in a terminal and
only before any account was adopted. It asks before the watcher starts, so no
sync can run first.

The same setup can run from Terminal:

```sh
./install.sh --automatic-targets --sync-layout --sync-routines
```

An AI agent can run either command for you. Setup asks its one question, the
main account, only when a person sits at a terminal. Without one it keeps the
account already chosen. Setup replaces the helpers and restarts the watcher. It
never quits Claude. The restarted watcher quits Claude only when the signed-in
account differs from the one it last saw, which is the normal switch restart.
The same command installs and updates, so when something needs attention,
update first.

From this directory:

```sh
python3 -m pip install .
claude-session-sync setup --automatic-targets --sync-layout --sync-routines --dry-run
claude-session-sync setup --automatic-targets --sync-layout --sync-routines --apply
```

The per-user installer creates:

- `~/.config/claude-session-sync/config.json` with mode `0600`;
- no replacement Claude app;
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

To stop routine account or workspace ID changes from blocking sync, enable
automatic targets:

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

A new account joins the next time Claude closes after you sign in and open the
Code tab once. `claude-session-sync restart-claude` asks the helper to quit Claude,
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
Its `Claude-Personal` data folder stays on disk and no longer syncs.
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

While `switch` syncs it keeps a `switch-guard` file in the state directory and
removes it just before it launches Claude. A Claude opened during that time,
for example by a sign-in page handing back to Claude, stops the chat sync
before any write. The watcher sees the guard during a switch restart and asks
that Claude to quit again, retrying every 1.5 seconds for 30 seconds while it
starts up. `switch` then waits up to 45 seconds for it to close and syncs
again, at most twice. A Claude that stays open ends the switch as before, and
the next quit syncs.
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

After the sync, `switch` runs Claude's launch command, even when the sync
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

Newer Claude builds keep pins, groups, and the sidebar view per account on
Anthropic's servers, in the `ccd/dframe-store` entry of
`/api/claude_code/organizations/{org}/user_settings`. They mark each account's
section list as seeded from account settings (`prefsSeeded` under
`codeSidebarByScope`). Local Storage keeps one slot per `ACCOUNT/WORKSPACE` in
`codeSidebarByScope` and `customGroupsByScope`, and signing out keeps them. At
startup Claude loads the account's server copy, except when
`ccd-sync-pending:ccd/dframe-store` equals the signed-in `ACCOUNT/WORKSPACE`
and `ccd-sync-owner` names that account: then the local slot wins and Claude
uploads it. A `|migrate` suffix merges the server copy in first, so sync never
writes it. Which local chat sits in which section is never uploaded.

When the layout adapter finds a seeded section list, it works this way and
reports `state: per-account`:

- `sidebar-main-0.json` in the state folder names the main account: the one
  signed in at the last closed sync.
- After a switch, it copies the main account's slot into the signed-in
  account's slot, rebuilds that account's `customGroupsByScope` entry from the
  sections, sets the pending-upload flag for that account, and makes it the
  main one. The reason is `claude-uploads-copy-on-next-start`. The slot holds
  each section's view settings too (`groupBy`, `sortBy`, `ascending`, `show`,
  `collapsed`, `shown`), so Group by, Sort by and Show travel with the groups.
- A switch runs this copy first, right after Claude quits, and syncs chats
  after it. A long chat sync could otherwise end with Claude open again and
  the copy skipped for that switch. When Claude is already back or another
  writer holds the lock, the copy runs after the chat sync as before.
- `keep-sidebar --account N --apply` copies that account's slot into the
  signed-in one the same way at the next closed sync.
- It writes nothing when the owner is another account
  (`signed-in-account-not-confirmed`), Claude's settings sync is quarantined
  (`claude-settings-sync-quarantined`), another account's upload waits
  (`another-upload-pending`), the signed-in slot is not loaded yet
  (`signed-in-sidebar-not-loaded`), the main slot has no sections
  (`main-account-has-no-groups`), or the account changed without the switch
  restart (`account-changed-without-switch-restart`). A signed-in account with
  its own upload waiting (`signed-in-edit-pending`) becomes the main one.
- Writes use their own journal, `layout-account-runs`. Older `layout-runs`
  journals stay on disk unchanged and no longer count as pending recovery.

Verified on Desktop 2.42304.0 on 19 October 2026: after a copy, Claude cleared
the flag within 10 seconds of opening and showed the copied sections. Seeded
lists go back to at least 7 October 2026. The
[sidebar-groups skill](../skills/sidebar-groups/SKILL.md) is a manual
alternative that rebuilds groups through the Code tab's sidebar tools.

To copy groups by hand with the skill:

1. Install it once:

   ```sh
   mkdir -p ~/.claude/skills/sidebar-groups && curl -fsSL https://raw.githubusercontent.com/djahlor/claude-session-sync/main/skills/sidebar-groups/SKILL.md -o ~/.claude/skills/sidebar-groups/SKILL.md
   ```

2. In the account whose groups you want to keep, open a chat in the Code tab
   and ask Claude to save your sidebar groups.
3. Sign in to another account, open a chat in the Code tab, and ask Claude to
   sync your groups. It makes the same groups and files the same chats there.
   Groups that are not in the saved list are removed, and their chats move to
   the plain list. No chat is archived or deleted.
4. Repeat step 3 once in each account. When you change your groups later, save
   them again from that account first.

The skill copies groups only, not pins.

The rest of this section describes builds without seeded lists.

Claude stores chat history and sidebar layout separately. The layout adapter
reads and writes only the group-scope record, pin record, and matching dframe
store record. It checks their shapes and stops on anything it does not know.
Pins are one list shared by every account, keyed by chat ID, so they stay as
they are. Custom groups belong to one account and workspace each, and sync
copies one account's groups, chat assignments, and group order to the others.
[Pins and groups across accounts](#pins-and-groups-across-accounts) says which
account is the source. A chat filed under a group that no longer exists counts
as ungrouped, as Claude shows it. That stale pointer is never copied, and an
account with no groups counts as empty whatever pointers it still holds.

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
after that login. It also joins when Claude created the folder at least 5
seconds after that login and it holds no chat older than the login, which is
how a new account's empty folder looks. So a new account joins the next time
Claude closes after you sign in and open the Code tab once. A folder copied
from another Mac has no login here and stays out.

Every write goes through the journal. Each run keeps the files it replaced or
removed. The newest runs always stay; older ones stay for 30 days while all
kept runs fit in 500 MB.

The watcher owns a persistent menu-bar status item and a non-activating status
window. It does not rely on AppleScript or Notification Center delivery.

## Pins and groups across accounts

This section describes builds without seeded section lists. On newer builds,
see [Pins and custom groups](#pins-and-custom-groups).

Claude keeps pins and custom groups in its Local Storage database, which it
locks while open, and each account's group list also lives in that account's
settings on Anthropic's servers. At sign-in Claude replaces the local list with
the server list. So pins and groups can only be carried between accounts with
Claude closed, and the new account must then upload the copy.

- **One source of truth.** One account's groups are the source, and the other
  accounts copy them. A private snapshot records which account that was.
- **The first sync.** It copies the account chosen at install or with
  `keep-sidebar`. Without a choice, it copies an account only when that
  overwrites no different groups. That is when one account has groups, or when
  every account with groups has the same ones, as installs from older versions
  do.
  Otherwise it stops with `reason=choose-main-account` until you choose. If no
  account has groups yet, nothing changes and the layout result says
  `reason=no-groups-yet`.
- **The main account at install.** When accounts have different groups, the
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
  `reason=choose-main-account`. The same happens on a first sync when accounts
  hold different groups and none was chosen.
- **Accounts not yet synced** are left alone.

To see the accounts and choose the main one yourself, run:

```sh
claude-session-sync keep-sidebar --dry-run
claude-session-sync keep-sidebar --account 2 --apply
claude-session-sync restart-claude
```

`--dry-run` prints one numbered row per account the next sync uses, with
whether it is signed in, its group, pin, and chat counts, and up to three of its
group names. Accounts have no
names on disk, so a row shows an 8-character ID prefix only when two rows would
otherwise look the same. It reads a private copy of Claude's database, so it
works while Claude is open. `--apply` makes that row the source the next time
Claude closes, and without `--account` it keeps the signed-in account.
`restart-claude` quits Claude, syncs, and reopens it now.

## Pictures sent in a chat

A picture sent with SendUserFile is stored twice: as a file on your Mac and as
an upload to the account that sent it. The chat transcript in
`~/.claude/projects/<folder>/<session>.jsonl` (or under `CLAUDE_CONFIG_DIR`)
records both, as an attachment with `path` and `file_uuid`. The Code tab draws
the card from the upload, always through the organization signed in now, and
has no fallback. After an account switch that organization does not have the
upload, so the card shows a broken image. An attachment without `file_uuid`
is drawn from its `path` through the desktop app instead. Seen in Claude
Desktop 2.42304.2 on 20 October 2026.

On the restart after an account switch, sync reads the transcripts of the Code
chats it syncs (found by `cliSessionId` in each chat record) and renames
`file_uuid` to `orig_uuid` for each picture that:

- is an image (`isImage`, or a .png, .jpg, .jpeg, .gif or .webp path),
- has an absolute path to a regular file that still exists, is not a symbolic
  link and is not inside Claude's own data folder,
- still has the size the chat recorded.

The new key has the same length, so each line is rewritten in place at the
same offset. The file never changes size, and a line Claude appends at the
same moment is not lost. The original id stays in the line; renaming the key
back undoes it. A line still being written waits for the next switch. Each
transcript is read once: `chat-pictures-offsets.json` in the state folder keeps
how far it was read, and a replaced transcript is read again from the start.
The last result is in `chat-pictures-status.json`, and a sync report shows a
`pictures` field only when it changed a picture or failed.

It runs only after a switch, so it reaches the pictures sent before it, which
went to another account. Pictures sent after the switch keep their upload until
the next switch. Claude's web and phone views cannot read files on the Mac: they
show a file tile instead of a picture whose upload id was renamed, the same as
they show a picture uploaded to another account. A picture whose file was moved
or deleted keeps its upload id. Set `"fix_chat_pictures": false` in the config
to turn it off.

## Claude Code routines

Claude Code stores routines in `scheduled-tasks.json`, separate from chat files.
The task instructions remain in Claude's shared `~/.claude/scheduled-tasks`
directory, so account switching needs only the small manifest copied.

The adapter merges routines by task ID across approved account and organization
targets. On first sync it combines unique tasks and keeps the newest manifest
when the same task differs. A private snapshot then tracks additions, edits, and
deletions so a deleted routine does not return from a stale account. New empty
accounts receive the current snapshot instead of deleting it.

The snapshot also remembers each deleted task ID with the time of the sync that
first saw it go. A folder that joins the sync later, such as an account or
organization last used before the delete, can still hold an old copy. That copy
is ignored and removed from the folder. A routine created again after the
delete, with a later `createdAt`, or added again in a folder that already
syncs, comes back as normal. A first-seen folder's copy also never outvotes a
delete made in a known folder during the same sync.

Each multi-file update has exact preimages, post-write verification, automatic
rollback, and crash recovery. Manifests and their comparison snapshot share one
transaction. Definitions sync, including each routine's permission mode and
saved approvals, because a routine without a mode falls back to its folder's
default, which is usually manual. Each permission field merges on its own, so a
copy that never chose a mode cannot erase another account's choice. Bypass mode
still needs bypass turned on in each account's Claude settings; elsewhere the
run drops to accept-edits. Execution history and unknown metadata remain local
to each destination. Routine errors are reported separately
and never change a completed chat result, but the overall command returns a
nonzero exit code with `progress=needs-attention`. This tool syncs Claude Code routines only;
Cowork routines use different space-specific context and are left untouched.

## Built-in browser logins

Optional, off by default (`sync_browser_logins`). Claude Desktop clears its
built-in browser on every account change: the `launch-preview-cowork-shared`
partition, its local storage, and that jar's entries in
`chrome-import-sites.json`. A same-account restart keeps them.

- `browser-logins snapshot` copies the jar into
  `<state_dir>/browser-logins/<profile>/snapshot` with SQLite's backup API, so
  it is safe while Claude runs. It skips an empty jar (`jar-empty`), an
  unchanged one (`unchanged`), and waits one run before it accepts a drop to
  less than half the saved cookies (`large-drop`). The watcher runs it every
  15 minutes and five seconds after the import record changes.
- `switch`, right after Claude quits and before the chat sync, and `auto`, at
  a quit and before the chat sync, restore the copy when the jar is empty and
  the account hash differs from the copy's. Claude can be opened again during
  a long chat sync, and a jar is only put back while Claude is closed. The live
  jar is copied to `backups/` first (last three kept).
- An empty jar seen while Claude runs under another account marks the copy
  `restore_pending`: a switch emptied it and nothing put it back yet. The next
  restore with Claude closed brings it back, also under the copy's own account.
  A new healthy snapshot drops the mark.
- Otherwise an empty jar under the same account is a clear the person made in
  Claude. The copy is then marked and never restored (`cleared-in-claude`),
  until a new healthy snapshot replaces it.
- An account change also sets `preferences.launchChromeImportPrompt` in
  `claude_desktop_config.json` back to `pending`, and Claude then offers to
  import cookies again. After a restore that value becomes `done`. Only that
  value changes: the edit replaces its one occurrence, checks that both versions
  parse to the same settings apart from it, and keeps the file's mode. Any other
  layout is left alone (`unrecognised`).
- `browser-logins status --json` reports counts, times, and whether the copy
  belongs to the signed-in account. `last_restore` keeps the time, counts and
  prompt result of the last restore after newer copies are saved. Cookie values
  and the account id are never read into output; the account is stored as a
  SHA-256 hash.
- A failure here never blocks chat sync or the relaunch.

## Ties

Copies of a chat that differ at the same `lastActivityAt` are a tie. Claude
changes some fields without touching that time: archiving, pull requests,
titles, trusted git folders, published pages. The planner reads the leading
copies of each tied chat again and lets one win when it holds everything the
others hold (`tie_break.py`): archived if any copy is, every pull request at the
same or a later state (`OPEN` < `CLOSED` < `MERGED`, dismissals kept), the title
named at a later turn or a person's rename over an automatic name, every git
folder and every published page at its latest update. Account and runtime
fields (Remote Control ids, interruption and error marks, pending reminders)
are ignored for this. Any other difference keeps the tie. `--prefer` still
comes first.

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
backups until the synchronized accounts have been verified.

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
