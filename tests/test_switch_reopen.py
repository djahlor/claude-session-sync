"""A switch whose chat sync is cut short by Claude opening again (20-10-2026)."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_session_sync import cli
from claude_session_sync.cli import CliDependencies, run
from claude_session_sync.config import Config
from claude_session_sync.model import Profile
from claude_session_sync.transaction import AppRunningError

SYNCED = {"bytes": 0, "counts": {"operations": 3, "planned": 3}, "state": "committed"}


class Claude:
    """Open or closed, as the process probe sees it."""

    def __init__(self) -> None:
        self.open = False
        self.launches = []

    def running(self, **_kwargs):
        return ("claude",) if self.open else ()

    def launch(self, _command):
        self.launches.append(self.open)


class SwitchReopenTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "Claude").mkdir()
        self.config = Config(
            profiles=(Profile("Work", self.root / "Claude", ("/usr/bin/true",), True),),
            state_dir=self.root / "state",
            retention=5,
            acknowledge_cross_profile_copy=False,
            acknowledge_cross_account_copy=True,
            claude_executable=Path("/usr/bin/true"),
        )
        self.claude = Claude()
        self.guard = self.config.state_dir / cli.SWITCH_GUARD_NAME

    def switch(self, chat_results):
        guard_seen = []

        def chat(*_args):
            guard_seen.append(self.guard.is_file())
            result = chat_results.pop(0)
            if callable(result):
                result = result()
            if isinstance(result, Exception):
                raise result
            return result

        out = io.StringIO()
        with mock.patch.object(cli, "_chat_payload", side_effect=chat):
            code = run(
                ["--config", str(self.root / "config.json"), "switch", "Work", "--json"],
                dependencies=CliDependencies(
                    config_loader=lambda _path: self.config,
                    process_probe=self.claude,
                    launcher=self.claude,
                    sleeper=lambda _seconds: None,
                ),
                stdout=out,
                stderr=io.StringIO(),
            )
        return code, json.loads(out.getvalue()), guard_seen

    def test_the_sync_runs_again_once_the_reopened_claude_is_gone(self):
        def reopened():
            self.claude.open = True
            return AppRunningError("a managed Claude process is running")

        original = cli._wait_for_managed_processes_to_exit

        def helper_quits_claude(config, dependencies, timeout):
            # The Sync helper saw the guard and asked Claude to quit.
            self.claude.open = False
            return original(config, dependencies, timeout)

        with mock.patch.object(cli, "_wait_for_managed_processes_to_exit",
                               side_effect=helper_quits_claude):
            code, payload, guard_seen = self.switch([reopened, dict(SYNCED)])

        self.assertEqual(0, code)
        self.assertEqual("committed", payload["state"])
        self.assertEqual("finished", payload["progress"])
        self.assertEqual([True, True], guard_seen)
        self.assertEqual([False], self.claude.launches)
        self.assertFalse(self.guard.exists())

    def test_a_claude_that_stays_open_ends_the_switch_as_before(self):
        self.claude.open = True
        error = AppRunningError("a managed Claude process is running")
        with mock.patch.object(cli, "_wait_for_managed_processes_to_exit",
                               side_effect=[True, False]):
            code, payload, guard_seen = self.switch([error])

        self.assertEqual(1, code)
        self.assertEqual("claude-open", payload["reason"])
        self.assertEqual([True], guard_seen)
        self.assertFalse(self.guard.exists())

    def test_retries_stop_after_two(self):
        errors = [AppRunningError("open") for _ in range(3)]
        code, payload, guard_seen = self.switch(errors)

        self.assertEqual(1, code)
        self.assertEqual("claude-open", payload["reason"])
        self.assertEqual(3, len(guard_seen))

    def test_the_guard_is_gone_before_claude_is_reopened(self):
        seen = []
        self.claude.launch = lambda _command: seen.append(self.guard.exists())
        self.switch([dict(SYNCED)])

        self.assertEqual([False], seen)


if __name__ == "__main__":
    unittest.main()
