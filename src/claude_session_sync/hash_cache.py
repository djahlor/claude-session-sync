"""Metadata-keyed SHA-256 cache for validated replica files."""

import os
import sqlite3
from pathlib import Path
from typing import Optional, Tuple, Union

from .filesystem import ensure_private_directory


# Bump when the table changes. A cache with another version is dropped and
# rebuilt: it only saves rereading files.
SCHEMA_VERSION = 2


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
        if self._connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self._connection.execute("DROP TABLE IF EXISTS file_hashes")
            self._connection.execute("PRAGMA user_version = {}".format(SCHEMA_VERSION))
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
                state_hash TEXT,
                activity INTEGER,
                normalisation TEXT,
                PRIMARY KEY (path, size, mtime_ns, ctime_ns, inode)
            )
            """
        )
        self._connection.commit()

    def lookup_record(
        self,
        path: Union[str, Path],
        stat_result: os.stat_result,
        session_id: str,
        normalisation: str,
    ) -> Optional[Tuple[str, str, int]]:
        """Return (digest, state hash, activity) cached for these exact file metadata."""

        size, mtime_ns, ctime_ns, inode = _signature(stat_result)
        row = self._connection.execute(
            """
            SELECT digest, state_hash, activity FROM file_hashes
            WHERE path = ? AND size = ? AND mtime_ns = ? AND ctime_ns = ? AND inode = ?
              AND session_id = ? AND normalisation = ?
            """,
            (str(path), size, mtime_ns, ctime_ns, inode, session_id, normalisation),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]), int(row[2])

    def store_record(
        self,
        path: Union[str, Path],
        stat_result: os.stat_result,
        session_id: str,
        digest: str,
        state_hash: str,
        activity: int,
        normalisation: str,
    ) -> None:
        replica_path = Path(path)
        current = replica_path.lstat()
        if _signature(current) != _signature(stat_result):
            raise FileChangedError(
                "file changed before caching: {}".format(replica_path)
            )
        size, mtime_ns, ctime_ns, inode = _signature(current)
        # One transaction spans a discovery pass. Committing every validated
        # replica made a cold 5,000-file plan spend most of its time fsyncing a
        # disposable cache rather than validating Claude data.
        self._connection.execute("DELETE FROM file_hashes WHERE path = ?", (str(path),))
        self._connection.execute(
            """
            INSERT INTO file_hashes(
                path, size, mtime_ns, ctime_ns, inode, session_id, digest,
                state_hash, activity, normalisation
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(path), size, mtime_ns, ctime_ns, inode, session_id, digest,
                state_hash, activity, normalisation,
            ),
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
