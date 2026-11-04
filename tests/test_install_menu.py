from __future__ import annotations

import os
import re
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("zsh"), "zsh is required for the Mac installer")
class InstallMenuTests(unittest.TestCase):
    def run_menu(self, choice: str) -> subprocess.CompletedProcess:
        source = (
            Path(__file__).resolve().parents[1] / "Install Claude Session Sync.command"
        )
        with tempfile.TemporaryDirectory(prefix="sync-menu-") as directory:
            root = Path(directory)
            menu = root / source.name
            shutil.copyfile(source, menu)
            installer = root / "install.sh"
            installer.write_text("#!/bin/sh\nprintf 'SETUP_ARG=%s\\n' \"$@\"\n")
            installer.chmod(0o700)
            return subprocess.run(
                ["zsh", str(menu)],
                input=choice + "\n",
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )

    def test_recommended_setup_uses_the_regular_claude_app(self) -> None:
        result = self.run_menu("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = re.findall(r"SETUP_ARG=([^\r\n]*)", result.stdout)
        self.assertEqual(
            arguments,
            [
                "--automatic-targets",
                "--sync-layout",
                "--sync-routines",
            ],
        )
        self.assertNotIn("Advanced:", result.stdout)
        self.assertNotIn("Work and Personal", result.stdout)

    def test_safe_mode_still_requires_explicit_target_approval(self) -> None:
        result = self.run_menu("2")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = re.findall(r"SETUP_ARG=([^\r\n]*)", result.stdout)
        self.assertEqual(arguments, ["--sync-layout", "--sync-routines"])

    def test_removed_and_invalid_choices_do_not_start_installation(self) -> None:
        for choice in ("3", "", "invalid"):
            with self.subTest(choice=choice):
                result = self.run_menu(choice)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("SETUP_ARG=", result.stdout)
                self.assertIn("No changes made", result.stdout + result.stderr)

    def run_install(
        self, root: Path, *options: str, typed: str | None = None
    ) -> subprocess.CompletedProcess:
        """install.sh with a fake Python. typed runs it on a terminal with those keys."""

        source = Path(__file__).resolve().parents[1] / "install.sh"
        python = root / "python3"
        python.write_text("#!/bin/sh\nprintf 'PY_ARG=%s\\n' \"$@\"\n")
        python.chmod(0o700)
        command = ["zsh", str(source), *options]
        environment = {
            "HOME": str(root / "home"),
            "PATH": "/usr/bin:/bin",
            "PYTHON_BIN": str(python),
        }
        if typed is not None:
            return self.run_on_terminal(command, environment, typed)
        return subprocess.run(
            command,
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def run_on_terminal(
        self, command: list, environment: dict, typed: str
    ) -> subprocess.CompletedProcess:
        """Run on a pseudo-terminal and type the answer once the question shows."""

        main, terminal = os.openpty()
        process = subprocess.Popen(
            command, env=environment, stdin=terminal, stdout=terminal, stderr=terminal
        )
        os.close(terminal)
        output = b""
        answered = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            ready, _, _ = select.select([main], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(main, 4096)
                except OSError:
                    break  # the program closed the terminal
                if not chunk:
                    break
                output += chunk
            if not answered and b"type n to skip: " in output:
                os.write(main, typed.encode())
                answered = True
            if process.poll() is not None and not ready:
                break
        os.close(main)
        process.wait(timeout=5)
        text = output.decode(errors="replace").replace("\r\n", "\n")
        return subprocess.CompletedProcess(command, process.returncode, text, "")

    def test_install_script_asks_for_the_main_account_during_setup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-install-") as directory:
            result = self.run_install(
                Path(directory),
                "--automatic-targets",
                "--sync-layout",
                "--sync-routines",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = re.findall(r"PY_ARG=([^\r\n]*)", result.stdout)
        setup = arguments[arguments.index("setup") :]
        self.assertEqual(
            [
                "setup",
                "--automatic-targets",
                "--sync-layout",
                "--ask-main-account",
                "--sync-routines",
                "--ask-artifact-warnings",
                "--apply",
            ],
            setup,
        )

    def test_the_warning_option_replaces_its_question(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-install-") as directory:
            result = self.run_install(Path(directory), "--stop-artifact-warnings")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = re.findall(r"PY_ARG=([^\r\n]*)", result.stdout)
        self.assertIn("--stop-artifact-warnings", arguments)
        self.assertNotIn("--ask-artifact-warnings", arguments)
        self.assertIn("'didn't resume' warnings stop", result.stdout)

    def test_a_no_to_the_warnings_is_saved_instead_of_asked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-install-") as directory:
            result = self.run_install(Path(directory), "--no-stop-artifact-warnings")
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = re.findall(r"PY_ARG=([^\r\n]*)", result.stdout)
        self.assertIn("--no-stop-artifact-warnings", arguments)
        self.assertNotIn("--ask-artifact-warnings", arguments)
        self.assertNotIn("--stop-artifact-warnings", arguments)
        self.assertNotIn("'didn't resume' warnings stop", result.stdout)

    def test_a_no_to_the_skill_is_remembered_for_the_next_terminal_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-install-") as directory:
            root = Path(directory)
            skill = root / "home/.claude/skills/share-artifacts/SKILL.md"

            declined = self.run_install(root, "--no-share-skill")
            self.assertEqual(declined.returncode, 0, declined.stderr)
            self.assertFalse(skill.exists())

            later = self.run_install(root, typed="\n")
            self.assertEqual(later.returncode, 0, later.stderr)
            self.assertNotIn("Add the share-artifacts skill", later.stdout)
            self.assertFalse(skill.exists())

    def test_a_no_to_the_skill_leaves_an_added_skill_alone(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-install-") as directory:
            root = Path(directory)
            skill = root / "home/.claude/skills/share-artifacts/SKILL.md"
            self.run_install(root, "--share-skill")
            self.assertTrue(skill.exists())

            result = self.run_install(root, "--no-share-skill")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(skill.exists())
            self.assertFalse(
                (
                    root
                    / "home/Library/Application Support/ClaudeSessionSync"
                    / "share-skill-skipped"
                ).exists()
            )

    def test_without_a_terminal_the_skill_is_added_only_when_asked_for(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-install-") as directory:
            root = Path(directory)
            skill = root / "home/.claude/skills/share-artifacts/SKILL.md"

            quiet = self.run_install(root)
            self.assertEqual(quiet.returncode, 0, quiet.stderr)
            self.assertFalse(skill.exists())
            self.assertNotIn("share-artifacts skill", quiet.stdout)

            added = self.run_install(root, "--share-skill")
            self.assertEqual(added.returncode, 0, added.stderr)
            shipped = (
                Path(__file__).resolve().parents[1] / "skills/share-artifacts/SKILL.md"
            )
            self.assertEqual(shipped.read_bytes(), skill.read_bytes())
            self.assertIn("share this page with my other accounts", added.stdout)

            accounts = skill.with_name("accounts.txt")
            accounts.write_text("work@example.com\n")
            skill.write_text("an older copy\n")
            updated = self.run_install(root)
            self.assertEqual(updated.returncode, 0, updated.stderr)
            self.assertEqual(shipped.read_bytes(), skill.read_bytes())
            self.assertEqual("work@example.com\n", accounts.read_text())

    def test_on_a_terminal_enter_adds_the_skill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-install-") as directory:
            root = Path(directory)
            result = self.run_install(root, typed="\n")
            skill = root / "home/.claude/skills/share-artifacts/SKILL.md"
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Add the share-artifacts skill", result.stdout)
            self.assertTrue(skill.exists())

    def test_a_skipped_skill_is_not_asked_again(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-install-") as directory:
            root = Path(directory)
            skill = root / "home/.claude/skills/share-artifacts/SKILL.md"

            first = self.run_install(root, typed="n\n")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("Add the share-artifacts skill", first.stdout)
            self.assertFalse(skill.exists())

            second = self.run_install(root, typed="\n")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("Add the share-artifacts skill", second.stdout)
            self.assertFalse(skill.exists())

            third = self.run_install(root, "--share-skill")
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertTrue(skill.exists())


if __name__ == "__main__":
    unittest.main()
