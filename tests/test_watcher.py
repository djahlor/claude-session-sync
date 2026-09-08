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

    def result_for(self, script):
        with tempfile.TemporaryDirectory(prefix="watcher-status-test-") as directory:
            status = Path(directory) / "watcher-status.json"
            env = dict(os.environ, CLAUDE_SESSION_SYNC_DISABLE_NOTIFICATIONS="1")
            process = subprocess.Popen(
                [
                    str(self.binary),
                    "--claude-executable",
                    "/nonexistent/Claude",
                    "--status",
                    str(status),
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

    def test_account_change_quits_syncs_and_reopens_once(self):
        """Exercise the real watcher and macOS quit event, not a mocked timer."""
        self.account_change_scenario()

    def test_failed_sync_does_not_reopen_or_repeat_after_helper_restart(self):
        self.account_change_scenario(fail_sync=True)

    def test_transient_restart_check_retries_without_another_account_change(self):
        self.account_change_scenario(fail_preflight_once=True)

    def test_repeated_preflight_timeouts_stop_without_quitting_or_looping(self):
        self.account_change_scenario(fail_preflight_always=True)

    def test_invalid_preflight_never_quits_or_retries(self):
        self.account_change_scenario(block_preflight=True)

    def account_change_scenario(self, fail_sync=False, fail_preflight_once=False,
                                fail_preflight_always=False, block_preflight=False):
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
            calls = root / "calls.jsonl"
            child = root / "child.py"
            child.write_text(
                "import json, pathlib, subprocess, sys\n"
                f"calls = pathlib.Path({str(calls)!r})\n"
                "if sys.argv[1] == 'restart-check':\n"
                " failure = calls.with_suffix('.preflight-failed')\n"
                " checks = calls.with_suffix('.checks')\n"
                " checks.write_text(str(int(checks.read_text()) + 1 if checks.exists() else 1))\n"
                f" if {block_preflight!r}: print('invalid'); raise SystemExit(1)\n"
                f" if {fail_preflight_always!r} or ({fail_preflight_once!r} and not failure.exists()):\n"
                "  failure.touch()\n"
                "  print(json.dumps({'state': 'failed', 'error_type': 'process-timeout', 'reason': 'process-inspection-timeout', 'next_action': 'retry-sync'})); raise SystemExit(1)\n"
                f" result = subprocess.run(['/usr/bin/pgrep', '-f', {str(executable)!r}], text=True, capture_output=True)\n"
                " print(json.dumps({'state': 'ready', 'pids': [int(pid) for pid in result.stdout.split()]})); raise SystemExit(0)\n"
                "with calls.open('a') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "if sys.argv[1] == 'switch':\n"
                f" assert subprocess.run(['/usr/bin/pgrep', '-f', {str(executable)!r}], stdout=subprocess.DEVNULL).returncode == 1, 'sync ran before app exited'\n"
                f" if {fail_sync!r}: print(json.dumps({{'state': 'blocked_invalid', 'progress': 'needs-attention'}})); raise SystemExit(1)\n"
                f" subprocess.run(['/usr/bin/open', {str(app)!r}], check=True)\n"
                " print(json.dumps({'state': 'noop', 'progress': 'finished'}))\n"
                "else: print(json.dumps({'state': 'skipped', 'reason': 'app-running', 'progress': 'waiting-for-Claude'}))\n"
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
            try:
                deadline = time.monotonic() + 10
                while not status.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIsNone(process.poll(), "watcher must support account-change detection")
                self.assertTrue(status.exists())
                account.write_text('{"lastKnownAccountUuid":null,"secret":"must-not-be-copied"}')
                time.sleep(2)
                self.assertEqual(1, len(calls.read_text().splitlines()), "logout alone must not restart")
                account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    entries = [json.loads(line) for line in calls.read_text().splitlines()]
                    if any(item[0] == "switch" for item in entries) or json.loads(status.read_text()).get("restart_phase") == "needs-attention":
                        break
                    time.sleep(0.1)
                receipt = root / "account-restart.json"
                detail = (status.read_text(), receipt.read_text() if receipt.exists() else "no restart receipt", entries)
                expected_switches = 0 if fail_preflight_always or block_preflight else 1
                self.assertEqual(expected_switches, sum(item[0] == "switch" for item in entries), "account change must quit and hand off to sync-and-launch: " + repr(detail))
                if not expected_switches:
                    self.assertEqual(0, subprocess.run(['/usr/bin/pgrep', '-f', str(executable)], stdout=subprocess.DEVNULL).returncode, "failed preflight must leave the app running")
                time.sleep(4)
                entries = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual(expected_switches, sum(item[0] == "switch" for item in entries), "reopen must not create a restart loop")
                phase = "needs-attention" if fail_sync or not expected_switches else "finished"
                self.assertEqual(phase, json.loads(status.read_text())["restart_phase"])
                state = json.loads(receipt.read_text())
                self.assertEqual({"account_hash", "phase"}, set(state))
                self.assertNotIn("must-not-be-copied", receipt.read_text())
                process.terminate()
                process.communicate(timeout=5)
                process = subprocess.Popen(watcher_command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                time.sleep(5)
                entries = [json.loads(line) for line in calls.read_text().splitlines()]
                self.assertEqual(expected_switches, sum(item[0] == "switch" for item in entries), "helper restart must preserve the once-only guard")
                self.assertEqual(phase, json.loads(receipt.read_text())["phase"])
                expected_checks = 3 if fail_preflight_always else 2 if fail_preflight_once else 1
                self.assertEqual(expected_checks, int(calls.with_suffix('.checks').read_text()), "preflight retries must be bounded across helper restarts")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)
                subprocess.run(["/usr/bin/pkill", "-f", str(executable)], check=False)
