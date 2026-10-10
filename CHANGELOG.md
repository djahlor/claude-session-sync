# Changelog

What each version changed. To get the newest one, run the install command in
the [README](README.md#install) again. Check the installed version with
`claude-session-sync --version`.

## 0.5.2 (2026-10-10)

- Fixed: pins and groups were skipped for every account when one chat was
  filed under a group that no longer exists. That chat now counts as
  ungrouped, as Claude shows it.
- Fixed: a new account with no chat never joined the sync. Its empty folder
  now joins the next time Claude closes (#12).
- Fixed: a routine reached the other accounts without its permission mode and
  ran in manual there. The mode and saved approvals now sync with it (#11).
- Added: `scripts/closed-sync-test.command`, a double-click test of one sync
  with Claude closed. It keeps a log.
- Docs: the limits found in a live test on Claude Desktop 2.31226.1, and
  install steps for AI agents.

## 0.5.1 (2026-10-03)

- Sync runs only while Claude is closed. New accounts and stuck restarts
  fixed (#5).
- A safe main-account choice for pins and groups, at install and with
  `keep-sidebar` (#6, #7, #8, #10).
- Install and update with one Terminal command (#9).
- A run is reported as finished only when pins, groups, and routines finished
  too.

## 0.5.0 (2026-09-30)

- Chats, pins, and groups stay in step across account switches (#4).

## 0.4.1 (2026-09-07)

- Account sync and installation can recover from an interrupted run.

## 0.4.0 (2026-09-04)

- Claude Code routines sync across accounts.

## 0.3.1 (2026-09-02)

- Sidebar records are reconciled safely.

## 0.3.0 (2026-09-01)

- Pins and custom groups sync across accounts.

## 0.2.0 (2026-09-01)

- First numbered release: Claude Code chat history syncs across accounts.
