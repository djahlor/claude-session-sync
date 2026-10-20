"""Sidebar layout records: decoding, validation and the copy plans built on them."""

from __future__ import annotations

import copy
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    cast,
)

from . import strict_json as json
from .filesystem import (
    FileChangedError,
    UnsafePathError,
    read_bounded,
)

ORIGIN_PREFIX = b"_https://claude.ai\x00\x01"


GROUP_SCOPES_KEY = ORIGIN_PREFIX + b"LSS-persisted.dframe-group-scopes"


LOCAL_SLICE_KEY = ORIGIN_PREFIX + b"LSS-persisted.dframe-local-slice"


DFRAME_STORE_KEY = ORIGIN_PREFIX + b"dframe-store"


LAYOUT_KEYS = (GROUP_SCOPES_KEY, LOCAL_SLICE_KEY, DFRAME_STORE_KEY)


GROUP_UPLOAD_KEY = ORIGIN_PREFIX + b"ccd-sync-pending:ccd/dframe-store"


SYNC_OWNER_KEY = ORIGIN_PREFIX + b"ccd-sync-owner"


SYNC_ACTIVE_KEY = ORIGIN_PREFIX + b"ccd-sync-active"


SYNC_QUARANTINE_KEY = ORIGIN_PREFIX + b"ccd-sync-quarantine"


SYNC_METADATA_KEYS = (
    SYNC_OWNER_KEY,
    SYNC_ACTIVE_KEY,
    SYNC_QUARANTINE_KEY,
    GROUP_UPLOAD_KEY,
)


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


# Newer Claude builds keep pins and groups per account on Anthropic's servers
# and load the account's copy at startup, unless Claude's pending-upload flag
# for that account says the local slot holds newer edits.
PER_ACCOUNT = "per-account"


PER_ACCOUNT_REASON = "claude-keeps-sidebar-per-account"


# Set when a sync copied the main sidebar into the signed-in account.
UPLOAD_ON_NEXT_START = "claude-uploads-copy-on-next-start"


# Per-account copies keep their own journal, so a stuck journal from the old
# group copy can never roll a newer sidebar back.
ACCOUNT_JOURNAL_DIRNAME = "layout-account-runs"


MAIN_SCOPE_FILENAME = "sidebar-main-{}.json"


class LayoutError(RuntimeError):
    """Raised when sidebar data cannot be changed without guessing."""

    # What status and doctor report; None reports unsafe-layout.
    reason: str | None = None


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
    unconfirmed_copy: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        document = {"version": SNAPSHOT_VERSION, "adopted_scope": self.adopted_scope}
        if self.unconfirmed_copy is not None:
            document["unconfirmed_copy"] = copy.deepcopy(dict(self.unconfirmed_copy))
        return document


@dataclass(frozen=True)
class LayoutTransform:
    records: Mapping[bytes, bytes]
    # None leaves the snapshot file as it is.
    snapshot: LayoutSnapshot | None
    group_count: int
    assignment_count: int
    pin_count: int
    canonical_upload_scope: str | None = None
    # Why nothing changed, when the user should know.
    reason: str | None = None


@dataclass(frozen=True)
class LayoutReceipt:
    state: str
    profile_count: int
    record_count: int
    group_count: int
    assignment_count: int
    pin_count: int
    reason: str | None = None


@dataclass(frozen=True)
class SidebarAccount:
    """One synced account's sidebar, as the main-account question shows it."""

    # ACCOUNT/WORKSPACE. Never printed whole: it is the only name on disk.
    scope: str
    is_signed_in: bool
    # In sidebar order. The user's own data, shown in their own terminal.
    group_names: tuple[str, ...]
    # Pinned chats among this account's chats; pins are one shared list.
    pins: int
    chats: int

    @property
    def groups(self) -> int:
        return len(self.group_names)


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise LayoutError(f"{label} has an unknown shape")
    if len(value) != len(set(value)):
        raise LayoutError(f"{label} contains duplicates")
    return list(value)


def _string_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise LayoutError(f"{label} has an unknown shape")
    return dict(value)


def _decode_record(value: bytes, label: str) -> dict[str, Any]:
    if (
        not value
        or len(value) > MAX_RECORD_BYTES
        or value[:1] not in (b"\x00", b"\x01")
    ):
        raise LayoutError(f"{label} has an unknown encoding")
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
        raise LayoutError(f"{label} contains malformed JSON") from error
    if not isinstance(decoded, dict):
        raise LayoutError(f"{label} is not a JSON object")
    return decoded


