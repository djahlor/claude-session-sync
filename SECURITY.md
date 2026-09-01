# Security

Claude Session Sync reads undocumented local Claude Desktop storage. Treat the
configured profile roots, transaction journals, and backups as private data.

Do not attach real session registries, Local Storage databases, account IDs,
workspace IDs, access tokens, or chat content to a GitHub issue. Reproduce a
problem with the synthetic test fixtures where possible.

For a suspected vulnerability, contact the repository owner privately before
opening a public issue. Include the tool version, macOS version, Claude Desktop
version, aggregate CLI state, and a minimal reproduction with secrets removed.

The tool intentionally stops on unknown layouts, malformed JSON, symlinks,
conflicting revisions, and incomplete recovery. Do not weaken those checks to
make a failed sync continue.
