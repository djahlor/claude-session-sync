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

The installer adds a background helper. It does not replace the Claude app.
New accounts you sign into on this Mac join the sync automatically.

## Switch accounts

1. Sign out and sign in to the other account in Claude.
2. Quit Claude.
3. Wait for **Sync finished**.
4. Open Claude normally.

Sync starts after Claude fully closes, not when you sign out. Leave it closed
until sync finishes. Larger histories take longer, so the notification is your
signal to reopen.

## If something goes wrong

If no notification appears, open Terminal and run:

```sh
"$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync" status
```

Status tells you whether sync is waiting, running, finished, or needs attention.
macOS notification settings can hide the banner.

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
