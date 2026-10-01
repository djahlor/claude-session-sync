import json
import os
import stat
import tempfile
import unittest
import base64
import hashlib
import subprocess
import sys
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

    def test_unknown_metadata_stays_on_its_destination(self):
        result = transform_routine_manifests(
            (
                sample("a", 10, manifest(task("daily"), futureState={"value": 1})),
                sample("b", 20, manifest(task("daily"), futureState={"value": 2})),
            )
        )

        self.assertNotIn("futureState", result.manifest)
        self.assertEqual(
            {"value": 1}, result.documents[("Work", "a", "org")]["futureState"]
        )
        self.assertEqual(
            {"value": 2}, result.documents[("Work", "b", "org")]["futureState"]
        )

    def test_permissions_and_execution_state_never_travel(self):
        first = task("daily", cron="0 8 * * *")
        first.update(
            permissionMode="bypassPermissions",
            lastRunAt="yesterday",
            approvedPermissions=[{"toolName": "Bash"}],
            futureField=True,
        )
        second = task("daily")
        second.update(permissionMode="default", lastRunAt="today")
        result = transform_routine_manifests(
            (
                sample("a", 10, manifest(first, runRetries={"daily": {"attempts": 3}})),
                sample("b", 20, manifest(second)),
                sample("new", 0, None),
            )
        )

        first_after = result.documents[("Work", "a", "org")]["scheduledTasks"][0]
        second_after = result.documents[("Work", "b", "org")]["scheduledTasks"][0]
        new_after = result.documents[("Work", "new", "org")]["scheduledTasks"][0]
        self.assertEqual("0 9 * * *", first_after["cronExpression"])
        self.assertEqual("bypassPermissions", first_after["permissionMode"])
        self.assertEqual("yesterday", first_after["lastRunAt"])
        self.assertTrue(first_after["futureField"])
        self.assertEqual("default", second_after["permissionMode"])
        self.assertNotIn("approvedPermissions", second_after)
        self.assertNotIn("permissionMode", new_after)
        self.assertNotIn("lastRunAt", new_after)
        self.assertNotIn("runRetries", result.documents[("Work", "new", "org")])

    def test_local_runtime_changes_do_not_override_a_definition_edit(self):
        baseline = manifest(task("daily"))
        snapshot = RoutineSnapshot(
            (("Work", "a", "org"), ("Work", "b", "org")), baseline
        )
        latest_runtime = dict(task("daily"), lastRunAt="today")

        result = transform_routine_manifests(
            (
                sample("a", 10, manifest(task("daily", cron="0 10 * * *"))),
                sample("b", 20, manifest(latest_runtime)),
            ),
            snapshot=snapshot,
        )

        self.assertEqual(
            "0 10 * * *", result.manifest["scheduledTasks"][0]["cronExpression"]
        )

    def test_legacy_snapshot_metadata_is_not_reintroduced(self):
        old_task = dict(
            task("daily"), permissionMode="bypassPermissions", futureFlag=True
        )
        snapshot = RoutineSnapshot(
            (("Work", "a", "org"),),
            manifest(old_task, runRetries={"daily": {"attempts": 2}}),
        )
        result = transform_routine_manifests(
            (sample("a", 10, manifest(task("daily"))), sample("new", 0, None)),
            snapshot=snapshot,
        )

        self.assertEqual(manifest(task("daily")), result.manifest)
        self.assertEqual(
            manifest(task("daily")), result.documents[("Work", "new", "org")]
        )

    def test_duplicate_task_ids_are_rejected(self):
        with self.assertRaisesRegex(RoutineError, "duplicate task ids"):
            transform_routine_manifests(
                (sample("a", 10, manifest(task("daily"), task("daily"))),)
            )


class RoutineSynchronizerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
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

    def synchronizer(self):
        return RoutineSynchronizer(
            self.config,
            task_root=self.skill.parent.parent,
            process_probe=lambda *args, **kwargs: (),
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

        receipt = self.synchronizer().sync()

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
        self.assertEqual("noop", self.synchronizer().sync().state)

    def test_deletion_propagates_after_snapshot(self):
        first = self.target("a")
        second = self.target("b")
        self.write_manifest(first, manifest(self.daily()), 10)
        self.synchronizer().sync()
        self.write_manifest(first, manifest(), 30)

        receipt = self.synchronizer().sync()

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
            self.synchronizer().sync()

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
            "version": 2,
            "state": "PREPARED",
            "records": [
                {
                    "target": str(path),
                    "before": base64.b64encode(original).decode("ascii"),
                    "after": base64.b64encode(after).decode("ascii"),
                    "before_sha256": hashlib.sha256(original).hexdigest(),
                    "after_sha256": hashlib.sha256(after).hexdigest(),
                }
                for path in (first_path, second_path)
            ],
        }
        journal_path = journal_root / "interrupted.json"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        os.chmod(journal_path, 0o600)

        receipt = self.synchronizer().sync()

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
                self.synchronizer().sync()

        self.assertEqual(originals[first_path], first_path.read_bytes())
        self.assertEqual(originals[second_path], second_path.read_bytes())

    def test_probe_validates_data_without_creating_state_or_manifests(self):
        first = self.target("a")
        second = self.target("b")
        source = self.write_manifest(first, manifest(self.daily()))
        before = source.read_bytes()

        result = self.synchronizer().probe()

        self.assertEqual(
            {
                "state": "compatible",
                "target_count": 2,
                "manifest_count": 1,
                "task_count": 1,
            },
            result,
        )
        self.assertEqual(before, source.read_bytes())
        self.assertFalse(self.config.state_dir.exists())
        self.assertFalse((second / "scheduled-tasks.json").exists())

    def test_malformed_chat_does_not_block_routines(self):
        first = self.target("a")
        self.target("b")
        self.write_manifest(first, manifest(self.daily()))
        (first / "local_bad.json").write_bytes(b"broken chat")

        self.assertEqual(1, self.synchronizer().sync().write_count)

    def test_nonfinite_or_duplicate_manifest_json_is_rejected(self):
        target = self.target("a")
        path = target / "scheduled-tasks.json"
        for content in (
            b'{"scheduledTasks":[],"scheduledTasks":[]}',
            b'{"scheduledTasks":[],"future":NaN}',
            b'{"scheduledTasks":[],"future":1e999}',
        ):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaisesRegex(RoutineError, "malformed JSON"):
                    self.synchronizer().probe()
                self.assertEqual(content, path.read_bytes())

    def test_definition_outside_task_root_is_rejected(self):
        first = self.target("a")
        other_skill = self.root / "other" / "daily" / "SKILL.md"
        other_skill.parent.mkdir(parents=True)
        other_skill.write_text("unrelated instruction", encoding="utf-8")
        self.write_manifest(first, manifest(task("daily", str(other_skill))))

        with self.assertRaisesRegex(
            RoutineError, "definition file is missing or unsafe"
        ):
            self.synchronizer().probe()

    def test_symlink_definition_ancestor_is_rejected(self):
        first = self.target("a")
        actual = self.root / "actual"
        self.skill.parent.rename(actual)
        self.skill.parent.symlink_to(actual, target_is_directory=True)
        self.write_manifest(first, manifest(self.daily()))

        with self.assertRaisesRegex(
            RoutineError, "definition file is missing or unsafe"
        ):
            self.synchronizer().probe()

    def test_changed_preimage_after_sampling_is_not_overwritten(self):
        first = self.target("a")
        second = self.target("b")
        path = self.write_manifest(first, manifest(self.daily()))
        changed = b'{"scheduledTasks":[],"editedAfterSampling":true}\n'
        synchronizer = self.synchronizer()
        validate = synchronizer._validate_task_files

        def edit_after_sampling(document):
            validate(document)
            path.write_bytes(changed)

        with patch.object(
            synchronizer, "_validate_task_files", side_effect=edit_after_sampling
        ):
            with self.assertRaisesRegex(RoutineError, "changed during synchronization"):
                synchronizer.sync()

        self.assertEqual(changed, path.read_bytes())
        self.assertFalse((second / "scheduled-tasks.json").exists())
        self.assertFalse(
            (self.config.state_dir / "code-routines-snapshot.json").exists()
        )

    def test_running_claude_stops_direct_sync_before_any_state_write(self):
        synchronizer = RoutineSynchronizer(
            self.config,
            task_root=self.skill.parent.parent,
            process_probe=lambda *args, **kwargs: (object(),),
        )

        with self.assertRaisesRegex(RoutineError, "Claude must be fully quit"):
            synchronizer.sync()

        self.assertFalse(self.config.state_dir.exists())

    def test_real_sync_preserves_local_permissions_and_unknown_metadata(self):
        first = self.target("a")
        second = self.target("b")
        first_document = manifest(
            dict(
                self.daily(), permissionMode="bypassPermissions", lastRunAt="yesterday"
            ),
            futureState={"local": "a"},
        )
        first_path = self.write_manifest(first, first_document, 10)
        second_path = self.write_manifest(
            second, manifest(futureState={"local": "b"}), 20
        )

        self.synchronizer().sync()

        first_after = json.loads(first_path.read_text())
        second_after = json.loads(second_path.read_text())
        self.assertEqual(first_document, first_after)
        self.assertEqual({"local": "b"}, second_after["futureState"])
        self.assertNotIn("permissionMode", second_after["scheduledTasks"][0])
        self.assertNotIn("lastRunAt", second_after["scheduledTasks"][0])
        self.assertEqual("noop", self.synchronizer().sync().state)

    def test_post_commit_edit_is_detected_against_atomic_snapshot(self):
        first = self.target("a")
        self.target("b")
        first_path = self.write_manifest(first, manifest(self.daily()))
        synchronizer = self.synchronizer()
        commit = synchronizer._commit
        changed = b'{"scheduledTasks":[],"changedAfterCommit":true}'

        def edit_after_commit(current, replacements, targets):
            commit(current, replacements, targets)
            first_path.write_bytes(changed)

        with patch.object(synchronizer, "_commit", side_effect=edit_after_commit):
            with self.assertRaisesRegex(RoutineError, "changed after synchronization"):
                synchronizer.sync()

        self.assertEqual(changed, first_path.read_bytes())
        self.assertTrue(synchronizer._snapshot_path().exists())
        self.assertEqual(0, synchronizer.sync().task_count)

    def test_crash_after_commit_cannot_resurrect_a_later_deletion(self):
        first = self.target("a")
        second = self.target("b")
        self.write_manifest(first, manifest(self.daily()), 10)
        synchronizer = self.synchronizer()
        commit = synchronizer._commit

        def crash_after_commit(current, replacements, targets):
            commit(current, replacements, targets)
            raise SystemExit("simulated crash after transaction")

        with patch.object(synchronizer, "_commit", side_effect=crash_after_commit):
            with self.assertRaises(SystemExit):
                synchronizer.sync()
        self.write_manifest(second, manifest(), 30)

        result = synchronizer.sync()

        self.assertEqual(0, result.task_count)
        self.assertEqual(
            [],
            json.loads((first / "scheduled-tasks.json").read_text())["scheduledTasks"],
        )

    def test_snapshot_write_failure_restores_manifests_and_baseline_together(self):
        first = self.target("a")
        second = self.target("b")
        synchronizer = self.synchronizer()
        self.write_manifest(first, manifest())
        synchronizer.sync()
        first_path = self.write_manifest(first, manifest(self.daily()), 10)
        second_path = second / "scheduled-tasks.json"
        snapshot_path = synchronizer._snapshot_path()
        originals = {
            path: path.read_bytes() for path in (first_path, second_path, snapshot_path)
        }
        write = routines_module.atomic_write_bytes
        failed = False

        def fail_snapshot_once(path, value):
            nonlocal failed
            if path == snapshot_path and not failed:
                failed = True
                raise OSError("injected snapshot write failure")
            return write(path, value)

        with patch.object(
            routines_module, "atomic_write_bytes", side_effect=fail_snapshot_once
        ):
            with self.assertRaisesRegex(RoutineError, "rolled back safely"):
                synchronizer.sync()

        self.assertTrue(failed)
        for path, before in originals.items():
            self.assertEqual(before, path.read_bytes())

    def test_fifo_manifest_is_rejected_without_blocking(self):
        target = self.target("a")
        path = target / "scheduled-tasks.json"
        os.mkfifo(path)
        code = (
            "import sys; from pathlib import Path; "
            "from claude_session_sync.routines import _read_regular, RoutineError\n"
            "try: _read_regular(Path(sys.argv[1]))\n"
            "except RoutineError: sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        environment = dict(
            os.environ, PYTHONPATH=str(Path(routines_module.__file__).parent.parent)
        )
        result = subprocess.run(
            [sys.executable, "-c", code, str(path)],
            env=environment,
            timeout=2,
            capture_output=True,
        )

        self.assertEqual(0, result.returncode)

    def test_unchanged_source_edit_during_snapshot_commit_requires_recovery(self):
        first = self.target("a")
        self.target("b")
        first_path = self.write_manifest(first, manifest(self.daily()), 10)
        source_before = first_path.read_bytes()
        synchronizer = self.synchronizer()
        snapshot_path = synchronizer._snapshot_path()
        changed = b'{"scheduledTasks":[],"independentEdit":true}'
        write = routines_module.atomic_write_bytes

        def edit_source_during_snapshot_write(path, value):
            write(path, value)
            if path == snapshot_path:
                first_path.write_bytes(changed)

        with patch.object(
            routines_module,
            "atomic_write_bytes",
            side_effect=edit_source_during_snapshot_write,
        ):
            with self.assertRaisesRegex(RoutineError, "recovery requires attention"):
                synchronizer.sync()

        self.assertEqual(changed, first_path.read_bytes())
        journals = list(synchronizer._journal_root().glob("*.json"))
        self.assertEqual(1, len(journals))
        journal = json.loads(journals[0].read_text())
        self.assertEqual("RECOVERY_REQUIRED", journal["state"])
        dependency = next(
            record
            for record in journal["records"]
            if record["target"] == str(first_path)
        )
        encoded_source = base64.b64encode(source_before).decode("ascii")
        self.assertEqual(encoded_source, dependency["before"])
        self.assertEqual(encoded_source, dependency["after"])


if __name__ == "__main__":
    unittest.main()
