# Security

Claude Session Sync reads undocumented local Claude Desktop storage. Treat the
configured profile roots, transaction journals, and backups as private data.

Do not attach real session registries, routine manifests, Local Storage
databases, account IDs, workspace IDs, access tokens, or chat content to a
GitHub issue. Routine manifests can contain local paths and permission settings.
Reproduce a problem with the synthetic test fixtures where possible.

For a suspected vulnerability, contact the repository owner privately before
opening a public issue. Include the tool version, macOS version, Claude Desktop
version, aggregate CLI state, and a minimal reproduction with secrets removed.

The affected adapter intentionally stops on unknown layouts, malformed JSON,
symlinks, conflicting revisions, and incomplete recovery. Routine and sidebar
failures remain separate from completed chat writes.
