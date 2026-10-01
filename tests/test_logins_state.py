"""The chat state file, Claude's logins, and login-based folder selection."""

import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from claude_session_sync.chat_model import SyncState
from claude_session_sync.chat_state import (
    ChatState,
    StateUnusable,
    encode_state,
    load_state,
    save_state,
)
from claude_session_sync.config import ApprovedTarget, Config
from claude_session_sync.enrollment import new_login_targets, select_targets
from claude_session_sync.fingerprint import normalisation
from claude_session_sync.logins import (
    last_known_account,
    login_dated_by_app,
    observe_logins,
)
from claude_session_sync.model import Profile, Target

ACCOUNT = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER = "bbbbbbbb-0000-4000-8000-000000000002"
NOW_MS = 1_800_000_000_000


def login_line(stamp: str, before: str, after: str) -> str:
    return (
        "{} [info] [account] Login-state transition (loggedOut: true → false, "
        "uuid: {} → {}), clearing oauth cache\n".format(stamp, before, after)
    )


class ChatStateTests(unittest.TestCase):
    def test_round_trip_is_private_and_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "chat-state.json"
            state = ChatState(
                sync=SyncState(synced={"k": {"x": "h"}}, seen={"k": {"x"}}),
                logins={"/root": (ACCOUNT, 5)},
                enrolled=["Work/a/b"],
                last_success_ms=9,
            )

            save_state(path, state)

            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(encode_state(state), encode_state(load_state(path)))

    def test_a_missing_file_is_a_fresh_start(self):
        with tempfile.TemporaryDirectory() as directory:
            state = load_state(Path(directory) / "missing.json")

            self.assertEqual({}, state.sync.synced)

    def test_a_corrupt_file_stops_sync_instead_of_forgetting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat-state.json"
            for content in ("{", "[]", '{"version": 9}', '{"version": 1, "seen": {"k": "x"}}'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(StateUnusable):
                        load_state(path)

    def test_hashes_made_another_way_are_dropped_but_presence_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat-state.json"
            for document in (
                {"version": 2, "normalisation": "older rules", "synced": {"k": {"x": "h"}}},
                {"version": 1, "normalisation": normalisation(), "agreed": {"x": "h"}},
            ):
                with self.subTest(version=document["version"]):
                    document["seen"] = {"k": ["x"]}
                    path.write_text(json.dumps(document), encoding="utf-8")

                    state = load_state(path)

                    self.assertEqual({}, state.sync.synced)
                    self.assertEqual({"k": {"x"}}, state.sync.seen)


    def test_a_state_saved_while_claude_synced_open_loads_and_drops_live_creates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat-state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "normalisation": normalisation(),
                        "synced": {"k": {"x": "h"}},
                        "seen": {"k": ["x"]},
                        "enrolled": ["Work/a/b"],
                        "live_creates": {
                            "k": {"account": ACCOUNT, "login_ms": 5, "pids": [3], "ids": ["x"]}
                        },
                    }
                ),
                encoding="utf-8",
            )

            state = load_state(path)
            save_state(path, state)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({"k": {"x": "h"}}, saved["synced"])
            self.assertEqual(["Work/a/b"], saved["enrolled"])
            self.assertNotIn("live_creates", saved)


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def sign_in(self, account):
        (self.root / "config.json").write_text(
            json.dumps({"lastKnownAccountUuid": account, "oauth:tokenCache": "secret"}),
            encoding="utf-8",
        )

    def test_only_the_account_id_is_read_from_claudes_config(self):
        self.sign_in(ACCOUNT.upper())

        self.assertEqual(ACCOUNT, last_known_account(self.root))

    def test_claudes_log_dates_a_login_and_ignores_a_later_logout(self):
        log = self.root / "main.log"
        stamp = "2026-09-22 14:56:48"
        log.write_text(
            login_line("2026-09-22 14:39:21", OTHER, ACCOUNT)
            + login_line(stamp, ACCOUNT, OTHER)
            + login_line("2026-09-22 15:00:00", OTHER, ACCOUNT),
            encoding="utf-8",
        )
        expected = int(time.mktime(time.strptime("2026-09-22 15:00:00", "%Y-%m-%d %H:%M:%S"))) * 1000

        self.assertEqual(expected, login_dated_by_app(log, ACCOUNT, NOW_MS))
        self.assertIsNone(login_dated_by_app(log, OTHER, NOW_MS))

    def test_a_login_the_log_does_not_name_is_dated_at_first_sight(self):
        self.sign_in(ACCOUNT)
        logins = {}

        observe_logins([self.root], logins, NOW_MS, lambda _root: None)

        self.assertEqual((ACCOUNT, NOW_MS), logins[str(self.root)])

    def test_a_login_time_in_the_future_is_read_as_now(self):
        log = self.root / "main.log"
        log.write_text(login_line("2099-01-01 00:00:00", OTHER, ACCOUNT), encoding="utf-8")

        self.assertEqual(NOW_MS, login_dated_by_app(log, ACCOUNT, NOW_MS))


