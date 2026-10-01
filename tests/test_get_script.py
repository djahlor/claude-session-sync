from pathlib import Path
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "get.sh"


@unittest.skipUnless(shutil.which("zsh"), "zsh is required for the Mac installer")
class GetScriptTests(unittest.TestCase):
    """The one-command installer, run against a local archive with a fake install.sh."""

    def run_get(self, root: Path, *, claude_app: bool = True, tools: bool = True):
        source = root / "claude-session-sync-main"
        source.mkdir()
        installer = source / "install.sh"
        installer.write_text(
            "#!/bin/sh\n"
            "printf 'SETUP_ARG=%s\\n' \"$@\"\n"
            "printf 'SETUP_DIR=%s\\n' \"$(cd \"$(dirname \"$0\")\" && pwd)\"\n"
        )
        installer.chmod(0o755)
        archive = root / "main.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(source, arcname=source.name)
        app = root / "Claude.app"
        if claude_app:
            app.mkdir()
        environment = dict(os.environ)
        environment.update({
            "CLAUDE_SESSION_SYNC_ARCHIVE": archive.as_uri(),
            "CLAUDE_SESSION_SYNC_CLAUDE_APP": str(app),
        })
        if not tools:
            stubs = root / "bin"
            stubs.mkdir()
            stub = stubs / "xcode-select"
            stub.write_text("#!/bin/sh\necho \"$@\" >> \"$(dirname \"$0\")/calls\"\nexit 2\n")
            stub.chmod(0o755)
            environment["PATH"] = "{}:{}".format(stubs, environment["PATH"])
        return subprocess.run(
            ["zsh", str(SCRIPT)],
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def test_it_downloads_installs_with_automatic_sync_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-get-") as directory:
            result = self.run_get(Path(directory))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            re.findall(r"SETUP_ARG=([^\r\n]*)", result.stdout),
            ["--automatic-targets", "--sync-layout", "--sync-routines"],
        )
        download = re.search(r"SETUP_DIR=([^\r\n]*)", result.stdout).group(1)
        self.assertFalse(Path(download).exists(), "the download folder is removed")

    def test_without_claude_desktop_nothing_is_installed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-get-") as directory:
            result = self.run_get(Path(directory), claude_app=False)

        self.assertEqual(result.returncode, 1)
        self.assertIn("Claude Desktop is not installed", result.stderr)
        self.assertNotIn("SETUP_ARG=", result.stdout)

    def test_without_apples_build_tools_it_asks_macos_to_install_them(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sync-get-") as directory:
            root = Path(directory)
            result = self.run_get(root, tools=False)
            calls = (root / "bin" / "calls").read_text().split()

        self.assertEqual(result.returncode, 1)
        self.assertIn("Command Line Tools", result.stdout)
        self.assertIn("--install", calls)
        self.assertNotIn("SETUP_ARG=", result.stdout)


if __name__ == "__main__":
    unittest.main()
