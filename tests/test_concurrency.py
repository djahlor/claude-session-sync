from __future__ import annotations

import hashlib
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plan(source: Path, destination: Path, destination_before: bytes):
    source_bytes = source.read_bytes()
    return SimpleNamespace(
        operations=(
            SimpleNamespace(
                kind="copy",
                session_id="concurrent",
                source=source,
                destination=destination,
                source_digest=_digest(source_bytes),
                destination_digest_or_none=_digest(destination_before),
                size=len(source_bytes),
            ),
        ),
        invalid_replicas=(),
        plan_id="concurrent-plan",
    )


def _apply_worker(
    run_plan,
    state_root: Path,
    results,
    locked=None,
    release=None,
) -> None:
    from claude_session_sync.transaction import TransactionBusyError, TransactionEngine

    def hold_at_staging(phase: str, unused_run_id: str) -> None:
        if phase == "STAGING" and locked is not None and release is not None:
            locked.set()
            if not release.wait(5):
                raise RuntimeError("test timed out while holding transaction lock")

    try:
        receipt = TransactionEngine(
            state_root,
            process_probe=lambda: False,
            fault_injector=hold_at_staging if locked is not None else None,
        ).apply(run_plan)
        results.put(receipt.status)
    except TransactionBusyError:
        results.put("busy")
    except BaseException as error:
        results.put("error:{}:{}".format(type(error).__name__, error))


class ConcurrencyTests(unittest.TestCase):
    def test_two_processes_yield_one_writer_and_one_journal(self) -> None:
        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            source = root / "source.json"
            destination = root / "target" / "local_session.json"
            destination.parent.mkdir()
            source.write_bytes(b"replacement")
            destination.write_bytes(b"preimage")
            run_plan = _plan(source, destination, destination.read_bytes())
            results = context.Queue()
            locked = context.Event()
            release = context.Event()
            first = context.Process(
                target=_apply_worker,
                args=(run_plan, root / "state", results, locked, release),
            )
            second = context.Process(
                target=_apply_worker,
                args=(run_plan, root / "state", results),
            )

            first.start()
            self.assertTrue(locked.wait(5), "first writer never acquired the lock")
            second.start()
            second.join(5)
            release.set()
            first.join(5)

            self.assertEqual(first.exitcode, 0)
            self.assertEqual(second.exitcode, 0)
            outcomes = sorted((results.get(timeout=2), results.get(timeout=2)))
            self.assertEqual(outcomes, ["busy", "committed"])
            manifests = list((root / "state" / "runs").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            self.assertEqual(destination.read_bytes(), b"replacement")

    def test_manual_lock_wait_is_bounded_by_timeout(self) -> None:
        from claude_session_sync.transaction import (
            TransactionBusyError,
            TransactionEngine,
        )

        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as root_string:
            root = Path(root_string)
            source = root / "source.json"
            destination = root / "target" / "local_session.json"
            destination.parent.mkdir()
            source.write_bytes(b"replacement")
            destination.write_bytes(b"preimage")
            run_plan = _plan(source, destination, destination.read_bytes())
            results = context.Queue()
            locked = context.Event()
            release = context.Event()
            holder = context.Process(
                target=_apply_worker,
                args=(run_plan, root / "state", results, locked, release),
            )
            holder.start()
            self.assertTrue(locked.wait(5), "holder never acquired the lock")
            started = time.monotonic()
            try:
                with self.assertRaises(TransactionBusyError):
                    TransactionEngine(
                        root / "state",
                        process_probe=lambda: False,
                        lock_mode="manual",
                        lock_timeout=0.15,
                    ).apply(run_plan)
            finally:
                elapsed = time.monotonic() - started
                release.set()
                holder.join(5)

            self.assertGreaterEqual(elapsed, 0.10)
            self.assertLess(elapsed, 1.0)
            self.assertEqual(holder.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
