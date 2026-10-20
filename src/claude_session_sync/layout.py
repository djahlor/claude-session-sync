"""Selective synchronization for Claude Desktop sidebar layout records."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import (
    Any,
    Callable,
)

from . import strict_json as json
from .config import Config
from .filesystem import (
    atomic_write_bytes,
    durable_unlink,
    ensure_private_directory,
)
from .layout_db import LevelDatabase
from .layout_records import (  # noqa: F401  the module's public names stay importable here
    ACCOUNT_JOURNAL_DIRNAME,
    ADOPT_PENDING_FILENAME,
    COPY_NOT_UPLOADED,
    DFRAME_STORE_KEY,
    GROUP_SCOPES_KEY,
    GROUP_UPLOAD_KEY,
    LAYOUT_KEYS,
    LOCAL_SLICE_KEY,
    MAIN_SCOPE_FILENAME,
    MAX_RECORD_BYTES,
    NEEDS_MAIN_ACCOUNT,
    ORIGIN_PREFIX,
    PER_ACCOUNT,
    PER_ACCOUNT_REASON,
    SNAPSHOT_VERSION,
    SYNC_ACTIVE_KEY,
    SYNC_METADATA_KEYS,
    SYNC_OWNER_KEY,
    SYNC_QUARANTINE_KEY,
    UPLOAD_ON_NEXT_START,
    AccountCopyPlan,
    LayoutBusyError,
    LayoutChoiceError,
    LayoutDisagreementError,
    LayoutError,
    LayoutReceipt,
    LayoutRecoveryError,
    LayoutSnapshot,
    LayoutTransform,
    SidebarAccount,
    _adopt_current_sidebar_records,
    _check_copies_agree,
    _copy_authoritative_scope,
    _decode_main_scope,
    _decode_record,
    _decode_snapshot,
    _encode_main_scope,
    _encode_record,
    _groups_from_sections,
    _layout,
    _live_assignments,
    _load_snapshot_document,
    _manual_sections,
    _ordered_group_pairs,
    _owner_account,
    _scope_is_empty,
    _scope_order_by_name,
    _signed_in_scope,
    _snapshot_bytes,
    _source_scope,
    _string_list,
    _string_map,
    _unchanged,
    _upload_scope,
    _valid_scope,
    _validated_scopes,
    copy_sidebar_slot,
    groups_differ,
    keeps_sidebar_on_server,
    load_snapshot,
    plan_account_copy,
    same_recovery_payload,
    scope_sessions,
    sidebar_accounts,
    transform_layout_records,
)
from .locking import ExclusiveFileLock, LockUnavailableError
from .processes import managed_processes
from .record_journal import (
    RecordJournal,
    RecordJournalError,
    RecordRecoveryError,
)
from .store import SessionStore

# Why a main-account choice cannot apply, with the words to tell the user.
MAIN_ACCOUNT_BLOCKERS = {
    "sidebar-sync-off": "pins and groups do not sync, so there is no main account",
    "needs-one-default-profile": (
        "a main account works only with exactly one Claude profile enabled, the default one"
    ),
}


def main_account_blocker(config: Config) -> str | None:
    """Why sync would refuse a main-account choice for this config, or None.

    The install question, keep-sidebar, and sync all ask this, so a choice is
    offered and recorded only where sync applies it.
    """

    if not config.sync_sidebar_layout:
        return "sidebar-sync-off"
    if len(config.profiles) != 1 or not config.profiles[0].is_default:
        return "needs-one-default-profile"
    return None


def request_adoption(state_dir: Path, source_scope: str) -> None:
    """Make one account's pins and groups the source of truth at the next closed-Claude sync."""

    account, separator, workspace = source_scope.partition("/")
    if not account or not separator or not workspace or "/" in workspace:
        raise LayoutError("sidebar scope must be ACCOUNT/WORKSPACE")
    ensure_private_directory(state_dir)
    path = Path(state_dir) / ADOPT_PENDING_FILENAME
    atomic_write_bytes(
        path,
        (json.dumps({"version": 1, "source_scope": source_scope}) + "\n").encode(
            "utf-8"
        ),
    )
    os.chmod(str(path), 0o600)


def read_pending_adoption(state_dir: Path) -> str | None:
    content = _snapshot_bytes(Path(state_dir) / ADOPT_PENDING_FILENAME)
    if content is None:
        return None
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LayoutError("pending sidebar adoption is malformed") from error
    scope = document.get("source_scope") if isinstance(document, dict) else None
    if (
        document.get("version") != 1
        or not isinstance(scope, str)
        or scope.count("/") != 1
    ):
        raise LayoutError("pending sidebar adoption is malformed")
    return scope


