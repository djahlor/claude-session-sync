import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from claude_session_sync.browser_logins import JAR_ID, JAR_NAME, BrowserLogins, run_browser_logins
from claude_session_sync.cli import CliDependencies, run
from claude_session_sync.config import Config
from claude_session_sync.model import Profile

OTHER_JAR = "persist:launch-preview-some-project"


class FakeClaude:
    """A Claude data folder with a built-in browser jar, no real cookies."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.partition = root / "Partitions" / JAR_NAME
        self.partition.mkdir(parents=True)
        self.sign_in("account-a")
        self.fill(cookies=120, sites=3)

    def sign_in(self, account: str) -> None:
        (self.root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account}))

    def fill(self, *, cookies: int, sites: int) -> None:
        database = self.partition / "Cookies"
        with contextlib.closing(sqlite3.connect(str(database))) as db, db:
            db.execute("create table if not exists cookies (host_key text, name text, encrypted_value blob)")
            db.execute("delete from cookies")
            db.executemany(
                "insert into cookies values (?, ?, ?)",
                [("site{}.test".format(i), "c", b"\x00" * 8) for i in range(cookies)],
            )
        storage = self.partition / "Local Storage" / "leveldb"
        storage.mkdir(parents=True, exist_ok=True)
        (storage / "CURRENT").write_text("MANIFEST-000001\n")
        (storage / "000003.log").write_bytes(b"x" * cookies)
        records = [{"jar": JAR_ID, "root": "site{}.test".format(i), "importedAt": "2026-10-19T19:56:00Z"}
                   for i in range(sites)]
        records.append({"jar": OTHER_JAR, "root": "other.test", "importedAt": "2026-10-19T19:56:00Z"})
        (self.root / "chrome-import-sites.json").write_text(json.dumps(records))

    def clear_like_claude(self) -> None:
        """What Claude does on an account change: empty jar, empty storage, no records."""
        with contextlib.closing(sqlite3.connect(str(self.partition / "Cookies"))) as db, db:
            db.execute("delete from cookies")
        (self.partition / "Cookies-journal").write_bytes(b"")
        for item in (self.partition / "Local Storage" / "leveldb").iterdir():
            item.unlink()
        records = json.loads((self.root / "chrome-import-sites.json").read_text())
        (self.root / "chrome-import-sites.json").write_text(
            json.dumps([r for r in records if r["jar"] != JAR_ID])
        )

    def jar_sites(self):
        records = json.loads((self.root / "chrome-import-sites.json").read_text())
        return sorted(r["root"] for r in records if r["jar"] == JAR_ID)

    def other_sites(self):
        records = json.loads((self.root / "chrome-import-sites.json").read_text())
        return [r["root"] for r in records if r["jar"] == OTHER_JAR]


class BrowserLoginsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.claude = FakeClaude(root / "Claude")
        self.profile = Profile("Work", root / "Claude", ("/usr/bin/true",), True)
        self.state = root / "state"
        self.open = False

    def logins(self):
        return BrowserLogins(self.profile, self.state, lambda: self.open)

    def test_a_healthy_jar_is_saved_with_counts_only(self):
        result = self.logins().snapshot()

        self.assertEqual({"state": "saved", "sites": 3, "cookies": 120}, result)
        meta = json.loads((self.state / "browser-logins" / "Work" / "snapshot" / "meta.json").read_text())
        self.assertNotIn("account-a", json.dumps(meta))
        self.assertEqual("unchanged", self.logins().snapshot()["reason"])

    def test_an_emptied_jar_never_replaces_the_saved_copy(self):
        self.logins().snapshot()
        self.claude.sign_in("account-b")
        self.claude.clear_like_claude()

        self.assertEqual("jar-empty", self.logins().snapshot()["reason"])
        self.assertEqual(120, self.logins().status()["snapshot"]["cookies"])

    def test_a_switch_gets_its_logins_back_before_claude_opens(self):
        self.logins().snapshot()
        self.claude.sign_in("account-b")
        self.claude.clear_like_claude()

        result = self.logins().restore()

        self.assertEqual({"state": "restored", "sites": 3, "cookies": 120}, result)
        self.assertEqual(["site0.test", "site1.test", "site2.test"], self.claude.jar_sites())
        self.assertEqual(["other.test"], self.claude.other_sites())
        self.assertFalse((self.claude.partition / "Cookies-journal").exists())
        self.assertTrue((self.claude.partition / "Local Storage" / "leveldb" / "CURRENT").is_file())
        backups = list((self.state / "browser-logins" / "Work" / "backups").iterdir())
        self.assertEqual(1, len(backups))
        self.assertTrue((backups[0] / "Cookies").is_file())
        self.assertEqual("not-needed", self.logins().restore()["state"])

    def test_a_jar_cleared_in_claude_without_a_switch_stays_cleared(self):
        self.logins().snapshot()
        self.claude.clear_like_claude()

        self.assertEqual("cleared", self.logins().snapshot()["state"])
        self.claude.sign_in("account-b")
        self.assertEqual("cleared-in-claude", self.logins().restore()["reason"])
        self.assertEqual([], self.claude.jar_sites())

    def test_a_clear_seen_only_at_restore_is_also_respected(self):
        self.logins().snapshot()
        self.claude.clear_like_claude()

        self.assertEqual("cleared-in-claude", self.logins().restore()["reason"])
        self.claude.sign_in("account-b")
        self.assertEqual("cleared-in-claude", self.logins().restore()["reason"])

    def test_a_new_import_after_a_clear_is_saved_and_restored_again(self):
        self.logins().snapshot()
        self.claude.clear_like_claude()
        self.logins().snapshot()
        self.claude.fill(cookies=80, sites=2)

        self.assertEqual("saved", self.logins().snapshot()["state"])
        self.claude.sign_in("account-b")
        self.claude.clear_like_claude()
        self.assertEqual({"state": "restored", "sites": 2, "cookies": 80}, self.logins().restore())

    def test_nothing_is_restored_while_claude_runs(self):
        self.logins().snapshot()
        self.claude.sign_in("account-b")
        self.claude.clear_like_claude()
        self.open = True

        self.assertEqual("claude-open", self.logins().restore()["reason"])
        self.assertEqual([], self.claude.jar_sites())

    def test_a_restore_missed_while_claude_runs_comes_back_at_the_next_close(self):
        self.logins().snapshot()
        self.claude.sign_in("account-b")
        self.claude.clear_like_claude()
        self.open = True

        self.assertEqual("claude-open", self.logins().restore()["reason"])
        self.assertTrue(self.logins().status()["snapshot"]["restore_pending"])
        self.open = False
        self.assertEqual({"state": "restored", "sites": 3, "cookies": 120}, self.logins().restore())
        self.assertFalse(self.logins().status()["snapshot"]["restore_pending"])

    def test_a_missed_restore_survives_switching_back_to_the_copys_account(self):
        # 20-10-2026: Claude came back mid-switch, so A's copy was never put
        # back in B. Back in A, the empty jar is the switch, not a clear.
        self.logins().snapshot()
        self.claude.sign_in("account-b")
        self.claude.clear_like_claude()
        self.open = True
        self.assertEqual("jar-empty", self.logins().settle()["reason"])
        self.claude.sign_in("account-a")
        self.claude.clear_like_claude()

        self.assertEqual("jar-empty", self.logins().settle()["reason"])
        self.open = False
        self.assertEqual({"state": "restored", "sites": 3, "cookies": 120}, self.logins().restore())
        self.claude.clear_like_claude()
        self.assertEqual("cleared-in-claude", self.logins().restore()["reason"])

    def test_a_new_import_replaces_a_copy_still_owed(self):
        self.logins().snapshot()
        self.claude.sign_in("account-b")
        self.claude.clear_like_claude()
        self.open = True
        self.logins().settle()
        self.claude.fill(cookies=60, sites=2)

        self.assertEqual("saved", self.logins().settle()["state"])
        self.assertFalse(self.logins().status()["snapshot"]["restore_pending"])
        self.assertTrue(self.logins().status()["snapshot"]["same_account"])

    def test_a_large_drop_waits_one_run_before_it_is_saved(self):
        self.logins().snapshot()
        self.claude.fill(cookies=20, sites=3)

        self.assertEqual("large-drop", self.logins().snapshot()["reason"])
        self.assertEqual(120, self.logins().status()["snapshot"]["cookies"])
        self.assertEqual("saved", self.logins().snapshot()["state"])
        self.assertEqual(20, self.logins().status()["snapshot"]["cookies"])

    def test_restore_without_a_saved_copy_changes_nothing(self):
        self.claude.clear_like_claude()

        self.assertEqual("no-snapshot", self.logins().restore()["reason"])

    def test_the_feature_is_off_unless_configured(self):
        config = make_config(self.profile, self.state)

        self.assertEqual({"state": "disabled"}, run_browser_logins(config, lambda: False, "snapshot"))
        enabled = replace(config, sync_browser_logins=True)
        result = run_browser_logins(enabled, lambda: False, "snapshot")
        self.assertEqual("saved", result["profiles"]["Work"]["state"])


class SwitchRestoresBeforeLaunchTests(unittest.TestCase):
    def test_switch_puts_logins_back_before_it_reopens_claude(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = FakeClaude(root / "Claude")
            profile = Profile("Work", root / "Claude", ("/usr/bin/true",), True)
            config = replace(make_config(profile, root / "state"), sync_browser_logins=True)
            BrowserLogins(profile, config.state_dir, lambda: False).snapshot()
            claude.sign_in("account-b")
            claude.clear_like_claude()
            seen_at_launch = []

            class Launcher:
                def launch(self, command):
                    seen_at_launch.append(claude.jar_sites())

            class Probe:
                def running(self, **_kwargs):
                    return ()

            out = io.StringIO()
            run(
                ["--config", str(root / "config.json"), "switch", "Work", "--json"],
                dependencies=CliDependencies(
                    config_loader=lambda _path: config,
                    process_probe=Probe(),
                    launcher=Launcher(),
                ),
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual([["site0.test", "site1.test", "site2.test"]], seen_at_launch)
            payload = json.loads(out.getvalue())
            self.assertEqual("restored", payload["browser_logins"]["profiles"]["Work"]["state"])

    def test_claude_opened_during_the_chat_sync_still_gets_its_logins(self):
        # 20-10-2026 11:47: the sign-in page reopened Claude 20 seconds into
        # the switch's chat sync, before the old restore step at the end.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = FakeClaude(root / "Claude")
            profile = Profile("Work", root / "Claude", ("/usr/bin/true",), True)
            config = replace(make_config(profile, root / "state"), sync_browser_logins=True)
            BrowserLogins(profile, config.state_dir, lambda: False).snapshot()
            claude.sign_in("account-b")
            claude.clear_like_claude()
            claude_open = []

            class AppRunningError(Exception):
                pass

            def reopen_during_chat_sync(_config):
                claude_open.append(claude.jar_sites())
                raise AppRunningError()

            class Probe:
                def running(self, **_kwargs):
                    return ("claude",) if claude_open else ()

            clock = [0.0]

            def sleep(seconds):
                clock[0] += seconds

            out = io.StringIO()
            run(
                ["--config", str(root / "config.json"), "switch", "Work", "--json"],
                dependencies=CliDependencies(
                    config_loader=lambda _path: config,
                    planner_factory=reopen_during_chat_sync,
                    process_probe=Probe(),
                    launcher=type("Launcher", (), {"launch": lambda self, command: None})(),
                    monotonic=lambda: clock[0],
                    sleeper=sleep,
                ),
                stdout=out,
                stderr=io.StringIO(),
            )

            self.assertEqual([["site0.test", "site1.test", "site2.test"]], claude_open)
            payload = json.loads(out.getvalue())
            self.assertEqual("claude-open", payload["reason"])
            self.assertEqual("restored", payload["browser_logins"]["profiles"]["Work"]["state"])
            self.assertEqual(["site0.test", "site1.test", "site2.test"], claude.jar_sites())


class QuitRestoresAMissedSwitchTests(unittest.TestCase):
    def test_a_quit_puts_back_logins_a_switch_missed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = FakeClaude(root / "Claude")
            profile = Profile("Work", root / "Claude", ("/usr/bin/true",), True)
            config = replace(make_config(profile, root / "state"), sync_browser_logins=True)
            BrowserLogins(profile, config.state_dir, lambda: False).snapshot()
            claude.sign_in("account-b")
            claude.clear_like_claude()
            BrowserLogins(profile, config.state_dir, lambda: True).settle()

            class Probe:
                def running(self, **_kwargs):
                    return ()

            out = io.StringIO()
            run(
                ["--config", str(root / "config.json"), "auto", "--json"],
                dependencies=CliDependencies(config_loader=lambda _path: config, process_probe=Probe()),
                stdout=out,
                stderr=io.StringIO(),
            )

            payload = json.loads(out.getvalue())
            self.assertEqual("restored", payload["browser_logins"]["profiles"]["Work"]["state"])
            self.assertEqual(["site0.test", "site1.test", "site2.test"], claude.jar_sites())


def make_config(profile: Profile, state: Path) -> Config:
    return Config(
        profiles=(profile,),
        state_dir=state,
        retention=5,
        acknowledge_cross_profile_copy=False,
        acknowledge_cross_account_copy=True,
        claude_executable=Path("/usr/bin/true"),
    )


if __name__ == "__main__":
    unittest.main()
