"""Keep the built-in browser's site logins across Claude account switches.

Claude Desktop clears its built-in browser every time the signed-in Claude
account changes: the shared cookie jar, its local storage and the list of
imported sites. Restarts with the same account keep them. A switch already
restarts Claude once with Claude closed, so the jar is copied while it is
healthy and put back at that moment, before Claude reopens. Claude then starts
with the new account already known and does not clear it again.

Only counts, sizes and times are read. Cookie values stay encrypted with
Claude's own key and are never read, printed or logged. The account is kept as
a hash.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import strict_json as json
from .filesystem import atomic_write_bytes, ensure_private_directory
from .locking import ExclusiveFileLock, LockUnavailableError


JAR_NAME = "launch-preview-cowork-shared"
JAR_ID = "persist:" + JAR_NAME
RECORDS_NAME = "chrome-import-sites.json"
ACCOUNT_FILE = "config.json"
KEEP_BACKUPS = 3
MAX_JSON_BYTES = 16 * 1024 * 1024


def _read_json(path: Path, limit: int = MAX_JSON_BYTES):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, document) -> None:
    atomic_write_bytes(path, (json.dumps(document, sort_keys=True) + "\n").encode("utf-8"))
    os.chmod(path, 0o600)


def _cookie_count(path: Path) -> int:
    """Row count only. A missing or unreadable database counts as empty."""
    if path.is_symlink() or not path.is_file():
        return 0
    try:
        with contextlib.closing(sqlite3.connect("file:{}?mode=ro".format(path), uri=True, timeout=3)) as db:
            return int(db.execute("select count(*) from cookies").fetchone()[0])
    except sqlite3.Error:
        return 0


def _integrity_ok(path: Path) -> bool:
    try:
        with contextlib.closing(sqlite3.connect("file:{}?mode=ro".format(path), uri=True)) as db:
            return db.execute("pragma integrity_check").fetchone()[0] == "ok"
    except sqlite3.Error:
        return False


def _newest_mtime(paths: List[Path]) -> float:
    newest = 0.0
    for root in paths:
        if root.is_file():
            newest = max(newest, root.stat().st_mtime)
        elif root.is_dir():
            for directory, _folders, files in os.walk(root):
                for name in files:
                    try:
                        newest = max(newest, os.stat(os.path.join(directory, name)).st_mtime)
                    except OSError:
                        pass
    return newest


def _copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(str(source), str(destination), ignore=shutil.ignore_patterns("LOCK"))


class BrowserLogins:
    """One profile's built-in browser jar and its saved copy."""

    def __init__(self, profile, state_dir: Path, running: Callable[[], bool]) -> None:
        self.claude = Path(profile.data_root)
        self.partition = self.claude / "Partitions" / JAR_NAME
        self.records_path = self.claude / RECORDS_NAME
        self.home = Path(state_dir) / "browser-logins" / profile.name
        self.saved = self.home / "snapshot"
        self.running = running

    # Reading state -----------------------------------------------------

    def account(self) -> Optional[str]:
        document = _read_json(self.claude / ACCOUNT_FILE)
        value = document.get("lastKnownAccountUuid") if isinstance(document, dict) else None
        if not isinstance(value, str) or not value:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _records(self, path: Path) -> List[dict]:
        entries = _read_json(path)
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, dict) and e.get("jar") == JAR_ID]

    def health(self) -> Tuple[int, int]:
        """Imported sites and cookies in the live jar."""
        return len(self._records(self.records_path)), _cookie_count(self.partition / "Cookies")

    def _meta(self) -> Dict:
        document = _read_json(self.saved / "meta.json", limit=64 * 1024)
        return document if isinstance(document, dict) else {}

    def _save_meta(self, meta: Dict) -> None:
        _write_json(self.saved / "meta.json", meta)

    # Commands ------------------------------------------------------------

    def snapshot(self) -> Dict:
        with self._lock():
            return self._snapshot()

    def restore(self) -> Dict:
        """Put the saved jar back. Only with Claude closed, only after a switch."""
        with self._lock():
            if self.running():
                return {"state": "skipped", "reason": "claude-open"}
            sites, cookies = self.health()
            if sites and cookies:
                saved = self._snapshot()
                return {"state": "not-needed", "sites": sites, "cookies": cookies,
                        "snapshot": saved["state"]}
            meta = self._meta()
            if not meta or _cookie_count(self.saved / "Cookies") == 0:
                return {"state": "skipped", "reason": "no-snapshot"}
            if meta.get("cleared"):
                return {"state": "skipped", "reason": "cleared-in-claude"}
            account = self.account()
            if account is None:
                return {"state": "skipped", "reason": "no-account"}
            if meta.get("account") == account:
                # Emptied without an account change: the person cleared it in
                # Claude. Respect that and never bring this copy back.
                meta["cleared"] = True
                self._save_meta(meta)
                return {"state": "skipped", "reason": "cleared-in-claude"}
            self._backup_live()
            self._put_back()
            sites, cookies = self.health()
            if not (sites and cookies):
                return {"state": "failed", "reason": "restore-not-visible"}
            meta["account"] = account
            meta["restored"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._save_meta(meta)
            return {"state": "restored", "sites": sites, "cookies": cookies}

    def settle(self) -> Dict:
        """After Claude quits: restore when a switch emptied the jar, else save it."""
        if self.running():
            return self.snapshot()
        return self.restore()

    def status(self) -> Dict:
        sites, cookies = self.health()
        meta = self._meta()
        saved = None
        if meta:
            saved = {key: meta.get(key) for key in ("taken", "sites", "cookies", "restored")}
            saved["cleared"] = bool(meta.get("cleared"))
            saved["same_account"] = meta.get("account") == self.account()
        return {"state": "ok", "sites": sites, "cookies": cookies, "snapshot": saved}

    # Internals -----------------------------------------------------------

    def _lock(self):
        ensure_private_directory(self.home)
        return _Held(ExclusiveFileLock(self.home / "browser-logins.lock", mode="auto", timeout=10))

    def _snapshot(self) -> Dict:
        sites, cookies = self.health()
        meta = self._meta()
        account = self.account()
        same_account = bool(meta) and meta.get("account") == account and account is not None
        if not (sites and cookies):
            if same_account and not meta.get("cleared"):
                meta["cleared"] = True
                self._save_meta(meta)
                return {"state": "cleared", "reason": "emptied-without-switch"}
            return {"state": "skipped", "reason": "jar-empty"}
        if same_account and not meta.get("cleared") and cookies * 2 < int(meta.get("cookies", 0)):
            # A half-finished clear can look like a smaller jar. Accept a big
            # drop only when the next run still sees it.
            if not meta.get("drop_seen"):
                meta["drop_seen"] = True
                self._save_meta(meta)
                return {"state": "skipped", "reason": "large-drop"}
        sources = [self.partition / "Cookies", self.partition / "Local Storage", self.records_path]
        if same_account and not meta.get("cleared") and not meta.get("drop_seen") \
                and float(meta.get("taken_epoch", 0)) >= _newest_mtime(sources):
            return {"state": "skipped", "reason": "unchanged"}

        ensure_private_directory(self.home)
        staging = self.home / "staging"
        shutil.rmtree(str(staging), ignore_errors=True)
        ensure_private_directory(staging)
        taken = time.time()
        # The backup API takes a consistent copy while Claude has the jar open.
        live = "file:{}?mode=ro".format(self.partition / "Cookies")
        with contextlib.closing(sqlite3.connect(live, uri=True, timeout=5)) as source:
            with contextlib.closing(sqlite3.connect(str(staging / "Cookies"))) as target:
                source.backup(target)
        os.chmod(staging / "Cookies", 0o600)
        copied = _cookie_count(staging / "Cookies")
        if not _integrity_ok(staging / "Cookies") or copied == 0:
            shutil.rmtree(str(staging), ignore_errors=True)
            return {"state": "failed", "reason": "copy-check"}
        if (self.partition / "Local Storage").is_dir():
            _copy_tree(self.partition / "Local Storage", staging / "Local Storage")
        records = self._records(self.records_path)
        _write_json(staging / "records.json", records)
        meta = {
            "taken": datetime.now().astimezone().isoformat(timespec="seconds"),
            "taken_epoch": taken,
            "sites": len(records),
            "cookies": copied,
            "account": account,
        }
        _write_json(staging / "meta.json", meta)
        previous = self.home / "snapshot.prev"
        shutil.rmtree(str(previous), ignore_errors=True)
        if self.saved.exists():
            self.saved.rename(previous)
        staging.rename(self.saved)
        return {"state": "saved", "sites": len(records), "cookies": copied}

    def _backup_live(self) -> Path:
        backups = self.home / "backups"
        ensure_private_directory(backups)
        folder = backups / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        ensure_private_directory(folder)
        for name in ("Cookies", "Cookies-journal"):
            if (self.partition / name).is_file():
                shutil.copy2(str(self.partition / name), str(folder / name))
        if (self.partition / "Local Storage").is_dir():
            _copy_tree(self.partition / "Local Storage", folder / "Local Storage")
        if self.records_path.is_file():
            shutil.copy2(str(self.records_path), str(folder / RECORDS_NAME))
        for old in sorted(backups.iterdir())[:-KEEP_BACKUPS]:
            shutil.rmtree(str(old), ignore_errors=True)
        return folder

    def _put_back(self) -> None:
        ensure_private_directory(self.partition)
        staged = self.partition / "Cookies.session-sync-tmp"
        shutil.copy2(str(self.saved / "Cookies"), str(staged))
        os.replace(str(staged), str(self.partition / "Cookies"))
        journal = self.partition / "Cookies-journal"
        if journal.exists():
            # It belongs to the emptied database, and is in the backup.
            journal.unlink()
        if (self.saved / "Local Storage").is_dir():
            incoming = self.partition / "Local Storage.session-sync-new"
            outgoing = self.partition / "Local Storage.session-sync-old"
            for leftover in (incoming, outgoing):
                shutil.rmtree(str(leftover), ignore_errors=True)
            _copy_tree(self.saved / "Local Storage", incoming)
            if (self.partition / "Local Storage").exists():
                (self.partition / "Local Storage").rename(outgoing)
            incoming.rename(self.partition / "Local Storage")
            shutil.rmtree(str(outgoing), ignore_errors=True)
        current = _read_json(self.records_path)
        others = [e for e in current if not (isinstance(e, dict) and e.get("jar") == JAR_ID)] \
            if isinstance(current, list) else []
        saved_records = _read_json(self.saved / "records.json")
        _write_json(self.records_path, others + (saved_records if isinstance(saved_records, list) else []))


class _Held:
    def __init__(self, lock: ExclusiveFileLock) -> None:
        self.lock = lock

    def __enter__(self):
        try:
            self.lock.acquire()
        except LockUnavailableError as error:
            raise BrowserLoginsBusy("another browser-logins run holds the lock") from error
        return self

    def __exit__(self, *_details) -> None:
        self.lock.release()


class BrowserLoginsBusy(RuntimeError):
    """Another snapshot or restore is running for this profile."""


def run_browser_logins(config, running: Callable[[], bool], action: str, profiles=None) -> Dict:
    """Run one action for each profile. A failure never stops a sync or a relaunch."""
    if not getattr(config, "sync_browser_logins", False):
        return {"state": "disabled"}
    results = {}
    for profile in config.profiles if profiles is None else profiles:
        logins = BrowserLogins(profile, config.state_dir, running)
        try:
            results[profile.name] = getattr(logins, action)()
        except BrowserLoginsBusy:
            results[profile.name] = {"state": "skipped", "reason": "busy"}
        except Exception as error:  # a jar problem must never block chats or the relaunch
            results[profile.name] = {"state": "failed", "reason": type(error).__name__}
    return {"state": "done", "profiles": results}
