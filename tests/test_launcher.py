import tempfile
import unittest
from pathlib import Path

from claude_session_sync.launcher import LaunchError, Launcher


class LauncherTests(unittest.TestCase):
    def test_a_command_that_exits_with_a_failure_is_a_launch_failure(self):
        with self.assertRaisesRegex(LaunchError, "status 3"):
            Launcher().launch(["/bin/sh", "-c", "exit 3"])

    def test_a_command_that_cannot_start_is_a_launch_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(LaunchError, "could not start"):
                Launcher().launch([str(Path(directory) / "missing")])

    def test_a_command_still_running_after_the_wait_is_a_launch_failure(self):
        with self.assertRaisesRegex(LaunchError, "did not finish in time"):
            Launcher(timeout=0.2).launch(["/bin/sleep", "30"])

    def test_a_command_that_returns_success_is_launched(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "launched"

            Launcher().launch(["/usr/bin/touch", str(marker)])

            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
