"""Selective synchronization for Claude Desktop sidebar layout records."""

from __future__ import annotations

import copy
import os
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from . import strict_json as json
from .config import Config
from .filesystem import atomic_write_bytes, durable_unlink, ensure_private_directory
from .locking import ExclusiveFileLock, LockUnavailableError
from .processes import managed_processes
from .record_journal import (
    Record,
    RecordJournal,
    RecordJournalError,
    RecordRecoveryError,
)
from .store import SessionStore


ORIGIN_PREFIX = b"_https://claude.ai\x00\x01"
GROUP_SCOPES_KEY = ORIGIN_PREFIX + b"LSS-persisted.dframe-group-scopes"
LOCAL_SLICE_KEY = ORIGIN_PREFIX + b"LSS-persisted.dframe-local-slice"
DFRAME_STORE_KEY = ORIGIN_PREFIX + b"dframe-store"
LAYOUT_KEYS = (GROUP_SCOPES_KEY, LOCAL_SLICE_KEY, DFRAME_STORE_KEY)
GROUP_UPLOAD_KEY = ORIGIN_PREFIX + b"ccd-sync-pending:ccd/dframe-store"
SYNC_OWNER_KEY = ORIGIN_PREFIX + b"ccd-sync-owner"
SYNC_ACTIVE_KEY = ORIGIN_PREFIX + b"ccd-sync-active"
SYNC_QUARANTINE_KEY = ORIGIN_PREFIX + b"ccd-sync-quarantine"
SYNC_METADATA_KEYS = (SYNC_OWNER_KEY, SYNC_ACTIVE_KEY, SYNC_QUARANTINE_KEY, GROUP_UPLOAD_KEY)
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
            "home_projects_pinned_order": list(self.home_projects_pinned_order),
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


def same_recovery_payload(key: bytes, expected: Optional[bytes], current: Optional[bytes]) -> bool:
    """Ignore only known UI bookkeeping when deciding a no-write recovery.

    Claude rewrites wrapper timestamps and collapsedGroups on reopen, and
    consumes scoped migration markers. All other fields must match, including
    unknown fields. Only the journal's whole-transaction phase check uses this
    comparison; it never authorizes replacing either record's bytes.
    """
    if expected == current:
        return True
    if key == GROUP_UPLOAD_KEY and current is None and expected is not None:
        if not expected.startswith(b"\x01") or not expected.endswith(b"|migrate"):
            return False
        try:
            scope = expected[1:-len(b"|migrate")].decode("utf-8")
        except UnicodeError:
            return False
        account, separator, workspace = scope.partition("/")
        return bool(
            account and separator and workspace and "/" not in workspace
            and not any(character.isspace() or ord(character) < 32 or ord(character) == 127
                        or character == "|" for character in scope)
        )
    if expected is None or current is None:
        return False
    try:
        previous = _decode_record(expected, "recovery preimage")
        present = _decode_record(current, "recovery current record")
        for document in (previous, present):
            if key in (GROUP_SCOPES_KEY, LOCAL_SLICE_KEY):
                if type(document.get("timestamp")) is not int or document["timestamp"] < 0:
                    return False
                del document["timestamp"]
            elif key == DFRAME_STORE_KEY:
                state = document.get("state")
                if not isinstance(state, dict):
                    return False
                _string_list(state.get("collapsedGroups"), "collapsed groups")
                del state["collapsedGroups"]
            else:
                return False
        # Dict equality treats True == 1; unknown schema types must stay exact.
        return json.dumps(previous, sort_keys=True) == json.dumps(present, sort_keys=True)
    except LayoutError:
        return False


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
    assignments = _string_map(scope.get("assignments", {}), "custom group assignments")
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
    assignments = _string_map(scope.get("assignments", {}), "custom group assignments")
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

    merged["groups"] = [{"id": ids_by_name[name], "name": name} for name in group_names]
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
            session for session in ordered if assignments_by_name.get(session) == name
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
        assignments=_string_map(document.get("assignments"), "snapshot assignments"),
        pinned_order=tuple(_string_list(document.get("pinned_order"), "snapshot pins")),
        home_projects_pinned_order=tuple(
            _string_list(
                document.get("home_projects_pinned_order"),
                "snapshot project pins",
            )
        ),
    )


