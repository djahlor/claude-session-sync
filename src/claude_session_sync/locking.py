"""Cross-process single-writer locking."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import time
from pathlib import Path
from typing import Optional, Union

from .filesystem import ensure_private_directory


PathLike = Union[str, os.PathLike]


class LockUnavailableError(RuntimeError):
    """Raised when the transaction lock cannot be acquired as requested."""


class ExclusiveFileLock:
    def __init__(
        self,
        path: PathLike,
        *,
        mode: str = "auto",
        timeout: float = 5.0,
        poll_interval: float = 0.05,
    ) -> None:
        if mode not in ("auto", "manual"):
            raise ValueError("lock mode must be 'auto' or 'manual'")
        if timeout < 0:
            raise ValueError("lock timeout cannot be negative")
        self.path = Path(path)
        self.mode = mode
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._descriptor: Optional[int] = None

    def acquire(self) -> "ExclusiveFileLock":
        ensure_private_directory(self.path.parent)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(self.path), flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise LockUnavailableError("transaction lock is not a regular file")
        os.fchmod(descriptor, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._descriptor = descriptor
                return self
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(descriptor)
                    raise
                if self.mode == "auto" or time.monotonic() >= deadline:
                    os.close(descriptor)
                    raise LockUnavailableError(
                        "another synchronization transaction holds {}".format(self.path)
                    )
                time.sleep(
                    min(self.poll_interval, max(0.0, deadline - time.monotonic()))
                )

    def release(self) -> None:
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "ExclusiveFileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
