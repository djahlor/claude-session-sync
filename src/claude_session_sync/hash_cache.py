"""Metadata-keyed SHA-256 cache for validated replica files."""

import hashlib
import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple, Union

from .filesystem import ensure_private_directory


class FileChangedError(OSError):
    """Raised when a file changes while it is being inspected."""


def _signature(stat_result: os.stat_result) -> Tuple[int, int, int, int]:
    return (
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
        stat_result.st_ino,
    )


class HashCache:
    """Caches digests only when all available identity metadata still matches."""

    def __init__(self, database_path: Union[str, Path]) -> None:
        self.database_path = Path(database_path)
        ensure_private_directory(self.database_path.parent)
        if self.database_path.is_symlink() or (
            os.path.lexists(str(self.database_path))
            and not self.database_path.is_file()
        ):
            raise OSError(
                "hash cache is not a regular file: {}".format(self.database_path)
            )
        self._connection = sqlite3.connect(str(self.database_path))
        os.chmod(str(self.database_path), 0o600)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS file_hashes (
                path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL,
                inode INTEGER NOT NULL,
                session_id TEXT,
                digest TEXT NOT NULL,
                PRIMARY KEY (path, size, mtime_ns, ctime_ns, inode)
            )
            """
        )
        columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(file_hashes)")
        }
        if "session_id" not in columns:
            self._connection.execute(
                "ALTER TABLE file_hashes ADD COLUMN session_id TEXT"
            )
        # Validation rules changed to reject duplicate keys and non-finite numbers.
        # Keep digest caching, but never reuse validation from the older reader.
        if self._connection.execute("PRAGMA user_version").fetchone()[0] < 1:
            self._connection.execute("UPDATE file_hashes SET session_id = NULL")
            self._connection.execute("PRAGMA user_version = 1")
        self._connection.commit()

    def digest(
        self,
        path: Union[str, Path],
        data: Optional[bytes] = None,
        stat_result: Optional[os.stat_result] = None,
    ) -> str:
        replica_path = Path(path)
        before = stat_result if stat_result is not None else replica_path.lstat()
        if replica_path.is_symlink():
            raise OSError("refusing to hash symlink: {}".format(replica_path))
        cached = self._lookup(replica_path, before, None)
        if cached is not None:
            return cached
        content = data if data is not None else replica_path.read_bytes()
        after = replica_path.lstat()
        if _signature(before) != _signature(after) or len(content) != after.st_size:
            raise FileChangedError(
                "file changed while hashing: {}".format(replica_path)
            )

        digest = hashlib.sha256(content).hexdigest()
        self._store(replica_path, after, None, digest)
        return digest

    def lookup_validated(
        self,
        path: Union[str, Path],
        stat_result: os.stat_result,
        expected_session_id: str,
    ) -> Optional[str]:
        return self._lookup(Path(path), stat_result, expected_session_id)

    def store_validated(
        self,
        path: Union[str, Path],
        stat_result: os.stat_result,
        session_id: str,
        digest: str,
    ) -> None:
        replica_path = Path(path)
        current = replica_path.lstat()
        if _signature(current) != _signature(stat_result):
            raise FileChangedError(
                "file changed before caching: {}".format(replica_path)
            )
        self._store(replica_path, current, session_id, digest)

    def _lookup(
        self,
        path: Path,
        stat_result: os.stat_result,
        expected_session_id: Optional[str],
    ) -> Optional[str]:
        size, mtime_ns, ctime_ns, inode = _signature(stat_result)
        if expected_session_id is None:
            row = self._connection.execute(
                """
                SELECT digest FROM file_hashes
                WHERE path = ? AND size = ? AND mtime_ns = ? AND ctime_ns = ? AND inode = ?
                """,
                (str(path), size, mtime_ns, ctime_ns, inode),
            ).fetchone()
        else:
            row = self._connection.execute(
                """
                SELECT digest FROM file_hashes
                WHERE path = ? AND size = ? AND mtime_ns = ? AND ctime_ns = ? AND inode = ?
                  AND session_id = ?
                """,
                (str(path), size, mtime_ns, ctime_ns, inode, expected_session_id),
            ).fetchone()
        return None if row is None else str(row[0])

    def _store(
        self,
        path: Path,
        stat_result: os.stat_result,
        session_id: Optional[str],
        digest: str,
    ) -> None:
        size, mtime_ns, ctime_ns, inode = _signature(stat_result)
        # One transaction spans a discovery pass. Committing every validated
        # replica made a cold 5,000-file plan spend most of its time fsyncing a
        # disposable cache rather than validating Claude data.
        self._connection.execute("DELETE FROM file_hashes WHERE path = ?", (str(path),))
        self._connection.execute(
            """
            INSERT INTO file_hashes(path, size, mtime_ns, ctime_ns, inode, session_id, digest)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(path), size, mtime_ns, ctime_ns, inode, session_id, digest),
        )

    def close(self) -> None:
        try:
            self._connection.commit()
        finally:
            self._connection.close()

    def __enter__(self) -> "HashCache":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
