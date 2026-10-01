"""Plan one chat sync run: which files to create, replace, or retire.

Discovery reads the chosen sidebar folders. The per-session decisions come
from rules.py (content, the last agreed version, then activity). Sessions the
rules leave alone are reported as problems and never block the rest.
"""

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import strict_json as json
from .chat_model import (
    Copy,
    CreateRecord,
    CreateTombstone,
    ReplaceRecord,
    RetireRecord,
    RetireTmp,
    RetireTombstone,
    Snapshot,
)
from .chat_state import ChatState, load_state, state_path
from .config import Config, ConfigError
from .enrollment import new_login_targets, select_targets, target_key
from .hash_cache import HashCache
from .logins import default_app_log, observe_logins
from .model import (
    Conflict,
    Discovery,
    InvalidReplica,
    Operation,
    Plan,
    Replica,
    SyncRequest,
    Target,
)
from .rules import plan as plan_rules
from .store import SessionStore


PLAN_VERSION = 2


@dataclass
class PlanContext:
    """What the run needs after planning: state, the folders, and a way to rescan."""

    state: ChatState
    targets: Dict[str, Target]
    snapshots: List[Snapshot]
    newly_enrolled: List[str] = field(default_factory=list)
    rescan: Optional[Callable[[], List[Snapshot]]] = None


def default_running_processes(config: Config) -> tuple:
    from .processes import managed_processes

    return managed_processes(
        config.profiles, executable=config.claude_executable, timeout=5.0
    )


class Planner:
    def __init__(
        self,
        store: Optional[SessionStore] = None,
        *,
        running_processes: Optional[Callable[[Config], Sequence[Any]]] = None,
        clock_ns: Callable[[], int] = time.time_ns,
        app_log: Optional[Path] = None,
        state: Optional[ChatState] = None,
    ) -> None:
        self._store = store
        self._running_processes = running_processes or default_running_processes
        self._clock_ns = clock_ns
        self._app_log = app_log
        self._state = state

    def plan(
        self,
        request: SyncRequest,
        *,
        state: Optional[ChatState] = None,
        prefer: Optional[str] = None,
        prefer_session: Optional[str] = None,
        running_processes: Optional[Callable[[Config], Sequence[Any]]] = None,
    ) -> Plan:
        config = request.config
        if running_processes is not None:
            self._running_processes = running_processes
        if len(config.profiles) > 1 and not config.acknowledge_cross_profile_copy:
            raise ConfigError(
                "acknowledge_cross_profile_copy must be true when multiple profiles are enabled"
            )
        state = state or self._state or load_state(state_path(config.state_dir))
        store, cache = self._open_store(config)
        try:
            return self._plan(config, store, state, prefer, prefer_session)
        finally:
            if cache is not None:
                cache.close()

    def _open_store(self, config: Config):
        if self._store is not None:
            return self._store, None
        cache = HashCache(config.state_dir / "hash-cache.sqlite3")
        return SessionStore(cache, clock_ns=self._clock_ns), cache

    def _plan(self, config, store, state, prefer, prefer_session) -> Plan:
        now_ms = self._clock_ns() // 1_000_000
        found = store.discover_targets(config)
        profiles = {profile.name: profile for profile in config.profiles}
        roots = {name: _normalized(profile.data_root) for name, profile in profiles.items()}
        default_roots = {
            roots[profile.name] for profile in config.profiles if profile.is_default
        }

        running = {
            _normalized(process.user_data_dir)
            for process in self._running_processes(config)
            if getattr(process, "user_data_dir", None) is not None
        }
        app_log = self._app_log or default_app_log()
        observe_logins(
            roots.values(),
            state.logins,
            now_ms,
            lambda root: app_log if root in default_roots else None,
        )

        selected, ignored = select_targets(config, found.targets, state.enrolled)
        newly = new_login_targets(
            config,
            found.targets,
            {target_key(target) for target in selected},
            running,
            state.logins,
        )
        if newly:
            state.enrolled = sorted(set(state.enrolled) | set(newly))
            selected, ignored = select_targets(config, found.targets, state.enrolled)

        invalid = list(found.invalid_replicas)
        if config.target_policy == "approved-only":
            invalid.extend(
                InvalidReplica(target.path, "target namespace is not approved")
                for target in ignored
            )
            ignored = ()
        account_namespaces = {
            (target.profile_name, target.account_id) for target in selected
        }
        if len(account_namespaces) > 1 and not config.acknowledge_cross_account_copy:
            raise ConfigError(
                "acknowledge_cross_account_copy must be true when discovery spans "
                "multiple profile/account namespaces"
            )

        scanned = store.scan_targets(selected)
        invalid.extend(scanned.invalid_replicas)
        targets = {target_key(target): target for target in selected}
        snapshots, index = _snapshots(targets, scanned)

        prefer_key = _resolve_prefer(prefer, targets)
        decided = plan_rules(snapshots, state.sync, prefer_key, prefer_session)
        operations = tuple(_operation(action, targets, index) for action in decided.actions)

        invalid_tuple = tuple(
            sorted(invalid, key=lambda item: _portable_path(item.path, config))
        )
        conflicts_tuple: Tuple[Conflict, ...] = ()
        config_digest = _config_digest(config)
        total_bytes = sum(
            operation.size for operation in operations if operation.kind != "retire"
        )
        identity = _plan_identity(
            config, config_digest, operations, conflicts_tuple, invalid_tuple, total_bytes
        )

        def rescan() -> List[Snapshot]:
            # The planning store's cache is closed by now; open a fresh one.
            fresh_store, fresh_cache = self._open_store(config)
            try:
                return _snapshots(targets, fresh_store.scan_targets(selected))[0]
            finally:
                if fresh_cache is not None:
                    fresh_cache.close()

        context = PlanContext(
            state=state,
            targets=targets,
            snapshots=snapshots,
            newly_enrolled=list(newly),
            rescan=rescan,
        )
        return Plan(
            PLAN_VERSION,
            config_digest,
            operations,
            conflicts_tuple,
            invalid_tuple,
            hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest(),
            total_bytes,
            problems=tuple(decided.problems),
            ignored_targets=len(ignored),
            context=context,
        )


