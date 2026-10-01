import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from claude_session_sync.cli import CliDependencies, run
from claude_session_sync.config import ApprovedTarget, Config
from claude_session_sync.installer import InstallAction, InstallReport
from claude_session_sync.model import Profile, RecoveryReceipt


def config(root: Path) -> Config:
    executable = "/usr/bin/true"
    return Config(
        profiles=(
            Profile("Work", root / "work", (executable,), True),
            Profile(
                "Personal",
                root / "personal",
                (executable, "--user-data-dir={}".format(root / "personal")),
            ),
        ),
        state_dir=root / "state",
        retention=5,
        acknowledge_cross_profile_copy=True,
        acknowledge_cross_account_copy=True,
        claude_executable=Path(executable),
        approved_targets=(
            ApprovedTarget("Work", "account", "workspace"),
            ApprovedTarget("Personal", "account", "workspace"),
        ),
    )


class FakeEngine:
    def rollback(self, run_id):
        self.rolled_back = run_id
        return RecoveryReceipt(run_id, "rolled_back", 3, 1)


class FakeLayoutReceipt:
    state = "synced"
    profile_count = 1
    record_count = 2
    group_count = 4
    assignment_count = 12
    pin_count = 3


class FakeLayout:
    def sync(self):
        return FakeLayoutReceipt()


class FakeRoutineReceipt:
    state = "synced"
    profile_count = 1
    target_count = 3
    manifest_count = 2
    task_count = 4
    write_count = 1


class FakeRoutine:
    def sync(self):
        return FakeRoutineReceipt()


class FakeProcessProbe:
    def __init__(self, processes=()):
        self.processes = tuple(processes)
        self.calls = []

    def running(self, **kwargs):
        self.calls.append(kwargs)
        return self.processes


class SequencedProcessProbe(FakeProcessProbe):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    def running(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.responses) > 1:
            return tuple(self.responses.pop(0))
        return tuple(self.responses[0])


class FakeLauncher:
    def __init__(self, events):
        self.events = events

    def launch(self, command):
        self.events.append(("launch", tuple(command)))


class ManualClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class CliSyncTests(unittest.TestCase):
    def test_sync_reports_process_inspection_permission_failure(self):
        class DeniedProbe(FakeProcessProbe):
            def running(self, **kwargs):
                raise PermissionError("private process details")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = io.StringIO()
            exit_code = run(
                ["--config", str(root / "config.json"), "sync", "--json"],
                dependencies=CliDependencies(
                    config_loader=lambda path: config(root),
                    process_probe=DeniedProbe(),
                ),
                stdout=out,
                stderr=io.StringIO(),
            )
            payload = json.loads(out.getvalue())
            self.assertEqual(1, exit_code)
            self.assertEqual("process-inspection-unavailable", payload["reason"])
            self.assertEqual("process-permission-error", payload["error_type"])
            self.assertNotIn("private", out.getvalue())

class CliAutomaticTests(unittest.TestCase):
    def test_a_sync_waits_without_writing_while_claude_is_open(self):
        for command, expected_exit in (("auto", 0), ("sync", 1)):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                out = io.StringIO()
                dependencies = CliDependencies(
                    config_loader=lambda path: config(root),
                    planner_factory=lambda _config: self.fail("planner must not run"),
                    process_probe=FakeProcessProbe((object(),)),
                )

                exit_code = run(
                    ["--config", str(root / "config.json"), command],
                    dependencies=dependencies,
                    stdout=out,
                    stderr=io.StringIO(),
                )

                self.assertEqual(expected_exit, exit_code)
                self.assertEqual(
                    "state=waiting reason=claude-open progress=waiting-for-Claude\n",
                    out.getvalue(),
                )

