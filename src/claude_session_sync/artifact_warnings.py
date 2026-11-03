"""Stop Claude Code's red "Artifact comment monitor ... didn't resume" warnings.

Publishing an artifact turns on automatic replies to its comments. The chat
saves the watch as an `artifact-comment-monitor` line in its transcript under
`~/.claude/projects/` (or `CLAUDE_CONFIG_DIR`), and writes a new line each time
its list of pages changes. Each time the chat is opened again, Claude Code
tries to restart every saved watch. A page owned by another Claude account
cannot be watched from the one signed in now, so after an account switch the
restarts fail and each failure shows a red notice. The lines never expire.

Optional, off by default (`stop_artifact_warnings`). With Claude closed:

1. The `env` block of `settings.json` gets
   `"CLAUDE_CODE_ARTIFACT_COMMENTS_AUTOREACT": "0"`. Claude Code checks it
   before it saves a watch, so a chat started after it saves none. The edit
   inserts that one member into the text and every other byte stays. A switch
   already there, with any value, is left alone. With the option turned off,
   the next sync takes the switch out only when sync put it in and it still
   says "0".
2. Every `artifact-comment-monitor` line goes from the transcripts, after it is
   appended to a backup in the state folder. A chat whose Claude Code process
   may still run is skipped: it holds its watches in memory and keeps writing
   to its transcript. Each transcript is read once; how far is kept in the
   state folder.

The cost: Claude stops answering artifact comments by itself in the
background. The switch is listed in Anthropic's Claude Code environment
variables (https://code.claude.com/docs/en/env-vars), from Claude Code 2.1.228.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import strict_json as json
from .adapters import read_status, save_status
from .filesystem import (
    UnsafePathError,
    atomic_write_bytes,
    ensure_private_directory,
    fsync_directory,
    open_regular_read,
    read_bounded_with_stat,
    sweep_stale_stages,
)
from .pictures import claude_config_dir, projects_root

SWITCH = "CLAUDE_CODE_ARTIFACT_COMMENTS_AUTOREACT"
SWITCH_VALUE = "0"
RECORD_TYPE = "artifact-comment-monitor"
MARK = b'"artifact-comment-monitor"'
OWNER_FILENAME = "artifact-warnings-switch.json"
STATUS_FILENAME = "artifact-warnings-status.json"
OFFSETS_FILENAME = "artifact-warnings-offsets.json"
BACKUP_FILENAME = "artifact-warnings-removed.jsonl"
OWNER_VERSION = 1
OFFSETS_VERSION = 1
MAX_SETTINGS_BYTES = 16 * 1024 * 1024
MAX_OFFSETS_BYTES = 64 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
# A transcript saved this recently can belong to a chat that is still closing.
RECENT_SECONDS = 30
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
SESSION_ARGUMENT = re.compile(
    rf"(?:^|\s)(?:--resume|--session-id|-r)[ =]({_UUID})(?=\s|$)"
)
# Command-line marks of Claude Code: the desktop app's bundled copy or the npm
# package, and the native installer.
CLAUDE_CODE_MARKS = ("/claude-code/", "/share/claude/versions/")
_SPACE = " \t\n\r"


class SettingsError(RuntimeError):
    """settings.json is in a state the switch edit leaves alone."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- The switch in settings.json -------------------------------------------


@dataclass(frozen=True)
class _Member:
    key: str
    key_start: int
    key_end: int
    value_start: int
    value_end: int


@dataclass(frozen=True)
class _Object:
    start: int
    end: int
    members: tuple[_Member, ...]

    def find(self, key: str) -> int | None:
        for index, member in enumerate(self.members):
            if member.key == key:
                return index
        return None


def _parse(text: str) -> dict:
    document = json.loads(text)
    if not isinstance(document, dict):
        raise SettingsError("settings-unrecognised")
    return document


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in _SPACE:
        index += 1
    return index


def _string_end(text: str, index: int) -> int:
    index += 1
    while text[index] != '"':
        index += 2 if text[index] == "\\" else 1
    return index + 1


