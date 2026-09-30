# Claude Session Sync verification

- A running launch agent or a successful chat-sync receipt does not prove sidebar sync succeeded. If layout or routines are deferred, report that component as unfinished.
- When the user has already restarted Claude, accept that evidence. Inspect the helper's completion timestamp, child processes, account receipt, and layout journal instead of asking for the same restart again.
- Before copying a sidebar, identify the intended source account and destination scope. Preview group names and assignment counts, preserve recovery data, then verify the destination in Claude after the write and relaunch.
- Do not direct the user to a Sync menu based only on source code. Verify the menu is available, or use the supported direct command within the authorized task.

- When a restored group looks empty, check the sidebar status filter and the assigned chats’ archive flags before changing assignments or repeating a sync. Verify the visible result after adjusting the filter.

These checks follow the 2026-09-26 repair: chat sync was reported as recovered while sidebar adoption remained pending, and restarting Claude did not clear the independently stuck watcher.
