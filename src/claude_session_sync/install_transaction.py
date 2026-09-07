"""Explicit filesystem and service transaction for installer mutations."""

import os
import hashlib
import shutil
import stat
import uuid
from pathlib import Path
from typing import Callable, Iterable, Sequence

from . import strict_json as json
from .filesystem import atomic_write_bytes, ensure_private_directory, fsync_directory
from .locking import ExclusiveFileLock


class InstallRecoveryError(RuntimeError):
    pass


class InstallTransaction:
    """Hold writer locks, snapshot owned paths, and restore them on failure."""

    def __init__(self, *, targets: Sequence[Path], state_roots: Iterable[Path],
                 launch_agent: Path, recovery_root: Path,
                 runner: Callable[..., object], legacy_agents: Sequence[Path] = ()):
        self.targets = tuple(self._normalize_system_temp(Path(path)) for path in targets)
        self.state_roots = tuple(sorted({
            self._normalize_system_temp(Path(path)) for path in state_roots
        }, key=str))
        self.launch_agent = self._normalize_system_temp(Path(launch_agent))
        self.runner = runner
        self.domain = "gui/{}".format(os.getuid())
        self.label = "com.claude-session-sync.watcher"
        self.legacy_agents = tuple(
            self._normalize_system_temp(Path(path)) for path in legacy_agents
        )
        self.root = self._normalize_system_temp(Path(recovery_root))
        self.snapshot = self.root / "snapshot"
        self.locks = []
        self.was_loaded = False
        self.legacy_loaded = {}
        self.preparation = None

    @staticmethod
    def _normalize_system_temp(path: Path) -> Path:
        text = str(path)
        if text == "/var/folders" or text.startswith("/var/folders/"):
            return Path("/private" + text)
        return path

    @staticmethod
    def _copy(source: Path, destination: Path) -> None:
        InstallTransaction._digest(source)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, destination, symlinks=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        paths = [path] if not path.is_dir() else [path] + sorted(path.rglob("*"))
        for item in paths:
            relative = "." if item == path else str(item.relative_to(path))
            metadata = item.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not (
                stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
            ):
                raise ValueError("installer snapshot contains unsupported path: {}".format(item))
            kind = "file" if stat.S_ISREG(metadata.st_mode) else "directory"
            digest.update(
                "{}\0{}\0{:o}\0".format(relative, kind, stat.S_IMODE(metadata.st_mode))
                .encode("utf-8")
            )
            if stat.S_ISREG(metadata.st_mode):
                digest.update(item.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _reject_symlink_ancestors(path: Path) -> None:
        candidate = path
        while candidate != candidate.parent:
            if candidate.is_symlink():
                raise ValueError("installer path has symlink ancestor: {}".format(candidate))
            candidate = candidate.parent

    def _loaded(self) -> bool:
        result = self.runner(
            ["/bin/launchctl", "print", self.domain + "/" + self.label],
            check=False, text=True, capture_output=True, timeout=10,
        )
        return self._checked_service_status(result, self.label)

    def _label_loaded(self, label: str) -> bool:
        result = self.runner(
            ["/bin/launchctl", "print", self.domain + "/" + label],
            check=False, text=True, capture_output=True, timeout=10,
        )
        return self._checked_service_status(result, label)

    @staticmethod
    def _checked_service_status(result, label: str) -> bool:
        if result.returncode == 0:
            return True
        if result.returncode == 113:
            return False
        raise RuntimeError(
            "could not verify launch service {} (launchctl exit {})".format(
                label, result.returncode
            )
        )

    def _fsync_snapshot(self) -> None:
        for path in sorted(self.snapshot.rglob("*"), reverse=True):
            if path.is_file():
                descriptor = os.open(str(path), os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        for path in sorted(
            (item for item in self.snapshot.rglob("*") if item.is_dir()),
            reverse=True,
        ):
            fsync_directory(path)
        fsync_directory(self.snapshot)

    def __enter__(self):
        # Global order: installation, switch handoff, then sync transaction.
        try:
            self._reject_symlink_ancestors(self.root)
            for root in self.state_roots:
                self._reject_symlink_ancestors(root)
            for name in ("installation.lock", "switch-handoff.lock", "transaction.lock"):
                for root in self.state_roots:
                    lock = ExclusiveFileLock(root / name, mode="auto", timeout=5.0)
                    lock.acquire()
                    self.locks.append(lock)
            self._recover_pending()
            self._reject_symlink_ancestors(self.root)
            self.preparation = self.root.parent / (
                ".install-state.prepare-" + uuid.uuid4().hex
            )
            ensure_private_directory(self.preparation)
            published_root = self.root
            self.root = self.preparation
            self.snapshot = self.root / "snapshot"
            self.snapshot.mkdir()
            for index, target in enumerate(self.targets):
                self._reject_symlink_ancestors(target)
                if target.exists() or target.is_symlink():
                    self._copy(target, self.snapshot / str(index))
            self.was_loaded = self._loaded()
            self.legacy_loaded = {
                path.stem: self._label_loaded(path.stem)
                for path in self.legacy_agents
            }
            self._fsync_snapshot()
            manifest = {
                "version": 1,
                "phase": "prepared",
                "was_loaded": self.was_loaded,
                "legacy_loaded": self.legacy_loaded,
                "targets": [str(path) for path in self.targets],
                "snapshot_digests": {
                    str(index): self._digest(self.snapshot / str(index))
                    for index in range(len(self.targets))
                    if (self.snapshot / str(index)).exists()
                },
            }
            atomic_write_bytes(
                self.root / "manifest.json",
                (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"),
            )
            os.replace(self.root, published_root)
            fsync_directory(published_root.parent)
            self.root = published_root
            self.snapshot = self.root / "snapshot"
            self.preparation = None
            return self
        except BaseException:
            if self.preparation is not None:
                shutil.rmtree(self.preparation, ignore_errors=True)
            self._release()
            raise

    def _recover_pending(self) -> None:
        if not self.root.exists():
            return
        manifest_path = self.root / "manifest.json"
        if not manifest_path.exists():
            raise InstallRecoveryError(
                "published recovery has no manifest; evidence retained at {}".format(self.root)
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("version") != 1:
                raise ValueError("unsupported recovery version")
            if manifest.get("targets") != [str(path) for path in self.targets]:
                raise ValueError("recovery target allowlist mismatch")
            if manifest.get("phase") in ("committed", "rolled_back"):
                self._retire()
                return
            if manifest.get("phase") != "prepared":
                raise ValueError("unsupported recovery phase")
            self.was_loaded = bool(manifest["was_loaded"])
            self.legacy_loaded = {
                str(key): bool(value)
                for key, value in manifest.get("legacy_loaded", {}).items()
            }
            if not self.snapshot.is_dir():
                raise ValueError("recovery snapshot is missing")
            actual = {
                str(index): self._digest(self.snapshot / str(index))
                for index in range(len(self.targets))
                if (self.snapshot / str(index)).exists()
            }
            if manifest.get("snapshot_digests") != actual:
                raise ValueError("recovery snapshot digest mismatch")
            self.stop_watcher()
            self._restore_files()
            self._restore_service()
            self._write_phase("rolled_back")
        except BaseException as error:
            raise InstallRecoveryError(
                "pending installer recovery failed; evidence retained at {}: {}"
                .format(self.root, error)
            ) from error
        self._retire()

    def _write_phase(self, phase: str) -> None:
        manifest_path = self.root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["phase"] = phase
        atomic_write_bytes(
            manifest_path,
            (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8"),
        )

    def _retire(self) -> None:
        retired = self.root.parent / (
            ".install-state.retired-" + uuid.uuid4().hex
        )
        os.replace(self.root, retired)
        fsync_directory(retired.parent)
        shutil.rmtree(retired)

    def stop_watcher(self) -> None:
        self.runner(
            ["/bin/launchctl", "bootout", self.domain, str(self.launch_agent)],
            check=False, text=True, capture_output=True,
        )
        if self._loaded():
            raise RuntimeError("watcher service could not be stopped before setup")

    def _restore_files(self) -> None:
        for index, target in reversed(tuple(enumerate(self.targets))):
            self._reject_symlink_ancestors(target)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            saved = self.snapshot / str(index)
            if saved.exists() or saved.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                self._copy(saved, target)
        expected = {
            str(index): self._digest(self.snapshot / str(index))
            for index in range(len(self.targets))
            if (self.snapshot / str(index)).exists()
        }
        actual = {
            str(index): self._digest(target)
            for index, target in enumerate(self.targets)
            if target.exists()
        }
        if actual != expected:
            raise InstallRecoveryError(
                "restored installer targets do not match snapshot at {}".format(self.root)
            )
        self.sync_targets()

    def sync_targets(self) -> None:
        """Flush final target files, directories, and their parent entries."""
        directories = set()
        for target in self.targets:
            if target.exists():
                if target.is_dir():
                    for item in target.rglob("*"):
                        if item.is_file():
                            descriptor = os.open(str(item), os.O_RDONLY)
                            try:
                                os.fsync(descriptor)
                            finally:
                                os.close(descriptor)
                        elif item.is_dir():
                            directories.add(item)
                    directories.add(target)
                else:
                    descriptor = os.open(str(target), os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            directories.add(target.parent)
        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            if directory.is_dir():
                fsync_directory(directory)

    def _restore_service(self) -> None:
        loaded = self._loaded()
        if self.was_loaded and not loaded:
            result = self.runner(
                ["/bin/launchctl", "bootstrap", self.domain, str(self.launch_agent)],
                check=False, text=True, capture_output=True,
            )
            if result.returncode != 0 or not self._loaded():
                raise InstallRecoveryError(
                    "watcher restore failed; recovery snapshot retained at {}".format(self.root)
                )
        elif not self.was_loaded and loaded:
            self.runner(
                ["/bin/launchctl", "bootout", self.domain, str(self.launch_agent)],
                check=False, text=True, capture_output=True,
            )
            if self._loaded():
                raise InstallRecoveryError(
                    "watcher stop failed; recovery snapshot retained at {}".format(self.root)
                )
        for path in self.legacy_agents:
            desired = self.legacy_loaded.get(path.stem, False)
            loaded = self._label_loaded(path.stem)
            if desired and not loaded and path.exists():
                result = self.runner(
                    ["/bin/launchctl", "bootstrap", self.domain, str(path)],
                    check=False, text=True, capture_output=True,
                )
                if result.returncode != 0 or not self._label_loaded(path.stem):
                    raise InstallRecoveryError(
                        "legacy service restore failed; recovery snapshot retained at {}"
                        .format(self.root)
                    )

    def _release(self) -> None:
        for lock in reversed(self.locks):
            lock.release()
        self.locks = []

    def __exit__(self, kind, error, traceback):
        try:
            if error is not None:
                try:
                    self.stop_watcher()
                    self._restore_files()
                    self._restore_service()
                    self._write_phase("rolled_back")
                    self._retire()
                except BaseException as recovery_error:
                    raise InstallRecoveryError(
                        "setup failed and recovery was incomplete; snapshot retained at {}: {}"
                        .format(self.root, recovery_error)
                    ) from error
            else:
                self.sync_targets()
                self._write_phase("committed")
                self._retire()
        finally:
            self._release()
        return False
