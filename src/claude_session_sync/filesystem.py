"""Durable filesystem primitives used by synchronization transactions."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path
from typing import BinaryIO, Union


PathLike = Union[str, os.PathLike]


class UnsafePathError(OSError):
    """Raised when transaction data is not a regular, non-symlink file."""


def ensure_private_directory(path: PathLike) -> Path:
    directory = Path(path)
    missing = []
    candidate = directory
    while not os.path.lexists(str(candidate)):
        missing.append(candidate)
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise UnsafePathError(
            "state directory is not a real directory: {}".format(directory)
        )
    os.chmod(str(directory), 0o700)
    for created in reversed(missing):
        os.chmod(str(created), 0o700)
    for created in missing:
        fsync_directory(created.parent)
    return directory


def ensure_private_file(path: PathLike) -> None:
    os.chmod(os.fspath(path), 0o600)


def _open_regular_read(path: PathLike) -> BinaryIO:
    file_path = Path(path)
    # Reject FIFOs after opening without waiting for a writer indefinitely.
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(file_path), flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafePathError("not a regular file: {}".format(file_path))
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def digest_file(path: PathLike) -> str:
    digest = hashlib.sha256()
    with _open_regular_read(path) as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def regular_file_size(path: PathLike) -> int:
    with _open_regular_read(path) as source:
        return os.fstat(source.fileno()).st_size


def fsync_directory(path: PathLike) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(os.fspath(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _temporary_path(destination: Path, suffix: str) -> Path:
    return destination.parent / ".{}.{}.{}".format(
        destination.name, uuid.uuid4().hex, suffix
    )


def stage_copy(source: PathLike, destination: PathLike) -> Path:
    """Copy to a private temporary file beside destination and fsync it."""

    destination_path = Path(destination)
    if destination_path.parent.is_symlink() or not destination_path.parent.is_dir():
        raise UnsafePathError(
            "destination parent is not a real directory: {}".format(
                destination_path.parent
            )
        )
    temporary = _temporary_path(destination_path, "stage")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with (
            _open_regular_read(source) as input_file,
            os.fdopen(descriptor, "wb") as output_file,
        ):
            descriptor = -1
            while True:
                chunk = input_file.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return temporary


def commit_staged(staged: PathLike, destination: PathLike) -> None:
    destination_path = Path(destination)
    os.replace(os.fspath(staged), str(destination_path))
    fsync_directory(destination_path.parent)


def atomic_copy(source: PathLike, destination: PathLike) -> None:
    destination_path = Path(destination)
    staged = stage_copy(source, destination_path)
    try:
        commit_staged(staged, destination_path)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def atomic_write_bytes(destination: PathLike, content: bytes) -> None:
    destination_path = Path(destination)
    if destination_path.parent.is_symlink() or not destination_path.parent.is_dir():
        raise UnsafePathError(
            "destination parent is not a real directory: {}".format(
                destination_path.parent
            )
        )
    temporary = _temporary_path(destination_path, "write")
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output_file:
            descriptor = -1
            output_file.write(content)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(str(temporary), str(destination_path))
        fsync_directory(destination_path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def durable_unlink(path: PathLike) -> None:
    file_path = Path(path)
    file_path.unlink()
    fsync_directory(file_path.parent)
