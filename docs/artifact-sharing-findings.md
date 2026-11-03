# Artifact pages on every account

Findings of 3 November 2026, on Claude Desktop 4.4212.0. Each claim is labelled: **verified** means it was seen in Anthropic's documentation or terms, on disk, or in a test that ran; **inferred** means it follows from those but was not tried.

## The answer

**Sync will not share pages for you.** Doing it automatically would mean reading your Claude login out of the Claude app and driving claude.ai with a script. Anthropic's terms forbid that kind of access, and the login is guarded by macOS. Sync has promised never to read your login.

**What ships instead:** the [share-artifacts skill](../skills/share-artifacts/SKILL.md). Ask Claude "share this page with my other accounts". It finds the page, asks whether the other accounts should only read it or also edit it, gives you the exact clicks with your emails ready to paste, and checks the result. You click **Share** on the page yourself. The installer offers to add the skill.

## Why a page looks deleted on your other accounts

| # | Claim | Evidence | Label |
|---|---|---|---|
| 1 | A page belongs to the account that published it. Other accounts cannot open it until it is shared. | Claude Code docs, [Share an artifact](https://code.claude.com/docs/en/artifacts#share-an-artifact): a new artifact is visible only to you. | verified |
| 2 | Only the page's **Share** menu shares it. Claude Code has no share or invite action. | The same docs send every share through the Share control in the page header. The Artifact tool's actions are publish, list, read, delete, open, pin and unpin. | verified |
| 3 | The Share menu can invite a person by email as reader, commenter or editor. | 153 runs on the author's own accounts, October 2026: roles offered `reader`, `commenter`, `editor`. | verified |

## Why sync cannot do it at a switch

| # | Claim | Evidence | Label |
|---|---|---|---|
| 4 | The Claude app keeps a claude.ai sign-in on disk. | `~/Library/Application Support/Claude/Cookies` holds `sessionKey` and `sessionKeyV3` cookies for `.claude.ai`. Only names and the `v10` encryption prefix were read, never a value. | verified |
| 5 | Those cookies are encrypted with a key in the login Keychain. | Keychain item `Claude Safe Storage`, account `Claude Key`. | verified |
| 6 | Another program that reads that key makes macOS ask the person to allow it. | The item was made by the Claude app, and macOS asks before any other app reads it. Not tried, so no prompt appeared. | inferred |
| 7 | A script that uses that sign-in breaks Anthropic's terms. | [Consumer Terms](https://www.anthropic.com/legal/consumer-terms), effective 8 October 2025, section 3: no access "through automated or non-human means" unless by API key or where Anthropic explicitly permits it. | verified |
| 8 | Claude Code's own login cannot send or accept invites. | Listing, sending, finding and reading invites with it all answered HTTP 403, "requires a browser session" (15 October 2026). It can open a page and read who owns it. | verified |
| 9 | Every invite sends an email. | The server refuses an invite without one: HTTP 409 `invite_notify_required` (15 October 2026). | verified |
| 10 | One account can send about 100 invites a day. | HTTP 429 after about 100 invites in 24 hours (17 October 2026). | verified |
| 11 | A page set to anyone with the link cannot be updated by Claude Code from an account that does not own it, even with Editor access. | Publish refused (22 October 2026). | verified |

The account-level numbers (8 to 11) come from a private script the author runs with one signed-in browser per account. It sent 3,028 invites from 15 October to 3 November 2026. It is the same kind of automated access as item 7, so it is not part of this project.

## Routes looked at

| Route | Result |
|---|---|
| Use the Claude app's sign-in at each switch, while Claude is closed | Not shipped: items 5 to 7, and it breaks sync's promise never to read your login. |
| One signed-in browser per account, driven by a script | Works, but needs a browser per account and is automated access (item 7). Not shipped. |
| Claude Code's own login | Refused by the server (item 8). |
| Anyone with the link | One click per page, no email, no accept step, opens on every account. Read only from the other accounts (item 11), and anyone with the link can see it. The skill offers it. |
| Invite each account as Editor | One Share per page, one email and one accept per account, about 100 a day (items 9 and 10). The other accounts can then update the page. The skill offers it. |
| A Claude skill that prepares the clicks | Shipped as share-artifacts. Nothing is sent without the person. |
