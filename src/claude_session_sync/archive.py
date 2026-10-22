"""Keep a chat archived, or open, in every account folder.

Claude archives and unarchives a chat in the folder of the account signed in at
the time, without moving lastActivityAt. The chat sync then sees copies that
differ at the same activity time, a tie, and it breaks a tie only when one copy
holds everything the others hold. On 22-10-2026 the archived copy was behind
on a title, a published page or a git folder for 24 chats, so their archive
never reached the other accounts and they showed again after a switch.

So the archive state is settled on its own, before the chat sync plans:

- Each run remembers every copy's flag. A copy whose flag changed since then
  holds a fresh archive or unarchive, dated by the save that carried it.
- Using a chat counts as opening it, as in Claude, which unarchives a chat that
  gets a message.
- The newest of these wins. With nothing remembered yet, archived wins unless
  the chat was used after the archived copy was saved.

The other copies get the winner's archive fields and nothing else, through the
journaled transaction engine. A copy that was still as last synced is
remembered as still synced, so the chat sync decides as it would have without
the archive difference.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import strict_json as json
from .filesystem import (
    UnsafePathError,
    atomic_write_bytes,
    ensure_private_directory,
    open_regular_read,
    read_bounded,
)
from .fingerprint import fingerprint
from .model import Operation, Plan

LEDGER_FILENAME = "chat-archive-state.json"
LEDGER_VERSION = 1
STAGE_DIRNAME = "archive-stage"
# Claude writes isArchived among the first keys, under 8 KB in on 22-10-2026.
HEAD_BYTES = 64 * 1024
MAX_RECORD_BYTES = 64 * 1024 * 1024
FLAG = re.compile(rb'"isArchived"\s*:\s*(true|false)')
TOKEN = re.compile(rb'"(?:[^"\\]|\\.)*"|[{}\[\]]')


class ArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArchiveCopy:
    folder: str
    path: Path
    raw: bytes
    record: Mapping[str, Any]
    saved_ms: int
    # The flag this folder held at the end of the last run, None if never seen.
    known: bool | None

    @property
    def archived(self) -> bool:
        return self.record.get("isArchived") is True


@dataclass(frozen=True)
class ArchiveReport:
    archived: int = 0
    opened: int = 0
    skipped: int = 0

    @property
    def copies(self) -> int:
        return self.archived + self.opened


def decide(copies: Sequence[ArchiveCopy]) -> ArchiveCopy | None:
    """The copy whose archive state every copy should take, or None to leave the chat alone."""

    if len({copy.archived for copy in copies}) < 2:
        return None
    fresh = {
        copy.folder
        for copy in copies
        if copy.known is not None and copy.known != copy.archived
    }

    def when(copy: ArchiveCopy) -> int:
        if copy.archived:
            # Next to a fresh change, a copy that was already archived is old news.
            return copy.saved_ms if not fresh or copy.folder in fresh else -1
        used = max(
            _ms(copy.record.get("lastActivityAt")), _ms(copy.record.get("resumedAt"))
        )
        return max(used, copy.saved_ms) if copy.folder in fresh else used

    newest = max(when(copy) for copy in copies)
    leaders = [copy for copy in copies if when(copy) == newest]
    if newest < 0 or len({copy.archived for copy in leaders}) > 1:
        return None
    return max(leaders, key=lambda copy: (copy.saved_ms, copy.folder))


def archive_fields(
    record: Mapping[str, Any], winner: Mapping[str, Any]
) -> dict[str, Any]:
    """record with the winner's archive state. Nothing is removed.

    An archive brings the kept-worktree marks Claude set when it archived, so
    no account forgets that a worktree with changes was kept. An unarchive
    brings autoArchiveExempt, which stops Claude archiving the chat again by
    itself.
    """

    result = dict(record)
    result["isArchived"] = winner.get("isArchived") is True
    if result["isArchived"]:
        if (
            winner.get("keptDirtyWorktree") is True
            and record.get("keptDirtyWorktree") is not True
        ):
            result["keptDirtyWorktree"] = True
            if "keptDirtyAt" in winner:
                result["keptDirtyAt"] = winner["keptDirtyAt"]
    elif winner.get("autoArchiveExempt") is True:
        result["autoArchiveExempt"] = True
    return result


def encode_record(record: Mapping[str, Any]) -> bytes:
    """Compact JSON, as Claude writes it. A half surrogate pair is kept as an escape."""

    try:
        return json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except UnicodeEncodeError:
        return json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode(
            "ascii"
        )


def head_flag(head: bytes) -> bool | None:
    """The top-level isArchived value in the first bytes of a record, if it is there."""

    for match in FLAG.finditer(head):
        if _depth(head, match.start()) == 1:
            return match.group(1) == b"true"
    return None


def _depth(data: bytes, end: int) -> int:
    # A key starts at a quote that opens a string, so no string crosses end.
    depth = 0
    for token in TOKEN.finditer(data, 0, end):
        first = token.group()[:1]
        if first in (b"{", b"["):
            depth += 1
        elif first in (b"}", b"]"):
            depth -= 1
    return depth


def _ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return 0
    return max(0, int(value))


class ArchiveSync:
    """Read, decide and write the archive state of the chats in the sync folders."""

    def __init__(self, state_dir: Path, targets: Mapping[str, Any]) -> None:
        self.state_dir = Path(state_dir)
        self.targets = dict(targets)
        self.ledger = _load_ledger(self.state_dir / LEDGER_FILENAME)

    def flags(self) -> dict[str, dict[str, list]]:
        """Each folder's flags now: {folder: {session: [flag, size, mtime_ns]}}.

        A file with the size and save time remembered is not read again.
        """

        current: dict[str, dict[str, list]] = {}
        for key, target in sorted(self.targets.items()):
            remembered = self.ledger.get(key, {})
            entries: dict[str, list] = {}
            try:
                names = [entry.name for entry in os.scandir(str(target.path))]
            except OSError:
                continue
            for name in names:
                if not (name.startswith("local_") and name.endswith(".json")):
                    continue
                session_id = name[len("local_") : -len(".json")]
                path = Path(target.path) / name
                try:
                    metadata = os.lstat(str(path))
                except OSError:
                    continue
                held = remembered.get(session_id)
                if (
                    held
                    and held[1] == metadata.st_size
                    and held[2] == metadata.st_mtime_ns
                ):
                    entries[session_id] = held
                    continue
                flag = _read_flag(path, session_id)
                if flag is not None:
                    entries[session_id] = [
                        int(flag),
                        metadata.st_size,
                        metadata.st_mtime_ns,
                    ]
            current[key] = entries
        return current

    def decisions(
        self, current: Mapping[str, Mapping[str, list]], skip: Iterable[str] = ()
    ) -> list[tuple[ArchiveCopy, list[ArchiveCopy]]]:
        """(winner, copies to change) for each chat whose copies disagree."""

        by_session: dict[str, dict[str, int]] = {}
        for key, entries in current.items():
            for session_id, entry in entries.items():
                by_session.setdefault(session_id, {})[key] = entry[0]
        skipped = set(skip)
        found = []
        for session_id, held in sorted(by_session.items()):
            if session_id in skipped or len(set(held.values())) < 2:
                continue
            copies = self._copies(session_id, held)
            if copies is None:
                continue
            winner = decide(copies)
            if winner is None:
                continue
            losers = [copy for copy in copies if copy.archived != winner.archived]
            found.append((winner, losers))
        return found

    def _copies(
        self, session_id: str, held: Mapping[str, int]
    ) -> list[ArchiveCopy] | None:
        copies = []
        for key in sorted(held):
            path = Path(self.targets[key].path) / f"local_{session_id}.json"
            try:
                raw = read_bounded(path, MAX_RECORD_BYTES)
                saved_ms = os.lstat(str(path)).st_mtime_ns // 1_000_000
                record = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeError, ValueError):
                return None
            if (
                not isinstance(record, dict)
                or record.get("sessionId") != "local_" + session_id
            ):
                return None
            if (record.get("isArchived") is True) != bool(held[key]):
                return None  # changed since the flags were read
            known = self.ledger.get(key, {}).get(session_id)
            copies.append(
                ArchiveCopy(
                    key,
                    path,
                    raw,
                    record,
                    saved_ms,
                    None if known is None else bool(known[0]),
                )
            )
        return copies

    def operations(
        self, decisions: Sequence[tuple[ArchiveCopy, list[ArchiveCopy]]], stage: Path
    ) -> list[Operation]:
        """Replace steps that carry each winner's archive state, staged under stage."""

        operations: list[Operation] = []
        for winner, losers in decisions:
            for loser in losers:
                record = archive_fields(loser.record, winner.record)
                content = encode_record(record)
                session_id = loser.path.name[len("local_") : -len(".json")]
                if json.loads(content.decode("utf-8")) != record:
                    raise ArchiveError(f"record did not survive encoding: {loser.path}")
                copy = fingerprint(session_id, content)
                if copy.state_hash is None:
                    raise ArchiveError(f"record would not be accepted: {loser.path}")
                source = stage / f"{len(operations)}-{loser.path.name}"
                atomic_write_bytes(source, content)
                operations.append(
                    Operation(
                        "replace",
                        session_id,
                        source,
                        loser.path,
                        hashlib.sha256(content).hexdigest(),
                        hashlib.sha256(loser.raw).hexdigest(),
                        len(content),
                        "record",
                        copy.state_hash,
                    )
                )
        return operations

    def remember(self, current: Mapping[str, Mapping[str, list]]) -> None:
        """Keep the flags as they are after this run, for the next run to compare."""

        document = {"folders": current, "version": LEDGER_VERSION}
        path = self.state_dir / LEDGER_FILENAME
        content = (
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        try:
            if read_bounded(path, MAX_RECORD_BYTES) == content:
                return
        except OSError:
            pass
        ensure_private_directory(self.state_dir)
        atomic_write_bytes(path, content)
        os.chmod(str(path), 0o600)


def spread_archive_state(
    state_dir: Path,
    targets: Mapping[str, Any],
    engine: Any,
    *,
    skip: Iterable[str] = (),
) -> tuple[ArchiveReport, tuple[Operation, ...]]:
    """Give every copy of a chat the archive state that won. Returns what was applied."""

    sync = ArchiveSync(state_dir, targets)
    decisions = sync.decisions(sync.flags(), skip)
    if not decisions:
        return ArchiveReport(), ()
    stage = Path(state_dir) / STAGE_DIRNAME / uuid.uuid4().hex
    ensure_private_directory(stage)
    try:
        operations = tuple(sync.operations(decisions, stage))
        label = hashlib.sha256(
            "\n".join(
                f"archive {op.session_id} {op.destination}" for op in operations
            ).encode("utf-8")
        ).hexdigest()[:12]
        receipt = engine.apply(
            Plan(operations, (), label, sum(op.size for op in operations))
        )
    finally:
        shutil.rmtree(str(stage), ignore_errors=True)
        try:
            os.rmdir(str(stage.parent))
        except OSError:
            pass  # another run's stage, or already gone
    applied = tuple(receipt.applied)
    archiving = {
        loser.path
        for winner, losers in decisions
        if winner.archived
        for loser in losers
    }
    archived = sum(1 for op in applied if op.destination in archiving)
    return ArchiveReport(archived=archived, opened=len(applied) - archived), applied


def preview_archive_state(
    state_dir: Path, targets: Mapping[str, Any], *, skip: Iterable[str] = ()
) -> ArchiveReport:
    """What spread_archive_state would change, without writing anything."""

    sync = ArchiveSync(state_dir, targets)
    archived = opened = 0
    for winner, losers in sync.decisions(sync.flags(), skip):
        if winner.archived:
            archived += len(losers)
        else:
            opened += len(losers)
    return ArchiveReport(archived=archived, opened=opened)


def remember_archive_state(state_dir: Path, targets: Mapping[str, Any]) -> None:
    sync = ArchiveSync(state_dir, targets)
    sync.remember(sync.flags())


def _read_flag(path: Path, session_id: str) -> bool | None:
    try:
        with open_regular_read(path) as stream:
            head = stream.read(HEAD_BYTES)
            larger = bool(stream.read(1))
    except (OSError, UnsafePathError):
        return None
    flag = head_flag(head)
    if flag is not None:
        return flag
    try:
        raw = head if not larger else read_bounded(path, MAX_RECORD_BYTES)
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("sessionId") != "local_" + session_id:
        return None
    return record.get("isArchived") is True


def _load_ledger(path: Path) -> dict[str, dict[str, list]]:
    """The flags of the last run. A missing or damaged file means none are known."""

    try:
        document = json.loads(read_bounded(path, MAX_RECORD_BYTES))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict) or document.get("version") != LEDGER_VERSION:
        return {}
    folders = document.get("folders")
    if not isinstance(folders, dict):
        return {}
    ledger: dict[str, dict[str, list]] = {}
    for key, entries in folders.items():
        if not isinstance(entries, dict):
            continue
        ledger[key] = {
            session_id: entry
            for session_id, entry in entries.items()
            if isinstance(entry, list)
            and len(entry) == 3
            and entry[0] in (0, 1)
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in entry
            )
        }
    return ledger
