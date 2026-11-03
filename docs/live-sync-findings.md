# Syncing a switch without restarting Claude

Findings of 3 November 2026, on Claude Desktop 4.4212.0 on Djahlor's Mac. Each claim is labelled: **verified** means it was seen in Claude's own code, its log, or a test that ran; **inferred** means it follows from those but was not seen directly.

## The answer

**Possible in part.**

- **No restart needed:** chats (new, changed and deleted), whether a chat is archived, sent pictures, and Code routines. Claude reads all of these from disk when an account signs in, so sync writes them while you are signed out, before the next sign-in.
- **Still needs Claude closed:** groups (sections) and the built-in browser's site logins. Claude keeps them in stores it locks while it runs, and at a sign-in it loads each account's groups from Anthropic's servers.
- **Falls back to the restart** when the sync cannot finish before you sign in again, or when one of the parts above still needs Claude closed. The fallback is today's behaviour.

## How Claude loads each part

| # | Claim | Evidence | Label |
|---|---|---|---|
| 1 | At a sign-in, Claude loads the new account's chat folder from disk, without a restart. | Claude's log, five switches from 28-10 to 03-11: `Login-state transition (loggedOut: true → false, …)` is followed 1 to 5 seconds later by `Loaded N persisted sessions from …/claude-code-sessions/<new account>/<org>`. On 01-11 it loaded 239 chats at the sign-in and 280 after the sync's restart: the 41 the sync added showed only after the restart. | verified |
| 2 | That load is the same loader a cold start uses. | Session manager (`index.chunk-C3r2jk0E.js`): `resetForAccountSwitch` sets `sessionRecordsLoaded=false` and `sessionListStore={status:"not_loaded"}`; `doInitialize` then calls `loadSessions()` → `loadSessionRecords()`, the same path as at start. | verified |
| 3 | Claude does not watch the chat folder. A file that changes on disk while its chat is loaded is ignored, then overwritten from memory at the chat's next save. | No `fs.watch`, `watchFile` or chokidar in the session manager chunk. The loader skips any chat already in memory (`if(this.sessions.has(T.sessionId)…)return`). `writeSessionToDisk` writes the whole record from memory, never merging with disk. Saves happen on focus (`lastFocusedAt`), activity, unread flags and quit. | verified; that no other chunk watches the folder is inferred |
| 4 | While signed out, Claude still holds the old account's chats and saves them into the old account's folder. It changes folders only at the next sign-in. | At sign-out the Code manager's override only resets GitHub and connector state; `currentAccountId` and `currentOrgId` stay, so `getStorageDir()` still names the old folder. At sign-in `doInitialize` runs `flushPendingSaves()`, `sessions.clear()`, then loads the new folder. | verified in code; which saves fire while signed out is inferred |
| 5 | While signed out, no other account's folder is in use. | Chats of other accounts are held only when "parked", which needs Claude's multi-account sign-in (feature gate `96101707`) and more than one account signed in. Every switch in the log since 26-10 went through a sign-out, so it is not active on this Mac. | verified |
| 6 | Claude marks a sign-out in `config.json`. | `windowSizeWasSignedIn` is set from the login state by an observer on the main window: `false` when signed out, `true` when signed in (`index.chunk-m4oMImJ-.js`, functions near `zur`/`Uur`). It sits in the same file as `lastKnownAccountUuid`, which keeps naming the account just left until the next sign-in. | verified in code; not yet seen at a real sign-out |
| 7 | Claude writes a chat file atomically. | `writeFileAtomic`: write `<file>.tmp` at mode 0600, then `rename`; a plain write only after EPERM/EACCES/EBUSY. | verified |
| 8 | Routines are read from disk at every account or org change, and never watched. | `loadScheduledTasksFromDisk` is called only from `initialize`, which `reset()` re-arms on an account change. A write that a reset superseded is dropped (`persistToDisk aborted — superseded by reset()`). Writes come from 33 places, including run bookkeeping with no user action. | verified; the old account's list can still be written while signed out (inferred) |
| 9 | `archived-sessions.idx` is only a hint. | Read only in `loadSessionRecords`; a chat file's own `isArchived` wins and the hint is rewritten after the load. | verified |
| 10 | Groups need a closed Claude. | The Code sidebar is the claude.ai web view. Its sections live in localStorage `dframe-store` (Claude's LevelDB, locked while it runs) and per account on Anthropic's servers. At an in-app account change the web app clears `ccd-sync-pending:*` and merges the new account's server copy into its slot, so a local copy cannot win without a restart (public JS, `shared-m-8-…js`). | verified |
| 11 | Browser logins need a closed Claude. | Claude clears the built-in browser on every account change (0.6.0 notes) and its cookie database is open while it runs. | verified in 0.6.0; not re-checked on 4.4212.0 |
| 12 | The gap between sign-out and sign-in is short. | Log: 14, 18, 37, 20 and 17 seconds for the five switches since 28-10. | verified |

## What goes wrong when sync writes while Claude runs

- **Into the signed-in account.** Claude keeps showing what it loaded, then overwrites the file from memory at the chat's next save (item 3). This is the old live sync's problem from 26-09.
- **Into the account just left, while signed out.** Same problem: Claude still saves those chats there until the next sign-in (item 4).
- **During the sign-in itself.** If a write lands after Claude listed the new folder, Claude keeps the old copy in memory and can save it back, for example when the chat gets focus. That old copy then looks like the newest change: it can undo an archive or a rename made in the account just left. So sync checks before every file it writes, and ends the run at the first sign that Claude signed in. Claude lists the new folder 1 to 5 seconds after the sign-in, and the check reads the same file Claude writes at the sign-in, so every write lands before that listing.
- **Routines of the signed-in account.** Claude rewrites the whole list from memory at its next bookkeeping write (item 8).
- **Groups and browser logins.** Claude holds both databases open (items 10 and 11).

## What was built

- **A sync at sign-out.** The menu-bar helper reads `windowSizeWasSignedIn` every second. When it goes from `true` to `false` while Claude runs, it runs `signed-out-sync`, which matches Djahlor's words of 26-09-2026: "only when when i sign out or close the app maybe".
- **Every folder but the account just left.** Chats, archive state and routines are written into every other account's folders. The account just left is read as the source and never written: its copies are held back for the next quit. Routines wait whole if the account just left would change, because a routine sync writes every manifest together.
- **A check before every write.** The engine checks right before journaling and right before its first write that no other profile's Claude runs and that Claude is still signed out. Before every later file it checks again that `windowSizeWasSignedIn` is still `false` and `lastKnownAccountUuid` still names the account just left. At the first sign of a sign-in it ends the run and keeps what it wrote, like a run cut short; the switch restart writes the rest.
- **Restart only when needed.** At the sign-in, the helper skips the restart when the signed-out sync finished before the sign-in and nothing else needs Claude closed. The reasons that keep it: `signed-in-during-sync`, `chats`, `routines`, `archive`, `groups` (sidebar sync on) and `browser-logins` (browser login sync on).
- **Faster checks.** The archive pass now reuses the planner's file list instead of listing all 46,000 chat files again. A no-change pass went from 15 to 21 seconds to 9 to 14 seconds on the clone.
- **One switch.** `sync_while_signed_out: false` in the config turns the sign-out sync off.
- **Other profiles.** The sync skips when another profile's Claude is open, because that Claude holds its own folders.

## How it was tested

- **Unit and command tests:** `tests/test_signed_out_sync.py` (21 tests, real folders, planner and engine) and two routine tests in `tests/test_routines.py`.
- **The real helper:** two new scenarios in `tests/test_watcher.py` run the compiled helper beside a stand-in app. A sign-out runs `signed-out-sync` and never quits the app; at the sign-in, a clean result keeps the app open, and a result that needs groups restarts it once.
- **Full suite:** 554 tests pass with ruff, ruff format and mypy.
- **Rehearsal on a copy of his data.** His 24 chat folders (12 GB) and the sync state were cloned with APFS clones into a scratch folder; his live Claude folders and live sync state were only read. After one closed sync to settle the clone, a working session was faked in the signed-in account (10 chats with new activity and titles, 1 new chat, 1 archived chat). Then:
  - The signed-out sync wrote 228 copies (79 MB) in 13.5 seconds, while a fake Claude was open and signed out.
  - All 19 folders of the other six accounts got the new chat, the 10 new titles and the archive.
  - The account just left stayed byte for byte the same: 9,690 files hashed before and after.
  - 48 copies were held back, all for that account's other four folders.
  - The next closed sync wrote those 48 (44 copies and 4 archives), and the sync after it found nothing to do.

- **Independent review.** A second reviewer read the whole change and found two medium issues and five small ones. All are fixed: the sync skips when another profile's Claude is open; it checks for a sign-in before every file and ends the run there instead of writing on; routines wait while an unfinished routine run needs recovery; a quit during the sync drops its result; an earlier `needs-attention` stays; a sign-in race reports `waiting` instead of a fault; and an option to let browser logins wait for the next quit was dropped, because the 15-minute snapshot could then replace the saved logins.

## Not verified

- **A real switch with Claude open.** It needs a sign-out and sign-in on his Mac with this build installed. Signing out ends every running Code chat, so it was not done from a chat.
- **`windowSizeWasSignedIn` at a real sign-out.** Seen in code only.
- **How often the sync beats the sign-in.** A no-change pass takes 9 to 14 seconds on his busy Mac, and his last five sign-ins came 14 to 37 seconds after the sign-out. A quick sign-in falls back to today's restart.

## Upstream check

- **Remotes:** `origin` (`djahlor/claude-session-sync`) is the only remote; fetched on 03-11. PR #22 (warnings option) is open and not merged, so this work does not build on it.
- **Anthropic:** nothing built in. claude-code issue #90188 (sessions across accounts on one machine, open since 27-08) is the tracking issue; #97295 was closed on 22-10 as its duplicate. #74662 (open) and #102826 (open) report the same problem. No pull requests on it.
- **Others:** a comment of 12-10 on #94507 says a record copied into another org's folder shows up when you switch org in the app, with no restart. A comment of 21-10 on #74662 reports a watcher that keeps all folders in step with no restart needed.
- **Log text:** "Login-state transition" is quoted in #98883 and #96049 with no discussion; "persisted sessions from" has no hits.

## Where the evidence came from

- Claude Desktop 4.4212.0's `app.asar`, unpacked into a scratch folder: `index.chunk-m4oMImJ-.js` (account state, routines, config keys), `index.chunk-C3r2jk0E.js` (Code session manager), `index.chunk-BReS2dO4.js` (shared session base), `index.chunk-DIg4rEdf.js` (Claude's own history import, which copies transcripts into new chats and is not account-to-account sync).
- The claude.ai web app's public JS for the sidebar (`index-Ct1kDFz4.js` and 32 imports).
- `~/Library/Logs/Claude/main*.log`, read only.
- The sync's own journals of past group copies, read only.
