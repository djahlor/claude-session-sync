import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_session_sync.config import load_config
from claude_session_sync.installer import InstallLayout, Installer


class FakeCommandRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((tuple(command), kwargs))
        if "swiftc" in command:
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(b"compiled-watcher")
        if "clang++" in command:
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(b"compiled-layout-helper")
        if len(command) > 1 and command[1] == "print":
            return subprocess.CompletedProcess(command, 113, stdout="", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class InstallerTests(unittest.TestCase):
    def layout(self, home: Path) -> InstallLayout:
        return InstallLayout.for_home(home)

    def test_only_automatic_mode_watches_default_profile_account_changes(self):
        import plistlib

        with tempfile.TemporaryDirectory() as directory:
            installer = Installer(self.layout(Path(directory)), runner=FakeCommandRunner())
            document = json.loads(installer.default_config_data())
            safe = plistlib.loads(installer._launch_agent(json.dumps(document).encode()))["ProgramArguments"]
            self.assertNotIn("--account-file", safe)
            document["target_policy"] = "all-configured-profiles"
            document["acknowledge_cross_account_copy"] = True
            automatic = plistlib.loads(installer._launch_agent(json.dumps(document).encode()))["ProgramArguments"]
            self.assertEqual(str(Path(document["profiles"][0]["data_root"]) / "config.json"), automatic[automatic.index("--account-file") + 1])
            self.assertEqual("Work", automatic[automatic.index("--profile") + 1])
            self.assertLess(automatic.index("--profile"), automatic.index("--"))
            document["sync_sidebar_layout"] = True
            with_layout = plistlib.loads(installer._launch_agent(json.dumps(document).encode()))["ProgramArguments"]
            self.assertEqual(
                automatic[: automatic.index("--")],
                with_layout[: with_layout.index("--")],
                "a switch restarts Claude whether or not pins and groups sync",
            )

    def test_dry_run_describes_install_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "fresh-home"
            installer = Installer(self.layout(home), runner=FakeCommandRunner())

            report = installer.install(dry_run=True)

            self.assertEqual(report.state, "planned")
            self.assertGreater(report.change_count, 0)
            self.assertFalse(home.exists())

    def test_apply_generates_private_config_runtime_watcher_and_launch_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            runner = FakeCommandRunner()
            installer = Installer(layout, runner=runner, backup_id=lambda: "backup-a")
            original = layout.applications_dir / "Claude.app" / "original"
            original.parent.mkdir(parents=True)
            original.write_text("untouched", encoding="utf-8")

            report = installer.install(dry_run=False)

            self.assertEqual(report.state, "installed")
            template = json.loads(layout.config_path.read_text(encoding="utf-8"))
            self.assertFalse(template["acknowledge_cross_profile_copy"])
            self.assertFalse(template["acknowledge_cross_account_copy"])
            self.assertEqual(template["target_policy"], "approved-only")
            self.assertFalse(template["sync_sidebar_layout"])
            self.assertFalse(template["sync_code_routines"])
            self.assertFalse(template["profiles"][1]["enabled"])
            self.assertEqual(
                template["profiles"][1]["data_root"],
                str(home / "Library" / "Application Support" / "Claude-Personal"),
            )
            self.assertEqual(
                [profile.name for profile in load_config(layout.config_path).profiles],
                ["Work"],
            )
            self.assertEqual(
                stat.S_IMODE(layout.config_path.stat().st_mode),
                stat.S_IRUSR | stat.S_IWUSR,
            )
            self.assertFalse(layout.work_app.exists())
            self.assertFalse(layout.personal_app.exists())
            template["profiles"][1]["enabled"] = True
            template["acknowledge_cross_profile_copy"] = True
            template["acknowledge_cross_account_copy"] = True
            layout.config_path.write_text(
                json.dumps(template, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(layout.config_path, 0o600)
            activated = installer.install(dry_run=False)
            self.assertEqual(activated.state, "installed")
            work_launcher = layout.work_app / "Contents" / "MacOS" / "launcher"
            personal_launcher = layout.personal_app / "Contents" / "MacOS" / "launcher"
            self.assertIn("switch Work", work_launcher.read_text(encoding="utf-8"))
            self.assertIn(
                "--wait-for-exit 15",
                work_launcher.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "switch Personal", personal_launcher.read_text(encoding="utf-8")
            )
            self.assertIn(
                "--wait-for-exit 15",
                personal_launcher.read_text(encoding="utf-8"),
            )
            self.assertTrue(os.access(work_launcher, os.X_OK))
            self.assertIn(
                str(layout.runtime_cli), work_launcher.read_text(encoding="utf-8")
            )
            self.assertTrue(layout.runtime_cli.is_file())
            self.assertTrue(os.access(layout.runtime_cli, os.X_OK))
            self.assertTrue((layout.runtime_package / "cli.py").is_file())
            self.assertTrue((layout.runtime_package / "layoutdb.cc").is_file())
            self.assertTrue(
                (
                    layout.runtime_package
                    / "vendor"
                    / "leveldb"
                    / "include"
                    / "leveldb"
                    / "db.h"
                ).is_file()
            )
            self.assertTrue(
                (layout.runtime_package / "SessionSyncWatcher.swift").is_file()
            )
            self.assertIn(
                str(layout.runtime_package.parent),
                layout.runtime_cli.read_text(encoding="utf-8"),
            )
            standalone = subprocess.run(
                [str(layout.runtime_cli), "--help"],
                cwd="/",
                env={"PATH": "/usr/bin:/bin"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(standalone.returncode, 0, standalone.stderr)
            standalone_dry_run = subprocess.run(
                [str(layout.runtime_cli), "install", "--dry-run"],
                cwd="/",
                env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                standalone_dry_run.returncode, 0, standalone_dry_run.stderr
            )
            self.assertEqual(layout.watcher_binary.read_bytes(), b"compiled-watcher")
            self.assertEqual(
                layout.layout_helper.read_bytes(), b"compiled-layout-helper"
            )
            agent = layout.launch_agent.read_text(encoding="utf-8")
            self.assertIn("<key>RunAtLoad</key>\n  <true/>", agent)
            self.assertIn(str(layout.watcher_binary), agent)
            self.assertIn("<string>--</string>", agent)
            self.assertTrue(
                any(
                    call[0][:2] == ("/usr/bin/plutil", "-lint") for call in runner.calls
                )
            )
            self.assertTrue(any("swiftc" in call[0] for call in runner.calls))
            self.assertTrue(any("clang++" in call[0] for call in runner.calls))
            self.assertTrue(
                any(
                    call[0][:2] == ("/bin/launchctl", "bootstrap")
                    for call in runner.calls
                )
            )
            self.assertEqual(original.read_text(encoding="utf-8"), "untouched")

            compile_count = sum("swiftc" in call[0] for call in runner.calls)
            second = installer.install(dry_run=False)
            self.assertEqual(second.state, "noop")
            self.assertEqual(
                sum("swiftc" in call[0] for call in runner.calls), compile_count
            )

    def test_replaced_wrapper_is_backed_up_and_uninstall_preserves_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            runner = FakeCommandRunner()
            installer = Installer(layout, runner=runner, backup_id=lambda: "backup-b")
            installer.install(dry_run=False)
            template = json.loads(layout.config_path.read_text(encoding="utf-8"))
            template["profiles"][1]["enabled"] = True
            template["acknowledge_cross_profile_copy"] = True
            template["acknowledge_cross_account_copy"] = True
            layout.config_path.write_text(
                json.dumps(template, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(layout.config_path, 0o600)
            installer.install(dry_run=False)
            launcher = layout.work_app / "Contents" / "MacOS" / "launcher"
            launcher.write_text("user replacement", encoding="utf-8")

            repaired = installer.install(dry_run=False)

            self.assertTrue(repaired.backups)
            backup_app = repaired.backups[0]
            self.assertEqual(
                (backup_app / "Contents" / "MacOS" / "launcher").read_text(
                    encoding="utf-8"
                ),
                "user replacement",
            )
            dry_uninstall = installer.uninstall(dry_run=True)
            self.assertEqual(dry_uninstall.state, "planned")
            self.assertTrue(layout.work_app.exists())

            removed = installer.uninstall(dry_run=False)

            self.assertEqual(removed.state, "uninstalled")
            self.assertFalse(layout.work_app.exists())
            self.assertFalse(layout.personal_app.exists())
            self.assertFalse(layout.launch_agent.exists())
            self.assertFalse(layout.watcher_binary.exists())
            self.assertFalse(layout.runtime_cli.exists())
            self.assertFalse(layout.runtime_package.exists())
            self.assertTrue(layout.config_path.exists())
            self.assertTrue(
                any(
                    call[0][:2] == ("/bin/launchctl", "bootout")
                    for call in runner.calls
                )
            )

    def test_install_reversibly_disables_known_legacy_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            legacy = layout.legacy_launch_agents[0]
            legacy.parent.mkdir(parents=True)
            legacy.write_text("legacy", encoding="utf-8")
            runner = FakeCommandRunner()
            installer = Installer(layout, runner=runner, backup_id=lambda: "legacy")

            report = installer.install(dry_run=False)

            self.assertFalse(legacy.exists())
            self.assertTrue(
                any(path.read_text() == "legacy" for path in report.backups)
            )
            self.assertTrue(
                any(
                    call[0][:2] == ("/bin/launchctl", "bootout")
                    and str(legacy) in call[0]
                    for call in runner.calls
                )
            )

    def test_invalid_existing_config_blocks_all_install_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            layout.config_path.parent.mkdir(parents=True)
            layout.config_path.write_text("{}", encoding="utf-8")
            os.chmod(layout.config_path, 0o600)
            installer = Installer(layout, runner=FakeCommandRunner())

            with self.assertRaisesRegex(ValueError, "missing config keys"):
                installer.install(dry_run=False)

            self.assertFalse(layout.runtime_package.exists())
            self.assertFalse(layout.work_app.exists())

    def test_compile_failure_restores_exact_preinstall_state(self):
        class FailingLayoutCompiler(FakeCommandRunner):
            def __call__(self, command, **kwargs):
                if "clang++" in command:
                    raise subprocess.CalledProcessError(
                        1, command, stderr="synthetic compiler failure"
                    )
                return super().__call__(command, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            unrelated = layout.support_dir / "keep.txt"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_bytes(b"keep-exactly")
            installer = Installer(layout, runner=FailingLayoutCompiler())

            with self.assertRaisesRegex(RuntimeError, "synthetic compiler failure"):
                installer.install(dry_run=False)

            self.assertEqual(unrelated.read_bytes(), b"keep-exactly")
            self.assertFalse(layout.config_path.exists())
            self.assertFalse(layout.runtime_package.exists())
            self.assertFalse(layout.runtime_cli.exists())
            self.assertFalse(layout.watcher_binary.exists())
            self.assertFalse(layout.layout_helper.exists())
            self.assertFalse(layout.launch_agent.exists())

    def test_legacy_service_must_be_confirmed_stopped_before_activation(self):
        class StubbornLegacyRunner(FakeCommandRunner):
            def __call__(self, command, **kwargs):
                result = super().__call__(command, **kwargs)
                if (
                    len(command) > 1
                    and command[1] == "print"
                    and command[-1].endswith("/com.djahlor.claude-session-sync")
                ):
                    return subprocess.CompletedProcess(
                        command, 0, stdout="still loaded", stderr=""
                    )
                return result

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            legacy = layout.legacy_launch_agents[0]
            legacy.parent.mkdir(parents=True)
            legacy.write_text("legacy", encoding="utf-8")
            installer = Installer(layout, runner=StubbornLegacyRunner())

            with self.assertRaisesRegex(RuntimeError, "still loaded"):
                installer.install(dry_run=False)

            self.assertTrue(legacy.exists())
            self.assertFalse(
                any(
                    call[0][:2] == ("/bin/launchctl", "bootstrap")
                    for call in installer._runner.calls
                )
            )

    def test_custom_state_directory_receives_watcher_status(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            runner = FakeCommandRunner()
            installer = Installer(layout, runner=runner)
            installer.install(dry_run=False)
            document = json.loads(layout.config_path.read_text(encoding="utf-8"))
            custom_state = home / "private-custom-state"
            document["state_dir"] = str(custom_state)
            layout.config_path.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.chmod(layout.config_path, 0o600)

            installer.install(dry_run=False)

            launch_agent = layout.launch_agent.read_text(encoding="utf-8")
            self.assertIn(str(custom_state / "watcher-status.json"), launch_agent)

    def test_setup_removes_a_launch_guard_an_older_version_left(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            installer = Installer(layout, runner=FakeCommandRunner())
            installer.install(dry_run=False)
            guard = load_config(layout.config_path).state_dir / "launch-pending.json"
            guard.parent.mkdir(parents=True, exist_ok=True)
            guard.write_text('{"profile": "Work", "version": 1}\n', encoding="utf-8")

            installer.setup(dry_run=True)
            self.assertTrue(guard.exists(), "a dry run changes nothing")
            installer.setup(dry_run=False)

            self.assertFalse(guard.exists())

    def test_setup_applies_supplied_config_in_the_same_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            installer = Installer(layout, runner=FakeCommandRunner())
            document = json.loads(installer.default_config_data())
            document["profiles"][1]["enabled"] = True
            document["acknowledge_cross_profile_copy"] = True
            document["acknowledge_cross_account_copy"] = True
            config_data = (
                json.dumps(document, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")

            report = installer.setup(dry_run=False, config_data=config_data)

            self.assertEqual(report.state, "installed")
            self.assertEqual(layout.config_path.read_bytes(), config_data)
            self.assertTrue(layout.work_app.exists())
            self.assertTrue(layout.personal_app.exists())

    def test_activation_failure_restores_existing_config_and_artifacts(self):
        class FailNextWatcherBootstrap(FakeCommandRunner):
            def __init__(self):
                super().__init__()
                self.fail_next = False

            def __call__(self, command, **kwargs):
                if (
                    self.fail_next
                    and len(command) > 1
                    and command[1] == "bootstrap"
                    and "com.claude-session-sync.watcher.plist" in command[-1]
                ):
                    self.fail_next = False
                    raise subprocess.CalledProcessError(1, command)
                return super().__call__(command, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            runner = FailNextWatcherBootstrap()
            installer = Installer(layout, runner=runner, backup_id=lambda: "rollback")
            installer.setup(dry_run=False)
            before_config = layout.config_path.read_bytes()
            before_agent = layout.launch_agent.read_bytes()
            before_runtime = {
                path.relative_to(layout.runtime_package): path.read_bytes()
                for path in layout.runtime_package.rglob("*")
                if path.is_file()
            }
            document = json.loads(before_config)
            document["sync_sidebar_layout"] = True
            changed = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
            runner.fail_next = True

            with self.assertRaises(subprocess.CalledProcessError):
                installer.setup(dry_run=False, config_data=changed)

            self.assertEqual(layout.config_path.read_bytes(), before_config)
            self.assertEqual(layout.launch_agent.read_bytes(), before_agent)
            self.assertEqual(
                {
                    path.relative_to(layout.runtime_package): path.read_bytes()
                    for path in layout.runtime_package.rglob("*")
                    if path.is_file()
                },
                before_runtime,
            )


if __name__ == "__main__":
    unittest.main()
