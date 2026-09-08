import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

from claude_session_sync.cli import CliDependencies, run
from claude_session_sync.config import ApprovedTarget, Config
from claude_session_sync.installer import InstallAction, InstallReport
from claude_session_sync.model import (
    InvalidReplica,
    Operation,
    Plan,
    Profile,
    RecoveryReceipt,
    RunReceipt,
)


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


class FakePlanner:
    def __init__(self, plan):
        self.result = plan
        self.requests = []

    def plan(self, request):
        self.requests.append(request)
        return self.result


class FakeEngine:
    def __init__(self, receipt):
        self.receipt = receipt
        self.applied = []

    def apply(self, plan):
        self.applied.append(plan)
        return self.receipt

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
    ambiguous_assignments = 1


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


class CliPlanTests(unittest.TestCase):
    def test_plan_json_reports_safe_aggregate_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            operation = Operation(
                kind="create",
                session_id="private-session-id",
                source=root / "acct-raw" / "chat-content.json",
                destination=root / "other-account" / "chat-content.json",
                source_digest="a" * 64,
                destination_digest_or_none=None,
                size=125,
            )
            planned = Plan(1, "digest", (operation,), (), (), "plan-safe", 125)
            planner = FakePlanner(planned)
            out = io.StringIO()
            err = io.StringIO()
            ticks = iter((5.0, 5.012))
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: planner,
                clock=lambda: next(ticks),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "plan", "--json"],
                dependencies=dependencies,
                stdout=out,
                stderr=err,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(err.getvalue(), "")
            payload = json.loads(out.getvalue())
            self.assertEqual(
                payload,
                {
                    "bytes": 125,
                    "counts": {
                        "conflicts": 0,
                        "invalid_replicas": 0,
                        "operations": 1,
                    },
                    "duration_ms": 12,
                    "plan_id": "plan-safe",
                    "state": "planned",
                },
            )
            self.assertNotIn("private-session-id", out.getvalue())
            self.assertNotIn("acct-raw", out.getvalue())
            self.assertIs(planner.requests[0].config, loaded)

    def test_plan_reports_safe_recovery_action_for_new_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_path = root / "private-account" / "private-workspace"
            planned = Plan(
                1,
                "digest",
                (),
                (),
                (
                    InvalidReplica(
                        private_path,
                        "target namespace is not approved",
                    ),
                ),
                "plan-blocked",
                0,
            )
            out = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: config(root),
                planner_factory=lambda _config: FakePlanner(planned),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "plan", "--json"],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            payload = json.loads(out.getvalue())
            self.assertEqual(1, exit_code)
            self.assertEqual("blocked_invalid", payload["state"])
            self.assertEqual(
                "approve-targets-or-enable-automatic-targets",
                payload["next_action"],
            )
            self.assertNotIn("private-account", out.getvalue())


