"""Command-line behavior of chat sync on real folders with the real planner and engine."""

import io
import itertools
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from claude_session_sync.chat_state import load_state, state_path
from claude_session_sync.cli import CliDependencies, run
from claude_session_sync.config import ApprovedTarget, Config
from claude_session_sync.locking import ExclusiveFileLock
from claude_session_sync.model import Profile
from claude_session_sync.planner import Planner
from claude_session_sync.transaction import TransactionEngine
from test_cli import FakeLauncher, FakeLayout, FakeRoutine, ManualClock, SequencedProcessProbe

A_ACCOUNT = "aaaaaaaa-0000-4000-8000-000000000001"
A_ORG = "aaaaaaaa-0000-4000-8000-0000000000a1"
B_ACCOUNT = "bbbbbbbb-0000-4000-8000-000000000002"
B_ORG = "bbbbbbbb-0000-4000-8000-0000000000b2"
OLD_ACCOUNT = "cccccccc-0000-4000-8000-000000000003"
X = "11111111-1111-4111-8111-111111111111"
Y = "22222222-2222-4222-8222-222222222222"


class FakeProbe:
    def __init__(self, test):
        self.test = test

    def running(self, **_options):
        if not self.test.running:
            return ()
        return (SimpleNamespace(pid=4242, user_data_dir=self.test.data_root, argv=()),)


class Terminal(io.StringIO):
    """Typed answers on what looks like a terminal."""

    def isatty(self):
        return True


