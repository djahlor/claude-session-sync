"""Synchronization for Claude Desktop Code routine manifests."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Set, Tuple

from .config import Config
from .filesystem import (
    atomic_write_bytes,
    durable_unlink,
    ensure_private_directory,
    fsync_directory,
)
from .locking import ExclusiveFileLock, LockUnavailableError
from .store import SessionStore


SNAPSHOT_VERSION = 1
JOURNAL_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_TASKS = 10_000
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
MISSING = object()
TargetKey = Tuple[str, str, str]


class RoutineError(RuntimeError):
    """Raised when Code routines cannot be synchronized safely."""


class RoutineBusyError(RoutineError):
    """Raised when another synchronization writer owns the lock."""


class RoutineRecoveryError(RoutineError):
    """Raised when a routine manifest cannot be restored automatically."""


@dataclass(frozen=True)
class RoutineSample:
    target: TargetKey
    path: Path
    mtime_ns: int
    document: Optional[Mapping[str, Any]]

    @property
    def exists(self) -> bool:
        return self.document is not None


@dataclass(frozen=True)
class RoutineSnapshot:
    targets: Tuple[TargetKey, ...]
    manifest: Mapping[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": SNAPSHOT_VERSION,
            "targets": [list(target) for target in self.targets],
            "manifest": copy.deepcopy(dict(self.manifest)),
        }


@dataclass(frozen=True)
class RoutineTransform:
    manifest: Mapping[str, Any]
    snapshot: RoutineSnapshot
    task_count: int


@dataclass(frozen=True)
class RoutineReceipt:
    state: str
    profile_count: int
    target_count: int
    manifest_count: int
    task_count: int
    write_count: int


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _value_key(value: Any) -> bytes:
    return b"<missing>" if value is MISSING else _canonical(value)


def _clone(value: Any) -> Any:
    return MISSING if value is MISSING else copy.deepcopy(value)


def _task_map(document: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    return {task["id"]: task for task in document["scheduledTasks"]}


def _validate_manifest(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise RoutineError("{} is not a JSON object".format(label))
    tasks = value.get("scheduledTasks")
    if not isinstance(tasks, list) or len(tasks) > MAX_TASKS:
        raise RoutineError("{} has an unknown task list".format(label))
    seen: Set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise RoutineError("{} contains an invalid task".format(label))
        task_id = task.get("id")
        if (
            not isinstance(task_id, str)
            or not TASK_ID.fullmatch(task_id)
            or task_id in (".", "..")
        ):
            raise RoutineError("{} contains an unsafe task id".format(label))
        if task_id in seen:
            raise RoutineError("{} contains duplicate task ids".format(label))
        seen.add(task_id)
        if not isinstance(task.get("enabled"), bool):
            raise RoutineError("{} contains an invalid enabled value".format(label))
        file_path = task.get("filePath")
        if not isinstance(file_path, str) or not file_path:
            raise RoutineError("{} contains an invalid task file path".format(label))
        created_at = task.get("createdAt")
        if isinstance(created_at, bool) or not isinstance(created_at, int):
            raise RoutineError("{} contains an invalid creation time".format(label))
        cron = task.get("cronExpression")
        fire_at = task.get("fireAt")
        if not (
            (isinstance(cron, str) and bool(cron))
            or (not isinstance(fire_at, bool) and isinstance(fire_at, (int, float)))
        ):
            raise RoutineError("{} contains an invalid schedule".format(label))
    for key in ("recordedSkips", "runRetries"):
        if key in value and not isinstance(value[key], dict):
            raise RoutineError("{} contains invalid {}".format(label, key))
    for key in ("sundayAliasBoundaryStamped", "dayFieldsOrBoundaryStamped"):
        if key in value and not isinstance(value[key], bool):
            raise RoutineError("{} contains invalid {}".format(label, key))
    return copy.deepcopy(value)


def _select_change(label: str, candidates: Sequence[Tuple[int, TargetKey, Any]]) -> Any:
    variants = {_value_key(value) for _mtime, _target, value in candidates}
    if len(variants) == 1:
        return _clone(candidates[0][2])
    newest = max(mtime for mtime, _target, _value in candidates)
    newest_candidates = [item for item in candidates if item[0] == newest]
    newest_variants = {
        _value_key(value) for _mtime, _target, value in newest_candidates
    }
    if len(newest_variants) != 1:
        raise RoutineError("{} has equally recent conflicting edits".format(label))
    selected = min(newest_candidates, key=lambda item: item[1])
    return _clone(selected[2])


def _merge_value(
    label: str,
    key: str,
    baseline: Any,
    samples: Sequence[RoutineSample],
    known_targets: Set[TargetKey],
    getter: Any,
) -> Any:
    candidates = []
    baseline_key = _value_key(baseline)
    for sample in samples:
        if not sample.exists:
            continue
        current = getter(sample.document, key)
        if current is MISSING and sample.target not in known_targets:
            continue
        if _value_key(current) == baseline_key:
            continue
        candidates.append((sample.mtime_ns, sample.target, current))
    if not candidates:
        return _clone(baseline)
    return _select_change(label, candidates)


def transform_routine_manifests(
    samples: Sequence[RoutineSample],
    *,
    snapshot: Optional[RoutineSnapshot] = None,
) -> RoutineTransform:
    """Three-way merge task records so additions, edits, and deletions propagate."""

    validated = tuple(
        RoutineSample(
            sample.target,
            sample.path,
            sample.mtime_ns,
            (
                None
                if sample.document is None
                else _validate_manifest(sample.document, "routine manifest")
            ),
        )
        for sample in samples
    )
    baseline = (
        {"scheduledTasks": []}
        if snapshot is None
        else _validate_manifest(snapshot.manifest, "routine snapshot")
    )
    known_targets = set(() if snapshot is None else snapshot.targets)
    baseline_tasks = _task_map(baseline)
    task_ids = set(baseline_tasks)
    for sample in validated:
        if sample.document is not None:
            task_ids.update(_task_map(sample.document))

    merged_tasks = []
    for task_id in sorted(task_ids):
        selected = _merge_value(
            "routine task",
            task_id,
            baseline_tasks.get(task_id, MISSING),
            validated,
            known_targets,
            lambda document, key: _task_map(document).get(key, MISSING),
        )
        if selected is not MISSING:
            merged_tasks.append(selected)

    metadata_keys = set(baseline) - {"scheduledTasks"}
    for sample in validated:
        if sample.document is not None:
            metadata_keys.update(set(sample.document) - {"scheduledTasks"})
    merged: Dict[str, Any] = {"scheduledTasks": merged_tasks}
    for key in sorted(metadata_keys):
        selected = _merge_value(
            "routine metadata {}".format(key),
            key,
            baseline.get(key, MISSING),
            validated,
            known_targets,
            lambda document, name: document.get(name, MISSING),
        )
        if selected is not MISSING:
            merged[key] = selected

    merged = _validate_manifest(merged, "merged routine manifest")
    targets = tuple(sorted(sample.target for sample in validated))
    return RoutineTransform(
        merged,
        RoutineSnapshot(targets, merged),
        len(merged_tasks),
    )


def _read_regular(path: Path) -> Tuple[bytes, int]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise RoutineError("routine manifest is not a readable regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RoutineError("routine manifest is not a regular file")
        if metadata.st_size > MAX_MANIFEST_BYTES:
            raise RoutineError("routine manifest is too large")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            content = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            raise RoutineError("routine manifest is too large")
        return content, metadata.st_mtime_ns
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_optional(path: Path) -> Optional[bytes]:
    if not os.path.lexists(str(path)):
        return None
    return _read_regular(path)[0]


def _decode_manifest(content: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RoutineError("routine manifest contains malformed JSON") from error
    return _validate_manifest(value, "routine manifest")


def _encode_manifest(document: Mapping[str, Any]) -> bytes:
    return _canonical(document) + b"\n"


def load_routine_snapshot(path: Path) -> Optional[RoutineSnapshot]:
    if not os.path.lexists(str(path)):
        return None
    content, _mtime = _read_regular(path)
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RoutineError("routine snapshot is malformed") from error
    if not isinstance(value, dict) or value.get("version") != SNAPSHOT_VERSION:
        raise RoutineError("routine snapshot has an unknown format")
    raw_targets = value.get("targets")
    if not isinstance(raw_targets, list):
        raise RoutineError("routine snapshot has invalid targets")
    targets = []
    for target in raw_targets:
        if (
            not isinstance(target, list)
            or len(target) != 3
            or any(not isinstance(part, str) or not part for part in target)
        ):
            raise RoutineError("routine snapshot has invalid targets")
        targets.append((target[0], target[1], target[2]))
    if len(targets) != len(set(targets)):
        raise RoutineError("routine snapshot has duplicate targets")
    manifest = _validate_manifest(value.get("manifest"), "routine snapshot")
    return RoutineSnapshot(tuple(sorted(targets)), manifest)


class RoutineSynchronizer:
    """Synchronize Code routines after Claude has fully terminated."""

    def __init__(self, config: Config) -> None:
        self.config = config

    def sync(self) -> RoutineReceipt:
        if not self.config.sync_code_routines:
            return RoutineReceipt("disabled", 0, 0, 0, 0, 0)
        ensure_private_directory(self.config.state_dir)
        try:
            lock = ExclusiveFileLock(
                self.config.state_dir / "transaction.lock", mode="auto"
            ).acquire()
        except LockUnavailableError as error:
            raise RoutineBusyError(
                "another synchronization is still finishing"
            ) from error
        try:
            targets = self._targets()
            self._recover_pending(targets)
            samples = tuple(self._sample(target) for target in targets)
            snapshot_path = self.config.state_dir / "code-routines-snapshot.json"
            transform = transform_routine_manifests(
                samples,
                snapshot=load_routine_snapshot(snapshot_path),
            )
            self._validate_task_files(transform.manifest)
            encoded = _encode_manifest(transform.manifest)
            current = {sample.path: _read_optional(sample.path) for sample in samples}
            replacements = {
                sample.path: encoded
                for sample in samples
                if sample.document is None or sample.document != transform.manifest
            }
            if replacements:
                self._commit(current, replacements, targets)
            if snapshot_path.is_symlink():
                raise RoutineError("routine snapshot path is unsafe")
            atomic_write_bytes(
                snapshot_path,
                (_canonical(transform.snapshot.as_dict()) + b"\n"),
            )
            os.chmod(str(snapshot_path), 0o600)
            return RoutineReceipt(
                "synced" if replacements else "noop",
                len(self.config.profiles),
                len(targets),
                sum(sample.exists for sample in samples),
                transform.task_count,
                len(replacements),
            )
        finally:
            lock.release()

    def _targets(self) -> Tuple[Any, ...]:
        if (
            len(self.config.profiles) > 1
            and not self.config.acknowledge_cross_profile_copy
        ):
            raise RoutineError("cross-profile routine copying is not acknowledged")
        discovery = SessionStore().discover(self.config)
        if discovery.invalid_replicas:
            raise RoutineError("Claude Code session storage is unsafe")
        approved = {
            (target.profile_name, target.account_id, target.workspace_id)
            for target in self.config.approved_targets
        }
        targets = tuple(
            target
            for target in discovery.targets
            if self.config.target_policy != "approved-only"
            or (target.profile_name, target.account_id, target.workspace_id) in approved
        )
        if not targets:
            raise RoutineError("no approved Claude Code routine targets were found")
        account_namespaces = {
            (target.profile_name, target.account_id) for target in targets
        }
        if (
            len(account_namespaces) > 1
            and not self.config.acknowledge_cross_account_copy
        ):
            raise RoutineError("cross-account routine copying is not acknowledged")
        return targets

    def _sample(self, target: Any) -> RoutineSample:
        path = target.path / "scheduled-tasks.json"
        key = (target.profile_name, target.account_id, target.workspace_id)
        if not os.path.lexists(str(path)):
            return RoutineSample(key, path, 0, None)
        content, mtime_ns = _read_regular(path)
        return RoutineSample(key, path, mtime_ns, _decode_manifest(content))

    def _validate_task_files(self, manifest: Mapping[str, Any]) -> None:
        for task in manifest["scheduledTasks"]:
            path = Path(task["filePath"]).expanduser()
            if (
                not path.is_absolute()
                or path.name != "SKILL.md"
                or path.parent.name != task["id"]
                or path.is_symlink()
                or not path.is_file()
            ):
                raise RoutineError("a routine definition file is missing or unsafe")

    def _snapshot_path(self) -> Path:
        return self.config.state_dir / "code-routines-snapshot.json"

    def _journal_root(self) -> Path:
        return self.config.state_dir / "routine-runs"

    def _allowed_paths(self, targets: Sequence[Any]) -> Set[str]:
        return {str(target.path / "scheduled-tasks.json") for target in targets}

    def _write_journal(self, path: Path, document: Mapping[str, Any]) -> None:
        atomic_write_bytes(path, _canonical(document) + b"\n")
        os.chmod(str(path), 0o600)

    def _journal_records(
        self,
        current: Mapping[Path, Optional[bytes]],
        replacements: Mapping[Path, bytes],
    ) -> Sequence[Dict[str, Any]]:
        records = []
        for path, after in sorted(replacements.items(), key=lambda item: str(item[0])):
            before = current[path]
            records.append(
                {
                    "path": str(path),
                    "before": (
                        None
                        if before is None
                        else base64.b64encode(before).decode("ascii")
                    ),
                    "after_sha256": hashlib.sha256(after).hexdigest(),
                }
            )
        return records

    def _commit(
        self,
        current: Mapping[Path, Optional[bytes]],
        replacements: Mapping[Path, bytes],
        targets: Sequence[Any],
    ) -> None:
        allowed = self._allowed_paths(targets)
        if any(str(path) not in allowed for path in replacements):
            raise RoutineError("routine write target is outside configured storage")
        root = ensure_private_directory(self._journal_root())
        journal_path = root / "{}.json".format(uuid.uuid4().hex)
        journal = {
            "version": JOURNAL_VERSION,
            "state": "PREPARED",
            "records": self._journal_records(current, replacements),
        }
        self._write_journal(journal_path, journal)
        try:
            for path, content in sorted(
                replacements.items(), key=lambda item: str(item[0])
            ):
                if _read_optional(path) != current[path]:
                    raise RoutineError(
                        "a routine manifest changed during synchronization"
                    )
                atomic_write_bytes(path, content)
                os.chmod(str(path), 0o600)
                if _read_optional(path) != content:
                    raise RoutineError("routine manifest write failed verification")
        except BaseException as write_error:
            try:
                self._restore(current, replacements)
                journal["state"] = "ROLLED_BACK"
                self._write_journal(journal_path, journal)
            except BaseException as recovery_error:
                journal["state"] = "RECOVERY_REQUIRED"
                self._write_journal(journal_path, journal)
                raise RoutineRecoveryError(
                    "routine recovery is required before another write"
                ) from recovery_error
            raise RoutineError("routine update was rolled back safely") from write_error
        journal["state"] = "COMMITTED"
        self._write_journal(journal_path, journal)
        self._prune_journals(root)

    def _restore(
        self,
        before: Mapping[Path, Optional[bytes]],
        replacements: Mapping[Path, bytes],
    ) -> None:
        for path, original in sorted(before.items(), key=lambda item: str(item[0])):
            if path not in replacements:
                continue
            current = _read_optional(path)
            after = replacements[path]
            if current not in (original, after):
                raise RoutineRecoveryError("routine manifest changed during recovery")
            if original is None:
                if current is not None:
                    durable_unlink(path)
            else:
                atomic_write_bytes(path, original)
                os.chmod(str(path), 0o600)
            if _read_optional(path) != original:
                raise RoutineRecoveryError("routine rollback failed verification")

    def _recover_pending(self, targets: Sequence[Any]) -> None:
        root = self._journal_root()
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            raise RoutineRecoveryError("routine recovery state is unsafe")
        allowed = self._allowed_paths(targets)
        for path in sorted(root.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise RoutineRecoveryError("routine recovery record is unsafe")
            try:
                content, _mtime_ns = _read_regular(path)
                document = json.loads(content.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, RoutineError) as error:
                raise RoutineRecoveryError(
                    "routine recovery state is malformed"
                ) from error
            if not isinstance(document, dict):
                raise RoutineRecoveryError("routine recovery state is malformed")
            if document.get("state") in ("COMMITTED", "ROLLED_BACK"):
                continue
            if document.get("version") != JOURNAL_VERSION or document.get(
                "state"
            ) not in ("PREPARED", "RECOVERY_REQUIRED"):
                raise RoutineRecoveryError("routine recovery state is malformed")
            records = document.get("records")
            if not isinstance(records, list) or not records:
                raise RoutineRecoveryError("routine recovery state is malformed")
            before: Dict[Path, Optional[bytes]] = {}
            after_digests: Dict[Path, str] = {}
            for record in records:
                if not isinstance(record, dict) or record.get("path") not in allowed:
                    raise RoutineRecoveryError("routine recovery state is malformed")
                destination = Path(record["path"])
                if destination in before:
                    raise RoutineRecoveryError("routine recovery state is malformed")
                raw_before = record.get("before")
                digest = record.get("after_sha256")
                try:
                    decoded = (
                        None
                        if raw_before is None
                        else base64.b64decode(raw_before, validate=True)
                    )
                except (TypeError, ValueError) as error:
                    raise RoutineRecoveryError(
                        "routine recovery state is malformed"
                    ) from error
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    raise RoutineRecoveryError("routine recovery state is malformed")
                before[destination] = decoded
                after_digests[destination] = digest
            current = {
                destination: _read_optional(destination) for destination in before
            }
            if all(
                current[destination] == value for destination, value in before.items()
            ):
                document["state"] = "ROLLED_BACK"
            elif all(
                current[destination] is not None
                and hashlib.sha256(current[destination]).hexdigest()
                == after_digests[destination]
                for destination in before
            ):
                document["state"] = "COMMITTED"
            else:
                replacements = {}
                for destination, value in current.items():
                    if value == before[destination]:
                        continue
                    if (
                        value is None
                        or hashlib.sha256(value).hexdigest()
                        != after_digests[destination]
                    ):
                        document["state"] = "RECOVERY_REQUIRED"
                        self._write_journal(path, document)
                        raise RoutineRecoveryError(
                            "routine recovery found an independently changed manifest"
                        )
                    replacements[destination] = value
                try:
                    self._restore(before, replacements)
                except BaseException as error:
                    document["state"] = "RECOVERY_REQUIRED"
                    self._write_journal(path, document)
                    raise RoutineRecoveryError(
                        "routine recovery is required"
                    ) from error
                document["state"] = "ROLLED_BACK"
            self._write_journal(path, document)

    def _prune_journals(self, root: Path) -> None:
        paths = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        for path in paths[: -self.config.retention]:
            durable_unlink(path)
        fsync_directory(root)