class EnrollmentTests(unittest.TestCase):
    def config(self, root, policy="logins", approved=()):
        return Config(
            profiles=(Profile("Work", root, ("open",), True),),
            state_dir=root / "state",
            retention=5,
            acknowledge_cross_profile_copy=False,
            acknowledge_cross_account_copy=True,
            claude_executable=Path("/Applications/Claude.app/Contents/MacOS/Claude"),
            approved_targets=tuple(ApprovedTarget("Work", a, w) for a, w in approved),
            target_policy=policy,
        )

    def target(self, root, account, workspace):
        path = root / "claude-code-sessions" / account / workspace
        path.mkdir(parents=True, exist_ok=True)
        return Target("Work", account, workspace, path)

    def test_logins_select_approved_and_joined_folders_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = self.target(root, ACCOUNT, "w1")
            two = self.target(root, OTHER, "w2")
            old = self.target(root, "old-account", "w3")
            config = self.config(root, approved=[(ACCOUNT, "w1")])

            selected, ignored = select_targets(config, [one, two, old], ["Work/{}/w2".format(OTHER)])

            self.assertEqual([one, two], list(selected))
            self.assertEqual([old], list(ignored))

    def test_every_folder_takes_part_under_the_all_folders_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = [self.target(root, ACCOUNT, "w1"), self.target(root, OTHER, "w2")]

            selected, ignored = select_targets(self.config(root, "all-configured-profiles"), targets, [])

            self.assertEqual((targets, []), (list(selected), list(ignored)))

    def test_a_folder_joins_once_claude_writes_a_chat_there_after_the_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(
                json.dumps({"lastKnownAccountUuid": ACCOUNT}), encoding="utf-8"
            )
            fresh = self.target(root, ACCOUNT, "fresh")
            stale = self.target(root, ACCOUNT, "stale")
            other = self.target(root, OTHER, "other")
            login_ms = int(time.time() * 1000) - 60_000
            for target in (fresh, stale, other):
                (target.path / "local_x.json").write_text("{}", encoding="utf-8")
            old = (login_ms - 60_000) / 1000
            os.utime(stale.path / "local_x.json", (old, old))

            joined = new_login_targets(
                self.config(root),
                [fresh, stale, other],
                set(),
                {Path(os.path.abspath(root))},
                {str(Path(os.path.abspath(root))): (ACCOUNT, login_ms)},
            )

            self.assertEqual(["Work/{}/fresh".format(ACCOUNT)], joined)

    def test_nothing_joins_while_claude_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(
                json.dumps({"lastKnownAccountUuid": ACCOUNT}), encoding="utf-8"
            )
            fresh = self.target(root, ACCOUNT, "fresh")
            (fresh.path / "local_x.json").write_text("{}", encoding="utf-8")

            joined = new_login_targets(
                self.config(root), [fresh], set(), set(), {str(root): (ACCOUNT, 0)}
            )

            self.assertEqual([], joined)


if __name__ == "__main__":
    unittest.main()
