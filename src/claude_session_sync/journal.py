"""Private, durable transaction journals."""

from __future__ import annotations

import hashlib
import hmac
from . import strict_json as json
import os
import re
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

from .filesystem import (
    _open_regular_read,
    atomic_copy,
    atomic_write_bytes,
    digest_file,
    ensure_private_directory,
    ensure_private_file,
    fsync_directory,
)


PathLike = Union[str, os.PathLike]


class JournalError(RuntimeError):
    """Raised when a run journal is missing, corrupt, or internally inconsistent."""


class RunJournal:
    VERSION = 1
    PHASES = {
        "JOURNALING",
        "STAGING",
        "COMMITTING",
        "VERIFYING",
        "COMMITTED",
        "ABORTING",
        "ROLLED_BACK",
        "RECOVERY_REQUIRED",
    }

    def __init__(
        self,
        state_root: PathLike,
        run_id: str,
        manifest: Dict[str, Any],
        authentication_key: bytes,
    ):
        self._validate_run_id(run_id)
        self.state_root = Path(state_root)
        self.run_id = run_id
        self.run_root = self.state_root / "runs" / run_id
        self.manifest_path = self.run_root / "manifest.json"
        self.manifest = manifest
        self.authentication_key = authentication_key

    @classmethod
    def create(
        cls,
        state_root: PathLike,
        plan_id: str,
        records: Iterable[Mapping[str, Any]],
        *,
        run_id: Optional[str] = None,
    ) -> "RunJournal":
        state = ensure_private_directory(state_root)
        authentication_key = cls._load_or_create_key(state)
        runs = ensure_private_directory(state / "runs")
        preparations = ensure_private_directory(state / "preparations")
        fsync_directory(state)
        identifier = run_id or uuid.uuid4().hex
        cls._validate_run_id(identifier)
        # A run becomes recoverable only after every backup and its signed
        # manifest are durable. Failed preparations never changed live data.
        if os.path.lexists(str(runs / identifier)):
            raise JournalError("run already exists: {}".format(identifier))
        run_root = preparations / identifier
        try:
            run_root.mkdir(mode=0o700)
        except FileExistsError as error:
            raise JournalError("run already exists: {}".format(identifier)) from error
        materialized = []  # type: List[Dict[str, Any]]
        try:
            fsync_directory(preparations)
            ensure_private_directory(run_root / "preimages")
            for index, raw_record in enumerate(records):
                record = dict(raw_record)
                record["index"] = index
                record["applied"] = False
                if record["existed"]:
                    preimage_relative = "preimages/{:06d}.bin".format(index)
                    preimage = run_root / preimage_relative
                    atomic_copy(Path(record["destination"]), preimage)
                    if digest_file(preimage) != record["destination_digest_before"]:
                        raise JournalError(
                            "destination changed while journaling: {}".format(
                                record["destination"]
                            )
                        )
                    record["preimage"] = preimage_relative
                else:
                    record["preimage"] = None
                materialized.append(record)
            manifest = {
                "version": cls.VERSION,
                "run_id": identifier,
                "plan_id": plan_id,
                "phase": "JOURNALING",
                "records": materialized,
                "receipt": None,
            }
            journal = cls(state, identifier, manifest, authentication_key)
            journal._persist(destination=run_root / "manifest.json")
            fsync_directory(run_root)
            if os.path.lexists(str(journal.run_root)):
                raise JournalError("run already exists: {}".format(identifier))
            os.rename(str(run_root), str(journal.run_root))
            fsync_directory(runs)
            fsync_directory(preparations)
            return journal
        finally:
            # These are copies, never originals: live mutation starts only
            # after publication and return. Never remove the published journal.
            if os.path.lexists(str(run_root)):
                shutil.rmtree(str(run_root))
                fsync_directory(preparations)

    @classmethod
    def load(cls, state_root: PathLike, run_id: str) -> "RunJournal":
        cls._validate_run_id(run_id)
        state = ensure_private_directory(state_root)
        authentication_key = cls._load_or_create_key(state)
        manifest_path = state / "runs" / run_id / "manifest.json"
        try:
            with _open_regular_read(manifest_path) as stream:
                raw = stream.read()
        except FileNotFoundError as error:
            raise JournalError("unknown run: {}".format(run_id)) from error
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise JournalError("corrupt manifest for run {}".format(run_id)) from error
        cls._validate_manifest(manifest, run_id, authentication_key)
        return cls(state, run_id, manifest, authentication_key)

    @property
    def records(self) -> List[Dict[str, Any]]:
        return self.manifest["records"]

    @property
    def phase(self) -> str:
        return self.manifest["phase"]

    def preimage_path(self, record: Mapping[str, Any]) -> Path:
        relative = record.get("preimage")
        if not isinstance(relative, str):
            raise JournalError("record has no preimage")
        candidate = (self.run_root / relative).resolve(strict=False)
        try:
            candidate.relative_to(self.run_root.resolve(strict=False))
        except ValueError as error:
            raise JournalError("preimage escapes run journal") from error
        return candidate

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
            raise JournalError("invalid run id")

    def set_phase(self, phase: str) -> None:
        self.manifest["phase"] = phase
        self._persist()

    def mark_applied(self, index: int) -> None:
        self.records[index]["applied"] = True

    def mark_unapplied(self, index: int) -> None:
        self.records[index]["applied"] = False

    def mark_staged(self, identities: Iterable[Mapping[str, int]]) -> None:
        for identity in identities:
            index = identity["index"]
            self.records[index]["staged_device"] = identity["device"]
            self.records[index]["staged_inode"] = identity["inode"]
        self._persist()

    def finish(self, phase: str, receipt: Mapping[str, Any]) -> None:
        if phase not in ("COMMITTED", "ROLLED_BACK"):
            raise JournalError("invalid terminal phase: {}".format(phase))
        self.manifest["phase"] = phase
        self.manifest["receipt"] = dict(receipt)
        self._persist()

    def _persist(self, *, destination: Optional[Path] = None) -> None:
        self.manifest["generation"] = int(self.manifest.get("generation", 0)) + 1
        self.manifest["checksum"] = self._checksum(
            self.manifest, self.authentication_key
        )
        encoded = (
            json.dumps(self.manifest, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
        atomic_write_bytes(destination or self.manifest_path, encoded)

    @classmethod
    def _validate_manifest(
        cls, manifest: Any, run_id: str, authentication_key: bytes
    ) -> None:
        if not isinstance(manifest, dict):
            raise JournalError("invalid manifest for run {}".format(run_id))
        checksum = manifest.get("checksum")
        if not isinstance(checksum, str) or not hmac.compare_digest(
            checksum, cls._checksum(manifest, authentication_key)
        ):
            raise JournalError("manifest checksum mismatch for run {}".format(run_id))
        records = manifest.get("records")
        if (
            manifest.get("version") != cls.VERSION
            or manifest.get("run_id") != run_id
            or not isinstance(manifest.get("plan_id"), str)
            or manifest.get("phase") not in cls.PHASES
            or not isinstance(records, list)
            or not isinstance(manifest.get("generation"), int)
            or manifest["generation"] < 1
        ):
            raise JournalError("invalid manifest for run {}".format(run_id))
        receipt = manifest.get("receipt")
        if receipt is not None and not isinstance(receipt, dict):
            raise JournalError("invalid receipt for run {}".format(run_id))
        for index, record in enumerate(records):
            cls._validate_record(record, index, run_id)

    @classmethod
    def _validate_record(cls, record: Any, index: int, run_id: str) -> None:
        if not isinstance(record, dict):
            raise JournalError("invalid record for run {}".format(run_id))
        existed = record.get("existed")
        before_digest = record.get("destination_digest_before")
        staged_device = record.get("staged_device")
        staged_inode = record.get("staged_inode")
        if (
            record.get("index") != index
            or not isinstance(record.get("source"), str)
            or not record["source"]
            or not isinstance(record.get("destination"), str)
            or not record["destination"]
            or not cls._is_digest(record.get("source_digest"))
            or not isinstance(existed, bool)
            or (existed and not cls._is_digest(before_digest))
            or (not existed and before_digest is not None)
            or not isinstance(record.get("applied"), bool)
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
            or not isinstance(record.get("destination_size_before"), int)
            or record["destination_size_before"] < 0
            or not isinstance(record.get("session_id"), str)
            or (
                existed
                and record.get("preimage") != "preimages/{:06d}.bin".format(index)
            )
            or (not existed and record.get("preimage") is not None)
            or (staged_device is None) != (staged_inode is None)
            or (
                staged_device is not None
                and (
                    not isinstance(staged_device, int)
                    or staged_device < 0
                    or not isinstance(staged_inode, int)
                    or staged_inode < 0
                )
            )
        ):
            raise JournalError("invalid record for run {}".format(run_id))

    @staticmethod
    def _checksum(manifest: Mapping[str, Any], authentication_key: bytes) -> str:
        payload = dict(manifest)
        payload.pop("checksum", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hmac.new(authentication_key, encoded, hashlib.sha256).hexdigest()

    @staticmethod
    def _is_digest(value: Any) -> bool:
        return (
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        )

    @staticmethod
    def _load_or_create_key(state_root: Path) -> bytes:
        key_path = state_root / "journal.key"
        if not os.path.lexists(str(key_path)):
            atomic_write_bytes(key_path, os.urandom(32))
        flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(str(key_path), flags)
        except OSError as error:
            raise JournalError("cannot open journal authentication key") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise JournalError("journal authentication key is not a regular file")
            with os.fdopen(descriptor, "rb") as key_file:
                descriptor = -1
                key = key_file.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(key) != 32:
            raise JournalError("invalid journal authentication key")
        ensure_private_file(key_path)
        return key


def prune_terminal_runs(state_root: PathLike, retention: int) -> List[str]:
    """Best-effort deletion of old terminal journals only."""

    if retention < 1:
        raise ValueError("retention must be at least one run")
    state = ensure_private_directory(state_root)
    runs = ensure_private_directory(state / "runs")
    terminal = []
    for run_root in runs.iterdir():
        if run_root.is_symlink() or not run_root.is_dir():
            continue
        try:
            journal = RunJournal.load(state, run_root.name)
            modified_ns = journal.manifest_path.stat().st_mtime_ns
        except (JournalError, OSError):
            continue
        if journal.phase in ("COMMITTED", "ROLLED_BACK"):
            terminal.append((modified_ns, journal.run_id, run_root))
    terminal.sort(key=lambda item: (item[0], item[1]), reverse=True)

    deleted = []
    for unused_modified_ns, run_id, run_root in terminal[retention:]:
        try:
            shutil.rmtree(str(run_root))
            fsync_directory(runs)
        except OSError:
            continue
        deleted.append(run_id)
    return deleted


def abandoned_preparations(state_root: PathLike) -> List[str]:
    """Expose crash residue without mistaking it for a published transaction."""

    root = Path(state_root) / "preparations"
    if not os.path.lexists(str(root)):
        return []
    if root.is_symlink() or not root.is_dir():
        raise JournalError("preparations path is not a real directory")
    identifiers = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            raise JournalError("unexpected entry in preparations directory")
        RunJournal._validate_run_id(path.name)
        identifiers.append(path.name)
    return sorted(identifiers)


def pending_recovery_runs(state_root: PathLike) -> List[str]:
    """Return nonterminal runs; malformed state raises instead of being skipped."""

    state = ensure_private_directory(state_root)
    runs = state / "runs"
    if not os.path.lexists(str(runs)):
        return []
    if runs.is_symlink() or not runs.is_dir():
        raise JournalError("journal runs path is not a real directory")
    pending = []
    for run_root in runs.iterdir():
        if run_root.is_symlink() or not run_root.is_dir():
            raise JournalError("unexpected entry in journal runs directory")
        journal = RunJournal.load(state, run_root.name)
        if journal.phase not in ("COMMITTED", "ROLLED_BACK"):
            pending.append(journal.run_id)
    return pending