class ChatCliFixture(unittest.TestCase):
    """Real chat folders, planner and engine. Fakes stand in only for Claude itself."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data_root = self.root / "Claude"
        self.a = self.folder(A_ACCOUNT, A_ORG)
        self.b = self.folder(B_ACCOUNT, B_ORG)
        self.old = self.folder(OLD_ACCOUNT, A_ORG)
        self.running = False
        (self.data_root / "config.json").write_text(
            json.dumps({"lastKnownAccountUuid": A_ACCOUNT}), encoding="utf-8"
        )
        self.app_log = self.root / "main.log"
        self.app_log.write_text("", encoding="utf-8")
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

    def tearDown(self):
        self.directory.cleanup()

    def folder(self, account, org):
        path = self.data_root / "claude-code-sessions" / account / org
        path.mkdir(parents=True)
        return path

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

    def engine(self, **options):
        return lambda config: TransactionEngine(
            config.state_dir, process_probe=lambda: self.running, retention=config.retention, **options
        )

    def cli(self, *arguments, **dependencies):
        out = io.StringIO()
        errors = io.StringIO()
        dependencies = replace(
            CliDependencies(
                config_loader=lambda _path: self.config,
                planner_factory=lambda _config: Planner(app_log=self.app_log),
                process_probe=FakeProbe(self),
                engine_factory=self.engine(),
                layout_factory=lambda _config: self.fail("pins and groups must not sync"),
                routine_factory=lambda _config: self.fail("routines must not sync"),
            ),
            **dependencies,
        )
        code = run(
            ["--config", str(self.root / "config.json"), *arguments],
            dependencies=dependencies,
            stdout=out,
            stderr=errors,
        )
        return code, out.getvalue(), errors.getvalue()


class LiveCliTests(ChatCliFixture):
    def test_nothing_syncs_while_claude_is_open_and_the_next_quit_does(self):
        from dataclasses import replace

        self.config = replace(self.config, sync_sidebar_layout=True, sync_code_routines=True)
        self.running = True
        self.write(self.b, Y)

        code, out, errors = self.cli("auto", "--json")

        self.assertEqual(0, code, errors)
        self.assertEqual(
            {"state": "waiting", "reason": "claude-open", "progress": "waiting-for-Claude"},
            json.loads(out),
        )
        self.assertIsNone(self.read(self.a, Y))
        self.assertFalse(state_path(self.config.state_dir).exists())
        self.assertFalse((self.config.state_dir / "runs").exists())

        self.running = False
        self.config = replace(self.config, sync_sidebar_layout=False, sync_code_routines=False)
        code, out, errors = self.cli("auto", "--json")

        self.assertEqual(0, code, errors)
        self.assertEqual("committed", json.loads(out)["state"])
        self.assertIsNotNone(self.read(self.a, Y))

    def test_a_sync_reports_a_run_a_killed_process_left_open(self):
        from unittest.mock import patch
        from claude_session_sync import transaction

        self.write(self.a, X)
        with patch.object(transaction, "commit_staged_new", side_effect=KeyboardInterrupt("killed")):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("sync", "--json")

        code, out, errors = self.cli("sync", "--json")

        self.assertEqual(0, code, errors)
        payload = json.loads(out)
        self.assertEqual("committed", payload["state"])
        self.assertEqual(1, payload["counts"]["recovered_runs"])
        self.assertIsNotNone(self.read(self.b, X))

    def test_output_never_names_a_chat_or_a_path(self):
        self.write(self.a, X, title="private title")

        code, out, _errors = self.cli("sync", "--json")

        self.assertEqual(0, code)
        self.assertNotIn("private title", out)
        self.assertNotIn(X, out)
        self.assertNotIn(str(self.root), out)

    def test_plan_report_lists_actions_without_titles(self):
        self.write(self.a, X, title="private title")

        code, out, _errors = self.cli("plan", "--json", "--report")

        self.assertEqual(0, code)
        self.assertEqual(1, json.loads(out)["counts"]["creates"])
        report = json.loads((self.config.state_dir / "plan-report.json").read_text())
        self.assertEqual(
            [{"kind": "create", "artifact": "record", "session": X,
              "from": "aaaaaaaa/aaaaaaaa", "to": "bbbbbbbb/bbbbbbbb"}],
            report["actions"],
        )
        self.assertNotIn("private title", json.dumps(report))
        self.assertIsNone(self.read(self.b, X), "planning writes no chat")

    def test_seed_state_starts_from_the_old_folders(self):
        self.write(self.old, X, title="t", lastActivityAt=100)
        self.write(self.a, X, title="t", lastActivityAt=100)
        self.write(self.b, X, title="renamed", lastActivityAt=100)

        code, out, errors = self.cli("seed-state", "--from-unenrolled", "--dry-run")
        self.assertEqual(0, code, errors)
        self.assertIn("'seeded': 1", out)
        self.assertFalse(state_path(self.config.state_dir).exists())

        self.cli("seed-state", "--from-unenrolled", "--apply")
        self.cli("sync")

        # The old copy shows the rename is the change, although activity is tied.
        self.assertEqual("renamed", self.read(self.a, X)["title"])

    def test_prefer_settles_a_tie_from_the_command_line(self):
        self.write(self.a, X, title="one", lastActivityAt=100)
        self.write(self.b, X, title="two", lastActivityAt=100)

        code, out, _errors = self.cli("sync", "--json")
        self.assertEqual(2, json.loads(out)["counts"]["tied"])
        self.assertEqual("run-plan-report", json.loads(out)["next_action"])

        self.cli("sync", "--prefer", "{}/{}".format(A_ACCOUNT[:8], A_ORG[:8]))

        self.assertEqual("one", self.read(self.b, X)["title"])

    def test_forget_lost_lets_the_next_sync_put_a_chat_back(self):
        self.write(self.a, X)
        self.cli("sync")
        (self.b / "local_{}.json".format(X)).unlink()
        _code, out, _errors = self.cli("sync", "--json")
        self.assertEqual(1, json.loads(out)["counts"]["lost"])

        self.cli("forget-lost", X)
        self.cli("sync")

        self.assertIsNotNone(self.read(self.b, X))

    def test_restart_claude_leaves_a_request_for_the_helper(self):
        code, out, _errors = self.cli("restart-claude")

        self.assertEqual(0, code)
        self.assertEqual("state=requested\n", out)
        self.assertTrue((self.config.state_dir / "restart-request").exists())

    def test_a_corrupt_state_file_stops_chat_sync_with_a_clear_reason(self):
        self.write(self.a, X)
        state_file = state_path(self.config.state_dir)
        state_file.parent.mkdir(parents=True)
        state_file.write_text("{", encoding="utf-8")

        code, out, _errors = self.cli("sync", "--json")

        payload = json.loads(out)
        self.assertEqual(1, code)
        self.assertEqual("state-unusable", payload["reason"])
        self.assertIsNone(self.read(self.b, X))

    def test_the_state_remembers_new_folders_and_ignores_leftovers(self):
        self.write(self.old, X)

        _code, out, _errors = self.cli("sync", "--json")

        self.assertEqual(1, json.loads(out)["counts"]["ignored_folders"])
        self.assertIsNone(self.read(self.a, X))
        self.assertEqual({}, load_state(state_path(self.config.state_dir)).sync.synced)

    def sidebar(self, groups_by_folder, *, signed_in, pins=()):
        """A fake sidebar database: {(account, org): group names}."""
        from unittest.mock import patch

        from claude_session_sync import layout

        scopes = {
            "{}/{}".format(account, org): {
                "groups": [{"id": "id-" + name, "name": name} for name in names],
                "assignments": {},
                "order": {},
            }
            for (account, org), names in groups_by_folder.items()
        }
        state = {
            "customGroupsByScope": scopes,
            "pinnedOrder": list(pins),
            "homeProjectsPinnedOrder": [],
            "lastSidebarScopeKey": None,
        }
        records = {
            layout.GROUP_SCOPES_KEY: layout._encode_record({"value": scopes, "timestamp": 1}),
            layout.LOCAL_SLICE_KEY: layout._encode_record({"value": {"pinnedOrder": list(pins)}, "timestamp": 1}),
            layout.DFRAME_STORE_KEY: layout._encode_record({"state": state, "version": 4}),
            layout.SYNC_OWNER_KEY: b"\x01" + signed_in.encode("utf-8"),
        }

        class FakeDatabase:
            def __init__(self, _helper, database):
                self.database = database

            def get(self, key):
                return records[key]

            def get_optional(self, key):
                return records.get(key)

        live = self.data_root / "Local Storage" / "leveldb"
        live.mkdir(parents=True, exist_ok=True)
        (live / "CURRENT").write_bytes(b"MANIFEST-000001\n")
        helper = self.root / "bin" / "layoutdb"
        helper.parent.mkdir(exist_ok=True)
        helper.write_bytes(b"fixture")
        helper.chmod(0o700)
        return patch.object(layout, "LevelDatabase", FakeDatabase)

    def test_keep_sidebar_lists_accounts_and_marks_the_signed_in_one_for_the_next_closed_sync(self):
        from dataclasses import replace
        from claude_session_sync.layout import read_pending_adoption

        self.config = replace(self.config, sync_sidebar_layout=True)
        self.write(self.a, X)
        self.write(self.a, Y)
        self.write(self.b, X)
        self.running = True  # The list reads a copy, so Claude may stay open.

        with self.sidebar(
            {(A_ACCOUNT, A_ORG): ["Focus", "Admin"], (B_ACCOUNT, B_ORG): ["Old"]},
            signed_in=A_ACCOUNT,
            pins=["code:local_" + X],
        ):
            code, out, errors = self.cli("keep-sidebar", "--dry-run")
            self.assertEqual(0, code, errors)
            self.assertEqual(
                "  1  signed in     2 groups     1 pin        2 chats   Focus, Admin\n"
                "  2                1 group      1 pin        1 chat    Old\n"
                "state=planned account=1 next_action=restart-claude\n",
                out,
            )
            self.assertIsNone(read_pending_adoption(self.config.state_dir))

            code, out, _errors = self.cli("keep-sidebar", "--apply")
        self.assertEqual(0, code)
        self.assertTrue(out.endswith("state=pending account=1 next_action=restart-claude\n"))
        self.assertEqual("{}/{}".format(A_ACCOUNT, A_ORG), read_pending_adoption(self.config.state_dir))

    def test_keep_sidebar_lists_accounts_the_next_sync_would_join_on_a_fresh_install(self):
        from dataclasses import replace

        self.config = replace(self.config, sync_sidebar_layout=True, approved_targets=())
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 600))
        self.app_log.write_text(
            "".join(
                "{} [info] [account] Login-state transition (loggedOut: true \u2192 false, "
                "uuid: <none> \u2192 {}), clearing oauth cache\n".format(stamp, account)
                for account in (B_ACCOUNT, A_ACCOUNT)
            ),
            encoding="utf-8",
        )
        self.write(self.a, X)
        self.write(self.b, Y)

        with self.sidebar(
            {(A_ACCOUNT, A_ORG): ["Focus"], (B_ACCOUNT, B_ORG): ["Old", "Ideas"]}, signed_in=A_ACCOUNT
        ):
            code, out, errors = self.cli("keep-sidebar", "--dry-run")

        self.assertEqual(0, code, errors)
        self.assertEqual(
            "  1  signed in     1 group      0 pins       1 chat    Focus\n"
            "  2                2 groups     0 pins       1 chat    Old, Ideas\n"
            "state=planned account=1 next_action=restart-claude\n",
            out,
        )
        self.assertFalse(state_path(self.config.state_dir).exists(), "listing writes no chat state")

    def test_keep_sidebar_shows_three_group_names_then_how_many_more(self):
        from dataclasses import replace

        self.config = replace(self.config, sync_sidebar_layout=True)
        long_name = "A very long group name that keeps going"
        with self.sidebar(
            {(A_ACCOUNT, A_ORG): ["Focus", "Line\nbreak", long_name, "Four", "Five"], (B_ACCOUNT, B_ORG): []},
            signed_in=A_ACCOUNT,
        ):
            code, out, _errors = self.cli("keep-sidebar", "--dry-run")

        self.assertEqual(0, code)
        self.assertEqual(
            "  1  signed in     5 groups     0 pins       0 chats   "
            "Focus, Line break, A very long group name that k\u2026 and 2 more",
            out.splitlines()[0],
        )
        self.assertEqual("  2                0 groups     0 pins       0 chats", out.splitlines()[1])

    def test_keep_sidebar_keeps_another_account_by_its_row_number(self):
        from dataclasses import replace
        from claude_session_sync.layout import read_pending_adoption

        self.config = replace(self.config, sync_sidebar_layout=True)
        with self.sidebar(
            {(A_ACCOUNT, A_ORG): ["Focus"], (B_ACCOUNT, B_ORG): ["Old"]}, signed_in=A_ACCOUNT
        ):
            code, out, _errors = self.cli("keep-sidebar", "--account", "2", "--apply")
            self.assertEqual(0, code)
            self.assertEqual("{}/{}".format(B_ACCOUNT, B_ORG), read_pending_adoption(self.config.state_dir))

            code, out, _errors = self.cli("keep-sidebar", "--account", "3", "--apply")
            self.assertEqual(1, code)
            self.assertIn("state=blocked reason=no-such-account", out)

    def test_keep_sidebar_shows_a_short_id_when_two_rows_look_the_same(self):
        from dataclasses import replace

        self.config = replace(self.config, sync_sidebar_layout=True)
        with self.sidebar(
            {(A_ACCOUNT, A_ORG): ["Focus"], (B_ACCOUNT, B_ORG): ["Focus"]}, signed_in=OLD_ACCOUNT
        ):
            code, out, _errors = self.cli("keep-sidebar", "--dry-run")

        self.assertEqual(1, code)
        self.assertEqual(
            "  1             aaaaaaaa/aaaaaaaa     1 group      0 pins       0 chats   Focus\n"
            "  2             bbbbbbbb/bbbbbbbb     1 group      0 pins       0 chats   Focus\n"
            "state=blocked reason=signed-in-account-not-synced\n",
            out,
        )
        self.assertNotIn(A_ACCOUNT, out, "never a full account ID")

    def test_keep_sidebar_refuses_when_pins_and_groups_do_not_sync(self):
        code, out, _errors = self.cli("keep-sidebar", "--apply")

        self.assertEqual(1, code)
        self.assertIn("sidebar-sync-off", out)


    def install(self, answers, *, tty=True, output_tty=True, second_profile=False):
        from claude_session_sync.installer import InstallReport

        test = self
        document = {
            "version": 1,
            "approved_targets": [
                {"profile": "Work", "account": A_ACCOUNT, "workspace": A_ORG},
                {"profile": "Work", "account": B_ACCOUNT, "workspace": B_ORG},
            ],
            "profiles": [
                {"name": "Work", "data_root": str(self.data_root), "launch_command": ["open"], "is_default": True}
            ],
            "state_dir": str(self.config.state_dir),
            "retention": 5,
            "acknowledge_cross_profile_copy": second_profile,
            "acknowledge_cross_account_copy": True,
            "target_policy": "logins",
            "claude_executable": str(self.config.claude_executable),
        }
        if second_profile:
            document["profiles"].append(
                {"name": "Personal", "data_root": str(self.root / "Personal"), "launch_command": ["open"],
                 "is_default": False}
            )
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(document), encoding="utf-8")

        class Installer:
            def default_config_data(self):
                raise AssertionError("the config file exists")

            def setup(self, *, dry_run, config_data=None, before_activation=None):
                if before_activation is not None:
                    before_activation(test.root / "bin" / "layoutdb")
                return InstallReport("installed", ())

        out = Terminal() if output_tty else io.StringIO()
        dependencies = CliDependencies(
            planner_factory=lambda _config: Planner(app_log=self.app_log),
            installer_factory=lambda _path: Installer(),
        )
        code = run(
            ["--config", str(config_path), "setup", "--automatic-targets", "--sync-layout",
             "--ask-main-account", "--apply"],
            dependencies=dependencies,
            stdout=out,
            stderr=io.StringIO(),
            stdin=(Terminal if tty else io.StringIO)(answers),
        )
        return code, out.getvalue()

    def pending(self):
        from claude_session_sync.layout import read_pending_adoption

        return read_pending_adoption(self.config.state_dir)

    def two_grouped_accounts(self):
        self.write(self.a, X)
        self.write(self.b, Y)
        return self.sidebar(
            {(A_ACCOUNT, A_ORG): ["Focus", "Admin"], (B_ACCOUNT, B_ORG): ["Old"]}, signed_in=A_ACCOUNT
        )

    def test_the_question_shows_the_accounts_and_keeps_the_typed_one(self):
        with self.two_grouped_accounts():
            code, out = self.install("2\n")

        self.assertEqual(0, code)
        self.assertEqual(
            "\nWhich account is the main one?\n"
            "The other accounts will copy its pins and groups.\n\n"
            "  1  signed in     2 groups     0 pins       1 chat    Focus, Admin\n"
            "  2                1 group      0 pins       1 chat    Old\n\n"
            "Press Enter for 1, or type a number: "
            "Account 2 is the main one. The others copy its pins and groups "
            "the next time Claude closes.\n\n",
            out[: out.index("state=")],
        )
        self.assertEqual("{}/{}".format(B_ACCOUNT, B_ORG), self.pending())

    def test_enter_keeps_the_signed_in_account_after_a_wrong_answer(self):
        with self.two_grouped_accounts():
            code, out = self.install("9\n\n")

        self.assertEqual(0, code)
        self.assertIn("Choose a number from the list that has groups.\n", out)
        self.assertEqual("{}/{}".format(A_ACCOUNT, A_ORG), self.pending())

    def test_no_question_without_a_terminal(self):
        for tty, output_tty in ((False, True), (True, False)):
            with self.subTest(tty=tty, output_tty=output_tty), self.two_grouped_accounts():
                code, out = self.install("2\n", tty=tty, output_tty=output_tty)

                self.assertEqual((0, None), (code, self.pending()))
                self.assertNotIn("Which account", out)

    def test_a_number_python_cannot_read_asks_again_instead_of_failing_the_install(self):
        with self.two_grouped_accounts():
            code, out = self.install("\u00b2\n\n")

        self.assertEqual(0, code)
        self.assertIn("Choose a number from the list that has groups.\n", out)
        self.assertEqual("{}/{}".format(A_ACCOUNT, A_ORG), self.pending())

    def test_keys_pressed_before_the_question_do_not_answer_it(self):
        from claude_session_sync.cli import _discard_typeahead

        main, follower = os.openpty()
        with open(follower, "r", encoding="utf-8") as terminal:
            os.write(main, b"\n")  # Enter pressed while setup compiled
            _discard_typeahead(terminal)
            os.write(main, b"2\n")
            answer = terminal.readline()
        os.close(main)

        self.assertEqual("2\n", answer)

    def test_no_question_when_only_one_account_has_groups(self):
        self.write(self.a, X)
        with self.sidebar({(A_ACCOUNT, A_ORG): ["Focus"], (B_ACCOUNT, B_ORG): []}, signed_in=A_ACCOUNT):
            code, out = self.install("2\n")

        self.assertEqual((0, None), (code, self.pending()))
        self.assertNotIn("Which account", out)

    def test_no_question_when_the_accounts_hold_the_same_groups(self):
        self.write(self.a, X)
        self.write(self.b, Y)
        with self.sidebar(
            {(A_ACCOUNT, A_ORG): ["Focus", "Admin"], (B_ACCOUNT, B_ORG): ["Admin", "Focus"]}, signed_in=A_ACCOUNT
        ):
            code, out = self.install("2\n")

        self.assertEqual((0, None), (code, self.pending()))
        self.assertNotIn("Which account", out)

    def test_a_main_account_is_refused_plainly_with_two_profiles_enabled(self):
        from dataclasses import replace

        self.config = replace(
            self.config,
            profiles=self.config.profiles + (Profile("Personal", self.root / "Personal", ("open",), False),),
            acknowledge_cross_profile_copy=True,
            sync_sidebar_layout=True,
        )
        reason = "a main account works only with exactly one Claude profile enabled, the default one"
        with self.two_grouped_accounts():
            code, out, _errors = self.cli("keep-sidebar", "--account", "2", "--apply")
            self.assertEqual(
                (1, reason + "\nstate=blocked reason=needs-one-default-profile\n"), (code, out)
            )
            self.assertIsNone(self.pending())

            code, out = self.install("2\n", second_profile=True)

        self.assertEqual((0, None), (code, self.pending()))
        self.assertIn("\nSkipped the main-account question: {}.\n".format(reason), out)
        self.assertNotIn("Which account", out)

    def test_no_question_once_an_account_was_adopted(self):
        state_dir = self.config.state_dir
        state_dir.mkdir(parents=True)
        (state_dir / "sidebar-layout-0.json").write_text(
            json.dumps({"version": 1, "adopted_scope": "{}/{}".format(A_ACCOUNT, A_ORG)})
        )
        with self.two_grouped_accounts():
            code, out = self.install("2\n")

        self.assertEqual((0, None), (code, self.pending()))
        self.assertNotIn("Which account", out)

    def test_an_unreadable_sidebar_skips_the_question_and_installs(self):
        self.write(self.a, X)
        code, out = self.install("2\n")  # no sidebar database or helper

        self.assertEqual((0, None), (code, self.pending()))
        self.assertIn("Skipped the main-account question: sidebar helper is not installed\n", out)
        self.assertIn("state=installed", out)


class ChatCommandTests(ChatCliFixture):
    """plan, sync, and auto as the watcher and a user run them."""

    def test_plan_counts_the_steps_without_naming_a_chat_or_a_path(self):
        size = self.write(self.a, X, title="private title").stat().st_size

        code, out, errors = self.cli("plan", "--json", clock=itertools.count(5.0, 0.012).__next__)

        self.assertEqual((0, ""), (code, errors))
        payload = json.loads(out)
        self.assertIsInstance(payload.pop("plan_id"), str)
        self.assertEqual(
            {
                "bytes": size,
                "counts": {
                    "creates": 1, "ignored_folders": 1, "invalid_replicas": 0,
                    "operations": 1, "replaces": 0, "retires": 0,
                },
                "duration_ms": 12,
                "state": "planned",
            },
            payload,
        )
        for private in ("private title", X, str(self.root)):
            self.assertNotIn(private, out)

    def test_an_unapproved_folder_blocks_manual_approval_mode_and_says_how_to_go_on(self):
        self.config = replace(self.config, target_policy="approved-only")
        self.write(self.a, X)

        code, out, _errors = self.cli("plan", "--json")

        payload = json.loads(out)
        self.assertEqual(1, code)
        self.assertEqual(
            ("blocked_invalid", 1, "approve-targets-or-enable-automatic-targets"),
            (payload["state"], payload["counts"]["invalid_replicas"], payload["next_action"]),
        )
        self.assertNotIn(OLD_ACCOUNT[:8], out)

    def test_sync_copies_a_chat_and_reports_the_run(self):
        size = self.write(self.a, X).stat().st_size

        code, out, errors = self.cli("sync", "--json", clock=itertools.count(10.0, 0.012).__next__)

        self.assertEqual((0, ""), (code, errors))
        payload = json.loads(out)
        self.assertIsInstance(payload.pop("plan_id"), str)
        self.assertRegex(payload.pop("run_id"), "^[0-9a-f]{32}$")
        self.assertEqual(
            {
                "bytes": size,
                "counts": {"ignored_folders": 1, "operations": 1, "planned": 1},
                "duration_ms": 12,
                "state": "committed",
                "progress": "finished",
            },
            payload,
        )
        self.assertEqual("local_" + X, self.read(self.b, X)["sessionId"])

    def test_auto_reports_the_chat_sync_in_plain_text(self):
        size = self.write(self.a, X).stat().st_size

        code, out, errors = self.cli("auto", clock=itertools.count(20.0, 0.012).__next__)

        self.assertEqual((0, ""), (code, errors))
        self.assertRegex(
            out,
            r"^bytes={} counts=\{{'operations': 1, 'planned': 1, 'ignored_folders': 1\}} "
            r"duration_ms=12 plan_id=\S+ run_id=[0-9a-f]{{32}} state=committed progress=finished\n$".format(size),
        )

    def test_auto_waits_without_writing_while_another_writer_holds_the_lock(self):
        self.write(self.a, X)

        with ExclusiveFileLock(self.config.state_dir / "transaction.lock"):
            code, out, errors = self.cli("auto", engine_factory=self.engine(lock_timeout=0))

        self.assertEqual((0, "state=skipped reason=busy progress=waiting-for-sync\n", ""), (code, out, errors))
        self.assertIsNone(self.read(self.b, X))
        self.assertFalse(state_path(self.config.state_dir).exists())

    def test_a_run_left_half_rolled_back_stops_sync_without_naming_it(self):
        from unittest.mock import patch
        from claude_session_sync import transaction
        from claude_session_sync.journal import RunJournal

        self.write(self.a, X)
        with patch.object(transaction, "commit_staged_new", side_effect=KeyboardInterrupt("killed")):
            with self.assertRaises(KeyboardInterrupt):
                self.cli("sync")
        run_id = next((self.config.state_dir / "runs").iterdir()).name
        RunJournal.load(self.config.state_dir, run_id).set_phase("ABORTING")

        code, out, _errors = self.cli("sync", "--json")

        self.assertEqual(1, code)
        self.assertEqual(
            {
                "state": "failed",
                "reason": "recovery-pending",
                "next_action": "run-doctor",
                "error_type": "recovery-pending-error",
                "progress": "needs-attention",
            },
            json.loads(out),
        )
        self.assertNotIn(run_id, out)

    def test_an_unusable_journal_key_stops_sync_without_naming_it(self):
        self.write(self.a, X)
        self.config.state_dir.mkdir(mode=0o700)
        key = self.config.state_dir / "journal.key"
        key.write_bytes(b"short")
        key.chmod(0o600)

        code, out, _errors = self.cli("sync", "--json")

        payload = json.loads(out)
        self.assertEqual(1, code)
        self.assertEqual(
            ("invalid-journal", "journal-error", "run-doctor"),
            (payload["reason"], payload["error_type"], payload["next_action"]),
        )
        self.assertNotIn("key", out)
        self.assertIsNone(self.read(self.b, X))

    def test_a_slow_process_check_asks_for_a_retry_without_naming_the_process(self):
        def slow_probe():
            raise subprocess.TimeoutExpired(["ps", "private-argument"], 2)

        self.write(self.a, X)
        code, out, _errors = self.cli(
            "sync", "--json",
            engine_factory=lambda config: TransactionEngine(config.state_dir, process_probe=slow_probe),
        )

        payload = json.loads(out)
        self.assertEqual(1, code)
        self.assertEqual(
            ("process-inspection-timeout", "process-timeout", "retry-sync"),
            (payload["reason"], payload["error_type"], payload["next_action"]),
        )
        self.assertNotIn("private", out)
        self.assertIsNone(self.read(self.b, X))


class SwitchTests(ChatCliFixture):
    """switch: wait for Claude to quit, sync, then reopen Claude."""

    def launched(self, launches):
        return launches == [("launch", ("open",))]

    def test_a_switch_waits_for_the_other_writer_then_syncs_and_reopens_claude(self):
        self.write(self.a, X)
        writer = ExclusiveFileLock(self.config.state_dir / "transaction.lock").acquire()
        self.addCleanup(writer.release)
        sleeps, launches = [], []

        def sleep(seconds):
            sleeps.append(seconds)
            writer.release()

        code, out, errors = self.cli(
            "switch", "Work", "--json",
            engine_factory=self.engine(lock_timeout=0),
            launcher=FakeLauncher(launches),
            sleeper=sleep,
        )

        self.assertEqual((0, ""), (code, errors))
        self.assertEqual([0.1], sleeps)
        self.assertEqual("committed", json.loads(out)["state"])
        self.assertEqual("local_" + X, self.read(self.b, X)["sessionId"])
        self.assertTrue(self.launched(launches))

    def test_a_switch_waits_for_claude_to_finish_quitting(self):
        probe = SequencedProcessProbe(((object(),), (object(),), ()))
        clock = ManualClock()
        launches = []

        code, _out, errors = self.cli(
            "switch", "Work", "--wait-for-exit", "1",
            process_probe=probe, launcher=FakeLauncher(launches), monotonic=clock, sleeper=clock.sleep,
        )

        self.assertEqual((0, ""), (code, errors))
        self.assertEqual(3, len(probe.calls))
        self.assertEqual([0.1, 0.1], clock.sleeps)
        self.assertTrue(self.launched(launches))

    def test_a_second_switch_during_a_handoff_neither_syncs_nor_launches(self):
        entered, release = threading.Event(), threading.Event()
        launches, first = [], []

        class HeldProbe:
            def running(self, **_options):
                entered.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("the test did not release the first switch")
                return ()

        thread = threading.Thread(
            target=lambda: first.append(
                self.cli("switch", "Work", process_probe=HeldProbe(), launcher=FakeLauncher(launches))
            )
        )
        thread.start()
        self.assertTrue(entered.wait(timeout=5))
        second = self.cli(
            "switch", "Work",
            planner_factory=lambda _config: self.fail("the second switch must not plan"),
            launcher=FakeLauncher(launches),
        )
        release.set()
        thread.join(timeout=5)

        self.assertEqual((1, "state=blocked_switch reason=handoff-running\n", ""), second)
        self.assertEqual(0, first[0][0], first)
        self.assertTrue(self.launched(launches))

    def test_a_failed_launch_command_is_reported_and_the_next_switch_still_syncs(self):
        self.config = replace(
            self.config,
            profiles=(replace(self.config.profiles[0], launch_command=("/bin/sh", "-c", "exit 3")),),
        )
        self.write(self.a, X)
        first = self.cli("switch", "Work")
        self.write(self.a, Y)
        second = self.cli("switch", "Work")

        self.assertEqual([(1, "state=launch_failed reason=launch-command-failed\n", "")] * 2, [first, second])
        self.assertIsNotNone(self.read(self.b, Y), "a failed launch must not block the next switch")

    def test_a_claude_that_has_not_shown_up_yet_does_not_block_the_next_switch(self):
        # The command succeeds, but Claude never shows up in the process list.
        self.config = replace(
            self.config, profiles=(replace(self.config.profiles[0], launch_command=("/usr/bin/true",)),)
        )
        self.write(self.a, X)
        first = self.cli("switch", "Work")[0]
        self.write(self.a, Y)
        second = self.cli("switch", "Work")[0]

        self.assertEqual([0, 0], [first, second])
        self.assertIsNotNone(self.read(self.b, Y))

    def test_a_switch_copies_chats_before_it_reopens_claude(self):
        self.write(self.a, X)
        seen = []
        launcher = SimpleNamespace(
            launch=lambda command: seen.append((tuple(command), self.read(self.b, X) is not None))
        )

        code, out, errors = self.cli("switch", "Work", "--json", launcher=launcher)

        self.assertEqual((0, ""), (code, errors))
        self.assertEqual([(("open",), True)], seen)
        self.assertEqual("finished", json.loads(out)["progress"])

    def test_a_switch_blocked_by_an_unsafe_chat_file_still_reopens_claude(self):
        (self.a / "local_{}.json".format(Y)).symlink_to(self.root / "elsewhere.json")
        launches = []

        code, out, _errors = self.cli("switch", "Work", "--json", launcher=FakeLauncher(launches))

        payload = json.loads(out)
        self.assertEqual(1, code)
        self.assertTrue(self.launched(launches))
        self.assertEqual(
            ("blocked_invalid", "needs-attention", "started"),
            (payload["state"], payload["progress"], payload["launch"]),
        )

    def test_a_switch_whose_chat_sync_fails_still_reopens_claude(self):
        def broken_probe():
            raise RuntimeError("private engine detail")

        self.write(self.a, X)
        launches = []

        code, out, _errors = self.cli(
            "switch", "Work", "--json",
            engine_factory=lambda config: TransactionEngine(config.state_dir, process_probe=broken_probe),
            launcher=FakeLauncher(launches),
        )

        payload = json.loads(out)
        self.assertEqual(1, code)
        self.assertTrue(self.launched(launches))
        self.assertEqual(
            ("failed", "chat-sync-error", "needs-attention", "started"),
            (payload["state"], payload["reason"], payload["progress"], payload["launch"]),
        )
        self.assertNotIn("private", out)

    def test_claude_reopens_even_when_the_switch_crashes_after_claude_closed(self):
        from unittest import mock

        launches = []
        with mock.patch("claude_session_sync.cli.finish_progress", side_effect=OSError("disk full")):
            code, _out, errors = self.cli("switch", "Work", launcher=FakeLauncher(launches))

        self.assertEqual(1, code)
        self.assertIn("disk full", errors)
        self.assertTrue(self.launched(launches))


class AdapterBesideChatTests(ChatCliFixture):
    """Routines and pins and groups report beside a committed chat sync and never change it."""

    def sync(self, **dependencies):
        self.write(self.a, X)
        code, out, errors = self.cli("sync", "--json", **dependencies)
        return code, json.loads(out), errors

    def test_routines_report_beside_a_committed_chat_sync(self):
        self.config = replace(self.config, sync_code_routines=True)

        code, payload, errors = self.sync(routine_factory=lambda _config: FakeRoutine())

        self.assertEqual((0, ""), (code, errors))
        self.assertEqual("committed", payload["state"])
        self.assertEqual(
            {"state": "synced", "profiles": 1, "targets": 3, "manifests": 2, "tasks": 4, "writes": 1},
            payload["routines"],
        )

    def test_a_routine_failure_leaves_the_committed_chat_result(self):
        class BrokenRoutine:
            def sync(self):
                raise RuntimeError("unexpected routine adapter failure")

        self.config = replace(self.config, sync_code_routines=True)

        code, payload, _errors = self.sync(routine_factory=lambda _config: BrokenRoutine())

        self.assertEqual(1, code)
        self.assertEqual(
            ("committed", "needs-attention", {"state": "skipped", "reason": "routine-error"}),
            (payload["state"], payload["progress"], payload["routines"]),
        )

    def test_adopt_current_sidebar_reaches_only_the_layout_adapter(self):
        from test_cli import FakeLayoutReceipt

        calls = []

        class RecordingLayout:
            def sync(self, *, adopt_current_sidebar=False):
                calls.append(adopt_current_sidebar)
                return FakeLayoutReceipt()

        self.config = replace(self.config, sync_sidebar_layout=True, sync_code_routines=True)
        self.write(self.a, X)

        code, out, errors = self.cli(
            "sync", "--adopt-current-sidebar", "--json",
            layout_factory=lambda _config: RecordingLayout(),
            routine_factory=lambda _config: FakeRoutine(),
        )

        self.assertEqual((0, ""), (code, errors))
        self.assertEqual([True], calls)
        self.assertEqual(("committed", "synced"), (json.loads(out)["state"], json.loads(out)["layout"]["state"]))

    def test_pins_and_groups_report_beside_a_committed_chat_sync(self):
        self.config = replace(self.config, sync_sidebar_layout=True)

        code, payload, errors = self.sync(layout_factory=lambda _config: FakeLayout())

        self.assertEqual((0, ""), (code, errors))
        self.assertEqual("committed", payload["state"])
        self.assertEqual(
            {"state": "synced", "profiles": 1, "records": 2, "groups": 4, "assignments": 12, "pins": 3},
            payload["layout"],
        )

    def test_sync_says_plainly_when_no_account_has_groups_yet(self):
        from claude_session_sync.layout import LayoutReceipt

        class EmptyLayout:
            def sync(self):
                return LayoutReceipt("noop", 1, 0, 0, 0, 0, "no-groups-yet")

        self.config = replace(self.config, sync_sidebar_layout=True)

        code, payload, errors = self.sync(layout_factory=lambda _config: EmptyLayout())

        self.assertEqual((0, ""), (code, errors))
        self.assertEqual("finished", payload["progress"])
        self.assertEqual(
            {"state": "noop", "reason": "no-groups-yet", "profiles": 1, "records": 0,
             "groups": 0, "assignments": 0, "pins": 0},
            payload["layout"],
        )

    def test_a_layout_failure_leaves_the_committed_chat_result(self):
        class BrokenLayout:
            def sync(self):
                raise RuntimeError("unexpected adapter failure")

        self.config = replace(self.config, sync_sidebar_layout=True)

        code, payload, _errors = self.sync(layout_factory=lambda _config: BrokenLayout())

        self.assertEqual(1, code)
        self.assertEqual(
            ("committed", "needs-attention", {"state": "skipped", "reason": "layout-error"}),
            (payload["state"], payload["progress"], payload["layout"]),
        )

    def test_an_unsafe_layout_reports_the_check_that_failed(self):
        from claude_session_sync.layout import LayoutError

        class UnsafeLayout:
            def sync(self):
                raise LayoutError("custom group records contain conflicting ids")

        self.config = replace(self.config, sync_sidebar_layout=True)

        code, payload, _errors = self.sync(layout_factory=lambda _config: UnsafeLayout())

        self.assertEqual(1, code)
        self.assertEqual(
            ("committed", "needs-attention", "unsafe-layout", "custom group records contain conflicting ids"),
            (payload["state"], payload["progress"], payload["layout"]["reason"], payload["layout"]["detail"]),
        )


if __name__ == "__main__":
    unittest.main()
