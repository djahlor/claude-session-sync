"""Start chat sync from the last synced version instead of from nothing.

With no memory of the last agreed version, two differing copies can only be
judged by activity, and a rename or an archive moves no activity. Folders left
out of sync (old accounts, leftovers from another Mac) still hold the version
from the last full sync. Where they all agree, that version becomes the agreed
starting point. Nothing outside the private state file is written.
"""

import hashlib
import os
from pathlib import Path
from typing import Dict, Set

from .chat_state import load_state, save_state, state_path
from .enrollment import select_targets, target_key
from .fingerprint import fingerprint
from .store import RECORD_NAME, SessionStore


def seed_from_unenrolled(config, *, apply: bool) -> dict:
    state = load_state(state_path(config.state_dir))
    if state.sync.agreed:
        raise ValueError("chat state already has agreed versions; seeding only starts a new state")
    found = SessionStore().discover_targets(config)
    selected, ignored = select_targets(config, found.targets, state.enrolled)
    if config.target_policy != "logins" or not ignored:
        raise ValueError("seeding needs the logins policy and folders outside sync")

    present: Dict[str, Set[str]] = {}
    for target in selected:
        present[target_key(target)] = _record_ids(Path(target.path))
    wanted = set().union(*present.values()) if present else set()

    versions: Dict[str, Set[str]] = {}
    unreadable: Set[str] = set()
    cache: Dict[str, str] = {}
    for target in ignored:
        folder = Path(target.path)
        for session_id in sorted(wanted & _record_ids(folder)):
            path = folder / "local_{}.json".format(session_id)
            try:
                raw = path.read_bytes()
            except OSError:
                unreadable.add(session_id)
                continue
            digest = hashlib.sha256(raw).hexdigest()
            state_hash = cache.get(digest)
            if state_hash is None:
                copy = fingerprint(session_id, raw)
                if not copy.readable:
                    unreadable.add(session_id)
                    continue
                state_hash = copy.state_hash
                cache[digest] = state_hash
            versions.setdefault(session_id, set()).add(state_hash)

    seeded = {
        session_id: hashes.pop()
        for session_id, hashes in versions.items()
        if len(hashes) == 1 and session_id not in unreadable
    }
    disagree = sum(
        1 for session_id, hashes in versions.items() if session_id not in seeded
    )
    if apply:
        state.sync.agreed.update(seeded)
        for key, ids in present.items():
            state.sync.seen.setdefault(key, set()).update(ids)
        save_state(state_path(config.state_dir), state)
    return {
        "state": "seeded" if apply else "planned",
        "counts": {
            "chats": len(wanted),
            "seeded": len(seeded),
            "old_copies_disagree": disagree,
            "no_old_copy": len(wanted) - len(versions),
            "old_folders": len(ignored),
        },
    }


def _record_ids(folder: Path) -> Set[str]:
    ids = set()
    try:
        entries = list(os.scandir(str(folder)))
    except OSError:
        return ids
    for entry in entries:
        match = RECORD_NAME.match(entry.name)
        if match and entry.is_file(follow_symlinks=False):
            ids.add(match.group(1))
    return ids
