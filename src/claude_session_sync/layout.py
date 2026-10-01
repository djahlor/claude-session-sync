"""Selective synchronization for Claude Desktop sidebar layout records."""

from __future__ import annotations

import contextlib
import copy
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from . import strict_json as json
from .config import Config
from .filesystem import atomic_write_bytes, durable_unlink, ensure_private_directory
from .locking import ExclusiveFileLock, LockUnavailableError
from .processes import managed_processes
from .record_journal import (
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
ADOPT_PENDING_FILENAME = "sidebar-adopt-pending.json"
COPY_NOT_UPLOADED = (
    "an account's groups changed before Claude uploaded the copy synced into it, "
    "so they may be its old server groups; list the accounts with keep-sidebar "
    "--dry-run, then keep one with keep-sidebar --account N --apply"
)
NEEDS_MAIN_ACCOUNT = (
    "it is unclear which account's pins and groups to keep; list the accounts "
    "with keep-sidebar --dry-run, then keep one with keep-sidebar --account N --apply"
)


class LayoutError(RuntimeError):
    """Raised when sidebar data cannot be changed without guessing."""

    # What status and doctor report; None reports unsafe-layout.
    reason: Optional[str] = None


class LayoutBusyError(LayoutError):
    """Raised when another synchronization writer owns the lock."""


class LayoutRecoveryError(LayoutError):
    """Raised when a failed write cannot be restored automatically."""


class LayoutChoiceError(LayoutError):
    """Raised when only the user can say which account's sidebar to keep."""

    reason = "choose-main-account"


class LayoutDisagreementError(LayoutError):
    """Raised when Claude's two saved copies of the same sidebar data differ."""

    reason = "sidebar-records-disagree"


@dataclass(frozen=True)
class LayoutSnapshot:
    """The account whose pins and groups the last sync copied to the others.

    No snapshot means no account has been adopted yet.
    """

    adopted_scope: str
    # The layout last copied into adopted_scope, kept while Claude has not
    # consumed the marker that uploads it as that account's own. Until then
    # that account is the source only while it still holds this layout.
    unconfirmed_copy: Optional[Mapping[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        document = {"version": SNAPSHOT_VERSION, "adopted_scope": self.adopted_scope}
        if self.unconfirmed_copy is not None:
            document["unconfirmed_copy"] = copy.deepcopy(dict(self.unconfirmed_copy))
        return document


@dataclass(frozen=True)
class LayoutTransform:
    records: Mapping[bytes, bytes]
    # None leaves the snapshot file as it is.
    snapshot: Optional[LayoutSnapshot]
    group_count: int
    assignment_count: int
    pin_count: int
    canonical_upload_scope: Optional[str] = None
    # Why nothing changed, when the user should know.
    reason: Optional[str] = None


@dataclass(frozen=True)
class LayoutReceipt:
    state: str
    profile_count: int
    record_count: int
    group_count: int
    assignment_count: int
    pin_count: int
    reason: Optional[str] = None


@dataclass(frozen=True)
class SidebarAccount:
    """One synced account's sidebar, as the main-account question shows it."""

    # ACCOUNT/WORKSPACE. Never printed whole: it is the only name on disk.
    scope: str
    is_signed_in: bool
    groups: int
    # Pinned chats among this account's chats; pins are one shared list.
    pins: int
    chats: int


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
    if not value or len(value) > MAX_RECORD_BYTES or value[:1] not in (b"\x00", b"\x01"):
        raise LayoutError("{} has an unknown encoding".format(label))
    try:
        payload = value[1:]
        if value[:1] == b"\x00":
            if len(payload) % 2:
                raise UnicodeError("odd UTF-16 payload")
            text = payload.decode("utf-16-le")
        else:
            text = payload.decode("latin-1")
        decoded = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LayoutError("{} contains malformed JSON".format(label)) from error
    if not isinstance(decoded, dict):
        raise LayoutError("{} is not a JSON object".format(label))
    return decoded


def _encode_record(value: Mapping[str, Any]) -> bytes:
    return b"\x01" + json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


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
        _string_list(sessions, "custom group session order")
    return pairs


def _scope_order_by_name(scope: Mapping[str, Any]) -> Dict[str, Tuple[str, ...]]:
    """Return the valid part of Chromium's stale-prone order index."""

    pairs = _ordered_group_pairs(scope)
    assignments = _string_map(scope.get("assignments", {}), "custom group assignments")
    raw_order = scope.get("order", {})
    return {
        name: tuple(
            session
            for session in _string_list(
                raw_order.get(group_id, []), "custom group session order"
            )
            if assignments.get(session) == group_id
        )
        for group_id, name in pairs
    }


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


def _load_snapshot_document(document: Any) -> Optional[LayoutSnapshot]:
    """Read the adopted account. Older snapshots also hold a merged layout,
    which nothing reads now; one without an adopted account means none yet."""

    if not isinstance(document, dict) or document.get("version") != SNAPSHOT_VERSION:
        raise LayoutError("sidebar snapshot has an unknown version")
    adopted_scope = document.get("adopted_scope")
    if adopted_scope is None:
        return None
    if not isinstance(adopted_scope, str) or "/" not in adopted_scope:
        raise LayoutError("snapshot adopted scope has an unknown shape")
    unconfirmed = document.get("unconfirmed_copy")
    if unconfirmed is not None and (
        not isinstance(unconfirmed, dict)
        or set(unconfirmed) != {"groups", "assignments", "order"}
    ):
        raise LayoutError("snapshot unconfirmed copy has an unknown shape")
    return LayoutSnapshot(adopted_scope, unconfirmed)


def _layout(scope: Mapping[str, Any]) -> Dict[str, Any]:
    """A scope's groups, chat assignments, and group order by name, for comparison."""

    names = dict(_ordered_group_pairs(scope))
    assignments = _string_map(scope.get("assignments", {}), "custom group assignments")
    return {
        "groups": list(names.values()),
        "assignments": {session: names[group] for session, group in sorted(assignments.items())},
        "order": {name: list(sessions) for name, sessions in _scope_order_by_name(scope).items()},
    }


def _upload_scope(marker: Optional[bytes]) -> Optional[str]:
    """The scope whose sidebar upload Claude has not consumed yet, if any."""

    if marker is None or not marker.startswith(b"\x01"):
        return None
    try:
        value = marker[1:].decode("utf-8")
    except UnicodeError:
        return None
    return value[: -len("|migrate")] if value.endswith("|migrate") else value


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


def _copy_authoritative_scope(
    source: Mapping[str, Any], existing: Mapping[str, Any]
) -> Dict[str, Any]:
    """Copy one trusted layout while retaining unrelated target metadata."""

    _ordered_group_pairs(source)
    _ordered_group_pairs(existing)
    existing_groups = {
        group["name"]: group
        for group in existing.get("groups", [])
    }
    replacement = copy.deepcopy(existing)
    replacement_groups = []
    for source_group in source["groups"]:
        merged_group = copy.deepcopy(existing_groups.get(source_group["name"], {}))
        merged_group.update(copy.deepcopy(source_group))
        replacement_groups.append(merged_group)
    replacement["groups"] = replacement_groups
    replacement["assignments"] = copy.deepcopy(source.get("assignments", {}))
    canonical_order = _scope_order_by_name(source)
    replacement["order"] = {
        group["id"]: list(canonical_order[group["name"]])
        for group in source["groups"]
    }
    return replacement


def _adopt_current_sidebar_records(
    group_record: Mapping[str, Any],
    local_record: Mapping[str, Any],
    store_record: Mapping[str, Any],
    target_sessions: Mapping[str, Set[str]],
    *,
    timestamp_ms: Optional[int],
    source_scope: Optional[str],
    active_scope: Optional[str],
    unconfirmed_copy: Optional[Mapping[str, Any]] = None,
) -> LayoutTransform:
    """Copy one account's groups into every sync target; pins stay one shared list.

    A copy into the signed-in account stays unconfirmed until Claude uploads
    it; otherwise unconfirmed_copy is carried over as given.
    """

    store_state = store_record["state"]
    store_scopes = _validated_scopes(store_state.get("customGroupsByScope", {}))
    persisted_scopes = _validated_scopes(group_record["value"])
    stored_active = store_state.get("lastSidebarScopeKey")
    if active_scope is None:
        raise LayoutError(
            "current sidebar must belong to an approved sync target"
        )
    selected_scope = source_scope
    if (
        selected_scope not in target_sessions
        or selected_scope not in store_scopes
        or not _ordered_group_pairs(store_scopes[selected_scope])
    ):
        raise LayoutError(
            "adopted sidebar source must have groups and belong to an approved sync target"
        )

    source = store_scopes[selected_scope]
    _check_copies_agree(selected_scope, store_scopes, persisted_scopes, store_state, local_record)
    current_pins = _string_list(store_state.get("pinnedOrder", []), "stored pins")
    current_project_pins = _string_list(
        store_state.get("homeProjectsPinnedOrder", []), "stored project pins"
    )

    updated_store_scopes = copy.deepcopy(store_scopes)
    updated_persisted_scopes = copy.deepcopy(persisted_scopes)
    for scope_key in sorted(target_sessions):
        if scope_key == selected_scope:
            updated_persisted_scopes[scope_key] = copy.deepcopy(source)
            continue
        existing = updated_store_scopes.get(
            scope_key,
            updated_persisted_scopes.get(
                scope_key, {"groups": [], "assignments": {}, "order": {}}
            ),
        )
        replacement = _copy_authoritative_scope(source, existing)
        updated_store_scopes[scope_key] = replacement
        updated_persisted_scopes[scope_key] = copy.deepcopy(replacement)

    updated_store = copy.deepcopy(store_record)
    updated_group = copy.deepcopy(group_record)
    updated_local = copy.deepcopy(local_record)
    updated_store["state"]["customGroupsByScope"] = updated_store_scopes
    if active_scope != stored_active:
        # The signed-in account's folder, when Claude had not shown it yet.
        updated_store["state"]["lastSidebarScopeKey"] = active_scope
    updated_group["value"] = updated_persisted_scopes
    updated_store["state"]["pinnedOrder"] = current_pins
    updated_local["value"]["pinnedOrder"] = current_pins
    updated_store["state"]["homeProjectsPinnedOrder"] = current_project_pins
    updated_local["value"]["homeProjectsPinnedOrder"] = current_project_pins
    now = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if updated_group != group_record:
        updated_group["timestamp"] = now
    if updated_local != local_record:
        updated_local["timestamp"] = now

    return LayoutTransform(
        records={
            GROUP_SCOPES_KEY: _encode_record(updated_group),
            LOCAL_SLICE_KEY: _encode_record(updated_local),
            DFRAME_STORE_KEY: _encode_record(updated_store),
        },
        # The account in use now holds the copy, so it is the source of
        # truth from here: its later edits win until the next switch.
        snapshot=LayoutSnapshot(
            active_scope,
            _layout(source) if selected_scope != active_scope else unconfirmed_copy,
        ),
        group_count=len(_ordered_group_pairs(source)),
        assignment_count=len(source.get("assignments", {})),
        pin_count=len(current_pins),
        canonical_upload_scope=(
            active_scope if selected_scope != active_scope else None
        ),
    )


def _check_copies_agree(
    source_scope: str,
    store_scopes: Mapping[str, Mapping[str, Any]],
    persisted_scopes: Mapping[str, Mapping[str, Any]],
    store_state: Mapping[str, Any],
    local_record: Mapping[str, Any],
) -> None:
    """Stop before a copy when Claude's two saves of the source data differ.

    Claude keeps each account's groups, and the pins, in two records. An
    interrupted save can leave a change in only one, and the copy would drop it.
    """

    if source_scope in persisted_scopes and _layout(persisted_scopes[source_scope]) != _layout(
        store_scopes[source_scope]
    ):
        raise LayoutDisagreementError(
            "Claude's two saved copies of the source account's groups differ, so "
            "sync wrote nothing; open Claude, check its sidebar, and quit it"
        )
    for key in ("pinnedOrder", "homeProjectsPinnedOrder"):
        local = local_record["value"].get(key)
        if local is not None and _string_list(local, "local pins") != _string_list(
            store_state.get(key, []), "stored pins"
        ):
            raise LayoutDisagreementError(
                "Claude's two saved copies of the pins differ, so sync wrote "
                "nothing; open Claude, check its pins, and quit it"
            )


def transform_layout_records(
    records: Mapping[bytes, bytes],
    target_sessions: Mapping[str, Set[str]],
    *,
    snapshot: Optional[LayoutSnapshot] = None,
    timestamp_ms: Optional[int] = None,
    adopt_current_sidebar: bool = False,
    adopt_source_scope: Optional[str] = None,
    after_account_switch: bool = False,
    owner_account: Optional[str] = None,
    upload_pending_scope: Optional[str] = None,
) -> LayoutTransform:
    """Return exact allowlisted record replacements for one Claude data root.

    One account's sidebar is the source of truth at a time, and its groups
    are copied to the other accounts. The first sync adopts the account chosen
    at install, else the signed-in account, else the only account with
    groups. After that the signed-in account wins. Right after an account
    switch, the account just left still holds the newest organization,
    because Claude reloads the new account's groups from its servers at
    sign-in, so that one is copied instead.
    """

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
    if adopt_current_sidebar and adopt_source_scope is not None:
        raise LayoutError("current sidebar modes cannot be combined")
    store_state = store_record["state"]
    active = _signed_in_scope(store_state, target_sessions, owner_account)
    unconfirmed = confirmed = None
    if snapshot is not None and snapshot.unconfirmed_copy is not None:
        if upload_pending_scope == snapshot.adopted_scope:
            unconfirmed = snapshot.unconfirmed_copy
        else:
            # Claude uploaded the copy; the snapshot file forgets it either way.
            snapshot = confirmed = LayoutSnapshot(snapshot.adopted_scope)
    if adopt_current_sidebar:
        source = active
    elif adopt_source_scope is not None:
        source = adopt_source_scope
    elif active is None:
        # The signed-in account does not sync yet.
        return _unchanged(records, snapshot=confirmed)
    else:
        store_scopes = _validated_scopes(store_state.get("customGroupsByScope", {}))
        persisted_scopes = _validated_scopes(group_record["value"])
        source = _source_scope(
            active,
            snapshot,
            store_scopes,
            target_sessions,
            active_is_empty=_scope_is_empty(
                store_scopes.get(active, persisted_scopes.get(active))
            ),
            after_account_switch=after_account_switch,
        )
        if source is None:
            return _unchanged(
                records,
                reason="no-groups-yet" if snapshot is None else "main-account-has-no-groups",
                snapshot=confirmed,
            )
        if (
            unconfirmed is not None
            and source == snapshot.adopted_scope
            and _layout(store_scopes[source]) != unconfirmed
        ):
            raise LayoutChoiceError(COPY_NOT_UPLOADED)
    return _adopt_current_sidebar_records(
        group_record,
        local_record,
        store_record,
        target_sessions,
        timestamp_ms=timestamp_ms,
        source_scope=source,
        active_scope=active,
        unconfirmed_copy=(
            unconfirmed if snapshot is not None and active == snapshot.adopted_scope else None
        ),
    )


def _signed_in_scope(
    store_state: Mapping[str, Any],
    target_sessions: Mapping[str, Set[str]],
    owner_account: Optional[str],
) -> Optional[str]:
    """The signed-in account's synced scope, or None when it does not sync yet."""

    active = store_state.get("lastSidebarScopeKey")
    if active is not None and not isinstance(active, str):
        raise LayoutError("last sidebar scope has an unknown shape")
    if owner_account is not None and (
        active is None or active.partition("/")[0] != owner_account
    ):
        # Claude has not shown the signed-in account's Code sidebar yet.
        owned = [key for key in target_sessions if key.partition("/")[0] == owner_account]
        active = owned[0] if len(owned) == 1 else None
    return active if active in target_sessions else None


def sidebar_accounts(
    records: Mapping[bytes, bytes],
    target_sessions: Mapping[str, Set[str]],
    owner_account: Optional[str],
) -> List[SidebarAccount]:
    """The accounts whose pins and groups can be the source, signed-in first."""

    store_record = _decode_record(records[DFRAME_STORE_KEY], "dframe store record")
    store_state = store_record.get("state")
    if not isinstance(store_state, dict):
        raise LayoutError("dframe store record has an unknown shape")
    store_scopes = _validated_scopes(store_state.get("customGroupsByScope", {}))
    pins = set(_string_list(store_state.get("pinnedOrder", []), "stored pins"))
    active = _signed_in_scope(store_state, target_sessions, owner_account)
    rows = [
        SidebarAccount(
            scope=scope_key,
            is_signed_in=scope_key == active,
            groups=len(_ordered_group_pairs(store_scopes.get(scope_key, {"groups": []}))),
            pins=len(pins & sessions),
            chats=len(sessions),
        )
        for scope_key, sessions in target_sessions.items()
    ]
    return sorted(rows, key=lambda row: (not row.is_signed_in, row.scope))


def scope_sessions(targets: Iterable[Any]) -> Dict[str, Set[str]]:
    """Map each sync target's ACCOUNT/WORKSPACE to the chats in its folder."""

    scopes = {}
    for target in targets:
        sessions = set()
        for replica in target.path.iterdir():
            if replica.is_symlink() or not replica.is_file():
                continue
            if replica.name.startswith("local_") and replica.name.endswith(".json"):
                sessions.add("code:{}".format(replica.stem))
        scopes["{}/{}".format(target.account_id, target.workspace_id)] = sessions
    return scopes


def _source_scope(
    active: str,
    snapshot: Optional[LayoutSnapshot],
    store_scopes: Mapping[str, Mapping[str, Any]],
    target_sessions: Mapping[str, Set[str]],
    *,
    active_is_empty: bool,
    after_account_switch: bool,
) -> Optional[str]:
    """Which account's sidebar to copy, or None when it has no groups to copy.

    An empty sidebar is missing data, never a choice to delete every group,
    so it is never copied over another account's groups.
    """

    grouped = [
        key
        for key in sorted(target_sessions)
        if key in store_scopes and _ordered_group_pairs(store_scopes[key])
    ]
    if snapshot is not None:
        if active == snapshot.adopted_scope:
            source = active
        elif after_account_switch or active_is_empty:
            source = snapshot.adopted_scope
        else:
            raise LayoutChoiceError(NEEDS_MAIN_ACCOUNT)
        return source if source in grouped else None
    if active in grouped:
        return active
    if not grouped:
        return None
    if len(grouped) == 1 and active_is_empty:
        return grouped[0]
    raise LayoutChoiceError(NEEDS_MAIN_ACCOUNT)


def _scope_is_empty(scope: Optional[Mapping[str, Any]]) -> bool:
    if scope is None:
        return True
    return (
        not _ordered_group_pairs(scope)
        and not scope.get("assignments", {})
        and not any(scope.get("order", {}).values())
    )


def request_adoption(state_dir: Path, source_scope: str) -> None:
    """Make one account's pins and groups the source of truth at the next closed-Claude sync."""

    account, separator, workspace = source_scope.partition("/")
    if not account or not separator or not workspace or "/" in workspace:
        raise LayoutError("sidebar scope must be ACCOUNT/WORKSPACE")
    ensure_private_directory(state_dir)
    path = Path(state_dir) / ADOPT_PENDING_FILENAME
    atomic_write_bytes(
        path, (json.dumps({"version": 1, "source_scope": source_scope}) + "\n").encode("utf-8")
    )
    os.chmod(str(path), 0o600)


def read_pending_adoption(state_dir: Path) -> Optional[str]:
    content = _snapshot_bytes(Path(state_dir) / ADOPT_PENDING_FILENAME)
    if content is None:
        return None
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LayoutError("pending sidebar adoption is malformed") from error
    scope = document.get("source_scope") if isinstance(document, dict) else None
    if document.get("version") != 1 or not isinstance(scope, str) or scope.count("/") != 1:
        raise LayoutError("pending sidebar adoption is malformed")
    return scope


def clear_pending_adoption(state_dir: Path) -> None:
    path = Path(state_dir) / ADOPT_PENDING_FILENAME
    if os.path.lexists(str(path)):
        durable_unlink(path)


def _owner_account(value: Optional[bytes]) -> Optional[str]:
    """The signed-in account Claude recorded for its settings sync, if any."""

    if value is None or not value.startswith(b"\x01"):
        return None
    try:
        account = value[1:].decode("utf-8")
    except UnicodeError:
        return None
    return account if account and "/" not in account else None


def _unchanged(
    records: Mapping[bytes, bytes],
    reason: Optional[str] = None,
    snapshot: Optional[LayoutSnapshot] = None,
) -> LayoutTransform:
    """Write no record. A snapshot, when given, still replaces the file."""
    return LayoutTransform(
        records={key: records[key] for key in LAYOUT_KEYS},
        snapshot=snapshot,
        group_count=0,
        assignment_count=0,
        pin_count=0,
        reason=reason,
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
        canonical_upload_scope: Optional[str] = None,
    ) -> Optional[Tuple[Optional[bytes], bytes]]:
        """Ask Claude to upload a layout copied into the signed-in account.

        Claude's account settings own the group list. Without a pending
        upload, startup replaces copied groups with the server list and drops
        local assignments to missing IDs. Identity and quarantine markers are
        read-only; only the dframe upload marker may be written.
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
        if canonical_upload_scope is None or not settings_sync_active or old_groups == new_groups:
            return None
        if canonical_upload_scope != active:
            raise LayoutError("canonical sidebar upload does not match the active scope")
        if pending is not None:
            raise LayoutBusyError("another account sidebar update is pending")
        return None, scoped

    def _check_database_path(self, path: Path) -> None:
        allowed = {
            profile.data_root / "Local Storage" / "leveldb"
            for profile in self.config.profiles
        }
        if path not in allowed or path.is_symlink() or not path.is_dir():
            raise LayoutError("Claude Local Storage has an unknown layout")
        if any(parent.is_symlink() for parent in (path.parent, path.parent.parent)):
            raise LayoutError("Claude Local Storage includes an unsafe symlink")

    @contextlib.contextmanager
    def _database_copy(self, data_root: Path) -> Iterator[LevelDatabase]:
        """Open a disposable copy of one profile's sidebar database.

        Opening LevelDB can write to it, so the live files are never opened.
        """
        if not self.helper.is_file() or not os.access(self.helper, os.X_OK):
            raise LayoutError("sidebar helper is not installed")
        source = data_root / "Local Storage" / "leveldb"
        self._check_database_path(source)
        with tempfile.TemporaryDirectory(prefix="claude-layout-probe-") as temporary:
            copied = Path(temporary) / "leveldb"
            # Preserve symlinks in the copy, then reject them before opening.
            shutil.copytree(source, copied, symlinks=True)
            for entry in copied.rglob("*"):
                mode = entry.lstat().st_mode
                if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise LayoutError("Claude Local Storage contains an unsafe entry")
                os.chmod(str(entry), 0o700 if stat.S_ISDIR(mode) else 0o600)
            os.chmod(str(copied), 0o700)
            yield LevelDatabase(self.helper, copied)

    def accounts(self, target_sessions: Mapping[str, Set[str]]) -> List[SidebarAccount]:
        """The default profile's accounts as rows. Works while Claude is open."""

        defaults = [profile for profile in self.config.profiles if profile.is_default]
        if len(defaults) != 1:
            raise LayoutError("choose the single default data profile before listing accounts")
        with self._database_copy(defaults[0].data_root) as database:
            records = {key: database.get(key) for key in LAYOUT_KEYS}
            owner = _owner_account(database.get_optional(SYNC_OWNER_KEY))
        return sidebar_accounts(records, target_sessions, owner)

    def _requested_source(
        self, adopt_current_sidebar: bool, adopt_source_scope: Optional[str]
    ) -> Tuple[Optional[str], bool]:
        """The account a sync must copy from by request, and whether it is a pending choice.

        A choice made with keep-sidebar, or at install, waits in a file until
        Claude is closed.
        """
        if adopt_current_sidebar and adopt_source_scope is not None:
            raise LayoutError("current sidebar modes cannot be combined")
        pending = False
        if not adopt_current_sidebar and adopt_source_scope is None:
            adopt_source_scope = read_pending_adoption(self.config.state_dir)
            pending = adopt_source_scope is not None
        if (adopt_current_sidebar or adopt_source_scope is not None) and (
            len(self.config.profiles) != 1 or not self.config.profiles[0].is_default
        ):
            raise LayoutError(
                "choose the single default data profile before adopting its current sidebar"
            )
        return adopt_source_scope, pending

    def _plan_profile(
        self,
        database: LevelDatabase,
        target_sessions: Mapping[str, Set[str]],
        snapshot: Optional[LayoutSnapshot],
        *,
        adopt_current_sidebar: bool,
        adopt_source_scope: Optional[str],
        after_account_switch: bool,
    ) -> Tuple[Dict[bytes, Optional[bytes]], Dict[bytes, bytes], LayoutTransform]:
        """Read one profile's sidebar and plan the records to write.

        Returns the records read, the records planned, and the transform.
        Sync runs this on the live database and doctor on a disposable copy.
        """
        current = {}  # type: Dict[bytes, Optional[bytes]]
        for key in LAYOUT_KEYS:
            self._assert_stopped()
            current[key] = database.get(key)
        self._assert_stopped()
        transformed = transform_layout_records(
            current,
            target_sessions,
            snapshot=snapshot,
            adopt_current_sidebar=adopt_current_sidebar,
            adopt_source_scope=adopt_source_scope,
            after_account_switch=after_account_switch,
            owner_account=_owner_account(database.get_optional(SYNC_OWNER_KEY)),
            upload_pending_scope=_upload_scope(database.get_optional(GROUP_UPLOAD_KEY)),
        )
        planned = dict(transformed.records)
        marker = self._group_upload_marker(
            database,
            current,
            planned,
            canonical_upload_scope=transformed.canonical_upload_scope,
        )
        if marker is not None:
            current[GROUP_UPLOAD_KEY], planned[GROUP_UPLOAD_KEY] = marker
        return current, planned, transformed

    def probe(self) -> Dict[str, Any]:
        """Plan the next closed sync on disposable copies, without opening live DBs.

        It takes the inputs `auto` takes: the signed-in owner account and a
        pending choice, with no account-switch mode. A switch restart syncs
        at once, so the sync doctor checks is the one when Claude closes.
        """
        if not self.config.sync_sidebar_layout:
            return {"state": "disabled"}
        self._assert_stopped()
        adopt_source_scope, _pending = self._requested_source(False, None)
        profiles = groups = pins = assignments = 0
        reasons = []
        for index, profile in enumerate(self.config.profiles):
            targets = self._target_sessions(profile.name, profile.data_root)
            snapshot = load_snapshot(
                self.config.state_dir / "sidebar-layout-{}.json".format(index)
            )
            self._assert_stopped()
            with self._database_copy(profile.data_root) as database:
                _current, _planned, transformed = self._plan_profile(
                    database,
                    targets,
                    snapshot,
                    adopt_current_sidebar=False,
                    adopt_source_scope=adopt_source_scope,
                    after_account_switch=False,
                )
            profiles += 1
            groups += transformed.group_count
            pins += transformed.pin_count
            assignments += transformed.assignment_count
            if transformed.reason is not None:
                reasons.append(transformed.reason)
        self._assert_stopped()
        result = {
            "state": "compatible",
            "profile_count": profiles,
            "group_count": groups,
            "pin_count": pins,
            "assignment_count": assignments,
        }  # type: Dict[str, Any]
        if reasons:
            result["reason"] = reasons[0]
        return result

    def sync(
        self,
        *,
        adopt_current_sidebar: bool = False,
        adopt_source_scope: Optional[str] = None,
        after_account_switch: bool = False,
    ) -> LayoutReceipt:
        if not self.config.sync_sidebar_layout:
            return LayoutReceipt("disabled", 0, 0, 0, 0, 0)
        adopt_source_scope, pending = self._requested_source(
            adopt_current_sidebar, adopt_source_scope
        )
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
            totals = [0, 0, 0, 0]
            changed_profiles = 0
            reasons = []
            for index, profile in enumerate(self.config.profiles):
                database_path = profile.data_root / "Local Storage" / "leveldb"
                self._check_database_path(database_path)
                target_sessions = self._target_sessions(profile.name, profile.data_root)
                database = LevelDatabase(self.helper, database_path)
                snapshot_path = self.config.state_dir / "sidebar-layout-{}.json".format(
                    index
                )
                snapshot_before = _snapshot_bytes(snapshot_path)
                current, planned_records, transformed = self._plan_profile(
                    database,
                    target_sessions,
                    _decode_snapshot(snapshot_before),
                    adopt_current_sidebar=adopt_current_sidebar,
                    adopt_source_scope=adopt_source_scope,
                    after_account_switch=after_account_switch,
                )
                replacements = {
                    key: value
                    for key, value in planned_records.items()
                    if current[key] != value
                }
                snapshot_after = snapshot_before
                if transformed.snapshot is not None:
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
                if transformed.reason is not None:
                    reasons.append(transformed.reason)
            if pending:
                clear_pending_adoption(self.config.state_dir)
            return LayoutReceipt(
                "synced" if changed_profiles else "noop",
                len(self.config.profiles),
                totals[0],
                totals[1],
                totals[2],
                totals[3],
                reasons[0] if reasons and not changed_profiles else None,
            )
        finally:
            lock.release()

    def _target_sessions(
        self, profile_name: str, data_root: Path
    ) -> Dict[str, Set[str]]:
        discovery = SessionStore().discover_targets(self.config)
        if discovery.invalid_replicas:
            raise LayoutError("Claude session storage has an unknown layout")
        from .enrollment import selected_targets

        targets = scope_sessions(
            target
            for target in selected_targets(self.config, discovery.targets)
            if target.profile_name == profile_name
        )
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
