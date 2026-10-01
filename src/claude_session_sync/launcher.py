"""Launch configured Claude profiles without shell interpretation."""

from __future__ import annotations

import subprocess
from typing import Sequence


LAUNCH_TIMEOUT_SECONDS = 10.0


class LaunchError(RuntimeError):
    """The launch command could not start, failed, or did not finish in time."""


class Launcher:
    """Run a profile's launch command as an argv list, never through a shell.

    Every launch command is `open`, which returns once macOS has started Claude.
    """

    def __init__(self, timeout: float = LAUNCH_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    def launch(self, command: Sequence[str]) -> None:
        try:
            subprocess.run(
                list(command),
                check=True,
                timeout=self._timeout,
                close_fds=True,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise LaunchError("launch command could not start") from error
        except subprocess.CalledProcessError as error:
            raise LaunchError(
                "launch command exited with status {}".format(error.returncode)
            ) from error
        except subprocess.TimeoutExpired as error:
            raise LaunchError("launch command did not finish in time") from error
