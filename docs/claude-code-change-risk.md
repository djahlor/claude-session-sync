# Claude Code change-risk research

Date: 2026-09-05

## Question

What has changed historically in Claude Code, which changes could break Claude
Session Sync, and how should the project handle future changes?

## Scope and limits

This review uses Anthropic's official Claude Code repository and release notes.
Those sources document the Claude Code CLI and sometimes its Desktop integration.
They are not a complete source history for Claude Desktop, and they do not define
a stable contract for these private local stores:

- `claude-code-sessions/<account>/<workspace>/local_<session>.json`
- `claude-code-sessions/<account>/<workspace>/scheduled-tasks.json`
- Claude Desktop's LevelDB records for pins and groups

The local machine currently has Claude Code `2.1.258` and Claude Desktop
`1.46388.3`. The newest official Claude Code release observed during this review
was `2.1.261`. A Claude Code version difference does not prove a Claude Desktop
storage difference because these are separate release surfaces.

## Documented changes

### Persistent settings moved

Claude Code `1.0.7` moved `allowedTools` and `ignorePatterns` from
`.claude.json` to `settings.json`, and deprecated the old configuration commands.
This is direct evidence that Anthropic moves persistent state between files.

Source: [Claude Code 1.0.7 release notes](https://github.com/anthropics/claude-code/releases/tag/v1.0.7)

Related reliability fixes followed. Version `1.0.31` stopped invalid JSON from
resetting `.claude.json`, and version `1.0.45` enforced atomic config writes.

Sources: [Claude Code 1.0.31 release notes](https://github.com/anthropics/claude-code/releases/tag/v1.0.31),
[Claude Code 1.0.45 release notes](https://github.com/anthropics/claude-code/releases/tag/v1.0.45)

### Database use changed

Claude Code `0.2.100` made database storage optional and disabled continue and
resume when database support was missing. This shows that session features have
changed their dependency on a storage backend.

Source: [Claude Code 0.2.100 release notes](https://github.com/anthropics/claude-code/releases/tag/v0.2.100)

### Session indexing changed

Claude Code `2.1.30` replaced its session index with stat-based loading and
progressive enrichment. Version `2.1.50` fixed invisible resumable sessions
caused by resolving a storage path at different times and added a shutdown flush
to prevent data loss after an SSH disconnect.

Sources: [Claude Code 2.1.30 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.30),
[Claude Code 2.1.50 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.50)

Claude Code `2.1.47` also fixed sessions disappearing from resume when the first
message exceeded a size threshold or used array-format content. The session
still existed, but the index did not recognize its newer shape.

Source: [Claude Code 2.1.47 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.47)

### Retention rules changed

Claude Code `2.1.248` fixed Claude Desktop and Cowork sessions disappearing after
30 days. It exempted Desktop-written sessions from transcript cleanup and added
`desktopSessionCleanupPeriodDays` to cap that exemption.

Source: [Claude Code 2.1.248 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.248)

### Routine records changed

Claude Code `2.1.257` fixed `/schedule` routines whose saved prompt lacked a
message role and therefore ran with nothing to do. This is direct evidence that
serialized routine records and their interpretation are still changing.

Source: [Claude Code 2.1.257 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.257)

Claude Code `2.1.85` added transcript timestamp markers when scheduled tasks
fired, while `2.1.72` added a switch that could immediately stop scheduled cron
jobs. Scheduler execution and transcript integration are both active areas.

Sources: [Claude Code 2.1.85 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.85),
[Claude Code 2.1.72 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.72)

### Concurrent state writes lost data

Claude Code `2.1.259` fixed concurrent sessions silently reverting each other's
`.claude.json` changes, including workspace trust and MCP project state. This is
strong evidence for single-writer locks, revalidation, atomic replacement, and
post-write checks in a synchronizer.

Source: [Claude Code 2.1.259 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.259)

### Desktop gateway payloads are versioned

Claude Code `2.1.260` changed the Claude apps gateway to send
`orgPluginSettings` in a list form understood by Claude Desktop `1.15200.0` and
later. Older Desktop builds ignore it. Version `2.1.261` also changed gateway
login behavior when managed policy forces gateway authentication.

Sources: [Claude Code 2.1.260 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.260),
[Claude Code 2.1.261 release notes](https://github.com/anthropics/claude-code/releases/tag/v2.1.261)

This does not mean the sync tool should copy gateway or authentication data. It
means Desktop and Claude Code already use explicit version boundaries for some
shared payloads, so local compatibility should also be tied to an observed
Desktop build.

### Public output formats break too

Claude Code `0.2.117` explicitly changed print-mode JSON to nested message
objects for future metadata. Version `0.2.125` made a breaking change to Bedrock
model ARN spelling. Anthropic does mark some public changes as breaking, but
private Desktop storage has no equivalent promise.

Sources: [Claude Code 0.2.117 release notes](https://github.com/anthropics/claude-code/releases/tag/v0.2.117),
[Claude Code 0.2.125 release notes](https://github.com/anthropics/claude-code/releases/tag/v0.2.125)

## Risk to this project

| Surface | Evidence | Break risk | Likely symptom |
| --- | --- | --- | --- |
| Pins and groups | No public storage contract; exact LevelDB records | Highest | Layout adapter reports an unknown record shape |
| Code routines | Recent saved-role and scheduler changes | High | Missing routines, invalid schedules, or copied runtime state |
| Code chats | Index, path, content-shape, retention, and flush changes | Medium | Existing chats disappear from discovery or fail validation |
| Account namespaces | Frequent auth, gateway, and organization changes | Medium | A new account or workspace directory is not targeted |
| Authentication | Explicitly excluded from sync | Low direct risk | Sign-in changes do not migrate, but namespaces may change |

These rankings are engineering judgments from the documented changes and the
project's dependence on undocumented formats. They are not Anthropic guarantees.

## Future change hypotheses

The release history supports these concrete hypotheses:

1. A session index or namespace can move while the transcript data remains.
2. A routine definition can gain a schema version or move into another backend.
3. Scheduler execution state can separate further from routine definitions.
4. Desktop LevelDB keys or record envelopes can change without a CLI note.
5. Cloud, remote, and local sessions can gain different retention rules.
6. Account and organization identity can become more gateway-managed.

## Robust response

### Use capabilities, not a permanent format promise

Each adapter should expose a read-only compatibility probe. A probe should check
only what that adapter needs:

- chat target directories, file naming, and minimal session identity;
- routine manifest structure and definition-file boundary;
- exact sidebar keys, envelopes, internal agreement, and checksums.

A successful probe should save the Claude Desktop build, tool version, schema
fingerprints, and adapter result. A failed probe should skip only that adapter.

### Treat an application update as a gate

After Claude quits, compare the current Desktop build with the last probed build.
If it changed, run all read-only probes before writes. Compatible adapters can
continue immediately; an incompatible adapter stays unchanged and reports the
exact failed capability.

This keeps the whole system from being blocked by one changed surface while
preserving the stop-on-ambiguity rule for the affected data.

### Keep structural fixtures

Store redacted fixtures that retain field names, types, nesting, key hashes, and
relationships without account IDs, paths, titles, prompts, or content. Add a
fixture when a new Desktop build is verified. CI should replay every adapter
against all retained fixture generations.

### Monitor official releases as an early signal

Release notes can flag session, schedule, Desktop, account, gateway, settings,
storage, or migration changes. They cannot prove private-store compatibility.
The local probe remains the release gate.

## Recommendation

Use the private repository for a team beta now. Before a public GitHub launch,
add per-adapter probes, version-bound compatibility receipts, structural fixture
generations, and one shared adapter runner. Then publish a tested compatibility
table instead of promising that undocumented formats will never change.
