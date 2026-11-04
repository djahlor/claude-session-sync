"""A sign-in that comes while the signed-out sync still runs (04-11-2026).

Claude was signed out for 37 seconds. The signed-out sync needed longer than
that to plan, and Claude quit to install an update the moment the next account
signed in. The sync then wrote none of its 37 planned changes but recorded the
run as committed, and went on to scan chat transcripts for pictures for minutes
while the restart that would have copied the chats waited for it.

Real chat folders, planner and engine. Fakes stand in only for Claude itself.
"""

import json
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path

from test_cli_live import A_ACCOUNT, B_ACCOUNT, ChatCliFixture, X, Y

from claude_session_sync.processes import ProcessProbe
from claude_session_sync.transaction import TransactionEngine


class SignInDuringSignedOutSyncTests(ChatCliFixture):
    def setUp(self):
        super().setUp()
        self.running = True
        self.claude_config(lastKnownAccountUuid=A_ACCOUNT, windowSizeWasSignedIn=False)

    def claude_config(self, **values):
        (self.data_root / "config.json").write_text(
            json.dumps(values), encoding="utf-8"
        )

    def sign_in(self, account):
        self.claude_config(lastKnownAccountUuid=account, windowSizeWasSignedIn=True)

    def live_engine(self, config, holds, guard):
        return TransactionEngine(
            config.state_dir,
            process_probe=holds,
            retention=config.retention,
            write_guard=guard,
        )

    def signed_out_sync(self, **dependencies):
        options = {
            "live_engine_factory": self.live_engine,
            "sleeper": lambda _seconds: None,
            "picture_factory": lambda _config: self.fail("pictures must wait"),
        }
        options.update(dependencies)
        code, out, errors = self.cli("signed-out-sync", "Work", "--json", **options)
        self.assertEqual(0, code, errors)
        return json.loads(out)

    def test_a_sign_in_that_quits_claude_still_gets_the_chats(self):
        """Claude quit for an update at the sign-in: no Claude holds a folder."""
        self.write(self.a, X)
        self.write(self.a, Y)

        def sign_in_and_quit_for_update(_seconds):
            self.sign_in(B_ACCOUNT)
            self.running = False

        payload = self.signed_out_sync(sleeper=sign_in_and_quit_for_update)

        self.assertEqual("committed", payload["state"])
        self.assertIsNotNone(self.read(self.b, X))
        self.assertIsNotNone(self.read(self.b, Y))
        # The switch still restarts once, for the account just left.
        self.assertTrue(payload["signed_in_during_sync"])
        self.assertTrue(payload["restart"])

    def test_a_sign_in_with_claude_open_stops_the_writes_and_says_so(self):
        self.write(self.a, X)
        self.write(self.a, Y)
        checks = []

        def engine(config, holds, guard):
            def sign_in_at_the_second_write():
                checks.append(True)
                if len(checks) == 2:
                    self.sign_in(B_ACCOUNT)
                return guard()

            return self.live_engine(config, holds, sign_in_at_the_second_write)

        payload = self.signed_out_sync(live_engine_factory=engine)

        # One of two chats was written. That is no commit of the plan.
        self.assertEqual("interrupted", payload["state"])
        self.assertEqual(1, payload["counts"]["operations"])
        self.assertEqual(2, payload["counts"]["planned"])
        self.assertIn("chats", payload["restart_reasons"])
        self.assertTrue(payload["restart"])
        # A sign-in race is no fault: the switch restart copies the rest.
        self.assertEqual("waiting-for-Claude", payload["progress"])
        progress = json.loads(
            (self.config.state_dir / "sync-progress.json").read_text()
        )
        self.assertIsNone(progress["last_success_at"])

    def test_after_a_sign_in_it_ends_without_the_picture_scan(self):
        self.write(self.a, X)

        payload = self.signed_out_sync(sleeper=lambda _seconds: self.sign_in(B_ACCOUNT))

        self.assertEqual("waiting", payload["state"])
        self.assertEqual(
            {"state": "deferred", "reason": "signed-in"}, payload["pictures"]
        )
        self.assertTrue(payload["restart"])

    def test_still_signed_out_the_picture_scan_runs_last_and_stops_at_a_sign_in(self):
        self.config = replace(self.config, sync_code_routines=True)
        self.write(self.a, X)
        order = []

        class Routines:
            def sync(_self):
                order.append("routines")
                return type("R", (), {"state": "noop", "reason": None})()

        class Pictures:
            def sync(_self, should_stop):
                order.append("pictures")
                self.assertFalse(should_stop())
                self.sign_in(B_ACCOUNT)
                self.assertTrue(should_stop())
                from claude_session_sync.pictures import PictureReceipt

                return PictureReceipt("stopped", 0, 0)

        payload = self.signed_out_sync(
            live_routine_factory=lambda *_args: Routines(),
            picture_factory=lambda _config: Pictures(),
        )

        self.assertEqual(["routines", "pictures"], order)
        self.assertEqual("committed", payload["state"])
        self.assertIsNotNone(self.read(self.b, X))
        self.assertEqual("stopped", payload["pictures"]["state"])

    def test_an_archive_change_cut_short_is_not_remembered(self):
        from claude_session_sync.chat_sync import run_chat_sync
        from claude_session_sync.model import RunReceipt
        from claude_session_sync.planner import Planner

        # Archived in A only: the archive pass would write B.
        self.write(self.a, X, isArchived=True)
        self.write(self.b, X, isArchived=False)
        engine = TransactionEngine(self.config.state_dir, process_probe=lambda: False)

        class CutAtTheFirstWrite:
            def apply(_self, plan):
                return RunReceipt("r", "interrupted", plan.plan_id, 0, 0, ())

            def close_interrupted_runs(_self):
                return engine.close_interrupted_runs()

        run = run_chat_sync(
            self.config, Planner(app_log=self.app_log), CutAtTheFirstWrite()
        )

        self.assertTrue(run.archive_waiting)
        self.assertFalse(self.read(self.b, X)["isArchived"])
        ledger = self.config.state_dir / "chat-archive-state.json"
        self.assertFalse(ledger.exists())