class CliSyncTests(unittest.TestCase):
    def test_sync_json_applies_plan_and_reports_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-7", 0)
            planner = FakePlanner(planned)
            receipt = RunReceipt("run-9", "committed", "plan-7", 2, 450)
            engine = FakeEngine(receipt)
            out = io.StringIO()
            err = io.StringIO()
            ticks = iter((10.0, 10.005, 10.012))
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: planner,
                engine_factory=lambda _config: engine,
                clock=lambda: next(ticks),
                process_probe=FakeProcessProbe(),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "sync", "--json"],
                dependencies=dependencies,
                stdout=out,
                stderr=err,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(engine.applied, [planned])
            self.assertEqual(
                json.loads(out.getvalue()),
                {
                    "bytes": 450,
                    "counts": {"operations": 2},
                    "duration_ms": 12,
                    "plan_id": "plan-7",
                    "run_id": "run-9",
                    "state": "committed",
                    "progress": "finished",
                },
            )

    def test_sync_reports_recovery_pending_without_exception_details(self):
        from claude_session_sync.transaction import RecoveryPendingError

        class RecoveryPlanner:
            def plan(self, request):
                raise RecoveryPendingError("private journal path and run id")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = io.StringIO()
            exit_code = run(
                ["--config", str(root / "config.json"), "sync", "--json"],
                dependencies=CliDependencies(
                    config_loader=lambda path: config(root),
                    planner_factory=lambda loaded: RecoveryPlanner(),
                    process_probe=FakeProcessProbe(),
                ),
                stdout=out,
                stderr=io.StringIO(),
            )
            payload = json.loads(out.getvalue())
            self.assertEqual(1, exit_code)
            self.assertEqual("recovery-pending", payload["reason"])
            self.assertEqual("recovery-pending-error", payload["error_type"])
            self.assertEqual("run-doctor", payload["next_action"])
            self.assertNotIn("private", out.getvalue())

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

    def test_sync_masks_journal_and_process_timeout_details(self):
        from claude_session_sync.journal import JournalError

        cases = (
            (JournalError("private manifest path"), "invalid-journal", "journal-error", "run-doctor"),
            (subprocess.TimeoutExpired(["ps", "private"], 2), "process-inspection-timeout", "process-timeout", "retry-sync"),
        )
        for error, reason, error_type, next_action in cases:
            with self.subTest(error_type=error_type), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                out = io.StringIO()

                class BrokenPlanner:
                    def plan(self, request):
                        raise error

                exit_code = run(
                    ["--config", str(root / "config.json"), "sync", "--json"],
                    dependencies=CliDependencies(
                        config_loader=lambda path: config(root),
                        planner_factory=lambda loaded: BrokenPlanner(),
                        process_probe=FakeProcessProbe(),
                    ),
                    stdout=out,
                    stderr=io.StringIO(),
                )
                payload = json.loads(out.getvalue())
                self.assertEqual(1, exit_code)
                self.assertEqual(reason, payload["reason"])
                self.assertEqual(error_type, payload["error_type"])
                self.assertEqual(next_action, payload["next_action"])
                self.assertNotIn("private", out.getvalue())


