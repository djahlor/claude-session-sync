import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from claude_session_sync.cli import CliDependencies, run
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
            self.assertEqual(
                [
                    {
                        "name": "Work",
                        "data_root": str(home / "Library" / "Application Support" / "Claude"),
                        "launch_command": ["/usr/bin/open", "-a", "/Applications/Claude.app"],
                        "is_default": True,
                    }
                ],
                template["profiles"],
            )
            self.assertEqual(
                stat.S_IMODE(layout.config_path.stat().st_mode),
                stat.S_IRUSR | stat.S_IWUSR,
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

    def test_uninstall_backs_up_what_setup_made_and_keeps_the_config(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            runner = FakeCommandRunner()
            installer = Installer(layout, runner=runner, backup_id=lambda: "removed")
            installer.install(dry_run=False)
            generated = (
                layout.launch_agent, layout.watcher_binary, layout.layout_helper,
                layout.runtime_cli, layout.runtime_package,
            )

            dry_uninstall = installer.uninstall(dry_run=True)
            self.assertEqual(
                ("planned", [True] * len(generated)),
                (dry_uninstall.state, [path.exists() for path in generated]),
            )
            removed = installer.uninstall(dry_run=False)

            self.assertEqual("uninstalled", removed.state)
            self.assertEqual([False] * len(generated), [path.exists() for path in generated])
            self.assertEqual(
                sorted(path.name for path in generated),
                sorted(path.name for path in (layout.backups_dir / "removed").iterdir()),
            )
            self.assertTrue(layout.config_path.exists())
            self.assertTrue(
                any(
                    call[0][:2] == ("/bin/launchctl", "bootout")
                    for call in runner.calls
                )
            )

    def september_install(self, home: Path) -> InstallLayout:
        """A home set up in September, plus the profile apps an older version made."""

        layout = self.layout(home)
        support = home / "Library" / "Application Support"
        document = {
            "version": 1,
            "approved_targets": [
                {"profile": "Work", "account": "account-a", "workspace": "workspace-a"},
                {"profile": "Personal", "account": "account-p", "workspace": "workspace-p"},
            ],
            "acknowledge_cross_account_copy": True,
            "acknowledge_cross_profile_copy": False,
            "target_policy": "logins",
            "sync_sidebar_layout": True,
            "sync_code_routines": True,
            "claude_executable": "/Applications/Claude.app/Contents/MacOS/Claude",
            "profiles": [
                {
                    "data_root": str(support / "Claude"),
                    "launch_command": ["/usr/bin/open", "-a", "/Applications/Claude.app"],
                    "name": "Work",
                    "is_default": True,
                },
                {
                    "data_root": str(support / "Claude-Personal"),
                    "launch_command": [
                        "/Applications/Claude.app/Contents/MacOS/Claude",
                        "--user-data-dir={}".format(support / "Claude-Personal"),
                    ],
                    "name": "Personal",
                    "enabled": False,
                    "is_default": False,
                },
            ],
            "retention": 10,
            "state_dir": str(layout.support_dir / "state"),
        }
        layout.config_path.parent.mkdir(parents=True)
        layout.config_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        os.chmod(layout.config_path, 0o600)
        for app in layout.retired_apps:
            launcher = app / "Contents" / "MacOS" / "launcher"
            launcher.parent.mkdir(parents=True)
            launcher.write_text("#!/bin/sh\nexec old-switch {}\n".format(app.stem))
        return layout

    def test_setup_over_a_september_install_leaves_one_profile_and_backs_up_the_old_apps(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = self.september_install(Path(directory))
            out = io.StringIO()

            code = run(
                ["--config", str(layout.config_path), "setup", "--automatic-targets",
                 "--sync-layout", "--sync-routines", "--apply"],
                dependencies=CliDependencies(
                    installer_factory=lambda path: Installer(
                        replace(layout, config_path=path),
                        runner=FakeCommandRunner(),
                        backup_id=lambda: "upgrade",
                    ),
                ),
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual(0, code, out.getvalue())
            document = json.loads(layout.config_path.read_text(encoding="utf-8"))
            self.assertEqual(["Work"], [profile["name"] for profile in document["profiles"]])
            self.assertEqual(
                [{"profile": "Work", "account": "account-a", "workspace": "workspace-a"}],
                document["approved_targets"],
            )
            self.assertEqual(["Work"], [profile.name for profile in load_config(layout.config_path).profiles])
            for app in layout.retired_apps:
                self.assertFalse(app.exists())
                kept = layout.backups_dir / "upgrade" / app.name / "Contents" / "MacOS" / "launcher"
                self.assertEqual("#!/bin/sh\nexec old-switch {}\n".format(app.stem), kept.read_text())

    def test_uninstall_still_backs_up_old_launcher_apps(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = self.september_install(Path(directory))

            removed = Installer(layout, runner=FakeCommandRunner(), backup_id=lambda: "removed").uninstall(
                dry_run=False
            )

            self.assertEqual("uninstalled", removed.state)
            for app in layout.retired_apps:
                self.assertFalse(app.exists())
                self.assertTrue((layout.backups_dir / "removed" / app.name / "Contents" / "MacOS" / "launcher").is_file())

    def test_a_dry_run_over_a_september_install_lists_the_old_apps_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = self.september_install(Path(directory))
            before = layout.config_path.read_bytes()

            report = Installer(layout, runner=FakeCommandRunner()).setup(dry_run=True)

            self.assertEqual(
                [("remove-retired", app) for app in layout.retired_apps],
                [(action.kind, action.path) for action in report.actions if action.kind == "remove-retired"],
            )
            self.assertEqual(before, layout.config_path.read_bytes())
            self.assertTrue(all(app.exists() for app in layout.retired_apps))

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

    def test_the_main_account_question_runs_before_the_new_watcher_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = self.layout(Path(directory))
            runner = FakeCommandRunner()
            installer = Installer(layout, runner=runner)
            seen = []

            def ask(helper):
                launchctl = [call[0][1] for call in runner.calls if call[0][0] == "/bin/launchctl"]
                seen.append((helper.read_bytes(), launchctl))

            installer.setup(dry_run=False, config_data=installer.default_config_data(), before_activation=ask)

            (helper_bytes, before), = seen
            after = [call[0][1] for call in runner.calls if call[0][0] == "/bin/launchctl"]
            self.assertEqual(b"compiled-layout-helper", helper_bytes, "the staged helper reads the sidebar")
            self.assertIn("bootout", before, "the old watcher was stopped first")
            self.assertNotIn("bootstrap", before, "the new watcher had not started")
            self.assertIn("bootstrap", after[len(before):], "the new watcher starts after the answer")

    def test_setup_applies_supplied_config_in_the_same_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            layout = self.layout(home)
            installer = Installer(layout, runner=FakeCommandRunner())
            document = json.loads(installer.default_config_data())
            document["sync_sidebar_layout"] = True
            config_data = (
                json.dumps(document, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")

            report = installer.setup(dry_run=False, config_data=config_data)

            self.assertEqual(report.state, "installed")
            self.assertEqual(layout.config_path.read_bytes(), config_data)

    def run_failed_setup_that_records_a_choice(self, home, existing_choice):
        from claude_session_sync.config import load_config
        from claude_session_sync.layout import ADOPT_PENDING_FILENAME, request_adoption

        class FailNextWatcherBootstrap(FakeCommandRunner):
            fail_next = False

            def __call__(self, command, **kwargs):
                if self.fail_next and len(command) > 1 and command[1] == "bootstrap":
                    self.fail_next = False
                    raise subprocess.CalledProcessError(1, command)
                return super().__call__(command, **kwargs)

        layout = self.layout(home)
        runner = FailNextWatcherBootstrap()
        installer = Installer(layout, runner=runner, backup_id=lambda: "rollback")
        installer.setup(dry_run=False)
        state_dir = load_config(layout.config_path).state_dir
        pending = state_dir / ADOPT_PENDING_FILENAME
        if existing_choice is not None:
            state_dir.mkdir(parents=True, exist_ok=True)
            pending.write_bytes(existing_choice)
        document = json.loads(layout.config_path.read_bytes())
        document["sync_sidebar_layout"] = True
        changed = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        runner.fail_next = True

        with self.assertRaises(subprocess.CalledProcessError):
            installer.setup(
                dry_run=False,
                config_data=changed,
                before_activation=lambda helper: request_adoption(state_dir, "acct/space"),
            )
        return pending

    def test_a_failed_setup_leaves_no_new_main_account_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = self.run_failed_setup_that_records_a_choice(Path(directory), None)

            self.assertFalse(pending.exists(), "the answer to a failed setup is not kept")

    def test_a_failed_setup_keeps_the_choice_made_before_it(self):
        earlier = b'{"version": 1, "source_scope": "earlier/space"}\n'
        with tempfile.TemporaryDirectory() as directory:
            pending = self.run_failed_setup_that_records_a_choice(Path(directory), earlier)

            self.assertEqual(earlier, pending.read_bytes())

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
