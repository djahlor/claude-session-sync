import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_session_sync.record_journal import (
    RecordJournal,
    RecordJournalError,
    RecordRecoveryError,
    digest,
)


class RecordJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "records"
        self.values = {"a": b"old-a", "b": b"old-b"}
        self.writes = []
        self.running = False

    def validate(self, target):
        if target not in ("a", "b"):
            raise RecordRecoveryError("target is not allowed")

    def guard(self):
        if self.running:
            raise RuntimeError("Claude running")

    def write(self, target, content):
        self.writes.append(target)
        self.values[target] = content

    def delete(self, target):
        self.writes.append(target)
        self.values.pop(target, None)

    def journal(self, write=None):
        return RecordJournal(
            self.root,
            read=self.values.get,
            write=write or self.write,
            delete=self.delete,
            validate=self.validate,
            before_mutation=self.guard,
            retention=1,
        )

    def pending(self, before=None, after=None, name="pending.json"):
        before = self.values.copy() if before is None else before
        after = {"a": b"new-a", "b": b"new-b"} if after is None else after
        self.root.mkdir(exist_ok=True)
        path = self.root / name

        def encode(value):
            return None if value is None else base64.b64encode(value).decode("ascii")

        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "state": "PREPARED",
                    "records": [
                        {
                            "target": target,
                            "before": encode(before[target]),
                            "after": encode(value),
                            "before_sha256": digest(before[target]),
                            "after_sha256": digest(value),
                        }
                        for target, value in after.items()
                    ],
                }
            )
        )
        return path

    def test_commit_preserves_exact_preimages_and_hashes(self):
        before = {"a": b' {"x": 1} \n', "b": None}
        self.values = {"a": before["a"]}
        self.journal().commit(before, {"a": b"new", "b": b"created"})
        document = json.loads(next(self.root.glob("*.json")).read_text())
        self.assertEqual("COMMITTED", document["state"])
        self.assertEqual(
            before["a"], base64.b64decode(document["records"][0]["before"])
        )
        self.assertEqual(digest(before["a"]), document["records"][0]["before_sha256"])
        self.assertEqual({"a": b"new", "b": b"created"}, self.values)

    def test_recovery_checks_all_records_before_any_rollback(self):
        path = self.pending()
        self.values = {"a": b"new-a", "b": b"independent"}
        with self.assertRaisesRegex(RecordRecoveryError, "independently"):
            self.journal().recover()
        self.assertEqual([], self.writes)
        self.assertEqual({"a": b"new-a", "b": b"independent"}, self.values)
        self.assertEqual("RECOVERY_REQUIRED", json.loads(path.read_text())["state"])

    def test_mixed_recovery_restores_only_written_records(self):
        path = self.pending()
        self.values["a"] = b"new-a"
        self.journal().recover()
        self.assertEqual(["a"], self.writes)
        self.assertEqual(b"old-a", self.values["a"])
        self.assertEqual("ROLLED_BACK", json.loads(path.read_text())["state"])

    def test_absent_preimage_is_deleted_on_recovery(self):
        self.pending(before={"a": None, "b": b"old-b"})
        self.values["a"] = b"new-a"
        self.journal().recover()
        self.assertNotIn("a", self.values)
        self.assertEqual(["a"], self.writes)

    def test_fully_applied_recovery_commits_without_writes(self):
        path = self.pending()
        self.values = {"a": b"new-a", "b": b"new-b"}
        self.journal().recover()
        self.assertEqual([], self.writes)
        self.assertEqual("COMMITTED", json.loads(path.read_text())["state"])

    def test_failed_write_restores_exact_values(self):
        original = self.values.copy()

        def write(target, content):
            if target == "b" and content == b"new-b":
                raise OSError("disk full")
            self.write(target, content)

        with self.assertRaisesRegex(RecordJournalError, "rolled back"):
            self.journal(write).commit(original, {"a": b"new-a", "b": b"new-b"})
        self.assertEqual(original, self.values)

    def test_changed_preimage_is_rejected_before_first_write(self):
        before = self.values.copy()
        self.values["b"] = b"independent"
        with self.assertRaisesRegex(RecordJournalError, "changed before"):
            self.journal().commit(before, {"a": b"new-a", "b": b"new-b"})
        self.assertEqual([], self.writes)

    def test_relaunch_stops_next_write_and_preserves_journal(self):
        def write(target, content):
            self.write(target, content)
            self.running = True

        with self.assertRaises(RecordRecoveryError):
            self.journal(write).commit(
                self.values.copy(), {"a": b"new-a", "b": b"new-b"}
            )
        self.assertEqual(["a"], self.writes)
        self.assertEqual(b"old-b", self.values["b"])
        self.assertEqual(
            "RECOVERY_REQUIRED",
            json.loads(next(self.root.glob("*.json")).read_text())["state"],
        )

    def test_failed_commit_does_not_rollback_before_discovering_later_conflict(self):
        def write(target, content):
            if target == "b":
                self.values[target] = b"independent"
                raise OSError("another writer changed this target")
            self.write(target, content)

        with self.assertRaises(RecordRecoveryError):
            self.journal(write).commit(
                self.values.copy(), {"a": b"new-a", "b": b"new-b"}
            )
        self.assertEqual(["a"], self.writes)
        self.assertEqual({"a": b"new-a", "b": b"independent"}, self.values)

    def test_unknown_target_is_rejected_before_read_or_write(self):
        self.pending(before={"outside": b"old"}, after={"outside": b"new"})
        with self.assertRaisesRegex(RecordRecoveryError, "not allowed"):
            self.journal().recover()
        self.assertEqual([], self.writes)

    def test_earlier_unchanged_dependency_change_stops_final_commit(self):
        original = self.values.copy()

        def write(target, content):
            self.write(target, content)
            self.values["a"] = b"independent source edit"

        with self.assertRaises(RecordRecoveryError):
            self.journal(write).commit(original, {"a": original["a"], "b": b"new-b"})
        self.assertEqual(["b"], self.writes)
        self.assertEqual(b"independent source edit", self.values["a"])
        self.assertEqual(b"new-b", self.values["b"])
        document = json.loads(next(self.root.glob("*.json")).read_text())
        self.assertEqual("RECOVERY_REQUIRED", document["state"])

    def test_malformed_state_is_a_recovery_error(self):
        self.root.mkdir()
        (self.root / "bad.json").write_text('{"version":2,"state":[]}')
        with self.assertRaises(RecordRecoveryError):
            self.journal().recover()
        self.assertEqual([], self.writes)

    def test_bad_hash_or_unknown_target_stops_recovery(self):
        path = self.pending()
        document = json.loads(path.read_text())
        document["records"][0]["before_sha256"] = "0" * 64
        path.write_text(json.dumps(document))
        with self.assertRaises(RecordRecoveryError):
            self.journal().recover()
        self.assertEqual([], self.writes)

    def test_pending_and_malformed_journals_survive_retention(self):
        pending = self.pending()
        malformed = self.root / "malformed.json"
        malformed.write_text("broken")
        for name in ("old.json", "new.json"):
            path = self.root / name
            path.write_text(json.dumps({"version": 2, "state": "COMMITTED"}))
        self.journal().prune()
        self.assertTrue(pending.exists())
        self.assertTrue(malformed.exists())
        self.assertEqual(3, len(list(self.root.glob("*.json"))))

    def test_journal_symlink_is_rejected(self):
        self.root.mkdir()
        external = self.root.parent / "external"
        external.write_text("{}")
        (self.root / "pending.json").symlink_to(external)
        with self.assertRaises(RecordRecoveryError):
            self.journal().recover()
        self.assertEqual("{}", external.read_text())

    def test_fifo_journal_fails_without_waiting_for_a_writer(self):
        self.root.mkdir()
        path = self.root / "pending.json"
        os.mkfifo(path)
        code = (
            "from pathlib import Path; from claude_session_sync.record_journal import RecordJournal, RecordRecoveryError; "
            "\ntry: RecordJournal._load(None, Path({!r}))\nexcept RecordRecoveryError: pass\nelse: raise AssertionError('accepted FIFO')"
        ).format(str(path))
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, timeout=3
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