def keeps_sidebar_on_server(record: bytes | None) -> bool:
    """Whether Claude seeded a sidebar section list from its account settings.

    Such builds keep each account's sections in its own slot of
    `codeSidebarByScope` and load the account's server copy at startup. A slot
    written here survives only with Claude's pending-upload flag for that
    account, which makes Claude upload it instead.
    """
    if record is None:
        return False
    try:
        state = _decode_record(record, "dframe store").get("state")
    except LayoutError:
        return False
    scopes = state.get("codeSidebarByScope") if isinstance(state, dict) else None
    return isinstance(scopes, dict) and any(
        isinstance(scope, dict) and scope.get("prefsSeeded") is True
        for scope in scopes.values()
    )


def _encode_record(value: Mapping[str, Any]) -> bytes:
    return b"\x01" + json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def _valid_scope(scope: Any) -> bool:
    """Whether a value is one ACCOUNT/WORKSPACE key, as Claude writes them."""

    if not isinstance(scope, str):
        return False
    account, separator, workspace = scope.partition("/")
    return bool(
        account
        and separator
        and workspace
        and "/" not in workspace
        and not any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            or character == "|"
            for character in scope
        )
    )


def _manual_sections(slot: Any, label: str) -> list[Mapping[str, Any]]:
    """The custom sections of one account's sidebar slot, in sidebar order."""

    if not isinstance(slot, dict) or not isinstance(slot.get("sections"), list):
        raise LayoutError(f"{label} has an unknown shape")
    manual: list[Mapping[str, Any]] = []
    for section in slot["sections"]:
        if (
            not isinstance(section, dict)
            or not isinstance(section.get("id"), str)
            or not isinstance(section.get("kind"), str)
        ):
            raise LayoutError(f"{label} section has an unknown shape")
        if section["kind"] != "manual":
            continue
        if not isinstance(section.get("name"), str):
            raise LayoutError(f"{label} section name has an unknown shape")
        _string_list(section.get("members", []), f"{label} members")
        manual.append(section)
    return manual