def _snapshots(targets: Mapping[str, Target], scanned: Discovery):
    by_target: Dict[str, Dict[str, Dict[str, Any]]] = {
        key: {"records": {}, "markers": {}, "tmps": {}} for key in targets
    }
    for replica in scanned.replicas:
        by_target[target_key(replica.target)]["records"][replica.session_id] = replica
    for marker in scanned.markers:
        by_target[target_key(marker.target)]["markers"][marker.session_id] = marker
    for tmp in scanned.tmps:
        by_target[target_key(tmp.target)]["tmps"][tmp.session_id] = tmp
    snapshots = []
    for key in sorted(targets):
        entry = by_target[key]
        records = {
            session_id: Copy(replica.state_hash, replica.activity, replica.future_dated)
            for session_id, replica in entry["records"].items()
        }
        tombstones = {
            session_id: marker.deleted_at for session_id, marker in entry["markers"].items()
        }
        orphans = frozenset(
            session_id for session_id in entry["tmps"] if session_id not in records
        )
        snapshots.append(Snapshot(key, records, tombstones, orphans))
    return snapshots, by_target


def _operation(action, targets: Mapping[str, Target], index) -> Operation:
    session_id = action.session_id
    if isinstance(action, CreateRecord):
        source = index[action.source]["records"][session_id]
        destination = targets[action.target].path / "local_{}.json".format(session_id)
        return Operation(
            "create", session_id, source.path, destination, source.digest, None,
            source.size, "record", source.state_hash,
        )
    if isinstance(action, ReplaceRecord):
        source = index[action.source]["records"][session_id]
        held = index[action.target]["records"][session_id]
        return Operation(
            "replace", session_id, source.path, held.path, source.digest, held.digest,
            source.size, "record", source.state_hash,
        )
    if isinstance(action, CreateTombstone):
        source = index[action.source]["markers"][session_id]
        destination = targets[action.target].path / "deleted_{}".format(session_id)
        return Operation(
            "create", session_id, source.path, destination, source.digest, None,
            source.size, "marker",
        )
    if isinstance(action, RetireRecord):
        held = index[action.target]["records"][session_id]
        artifact = "record"
    elif isinstance(action, RetireTmp):
        held = index[action.target]["tmps"][session_id]
        artifact = "tmp"
    elif isinstance(action, RetireTombstone):
        held = index[action.target]["markers"][session_id]
        artifact = "marker"
    else:  # pragma: no cover - the rules emit only the kinds above
        raise ValueError("unknown action: {!r}".format(action))
    return Operation(
        "retire", session_id, held.path, held.path, held.digest, held.digest,
        held.size, artifact,
    )


