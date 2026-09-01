from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Optional

from claude_session_sync.transaction import RecoveryError, TransactionEngine


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def operation(
    source: Path,
    destination: Path,
    destination_before: Optional[bytes],
    session_id: str,
):
    source_bytes = source.read_bytes()
    return SimpleNamespace(
        kind="copy",
        session_id=session_id,
        source=source,
        destination=destination,
        source_digest=digest(source_bytes),
        destination_digest_or_none=(
            digest(destination_before) if destination_before is not None else None
        ),
        size=len(source_bytes),
    )


def plan(operations: Iterable[object]):
    return SimpleNamespace(
        operations=tuple(operations),
        conflicts=(),
        invalid_replicas=(),
        plan_id="rollback-plan",
    )


class RollbackTests(unittest.TestCase):
    def test_rollback_restores_existing_bytes_and_removes_new_destination(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            old_source = root / "old-source.json"
            new_source = root / "new-source.json"
            old_destination = target / "local_old.json"
            new_destination = target / "local_new.json"
            old_source.write_bytes(b"replacement is longer")
            new_source.write_bytes(b"brand new")
            old_destination.write_bytes(b"old")
            run_plan = plan(
                (
                    operation(
                        old_source,
                        old_destination,
                        old_destination.read_bytes(),
                        "old",
                    ),
                    operation(new_source, new_destination, None, "new"),
                )
            )
            engine = TransactionEngine(root / "state", process_probe=lambda: False)
            run = engine.apply(run_plan)

            recovery = engine.rollback(run.run_id)

            self.assertEqual(old_destination.read_bytes(), b"old")
            self.assertFalse(new_destination.exists())
            self.assertEqual(recovery.status, "rolled_back")
            self.assertEqual(recovery.operation_count, 2)
            self.assertEqual(recovery.bytes_restored, 3)
            manifest = json.loads(
                (root / "state" / "runs" / run.run_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["phase"], "ROLLED_BACK")
            self.assertEqual(manifest["receipt"]["status"], "rolled_back")

            repeated = engine.rollback(run.run_id)
            self.assertEqual(repeated.status, "already_rolled_back")
            self.assertEqual(repeated.operation_count, 0)

    def test_failure_after_commit_rolls_every_applied_operation_back(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            source = root / "source.json"
            destination = target / "local_session.json"
            source.write_bytes(b"replacement")
            destination.write_bytes(b"preimage")
            run_plan = plan(
                (
                    operation(
                        source,
                        destination,
                        destination.read_bytes(),
                        "session",
                    ),
                )
            )

            def fail_during_verification(phase: str, unused_run_id: str) -> None:
                if phase == "VERIFYING":
                    raise OSError("injected disk verification failure")

            engine = TransactionEngine(
                root / "state",
                process_probe=lambda: False,
                fault_injector=fail_during_verification,
            )

            with self.assertRaisesRegex(OSError, "injected disk verification failure"):
                engine.apply(run_plan)

            self.assertEqual(destination.read_bytes(), b"preimage")

    def test_crashed_run_can_be_recovered_through_public_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            source = root / "source.json"
            destination = target / "local_session.json"
            source.write_bytes(b"replacement")
            destination.write_bytes(b"preimage")
            run_plan = plan(
                (
                    operation(
                        source,
                        destination,
                        destination.read_bytes(),
                        "session",
                    ),
                )
            )
            captured_run_ids = []

            def crash_during_verification(phase: str, run_id: str) -> None:
                if phase == "VERIFYING":
                    captured_run_ids.append(run_id)
                    raise KeyboardInterrupt("simulated process crash")

            crashing_engine = TransactionEngine(
                root / "state",
                process_probe=lambda: False,
                fault_injector=crash_during_verification,
            )

            with self.assertRaises(KeyboardInterrupt):
                crashing_engine.apply(run_plan)

            self.assertEqual(destination.read_bytes(), b"replacement")
            self.assertEqual(len(captured_run_ids), 1)
            recovery = TransactionEngine(
                root / "state", process_probe=lambda: False
            ).rollback(captured_run_ids[0])
            self.assertEqual(recovery.status, "rolled_back")
            self.assertEqual(destination.read_bytes(), b"preimage")

    def test_missing_preimage_blocks_all_rollback_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            old_source = root / "old-source.json"
            new_source = root / "new-source.json"
            old_destination = target / "local_old.json"
            new_destination = target / "local_new.json"
            old_source.write_bytes(b"replacement")
            new_source.write_bytes(b"new")
            old_destination.write_bytes(b"preimage")
            run_plan = plan(
                (
                    operation(
                        old_source,
                        old_destination,
                        old_destination.read_bytes(),
                        "old",
                    ),
                    operation(new_source, new_destination, None, "new"),
                )
            )
            engine = TransactionEngine(root / "state", process_probe=lambda: False)
            run = engine.apply(run_plan)
            preimages = list(
                (root / "state" / "runs" / run.run_id / "preimages").iterdir()
            )
            self.assertEqual(len(preimages), 1)
            preimages[0].unlink()

            with self.assertRaises(RecoveryError):
                engine.rollback(run.run_id)

            self.assertEqual(old_destination.read_bytes(), b"replacement")
            self.assertEqual(new_destination.read_bytes(), b"new")

    def test_changed_new_destination_is_never_deleted_by_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            source = root / "source.json"
            destination = target / "local_session.json"
            source.write_bytes(b"applied")
            run_plan = plan((operation(source, destination, None, "session"),))
            engine = TransactionEngine(root / "state", process_probe=lambda: False)
            run = engine.apply(run_plan)
            destination.write_bytes(b"changed after apply")

            with self.assertRaises(RecoveryError):
                engine.rollback(run.run_id)

            self.assertEqual(destination.read_bytes(), b"changed after apply")

    def test_precommit_crash_never_deletes_independently_created_new_path(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            source = root / "source.json"
            destination = target / "local_session.json"
            source.write_bytes(b"planned bytes")
            run_plan = plan((operation(source, destination, None, "session"),))
            captured_run_ids = []

            def crash_after_path_appears(phase: str, run_id: str) -> None:
                if phase == "STAGING":
                    destination.write_bytes(b"planned bytes")
                    captured_run_ids.append(run_id)
                    raise KeyboardInterrupt("crash before transaction staging")

            with self.assertRaises(KeyboardInterrupt):
                TransactionEngine(
                    root / "state",
                    process_probe=lambda: False,
                    fault_injector=crash_after_path_appears,
                ).apply(run_plan)

            with self.assertRaises(RecoveryError):
                TransactionEngine(root / "state", process_probe=lambda: False).rollback(
                    captured_run_ids[0]
                )
            self.assertEqual(destination.read_bytes(), b"planned bytes")

    def test_authenticated_manifest_blocks_arbitrary_destination_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            source = root / "source.json"
            destination = target / "local_session.json"
            victim = root / "victim.json"
            source.write_bytes(b"applied")
            victim.write_bytes(b"applied")
            engine = TransactionEngine(root / "state", process_probe=lambda: False)
            run = engine.apply(plan((operation(source, destination, None, "session"),)))
            manifest_path = root / "state" / "runs" / run.run_id / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["records"][0]["destination"] = str(victim)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaises(RecoveryError):
                engine.rollback(run.run_id)

            self.assertEqual(victim.read_bytes(), b"applied")

    def test_first_durable_journal_phase_survives_crash_for_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            source = root / "source.json"
            destination = target / "local_session.json"
            source.write_bytes(b"replacement")
            destination.write_bytes(b"preimage")
            run_plan = plan(
                (
                    operation(
                        source,
                        destination,
                        destination.read_bytes(),
                        "session",
                    ),
                )
            )
            captured_run_ids = []

            def crash_after_journal(phase: str, run_id: str) -> None:
                if phase == "JOURNALING":
                    captured_run_ids.append(run_id)
                    raise KeyboardInterrupt("simulated power loss")

            with self.assertRaises(KeyboardInterrupt):
                TransactionEngine(
                    root / "state",
                    process_probe=lambda: False,
                    fault_injector=crash_after_journal,
                ).apply(run_plan)

            manifest_path = (
                root / "state" / "runs" / captured_run_ids[0] / "manifest.json"
            )
            self.assertTrue(manifest_path.is_file())
            recovery = TransactionEngine(
                root / "state", process_probe=lambda: False
            ).rollback(captured_run_ids[0])
            self.assertEqual(recovery.status, "rolled_back")
            self.assertEqual(destination.read_bytes(), b"preimage")


if __name__ == "__main__":
    unittest.main()