def _snapshot_bytes(path: Path) -> Optional[bytes]:
    if not os.path.lexists(str(path)):
        return None
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > MAX_RECORD_BYTES
            ):
                raise LayoutError("sidebar snapshot is not a regular bounded file")
            content = stream.read(MAX_RECORD_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or len(content) > MAX_RECORD_BYTES:
            raise LayoutError("sidebar snapshot changed while being read")
        return content
    except OSError as error:
        raise LayoutError("sidebar snapshot is not a readable regular file") from error


def _decode_snapshot(content: Optional[bytes]) -> Optional[LayoutSnapshot]:
    if content is None:
        return None
    try:
        document = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LayoutError("sidebar snapshot is malformed") from error
    return _load_snapshot_document(document)


def load_snapshot(path: Path) -> Optional[LayoutSnapshot]:
    return _decode_snapshot(_snapshot_bytes(path))


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
                for _group_id, name in _ordered_group_pairs(store_scopes[active_scope])
            ]
        groups = _stable_union((active_groups, snapshot.groups, current_groups))
        assignments = dict(snapshot.assignments)
        if active_has_groups:
            assignments.update(_scope_assignments_by_name(store_scopes[active_scope]))
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
            ordered.extend(
                session for session in assigned if session not in set(ordered)
            )
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
        value = self.get_optional(key)
        if value is None:
            raise LayoutError("required sidebar record is missing")
        return value

    def get_optional(self, key: bytes) -> Optional[bytes]:
        result = subprocess.run(
            [str(self.helper), "get", str(self.database), key.hex()],
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 3:
            return None
        if result.returncode != 0:
            raise LayoutError("Claude sidebar record could not be read safely")
        encoded = result.stdout.strip()
        try:
            return bytes.fromhex(encoded)
        except ValueError as error:
            raise LayoutError("sidebar helper returned malformed data") from error

    def write(self, records: Mapping[bytes, Optional[bytes]], state_dir: Path) -> None:
        ensure_private_directory(state_dir)
        descriptor, name = tempfile.mkstemp(prefix=".layout-ops-", dir=str(state_dir))
        path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                for key, value in sorted(records.items()):
                    if value is None:
                        handle.write("D\t{}\n".format(key.hex()))
                    else:
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

    def __init__(
        self,
        config: Config,
        *,
        helper: Optional[Path] = None,
        process_probe: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config = config
        self.helper = Path(helper or config.state_dir.parent / "bin" / "layoutdb")
        self.process_probe = process_probe or (
            lambda: managed_processes(
                config.profiles, executable=config.claude_executable, timeout=5
            )
        )

    def _assert_stopped(self) -> None:
        if self.process_probe():
            raise LayoutBusyError(
                "waiting for Claude to quit before syncing sidebar layout"
            )

    def _group_upload_marker(
        self,
        database: LevelDatabase,
        current: Mapping[bytes, bytes],
        replacements: Mapping[bytes, bytes],
        target_sessions: Mapping[str, Set[str]],
    ) -> Optional[Tuple[Optional[bytes], bytes]]:
        """Use Claude's scoped migration path, never replace server preferences.

        Claude's account settings own the group list. Without a pending seed,
        startup replaces restored groups with the server list and drops local
        assignments to missing IDs. The |migrate seed merges groups while taking
        unrelated preferences from the server. Identity and quarantine markers
        are read-only; only the dframe migration marker may be written.
        """
        before = _decode_record(current[DFRAME_STORE_KEY], "dframe store")["state"]
        after = _decode_record(replacements[DFRAME_STORE_KEY], "dframe store")["state"]
        old_scopes = before.get("customGroupsByScope", {})
        new_scopes = after.get("customGroupsByScope", {})
        old_persisted = _decode_record(current[GROUP_SCOPES_KEY], "group scopes")["value"]
        new_persisted = _decode_record(replacements[GROUP_SCOPES_KEY], "group scopes")["value"]
        changed_scopes = set()
        for old, new in ((old_scopes, new_scopes), (old_persisted, new_persisted)):
            changed_scopes.update(
                scope for scope in set(old) | set(new) if old.get(scope) != new.get(scope)
            )
        if not changed_scopes:
            return None
        active = after.get("lastSidebarScopeKey")
        metadata = {}
        for key in SYNC_METADATA_KEYS:
            self._assert_stopped()
            metadata[key] = database.get_optional(key)
        if metadata[SYNC_ACTIVE_KEY] not in (None, b"\x010", b"\x011"):
            raise LayoutError("account sidebar sync has an unknown state")
        if metadata[SYNC_QUARANTINE_KEY] is not None:
            raise LayoutError("account sidebar sync is quarantined; sign in again before syncing")
        settings_sync_active = metadata[SYNC_ACTIVE_KEY] == b"\x011"
        # Older clients need only local data when no account update is pending.
        if not settings_sync_active and all(
            metadata[key] is None for key in (SYNC_OWNER_KEY, GROUP_UPLOAD_KEY)
        ):
            return None
        if not isinstance(active, str):
            raise LayoutError("account sidebar scope is malformed")
        account, separator, workspace = active.partition("/")
        if not account or not separator or not workspace or "/" in workspace:
            raise LayoutError("account sidebar scope is malformed")
        if metadata[SYNC_OWNER_KEY] != b"\x01" + account.encode("utf-8"):
            raise LayoutError("account sidebar identity does not match the active scope")
        pending = metadata[GROUP_UPLOAD_KEY]
        scoped = b"\x01" + active.encode("utf-8")
        if pending not in (None, scoped, scoped + b"|migrate"):
            raise LayoutError("another account sidebar update is pending")
        if pending == scoped and active in changed_scopes:
            raise LayoutBusyError("account sidebar user edit is pending; reopen Claude to upload it before syncing")
        old_groups = old_scopes.get(active, {}).get("groups", [])
        new_groups = new_scopes.get(active, {}).get("groups", [])
        if not settings_sync_active or active not in target_sessions or old_groups == new_groups:
            return None
        return pending, scoped + b"|migrate"

    def _check_database_path(self, path: Path) -> None:
        allowed = {
            profile.data_root / "Local Storage" / "leveldb"
            for profile in self.config.profiles
        }
        if path not in allowed or path.is_symlink() or not path.is_dir():
            raise LayoutError("Claude Local Storage has an unknown layout")
        if any(parent.is_symlink() for parent in (path.parent, path.parent.parent)):
            raise LayoutError("Claude Local Storage includes an unsafe symlink")

    def probe(self) -> Dict[str, Any]:
        """Validate current records in disposable copies without opening live DBs."""
        if not self.config.sync_sidebar_layout:
            return {"state": "disabled"}
        self._assert_stopped()
        if not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            raise LayoutError("sidebar helper is not installed")
        profiles = groups = pins = assignments = 0
        for index, profile in enumerate(self.config.profiles):
            source = profile.data_root / "Local Storage" / "leveldb"
            self._check_database_path(source)
            targets = self._target_sessions(profile.name, profile.data_root)
            with tempfile.TemporaryDirectory(
                prefix="claude-layout-probe-"
            ) as temporary:
                copied = Path(temporary) / "leveldb"
                self._assert_stopped()
                # Preserve symlinks in the copy, then reject them before opening.
                shutil.copytree(source, copied, symlinks=True)
                for entry in copied.rglob("*"):
                    mode = entry.lstat().st_mode
                    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                        raise LayoutError(
                            "Claude Local Storage contains an unsafe entry"
                        )
                    os.chmod(str(entry), 0o700 if stat.S_ISDIR(mode) else 0o600)
                os.chmod(str(copied), 0o700)
                self._assert_stopped()
                database = LevelDatabase(self.helper, copied)
                current = {key: database.get(key) for key in LAYOUT_KEYS}
                transformed = transform_layout_records(
                    current,
                    targets,
                    snapshot=load_snapshot(
                        self.config.state_dir / "sidebar-layout-{}.json".format(index)
                    ),
                )
                self._group_upload_marker(database, current, transformed.records, targets)
                profiles += 1
                groups += transformed.group_count
                pins += transformed.pin_count
                assignments += transformed.assignment_count
        self._assert_stopped()
        return {
            "state": "compatible",
            "profile_count": profiles,
            "group_count": groups,
            "pin_count": pins,
            "assignment_count": assignments,
        }

    def sync(self) -> LayoutReceipt:
        if not self.config.sync_sidebar_layout:
            return LayoutReceipt("disabled", 0, 0, 0, 0, 0, 0)
        self._assert_stopped()
        if not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            raise LayoutError("sidebar helper is not installed")
        ensure_private_directory(self.config.state_dir)
        try:
            lock = ExclusiveFileLock(
                self.config.state_dir / "transaction.lock", mode="auto"
            ).acquire()
        except LockUnavailableError as error:
            raise LayoutBusyError(
                "another synchronization is still finishing"
            ) from error
        try:
            self._recover_pending()
            totals = [0, 0, 0, 0, 0]
            changed_profiles = 0
            for index, profile in enumerate(self.config.profiles):
                database_path = profile.data_root / "Local Storage" / "leveldb"
                self._check_database_path(database_path)
                target_sessions = self._target_sessions(profile.name, profile.data_root)
                database = LevelDatabase(self.helper, database_path)
                current = {}
                for key in LAYOUT_KEYS:
                    self._assert_stopped()
                    current[key] = database.get(key)
                snapshot_path = self.config.state_dir / "sidebar-layout-{}.json".format(
                    index
                )
                snapshot_before = _snapshot_bytes(snapshot_path)
                transformed = transform_layout_records(
                    current,
                    target_sessions,
                    snapshot=_decode_snapshot(snapshot_before),
                )
                planned_records = dict(transformed.records)
                marker = self._group_upload_marker(database, current, planned_records, target_sessions)
                if marker is not None:
                    current[GROUP_UPLOAD_KEY], planned_records[GROUP_UPLOAD_KEY] = marker
                replacements = {
                    key: value
                    for key, value in planned_records.items()
                    if current[key] != value
                }
                snapshot_after = (
                    json.dumps(transformed.snapshot.as_dict(), indent=2, sort_keys=True)
                    + "\n"
                ).encode("utf-8")
                if replacements or snapshot_before != snapshot_after:
                    self._commit(
                        database,
                        current,
                        planned_records,
                        snapshot_path=snapshot_path,
                        snapshot_before=snapshot_before,
                        snapshot_after=snapshot_after,
                    )
                if replacements:
                    changed_profiles += 1
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

    def _target_sessions(
        self, profile_name: str, data_root: Path
    ) -> Dict[str, Set[str]]:
        discovery = SessionStore().discover_targets(self.config)
        if discovery.invalid_replicas:
            raise LayoutError("Claude session storage has an unknown layout")
        approved = {
            (target.account_id, target.workspace_id)
            for target in self.config.approved_targets
            if target.profile_name == profile_name
        }
        targets = {}
        for target in discovery.targets:
            if target.profile_name != profile_name:
                continue
            if (
                self.config.target_policy == "approved-only"
                and (target.account_id, target.workspace_id) not in approved
            ):
                continue
            sessions = set()
            for replica in target.path.iterdir():
                if replica.is_symlink() or not replica.is_file():
                    continue
                if replica.name.startswith("local_") and replica.name.endswith(".json"):
                    sessions.add("code:{}".format(replica.stem))
            targets["{}/{}".format(target.account_id, target.workspace_id)] = sessions
        if not targets:
            raise LayoutError("no approved Claude sidebar scopes were found")
        if (
            len({key.split("/")[0] for key in targets}) > 1
            and not self.config.acknowledge_cross_account_copy
        ):
            raise LayoutError("cross-account sidebar copying is not acknowledged")
        return targets

    def _journal_root(self) -> Path:
        return self.config.state_dir / "layout-runs"

    def _record_target(self, database: Path, key: bytes) -> str:
        return json.dumps([str(database), key.hex()], separators=(",", ":"))

    def _snapshot_target(self, path: Path) -> str:
        return json.dumps(["snapshot", str(path)], separators=(",", ":"))

    def _parse_target(self, target: str) -> Tuple[Path, Optional[bytes]]:
        try:
            value = json.loads(target)
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("invalid sidebar target")
            database, encoded_key = value
            if database == "snapshot":
                path = Path(encoded_key)
                allowed = {
                    self.config.state_dir / "sidebar-layout-{}.json".format(index)
                    for index in range(len(self.config.profiles))
                }
                if path not in allowed or path.is_symlink() or path.parent.is_symlink():
                    raise ValueError("invalid sidebar snapshot target")
                if not path.parent.is_dir():
                    raise ValueError("sidebar snapshot directory is missing")
                return path, None
            key = bytes.fromhex(encoded_key)
            path = Path(database)
        except (TypeError, ValueError) as error:
            raise LayoutRecoveryError("sidebar recovery target is malformed") from error
        if key not in (*LAYOUT_KEYS, GROUP_UPLOAD_KEY):
            raise LayoutRecoveryError("sidebar recovery key is not allowed")
        self._check_database_path(path)
        return path, key

    def _legacy_records(self, document: Mapping[str, Any]) -> Sequence[Record]:
        raw = document["records"]
        if not isinstance(raw, list):
            raise ValueError("invalid sidebar records")
        return [
            Record(
                self._record_target(
                    Path(document["database"]), bytes.fromhex(item["key"])
                ),
                bytes.fromhex(item["before"]),
                None,
                item["after_sha256"],
                legacy=True,
            )
            for item in raw
        ]

    def _journal(self) -> RecordJournal:
        def recovery_equivalent(target: str, expected: Optional[bytes], current: Optional[bytes]) -> bool:
            _path, key = self._parse_target(target)
            if key is None:
                return expected == current
            return same_recovery_payload(key, expected, current)

        def read(target: str) -> Optional[bytes]:
            path, key = self._parse_target(target)
            if key is None:
                return _snapshot_bytes(path)
            self._assert_stopped()  # Opening LevelDB can itself mutate its files.
            database = LevelDatabase(self.helper, path)
            return database.get_optional(key) if key == GROUP_UPLOAD_KEY else database.get(key)

        def write(target: str, value: bytes) -> None:
            path, key = self._parse_target(target)
            self._assert_stopped()
            if key is None:
                atomic_write_bytes(path, value)
            else:
                LevelDatabase(self.helper, path).write(
                    {key: value}, self.config.state_dir
                )

        def delete(target: str) -> None:
            path, key = self._parse_target(target)
            if key is not None and key != GROUP_UPLOAD_KEY:
                raise LayoutRecoveryError("sidebar records cannot be deleted")
            self._assert_stopped()
            if key == GROUP_UPLOAD_KEY:
                LevelDatabase(self.helper, path).write({key: None}, self.config.state_dir)
            else:
                durable_unlink(path)

        return RecordJournal(
            self._journal_root(),
            read=read,
            write=write,
            delete=delete,
            validate=self._parse_target,
            before_mutation=self._assert_stopped,
            retention=self.config.retention,
            legacy_loader=self._legacy_records,
            recovery_equivalent=recovery_equivalent,
        )

    def _recover_pending(self) -> None:
        self._assert_stopped()
        try:
            self._journal().recover()
        except RecordJournalError as error:
            raise LayoutRecoveryError(str(error)) from error

    def _commit(
        self,
        database: LevelDatabase,
        current: Mapping[bytes, Optional[bytes]],
        replacements: Mapping[bytes, bytes],
        *,
        snapshot_path: Optional[Path] = None,
        snapshot_before: Optional[bytes] = None,
        snapshot_after: Optional[bytes] = None,
    ) -> None:
        before = {
            self._record_target(database.database, key): current[key]
            for key in replacements
        }
        after = {
            self._record_target(database.database, key): value
            for key, value in replacements.items()
        }
        if snapshot_path is not None:
            target = self._snapshot_target(snapshot_path)
            before[target] = snapshot_before
            after[target] = snapshot_after
        try:
            self._journal().commit(before, after)
        except RecordRecoveryError as error:
            raise LayoutRecoveryError(str(error)) from error
        except RecordJournalError as error:
            raise LayoutError(str(error)) from error
