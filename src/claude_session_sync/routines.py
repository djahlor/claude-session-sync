"""Synchronization for Claude Desktop Code routine manifests."""

from __future__ import annotations

import copy
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Set, Tuple

from .config import Config
from . import strict_json as json
from .filesystem import (
    atomic_write_bytes,
    durable_unlink,
    ensure_private_directory,
)
from .locking import ExclusiveFileLock, LockUnavailableError
from .record_journal import (
    RecordJournal,
    RecordJournalError,
    RecordRecoveryError,
)
from .processes import managed_processes
from .store import SessionStore


SNAPSHOT_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_TASKS = 10_000
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
MISSING = object()
TargetKey = Tuple[str, str, str]
# Observed in Claude Desktop's CCDScheduledTasks schema. All other fields,
# including run history and future fields, belong to the destination account
# and never travel.
DEFINITION_FIELDS = frozenset(
    (
        "id",
        "displayName",
        "cronExpression",
        "fireAt",
        "enabled",
        "filePath",
        "model",
        "createdAt",
        "cwd",
        "useWorktree",
        "sourceBranch",
        "disableJitter",
    )
)
# A routine without a permissionMode falls back to its folder's default, which
# is usually manual. These fields travel too, but each merges on its own, so a
# copy that never chose a mode cannot erase another account's choice.
PERMISSION_FIELDS = ("permissionMode", "approvedPermissions")
SYNCED_FIELDS = DEFINITION_FIELDS.union(PERMISSION_FIELDS)


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
    content: Optional[bytes] = None

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
    documents: Mapping[TargetKey, Mapping[str, Any]]


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


def _definitions(document: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "scheduledTasks": [
            {
                key: copy.deepcopy(value)
                for key, value in task.items()
                if key in SYNCED_FIELDS
            }
            for task in document["scheduledTasks"]
        ]
    }


def _destination_manifest(
    definitions: Mapping[str, Any], original: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    document = copy.deepcopy(dict(original or {"scheduledTasks": []}))
    originals = _task_map(document)
    tasks = []
    for definition in definitions["scheduledTasks"]:
        local = {
            key: copy.deepcopy(value)
            for key, value in originals.get(definition["id"], {}).items()
            if key not in SYNCED_FIELDS
        }
        local.update(copy.deepcopy(definition))
        tasks.append(local)
    document["scheduledTasks"] = tasks
    return document


def _valid_approval(rule: Any) -> bool:
    # Mirrors Claude Desktop's own check for one saved approval.
    return (
        isinstance(rule, dict)
        and isinstance(rule.get("toolName"), str)
        and isinstance(rule.get("ruleContent", ""), str)
        and all(
            isinstance(rule.get(key, False), bool) for key in ("expired", "timeLimited")
        )
    )


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
        for key in ("displayName", "model", "cwd", "sourceBranch", "permissionMode"):
            if key in task and not isinstance(task[key], str):
                raise RoutineError("{} contains an invalid {}".format(label, key))
        for key in ("useWorktree", "disableJitter"):
            if key in task and not isinstance(task[key], bool):
                raise RoutineError("{} contains an invalid {}".format(label, key))
        approved = task.get("approvedPermissions", [])
        if not isinstance(approved, list) or not all(
            _valid_approval(rule) for rule in approved
        ):
            raise RoutineError("{} contains invalid approvedPermissions".format(label))
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
    baseline: Any,
    values: Sequence[Tuple[int, TargetKey, Any]],
    known_targets: Set[TargetKey],
) -> Any:
    baseline_key = _value_key(baseline)
    candidates = [
        (mtime, target, value)
        for mtime, target, value in values
        if (value is not MISSING or target in known_targets)
        and _value_key(value) != baseline_key
    ]
    if not candidates:
        return _clone(baseline)
    return _select_change(label, candidates)


def _field(task: Any, field: str) -> Any:
    return MISSING if task is MISSING else task.get(field, MISSING)


def _without_permissions(task: Any) -> Any:
    if task is MISSING:
        return MISSING
    return {key: value for key, value in task.items() if key not in PERMISSION_FIELDS}