def _groups_from_sections(manual: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The legacy group record Claude keeps beside the sections, rebuilt from them.

    Claude uploads its group list from this record and its sections from the
    slot, so both must name the same sections.
    """

    groups = []
    assignments: dict[str, str] = {}
    order = {}
    for section in manual:
        groups.append({"id": section["id"], "name": section["name"]})
        members = list(section.get("members", []))
        for member in members:
            assignments.setdefault(member, section["id"])
        if members:
            order[section["id"]] = members
    return {"groups": groups, "assignments": assignments, "order": order}


def copy_sidebar_slot(
    store_record: bytes, source: str, target: str
) -> tuple[bytes, int, int]:
    """Make TARGET's Code sidebar a copy of SOURCE's, inside Claude's own record.

    Returns the new record, the section count and the filed chat count.
    """

    if not (_valid_scope(source) and _valid_scope(target)) or source == target:
        raise LayoutError("sidebar copy needs two different account scopes")
    document = _decode_record(store_record, "dframe store record")
    state = document.get("state")
    if not isinstance(state, dict):
        raise LayoutError("dframe store record has an unknown shape")
    slots = state.get("codeSidebarByScope")
    groups = state.get("customGroupsByScope", {})
    if not isinstance(slots, dict) or not isinstance(groups, dict):
        raise LayoutError("sidebar scopes have an unknown shape")
    manual = _manual_sections(slots.get(source), "main account sidebar")
    if not manual:
        raise LayoutError("main account sidebar has no sections")
    slot = copy.deepcopy(slots[source])
    slot["prefsSeeded"] = True
    state["codeSidebarByScope"] = {**slots, target: slot}
    state["customGroupsByScope"] = {**groups, target: _groups_from_sections(manual)}
    filed = sum(len(section.get("members", [])) for section in manual)
    return _encode_record(document), len(manual), filed


@dataclass(frozen=True)
class AccountCopyPlan:
    """What a per-account sync does: which slot to copy, and the main account after."""

    main_scope: str | None
    source: str | None = None
    target: str | None = None
    # Why nothing is copied, when the user should know.
    reason: str | None = None


def plan_account_copy(
    state: Mapping[str, Any],
    *,
    owner_account: str | None,
    quarantined: bool,
    upload_flag: bytes | None,
    main_scope: str | None,
    requested_source: str | None,
    after_account_switch: bool,
) -> AccountCopyPlan:
    """Decide whether the signed-in account takes the main account's sidebar.

    Claude uploads a local slot only when its pending-upload flag names the
    signed-in account and that account owns the local settings, so every
    other case copies nothing. Possible edits are never overwritten: a slot
    with an upload still pending wins, and an account change without the
    switch restart waits for the next switch.
    """

    active = state.get("lastSidebarScopeKey")
    slots = state.get("codeSidebarByScope")
    if not _valid_scope(active) or not isinstance(slots, dict) or active not in slots:
        return AccountCopyPlan(main_scope, reason="signed-in-sidebar-not-loaded")
    assert isinstance(active, str)  # _valid_scope accepted it
    if owner_account != active.partition("/")[0]:
        return AccountCopyPlan(main_scope, reason="signed-in-account-not-confirmed")
    if quarantined:
        return AccountCopyPlan(main_scope, reason="claude-settings-sync-quarantined")
    if upload_flag is not None:
        scoped = b"\x01" + active.encode("utf-8")
        if upload_flag in (scoped, scoped + b"|migrate"):
            # The newest edits are this account's and not uploaded yet.
            return AccountCopyPlan(active, reason="signed-in-edit-pending")
        return AccountCopyPlan(main_scope, reason="another-upload-pending")
    if requested_source is not None:
        source = requested_source
    elif main_scope is None:
        return AccountCopyPlan(active)
    elif main_scope == active:
        return AccountCopyPlan(active)
    elif after_account_switch:
        source = main_scope
    else:
        return AccountCopyPlan(
            main_scope, reason="account-changed-without-switch-restart"
        )
    if source == active:
        return AccountCopyPlan(active)
    if not _valid_scope(source) or source not in slots:
        return AccountCopyPlan(active, reason="main-account-sidebar-missing")
    if not _manual_sections(slots[source], "main account sidebar"):
        # An empty sidebar is missing data, never a choice to delete every group.
        return AccountCopyPlan(active, reason="main-account-has-no-groups")
    _manual_sections(slots[active], "signed-in account sidebar")
    if slots[source].get("sections") == slots[active].get("sections"):
        return AccountCopyPlan(active)
    return AccountCopyPlan(active, source=source, target=active)


def _decode_main_scope(content: bytes | None) -> str | None:
    if content is None:
        return None
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LayoutError("main sidebar account file is malformed") from error
    if (
        not isinstance(document, dict)
        or document.get("version") != SNAPSHOT_VERSION
        or not _valid_scope(document.get("main_scope"))
    ):
        raise LayoutError("main sidebar account file has an unknown shape")
    return document["main_scope"]


def _encode_main_scope(scope: str) -> bytes:
    document = {"version": SNAPSHOT_VERSION, "main_scope": scope}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def same_recovery_payload(
    key: bytes,
    expected: bytes | None,
    current: bytes | None,
    *,
    plain_flag_consumed: bool = False,
) -> bool:
    """Ignore only known UI bookkeeping when deciding a no-write recovery.

    Claude rewrites wrapper timestamps and collapsedGroups on reopen, and
    consumes scoped migration markers. A per-account copy also counts its plain
    upload flag as consumed, because the copy wrote it for Claude to upload.
    All other fields must match, including unknown fields. Only the journal's whole-transaction phase check uses this
    comparison; it never authorizes replacing either record's bytes.
    """
    if expected == current:
        return True
    if key == GROUP_UPLOAD_KEY and current is None and expected is not None:
        # Claude clears its pending-upload flag once it has uploaded the slot.
        if not expected.startswith(b"\x01"):
            return False
        flag = expected[1:]
        if flag.endswith(b"|migrate"):
            flag = flag[: -len(b"|migrate")]
        elif not plain_flag_consumed:
            return False
        try:
            return _valid_scope(flag.decode("utf-8"))
        except UnicodeError:
            return False
    if expected is None or current is None:
        return False
    try:
        previous = _decode_record(expected, "recovery preimage")
        present = _decode_record(current, "recovery current record")
        for document in (previous, present):
            if key in (GROUP_SCOPES_KEY, LOCAL_SLICE_KEY):
                if (
                    type(document.get("timestamp")) is not int
                    or document["timestamp"] < 0
                ):
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
        return json.dumps(previous, sort_keys=True) == json.dumps(
            present, sort_keys=True
        )
    except LayoutError:
        return False


def _ordered_group_pairs(scope: Any) -> list[tuple[str, str]]:
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
    # Claude leaves assignments and order entries behind for groups that are
    # gone, and shows those chats as ungrouped. They are stale, not unknown.
    _string_map(scope.get("assignments", {}), "custom group assignments")
    order = scope.get("order", {})
    if not isinstance(order, dict) or any(
        not isinstance(group_id, str) for group_id in order
    ):
        raise LayoutError("custom group order has an unknown shape")
    for group_id, sessions in order.items():
        _string_list(sessions, "custom group session order")
    return pairs


def _live_assignments(scope: Mapping[str, Any]) -> dict[str, str]:
    """The chats filed under a group the scope still has; the rest are stale."""

    ids = {group_id for group_id, _name in _ordered_group_pairs(scope)}
    assignments = _string_map(scope.get("assignments", {}), "custom group assignments")
    return {
        session: group_id
        for session, group_id in assignments.items()
        if group_id in ids
    }


def _scope_order_by_name(scope: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Return the valid part of Chromium's stale-prone order index."""

    pairs = _ordered_group_pairs(scope)
    assignments = _live_assignments(scope)
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


def _validated_scopes(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise LayoutError("custom group scopes have an unknown shape")
    scopes = {}
    for scope_key, scope in value.items():
        if not isinstance(scope_key, str) or "/" not in scope_key:
            raise LayoutError("custom group scope key has an unknown shape")
        _ordered_group_pairs(scope)
        scopes[scope_key] = copy.deepcopy(scope)
    return scopes


def _load_snapshot_document(document: Any) -> LayoutSnapshot | None:
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


def _layout(scope: Mapping[str, Any]) -> dict[str, Any]:
    """A scope's groups, chat assignments, and group order by name, for comparison."""

    names = dict(_ordered_group_pairs(scope))
    assignments = _live_assignments(scope)
    return {
        "groups": list(names.values()),
        "assignments": {
            session: names[group] for session, group in sorted(assignments.items())
        },
        "order": {
            name: list(sessions)
            for name, sessions in _scope_order_by_name(scope).items()
        },
    }


def _upload_scope(marker: bytes | None) -> str | None:
    """The scope whose sidebar upload Claude has not consumed yet, if any."""

    if marker is None or not marker.startswith(b"\x01"):
        return None
    try:
        value = marker[1:].decode("utf-8")
    except UnicodeError:
        return None
    return value[: -len("|migrate")] if value.endswith("|migrate") else value


def _snapshot_bytes(path: Path) -> bytes | None:
    if not os.path.lexists(str(path)):
        return None
    try:
        return read_bounded(path, MAX_RECORD_BYTES, stable=True)
    except FileChangedError as error:
        raise LayoutError("sidebar snapshot changed while being read") from error
    except UnsafePathError as error:
        raise LayoutError("sidebar snapshot is not a regular bounded file") from error
    except OSError as error:
        raise LayoutError("sidebar snapshot is not a readable regular file") from error


def _decode_snapshot(content: bytes | None) -> LayoutSnapshot | None:
    if content is None:
        return None
    try:
        document = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LayoutError("sidebar snapshot is malformed") from error
    return _load_snapshot_document(document)


def load_snapshot(path: Path) -> LayoutSnapshot | None:
    return _decode_snapshot(_snapshot_bytes(path))


def _copy_authoritative_scope(
    source: Mapping[str, Any], existing: Mapping[str, Any]
) -> dict[str, Any]:
    """Copy one trusted layout while retaining unrelated target metadata."""

    _ordered_group_pairs(source)
    _ordered_group_pairs(existing)
    existing_groups = {group["name"]: group for group in existing.get("groups", [])}
    replacement = cast("dict[str, Any]", copy.deepcopy(existing))
    replacement_groups = []
    for source_group in source["groups"]:
        merged_group = copy.deepcopy(existing_groups.get(source_group["name"], {}))
        merged_group.update(copy.deepcopy(source_group))
        replacement_groups.append(merged_group)
    replacement["groups"] = replacement_groups
    replacement["assignments"] = _live_assignments(source)
    canonical_order = _scope_order_by_name(source)
    replacement["order"] = {
        group["id"]: list(canonical_order[group["name"]]) for group in source["groups"]
    }
    return replacement


def _adopt_current_sidebar_records(
    group_record: Mapping[str, Any],
    local_record: Mapping[str, Any],
    store_record: Mapping[str, Any],
    target_sessions: Mapping[str, set[str]],
    *,
    timestamp_ms: int | None,
    source_scope: str | None,
    active_scope: str | None,
    unconfirmed_copy: Mapping[str, Any] | None = None,
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
        raise LayoutError("current sidebar must belong to an approved sync target")
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
    _check_copies_agree(
        selected_scope, store_scopes, persisted_scopes, store_state, local_record
    )
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

    updated_store = cast("dict[str, Any]", copy.deepcopy(store_record))
    updated_group = cast("dict[str, Any]", copy.deepcopy(group_record))
    updated_local = cast("dict[str, Any]", copy.deepcopy(local_record))
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
        assignment_count=len(_live_assignments(source)),
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

    if source_scope in persisted_scopes and _layout(
        persisted_scopes[source_scope]
    ) != _layout(store_scopes[source_scope]):
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
    target_sessions: Mapping[str, set[str]],
    *,
    snapshot: LayoutSnapshot | None = None,
    timestamp_ms: int | None = None,
    adopt_current_sidebar: bool = False,
    adopt_source_scope: str | None = None,
    after_account_switch: bool = False,
    owner_account: str | None = None,
    upload_pending_scope: str | None = None,
) -> LayoutTransform:
    """Return exact allowlisted record replacements for one Claude data root.

    One account's sidebar is the source of truth at a time, and its groups
    are copied to the other accounts. The first sync adopts the account chosen
    at install or with keep-sidebar. Without a choice it adopts only when that
    overwrites no different groups. After that the signed-in account wins. Right after an account
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
                reason="no-groups-yet"
                if snapshot is None
                else "main-account-has-no-groups",
                snapshot=confirmed,
            )
        if (
            unconfirmed is not None
            and snapshot is not None
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
            unconfirmed
            if snapshot is not None and active == snapshot.adopted_scope
            else None
        ),
    )


def _signed_in_scope(
    store_state: Mapping[str, Any],
    target_sessions: Mapping[str, set[str]],
    owner_account: str | None,
) -> str | None:
    """The signed-in account's synced scope, or None when it does not sync yet."""

    active = store_state.get("lastSidebarScopeKey")
    if active is not None and not isinstance(active, str):
        raise LayoutError("last sidebar scope has an unknown shape")
    if owner_account is not None and (
        active is None or active.partition("/")[0] != owner_account
    ):
        # Claude has not shown the signed-in account's Code sidebar yet.
        owned = [
            key for key in target_sessions if key.partition("/")[0] == owner_account
        ]
        active = owned[0] if len(owned) == 1 else None
    return active if active in target_sessions else None


def sidebar_accounts(
    records: Mapping[bytes, bytes],
    target_sessions: Mapping[str, set[str]],
    owner_account: str | None,
) -> list[SidebarAccount]:
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
            group_names=tuple(
                name
                for _group_id, name in _ordered_group_pairs(
                    store_scopes.get(scope_key, {"groups": []})
                )
            ),
            pins=len(pins & sessions),
            chats=len(sessions),
        )
        for scope_key, sessions in target_sessions.items()
    ]
    return sorted(rows, key=lambda row: (not row.is_signed_in, row.scope))


