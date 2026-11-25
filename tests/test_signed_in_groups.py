"""After a sign-in with Claude open: skip the switch restart when groups already match.

Claude's web view merges the new account's groups from Anthropic's servers at
sign-in, then records the account in ccd-sync-owner. The check reads a copy of
Claude's database, so a fake stands in for the bundled LevelDB helper.
"""

import copy
import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_cli_live import A_ACCOUNT, A_ORG, B_ACCOUNT, B_ORG, ChatCliFixture
from test_layout import encoded, section, seeded_slot

from claude_session_sync.layout import LayoutSynchronizer
from claude_session_sync.layout_records import (
    DFRAME_STORE_KEY,
    GROUP_UPLOAD_KEY,
    NOT_RECONCILED,
    SAME_GROUPS,
    SYNC_ORG_KEY,
    SYNC_OWNER_KEY,
    SYNC_QUARANTINE_KEY,
    LayoutError,
    groups_after_sign_in,
    settled_scope,
    workspace_after_switch,
    workspace_with_same_groups,
)
from claude_session_sync.live import (
    GROUPS_CHECK_SECONDS,
    GROUPS_CONFIRM_SECONDS,
    WORKSPACE_CHECK_INTERVAL,
    WORKSPACE_CHECK_SECONDS,
    WORKSPACE_LAUNCH_SECONDS,
)

A_SCOPE = f"{A_ACCOUNT}/{A_ORG}"
B_SCOPE = f"{B_ACCOUNT}/{B_ORG}"
# B's other workspace: Claude opens it first, and the switch copies into it.
B_FIRST = f"{B_ACCOUNT}/{A_ORG}"
MAIN = seeded_slot(section("Popcorn", ["p1", "p2"]), section("Life", ["l1"]))
OLD = seeded_slot(section("Customers", ["p1"]), section("Old", ["l1"]))


def sidebar(*, owner, org, active, slots):
    """Claude's sidebar records as the web view leaves them."""

    state = {"codeSidebarByScope": copy.deepcopy(slots), "lastSidebarScopeKey": active}
    records = {DFRAME_STORE_KEY: encoded({"state": state, "version": 1})}
    if owner is not None:
        records[SYNC_OWNER_KEY] = b"\x01" + owner.encode()
    if org is not None:
        records[SYNC_ORG_KEY] = b"\x01" + org.encode()
    return records


def reconciled(b_slot, **changes):
    """Signed in to B, with B's server groups merged into its slot."""

    options = {
        "owner": B_ACCOUNT,
        "org": B_ORG,
        "active": B_SCOPE,
        "slots": {A_SCOPE: MAIN, B_SCOPE: b_slot},
    }
    options.update(changes)
    return sidebar(**options)


class GroupsAfterSignInTests(unittest.TestCase):
    def test_the_same_groups_need_no_restart(self):
        self.assertEqual(
            (SAME_GROUPS, B_SCOPE),
            groups_after_sign_in(reconciled(MAIN), B_ACCOUNT, A_SCOPE),
        )

    def test_different_groups_keep_the_restart(self):
        self.assertEqual(
            ("different-groups", B_SCOPE),
            groups_after_sign_in(reconciled(OLD), B_ACCOUNT, A_SCOPE),
        )

    def test_groups_that_only_differ_in_order_keep_the_restart(self):
        reordered = seeded_slot(
            section("Life", ["l1"]), section("Popcorn", ["p1", "p2"])
        )

        self.assertEqual(
            "different-groups",
            groups_after_sign_in(reconciled(reordered), B_ACCOUNT, A_SCOPE)[0],
        )

    def test_a_sign_in_claude_has_not_settled_is_read_again(self):
        # Before the merge Claude still names the account just left, and the
        # slot may hold an old copy that happens to match.
        for records in (
            reconciled(MAIN, owner=A_ACCOUNT, org=A_ORG),
            reconciled(MAIN, org=None),
            reconciled(MAIN, active=A_SCOPE),
            reconciled(MAIN, slots={A_SCOPE: MAIN}),
            {},
        ):
            with self.subTest(records=sorted(records)):
                self.assertEqual(
                    (NOT_RECONCILED, None),
                    groups_after_sign_in(records, B_ACCOUNT, A_SCOPE),
                )

    def test_anything_unclear_keeps_the_restart(self):
        quarantined = reconciled(MAIN)
        quarantined[SYNC_QUARANTINE_KEY] = b"\x011"
        other_upload = reconciled(MAIN)
        other_upload[GROUP_UPLOAD_KEY] = b"\x01" + A_SCOPE.encode()
        local = reconciled(
            MAIN, slots={A_SCOPE: {"sections": []}, B_SCOPE: {"sections": []}}
        )
        cases = {
            "claude-settings-sync-quarantined": (quarantined, A_SCOPE),
            "another-upload-pending": (other_upload, A_SCOPE),
            "no-main-account": (reconciled(MAIN), None),
            "main-account-sidebar-missing": (reconciled(MAIN), "c/w"),
            "claude-keeps-sidebar-locally": (local, A_SCOPE),
        }
        for reason, (records, main_scope) in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(
                    reason, groups_after_sign_in(records, B_ACCOUNT, main_scope)[0]
                )

    def test_an_edit_waiting_to_upload_in_the_new_account_is_compared_as_is(self):
        records = reconciled(MAIN)
        records[GROUP_UPLOAD_KEY] = b"\x01" + B_SCOPE.encode()

        self.assertEqual(
            SAME_GROUPS, groups_after_sign_in(records, B_ACCOUNT, A_SCOPE)[0]
        )

    def test_a_slot_of_an_unknown_shape_is_an_error(self):
        broken = reconciled({"sections": [{"id": 1}], "prefsSeeded": True})

        with self.assertRaises(LayoutError):
            groups_after_sign_in(broken, B_ACCOUNT, A_SCOPE)


