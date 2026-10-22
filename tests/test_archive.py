"""Archive and unarchive reach every account folder, the newest action winning."""

import json
import os
import time
import unittest
from dataclasses import replace
from pathlib import Path

from test_chat_sync import A_ACCOUNT, A_ORG, B_ACCOUNT, B_ORG, ChatSyncFixture, X, Y

from claude_session_sync.archive import (
    LEDGER_FILENAME,
    ArchiveCopy,
    archive_fields,
    decide,
    encode_record,
    head_flag,
)
from claude_session_sync.chat_state import load_state, state_path
from claude_session_sync.chat_sync import plan_chat_sync
from claude_session_sync.fingerprint import fingerprint


def copy(folder, archived, saved_ms, known=None, **fields):
    record = {"sessionId": "local_" + X, "isArchived": archived, **fields}
    return ArchiveCopy(folder, Path(folder), b"", record, saved_ms, known)


class DecideTests(unittest.TestCase):
    def test_copies_that_agree_are_left_alone(self):
        self.assertIsNone(decide([copy("a", True, 5), copy("b", True, 9)]))

    def test_first_sight_the_archived_copy_wins(self):
        winner = decide(
            [copy("a", True, 500), copy("b", False, 900, lastActivityAt=100)]
        )

        self.assertEqual(winner.folder, "a")

    def test_first_sight_a_chat_used_after_its_archive_stays_open(self):
        winner = decide(
            [copy("a", True, 500), copy("b", False, 400, lastActivityAt=600)]
        )

        self.assertEqual(winner.folder, "b")

    def test_an_unarchive_since_the_last_run_beats_copies_still_archived(self):
        winner = decide(
            [
                copy("a", True, 900, known=True),
                copy("b", False, 500, known=True, lastActivityAt=100),
            ]
        )

        self.assertEqual(winner.folder, "b")

    def test_an_archive_since_the_last_run_beats_copies_still_open(self):
        winner = decide(
            [
                copy("a", True, 500, known=False),
                copy("b", False, 900, known=False, lastActivityAt=100),
            ]
        )

        self.assertEqual(winner.folder, "a")

    def test_using_the_chat_after_a_fresh_archive_keeps_it_open(self):
        winner = decide(
            [
                copy("a", True, 500, known=False),
                copy("b", False, 400, known=False, lastActivityAt=700),
            ]
        )

        self.assertEqual(winner.folder, "b")

    def test_a_resume_counts_as_use(self):
        winner = decide([copy("a", True, 500), copy("b", False, 400, resumedAt=800)])

        self.assertEqual(winner.folder, "b")

    def test_an_archive_and_an_unarchive_at_the_same_moment_are_left_alone(self):
        self.assertIsNone(
            decide(
                [copy("a", True, 500, known=False), copy("b", False, 500, known=True)]
            )
        )


class RecordTests(unittest.TestCase):
    def test_the_flag_is_read_only_at_the_top_level(self):
        nested = b'{"a":{"isArchived":true},"isArchived":false,"b":1}'

        self.assertIs(head_flag(nested), False)
        self.assertIs(
            head_flag(b'{"x":"a { [ \\" isArchived","isArchived": true}'), True
        )
        self.assertIsNone(head_flag(b'{"title":"\\"isArchived\\":true"}'))

    def test_an_archive_brings_the_kept_worktree_marks(self):
        record = {"sessionId": "s", "title": "mine", "isArchived": False}
        winner = {
            "isArchived": True,
            "keptDirtyWorktree": True,
            "keptDirtyAt": 7,
            "title": "x",
        }

        self.assertEqual(
            archive_fields(record, winner),
            {
                "sessionId": "s",
                "title": "mine",
                "isArchived": True,
                "keptDirtyWorktree": True,
                "keptDirtyAt": 7,
            },
        )

    def test_an_unarchive_brings_the_exemption_and_removes_nothing(self):
        record = {
            "isArchived": True,
            "keptDirtyWorktree": True,
            "autoArchiveExempt": False,
        }
        winner = {"isArchived": False, "autoArchiveExempt": True}

        self.assertEqual(
            archive_fields(record, winner),
            {"isArchived": False, "keptDirtyWorktree": True, "autoArchiveExempt": True},
        )

    def test_encoding_keeps_every_value(self):
        record = {"sessionId": "local_" + X, "title": "✏️ \ud83d half", "n": 1.5}

        self.assertEqual(json.loads(encode_record(record)), record)
        self.assertEqual(json.loads(encode_record({"t": "✏️"})), {"t": "✏️"})
        self.assertIn("✏️".encode(), encode_record({"t": "✏️"}))