class CliSwitchTests(unittest.TestCase):
    def test_restart_check_reports_retryable_timeout_without_process_details(self):
        class SlowProbe:
            def running(self, **kwargs):
                raise subprocess.TimeoutExpired(["ps", "private-command-argument"], 5)

        with tempfile.TemporaryDirectory() as directory:
            loaded = replace(config(Path(directory)), target_policy="all-configured-profiles")
            deps = CliDependencies(config_loader=lambda _: loaded, process_probe=SlowProbe())
            out, errors = io.StringIO(), io.StringIO()
            self.assertEqual(1, run(("restart-check", "Work"), dependencies=deps, stdout=out, stderr=errors))
            self.assertEqual("process-inspection-timeout", json.loads(out.getvalue())["reason"])
            self.assertNotIn("private-command-argument", out.getvalue() + errors.getvalue())

    def test_restart_check_normalizes_default_profile_paths(self):
        from claude_session_sync.processes import ManagedProcess

        with tempfile.TemporaryDirectory() as directory:
            loaded = replace(config(Path(directory)), target_policy="all-configured-profiles")
            primary = loaded.profiles[0]
            process = ManagedProcess(1234, ("/usr/bin/true",), primary.data_root)
            for data_root in (primary.data_root / ".." / primary.data_root.name, Path(os.path.relpath(primary.data_root))):
                with self.subTest(data_root=data_root):
                    variant = replace(loaded, profiles=(replace(primary, data_root=data_root),))
                    deps = CliDependencies(config_loader=lambda _: variant, process_probe=FakeProcessProbe((process,)))
                    out = io.StringIO()
                    self.assertEqual(0, run(("restart-check", "Work"), dependencies=deps, stdout=out, stderr=io.StringIO()))
                    self.assertEqual([1234], json.loads(out.getvalue())["pids"])

    def test_restart_check_returns_only_default_profile_pids(self):
        from claude_session_sync.processes import ManagedProcess

        with tempfile.TemporaryDirectory() as directory:
            loaded = replace(config(Path(directory)), target_policy="all-configured-profiles")
            primary = ManagedProcess(1234, ("/usr/bin/true",), loaded.profiles[0].data_root)
            other = ManagedProcess(5678, ("/usr/bin/true",), loaded.profiles[1].data_root)
            for processes, expected in (((primary,), 0), ((primary, other), 1), ((), 0)):
                out = io.StringIO()
                deps = CliDependencies(config_loader=lambda _: loaded, process_probe=FakeProcessProbe(processes))
                self.assertEqual(expected, run(("restart-check", "Work"), dependencies=deps, stdout=out, stderr=io.StringIO()))
                if expected == 0:
                    self.assertEqual([p.pid for p in processes], json.loads(out.getvalue())["pids"])
                else:
                    self.assertEqual("", out.getvalue())
            for target in ("Work", "Personal"):
                deps = CliDependencies(config_loader=lambda _: config(Path(directory)), process_probe=FakeProcessProbe((primary,)))
                self.assertEqual(1, run(("restart-check", target), dependencies=deps, stdout=io.StringIO(), stderr=io.StringIO()))

    def test_switch_rejects_an_unbounded_wait_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            errors = io.StringIO()

            exit_code = run(
                [
                    "--config",
                    str(root / "config.json"),
                    "switch",
                    "Work",
                    "--wait-for-exit",
                    "inf",
                ],
                stdout=io.StringIO(),
                stderr=errors,
            )

            self.assertEqual(exit_code, 2)
            self.assertIn("must be a finite number", errors.getvalue())

    def test_switch_exit_deadline_includes_slow_process_probes(self):
        class SlowProcessProbe(FakeProcessProbe):
            def __init__(self, wait_clock):
                super().__init__()
                self.wait_clock = wait_clock

            def running(self, **kwargs):
                self.calls.append(kwargs)
                self.wait_clock.value += 0.6
                return (object(),)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wait_clock = ManualClock()
            process_probe = SlowProcessProbe(wait_clock)
            out = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: config(root),
                planner_factory=lambda _config: self.fail("planner must not run"),
                process_probe=process_probe,
                monotonic=wait_clock,
                sleeper=wait_clock.sleep,
            )

            exit_code = run(
                [
                    "--config",
                    str(root / "config.json"),
                    "switch",
                    "Work",
                    "--wait-for-exit",
                    "1",
                ],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(out.getvalue(), "state=blocked_app reason=app-running\n")
            self.assertEqual(len(process_probe.calls), 2)
            self.assertAlmostEqual(process_probe.calls[0]["timeout"], 1.0)
            self.assertLess(process_probe.calls[1]["timeout"], 0.4)
            self.assertEqual(wait_clock.sleeps, [0.1])

    def test_switch_refuses_to_sync_or_launch_while_managed_app_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            out = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: config(root),
                planner_factory=lambda _config: self.fail("planner must not run"),
                process_probe=FakeProcessProbe((object(),)),
                launcher=FakeLauncher(events),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "switch", "Work"],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertNotEqual(exit_code, 0)
            self.assertEqual(out.getvalue(), "state=blocked_app reason=app-running\n")
            self.assertEqual(events, [])


