import json
import os
import stat
import tempfile
import unittest
import base64
import hashlib
from pathlib import Path
from unittest.mock import patch

from claude_session_sync.config import Config
from claude_session_sync.model import Profile
from claude_session_sync.routines import (
    RoutineError,
    RoutineSample,
    RoutineSnapshot,
    RoutineSynchronizer,
    transform_routine_manifests,
)
import claude_session_sync.routines as routines_module


def task(task_id, file_path="/tmp/task/SKILL.md", cron="0 9 * * *"):
    return {
        "id": task_id,
        "enabled": True,
        "filePath": file_path,
        "createdAt": 1,
        "cronExpression": cron,
    }


def manifest(*tasks, **metadata):
    return {"scheduledTasks": list(tasks), **metadata}


def sample(name, mtime, document):
    return RoutineSample(("Work", name, "org"), Path("/tmp") / name, mtime, document)


class RoutineTransformTests(unittest.TestCase):
    def test_first_sync_unions_tasks_and_uses_newest_edit(self):
        first = manifest(task("shared", cron="0 8 * * *"), task("first"))
        second = manifest(task("shared", cron="0 9 * * *"), task("second"))

        result = transform_routine_manifests(
            (sample("a", 10, first), sample("b", 20, second))
        )

        tasks = {item["id"]: item for item in result.manifest["scheduledTasks"]}
        self.assertEqual({"first", "second", "shared"}, set(tasks))
        self.assertEqual("0 9 * * *", tasks["shared"]["cronExpression"])

    def test_existing_target_deletion_propagates(self):
        baseline = manifest(task("daily"))
        snapshot = RoutineSnapshot(
            (("Work", "a", "org"), ("Work", "b", "org")), baseline
        )

        result = transform_routine_manifests(
            (
                sample("a", 20, manifest()),
                sample("b", 10, baseline),
            ),
            snapshot=snapshot,
        )

        self.assertEqual([], result.manifest["scheduledTasks"])

    def test_empty_new_target_does_not_delete_snapshot_tasks(self):
        baseline = manifest(task("daily"))
        snapshot = RoutineSnapshot((("Work", "a", "org"),), baseline)

        result = transform_routine_manifests(
            (
                sample("a", 10, baseline),
                sample("new", 30, manifest()),
            ),
            snapshot=snapshot,
        )

        self.assertEqual(
            ["daily"], [item["id"] for item in result.manifest["scheduledTasks"]]
        )

    def test_equal_latest_divergent_edits_stop_safely(self):
        with self.assertRaisesRegex(RoutineError, "equally recent conflicting edits"):
            transform_routine_manifests(
                (
                    sample("a", 20, manifest(task("daily", cron="0 8 * * *"))),
                    sample("b", 20, manifest(task("daily", cron="0 9 * * *"))),
                )
            )

    def test_unknown_metadata_is_preserved_by_newest_manifest(self):
        result = transform_routine_manifests(
            (
                sample("a", 10, manifest(task("daily"), futureState={"value": 1})),
                sample("b", 20, manifest(task("daily"), futureState={"value": 2})),
            )
        )

        self.assertEqual({"value": 2}, result.manifest["futureState"])

    def test_duplicate_task_ids_are_rejected(self):
        with self.assertRaisesRegex(RoutineError, "duplicate task ids"):
            transform_routine_manifests(
                (sample("a", 10, manifest(task("daily"), task("daily"))),)
            )


class RoutineSynchronizerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_root = self.root / "Claude"
        self.sessions = self.data_root / "claude-code-sessions"
        self.skill = self.root / ".claude" / "scheduled-tasks" / "daily" / "SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("prompt", encoding="utf-8")
        self.config = Config(
            profiles=(Profile("Work", self.data_root, ("/usr/bin/true",), True),),
            state_dir=self.root / "state",
            retention=5,
            acknowledge_cross_profile_copy=False,
            acknowledge_cross_account_copy=True,
            claude_executable=Path("/usr/bin/true"),
            target_policy="all-configured-profiles",
            sync_code_routines=True,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def target(self, account):
        path = self.sessions / account / "org"
        path.mkdir(parents=True)
        return path

    def write_manifest(self, target, document, mtime_ns=None):
        path = target / "scheduled-tasks.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        os.chmod(path, 0o600)
        if mtime_ns is not None:
            os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def daily(self):
        return task("daily", str(self.skill))

    def test_sync_writes_every_target_then_becomes_noop(self):
        first = self.target("a")
        second = self.target("b")
        self.write_manifest(first, manifest(self.daily()))

        receipt = RoutineSynchronizer(self.config).sync()

        self.assertEqual("synced", receipt.state)
        self.assertEqual(1, receipt.task_count)
        self.assertEqual(1, receipt.write_count)
        for target in (first, second):
            path = target / "scheduled-tasks.json"
            self.assertEqual(
                ["daily"],
                [item["id"] for item in json.loads(path.read_text())["scheduledTasks"]],
            )
            self.assertEqual(
                stat.S_IRUSR | stat.S_IWUSR, stat.S_IMODE(path.stat().st_mode)
            )
        self.assertEqual("noop", RoutineSynchronizer(self.config).sync().state)

    def test_deletion_propagates_after_snapshot(self):
        first = self.target("a")
        second = self.target("b")
        self.write_manifest(first, manifest(self.daily()), 10)
        RoutineSynchronizer(self.config).sync()
        self.write_manifest(first, manifest(), 30)

        receipt = RoutineSynchronizer(self.config).sync()

        self.assertEqual(0, receipt.task_count)
        for target in (first, second):
            self.assertEqual(
                [],
                json.loads((target / "scheduled-tasks.json").read_text())[
                    "scheduledTasks"
                ],
            )

    def test_missing_definition_stops_before_any_manifest_write(self):
        first = self.target("a")
        second = self.target("b")
        missing = task(
            "missing", str(self.skill.parent.parent / "missing" / "SKILL.md")
        )
        original = self.write_manifest(first, manifest(missing)).read_bytes()

        with self.assertRaisesRegex(RoutineError, "definition file is missing"):
            RoutineSynchronizer(self.config).sync()

        self.assertEqual(original, (first / "scheduled-tasks.json").read_bytes())
        self.assertFalse((second / "scheduled-tasks.json").exists())

    def test_interrupted_mixed_write_recovers_before_merging(self):
        first = self.target("a")
        second = self.target("b")
        original = json.dumps(manifest()).encode("utf-8")
        first_path = self.write_manifest(first, manifest())
        second_path = self.write_manifest(second, manifest())
        after = b'{"scheduledTasks":[],"futureState":true}\n'
        first_path.write_bytes(after)
        journal_root = self.config.state_dir / "routine-runs"
        journal_root.mkdir(parents=True)
        journal = {
            "version": 1,
            "state": "PREPARED",
            "records": [
                {
                    "path": str(path),
                    "before": base64.b64encode(original).decode("ascii"),
                    "after_sha256": hashlib.sha256(after).hexdigest(),
                }
                for path in (first_path, second_path)
            ],
        }
        journal_path = journal_root / "interrupted.json"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        os.chmod(journal_path, 0o600)

        receipt = RoutineSynchronizer(self.config).sync()

        self.assertEqual("noop", receipt.state)
        self.assertEqual(original, first_path.read_bytes())
        self.assertEqual(original, second_path.read_bytes())
        self.assertEqual("ROLLED_BACK", json.loads(journal_path.read_text())["state"])

    def test_failed_multi_manifest_write_restores_exact_preimages(self):
        first = self.target("a")
        second = self.target("b")
        second_skill = self.root / ".claude" / "scheduled-tasks" / "second" / "SKILL.md"
        second_skill.parent.mkdir(parents=True)
        second_skill.write_text("second prompt", encoding="utf-8")
        first_path = self.write_manifest(first, manifest(self.daily()))
        second_path = self.write_manifest(
            second,
            manifest(task("second", str(second_skill))),
        )
        originals = {
            first_path: first_path.read_bytes(),
            second_path: second_path.read_bytes(),
        }
        real_write = routines_module.atomic_write_bytes
        manifest_writes = 0

        def flaky_write(path, content):
            nonlocal manifest_writes
            if Path(path).name == "scheduled-tasks.json":
                manifest_writes += 1
                if manifest_writes == 2:
                    raise OSError("injected write failure")
            return real_write(path, content)

        with patch(
            "claude_session_sync.routines.atomic_write_bytes", side_effect=flaky_write
        ):
            with self.assertRaisesRegex(RoutineError, "rolled back safely"):
                RoutineSynchronizer(self.config).sync()

        self.assertEqual(originals[first_path], first_path.read_bytes())
        self.assertEqual(originals[second_path], second_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
