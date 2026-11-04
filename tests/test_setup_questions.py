"""The install asks once whether to stop the artifact "didn't resume" warnings."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from claude_session_sync.cli import CliDependencies, run
from claude_session_sync.installer import InstallAction, InstallReport


class Terminal(io.StringIO):
    """Typed answers, or printed output, on what looks like a terminal."""

    def isatty(self):
        return True


class FakeInstaller:
    def __init__(self, default_data: bytes):
        self.default_data = default_data
        self.calls: list[tuple[bool, bytes | None]] = []

    def default_config_data(self) -> bytes:
        return self.default_data

    def setup(self, *, dry_run, config_data=None, before_activation=None):
        self.calls.append((dry_run, config_data))
        return InstallReport(
            "planned" if dry_run else "installed",
            (InstallAction("create", Path("/private/not-output")),),
        )


class ArtifactWarningsQuestionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.document = {
            "version": 1,
            "approved_targets": [],
            "profiles": [
                {
                    "name": "Work",
                    "data_root": str(root / "Claude"),
                    "launch_command": ["/usr/bin/true"],
                    "is_default": True,
                },
            ],
            "state_dir": str(root / "state"),
            "retention": 5,
            "acknowledge_cross_profile_copy": False,
            "acknowledge_cross_account_copy": False,
            "target_policy": "approved-only",
            "claude_executable": "/usr/bin/true",
        }
        self.config_path = root / "config" / "config.json"

    def tearDown(self):
        self.directory.cleanup()

    def setup_with(
        self, typed: str | None, *options: str, existing: dict | None = None
    ):
        if existing is not None:
            self.config_path.parent.mkdir(parents=True)
            self.config_path.write_text(json.dumps(existing))
        installer = FakeInstaller((json.dumps(self.document) + "\n").encode())
        output = Terminal() if typed is not None else io.StringIO()
        answers = Terminal(typed) if typed is not None else io.StringIO("y\n")
        code = run(
            ["--config", str(self.config_path), "setup", *options],
            dependencies=CliDependencies(installer_factory=lambda _path: installer),
            stdout=output,
            stderr=io.StringIO(),
            stdin=answers,
        )
        self.assertEqual(0, code)
        self.assertEqual(1, len(installer.calls))
        desired = json.loads(installer.calls[0][1])
        return desired, output.getvalue()

    def test_yes_stops_the_warnings_and_says_the_cost(self):
        desired, printed = self.setup_with("y\n", "--ask-artifact-warnings", "--apply")
        self.assertIs(True, desired["stop_artifact_warnings"])
        self.assertIn("didn't resume", printed)
        self.assertIn("stops answering comments", printed)
        self.assertIn("stop from the next time Claude closes", printed)

    def test_enter_keeps_them_and_records_the_answer(self):
        desired, printed = self.setup_with("\n", "--ask-artifact-warnings", "--apply")
        self.assertIs(False, desired["stop_artifact_warnings"])
        self.assertIn("configure --stop-artifact-warnings --apply", printed)

    def test_end_of_input_leaves_the_question_for_next_time(self):
        desired, _printed = self.setup_with("", "--ask-artifact-warnings", "--apply")
        self.assertNotIn("stop_artifact_warnings", desired)

    def test_an_update_after_an_answer_does_not_ask_again(self):
        for answer in (False, True):
            with self.subTest(answer=answer):
                existing = dict(self.document, stop_artifact_warnings=answer)
                if self.config_path.exists():
                    self.config_path.unlink()
                    self.config_path.parent.rmdir()
                desired, printed = self.setup_with(
                    "y\n", "--ask-artifact-warnings", "--apply", existing=existing
                )
                self.assertIs(answer, desired["stop_artifact_warnings"])
                self.assertNotIn("didn't resume", printed)

    def test_without_a_terminal_nothing_is_asked(self):
        desired, printed = self.setup_with(None, "--ask-artifact-warnings", "--apply")
        self.assertNotIn("stop_artifact_warnings", desired)
        self.assertNotIn("didn't resume", printed)

    def test_the_option_turns_it_on_without_a_question(self):
        desired, printed = self.setup_with(
            "\n", "--stop-artifact-warnings", "--ask-artifact-warnings", "--apply"
        )
        self.assertIs(True, desired["stop_artifact_warnings"])
        self.assertNotIn("didn't resume", printed)

    def test_a_dry_run_asks_nothing(self):
        desired, printed = self.setup_with(
            "y\n", "--ask-artifact-warnings", "--dry-run"
        )
        self.assertNotIn("stop_artifact_warnings", desired)
        self.assertNotIn("didn't resume", printed)

    # A no given in a Claude Code chat is saved the way Enter is.

    def test_the_no_option_records_the_answer_without_a_question(self):
        desired, printed = self.setup_with(
            "y\n", "--no-stop-artifact-warnings", "--ask-artifact-warnings", "--apply"
        )
        self.assertIs(False, desired["stop_artifact_warnings"])
        self.assertNotIn("didn't resume", printed)

    def test_the_no_option_works_without_a_terminal(self):
        desired, _printed = self.setup_with(
            None, "--no-stop-artifact-warnings", "--apply"
        )
        self.assertIs(False, desired["stop_artifact_warnings"])

    def test_the_no_option_never_turns_off_an_earlier_yes(self):
        existing = dict(self.document, stop_artifact_warnings=True)
        desired, _printed = self.setup_with(
            None, "--no-stop-artifact-warnings", "--apply", existing=existing
        )
        self.assertIs(True, desired["stop_artifact_warnings"])

    def test_a_dry_run_records_nothing(self):
        desired, _printed = self.setup_with(
            None, "--no-stop-artifact-warnings", "--dry-run"
        )
        self.assertNotIn("stop_artifact_warnings", desired)

    def test_yes_and_no_together_are_refused(self):
        errors = io.StringIO()
        code = run(
            [
                "--config",
                str(self.config_path),
                "setup",
                "--stop-artifact-warnings",
                "--no-stop-artifact-warnings",
                "--apply",
            ],
            stdout=io.StringIO(),
            stderr=errors,
        )
        self.assertNotEqual(0, code)
        self.assertIn("not allowed with", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
