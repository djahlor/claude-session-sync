"""A sync while Claude is open and signed out, so a switch needs no restart.

Real chat folders, planner and engine. Fakes stand in only for Claude itself:
its processes and the signed-in flag it keeps in config.json.
"""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from test_cli_live import (
    A_ACCOUNT,
    B_ACCOUNT,
    ChatCliFixture,
    X,
    Y,
)
from test_routines import manifest, task

from claude_session_sync.config import ApprovedTarget
from claude_session_sync.live import KeepAccountsUnchanged, restart_reasons
from claude_session_sync.logins import signed_in
from claude_session_sync.model import Operation, Plan, Profile
from claude_session_sync.routines import RoutineSynchronizer
from claude_session_sync.transaction import TransactionEngine


class SignedOutSyncTests(ChatCliFixture):
    def setUp(self):
        super().setUp()
        self.running = True
        self.sign_out(A_ACCOUNT)

    def sign_out(self, account):
        self.claude_config(lastKnownAccountUuid=account, windowSizeWasSignedIn=False)

    def sign_in(self, account):
        self.claude_config(lastKnownAccountUuid=account, windowSizeWasSignedIn=True)

    def claude_config(self, **values):
        (self.data_root / "config.json").write_text(
            json.dumps(values), encoding="utf-8"
        )

    def live_engine(self, config, holds, signed_in_again):
        return TransactionEngine(
            config.state_dir,
            process_probe=holds,
            retention=config.retention,
            write_guard=signed_in_again,
        )

    def signed_out_sync(self, **dependencies):
        options = {
            "live_engine_factory": self.live_engine,
            "sleeper": lambda _seconds: None,
        }
        options.update(dependencies)
        code, out, errors = self.cli("signed-out-sync", "Work", "--json", **options)
        self.assertEqual(0, code, errors)
        return json.loads(out)

    def test_every_folder_but_the_account_just_left_gets_the_chats(self):
        self.write(self.a, X)
        self.write(self.b, Y)

        payload = self.signed_out_sync()

        self.assertEqual("committed", payload["state"])
        self.assertIsNotNone(self.read(self.b, X))
        # Claude still saves the account just left while signed out.
        self.assertIsNone(self.read(self.a, Y))
        self.assertEqual(1, payload["counts"]["held_back"])
        self.assertFalse(payload["signed_in_during_sync"])
        self.assertFalse(payload["restart"])
        self.assertEqual([], payload["restart_reasons"])
        self.assertEqual("finished", payload["progress"])

    def test_the_chat_held_back_reaches_the_account_left_at_the_next_quit(self):
        self.write(self.b, Y)
        self.signed_out_sync()

        self.running = False
        code, out, errors = self.cli("sync", "--json")

        self.assertEqual(0, code, errors)
        self.assertIsNotNone(self.read(self.a, Y))

    def test_nothing_is_written_while_claude_is_signed_in(self):
        self.sign_in(B_ACCOUNT)
        self.write(self.a, X)

        payload = self.signed_out_sync()

        self.assertEqual(
            {"state": "skipped", "reason": "signed-in", "restart": True}, payload
        )
        self.assertIsNone(self.read(self.b, X))

    def test_a_config_that_does_not_say_counts_as_signed_in(self):
        self.claude_config(lastKnownAccountUuid=A_ACCOUNT)
        self.write(self.a, X)

        self.assertEqual("signed-in", self.signed_out_sync()["reason"])
        self.assertIsNone(self.read(self.b, X))

    def test_a_closed_claude_is_left_to_the_quit_sync(self):
        self.running = False
        self.write(self.a, X)

        self.assertEqual("claude-closed", self.signed_out_sync()["reason"])
        self.assertIsNone(self.read(self.b, X))

    def test_it_can_be_turned_off(self):
        self.config = replace(self.config, sync_while_signed_out=False)
        self.write(self.a, X)

        self.assertEqual("disabled", self.signed_out_sync()["reason"])
        self.assertIsNone(self.read(self.b, X))

    def test_a_sign_in_before_the_first_write_writes_nothing(self):
        self.write(self.a, X)

        payload = self.signed_out_sync(sleeper=lambda _seconds: self.sign_in(B_ACCOUNT))

        self.assertEqual("waiting", payload["state"])
        self.assertEqual("signed-in", payload["reason"])
        self.assertIsNone(self.read(self.b, X))
        self.assertTrue(payload["signed_in_during_sync"])
        self.assertIn("chats", payload["restart_reasons"])
        # A sign-in race is no fault: the switch restart syncs instead.
        self.assertEqual("waiting-for-Claude", payload["progress"])

    def test_a_new_account_in_the_config_counts_as_signed_in(self):
        self.write(self.a, X)

        payload = self.signed_out_sync(
            sleeper=lambda _seconds: self.sign_out(B_ACCOUNT)
        )

        self.assertEqual("signed-in", payload["reason"])
        self.assertIsNone(self.read(self.b, X))

    def test_a_sign_in_between_two_writes_ends_the_run_there(self):
        self.write(self.a, X)
        self.write(self.a, Y)
        checks = []

        def guard_then_sign_in(config, holds, signed_in_again):
            def again():
                checks.append(True)
                if len(checks) == 2:
                    self.sign_in(B_ACCOUNT)
                return signed_in_again()

            return self.live_engine(config, holds, again)

        payload = self.signed_out_sync(live_engine_factory=guard_then_sign_in)

        # The run ends at the sign-in and keeps the copy written before it.
        self.assertEqual("committed", payload["state"])
        self.assertEqual(1, payload["counts"]["operations"])
        written = [self.read(self.b, X), self.read(self.b, Y)]
        self.assertEqual(1, sum(copy is not None for copy in written))
        self.assertIn("signed-in-during-sync", payload["restart_reasons"])
        # The next sync writes the rest, with no recovery left to do.
        self.running = False
        self.assertEqual(0, self.cli("sync", "--json")[0])
        self.assertIsNotNone(self.read(self.b, X))
        self.assertIsNotNone(self.read(self.b, Y))

    def test_another_profiles_open_claude_stops_it(self):
        other = self.root / "Other"
        self.config = replace(
            self.config,
            profiles=(
                *self.config.profiles,
                Profile("Other", other, ("open",), False),
            ),
        )
        self.write(self.a, X)

        class TwoClaudes:
            def running(probe, **_options):
                return (
                    SimpleNamespace(pid=1, user_data_dir=self.data_root, argv=()),
                    SimpleNamespace(pid=2, user_data_dir=other, argv=()),
                )

        payload = self.signed_out_sync(process_probe=TwoClaudes())

        self.assertEqual("other-profile-open", payload["reason"])
        self.assertIsNone(self.read(self.b, X))

    def test_a_sign_in_during_the_sync_asks_for_the_restart(self):
        self.write(self.a, X)

        def engine(config, holds, signed_in_again):
            inner = self.live_engine(config, holds, signed_in_again)
            apply = inner.apply

            def apply_then_sign_in(plan):
                receipt = apply(plan)
                self.sign_in(B_ACCOUNT)
                return receipt

            inner.apply = apply_then_sign_in
            return inner

        payload = self.signed_out_sync(live_engine_factory=engine)

        self.assertEqual("committed", payload["state"])
        self.assertIsNotNone(self.read(self.b, X))
        self.assertTrue(payload["signed_in_during_sync"])
        self.assertEqual(["signed-in-during-sync"], payload["restart_reasons"])

    def test_an_error_after_the_sync_starts_is_not_left_as_syncing(self):
        from unittest import mock

        from claude_session_sync import cli

        with mock.patch.object(cli, "finish_progress", side_effect=OSError("disk")):
            code, _out, _errors = self.cli(
                "signed-out-sync",
                "Work",
                "--json",
                live_engine_factory=self.live_engine,
                sleeper=lambda _seconds: None,
            )

        self.assertEqual(1, code)
        progress = json.loads(
            (self.config.state_dir / "sync-progress.json").read_text()
        )
        self.assertEqual("needs-attention", progress["state"])

    def test_groups_and_browser_logins_still_need_claude_closed(self):
        self.config = replace(
            self.config, sync_sidebar_layout=True, sync_browser_logins=True
        )
        self.write(self.a, X)

        payload = self.signed_out_sync()

        self.assertIsNotNone(self.read(self.b, X))
        self.assertTrue(payload["restart"])
        self.assertEqual(["groups", "browser-logins"], payload["restart_reasons"])

    def test_an_archive_that_would_change_the_account_left_waits_whole(self):
        # Archived in B with nothing remembered: archived wins, so the archive
        # pass would write into A, which Claude may still save.
        self.write(self.a, X, isArchived=False)
        self.write(self.b, X, isArchived=True)

        payload = self.signed_out_sync()

        self.assertEqual("waiting", payload["archive"])
        self.assertIn("archive", payload["restart_reasons"])
        self.assertFalse(self.read(self.a, X)["isArchived"])
        ledger = self.config.state_dir / "chat-archive-state.json"
        self.assertFalse(ledger.exists())

    def test_an_archive_made_in_the_account_left_reaches_the_others(self):
        self.write(self.a, X, isArchived=True)
        self.write(self.b, X, isArchived=False)

        payload = self.signed_out_sync()

        self.assertNotIn("archive", payload)
        self.assertTrue(self.read(self.b, X)["isArchived"])

    def test_an_archive_in_the_account_left_reaches_its_other_folders_later(self):
        sibling = self.folder(A_ACCOUNT, "aaaaaaaa-0000-4000-8000-0000000000a9")
        self.config = replace(
            self.config,
            approved_targets=(
                *self.config.approved_targets,
                ApprovedTarget("Work", A_ACCOUNT, sibling.name),
            ),
        )
        for folder in (self.a, sibling, self.b):
            self.write(folder, X, isArchived=False)
        self.running = False
        self.assertEqual(0, self.cli("sync", "--json")[0])
        self.running = True
        self.write(self.a, X, isArchived=True)
        os.utime(self.a / f"local_{X}.json")

        payload = self.signed_out_sync()

        self.assertNotIn("archive", payload)
        self.assertEqual([], payload["restart_reasons"])
        self.assertTrue(self.read(self.b, X)["isArchived"])
        self.assertFalse(self.read(sibling, X)["isArchived"])

        self.running = False
        self.assertEqual(0, self.cli("sync", "--json")[0])
        self.assertTrue(self.read(sibling, X)["isArchived"])

    def test_routines_sync_into_every_account_but_the_one_left(self):
        # macOS keeps temporary folders behind a symlink, which routines refuse.
        skill = self.root.resolve() / "tasks" / "daily" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("prompt", encoding="utf-8")
        daily = task("daily", str(skill))
        (self.a / "scheduled-tasks.json").write_text(
            json.dumps(manifest(daily)), encoding="utf-8"
        )
        self.config = replace(self.config, sync_code_routines=True)

        def routines(config, holds, read_only):
            return RoutineSynchronizer(
                config,
                task_root=skill.parent.parent,
                process_probe=lambda *_args, **_kwargs: holds(),
                read_only_accounts=read_only,
            )

        payload = self.signed_out_sync(live_routine_factory=routines)

        self.assertEqual("synced", payload["routines"]["state"])
        written = json.loads((self.b / "scheduled-tasks.json").read_text())
        self.assertEqual(["daily"], [item["id"] for item in written["scheduledTasks"]])
        self.assertFalse(payload["restart"])


