import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timezone

from claude_session_sync import strict_json
from claude_session_sync.adapters import read_status, save_status
from claude_session_sync.cli import CliDependencies, run
from claude_session_sync.locking import ExclusiveFileLock
from claude_session_sync.model import InvalidReplica, Plan
from claude_session_sync.progress import current_progress, record_progress
from test_cli import (
    config,
    FakeEngine,
    FakeLayout,
    FakePlanner,
    FakeProcessProbe,
    FakeRoutine,
    SequencedProcessProbe,
)


class ProgressTests(unittest.TestCase):
    def test_active_recovery_overrides_the_previous_restart_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = config(Path(directory))
            save_status(loaded.state_dir, "watcher-status.json", {"restart_phase": "needs-attention"})
            record_progress(loaded, "syncing")
            with ExclusiveFileLock(loaded.state_dir / "switch-handoff.lock"):
                self.assertEqual("syncing", current_progress(loaded, app_running=False, failures=1)["progress"])
            self.assertEqual("needs-attention", current_progress(loaded, app_running=False)["progress"])

    def test_retrying_account_check_is_visible_without_claiming_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = config(Path(directory))
            save_status(loaded.state_dir, "watcher-status.json", {
                "automatic_restart": True,
                "restart_phase": "checking",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            result = current_progress(loaded, app_running=True)
            self.assertEqual("checking-account", result["progress"])
            self.assertEqual("wait-for-automatic-restart", result["next_action"])

    def test_invalid_restart_timestamps_do_not_break_status(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = config(Path(directory))
            for timestamp in (None, 123, [], {}, "not-a-date"):
                with self.subTest(timestamp=timestamp):
                    save_status(loaded.state_dir, "watcher-status.json", {"restart_phase": "quitting", "timestamp": timestamp})
                    self.assertEqual("waiting-for-Claude", current_progress(loaded, app_running=True)["progress"])

    def test_account_restart_progress_is_visible_and_stale_receipts_do_not_look_active(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = config(Path(directory))
            record_progress(loaded, "finished")
            watcher = {"automatic_restart": True, "restart_phase": "quitting", "timestamp": datetime.now(timezone.utc).isoformat()}
            save_status(loaded.state_dir, "watcher-status.json", watcher)
            status = current_progress(loaded, app_running=True)
            self.assertEqual("quitting-Claude", status["progress"])
            self.assertEqual("wait-for-automatic-restart", status["next_action"])
            watcher["timestamp"] = "2000-01-01T00:00:00Z"
            save_status(loaded.state_dir, "watcher-status.json", watcher)
            self.assertEqual("waiting-for-Claude", current_progress(loaded, app_running=True)["progress"])
            watcher["restart_phase"] = "needs-attention"
            save_status(loaded.state_dir, "watcher-status.json", watcher)
            self.assertEqual("needs-attention", current_progress(loaded, app_running=True)["progress"])

    def test_missing_finished_waiting_and_interrupted_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = config(Path(directory))
            self.assertEqual(
                "not-synced-yet",
                current_progress(loaded, app_running=False)["progress"],
            )
            self.assertFalse(loaded.state_dir.exists())
            record_progress(loaded, "finished")
            waiting = current_progress(loaded, app_running=True)
            self.assertEqual("waiting-for-Claude", waiting["progress"])
            self.assertIsNotNone(waiting["last_success_at"])
            record_progress(loaded, "waiting-for-sync")
            self.assertEqual(
                "waiting-for-sync",
                current_progress(loaded, app_running=False)["progress"],
            )
            record_progress(loaded, "syncing")
            self.assertEqual(
                "needs-attention",
                current_progress(loaded, app_running=False)["progress"],
            )
            with ExclusiveFileLock(loaded.state_dir / "switch-handoff.lock"):
                self.assertEqual(
                    "syncing", current_progress(loaded, app_running=False)["progress"]
                )

    def test_malformed_chat_does_not_stop_other_adapters_in_any_sync_command(self):
        for command in (
            ("sync", "--json"),
            ("auto", "--json"),
            ("switch", "Work", "--no-launch"),
        ):
            with (
                self.subTest(command=command),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                loaded = replace(
                    config(root), sync_code_routines=True, sync_sidebar_layout=True
                )
                plan = Plan(
                    1,
                    "config",
                    (),
                    (),
                    (InvalidReplica(root / "bad.json", "malformed JSON"),),
                    "plan",
                    0,
                )
                engine = FakeEngine(None)
                deps = CliDependencies(
                    config_loader=lambda _: loaded,
                    planner_factory=lambda _: FakePlanner(plan),
                    engine_factory=lambda _: engine,
                    routine_factory=lambda _: FakeRoutine(),
                    layout_factory=lambda _: FakeLayout(),
                    process_probe=FakeProcessProbe(),
                )
                out = io.StringIO()
                self.assertEqual(
                    1, run(command, dependencies=deps, stdout=out, stderr=io.StringIO())
                )
                result = read_status(loaded.state_dir, "sync-progress.json")
                self.assertEqual("needs-attention", result["state"])
                self.assertEqual("blocked_invalid", result["result"]["state"])
                self.assertEqual("synced", result["result"]["routines"]["state"])
                self.assertEqual("synced", result["result"]["layout"]["state"])
                self.assertEqual([], engine.applied)

    def test_doctor_checks_current_adapter_data_and_pending_recovery_read_only(self):
        class ProbeLayout:
            def probe(self):
                return {"state": "compatible", "groups": 0}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = replace(config(root), sync_sidebar_layout=True)
            for profile in loaded.profiles:
                (profile.data_root / "claude-code-sessions/account/workspace").mkdir(
                    parents=True
                )
            save_status(
                loaded.state_dir, "sidebar-layout-status.json", {"state": "synced"}
            )
            save_status(
                loaded.state_dir / "layout-runs",
                "interrupted.json",
                {"state": "PREPARED"},
            )
            before = {
                str(path): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            deps = CliDependencies(
                config_loader=lambda _: loaded,
                process_probe=FakeProcessProbe(),
                layout_factory=lambda _: ProbeLayout(),
            )
            out = io.StringIO()
            self.assertEqual(
                1,
                run(
                    ("doctor", "--json"),
                    dependencies=deps,
                    stdout=out,
                    stderr=io.StringIO(),
                ),
            )
            result = json.loads(out.getvalue())
            self.assertEqual(1, result["counts"]["recovery_pending"])
            self.assertEqual("compatible", result["adapters"]["layout"]["state"])
            self.assertEqual(
                before,
                {
                    str(path): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                },
            )

    def test_running_claude_never_starts_a_planner_or_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            loaded = replace(
                config(Path(directory)),
                sync_sidebar_layout=True,
                sync_code_routines=True,
            )

            def forbidden(_):
                self.fail("storage adapter must not run while Claude is open")

            deps = CliDependencies(
                config_loader=lambda _: loaded,
                process_probe=FakeProcessProbe((object(),)),
                planner_factory=forbidden,
                layout_factory=forbidden,
                routine_factory=forbidden,
            )
            out = io.StringIO()
            self.assertEqual(
                0,
                run(
                    ("auto", "--json"),
                    dependencies=deps,
                    stdout=out,
                    stderr=io.StringIO(),
                ),
            )
            self.assertEqual(
                "waiting-for-Claude", json.loads(out.getvalue())["progress"]
            )

    def test_a_component_deferred_because_claude_reopened_is_not_finished(self):
        from claude_session_sync.model import RunReceipt

        for command, expected_exit in (("auto", 0), ("sync", 1)):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                loaded = replace(config(Path(directory)), sync_sidebar_layout=True)
                plan = Plan(1, "digest", (), (), (), "plan", 0)
                deps = CliDependencies(
                    config_loader=lambda _: loaded,
                    planner_factory=lambda _: FakePlanner(plan),
                    engine_factory=lambda _: FakeEngine(RunReceipt("run", "committed", "plan", 1, 10)),
                    layout_factory=lambda _: self.fail("pins and groups must wait for Claude"),
                    # Closed for the chat phase, open again before pins and groups.
                    process_probe=SequencedProcessProbe(((), (object(),))),
                )
                out = io.StringIO()

                exit_code = run((command, "--json"), dependencies=deps, stdout=out, stderr=io.StringIO())

                payload = json.loads(out.getvalue())
                self.assertEqual(expected_exit, exit_code)
                self.assertEqual("committed", payload["state"])
                self.assertEqual("deferred", payload["layout"]["state"])
                self.assertEqual("waiting-for-Claude", payload["progress"])
                saved = read_status(loaded.state_dir, "sync-progress.json")
                self.assertEqual("waiting-for-Claude", saved["state"])
                self.assertIsNone(saved["last_success_at"])

    def test_fifo_status_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.mkfifo(root / "status.json")
            self.assertEqual("unreadable", read_status(root, "status.json")["state"])


class StrictJsonTests(unittest.TestCase):
    def test_rejects_javascript_incompatible_numbers_and_duplicate_keys(self):
        for text in (
            '{"a":NaN}',
            '{"a":Infinity}',
            '{"a":-Infinity}',
            '{"a":1e999}',
            '{"a":1,"a":2}',
        ):
            with (
                self.subTest(text=text),
                self.assertRaises(strict_json.JSONDecodeError),
            ):
                strict_json.loads(text)
        self.assertEqual({"a": 1.5}, strict_json.loads('{"a":1.5}'))

    def test_never_emits_nonfinite_numbers(self):
        with self.assertRaises(ValueError):
            strict_json.dumps({"a": float("nan")})
