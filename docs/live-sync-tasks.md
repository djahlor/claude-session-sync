# Sync without a restart: checklist

Started 03-11-2026 on his words: "it would be so good if it worked without restarted".
Branch `claude/sync-without-restart-e3187c`, from `main` at 0.6.7. PR #22 is open and not merged, so this branch does not build on it.

- [x] Read the repo, the 01-10 review, the sync rules evidence and the timeline of breakages
- [x] Unpack Claude 4.4212.0 and find how it loads chats at sign-in
- [x] Read Claude's logs for every sign-out and sign-in since 26-10
- [x] Upstream check: claude-code issues and pull requests, web
- [x] Routines, groups and the archive list: when Claude reads them
- [x] Write `docs/live-sync-findings.md` with the answer and its evidence
- [x] Build the sync at sign-out, with tests (18 new, plus 2 helper scenarios and 1 routine test)
- [x] Rehearse on a clone of his Claude folders (24 folders, 12 GB)
- [x] Independent review of the change
- [ ] Live check with Claude open: needs this build installed and one real switch by him
- [x] PR to `main`

## Later

- Skip the restart for groups when the arriving account already has the same groups: wait until Claude's web view has merged that account's server copy (`ccd-sync-owner` names it), then compare its slot with the slot of the account just left.
- Make a no-change check faster than about 10 seconds, so it beats a quick sign-in more often.
