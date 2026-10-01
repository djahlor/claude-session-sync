"""Per-session chat sync rules, ported case for case from vinlim/claude-desktop-sync (0BSD)."""

import unittest

from claude_session_sync.chat_model import (
    Copy,
    CreateRecord,
    CreateTombstone,
    Problem,
    ReplaceRecord,
    RetireRecord,
    RetireTmp,
    RetireTombstone,
    Snapshot,
    SyncState,
)
from claude_session_sync.rules import plan, settle

X = "11111111-1111-4111-8111-111111111111"
Y = "22222222-2222-4222-8222-222222222222"
NOW = 1_800_000_000_000
DELETED_AT = NOW - 1_000


def copy(state_hash, activity=0):
    return Copy(state_hash=state_hash, last_activity_at=activity)


def unreadable():
    return Copy(state_hash=None)


def snapshot(key, records=None, tombstones=None, orphan_tmps=()):
    return Snapshot(
        key=key,
        records=dict(records or {}),
        tombstones=dict(tombstones or {}),
        orphan_tmps=frozenset(orphan_tmps),
    )


def state(agreed=None, seen=None, placed=None, folders="ABC", synced=None):
    """agreed: the version every listed folder last held in step. placed: per-folder overrides."""

    remembered = {key: dict(agreed or {}) for key in folders}
    for key, entries in (placed or {}).items():
        remembered.setdefault(key, {}).update(entries)
    for key, entries in (synced or {}).items():
        remembered[key] = dict(entries)
    return SyncState(
        synced={key: entries for key, entries in remembered.items() if entries},
        seen={key: set(value) for key, value in (seen or {}).items()},
    )


def planned(snapshots, sync_state=None, prefer=None, prefer_session=None):
    return plan(
        snapshots,
        sync_state or state(),
        prefer=prefer,
        prefer_session=prefer_session,
    )