def _resolve_prefer(prefer: Optional[str], targets: Mapping[str, Target]) -> Optional[str]:
    if prefer is None:
        return None
    for key, target in targets.items():
        short = "{}/{}".format(target.account_id[:8], target.workspace_id[:8])
        if prefer in (key, short, "{}/{}".format(target.account_id, target.workspace_id)):
            return key
    raise ValueError("--prefer names no synced sidebar folder")


def _normalized(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _config_digest(config: Config) -> str:
    profiles = [
        {
            "name": profile.name,
            "launch_command": list(profile.launch_command),
            "is_default": profile.is_default,
        }
        for profile in sorted(config.profiles, key=lambda item: item.name)
    ]
    portable_config = {
        "profiles": profiles,
        "retention": config.retention,
        "acknowledge_cross_profile_copy": config.acknowledge_cross_profile_copy,
        "acknowledge_cross_account_copy": config.acknowledge_cross_account_copy,
        "claude_executable": config.claude_executable.name,
        "approved_targets": [
            [target.profile_name, target.account_id, target.workspace_id]
            for target in sorted(
                config.approved_targets,
                key=lambda item: (
                    item.profile_name,
                    item.account_id,
                    item.workspace_id,
                ),
            )
        ],
    }
    return hashlib.sha256(_canonical_json(portable_config).encode("utf-8")).hexdigest()


def _plan_identity(
    config: Config,
    config_digest: str,
    operations: Sequence[Operation],
    conflicts: Sequence[Conflict],
    invalid_replicas: Sequence[InvalidReplica],
    total_bytes: int,
) -> Mapping[str, Any]:
    return {
        "version": PLAN_VERSION,
        "config_digest": config_digest,
        "operations": [
            {
                "kind": operation.kind,
                "artifact": operation.artifact,
                "session_id": operation.session_id,
                "source": _portable_path(operation.source, config),
                "destination": _portable_path(operation.destination, config),
                "source_digest": operation.source_digest,
                "destination_digest": operation.destination_digest_or_none,
                "size": operation.size,
            }
            for operation in operations
        ],
        "conflicts": [
            {
                "session_id": conflict.session_id,
                "reason": conflict.reason,
                "replicas": [
                    {
                        "path": _portable_path(replica.path, config),
                        "digest": replica.digest,
                        "size": replica.size,
                    }
                    for replica in conflict.replicas
                ],
            }
            for conflict in conflicts
        ],
        "invalid_replicas": [
            {
                "path": _portable_path(invalid.path, config),
                "reason": _portable_reason(invalid.reason, config),
            }
            for invalid in invalid_replicas
        ],
        "total_bytes": total_bytes,
    }


def _portable_path(path: Path, config: Config) -> str:
    candidates = sorted(
        config.profiles, key=lambda profile: len(str(profile.data_root)), reverse=True
    )
    for profile in candidates:
        try:
            relative = path.relative_to(profile.data_root)
        except ValueError:
            continue
        return "{}/{}".format(profile.name, relative.as_posix())
    return "<external>/{}".format(path.name)


def _portable_reason(reason: str, config: Config) -> str:
    portable = reason
    for profile in config.profiles:
        portable = portable.replace(str(profile.data_root), "<{}>".format(profile.name))
    portable = portable.replace(str(config.state_dir), "<state-dir>")
    return portable


def _target_key(target: Target) -> Tuple[str, str, str]:
    return target.profile_name, target.account_id, target.workspace_id


def _replica_key(replica: Replica) -> Tuple[str, str, str, str]:
    return (
        replica.target.profile_name,
        replica.target.account_id,
        replica.target.workspace_id,
        replica.session_id,
    )