class ArchiveSyncTests(ChatSyncFixture):
    def test_an_archive_in_one_account_reaches_the_other_despite_a_tie(self):
        # The archived copy is behind on the title, so the chat sync ties.
        self.write(self.a, X, title="old", lastActivityAt=100, isArchived=True)
        self.write(self.b, X, title="renamed", lastActivityAt=100, isArchived=False)
        self.agree(X, title="zero", lastActivityAt=100, isArchived=False)

        run = self.sync()

        self.assertEqual(run.archive.archived, 1)
        self.assertIs(self.read(self.b, X)["isArchived"], True)
        self.assertEqual(self.read(self.b, X)["title"], "renamed")
        self.assertEqual(self.read(self.a, X)["title"], "old")

    def test_an_unarchive_later_reaches_the_account_that_archived(self):
        self.write(self.a, X, isArchived=True)
        self.write(self.b, X, isArchived=True)
        self.sync()
        self.write(self.b, X, isArchived=False, autoArchiveExempt=True)

        run = self.sync()

        self.assertEqual(run.archive.opened, 1)
        self.assertIs(self.read(self.a, X)["isArchived"], False)
        self.assertIs(self.read(self.a, X)["autoArchiveExempt"], True)

    def test_an_archive_after_an_unarchive_wins_again(self):
        self.write(self.a, X, isArchived=False)
        self.write(self.b, X, isArchived=False)
        self.sync()
        self.write(self.a, X, isArchived=True)

        self.sync()

        self.assertIs(self.read(self.b, X)["isArchived"], True)

    def test_the_copy_that_really_changed_still_wins_the_chat(self):
        self.write(self.a, X, title="t", lastActivityAt=100, isArchived=False)
        self.write(self.b, X, title="t", lastActivityAt=100, isArchived=False)
        self.sync()
        # B is archived and renamed at the same activity time, A is untouched.
        self.write(self.b, X, title="renamed", lastActivityAt=100, isArchived=True)

        run = self.sync()

        self.assertEqual(run.problems.get("tied"), None)
        self.assertEqual(self.read(self.a, X)["title"], "renamed")
        self.assertIs(self.read(self.a, X)["isArchived"], True)

    def test_a_chat_in_use_where_it_was_never_archived_is_opened_everywhere(self):
        self.write(self.a, X, lastActivityAt=100, isArchived=True)
        # Saved a minute ago, so this use came after the archive.
        later = int((time.time() - 30) * 1000)
        self.write(self.b, X, lastActivityAt=later, isArchived=False)

        run = self.sync()

        self.assertEqual(run.archive.opened, 1)
        self.assertIs(self.read(self.a, X)["isArchived"], False)

    def test_a_deleted_chat_is_left_to_the_delete(self):
        self.write(self.a, X, isArchived=True)
        self.write(self.b, X, isArchived=False)
        (self.b / f"deleted_{X}").write_text(str(1), encoding="ascii")

        run = self.sync()

        self.assertEqual(run.archive.copies, 0)

    def test_the_plan_previews_without_writing(self):
        self.write(self.a, X, title="one", isArchived=True)
        self.write(self.b, X, title="two", isArchived=False)
        self.write(self.a, Y)

        run = plan_chat_sync(self.config, self.planner())

        self.assertEqual(run.archive.archived, 1)
        self.assertIs(self.read(self.b, X)["isArchived"], False)
        self.assertFalse((self.config.state_dir / LEDGER_FILENAME).exists())

    def test_the_archive_write_is_journaled_and_can_be_rolled_back(self):
        self.write(self.a, X, title="one", isArchived=True)
        self.write(self.b, X, title="two", isArchived=False)
        before = (self.b / f"local_{X}.json").read_bytes()

        run = self.sync()

        runs = sorted((self.config.state_dir / "runs").iterdir())
        self.assertEqual(len(runs), 1)
        self.assertEqual(run.archive.archived, 1)
        from claude_session_sync.transaction import TransactionEngine

        TransactionEngine(self.config.state_dir, process_probe=lambda: False).rollback(
            runs[0].name
        )
        self.assertEqual((self.b / f"local_{X}.json").read_bytes(), before)

    def test_nothing_is_written_while_claude_runs(self):
        from claude_session_sync.transaction import AppRunningError

        self.write(self.a, X, title="one", isArchived=True)
        self.write(self.b, X, title="two", isArchived=False)
        self.running = True

        with self.assertRaises(AppRunningError):
            self.sync()

        self.assertIs(self.read(self.b, X)["isArchived"], False)

    def test_it_can_be_turned_off(self):
        self.config = replace(self.config, sync_archive_state=False)
        self.write(self.a, X, title="one", isArchived=True)
        self.write(self.b, X, title="two", isArchived=False)

        run = self.sync()

        self.assertIsNone(run.archive)
        self.assertIs(self.read(self.b, X)["isArchived"], False)

    def test_a_patched_copy_is_still_accepted_by_the_chat_sync(self):
        self.write(self.a, X, title="one", isArchived=True)
        self.write(self.b, X, title="two", isArchived=False)

        self.sync()

        raw = (self.b / f"local_{X}.json").read_bytes()
        self.assertIsNotNone(fingerprint(X, raw).state_hash)
        state = load_state(state_path(self.config.state_dir))
        self.assertIn(f"Work/{A_ACCOUNT}/{A_ORG}", state.sync.seen)
        self.assertIn(f"Work/{B_ACCOUNT}/{B_ORG}", state.sync.seen)
        self.assertTrue(os.path.exists(self.config.state_dir / LEDGER_FILENAME))


if __name__ == "__main__":
    unittest.main()