class KeepAccountsUnchangedTests(unittest.TestCase):
    def operation(self, account, kind="create"):
        folder = Path("/data/claude-code-sessions") / account / "org"
        return Operation(
            kind, X, Path("/stage/x"), folder / f"local_{X}.json", "d", None, 7
        )

    def test_writes_into_a_kept_account_are_held_back(self):
        applied = []

        class Engine:
            def apply(self, plan):
                applied.append(plan)
                return plan

        engine = KeepAccountsUnchanged(Engine(), frozenset({A_ACCOUNT}))
        plan = Plan(
            (
                self.operation(A_ACCOUNT),
                self.operation(B_ACCOUNT.upper()),
                self.operation(A_ACCOUNT, "retire"),
            ),
            (),
            "plan",
            21,
        )

        engine.apply(plan)

        self.assertEqual(2, engine.held_back)
        self.assertEqual(1, len(applied[0].operations))
        self.assertEqual(7, applied[0].total_bytes)


class RestartReasonTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        from claude_session_sync.config import Config

        self.config = Config(
            profiles=(Profile("Work", root, ("open",), True),),
            state_dir=root / "state",
            retention=5,
            acknowledge_cross_profile_copy=False,
            acknowledge_cross_account_copy=True,
            claude_executable=Path("/usr/bin/true"),
        )

    def test_a_clean_run_needs_no_restart(self):
        self.assertEqual(
            [], restart_reasons(self.config, {"state": "noop", "routines": {}})
        )

    def test_waiting_routines_need_the_restart(self):
        payload = {"state": "committed", "routines": {"state": "deferred"}}

        self.assertEqual(["routines"], restart_reasons(self.config, payload))


class SignedInFlagTests(unittest.TestCase):
    def test_reads_only_a_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            for value, expected in ((True, True), (False, False), ("no", None)):
                path.write_text(json.dumps({"windowSizeWasSignedIn": value}))
                self.assertIs(expected, signed_in(root))
            os.unlink(path)
            self.assertIsNone(signed_in(root))


if __name__ == "__main__":
    unittest.main()
