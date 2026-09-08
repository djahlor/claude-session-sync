"""Read-only process inspection for managed Claude profile instances."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple


Runner = Callable[..., subprocess.CompletedProcess]
DEFAULT_CLAUDE_EXECUTABLE = Path("/Applications/Claude.app/Contents/MacOS/Claude")


@dataclass(frozen=True)
class ManagedProcess:
    """A running Claude process using one of the configured profile roots."""

    pid: int
    argv: Tuple[str, ...]
    user_data_dir: Path


def _normalized(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _user_data_dir(argv: Sequence[str]) -> Tuple[bool, Optional[Path]]:
    for index, argument in enumerate(argv[1:], start=1):
        if argument.startswith("--user-data-dir="):
            value = argument.partition("=")[2]
            return True, _normalized(Path(value)) if value else None
        if argument == "--user-data-dir":
            if index + 1 >= len(argv) or not argv[index + 1]:
                return True, None
            return True, _normalized(Path(argv[index + 1]))
    return False, None


def _raw_managed_user_data_dir(
    command: str, managed_roots: Iterable[Path]
) -> Optional[Path]:
    """Match configured roots in macOS ``ps`` output, which drops quoting.

    A live argument containing ``Application Support`` is emitted as raw text,
    so generic shell tokenization truncates it. Configured roots let us match
    the exact value without guessing where an arbitrary path ends.
    """

    for root in sorted(managed_roots, key=lambda item: len(str(item)), reverse=True):
        value = os.fspath(root)
        for prefix in ("--user-data-dir=", "--user-data-dir "):
            marker = prefix + value
            start = command.find(marker)
            if start < 0:
                continue
            before_ok = start == 0 or command[start - 1].isspace()
            end = start + len(marker)
            after_ok = end == len(command) or command[end:].startswith(" --")
            if before_ok and after_ok:
                return root
    return None


def parse_process_table(
    output: str,
    *,
    executable: Path,
    managed_profile_roots: Iterable[Path],
    default_profile_root: Optional[Path] = None,
) -> Tuple[ManagedProcess, ...]:
    """Parse ``ps -axo pid=,command=`` without substring process matching."""

    expected_executable = os.fspath(_normalized(executable))
    managed_roots = {_normalized(path) for path in managed_profile_roots}
    matches = []

    for line in output.splitlines():
        row = line.strip()
        if not row:
            continue
        pid_text, separator, command = row.partition(" ")
        if not separator:
            continue
        command = command.lstrip()
        try:
            pid = int(pid_text)
        except ValueError:
            continue

        # Claude's main executable is followed by Electron flags. Requiring the
        # first token after the exact path to be a flag also excludes executable
        # names such as ``Claude Helper`` and ``Claude-pretender``.
        if not command.startswith(expected_executable):
            continue
        suffix = command[len(expected_executable) :]
        if suffix and not suffix.lstrip().startswith("--"):
            continue

        try:
            argv = tuple(shlex.split(command, posix=True))
        except ValueError:
            continue
        if not argv or _normalized(Path(argv[0])) != Path(expected_executable):
            continue
        has_profile_argument, profile_root = _user_data_dir(argv)
        if has_profile_argument and profile_root not in managed_roots:
            raw_profile_root = _raw_managed_user_data_dir(command, managed_roots)
            if raw_profile_root is not None:
                profile_root = raw_profile_root
        if not has_profile_argument and default_profile_root is not None:
            profile_root = _normalized(default_profile_root)
        if profile_root is None or profile_root not in managed_roots:
            continue
        matches.append(ManagedProcess(pid, argv, profile_root))

    return tuple(matches)


class ProcessProbe:
    """System-boundary adapter for a single, immutable process snapshot."""

    def __init__(self, runner: Runner = subprocess.run) -> None:
        self._runner = runner

    def running(
        self,
        *,
        executable: Path,
        managed_profile_roots: Iterable[Path],
        default_profile_root: Optional[Path] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[ManagedProcess, ...]:
        options = {
            "check": True,
            "text": True,
            "capture_output": True,
        }
        deadline = None if timeout is None else time.monotonic() + timeout
        if timeout is not None:
            options["timeout"] = min(2.0, timeout)
        command = ["ps", "-axo", "pid=,command="]
        try:
            completed = self._runner(command, **options)
        except subprocess.TimeoutExpired:
            # Retry only this read, never a write, and share the caller's total
            # deadline so exit/launch safety checks remain bounded.
            remaining = 0 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                raise
            options["timeout"] = remaining
            completed = self._runner(command, **options)
        return parse_process_table(
            completed.stdout,
            executable=executable,
            managed_profile_roots=managed_profile_roots,
            default_profile_root=default_profile_root,
        )

    def any_running(
        self,
        *,
        executable: Path,
        managed_profile_roots: Iterable[Path],
        default_profile_root: Optional[Path] = None,
    ) -> bool:
        return bool(
            self.running(
                executable=executable,
                managed_profile_roots=managed_profile_roots,
                default_profile_root=default_profile_root,
            )
        )


def managed_processes(
    profiles: Iterable[object],
    *,
    probe: Optional[ProcessProbe] = None,
    executable: Path = DEFAULT_CLAUDE_EXECUTABLE,
    timeout: Optional[float] = None,
) -> Tuple[ManagedProcess, ...]:
    """Probe profile-like objects without conflating launch and process commands."""

    roots = []
    default_profile_root = None
    for profile in profiles:
        roots.append(Path(getattr(profile, "data_root")))
        if bool(getattr(profile, "is_default", False)):
            if default_profile_root is not None:
                raise ValueError("only one managed profile may be the default")
            default_profile_root = roots[-1]
    if not roots:
        return ()
    options = {
        "executable": executable,
        "managed_profile_roots": roots,
        "default_profile_root": default_profile_root,
    }
    if timeout is not None:
        options["timeout"] = timeout
    return (probe or ProcessProbe()).running(
        **options,
    )
