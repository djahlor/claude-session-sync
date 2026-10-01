"""Launch configured Claude profiles without shell interpretation."""

from __future__ import annotations

import subprocess
from typing import Any, Callable, Sequence


LAUNCH_TIMEOUT_SECONDS = 10.0


class LaunchError(RuntimeError):
    """The launch command could not start, or it exited with a failure."""


class Launcher:
    """Small subprocess boundary that accepts an argv sequence, never a shell."""

    def __init__(
        self,
        popen: Callable[..., Any] = subprocess.Popen,
        timeout: float = LAUNCH_TIMEOUT_SECONDS,
    ) -> None:
        self._popen = popen
        self._timeout = timeout

    def launch(self, command: Sequence[str]) -> Any:
        if not command or any(
            not isinstance(part, str) or not part for part in command
        ):
            raise ValueError("launch command must be a non-empty argv sequence")
        try:
            process = self._popen(
                list(command),
                close_fds=True,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise LaunchError("launch command could not start") from error
        try:
            status = process.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            # `open` returns once macOS has started the app. A profile whose
            # command is the Claude executable itself never returns, so a
            # command still running here has launched and is left running.
            return process
        if status != 0:
            raise LaunchError("launch command exited with status {}".format(status))
        return process
