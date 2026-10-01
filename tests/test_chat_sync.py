"""End-to-end chat sync over real folders: rules, journal, and state."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from claude_session_sync.chat_state import load_state, save_state, state_path
from claude_session_sync.chat_sync import plan_chat_sync, run_chat_sync
from claude_session_sync.config import ApprovedTarget, Config
from claude_session_sync.fingerprint import fingerprint
from claude_session_sync.model import Profile
from claude_session_sync.planner import Planner
from claude_session_sync.transaction import TransactionEngine

A_ACCOUNT = "aaaaaaaa-0000-4000-8000-000000000001"
A_ORG = "aaaaaaaa-0000-4000-8000-0000000000a1"
B_ACCOUNT = "bbbbbbbb-0000-4000-8000-000000000002"
B_ORG = "bbbbbbbb-0000-4000-8000-0000000000b2"
X = "11111111-1111-4111-8111-111111111111"
Y = "22222222-2222-4222-8222-222222222222"
NOW_MS = int(time.time() * 1000)


class ChatSyncFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data_root = self.root / "Claude"
        self.a = self.folder(A_ACCOUNT, A_ORG)
        self.b = self.folder(B_ACCOUNT, B_ORG)
        self.app_log = self.root / "main.log"
        self.app_log.write_text("", encoding="utf-8")
        self.running = False
        self.signed_in = A_ACCOUNT
        self.config = Config(
            profiles=(Profile("Work", self.data_root, ("open",), True),),
            state_dir=self.root / "state",
            retention=5,
            acknowledge_cross_profile_copy=False,
            acknowledge_cross_account_copy=True,
            claude_executable=Path("/Applications/Claude.app/Contents/MacOS/Claude"),
            approved_targets=(
                ApprovedTarget("Work", A_ACCOUNT, A_ORG),
                ApprovedTarget("Work", B_ACCOUNT, B_ORG),
            ),
            target_policy="logins",
        )
        self.write_config()

    def tearDown(self):
        self.directory.cleanup()

    def folder(self, account, org):
        path = self.data_root / "claude-code-sessions" / account / org
        path.mkdir(parents=True)
        return path

    def write_config(self):
        (self.data_root / "config.json").write_text(
            json.dumps({"lastKnownAccountUuid": self.signed_in, "oauth": "secret"}),
            encoding="utf-8",
        )

    def write(self, folder, session_id, **fields):
        body = {"sessionId": "local_" + session_id, "title": "t", "lastActivityAt": 100}
        body.update(fields)
        path = folder / "local_{}.json".format(session_id)
        path.write_text(json.dumps(body), encoding="utf-8")
        old = time.time() - 60
        os.utime(path, (old, old))
        return path

    def read(self, folder, session_id):
        path = folder / "local_{}.json".format(session_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def planner(self):
        return Planner(app_log=self.app_log)

    def log_logins(self, *logins):
        """Write Claude's login lines: (account left, account signed in, seconds ago)."""

        self.app_log.write_text(
            "".join(
                "{} [info] [account] Login-state transition (loggedOut: true → false, "
                "uuid: {} → {}), clearing oauth cache\n".format(
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - ago)),
                    before,
                    after,
                )
                for before, after, ago in logins
            ),
            encoding="utf-8",
        )

    def sync(self, **options):
        engine = TransactionEngine(self.config.state_dir, process_probe=lambda: self.running)
        return run_chat_sync(self.config, self.planner(), engine, **options)

    def agree(self, session_id, **fields):
        """Record the version both accounts last agreed on."""

        body = {"sessionId": "local_" + session_id, "title": "t", "lastActivityAt": 100}
        body.update(fields)
        state = load_state(state_path(self.config.state_dir))
        state_hash = fingerprint(session_id, json.dumps(body).encode()).state_hash
        for key in self.keys():
            state.sync.synced.setdefault(key, {})[session_id] = state_hash
            state.sync.seen.setdefault(key, set()).add(session_id)
        save_state(state_path(self.config.state_dir), state)

    def phases(self):
        from claude_session_sync.journal import RunJournal

        return [
            RunJournal.load(self.config.state_dir, run.name).phase
            for run in sorted((self.config.state_dir / "runs").iterdir())
        ]

    def keys(self):
        return ["Work/{}/{}".format(A_ACCOUNT, A_ORG), "Work/{}/{}".format(B_ACCOUNT, B_ORG)]


