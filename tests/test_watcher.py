"""Smoke-test the actual macOS watcher with an isolated, non-writing child."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import plistlib
from pathlib import Path


@unittest.skipUnless(sys.platform == "darwin", "macOS watcher")
class WatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="watcher-tests-")
        cls.binary = Path(cls.temporary.name) / "watcher"
        source = (
            Path(__file__).parents[1]
            / "src/claude_session_sync/SessionSyncWatcher.swift"
        )
        subprocess.run(
            [
                "/usr/bin/xcrun",
                "swiftc",
                str(source),
                "-o",
                str(cls.binary),
                "-framework",
                "AppKit",
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def result_for(self, script, restart_receipt=None):
        with tempfile.TemporaryDirectory(prefix="watcher-status-test-") as directory:
            status = Path(directory) / "watcher-status.json"
            account_options = []
            if restart_receipt is not None:
                account = Path(directory) / "config.json"
                account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
                (Path(directory) / "account-restart.json").write_text(json.dumps(restart_receipt))
                account_options = ["--account-file", str(account), "--profile", "Work"]
            env = dict(os.environ, CLAUDE_SESSION_SYNC_DISABLE_NOTIFICATIONS="1")
            process = subprocess.Popen(
                [
                    str(self.binary),
                    "--claude-executable",
                    "/nonexistent/Claude",
                    "--status",
                    str(status),
                    *account_options,
                    "--",
                    sys.executable,
                    "-c",
                    script,
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if status.exists():
                        return json.loads(status.read_bytes())
                    if process.poll() is not None:
                        self.fail("watcher exited before producing its receipt")
                    time.sleep(0.05)
                self.fail("watcher did not drain child output or save status")
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_finished_result_is_persisted(self):
        result = self.result_for('print(\'{"state":"noop","progress":"finished"}\')')
        self.assertEqual("ok", result["state"])
        self.assertEqual("finished", json.loads(result["output"])["progress"])

    def test_large_invalid_output_does_not_deadlock_or_report_success(self):
        result = self.result_for('print("x" * 200000)')
        self.assertEqual("failed", result["state"])
        self.assertLessEqual(len(result["output"]), 2048)

    def test_failed_child_is_attention_not_success(self):
        result = self.result_for(
            'print(\'{"state":"failed","progress":"needs-attention"}\'); raise SystemExit(1)'
        )
        self.assertEqual("failed", result["state"])
        self.assertEqual(1, result["exit_status"])

    def test_launch_failure_does_not_prevent_a_subsequent_auto_attempt(self):
        with tempfile.TemporaryDirectory(prefix="watcher-launch-failure-test-") as directory:
            root = Path(directory)
            status = root / "watcher-status.json"
            account = root / "config.json"
            account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
            process = subprocess.Popen(
                [
                    str(self.binary),
                    "--claude-executable", "/nonexistent/Claude",
                    "--status", str(status),
                    "--account-file", str(account),
                    "--profile", "Work",
                    "--", str(root / "missing-sync-command"),
                ],
                env=dict(os.environ, CLAUDE_SESSION_SYNC_DISABLE_NOTIFICATIONS="1"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            def receipt():
                try:
                    return json.loads(status.read_bytes())
                except (FileNotFoundError, json.JSONDecodeError):
                    return None

            try:
                deadline = time.monotonic() + 5
                first = None
                while time.monotonic() < deadline:
                    first = receipt()
                    if first and first["launch_failed"]:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(first, "initial launch failure receipt")
                self.assertTrue(first["launch_failed"])

                account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
                deadline = time.monotonic() + 7
                second = None
                while time.monotonic() < deadline:
                    second = receipt()
                    if second and second["timestamp"] != first["timestamp"]:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(second, "subsequent launch failure receipt")
                self.assertNotEqual(first["timestamp"], second["timestamp"])
                self.assertTrue(second["launch_failed"])
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_repeated_fast_children_finish_after_their_output_is_drained(self):
        """One watcher must finish every child before starting the next retry."""
        with tempfile.TemporaryDirectory(prefix="watcher-fast-exit-test-") as directory:
            root = Path(directory)
            calls = root / "calls"
            child = root / "child.py"
            child.write_text(
                "import json, pathlib, sys\n"
                f"calls = pathlib.Path({str(calls)!r})\n"
                "sequence = len(calls.read_text().splitlines()) + 1 if calls.exists() else 1\n"
                "with calls.open('a') as out: out.write(str(sequence) + '\\n')\n"
                "result = {'state': 'noop', 'sequence': sequence, 'padding': 'x' * 1024}\n"
                "if sequence <= 15: result.update(reason='busy', progress='waiting-for-sync')\n"
                "else: result['progress'] = 'finished'\n"
                "text = json.dumps(result)\n"
                "before, after = text.split('\\\"progress\\\"', 1)\n"
                "sys.stdout.write(before); sys.stdout.flush()\n"
                "sys.stdout.write('\\\"progress\\\"' + after)\n"
            )
            status = root / "watcher-status.json"
            process = subprocess.Popen(
                [
                    str(self.binary),
                    "--claude-executable", "/nonexistent/Claude",
                    "--status", str(status),
                    "--", sys.executable, str(child),
                ],
                env=dict(os.environ, CLAUDE_SESSION_SYNC_DISABLE_NOTIFICATIONS="1"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 25
                output = None
                while time.monotonic() < deadline:
                    try:
                        receipt = json.loads(status.read_bytes())
                        output = json.loads(receipt["output"])
                    except (FileNotFoundError, json.JSONDecodeError, KeyError):
                        time.sleep(0.05)
                        continue
                    if output.get("sequence") == 16 and output.get("progress") == "finished":
                        break
                    if process.poll() is not None:
                        self.fail("watcher exited during the fast-child stress run")
                    time.sleep(0.05)
                self.assertIsNotNone(output, "fast-child completion receipt")
                self.assertEqual(16, output.get("sequence"))
                self.assertEqual("finished", output.get("progress"))
                self.assertEqual("x" * 1024, output.get("padding"))
                self.assertEqual([str(number) for number in range(1, 17)], calls.read_text().splitlines())
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_an_account_change_syncs_once_while_chat_saves_do_not(self):
        with tempfile.TemporaryDirectory(prefix="watcher-account-folder-race-test-") as directory:
            root = Path(directory)
            account = root / "config.json"
            account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
            folder = root / "claude-code-sessions" / "account" / "workspace"
            folder.mkdir(parents=True)
            calls = root / "calls"
            child = root / "child.py"
            child.write_text(
                "import json, pathlib\n"
                f"calls = pathlib.Path({str(calls)!r})\n"
                "sequence = len(calls.read_text().splitlines()) + 1 if calls.exists() else 1\n"
                "with calls.open('a') as out: out.write(str(sequence) + '\\n')\n"
                "print(json.dumps({'state': 'noop', 'sequence': sequence, 'progress': 'finished'}))\n"
            )
            status = root / "watcher-status.json"
            process = subprocess.Popen(
                [
                    str(self.binary),
                    "--claude-executable", "/nonexistent/Claude",
                    "--status", str(status),
                    "--account-file", str(account),
                    "--profile", "Work",
                    "--", sys.executable, str(child),
                ],
                env=dict(os.environ, CLAUDE_SESSION_SYNC_DISABLE_NOTIFICATIONS="1"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            def call_count():
                return len(calls.read_text().splitlines()) if calls.exists() else 0

            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and call_count() < 1:
                    time.sleep(0.05)
                self.assertEqual(1, call_count(), "startup auto run")

                # Save a chat and switch accounts together. Only the switch syncs.
                time.sleep(3.2)
                account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
                (folder / "new-chat.json").write_text("{}")

                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and call_count() < 2:
                    if process.poll() is not None:
                        self.fail("watcher exited during the account-folder race")
                    time.sleep(0.05)
                time.sleep(6)
                self.assertEqual(
                    2,
                    call_count(),
                    "only the account change may sync; a chat save must not",
                )
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_legacy_receipts_remain_supported(self):
        for phase in ("ready", "finished"):
            with self.subTest(phase=phase):
                result = self.result_for(
                    'print(\'{"state":"skipped","reason":"app-running","progress":"waiting-for-Claude"}\')',
                    {"account_hash": "a" * 64, "phase": phase},
                )
                self.assertEqual("ready", result["restart_phase"])

    def test_invalid_pending_budget_fails_closed(self):
        valid = {"account_hash": "a" * 64, "phase": "checking", "candidate_hash": "b" * 64,
                 "preflight_attempts": 1, "retry_not_before": time.time()}
        for change in ({"candidate_hash": None}, {"candidate_hash": "a" * 64},
                       {"candidate_hash": "raw-account-uuid"}, {"preflight_attempts": None},
                       {"preflight_attempts": 0}, {"preflight_attempts": 4},
                       {"preflight_attempts": True}, {"preflight_attempts": 1.5},
                       {"retry_not_before": None}, {"retry_not_before": -1}):
            with self.subTest(change=change):
                result = self.result_for(
                    'print(\'{"state":"skipped","reason":"app-running","progress":"waiting-for-Claude"}\')',
                    dict(valid, **change),
                )
                self.assertEqual("needs-attention", result["restart_phase"])

    def test_account_change_syncs_without_quitting_claude(self):
        """Exercise the real watcher: a switch triggers a sync, never a quit."""
        self.live_scenario(change_account=True)

    def test_a_changed_chat_folder_does_not_sync_while_claude_runs(self):
        self.live_scenario(touch_folder=True)

    def test_restart_suggestion_is_recorded_without_quitting(self):
        self.live_scenario(suggest_restart=True)

    def test_restart_request_quits_syncs_and_reopens_once(self):
        self.live_scenario(request_restart=True)

    def test_failed_restart_sync_does_not_reopen_or_repeat(self):
        self.live_scenario(request_restart=True, fail_sync=True)

    def test_blocked_restart_check_leaves_claude_open(self):
        self.live_scenario(request_restart=True, block_preflight=True)

    def test_a_restart_requested_during_a_sync_waits_for_it(self):
        self.live_scenario(request_restart=True, request_during_sync=True)

    def test_with_pins_and_groups_a_switch_restarts_once_and_copies_them(self):
        self.live_scenario(change_account=True, restart_on_switch=True)

    def test_an_adoption_request_reaches_the_closed_claude_sync(self):
        self.live_scenario(request_restart=True, request_text="adopt-current-sidebar\n")

    def live_scenario(self, change_account=False, touch_folder=False, suggest_restart=False,
                      request_restart=False, fail_sync=False, block_preflight=False,
                      request_during_sync=False, restart_on_switch=False, request_text=""):
        with tempfile.TemporaryDirectory(prefix="account-restart-test-") as directory:
            root = Path(directory)
            app = root / "SyncTest.app"
            executable = app / "Contents/MacOS/SyncTest"
            executable.parent.mkdir(parents=True)
            source = root / "app.swift"
            source.write_text('import AppKit\nlet app = NSApplication.shared\napp.setActivationPolicy(.accessory)\napp.run()\n')
            subprocess.run(["/usr/bin/xcrun", "swiftc", str(source), "-o", str(executable), "-framework", "AppKit"], check=True, capture_output=True)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleExecutable": "SyncTest", "CFBundleIdentifier": "com.example.sync-test." + uuid.uuid4().hex,
                "CFBundleName": "SyncTest", "CFBundlePackageType": "APPL",
            }))
            account = root / "config.json"
            account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
            folder = root / "claude-code-sessions" / "account" / "workspace"
            folder.mkdir(parents=True)
            calls = root / "calls.jsonl"
            child = root / "child.py"
            child.write_text(
                "import json, pathlib, subprocess, sys, time\n"
                f"calls = pathlib.Path({str(calls)!r})\n"
                "if sys.argv[1] == 'restart-check':\n"
                f" if {block_preflight!r}: print('invalid'); raise SystemExit(1)\n"
                f" result = subprocess.run(['/usr/bin/pgrep', '-f', {str(executable)!r}], text=True, capture_output=True)\n"
                " print(json.dumps({'state': 'ready', 'pids': [int(pid) for pid in result.stdout.split()]})); raise SystemExit(0)\n"
                "with calls.open('a') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "if sys.argv[1] == 'switch':\n"
                f" assert subprocess.run(['/usr/bin/pgrep', '-f', {str(executable)!r}], stdout=subprocess.DEVNULL).returncode == 1, 'sync ran before app exited'\n"
                f" if {fail_sync!r}: print(json.dumps({{'state': 'blocked_invalid', 'progress': 'needs-attention'}})); raise SystemExit(1)\n"
                f" subprocess.run(['/usr/bin/open', {str(app)!r}], check=True)\n"
                " print(json.dumps({'state': 'noop', 'progress': 'finished'}))\n"
                "else:\n"
                f" if {request_during_sync!r} and len(calls.read_text().splitlines()) > 1: time.sleep(3)\n"
                f" print(json.dumps({{'state': 'noop', 'progress': 'finished', 'restart_suggested': 2 if {suggest_restart!r} else 0}}))\n"
            )
            subprocess.run(["/usr/bin/open", str(app)], check=True)
            status = root / "watcher-status.json"
            env = dict(os.environ, CLAUDE_SESSION_SYNC_DISABLE_NOTIFICATIONS="1")
            watcher_command = [
                str(self.binary), "--claude-executable", str(executable), "--status", str(status),
                "--account-file", str(account), "--profile", "Work",
                *(["--restart-on-switch", "1"] if restart_on_switch else []), "--",
                sys.executable, str(child),
            ]
            process = subprocess.Popen(watcher_command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            def entries():
                return [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

            def wait_for(condition, seconds):
                deadline = time.monotonic() + seconds
                while time.monotonic() < deadline:
                    if condition():
                        return True
                    time.sleep(0.1)
                return False

            def app_running():
                return subprocess.run(['/usr/bin/pgrep', '-f', str(executable)], stdout=subprocess.DEVNULL).returncode == 0

            try:
                self.assertTrue(wait_for(lambda: status.exists() and len(entries()) >= 1, 10), "startup sync")
                self.assertIsNone(process.poll())
                self.assertEqual(["auto", "--json"], entries()[0])
                if change_account:
                    account.write_text('{"lastKnownAccountUuid":null,"secret":"must-not-be-copied"}')
                    time.sleep(2)
                    self.assertEqual(1, len(entries()), "logout alone must not sync")
                    account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
                    self.assertTrue(wait_for(lambda: len(entries()) >= 2, 20), "a switch must trigger a sync")
                    self.assertNotIn("must-not-be-copied", (root / "account-restart.json").read_text())
                    if restart_on_switch:
                        def switches():
                            return [item for item in entries() if item[0] == "switch"]

                        self.assertTrue(wait_for(lambda: switches(), 30), "a switch must restart once")
                        self.assertIn("--after-account-switch", switches()[0])
                        self.assertTrue(wait_for(app_running, 10), "Claude must reopen")
                        time.sleep(4)
                        self.assertEqual(1, len(switches()), "no restart loop")
                    else:
                        self.assertTrue(app_running(), "a switch must never quit Claude")
                if touch_folder:
                    time.sleep(1)
                    (folder / "local_new.json").write_text("{}")
                    time.sleep(7)
                    self.assertEqual(1, len(entries()), "a chat save must not sync while Claude runs")
                if suggest_restart:
                    self.assertTrue(wait_for(lambda: json.loads(status.read_text()).get("restart_suggested") == 2, 5))
                    time.sleep(2)
                    self.assertTrue(app_running(), "a suggestion must never quit Claude")
                if request_during_sync:
                    time.sleep(1)
                    account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
                    self.assertTrue(wait_for(lambda: len(entries()) >= 2, 12), "a sync must be running")
                if request_restart:
                    (root / "restart-request").write_text(request_text)
                    finished = wait_for(
                        lambda: any(item[0] == "switch" for item in entries())
                        or json.loads(status.read_text()).get("restart_phase") == "needs-attention",
                        30,
                    )
                    self.assertTrue(
                        finished,
                        "the restart request must be handled: {} {}".format(
                            status.read_text(), entries()
                        ),
                    )
                    expected = 0 if block_preflight else 1
                    time.sleep(4)
                    self.assertEqual(expected, sum(item[0] == "switch" for item in entries()))
                    if expected and request_text:
                        switch_call = next(item for item in entries() if item[0] == "switch")
                        self.assertIn("--adopt-current-sidebar", switch_call)
                    self.assertFalse((root / "restart-request").exists())
                    phase = json.loads(status.read_text())["restart_phase"]
                    self.assertEqual("needs-attention" if fail_sync or block_preflight else "finished", phase)
                    if block_preflight:
                        self.assertTrue(app_running(), "a blocked check must leave Claude open")
                    elif not fail_sync:
                        self.assertTrue(wait_for(app_running, 10), "Claude must reopen after the sync")
                self.assertTrue(
                    all(item[0] != "switch" for item in entries()) or request_restart or restart_on_switch
                )
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)
                subprocess.run(["/usr/bin/pkill", "-f", str(executable)], check=False)