class NewRecordTests(unittest.TestCase):
    def test_a_record_held_by_one_partition_is_created_in_the_other(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B")])

        self.assertEqual(result.actions, [CreateRecord(X, source="A", target="B")])
        self.assertEqual(result.problems, [])

    def test_identical_copies_need_nothing(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v1")})])

        self.assertEqual((result.actions, result.problems), ([], []))


class OneSideChangedTests(unittest.TestCase):
    def test_the_changed_side_replaces_the_side_still_at_the_agreed_state(self):
        result = planned(
            [snapshot("A", {X: copy("v1", 10)}), snapshot("B", {X: copy("v2", 50)})],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A")])

    def test_a_change_without_new_activity_still_wins(self):
        # A rename or an archive moves no activity.
        result = planned(
            [snapshot("A", {X: copy("v1", 50)}), snapshot("B", {X: copy("renamed", 50)})],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A")])

    def test_a_copy_that_went_back_in_time_never_wins(self):
        result = planned(
            [snapshot("A", {X: copy("older", 10)}), snapshot("B", {X: copy("v2", 50)})],
            state(agreed={X: "v2"}),
        )

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A")])

    def test_file_time_plays_no_part_so_a_clicked_stale_copy_never_wins(self):
        result = planned(
            [snapshot("A", {X: copy("v1", 10)}), snapshot("B", {X: copy("v2", 50)})],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A")])

    def test_with_three_partitions_every_unchanged_copy_is_replaced(self):
        result = planned(
            [
                snapshot("A", {X: copy("v1", 10)}),
                snapshot("B", {X: copy("v2", 50)}),
                snapshot("C", {X: copy("v1", 10)}),
            ],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(
            result.actions,
            [
                ReplaceRecord(X, source="B", target="A"),
                ReplaceRecord(X, source="B", target="C"),
            ],
        )


class BothSidesChangedTests(unittest.TestCase):
    def test_the_copy_with_later_activity_wins(self):
        result = planned(
            [snapshot("A", {X: copy("v2", 10)}), snapshot("B", {X: copy("v3", 50)})],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A")])

    def test_first_contact_with_differing_copies_is_decided_the_same_way(self):
        result = planned([snapshot("A", {X: copy("v1", 90)}), snapshot("B", {X: copy("v2", 50)})])

        self.assertEqual(result.actions, [ReplaceRecord(X, source="A", target="B")])

    def test_a_tie_is_left_alone_and_reported_for_every_copy(self):
        result = planned(
            [snapshot("A", {X: copy("v2", 50)}), snapshot("B", {X: copy("v3", 50)})],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("tied", X, "A"), Problem("tied", X, "B")])

    def test_a_tie_blocks_creation_too_because_no_version_is_chosen(self):
        result = planned(
            [
                snapshot("A", {X: copy("v2", 50)}),
                snapshot("B", {X: copy("v3", 50)}),
                snapshot("C"),
            ]
        )

        self.assertEqual(result.actions, [])

    def test_a_tie_on_one_chat_does_not_stop_another(self):
        result = planned(
            [
                snapshot("A", {X: copy("x2", 50), Y: copy("y1", 1)}),
                snapshot("B", {X: copy("x3", 50)}),
            ],
            state(agreed={X: "x1"}),
        )

        self.assertEqual(result.actions, [CreateRecord(Y, source="A", target="B")])
        self.assertEqual({problem.session_id for problem in result.problems}, {X})

    def test_prefer_settles_a_tie(self):
        result = planned(
            [snapshot("A", {X: copy("v2", 50)}), snapshot("B", {X: copy("v3", 50)})],
            state(agreed={X: "v1"}),
            prefer="B",
        )

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A")])
        self.assertEqual(result.problems, [])

    def test_prefer_can_be_limited_to_one_session(self):
        tied = {X: copy("x2", 50), Y: copy("y2", 50)}
        other = {X: copy("x3", 50), Y: copy("y3", 50)}

        result = planned(
            [snapshot("A", tied), snapshot("B", other)],
            state(agreed={X: "x1", Y: "y1"}),
            prefer="B",
            prefer_session=Y,
        )

        self.assertEqual(result.actions, [ReplaceRecord(Y, source="B", target="A")])
        self.assertEqual({problem.session_id for problem in result.problems}, {X})

    def test_prefer_does_not_override_a_clear_winner(self):
        result = planned(
            [snapshot("A", {X: copy("v2", 90)}), snapshot("B", {X: copy("v3", 50)})],
            state(agreed={X: "v1"}),
            prefer="B",
        )

        self.assertEqual(result.actions, [ReplaceRecord(X, source="A", target="B")])

    def test_among_three_versions_the_latest_activity_replaces_both_others(self):
        result = planned(
            [
                snapshot("A", {X: copy("v2", 10)}),
                snapshot("B", {X: copy("v3", 50)}),
                snapshot("C", {X: copy("v1", 5)}),
            ],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(
            result.actions,
            [
                ReplaceRecord(X, source="B", target="A"),
                ReplaceRecord(X, source="B", target="C"),
            ],
        )

    def test_a_copy_already_equal_to_the_winner_is_left_alone(self):
        result = planned(
            [
                snapshot("A", {X: copy("v3", 50)}),
                snapshot("B", {X: copy("v3", 50)}),
                snapshot("C", {X: copy("v2", 10)}),
            ],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(result.actions, [ReplaceRecord(X, source="A", target="C")])


class PlacementTests(unittest.TestCase):
    def test_a_second_change_after_partial_propagation_is_still_one_sided(self):
        result = planned(
            [
                snapshot("A", {X: copy("second", 5)}),
                snapshot("B", {X: copy("first", 5)}),
                snapshot("C", {X: copy("v0", 5)}),
            ],
            state(agreed={X: "v0"}, placed={"B": {X: "first"}}),
        )

        self.assertEqual(
            result.actions,
            [
                ReplaceRecord(X, source="A", target="B"),
                ReplaceRecord(X, source="A", target="C"),
            ],
        )
        self.assertEqual(result.problems, [])

    def test_a_placed_copy_claude_has_since_changed_counts_as_changed(self):
        result = planned(
            [
                snapshot("A", {X: copy("second", 5)}),
                snapshot("B", {X: copy("edited under B", 5)}),
            ],
            state(agreed={X: "v0"}, placed={"B": {X: "first"}}),
        )

        self.assertEqual({problem.kind for problem in result.problems}, {"tied"})


class UnusableCopyTests(unittest.TestCase):
    def test_an_unreadable_copy_freezes_the_session_everywhere(self):
        result = planned(
            [snapshot("A", {X: unreadable()}), snapshot("B", {X: copy("v1")}), snapshot("C")]
        )

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("unreadable", X, "A")])

    def test_a_future_dated_copy_freezes_the_session_and_says_why(self):
        ahead = Copy(state_hash="v2", last_activity_at=10**15, future_dated=True)

        result = planned(
            [snapshot("A", {X: ahead}), snapshot("B", {X: copy("v1")}), snapshot("C")],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("future", X, "A")])


class LostRecordTests(unittest.TestCase):
    def test_a_record_seen_before_and_now_gone_without_a_tombstone_is_not_recreated(self):
        result = planned(
            [snapshot("A", {X: copy("v1")}), snapshot("B")],
            state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}),
        )

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("lost", X, "B")])

    def test_a_loss_in_one_partition_does_not_stop_creation_in_another(self):
        result = planned(
            [snapshot("A", {X: copy("v1")}), snapshot("B"), snapshot("C")],
            state(seen={"B": {X}}),
        )

        self.assertEqual(result.actions, [CreateRecord(X, source="A", target="C")])
        self.assertEqual(result.problems, [Problem("lost", X, "B")])


class DeleteWinsTests(unittest.TestCase):
    def test_a_stale_copy_is_retired_and_then_the_tombstone_travels(self):
        result = planned(
            [
                snapshot("A", {X: copy("v1", DELETED_AT - 5)}),
                snapshot("B", tombstones={X: DELETED_AT}),
            ],
            state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}),
        )

        self.assertEqual(
            result.actions,
            [RetireRecord(X, target="A"), CreateTombstone(X, source="B", target="A")],
        )
        self.assertEqual(result.problems, [])

    def test_a_click_on_the_stale_copy_does_not_undo_the_delete(self):
        result = planned(
            [
                snapshot("A", {X: copy("clicked", DELETED_AT - 5)}),
                snapshot("B", tombstones={X: DELETED_AT}),
            ],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(result.actions[0], RetireRecord(X, target="A"))

    def test_activity_in_the_same_millisecond_as_the_delete_is_not_after_it(self):
        result = planned(
            [
                snapshot("A", {X: copy("v1", DELETED_AT)}),
                snapshot("B", tombstones={X: DELETED_AT}),
            ]
        )

        self.assertEqual(result.actions[0], RetireRecord(X, target="A"))

    def test_an_orphaned_temp_file_is_retired_so_claude_cannot_promote_it(self):
        result = planned(
            [
                snapshot("A", tombstones={X: DELETED_AT}, orphan_tmps={X}),
                snapshot("B", tombstones={X: DELETED_AT}),
            ]
        )

        self.assertEqual(result.actions, [RetireTmp(X, target="A")])

    def test_a_tombstone_with_no_record_anywhere_is_created_where_missing(self):
        result = planned([snapshot("A"), snapshot("B", tombstones={X: DELETED_AT})])

        self.assertEqual(result.actions, [CreateTombstone(X, source="B", target="A")])

    def test_an_unreadable_copy_freezes_a_delete_as_well(self):
        result = planned(
            [snapshot("A", {X: unreadable()}), snapshot("B", tombstones={X: DELETED_AT})]
        )

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("unreadable", X, "A")])


