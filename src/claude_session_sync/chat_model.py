"""Pure values for deciding chat sync, one session at a time.

The decision rules follow vinlim/claude-desktop-sync (0BSD). A partition key
names one account/workspace sidebar folder; nothing here touches the disk.
"""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Set, Union


@dataclass(frozen=True)
class Copy:
    """One partition's copy of a chat record, reduced to what a decision needs."""

    state_hash: Optional[str]  # None: unreadable, or not a record Claude would accept
    last_activity_at: int = 0
    future_dated: bool = False  # activity later than now cannot be ordered

    @property
    def readable(self) -> bool:
        return self.state_hash is not None

    @property
    def usable(self) -> bool:
        return self.readable and not self.future_dated


@dataclass(frozen=True)
class Snapshot:
    """What one partition holds at scan time."""

    key: str
    records: Mapping[str, Copy]
    tombstones: Mapping[str, int]  # session id -> delete time in epoch ms
    orphan_tmps: FrozenSet[str] = frozenset()


@dataclass
class SyncState:
    """What sync remembers between runs.

    ``synced`` holds, per folder, the version of each chat that folder last held
    in step with the others: at the last run where every folder agreed, or as
    placed there by sync. A folder with no entry is unknown, so its copy can
    never win just by looking changed.
    """

    synced: Dict[str, Dict[str, str]] = field(default_factory=dict)
    seen: Dict[str, Set[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class CreateRecord:
    session_id: str
    source: str
    target: str


@dataclass(frozen=True)
class ReplaceRecord:
    session_id: str
    source: str
    target: str
    keep: bool  # the replaced copy holds state found nowhere else


@dataclass(frozen=True)
class RetireRecord:
    session_id: str
    target: str


@dataclass(frozen=True)
class RetireTmp:
    session_id: str
    target: str


@dataclass(frozen=True)
class CreateTombstone:
    session_id: str
    source: str
    target: str


@dataclass(frozen=True)
class RetireTombstone:
    session_id: str
    target: str


Action = Union[
    CreateRecord,
    ReplaceRecord,
    RetireRecord,
    RetireTmp,
    CreateTombstone,
    RetireTombstone,
]


@dataclass(frozen=True)
class Problem:
    """A session left alone on purpose: live, tied, lost, unreadable or future."""

    kind: str
    session_id: str
    partition: str


@dataclass
class RulePlan:
    actions: List[Action] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
