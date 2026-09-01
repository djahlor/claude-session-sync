"""Immutable domain values shared by the synchronization core."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


@dataclass(frozen=True)
class Profile:
    name: str
    data_root: Path
    launch_command: Tuple[str, ...]
    is_default: bool = False


@dataclass(frozen=True)
class Target:
    profile_name: str
    account_id: str
    workspace_id: str
    path: Path


@dataclass(frozen=True)
class Replica:
    session_id: str
    target: Target
    path: Path
    size: int
    mtime_ns: int
    digest: str


@dataclass(frozen=True)
class Operation:
    kind: str
    session_id: str
    source: Path
    destination: Path
    source_digest: str
    destination_digest_or_none: Optional[str]
    size: int


@dataclass(frozen=True)
class Conflict:
    session_id: str
    reason: str
    replicas: Tuple[Replica, ...]


@dataclass(frozen=True)
class InvalidReplica:
    path: Path
    reason: str


@dataclass(frozen=True)
class Discovery:
    targets: Tuple[Target, ...]
    replicas: Tuple[Replica, ...]
    invalid_replicas: Tuple[InvalidReplica, ...]


@dataclass(frozen=True)
class Plan:
    version: int
    config_digest: str
    operations: Tuple[Operation, ...]
    conflicts: Tuple[Conflict, ...]
    invalid_replicas: Tuple[InvalidReplica, ...]
    plan_id: str
    total_bytes: int


@dataclass(frozen=True)
class SyncRequest:
    config: "Config"


@dataclass(frozen=True)
class RunReceipt:
    run_id: Optional[str]
    status: str
    plan_id: str
    operation_count: int
    bytes_copied: int


@dataclass(frozen=True)
class RecoveryReceipt:
    run_id: str
    status: str
    operation_count: int
    bytes_restored: int
