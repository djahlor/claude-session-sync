"""Choose which sidebar folders take part in sync.

Under the ``logins`` policy only real logins take part: the approved targets,
plus any folder Claude itself wrote a chat to after its account logged in on
this Mac, plus a folder Claude created after that login and that holds no chat
older than it (a brand new account's own folder starts empty). Leftover
folders from other Macs never had a login here, so they stay out. Whether Claude runs, and which account is signed in now, do not
matter.
"""

import os
from pathlib import Path
from typing import Iterable, List, Sequence, Set, Tuple

from .filesystem import normalized_path
from .logins import Logins


# A chat Claude saved this long after the login began was written by Claude,
# not copied in during the switch.
LOGIN_SETTLE_MS = 5_000


def target_key(target) -> str:
    return "{}/{}/{}".format(target.profile_name, target.account_id, target.workspace_id)


def approved_keys(config) -> Set[str]:
    return {
        "{}/{}/{}".format(target.profile_name, target.account_id, target.workspace_id)
        for target in config.approved_targets
    }


def auto_enrolled(config) -> List[str]:
    """Folders that joined after a login. An unreadable state adds none."""

    from .chat_state import StateUnusable, load_state, state_path

    try:
        return list(load_state(state_path(config.state_dir)).enrolled)
    except StateUnusable:
        return []


def selected_targets(config, targets: Sequence) -> tuple:
    return select_targets(config, targets, auto_enrolled(config))[0]


def select_targets(config, targets: Sequence, auto_enrolled: Iterable[str]) -> Tuple[tuple, tuple]:
    """Return (selected, ignored) for the configured policy."""

    if config.target_policy == "all-configured-profiles":
        return tuple(targets), ()
    wanted = approved_keys(config)
    if config.target_policy == "logins":
        wanted |= set(auto_enrolled)
    selected = tuple(target for target in targets if target_key(target) in wanted)
    ignored = tuple(target for target in targets if target_key(target) not in wanted)
    return selected, ignored


def new_login_targets(
    config,
    targets: Sequence,
    selected_keys: Set[str],
    logins: Logins,
) -> List[str]:
    """Folders outside sync that Claude wrote for their account after its first login here."""

    if config.target_policy != "logins":
        return []
    roots = {profile.name: normalized_path(profile.data_root) for profile in config.profiles}
    found = []
    for target in targets:
        key = target_key(target)
        root = roots.get(target.profile_name)
        if key in selected_keys or root is None:
            continue
        first_login = logins.get(str(root), {}).get(target.account_id.lower())
        if first_login is None:
            continue
        since_ms = first_login + LOGIN_SETTLE_MS
        folder = Path(target.path)
        if _written_since(folder, since_ms) or _created_since(folder, since_ms, first_login):
            found.append(key)
    return sorted(found)


def _written_since(folder: Path, since_ms: int) -> bool:
    try:
        entries = list(os.scandir(str(folder)))
    except OSError:
        return False
    for entry in entries:
        if not (entry.name.startswith("local_") and entry.name.endswith(".json")):
            continue
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_mtime_ns // 1_000_000 > since_ms:
                return True
        except OSError:
            continue
    return False


def _created_since(folder: Path, since_ms: int, first_login_ms: int) -> bool:
    """A folder Claude made after the login, with no chat that predates it.

    A new account has no chat until its first one, so the first folder Claude
    makes for it is empty. A folder with an older chat was copied in from
    somewhere else.
    """

    try:
        born_ms = int(folder.stat().st_birthtime * 1000)
        entries = list(os.scandir(str(folder)))
    except (AttributeError, OSError):
        return False
    if born_ms <= since_ms:
        return False
    for entry in entries:
        if not (entry.name.startswith("local_") and entry.name.endswith(".json")):
            continue
        try:
            if entry.stat(follow_symlinks=False).st_mtime_ns // 1_000_000 <= first_login_ms:
                return False
        except OSError:
            return False
    return True