class RecordWinsTests(unittest.TestCase):
    def test_a_session_used_after_the_delete_comes_back_and_the_tombstone_goes(self):
        result = planned(
            [
                snapshot("A", {X: copy("v2", DELETED_AT + 5)}),
                snapshot("B", tombstones={X: DELETED_AT}),
            ],
            state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}),
        )

        self.assertEqual(
            result.actions,
            [CreateRecord(X, source="A", target="B"), RetireTombstone(X, target="B")],
        )
        self.assertEqual(result.problems, [])

    def test_a_stale_copy_in_a_new_partition_does_not_undo_a_finished_delete(self):
        result = planned(
            [
                snapshot("A", tombstones={X: DELETED_AT}),
                snapshot("B", tombstones={X: DELETED_AT}),
                snapshot("C", {X: copy("v1", DELETED_AT - 500)}),
            ]
        )

        self.assertEqual(
            result.actions,
            [RetireRecord(X, target="C"), CreateTombstone(X, source="A", target="C")],
        )

    def test_a_tie_among_survivors_keeps_the_deleting_partitions_tombstone(self):
        result = planned(
            [
                snapshot("A", {X: copy("v2", DELETED_AT + 5)}),
                snapshot("B", {X: copy("v3", DELETED_AT + 5)}),
                snapshot("C", tombstones={X: DELETED_AT}),
            ],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(result.actions, [])
        self.assertEqual({problem.kind for problem in result.problems}, {"tied"})

    def test_differing_copies_still_converge_when_the_record_wins(self):
        result = planned(
            [
                snapshot("A", {X: copy("v2", DELETED_AT + 5)}, tombstones={X: DELETED_AT}),
                snapshot("B", {X: copy("v1", DELETED_AT - 5)}),
            ],
            state(agreed={X: "v1"}),
        )

        self.assertEqual(
            result.actions,
            [
                ReplaceRecord(X, source="A", target="B"),
                RetireTombstone(X, target="A"),
            ],
        )


class UnknownVersionTests(unittest.TestCase):
    """A folder with no remembered version cannot win just by looking changed."""

    def test_a_folder_that_joins_later_cannot_undo_a_rename(self):
        result = planned(
            [snapshot("A", {X: copy("renamed", 50)}), snapshot("B", {X: copy("t", 50)})],
            state(synced={"A": {X: "renamed"}}, folders=""),
        )

        self.assertEqual(result.actions, [])
        self.assertEqual({problem.kind for problem in result.problems}, {"tied"})

    def test_with_an_unknown_copy_later_activity_still_wins(self):
        result = planned(
            [snapshot("A", {X: copy("renamed", 50)}), snapshot("B", {X: copy("t", 90)})],
            state(synced={"A": {X: "renamed"}}, folders=""),
        )

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A")])

    def test_a_chat_used_after_a_delete_is_not_recreated_where_it_was_lost(self):
        result = planned(
            [
                snapshot("A", {X: copy("v2", DELETED_AT + 5)}),
                snapshot("B", tombstones={X: DELETED_AT}),
                snapshot("C"),
            ],
            state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}, "C": {X}}),
        )

        self.assertIn(CreateRecord(X, source="A", target="B"), result.actions)
        self.assertNotIn(CreateRecord(X, source="A", target="C"), result.actions)
        self.assertIn(Problem("lost", X, "C"), result.problems)


