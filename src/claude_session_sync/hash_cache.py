"""Metadata-keyed SHA-256 cache for validated replica files."""

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional, Union

from .filesystem import FileChangedError, ensure_private_directory

# Bump when the table changes. A cache with another version is dropped and
# rebuilt: it only saves rereading files.
SCHEMA_VERSION = 2


def _signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
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
            raise OSError(f"hash cache is not a regular file: {self.database_path}")
        self._connection = sqlite3.connect(str(self.database_path))
        os.chmod(str(self.database_path), 0o600)
        if (
            self._connection.execute("PRAGMA user_version").fetchone()[0]
            != SCHEMA_VERSION
        ):
            self._connection.execute("DROP TABLE IF EXISTS file_hashes")
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
        # One read of the whole table, then lookups in memory. A query per
        # file took about a millisecond on a loaded Mac: 32 seconds for 29,000
        # chat files, while Claude waited closed for a switch (20-10-2026).
        self._rows: dict[str, tuple[Any, ...]] | None = None

    def _loaded(self) -> dict[str, tuple[Any, ...]]:
        if self._rows is None:
            self._rows = {
                str(row[0]): ((row[1], row[2], row[3], row[4]),) + tuple(row[5:])
                for row in self._connection.execute(
                    """
                    SELECT path, size, mtime_ns, ctime_ns, inode, session_id, normalisation,
                           digest, state_hash, activity
                    FROM file_hashes
                    """
                )
            }
        return self._rows

    def lookup_record(
        self,
        path: Union[str, Path],
        stat_result: os.stat_result,
        session_id: str,
        normalisation: str,
    ) -> Optional[tuple[str, str, int]]:
        """Return (digest, state hash, activity) cached for these exact file metadata."""

        row = self._loaded().get(str(path))
        if row is None or row[:3] != (
            _signature(stat_result),
            session_id,
            normalisation,
        ):
            return None
        return str(row[3]), str(row[4]), int(row[5])

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
            raise FileChangedError(f"file changed before caching: {replica_path}")
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
                str(path),
                size,
                mtime_ns,
                ctime_ns,
                inode,
                session_id,
                digest,
                state_hash,
                activity,
                normalisation,
            ),
        )
        if self._rows is not None:
            self._rows[str(path)] = (
                (size, mtime_ns, ctime_ns, inode),
                session_id,
                normalisation,
                digest,
                state_hash,
                activity,
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
