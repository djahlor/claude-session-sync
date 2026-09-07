import os
import json
import platform
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from claude_session_sync.installer import InstallLayout, Installer, _plist, _string


@unittest.skipUnless(platform.system() == "Darwin", "macOS installer integration")
class InstallerIntegrationTests(unittest.TestCase):
    class StatefulLaunchctlRunner:
        def __init__(self):
            self.loaded = set()

        def __call__(self, command, **kwargs):
            if command[0] != "/bin/launchctl":
                return subprocess.run(command, **kwargs)
            if command[1] == "print":
                code = 0 if command[-1].split("/")[-1] in self.loaded else 113
            elif command[1] == "bootstrap":
                self.loaded.add(Path(command[-1]).stem)
                code = 0
            else:
                self.loaded.discard(Path(command[-1]).stem)
                code = 0
            return subprocess.CompletedProcess(command, code, stdout="", stderr="")

    def test_real_compilers_create_executable_artifacts(self):
        if os.environ.get("RUN_MACOS_INSTALLER_INTEGRATION") != "1":
            self.skipTest("set RUN_MACOS_INSTALLER_INTEGRATION=1 to run real compilers")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = Installer(InstallLayout.for_home(root / "home"))
            watcher = root / "SessionSyncWatcher"
            helper = root / "layoutdb"

            installer._stage_watcher(watcher)
            installer._stage_layout_helper(helper)

            self.assertTrue(os.access(watcher, os.X_OK))
            self.assertTrue(os.access(helper, os.X_OK))

    def test_synthetic_launch_agent_round_trip_when_explicitly_enabled(self):
        if os.environ.get("RUN_LAUNCHCTL_INTEGRATION") != "1":
            self.skipTest("set RUN_LAUNCHCTL_INTEGRATION=1 in a GUI session")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "synthetic-home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            label = "com.claude-session-sync.test.{}".format(uuid.uuid4().hex)
            plist = agents / (label + ".plist")
            plist.write_bytes(
                _plist(
                    (
                        ("Label", _string(label)),
                        ("ProgramArguments", "<array>\n    <string>/usr/bin/true</string>\n  </array>"),
                        ("RunAtLoad", "<true/>"),
                    )
                )
            )
            domain = "gui/{}".format(os.getuid())
            subprocess.run(["/usr/bin/plutil", "-lint", str(plist)], check=True)
            try:
                subprocess.run(
                    ["/bin/launchctl", "bootstrap", domain, str(plist)], check=True
                )
                result = subprocess.run(
                    ["/bin/launchctl", "print", domain + "/" + label],
                    check=False, text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0)
            finally:
                subprocess.run(
                    ["/bin/launchctl", "bootout", domain, str(plist)], check=False
                )

    def test_real_artifact_setup_upgrade_and_uninstall_with_fake_launchctl(self):
        if os.environ.get("RUN_MACOS_INSTALLER_INTEGRATION") != "1":
            self.skipTest("set RUN_MACOS_INSTALLER_INTEGRATION=1 to run real compilers")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "isolated-home"
            runner = self.StatefulLaunchctlRunner()
            layout = InstallLayout.for_home(home)
            installer = Installer(layout, runner=runner)

            installed = installer.setup(dry_run=False)
            self.assertEqual(installed.state, "installed")
            self.assertTrue(layout.watcher_binary.is_file())
            self.assertTrue(layout.layout_helper.is_file())
            self.assertIn("com.claude-session-sync.watcher", runner.loaded)

            document = json.loads(layout.config_path.read_text(encoding="utf-8"))
            document["sync_sidebar_layout"] = True
            configured = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
            upgraded = installer.setup(dry_run=False, config_data=configured)
            self.assertEqual(upgraded.state, "installed")
            self.assertEqual(layout.config_path.read_bytes(), configured)

            removed = installer.uninstall(dry_run=False)
            self.assertEqual(removed.state, "uninstalled")
            self.assertFalse(layout.runtime_package.exists())
            self.assertNotIn("com.claude-session-sync.watcher", runner.loaded)


if __name__ == "__main__":
    unittest.main()
