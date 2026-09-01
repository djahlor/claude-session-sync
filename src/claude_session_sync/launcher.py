"""Launch configured Claude profiles without shell interpretation."""

from __future__ import annotations

import subprocess
from typing import Any, Callable, Sequence


class Launcher:
    """Small subprocess boundary that accepts an argv sequence, never a shell."""

    def __init__(self, popen: Callable[..., Any] = subprocess.Popen) -> None:
        self._popen = popen

    def launch(self, command: Sequence[str]) -> Any:
        if not command or any(
            not isinstance(part, str) or not part for part in command
        ):
            raise ValueError("launch command must be a non-empty argv sequence")
        return self._popen(
            list(command),
            close_fds=True,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
