"""Command-line behavior of live chat sync with the built-in planner."""

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from claude_session_sync.chat_state import load_state, state_path
from claude_session_sync.cli import CliDependencies, run
from claude_session_sync.config import ApprovedTarget, Config
from claude_session_sync.model import Profile
from claude_session_sync.planner import Planner
from claude_session_sync.transaction import TransactionEngine

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


class LiveCliTests(unittest.TestCase):
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

    def read(self, folder, session_id):
        path = folder / "local_{}.json".format(session_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def cli(self, *arguments):
        out = io.StringIO()
        errors = io.StringIO()
        dependencies = CliDependencies(
            config_loader=lambda _path: self.config,
            planner_factory=lambda _config: Planner(app_log=self.app_log),
            process_probe=FakeProbe(self),
            engine_factory=lambda config: TransactionEngine(
                config.state_dir, process_probe=lambda: self.running, retention=config.retention
            ),
            layout_factory=lambda _config: self.fail("pins and groups must not sync"),
            routine_factory=lambda _config: self.fail("routines must not sync"),
            live_sync=True,
        )
        code = run(
            ["--config", str(self.root / "config.json"), *arguments],
            dependencies=dependencies,
            stdout=out,
            stderr=errors,
        )
        return code, out.getvalue(), errors.getvalue()

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

    def test_keep_sidebar_marks_the_signed_in_account_for_the_next_closed_sync(self):
        from dataclasses import replace
        from claude_session_sync.layout import read_pending_adoption

        self.config = replace(self.config, sync_sidebar_layout=True)

        code, out, _errors = self.cli("keep-sidebar", "--dry-run")
        self.assertEqual((0, "state=planned next_action=restart-claude\n"), (code, out))
        self.assertIsNone(read_pending_adoption(self.config.state_dir))

        code, out, _errors = self.cli("keep-sidebar", "--apply")
        self.assertEqual(0, code)
        self.assertEqual("{}/{}".format(A_ACCOUNT, A_ORG), read_pending_adoption(self.config.state_dir))

    def test_keep_sidebar_refuses_when_pins_and_groups_do_not_sync(self):
        code, out, _errors = self.cli("keep-sidebar", "--apply")

        self.assertEqual(1, code)
        self.assertIn("sidebar-sync-off", out)


if __name__ == "__main__":
    unittest.main()