def _merge_task(
    key: str,
    baseline: Any,
    samples: Sequence[RoutineSample],
    known_targets: Set[TargetKey],
    task_maps: Mapping[TargetKey, Mapping[str, Mapping[str, Any]]],
) -> Any:
    tasks = [
        (sample.mtime_ns, sample.target, task_maps[sample.target].get(key, MISSING))
        for sample in samples
        if sample.exists
    ]
    merged = _merge_value(
        "routine task",
        _without_permissions(baseline),
        [(mtime, target, _without_permissions(task)) for mtime, target, task in tasks],
        known_targets,
    )
    if merged is MISSING:
        return MISSING
    for field in PERMISSION_FIELDS:
        value = _merge_value(
            "routine {}".format(field),
            _field(baseline, field),
            [
                (mtime, target, _field(task, field))
                for mtime, target, task in tasks
                if task is not MISSING
            ],
            known_targets,
        )
        if value is not MISSING:
            merged[field] = value
    return merged


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
                else _definitions(
                    _validate_manifest(sample.document, "routine manifest")
                )
            ),
            sample.content,
        )
        for sample in samples
    )
    baseline = (
        {"scheduledTasks": []}
        if snapshot is None
        else _definitions(_validate_manifest(snapshot.manifest, "routine snapshot"))
    )
    known_targets = set(() if snapshot is None else snapshot.targets)
    baseline_tasks = _task_map(baseline)
    task_maps = {
        sample.target: _task_map(sample.document)
        for sample in validated
        if sample.document is not None
    }
    task_ids = set(baseline_tasks)
    for tasks in task_maps.values():
        task_ids.update(tasks)

    merged_tasks = []
    for task_id in sorted(task_ids):
        selected = _merge_task(
            task_id,
            baseline_tasks.get(task_id, MISSING),
            validated,
            known_targets,
            task_maps,
        )
        if selected is not MISSING:
            merged_tasks.append(selected)

    merged: Dict[str, Any] = {"scheduledTasks": merged_tasks}
    merged = _validate_manifest(merged, "merged routine manifest")
    targets = tuple(sorted(sample.target for sample in validated))
    return RoutineTransform(
        merged,
        RoutineSnapshot(targets, merged),
        len(merged_tasks),
        {
            sample.target: _destination_manifest(merged, sample.document)
            for sample in samples
        },
    )


def _read_regular(path: Path) -> Tuple[bytes, int]:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
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
            after = os.fstat(stream.fileno())
        if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise RoutineError("routine manifest changed while being read")
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
    return _decode_snapshot(_read_optional(path))