class CliAutomaticTests(unittest.TestCase):
    def test_auto_reports_end_to_end_sync_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-auto", 0)
            receipt = RunReceipt("run-auto", "committed", "plan-auto", 2, 450)
            ticks = iter((20.0, 20.012))
            out = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                process_probe=FakeProcessProbe(),
                clock=lambda: next(ticks),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "auto"],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                out.getvalue(),
                "bytes=450 counts={'operations': 2} duration_ms=12 "
                "plan_id=plan-auto run_id=run-auto state=committed progress=finished\n",
            )

    def test_auto_succeeds_with_explicit_skip_while_app_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: config(root),
                planner_factory=lambda _config: self.fail("planner must not run"),
                process_probe=FakeProcessProbe((object(),)),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "auto"],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(out.getvalue(), "state=skipped reason=app-running progress=waiting-for-Claude\n")

    def test_auto_succeeds_with_explicit_skip_when_transaction_is_busy(self):
        class TransactionBusyError(Exception):
            pass

        class BusyEngine:
            def apply(self, plan):
                raise TransactionBusyError("lock contains private path")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-safe", 0)
            ticks = iter((1.0, 1.001))
            out = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: BusyEngine(),
                process_probe=FakeProcessProbe(),
                clock=lambda: next(ticks),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "auto"],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(out.getvalue(), "state=skipped reason=busy progress=waiting-for-sync\n")


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

    def test_switch_can_wait_for_a_managed_app_to_finish_exiting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-safe", 0)
            receipt = RunReceipt(None, "noop", "plan-safe", 0, 0)
            process_probe = SequencedProcessProbe(((object(),), (object(),), ()))
            wait_clock = ManualClock()
            events = []
            ticks = iter((2.0, 2.001, 2.003))
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                process_probe=process_probe,
                launcher=FakeLauncher(events),
                clock=lambda: next(ticks),
                monotonic=wait_clock,
                sleeper=wait_clock.sleep,
                launch_confirmation_timeout=0,
            )

            exit_code = run(
                [
                    "--config",
                    str(root / "config.json"),
                    "switch",
                    "Personal",
                    "--wait-for-exit",
                    "1",
                ],
                dependencies=dependencies,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(process_probe.calls), 3)
            self.assertEqual(wait_clock.sleeps, [0.1, 0.1])
            self.assertEqual(events[-1], ("launch", loaded.profiles[1].launch_command))

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

    def test_only_one_concurrent_switch_handoff_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-safe", 0)
            receipt = RunReceipt(None, "noop", "plan-safe", 0, 0)
            planner_entered = threading.Event()
            release_planner = threading.Event()
            launches = []
            first_result = []

            class BlockingPlanner(FakePlanner):
                def plan(self, request):
                    planner_entered.set()
                    if not release_planner.wait(timeout=2):
                        raise TimeoutError("test did not release planner")
                    return super().plan(request)

            first_dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: BlockingPlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                process_probe=FakeProcessProbe(),
                launcher=FakeLauncher(launches),
                clock=lambda: 2.0,
                launch_confirmation_timeout=0,
            )
            second_dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: self.fail(
                    "second planner must not run"
                ),
                process_probe=FakeProcessProbe(),
                launcher=FakeLauncher(launches),
                launch_confirmation_timeout=0,
            )

            first = threading.Thread(
                target=lambda: first_result.append(
                    run(
                        [
                            "--config",
                            str(root / "config.json"),
                            "switch",
                            "Work",
                        ],
                        dependencies=first_dependencies,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )
                )
            )
            first.start()
            self.assertTrue(planner_entered.wait(timeout=2))
            second_out = io.StringIO()
            second_result = run(
                [
                    "--config",
                    str(root / "config.json"),
                    "switch",
                    "Personal",
                ],
                dependencies=second_dependencies,
                stdout=second_out,
                stderr=io.StringIO(),
            )
            release_planner.set()
            first.join(timeout=2)

            self.assertFalse(first.is_alive())
            self.assertEqual(first_result, [0])
            self.assertEqual(second_result, 1)
            self.assertEqual(
                second_out.getvalue(),
                "state=blocked_switch reason=handoff-running\n",
            )
            self.assertEqual(len(launches), 1)

    def test_switch_confirms_the_selected_profile_before_success(self):
        class RunningProfile:
            def __init__(self, data_root):
                self.user_data_dir = data_root

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-safe", 0)
            receipt = RunReceipt(None, "noop", "plan-safe", 0, 0)
            process_probe = SequencedProcessProbe(
                ((), (RunningProfile(loaded.profiles[0].data_root),))
            )
            wait_clock = ManualClock()
            launches = []
            ticks = iter((2.0, 2.001, 2.003))
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                process_probe=process_probe,
                launcher=FakeLauncher(launches),
                clock=lambda: next(ticks),
                monotonic=wait_clock,
                sleeper=wait_clock.sleep,
                launch_confirmation_timeout=1,
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "switch", "Work"],
                dependencies=dependencies,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(launches), 1)
            self.assertFalse((loaded.state_dir / "launch-pending.json").exists())

    def test_unconfirmed_launch_fails_closed_and_blocks_another_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-safe", 0)
            receipt = RunReceipt(None, "noop", "plan-safe", 0, 0)
            wait_clock = ManualClock()
            launches = []
            planner = FakePlanner(planned)
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: planner,
                engine_factory=lambda _config: FakeEngine(receipt),
                process_probe=FakeProcessProbe(),
                launcher=FakeLauncher(launches),
                clock=lambda: 2.0,
                monotonic=wait_clock,
                sleeper=wait_clock.sleep,
                launch_confirmation_timeout=0.2,
            )
            first_out = io.StringIO()

            first_result = run(
                ["--config", str(root / "config.json"), "switch", "Work"],
                dependencies=dependencies,
                stdout=first_out,
                stderr=io.StringIO(),
            )
            second_out = io.StringIO()
            second_result = run(
                ["--config", str(root / "config.json"), "switch", "Personal"],
                dependencies=dependencies,
                stdout=second_out,
                stderr=io.StringIO(),
            )

            self.assertEqual(first_result, 1)
            self.assertEqual(
                first_out.getvalue(),
                "state=launch_unconfirmed reason=timeout\n",
            )
            self.assertTrue((loaded.state_dir / "launch-pending.json").is_file())
            self.assertEqual(second_result, 1)
            self.assertEqual(
                second_out.getvalue(),
                "state=blocked_switch reason=launch-unconfirmed\n",
            )
            self.assertEqual(len(launches), 1)
            self.assertEqual(len(planner.requests), 1)

    def test_wrong_profile_confirmation_fails_closed(self):
        class RunningProfile:
            def __init__(self, data_root):
                self.user_data_dir = data_root

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-safe", 0)
            receipt = RunReceipt(None, "noop", "plan-safe", 0, 0)
            process_probe = SequencedProcessProbe(
                ((), (RunningProfile(loaded.profiles[1].data_root),))
            )
            wait_clock = ManualClock()
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                process_probe=process_probe,
                launcher=FakeLauncher([]),
                clock=lambda: 2.0,
                monotonic=wait_clock,
                sleeper=wait_clock.sleep,
                launch_confirmation_timeout=1,
            )
            out = io.StringIO()

            exit_code = run(
                ["--config", str(root / "config.json"), "switch", "Work"],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(
                out.getvalue(),
                "state=launch_unconfirmed reason=wrong-profile\n",
            )
            self.assertTrue((loaded.state_dir / "launch-pending.json").is_file())

            second_out = io.StringIO()
            second_result = run(
                ["--config", str(root / "config.json"), "switch", "Personal"],
                dependencies=dependencies,
                stdout=second_out,
                stderr=io.StringIO(),
            )
            self.assertEqual(second_result, 1)
            self.assertEqual(
                second_out.getvalue(),
                "state=blocked_switch reason=launch-unconfirmed\n",
            )
            self.assertTrue((loaded.state_dir / "launch-pending.json").is_file())

    def test_guard_for_a_removed_profile_stays_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            loaded.state_dir.mkdir(parents=True)
            guard = loaded.state_dir / "launch-pending.json"
            guard.write_text(
                json.dumps({"profile": "Removed", "version": 1}) + "\n",
                encoding="utf-8",
            )
            out = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: self.fail("planner must not run"),
                process_probe=FakeProcessProbe(),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "switch", "Work"],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(
                out.getvalue(),
                "state=blocked_switch reason=launch-unconfirmed\n",
            )
            self.assertTrue(guard.is_file())

    def test_switch_waits_for_the_automatic_writer_then_retries(self):
        class TransactionBusyError(Exception):
            pass

        class BusyOnceEngine(FakeEngine):
            def apply(self, plan):
                self.applied.append(plan)
                if len(self.applied) == 1:
                    raise TransactionBusyError("watcher owns the writer lock")
                return self.receipt

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-safe", 0)
            receipt = RunReceipt(None, "noop", "plan-safe", 0, 0)
            engine = BusyOnceEngine(receipt)
            sleeps = []
            events = []
            ticks = iter((2.0, 2.001, 2.003))
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: engine,
                process_probe=FakeProcessProbe(),
                launcher=FakeLauncher(events),
                clock=lambda: next(ticks),
                sleeper=sleeps.append,
                launch_confirmation_timeout=0,
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "switch", "Work"],
                dependencies=dependencies,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(engine.applied), 2)
            self.assertEqual(sleeps, [0.1])
            self.assertEqual(events[-1], ("launch", loaded.profiles[0].launch_command))

    def test_switch_replans_if_the_watcher_commits_first(self):
        class RevalidationError(Exception):
            pass

        class SequencedPlanner:
            def __init__(self, plans):
                self.plans = list(plans)
                self.requests = []

            def plan(self, request):
                self.requests.append(request)
                return self.plans.pop(0)

        class RevalidateOnceEngine:
            def __init__(self):
                self.applied = []

            def apply(self, plan):
                self.applied.append(plan)
                if len(self.applied) == 1:
                    raise RevalidationError("watcher changed the destination")
                return RunReceipt(None, "noop", plan.plan_id, 0, 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            first = Plan(1, "digest", (), (), (), "plan-before-auto", 0)
            second = Plan(1, "digest", (), (), (), "plan-after-auto", 0)
            planner = SequencedPlanner((first, second))
            engine = RevalidateOnceEngine()
            events = []
            ticks = iter((2.0, 2.001, 2.003))
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: planner,
                engine_factory=lambda _config: engine,
                process_probe=FakeProcessProbe(),
                launcher=FakeLauncher(events),
                clock=lambda: next(ticks),
                sleeper=lambda _seconds: None,
                launch_confirmation_timeout=0,
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "switch", "Work"],
                dependencies=dependencies,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                [plan.plan_id for plan in engine.applied],
                [
                    "plan-before-auto",
                    "plan-after-auto",
                ],
            )
            self.assertEqual(events[-1], ("launch", loaded.profiles[0].launch_command))

    def test_switch_syncs_before_launching_selected_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            planned = Plan(1, "digest", (), (), (), "plan-safe", 0)
            events = []

            class OrderedEngine(FakeEngine):
                def apply(self, plan):
                    events.append(("apply", plan.plan_id))
                    return super().apply(plan)

            receipt = RunReceipt(None, "noop", "plan-safe", 0, 0)
            engine = OrderedEngine(receipt)
            ticks = iter((2.0, 2.001, 2.003))
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: engine,
                process_probe=FakeProcessProbe(),
                launcher=FakeLauncher(events),
                clock=lambda: next(ticks),
                launch_confirmation_timeout=0,
            )

            out = io.StringIO()
            exit_code = run(
                ["--config", str(root / "config.json"), "switch", "Personal", "--json"],
                dependencies=dependencies,
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(events[0], ("apply", "plan-safe"))
            self.assertEqual(events[1], ("launch", loaded.profiles[1].launch_command))
            self.assertEqual("finished", json.loads(out.getvalue())["progress"])

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
    def test_clear_launch_guard_supports_dry_run_then_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            loaded.state_dir.mkdir(parents=True)
            guard = loaded.state_dir / "launch-pending.json"
            guard.write_text(
                json.dumps({"profile": "Work", "version": 1}) + "\n",
                encoding="utf-8",
            )
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                process_probe=FakeProcessProbe(),
            )
            dry_run_out = io.StringIO()

            self.assertEqual(
                run(
                    [
                        "--config",
                        str(root / "config.json"),
                        "clear-launch-guard",
                        "--dry-run",
                    ],
                    dependencies=dependencies,
                    stdout=dry_run_out,
                    stderr=io.StringIO(),
                ),
                0,
            )
            self.assertTrue(guard.exists())
            self.assertEqual(
                dry_run_out.getvalue(),
                "state=planned counts={'guards': 1}\n",
            )

            apply_out = io.StringIO()
            self.assertEqual(
                run(
                    [
                        "--config",
                        str(root / "config.json"),
                        "clear-launch-guard",
                        "--apply",
                    ],
                    dependencies=dependencies,
                    stdout=apply_out,
                    stderr=io.StringIO(),
                ),
                0,
            )
            self.assertFalse(guard.exists())
            self.assertEqual(
                apply_out.getvalue(),
                "state=cleared counts={'guards': 1}\n",
            )

    def test_rollback_json_reports_only_recovery_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = FakeEngine(None)
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
                        "launch_guards": 0,
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

            def setup(self, *, dry_run, config_data=None):
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
                    {
                        "name": "Personal",
                        "data_root": str(root / "Claude-Personal"),
                        "launch_command": ["/usr/bin/true"],
                        "enabled": False,
                        "is_default": False,
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
                    "--automatic-targets", "--enable-personal",
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
            self.assertEqual("all-configured-profiles", desired["target_policy"])
            self.assertTrue(desired["profiles"][1]["enabled"])
            self.assertTrue(desired["sync_sidebar_layout"])
            self.assertTrue(desired["sync_code_routines"])
            self.assertFalse(config_path.exists())

    def test_configure_enables_automatic_targets_and_personal_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
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
                    {
                        "name": "Personal",
                        "data_root": str(root / "Claude-Personal"),
                        "launch_command": ["/usr/bin/true"],
                        "enabled": False,
                        "is_default": False,
                    },
                ],
                "state_dir": str(root / "state"),
                "retention": 5,
                "acknowledge_cross_profile_copy": False,
                "acknowledge_cross_account_copy": False,
                "target_policy": "approved-only",
                "claude_executable": "/usr/bin/true",
            }
            config_path.write_text(json.dumps(document), encoding="utf-8")

            self.assertEqual(
                0,
                run(
                    [
                        "--config",
                        str(config_path),
                        "configure",
                        "--automatic-targets",
                        "--enable-personal",
                        "--dry-run",
                    ],
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                ),
            )
            self.assertFalse(
                json.loads(config_path.read_text(encoding="utf-8"))["profiles"][1][
                    "enabled"
                ]
            )

            output = io.StringIO()
            self.assertEqual(
                0,
                run(
                    [
                        "--config",
                        str(config_path),
                        "configure",
                        "--automatic-targets",
                        "--enable-personal",
                        "--apply",
                    ],
                    stdout=output,
                    stderr=io.StringIO(),
                ),
            )
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual("all-configured-profiles", updated["target_policy"])
            self.assertTrue(updated["acknowledge_cross_account_copy"])
            self.assertTrue(updated["acknowledge_cross_profile_copy"])
            self.assertTrue(updated["profiles"][1]["enabled"])
            self.assertIn("state=configured", output.getvalue())

    def test_configure_disables_personal_and_enables_layout_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
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
                    {
                        "name": "Personal",
                        "data_root": str(root / "Claude-Personal"),
                        "launch_command": ["/usr/bin/true"],
                        "enabled": True,
                        "is_default": False,
                    },
                ],
                "state_dir": str(root / "state"),
                "retention": 5,
                "acknowledge_cross_profile_copy": True,
                "acknowledge_cross_account_copy": True,
                "target_policy": "all-configured-profiles",
                "sync_sidebar_layout": False,
                "claude_executable": "/usr/bin/true",
            }
            config_path.write_text(json.dumps(document), encoding="utf-8")

            exit_code = run(
                [
                    "--config",
                    str(config_path),
                    "configure",
                    "--disable-personal",
                    "--sync-layout",
                    "--sync-routines",
                    "--apply",
                ],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            self.assertEqual(0, exit_code)
            updated = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(updated["profiles"][1]["enabled"])
            self.assertFalse(updated["acknowledge_cross_profile_copy"])
            self.assertTrue(updated["sync_sidebar_layout"])
            self.assertTrue(updated["sync_code_routines"])


class CliRoutineTests(unittest.TestCase):
    def test_sync_reports_routines_without_changing_chat_receipt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = replace(config(root), sync_code_routines=True)
            planned = Plan(1, "digest", (), (), (), "plan-routines", 0)
            receipt = RunReceipt("run-routines", "committed", "plan-routines", 0, 0)
            output = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                routine_factory=lambda _config: FakeRoutine(),
                process_probe=FakeProcessProbe(),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "sync", "--json"],
                dependencies=dependencies,
                stdout=output,
                stderr=io.StringIO(),
            )

            payload = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual("committed", payload["state"])
            self.assertEqual("synced", payload["routines"]["state"])
            self.assertEqual(4, payload["routines"]["tasks"])

    def test_routine_failure_does_not_change_committed_chat_result(self):
        class BrokenRoutine:
            def sync(self):
                raise RuntimeError("unexpected routine adapter failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = replace(config(root), sync_code_routines=True)
            planned = Plan(1, "digest", (), (), (), "plan-routines", 0)
            receipt = RunReceipt("run-routines", "committed", "plan-routines", 0, 0)
            output = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                routine_factory=lambda _config: BrokenRoutine(),
                process_probe=FakeProcessProbe(),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "sync", "--json"],
                dependencies=dependencies,
                stdout=output,
                stderr=io.StringIO(),
            )

            payload = json.loads(output.getvalue())
            self.assertEqual(1, exit_code)
            self.assertEqual("needs-attention", payload["progress"])
            self.assertEqual("committed", payload["state"])
            self.assertEqual(
                {"state": "skipped", "reason": "routine-error"},
                payload["routines"],
            )