class ProcessCheckOnABusyMacTests(ChatCliFixture):
    """A process check that takes 3 seconds is slow, not a reason to give up.

    On 04-11-2026 `ps` needed more than the 2.8 seconds left of a 5 second
    budget. Routines were skipped as unsafe and the switch reported a failed
    restart.
    """

    def slow_ps(self):
        """`ps` that needs 3.5 seconds, on a clock that moves only when it runs."""

        from unittest import mock

        from claude_session_sync import processes

        clock = [1000.0]
        calls = []

        def runner(command, **options):
            calls.append(options["timeout"])
            if options["timeout"] < 3.5:
                clock[0] += options["timeout"]
                raise subprocess.TimeoutExpired(command, options["timeout"])
            clock[0] += 3.5
            return subprocess.CompletedProcess(command, 0, stdout="")

        patches = (
            mock.patch.object(
                processes, "ProcessProbe", lambda: ProcessProbe(runner=runner)
            ),
            mock.patch.object(processes.time, "monotonic", lambda: clock[0]),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return calls

    def test_routines_wait_for_a_slow_process_check(self):
        from claude_session_sync.routines import RoutineSynchronizer

        self.config = replace(self.config, sync_code_routines=True)
        calls = self.slow_ps()
        tasks = self.root.resolve() / "tasks"
        tasks.mkdir()

        receipt = RoutineSynchronizer(self.config, task_root=tasks).sync()

        self.assertIn(receipt.state, ("noop", "synced"))
        self.assertGreaterEqual(max(calls), 3.5)

    def test_groups_wait_for_a_slow_process_check(self):
        from claude_session_sync.layout import LayoutSynchronizer

        calls = self.slow_ps()

        LayoutSynchronizer(self.config, helper=Path("/nonexistent"))._assert_stopped()

        self.assertGreaterEqual(max(calls), 3.5)

    def test_every_claude_check_shares_one_budget(self):
        from claude_session_sync.processes import PROCESS_PROBE_TIMEOUT_SECONDS

        self.assertGreaterEqual(PROCESS_PROBE_TIMEOUT_SECONDS, 15.0)


class StopWhenClaudeHoldsTests(unittest.TestCase):
    """The signed-out sync's write guard."""

    def guard(self, signed_in, holds):
        from claude_session_sync.live import StopWhenClaudeHolds

        self.now = 0.0
        self.checks = 0

        def counted():
            self.checks += 1
            return holds()

        return StopWhenClaudeHolds(signed_in, counted, lambda: self.now)

    def test_still_signed_out_it_never_looks_for_claude(self):
        guard = self.guard(lambda: False, lambda: self.fail("no process check"))

        self.assertFalse(guard())

    def test_signed_in_with_claude_open_it_stops(self):
        self.assertTrue(self.guard(lambda: True, lambda: True)())

    def test_signed_in_with_claude_closed_it_goes_on_and_checks_again(self):
        running = [False]
        guard = self.guard(lambda: True, lambda: running[0])

        self.assertFalse(guard())
        self.assertFalse(guard())
        self.assertEqual(1, self.checks, "a second write within a second reuses it")
        running[0] = True
        self.now = 1.5

        self.assertTrue(guard(), "Claude opened again: the writes stop")
        self.assertEqual(2, self.checks)

    def test_a_process_check_that_fails_stops(self):
        def timed_out():
            raise subprocess.TimeoutExpired(["ps"], 15)

        self.assertTrue(self.guard(lambda: True, timed_out)())
