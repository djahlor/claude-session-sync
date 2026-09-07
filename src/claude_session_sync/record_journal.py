"""Durable compare-before-write recovery for byte records and optional files.

Storage adapters own target validation and I/O. This module owns one journal
format and one recovery policy; it never replaces independently changed bytes.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from . import strict_json as json
from .filesystem import atomic_write_bytes, durable_unlink, ensure_private_directory


VERSION = 2
TERMINAL = {"COMMITTED", "ROLLED_BACK"}
STATES = TERMINAL | {"PREPARED", "RECOVERY_REQUIRED"}
MAX_JOURNAL_BYTES = 256 * 1024 * 1024


class RecordJournalError(RuntimeError):
    """A record update failed but was restored or never started."""


class RecordRecoveryError(RecordJournalError):
    """Recovery needs attention before this adapter can write again."""


def digest(value: Optional[bytes]) -> Optional[str]:
    return None if value is None else hashlib.sha256(value).hexdigest()


def valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class Record:
    target: str
    before: Optional[bytes]
    after: Optional[bytes]
    after_sha256: Optional[str]
    legacy: bool = False

    def matches_after(self, value: Optional[bytes]) -> bool:
        if self.legacy:
            return value is not None and digest(value) == self.after_sha256
        return value == self.after


class RecordJournal:
    def __init__(
        self,
        root: Path,
        *,
        read: Callable[[str], Optional[bytes]],
        write: Callable[[str, bytes], None],
        delete: Callable[[str], None],
        validate: Callable[[str], None],
        before_mutation: Callable[[], None],
        retention: int,
        legacy_loader: Optional[Callable[[Mapping[str, Any]], Sequence[Record]]] = None,
    ) -> None:
        self.root = root
        self.read = read
        self.write = write
        self.delete = delete
        self.validate = validate
        self.before_mutation = before_mutation
        self.retention = max(1, retention)
        self.legacy_loader = legacy_loader

    def _load(self, path: Path) -> Dict[str, Any]:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
            descriptor = os.open(str(path), flags)
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size > MAX_JOURNAL_BYTES
                ):
                    raise RecordRecoveryError("recovery record is unsafe")
                content = stream.read(MAX_JOURNAL_BYTES + 1)
            if len(content) > MAX_JOURNAL_BYTES:
                raise RecordRecoveryError("recovery record exceeds size limit")
            document = json.loads(content.decode("utf-8"))
        except (OSError, ValueError, UnicodeError) as error:
            raise RecordRecoveryError(
                "recovery state is malformed or unsafe"
            ) from error
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("state"), str)
            or document["state"] not in STATES
        ):
            raise RecordRecoveryError("recovery state is malformed")
        return document

    def _save(self, path: Path, document: Mapping[str, Any]) -> None:
        content = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if len(content) > MAX_JOURNAL_BYTES:
            raise RecordJournalError("recovery record exceeds size limit")
        atomic_write_bytes(path, content)

    def _records(self, document: Mapping[str, Any]) -> Sequence[Record]:
        if document.get("version") == VERSION:
            raw_records = document.get("records")
            if not isinstance(raw_records, list) or not raw_records:
                raise RecordRecoveryError("recovery records are malformed")
            records = []
            for raw in raw_records:
                try:
                    before = self._decode(raw["before"])
                    after = self._decode(raw["after"])
                    if (
                        digest(before) != raw["before_sha256"]
                        or digest(after) != raw["after_sha256"]
                    ):
                        raise ValueError("record digest mismatch")
                    records.append(Record(raw["target"], before, after, digest(after)))
                except (KeyError, ValueError, TypeError) as error:
                    raise RecordRecoveryError("recovery record is malformed") from error
        elif document.get("version") == 1 and self.legacy_loader:
            try:
                records = list(self.legacy_loader(document))
            except (KeyError, ValueError, TypeError) as error:
                raise RecordRecoveryError(
                    "legacy recovery record is malformed"
                ) from error
        else:
            raise RecordRecoveryError("recovery version is unsupported")
        seen = set()
        for record in records:
            if not isinstance(record.target, str) or record.target in seen:
                raise RecordRecoveryError("recovery targets are malformed")
            if record.legacy and not valid_digest(record.after_sha256):
                raise RecordRecoveryError("legacy recovery digest is malformed")
            self.validate(record.target)
            seen.add(record.target)
        if not records:
            raise RecordRecoveryError("recovery records are empty")
        return records

    @staticmethod
    def _decode(value: Any) -> Optional[bytes]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("invalid record encoding")
        return base64.b64decode(value, validate=True)

    @staticmethod
    def _encode(value: Optional[bytes]) -> Optional[str]:
        return None if value is None else base64.b64encode(value).decode("ascii")

    def _current(self, records: Sequence[Record]) -> Dict[str, Optional[bytes]]:
        current = {}
        for record in records:
            self.validate(record.target)
            current[record.target] = self.read(record.target)
        return current

    def _restore(self, records: Sequence[Record]) -> None:
        # Preflight every target before touching even the first record.
        current = self._current(records)
        if any(
            current[record.target] != record.before
            and not record.matches_after(current[record.target])
            for record in records
        ):
            raise RecordRecoveryError("recovery found independently changed data")
        for record in records:
            if current[record.target] == record.before:
                continue
            self.before_mutation()
            self.validate(record.target)
            if self.read(record.target) != current[record.target]:
                raise RecordRecoveryError("data changed during recovery")
            if record.before is None:
                self.delete(record.target)
            else:
                self.write(record.target, record.before)
            if self.read(record.target) != record.before:
                raise RecordRecoveryError("rollback failed verification")

    def recover(self) -> None:
        if not os.path.lexists(str(self.root)):
            return
        if self.root.is_symlink() or not self.root.is_dir():
            raise RecordRecoveryError("recovery state is unsafe")
        for path in sorted(self.root.glob("*.json")):
            document = self._load(path)
            if document["state"] in TERMINAL:
                continue
            records = self._records(document)
            current = self._current(records)
            if all(current[item.target] == item.before for item in records):
                document["state"] = "ROLLED_BACK"
            elif all(item.matches_after(current[item.target]) for item in records):
                document["state"] = "COMMITTED"
            else:
                try:
                    self._restore(records)
                except BaseException as error:
                    document["state"] = "RECOVERY_REQUIRED"
                    self._save(path, document)
                    raise RecordRecoveryError(
                        "recovery requires attention: {}".format(error)
                    ) from error
                document["state"] = "ROLLED_BACK"
            self._save(path, document)

    def commit(
        self,
        current: Mapping[str, Optional[bytes]],
        replacements: Mapping[str, Optional[bytes]],
    ) -> None:
        if not replacements:
            return
        self.recover()
        records = [
            Record(target, current[target], after, digest(after))
            for target, after in sorted(replacements.items())
        ]
        if self._current(records) != {item.target: item.before for item in records}:
            raise RecordJournalError("data changed before synchronization")
        self.before_mutation()
        ensure_private_directory(self.root)
        path = self.root / "{}.json".format(uuid.uuid4().hex)
        document = {
            "version": VERSION,
            "state": "PREPARED",
            "records": [
                {
                    "target": item.target,
                    "before": self._encode(item.before),
                    "after": self._encode(item.after),
                    "before_sha256": digest(item.before),
                    "after_sha256": item.after_sha256,
                }
                for item in records
            ],
        }
        self._save(path, document)
        try:
            for record in records:
                self.before_mutation()
                self.validate(record.target)
                if self.read(record.target) != record.before:
                    raise RecordJournalError("data changed during synchronization")
                if record.after == record.before:
                    continue
                if record.after is None:
                    self.delete(record.target)
                else:
                    self.write(record.target, record.after)
                if self.read(record.target) != record.after:
                    raise RecordJournalError("record write failed verification")
            self.before_mutation()
            if self._current(records) != {
                record.target: record.after for record in records
            }:
                raise RecordJournalError("data changed before transaction completion")
        except BaseException as error:
            try:
                self._restore(records)
            except BaseException as recovery_error:
                document["state"] = "RECOVERY_REQUIRED"
                self._save(path, document)
                raise RecordRecoveryError(
                    "recovery requires attention: {}".format(recovery_error)
                ) from error
            document["state"] = "ROLLED_BACK"
            self._save(path, document)
            raise RecordJournalError("update was rolled back safely") from error
        document["state"] = "COMMITTED"
        self._save(path, document)
        self.prune()

    def prune(self) -> None:
        terminal = []
        for path in self.root.glob("*.json"):
            try:
                if self._load(path)["state"] in TERMINAL:
                    terminal.append(path)
            except RecordRecoveryError:
                continue
        terminal.sort(key=lambda item: item.stat().st_mtime_ns)
        for path in terminal[: -self.retention]:
            durable_unlink(path)
