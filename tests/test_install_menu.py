from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("zsh"), "zsh is required for the Mac installer")
class InstallMenuTests(unittest.TestCase):
    def run_menu(self, choice: str) -> subprocess.CompletedProcess:
        source = Path(__file__).resolve().parents[1] / "Install Claude Session Sync.command"
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
        self.assertEqual(arguments, [
            "--automatic-targets",
            "--sync-layout",
            "--sync-routines",
            "--disable-personal",
        ])
        self.assertNotIn("Advanced:", result.stdout)
        self.assertNotIn("Work and Personal", result.stdout)

    def test_safe_mode_still_requires_explicit_target_approval(self) -> None:
        result = self.run_menu("2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SETUP_ARG=--automatic-targets", result.stdout)
        self.assertIn("SETUP_ARG=--disable-personal", result.stdout)

    def test_removed_and_invalid_choices_do_not_start_installation(self) -> None:
        for choice in ("3", "", "invalid"):
            with self.subTest(choice=choice):
                result = self.run_menu(choice)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("SETUP_ARG=", result.stdout)
                self.assertIn("No changes made", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
