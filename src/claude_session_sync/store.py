"""Discovery and validation of Claude Code session replicas."""

import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

from .config import Config
from .hash_cache import HashCache
from .model import Discovery, InvalidReplica, Replica, Target


def _stat_signature(stat_result: os.stat_result) -> Tuple[int, int, int, int]:
    return (
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
        stat_result.st_ino,
    )


class SessionStore:
    def __init__(self, hash_cache: Optional[HashCache] = None) -> None:
        self._hash_cache = hash_cache

    def discover(self, config: Config) -> Discovery:
        targets: List[Target] = []
        replicas: List[Replica] = []
        invalid: List[InvalidReplica] = []

        for profile in sorted(config.profiles, key=lambda item: item.name):
            sessions_root = profile.data_root / "claude-code-sessions"
            if not sessions_root.exists():
                invalid.append(
                    InvalidReplica(sessions_root, "sessions root is missing")
                )
                continue
            if sessions_root.is_symlink() or not sessions_root.is_dir():
                invalid.append(
                    InvalidReplica(
                        sessions_root, "sessions root is not a real directory"
                    )
                )
                continue
            try:
                account_entries = sorted(
                    os.scandir(str(sessions_root)), key=lambda item: item.name
                )
            except OSError as error:
                invalid.append(
                    InvalidReplica(
                        sessions_root, "cannot scan sessions root: {}".format(error)
                    )
                )
                continue
            profile_target_count = 0
            for account_entry in account_entries:
                account_path = Path(account_entry.path)
                if account_entry.is_symlink():
                    invalid.append(
                        InvalidReplica(account_path, "symlink is not allowed")
                    )
                    continue
                if not account_entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    workspace_entries = sorted(
                        os.scandir(account_entry.path), key=lambda item: item.name
                    )
                except OSError as error:
                    invalid.append(
                        InvalidReplica(
                            account_path,
                            "cannot scan account directory: {}".format(error),
                        )
                    )
                    continue
                for workspace_entry in workspace_entries:
                    workspace_path = Path(workspace_entry.path)
                    if workspace_entry.is_symlink():
                        invalid.append(
                            InvalidReplica(workspace_path, "symlink is not allowed")
                        )
                        continue
                    if not workspace_entry.is_dir(follow_symlinks=False):
                        continue
                    target = Target(
                        profile.name,
                        account_entry.name,
                        workspace_entry.name,
                        workspace_path,
                    )
                    targets.append(target)
                    profile_target_count += 1
                    self._discover_target(target, replicas, invalid)
            if profile_target_count == 0:
                invalid.append(
                    InvalidReplica(
                        sessions_root,
                        "sessions root contains no account/workspace targets",
                    )
                )

        return Discovery(
            tuple(sorted(targets, key=_target_key)),
            tuple(sorted(replicas, key=_replica_key)),
            tuple(sorted(invalid, key=lambda item: str(item.path))),
        )

    def _discover_target(
        self,
        target: Target,
        replicas: List[Replica],
        invalid: List[InvalidReplica],
    ) -> None:
        try:
            entries = sorted(os.scandir(str(target.path)), key=lambda item: item.name)
        except OSError as error:
            invalid.append(
                InvalidReplica(target.path, "cannot scan target: {}".format(error))
            )
            return
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                invalid.append(InvalidReplica(path, "symlink is not allowed"))
                continue
            if not entry.name.startswith("local_") or not entry.name.endswith(".json"):
                continue
            if not entry.is_file(follow_symlinks=False):
                invalid.append(InvalidReplica(path, "replica is not a regular file"))
                continue
            session_id = entry.name[len("local_") : -len(".json")]
            if not session_id:
                invalid.append(
                    InvalidReplica(path, "replica filename has no session id")
                )
                continue
            replica = self._read_replica(path, target, session_id, invalid)
            if replica is not None:
                replicas.append(replica)

    def _read_replica(
        self,
        path: Path,
        target: Target,
        session_id: str,
        invalid: List[InvalidReplica],
    ) -> Optional[Replica]:
        try:
            before = path.lstat()
            if self._hash_cache is not None:
                cached_digest = self._hash_cache.lookup_validated(
                    path, before, session_id
                )
                if cached_digest is not None:
                    after_lookup = path.lstat()
                    if _stat_signature(before) != _stat_signature(after_lookup):
                        invalid.append(
                            InvalidReplica(path, "replica changed during discovery")
                        )
                        return None
                    return Replica(
                        session_id,
                        target,
                        path,
                        after_lookup.st_size,
                        after_lookup.st_mtime_ns,
                        cached_digest,
                    )
            raw = path.read_bytes()
            after = path.lstat()
        except OSError as error:
            invalid.append(
                InvalidReplica(path, "cannot read replica: {}".format(error))
            )
            return None
        if (
            _stat_signature(before) != _stat_signature(after)
            or len(raw) != after.st_size
        ):
            invalid.append(InvalidReplica(path, "replica changed during discovery"))
            return None
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            invalid.append(InvalidReplica(path, "malformed JSON: {}".format(error)))
            return None
        if not isinstance(document, dict) or not document:
            invalid.append(
                InvalidReplica(path, "session JSON must be a non-empty object")
            )
            return None
        document_session_id = document.get("sessionId")
        if not isinstance(document_session_id, str) or not document_session_id:
            invalid.append(InvalidReplica(path, "sessionId must be a non-empty string"))
            return None
        try:
            digest = hashlib.sha256(raw).hexdigest()
            if self._hash_cache is not None:
                self._hash_cache.store_validated(path, after, session_id, digest)
        except OSError as error:
            invalid.append(
                InvalidReplica(path, "cannot hash replica: {}".format(error))
            )
            return None
        return Replica(
            session_id, target, path, after.st_size, after.st_mtime_ns, digest
        )


def _target_key(target: Target) -> Tuple[str, str, str]:
    return target.profile_name, target.account_id, target.workspace_id


def _replica_key(replica: Replica) -> Tuple[str, str, str, str]:
    return (
        replica.target.profile_name,
        replica.target.account_id,
        replica.target.workspace_id,
        replica.session_id,
    )


Store = SessionStore