class SettleTests(unittest.TestCase):
    def test_identical_copies_everywhere_become_each_folders_version(self):
        after = settle(state(), [snapshot("A", {X: copy("v2")}), snapshot("B", {X: copy("v2")})])

        self.assertEqual(after.synced, {"A": {X: "v2"}, "B": {X: "v2"}})

    def test_differing_copies_leave_the_remembered_versions_where_they_were(self):
        after = settle(
            state(agreed={X: "v1"}, folders="AB"),
            [snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v2")})],
        )

        self.assertEqual(after.synced, {"A": {X: "v1"}, "B": {X: "v1"}})

    def test_a_folder_without_the_record_means_no_agreement_yet(self):
        after = settle(state(), [snapshot("A", {X: copy("v1")}), snapshot("B")])

        self.assertEqual(after.synced, {})

    def test_one_folder_alone_agrees_with_nothing(self):
        after = settle(state(), [snapshot("A", {X: copy("v1")})])

        self.assertEqual(after.synced, {})
        self.assertEqual(after.seen, {"A": {X}})

    def test_folders_outside_the_run_keep_what_they_had(self):
        before = state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}, "C": {X}}, folders="ABC")

        after = settle(before, [snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v1")})])

        self.assertEqual(after.synced["C"], {X: "v1"})
        self.assertEqual(after.seen["C"], {X})

    def test_no_folders_change_nothing(self):
        before = state(agreed={X: "v1"}, seen={"A": {X}}, folders="A")

        after = settle(before, [])

        self.assertEqual((after.synced, after.seen), (before.synced, before.seen))

    def test_an_unreadable_copy_means_no_agreement(self):
        after = settle(state(), [snapshot("A", {X: unreadable()}), snapshot("B", {X: unreadable()})])

        self.assertEqual(after.synced, {})

    def test_identical_copies_agree_even_when_their_time_cannot_be_trusted(self):
        ahead = Copy(state_hash="v2", last_activity_at=10**15, future_dated=True)

        after = settle(state(), [snapshot("A", {X: ahead}), snapshot("B", {X: ahead})])

        self.assertEqual(after.synced, {"A": {X: "v2"}, "B": {X: "v2"}})

    def test_the_given_state_is_not_modified(self):
        before = state(agreed={X: "v1"}, folders="AB")

        settle(before, [snapshot("A", {X: copy("v2")}), snapshot("B", {X: copy("v2")})])

        self.assertEqual(before.synced, {"A": {X: "v1"}, "B": {X: "v1"}})

    def test_a_finished_delete_is_forgotten_so_a_later_re_adoption_is_not_lost(self):
        after = settle(
            state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}, folders="AB"),
            [snapshot("A", tombstones={X: NOW}), snapshot("B", tombstones={X: NOW})],
        )

        self.assertEqual(after.synced, {})
        self.assertEqual(after.seen, {"A": set(), "B": set()})

    def test_a_delete_still_on_its_way_keeps_what_is_known(self):
        after = settle(
            state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}, folders="AB"),
            [snapshot("A", {X: copy("v1")}), snapshot("B", tombstones={X: NOW})],
        )

        self.assertEqual(after.synced, {"A": {X: "v1"}, "B": {X: "v1"}})
        self.assertEqual(after.seen, {"A": {X}, "B": {X}})

    def test_a_placement_waiting_for_the_rest_to_catch_up_is_remembered(self):
        after = settle(
            state(agreed={X: "v0"}, placed={"B": {X: "v1"}}),
            [
                snapshot("A", {X: copy("v1")}),
                snapshot("B", {X: copy("v1")}),
                snapshot("C", {X: copy("v0")}),
            ],
        )

        self.assertEqual(after.synced["B"], {X: "v1"})
        self.assertEqual(after.synced["C"], {X: "v0"})

    def test_observed_records_are_remembered_per_folder(self):
        after = settle(
            state(seen={"A": {X}}),
            [snapshot("A", {X: copy("v1"), Y: copy("v1")}), snapshot("B")],
        )

        self.assertEqual(after.seen, {"A": {X, Y}, "B": set()})

    def test_a_record_missing_from_one_folder_stays_remembered_there(self):
        after = settle(state(seen={"A": {X}, "B": {X}}), [snapshot("A", {X: copy("v1")}), snapshot("B")])

        self.assertEqual(after.seen["B"], {X})

    def test_a_record_gone_from_every_folder_is_forgotten(self):
        after = settle(state(seen={"A": {X}, "B": {X}}), [snapshot("A"), snapshot("B")])

        self.assertEqual(after.seen, {"A": set(), "B": set()})


if __name__ == "__main__":
    unittest.main()
