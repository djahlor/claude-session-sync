"""Decide whether a running Claude may hold a sidebar folder in memory.

Ported from vinlim/claude-desktop-sync (0BSD). Claude keeps the signed-in
account's chats in memory and writes them back from memory, so sync may only
add missing files there. Claude records a new login before it flushes the old
login's saves, so every folder counts as live for two minutes after a switch.
"""

import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

from . import strict_json as json


SWITCH_GRACE_MS = 120_000
APP_LOG_TAIL = 16 * 1024 * 1024  # Claude rotates main.log at about 10 MB
MAX_CONFIG_BYTES = 8 * 1024 * 1024
# A logout ends in "uuid: X → <none>", so the uuid alone tells the two apart.
LOGIN_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) .*Login-state transition \(loggedOut: [^,]+, "
    r"uuid: \S+ \u2192 ([0-9A-Fa-f-]{36})\)"
)

Logins = Dict[str, Tuple[str, int]]


def default_app_log() -> Path:
    return Path.home() / "Library" / "Logs" / "Claude" / "main.log"


def last_known_account(data_root: Path) -> Optional[str]:
    """Read only lastKnownAccountUuid. The rest of this file can hold credentials."""

    path = Path(data_root) / "config.json"
    try:
        if path.is_symlink() or path.stat().st_size > MAX_CONFIG_BYTES:
            return None
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    value = document.get("lastKnownAccountUuid") if isinstance(document, dict) else None
    return value.lower() if isinstance(value, str) and value else None


def config_write_in_flight(data_root: Path) -> bool:
    return os.path.lexists(str(Path(data_root) / "config.json.journal"))


def login_dated_by_app(app_log: Path, account: str, now_ms: int) -> Optional[int]:
    """When the log last recorded a login to this account, or None if its end does not say."""

    try:
        with open(str(app_log), "rb") as handle:
            handle.seek(max(0, os.fstat(handle.fileno()).st_size - APP_LOG_TAIL))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        found = LOGIN_LINE.match(line)
        if found is None:
            continue
        if found.group(2).lower() != account.lower():
            return None
        try:
            stamped = int(time.mktime(time.strptime(found.group(1), "%Y-%m-%d %H:%M:%S"))) * 1000
        except (ValueError, OverflowError):
            return None
        return min(stamped, now_ms)
    return None


def observe_logins(
    roots: Iterable[Path],
    logins: Logins,
    now_ms: int,
    app_log_for: Callable[[Path], Optional[Path]],
) -> None:
    """Date each root's login by Claude's log where it names it, else by first sighting.

    Claude rewrites config.json about once a minute for other reasons, so its
    modification time cannot date a login.
    """

    for root in {_normalized(Path(root)) for root in roots}:
        account = last_known_account(root)
        if account is None:
            continue
        app_log = app_log_for(root)
        dated = login_dated_by_app(app_log, account, now_ms) if app_log is not None else None
        known = logins.get(str(root))
        if known is None or known[0] != account:
            logins[str(root)] = (account, now_ms if dated is None else dated)
        elif dated is not None and dated > known[1]:
            logins[str(root)] = (account, dated)


def is_live(
    data_root: Path,
    account_id: str,
    running: bool,
    now_ms: int,
    logins: Logins,
) -> bool:
    if not running:
        return False
    if config_write_in_flight(data_root):
        return True  # the login on disk may still be the old one
    account = last_known_account(data_root)
    if account is None or account == account_id.lower():
        return True
    seen = logins.get(str(data_root))
    if seen is None or seen[0] != account:
        return True  # a login sync has not dated yet
    return now_ms - seen[1] < SWITCH_GRACE_MS


class Liveness:
    """Answer liveness for sidebar folders, re-reading processes at most every half second."""

    def __init__(
        self,
        running_roots: Callable[[], Set[Path]],
        logins: Logins,
        *,
        clock_ms: Callable[[], int],
        monotonic: Callable[[], float] = time.monotonic,
        cache_seconds: float = 0.5,
    ) -> None:
        self._running_roots = running_roots
        self._logins = logins
        self._clock_ms = clock_ms
        self._monotonic = monotonic
        self._cache_seconds = cache_seconds
        self._cached: Optional[Tuple[float, Set[Path]]] = None

    def running(self) -> Set[Path]:
        now = self._monotonic()
        if self._cached is None or now - self._cached[0] > self._cache_seconds:
            self._cached = (now, {_normalized(root) for root in self._running_roots()})
        return self._cached[1]

    def partition_is_live(self, data_root: Path, account_id: str) -> bool:
        root = _normalized(data_root)
        return is_live(root, account_id, root in self.running(), self._clock_ms(), self._logins)


def _normalized(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
