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

    def test_a_changed_sidebar_folder_triggers_a_sync(self):
        self.live_scenario(touch_folder=True)

    def test_restart_suggestion_is_recorded_without_quitting(self):
        self.live_scenario(suggest_restart=True)

    def test_restart_request_quits_syncs_and_reopens_once(self):
        self.live_scenario(request_restart=True)

    def test_failed_restart_sync_does_not_reopen_or_repeat(self):
        self.live_scenario(request_restart=True, fail_sync=True)

    def test_blocked_restart_check_leaves_claude_open(self):
        self.live_scenario(request_restart=True, block_preflight=True)

    def live_scenario(self, change_account=False, touch_folder=False, suggest_restart=False,
                      request_restart=False, fail_sync=False, block_preflight=False):
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
                "import json, pathlib, subprocess, sys\n"
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
                f"else: print(json.dumps({{'state': 'noop', 'progress': 'finished', 'restart_suggested': 2 if {suggest_restart!r} else 0}}))\n"
            )
            subprocess.run(["/usr/bin/open", str(app)], check=True)
            status = root / "watcher-status.json"
            env = dict(os.environ, CLAUDE_SESSION_SYNC_DISABLE_NOTIFICATIONS="1")
            watcher_command = [
                str(self.binary), "--claude-executable", str(executable), "--status", str(status),
                "--account-file", str(account), "--profile", "Work", "--",
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
                    self.assertTrue(wait_for(lambda: len(entries()) >= 2, 10), "a switch must trigger a sync")
                    self.assertTrue(app_running(), "a switch must never quit Claude")
                    self.assertNotIn("must-not-be-copied", (root / "account-restart.json").read_text())
                if touch_folder:
                    time.sleep(1)
                    (folder / "local_new.json").write_text("{}")
                    self.assertTrue(wait_for(lambda: len(entries()) >= 2, 12), "a folder change must trigger a sync")
                if suggest_restart:
                    self.assertTrue(wait_for(lambda: json.loads(status.read_text()).get("restart_suggested") == 2, 5))
                    time.sleep(2)
                    self.assertTrue(app_running(), "a suggestion must never quit Claude")
                if request_restart:
                    (root / "restart-request").touch()
                    finished = wait_for(
                        lambda: any(item[0] == "switch" for item in entries())
                        or json.loads(status.read_text()).get("restart_phase") == "needs-attention",
                        30,
                    )
                    self.assertTrue(finished, "the restart request must be handled")
                    expected = 0 if block_preflight else 1
                    time.sleep(4)
                    self.assertEqual(expected, sum(item[0] == "switch" for item in entries()))
                    self.assertFalse((root / "restart-request").exists())
                    phase = json.loads(status.read_text())["restart_phase"]
                    self.assertEqual("needs-attention" if fail_sync or block_preflight else "finished", phase)
                    if block_preflight:
                        self.assertTrue(app_running(), "a blocked check must leave Claude open")
                    elif not fail_sync:
                        self.assertTrue(wait_for(app_running, 10), "Claude must reopen after the sync")
                self.assertTrue(all(item[0] != "switch" for item in entries()) or request_restart)
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)
                subprocess.run(["/usr/bin/pkill", "-f", str(executable)], check=False)
