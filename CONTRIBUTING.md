# Contributing

Thanks for helping. Claude Session Sync writes to private Claude Desktop files, so every change has to keep people's chats safe.

## Set up

You need macOS, Python 3.9 or newer, and Apple's Command Line Tools.

```sh
git clone https://github.com/djahlor/claude-session-sync
cd claude-session-sync
```

## Check a change

Run the tests with your Python and with the macOS system Python, then lint and format:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests
ruff check .
ruff format --check .
```

CI runs the same checks on Python 3.9, 3.12 and 3.13. The wall-clock benchmark runs only with `RUN_PERFORMANCE_TESTS=1`, on an idle Mac. The [technical reference](docs/technical-reference.md#development) has the opt-in installer tests and a live test with Claude closed.

## Rules for a pull request

- One change per pull request, with a test that fails without it.
- Tests use temporary folders and synthetic records. Never add real chats, account IDs, workspace IDs, tokens or Claude databases to the repository or an issue.
- Sync must stop on a format it does not know. Do not make it guess.
- Add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md).

## Releases

A merge to `main` that raises `__version__` in `src/claude_session_sync/__init__.py` publishes that version as a GitHub release, with its `## [version]` section of the changelog as the notes and `Claude-Session-Sync.zip` attached ([workflow](.github/workflows/release.yml)). The Terminal command installs the newest release, and the [Homebrew tap](https://github.com/djahlor/homebrew-tap) points at it within six hours.

Report a security problem privately, as [SECURITY.md](SECURITY.md) says.
