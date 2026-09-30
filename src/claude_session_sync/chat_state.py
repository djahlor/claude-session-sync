"""What chat sync remembers between runs, stored as one private JSON file."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

from . import strict_json as json
from .chat_model import SyncState
from .filesystem import atomic_write_bytes, ensure_private_directory
from .fingerprint import normalisation


STATE_FILENAME = "chat-state.json"
STATE_VERSION = 2
MAX_STATE_BYTES = 64 * 1024 * 1024

PathLike = Union[str, os.PathLike]


class StateUnusable(RuntimeError):
    """The state file exists but cannot be trusted. Nothing was changed."""


@dataclass
class ChatState:
    sync: SyncState = field(default_factory=SyncState)
    # data root -> (signed-in account, when that login began, in epoch ms)
    logins: Dict[str, Tuple[str, int]] = field(default_factory=dict)
    # partition keys that joined because Claude wrote a chat there after a login
    enrolled: List[str] = field(default_factory=list)
    # partition key -> chats created in a live folder, invisible until Claude reloads it
    live_creates: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    last_success_ms: int = 0


def state_path(state_dir: PathLike) -> Path:
    return Path(state_dir) / STATE_FILENAME


def load_state(path: PathLike) -> ChatState:
    file_path = Path(path)
    if not os.path.lexists(str(file_path)):
        return ChatState()
    try:
        if file_path.is_symlink() or not file_path.is_file():
            raise StateUnusable("chat state is not a regular file")
        if file_path.stat().st_size > MAX_STATE_BYTES:
            raise StateUnusable("chat state is too large")
        document = json.loads(file_path.read_bytes().decode("utf-8"))
    except StateUnusable:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise StateUnusable("chat state cannot be read: {}".format(error)) from error
    try:
        return _decode(document)
    except (KeyError, TypeError, ValueError) as error:
        raise StateUnusable("chat state has an unknown shape: {}".format(error)) from error


def _decode(document: Any) -> ChatState:
    if not isinstance(document, dict) or document.get("version") not in (1, STATE_VERSION):
        raise ValueError("unsupported version")
    # Version 1 kept one agreed version for all folders, which cannot say which
    # folders took part. Its hashes are dropped; what was seen is kept.
    same_hashes = (
        document.get("version") == STATE_VERSION
        and document.get("normalisation") == normalisation()
    )
    synced = (
        {
            _string(key): _string_map(value)
            for key, value in _mapping(document.get("synced", {})).items()
        }
        if same_hashes
        else {}
    )
    seen = {
        _string(key): {_string(item) for item in _list(value)}
        for key, value in _mapping(document.get("seen", {})).items()
    }
    logins = {}
    for root, value in _mapping(document.get("logins", {})).items():
        values = _list(value)
        if len(values) != 2:
            raise ValueError("login entry")
        logins[_string(root)] = (_string(values[0]), _integer(values[1]))
    enrolled = [_string(item) for item in _list(document.get("enrolled", []))]
    live_creates = {}
    for key, value in _mapping(document.get("live_creates", {})).items():
        entry = _mapping(value)
        live_creates[_string(key)] = {
            "account": _string(entry["account"]),
            "login_ms": _integer(entry["login_ms"]),
            "pids": sorted(_integer(pid) for pid in _list(entry["pids"])),
            "ids": sorted({_string(item) for item in _list(entry["ids"])}),
        }
    return ChatState(
        sync=SyncState(synced=synced, seen=seen),
        logins=logins,
        enrolled=sorted(set(enrolled)),
        live_creates=live_creates,
        last_success_ms=_integer(document.get("last_success_ms", 0)),
    )


def encode_state(state: ChatState) -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "normalisation": normalisation(),
        "synced": {
            key: dict(sorted(entries.items()))
            for key, entries in sorted(state.sync.synced.items())
            if entries
        },
        "seen": {key: sorted(ids) for key, ids in sorted(state.sync.seen.items())},
        "logins": {
            root: [account, changed]
            for root, (account, changed) in sorted(state.logins.items())
        },
        "enrolled": sorted(set(state.enrolled)),
        "live_creates": {
            key: {
                "account": entry["account"],
                "login_ms": entry["login_ms"],
                "pids": sorted(entry["pids"]),
                "ids": sorted(set(entry["ids"])),
            }
            for key, entry in sorted(state.live_creates.items())
            if entry.get("ids")
        },
        "last_success_ms": state.last_success_ms,
    }


def save_state(path: PathLike, state: ChatState) -> None:
    file_path = Path(path)
    ensure_private_directory(file_path.parent)
    encoded = (
        json.dumps(encode_state(state), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(file_path, encoded)
    os.chmod(str(file_path), 0o600)


def _mapping(value: Any) -> Dict[Any, Any]:
    if not isinstance(value, dict):
        raise TypeError("expected an object")
    return value


def _list(value: Any) -> List[Any]:
    if not isinstance(value, list):
        raise TypeError("expected an array")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("expected a non-empty string")
    return value


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("expected a non-negative integer")
    return value


def _string_map(value: Any) -> Dict[str, str]:
    return {_string(key): _string(item) for key, item in _mapping(value).items()}
