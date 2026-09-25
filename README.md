# Claude Session Sync

Keep your Claude Code chats when you switch accounts.

Sync chats, routines, pins, and custom groups between accounts in Claude Desktop
on one Mac. Keep using the regular Claude app.

> [!WARNING]
> Experimental and unofficial. Claude updates can break sync.
> Only use this with accounts whose histories you are allowed to combine.

## What it syncs

- Claude Code chat history.
- Pins, custom groups, and chat assignments where they do not conflict.
- Claude Code routine definitions, but not saved permissions or past runs.

Sync runs locally. It does not move data between Macs, transfer Claude web chats,
copy logins, or sync Cowork routines.

## Install

You need a Mac with Claude Desktop, Python 3.9 or newer, and Xcode Command Line
Tools, Apple's build tools.

1. Quit Claude.
2. [Download the ZIP](https://github.com/popcorn-so/claude-session-sync/archive/refs/heads/main.zip).
3. Unzip it.
4. Double-click `Install Claude Session Sync.command` inside the folder.
5. Choose **1** for automatic sync.

The installer adds a background helper and a small **Sync** menu-bar item.
It does not replace the Claude app.
A new account joins the sync once you start a chat in it.

## Switch accounts

1. Sign out of Claude.
2. Sign in to the other account.
3. Keep working. Chats sync in the background while Claude is open, so the
   other account's sidebar is already up to date.

With pin and group sync on, Claude restarts once right after the switch. Claude
keeps pins and groups in its own locked database and reloads each account's
groups from its servers at sign-in, so they can only be carried over with
Claude closed. The restart copies the organization from the account you just
left.

Without it, if chats arrive after Claude loaded the new account, the **Sync**
menu says how many and offers **Restart Claude now**.

A small status window shows what is happening, even if macOS hides notifications.
The **Sync** menu keeps the latest status.

## How it picks the right copy

- The copy that changed since the last sync wins. A click never counts.
- If both changed, the one with the latest real activity wins.
- If that is a tie, the chat is left alone and the status says so.
- Deleted chats stay deleted.
- Only folders of accounts you really use take part. Leftover folders from an
  old Mac are ignored.

Pins and groups follow the account you use: the one you just left is the
source when you switch, and the one you are signed into wins otherwise.
Routines sync when Claude quits.

## Update

Quit Claude, download a fresh ZIP, and run the installer again. Choose **1**
for automatic sync. No separate Claude app is needed.

## If something goes wrong

If no notification appears, open Terminal and run:

```sh
"$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync" status
```

Status tells you whether sync is waiting, running, finished, or needs attention.
The menu-bar item and status window do not depend on macOS notification banners.

If it needs attention, run:

```sh
"$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync" doctor
```

The tool keeps recovery backups. It stops affected changes when it finds damaged
data, conflicting edits, or an unknown format. Never delete backups to clear an error.

## Details

[Commands and recovery](docs/technical-reference.md) ·
[Tested scope and limits](docs/public-readiness-audit.md) ·
[MIT license](LICENSE)

This project is not affiliated with or supported by Anthropic.