def _decode_snapshot(content: Optional[bytes]) -> Optional[RoutineSnapshot]:
    if content is None:
        return None
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

    def __init__(
        self,
        config: Config,
        *,
        task_root: Optional[Path] = None,
        process_probe: Callable[..., Any] = managed_processes,
    ) -> None:
        self.config = config
        self.task_root = task_root or Path.home() / ".claude" / "scheduled-tasks"
        self.process_probe = process_probe

    def probe(self) -> Dict[str, Any]:
        """Validate current source data without creating state or restoring files."""

        if not self.config.sync_code_routines:
            return {
                "state": "disabled",
                "target_count": 0,
                "manifest_count": 0,
                "task_count": 0,
            }
        self._assert_stopped()
        targets = self._targets()
        samples = tuple(self._sample(target) for target in targets)
        transform = transform_routine_manifests(
            samples, snapshot=load_routine_snapshot(self._snapshot_path())
        )
        self._validate_task_files(transform.manifest)
        return {
            "state": "compatible",
            "target_count": len(targets),
            "manifest_count": sum(sample.exists for sample in samples),
            "task_count": transform.task_count,
        }

    def _assert_stopped(self) -> None:
        if self.process_probe(
            self.config.profiles, executable=self.config.claude_executable, timeout=5
        ):
            raise RoutineBusyError("Claude must be fully quit before syncing routines")

    def sync(self) -> RoutineReceipt:
        if not self.config.sync_code_routines:
            return RoutineReceipt("disabled", 0, 0, 0, 0, 0)
        self._assert_stopped()
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
            snapshot_path = self._snapshot_path()
            snapshot_before = _read_optional(snapshot_path)
            transform = transform_routine_manifests(
                samples,
                snapshot=_decode_snapshot(snapshot_before),
            )
            self._validate_task_files(transform.manifest)
            current = {sample.path: sample.content for sample in samples}
            replacements = {
                sample.path: _encode_manifest(transform.documents[sample.target])
                for sample in samples
                if sample.document != transform.documents[sample.target]
            }
            manifest_writes = len(replacements)
            current[snapshot_path] = snapshot_before
            snapshot_after = _canonical(transform.snapshot.as_dict()) + b"\n"
            if snapshot_after != snapshot_before:
                replacements[snapshot_path] = snapshot_after
            if any(_read_optional(path) != before for path, before in current.items()):
                raise RoutineError("a routine manifest changed during synchronization")
            if replacements:
                self._commit(current, replacements, targets)
            self._assert_stopped()
            expected = dict(current)
            expected.update(replacements)
            if any(_read_optional(path) != after for path, after in expected.items()):
                raise RoutineError("routine data changed after synchronization")
            return RoutineReceipt(
                "synced" if manifest_writes else "noop",
                len(self.config.profiles),
                len(targets),
                sum(sample.exists for sample in samples),
                transform.task_count,
                manifest_writes,
            )
        finally:
            lock.release()

    def _targets(self) -> Tuple[Any, ...]:
        if (
            len(self.config.profiles) > 1
            and not self.config.acknowledge_cross_profile_copy
        ):
            raise RoutineError("cross-profile routine copying is not acknowledged")
        discovery = SessionStore().discover_targets(self.config)
        if discovery.invalid_replicas:
            raise RoutineError("Claude Code session storage is unsafe")
        from .enrollment import selected_targets

        targets = selected_targets(self.config, discovery.targets)
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
        return RoutineSample(key, path, mtime_ns, _decode_manifest(content), content)

    def _validate_task_files(self, manifest: Mapping[str, Any]) -> None:
        for task in manifest["scheduledTasks"]:
            path = Path(task["filePath"])
            expected = self.task_root / task["id"] / "SKILL.md"
            if (
                not path.is_absolute()
                or path != expected
                or any(parent.is_symlink() for parent in (path, *path.parents))
                or not path.is_file()
            ):
                raise RoutineError("a routine definition file is missing or unsafe")
            _read_regular(path)

    def _snapshot_path(self) -> Path:
        return self.config.state_dir / "code-routines-snapshot.json"

    def _journal_root(self) -> Path:
        return self.config.state_dir / "routine-runs"

    def _allowed_paths(self, targets: Sequence[Any]) -> Set[str]:
        return {str(target.path / "scheduled-tasks.json") for target in targets} | {
            str(self._snapshot_path())
        }

    def _journal(self, targets: Sequence[Any]) -> RecordJournal:
        allowed = self._allowed_paths(targets)

        def validate(target: str) -> None:
            if target not in allowed:
                raise RoutineRecoveryError(
                    "routine target is outside configured storage"
                )
            path = Path(target)
            if path.is_symlink() or not path.parent.is_dir():
                raise RoutineRecoveryError("routine target is unsafe")
            if path == self._snapshot_path():
                if path.parent.is_symlink():
                    raise RoutineRecoveryError("routine snapshot target is unsafe")
                return
            # The known target hierarchy must remain real directories.
            if any(
                parent.is_symlink()
                for parent in (
                    path.parent,
                    path.parent.parent,
                    path.parent.parent.parent,
                )
            ):
                raise RoutineRecoveryError("routine target includes an unsafe symlink")

        def write(target: str, value: bytes) -> None:
            self._assert_stopped()
            atomic_write_bytes(Path(target), value)
            os.chmod(target, 0o600)

        def delete(target: str) -> None:
            self._assert_stopped()
            durable_unlink(Path(target))

        return RecordJournal(
            self._journal_root(),
            read=lambda target: _read_optional(Path(target)),
            write=write,
            delete=delete,
            validate=validate,
            before_mutation=self._assert_stopped,
            retention=self.config.retention,
        )

    def _commit(
        self,
        current: Mapping[Path, Optional[bytes]],
        replacements: Mapping[Path, bytes],
        targets: Sequence[Any],
    ) -> None:
        try:
            after = dict(current)
            after.update(replacements)
            self._journal(targets).commit(
                {str(path): value for path, value in current.items()},
                {str(path): value for path, value in after.items()},
            )
        except RecordRecoveryError as error:
            raise RoutineRecoveryError(str(error)) from error
        except RecordJournalError as error:
            raise RoutineError(str(error)) from error

    def _recover_pending(self, targets: Sequence[Any]) -> None:
        self._assert_stopped()
        try:
            self._journal(targets).recover()
        except RecordJournalError as error:
            raise RoutineRecoveryError(str(error)) from error
