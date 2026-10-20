"""The bundled helper that reads and writes Claude's sidebar records in LevelDB."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .filesystem import (
    ensure_private_directory,
)
from .layout_records import (
    LayoutError,
)


class LevelDatabase:
    """Narrow process adapter for the bundled LevelDB helper."""

    def __init__(self, helper: Path, database: Path) -> None:
        self.helper = Path(helper)
        self.database = Path(database)

    def get(self, key: bytes) -> bytes:
        value = self.get_optional(key)
        if value is None:
            raise LayoutError("required sidebar record is missing")
        return value

    def get_optional(self, key: bytes) -> bytes | None:
        result = subprocess.run(
            [str(self.helper), "get", str(self.database), key.hex()],
            check=False,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 3:
            return None
        if result.returncode != 0:
            raise LayoutError("Claude sidebar record could not be read safely")
        encoded = result.stdout.strip()
        try:
            return bytes.fromhex(encoded)
        except ValueError as error:
            raise LayoutError("sidebar helper returned malformed data") from error

    def write(self, records: Mapping[bytes, bytes | None], state_dir: Path) -> None:
        ensure_private_directory(state_dir)
        descriptor, name = tempfile.mkstemp(prefix=".layout-ops-", dir=str(state_dir))
        path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                for key, value in sorted(records.items()):
                    if value is None:
                        handle.write(f"D\t{key.hex()}\n")
                    else:
                        handle.write(f"P\t{key.hex()}\t{value.hex()}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, 0o600)
            result = subprocess.run(
                [str(self.helper), "batch", str(self.database), str(path)],
                check=False,
                text=True,
                capture_output=True,
                timeout=20,
            )
            if result.returncode != 0:
                raise LayoutError("Claude sidebar records could not be written safely")
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