def moved(second_slot, **changes):
    """Signed in to B: the switch copied into its first workspace, Claude shows its second."""

    options = {
        "owner": B_ACCOUNT,
        "org": B_ORG,
        "active": B_SCOPE,
        "slots": {B_FIRST: MAIN, B_SCOPE: second_slot},
    }
    options.update(changes)
    return sidebar(**options)


class WorkspaceAfterSwitchTests(unittest.TestCase):
    def test_another_workspace_with_other_groups_is_named(self):
        self.assertEqual(
            B_SCOPE, workspace_after_switch(moved(OLD), B_ACCOUNT, B_FIRST)
        )

    def test_the_first_copy_still_waiting_to_upload_does_not_hide_the_move(self):
        records = moved(OLD)
        records[GROUP_UPLOAD_KEY] = b"\x01" + B_FIRST.encode()

        self.assertEqual(B_SCOPE, workspace_after_switch(records, B_ACCOUNT, B_FIRST))

    def test_another_workspace_with_the_same_groups_is_named_to_follow(self):
        self.assertEqual(
            B_SCOPE, workspace_with_same_groups(moved(MAIN), B_ACCOUNT, B_FIRST)
        )
        self.assertIsNone(workspace_after_switch(moved(MAIN), B_ACCOUNT, B_FIRST))
        self.assertIsNone(workspace_with_same_groups(moved(OLD), B_ACCOUNT, B_FIRST))

    def test_nothing_unclear_is_followed(self):
        edited = moved(MAIN)
        edited[GROUP_UPLOAD_KEY] = b"\x01" + B_SCOPE.encode()
        quarantined = moved(MAIN)
        quarantined[SYNC_QUARANTINE_KEY] = b"\x011"
        cases = {
            "the workspace the copy went to": (
                moved(MAIN, org=A_ORG, active=B_FIRST),
                B_FIRST,
            ),
            "a main scope of another account": (moved(MAIN), A_SCOPE),
            "a sign-in Claude has not settled": (moved(MAIN, org=None), B_FIRST),
            "groups edited in the new workspace": (edited, B_FIRST),
            "a quarantined settings sync": (quarantined, B_FIRST),
            "no records": ({}, B_FIRST),
        }
        for name, (records, main_scope) in cases.items():
            with self.subTest(name):
                self.assertIsNone(
                    workspace_with_same_groups(records, B_ACCOUNT, main_scope)
                )

    def test_everything_else_names_nothing(self):
        edited = moved(OLD)
        edited[GROUP_UPLOAD_KEY] = b"\x01" + B_SCOPE.encode()
        other_upload = moved(OLD)
        other_upload[GROUP_UPLOAD_KEY] = b"\x01" + A_SCOPE.encode()
        quarantined = moved(OLD)
        quarantined[SYNC_QUARANTINE_KEY] = b"\x011"
        empty_main = seeded_slot()
        cases = {
            "the workspace the copy went to": (
                moved(OLD, org=A_ORG, active=B_FIRST),
                B_FIRST,
            ),
            "the same groups": (moved(MAIN), B_FIRST),
            "a main scope of another account": (moved(OLD), A_SCOPE),
            "no main scope": (moved(OLD), None),
            "a sign-in Claude has not settled": (moved(OLD, org=None), B_FIRST),
            "a sidebar that still shows the first workspace": (
                moved(OLD, active=B_FIRST),
                B_FIRST,
            ),
            "no slot for the new workspace": (
                moved(OLD, slots={B_FIRST: MAIN}),
                B_FIRST,
            ),
            "no slot for the main scope": (moved(OLD, slots={B_SCOPE: OLD}), B_FIRST),
            "groups edited in the new workspace": (edited, B_FIRST),
            "another account's upload": (other_upload, B_FIRST),
            "a quarantined settings sync": (quarantined, B_FIRST),
            "a main scope without groups": (
                moved(OLD, slots={B_FIRST: empty_main, B_SCOPE: OLD}),
                B_FIRST,
            ),
            "another account signed in": (moved(OLD, owner=A_ACCOUNT), B_FIRST),
            "no records": ({}, B_FIRST),
        }
        for name, (records, main_scope) in cases.items():
            with self.subTest(name):
                self.assertIsNone(
                    workspace_after_switch(records, B_ACCOUNT, main_scope)
                )


