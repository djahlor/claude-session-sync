"""Show pictures sent in a chat from the copy on this Mac, in every account.

A picture sent with SendUserFile is recorded in the chat's transcript as an
attachment with its local `path` and the `file_uuid` of a copy uploaded to the
account that sent it. The Code tab draws such a card from that upload, always
through the organization signed in now, and has no fallback. After an account
switch the upload belongs to another account, so the picture shows as broken.
An attachment without `file_uuid` is drawn from its local path instead.

So, for each picture whose local file still exists, the key `file_uuid` is
renamed to `orig_uuid` inside the transcript line. The new key has the same
length, so the line is rewritten in place at the same offset: the file never
changes size, and a writer appending to it at the same time loses nothing. The
original id stays in the line, so the change can be undone by renaming the key
back. Only transcripts of the Code chats this tool syncs are read, found by
the `cliSessionId` in each chat record, and each line is read once: the
scanned length of each transcript is kept in the state folder.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import strict_json as json
from .config import Config
from .filesystem import atomic_write_bytes, ensure_private_directory

OFFSETS_FILENAME = "chat-pictures-offsets.json"
OFFSETS_VERSION = 1
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
OLD_KEY = b'"file_uuid":"'
NEW_KEY = b'"orig_uuid":"'
UUID = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
MAX_LINE_BYTES = 64 * 1024 * 1024
CLI_SESSION_ID = re.compile(
    rb'"cliSessionId"\s*:\s*"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"'
)
RECORD_HEAD_BYTES = 64 * 1024


class PictureError(RuntimeError):
    pass


@dataclass(frozen=True)
class PictureReceipt:
    state: str
    file_count: int
    picture_count: int


def projects_root() -> Path:
    """Where Claude Code keeps chat transcripts, shared by every account."""

    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return base / "projects"


def _attachment_lists(value: Any) -> Iterator[List[Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "attachments" and isinstance(item, list):
                yield item
            else:
                yield from _attachment_lists(item)
    elif isinstance(value, list):
        for item in value:
            yield from _attachment_lists(item)


class PictureFixer:
    def __init__(self, config: Config, *, root: Optional[Path] = None) -> None:
        self.config = config
        self.root = Path(root) if root is not None else projects_root()
        # Claude's own data folder: the Code tab never draws a card from there.
        self.app_roots = tuple(
            str(profile.data_root).rstrip("/") + "/" for profile in config.profiles
        )

    def _local_picture(self, attachment: Any) -> Optional[str]:
        """The upload id of a picture whose copy on this Mac is the file sent, or None."""

        if not isinstance(attachment, dict):
            return None
        path, uuid = attachment.get("path"), attachment.get("file_uuid")
        if not isinstance(path, str) or not isinstance(uuid, str) or not UUID.match(uuid):
            return None
        if (
            attachment.get("isImage") is not True
            and Path(path).suffix.lower() not in IMAGE_SUFFIXES
        ):
            return None
        if not os.path.isabs(path) or path.startswith(self.app_roots):
            return None
        try:
            metadata = os.lstat(path)
        except OSError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            return None
        size = attachment.get("size")
        if isinstance(size, int) and not isinstance(size, bool) and size != metadata.st_size:
            return None
        return uuid

    def plan_line(self, line: bytes) -> Optional[bytes]:
        """The same line with its local pictures freed from their upload, or None."""

        if OLD_KEY not in line or b'"attachments":[' not in line:
            return None
        try:
            document = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        uuids = {
            uuid
            for attachments in _attachment_lists(document)
            for uuid in map(self._local_picture, attachments)
            if uuid is not None
        }
        changed = line
        for uuid in sorted(uuids):
            encoded = uuid.encode("ascii") + b'"'
            changed = changed.replace(OLD_KEY + encoded, NEW_KEY + encoded)
        if changed == line or len(changed) != len(line):
            return None
        try:
            json.loads(changed.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return changed

    def _load_offsets(self) -> Dict[str, Dict[str, int]]:
        path = self.config.state_dir / OFFSETS_FILENAME
        try:
            document = json.loads(path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise PictureError("chat picture offsets are unreadable") from error
        files = document.get("files") if isinstance(document, dict) else None
        if not isinstance(document, dict) or document.get("version") != OFFSETS_VERSION or not isinstance(files, dict):
            raise PictureError("chat picture offsets have an unknown shape")
        return {
            key: value
            for key, value in files.items()
            if isinstance(key, str)
            and isinstance(value, dict)
            and all(
                isinstance(value.get(field), int) and not isinstance(value.get(field), bool)
                for field in ("dev", "ino", "scanned")
            )
        }

    def _save_offsets(self, files: Dict[str, Dict[str, int]]) -> None:
        ensure_private_directory(self.config.state_dir)
        document = {"version": OFFSETS_VERSION, "files": files}
        path = self.config.state_dir / OFFSETS_FILENAME
        atomic_write_bytes(path, (json.dumps(document, sort_keys=True) + "\n").encode("utf-8"))
        os.chmod(path, 0o600)

    def _synced_chat_ids(self) -> set:
        """The Claude Code session ids of the chats in every synced account folder."""

        ids, seen = set(), set()
        for profile in self.config.profiles:
            sessions = profile.data_root / "claude-code-sessions"
            if sessions.is_symlink() or not sessions.is_dir():
                continue
            for record in sessions.glob("*/*/local_*.json"):
                if record.name in seen or record.is_symlink() or not record.is_file():
                    continue
                seen.add(record.name)
                try:
                    with open(record, "rb") as stream:
                        match = CLI_SESSION_ID.search(stream.read(RECORD_HEAD_BYTES))
                except OSError:
                    continue
                if match:
                    ids.add(match.group(1).decode("ascii").lower())
        return ids

    def _transcripts(self) -> Iterator[Path]:
        if self.root.is_symlink() or not self.root.is_dir():
            return
        ids = self._synced_chat_ids()
        for project in sorted(self.root.iterdir()):
            if project.is_symlink() or not project.is_dir():
                continue
            for path in sorted(project.glob("*.jsonl")):
                if path.stem.lower() in ids and not path.is_symlink() and path.is_file():
                    yield path

    def _fix_file(self, path: Path, start: int) -> Tuple[int, int]:
        """Rewrite qualifying lines from START. Returns (scanned length, pictures)."""

        pictures = 0
        try:
            descriptor = os.open(str(path), os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except OSError as error:
            raise PictureError("a chat transcript could not be opened") from error
        try:
            with os.fdopen(os.dup(descriptor), "rb") as reader:
                reader.seek(start)
                offset = start
                while True:
                    line = reader.readline(MAX_LINE_BYTES + 1)
                    if not line or not line.endswith(b"\n"):
                        # A line still being written waits for the next run.
                        break
                    changed = self.plan_line(line)
                    # Write only over the exact bytes just read, at the same length.
                    if changed is not None and os.pread(descriptor, len(line), offset) == line:
                        os.pwrite(descriptor, changed, offset)
                        pictures += changed.count(NEW_KEY) - line.count(NEW_KEY)
                    offset += len(line)
            if pictures:
                os.fsync(descriptor)
            return offset, pictures
        except OSError as error:
            raise PictureError("a chat transcript could not be updated") from error
        finally:
            os.close(descriptor)

    def sync(self) -> PictureReceipt:
        offsets = self._load_offsets()
        kept: Dict[str, Dict[str, int]] = {}
        files = pictures = 0
        for path in self._transcripts():
            metadata = path.stat()
            previous = offsets.get(str(path))
            start = 0
            if (
                previous is not None
                and previous["dev"] == metadata.st_dev
                and previous["ino"] == metadata.st_ino
                and previous["scanned"] <= metadata.st_size
            ):
                start = previous["scanned"]
            scanned, fixed = start, 0
            if start < metadata.st_size:
                scanned, fixed = self._fix_file(path, start)
            kept[str(path)] = {"dev": metadata.st_dev, "ino": metadata.st_ino, "scanned": scanned}
            if fixed:
                files += 1
                pictures += fixed
        self._save_offsets(kept)
        return PictureReceipt("synced" if pictures else "noop", files, pictures)

    def probe(self) -> Dict[str, Any]:
        self._load_offsets()
        return {"state": "compatible"}