def clear_pending_adoption(state_dir: Path) -> None:
    path = Path(state_dir) / ADOPT_PENDING_FILENAME
    if os.path.lexists(str(path)):
        durable_unlink(path)


class LayoutSynchronizer:
    """Synchronize allowlisted sidebar records after Claude has terminated."""

    def __init__(
        self,
        config: Config,
        *,
        helper: Path | None = None,
        process_probe: Callable[[], Any] | None = None,
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
        canonical_upload_scope: str | None = None,
    ) -> tuple[bytes | None, bytes] | None:
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
        old_persisted = _decode_record(current[GROUP_SCOPES_KEY], "group scopes")[
            "value"
        ]
        new_persisted = _decode_record(replacements[GROUP_SCOPES_KEY], "group scopes")[
            "value"
        ]
        changed_scopes = set()
        for old, new in ((old_scopes, new_scopes), (old_persisted, new_persisted)):
            changed_scopes.update(
                scope
                for scope in set(old) | set(new)
                if old.get(scope) != new.get(scope)
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
            raise LayoutError(
                "account sidebar sync is quarantined; sign in again before syncing"
            )
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
            raise LayoutError(
                "account sidebar identity does not match the active scope"
            )
        pending = metadata[GROUP_UPLOAD_KEY]
        scoped = b"\x01" + active.encode("utf-8")
        if pending not in (None, scoped, scoped + b"|migrate"):
            raise LayoutError("another account sidebar update is pending")
        if pending == scoped and active in changed_scopes:
            raise LayoutBusyError(
                "account sidebar user edit is pending; reopen Claude to upload it before syncing"
            )
        old_groups = old_scopes.get(active, {}).get("groups", [])
        new_groups = new_scopes.get(active, {}).get("groups", [])
        if (
            canonical_upload_scope is None
            or not settings_sync_active
            or old_groups == new_groups
        ):
            return None
        if canonical_upload_scope != active:
            raise LayoutError(
                "canonical sidebar upload does not match the active scope"
            )
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

    def _keeps_sidebar_on_server(self) -> bool:
        for profile in self.config.profiles:
            self._assert_stopped()
            with self._database_copy(profile.data_root) as database:
                if keeps_sidebar_on_server(database.get_optional(DFRAME_STORE_KEY)):
                    return True
        return False

    def keeps_sidebar_per_account(self) -> bool:
        """Whether sync copies one account's whole sidebar slot into another.

        That copy needs nothing from the chat sync, so a switch can run it
        first, in the moment right after Claude quits. Reads disposable copies
        only and raises LayoutBusyError while Claude runs.
        """
        if not self.config.sync_sidebar_layout:
            return False
        return self._keeps_sidebar_on_server()

    def _account_journal_root(self) -> Path:
        return self.config.state_dir / ACCOUNT_JOURNAL_DIRNAME

    def _main_scope_path(self) -> Path:
        return self.config.state_dir / MAIN_SCOPE_FILENAME.format(0)

    def _plan_per_account(
        self,
        database: LevelDatabase,
        *,
        requested_source: str | None,
        adopt_current_sidebar: bool,
        after_account_switch: bool,
    ) -> tuple[dict[bytes, bytes | None], AccountCopyPlan, bytes | None]:
        """Read the default profile's sidebar and plan a per-account copy.

        Returns the records read, the plan and the main-account file's bytes.
        """
        current: dict[bytes, bytes | None] = {}
        for key in (DFRAME_STORE_KEY, GROUP_UPLOAD_KEY):
            self._assert_stopped()
            current[key] = database.get_optional(key)
        if current[DFRAME_STORE_KEY] is None:
            raise LayoutError("required sidebar record is missing")
        self._assert_stopped()
        owner = _owner_account(database.get_optional(SYNC_OWNER_KEY))
        self._assert_stopped()
        quarantined = database.get_optional(SYNC_QUARANTINE_KEY) is not None
        state = _decode_record(current[DFRAME_STORE_KEY], "dframe store record").get(
            "state"
        )
        if not isinstance(state, dict):
            raise LayoutError("dframe store record has an unknown shape")
        main_before = _snapshot_bytes(self._main_scope_path())
        if adopt_current_sidebar:
            requested_source = state.get("lastSidebarScopeKey")
        plan = plan_account_copy(
            state,
            owner_account=owner,
            quarantined=quarantined,
            upload_flag=current[GROUP_UPLOAD_KEY],
            main_scope=_decode_main_scope(main_before),
            requested_source=requested_source,
            after_account_switch=after_account_switch,
        )
        return current, plan, main_before

    def _probe_per_account(self) -> dict[str, Any]:
        result: dict[str, Any] = {"state": PER_ACCOUNT, "reason": PER_ACCOUNT_REASON}
        if main_account_blocker(self.config) is not None:
            return result
        requested, _pending = self._requested_source(False, None)
        with self._database_copy(self.config.profiles[0].data_root) as database:
            current, plan, _main = self._plan_per_account(
                database,
                requested_source=requested,
                adopt_current_sidebar=False,
                after_account_switch=False,
            )
        if plan.source is not None:
            _record, groups, filed = copy_sidebar_slot(
                current[DFRAME_STORE_KEY], plan.source, plan.target
            )
            result.update(
                {
                    "reason": UPLOAD_ON_NEXT_START,
                    "group_count": groups,
                    "assignment_count": filed,
                }
            )
        elif plan.reason is not None:
            result["reason"] = plan.reason
        return result

    def _sync_per_account(
        self,
        *,
        adopt_current_sidebar: bool,
        adopt_source_scope: str | None,
        pending_choice: bool,
        after_account_switch: bool,
    ) -> LayoutReceipt:
        """Copy the main account's sidebar into the signed-in account's slot.

        Claude uploads the slot as that account's own sidebar at its next
        start, because the copy sets Claude's pending-upload flag for exactly
        that account. Nothing else in Claude's settings sync is touched.
        """
        profiles = len(self.config.profiles)
        if main_account_blocker(self.config) is not None:
            return LayoutReceipt(PER_ACCOUNT, profiles, 0, 0, 0, 0, PER_ACCOUNT_REASON)
        self._recover_pending(self._account_journal_root())
        database_path = self.config.profiles[0].data_root / "Local Storage" / "leveldb"
        self._check_database_path(database_path)
        database = LevelDatabase(self.helper, database_path)
        current, plan, main_before = self._plan_per_account(
            database,
            requested_source=adopt_source_scope,
            adopt_current_sidebar=adopt_current_sidebar,
            after_account_switch=after_account_switch,
        )
        replacements: dict[bytes, bytes] = {}
        groups = filed = 0
        if plan.source is not None:
            record, groups, filed = copy_sidebar_slot(
                current[DFRAME_STORE_KEY], plan.source, plan.target
            )
            replacements = {
                DFRAME_STORE_KEY: record,
                GROUP_UPLOAD_KEY: b"\x01" + plan.target.encode("utf-8"),
            }
        main_after = main_before
        if plan.main_scope is not None:
            main_after = _encode_main_scope(plan.main_scope)
        if replacements or main_after != main_before:
            self._commit(
                database,
                current,
                replacements,
                snapshot_path=self._main_scope_path(),
                snapshot_before=main_before,
                snapshot_after=main_after,
                root=self._account_journal_root(),
            )
        if pending_choice:
            clear_pending_adoption(self.config.state_dir)
        reason = (
            UPLOAD_ON_NEXT_START if replacements else plan.reason or PER_ACCOUNT_REASON
        )
        return LayoutReceipt(
            PER_ACCOUNT, profiles, len(replacements), groups, filed, 0, reason
        )

    def accounts(self, target_sessions: Mapping[str, set[str]]) -> list[SidebarAccount]:
        """The default profile's accounts as rows. Works while Claude is open."""

        blocker = main_account_blocker(self.config)
        if blocker is not None:
            raise LayoutError(MAIN_ACCOUNT_BLOCKERS[blocker])
        with self._database_copy(self.config.profiles[0].data_root) as database:
            records = {key: database.get(key) for key in LAYOUT_KEYS}
            owner = _owner_account(database.get_optional(SYNC_OWNER_KEY))
        return sidebar_accounts(records, target_sessions, owner)

    def _requested_source(
        self, adopt_current_sidebar: bool, adopt_source_scope: str | None
    ) -> tuple[str | None, bool]:
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
        blocker = main_account_blocker(self.config)
        if (
            adopt_current_sidebar or adopt_source_scope is not None
        ) and blocker is not None:
            raise LayoutError(MAIN_ACCOUNT_BLOCKERS[blocker])
        return adopt_source_scope, pending

    def _plan_profile(
        self,
        database: LevelDatabase,
        target_sessions: Mapping[str, set[str]],
        snapshot: LayoutSnapshot | None,
        *,
        adopt_current_sidebar: bool,
        adopt_source_scope: str | None,
        after_account_switch: bool,
    ) -> tuple[dict[bytes, bytes | None], dict[bytes, bytes], LayoutTransform]:
        """Read one profile's sidebar and plan the records to write.

        Returns the records read, the records planned, and the transform.
        Sync runs this on the live database and doctor on a disposable copy.
        """
        current: dict[bytes, bytes | None] = {}
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

    def probe(self) -> dict[str, Any]:
        """Plan the next closed sync on disposable copies, without opening live DBs.

        It takes the inputs `auto` takes: the signed-in owner account and a
        pending choice, with no account-switch mode. A switch restart syncs
        at once, so the sync doctor checks is the one when Claude closes.
        """
        if not self.config.sync_sidebar_layout:
            return {"state": "disabled"}
        self._assert_stopped()
        if self._keeps_sidebar_on_server():
            return self._probe_per_account()
        adopt_source_scope, _pending = self._requested_source(False, None)
        profiles = groups = pins = assignments = 0
        reasons = []
        for index, profile in enumerate(self.config.profiles):
            targets = self._target_sessions(profile.name, profile.data_root)
            snapshot = load_snapshot(
                self.config.state_dir / f"sidebar-layout-{index}.json"
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
        result: dict[str, Any] = {
            "state": "compatible",
            "profile_count": profiles,
            "group_count": groups,
            "pin_count": pins,
            "assignment_count": assignments,
        }
        if reasons:
            result["reason"] = reasons[0]
        return result

    def sync(
        self,
        *,
        adopt_current_sidebar: bool = False,
        adopt_source_scope: str | None = None,
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
            if self._keeps_sidebar_on_server():
                # Older run journals stay on disk untouched; this mode keeps its own.
                return self._sync_per_account(
                    adopt_current_sidebar=adopt_current_sidebar,
                    adopt_source_scope=adopt_source_scope,
                    pending_choice=pending,
                    after_account_switch=after_account_switch,
                )
            self._recover_pending()
            totals = [0, 0, 0, 0]
            changed_profiles = 0
            reasons = []
            for index, profile in enumerate(self.config.profiles):
                database_path = profile.data_root / "Local Storage" / "leveldb"
                self._check_database_path(database_path)
                target_sessions = self._target_sessions(profile.name, profile.data_root)
                database = LevelDatabase(self.helper, database_path)
                snapshot_path = self.config.state_dir / f"sidebar-layout-{index}.json"
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
                        json.dumps(
                            transformed.snapshot.as_dict(), indent=2, sort_keys=True
                        )
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
                ",".join(dict.fromkeys(reasons)) or None,
            )
        finally:
            lock.release()

    def _target_sessions(
        self, profile_name: str, data_root: Path
    ) -> dict[str, set[str]]:
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

    def _parse_target(self, target: str) -> tuple[Path, bytes | None]:
        try:
            value = json.loads(target)
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("invalid sidebar target")
            database, encoded_key = value
            if database == "snapshot":
                path = Path(encoded_key)
                allowed = {
                    self.config.state_dir / name.format(index)
                    for name in ("sidebar-layout-{}.json", MAIN_SCOPE_FILENAME)
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

    def _journal(self, root: Path | None = None) -> RecordJournal:
        per_account = root == self._account_journal_root()

        def recovery_equivalent(
            target: str, expected: bytes | None, current: bytes | None
        ) -> bool:
            _path, key = self._parse_target(target)
            if key is None:
                return expected == current
            return same_recovery_payload(
                key, expected, current, plain_flag_consumed=per_account
            )

        def read(target: str) -> bytes | None:
            path, key = self._parse_target(target)
            if key is None:
                return _snapshot_bytes(path)
            self._assert_stopped()  # Opening LevelDB can itself mutate its files.
            database = LevelDatabase(self.helper, path)
            return (
                database.get_optional(key)
                if key == GROUP_UPLOAD_KEY
                else database.get(key)
            )

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
                LevelDatabase(self.helper, path).write(
                    {key: None}, self.config.state_dir
                )
            else:
                durable_unlink(path)

        return RecordJournal(
            root or self._journal_root(),
            read=read,
            write=write,
            delete=delete,
            validate=self._parse_target,
            before_mutation=self._assert_stopped,
            retention=self.config.retention,
            recovery_equivalent=recovery_equivalent,
        )

    def _recover_pending(self, root: Path | None = None) -> None:
        self._assert_stopped()
        try:
            self._journal(root).recover()
        except RecordJournalError as error:
            raise LayoutRecoveryError(str(error)) from error

    def _commit(
        self,
        database: LevelDatabase,
        current: Mapping[bytes, bytes | None],
        replacements: Mapping[bytes, bytes],
        *,
        snapshot_path: Path | None = None,
        snapshot_before: bytes | None = None,
        snapshot_after: bytes | None = None,
        root: Path | None = None,
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
            self._journal(root).commit(before, after)
        except RecordRecoveryError as error:
            raise LayoutRecoveryError(str(error)) from error
        except RecordJournalError as error:
            raise LayoutError(str(error)) from error
