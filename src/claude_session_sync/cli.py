"""Command-line boundary for safe session synchronization."""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import subprocess
import stat
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TextIO

from .config import Config, load_config
from . import __version__
from . import strict_json as json
from .adapters import adapter_failure, run_adapters
from .config_commands import _approve_current_targets, _configure, _prepare_config_data
from .health import abandoned_preparation_count, doctor_summary, watcher_failure
from .progress import current_progress, finish_progress, record_progress
from .model import Plan, SyncRequest


ConfigLoader = Callable[[Path], Config]
PlannerFactory = Callable[[Config], Any]
EngineFactory = Callable[[Config], Any]
InstallerFactory = Callable[[Path], Any]
LayoutFactory = Callable[[Config], Any]
RoutineFactory = Callable[[Config], Any]
PROCESS_EXIT_POLL_SECONDS = 0.1
PROCESS_PROBE_TIMEOUT_SECONDS = 5.0
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


def _default_layout_factory(config: Config) -> Any:
    from .layout import LayoutSynchronizer

    return LayoutSynchronizer(config)


def _default_routine_factory(config: Config) -> Any:
    from .routines import RoutineSynchronizer

    return RoutineSynchronizer(config)


@dataclass(frozen=True)
class CliDependencies:
    """Injected system boundaries used by command tests and platform adapters."""

    config_loader: ConfigLoader = load_config
    planner_factory: PlannerFactory = _default_planner_factory
    engine_factory: EngineFactory = _default_engine_factory
    process_probe: Any = None
    launcher: Any = None
    installer_factory: InstallerFactory = _default_installer_factory
    layout_factory: LayoutFactory = _default_layout_factory
    routine_factory: RoutineFactory = _default_routine_factory
    clock: Callable[[], float] = time.monotonic
    monotonic: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    launch_confirmation_timeout: float = LAUNCH_CONFIRMATION_TIMEOUT_SECONDS
    # Live sync needs the built-in planner, which knows the chat state and
    # which folders a running Claude holds. None means: use it when it is built in.
    live_sync: Optional[bool] = None


