"""Pure synchronization planning over validated store discovery."""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .config import Config, ConfigError
from .hash_cache import HashCache
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
from .store import SessionStore


PLAN_VERSION = 1


class Planner:
    def __init__(self, store: Optional[SessionStore] = None) -> None:
        self._store = store

    def plan(self, request: SyncRequest) -> Plan:
        config = request.config
        if len(config.profiles) > 1 and not config.acknowledge_cross_profile_copy:
            raise ConfigError(
                "acknowledge_cross_profile_copy must be true when multiple profiles are enabled"
            )
        discovery = self._discover(config)
        approved_targets = {
            (target.profile_name, target.account_id, target.workspace_id)
            for target in config.approved_targets
        }
        unapproved = ()
        if config.target_policy == "approved-only":
            unapproved = tuple(
                InvalidReplica(target.path, "target namespace is not approved")
                for target in discovery.targets
                if (target.profile_name, target.account_id, target.workspace_id)
                not in approved_targets
            )
        discovery = Discovery(
            discovery.targets,
            discovery.replicas,
            discovery.invalid_replicas + unapproved,
        )
        account_namespaces = {
            (target.profile_name, target.account_id) for target in discovery.targets
        }
        if len(account_namespaces) > 1 and not config.acknowledge_cross_account_copy:
            raise ConfigError(
                "acknowledge_cross_account_copy must be true when discovery spans "
                "multiple profile/account namespaces"
            )

        grouped: Dict[str, List[Replica]] = {}
        for replica in discovery.replicas:
            grouped.setdefault(replica.session_id, []).append(replica)

        operations: List[Operation] = []
        conflicts: List[Conflict] = []
        targets = tuple(sorted(discovery.targets, key=_target_key))
        for session_id in sorted(grouped):
            replicas = tuple(sorted(grouped[session_id], key=_replica_key))
            maximum_mtime = max(replica.mtime_ns for replica in replicas)
            maximum_replicas = tuple(
                replica for replica in replicas if replica.mtime_ns == maximum_mtime
            )
            maximum_digests = {replica.digest for replica in maximum_replicas}
            if len(maximum_digests) > 1:
                conflicts.append(
                    Conflict(
                        session_id,
                        "equal maximum mtime has divergent hashes",
                        replicas,
                    )
                )
                continue

            winner_digest = maximum_replicas[0].digest
            source = min(
                (replica for replica in replicas if replica.digest == winner_digest),
                key=_replica_key,
            )
            by_target = {replica.target: replica for replica in replicas}
            for target in targets:
                destination_replica = by_target.get(target)
                if (
                    destination_replica is not None
                    and destination_replica.digest == winner_digest
                ):
                    continue
                operations.append(
                    Operation(
                        "copy",
                        session_id,
                        source.path,
                        target.path / "local_{}.json".format(session_id),
                        winner_digest,
                        (
                            None
                            if destination_replica is None
                            else destination_replica.digest
                        ),
                        source.size,
                    )
                )

        operations_tuple = tuple(
            sorted(
                operations,
                key=lambda operation: (
                    operation.session_id,
                    _portable_path(operation.destination, config),
                    _portable_path(operation.source, config),
                ),
            )
        )
        conflicts_tuple = tuple(
            sorted(conflicts, key=lambda conflict: conflict.session_id)
        )
        invalid_tuple = tuple(
            sorted(
                discovery.invalid_replicas,
                key=lambda item: _portable_path(item.path, config),
            )
        )
        config_digest = _config_digest(config)
        total_bytes = sum(operation.size for operation in operations_tuple)
        identity = _plan_identity(
            config,
            config_digest,
            operations_tuple,
            conflicts_tuple,
            invalid_tuple,
            total_bytes,
        )
        return Plan(
            PLAN_VERSION,
            config_digest,
            operations_tuple,
            conflicts_tuple,
            invalid_tuple,
            hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest(),
            total_bytes,
        )

    def _discover(self, config: Config) -> Discovery:
        if self._store is not None:
            return self._store.discover(config)
        cache = HashCache(config.state_dir / "hash-cache.sqlite3")
        try:
            return SessionStore(cache).discover(config)
        finally:
            cache.close()


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
