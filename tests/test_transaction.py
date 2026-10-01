from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

from claude_session_sync.journal import abandoned_preparations, pending_recovery_runs

from claude_session_sync.transaction import (
    AppRunningError,
    PlanBlockedError,
    RevalidationError,
    RecoveryError,
    RecoveryPendingError,
    TransactionEngine,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_plan(source: Path, destination: Path, destination_before: Optional[bytes]):
    source_bytes = source.read_bytes()
    operation = SimpleNamespace(
        kind="copy",
        session_id="session-1",
        source=source,
        destination=destination,
        source_digest=digest(source_bytes),
        destination_digest_or_none=(
            digest(destination_before) if destination_before is not None else None
        ),
        size=len(source_bytes),
    )
    return SimpleNamespace(
        operations=(operation,),
        invalid_replicas=(),
        plan_id="plan-1",
    )


class TransactionEngineTests(unittest.TestCase):
    def test_fifo_backup_input_is_rejected_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            result = subprocess.run(
                [sys.executable, "-c", (
                    "import sys\n"
                    "from claude_session_sync.filesystem import digest_file, UnsafePathError\n"
                    "try: digest_file(sys.argv[1])\n"
                    "except UnsafePathError: pass\n"
                    "else: raise AssertionError('FIFO was accepted')\n"
                ), str(fifo)],
                capture_output=True, text=True, timeout=3,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_manifest_write_discards_only_unpublished_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "destination"
            source.write_bytes(b"new")
            destination.write_bytes(b"old")
            plan = make_plan(source, destination, b"old")
            engine = TransactionEngine(root / "state", process_probe=lambda: False)
            with patch("claude_session_sync.journal.RunJournal._persist", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    engine.apply(plan)
            self.assertEqual(pending_recovery_runs(root / "state"), [])
            self.assertEqual(abandoned_preparations(root / "state"), [])
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(engine.apply(plan).status, "committed")

    def test_crash_preparations_remain_visible_without_blocking_safe_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            identifier = "a" * 32
            (state / "preparations" / identifier).mkdir(parents=True)
            self.assertEqual(abandoned_preparations(state), [identifier])
            self.assertEqual(pending_recovery_runs(state), [])

    def test_interrupted_backup_does_not_publish_an_invalid_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "destination"
            source.write_bytes(b"new")
            destination.write_bytes(b"old")
            plan = make_plan(source, destination, b"old")
            engine = TransactionEngine(root / "state", process_probe=lambda: False)
            with patch("claude_session_sync.journal.atomic_copy", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    engine.apply(plan)
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(pending_recovery_runs(root / "state"), [])
            self.assertEqual(engine.apply(plan).status, "committed")
            self.assertEqual(destination.read_bytes(), b"new")

    def test_noop_aborts_if_an_app_reopens_after_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = SimpleNamespace(
                operations=(),
                invalid_replicas=(),
                plan_id="plan-noop",
            )
            state = root / "state"

            with self.assertRaises(AppRunningError):
                TransactionEngine(state, process_probe=lambda: True).apply(plan)

            self.assertFalse((state / "journal").exists())

    def test_apply_atomically_copies_and_returns_committed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            source = root / "source.json"
            destination = root / "target" / "local_session-1.json"
            destination.parent.mkdir()
            source.write_bytes(b'{"new":true}\n')
            destination.write_bytes(b'{"old":true}\n')
            plan = make_plan(source, destination, destination.read_bytes())

            receipt = TransactionEngine(
                root / "state", process_probe=lambda: False
            ).apply(plan)

            self.assertEqual(destination.read_bytes(), b'{"new":true}\n')
            self.assertEqual(receipt.status, "committed")
            self.assertEqual(receipt.plan_id, "plan-1")
            self.assertEqual(receipt.operation_count, 1)
            self.assertEqual(receipt.bytes_copied, len(b'{"new":true}\n'))
            self.assertIsNotNone(receipt.run_id)
            manifest = json.loads(
                (root / "state" / "runs" / receipt.run_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["phase"], "COMMITTED")
            self.assertEqual(manifest["receipt"]["run_id"], receipt.run_id)
            self.assertEqual(manifest["receipt"]["status"], "committed")

    def test_apply_revalidates_source_and_destination_before_journaling(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            source = root / "source.json"
            destination = root / "target" / "local_session-1.json"
            destination.parent.mkdir()
            source.write_bytes(b"planned source")
            destination.write_bytes(b"planned destination")
            plan = make_plan(source, destination, destination.read_bytes())
            destination.write_bytes(b"changed after planning")

            with self.assertRaises(RevalidationError):
                TransactionEngine(root / "state", process_probe=lambda: False).apply(
                    plan
                )

            self.assertEqual(destination.read_bytes(), b"changed after planning")
            self.assertFalse((root / "state" / "runs").exists())

    def test_apply_aborts_without_journal_when_app_is_already_running(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            source = root / "source.json"
            destination = root / "target" / "local_session-1.json"
            destination.parent.mkdir()
            source.write_bytes(b"source")
            destination.write_bytes(b"destination")
            plan = make_plan(source, destination, destination.read_bytes())

            with self.assertRaises(AppRunningError):
                TransactionEngine(root / "state", process_probe=lambda: True).apply(
                    plan
                )

            self.assertEqual(destination.read_bytes(), b"destination")
            self.assertFalse((root / "state" / "runs").exists())

    def test_apply_aborts_if_app_reopens_immediately_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            source = root / "source.json"
            destination = root / "target" / "local_session-1.json"
            destination.parent.mkdir()
            source.write_bytes(b"source")
            destination.write_bytes(b"destination")
            plan = make_plan(source, destination, destination.read_bytes())
            probe_results = iter((False, True))

            with self.assertRaises(AppRunningError):
                TransactionEngine(
                    root / "state", process_probe=lambda: next(probe_results)
                ).apply(plan)

            self.assertEqual(destination.read_bytes(), b"destination")
            manifests = list((root / "state" / "runs").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            self.assertIn(b'"phase":"ROLLED_BACK"', manifests[0].read_bytes())

    def test_applying_the_same_plan_twice_is_a_noop_with_one_journal(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            source = root / "source.json"
            destination = root / "target" / "local_session-1.json"
            destination.parent.mkdir()
            source.write_bytes(b"source")
            destination.write_bytes(b"destination")
            plan = make_plan(source, destination, destination.read_bytes())
            engine = TransactionEngine(root / "state", process_probe=lambda: False)

            first = engine.apply(plan)
            second = engine.apply(plan)

            self.assertEqual(first.status, "committed")
            self.assertEqual(second.status, "noop")
            self.assertIsNone(second.run_id)
            manifests = list((root / "state" / "runs").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)

    def test_private_transaction_state_uses_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            source = root / "source.json"
            destination = root / "target" / "local_session-1.json"
            destination.parent.mkdir()
            source.write_bytes(b"source")
            destination.write_bytes(b"destination")
            plan = make_plan(source, destination, destination.read_bytes())

            receipt = TransactionEngine(
                root / "state", process_probe=lambda: False
            ).apply(plan)

            run_root = root / "state" / "runs" / receipt.run_id
            private_directories = (
                root / "state",
                root / "state" / "runs",
                run_root,
                run_root / "preimages",
            )
            private_files = tuple(run_root.glob("preimages/*")) + (
                root / "state" / "transaction.lock",
                root / "state" / "journal.key",
                run_root / "manifest.json",
            )
            self.assertEqual(len(private_files), 4)
            for directory in private_directories:
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            for file_path in private_files:
                self.assertEqual(stat.S_IMODE(file_path.stat().st_mode), 0o600)

    def test_plan_with_invalid_replicas_is_blocked_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            blocked_plan = SimpleNamespace(
                operations=(),
                invalid_replicas=(SimpleNamespace(reason="symlink is not allowed"),),
                plan_id="blocked-plan",
            )

            with self.assertRaises(PlanBlockedError):
                TransactionEngine(root / "state", process_probe=lambda: False).apply(
                    blocked_plan
                )

            self.assertFalse((root / "state").exists())

    def test_commit_transition_cannot_hide_destination_change(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            source = root / "source.json"
            destination = root / "target" / "local_session-1.json"
            destination.parent.mkdir()
            source.write_bytes(b"source")
            destination.write_bytes(b"destination")
            plan = make_plan(source, destination, destination.read_bytes())

            def mutate_at_commit(phase: str, unused_run_id: str) -> None:
                if phase == "COMMITTING":
                    destination.write_bytes(b"concurrent change")

            with self.assertRaises(RecoveryError):
                TransactionEngine(
                    root / "state",
                    process_probe=lambda: False,
                    fault_injector=mutate_at_commit,
                ).apply(plan)

            self.assertEqual(destination.read_bytes(), b"concurrent change")

    def test_lock_symlink_is_rejected_without_touching_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            state = root / "state"
            state.mkdir()
            victim = root / "victim"
            victim.write_bytes(b"do not touch")
            original_mode = stat.S_IMODE(victim.stat().st_mode)
            (state / "transaction.lock").symlink_to(victim)
            source = root / "source.json"
            destination = root / "target" / "local_session-1.json"
            destination.parent.mkdir()
            source.write_bytes(b"source")
            destination.write_bytes(b"destination")

            with self.assertRaises(OSError):
                TransactionEngine(state, process_probe=lambda: False).apply(
                    make_plan(source, destination, destination.read_bytes())
                )

            self.assertEqual(victim.read_bytes(), b"do not touch")
            self.assertEqual(stat.S_IMODE(victim.stat().st_mode), original_mode)

    def test_retention_prunes_only_old_terminal_runs(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            terminal_run_ids = []
            for index in range(2):
                source = root / "source-{}.json".format(index)
                destination = target / "local_{}.json".format(index)
                source.write_bytes("source-{}".format(index).encode("utf-8"))
                receipt = TransactionEngine(
                    root / "state",
                    process_probe=lambda: False,
                ).apply(make_plan(source, destination, None))
                terminal_run_ids.append(receipt.run_id)

            crashing_source = root / "crashing-source.json"
            crashing_destination = target / "local_crashing.json"
            crashing_source.write_bytes(b"crashing")
            incomplete_run_ids = []

            def crash_after_journal(phase: str, run_id: str) -> None:
                if phase == "JOURNALING":
                    incomplete_run_ids.append(run_id)
                    raise KeyboardInterrupt("leave an incomplete run")

            with self.assertRaises(KeyboardInterrupt):
                TransactionEngine(
                    root / "state",
                    process_probe=lambda: False,
                    fault_injector=crash_after_journal,
                    retention=1,
                ).apply(make_plan(crashing_source, crashing_destination, None))

            # A young terminal run doubles as the kept copy of what it replaced.
            engine = TransactionEngine(root / "state", process_probe=lambda: False, retention=1)
            engine.rollback(terminal_run_ids[0])
            runs = root / "state" / "runs"
            self.assertEqual(
                {path.name for path in runs.iterdir()},
                {incomplete_run_ids[0], *terminal_run_ids},
            )

            # Past 30 days it goes, but the newest run and unfinished runs stay.
            month_old = time.time() - 31 * 24 * 60 * 60
            manifest = runs / terminal_run_ids[1] / "manifest.json"
            os.utime(manifest, (month_old, month_old))
            engine.rollback(terminal_run_ids[0])

            remaining = {path.name for path in runs.iterdir()}
            self.assertEqual(remaining, {incomplete_run_ids[0], terminal_run_ids[0]})
            self.assertFalse((runs / terminal_run_ids[1]).exists())

    def test_incomplete_run_blocks_apply_until_public_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            crashed_source = root / "crashed-source.json"
            crashed_destination = target / "local_crashed.json"
            crashed_source.write_bytes(b"crashed")
            captured_run_ids = []

            def crash_after_journal(phase: str, run_id: str) -> None:
                if phase == "JOURNALING":
                    captured_run_ids.append(run_id)
                    raise KeyboardInterrupt("crash")

            with self.assertRaises(KeyboardInterrupt):
                TransactionEngine(
                    root / "state",
                    process_probe=lambda: False,
                    fault_injector=crash_after_journal,
                ).apply(make_plan(crashed_source, crashed_destination, None))

            next_source = root / "next-source.json"
            next_destination = target / "local_next.json"
            next_source.write_bytes(b"next")
            engine = TransactionEngine(root / "state", process_probe=lambda: False)
            with self.assertRaises(RecoveryPendingError):
                engine.apply(make_plan(next_source, next_destination, None))
            self.assertFalse(next_destination.exists())

            engine.rollback(captured_run_ids[0])
            self.assertEqual(
                engine.apply(make_plan(next_source, next_destination, None)).status,
                "committed",
            )

    def test_malformed_journal_state_blocks_apply(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            source = root / "source.json"
            destination = target / "local_session.json"
            source.write_bytes(b"source")
            (root / "state" / "runs" / "not-a-run").mkdir(parents=True)

            with self.assertRaises(RecoveryPendingError):
                TransactionEngine(root / "state", process_probe=lambda: False).apply(
                    make_plan(source, destination, None)
                )

            self.assertFalse(destination.exists())

    def test_large_plan_uses_constant_manifest_writes_and_process_probes(self) -> None:
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            target = root / "target"
            target.mkdir()
            operations = []
            for index in range(40):
                source = root / "source-{}.json".format(index)
                destination = target / "local_{}.json".format(index)
                source.write_bytes("source-{}".format(index).encode("utf-8"))
                operations.append(make_plan(source, destination, None).operations[0])
            bulk_plan = SimpleNamespace(
                operations=tuple(operations),
                invalid_replicas=(),
                plan_id="bulk-plan",
            )
            probe_calls = []

            def process_probe() -> bool:
                probe_calls.append(True)
                return False

            receipt = TransactionEngine(
                root / "state", process_probe=process_probe
            ).apply(bulk_plan)
            manifest = json.loads(
                (root / "state" / "runs" / receipt.run_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(len(probe_calls), 2)
            self.assertLessEqual(manifest["generation"], 6)


if __name__ == "__main__":
    unittest.main()