class CliRecoveryAndHealthTests(unittest.TestCase):
    def test_rollback_json_reports_only_recovery_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = FakeEngine()
            out = io.StringIO()
            ticks = iter((4.0, 4.006))
            dependencies = CliDependencies(
                config_loader=lambda path: config(root),
                engine_factory=lambda _config: engine,
                clock=lambda: next(ticks),
            )

            exit_code = run(
                [
                    "--config",
                    str(root / "config.json"),
                    "rollback",
                    "run-public",
                    "--json",
                ],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(engine.rolled_back, "run-public")
            self.assertEqual(
                json.loads(out.getvalue()),
                {
                    "bytes": 1,
                    "counts": {"operations": 3},
                    "duration_ms": 6,
                    "run_id": "run-public",
                    "state": "rolled_back",
                },
            )

    def test_status_and_doctor_are_read_only_aggregate_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            for profile in loaded.profiles:
                (
                    profile.data_root / "claude-code-sessions" / "account" / "workspace"
                ).mkdir(parents=True)
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                process_probe=FakeProcessProbe(),
            )

            status_out = io.StringIO()
            doctor_out = io.StringIO()
            self.assertEqual(
                run(
                    ["--config", str(root / "config.json"), "status", "--json"],
                    dependencies=dependencies,
                    stdout=status_out,
                    stderr=io.StringIO(),
                ),
                0,
            )
            self.assertEqual(
                json.loads(status_out.getvalue()),
                {
                    "bytes": 0,
                    "counts": {
                        "abandoned_preparations": 0,
                        "layout_failures": 0,
                        "profiles": 2,
                        "routine_failures": 0,
                        "running_processes": 0,
                        "watcher_failures": 0,
                    },
                    "duration_ms": 0,
                    "state": "idle",
                    "progress": "not-synced-yet",
                    "last_success_at": None,
                    "next_action": "run-sync",
                },
            )
            self.assertEqual(
                run(
                    ["--config", str(root / "config.json"), "doctor", "--json"],
                    dependencies=dependencies,
                    stdout=doctor_out,
                    stderr=io.StringIO(),
                ),
                0,
            )
            self.assertEqual(json.loads(doctor_out.getvalue())["state"], "healthy")

    def test_status_reports_abandoned_preparations_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            (loaded.state_dir / "preparations" / "crash-copy").mkdir(parents=True)
            output = io.StringIO()

            exit_code = run(
                ["--config", str(root / "config.json"), "status", "--json"],
                dependencies=CliDependencies(
                    config_loader=lambda path: loaded,
                    process_probe=FakeProcessProbe(),
                ),
                stdout=output,
                stderr=io.StringIO(),
            )

            payload = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual(1, payload["counts"]["abandoned_preparations"])
            self.assertEqual("needs-attention", payload["progress"])
            self.assertEqual("inspect-preparations", payload["next_action"])

    def test_unknown_command_is_nonzero(self):
        error = io.StringIO()

        exit_code = run(["teleport"], stdout=io.StringIO(), stderr=error)

        self.assertNotEqual(exit_code, 0)
        self.assertIn("invalid choice", error.getvalue())

    def test_doctor_rejects_missing_profile_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            output = io.StringIO()
            exit_code = run(
                ["--config", str(root / "config.json"), "doctor", "--json"],
                dependencies=CliDependencies(config_loader=lambda path: loaded),
                stdout=output,
                stderr=io.StringIO(),
            )

            self.assertNotEqual(exit_code, 0)
            self.assertEqual("unhealthy", json.loads(output.getvalue())["state"])


class CliApprovalTests(unittest.TestCase):
    def test_approval_dry_run_then_apply_pins_exact_current_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_root = root / "Claude"
            (data_root / "claude-code-sessions" / "account" / "workspace").mkdir(
                parents=True
            )
            config_path = root / "config.json"
            document = {
                "version": 1,
                "approved_targets": [],
                "profiles": [
                    {
                        "name": "Work",
                        "data_root": str(data_root),
                        "launch_command": ["/usr/bin/true"],
                        "is_default": True,
                    }
                ],
                "state_dir": str(root / "state"),
                "retention": 5,
                "acknowledge_cross_profile_copy": False,
                "acknowledge_cross_account_copy": False,
                "claude_executable": "/usr/bin/true",
            }
            config_path.write_text(json.dumps(document), encoding="utf-8")

            dry_output = io.StringIO()
            self.assertEqual(
                0,
                run(
                    [
                        "--config",
                        str(config_path),
                        "approve-current-targets",
                        "--dry-run",
                    ],
                    stdout=dry_output,
                    stderr=io.StringIO(),
                ),
            )
            self.assertEqual(
                [], json.loads(config_path.read_text())["approved_targets"]
            )

            self.assertEqual(
                0,
                run(
                    [
                        "--config",
                        str(config_path),
                        "approve-current-targets",
                        "--apply",
                    ],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                ),
            )
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [
                    {
                        "profile": "Work",
                        "account": "account",
                        "workspace": "workspace",
                    }
                ],
                updated["approved_targets"],
            )