class ChatSyncTests(ChatSyncFixture):
    def test_a_new_chat_is_copied_to_the_other_account(self):
        self.write(self.a, X)

        run = self.sync()

        self.assertEqual(run.receipt.status, "committed")
        self.assertEqual(self.read(self.b, X)["sessionId"], "local_" + X)

    def test_the_side_that_changed_wins_even_when_the_other_file_is_newer(self):
        self.write(self.a, X, title="t", lastActivityAt=100)
        self.write(self.b, X, title="renamed", lastActivityAt=200)
        self.agree(X, title="t", lastActivityAt=100)
        # A click on the stale copy makes its file the newest one.
        stale = self.write(self.a, X, title="t", lastActivityAt=100, lastFocusedAt=999)
        os.utime(stale, None)

        self.sync()

        self.assertEqual(self.read(self.a, X)["title"], "renamed")

    def test_a_click_or_a_connector_list_alone_is_not_copied(self):
        self.write(self.a, X, lastFocusedAt=1, enabledMcpTools={"mcp__a__x": True})
        self.write(self.b, X, lastFocusedAt=2, enabledMcpTools={"mcp__b__y": True})

        run = self.sync()

        self.assertEqual(run.receipt.status, "noop")
        self.assertEqual(self.read(self.b, X)["enabledMcpTools"], {"mcp__b__y": True})

    def test_a_tie_is_reported_and_the_other_chats_still_sync(self):
        self.write(self.a, X, title="one", lastActivityAt=100)
        self.write(self.b, X, title="two", lastActivityAt=100)
        self.write(self.a, Y)
        self.agree(X, title="zero")

        run = self.sync()

        self.assertEqual(run.problems, {"tied": 2})
        self.assertIsNotNone(self.read(self.b, Y))
        self.assertEqual(self.read(self.b, X)["title"], "two")

    def test_prefer_settles_a_tie(self):
        self.write(self.a, X, title="one", lastActivityAt=100)
        self.write(self.b, X, title="two", lastActivityAt=100)
        self.agree(X, title="zero")

        self.sync(prefer="Work/{}/{}".format(A_ACCOUNT, A_ORG))

        self.assertEqual(self.read(self.b, X)["title"], "one")

    def test_a_deleted_chat_stays_deleted(self):
        self.write(self.a, X, lastActivityAt=100)
        (self.b / "deleted_{}".format(X)).write_text(str(NOW_MS - 1_000), encoding="ascii")
        self.agree(X)

        self.sync()

        self.assertIsNone(self.read(self.a, X))
        self.assertTrue((self.a / "deleted_{}".format(X)).exists())
        runs = list((self.config.state_dir / "runs").iterdir())
        self.assertEqual(len(runs), 1, "the retired record is kept in the run journal")

    def test_a_chat_that_vanished_where_it_was_seen_is_not_put_back(self):
        self.write(self.a, X)
        self.write(self.b, X)
        self.sync()
        (self.b / "local_{}.json".format(X)).unlink()

        run = self.sync()

        self.assertEqual(run.problems, {"lost": 1})
        self.assertIsNone(self.read(self.b, X))

    def test_the_agreed_version_is_remembered_after_a_run(self):
        self.write(self.a, X)

        self.sync()

        state = load_state(state_path(self.config.state_dir))
        self.assertEqual({X}, set(state.sync.synced[self.keys()[0]]))
        self.assertEqual({X}, set(state.sync.synced[self.keys()[1]]))
        self.assertEqual(state.sync.seen[self.keys()[1]], {X})

    def test_leftover_folders_are_ignored(self):
        leftover = self.folder("cccccccc-0000-4000-8000-000000000003", A_ORG)
        self.write(leftover, X)

        run = self.sync()

        self.assertEqual(run.plan.ignored_targets, 1)
        self.assertIsNone(self.read(self.a, X))

    def test_an_account_that_logged_in_earlier_joins_with_claude_closed(self):
        new_account = "dddddddd-0000-4000-8000-000000000004"
        new_folder = self.folder(new_account, A_ORG)
        # The new account logged in 10 minutes ago. A is signed in again now.
        self.log_logins((A_ACCOUNT, new_account, 600), (new_account, A_ACCOUNT, 60))
        self.write(self.a, X)
        chat = new_folder / "local_{}.json".format(Y)
        chat.write_text(json.dumps({"sessionId": "local_" + Y, "lastActivityAt": 5}), encoding="utf-8")
        saved = time.time() - 300
        os.utime(chat, (saved, saved))

        run = self.sync()

        self.assertEqual(1, run.newly_enrolled)
        self.assertIsNotNone(self.read(new_folder, X))
        self.assertIsNotNone(self.read(self.a, Y))

    def test_a_login_stays_recorded_after_claudes_log_rotates(self):
        new_account = "dddddddd-0000-4000-8000-000000000004"
        new_folder = self.folder(new_account, A_ORG)
        self.log_logins((A_ACCOUNT, new_account, 600), (new_account, A_ACCOUNT, 300))
        self.write(self.a, X)
        self.assertEqual(0, self.sync().newly_enrolled, "no chat was saved there after the login")

        self.app_log.write_text("", encoding="utf-8")
        (new_folder / "local_{}.json".format(Y)).write_text(
            json.dumps({"sessionId": "local_" + Y, "lastActivityAt": 5}), encoding="utf-8"
        )
        run = self.sync()

        self.assertEqual(1, run.newly_enrolled)
        self.assertIsNotNone(self.read(new_folder, X))

    def test_the_engine_writes_no_chat_while_claude_is_open(self):
        from claude_session_sync.transaction import AppRunningError

        self.write(self.a, X)
        self.running = True

        with self.assertRaises(AppRunningError):
            self.sync()

        self.assertIsNone(self.read(self.b, X))
        self.assertFalse((self.config.state_dir / "runs").exists())

    def test_planning_writes_nothing(self):
        self.write(self.a, X)

        run = plan_chat_sync(self.config, self.planner())

        self.assertEqual(len(run.plan.operations), 1)
        self.assertIsNone(self.read(self.b, X))
        self.assertFalse(state_path(self.config.state_dir).exists())