class CliLayoutTests(unittest.TestCase):
    def test_explicit_current_sidebar_option_reaches_only_layout_adapter(self):
        calls = []

        class RecordingLayout:
            def sync(self, *, prefer_current_sidebar=False):
                calls.append(prefer_current_sidebar)
                return FakeLayoutReceipt()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            loaded = replace(loaded, profiles=loaded.profiles[:1], sync_sidebar_layout=True, sync_code_routines=True)
            planned = Plan(1, "digest", (), (), (), "plan-layout", 0)
            receipt = RunReceipt("run-layout", "committed", "plan-layout", 0, 0)
            deps = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                layout_factory=lambda _config: RecordingLayout(),
                routine_factory=lambda _config: FakeRoutine(),
                process_probe=FakeProcessProbe(),
            )
            output = io.StringIO()
            self.assertEqual(0, run(
                ["sync", "--prefer-current-sidebar", "--json"],
                dependencies=deps, stdout=output, stderr=io.StringIO(),
            ))
            self.assertEqual([True], calls)
            self.assertEqual("synced", json.loads(output.getvalue())["routines"]["state"])

    def test_current_sidebar_resolution_requires_one_profile_and_layout_sync_before_any_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = config(root)
            for unsafe in (
                replace(loaded, sync_sidebar_layout=True),
                replace(loaded, profiles=loaded.profiles[:1], sync_sidebar_layout=False),
            ):
                with self.subTest(config=unsafe):
                    planner_calls = []
                    deps = CliDependencies(
                        config_loader=lambda path: unsafe,
                        planner_factory=lambda config: planner_calls.append(config),
                    )
                    errors = io.StringIO()
                    self.assertNotEqual(0, run(
                        ["sync", "--prefer-current-sidebar"], dependencies=deps,
                        stdout=io.StringIO(), stderr=errors,
                    ))
                    self.assertEqual([], planner_calls)
                    self.assertIn("exactly one profile", errors.getvalue())

    def test_sync_reports_layout_without_changing_chat_receipt_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = replace(config(root), sync_sidebar_layout=True)
            planned = Plan(1, "digest", (), (), (), "plan-layout", 0)
            receipt = RunReceipt("run-layout", "committed", "plan-layout", 0, 0)
            output = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                layout_factory=lambda _config: FakeLayout(),
                process_probe=FakeProcessProbe(),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "sync", "--json"],
                dependencies=dependencies,
                stdout=output,
                stderr=io.StringIO(),
            )

            payload = json.loads(output.getvalue())
            self.assertEqual(0, exit_code)
            self.assertEqual("committed", payload["state"])
            self.assertEqual("synced", payload["layout"]["state"])
            self.assertEqual(1, payload["layout"]["ambiguous_assignments"])

    def test_layout_failure_does_not_change_a_committed_chat_result(self):
        class BrokenLayout:
            def sync(self):
                raise RuntimeError("unexpected adapter failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = replace(config(root), sync_sidebar_layout=True)
            planned = Plan(1, "digest", (), (), (), "plan-layout", 0)
            receipt = RunReceipt("run-layout", "committed", "plan-layout", 0, 0)
            output = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                layout_factory=lambda _config: BrokenLayout(),
                process_probe=FakeProcessProbe(),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "sync", "--json"],
                dependencies=dependencies,
                stdout=output,
                stderr=io.StringIO(),
            )

            payload = json.loads(output.getvalue())
            self.assertEqual(1, exit_code)
            self.assertEqual("needs-attention", payload["progress"])
            self.assertEqual("committed", payload["state"])
            self.assertEqual(
                {"state": "skipped", "reason": "layout-error"},
                payload["layout"],
            )

    def test_safe_layout_failure_reports_the_specific_check(self):
        from claude_session_sync.layout import LayoutError

        class UnsafeLayout:
            def sync(self):
                raise LayoutError("custom group records contain conflicting ids")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = replace(config(root), sync_sidebar_layout=True)
            planned = Plan(1, "digest", (), (), (), "plan-layout", 0)
            receipt = RunReceipt("run-layout", "committed", "plan-layout", 0, 0)
            output = io.StringIO()
            dependencies = CliDependencies(
                config_loader=lambda path: loaded,
                planner_factory=lambda _config: FakePlanner(planned),
                engine_factory=lambda _config: FakeEngine(receipt),
                layout_factory=lambda _config: UnsafeLayout(),
                process_probe=FakeProcessProbe(),
            )

            exit_code = run(
                ["--config", str(root / "config.json"), "sync", "--json"],
                dependencies=dependencies,
                stdout=output,
                stderr=io.StringIO(),
            )

            payload = json.loads(output.getvalue())
            self.assertEqual(1, exit_code)
            self.assertEqual("needs-attention", payload["progress"])
            self.assertEqual("committed", payload["state"])
            self.assertEqual("unsafe-layout", payload["layout"]["reason"])
            self.assertEqual(
                "custom group records contain conflicting ids",
                payload["layout"]["detail"],
            )


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