def scope_sessions(targets: Iterable[Any]) -> dict[str, set[str]]:
    """Map each sync target's ACCOUNT/WORKSPACE to the chats in its folder."""

    scopes = {}
    for target in targets:
        sessions = set()
        for replica in target.path.iterdir():
            if replica.is_symlink() or not replica.is_file():
                continue
            if replica.name.startswith("local_") and replica.name.endswith(".json"):
                sessions.add(f"code:{replica.stem}")
        scopes[f"{target.account_id}/{target.workspace_id}"] = sessions
    return scopes


def _source_scope(
    active: str,
    snapshot: LayoutSnapshot | None,
    store_scopes: Mapping[str, Mapping[str, Any]],
    target_sessions: Mapping[str, set[str]],
    *,
    active_is_empty: bool,
    after_account_switch: bool,
) -> str | None:
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
    if not grouped:
        return None
    if groups_differ(
        [name for _group_id, name in _ordered_group_pairs(store_scopes[key])]
        for key in grouped
    ):
        raise LayoutChoiceError(NEEDS_MAIN_ACCOUNT)
    return active if active in grouped else grouped[0]


def groups_differ(group_names: Iterable[Iterable[str]]) -> bool:
    """Whether accounts with groups hold different ones, so only the user can pick.

    With no adopted account, sync copies on its own only when one account has
    groups or all hold the same ones, as installs from the union model do.
    """

    return len({frozenset(names) for names in group_names if names}) > 1


def _scope_is_empty(scope: Mapping[str, Any] | None) -> bool:
    """Whether a scope has no groups. What is left without groups is stale."""

    return scope is None or not _ordered_group_pairs(scope)


def _owner_account(value: bytes | None) -> str | None:
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
    reason: str | None = None,
    snapshot: LayoutSnapshot | None = None,
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
