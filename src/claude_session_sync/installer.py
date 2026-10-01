"""Portable, per-user macOS installation templates and artifact management."""

from __future__ import annotations

import hashlib
from . import strict_json as json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import load_config
from .filesystem import fsync_directory
from .install_transaction import InstallTransaction


def _xml(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _plist(entries: Sequence[Tuple[str, str]]) -> bytes:
    body = "\n".join(
        "  <key>{}</key>\n  {}".format(_xml(key), value) for key, value in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n{}\n</dict>\n</plist>\n'.format(body)
    ).encode("utf-8")


def _string(value: object) -> str:
    return "<string>{}</string>".format(_xml(value))


def _array(values: Iterable[object]) -> str:
    items = "\n".join("    {}".format(_string(value)) for value in values)
    return "<array>\n{}\n  </array>".format(items)


@dataclass(frozen=True)
class InstallLayout:
    home: Path
    config_path: Path
    applications_dir: Path
    launch_agents_dir: Path
    support_dir: Path
    source_watcher: Path
    cli_command: Tuple[str, ...]

    @classmethod
    def for_home(
        cls,
        home: Path,
        *,
        cli_command: Optional[Sequence[str]] = None,
        source_watcher: Optional[Path] = None,
    ) -> "InstallLayout":
        home = Path(home)
        adjacent_watcher = Path(__file__).with_name("SessionSyncWatcher.swift")
        support_dir = home / "Library" / "Application Support" / "ClaudeSessionSync"
        command = cli_command or (str(support_dir / "bin" / "claude-session-sync"),)
        return cls(
            home=home,
            config_path=home / ".config" / "claude-session-sync" / "config.json",
            applications_dir=home / "Applications",
            launch_agents_dir=home / "Library" / "LaunchAgents",
            support_dir=support_dir,
            source_watcher=source_watcher or adjacent_watcher,
            cli_command=tuple(command),
        )

    @property
    def work_app(self) -> Path:
        return self.applications_dir / "Claude Work.app"

    @property
    def personal_app(self) -> Path:
        return self.applications_dir / "Claude Personal Synced.app"

    @property
    def watcher_binary(self) -> Path:
        return self.support_dir / "bin" / "SessionSyncWatcher"

    @property
    def layout_helper(self) -> Path:
        return self.support_dir / "bin" / "layoutdb"

    @property
    def runtime_cli(self) -> Path:
        return self.support_dir / "bin" / "claude-session-sync"

    @property
    def runtime_package(self) -> Path:
        return self.support_dir / "runtime" / "claude_session_sync"

    @property
    def watcher_stamp(self) -> Path:
        return self.support_dir / "bin" / ".SessionSyncWatcher.sha256"

    @property
    def layout_helper_stamp(self) -> Path:
        return self.support_dir / "bin" / ".layoutdb.sha256"

    @property
    def launch_agent(self) -> Path:
        return self.launch_agents_dir / "com.claude-session-sync.watcher.plist"

    @property
    def backups_dir(self) -> Path:
        return self.support_dir / "backups"

    @property
    def watcher_status(self) -> Path:
        return self.support_dir / "state" / "watcher-status.json"

    @property
    def legacy_launch_agents(self) -> Tuple[Path, ...]:
        return (self.launch_agents_dir / "com.djahlor.claude-session-sync.plist",)


@dataclass(frozen=True)
class InstallAction:
    kind: str
    path: Path


@dataclass(frozen=True)
class InstallReport:
    state: str
    actions: Tuple[InstallAction, ...]
    backups: Tuple[Path, ...] = ()

    @property
    def change_count(self) -> int:
        return len(self.actions)


class Installer:
    """Generate only Claude Session Sync-owned artifacts under a user home."""

    def __init__(
        self,
        layout: InstallLayout,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        backup_id: Callable[[], str] = lambda: time.strftime("%Y%m%d-%H%M%S"),
    ) -> None:
        if not layout.cli_command:
            raise ValueError("cli_command must not be empty")
        self.layout = layout
        self._runner = runner
        self._backup_id = backup_id
        self._backups: List[Path] = []

    def _config_template(self) -> bytes:
        claude_app = Path("/Applications/Claude.app")
        executable = claude_app / "Contents" / "MacOS" / "Claude"
        work_root = self.layout.home / "Library" / "Application Support" / "Claude"
        personal_root = (
            self.layout.home / "Library" / "Application Support" / "Claude-Personal"
        )
        document = {
            "version": 1,
            "approved_targets": [],
            "acknowledge_cross_account_copy": False,
            "acknowledge_cross_profile_copy": False,
            "target_policy": "approved-only",
            "sync_sidebar_layout": False,
            "sync_code_routines": False,
            "claude_executable": str(executable),
            "profiles": [
                {
                    "data_root": str(work_root),
                    "launch_command": ["/usr/bin/open", "-a", str(claude_app)],
                    "name": "Work",
                    "is_default": True,
                },
                {
                    "data_root": str(personal_root),
                    "launch_command": [
                        str(executable),
                        "--user-data-dir={}".format(personal_root),
                    ],
                    "name": "Personal",
                    "enabled": False,
                    "is_default": False,
                },
            ],
            "retention": 10,
            "state_dir": str(self.layout.support_dir / "state"),
        }
        return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def default_config_data(self) -> bytes:
        """Return the validated default configuration used for a fresh setup."""

        data = self._config_template()
        self._load_config_bytes(data)
        return data

    def _bundle(self, profile: str, identifier: str) -> Dict[Path, Tuple[bytes, int]]:
        command = self.layout.cli_command + (
            "--config",
            str(self.layout.config_path),
            "switch",
            profile,
            "--wait-for-exit",
            "15",
        )
        launcher = "#!/bin/sh\nexec {}\n".format(shlex.join(command)).encode("utf-8")
        info = _plist(
            (
                ("CFBundleDevelopmentRegion", _string("English")),
                ("CFBundleExecutable", _string("launcher")),
                ("CFBundleIdentifier", _string(identifier)),
                ("CFBundleInfoDictionaryVersion", _string("6.0")),
                ("CFBundleName", _string("Claude {}".format(profile))),
                ("CFBundlePackageType", _string("APPL")),
                ("CFBundleVersion", _string("1")),
            )
        )
        return {
            Path("Contents/Info.plist"): (info, 0o644),
            Path("Contents/MacOS/launcher"): (launcher, 0o755),
        }

    def _launch_agent(self, config_data: Optional[bytes] = None) -> bytes:
        configured = None
        if config_data is not None:
            configured = self._load_config_bytes(config_data)
            claude_executable = configured.claude_executable
            watcher_status = configured.state_dir / "watcher-status.json"
        elif self.layout.config_path.exists():
            configured = load_config(self.layout.config_path)
            claude_executable = configured.claude_executable
            watcher_status = configured.state_dir / "watcher-status.json"
        else:
            template = json.loads(self._config_template().decode("utf-8"))
            claude_executable = Path(template["claude_executable"])
            watcher_status = Path(template["state_dir"]) / "watcher-status.json"
        account_arguments = ()
        if configured is not None and configured.target_policy in (
            "all-configured-profiles",
            "logins",
        ):
            defaults = [profile for profile in configured.profiles if profile.is_default]
            if len(defaults) == 1:
                account_arguments = (
                    "--account-file", str(defaults[0].data_root / "config.json"),
                    "--profile", defaults[0].name,
                )
        arguments = (
            (
                str(self.layout.watcher_binary),
                "--claude-executable",
                str(claude_executable),
                "--status",
                str(watcher_status),
            )
            + account_arguments
            + ("--",)
            + self.layout.cli_command
            + ("--config", str(self.layout.config_path))
        )
        return _plist(
            (
                ("Label", _string("com.claude-session-sync.watcher")),
                ("ProgramArguments", _array(arguments)),
                ("RunAtLoad", "<true/>"),
                ("KeepAlive", "<true/>"),
            )
        )

    def _runtime_files(self) -> Dict[Path, Tuple[bytes, int]]:
        source_package = Path(__file__).resolve().parent
        files = {}
        allowed_names = {"LICENSE", "COPYING"}
        allowed_suffixes = {".py", ".cc", ".h", ".md", ".swift"}
        for source in sorted(source_package.rglob("*")):
            if (
                source.is_file()
                and "__pycache__" not in source.parts
                and (
                    source.name in allowed_names
                    or source.suffix in allowed_suffixes
                )
            ):
                files[source.relative_to(source_package)] = (
                    source.read_bytes(),
                    0o644,
                )
        files[Path("SessionSyncWatcher.swift")] = (
            self.layout.source_watcher.read_bytes(), 0o644
        )
        return files

    def _runtime_shim(self) -> bytes:
        runtime_root = str(self.layout.runtime_package.parent)
        return (
            "#!/usr/bin/python3\n"
            "import sys\n"
            "sys.dont_write_bytecode = True\n"
            "sys.path.insert(0, {!r})\n"
            "from claude_session_sync.cli import main\n"
            "raise SystemExit(main())\n".format(runtime_root)
        ).encode("utf-8")

    @staticmethod
    def _same_file(path: Path, data: bytes, mode: int) -> bool:
        try:
            return (
                path.read_bytes() == data and stat.S_IMODE(path.stat().st_mode) == mode
            )
        except OSError:
            return False

    def _same_bundle(self, target: Path, files: Dict[Path, Tuple[bytes, int]]) -> bool:
        if not target.is_dir():
            return False
        expected = {target / relative for relative in files}
        actual = {path for path in target.rglob("*") if path.is_file()}
        return expected == actual and all(
            self._same_file(target / relative, data, mode)
            for relative, (data, mode) in files.items()
        )

    def _watcher_digest(self) -> str:
        return hashlib.sha256(self.layout.source_watcher.read_bytes()).hexdigest()

    def _watcher_current(self) -> bool:
        try:
            return (
                self.layout.watcher_binary.is_file()
                and os.access(self.layout.watcher_binary, os.X_OK)
                and self.layout.watcher_stamp.read_text(encoding="ascii").strip()
                == self._watcher_digest()
            )
        except OSError:
            return False

    def _layout_sources(self) -> Tuple[Path, ...]:
        package = Path(__file__).resolve().parent
        sources = [package / "layoutdb.cc"]
        sources.extend(sorted((package / "vendor" / "leveldb").rglob("*.cc")))
        sources.extend(sorted((package / "vendor" / "snappy" / "snappy").glob("*.cc")))
        return tuple(sources)

    def _layout_helper_digest(self) -> str:
        package = Path(__file__).resolve().parent
        digest = hashlib.sha256()
        source_files = list(self._layout_sources())
        source_files.extend(sorted((package / "vendor").rglob("*.h")))
        for source in source_files:
            digest.update(str(source.relative_to(package)).encode("utf-8"))
            digest.update(b"\x00")
            digest.update(source.read_bytes())
        return digest.hexdigest()

    def _layout_helper_current(self) -> bool:
        try:
            return (
                self.layout.layout_helper.is_file()
                and os.access(self.layout.layout_helper, os.X_OK)
                and self.layout.layout_helper_stamp.read_text(encoding="ascii").strip()
                == self._layout_helper_digest()
            )
        except OSError:
            return False

    def _load_config_bytes(self, data: bytes):
        descriptor, name = tempfile.mkstemp(prefix=".session-sync-config-")
        path = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.chmod(path, 0o600)
            return load_config(path)
        finally:
            path.unlink(missing_ok=True)

    def _planned_actions(self, config_data: Optional[bytes] = None) -> List[InstallAction]:
        actions = []
        for legacy in self.layout.legacy_launch_agents:
            if legacy.exists():
                actions.append(InstallAction("disable-legacy", legacy))
        if config_data is not None:
            configured = self._load_config_bytes(config_data)
            if not self._same_file(self.layout.config_path, config_data, 0o600):
                actions.append(InstallAction(
                    "replace" if self.layout.config_path.exists() else "create",
                    self.layout.config_path,
                ))
        elif not self.layout.config_path.exists():
            actions.append(InstallAction("create", self.layout.config_path))
        elif stat.S_IMODE(self.layout.config_path.stat().st_mode) != 0o600:
            actions.append(InstallAction("permission", self.layout.config_path))
        if config_data is not None:
            enabled_profiles = {profile.name for profile in configured.profiles}
        elif self.layout.config_path.exists():
            enabled_profiles = {
                profile.name
                for profile in load_config(self.layout.config_path).profiles
            }
        else:
            template = json.loads(self._config_template().decode("utf-8"))
            enabled_profiles = {
                profile["name"]
                for profile in template["profiles"]
                if profile.get("enabled", True)
            }
        runtime_files = self._runtime_files()
        if not self._same_bundle(self.layout.runtime_package, runtime_files):
            actions.append(
                InstallAction(
                    "replace" if self.layout.runtime_package.exists() else "create",
                    self.layout.runtime_package,
                )
            )
        shim = self._runtime_shim()
        if not self._same_file(self.layout.runtime_cli, shim, 0o755):
            actions.append(
                InstallAction(
                    "replace" if self.layout.runtime_cli.exists() else "create",
                    self.layout.runtime_cli,
                )
            )
        wrapper_profiles = enabled_profiles if len(enabled_profiles) > 1 else set()
        bundle_specs = (
            (
                "Work",
                self.layout.work_app,
                self._bundle("Work", "com.claude-session-sync.work"),
            ),
            (
                "Personal",
                self.layout.personal_app,
                self._bundle("Personal", "com.claude-session-sync.personal"),
            ),
        )
        for profile_name, path, files in bundle_specs:
            if profile_name not in wrapper_profiles:
                if path.exists():
                    actions.append(InstallAction("remove-disabled", path))
            elif not self._same_bundle(path, files):
                actions.append(
                    InstallAction("replace" if path.exists() else "create", path)
                )
        if not self._watcher_current():
            actions.append(InstallAction("compile", self.layout.watcher_binary))
        if not self._layout_helper_current():
            actions.append(InstallAction("compile", self.layout.layout_helper))
        agent = self._launch_agent(config_data)
        if not self._same_file(self.layout.launch_agent, agent, 0o644):
            actions.append(
                InstallAction(
                    "replace" if self.layout.launch_agent.exists() else "create",
                    self.layout.launch_agent,
                )
            )
        return actions

    def _backup(self, target: Path) -> Path:
        root = self.layout.backups_dir / self._backup_id()
        destination = root / target.name
        index = 1
        while destination.exists():
            destination = root / "{}.{}".format(target.name, index)
            index += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(target, destination)
        fsync_directory(target.parent)
        fsync_directory(destination.parent)
        self._backups.append(destination)
        return destination

    def _atomic_file(
        self, target: Path, data: bytes, mode: int, *, lint: bool = False
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".session-sync-", dir=str(target.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            if lint:
                self._runner(
                    ["/usr/bin/plutil", "-lint", str(temporary)],
                    check=True,
                    text=True,
                    capture_output=True,
                )
            if target.exists():
                self._backup(target)
            os.replace(temporary, target)
            fsync_directory(target.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _install_bundle(
        self, target: Path, files: Dict[Path, Tuple[bytes, int]]
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=".session-sync-app-", dir=str(target.parent))
        )
        try:
            for relative, (data, mode) in files.items():
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                os.chmod(destination, mode)
                if destination.suffix == ".plist":
                    self._runner(
                        ["/usr/bin/plutil", "-lint", str(destination)],
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                descriptor = os.open(str(destination), os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            for directory in sorted(
                (path for path in staging.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts), reverse=True,
            ):
                fsync_directory(directory)
            fsync_directory(staging)
            if target.exists():
                self._backup(target)
            os.replace(staging, target)
            fsync_directory(target.parent)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _stage_watcher(self, target: Path) -> None:
        self._runner(
            [
                "/usr/bin/xcrun", "swiftc", str(self.layout.source_watcher),
                "-o", str(target), "-framework", "AppKit",
            ],
            check=True, text=True, capture_output=True,
        )
        if not target.is_file():
            raise RuntimeError("swiftc did not create the watcher binary")
        os.chmod(target, 0o755)

    def _stage_layout_helper(self, target: Path) -> None:
        package = Path(__file__).resolve().parent
        leveldb = package / "vendor" / "leveldb"
        snappy = package / "vendor" / "snappy"
        command = [
            "/usr/bin/xcrun", "clang++", "-std=c++11", "-O2", "-DNDEBUG",
            "-DSNAPPY", "-DOS_MACOSX", "-DLEVELDB_PLATFORM_POSIX",
            "-DLEVELDB_ATOMIC_PRESENT", "-Wno-deprecated-declarations",
            "-I{}".format(leveldb), "-I{}".format(leveldb / "include"),
            "-I{}".format(snappy / "snappy"), "-I{}".format(snappy / "mac"),
        ]
        command.extend(str(source) for source in self._layout_sources())
        command.extend(("-o", str(target)))
        try:
            self._runner(command, check=True, text=True, capture_output=True)
        except subprocess.CalledProcessError as error:
            lines = (error.stderr or "").strip().splitlines()
            detail = " | ".join(lines[-8:]) if lines else "compiler returned a failure"
            raise RuntimeError(
                "could not compile the sidebar helper: {}".format(detail)
            ) from error
        if not target.is_file():
            raise RuntimeError("clang++ did not create the sidebar helper")
        os.chmod(target, 0o755)

    def _transaction_targets(self) -> Tuple[Path, ...]:
        return (
            self.layout.config_path,
            self.layout.work_app,
            self.layout.personal_app,
            self.layout.runtime_package,
            self.layout.runtime_cli,
            self.layout.watcher_binary,
            self.layout.watcher_stamp,
            self.layout.layout_helper,
            self.layout.layout_helper_stamp,
            self.layout.launch_agent,
            self.layout.backups_dir,
        ) + self.layout.legacy_launch_agents

    def _install_transaction(
        self, config_data: Optional[bytes] = None
    ) -> InstallTransaction:
        state_roots = {self.layout.support_dir / "state"}
        if self.layout.config_path.exists():
            try:
                state_roots.add(load_config(self.layout.config_path).state_dir)
            except (OSError, ValueError):
                if not (self.layout.support_dir / "install-state").exists():
                    raise
        if config_data is not None:
            state_roots.add(self._load_config_bytes(config_data).state_dir)
        return InstallTransaction(
            targets=self._transaction_targets(), state_roots=state_roots,
            launch_agent=self.layout.launch_agent,
            recovery_root=self.layout.support_dir / "install-state",
            runner=self._runner, legacy_agents=self.layout.legacy_launch_agents,
        )

    def setup(
        self,
        *,
        dry_run: bool,
        config_data: Optional[bytes] = None,
        before_activation: Optional[Callable[[Path], None]] = None,
    ) -> InstallReport:
        """Validate and stage a complete setup, then apply it transactionally.

        before_activation gets the sidebar helper's path. It runs while the
        old watcher is stopped and sync is locked, before the new watcher
        starts, so whatever it records is in place before any sync.
        """

        if dry_run:
            if (self.layout.support_dir / "install-state").exists():
                with self._install_transaction(config_data):
                    pass
            return InstallReport("planned", tuple(self._planned_actions(config_data)))
        staging = Path(tempfile.mkdtemp(prefix="claude-session-sync-setup-"))
        try:
            # The install lock encloses recovery, planning, staging, and apply.
            transaction = self._install_transaction(config_data)
            with transaction:
                actions = self._planned_actions(config_data)
                watcher = staging / "SessionSyncWatcher"
                helper = staging / "layoutdb"
                if any(a.path == self.layout.watcher_binary for a in actions):
                    self._stage_watcher(watcher)
                if any(a.path == self.layout.layout_helper for a in actions):
                    self._stage_layout_helper(helper)
                self._validate_staged_plists(staging, config_data)
                transaction.stop_watcher()
                self._remove_retired_launch_guard(config_data)
                if before_activation is not None:
                    before_activation(helper if helper.exists() else self.layout.layout_helper)
                report = self._apply_setup(actions, config_data, watcher, helper)
                remaining = self._planned_actions(config_data)
                if remaining:
                    raise RuntimeError(
                        "installed artifacts failed verification: {}".format(
                            ", ".join(str(action.path) for action in remaining)
                        )
                    )
                transaction.sync_targets()
                return report
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _remove_retired_launch_guard(self, config_data: Optional[bytes]) -> None:
        """Older versions left this file after a slow launch. Nothing reads it now."""

        if config_data is not None:
            state_dir = self._load_config_bytes(config_data).state_dir
        elif self.layout.config_path.exists():
            state_dir = load_config(self.layout.config_path).state_dir
        else:
            return
        (state_dir / "launch-pending.json").unlink(missing_ok=True)

    def _validate_staged_plists(
        self, staging: Path, config_data: Optional[bytes]
    ) -> None:
        data = [self._launch_agent(config_data)] + [
            self._bundle(profile, identifier)[Path("Contents/Info.plist")][0]
            for profile, identifier in (
                ("Work", "com.claude-session-sync.work"),
                ("Personal", "com.claude-session-sync.personal"),
            )
        ]
        for index, content in enumerate(data):
            candidate = staging / "validate-{}.plist".format(index)
            candidate.write_bytes(content)
            self._runner(["/usr/bin/plutil", "-lint", str(candidate)], check=True,
                         text=True, capture_output=True)

    def _apply_setup(self, actions, config_data, watcher, helper) -> InstallReport:
        self._backups = []
        for action in actions:
            if action.kind == "disable-legacy":
                continue
            if action.path == self.layout.config_path:
                if config_data is not None:
                    self._atomic_file(action.path, config_data, 0o600)
                elif action.kind == "create":
                    self._atomic_file(action.path, self._config_template(), 0o600)
                else:
                    os.chmod(action.path, 0o600)
            elif action.path == self.layout.runtime_package:
                self._install_bundle(action.path, self._runtime_files())
            elif action.path == self.layout.runtime_cli:
                self._atomic_file(action.path, self._runtime_shim(), 0o755)
            elif action.path in (self.layout.work_app, self.layout.personal_app):
                profile = "Work" if action.path == self.layout.work_app else "Personal"
                if action.kind == "remove-disabled":
                    self._backup(action.path)
                else:
                    self._install_bundle(
                        action.path, self._bundle(
                            profile, "com.claude-session-sync." + profile.lower()
                        )
                    )
            elif action.path == self.layout.watcher_binary:
                self._atomic_file(action.path, watcher.read_bytes(), 0o755)
                self._atomic_file(self.layout.watcher_stamp,
                    (self._watcher_digest() + "\n").encode("ascii"), 0o644)
            elif action.path == self.layout.layout_helper:
                self._atomic_file(action.path, helper.read_bytes(), 0o755)
                self._atomic_file(self.layout.layout_helper_stamp,
                    (self._layout_helper_digest() + "\n").encode("ascii"), 0o644)
            elif action.path == self.layout.launch_agent:
                self._atomic_file(action.path, self._launch_agent(config_data),
                                  0o644, lint=True)
        for action in (a for a in actions if a.kind == "disable-legacy"):
            self._runner(["/bin/launchctl", "bootout",
                          "gui/{}".format(os.getuid()), str(action.path)],
                         check=False, text=True, capture_output=True)
            verification = self._runner(
                ["/bin/launchctl", "print",
                 "gui/{}/{}".format(os.getuid(), action.path.stem)],
                check=False, text=True, capture_output=True)
            if verification.returncode == 0:
                raise RuntimeError("legacy session sync service is still loaded: {}"
                                   .format(action.path.stem))
            self._backup(action.path)
        self.load_launch_agent()
        return InstallReport("installed" if actions else "noop", tuple(actions),
                             tuple(self._backups))

    def install(self, *, dry_run: bool) -> InstallReport:
        """Compatibility wrapper for callers that do not update configuration."""

        return self.setup(dry_run=dry_run)

    def load_launch_agent(self) -> None:
        """Bootstrap or replace the per-user watcher in the current GUI domain."""

        domain = "gui/{}".format(os.getuid())
        self._runner(
            ["/bin/launchctl", "bootout", domain, str(self.layout.launch_agent)],
            check=False,
            text=True,
            capture_output=True,
        )
        self._runner(
            ["/bin/launchctl", "bootstrap", domain, str(self.layout.launch_agent)],
            check=True,
            text=True,
            capture_output=True,
        )

    def unload_launch_agent(self) -> None:
        """Stop the per-user watcher before its plist is removed."""

        self._runner(
            [
                "/bin/launchctl",
                "bootout",
                "gui/{}".format(os.getuid()),
                str(self.layout.launch_agent),
            ],
            check=False,
            text=True,
            capture_output=True,
        )

    def uninstall(self, *, dry_run: bool) -> InstallReport:
        targets = (
            self.layout.work_app,
            self.layout.personal_app,
            self.layout.launch_agent,
            self.layout.watcher_binary,
            self.layout.layout_helper,
            self.layout.runtime_cli,
            self.layout.runtime_package,
        )
        if dry_run:
            if (self.layout.support_dir / "install-state").exists():
                with self._install_transaction():
                    pass
            actions = [
                InstallAction("remove", target) for target in targets if target.exists()
            ]
            return InstallReport("planned", tuple(actions))
        transaction = self._install_transaction()
        with transaction:
            actions = [
                InstallAction("remove", target) for target in targets if target.exists()
            ]
            transaction.stop_watcher()
            self._backups = []
            for action in actions:
                self._backup(action.path)
            for stamp in (self.layout.watcher_stamp, self.layout.layout_helper_stamp):
                if stamp.exists():
                    stamp.unlink()
            remaining = [target for target in targets if target.exists()]
            if remaining:
                raise RuntimeError(
                    "uninstall target still exists: {}".format(remaining[0])
                )
            transaction.sync_targets()
            state = "uninstalled" if actions else "noop"
            return InstallReport(state, tuple(actions), tuple(self._backups))
