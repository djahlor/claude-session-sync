# Claude Session Sync

[![Test](https://github.com/djahlor/claude-session-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/djahlor/claude-session-sync/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Keep your Claude Code chats, groups, routines and browser logins when you switch accounts in the Claude Desktop app on a Mac, and stop the artifact warnings that switching leaves behind.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/how-it-works-dark.svg">
  <img alt="You switch accounts, Claude closes, sync copies chats, groups, routines and logins, Claude opens again" src="docs/images/how-it-works-light.svg">
</picture>

For people with more than one Claude account on one Mac, for example a work account and a personal one. You keep using the normal Claude app. Everything runs on your Mac and nothing leaves it.

> [!WARNING]
> Experimental and unofficial. Claude updates can break parts of it. Only use it with accounts whose histories you are allowed to combine.

## Install

Open Claude Code on your Mac, for example the **Code** tab in the Claude app, and paste this:

```text
Install Claude Session Sync from github.com/djahlor/claude-session-sync. Follow its INSTALL.md, and tell me what it does before you run anything.
```

Your own Claude does the rest, and you see each step:

1. It downloads the code to a temporary folder and reads it first. It checks that nothing in it goes online.
2. It tells you what it will add and where.
3. It asks two questions: whether to [stop the artifact warnings](#artifact-warnings), which has a cost, and whether to add the [share-artifacts skill](#artifact-pages-on-your-other-accounts).
4. After your yes, it installs, checks that it runs, and tells you how to switch accounts.

Claude never quits or changes the Claude app, so your chat stays open. [INSTALL.md](INSTALL.md) is the guide Claude follows, with every command in it. To update, paste the same sentence again.

### What it puts on your Mac

| What | Where |
|---|---|
| The tool, its backups and its history | `~/Library/Application Support/ClaudeSessionSync` |
| Its settings | `~/.config/claude-session-sync` |
| A login item that starts the background helper and the **Sync** menu-bar item | `~/Library/LaunchAgents/com.claude-session-sync.watcher.plist` |
| The share-artifacts skill, if you say yes | `~/.claude/skills/share-artifacts` |

It has no account, no server and no tracking. Nothing leaves your Mac. It needs macOS, the [Claude desktop app](https://claude.ai/download) and Apple's Command Line Tools, which macOS offers to install when they are missing.

To remove it, paste this into Claude Code:

```text
Remove Claude Session Sync from my Mac. Follow INSTALL.md at github.com/djahlor/claude-session-sync.
```

### In Terminal

```sh
zsh -c "$(curl -fsSL https://raw.githubusercontent.com/djahlor/claude-session-sync/main/get.sh)"
```

It does the same install and asks the same two questions in Terminal. Press Enter to keep the warnings and to add the skill. When your accounts have different groups on an older Claude version, it also asks which account is the main one. An update keeps your answers and does not ask again. To answer up front, put the options after a dash:

```sh
zsh -c "$(curl -fsSL https://raw.githubusercontent.com/djahlor/claude-session-sync/main/get.sh)" - --stop-artifact-warnings --share-skill
```

`--no-stop-artifact-warnings` and `--no-share-skill` answer no, and are saved the same way.

### Without Terminal

1. [Download the ZIP](https://github.com/djahlor/claude-session-sync/archive/refs/heads/main.zip) and unzip it.
2. Double-click `Install Claude Session Sync.command`. If macOS blocks it, open System Settings, then Privacy & Security, and click **Open Anyway**.
3. Choose **1** for automatic sync, or **2** to approve each new account yourself.

## What it syncs

| What | When | Standard install |
|---|---|---|
| Claude Code chats | Each switch and each quit | On |
| Whether a chat is archived | Each switch and each quit | On |
| Custom groups, which Claude now calls sections | Each switch, with one restart when the accounts' groups differ | On |
| Claude Code routines, with their permission mode and saved approvals | Each switch and each quit | On |
| Site logins in Claude's built-in browser | Each switch, with one restart | Off, [turn it on](#browser-logins) |
| Pictures sent in a chat, shown from your Mac in every account ([how](docs/technical-reference.md#pictures-sent-in-a-chat)) | Each switch | On |
| Stops the red artifact "didn't resume" warnings in old chats | Each switch and each quit | Asked at install, [cost](#artifact-warnings) |
| Artifact pages on your other accounts, with a Claude skill | When you ask Claude | Asked at install, [how](#artifact-pages-on-your-other-accounts) |

It does not sync past routine runs, Claude web chats, your Claude logins, Cowork routines, or anything between Macs. Pins sync only on older Claude versions. A routine in bypass mode also needs bypass turned on in each account. A routine saved with no mode runs in manual on every account; `routine_mode_when_unset` in the config gives such routines a mode ([how](docs/technical-reference.md#claude-code-routines)).

## Switch accounts

1. Sign out of Claude.
2. Sign in to the other account.

While you sign in, sync copies chats and routines into the other accounts. Claude reads them when the new account signs in, so they need no restart. Claude closes, syncs and opens again by itself only when something still needs it closed: groups that differ from the account you left, browser logins, or a sign-in faster than the sync. After the sign-in, sync waits up to 20 seconds for Claude to load the new account's groups and compares them. When they already match, Claude stays open. A restart is shorter because the chats are already done. Chats and routines also sync when you quit Claude. When Claude closes or opens again by itself, a small status window says why, even if macOS hides notifications. It closes after a few seconds, with its close button, or with a click. To stop it, turn off **Show the status window** in the **Sync** menu. The menu always shows the latest status.

Nothing syncs while you work in Claude, because Claude keeps chats in memory and locks its database while it runs. Sync writes only while Claude is closed or signed out, and never into the account Claude still has open ([how](docs/live-sync-findings.md)).

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

## Artifact warnings

When Claude publishes an artifact page, the chat turns on automatic replies to comments on that page and saves a note about it in the chat's history. Each time the chat opens again, Claude tries to restart every saved note. After an account switch, a page owned by another account cannot be watched, so each note shows a red warning such as "Artifact comment monitor ... didn't resume". The notes never expire, so old chats show them again on every reopen.

With this option on, every sync does two things while Claude is closed:

1. It adds `"CLAUDE_CODE_ARTIFACT_COMMENTS_AUTOREACT": "0"` to the `env` block of `~/.claude/settings.json`, so new chats save no notes. Every other line of the file stays as it was. If that key is already there, sync leaves it alone.
2. It removes the saved notes from the chat histories in `~/.claude/projects/`. Each removed line is first copied to a backup in sync's private folder. A chat whose Claude Code process still runs is skipped until it closes.

The installer asks whether to turn it on. You can also turn it on later with `configure --stop-artifact-warnings --apply` from [Commands](#commands). It works from the next time Claude closes.

- It is off by default, because it has a cost: Claude stops answering comments on your artifacts by itself in the background.
- The switch is listed in Anthropic's [Claude Code environment variables](https://code.claude.com/docs/en/env-vars). It needs Claude Code 2.1.228 or later.
- To turn it off, set `"stop_artifact_warnings": false` in the config. The next sync takes the key out of `settings.json`, but only if sync put it there.
- `status` shows `artifact_warnings=on` or `off`, and `artifact_warnings_cleared` counts the notes the last sync removed.

## Artifact pages on your other accounts

Each artifact page belongs to the account that published it. After a switch, its link says the page is not found, and Claude cannot update it from the other account. Only the page's own **Share** menu fixes that, so sync does not do it for you ([why](docs/artifact-sharing-findings.md)).

The share-artifacts skill makes it a few clicks. The installer offers to add it. Then ask Claude:

> Share this page with my other accounts.

Claude asks for your accounts' emails once, finds the page, and asks which way:

- **Read only.** You set the page to anyone with the link. It opens on every account at once, with no email. Anyone who has the link can see it.
- **Edit too.** You invite your other accounts as Editor, with the emails Claude gives you to paste. Each account gets an email and accepts once. Then Claude can update the page from that account too. One account can send about 100 invites a day.

After you accept on another account, Claude checks that the page is shared with it.

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
| `"$css" configure --stop-artifact-warnings --apply` | Stops the artifact "didn't resume" warnings ([cost](#artifact-warnings)) |
| `"$css" uninstall --apply` | Removes the background helper and menu-bar item, and the artifact warning switch sync put in |

All commands and recovery steps are in the [technical reference](docs/technical-reference.md).

## Update and uninstall

Paste the install sentence into Claude Code again, or run the Terminal command again. Your sync history, backups and main account stay as they are, and Claude stays open. Pulling the repository alone does not update the installed helper. [CHANGELOG.md](CHANGELOG.md) lists what each version changed.

To remove it, paste the remove sentence from [Install](#install) into Claude Code, or run `"$css" uninstall --apply`. That also takes the [artifact warning](#artifact-warnings) switch out of `settings.json` if sync put it there. Your history and backups stay in `~/Library/Application Support/ClaudeSessionSync`, and the skill stays in `~/.claude/skills/share-artifacts`; delete those folders to remove them too.

## If something goes wrong

1. Update first, with the install sentence or the Terminal command. Most problems are fixed in a newer version, and an update keeps your history and backups.
2. Quit Claude once, at a moment when no chat is working, so sync runs again.
3. Run `"$css" status`. If it needs attention, run `"$css" doctor`.

On older Claude versions, if groups synced but do not show, open the sidebar **Filter** menu and choose **Group by**, then **Custom groups**. Each account has its own setting, and **None** hides groups even when they synced.

Sync keeps recovery backups and stops a change when it finds damaged data, conflicting edits, or a format it does not know. Never delete backups to clear an error.

## Limits

- Sync writes only while Claude is closed or signed out, and quitting Claude ends every open chat.
- Browser logins, and groups that differ between the two accounts, still need one restart after a switch. Without them, a switch restarts only when you sign in before the sync finishes. A sync with nothing to do takes about 10 seconds on 46,000 chat files.
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
