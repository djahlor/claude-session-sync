"""Read which accounts signed in to Claude on this Mac, from Claude's own files.

Ported from vinlim/claude-desktop-sync (0BSD). Only the account id is read from
Claude's config.json, and only login lines from its main.log.
"""

import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

from . import strict_json as json


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


def _normalized(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
