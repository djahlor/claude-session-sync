# Changelog

All notable changes to Claude Session Sync, newest first. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version numbers follow [Semantic Versioning](https://semver.org/).

To update, run the install command in the [README](README.md#install) again. Check your version with `claude-session-sync --version`.

## [Unreleased]

## [0.6.1] - 2026-10-20

### Fixed

- A switch could leave the new account on its old sidebar, with every section's Group by, Sort by and Show back to that account's old values. When Claude was opened again while the switch still synced chats, the sidebar step waited and never ran for that switch. On Claude versions that keep the sidebar per account, the switch now copies the sidebar first, right after Claude quits, and syncs chats after. The copy already carried each section's view settings; it now runs before Claude can come back.

## [0.6.0] - 2026-10-20

### Added

- Browser logins, off by default. Claude clears its built-in browser on every account change. With `configure --sync-browser-logins --apply`, the background helper saves a copy every 15 minutes and after an import, and the restart after a switch puts it back before Claude opens. A browser you cleared yourself stays cleared.
- `browser-logins snapshot`, `restore` and `status` commands.

## [0.5.4] - 2026-10-19

### Added

- The sidebar-groups skill, to rebuild groups by hand through the Code tab's sidebar tools.

### Fixed

- On Claude versions that keep groups per account on Anthropic's servers, a switch showed that account's old groups. The restart after a switch now copies the groups of the account you just left into the new account and sets Claude's own pending-upload flag, so Claude saves them to that account. It changes nothing when the signed-in account, a waiting upload or Claude's settings sync does not match. Pins stay per account on these versions.
- 0.5.3 said Claude clears the local copy at every sign-in. Claude keeps every account's copy and loads the server copy unless the upload flag names the signed-in account.

## [0.5.3] - 2026-10-17

### Changed

- On Claude versions that keep pins and groups per account on Anthropic's servers, sync leaves the sidebar alone and reports `per-account`. Those versions seemed to clear the copy on disk at every sign-in, so a copied group list was dropped or never read, and a failed write could block every later sidebar sync. Old sidebar journals no longer count as pending recovery there.

### Fixed

- A deleted routine came back when an account folder last used before the delete joined the sync. Deletes are now remembered, so the old copy is ignored and removed. A routine made again later still syncs.

## [0.5.2] - 2026-10-10

### Added

- `scripts/closed-sync-test.command`, a double-click test of one sync with Claude closed. It keeps a log.
- The limits found in a live test on Claude Desktop 2.31226.1, and install steps for AI agents.

### Fixed

- Pins and groups were skipped for every account when one chat was filed under a group that no longer exists. That chat now counts as ungrouped, as Claude shows it.
- A new account with no chat never joined the sync. Its empty folder now joins the next time Claude closes (#12).
- A routine reached the other accounts without its permission mode and ran in manual there. The mode and saved approvals now sync with it (#11).

## [0.5.1] - 2026-10-03

### Added

- A safe main-account choice for pins and groups, at install and with `keep-sidebar` (#6, #7, #8, #10).
- Install and update with one Terminal command (#9).

### Changed

- Sync runs only while Claude is closed (#5).
- A run counts as finished only when pins, groups and routines finished too.

### Fixed

- New accounts and stuck restarts (#5).

## [0.5.0] - 2026-09-30

### Added

- Chats, pins and groups stay in step across account switches (#4).

## [0.4.1] - 2026-09-07

### Fixed

- Account sync and installation recover from an interrupted run.

## [0.4.0] - 2026-09-04

### Added

- Claude Code routines sync across accounts.

## [0.3.1] - 2026-09-02

### Fixed

- Sidebar records are reconciled safely.

## [0.3.0] - 2026-09-01

### Added

- Pins and custom groups sync across accounts.

## [0.2.0] - 2026-09-01

### Added

- First numbered release: Claude Code chat history syncs across accounts.
