"""Read which accounts signed in to Claude on this Mac, from Claude's own files.

Ported from vinlim/claude-desktop-sync (0BSD). Only the account id is read from
Claude's config.json, and only login lines from its main.log.
"""

import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from . import strict_json as json
from .filesystem import normalized_path


APP_LOG_TAIL = 16 * 1024 * 1024  # Claude rotates main.log at about 10 MB
MAX_CONFIG_BYTES = 8 * 1024 * 1024
# A logout ends in "uuid: X → <none>", so the uuid alone tells the two apart.
LOGIN_LINE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) .*Login-state transition \(loggedOut: [^,]+, "
    r"uuid: \S+ \u2192 ([0-9A-Fa-f-]{36})\)"
)

# data root -> account -> its first login this Mac recorded, in epoch ms
Logins = Dict[str, Dict[str, int]]


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


def logins_in_app_log(app_log: Path, now_ms: int) -> List[Tuple[str, int]]:
    """Every login the end of Claude's log records, as (account, epoch ms), oldest first."""

    try:
        with open(str(app_log), "rb") as handle:
            handle.seek(max(0, os.fstat(handle.fileno()).st_size - APP_LOG_TAIL))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    found = []
    for line in tail.splitlines():
        match = LOGIN_LINE.match(line)
        if match is None:
            continue
        try:
            stamped = int(time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))) * 1000
        except (ValueError, OverflowError):
            continue
        found.append((match.group(2).lower(), min(stamped, now_ms)))
    return found


def record_logins(
    roots: Iterable[Path],
    logins: Logins,
    now_ms: int,
    app_log_for: Callable[[Path], Optional[Path]],
) -> None:
    """Remember each account's first login per data root, so log rotation cannot lose it.

    Claude's log dates a login. A signed-in account the log does not name
    counts from the first time sync sees it. Claude rewrites config.json about
    once a minute for other reasons, so its modification time cannot date a
    login.
    """

    for root in {normalized_path(root) for root in roots}:
        app_log = app_log_for(root)
        seen = list(logins_in_app_log(app_log, now_ms)) if app_log is not None else []
        signed_in = last_known_account(root)
        if signed_in is not None:
            seen.append((signed_in, now_ms))
        for account, login_ms in seen:
            known = logins.setdefault(str(root), {})
            known[account] = min(login_ms, known.get(account, login_ms))
