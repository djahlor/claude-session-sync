"""Immutable domain values shared by the synchronization core."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Tuple, TYPE_CHECKING

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
    # The chat's state without fields Claude rewrites per click or per account.
    # None means Claude would not accept the file, so the chat is left alone.
    state_hash: Optional[str] = None
    activity: int = 0
    future_dated: bool = False


@dataclass(frozen=True)
class Marker:
    """Claude's delete marker, deleted_<id>, holding the delete time in epoch ms."""

    session_id: str
    target: Target
    path: Path
    size: int
    mtime_ns: int
    digest: str
    deleted_at: int


@dataclass(frozen=True)
class TmpFile:
    """A local_<id>.json.tmp that Claude promotes to a record at startup."""

    session_id: str
    target: Target
    path: Path
    size: int
    mtime_ns: int
    digest: str


@dataclass(frozen=True)
class Operation:
    # create: new file, never overwriting; replace: swap an existing file;
    # retire: remove a file (its bytes stay in the run journal); copy: legacy
    # create-or-replace.
    kind: str
    session_id: str
    source: Path
    destination: Path
    source_digest: str
    destination_digest_or_none: Optional[str]
    size: int
    artifact: str = "record"  # record, marker, or tmp
    source_state_hash: Optional[str] = None


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
    markers: Tuple[Marker, ...] = ()
    tmps: Tuple[TmpFile, ...] = ()
    ignored_targets: Tuple[Target, ...] = ()


@dataclass(frozen=True)
class Plan:
    version: int
    config_digest: str
    operations: Tuple[Operation, ...]
    conflicts: Tuple[Conflict, ...]
    invalid_replicas: Tuple[InvalidReplica, ...]
    plan_id: str
    total_bytes: int
    # Sessions left alone on purpose (live, tied, lost, unreadable, future).
    # They never block the rest of the plan.
    problems: Tuple[Any, ...] = ()
    live_targets: Tuple[str, ...] = ()
    ignored_targets: int = 0
    context: Any = field(default=None, compare=False, repr=False)


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
    # Operations left for the next run because a file moved on or turned live.
    skipped_count: int = 0
    applied: Tuple[Operation, ...] = ()


@dataclass(frozen=True)
class RecoveryReceipt:
    run_id: str
    status: str
    operation_count: int
    bytes_restored: int
