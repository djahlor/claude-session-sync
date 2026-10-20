"""Settle chats whose copies differ only in what was done to the chat. Pure.

Claude archives a chat, records its pull requests and renames it without moving
lastActivityAt. When copies in several folders changed like that, they differ
at the same activity time, and the rules leave the chat alone as a tie. On
20-10-2026 that held 329 chats, 224 of them only on whether they were archived.

A copy wins a tie only when it already holds everything every other leading
copy holds: archived if any copy is, each pull request at the same or a later
state, the latest title, every trusted git folder and every published page.
Anything else that differs keeps the tie, so a tie-break never drops data.
"""

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from .fingerprint import VOLATILE_KEYS

# State of the copy that ran the chat or of the account that holds it, never
# something to keep across folders: remote steering and Remote Control ids, an
# interruption wait, a reminder waiting for the next turn, error and spawn
# marks, the subagent truncation marker and a resolved folder path.
RUNTIME_KEYS = frozenset(
    {
        "bridgeSessionIds",
        "gitAnchorsFolderRealpath",
        "interruptedAwaitingPerson",
        "lastSpawnRootDetected",
        "pendingSystemReminder",
        "priorErrorMark",
        "remoteControlUserEnabled",
        "steeredByRemoteClient",
        "subagentsTruncatedFor",
    }
) | frozenset(VOLATILE_KEYS)
PULL_REQUEST_KEYS = frozenset({"prNumber", "prRepository", "prState", "prUrl", "prs"})
TITLE_KEYS = frozenset({"title", "titleSource", "titleTurn", "previousTitles"})
PR_STATE_ORDER = {"OPEN": 0, "CLOSED": 1, "MERGED": 2}


def tie_break(copies: Mapping[str, Mapping[str, Any]]) -> Optional[str]:
    """The folder key of a copy that covers all the others, or None."""

    for key in sorted(copies):
        if all(
            covers(copies[key], other) for name, other in copies.items() if name != key
        ):
            return key
    return None


def covers(winner: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    differing = {
        key
        for key in set(winner) | set(other)
        if key not in RUNTIME_KEYS and winner.get(key) != other.get(key)
    }
    if not differing:
        return True
    if "isArchived" in differing:
        if winner.get("isArchived") is not True:
            return False
        differing.discard("isArchived")
    if differing & PULL_REQUEST_KEYS:
        if not _covers_pull_requests(winner, other):
            return False
        differing -= PULL_REQUEST_KEYS
    if differing & TITLE_KEYS:
        if not _covers_title(winner, other):
            return False
        differing -= TITLE_KEYS
    if "gitAnchors" in differing:
        if not _contains(
            winner.get("gitAnchors"), other.get("gitAnchors"), ("gitRoot", "commonDir")
        ):
            return False
        differing.discard("gitAnchors")
    if "publishedArtifacts" in differing:
        if not _covers_artifacts(
            winner.get("publishedArtifacts"), other.get("publishedArtifacts")
        ):
            return False
        differing.discard("publishedArtifacts")
    return not differing


def _rank(state: Any) -> int:
    return PR_STATE_ORDER.get(state, -1) if isinstance(state, str) else -1


def _pull_requests(record: Mapping[str, Any]) -> dict:
    found = {}
    items = record.get("prs")
    for item in items if isinstance(items, list) else ():
        if isinstance(item, dict) and isinstance(item.get("url"), str):
            found[item["url"]] = item
    if isinstance(record.get("prUrl"), str) and record["prUrl"] not in found:
        found[record["prUrl"]] = {
            "url": record["prUrl"],
            "state": record.get("prState"),
        }
    return found


def _covers_pull_requests(winner: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    mine, theirs = _pull_requests(winner), _pull_requests(other)
    # Newer Claude keeps pull requests only in prs and drops the single
    # prNumber/prState/prUrl fields, so those count as one more entry.
    for url, item in theirs.items():
        held = mine.get(url)
        if held is None or _rank(held.get("state")) < _rank(item.get("state")):
            return False
        if item.get("dismissed") is True and held.get("dismissed") is not True:
            return False
    return True


def _turn(record: Mapping[str, Any]) -> int:
    value = record.get("titleTurn")
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _covers_title(winner: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    """A title named at a later turn wins, then a person's rename over an automatic one."""

    if _turn(winner) != _turn(other):
        return _turn(winner) > _turn(other)
    if winner.get("title") == other.get("title"):
        return set(_strings(other.get("previousTitles"))) <= set(
            _strings(winner.get("previousTitles"))
        )
    if winner.get("titleSource") == "user" and other.get("titleSource") != "user":
        return True
    return other.get("title") in _strings(winner.get("previousTitles"))


def _strings(values: Any) -> Iterable[str]:
    return (
        [value for value in values if isinstance(value, str)]
        if isinstance(values, list)
        else []
    )


def _contains(mine: Any, theirs: Any, identity: tuple) -> bool:
    def keys(items: Any) -> set:
        return {
            tuple(item.get(name) for name in identity)
            for item in (items if isinstance(items, list) else ())
            if isinstance(item, dict)
        }

    if theirs is not None and not isinstance(theirs, list):
        return False
    return keys(theirs) <= keys(mine)


def _covers_artifacts(mine: Any, theirs: Any) -> bool:
    if theirs is not None and not isinstance(theirs, list):
        return False
    held = {
        item.get("url"): item
        for item in (mine if isinstance(mine, list) else ())
        if isinstance(item, dict)
    }
    for item in theirs or ():
        if not isinstance(item, dict):
            return False
        kept = held.get(item.get("url"))
        if kept is None or str(kept.get("updatedAt") or "") < str(
            item.get("updatedAt") or ""
        ):
            return False
    return True
