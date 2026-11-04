"""The release workflow builds the ZIP the README's Download button gets."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

import claude_session_sync

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


class ReleaseTests(unittest.TestCase):
    def test_the_download_button_gets_the_file_the_release_attaches(self) -> None:
        link = re.search(r"releases/latest/download/([\w.-]+\.zip)", README)
        self.assertIsNotNone(link)
        self.assertIn(f"-o {link.group(1)} HEAD", WORKFLOW)
        self.assertIn(f'gh release create "$tag" {link.group(1)}', WORKFLOW)

    def test_the_workflow_reads_the_package_version(self) -> None:
        expression = re.search(r"sed -n '([^']+)'", WORKFLOW).group(1)
        result = subprocess.run(
            ["sed", "-n", expression, "src/claude_session_sync/__init__.py"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(claude_session_sync.__version__, result.stdout.strip())

    def test_the_release_notes_are_this_version_of_the_changelog(self) -> None:
        program = re.search(
            r"awk -v heading=\"[^\"]+\" '\n(.+?)\n\s+' CHANGELOG", WORKFLOW, re.S
        )
        version = claude_session_sync.__version__
        result = subprocess.run(
            ["awk", "-v", f"heading=## [{version}]", program.group(1), "CHANGELOG.md"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("###", result.stdout)
        self.assertNotIn("## [", result.stdout)

    @unittest.skipUnless(
        shutil.which("git") and (ROOT / ".git").exists(), "needs a git checkout"
    )
    def test_the_zip_opens_to_the_installer_a_reader_double_clicks(self) -> None:
        folder = re.search(r'git archive [^\n]*--prefix="([^"]+)"', WORKFLOW).group(1)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.zip"
            subprocess.run(
                [
                    "git",
                    "archive",
                    "--format=zip",
                    f"--prefix={folder}",
                    "-o",
                    str(archive),
                    "HEAD",
                ],
                cwd=ROOT,
                check=True,
            )
            with zipfile.ZipFile(archive) as bundle:
                entry = bundle.getinfo(folder + "Install Claude Session Sync.command")
                self.assertTrue(
                    entry.external_attr >> 16 & 0o111, "it stays executable"
                )
                self.assertIn(folder + "install.sh", bundle.namelist())
        self.assertIn("Install Claude Session Sync.command", README)


if __name__ == "__main__":
    unittest.main()