def _live_sync(deps: "CliDependencies") -> bool:
    if deps.live_sync is not None:
        return deps.live_sync
    return deps.planner_factory is _default_planner_factory


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
    parser.add_argument("--version", action="version", version=__version__)
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
    plan.add_argument(
        "--report",
        action="store_true",
        help="write every planned action to plan-report.json in the private state folder",
    )
    sync = commands.add_parser("sync", help="apply the next synchronization")
    sync.add_argument("--json", action="store_true", dest="as_json")
    sync.add_argument(
        "--prefer",
        metavar="FOLDER",
        help="settle tied chats in favour of one sidebar folder (ACCOUNT/WORKSPACE)",
    )
    sync.add_argument("--session", metavar="ID", help="limit --prefer to one chat")
    sidebar_mode = sync.add_mutually_exclusive_group()
    sidebar_mode.add_argument(
        "--prefer-current-sidebar", action="store_true",
        help="resolve conflicting chat folders using the current sidebar (one profile only)",
    )
    sidebar_mode.add_argument(
        "--adopt-current-sidebar",
        action="store_true",
        help="bootstrap every approved scope from the current sidebar (single default profile only)",
    )
    sidebar_mode.add_argument(
        "--adopt-source-scope",
        metavar="ACCOUNT/WORKSPACE",
        help="one-time restore from a verified inactive sidebar scope",
    )
    auto = commands.add_parser("auto", help="sync after Claude terminates")
    auto.add_argument("--json", action="store_true", dest="as_json")
    switch = commands.add_parser("switch", help="sync and launch a profile")
    switch.add_argument("profile")
    switch.add_argument("--json", action="store_true", dest="as_json")
    restart_check = commands.add_parser("restart-check", help="inspect which default-profile processes may be restarted")
    restart_check.add_argument("profile")
    switch.add_argument("--no-launch", action="store_true")
    switch_layout = switch.add_mutually_exclusive_group()
    switch_layout.add_argument(
        "--after-account-switch",
        action="store_true",
        help="copy pins and groups from the account just left into the new one",
    )
    switch_layout.add_argument(
        "--adopt-current-sidebar",
        action="store_true",
        dest="adopt_current_sidebar",
        help="make the signed-in account's pins and groups the source of truth",
    )
    switch.add_argument(
        "--wait-for-exit",
        type=_nonnegative_seconds,
        default=0.0,
        metavar="SECONDS",
        help="wait up to SECONDS for a managed Claude process to exit",
    )
    seed = commands.add_parser(
        "seed-state",
        help="start from the last synced version held in folders outside sync",
    )
    seed.add_argument("--from-unenrolled", action="store_true", required=True)
    seed_mode = seed.add_mutually_exclusive_group(required=True)
    seed_mode.add_argument("--dry-run", action="store_true")
    seed_mode.add_argument("--apply", action="store_true")
    restart_claude = commands.add_parser(
        "restart-claude",
        help="ask the Sync helper to quit, sync, and reopen Claude once",
    )
    restart_claude.add_argument(
        "--adopt-current-sidebar",
        action="store_true",
        dest="adopt_current_sidebar",
        help="while Claude is closed, make the signed-in account's pins and groups the source of truth",
    )
    keep_sidebar = commands.add_parser(
        "keep-sidebar",
        help="make the signed-in account's pins and groups the source of truth next time Claude closes",
    )
    keep_mode = keep_sidebar.add_mutually_exclusive_group(required=True)
    keep_mode.add_argument("--dry-run", action="store_true")
    keep_mode.add_argument("--apply", action="store_true")
    forget = commands.add_parser(
        "forget-lost",
        help="let the next sync put back one chat reported as lost",
    )
    forget.add_argument("session_id")
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
    profile_mode = configure.add_mutually_exclusive_group()
    profile_mode.add_argument(
        "--enable-personal",
        action="store_true",
        help="enable the generated Personal profile",
    )
    profile_mode.add_argument(
        "--disable-personal",
        action="store_true",
        help="disable the generated Personal profile",
    )
    configure.add_argument(
        "--sync-layout",
        action="store_true",
        help="sync pins and custom groups after Claude quits",
    )
    configure.add_argument(
        "--sync-routines",
        action="store_true",
        help="sync Claude Code routines after Claude quits",
    )
    configure_mode = configure.add_mutually_exclusive_group(required=True)
    configure_mode.add_argument("--dry-run", action="store_true")
    configure_mode.add_argument("--apply", action="store_true")
    setup = commands.add_parser(
        "setup", help="configure and install macOS adapters atomically"
    )
    setup.add_argument("--automatic-targets", action="store_true")
    setup_profile = setup.add_mutually_exclusive_group()
    setup_profile.add_argument("--enable-personal", action="store_true")
    setup_profile.add_argument("--disable-personal", action="store_true")
    setup.add_argument("--sync-layout", action="store_true")
    setup.add_argument("--sync-routines", action="store_true")
    setup_mode = setup.add_mutually_exclusive_group(required=True)
    setup_mode.add_argument("--dry-run", action="store_true")
    setup_mode.add_argument("--apply", action="store_true")
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


def _switch_chat_result(
    config: Config, dependencies: CliDependencies, started: float
) -> dict:
    if _live_sync(dependencies):
        writer_deadline = dependencies.monotonic() + SWITCH_WRITER_WAIT_SECONDS
        while True:
            try:
                return _live_chat_payload(None, config, dependencies)
            except Exception as error:
                remaining = writer_deadline - dependencies.monotonic()
                if _busy_reason(error) == "busy" and remaining > 0:
                    dependencies.sleeper(min(PROCESS_EXIT_POLL_SECONDS, remaining))
                    continue
                return _chat_failure(error)
    try:
        planner = dependencies.planner_factory(config)
        engine = dependencies.engine_factory(config)
        plan = planner.plan(SyncRequest(config))
        dependencies.clock()
        writer_deadline = dependencies.monotonic() + SWITCH_WRITER_WAIT_SECONDS
        revalidation_retries = SWITCH_REVALIDATION_RETRIES
        while True:
            if _plan_state(plan).startswith("blocked_"):
                return _plan_summary(
                    plan, round((dependencies.clock() - started) * 1000)
                )
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
        return _receipt_summary(receipt, duration_ms)
    except Exception as error:
        return _chat_failure(error)


