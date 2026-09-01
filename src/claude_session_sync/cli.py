"""Command-line boundary for safe session synchronization."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import subprocess
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TextIO

from .config import Config, load_config
from .model import Plan, SyncRequest


ConfigLoader = Callable[[Path], Config]
PlannerFactory = Callable[[Config], Any]
EngineFactory = Callable[[Config], Any]
InstallerFactory = Callable[[Path], Any]
PROCESS_EXIT_POLL_SECONDS = 0.1
PROCESS_PROBE_TIMEOUT_SECONDS = 2.0
LAUNCH_CONFIRMATION_TIMEOUT_SECONDS = 5.0
LAUNCH_GUARD_FILENAME = "launch-pending.json"
SWITCH_WRITER_WAIT_SECONDS = 15.0
SWITCH_REVALIDATION_RETRIES = 2


def _default_planner_factory(_config: Config) -> Any:
    from .planner import Planner

    return Planner()


def _default_engine_factory(config: Config) -> Any:
    from .processes import managed_processes
    from .transaction import TransactionEngine

    return TransactionEngine(
        config.state_dir,
        process_probe=lambda: bool(
            managed_processes(
                config.profiles,
                executable=config.claude_executable,
                timeout=PROCESS_PROBE_TIMEOUT_SECONDS,
            )
        ),
        retention=config.retention,
    )


def _default_installer_factory(config_path: Path) -> Any:
    from .installer import InstallLayout, Installer

    layout = InstallLayout.for_home(
        Path.home(),
    )
    return Installer(replace(layout, config_path=config_path))


@dataclass(frozen=True)
class CliDependencies:
    """Injected system boundaries used by command tests and platform adapters."""

    config_loader: ConfigLoader = load_config
    planner_factory: PlannerFactory = _default_planner_factory
    engine_factory: EngineFactory = _default_engine_factory
    process_probe: Any = None
    launcher: Any = None
    installer_factory: InstallerFactory = _default_installer_factory
    clock: Callable[[], float] = time.monotonic
    monotonic: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    launch_confirmation_timeout: float = LAUNCH_CONFIRMATION_TIMEOUT_SECONDS


def _nonnegative_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(seconds):
        raise argparse.ArgumentTypeError("must be a finite number")
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return seconds


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-session-sync")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(
            os.environ.get(
                "CLAUDE_SESSION_SYNC_CONFIG",
                "~/.config/claude-session-sync/config.json",
            )
        ).expanduser(),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="inspect the next synchronization")
    plan.add_argument("--json", action="store_true", dest="as_json")
    sync = commands.add_parser("sync", help="apply the next synchronization")
    sync.add_argument("--json", action="store_true", dest="as_json")
    commands.add_parser("auto", help="sync after Claude terminates")
    switch = commands.add_parser("switch", help="sync and launch a profile")
    switch.add_argument("profile")
    switch.add_argument("--no-launch", action="store_true")
    switch.add_argument(
        "--wait-for-exit",
        type=_nonnegative_seconds,
        default=0.0,
        metavar="SECONDS",
        help="wait up to SECONDS for a managed Claude process to exit",
    )
    rollback = commands.add_parser("rollback", help="restore an earlier run")
    rollback.add_argument("run_id")
    rollback.add_argument("--json", action="store_true", dest="as_json")
    status = commands.add_parser("status", help="show current adapter state")
    status.add_argument("--json", action="store_true", dest="as_json")
    doctor = commands.add_parser("doctor", help="validate configuration safety")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    approve = commands.add_parser(
        "approve-current-targets",
        help="approve exactly the account/workspace targets currently present",
    )
    approve_mode = approve.add_mutually_exclusive_group(required=True)
    approve_mode.add_argument("--dry-run", action="store_true")
    approve_mode.add_argument("--apply", action="store_true")
    configure = commands.add_parser(
        "configure",
        help="configure automatic targets and optional profile launchers",
    )
    configure.add_argument(
        "--automatic-targets",
        action="store_true",
        help="trust future account/workspace targets inside configured profiles",
    )
    configure.add_argument(
        "--enable-personal",
        action="store_true",
        help="enable the generated Personal profile",
    )
    configure_mode = configure.add_mutually_exclusive_group(required=True)
    configure_mode.add_argument("--dry-run", action="store_true")
    configure_mode.add_argument("--apply", action="store_true")
    clear_guard = commands.add_parser(
        "clear-launch-guard",
        help="clear a failed launch guard after confirming Claude is stopped",
    )
    clear_guard_mode = clear_guard.add_mutually_exclusive_group(required=True)
    clear_guard_mode.add_argument("--dry-run", action="store_true")
    clear_guard_mode.add_argument("--apply", action="store_true")
    for name in ("install", "uninstall"):
        installer = commands.add_parser(name, help="{} macOS adapters".format(name))
        mode = installer.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
    return parser


def _plan_state(plan: Plan) -> str:
    if plan.invalid_replicas:
        return "blocked_invalid"
    if plan.conflicts:
        return "blocked_conflict"
    if not plan.operations:
        return "noop"
    return "planned"


def _plan_summary(plan: Plan, duration_ms: int) -> dict:
    # This is an allowlist by design. Domain identifiers and source paths must
    # never leak into machine-readable CLI output.
    payload = {
        "bytes": plan.total_bytes,
        "counts": {
            "conflicts": len(plan.conflicts),
            "invalid_replicas": len(plan.invalid_replicas),
            "operations": len(plan.operations),
        },
        "duration_ms": duration_ms,
        "plan_id": plan.plan_id,
        "state": _plan_state(plan),
    }
    if payload["state"] == "blocked_invalid":
        target_only = bool(plan.invalid_replicas) and all(
            item.reason == "target namespace is not approved"
            for item in plan.invalid_replicas
        )
        payload["next_action"] = (
            "approve-targets-or-enable-automatic-targets"
            if target_only
            else "run-doctor"
        )
    elif payload["state"] == "blocked_conflict":
        payload["next_action"] = "run-doctor"
    return payload


def _receipt_summary(receipt: Any, duration_ms: int) -> dict:
    return {
        "bytes": receipt.bytes_copied,
        "counts": {"operations": receipt.operation_count},
        "duration_ms": duration_ms,
        "plan_id": receipt.plan_id,
        "run_id": receipt.run_id,
        "state": receipt.status,
    }


def _recovery_summary(receipt: Any, duration_ms: int) -> dict:
    return {
        "bytes": receipt.bytes_restored,
        "counts": {"operations": receipt.operation_count},
        "duration_ms": duration_ms,
        "run_id": receipt.run_id,
        "state": receipt.status,
    }


def _write(payload: dict, *, as_json: bool, stream: TextIO) -> None:
    if as_json:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        return
    fields = ["{}={}".format(key, value) for key, value in payload.items()]
    stream.write(" ".join(fields) + "\n")


def _running_processes(
    config: Config,
    dependencies: CliDependencies,
    *,
    timeout: float = PROCESS_PROBE_TIMEOUT_SECONDS,
) -> tuple:
    from .processes import managed_processes

    return managed_processes(
        config.profiles,
        probe=dependencies.process_probe,
        executable=config.claude_executable,
        timeout=timeout,
    )


def _wait_for_managed_processes_to_exit(
    config: Config,
    dependencies: CliDependencies,
    timeout: float,
) -> bool:
    deadline = dependencies.monotonic() + timeout
    while True:
        if timeout > 0:
            remaining = deadline - dependencies.monotonic()
            if remaining <= 0:
                return False
            probe_timeout = min(PROCESS_PROBE_TIMEOUT_SECONDS, remaining)
        else:
            probe_timeout = PROCESS_PROBE_TIMEOUT_SECONDS
        try:
            running = bool(
                _running_processes(
                    config,
                    dependencies,
                    timeout=max(0.001, probe_timeout),
                )
            )
        except subprocess.TimeoutExpired:
            return False
        if not running:
            return True
        if timeout <= 0:
            return False
        remaining = deadline - dependencies.monotonic()
        if remaining <= 0:
            return False
        dependencies.sleeper(min(PROCESS_EXIT_POLL_SECONDS, remaining))


def _normalized_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _wait_for_managed_profile_to_start(
    config: Config,
    dependencies: CliDependencies,
    profile: Any,
    timeout: float,
) -> str:
    if timeout <= 0:
        return "disabled"
    selected_root = _normalized_path(Path(profile.data_root))
    deadline = dependencies.monotonic() + timeout
    while True:
        remaining = deadline - dependencies.monotonic()
        if remaining <= 0:
            return "timeout"
        try:
            processes = _running_processes(
                config,
                dependencies,
                timeout=max(
                    0.001,
                    min(PROCESS_PROBE_TIMEOUT_SECONDS, remaining),
                ),
            )
        except subprocess.TimeoutExpired:
            return "probe-timeout"
        if any(
            _normalized_path(Path(getattr(process, "user_data_dir"))) == selected_root
            for process in processes
            if getattr(process, "user_data_dir", None) is not None
        ):
            return "confirmed"
        if processes:
            return "wrong-profile"
        remaining = deadline - dependencies.monotonic()
        if remaining <= 0:
            return "timeout"
        dependencies.sleeper(min(PROCESS_EXIT_POLL_SECONDS, remaining))


def _launch_guard_path(config: Config) -> Path:
    return config.state_dir / LAUNCH_GUARD_FILENAME


def _write_launch_guard(config: Config, profile_name: str) -> None:
    from .filesystem import atomic_write_bytes, ensure_private_directory

    ensure_private_directory(config.state_dir)
    document = {
        "profile": profile_name,
        "version": 1,
    }
    encoded = (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
    path = _launch_guard_path(config)
    atomic_write_bytes(path, encoded)
    os.chmod(str(path), 0o600)


def _valid_launch_guard(config: Config) -> Optional[str]:
    path = _launch_guard_path(config)
    if not os.path.lexists(str(path)):
        return None
    metadata = os.lstat(str(path))
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 2_048:
        raise ValueError("launch guard is invalid")
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("version") != 1
        or not isinstance(document.get("profile"), str)
        or not document["profile"]
    ):
        raise ValueError("launch guard is invalid")
    return document["profile"]


def _clear_launch_guard(config: Config) -> None:
    from .filesystem import durable_unlink

    path = _launch_guard_path(config)
    if _valid_launch_guard(config) is None:
        return
    durable_unlink(path)


def _reconcile_launch_guard(
    config: Config,
    dependencies: CliDependencies,
) -> bool:
    guarded_profile_name = _valid_launch_guard(config)
    if guarded_profile_name is None:
        return True
    guarded_profile = next(
        (
            profile
            for profile in config.profiles
            if profile.name == guarded_profile_name
        ),
        None,
    )
    if guarded_profile is None:
        return False
    try:
        running = _running_processes(config, dependencies)
    except subprocess.TimeoutExpired:
        return False
    guarded_root = _normalized_path(Path(guarded_profile.data_root))
    guarded_running = any(
        _normalized_path(Path(getattr(process, "user_data_dir"))) == guarded_root
        for process in running
        if getattr(process, "user_data_dir", None) is not None
    )
    if not guarded_running:
        return False
    _clear_launch_guard(config)
    return True


def _launch_guard_failure(config: Config) -> int:
    try:
        return int(_valid_launch_guard(config) is not None)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return 1


def _clear_launch_guard_command(
    config: Config,
    dependencies: CliDependencies,
    *,
    apply: bool,
) -> tuple[dict, int]:
    from .locking import ExclusiveFileLock, LockUnavailableError

    handoff = ExclusiveFileLock(
        config.state_dir / "switch-handoff.lock",
        mode="auto",
        timeout=0,
    )
    try:
        handoff.acquire()
    except LockUnavailableError:
        return {"state": "blocked_switch", "reason": "handoff-running"}, 1
    try:
        try:
            if _running_processes(config, dependencies):
                return {"state": "blocked_app", "reason": "app-running"}, 1
        except subprocess.TimeoutExpired:
            return {"state": "blocked_probe", "reason": "process-timeout"}, 1
        try:
            pending = _valid_launch_guard(config) is not None
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return {"state": "blocked_invalid", "reason": "invalid-guard"}, 1
        if not pending:
            return {"state": "noop", "counts": {"guards": 0}}, 0
        if apply:
            _clear_launch_guard(config)
        return {
            "state": "cleared" if apply else "planned",
            "counts": {"guards": 1},
        }, 0
    finally:
        handoff.release()


def _run_switch(
    arguments: Any,
    config: Config,
    dependencies: CliDependencies,
    output: TextIO,
) -> int:
    from .locking import ExclusiveFileLock, LockUnavailableError

    profile = _find_profile(config, arguments.profile)
    handoff = ExclusiveFileLock(
        config.state_dir / "switch-handoff.lock",
        mode="auto",
        timeout=0,
    )
    try:
        handoff.acquire()
    except LockUnavailableError:
        _write(
            {"state": "blocked_switch", "reason": "handoff-running"},
            as_json=False,
            stream=output,
        )
        return 1

    try:
        if not _reconcile_launch_guard(config, dependencies):
            _write(
                {"state": "blocked_switch", "reason": "launch-unconfirmed"},
                as_json=False,
                stream=output,
            )
            return 1
        if not _wait_for_managed_processes_to_exit(
            config,
            dependencies,
            arguments.wait_for_exit,
        ):
            _write(
                {"state": "blocked_app", "reason": "app-running"},
                as_json=False,
                stream=output,
            )
            return 1
        started = dependencies.clock()
        planner = dependencies.planner_factory(config)
        engine = dependencies.engine_factory(config)
        plan = planner.plan(SyncRequest(config))
        dependencies.clock()
        writer_deadline = dependencies.monotonic() + SWITCH_WRITER_WAIT_SECONDS
        revalidation_retries = SWITCH_REVALIDATION_RETRIES
        while True:
            if _plan_state(plan).startswith("blocked_"):
                _write(
                    _plan_summary(
                        plan,
                        round((dependencies.clock() - started) * 1000),
                    ),
                    as_json=False,
                    stream=output,
                )
                return 1
            try:
                receipt = engine.apply(plan)
                break
            except Exception as error:
                remaining = writer_deadline - dependencies.monotonic()
                if _busy_reason(error) == "busy" and remaining > 0:
                    dependencies.sleeper(min(PROCESS_EXIT_POLL_SECONDS, remaining))
                    continue
                if (
                    type(error).__name__ == "RevalidationError"
                    and revalidation_retries > 0
                ):
                    revalidation_retries -= 1
                    plan = planner.plan(SyncRequest(config))
                    continue
                raise
        duration_ms = round((dependencies.clock() - started) * 1000)
        if not arguments.no_launch:
            confirmation_enabled = dependencies.launch_confirmation_timeout > 0
            if confirmation_enabled:
                _write_launch_guard(config, profile.name)
            if dependencies.launcher is None:
                from .launcher import Launcher

                Launcher().launch(profile.launch_command)
            else:
                dependencies.launcher.launch(profile.launch_command)
            if confirmation_enabled:
                confirmation = _wait_for_managed_profile_to_start(
                    config,
                    dependencies,
                    profile,
                    dependencies.launch_confirmation_timeout,
                )
                if confirmation != "confirmed":
                    _write(
                        {
                            "state": "launch_unconfirmed",
                            "reason": confirmation,
                        },
                        as_json=False,
                        stream=output,
                    )
                    return 1
                _clear_launch_guard(config)
        _write(
            _receipt_summary(receipt, duration_ms),
            as_json=False,
            stream=output,
        )
        return 0
    finally:
        handoff.release()


def _find_profile(config: Config, requested_name: str) -> Any:
    for profile in config.profiles:
        if profile.name == requested_name:
            return profile
    raise ValueError("unknown profile: {}".format(requested_name))


def _busy_reason(error: Exception) -> Optional[str]:
    name = type(error).__name__
    if name in ("TransactionBusyError", "BlockingIOError", "TimeoutError"):
        return "busy"
    if name == "AppRunningError":
        return "app-running"
    return None


def _watcher_failure(state_dir: Path) -> int:
    path = state_dir / "watcher-status.json"
    if not path.exists():
        return 0
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 1
    return 0 if isinstance(document, dict) and document.get("state") == "ok" else 1


def _doctor_summary(config: Config) -> dict:
    from .journal import JournalError, pending_recovery_runs
    from .store import SessionStore

    discovery = SessionStore().discover(config)
    approved = {
        (target.profile_name, target.account_id, target.workspace_id)
        for target in config.approved_targets
    }
    unapproved = 0
    if config.target_policy == "approved-only":
        unapproved = sum(
            1
            for target in discovery.targets
            if (target.profile_name, target.account_id, target.workspace_id)
            not in approved
        )
    errors = len(discovery.invalid_replicas) + unapproved
    executable = config.claude_executable
    if not executable.is_file() or not os.access(str(executable), os.X_OK):
        errors += 1
    for profile in config.profiles:
        root = profile.data_root
        if root.is_symlink() or not root.is_dir():
            errors += 1
        program = profile.launch_command[0]
        if os.path.isabs(program):
            launchable = Path(program).is_file() and os.access(program, os.X_OK)
        else:
            launchable = shutil.which(program) is not None
        if not launchable:
            errors += 1
    if not discovery.targets:
        errors += 1
    account_namespaces = {
        (target.profile_name, target.account_id) for target in discovery.targets
    }
    if len(account_namespaces) > 1 and not config.acknowledge_cross_account_copy:
        errors += 1
    recovery_pending = 0
    if os.path.lexists(str(config.state_dir)):
        try:
            recovery_pending = len(pending_recovery_runs(config.state_dir))
        except (JournalError, OSError):
            recovery_pending = 1
        errors += recovery_pending
    watcher_failure = _watcher_failure(config.state_dir)
    errors += watcher_failure
    launch_guard_failure = _launch_guard_failure(config)
    errors += launch_guard_failure
    return {
        "bytes": 0,
        "counts": {
            "checks": 6 + len(config.profiles),
            "errors": errors,
            "invalid_replicas": len(discovery.invalid_replicas),
            "launch_guards": launch_guard_failure,
            "profiles": len(config.profiles),
            "recovery_pending": recovery_pending,
            "targets": len(discovery.targets),
            "unapproved_targets": unapproved,
            "watcher_failures": watcher_failure,
        },
        "duration_ms": 0,
        "state": "healthy" if errors == 0 else "unhealthy",
    }


def _approve_current_targets(config_path: Path, config: Config, *, apply: bool) -> dict:
    from .filesystem import atomic_write_bytes, ensure_private_directory
    from .store import SessionStore

    discovery = SessionStore().discover(config)
    if discovery.invalid_replicas or not discovery.targets:
        return {
            "state": "blocked",
            "counts": {
                "approved": len(config.approved_targets),
                "invalid_replicas": len(discovery.invalid_replicas),
                "targets": len(discovery.targets),
            },
        }
    approved = [
        {
            "profile": target.profile_name,
            "account": target.account_id,
            "workspace": target.workspace_id,
        }
        for target in discovery.targets
    ]
    current = {
        (target.profile_name, target.account_id, target.workspace_id)
        for target in config.approved_targets
    }
    discovered = {
        (target.profile_name, target.account_id, target.workspace_id)
        for target in discovery.targets
    }
    if apply:
        document = json.loads(config_path.read_text(encoding="utf-8"))
        document["approved_targets"] = approved
        encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        ensure_private_directory(config_path.parent)
        atomic_write_bytes(config_path, encoded)
        os.chmod(str(config_path), 0o600)
        # Validate the exact durable document before reporting success.
        load_config(config_path)
    return {
        "state": "approved" if apply else "planned",
        "counts": {
            "approved": len(discovered),
            "new": len(discovered - current),
            "removed": len(current - discovered),
            "targets": len(discovered),
        },
    }


def _validate_config_document(document: dict) -> None:
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, raw_path = tempfile.mkstemp(prefix="claude-session-sync-config-")
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
        os.chmod(str(path), 0o600)
        load_config(path)
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _configure(
    config_path: Path,
    *,
    automatic_targets: bool,
    enable_personal: bool,
    apply: bool,
) -> dict:
    from .filesystem import atomic_write_bytes, ensure_private_directory

    load_config(config_path)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    changes = 0

    if automatic_targets:
        desired = {
            "target_policy": "all-configured-profiles",
            "acknowledge_cross_account_copy": True,
        }
        for key, value in desired.items():
            if document.get(key) != value:
                document[key] = value
                changes += 1

    if enable_personal:
        personal = next(
            (
                profile
                for profile in document.get("profiles", [])
                if profile.get("name") == "Personal"
            ),
            None,
        )
        if personal is None:
            raise ValueError("generated Personal profile is missing")
        if personal.get("enabled") is not True:
            personal["enabled"] = True
            changes += 1
        for key in (
            "acknowledge_cross_profile_copy",
            "acknowledge_cross_account_copy",
        ):
            if document.get(key) is not True:
                document[key] = True
                changes += 1

    _validate_config_document(document)
    if apply and changes:
        encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        ensure_private_directory(config_path.parent)
        atomic_write_bytes(config_path, encoded)
        os.chmod(str(config_path), 0o600)
        load_config(config_path)
    return {
        "state": "configured" if apply and changes else "planned" if changes else "noop",
        "counts": {
            "changes": changes,
            "personal_enabled": int(
                any(
                    profile.get("name") == "Personal"
                    and profile.get("enabled") is True
                    for profile in document.get("profiles", [])
                )
            ),
            "automatic_targets": int(
                document.get("target_policy") == "all-configured-profiles"
            ),
        },
    }


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    dependencies: Optional[CliDependencies] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    """Run one command and return an exit status without terminating tests."""

    deps = dependencies or CliDependencies()
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    try:
        with contextlib.redirect_stderr(errors):
            arguments = _parser().parse_args(argv)
    except SystemExit as error:
        return int(error.code)

    try:
        if arguments.command in ("install", "uninstall"):
            installer = deps.installer_factory(arguments.config)
            if arguments.command == "install":
                report = installer.install(dry_run=arguments.dry_run)
            else:
                report = installer.uninstall(dry_run=arguments.dry_run)
            _write(
                {
                    "state": report.state,
                    "counts": {
                        "actions": report.change_count,
                        "backups": len(report.backups),
                    },
                    "bytes": 0,
                    "duration_ms": 0,
                },
                as_json=False,
                stream=output,
            )
            return 0
        if arguments.command == "configure":
            payload = _configure(
                arguments.config,
                automatic_targets=arguments.automatic_targets,
                enable_personal=arguments.enable_personal,
                apply=arguments.apply,
            )
            _write(payload, as_json=False, stream=output)
            return 0
        config = deps.config_loader(arguments.config)
        if arguments.command == "plan":
            started = deps.clock()
            plan = deps.planner_factory(config).plan(SyncRequest(config))
            duration_ms = round((deps.clock() - started) * 1000)
            _write(
                _plan_summary(plan, duration_ms),
                as_json=arguments.as_json,
                stream=output,
            )
            return 0 if _plan_state(plan) in ("planned", "noop") else 1
        if arguments.command == "sync":
            started = deps.clock()
            plan = deps.planner_factory(config).plan(SyncRequest(config))
            deps.clock()  # Preserve a phase boundary for injected timing adapters.
            if _plan_state(plan).startswith("blocked_"):
                _write(
                    _plan_summary(plan, round((deps.clock() - started) * 1000)),
                    as_json=arguments.as_json,
                    stream=output,
                )
                return 1
            receipt = deps.engine_factory(config).apply(plan)
            duration_ms = round((deps.clock() - started) * 1000)
            _write(
                _receipt_summary(receipt, duration_ms),
                as_json=arguments.as_json,
                stream=output,
            )
            return 0
        if arguments.command == "auto":
            if _running_processes(config, deps):
                _write(
                    {"state": "skipped", "reason": "app-running"},
                    as_json=False,
                    stream=output,
                )
                return 0
            started = deps.clock()
            try:
                plan = deps.planner_factory(config).plan(SyncRequest(config))
                if _plan_state(plan).startswith("blocked_"):
                    _write(
                        _plan_summary(
                            plan,
                            round((deps.clock() - started) * 1000),
                        ),
                        as_json=False,
                        stream=output,
                    )
                    return 1
                receipt = deps.engine_factory(config).apply(plan)
            except Exception as error:
                reason = _busy_reason(error)
                if reason is None:
                    raise
                _write(
                    {"state": "skipped", "reason": reason},
                    as_json=False,
                    stream=output,
                )
                return 0
            _write(
                _receipt_summary(
                    receipt,
                    round((deps.clock() - started) * 1000),
                ),
                as_json=False,
                stream=output,
            )
            return 0
        if arguments.command == "switch":
            return _run_switch(arguments, config, deps, output)
        if arguments.command == "clear-launch-guard":
            payload, exit_code = _clear_launch_guard_command(
                config,
                deps,
                apply=arguments.apply,
            )
            _write(payload, as_json=False, stream=output)
            return exit_code
        if arguments.command == "rollback":
            started = deps.clock()
            receipt = deps.engine_factory(config).rollback(arguments.run_id)
            duration_ms = round((deps.clock() - started) * 1000)
            _write(
                _recovery_summary(receipt, duration_ms),
                as_json=arguments.as_json,
                stream=output,
            )
            return 0
        if arguments.command == "status":
            running = _running_processes(config, deps)
            watcher_failure = _watcher_failure(config.state_dir)
            launch_guard_failure = _launch_guard_failure(config)
            _write(
                {
                    "bytes": 0,
                    "counts": {
                        "launch_guards": launch_guard_failure,
                        "profiles": len(config.profiles),
                        "running_processes": len(running),
                        "watcher_failures": watcher_failure,
                    },
                    "duration_ms": 0,
                    "state": (
                        "app-running"
                        if running
                        else "launch-unconfirmed"
                        if launch_guard_failure
                        else "watcher-failed"
                        if watcher_failure
                        else "idle"
                    ),
                },
                as_json=arguments.as_json,
                stream=output,
            )
            return 0
        if arguments.command == "doctor":
            payload = _doctor_summary(config)
            _write(
                payload,
                as_json=arguments.as_json,
                stream=output,
            )
            return 0 if payload["state"] == "healthy" else 1
        if arguments.command == "approve-current-targets":
            payload = _approve_current_targets(
                arguments.config, config, apply=arguments.apply
            )
            _write(payload, as_json=False, stream=output)
            return 0 if payload["state"] != "blocked" else 1
    except Exception as error:
        errors.write("claude-session-sync: {}\n".format(error))
        return 1
    errors.write("claude-session-sync: unsupported command\n")
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
