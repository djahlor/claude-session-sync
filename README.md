# Claude Session Sync

[![Test](https://github.com/djahlor/claude-session-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/djahlor/claude-session-sync/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Keep your Claude Code chats, groups, routines and browser logins when you switch accounts in the Claude Desktop app on a Mac.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/how-it-works-dark.svg">
  <img alt="You switch accounts, Claude closes, sync copies chats, groups, routines and logins, Claude opens again" src="docs/images/how-it-works-light.svg">
</picture>

For people with more than one Claude account on one Mac, for example a work account and a personal one. You keep using the normal Claude app. Everything runs on your Mac and nothing leaves it.

> [!WARNING]
> Experimental and unofficial. Claude updates can break parts of it. Only use it with accounts whose histories you are allowed to combine.

## What it syncs

| What | When | Standard install |
|---|---|---|
| Claude Code chats | Each switch and each quit | On |
| Whether a chat is archived | Each switch and each quit | On |
| Custom groups, which Claude now calls sections | Each switch | On |
| Claude Code routines, with their permission mode and saved approvals | Each switch and each quit | On |
| Site logins in Claude's built-in browser | Each switch | Off, [turn it on](#browser-logins) |
| Pictures sent in a chat, shown from your Mac in every account ([how](docs/technical-reference.md#pictures-sent-in-a-chat)) | Each switch | On |

It does not sync past routine runs, Claude web chats, your Claude logins, Cowork routines, or anything between Macs. Pins sync only on older Claude versions. A routine in bypass mode also needs bypass turned on in each account.

## Requirements

- macOS with [Claude Desktop](https://claude.ai/download).
- Apple's Command Line Tools. The installer asks macOS to install them if they are missing.

## Install

Paste this into Terminal:

```sh
zsh -c "$(curl -fsSL https://raw.githubusercontent.com/djahlor/claude-session-sync/main/get.sh)"
```

It downloads the latest version, builds its helpers, and adds a background helper with a small **Sync** item in the menu bar. It never replaces or quits the Claude app. The same command updates it.

### Without Terminal

1. [Download the ZIP](https://github.com/djahlor/claude-session-sync/archive/refs/heads/main.zip) and unzip it.
2. Double-click `Install Claude Session Sync.command`. If macOS blocks it, open System Settings, then Privacy & Security, and click **Open Anyway**.
3. Choose **1** for automatic sync, or **2** to approve each new account yourself.

### With an AI agent

Ask your agent: "Install or update Claude Session Sync from github.com/djahlor/claude-session-sync, then check it." The install command needs no keyboard. Without a terminal it skips the main-account question and keeps the account already chosen. The agent can check the result with `--version` and `status` from [Commands](#commands). A fix shows in `status` only after Claude has closed once, and quitting Claude ends every open chat, so the agent should ask you before it quits Claude.

## Switch accounts

1. Sign out of Claude.
2. Sign in to the other account.
3. Claude closes, syncs, and opens again by itself.

Nothing syncs while Claude is open, because Claude keeps chats in memory and locks its database while it runs. Chats and routines also sync when you quit Claude. A small status window and the **Sync** menu show what is happening, even if macOS hides notifications.

A new account joins the next time Claude closes after you sign in and open the Code tab once. You do not have to start a chat first.

## How it picks the right copy

- The copy that changed since the last sync wins. A click never counts.
- If both changed, the one with the latest real activity wins.
- If that is a tie, the chat is left alone and the status says so.
- Archiving or unarchiving a chat reaches every account, even in a tie. The newest one wins, and using a chat counts as opening it ([how](docs/technical-reference.md#archived-chats)).
- Deleted chats and routines stay deleted.
- Only folders of accounts you really use take part. Leftover folders from an old Mac are ignored.

## Groups

Newer Claude versions keep groups per account on Anthropic's servers. When you switch, the restart copies the groups of the account you just left into the account you signed in to, and Claude saves them to that account itself. Sync never reads or sends your login. If something is not as sync expects, it changes nothing and `status` says why. Pins are not copied on these versions.

Older Claude versions keep pins and groups on your Mac. One main account is the source. When your accounts have different groups, the installer asks which one is the main account, and until you choose, sync leaves the groups alone.

You can also copy groups by hand with the [sidebar-groups skill](skills/sidebar-groups/SKILL.md). The [technical reference](docs/technical-reference.md#pins-and-custom-groups) has the steps and the rules for each version.

## Browser logins

Claude clears its built-in browser every time the signed-in Claude account changes: the sites you signed in to, the cookies you imported, and their saved data. A restart with the same account keeps them, if **Keep cookies** is on.

With browser logins on, the background helper saves a copy of the built-in browser every 15 minutes and a few seconds after you import cookies. When you switch, the restart puts that copy back before Claude opens. Turn it on with `configure --sync-browser-logins --apply` from [Commands](#commands).

- It is off by default. Turned on, every Claude account on this Mac shares the same site logins in the built-in browser, which is what Claude's clearing prevents. Turn it on only when every account is yours.
- If you clear the built-in browser yourself without switching, sync keeps it cleared.
- The copy stays in sync's private folder, still encrypted with Claude's own key. Sync reads counts and times, never a cookie.

## Commands

The installed tool lives in your Library folder. Set it once per Terminal window:

```sh
css="$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync"
```

| Command | What it does |
|---|---|
| `"$css" status` | Says whether sync is waiting, running, finished, or needs attention |
| `"$css" doctor` | Checks the setup and names the part that failed |
| `"$css" --version` | Shows the installed version |
| `"$css" configure --sync-layout --apply` | Turns on groups |
| `"$css" configure --sync-browser-logins --apply` | Turns on browser logins |
| `"$css" browser-logins status --json` | Shows the saved browser copy, counts only |
| `"$css" uninstall --apply` | Removes the background helper and menu-bar item |

All commands and recovery steps are in the [technical reference](docs/technical-reference.md).

## Update and uninstall

Run the install command again. Your sync history, backups and main account stay as they are, and Claude stays open. Pulling the repository alone does not update the installed helper. [CHANGELOG.md](CHANGELOG.md) lists what each version changed.

To remove it, run `"$css" uninstall --apply`.

## If something goes wrong

1. Update first, with the install command. Most problems are fixed in a newer version, and an update keeps your history and backups.
2. Quit Claude once, at a moment when no chat is working, so sync runs again.
3. Run `"$css" status`. If it needs attention, run `"$css" doctor`.

On older Claude versions, if groups synced but do not show, open the sidebar **Filter** menu and choose **Group by**, then **Custom groups**. Each account has its own setting, and **None** hides groups even when they synced.

Sync keeps recovery backups and stops a change when it finds damaged data, conflicting edits, or a format it does not know. Never delete backups to clear an error.

## Limits

- Sync writes only while Claude is closed, and quitting Claude ends every open chat.
- Sync reads Claude's private files, which an update can change. Sync then stops that part, leaves the rest running, and says so.
- The last full live test used Claude Desktop 2.31226.1 on 10 October 2026. Groups were checked on 2.42304.0 on 19 October 2026. Browser login saving was checked on 2.42304.2 on 20 October 2026, and its restore is tested on copies.
- To check that Claude is closed, use `pgrep -x Claude`. `pgrep -f` also matches the background helper.

The [technical reference](docs/technical-reference.md#limits) has the full list and a double-click script that repeats the live test.

## More

- [Technical reference](docs/technical-reference.md)
- [Changelog](CHANGELOG.md)
- [Security](SECURITY.md)
- [MIT license](LICENSE)

This project is not affiliated with or supported by Anthropic.