if __name__ == "__main__":
    unittest.main()


class RecoveryTests(ChatSyncFixture):
    """Paired steps, crashes mid-run, and leftovers of killed runs."""

    def test_a_crash_right_after_a_removal_neither_blocks_sync_nor_loses_the_file(self):
        from unittest.mock import patch
        from claude_session_sync import transaction

        self.write(self.a, X, lastActivityAt=100)
        (self.b / "deleted_{}".format(X)).write_text(str(NOW_MS - 1_000), encoding="ascii")
        self.agree(X)
        real_unlink = transaction.durable_unlink

        def unlink_then_die(path):
            real_unlink(path)
            raise KeyboardInterrupt("killed mid-run")

        with patch.object(transaction, "durable_unlink", unlink_then_die):
            with self.assertRaises(KeyboardInterrupt):
                self.sync()
        self.assertIsNone(self.read(self.a, X))
        run_id = next((self.config.state_dir / "runs").iterdir()).name

        # The removed record can still be put back from the journal.
        TransactionEngine(self.config.state_dir, process_probe=lambda: False).rollback(run_id)
        self.assertIsNotNone(self.read(self.a, X))

        # And the next live run is not blocked by the crash.
        self.write(self.a, Y)
        run = self.sync()
        self.assertEqual("committed", run.receipt.status)
        self.assertIsNotNone(self.read(self.b, Y))

    def test_an_interrupted_run_is_closed_and_the_next_run_goes_on(self):
        from unittest.mock import patch
        from claude_session_sync import transaction
        from claude_session_sync.journal import RunJournal

        self.write(self.a, X)
        with patch.object(transaction, "commit_staged_new", side_effect=KeyboardInterrupt("killed")):
            with self.assertRaises(KeyboardInterrupt):
                self.sync()

        run = self.sync()

        self.assertEqual("committed", run.receipt.status)
        self.assertEqual(1, run.recovered_runs)
        self.assertIsNotNone(self.read(self.b, X))
        self.assertEqual(0, self.sync().recovered_runs)
        self.assertEqual(
            ["committed", "recovered"],
            sorted(
                RunJournal.load(self.config.state_dir, path.name).manifest["receipt"]["status"]
                for path in (self.config.state_dir / "runs").iterdir()
            ),
        )

    def test_a_run_whose_rollback_failed_blocks_chat_sync_until_it_is_rolled_back(self):
        from unittest.mock import patch
        from claude_session_sync import transaction

        z = "33333333-3333-4333-8333-333333333333"
        self.write(self.a, X, title="from a", lastActivityAt=200)
        self.write(self.b, X, title="t", lastActivityAt=100)
        self.write(self.a, Y)
        self.agree(X)

        def write_then_fail(staged, destination):
            transaction.commit_staged(staged, destination)
            raise OSError("disk went away")

        with patch.object(transaction, "commit_staged_new", write_then_fail), patch.object(
            transaction.TransactionEngine, "_rollback_locked", side_effect=OSError("still gone")
        ):
            with self.assertRaises(transaction.RecoveryError):
                self.sync()
        self.assertEqual(["RECOVERY_REQUIRED"], self.phases())
        self.write(self.a, z)

        with self.assertRaises(transaction.RecoveryPendingError):
            self.sync()

        self.assertEqual(["RECOVERY_REQUIRED"], self.phases())
        self.assertIsNone(self.read(self.b, z), "nothing more is written over a half-restored run")
        run_id = next((self.config.state_dir / "runs").iterdir()).name
        TransactionEngine(self.config.state_dir, process_probe=lambda: False).rollback(run_id)
        self.assertEqual("t", self.read(self.b, X)["title"])

        run = self.sync()

        self.assertEqual(0, run.recovered_runs)
        self.assertEqual("from a", self.read(self.b, X)["title"])
        self.assertIsNotNone(self.read(self.b, Y))
        self.assertIsNotNone(self.read(self.b, z))

    def test_a_rollback_cut_short_is_not_closed_as_recovered(self):
        from unittest.mock import patch
        from claude_session_sync import transaction
        from claude_session_sync.journal import RunJournal

        self.write(self.a, X)
        with patch.object(transaction, "commit_staged_new", side_effect=KeyboardInterrupt("killed")):
            with self.assertRaises(KeyboardInterrupt):
                self.sync()
        run_id = next((self.config.state_dir / "runs").iterdir()).name
        RunJournal.load(self.config.state_dir, run_id).set_phase("ABORTING")

        with self.assertRaises(transaction.RecoveryPendingError):
            self.sync()

        self.assertEqual(["ABORTING"], self.phases())

    def test_staged_copies_left_by_a_killed_run_are_swept(self):
        leftover = self.b / ".local_{}.json.0123.stage".format(X)
        leftover.write_text("{}", encoding="utf-8")
        old = time.time() - 3600
        os.utime(leftover, (old, old))
        fresh = self.b / ".local_{}.json.4567.stage".format(Y)
        fresh.write_text("{}", encoding="utf-8")
        self.write(self.a, X)

        self.sync()

        self.assertFalse(leftover.exists())
        self.assertTrue(fresh.exists(), "a staged copy this new may belong to a running sync")