def _run_switch(
    arguments: Any,
    config: Config,
    dependencies: CliDependencies,
    output: TextIO,
) -> int:
    from .locking import ExclusiveFileLock, LockUnavailableError

    as_json = arguments.as_json
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
            as_json=as_json,
            stream=output,
        )
        return 1

    try:
        if not _reconcile_launch_guard(config, dependencies):
            _write(
                {"state": "blocked_switch", "reason": "launch-unconfirmed"},
                as_json=as_json,
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
                as_json=as_json,
                stream=output,
            )
            return 1
        record_progress(config, "syncing")
        started = dependencies.clock()
        payload = _switch_chat_result(config, dependencies, started)
        payload.update(
            run_adapters(
                config,
                dependencies,
                lambda: bool(_running_processes(config, dependencies)),
                adopt_current_sidebar=getattr(arguments, "adopt_current_sidebar", False),
                after_account_switch=getattr(arguments, "after_account_switch", False),
            )
        )
        payload["progress"] = finish_progress(config, payload)
        if payload["progress"] == "needs-attention":
            _write(payload, as_json=as_json, stream=output)
            return 1
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
                        as_json=as_json,
                        stream=output,
                    )
                    return 1
                _clear_launch_guard(config)
        _write(
            payload,
            as_json=as_json,
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


def _chat_failure(error: Exception) -> dict:
    """Map chat failures to an allowlisted, content-free operator action."""

    from .journal import JournalError
    from .transaction import RecoveryPendingError

    if isinstance(error, RecoveryPendingError):
        return {
            "state": "failed",
            "reason": "recovery-pending",
            "next_action": "run-doctor",
            "error_type": "recovery-pending-error",
        }
    if isinstance(error, JournalError):
        return {
            "state": "failed",
            "reason": "invalid-journal",
            "next_action": "run-doctor",
            "error_type": "journal-error",
        }
    if isinstance(error, subprocess.TimeoutExpired):
        return {
            "state": "failed",
            "reason": "process-inspection-timeout",
            "next_action": "retry-sync",
            "error_type": "process-timeout",
        }
    return {
        "state": "failed",
        "reason": _busy_reason(error) or "chat-sync-error",
        "next_action": "run-doctor",
        "error_type": "chat-sync-error",
    }


def _keep_sidebar(config: Config, *, apply: bool) -> tuple:
    """Choose the signed-in account's synced folder as the pins-and-groups source."""

    from .enrollment import selected_targets
    from .layout import request_adoption
    from .liveness import last_known_account
    from .store import SessionStore

    if not config.sync_sidebar_layout:
        return {"state": "blocked", "reason": "sidebar-sync-off"}, 1
    defaults = [profile for profile in config.profiles if profile.is_default]
    if len(defaults) != 1:
        return {"state": "blocked", "reason": "needs-one-default-profile"}, 1
    account = last_known_account(defaults[0].data_root)
    folders = [
        target
        for target in selected_targets(config, SessionStore().discover_targets(config).targets)
        if target.profile_name == defaults[0].name and target.account_id.lower() == account
    ]
    if account is None or len(folders) != 1:
        return {"state": "blocked", "reason": "signed-in-account-not-synced"}, 1
    if apply:
        request_adoption(
            config.state_dir, "{}/{}".format(folders[0].account_id, folders[0].workspace_id)
        )
    return {"state": "pending" if apply else "planned", "next_action": "restart-claude"}, 0


def _chat_run_summary(run: Any, duration_ms: int) -> dict:
    plan = run.plan
    if plan.invalid_replicas or run.receipt is None:
        return _chat_plan_summary(run, duration_ms)
    receipt = run.receipt
    counts = {
        "operations": receipt.operation_count,
        "planned": len(plan.operations),
        "skipped": receipt.skipped_count,
    }
    counts.update(_problem_counts(run))
    payload = {
        "bytes": receipt.bytes_copied,
        "counts": counts,
        "duration_ms": duration_ms,
        "plan_id": receipt.plan_id,
        "run_id": receipt.run_id,
        "state": receipt.status,
    }
    if run.restart_suggested:
        payload["restart_suggested"] = run.restart_suggested
    if any(run.problems.get(kind) for kind in ("tied", "lost", "unreadable", "future")):
        payload["next_action"] = "run-plan-report"
    return payload


def _chat_plan_summary(run: Any, duration_ms: int) -> dict:
    plan = run.plan
    payload = _plan_summary(plan, duration_ms)
    counts = payload["counts"]
    for kind in ("create", "replace", "retire"):
        counts[kind + "s"] = sum(1 for operation in plan.operations if operation.kind == kind)
    counts.update(_problem_counts(run))
    if run.restart_suggested:
        payload["restart_suggested"] = run.restart_suggested
    return payload


def _problem_counts(run: Any) -> dict:
    counts = {
        kind: run.problems[kind]
        for kind in ("live", "tied", "lost", "unreadable", "future")
        if run.problems.get(kind)
    }
    if run.plan.ignored_targets:
        counts["ignored_folders"] = run.plan.ignored_targets
    if run.newly_enrolled:
        counts["new_folders"] = run.newly_enrolled
    return counts


def _folder_label(path: Path) -> str:
    return "{}/{}".format(path.parent.name[:8], path.name[:8])


def _write_plan_report(config: Config, run: Any) -> None:
    """Every planned action and every chat left alone, for a human to review.

    Session ids and short folder labels only: no titles, paths or contents.
    """

    from datetime import datetime, timezone

    from .adapters import save_status

    plan = run.plan
    context = plan.context
    labels = {}
    if context is not None:
        labels = {key: _folder_label(target.path) for key, target in context.targets.items()}
    save_status(
        config.state_dir,
        "plan-report.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actions": [
                {
                    "kind": operation.kind,
                    "artifact": operation.artifact,
                    "session": operation.session_id,
                    "from": _folder_label(Path(operation.source).parent),
                    "to": _folder_label(Path(operation.destination).parent),
                }
                for operation in plan.operations
            ],
            "left_alone": [
                {
                    "kind": problem.kind,
                    "session": problem.session_id,
                    "folder": labels.get(problem.partition, "?"),
                }
                for problem in plan.problems
            ],
            "live_folders": sorted(labels.get(key, "?") for key in plan.live_targets),
            "ignored_folders": plan.ignored_targets,
        },
    )