class SettledScopeTests(unittest.TestCase):
    def test_the_account_and_workspace_claude_settled_on_are_named(self):
        self.assertEqual(B_SCOPE, settled_scope(reconciled(MAIN)))

    def test_a_sign_in_claude_has_not_settled_names_nothing(self):
        for records in (
            reconciled(MAIN, owner=None),
            reconciled(MAIN, org=None),
            reconciled(MAIN, active=A_SCOPE),
            reconciled(MAIN, slots={A_SCOPE: MAIN}),
            {SYNC_OWNER_KEY: b"\x01" + B_ACCOUNT.encode()},
            {},
        ):
            with self.subTest(records=sorted(records)):
                self.assertIsNone(settled_scope(records))


class SidebarCommandFixture(ChatCliFixture):
    """Claude open and signed in to B, with a fake standing in for its sidebar database."""

    def setUp(self):
        super().setUp()
        self.running = True
        self.config = replace(self.config, sync_sidebar_layout=True)
        self.sign_in(B_ACCOUNT)
        (self.data_root / "Local Storage" / "leveldb").mkdir(parents=True)
        self.helper = self.root / "layoutdb"
        self.helper.write_bytes(b"placeholder")
        os.chmod(self.helper, 0o700)
        self.records = reconciled(MAIN)
        self.reads = 0
        self.main = self.config.state_dir / "sidebar-main-0.json"
        self.main.parent.mkdir(parents=True)
        self.main.write_text(json.dumps({"version": 1, "main_scope": A_SCOPE}))

    def sign_in(self, account):
        (self.data_root / "config.json").write_text(
            json.dumps(
                {"lastKnownAccountUuid": account, "windowSizeWasSignedIn": True}
            ),
            encoding="utf-8",
        )

    def fake_database(self):
        test = self

        class FakeDatabase:
            def __init__(self, helper, database):
                # Only ever a disposable copy, never Claude's live files.
                assert Path(database) != test.data_root / "Local Storage" / "leveldb"

            def get_optional(self, key):
                test.reads += 1
                return test.records.get(key)

            def write(self, records, state_dir):
                raise AssertionError("Claude's database must never be written")

        return FakeDatabase

    def main_scope(self):
        return json.loads(self.main.read_text())["main_scope"]


