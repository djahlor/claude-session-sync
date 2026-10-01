# Claude Session Sync

Keep your Claude Code chats, pins, and groups when you switch accounts in the
Claude Desktop app on macOS. Runs locally, nothing leaves your Mac.

For people who use more than one Claude account in Claude Desktop on one Mac,
for example a work account and a personal one. Keep using the regular Claude app.

> [!WARNING]
> Experimental and unofficial. Claude updates can break sync.
> Only use this with accounts whose histories you are allowed to combine.

## What it syncs

- Claude Code chat history.
- Pins and custom groups, copied from one main account to the others.
- Claude Code routine definitions, but not saved permissions or past runs.

Sync runs locally. It does not move data between Macs, transfer Claude web chats,
copy logins, or sync Cowork routines.

## Install

You need macOS and [Claude Desktop](https://claude.ai/download). Paste this into
Terminal:

```sh
zsh -c "$(curl -fsSL https://raw.githubusercontent.com/djahlor/claude-session-sync/main/get.sh)"
```

It checks for Apple's Command Line Tools and asks macOS to install them if they
are missing. Then it downloads the latest version, builds its helpers, and adds a
background helper and a small **Sync** menu-bar item. It does not replace the
Claude app.

If your accounts have different custom groups, it lists them, with up to three
group names each, and asks which one is the main one. Type its number, or press
Enter to keep the account you are signed into. The other accounts copy the main
account's pins and groups the next time Claude closes. Sync never overwrites
different groups on its own. Until you choose a main account, it leaves them
alone.

A new account joins the sync after you chat in it and then switch accounts or
quit Claude.

### Without Terminal

1. [Download the ZIP](https://github.com/djahlor/claude-session-sync/archive/refs/heads/main.zip) and unzip it.
2. Double-click `Install Claude Session Sync.command`. If macOS blocks it, open
   System Settings, then Privacy & Security, and click **Open Anyway**.
3. Choose **1** for automatic sync, or **2** to approve each new account yourself.

## Switch accounts

1. Sign out of Claude.
2. Sign in to the other account.
3. Claude closes, syncs, and opens again by itself.

Chats sync when you switch accounts and when you quit Claude. Nothing syncs
while Claude is open. Claude keeps your chats in memory and keeps pins and
groups in a database it locks while it runs, so sync waits until Claude is
closed. The restart after a switch also copies pins and groups from the account
you just left.

A small status window shows what is happening, even if macOS hides notifications.
The **Sync** menu keeps the latest status.

## How it picks the right copy

- The copy that changed since the last sync wins. A click never counts.
- If both changed, the one with the latest real activity wins.
- If that is a tie, the chat is left alone and the status says so.
- Deleted chats stay deleted.
- Only folders of accounts you really use take part. Leftover folders from an
  old Mac are ignored.

Pins and groups follow the account you use. When you switch, the account you
just left is the source. Otherwise the account you are signed into wins. A
group you delete stays deleted in every account. The one exception: if you
delete the main account's last group, the other accounts keep theirs, and the
status says so. To pick another main account later, see [pins and groups across accounts](docs/technical-reference.md#pins-and-groups-across-accounts).
Routines sync at the same moments as chats.

## Update

Run the same Terminal command again. Your sync history, backups, and main
account stay as they are.

To remove it, run:

```sh
"$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync" uninstall --apply
```

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
[Tested scope and limits](docs/technical-reference.md#limits) ·
[MIT license](LICENSE)

This project is not affiliated with or supported by Anthropic.