def _value_end(text: str, index: int) -> int:
    if text[index] == '"':
        return _string_end(text, index)
    if text[index] in "{[":
        depth = 0
        while True:
            character = text[index]
            if character == '"':
                index = _string_end(text, index)
                continue
            if character in "{[":
                depth += 1
            elif character in "}]":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
    while index < len(text) and text[index] not in _SPACE + ",}]":
        index += 1
    return index


def _object_at(text: str, start: int) -> _Object:
    """The members of the object at START. The text already parsed as JSON."""

    members = []
    index = _skip_space(text, start + 1)
    while text[index] != "}":
        key_end = _string_end(text, index)
        value_start = _skip_space(text, _skip_space(text, key_end) + 1)
        value_end = _value_end(text, value_start)
        members.append(
            _Member(
                json.loads(text[index:key_end]), index, key_end, value_start, value_end
            )
        )
        index = _skip_space(text, value_end)
        if text[index] == ",":
            index = _skip_space(text, index + 1)
    return _Object(start, index, tuple(members))


def _line_indent(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    end = start
    while end < position and text[end] in " \t":
        end += 1
    return text[start:end]


def _indent_unit(text: str, top: _Object) -> str:
    if top.members:
        lead = text[top.start + 1 : top.members[0].key_start]
        if "\n" in lead and lead.rpartition("\n")[2]:
            return lead.rpartition("\n")[2]
    return "  "


def _render(
    key: str, value: Any, colon: str, indent: str | None, unit: str, newline: str
) -> str:
    """KEY: VALUE as one member. A dict value is laid out like the file."""

    if not isinstance(value, dict):
        return json.dumps(key) + colon + json.dumps(value)
    inner = [
        json.dumps(name) + colon + json.dumps(item) for name, item in value.items()
    ]
    if indent is None:
        return json.dumps(key) + colon + "{" + ", ".join(inner) + "}"
    lines = ("," + newline).join(indent + unit + member for member in inner)
    return json.dumps(key) + colon + "{" + newline + lines + newline + indent + "}"


def _insert(text: str, target: _Object, key: str, value: Any, unit: str) -> str:
    """TEXT with KEY: VALUE added as the last member of TARGET, in the file's layout."""

    newline = "\r\n" if "\r\n" in text else "\n"
    if target.members:
        last = target.members[-1]
        lead_start = last.key_start
        while text[lead_start - 1] in _SPACE:
            lead_start -= 1
        lead = text[lead_start : last.key_start]
        colon = text[last.key_end : last.value_start]
        indent = lead.rpartition("\n")[2] if "\n" in lead else None
        member = _render(key, value, colon, indent, unit, newline)
        return text[: last.value_end] + "," + lead + member + text[last.value_end :]
    indent = _line_indent(text, target.start)
    member = _render(key, value, ": ", indent + unit, unit, newline)
    return (
        text[: target.start + 1]
        + newline
        + indent
        + unit
        + member
        + newline
        + indent
        + text[target.end :]
    )


def _remove(text: str, target: _Object, index: int, empty: str = "") -> str:
    """TEXT without member INDEX of TARGET and its comma: the inverse of _insert.

    EMPTY is what stood between the braces of a TARGET that had no members.
    """

    member = target.members[index]
    if len(target.members) == 1:
        return text[: target.start + 1] + empty + text[target.end :]
    if index == len(target.members) - 1:
        return text[: target.members[index - 1].value_end] + text[member.value_end :]
    return text[: member.key_start] + text[target.members[index + 1].key_start :]


def add_switch(text: str) -> tuple[str | None, bool]:
    """The settings text with the switch added, and whether `env` is new.

    None when the switch is already there, with any value.
    """

    document = _parse(text)
    top = _object_at(text, _skip_space(text, 0))
    unit = _indent_unit(text, top)
    env_index = top.find("env")
    if env_index is None:
        changed = _insert(text, top, "env", {SWITCH: SWITCH_VALUE}, unit)
        expected = dict(document, env={SWITCH: SWITCH_VALUE})
        created = True
    else:
        env = document["env"]
        if not isinstance(env, dict):
            raise SettingsError("settings-unrecognised")
        if SWITCH in env:
            return None, False
        target = _object_at(text, top.members[env_index].value_start)
        changed = _insert(text, target, SWITCH, SWITCH_VALUE, unit)
        expected = dict(document, env=dict(env, **{SWITCH: SWITCH_VALUE}))
        created = False
    if _parse(changed) != expected:
        raise SettingsError("settings-unrecognised")
    return changed, created


def empty_inner(text: str) -> str:
    """The spaces inside the empty object that the switch goes into, else "".

    That is an `env` block with no keys yet, or, with no `env` block, a file
    holding only `{}`.
    """

    top = _object_at(text, _skip_space(text, 0))
    env_index = top.find("env")
    if env_index is None:
        target = top
    elif text[top.members[env_index].value_start] == "{":
        target = _object_at(text, top.members[env_index].value_start)
    else:
        return ""
    return "" if target.members else text[target.start + 1 : target.end]


def remove_switch(text: str, *, created_env: bool, inner: str = "") -> str | None:
    """The settings text without the switch. None when it is gone or not "0".

    INNER is what empty_inner returned before the switch went in.
    """

    document = _parse(text)
    env = document.get("env")
    if not isinstance(env, dict) or env.get(SWITCH) != SWITCH_VALUE:
        return None
    top = _object_at(text, _skip_space(text, 0))
    env_index = top.find("env")
    assert env_index is not None
    if created_env and len(env) == 1:
        changed = _remove(text, top, env_index, inner)
        expected = {key: value for key, value in document.items() if key != "env"}
    else:
        target = _object_at(text, top.members[env_index].value_start)
        switch_index = target.find(SWITCH)
        assert switch_index is not None
        changed = _remove(text, target, switch_index, inner)
        expected = dict(
            document, env={key: value for key, value in env.items() if key != SWITCH}
        )
    if _parse(changed) != expected:
        raise SettingsError("settings-unrecognised")
    return changed


def _identity(metadata: os.stat_result | None) -> tuple | None:
    if metadata is None:
        return None
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _read_settings(path: Path) -> tuple[str, os.stat_result | None]:
    """The settings text and its file details. An empty object when there is no file."""

    if path.is_symlink():
        raise SettingsError("settings-link")
    if not os.path.lexists(path):
        return "{}\n", None
    try:
        content, metadata = read_bounded_with_stat(
            path, MAX_SETTINGS_BYTES, stable=True
        )
        text = content.decode("utf-8")
        _parse(text)
    except (OSError, UnicodeError, ValueError) as error:
        raise SettingsError("settings-unreadable") from error
    return text, metadata


def _write_settings(path: Path, text: str, before: os.stat_result | None) -> None:
    """Replace settings.json, unless something changed it since it was read.

    The new copy has the old file's mode before it takes its place.
    """

    try:
        current: os.stat_result | None = os.lstat(path)
    except FileNotFoundError:
        current = None
    if _identity(current) != _identity(before):
        raise SettingsError("settings-changed")
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise UnsafePathError(f"not a real directory: {path.parent}")
    staged = path.parent / f".{path.name}.{uuid.uuid4().hex}.stage"
    descriptor = os.open(str(staged), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, stat.S_IMODE(before.st_mode) if before is not None else 0o600)
        os.replace(staged, path)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
    fsync_directory(path.parent)


def _holds_switch(path: Path) -> bool:
    """True unless settings.json surely has no switch saying "0"."""

    try:
        text, _metadata = _read_settings(path)
        env = _parse(text).get("env")
    except Exception:
        return True
    return isinstance(env, dict) and env.get(SWITCH) == SWITCH_VALUE


def _load_owner(state_dir: Path) -> dict | None:
    """What sync recorded when it put the switch in. None when it did not."""

    document = read_status(state_dir, OWNER_FILENAME)
    if document == {"state": "unknown"}:
        return None
    if (
        document.get("version") != OWNER_VERSION
        or not isinstance(document.get("path"), str)
        or not isinstance(document.get("created_env"), bool)
        or not isinstance(document.get("inner", ""), str)
        or document.get("inner", "").strip(_SPACE)
    ):
        raise SettingsError("switch-record-unreadable")
    return document


def _forget_owner(state_dir: Path) -> None:
    path = state_dir / OWNER_FILENAME
    if os.path.lexists(path):
        os.unlink(path)
        fsync_directory(state_dir)


def ensure_switch(state_dir: Path, path: Path) -> str:
    """Put the switch in settings.json. Returns added, present or other-value."""

    text, before = _read_settings(path)
    changed, created_env = add_switch(text)
    if changed is None:
        value = _parse(text)["env"][SWITCH]
        return "present" if value == SWITCH_VALUE else "other-value"
    # Recorded first: a crash before the write leaves a record of a switch that
    # is not there, which the release reads as already gone.
    save_status(
        state_dir,
        OWNER_FILENAME,
        {
            "version": OWNER_VERSION,
            "path": str(path),
            "created_env": created_env,
            "inner": empty_inner(text),
        },
    )
    try:
        _write_settings(path, changed, before)
    except BaseException:
        # An error after the new file took its place keeps the record, so
        # turning the option off can still take the switch out.
        if not _holds_switch(path):
            _forget_owner(state_dir)
        raise
    return "added"


def release_switch(state_dir: Path) -> str | None:
    """Take out a switch sync put in. None when sync put none in.

    Returns removed, or left when the switch is gone or no longer says "0".
    """

    owner = _load_owner(state_dir)
    if owner is None:
        return None
    path = Path(owner["path"])
    text, before = _read_settings(path)
    changed = remove_switch(
        text,
        created_env=owner["created_env"],
        inner=owner.get("inner", ""),
    )
    if changed is not None:
        _write_settings(path, changed, before)
    _forget_owner(state_dir)
    return "removed" if changed is not None else "left"


# --- Saved watches in the transcripts --------------------------------------


@dataclass(frozen=True)
class OpenChats:
    """Chats the process table says may still be open."""

    session_ids: frozenset[str] = frozenset()
    # When the oldest running Claude Code process started. Even one that names
    # its chat can write to another transcript, after a fork or a /clear.
    running_since: float | None = None

    def may_be_open(self, path: Path, metadata: os.stat_result, now: float) -> bool:
        if path.stem.lower() in self.session_ids:
            return True
        if now - metadata.st_mtime < RECENT_SECONDS:
            return True
        return (
            self.running_since is not None
            and metadata.st_mtime >= self.running_since - 1
        )


def _elapsed_seconds(text: str) -> int:
    """Seconds in a `ps` elapsed time: [[days-]hours:]minutes:seconds."""

    days, _, clock = text.strip().rpartition("-")
    parts = [int(part) for part in clock.split(":")]
    if not 2 <= len(parts) <= 3:
        raise ValueError(f"unknown elapsed time: {text}")
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts
    return int(days or 0) * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_open_chats(listing: str, now: float) -> OpenChats:
    """Read `ps -axo etime=,args=`. Over-matching only skips more chats."""

    ids: set[str] = set()
    since: float | None = None
    for row in listing.splitlines():
        elapsed, _, command = row.strip().partition(" ")
        command = command.strip()
        if "claude" not in command.lower():
            continue
        named = SESSION_ARGUMENT.findall(command)
        ids.update(session_id.lower() for session_id in named)
        program = os.path.basename(command.split(" ", 1)[0])
        if (
            not named
            and program != "claude"
            and not any(mark in command for mark in CLAUDE_CODE_MARKS)
        ):
            continue
        try:
            started = now - _elapsed_seconds(elapsed)
        except ValueError:
            started = 0.0
        since = started if since is None else min(since, started)
    return OpenChats(frozenset(ids), since)


def open_chats(
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    clock: Callable[[], float] = time.time,
) -> OpenChats:
    listing = runner(
        ["ps", "-axo", "etime=,args="],
        check=True,
        text=True,
        capture_output=True,
        timeout=15,
    ).stdout
    return parse_open_chats(listing, clock())


def is_record(line: bytes) -> bool:
    """True for a complete saved-watch line."""

    if not line.endswith(b"\n") or MARK not in line:
        return False
    try:
        document = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(document, dict) and document.get("type") == RECORD_TYPE


def _scan(path: Path, start: int) -> tuple[bool, int]:
    """Whether a saved watch follows START, and where the last complete line ends."""

    with open_regular_read(path) as source:
        source.seek(start)
        position = line_end = start
        marked = False
        tail = b""
        while True:
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            marked = marked or MARK in chunk or MARK in tail + chunk[: len(MARK) - 1]
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                line_end = position + newline + 1
            position += len(chunk)
            tail = chunk[-(len(MARK) - 1) :]
        if not marked:
            return False, line_end
        source.seek(start)
        for line in source:
            if is_record(line):
                return True, line_end
    return False, line_end


@dataclass(frozen=True)
class CleanReceipt:
    records: int = 0
    chats: int = 0
    # Transcripts that still hold saved watches because their chat may be open.
    open_chats: int = 0


class TranscriptCleaner:
    def __init__(
        self,
        state_dir: Path,
        root: Path,
        chats: OpenChats,
        *,
        claude_running: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.root = Path(root)
        self.chats = chats
        self.claude_running = claude_running
        self.clock = clock

    def _folders(self) -> list[Path]:
        if self.root.is_symlink() or not self.root.is_dir():
            return []
        return [
            folder
            for folder in sorted(self.root.iterdir())
            if not folder.is_symlink() and folder.is_dir()
        ]

    def _transcripts(self, folders: list[Path]) -> Iterator[Path]:
        for folder in folders:
            for path in sorted(folder.glob("*.jsonl")):
                if not path.is_symlink() and path.is_file():
                    yield path

    def _load_offsets(self) -> dict[str, dict[str, int]]:
        """How far each transcript was read. Unreadable means read all again."""

        path = self.state_dir / OFFSETS_FILENAME
        try:
            content, _metadata = read_bounded_with_stat(path, MAX_OFFSETS_BYTES)
            document = json.loads(content.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return {}
        if not isinstance(document, dict) or document.get("version") != OFFSETS_VERSION:
            return {}
        files = document.get("files")
        if not isinstance(files, dict):
            return {}
        return {
            key: value
            for key, value in files.items()
            if isinstance(value, dict)
            and all(
                isinstance(value.get(field), int)
                and not isinstance(value.get(field), bool)
                for field in ("dev", "ino", "scanned")
            )
        }

    def _save_offsets(self, files: dict[str, dict[str, int]]) -> None:
        ensure_private_directory(self.state_dir)
        document = {"version": OFFSETS_VERSION, "files": files}
        path = self.state_dir / OFFSETS_FILENAME
        atomic_write_bytes(
            path, (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
        )
        os.chmod(path, 0o600)

    def _back_up(self, path: Path, removed: list[tuple[int, bytes]]) -> None:
        """Append each line about to go, with its file and line number."""

        ensure_private_directory(self.state_dir)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(self.state_dir / BACKUP_FILENAME), flags, 0o600)
        with os.fdopen(descriptor, "ab") as stream:
            os.fchmod(stream.fileno(), 0o600)
            removed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for number, line in removed:
                entry = {
                    "file": str(path),
                    "line": number,
                    "raw": line.decode("utf-8"),
                    "removed_at": removed_at,
                }
                stream.write((json.dumps(entry, sort_keys=True) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())

    def _clean_file(self, path: Path) -> tuple[int, dict[str, int]] | None:
        """Remove the saved watches of one closed chat. None when its file changed."""

        staged = path.parent / f".{path.name}.{uuid.uuid4().hex}.stage"
        removed: list[tuple[int, bytes]] = []
        complete = 0
        try:
            with open_regular_read(path) as source:
                before = os.fstat(source.fileno())
                descriptor = os.open(
                    str(staged), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as target:
                    for number, line in enumerate(source):
                        if is_record(line):
                            removed.append((number, line))
                            continue
                        target.write(line)
                        if line.endswith(b"\n"):
                            complete += len(line)
                    target.flush()
                    os.fsync(target.fileno())
            if not removed or _identity(os.lstat(path)) != _identity(before):
                return None
            self._back_up(path, removed)
            # A chat that wrote meanwhile keeps its file: the next sync tries again.
            if _identity(os.lstat(path)) != _identity(before):
                return None
            os.chmod(staged, stat.S_IMODE(before.st_mode))
            os.replace(staged, path)
            fsync_directory(path.parent)
            after = os.lstat(path)
        finally:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
        return len(removed), _offset(after, complete)

    def clean(self) -> CleanReceipt:
        folders = self._folders()
        now = self.clock()
        sweep_stale_stages(folders, now)
        offsets = self._load_offsets()
        kept: dict[str, dict[str, int]] = {}
        records = chats = waiting = 0
        reopened = False
        for path in self._transcripts(folders):
            try:
                metadata = os.lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
            key = str(path)
            previous = offsets.get(key)
            start = 0
            if (
                previous is not None
                and (previous["dev"], previous["ino"])
                == (metadata.st_dev, metadata.st_ino)
                and previous["scanned"] <= metadata.st_size
            ):
                start = previous["scanned"]
            found, scanned = _scan(path, start)
            if not found:
                kept[key] = _offset(metadata, scanned)
                continue
            result = None
            if not reopened and not self.chats.may_be_open(path, metadata, now):
                # Claude opened again: its chats load these files, so the rest
                # waits for the next sync.
                reopened = self.claude_running()
                if not reopened:
                    result = self._clean_file(path)
            if result is None:
                waiting += 1
                if start:
                    kept[key] = _offset(metadata, start)
                continue
            removed, kept[key] = result
            records += removed
            chats += 1
        self._save_offsets(kept)
        return CleanReceipt(records, chats, waiting)


def _offset(metadata: os.stat_result, scanned: int) -> dict[str, int]:
    return {"dev": metadata.st_dev, "ino": metadata.st_ino, "scanned": scanned}


# --- One step of each sync --------------------------------------------------


def _reason(error: Exception, fallback: str) -> str:
    return error.reason if isinstance(error, SettingsError) else fallback


def run_artifact_warnings(
    config,
    claude_running: Callable[[], bool],
    *,
    settings: Path | None = None,
    projects: Path | None = None,
    chats: Callable[[], OpenChats] = open_chats,
) -> dict:
    """Claude is closed: put the switch in and clear saved watches. Never raises.

    With the option off it only takes out a switch an earlier sync put in, and
    returns {} when there is none.
    """

    state_dir = config.state_dir
    if not config.stop_artifact_warnings:
        try:
            switch = release_switch(state_dir)
        except Exception as error:
            return {"state": "skipped", "reason": _reason(error, "settings-error")}
        return {} if switch is None else {"state": "done", "switch": switch}

    result: dict[str, Any] = {"state": "noop", "records": 0}
    reason = None
    try:
        result["switch"] = ensure_switch(
            state_dir, settings or claude_config_dir() / "settings.json"
        )
    except Exception as error:
        result["switch"] = "not-set"
        reason = _reason(error, "settings-unwritable")
    deferred = False
    try:
        if claude_running():
            deferred = True
        else:
            receipt = TranscriptCleaner(
                state_dir,
                projects or projects_root(),
                chats(),
                claude_running=claude_running,
            ).clean()
            result.update(
                records=receipt.records,
                chats=receipt.chats,
                open_chats=receipt.open_chats,
            )
    except Exception:
        reason = reason or "transcripts-error"
    if reason is not None:
        result.update(state="skipped", reason=reason)
    elif deferred:
        result["state"] = "deferred"
    elif result["switch"] == "added" or result["records"]:
        result["state"] = "done"
    try:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_status(state_dir, STATUS_FILENAME, dict(result, at=stamp))
    except OSError:
        result["reporting_error"] = "status-write-failed"
    return result


def status_fields(config) -> dict[str, Any]:
    """Whether the option is on, and what the last sync cleared."""

    if not config.stop_artifact_warnings:
        return {"artifact_warnings": "off", "cleared": 0}
    last = read_status(config.state_dir, STATUS_FILENAME)
    records = last.get("records")
    fields: dict[str, Any] = {
        "artifact_warnings": "on",
        "cleared": records
        if isinstance(records, int) and not isinstance(records, bool)
        else 0,
    }
    if isinstance(last.get("reason"), str):
        fields["artifact_warnings_reason"] = last["reason"]
    return fields