class SignedInGroupsCommandTests(SidebarCommandFixture):
    """The command the menu-bar helper runs when groups are the last restart reason."""

    def check(self, **dependencies):
        options = {
            "layout_factory": lambda config: LayoutSynchronizer(
                config, helper=self.helper, process_probe=lambda: ()
            ),
            "sleeper": lambda _seconds: None,
        }
        options.update(dependencies)
        with patch("claude_session_sync.layout.LevelDatabase", self.fake_database()):
            code, out, errors = self.cli(
                "signed-in-groups", "Work", "--json", **options
            )
        self.assertEqual(0, code, errors)
        return json.loads(out)

    def test_the_same_groups_keep_claude_open_and_follow_the_new_account(self):
        payload = self.check()

        self.assertEqual(
            {"state": "no-restart", "reason": "same-groups", "restart": False}, payload
        )
        # The next switch copies from the account signed in now.
        self.assertEqual(B_SCOPE, self.main_scope())

    def test_different_groups_restart_and_keep_the_main_account(self):
        self.records = reconciled(OLD)

        payload = self.check()

        self.assertEqual(
            {"state": "restart", "reason": "different-groups", "restart": True}, payload
        )
        self.assertEqual(A_SCOPE, self.main_scope())

    def test_it_waits_for_claude_to_settle_the_sign_in(self):
        # Claude still names the account just left, with B's old copy.
        self.records = reconciled(OLD, owner=A_ACCOUNT, org=A_ORG, active=A_SCOPE)
        waits = []

        def sleeper(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                self.records = reconciled(MAIN)

        payload = self.check(sleeper=sleeper)

        self.assertEqual("same-groups", payload["reason"])
        # Three reads before Claude settled, then one more to confirm.
        self.assertEqual([1.0, 1.0, 1.0, GROUPS_CONFIRM_SECONDS], waits)
        self.assertEqual(B_SCOPE, self.main_scope())

    def test_same_groups_must_still_hold_after_chromiums_next_save(self):
        # Claude recorded the owner before its merged groups reached the disk.
        def sleeper(seconds):
            if seconds == GROUPS_CONFIRM_SECONDS:
                self.records = reconciled(OLD)

        payload = self.check(sleeper=sleeper)

        self.assertEqual(
            {
                "state": "restart",
                "reason": "groups-changed-during-check",
                "restart": True,
            },
            payload,
        )
        self.assertEqual(A_SCOPE, self.main_scope())

    def test_a_sign_in_claude_never_settles_restarts_after_the_time_limit(self):
        self.records = reconciled(MAIN, owner=A_ACCOUNT, org=A_ORG)
        now = [0.0]

        def sleeper(seconds):
            now[0] += seconds

        payload = self.check(clock=lambda: now[0], sleeper=sleeper)

        self.assertEqual(
            {"state": "restart", "reason": "timed-out", "restart": True}, payload
        )
        self.assertGreaterEqual(now[0], GROUPS_CHECK_SECONDS)
        self.assertLessEqual(now[0], GROUPS_CHECK_SECONDS + 1)
        self.assertEqual(A_SCOPE, self.main_scope())

    def test_an_unreadable_copy_restarts(self):
        class Unreadable:
            def get(self, _key):
                raise LayoutError("Claude sidebar record could not be read safely")

        self.records = Unreadable()

        payload = self.check()

        self.assertEqual("sidebar-unreadable", payload["reason"])
        self.assertTrue(payload["restart"])
        self.assertEqual(A_SCOPE, self.main_scope())

    def test_a_copy_with_an_unsafe_entry_restarts(self):
        (self.data_root / "Local Storage" / "leveldb" / "LOG").symlink_to("/etc/hosts")

        payload = self.check()

        self.assertEqual("sidebar-unreadable", payload["reason"])
        self.assertEqual(0, self.reads)

    def test_a_missing_helper_restarts(self):
        self.helper.unlink()

        self.assertEqual("sidebar-unreadable", self.check()["reason"])

    def test_a_closed_claude_restarts(self):
        self.running = False

        payload = self.check()

        self.assertEqual("claude-closed", payload["reason"])
        self.assertEqual(0, self.reads)

    def test_another_switch_during_the_wait_restarts(self):
        self.records = reconciled(MAIN, owner=A_ACCOUNT, org=A_ORG)

        payload = self.check(sleeper=lambda _seconds: self.sign_in(A_ACCOUNT))

        self.assertEqual("account-changed", payload["reason"])
        self.assertEqual(A_SCOPE, self.main_scope())

    def test_a_sign_out_during_the_wait_restarts(self):
        self.records = reconciled(MAIN, owner=A_ACCOUNT, org=A_ORG)

        def sign_out(_seconds):
            (self.data_root / "config.json").write_text(
                json.dumps(
                    {"lastKnownAccountUuid": B_ACCOUNT, "windowSizeWasSignedIn": False}
                )
            )

        self.assertEqual("account-changed", self.check(sleeper=sign_out)["reason"])

    def test_an_unfinished_sidebar_copy_keeps_the_restart(self):
        runs = self.config.state_dir / "layout-account-runs"
        runs.mkdir()
        (runs / "run.json").write_text(
            json.dumps({"version": 2, "state": "PREPARED", "records": []})
        )

        payload = self.check()

        self.assertEqual("main-account-not-saved", payload["reason"])
        self.assertTrue(payload["restart"])
        self.assertEqual(A_SCOPE, self.main_scope())

    def test_a_main_account_changed_during_the_check_is_not_overwritten(self):
        def follow(synchronizer, scope, main_scope):
            self.main.write_text(json.dumps({"version": 1, "main_scope": "c/w"}))
            return original(synchronizer, scope, main_scope)

        original = LayoutSynchronizer.follow_signed_in_account
        with patch.object(LayoutSynchronizer, "follow_signed_in_account", follow):
            payload = self.check()

        self.assertEqual("main-account-not-saved", payload["reason"])
        self.assertEqual("c/w", self.main_scope())

    def test_a_running_sync_keeps_the_restart(self):
        from claude_session_sync.locking import ExclusiveFileLock

        lock = ExclusiveFileLock(
            self.config.state_dir / "switch-handoff.lock", mode="auto", timeout=0
        ).acquire()
        try:
            payload = self.check()
        finally:
            lock.release()

        self.assertEqual("busy", payload["reason"])
        self.assertEqual(0, self.reads)

    def test_a_main_account_choice_waiting_for_the_switch_keeps_the_restart(self):
        pending = self.config.state_dir / "sidebar-adopt-pending.json"
        for content in (
            json.dumps({"version": 1, "source_scope": "c/w"}),
            "not json",
        ):
            with self.subTest(content=content):
                pending.write_text(content)

                payload = self.check()

                self.assertEqual("main-account-choice-pending", payload["reason"])
                self.assertEqual(A_SCOPE, self.main_scope())
                self.assertEqual(0, self.reads)

    def test_claudes_upload_and_quarantine_markers_are_read(self):
        upload = reconciled(MAIN)
        upload[GROUP_UPLOAD_KEY] = b"\x01" + A_SCOPE.encode()
        quarantined = reconciled(MAIN)
        quarantined[SYNC_QUARANTINE_KEY] = b"\x011"
        for reason, records in (
            ("another-upload-pending", upload),
            ("claude-settings-sync-quarantined", quarantined),
        ):
            with self.subTest(reason=reason):
                self.records = records

                self.assertEqual(reason, self.check()["reason"])
                self.assertEqual(A_SCOPE, self.main_scope())

    def test_a_sidebar_sync_holding_its_lock_keeps_the_restart(self):
        from claude_session_sync.locking import ExclusiveFileLock

        lock = ExclusiveFileLock(
            self.config.state_dir / "transaction.lock", mode="auto", timeout=0
        ).acquire()
        try:
            payload = self.check()
        finally:
            lock.release()

        self.assertEqual("main-account-not-saved", payload["reason"])
        self.assertEqual(A_SCOPE, self.main_scope())

    def test_no_main_account_keeps_the_restart(self):
        self.main.unlink()

        payload = self.check()

        self.assertEqual("no-main-account", payload["reason"])
        self.assertFalse(self.main.exists())

    def test_with_sidebar_sync_off_groups_never_need_the_restart(self):
        self.config = replace(self.config, sync_sidebar_layout=False)

        payload = self.check(
            layout_factory=lambda _config: self.fail("nothing to read")
        )

        self.assertEqual(
            {"state": "no-restart", "reason": "sidebar-sync-off", "restart": False},
            payload,
        )

    def test_two_profiles_keep_the_restart(self):
        from claude_session_sync.model import Profile

        second = Profile("Second", self.root / "second", ("open",), False)
        self.config = replace(self.config, profiles=self.config.profiles + (second,))

        payload = self.check()

        self.assertEqual("needs-one-default-profile", payload["reason"])
        self.assertEqual(0, self.reads)


class WorkspaceAfterSwitchCommandTests(SidebarCommandFixture):
    """The command the helper runs once after every switch. Only a certain move restarts."""

    def setUp(self):
        super().setUp()
        self.records = moved(OLD)
        self.main.write_text(json.dumps({"version": 1, "main_scope": B_FIRST}))

    def check(self, **dependencies):
        options = {
            "layout_factory": lambda config: LayoutSynchronizer(
                config, helper=self.helper, process_probe=lambda: ()
            ),
            "sleeper": lambda _seconds: None,
        }
        options.update(dependencies)
        with patch("claude_session_sync.layout.LevelDatabase", self.fake_database()):
            code, out, errors = self.cli(
                "workspace-after-switch", "Work", "--json", **options
            )
        self.assertEqual(0, code, errors)
        return json.loads(out)

    def timed(self, on_sleep=lambda _seconds: None):
        return self.timed_with(on_sleep)

    def timed_with(self, on_sleep=lambda _seconds: None, **dependencies):
        now = [0.0]
        waits = []

        def sleeper(seconds):
            now[0] += seconds
            waits.append(seconds)
            on_sleep(seconds)

        payload = self.check(clock=lambda: now[0], sleeper=sleeper, **dependencies)
        return payload, waits

    def test_a_move_to_a_workspace_with_other_groups_restarts(self):
        payload, waits = self.timed()

        self.assertEqual(
            {"state": "restart", "reason": "workspace-changed", "restart": True},
            payload,
        )
        self.assertEqual([GROUPS_CONFIRM_SECONDS], waits)
        # The check writes nothing: the restart's own sync moves the main scope.
        self.assertEqual(B_FIRST, self.main_scope())

    def test_a_move_seconds_after_claude_reopened_is_still_seen(self):
        self.records = moved(OLD, org=A_ORG, active=B_FIRST)

        reads = []

        def on_sleep(_seconds):
            reads.append(1)
            if len(reads) == 3:
                self.records = moved(OLD)

        payload, waits = self.timed(on_sleep)

        self.assertTrue(payload["restart"])
        self.assertEqual([WORKSPACE_CHECK_INTERVAL] * 3, waits[:3])

    def test_a_claude_that_stays_in_the_first_workspace_stays_open(self):
        self.records = moved(OLD, org=A_ORG, active=B_FIRST)

        payload, waits = self.timed()

        self.assertEqual(
            {"state": "no-restart", "reason": "same-workspace", "restart": False},
            payload,
        )
        self.assertGreaterEqual(sum(waits), WORKSPACE_CHECK_SECONDS)
        self.assertLessEqual(
            sum(waits), WORKSPACE_CHECK_SECONDS + WORKSPACE_CHECK_INTERVAL
        )

    def test_a_move_to_a_workspace_with_the_same_groups_moves_the_main_scope(self):
        # Nothing to copy, so Claude stays open. Groups changed there later
        # must travel at the next switch, which copies from the main scope.
        self.records = moved(MAIN)

        payload, waits = self.timed()

        self.assertEqual(
            {"state": "no-restart", "reason": "same-groups", "restart": False},
            payload,
        )
        self.assertEqual(B_SCOPE, self.main_scope())
        # Seen, then seen again after Chromium's next save, then followed.
        self.assertGreaterEqual(sum(waits), GROUPS_CONFIRM_SECONDS)
        self.assertLess(sum(waits), WORKSPACE_CHECK_SECONDS)

    def test_the_same_groups_seen_once_do_not_move_the_main_scope(self):
        self.records = moved(MAIN)

        def on_sleep(_seconds):
            self.records = moved(MAIN, org=A_ORG, active=B_FIRST)

        payload, _waits = self.timed(on_sleep)

        self.assertEqual("same-workspace", payload["reason"])
        self.assertEqual(B_FIRST, self.main_scope())

    def test_a_move_that_does_not_hold_goes_back_to_watching(self):
        def on_sleep(seconds):
            if seconds == GROUPS_CONFIRM_SECONDS:
                # Back in the workspace the copy went to.
                self.records = moved(OLD, org=A_ORG, active=B_FIRST)

        payload, waits = self.timed(on_sleep)

        self.assertEqual(
            {"state": "no-restart", "reason": "same-workspace", "restart": False},
            payload,
        )
        self.assertGreaterEqual(sum(waits), WORKSPACE_CHECK_SECONDS)
        self.assertEqual(B_FIRST, self.main_scope())

    def test_one_torn_copy_at_the_second_read_does_not_waste_the_check(self):
        class Unreadable:
            def get(self, _key):
                raise LayoutError("Claude sidebar record could not be read safely")

        confirms = []

        def on_sleep(seconds):
            if seconds == GROUPS_CONFIRM_SECONDS:
                confirms.append(1)
                self.records = Unreadable() if len(confirms) == 1 else moved(OLD)
            else:
                self.records = moved(OLD)

        payload, _waits = self.timed(on_sleep)

        self.assertEqual("workspace-changed", payload["reason"])
        self.assertTrue(payload["restart"])
        self.assertEqual(2, len(confirms))

    def test_nothing_unclear_restarts(self):
        class Unreadable:
            def get(self, _key):
                raise LayoutError("Claude sidebar record could not be read safely")

        self.records = Unreadable()
        self.assertFalse(self.timed()[0]["restart"])

        self.records = moved(OLD)
        self.helper.unlink()
        self.assertFalse(self.timed()[0]["restart"])

    def test_a_closed_claude_stays_closed(self):
        self.running = False

        payload, waits = self.timed()

        self.assertEqual(
            {"state": "no-restart", "reason": "claude-closed", "restart": False},
            payload,
        )
        self.assertEqual(0, self.reads)
        # Not listed yet is not closed: it is asked again for the first seconds.
        self.assertGreaterEqual(sum(waits), WORKSPACE_LAUNCH_SECONDS)
        self.assertLess(sum(waits), WORKSPACE_CHECK_SECONDS)

    def test_a_claude_that_is_listed_a_little_after_its_launch_is_still_checked(self):
        self.running = False
        slept = []

        def on_sleep(_seconds):
            slept.append(1)
            if len(slept) == 2:
                self.running = True

        payload, _waits = self.timed(on_sleep)

        self.assertEqual("workspace-changed", payload["reason"])
        self.assertTrue(payload["restart"])

    def test_another_switch_during_the_wait_ends_the_check(self):
        self.records = moved(OLD, org=A_ORG, active=B_FIRST)

        payload, waits = self.timed(lambda _seconds: self.sign_in(A_ACCOUNT))

        self.assertEqual("account-changed", payload["reason"])
        self.assertFalse(payload["restart"])
        self.assertEqual(1, len(waits))

    def test_groups_that_do_not_sync_leave_only_the_chat_folder_to_check(self):
        self.config = replace(self.config, sync_sidebar_layout=False)

        payload, _waits = self.timed()

        self.assertEqual(
            {"state": "no-restart", "reason": "same-workspace", "restart": False},
            payload,
        )

    def joining(self, *keys):
        class Planner:
            def joining_targets(self, _config):
                return list(keys)

        return lambda _config: Planner()

    def test_a_chat_folder_outside_sync_restarts_to_bring_the_chats(self):
        # Claude stayed in the workspace the groups went to, but that
        # account's folder is not part of sync yet.
        self.records = moved(MAIN)
        self.main.write_text(json.dumps({"version": 1, "main_scope": B_SCOPE}))

        payload, waits = self.timed_with(
            planner_factory=self.joining(f"Work/{B_ACCOUNT}/{B_ORG}")
        )

        self.assertEqual(
            {
                "state": "restart",
                "reason": "account-folder-outside-sync",
                "restart": True,
            },
            payload,
        )
        self.assertEqual([GROUPS_CONFIRM_SECONDS], waits)

    def test_a_chat_folder_outside_sync_restarts_when_groups_do_not_sync(self):
        self.config = replace(self.config, sync_sidebar_layout=False)

        payload, _waits = self.timed_with(
            planner_factory=self.joining(f"Work/{B_ACCOUNT}/{B_ORG}")
        )

        self.assertEqual("account-folder-outside-sync", payload["reason"])
        self.assertTrue(payload["restart"])

    def test_another_folder_joining_is_no_reason_to_restart(self):
        self.records = moved(MAIN)

        payload, _waits = self.timed_with(
            planner_factory=self.joining(f"Work/{B_ACCOUNT}/{A_ORG}")
        )

        # The other workspace shows the same groups, so only the main scope follows.
        self.assertEqual("same-groups", payload["reason"])
        self.assertFalse(payload["restart"])

    def test_a_chat_state_that_cannot_be_read_keeps_claude_open(self):
        from claude_session_sync.chat_state import StateUnusable

        class Planner:
            def joining_targets(self, _config):
                raise StateUnusable("chat state is unreadable")

        self.records = moved(MAIN)

        payload, _waits = self.timed_with(planner_factory=lambda _config: Planner())

        self.assertFalse(payload["restart"])