def _live_chat_payload(arguments: Any, config: Config, deps: CliDependencies) -> dict:
    from .chat_sync import ChatStateError, run_chat_sync

    started = deps.clock()
    try:
        chat_run = run_chat_sync(
            config,
            deps.planner_factory(config),
            deps.engine_factory(config),
            prefer=getattr(arguments, "prefer", None),
            prefer_session=getattr(arguments, "session", None),
            running_processes=lambda loaded: _running_processes(loaded, deps),
        )
    except ChatStateError:
        return {
            "state": "failed",
            "reason": "state-unusable",
            "next_action": "run-doctor",
            "error_type": "chat-state-error",
        }
    return _chat_run_summary(chat_run, round((deps.clock() - started) * 1000))


def _run_sync(arguments, config: Config, deps: CliDependencies, output: TextIO) -> int:
    from .locking import ExclusiveFileLock, LockUnavailableError

    live = _live_sync(deps)
    if not live and getattr(arguments, "prefer", None) is not None:
        raise ValueError("--prefer needs the built-in planner")
    handoff = ExclusiveFileLock(
        config.state_dir / "switch-handoff.lock", mode="auto", timeout=0
    )
    try:
        handoff.acquire()
    except LockUnavailableError:
        _write(
            {"state": "skipped", "reason": "busy"},
            as_json=arguments.as_json,
            stream=output,
        )
        return 0 if arguments.command == "auto" else 1
    try:
        try:
            running = _running_processes(config, deps)
        except PermissionError:
            _write(
                {
                    "state": "failed",
                    "reason": "process-inspection-unavailable",
                    "next_action": "allow-process-inspection-and-retry",
                    "error_type": "process-permission-error",
                },
                as_json=arguments.as_json,
                stream=output,
            )
            return 1
        except subprocess.TimeoutExpired as error:
            _write(_chat_failure(error), as_json=arguments.as_json, stream=output)
            return 1
        if running and not live:
            record_progress(config, "waiting-for-Claude")
            _write(
                {
                    "state": "skipped",
                    "reason": "app-running",
                    "progress": "waiting-for-Claude",
                },
                as_json=arguments.as_json,
                stream=output,
            )
            return 0 if arguments.command == "auto" else 1
        record_progress(config, "syncing")
        started = deps.clock()
        try:
            if live:
                payload = _live_chat_payload(arguments, config, deps)
            else:
                plan = deps.planner_factory(config).plan(SyncRequest(config))
                if arguments.command == "sync":
                    deps.clock()
                if _plan_state(plan).startswith("blocked_"):
                    payload = _plan_summary(plan, round((deps.clock() - started) * 1000))
                else:
                    receipt = deps.engine_factory(config).apply(plan)
                    payload = _receipt_summary(
                        receipt, round((deps.clock() - started) * 1000)
                    )
        except Exception as error:
            reason = _busy_reason(error)
            if reason is not None:
                state = (
                    "waiting-for-Claude"
                    if reason == "app-running"
                    else "waiting-for-sync"
                )
                record_progress(config, state)
                _write(
                    {"state": "skipped", "reason": reason, "progress": state},
                    as_json=arguments.as_json,
                    stream=output,
                )
                return 0 if arguments.command == "auto" else 1
            payload = _chat_failure(error)
        payload.update(
            run_adapters(
                config, deps, lambda: bool(_running_processes(config, deps)),
                prefer_current_sidebar=getattr(arguments, "prefer_current_sidebar", False),
                adopt_current_sidebar=getattr(arguments, "adopt_current_sidebar", False),
                adopt_source_scope=getattr(arguments, "adopt_source_scope", None),
            )
        )
        payload["progress"] = finish_progress(config, payload)
        _write(payload, as_json=arguments.as_json, stream=output)
        return 0 if payload["progress"] == "finished" else 1
    except Exception:
        record_progress(
            config, "needs-attention", reason="sync-error", next_action="run-doctor"
        )
        raise
    finally:
        handoff.release()


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
        if arguments.command == "setup":
            installer = deps.installer_factory(arguments.config)
            if arguments.config.exists():
                source = arguments.config.read_bytes()
            else:
                source = installer.default_config_data()
            desired, config_payload = _prepare_config_data(
                source,
                automatic_targets=arguments.automatic_targets,
                enable_personal=arguments.enable_personal,
                disable_personal=arguments.disable_personal,
                sync_layout=arguments.sync_layout,
                sync_routines=arguments.sync_routines,
            )
            report = installer.setup(
                dry_run=arguments.dry_run,
                config_data=desired,
            )
            _write(
                {
                    "state": report.state,
                    "counts": {
                        "actions": report.change_count,
                        "backups": len(report.backups),
                        **config_payload["counts"],
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
                disable_personal=arguments.disable_personal,
                sync_layout=arguments.sync_layout,
                sync_routines=arguments.sync_routines,
                apply=arguments.apply,
            )
            _write(payload, as_json=False, stream=output)
            return 0
        config = deps.config_loader(arguments.config)
        if getattr(arguments, "prefer_current_sidebar", False) and (
            not config.sync_sidebar_layout or len(config.profiles) != 1
        ):
            raise ValueError("--prefer-current-sidebar requires sidebar sync and exactly one profile")
        if getattr(arguments, "adopt_current_sidebar", False) and (
            not config.sync_sidebar_layout
            or len(config.profiles) != 1
            or not config.profiles[0].is_default
        ):
            raise ValueError(
                "--adopt-current-sidebar requires sidebar sync and exactly one default profile"
            )
        if getattr(arguments, "adopt_source_scope", None) is not None and (
            not config.sync_sidebar_layout
            or len(config.profiles) != 1
            or not config.profiles[0].is_default
        ):
            raise ValueError(
                "--adopt-source-scope requires sidebar sync and exactly one default profile"
            )
        if arguments.command == "restart-check":
            profile = _find_profile(config, arguments.profile)
            if not profile.is_default or config.target_policy not in (
                "all-configured-profiles",
                "logins",
            ):
                raise ValueError("automatic restart is enabled only for the default profile in automatic mode")
            try:
                processes = _running_processes(config, deps)
            except subprocess.TimeoutExpired as error:
                _write(_chat_failure(error), as_json=True, stream=output)
                return 1
            selected_root = _normalized_path(profile.data_root)
            if any(_normalized_path(process.user_data_dir) != selected_root for process in processes):
                raise ValueError("another managed profile is open; leave Claude open until it is closed")
            _write({"state": "ready", "pids": [process.pid for process in processes]}, as_json=True, stream=output)
            return 0
        if arguments.command == "plan":
            started = deps.clock()
            if _live_sync(deps):
                from .chat_sync import plan_chat_sync

                chat_run = plan_chat_sync(
                    config,
                    deps.planner_factory(config),
                    running_processes=lambda loaded: _running_processes(loaded, deps),
                )
                plan = chat_run.plan
                if arguments.report:
                    _write_plan_report(config, chat_run)
                payload = _chat_plan_summary(
                    chat_run, round((deps.clock() - started) * 1000)
                )
            else:
                plan = deps.planner_factory(config).plan(SyncRequest(config))
                payload = _plan_summary(plan, round((deps.clock() - started) * 1000))
            _write(payload, as_json=arguments.as_json, stream=output)
            return 0 if _plan_state(plan) in ("planned", "noop") else 1
        if arguments.command == "seed-state":
            from .seeding import seed_from_unenrolled

            _write(
                seed_from_unenrolled(config, apply=arguments.apply),
                as_json=False,
                stream=output,
            )
            return 0
        if arguments.command == "restart-claude":
            from .filesystem import atomic_write_bytes, ensure_private_directory

            ensure_private_directory(config.state_dir)
            request = config.state_dir / "restart-request"
            atomic_write_bytes(
                request,
                b"adopt-current-sidebar\n" if arguments.adopt_current_sidebar else b"restart\n",
            )
            os.chmod(str(request), 0o600)
            _write({"state": "requested"}, as_json=False, stream=output)
            return 0
        if arguments.command == "keep-sidebar":
            payload, exit_code = _keep_sidebar(config, apply=arguments.apply)
            _write(payload, as_json=False, stream=output)
            return exit_code
        if arguments.command == "forget-lost":
            from .chat_sync import forget_seen

            folders = forget_seen(config, arguments.session_id)
            _write(
                {"state": "forgotten" if folders else "noop", "counts": {"folders": folders}},
                as_json=False,
                stream=output,
            )
            return 0
        if arguments.command in ("sync", "auto"):
            return _run_sync(arguments, config, deps, output)
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
            watcher_error = watcher_failure(config.state_dir)
            launch_guard_failure = _launch_guard_failure(config)
            layout_failure = adapter_failure(config, "layout")
            routine_failure = adapter_failure(config, "routines")
            abandoned = abandoned_preparation_count(config.state_dir)
            progress = current_progress(
                config,
                app_running=bool(running),
                live=_live_sync(deps),
                failures=watcher_error
                + launch_guard_failure
                + layout_failure
                + routine_failure
                + abandoned,
            )
            if abandoned:
                progress["next_action"] = "inspect-preparations"
            _write(
                {
                    "bytes": 0,
                    "counts": {
                        "abandoned_preparations": abandoned,
                        "launch_guards": launch_guard_failure,
                        "layout_failures": layout_failure,
                        "profiles": len(config.profiles),
                        "routine_failures": routine_failure,
                        "running_processes": len(running),
                        "watcher_failures": watcher_error,
                    },
                    "duration_ms": 0,
                    **progress,
                    "state": (
                        "app-running"
                        if running
                        else "launch-unconfirmed"
                        if launch_guard_failure
                        else "layout-failed"
                        if layout_failure
                        else "routines-failed"
                        if routine_failure
                        else "watcher-failed"
                        if watcher_error
                        else "idle"
                    ),
                },
                as_json=arguments.as_json,
                stream=output,
            )
            return 0
        if arguments.command == "doctor":
            payload = doctor_summary(
                config,
                deps,
                lambda: bool(_running_processes(config, deps)),
                _launch_guard_failure(config),
            )
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
