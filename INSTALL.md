# Install Claude Session Sync with Claude Code

This guide is for Claude Code. Someone pasted a request like this into a chat:

> Install Claude Session Sync from github.com/djahlor/claude-session-sync. Follow its INSTALL.md, and tell me what it does before you run anything.

Follow the steps in order. The same steps update it, and [Remove](#remove) takes it off again. People can read this file too: it lists every command Claude runs.

## Rules for Claude

- Talk to the person in plain, short sentences. Explain what each step is for before you run it.
- Install nothing until the person says yes in step 5. Steps 1 to 4 only check, download into a temporary folder and read, which changes nothing. Say that in one line before step 1, then explain the tool in full after step 3.
- Never quit, restart or reinstall the Claude app. Quitting Claude ends every open chat, this one too. Sync starts working the next time the person closes Claude themselves.
- Shell variables do not carry over between your commands. Write the folder path from step 2 out in full in each later command.
- If a sandbox blocks a step, such as writing to `~/Library` or running `launchctl`, ask the person to approve that one command outside the sandbox.
- If something fails, stop: show the person the last lines of the output, say the Claude app was not changed, delete the download folder, and point them to "If something goes wrong" in the README. Do not try other ways to install it.

## What it installs

Tell the person this in your own words before step 5:

- **What it does.** When they switch accounts in the Claude desktop app, their Claude Code chats, archived state, groups and routines come along to the other account.
- **What it adds.** A small background helper that starts at login, with a **Sync** item in the menu bar. It runs only when Claude closes or signs out, and never while they work.
- **Where.** The tool, its backups and its history go in `~/Library/Application Support/ClaudeSessionSync`. Its settings go in `~/.config/claude-session-sync`. The login item is `~/Library/LaunchAgents/com.claude-session-sync.watcher.plist`. If they want the skill, it goes in `~/.claude/skills/share-artifacts`.
- **What it never does.** It never changes or quits the Claude app in `/Applications`. It never reads their passwords, their Keychain or their Claude login. It has no account, no server and no tracking: nothing leaves their Mac. Step 3 checks that in the code.
- **What they will see.** No Mac password prompt. macOS may show a note that a background item was added: that is the Sync helper.
- **How to remove it.** Ask Claude to remove it, with the sentence in [Remove](#remove). That takes off the helper, the menu-bar item and the login item.

## 1. Check the Mac

```sh
uname -s; ls -d /Applications/Claude.app; xcode-select -p && xcrun --find swiftc
```

- `uname -s` must say `Darwin`. If not, this chat does not run on the person's Mac, for example a cloud or remote session. Say so and stop.
- If `/Applications/Claude.app` is missing, the person needs the Claude desktop app from https://claude.ai/download first. Stop.
- If `xcode-select` or `xcrun` fails, the Mac lacks Apple's Command Line Tools, which build the helper. Run `xcode-select --install`, tell the person to click **Install** in the window macOS opens, and wait until they say it finished. Then run the check again.

## 2. Download into a new folder

```sh
folder="$(mktemp -d)" && curl -fsSL https://github.com/djahlor/claude-session-sync/archive/refs/heads/main.tar.gz | tar -xz -C "$folder" --strip-components 1 && echo "$folder"
```

It prints the folder. Then read `INSTALL.md` in that folder with your Read tool, in full, and follow that copy from step 3. When it differs from a page you fetched earlier, the copy in the folder wins: it is the code you are about to run.

## 3. Read the code before you run it

1. Read `install.sh` in the folder. It is the script step 6 runs.
2. Read the version in `src/claude_session_sync/__init__.py`.
3. Check that the code makes no network calls and does not read the Keychain:

   ```sh
   grep -rnE "import requests|urlopen|urllib|http\.client|import socket|URLSession|NSURL|curl |SecItem|find-generic-password|/usr/bin/security" "<folder>/src" "<folder>/install.sh"
   ```

   No output means none were found. If anything shows, or the code does something [What it installs](#what-it-installs) does not list, tell the person and stop.

Then tell the person what it does, from [What it installs](#what-it-installs), and the version.

## 4. Is it installed already?

```sh
"$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync" --version
grep -c '"stop_artifact_warnings"' "$HOME/.config/claude-session-sync/config.json"
ls "$HOME/.claude/skills/share-artifacts/SKILL.md" "$HOME/Library/Application Support/ClaudeSessionSync/share-skill-skipped"
```

- If the first line prints a version, it is installed, and this run updates it. Tell the person the old and the new version before the questions.
- Ask question A only when the `grep` prints `0` or cannot find the file.
- Ask question B only when `ls` finds neither file.
- On a first install all three report missing files and exit with an error. That is expected, not a failure.

In step 5, ask only the questions this step left open, and always the go-ahead.

## 5. Ask the questions

Use your question tool if you have one, so the person can click an answer. Otherwise ask in chat and wait. Ask all of them at once. Keep these meanings, in your own plain words:

**A. Stop the red "Artifact comment monitor ... didn't resume" warnings?** After an account switch, old chats show them for pages another account published, every time the chat opens. The cost: Claude stops answering comments on their artifact pages by itself. They can still ask Claude to read and answer comments.
- No, keep things as they are. This is the default.
- Yes, stop the warnings.

**B. Add the share-artifacts skill to Claude Code?** A page Claude publishes belongs to one account and looks deleted on the others. With the skill, they ask Claude "share this page with my other accounts" and Claude walks them through the Share menu. It adds one folder, `~/.claude/skills/share-artifacts`.
- Yes, add it. Recommended.
- No.

**Go-ahead. Install it now?** On an update: update it now?
- Yes.
- Not now. Then delete the download folder and stop.

## 6. Install

Run this in the foreground with a 10-minute timeout. Building the helper takes a minute or two.

```sh
cd "<folder>" && ./install.sh --automatic-targets --sync-layout --sync-routines <answers>
```

Put in `<answers>`:

| Answer | Add |
|---|---|
| A: yes | `--stop-artifact-warnings` |
| A: no | `--no-stop-artifact-warnings` |
| B: yes | `--share-skill` |
| B: no | `--no-share-skill` |
| A or B not asked | nothing for that question |

The options are saved, so a later update, from Claude or from Terminal, does not ask again. The install never quits Claude, and Claude can stay open.

It worked when it ends with `Claude Session Sync is installed.`

## 7. Check it

```sh
"$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync" --version
"$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync" status
launchctl print "gui/$(id -u)/com.claude-session-sync.watcher" | head -3
```

The install is done when both of these hold:

- `--version` prints the version from step 3. If it prints an older one, the install did not take effect: stop, as the rules say.
- `launchctl print` shows the helper. An error there means the login item did not start: stop, as the rules say.

Then read `status`:

- `state=app-running` is normal, because Claude is open. Sync waits until Claude closes.
- If it names `keep-sidebar`, the accounts have different groups on an older Claude version. Run `keep-sidebar --dry-run` with the same tool path, show the person the accounts it lists, ask which one is the main one, and run `keep-sidebar --account N --apply` with their number.
- If it says `needs-attention`, run `doctor` with the same tool path. It can take a few minutes on a big chat history. On an update this can be left over from the old version. Tell the person which counts are not zero, then the fix from the README: quit Claude once, at a moment when no chat is working, so sync runs with the new version, then ask Claude to check `status` again. The install itself is still done.

Then delete the download folder with `rm -rf "<folder>"`. The installed tool does not need it.

## 8. Tell the person what to do next

In plain words, only what applies:

- It is installed, with the version. A small **Sync** item is in the menu bar.
- If `status` said `needs-attention`: what it counted, and that quitting Claude once, when no chat is working, lets sync run again.
- To switch accounts: sign out of Claude, then sign in to the other account. The Claude Code chats come along. Sometimes Claude closes and opens again by itself, and a small window says why.
- A new account joins the first time they sign in to it and open the Code tab once.
- If they said yes to A: the warnings stop from the next time Claude closes.
- If they said yes to B: to open a page from their other accounts, ask Claude "share this page with my other accounts".
- To update later, paste the same sentence again. To remove it, use the sentence in [Remove](#remove).

## Remove

The person pastes something like:

> Remove Claude Session Sync from my Mac. Follow INSTALL.md at github.com/djahlor/claude-session-sync.

1. Tell the person what goes: the background helper, the **Sync** menu-bar item, the login item and the installed tool. If sync turned off the artifact warnings, that switch comes out of `~/.claude/settings.json` too. Chats already copied to each account stay where they are.
2. Ask for a yes. Then run:

   ```sh
   "$HOME/Library/Application Support/ClaudeSessionSync/bin/claude-session-sync" uninstall --apply
   ```

3. Ask whether to also delete these. Each is kept unless the person says yes:
   - The share-artifacts skill: `~/.claude/skills/share-artifacts`.
   - Sync's history and recovery backups: `~/Library/Application Support/ClaudeSessionSync`, and its settings: `~/.config/claude-session-sync`. Once deleted, the backups cannot come back.
4. Check that `launchctl print "gui/$(id -u)/com.claude-session-sync.watcher"` now fails, and tell the person it is removed.
