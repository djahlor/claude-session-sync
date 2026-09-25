"""Reduce a chat record's bytes to the state sync compares. Pure."""

import hashlib
import math

from . import strict_json as json
from .chat_model import Copy


# Top-level keys Claude rewrites without any user action, or that describe the
# account or machine that loaded the session rather than the chat itself:
# - lastFocusedAt changes on every click.
# - errorAt is stamped afresh by each login on a side session that never started.
# - remoteMcpServersConfig and enabledMcpTools hold the signed-in account's
#   connector ids, so each account rewrites them with its own.
# - transcriptUnavailable is derived from what is on disk when a session warms.
# - promptSuggestion, promptAppendSnapshot and toolSurfaceSnapshot are runtime
#   snapshots and suggestions that change when a session spawns or idles.
# Measured on Claude 2.2553.13: each changed without chat activity in the other
# account's copies. A copy that differs only in these keys is the same chat.
VOLATILE_KEYS = (
    "enabledMcpTools",
    "errorAt",
    "lastFocusedAt",
    "promptAppendSnapshot",
    "promptSuggestion",
    "remoteMcpServersConfig",
    "toolSurfaceSnapshot",
    "transcriptUnavailable",
)

UNREADABLE = Copy(state_hash=None)


def normalisation() -> str:
    """Name how a hash is made. The state file drops its hashes when this changes."""

    return "sha256 of sorted ASCII JSON, without " + ",".join(sorted(VOLATILE_KEYS))


def fingerprint(session_id: str, data: bytes) -> Copy:
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        return UNREADABLE
    if not isinstance(record, dict) or record.get("sessionId") != "local_" + session_id:
        return UNREADABLE
    state = {key: value for key, value in record.items() if key not in VOLATILE_KEYS}
    # ASCII output: Claude cuts strings by UTF-16 code unit, so a record can hold
    # half a surrogate pair, which UTF-8 cannot encode.
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return Copy(
        state_hash=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
        last_activity_at=activity(record.get("lastActivityAt")),
    )


def activity(value) -> int:
    # bool is an int in Python; a record holding true here is not a timestamp.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value) or value < 0:
        return 0
    return math.floor(value)
