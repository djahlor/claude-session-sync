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

    def test_account_change_quits_syncs_and_reopens_once(self):
        """Exercise the real watcher and macOS quit event, not a mocked timer."""
        self.account_change_scenario()

    def test_failed_sync_does_not_reopen_or_repeat_after_helper_restart(self):
        self.account_change_scenario(fail_sync=True)

    def test_transient_restart_check_retries_without_another_account_change(self):
        self.account_change_scenario(fail_preflight_once=True)

    def test_repeated_preflight_timeouts_stop_without_quitting_or_looping(self):
        self.account_change_scenario(fail_preflight_always=True)

    def test_preflight_budget_survives_replacement_between_failed_checks(self):
        self.account_change_scenario(fail_preflight_always=True, restart_between_preflights=True)

    def test_last_preflight_is_consumed_before_a_crash_during_the_probe(self):
        self.account_change_scenario(fail_preflight_always=True, restart_during_third_preflight=True)

    def test_unsavable_preflight_budget_never_probes_or_quits(self):
        self.account_change_scenario(fail_receipt_save=True)

    def test_different_candidate_gets_its_own_budget_after_helper_replacement(self):
        self.account_change_scenario(fail_preflight_always=True, replace_candidate_after_first=True)

    def test_invalid_preflight_never_quits_or_retries(self):
        self.account_change_scenario(block_preflight=True)

    def account_change_scenario(self, fail_sync=False, fail_preflight_once=False,
                                fail_preflight_always=False, block_preflight=False,
                                restart_between_preflights=False, restart_during_third_preflight=False,
                                fail_receipt_save=False, replace_candidate_after_first=False):
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
                "import json, pathlib, subprocess, sys, time\n"
                f"calls = pathlib.Path({str(calls)!r})\n"
                "if sys.argv[1] == 'restart-check':\n"
                " failure = calls.with_suffix('.preflight-failed')\n"
                " checks = calls.with_suffix('.checks')\n"
                " checks.write_text(str(int(checks.read_text()) + 1 if checks.exists() else 1))\n"
                f" if {restart_during_third_preflight!r} and int(checks.read_text()) == 3:\n"
                "  while not calls.with_suffix('.probe-release').exists(): time.sleep(0.05)\n"
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
                receipt = root / "account-restart.json"
                if fail_receipt_save:
                    backup = root / "original-receipt.json"
                    receipt.rename(backup)
                    receipt.symlink_to(backup)
                account.write_text('{"lastKnownAccountUuid":null,"secret":"must-not-be-copied"}')
                time.sleep(2)
                self.assertEqual(1, len(calls.read_text().splitlines()), "logout alone must not restart")
                account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
                deadline = time.monotonic() + 30
                replacements = 0
                while time.monotonic() < deadline:
                    entries = [json.loads(line) for line in calls.read_text().splitlines()]
                    checks = calls.with_suffix('.checks')
                    between_failures = (restart_between_preflights and replacements < 2 and checks.exists()
                            and int(checks.read_text()) == replacements + 1
                            and json.loads(status.read_text()).get("restart_phase") == "checking")
                    during_probe = (restart_during_third_preflight and replacements == 0 and checks.exists()
                                    and int(checks.read_text()) == 3)
                    new_candidate = (replace_candidate_after_first and replacements == 0 and checks.exists()
                                     and int(checks.read_text()) == 1
                                     and json.loads(status.read_text()).get("restart_phase") == "checking")
                    if between_failures or during_probe or new_candidate:
                        state = json.loads(receipt.read_text())
                        self.assertEqual({"account_hash", "phase", "candidate_hash", "preflight_attempts", "retry_not_before"}, set(state))
                        self.assertEqual("checking", state["phase"])
                        self.assertEqual(int(checks.read_text()), state["preflight_attempts"])
                        self.assertRegex(state["candidate_hash"], r"^[0-9a-f]{64}$")
                        self.assertNotEqual(state["account_hash"], state["candidate_hash"])
                        self.assertLessEqual(state["retry_not_before"], time.time() + 6)
                        self.assertGreater(state["retry_not_before"], 0)
                        self.assertNotIn(json.loads(account.read_text())["lastKnownAccountUuid"], receipt.read_text())
                        process.terminate()
                        process.communicate(timeout=5)
                        if new_candidate:
                            account.write_text(json.dumps({"lastKnownAccountUuid": str(uuid.uuid4())}))
                        if during_probe:
                            calls.with_suffix('.probe-release').touch()
                        process = subprocess.Popen(watcher_command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                        replacements += 1
                    if any(item[0] == "switch" for item in entries) or json.loads(status.read_text()).get("restart_phase") == "needs-attention":
                        break
                    time.sleep(0.1)
                if restart_between_preflights:
                    self.assertEqual(2, replacements, "replace the helper after each of its first two failed checks")
                if restart_during_third_preflight:
                    self.assertEqual(1, replacements, "replace the helper while its last check is still running")
                if replace_candidate_after_first:
                    self.assertEqual(1, replacements, "a different candidate must be present when the helper restarts")
                detail = (status.read_text(), receipt.read_text() if receipt.exists() else "no restart receipt", entries)
                expected_switches = 0 if fail_preflight_always or block_preflight or fail_receipt_save else 1
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
                self.assertEqual("ready" if fail_receipt_save else phase, json.loads(receipt.read_text())["phase"])
                expected_checks = 0 if fail_receipt_save else 3 if fail_preflight_always else 2 if fail_preflight_once else 1
                if replace_candidate_after_first:
                    expected_checks += 1
                actual_checks = int(checks.read_text()) if checks.exists() else 0
                self.assertEqual(expected_checks, actual_checks, "preflight retries must be bounded across helper restarts")
            finally:
                calls.with_suffix('.probe-release').touch()
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)
                subprocess.run(["/usr/bin/pkill", "-f", str(executable)], check=False)