class CliConfigureTests(unittest.TestCase):
    def test_setup_prepares_config_then_calls_atomic_installer_once(self):
        class FakeInstaller:
            def __init__(self, default_data):
                self.default_data = default_data
                self.calls = []

            def default_config_data(self):
                return self.default_data

            def setup(self, *, dry_run, config_data=None, before_activation=None):
                self.calls.append((dry_run, config_data))
                return InstallReport(
                    "planned" if dry_run else "installed",
                    (InstallAction("create", Path("/private/not-output")),),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = {
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
            fake = FakeInstaller((json.dumps(document) + "\n").encode())
            config_path = root / "missing" / "config.json"
            output = io.StringIO()

            exit_code = run(
                [
                    "--config", str(config_path), "setup",
                    "--automatic-targets",
                    "--sync-layout", "--sync-routines", "--dry-run",
                ],
                dependencies=CliDependencies(installer_factory=lambda path: fake),
                stdout=output,
                stderr=io.StringIO(),
            )

            self.assertEqual(0, exit_code)
            self.assertEqual(1, len(fake.calls))
            self.assertTrue(fake.calls[0][0])
            desired = json.loads(fake.calls[0][1])
            self.assertEqual(
                dict(
                    document,
                    target_policy="logins",
                    acknowledge_cross_account_copy=True,
                    sync_sidebar_layout=True,
                    sync_code_routines=True,
                ),
                desired,
            )
            self.assertEqual(
                "state=planned counts={'actions': 1, 'backups': 0, 'changes': 4, "
                "'automatic_targets': 1, 'layout_sync': 1, 'routine_sync': 1} "
                "bytes=0 duration_ms=0\n",
                output.getvalue(),
            )
            self.assertFalse(config_path.exists())

    def personal_era_config(self, root: Path, *, personal_enabled: bool) -> dict:
        """A config from a version that generated a Personal profile."""

        return {
            "version": 1,
            "approved_targets": [
                {"profile": "Personal", "account": "account", "workspace": "workspace"},
            ],
            "profiles": [
                {
                    "name": "Work",
                    "data_root": str(root / "Claude"),
                    "launch_command": ["/usr/bin/true"],
                    "is_default": True,
                },
                {
                    "name": "Personal",
                    "data_root": str(root / "Claude-Personal"),
                    "launch_command": ["/usr/bin/true"],
                    "enabled": personal_enabled,
                    "is_default": False,
                },
            ],
            "state_dir": str(root / "state"),
            "retention": 5,
            "acknowledge_cross_profile_copy": personal_enabled,
            "acknowledge_cross_account_copy": False,
            "target_policy": "approved-only",
            "claude_executable": "/usr/bin/true",
        }

    def test_configure_enables_automatic_targets_and_drops_the_old_personal_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            document = self.personal_era_config(root, personal_enabled=False)
            config_path.write_text(json.dumps(document), encoding="utf-8")
            before = config_path.read_bytes()

            self.assertEqual(
                0,
                run(
                    ["--config", str(config_path), "configure", "--automatic-targets", "--dry-run"],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                ),
            )
            self.assertEqual(before, config_path.read_bytes())

            output = io.StringIO()
            self.assertEqual(
                0,
                run(
                    ["--config", str(config_path), "configure", "--automatic-targets", "--apply"],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
            )
            self.assertEqual(
                dict(
                    document,
                    approved_targets=[],
                    profiles=document["profiles"][:1],
                    target_policy="logins",
                    acknowledge_cross_account_copy=True,
                ),
                json.loads(config_path.read_text(encoding="utf-8")),
            )
            self.assertIn("state=configured", output.getvalue())

    def test_configure_drops_an_enabled_personal_profile_and_enables_layout_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            document = self.personal_era_config(root, personal_enabled=True)
            config_path.write_text(json.dumps(document), encoding="utf-8")

            exit_code = run(
                ["--config", str(config_path), "configure", "--sync-layout", "--sync-routines", "--apply"],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(0, exit_code)
            self.assertEqual(
                dict(
                    document,
                    approved_targets=[],
                    profiles=document["profiles"][:1],
                    sync_sidebar_layout=True,
                    sync_code_routines=True,
                ),
                json.loads(config_path.read_text(encoding="utf-8")),
            )

    def test_dropping_the_personal_profile_keeps_a_third_profile_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            document = self.personal_era_config(root, personal_enabled=True)
            other = {
                "name": "Other",
                "data_root": str(root / "Claude-Other"),
                "launch_command": ["/usr/bin/true"],
                "is_default": False,
            }
            document["profiles"].append(other)
            config_path.write_text(json.dumps(document), encoding="utf-8")
            errors = io.StringIO()

            exit_code = run(
                ["--config", str(config_path), "configure", "--apply"],
                stdout=io.StringIO(),
                stderr=errors,
            )

            self.assertEqual((0, ""), (exit_code, errors.getvalue()))
            self.assertEqual(
                dict(document, approved_targets=[], profiles=[document["profiles"][0], other]),
                json.loads(config_path.read_text(encoding="utf-8")),
            )

    def test_setup_names_a_malformed_profile_before_it_drops_personal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            document = self.personal_era_config(root, personal_enabled=False)
            document["profiles"].append("not-a-profile")
            config_path.write_text(json.dumps(document), encoding="utf-8")
            errors = io.StringIO()

            exit_code = run(
                ["--config", str(config_path), "setup", "--dry-run"],
                dependencies=CliDependencies(
                    installer_factory=lambda _path: SimpleNamespace(
                        setup=lambda **_options: self.fail("an invalid config must not install")
                    )
                ),
                stdout=io.StringIO(),
                stderr=errors,
            )

            self.assertEqual(
                (1, "claude-session-sync: profile 1 must be a JSON object\n"),
                (exit_code, errors.getvalue()),
            )


class CliLayoutTests(unittest.TestCase):
    def test_adopt_current_sidebar_requires_one_default_profile_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            one_profile = "exactly one Claude profile enabled, the default one"
            unsafe_configs = (
                (replace(loaded, sync_sidebar_layout=True), one_profile),
                (
                    replace(
                        loaded,
                        profiles=(replace(loaded.profiles[1], is_default=False),),
                        sync_sidebar_layout=True,
                    ),
                    one_profile,
                ),
                (
                    replace(loaded, profiles=loaded.profiles[:1], sync_sidebar_layout=False),
                    "pins and groups do not sync",
                ),
            )
            for unsafe, reason in unsafe_configs:
                with self.subTest(reason=reason, profiles=len(unsafe.profiles)):
                    planner_calls = []
                    deps = CliDependencies(
                        config_loader=lambda path, value=unsafe: value,
                        planner_factory=lambda value: planner_calls.append(value),
                    )
                    errors = io.StringIO()
                    self.assertNotEqual(
                        0,
                        run(
                            ["sync", "--adopt-current-sidebar"],
                            dependencies=deps,
                            stdout=io.StringIO(),
                            stderr=errors,
                        ),
                    )
                    self.assertEqual([], planner_calls)
                    self.assertIn(reason, errors.getvalue())

class CliInstallTests(unittest.TestCase):
    def test_install_modes_do_not_require_an_existing_config(self):
        class FakeInstaller:
            def __init__(self):
                self.calls = []

            def install(self, *, dry_run):
                self.calls.append(("install", dry_run))
                return InstallReport(
                    "planned" if dry_run else "installed",
                    (InstallAction("create", Path("/private/not-output")),),
                )

            def uninstall(self, *, dry_run):
                self.calls.append(("uninstall", dry_run))
                return InstallReport("uninstalled", ())

        fake = FakeInstaller()
        dependencies = CliDependencies(
            config_loader=lambda path: self.fail("install must not load config"),
            installer_factory=lambda path: fake,
        )
        out = io.StringIO()

        self.assertEqual(
            run(
                ["--config", "/tmp/not-created.json", "install", "--dry-run"],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            ),
            0,
        )
        self.assertEqual(fake.calls, [("install", True)])
        self.assertEqual(
            out.getvalue(),
            "state=planned counts={'actions': 1, 'backups': 0} bytes=0 duration_ms=0\n",
        )
        self.assertNotIn("private", out.getvalue())

    def test_install_requires_exactly_one_mode(self):
        error = io.StringIO()

        exit_code = run(["install"], stdout=io.StringIO(), stderr=error)

        self.assertNotEqual(exit_code, 0)
        self.assertIn("required", error.getvalue())


if __name__ == "__main__":
    unittest.main()
