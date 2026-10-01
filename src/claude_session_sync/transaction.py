"""Single-writer apply and rollback transactions."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from .filesystem import (
    UnsafePathError,
    atomic_copy,
    commit_staged,
    commit_staged_new,
    digest_file,
    durable_unlink,
    ensure_private_directory,
    regular_file_size,
    stage_copy,
)
from .journal import (
    JournalError,
    RunJournal,
    pending_recovery_runs,
    prune_terminal_runs,
)
from .locking import ExclusiveFileLock, LockUnavailableError
from .model import RecoveryReceipt, RunReceipt


PathLike = Union[str, os.PathLike]
FaultInjector = Callable[[str, str], None]
LiveGuard = Callable[[Path], bool]
WRITE_KINDS = ("copy", "create", "replace")


class TransactionError(RuntimeError):
    """Base class for safe transaction failures."""


class TransactionBusyError(TransactionError):
    """Raised when another writer owns the transaction lock."""


class AppRunningError(TransactionError):
    """Raised when a managed Claude process is running."""


class PlanBlockedError(TransactionError):
    """Raised when a plan contains conflicts or invalid replicas."""


class RevalidationError(TransactionError):
    """Raised when live files no longer match the immutable plan."""


class RecoveryError(TransactionError):
    """Raised when rollback cannot safely restore every destination."""

    def __init__(
        self,
        message: str,
        *,
        transaction_error: Optional[BaseException] = None,
        recovery_error: Optional[BaseException] = None,
        persistence_error: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.transaction_error = transaction_error
        self.recovery_error = recovery_error
        self.persistence_error = persistence_error


class RecoveryPendingError(TransactionError):
    """Raised when apply must wait for an incomplete run to be recovered."""


def _group(record: Mapping[str, Any]) -> Tuple[str, str]:
    """One chat in one folder: its steps succeed or wait together."""

    return (
        record["session_id"],
        os.path.dirname(os.path.abspath(record["destination"])),
    )


class TransactionEngine:
    """Apply immutable plans and roll committed runs back safely.

    ``process_probe`` must return true when any managed Claude process is
    running. ``fault_injector`` is called as ``(phase, run_id)`` immediately
    after each durable journal phase transition.
    """

    def __init__(
        self,
        state_root: PathLike,
        process_probe: Callable[[], bool],
        *,
        lock_mode: str = "auto",
        lock_timeout: float = 5.0,
        fault_injector: Optional[FaultInjector] = None,
        retention: Optional[int] = None,
    ) -> None:
        if retention is not None and retention < 1:
            raise ValueError("retention must be at least one run")
        self.state_root = Path(state_root)
        self.process_probe = process_probe
        self.lock_mode = lock_mode
        self.lock_timeout = lock_timeout
        self.fault_injector = fault_injector
        self.retention = retention

    def apply(self, plan: Any, *, live_guard: Optional[LiveGuard] = None) -> RunReceipt:
        """Apply a plan under the single-writer lock.

        Without ``live_guard`` every Claude process must be stopped and any
        drift aborts the run. With it, Claude may run: a file that moved on, or
        a folder that turned live, is skipped and left for the next run, and
        creates never overwrite a file that appeared meanwhile.
        """

        if plan.conflicts or plan.invalid_replicas:
            raise PlanBlockedError(
                "plan {} has conflicts or invalid replicas".format(plan.plan_id)
            )
        with self._writer_lock():
            try:
                pending = pending_recovery_runs(self.state_root)
            except JournalError as error:
                raise RecoveryPendingError(
                    "journal state is invalid; recovery is required before apply"
                ) from error
            if pending and live_guard is not None:
                pending = self._finish_interrupted_live_runs(pending)
            if pending:
                raise RecoveryPendingError(
                    "{} run(s) require recovery before apply".format(len(pending))
                )
            receipt = self._apply_locked(plan, live_guard)
            if self.retention is not None and receipt.status in ("committed", "partial"):
                prune_terminal_runs(self.state_root, self.retention)
            return receipt

    def rollback(self, run_id: str) -> RecoveryReceipt:
        with self._writer_lock():
            try:
                journal = RunJournal.load(self.state_root, run_id)
            except JournalError as error:
                raise RecoveryError(str(error), recovery_error=error) from error
            receipt = self._rollback_locked(journal)
            if self.retention is not None:
                prune_terminal_runs(self.state_root, self.retention)
            return receipt

    def close_interrupted_runs(self) -> None:
        """Close runs a killed process left open, keeping what they wrote.

        Each write is one atomic step toward a version the next plan works out
        again, so a run cut short equals a run whose other steps were skipped.
        The journal keeps every file a run replaced or removed, for a manual
        rollback. A run whose journal cannot be read stays open, and apply
        refuses until it is recovered.
        """

        with self._writer_lock():
            try:
                pending = pending_recovery_runs(self.state_root)
            except JournalError:
                return
            self._finish_interrupted_runs(pending)

    @contextmanager
    def _writer_lock(self):
        ensure_private_directory(self.state_root)
        lock = ExclusiveFileLock(
            self.state_root / "transaction.lock",
            mode=self.lock_mode,
            timeout=self.lock_timeout,
        )
        try:
            lock.acquire()
        except LockUnavailableError as error:
            raise TransactionBusyError(str(error)) from error
        try:
            yield
        finally:
            lock.release()

    def _apply_locked(self, plan: Any, live_guard: Optional[LiveGuard]) -> RunReceipt:
        lenient = live_guard is not None
        records, skipped, blocked = self._revalidate(plan.operations, lenient=lenient)
        if not lenient:
            self._ensure_apps_stopped()
        if not records:
            return self._noop_receipt(plan.plan_id, skipped)

        run_id = uuid.uuid4().hex
        journal: Optional[RunJournal] = None
        staged: Dict[int, Path] = {}
        applied: List[Dict[str, Any]] = []
        try:
            journal = RunJournal.create(
                self.state_root, plan.plan_id, records, run_id=run_id, lenient=lenient
            )
            self._inject("JOURNALING", run_id)

            self._transition(journal, "STAGING")
            staged_identities = []  # type: List[Dict[str, int]]
            for record in journal.records:
                if record.get("kind", "copy") not in WRITE_KINDS:
                    continue
                staged_path = stage_copy(
                    Path(record["source"]), Path(record["destination"])
                )
                staged[record["index"]] = staged_path
                if digest_file(staged_path) != record["source_digest"]:
                    if not lenient:
                        raise RevalidationError(
                            "source changed while staging: {}".format(record["source"])
                        )
                    staged.pop(record["index"])
                    staged_path.unlink()
                    record["skipped"] = "source-moved-on"
                    blocked.add(_group(record))
                    skipped += 1
                    continue
                staged_metadata = staged_path.stat()
                staged_identities.append(
                    {
                        "index": record["index"],
                        "device": staged_metadata.st_dev,
                        "inode": staged_metadata.st_ino,
                    }
                )
            journal.mark_staged(staged_identities)

            self._transition(journal, "COMMITTING")
            if not lenient:
                self._verify_destinations_unchanged(journal.records)
                self._ensure_apps_stopped()
            for record in journal.records:
                if record.get("skipped"):
                    continue
                destination = Path(record["destination"])
                kind = record.get("kind", "copy")
                if lenient:
                    # Steps for one chat in one folder go together: a marker is
                    # never removed when the record meant to replace it was not
                    # written, nor written when the record could not be removed.
                    reason = (
                        "partner-skipped"
                        if _group(record) in blocked
                        else self._lenient_blocker(record, destination, live_guard)
                    )
                    if reason is not None:
                        record["skipped"] = reason
                        blocked.add(_group(record))
                        skipped += 1
                        continue
                if kind == "retire":
                    # Written down first: a crash right after the removal must
                    # still let recovery put the file back.
                    journal.mark_retire_started(record["index"])
                    durable_unlink(destination)
                elif kind == "create":
                    try:
                        commit_staged_new(staged[record["index"]], destination)
                    except FileExistsError:
                        if not lenient:
                            raise
                        record["skipped"] = "destination-appeared"
                        blocked.add(_group(record))
                        skipped += 1
                        continue
                else:
                    commit_staged(staged[record["index"]], destination)
                journal.mark_applied(record["index"])
                applied.append(record)

            self._transition(journal, "VERIFYING")
            for record in applied:
                destination = Path(record["destination"])
                if record.get("kind", "copy") == "retire":
                    if os.path.lexists(str(destination)):
                        raise RevalidationError(
                            "retired file is still present: {}".format(destination)
                        )
                    continue
                actual = self._digest_existing(destination)
                if actual != record["source_digest"]:
                    raise RevalidationError(
                        "committed destination failed verification: {}".format(
                            destination
                        )
                    )
            status = "partial" if skipped else "committed"
            receipt = RunReceipt(
                run_id=run_id,
                status=status,
                plan_id=plan.plan_id,
                operation_count=len(applied),
                bytes_copied=sum(
                    record["size"]
                    for record in applied
                    if record.get("kind", "copy") != "retire"
                ),
                skipped_count=skipped,
                applied=tuple(
                    plan.operations[record["operation_index"]] for record in applied
                ),
            )
            journal.finish(
                "COMMITTED",
                {
                    "kind": "apply",
                    "run_id": receipt.run_id,
                    "status": receipt.status,
                    "plan_id": receipt.plan_id,
                    "operation_count": receipt.operation_count,
                    "bytes_copied": receipt.bytes_copied,
                    "skipped_count": receipt.skipped_count,
                },
            )
            self._inject("COMMITTED", run_id)
            return receipt
        except Exception as transaction_error:
            if journal is None:
                raise
            try:
                self._rollback_locked(journal, strict=not lenient)
            except Exception as recovery_error:
                persistence_error: Optional[BaseException] = None
                try:
                    journal.set_phase("RECOVERY_REQUIRED")
                except Exception as phase_error:
                    persistence_error = phase_error
                persistence_detail = (
                    "; recovery state persistence also failed: {}".format(
                        persistence_error
                    )
                    if persistence_error is not None
                    else ""
                )
                raise RecoveryError(
                    "transaction failed and rollback also failed: {}; {}{}".format(
                        transaction_error, recovery_error, persistence_detail
                    ),
                    transaction_error=transaction_error,
                    recovery_error=recovery_error,
                    persistence_error=persistence_error,
                ) from transaction_error
            raise
        finally:
            for staged_path in staged.values():
                try:
                    staged_path.unlink()
                except OSError:
                    pass

    def _lenient_blocker(
        self, record: Mapping[str, Any], destination: Path, live_guard: LiveGuard
    ) -> Optional[str]:
        """Why this one record must wait for the next run, if it must."""

        kind = record.get("kind", "copy")
        exists = os.path.lexists(str(destination))
        if record["existed"]:
            if not exists:
                return "destination-moved-on"
            try:
                current = digest_file(destination)
            except (OSError, UnsafePathError):
                return "destination-moved-on"
            if current != record["destination_digest_before"]:
                return "destination-moved-on"
        elif exists:
            return "destination-appeared"
        # Claude writes the signed-in account's chats from memory: only a brand
        # new file may be added there. Re-check right before the write.
        if kind != "create" and live_guard(destination):
            return "live"
        return None

    def _revalidate(
        self, operations: Iterable[Any], *, lenient: bool = False
    ) -> Tuple[List[Dict[str, Any]], int, set]:
        records = []  # type: List[Dict[str, Any]]
        destinations = set()
        skipped = 0
        blocked = set()
        for operation_index, operation in enumerate(operations):
            kind = getattr(operation, "kind", "copy")
            if kind not in WRITE_KINDS + ("retire",):
                raise RevalidationError(
                    "unsupported operation kind: {}".format(kind)
                )
            source = Path(operation.source)
            destination = Path(operation.destination)
            destination_key = os.path.abspath(str(destination))
            if destination_key in destinations:
                raise RevalidationError(
                    "plan contains duplicate destination: {}".format(destination)
                )
            destinations.add(destination_key)
            group = (operation.session_id, os.path.dirname(destination_key))
            if lenient and group in blocked:
                skipped += 1
                continue

            try:
                source_digest = digest_file(source)
                source_size = regular_file_size(source)
            except (OSError, UnsafePathError) as error:
                if lenient:
                    blocked.add(group)
                    skipped += 1
                    continue
                raise RevalidationError(
                    "cannot revalidate source {}: {}".format(source, error)
                ) from error
            if (
                source_digest != operation.source_digest
                or source_size != operation.size
            ):
                if lenient:
                    blocked.add(group)
                    skipped += 1
                    continue
                raise RevalidationError(
                    "source no longer matches plan: {}".format(source)
                )

            destination_exists = os.path.lexists(str(destination))
            if destination_exists:
                destination_digest = self._digest_existing(destination)
                try:
                    destination_size_before = regular_file_size(destination)
                except (OSError, UnsafePathError) as error:
                    raise RevalidationError(
                        "cannot size destination {}: {}".format(destination, error)
                    ) from error
            else:
                destination_digest = None
                destination_size_before = 0

            planned_destination = operation.destination_digest_or_none
            if kind != "retire" and destination_digest == operation.source_digest:
                continue
            if destination_digest != planned_destination:
                if lenient:
                    blocked.add(group)
                    skipped += 1
                    continue
                raise RevalidationError(
                    "destination no longer matches plan: {}".format(destination)
                )

            records.append(
                {
                    "index": len(records),
                    "operation_index": operation_index,
                    "kind": kind,
                    "artifact": getattr(operation, "artifact", "record"),
                    "source": str(source),
                    "destination": str(destination),
                    "source_digest": operation.source_digest,
                    "destination_digest_before": destination_digest,
                    "destination_size_before": destination_size_before,
                    "existed": destination_exists,
                    "size": operation.size,
                    "session_id": operation.session_id,
                }
            )
        return records, skipped, blocked

    def _finish_interrupted_live_runs(self, pending: List[str]) -> List[str]:
        """Close runs a crash left open in live mode, keeping what they wrote."""

        remaining = []
        for run_id in pending:
            try:
                journal = RunJournal.load(self.state_root, run_id)
            except JournalError:
                remaining.append(run_id)
                continue
            if not journal.manifest.get("lenient"):
                remaining.append(run_id)
                continue
            self._close_as_recovered(journal)
        return remaining

    def _finish_interrupted_runs(self, pending: List[str]) -> None:
        for run_id in pending:
            try:
                journal = RunJournal.load(self.state_root, run_id)
            except JournalError:
                continue
            self._close_as_recovered(journal)

    @staticmethod
    def _close_as_recovered(journal: RunJournal) -> None:
        journal.finish(
            "COMMITTED",
            {
                "kind": "apply",
                "run_id": journal.run_id,
                "status": "recovered",
                "plan_id": journal.manifest.get("plan_id"),
            },
        )

    def _verify_destinations_unchanged(
        self, records: Iterable[Mapping[str, Any]]
    ) -> None:
        for record in records:
            self._verify_destination_unchanged(record)

    def _verify_destination_unchanged(self, record: Mapping[str, Any]) -> None:
        destination = Path(record["destination"])
        exists = os.path.lexists(str(destination))
        if record["existed"]:
            if (
                not exists
                or self._digest_existing(destination)
                != record["destination_digest_before"]
            ):
                raise RevalidationError(
                    "destination changed before commit: {}".format(destination)
                )
        elif exists:
            raise RevalidationError(
                "new destination appeared before commit: {}".format(destination)
            )

    def _rollback_locked(self, journal: RunJournal, *, strict: bool = True) -> RecoveryReceipt:
        """Undo a run's writes.

        Strict mode, used by the rollback command, refuses to touch anything it
        cannot fully explain and needs Claude closed. Automatic rollback after a
        failed live run instead leaves alone any file that changed after the
        run wrote it: that newer content belongs to Claude, not to this run.
        """

        if journal.phase == "ROLLED_BACK":
            return RecoveryReceipt(
                run_id=journal.run_id,
                status="already_rolled_back",
                operation_count=0,
                bytes_restored=0,
            )

        # Verify every preimage before examining or mutating a live destination.
        for record in journal.records:
            if record["existed"] and not record.get("skipped"):
                try:
                    actual = digest_file(journal.preimage_path(record))
                except (OSError, JournalError, UnsafePathError) as error:
                    raise RecoveryError(
                        "cannot verify preimage for {}: {}".format(
                            record["destination"], error
                        ),
                        recovery_error=error,
                    ) from error
                if actual != record["destination_digest_before"]:
                    raise RecoveryError(
                        "preimage digest mismatch for {}".format(record["destination"])
                    )

        def refuse(message: str) -> None:
            if strict:
                raise RecoveryError(message)

        actions: List[Tuple[str, Dict[str, Any]]] = []
        for record in journal.records:
            if record.get("skipped"):
                continue
            destination = Path(record["destination"])
            exists = os.path.lexists(str(destination))
            kind = record.get("kind", "copy")
            if kind == "retire":
                if exists:
                    if self._digest_existing(destination) != record["destination_digest_before"]:
                        refuse("retired file came back changed: {}".format(destination))
                    continue
                if not (record["applied"] or record.get("retire_started")):
                    refuse("retired file was removed by someone else: {}".format(destination))
                    continue
                actions.append(("unretire", record))
            elif record["existed"]:
                if not exists:
                    refuse("existing destination disappeared: {}".format(destination))
                    continue
                current_digest = self._digest_existing(destination)
                if current_digest == record["destination_digest_before"]:
                    continue
                if current_digest != record["source_digest"]:
                    refuse(
                        "destination changed after run; refusing restore: {}".format(
                            destination
                        )
                    )
                    continue
                if not self._record_was_applied(record, destination):
                    refuse(
                        "destination has planned bytes but was not applied by run: {}".format(
                            destination
                        )
                    )
                    continue
                actions.append(("restore", record))
            else:
                if not exists:
                    continue
                current_digest = self._digest_existing(destination)
                if current_digest != record["source_digest"]:
                    refuse(
                        "new destination changed after run; refusing removal: {}".format(
                            destination
                        )
                    )
                    continue
                if not self._record_was_applied(record, destination):
                    refuse(
                        "new destination was not applied by run; refusing removal: {}".format(
                            destination
                        )
                    )
                    continue
                actions.append(("remove", record))

        if actions and strict:
            self._ensure_apps_stopped()
        restored = 0
        removed = 0
        bytes_restored = 0
        try:
            self._transition(journal, "ABORTING")
            for action, record in reversed(actions):
                destination = Path(record["destination"])
                if action == "unretire":
                    if os.path.lexists(str(destination)):
                        if strict:
                            raise RecoveryError(
                                "retired file reappeared during rollback: {}".format(
                                    destination
                                )
                            )
                        continue
                    atomic_copy(journal.preimage_path(record), destination)
                    restored += 1
                    bytes_restored += int(record["destination_size_before"])
                    journal.mark_unapplied(record["index"])
                    continue
                current_digest = self._digest_existing(destination)
                if current_digest != record["source_digest"]:
                    if strict:
                        raise RecoveryError(
                            "destination changed during rollback: {}".format(destination)
                        )
                    continue
                if action == "restore":
                    atomic_copy(journal.preimage_path(record), destination)
                    restored += 1
                    bytes_restored += int(record["destination_size_before"])
                else:
                    durable_unlink(destination)
                    removed += 1
                journal.mark_unapplied(record["index"])
            receipt = RecoveryReceipt(
                run_id=journal.run_id,
                status="rolled_back",
                operation_count=restored + removed,
                bytes_restored=bytes_restored,
            )
            journal.finish(
                "ROLLED_BACK",
                {
                    "kind": "rollback",
                    "run_id": receipt.run_id,
                    "status": receipt.status,
                    "operation_count": receipt.operation_count,
                    "bytes_restored": receipt.bytes_restored,
                },
            )
            self._inject("ROLLED_BACK", journal.run_id)
        except Exception as error:
            try:
                journal.set_phase("RECOVERY_REQUIRED")
            except Exception as persist_error:
                raise RecoveryError(
                    "rollback failed and recovery state could not be persisted: {}; {}".format(
                        error, persist_error
                    ),
                    recovery_error=error,
                    persistence_error=persist_error,
                ) from error
            if isinstance(error, RecoveryError):
                raise
            raise RecoveryError(
                "rollback failed: {}".format(error), recovery_error=error
            ) from error

        return receipt

    @staticmethod
    def _record_was_applied(record: Mapping[str, Any], destination: Path) -> bool:
        if record["applied"]:
            return True
        staged_device = record.get("staged_device")
        staged_inode = record.get("staged_inode")
        if staged_device is None or staged_inode is None:
            return False
        try:
            metadata = os.stat(str(destination), follow_symlinks=False)
        except OSError:
            return False
        return metadata.st_dev == staged_device and metadata.st_ino == staged_inode

    def _ensure_apps_stopped(self) -> None:
        if self.process_probe():
            raise AppRunningError("a managed Claude process is running")

    def _transition(self, journal: RunJournal, phase: str) -> None:
        journal.set_phase(phase)
        self._inject(phase, journal.run_id)

    def _inject(self, phase: str, run_id: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(phase, run_id)

    @staticmethod
    def _digest_existing(path: Path) -> str:
        try:
            return digest_file(path)
        except (OSError, UnsafePathError) as error:
            raise RevalidationError(
                "cannot hash destination {}: {}".format(path, error)
            ) from error

    @staticmethod
    def _noop_receipt(plan_id: str, skipped: int = 0) -> RunReceipt:
        return RunReceipt(
            run_id=None,
            status="partial" if skipped else "noop",
            plan_id=plan_id,
            operation_count=0,
            bytes_copied=0,
            skipped_count=skipped,
        )
