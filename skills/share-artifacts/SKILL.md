---
name: share-artifacts
description: Make a claude.ai artifact page open from every Claude account the person uses on this Mac. A page belongs to the account that published it, so after an account switch it looks deleted or "not found" from the other accounts, and Claude Code there cannot update it. Use when someone says a page or artifact is gone, deleted or not found after switching accounts, asks to share a page with their other accounts, or wants to edit a page from another account. Claude prepares every step and the person clicks Share on the page; nothing is sent without them.
---

# Share artifact pages with your other accounts

Each artifact page belongs to the Claude account that published it. The other accounts on this Mac cannot open it until the owner shares it. Only the page's own **Share** menu can do that: Claude Code has no share or invite action, and a script that drives claude.ai for the person would break Anthropic's terms. So Claude prepares everything, the person clicks, and Claude checks the result.

## Inputs

| Input | Description |
|---|---|
| Accounts file | `~/.claude/skills/share-artifacts/accounts.txt`, one email per line: every Claude account the person uses on this Mac. Updates of this skill leave it alone. |
| Page | A claude.ai artifact link, "the page we just made", or nothing (then list the pages). |

## Steps

1. **Accounts.** Read the accounts file. If it is missing or empty, ask for the email of each Claude account the person uses on this Mac and write them one per line. Never guess an email.
2. **Pages.** Use the link the person gave. Otherwise call the Artifact tool with `action: "list"` and `scope: "mine"`, show the newest pages as title and link, and ask which ones to share.
3. **Owner.** Only the account that published a page can share it. A link that is not in this account's `mine` list belongs to another account, or was deleted. Say which account must be signed in to share it, and go on with the other pages.
4. **Ask once, for all chosen pages, which way:**
   - **Open on every account, read only.** The page opens at once for every account and for anyone who has the link. No email, no accept step. Not for pages with private details. Claude Code on another account still cannot update it.
   - **Edit from every account.** The owner invites each other account as Editor. Each invite sends an email, and each account accepts once. After that, Claude Code on that account can update the page.
5. **Give the clicks**, one short block per page, with the link:
   - Read only: open the page, click **Share** at the top, and set who can see it to anyone with the link.
   - Edit: open the page, click **Share**, paste the other accounts' emails, set the role to **Editor**, and send. Put the emails in a code block, comma separated, without this account's own email:

     ```text
     work@example.com, personal@example.com
     ```

     Then, on each other account: open the invite email from Claude and accept it, or open the page link while signed in to that account and accept there.
6. **Check.** For an invite, after the person switches to another account and accepts, call the Artifact tool with `action: "list"` and `scope: "shared"`. An accepted page is in that list. Report each page as shared, waiting for an accept, or not shared yet.

## Limits

- One account can send about 100 invites a day. After that, claude.ai refuses more until the next day (measured in October 2026).
- Every invite sends an email. It cannot be turned off.
- Claude Code's own login cannot send or accept invites. Only the Share menu and the invite can.
- A page set to anyone with the link cannot be updated by Claude Code from an account that does not own it, even with Editor access (seen in October 2026). Keep a page you edit from several accounts private, and invite.
- On Team and Enterprise plans, an Owner decides whether pages can be shared outside the organization or publicly.
- The red "Artifact comment monitor ... didn't resume" warnings are a different problem. Claude Session Sync stops them with `configure --stop-artifact-warnings --apply`.
