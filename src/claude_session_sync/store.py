"""Discovery and validation of Claude Code session replicas."""

import hashlib
import os
import re
import stat
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .config import Config
from .fingerprint import fingerprint, normalisation
from .hash_cache import HashCache
from .model import Discovery, InvalidReplica, Marker, Replica, Target, TmpFile


# Claude's own filters: local_<id>.json records, their .tmp saves, deleted_<id> markers.
SESSION_ID = r"[A-Za-z0-9_-]+"
RECORD_NAME = re.compile(r"^local_(%s)\.json$" % SESSION_ID)
TMP_NAME = re.compile(r"^local_(%s)\.json\.tmp$" % SESSION_ID)
MARKER_NAME = re.compile(r"^deleted_(%s)$" % SESSION_ID)
# Two saves inside one timestamp tick can share metadata, so a file this fresh
# is never trusted to match its cache entry.
RACY_WINDOW_NS = 2_000_000_000
MAX_MARKER_BYTES = 64


def _stat_signature(stat_result: os.stat_result) -> Tuple[int, int, int, int]:
    return (
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
        stat_result.st_ino,
    )


class SessionStore:
    def __init__(
        self,
        hash_cache: Optional[HashCache] = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._hash_cache = hash_cache
        self._clock_ns = clock_ns

    def discover(self, config: Config) -> Discovery:
        discovery = self.discover_targets(config)
        scanned = self.scan_targets(discovery.targets)
        return Discovery(
            discovery.targets,
            scanned.replicas,
            tuple(
                sorted(
                    discovery.invalid_replicas + scanned.invalid_replicas,
                    key=lambda item: str(item.path),
                )
            ),
            scanned.markers,
            scanned.tmps,
        )

    def scan_targets(self, targets: Sequence[Target]) -> Discovery:
        """Read records, delete markers and temp saves in the given folders only."""

        replicas: List[Replica] = []
        markers: List[Marker] = []
        tmps: List[TmpFile] = []
        invalid: List[InvalidReplica] = []
        for target in targets:
            self._discover_target(target, replicas, markers, tmps, invalid)
        return Discovery(
            tuple(sorted(targets, key=_target_key)),
            tuple(sorted(replicas, key=_replica_key)),
            tuple(sorted(invalid, key=lambda item: str(item.path))),
            tuple(sorted(markers, key=_replica_key)),
            tuple(sorted(tmps, key=_replica_key)),
        )

    def discover_targets(self, config: Config) -> Discovery:
        """Validate target directories without reading another adapter's records."""
        targets: List[Target] = []
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
            if profile_target_count == 0:
                invalid.append(
                    InvalidReplica(
                        sessions_root,
                        "sessions root contains no account/workspace targets",
                    )
                )

        return Discovery(
            tuple(sorted(targets, key=_target_key)),
            (),
            tuple(sorted(invalid, key=lambda item: str(item.path))),
        )

    def _discover_target(
        self,
        target: Target,
        replicas: List[Replica],
        markers: List[Marker],
        tmps: List[TmpFile],
        invalid: List[InvalidReplica],
    ) -> None:
        try:
            entries = sorted(os.scandir(str(target.path)), key=lambda item: item.name)
        except OSError as error:
            invalid.append(
                InvalidReplica(target.path, "cannot scan target: {}".format(error))
            )
            return
        started_ns = self._clock_ns()
        for entry in entries:
            path = Path(entry.path)
            record = RECORD_NAME.match(entry.name)
            tmp = TMP_NAME.match(entry.name)
            marker = MARKER_NAME.match(entry.name)
            if not (record or tmp or marker):
                continue
            if entry.is_symlink():
                invalid.append(InvalidReplica(path, "symlink is not allowed"))
                continue
            try:
                before = path.lstat()
            except FileNotFoundError:
                continue  # Claude renamed or removed it under the scan
            except OSError as error:
                invalid.append(InvalidReplica(path, "cannot inspect: {}".format(error)))
                continue
            if not stat.S_ISREG(before.st_mode):
                invalid.append(InvalidReplica(path, "replica is not a regular file"))
                continue
            if record:
                replica = self._read_replica(
                    path, target, record.group(1), before, started_ns
                )
                if replica is not None:
                    replicas.append(replica)
            elif tmp:
                read = _read_small(path, before, limit=None)
                if read is not None:
                    raw, after = read
                    tmps.append(
                        TmpFile(
                            tmp.group(1),
                            target,
                            path,
                            after.st_size,
                            after.st_mtime_ns,
                            hashlib.sha256(raw).hexdigest(),
                        )
                    )
            else:
                read = _read_small(path, before, limit=MAX_MARKER_BYTES)
                if read is None:
                    continue
                raw, after = read
                deleted_at = self._delete_time(raw, after)
                markers.append(
                    Marker(
                        marker.group(1),
                        target,
                        path,
                        after.st_size,
                        after.st_mtime_ns,
                        hashlib.sha256(raw).hexdigest(),
                        deleted_at,
                    )
                )

    def _delete_time(self, raw: bytes, metadata: os.stat_result) -> int:
        """The marker's content, unless unusable or in the future; then the file's own time."""

        now_ms = self._clock_ns() // 1_000_000
        try:
            claimed = int(raw.decode("ascii").strip())
        except (UnicodeError, ValueError):
            claimed = -1
        if 0 <= claimed <= now_ms:
            return claimed
        return min(metadata.st_mtime_ns // 1_000_000, now_ms)

    def _read_replica(
        self,
        path: Path,
        target: Target,
        session_id: str,
        before: os.stat_result,
        started_ns: int,
    ) -> Optional[Replica]:
        """None means the file is gone. A file that cannot be read is unreadable, never absent."""

        name = normalisation()
        settled = started_ns - before.st_mtime_ns >= RACY_WINDOW_NS
        if self._hash_cache is not None and settled:
            cached = self._hash_cache.lookup_record(path, before, session_id, name)
            if cached is not None:
                digest, state_hash, activity = cached
                return self._replica(
                    session_id, target, path, before, digest, state_hash, activity
                )
        try:
            raw = path.read_bytes()
            after = path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            return Replica(session_id, target, path, before.st_size, before.st_mtime_ns, "")
        if _stat_signature(before) != _stat_signature(after) or len(raw) != after.st_size:
            # Claude saved it while it was read. Judge it next run, not now.
            return Replica(session_id, target, path, after.st_size, after.st_mtime_ns, "")
        copy = fingerprint(session_id, raw)
        digest = hashlib.sha256(raw).hexdigest()
        if copy.readable and self._hash_cache is not None:
            try:
                self._hash_cache.store_record(
                    path, after, session_id, digest, copy.state_hash,
                    copy.last_activity_at, name,
                )
            except OSError:
                pass  # the cache is disposable; the verdict above still stands
        return self._replica(
            session_id, target, path, after, digest, copy.state_hash, copy.last_activity_at
        )

    def _replica(self, session_id, target, path, metadata, digest, state_hash, activity):
        # Read the clock after the file: Claude stamps a busy session with the
        # current time, so a save during the scan must not look like the future.
        future = state_hash is not None and activity > self._clock_ns() // 1_000_000
        return Replica(
            session_id,
            target,
            path,
            metadata.st_size,
            metadata.st_mtime_ns,
            digest,
            state_hash,
            activity,
            future,
        )


def _read_small(path: Path, before: os.stat_result, limit: Optional[int]):
    if limit is not None and before.st_size > limit:
        return None
    try:
        raw = path.read_bytes()
        after = path.lstat()
    except OSError:
        return None
    if _stat_signature(before) != _stat_signature(after) or len(raw) != after.st_size:
        return None
    return raw, after


def _target_key(target: Target) -> Tuple[str, str, str]:
    return target.profile_name, target.account_id, target.workspace_id


def _replica_key(replica: Replica) -> Tuple[str, str, str, str]:
    return (
        replica.target.profile_name,
        replica.target.account_id,
        replica.target.workspace_id,
        replica.session_id,
    )
