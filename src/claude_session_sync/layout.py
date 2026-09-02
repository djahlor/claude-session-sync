"""Selective synchronization for Claude Desktop sidebar layout records."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from .config import Config
from .filesystem import atomic_write_bytes, ensure_private_directory
from .locking import ExclusiveFileLock, LockUnavailableError


ORIGIN_PREFIX = b"_https://claude.ai\x00\x01"
GROUP_SCOPES_KEY = ORIGIN_PREFIX + b"LSS-persisted.dframe-group-scopes"
LOCAL_SLICE_KEY = ORIGIN_PREFIX + b"LSS-persisted.dframe-local-slice"
DFRAME_STORE_KEY = ORIGIN_PREFIX + b"dframe-store"
LAYOUT_KEYS = (GROUP_SCOPES_KEY, LOCAL_SLICE_KEY, DFRAME_STORE_KEY)
MAX_RECORD_BYTES = 32 * 1024 * 1024
SNAPSHOT_VERSION = 1


class LayoutError(RuntimeError):
    """Raised when sidebar data cannot be changed without guessing."""


class LayoutBusyError(LayoutError):
    """Raised when another synchronization writer owns the lock."""


class LayoutRecoveryError(LayoutError):
    """Raised when a failed write cannot be restored automatically."""


@dataclass(frozen=True)
class LayoutSnapshot:
    groups: Tuple[str, ...]
    assignments: Mapping[str, str]
    pinned_order: Tuple[str, ...]
    home_projects_pinned_order: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": SNAPSHOT_VERSION,
            "groups": list(self.groups),
            "assignments": dict(sorted(self.assignments.items())),
            "pinned_order": list(self.pinned_order),
            "home_projects_pinned_order": list(
                self.home_projects_pinned_order
            ),
        }


@dataclass(frozen=True)
class LayoutTransform:
    records: Mapping[bytes, bytes]
    snapshot: LayoutSnapshot
    group_count: int
    assignment_count: int
    pin_count: int
    ambiguous_assignments: int


@dataclass(frozen=True)
class LayoutReceipt:
    state: str
    profile_count: int
    record_count: int
    group_count: int
    assignment_count: int
    pin_count: int
    ambiguous_assignments: int


def _string_list(value: Any, label: str) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise LayoutError("{} has an unknown shape".format(label))
    if len(value) != len(set(value)):
        raise LayoutError("{} contains duplicates".format(label))
    return list(value)


def _string_map(value: Any, label: str) -> Dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise LayoutError("{} has an unknown shape".format(label))
    return dict(value)


def _decode_record(value: bytes, label: str) -> Dict[str, Any]:
    if not value or len(value) > MAX_RECORD_BYTES or value[:1] != b"\x01":
        raise LayoutError("{} has an unknown encoding".format(label))
    try:
        decoded = json.loads(value[1:].decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LayoutError("{} contains malformed JSON".format(label)) from error
    if not isinstance(decoded, dict):
        raise LayoutError("{} is not a JSON object".format(label))
    return decoded


def _encode_record(value: Mapping[str, Any]) -> bytes:
    return b"\x01" + json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _ordered_group_pairs(scope: Any) -> List[Tuple[str, str]]:
    if not isinstance(scope, dict):
        raise LayoutError("custom group scope has an unknown shape")
    groups = scope.get("groups")
    if not isinstance(groups, list):
        raise LayoutError("custom group list has an unknown shape")
    pairs = []
    for group in groups:
        if not isinstance(group, dict):
            raise LayoutError("custom group has an unknown shape")
        group_id = group.get("id")
        name = group.get("name")
        if not isinstance(group_id, str) or not group_id:
            raise LayoutError("custom group id has an unknown shape")
        if not isinstance(name, str) or not name:
            raise LayoutError("custom group name has an unknown shape")
        pairs.append((group_id, name))
    ids = [group_id for group_id, _name in pairs]
    names = [name for _group_id, name in pairs]
    if len(ids) != len(set(ids)) or len(names) != len(set(names)):
        raise LayoutError("custom groups contain duplicate ids or names")
    assignments = _string_map(
        scope.get("assignments", {}), "custom group assignments"
    )
    if set(assignments.values()) - set(ids):
        raise LayoutError("custom group assignment references an unknown group")
    order = scope.get("order", {})
    if not isinstance(order, dict) or any(
        not isinstance(group_id, str) for group_id in order
    ):
        raise LayoutError("custom group order has an unknown shape")
    if set(order) - set(ids):
        raise LayoutError("custom group order references an unknown group")
    for group_id, sessions in order.items():
        ordered_sessions = _string_list(sessions, "custom group session order")
        if any(assignments.get(session) != group_id for session in ordered_sessions):
            raise LayoutError("custom group order disagrees with assignments")
    return pairs


def _scope_assignments_by_name(scope: Mapping[str, Any]) -> Dict[str, str]:
    names = dict(_ordered_group_pairs(scope))
    assignments = _string_map(
        scope.get("assignments", {}), "custom group assignments"
    )
    return {session: names[group_id] for session, group_id in assignments.items()}


def _validated_scopes(value: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, dict):
        raise LayoutError("custom group scopes have an unknown shape")
    scopes = {}
    for scope_key, scope in value.items():
        if not isinstance(scope_key, str) or "/" not in scope_key:
            raise LayoutError("custom group scope key has an unknown shape")
        _ordered_group_pairs(scope)
        scopes[scope_key] = copy.deepcopy(scope)
    return scopes


def _merge_scope_pair(
    preferred: Mapping[str, Any], fallback: Mapping[str, Any]
) -> Dict[str, Any]:
    preferred_pairs = _ordered_group_pairs(preferred)
    fallback_pairs = _ordered_group_pairs(fallback)
    ids_by_name = {}  # type: Dict[str, str]
    names_by_id = {}  # type: Dict[str, str]
    group_names = []
    for group_id, name in preferred_pairs + fallback_pairs:
        if name in ids_by_name and ids_by_name[name] != group_id:
            raise LayoutError("custom group records contain conflicting ids")
        if group_id in names_by_id and names_by_id[group_id] != name:
            raise LayoutError("custom group records contain conflicting names")
        ids_by_name[name] = group_id
        names_by_id[group_id] = name
        if name not in group_names:
            group_names.append(name)

    preferred_assignments = _scope_assignments_by_name(preferred)
    fallback_assignments = _scope_assignments_by_name(fallback)
    for session in set(preferred_assignments) & set(fallback_assignments):
        if preferred_assignments[session] != fallback_assignments[session]:
            raise LayoutError("custom group records contain conflicting assignments")
    assignments_by_name = dict(preferred_assignments)
    for session, name in fallback_assignments.items():
        assignments_by_name.setdefault(session, name)

    merged = copy.deepcopy(preferred)
    for key, value in fallback.items():
        if key in {"groups", "assignments", "order"}:
            continue
        if key in merged and merged[key] != value:
            raise LayoutError("custom group scope metadata disagrees")
        merged.setdefault(key, copy.deepcopy(value))

    merged["groups"] = [
        {"id": ids_by_name[name], "name": name} for name in group_names
    ]
    merged["assignments"] = {
        session: ids_by_name[name] for session, name in assignments_by_name.items()
    }
    preferred_order = preferred.get("order", {})
    fallback_order = fallback.get("order", {})
    merged_order = {}
    for name in group_names:
        group_id = ids_by_name[name]
        ordered = _stable_union(
            (
                preferred_order.get(group_id, []),
                fallback_order.get(group_id, []),
            )
        )
        merged_order[group_id] = [
            session
            for session in ordered
            if assignments_by_name.get(session) == name
        ]
    merged["order"] = merged_order
    _ordered_group_pairs(merged)
    return merged


def _merge_sidebar_scopes(
    preferred: Mapping[str, Dict[str, Any]],
    fallback: Mapping[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    merged = {}
    for scope_key in sorted(set(preferred) | set(fallback)):
        if scope_key in preferred and scope_key in fallback:
            merged[scope_key] = _merge_scope_pair(
                preferred[scope_key], fallback[scope_key]
            )
        elif scope_key in preferred:
            merged[scope_key] = copy.deepcopy(preferred[scope_key])
        else:
            merged[scope_key] = copy.deepcopy(fallback[scope_key])
    return merged


def _stable_union(sequences: Iterable[Sequence[str]]) -> List[str]:
    output = []
    seen = set()
    for sequence in sequences:
        for item in sequence:
            if item not in seen:
                output.append(item)
                seen.add(item)
    return output


def _load_snapshot_document(document: Any) -> LayoutSnapshot:
    if not isinstance(document, dict) or document.get("version") != SNAPSHOT_VERSION:
        raise LayoutError("sidebar snapshot has an unknown version")
    return LayoutSnapshot(
        groups=tuple(_string_list(document.get("groups"), "snapshot groups")),
        assignments=_string_map(
            document.get("assignments"), "snapshot assignments"
        ),
        pinned_order=tuple(
            _string_list(document.get("pinned_order"), "snapshot pins")
        ),
        home_projects_pinned_order=tuple(
            _string_list(
                document.get("home_projects_pinned_order"),
                "snapshot project pins",
            )
        ),
    )


def load_snapshot(path: Path) -> Optional[LayoutSnapshot]:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise LayoutError("sidebar snapshot is not a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LayoutError("sidebar snapshot is malformed") from error
    return _load_snapshot_document(document)


def transform_layout_records(
    records: Mapping[bytes, bytes],
    target_sessions: Mapping[str, Set[str]],
    *,
    snapshot: Optional[LayoutSnapshot] = None,
    timestamp_ms: Optional[int] = None,
) -> LayoutTransform:
    """Return exact allowlisted record replacements for one Claude data root."""

    if set(records) != set(LAYOUT_KEYS):
        raise LayoutError("required sidebar records are missing")
    group_record = _decode_record(records[GROUP_SCOPES_KEY], "group scopes record")
    local_record = _decode_record(records[LOCAL_SLICE_KEY], "local slice record")
    store_record = _decode_record(records[DFRAME_STORE_KEY], "dframe store record")
    if not isinstance(group_record.get("value"), dict):
        raise LayoutError("group scopes record has an unknown shape")
    if not isinstance(local_record.get("value"), dict):
        raise LayoutError("local slice record has an unknown shape")
    if not isinstance(store_record.get("state"), dict):
        raise LayoutError("dframe store record has an unknown shape")

    store_state = store_record["state"]
    store_scopes = _validated_scopes(store_state.get("customGroupsByScope", {}))
    persisted_scopes = _validated_scopes(group_record["value"])
    store_scopes = _merge_sidebar_scopes(store_scopes, persisted_scopes)

    store_pins = _string_list(store_state.get("pinnedOrder", []), "stored pins")
    local_pins = _string_list(
        local_record["value"].get("pinnedOrder", []), "local pins"
    )
    store_project_pins = _string_list(
        store_state.get("homeProjectsPinnedOrder", []), "stored project pins"
    )
    local_project_pins = _string_list(
        local_record["value"].get("homeProjectsPinnedOrder", []),
        "local project pins",
    )

    active_scope = store_state.get("lastSidebarScopeKey")
    if active_scope is not None and not isinstance(active_scope, str):
        raise LayoutError("last sidebar scope has an unknown shape")
    ordered_scope_keys = []
    if active_scope in store_scopes:
        ordered_scope_keys.append(active_scope)
    ordered_scope_keys.extend(
        scope_key for scope_key in sorted(store_scopes) if scope_key != active_scope
    )

    active_has_groups = bool(
        active_scope in store_scopes
        and _ordered_group_pairs(store_scopes[active_scope])
    )
    current_groups = _stable_union(
        [
            [name for _group_id, name in _ordered_group_pairs(store_scopes[key])]
            for key in ordered_scope_keys
        ]
    )
    if snapshot is not None:
        active_groups = []
        if active_has_groups:
            active_groups = [
                name
                for _group_id, name in _ordered_group_pairs(
                    store_scopes[active_scope]
                )
            ]
        groups = _stable_union((active_groups, snapshot.groups, current_groups))
        assignments = dict(snapshot.assignments)
        if active_has_groups:
            assignments.update(
                _scope_assignments_by_name(store_scopes[active_scope])
            )
    else:
        groups = current_groups
        assignments = {}
        candidate_assignments = {}  # type: Dict[str, Set[str]]
        for scope in store_scopes.values():
            for session, name in _scope_assignments_by_name(scope).items():
                candidate_assignments.setdefault(session, set()).add(name)
        assignments = {
            session: next(iter(names))
            for session, names in candidate_assignments.items()
            if len(names) == 1
        }

    candidate_assignments = {}  # type: Dict[str, Set[str]]
    for scope in store_scopes.values():
        for session, name in _scope_assignments_by_name(scope).items():
            candidate_assignments.setdefault(session, set()).add(name)
    ambiguous_sessions = {
        session for session, names in candidate_assignments.items() if len(names) > 1
    }
    for session, names in candidate_assignments.items():
        if session not in assignments and len(names) == 1:
            assignments[session] = next(iter(names))
    group_names = set(groups)
    assignments = {
        session: name
        for session, name in assignments.items()
        if name in group_names and session not in ambiguous_sessions
    }

    current_pins = _stable_union((store_pins, local_pins))
    current_project_pins = _stable_union((store_project_pins, local_project_pins))
    if not current_pins and snapshot is not None:
        current_pins = list(snapshot.pinned_order)
    if not current_project_pins and snapshot is not None:
        current_project_pins = list(snapshot.home_projects_pinned_order)

    updated_scopes = copy.deepcopy(store_scopes)
    for scope_key, sessions in sorted(target_sessions.items()):
        existing = updated_scopes.get(
            scope_key, {"groups": [], "assignments": {}, "order": {}}
        )
        pairs = _ordered_group_pairs(existing)
        ids_by_name = {name: group_id for group_id, name in pairs}
        for name in groups:
            ids_by_name.setdefault(
                name,
                "cg-{}".format(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "claude-session-sync:{}:{}".format(scope_key, name),
                    )
                ),
            )
        replacement = copy.deepcopy(existing)
        replacement["groups"] = [
            {"id": ids_by_name[name], "name": name} for name in groups
        ]
        existing_by_name = _scope_assignments_by_name(existing)
        target_assignments = {}
        for session in sorted(sessions):
            name = assignments.get(session)
            if name is None and session in ambiguous_sessions:
                name = existing_by_name.get(session)
            if name in group_names:
                target_assignments[session] = ids_by_name[name]
        replacement["assignments"] = target_assignments
        existing_order = existing.get("order", {})
        replacement_order = {}
        for name in groups:
            group_id = ids_by_name[name]
            previous_id = next(
                (item_id for item_id, item_name in pairs if item_name == name), None
            )
            previous = list(existing_order.get(previous_id, []))
            assigned = [
                session
                for session, assigned_group in target_assignments.items()
                if assigned_group == group_id
            ]
            ordered = [session for session in previous if session in set(assigned)]
            ordered.extend(session for session in assigned if session not in set(ordered))
            replacement_order[group_id] = ordered
        replacement["order"] = replacement_order
        updated_scopes[scope_key] = replacement

    updated_store = copy.deepcopy(store_record)
    updated_group = copy.deepcopy(group_record)
    updated_local = copy.deepcopy(local_record)
    updated_store["state"]["customGroupsByScope"] = updated_scopes
    updated_group["value"] = updated_scopes
    updated_store["state"]["pinnedOrder"] = current_pins
    updated_local["value"]["pinnedOrder"] = current_pins
    updated_store["state"]["homeProjectsPinnedOrder"] = current_project_pins
    updated_local["value"]["homeProjectsPinnedOrder"] = current_project_pins
    now = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if updated_group != group_record:
        updated_group["timestamp"] = now
    if updated_local != local_record:
        updated_local["timestamp"] = now

    updated_records = {
        GROUP_SCOPES_KEY: _encode_record(updated_group),
        LOCAL_SLICE_KEY: _encode_record(updated_local),
        DFRAME_STORE_KEY: _encode_record(updated_store),
    }
    next_snapshot = LayoutSnapshot(
        tuple(groups),
        dict(assignments),
        tuple(current_pins),
        tuple(current_project_pins),
    )
    return LayoutTransform(
        records=updated_records,
        snapshot=next_snapshot,
        group_count=len(groups),
        assignment_count=len(assignments),
        pin_count=len(current_pins),
        ambiguous_assignments=len(ambiguous_sessions),
    )


class LevelDatabase:
    """Narrow process adapter for the bundled LevelDB helper."""

    def __init__(self, helper: Path, database: Path) -> None:
        self.helper = Path(helper)
        self.database = Path(database)

    def get(self, key: bytes) -> bytes:
        result = subprocess.run(
            [str(self.helper), "get", str(self.database), key.hex()],
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise LayoutError("Claude sidebar record could not be read safely")
        encoded = result.stdout.strip()
        try:
            return bytes.fromhex(encoded)
        except ValueError as error:
            raise LayoutError("sidebar helper returned malformed data") from error

    def write(self, records: Mapping[bytes, bytes], state_dir: Path) -> None:
        ensure_private_directory(state_dir)
        descriptor, name = tempfile.mkstemp(prefix=".layout-ops-", dir=str(state_dir))
        path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                for key, value in sorted(records.items()):
                    handle.write("P\t{}\t{}\n".format(key.hex(), value.hex()))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, 0o600)
            result = subprocess.run(
                [str(self.helper), "batch", str(self.database), str(path)],
                check=False,
                text=True,
                capture_output=True,
                timeout=20,
            )
            if result.returncode != 0:
                raise LayoutError("Claude sidebar records could not be written safely")
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


class LayoutSynchronizer:
    """Synchronize allowlisted sidebar records after Claude has terminated."""

    def __init__(self, config: Config, *, helper: Optional[Path] = None) -> None:
        self.config = config
        self.helper = Path(helper or config.state_dir.parent / "bin" / "layoutdb")

    def sync(self) -> LayoutReceipt:
        if not self.config.sync_sidebar_layout:
            return LayoutReceipt("disabled", 0, 0, 0, 0, 0, 0)
        if not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            raise LayoutError("sidebar helper is not installed")
        ensure_private_directory(self.config.state_dir)
        try:
            lock = ExclusiveFileLock(
                self.config.state_dir / "transaction.lock", mode="auto"
            ).acquire()
        except LockUnavailableError as error:
            raise LayoutBusyError("another synchronization is still finishing") from error
        try:
            self._recover_pending()
            totals = [0, 0, 0, 0, 0]
            changed_profiles = 0
            for index, profile in enumerate(self.config.profiles):
                database_path = profile.data_root / "Local Storage" / "leveldb"
                if database_path.is_symlink() or not database_path.is_dir():
                    raise LayoutError("Claude Local Storage has an unknown layout")
                target_sessions = self._target_sessions(profile.name, profile.data_root)
                database = LevelDatabase(self.helper, database_path)
                current = {key: database.get(key) for key in LAYOUT_KEYS}
                snapshot_path = self.config.state_dir / "sidebar-layout-{}.json".format(
                    index
                )
                transformed = transform_layout_records(
                    current,
                    target_sessions,
                    snapshot=load_snapshot(snapshot_path),
                )
                replacements = {
                    key: value
                    for key, value in transformed.records.items()
                    if current[key] != value
                }
                if replacements:
                    self._commit(database, current, replacements)
                    changed_profiles += 1
                atomic_write_bytes(
                    snapshot_path,
                    (
                        json.dumps(
                            transformed.snapshot.as_dict(), indent=2, sort_keys=True
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                os.chmod(snapshot_path, 0o600)
                totals[0] += len(replacements)
                totals[1] = max(totals[1], transformed.group_count)
                totals[2] = max(totals[2], transformed.assignment_count)
                totals[3] = max(totals[3], transformed.pin_count)
                totals[4] += transformed.ambiguous_assignments
            return LayoutReceipt(
                "synced" if changed_profiles else "noop",
                len(self.config.profiles),
                totals[0],
                totals[1],
                totals[2],
                totals[3],
                totals[4],
            )
        finally:
            lock.release()

    def _target_sessions(self, profile_name: str, data_root: Path) -> Dict[str, Set[str]]:
        approved = {
            (target.account_id, target.workspace_id)
            for target in self.config.approved_targets
            if target.profile_name == profile_name
        }
        sessions_root = data_root / "claude-code-sessions"
        if sessions_root.is_symlink() or not sessions_root.is_dir():
            raise LayoutError("Claude session storage has an unknown layout")
        targets = {}
        for account in sorted(sessions_root.iterdir()):
            if account.is_symlink() or not account.is_dir():
                continue
            for workspace in sorted(account.iterdir()):
                if workspace.is_symlink() or not workspace.is_dir():
                    continue
                if (
                    self.config.target_policy == "approved-only"
                    and (account.name, workspace.name) not in approved
                ):
                    continue
                sessions = set()
                for replica in workspace.iterdir():
                    if replica.is_symlink() or not replica.is_file():
                        continue
                    if replica.name.startswith("local_") and replica.name.endswith(
                        ".json"
                    ):
                        sessions.add("code:{}".format(replica.stem))
                targets["{}/{}".format(account.name, workspace.name)] = sessions
        if not targets:
            raise LayoutError("no approved Claude sidebar scopes were found")
        return targets

    def _journal_root(self) -> Path:
        return self.config.state_dir / "layout-runs"

    def _recover_pending(self) -> None:
        root = self._journal_root()
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            raise LayoutRecoveryError("sidebar recovery state is unsafe")
        allowed_databases = {
            str(profile.data_root / "Local Storage" / "leveldb")
            for profile in self.config.profiles
        }
        for path in root.glob("*.json"):
            if path.is_symlink() or not path.is_file():
                raise LayoutRecoveryError("sidebar recovery record is unsafe")
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise LayoutRecoveryError("sidebar recovery state is malformed") from error
            state = document.get("state")
            if state in ("COMMITTED", "ROLLED_BACK"):
                continue
            database_path = document.get("database")
            raw_records = document.get("records")
            if (
                document.get("version") != 1
                or database_path not in allowed_databases
                or not isinstance(raw_records, list)
                or not raw_records
            ):
                raise LayoutRecoveryError("sidebar recovery state is malformed")
            before = {}
            after_digests = {}
            for record in raw_records:
                if not isinstance(record, dict):
                    raise LayoutRecoveryError("sidebar recovery state is malformed")
                try:
                    key = bytes.fromhex(record["key"])
                    value = bytes.fromhex(record["before"])
                    after_digest = record["after_sha256"]
                except (KeyError, TypeError, ValueError) as error:
                    raise LayoutRecoveryError(
                        "sidebar recovery state is malformed"
                    ) from error
                if (
                    key not in LAYOUT_KEYS
                    or key in before
                    or not isinstance(after_digest, str)
                    or len(after_digest) != 64
                ):
                    raise LayoutRecoveryError("sidebar recovery state is malformed")
                before[key] = value
                after_digests[key] = after_digest
            database = LevelDatabase(self.helper, Path(database_path))
            current = {key: database.get(key) for key in before}
            if all(current[key] == before[key] for key in before):
                document["state"] = "ROLLED_BACK"
                self._write_journal(path, document)
                continue
            if all(
                hashlib.sha256(current[key]).hexdigest() == after_digests[key]
                for key in before
            ):
                document["state"] = "COMMITTED"
                self._write_journal(path, document)
                continue
            try:
                database.write(before, self.config.state_dir)
                if any(database.get(key) != value for key, value in before.items()):
                    raise LayoutRecoveryError("sidebar rollback failed verification")
            except BaseException as error:
                document["state"] = "RECOVERY_REQUIRED"
                self._write_journal(path, document)
                raise LayoutRecoveryError("sidebar recovery is required") from error
            document["state"] = "ROLLED_BACK"
            self._write_journal(path, document)

    def _write_journal(self, path: Path, document: Mapping[str, Any]) -> None:
        atomic_write_bytes(
            path,
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        os.chmod(path, 0o600)

    def _commit(
        self,
        database: LevelDatabase,
        current: Mapping[bytes, bytes],
        replacements: Mapping[bytes, bytes],
    ) -> None:
        root = ensure_private_directory(self._journal_root())
        run_id = uuid.uuid4().hex
        journal_path = root / "{}.json".format(run_id)
        journal = {
            "version": 1,
            "run_id": run_id,
            "state": "PREPARED",
            "database": str(database.database),
            "records": [
                {
                    "key": key.hex(),
                    "before": current[key].hex(),
                    "after_sha256": hashlib.sha256(value).hexdigest(),
                }
                for key, value in sorted(replacements.items())
            ],
        }
        self._write_journal(journal_path, journal)
        try:
            database.write(replacements, self.config.state_dir)
            if any(database.get(key) != value for key, value in replacements.items()):
                raise LayoutError("Claude sidebar write failed verification")
        except BaseException as write_error:
            try:
                database.write(
                    {key: current[key] for key in replacements}, self.config.state_dir
                )
                if any(
                    database.get(key) != current[key] for key in replacements
                ):
                    raise LayoutRecoveryError("sidebar rollback failed verification")
                journal["state"] = "ROLLED_BACK"
                self._write_journal(journal_path, journal)
            except BaseException as recovery_error:
                journal["state"] = "RECOVERY_REQUIRED"
                self._write_journal(journal_path, journal)
                raise LayoutRecoveryError(
                    "sidebar recovery is required before another layout write"
                ) from recovery_error
            raise LayoutError("sidebar update was rolled back safely") from write_error
        journal["state"] = "COMMITTED"
        self._write_journal(journal_path, journal)
        self._prune_journals(root)

    def _prune_journals(self, root: Path) -> None:
        paths = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        for path in paths[: -self.config.retention]:
            path.unlink()
