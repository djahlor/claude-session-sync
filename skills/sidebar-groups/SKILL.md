---
name: sidebar-groups
description: Make the Claude desktop Code sidebar show the same custom groups in every account. Newer Claude versions keep groups per account on Anthropic's servers and reload them at every sign-in, so a disk sync cannot copy them. This skill saves one account's groups to a layout file and rebuilds them in the signed-in account through the app's own sidebar tools. Use when someone says their groups did not sync, are missing or old after switching accounts, asks to copy or save their groups, or says "sync my groups".
---

# Sidebar groups across accounts

Groups now live per account (and per organization) on Anthropic's servers. A group made through the app's sidebar tools is saved there and stays. So one account is the source: its groups go to a layout file, and every other account is rebuilt from that file once, while signed in to it.

## Inputs

| Input | Description |
|---|---|
| Mode | `apply` (default): make this account match the layout file. `save`: write this account's groups to the layout file. |
| Layout file | `~/.claude/sidebar-groups/layout.json` |

## Prerequisites

- The Claude desktop app with its Code tab open, so these tools answer: `list_sections`, `list_sessions`, `create_group`, `move_sessions`, `delete_group`, `set_view`.
- The signed-in account and organization are in `CLAUDE_CODE_ACCOUNT_UUID` and `CLAUDE_CODE_ORGANIZATION_UUID`. Call the pair `<scope>`.

## Pick the mode

- The person says save, keep, or "these are my groups": `save`.
- The layout file is missing: `save`, then stop.
- Otherwise `apply`. If `<scope>` equals the file's `saved_from`, say this account is the source and stop, unless they asked to apply anyway.

## Save

1. Call `list_sections`. Keep the sections with `kind` `manual`, in `order`.
2. For each, call `list_sessions` with `group` set to its id and `limit` 1000. Keep the `sessionId` of every row. The current chat is not listed; if `list_sessions` without a group shows nothing for it, leave it out.
3. Write the file:

   ```json
   {
     "version": 1,
     "saved_at": "<ISO time now>",
     "saved_from": "<scope>",
     "groups": [
       {"name": "<name>", "group_by": "<group_by>", "sort_by": "<sort_by>", "sessions": ["local_..."]}
     ]
   }
   ```

4. Read the file back and check it parses and has one entry per manual section.
5. Report: groups saved, chats per group, and that other accounts get them with this skill once each.

## Apply

1. Read the layout file. Call `list_sections`. Match each layout group to a `manual` section by exact name.
2. Say the plan in one line: groups to create, groups to remove, chats to file. Then do it. Removing a group never archives or deletes a chat; its chats move to Ungrouped.
3. Create each missing group with `create_group`.
4. For each layout group, call `move_sessions` with its id and the layout's session ids, at most 100 per call. A reply that names a session as archived, a routine run, or not found is normal: drop that id and repeat the call with the rest.
5. Call `set_view` with each group's `section_id`, `group_by` and `sort_by` from the layout.
6. Delete every `manual` section whose name is not in the layout with `delete_group`.
7. Call `list_sections` again. Check every layout group exists here and holds at least the chats that moved. A count can be higher: the app files chats started from a grouped chat into that group.
8. Write `~/.claude/sidebar-groups/applied.json` as `{"<scope>": "<saved_at of the layout>"}`, merged with what is there.
9. Report: groups made, kept and removed, chats filed, chats skipped and why.

## Rules

- Never archive, delete, rename or pin a chat.
- Only change the signed-in account. Another account needs its own run after signing in.
- After the person edits groups in one account, run `save` there first, then `apply` in the others.
