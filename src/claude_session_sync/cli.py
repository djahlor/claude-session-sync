"""Command-line boundary for safe session synchronization."""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, TextIO

from . import __version__
from . import strict_json as json
from .adapters import adapter_failure, run_adapters, run_layout_first
from .config import Config, load_config
from .config_commands import (
    approve_current_targets,
    configure_file,
    prepare_config_data,
    validate_config_data,
)
from .filesystem import normalized_path
from .health import abandoned_preparation_count, doctor_summary, watcher_failure
from .model import Plan
from .processes import PROCESS_PROBE_TIMEOUT_SECONDS
from .progress import (
    current_progress,
    finish_progress,
    record_progress,
    unfinished_steps,
)

ConfigLoader = Callable[[Path], Config]
PlannerFactory = Callable[[Config], Any]
EngineFactory = Callable[[Config], Any]
InstallerFactory = Callable[[Path], Any]
LayoutFactory = Callable[[Config], Any]
RoutineFactory = Callable[[Config], Any]
PROCESS_EXIT_POLL_SECONDS = 0.1
SWITCH_WRITER_WAIT_SECONDS = 15.0
# A picture scan that may hold up a sync stops after this long and keeps how
# far it read. A scan from the start read 10.9 GB on 04-11-2026.
PICTURES_SCAN_SECONDS = 30.0
# While this file exists the Sync helper quits a Claude opened mid-switch.
SWITCH_GUARD_NAME = "switch-guard"
SWITCH_REOPEN_WAIT_SECONDS = 45.0
SWITCH_REOPEN_RETRIES = 2


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


def _default_live_engine_factory(
    config: Config, holds: Callable[[], bool], signed_in_again: Callable[[], bool]
) -> Any:
    """The chat engine for a sync while Claude is open and signed out.

    holds runs before journaling and before the first write; the cheaper
    signed_in_again runs before every write.
    """

    from .transaction import TransactionEngine

    return TransactionEngine(
        config.state_dir,
        process_probe=holds,
        retention=config.retention,
        write_guard=signed_in_again,
    )


def _default_live_routine_factory(
    config: Config, holds: Callable[[], bool], read_only: frozenset[str]
) -> Any:
    from .routines import RoutineSynchronizer

    return RoutineSynchronizer(
        config,
        process_probe=lambda *_args, **_kwargs: holds(),
        read_only_accounts=read_only,
    )


def _default_installer_factory(config_path: Path) -> Any:
    from .installer import Installer, InstallLayout

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


def _default_picture_factory(config: Config) -> Any:
    from .pictures import PictureFixer

    return PictureFixer(config)


def _fix_chat_pictures(
    config: Config,
    deps: CliDependencies,
    payload: dict,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Point sent pictures at their copy on this Mac. Reported only when it acts or fails.

    It runs on the restart after an account switch, and at a sign-out while
    Claude stays signed out, so it reaches the pictures sent before the
    switch: those were uploaded to another account. Pictures sent later keep
    their upload, which Claude's phone and web views need, until the next
    switch. It never blocks the chat sync: a failure is reported and the sync
    goes on. should_stop ends the scan early; the next one goes on from there.
    """
    if not config.fix_chat_pictures:
        return
    from .adapters import save_status

    try:
        fixer = deps.picture_factory(config)
        receipt = (
            fixer.sync() if should_stop is None else fixer.sync(should_stop=should_stop)
        )
        result = {
            "state": receipt.state,
            "files": receipt.file_count,
            "pictures": receipt.picture_count,
        }
    except Exception as error:
        result = {"state": "skipped", "reason": "pictures-error", "detail": str(error)}
    try:
        save_status(config.state_dir, "chat-pictures-status.json", result)
    except OSError:
        result["reporting_error"] = "status-write-failed"
    if result["state"] != "noop":
        payload["pictures"] = result


def _resume_chat_pictures(config: Config, deps: CliDependencies, payload: dict) -> None:
    """Go on with a picture scan a time limit or a sign-in stopped.

    A stopped scan is no reason to restart Claude, so the next sync picks it
    up, within the same time limit, where it ended.
    """

    from .adapters import read_status

    if not config.fix_chat_pictures:
        return
    if read_status(config.state_dir, "chat-pictures-status.json").get("state") != (
        "stopped"
    ):
        return
    deadline = deps.monotonic() + PICTURES_SCAN_SECONDS
    _fix_chat_pictures(
        config, deps, payload, should_stop=lambda: deps.monotonic() >= deadline
    )


def _default_artifact_warnings(config: Config, running: Callable[[], bool]) -> dict:
    from .artifact_warnings import run_artifact_warnings

    return run_artifact_warnings(config, running)


def _stop_artifact_warnings(config: Config, deps: CliDependencies) -> dict:
    """Claude is closed: switch off automatic artifact comment replies and clear
    the saved watches behind the "didn't resume" warnings. Never raises.

    It runs right after Claude quits, before the chat sync, so a Claude opened
    again during a long chat sync still starts with the switch set.
    """
    try:
        return deps.artifact_warnings(
            config, lambda: bool(_running_processes(config, deps))
        )
    except Exception as error:
        return {"state": "skipped", "reason": type(error).__name__}


def _report_artifact_warnings(result: dict, payload: dict) -> None:
    """Reported only when it acts or fails."""
    if result and result.get("state") != "noop":
        payload["artifact_warnings"] = result


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
    picture_factory: Callable[[Config], Any] = _default_picture_factory
    live_engine_factory: Callable[..., Any] = _default_live_engine_factory
    live_routine_factory: Callable[..., Any] = _default_live_routine_factory
    artifact_warnings: Callable[[Config, Callable[[], bool]], dict] = (
        _default_artifact_warnings
    )
    clock: Callable[[], float] = time.monotonic
    monotonic: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep


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


def _row_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a row number") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a row number")
    return number


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
    restart_check = commands.add_parser(
        "restart-check", help="inspect which default-profile processes may be restarted"
    )
    restart_check.add_argument("profile")
    signed_out = commands.add_parser(
        "signed-out-sync",
        help="sync while Claude is open and signed out, before the next sign-in",
    )
    signed_out.add_argument("profile")
    signed_out.add_argument("--json", action="store_true", dest="as_json")
    signed_in_groups = commands.add_parser(
        "signed-in-groups",
        help="after a sign-in with Claude open, check whether groups still need a restart",
    )
    signed_in_groups.add_argument("profile")
    signed_in_groups.add_argument("--json", action="store_true", dest="as_json")
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
        help="make one account's pins and groups the source of truth next time Claude closes",
    )
    keep_sidebar.add_argument(
        "--account",
        type=_row_number,
        metavar="N",
        help="the row number --dry-run shows; the signed-in account by default",
    )
    keep_mode = keep_sidebar.add_mutually_exclusive_group(required=True)
    keep_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="list the accounts and the one --apply keeps",
    )
    keep_mode.add_argument("--apply", action="store_true")
    forget = commands.add_parser(
        "forget-lost",
        help="let the next sync put back one chat reported as lost",
    )
    forget.add_argument("session_id")
    rollback = commands.add_parser("rollback", help="restore an earlier run")
    rollback.add_argument("run_id")
    rollback.add_argument("--json", action="store_true", dest="as_json")
    browser_logins = commands.add_parser(
        "browser-logins",
        help="save or restore the built-in browser's site logins",
    )
    browser_logins.add_argument("action", choices=("snapshot", "restore", "status"))
    browser_logins.add_argument("--json", action="store_true", dest="as_json")
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
        help="configure automatic targets and which stores sync",
    )
    configure.add_argument(
        "--automatic-targets",
        action="store_true",
        help="trust future account/workspace targets inside configured profiles",
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
    configure.add_argument(
        "--sync-browser-logins",
        action="store_true",
        help="keep the built-in browser's site logins across account switches",
    )
    configure.add_argument(
        "--stop-artifact-warnings",
        action="store_true",
        help="turn off automatic artifact comment replies and their 'didn't resume' warnings",
    )
    configure_mode = configure.add_mutually_exclusive_group(required=True)
    configure_mode.add_argument("--dry-run", action="store_true")
    configure_mode.add_argument("--apply", action="store_true")
    setup = commands.add_parser(
        "setup", help="configure and install macOS adapters atomically"
    )
    setup.add_argument("--automatic-targets", action="store_true")
    setup.add_argument("--sync-layout", action="store_true")
    setup.add_argument("--sync-routines", action="store_true")
    setup.add_argument("--sync-browser-logins", action="store_true")
    warnings_answer = setup.add_mutually_exclusive_group()
    warnings_answer.add_argument("--stop-artifact-warnings", action="store_true")
    warnings_answer.add_argument(
        "--no-stop-artifact-warnings",
        action="store_true",
        help="record a no to the artifact warnings question, for an install with no terminal",
    )
    setup.add_argument(
        "--ask-artifact-warnings",
        action="store_true",
        help="on a terminal, ask once whether to stop the artifact 'didn't resume' warnings",
    )
    setup.add_argument(
        "--ask-main-account",
        action="store_true",
        help="on a terminal, ask whose pins and groups the other accounts copy",
    )
    setup_mode = setup.add_mutually_exclusive_group(required=True)
    setup_mode.add_argument("--dry-run", action="store_true")
    setup_mode.add_argument("--apply", action="store_true")
    for name in ("install", "uninstall"):
        installer = commands.add_parser(name, help=f"{name} macOS adapters")
        mode = installer.add_mutually_exclusive_group(required=True)
        mode.add_argument("--dry-run", action="store_true")
        mode.add_argument("--apply", action="store_true")
    return parser


def _plan_state(plan: Plan) -> str:
    if plan.invalid_replicas:
        return "blocked_invalid"
    if not plan.operations:
        return "noop"
    return "planned"


def _plan_summary(plan: Plan, duration_ms: int) -> dict:
    # This is an allowlist by design. Domain identifiers and source paths must
    # never leak into machine-readable CLI output.
    payload = {
        "bytes": plan.total_bytes,
        "counts": {
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
    return payload


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
    fields = [f"{key}={value}" for key, value in payload.items()]
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


def _settle_browser_logins(
    config: Config, dependencies: CliDependencies, profiles
) -> dict:
    """Claude is closed: put back logins a switch cleared, or save them. Never raises."""

    from .browser_logins import run_browser_logins

    try:
        return run_browser_logins(
            config,
            lambda: bool(_running_processes(config, dependencies)),
            "settle",
            profiles=profiles,
        )
    except Exception as error:
        return {"state": "failed", "reason": type(error).__name__}


def _browser_logins_busy(result: dict) -> bool:
    """True when a settle met another writer and gets one more try."""

    if result.get("state") == "failed":
        return True
    profiles = result.get("profiles", {})
    return any(entry.get("reason") == "busy" for entry in profiles.values())


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


def _switch_chat_result(config: Config, dependencies: CliDependencies) -> dict:
    writer_deadline = dependencies.monotonic() + SWITCH_WRITER_WAIT_SECONDS
    reopened = 0
    while True:
        try:
            return _chat_payload(None, config, dependencies)
        except Exception as error:
            remaining = writer_deadline - dependencies.monotonic()
            reason = _busy_reason(error)
            if reason == "busy" and remaining > 0:
                dependencies.sleeper(min(PROCESS_EXIT_POLL_SECONDS, remaining))
                continue
            if reason == "claude-open" and reopened < SWITCH_REOPEN_RETRIES:
                # Claude was opened during the sync. The Sync helper asks it
                # to quit again; once it is gone the sync runs once more.
                reopened += 1
                if _wait_for_managed_processes_to_exit(
                    config, dependencies, SWITCH_REOPEN_WAIT_SECONDS
                ):
                    continue
            return _chat_failure(error)


def _set_switch_guard(config: Config, on: bool) -> None:
    from .filesystem import atomic_write_bytes, ensure_private_directory

    guard = config.state_dir / SWITCH_GUARD_NAME
    try:
        if on:
            ensure_private_directory(config.state_dir)
            atomic_write_bytes(guard, b"switch\n")
            os.chmod(str(guard), 0o600)
        elif os.path.lexists(str(guard)):
            os.unlink(str(guard))
    except OSError:
        pass  # without the guard a reopened Claude only stops this sync, as before


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
        _set_switch_guard(config, True)
        # The account change just cleared the built-in browser. Its logins go
        # back first, in the second after Claude quits, because a jar is only
        # put back while Claude is closed and Claude can be opened again during
        # the long chat sync. Claude then starts with the new account already
        # known and does not clear it again.
        browser_logins = _settle_browser_logins(config, dependencies, (profile,))
        artifact_warnings = _stop_artifact_warnings(config, dependencies)
        try:

            def running() -> bool:
                return bool(_running_processes(config, dependencies))

            sidebar_options = {
                "adopt_current_sidebar": getattr(
                    arguments, "adopt_current_sidebar", False
                ),
                "after_account_switch": getattr(
                    arguments, "after_account_switch", False
                ),
            }
            # Claude can be opened again while a long chat sync runs. The
            # sidebar copy goes first when it can, so that never skips it.
            layout_first = run_layout_first(
                config, dependencies, running, **sidebar_options
            )
            payload = _switch_chat_result(config, dependencies)
            payload.update(
                run_adapters(
                    config,
                    dependencies,
                    running,
                    names=("routines",) if layout_first else None,
                    **sidebar_options,
                )
            )
            payload.update(layout_first)
            _report_artifact_warnings(artifact_warnings, payload)
            payload["progress"] = finish_progress(config, payload)
        finally:
            # Claude was closed for this switch. It reopens even when the sync
            # failed, so a failed sync never leaves Claude closed.
            if _browser_logins_busy(browser_logins):
                browser_logins = _settle_browser_logins(
                    config, dependencies, (profile,)
                )
            _set_switch_guard(config, False)
            launch_failed = not arguments.no_launch and not _launch(
                profile, dependencies
            )
        if launch_failed:
            _write(
                {"state": "launch_failed", "reason": "launch-command-failed"},
                as_json=as_json,
                stream=output,
            )
            return 1
        if not arguments.no_launch:
            payload["launch"] = "started"
        if getattr(arguments, "after_account_switch", False):
            # After Claude reopened: the scan read 10.9 GB on 04-11-2026, and
            # its same-length rewrites in place are safe while Claude appends.
            _fix_chat_pictures(config, dependencies, payload)
        if browser_logins is not None and browser_logins["state"] != "disabled":
            payload["browser_logins"] = browser_logins
        steps = unfinished_steps(payload)
        if steps:
            payload["steps"] = steps
        _write(payload, as_json=as_json, stream=output)
        return 0 if payload["progress"] == "finished" else 1
    finally:
        handoff.release()


def _run_signed_out(
    arguments: Any,
    config: Config,
    dependencies: CliDependencies,
    output: TextIO,
) -> int:
    """Sync chats and routines while Claude is open and signed out.

    Claude reads the next account's folders when it signs in, so a switch
    then needs no restart for them. The account just left stays untouched,
    because Claude still saves its chats. The report says whether the switch
    still needs one restart, and why.

    A sign-in ends it quickly, so the restart that copies what is left can
    start: no picture scan runs after one. Claude closed at the sign-in, as
    when it quits to install an update, holds no folder, so the writes go on.
    """

    from .chat_sync import ChatStateError, run_chat_sync
    from .live import (
        SIGNED_OUT_SETTLE_SECONDS,
        KeepAccountsUnchanged,
        StopWhenClaudeHolds,
        holds_folders,
        restart_reasons,
        signed_in_again,
    )
    from .locking import ExclusiveFileLock, LockUnavailableError
    from .logins import last_known_account, signed_in

    profile = _find_profile(config, arguments.profile)

    def skipped(reason: str) -> int:
        payload = {"state": "skipped", "reason": reason, "restart": True}
        _write(payload, as_json=arguments.as_json, stream=output)
        return 0

    if not config.sync_while_signed_out:
        return skipped("disabled")
    handoff = ExclusiveFileLock(
        config.state_dir / "switch-handoff.lock", mode="auto", timeout=0
    )
    try:
        handoff.acquire()
    except LockUnavailableError:
        return skipped("busy")
    syncing = False
    try:
        processes = _running_processes(config, dependencies)
        if not processes:
            return skipped("claude-closed")
        root = normalized_path(profile.data_root)
        if not profile.is_default or any(
            normalized_path(process.user_data_dir) != root for process in processes
        ):
            # Another profile's Claude holds its own folders.
            return skipped("other-profile-open")
        if signed_in(profile.data_root) is not False:
            return skipped("signed-in")
        departing = last_known_account(profile.data_root)
        if departing is None:
            return skipped("account-unknown")
        dependencies.sleeper(SIGNED_OUT_SETTLE_SECONDS)

        def holds() -> bool:
            return holds_folders(
                _running_processes(config, dependencies), profile.data_root, departing
            )

        def again() -> bool:
            return signed_in_again(profile.data_root, departing)

        read_only = frozenset({departing})
        record_progress(config, "syncing")
        syncing = True
        engine = KeepAccountsUnchanged(
            dependencies.live_engine_factory(
                config,
                holds,
                StopWhenClaudeHolds(again, holds, dependencies.monotonic),
            ),
            read_only,
        )
        started = dependencies.clock()
        chat_run = None
        try:
            chat_run = run_chat_sync(
                config,
                dependencies.planner_factory(config),
                engine,
                read_only_accounts=read_only,
            )
            payload = _chat_run_summary(
                chat_run, round((dependencies.clock() - started) * 1000)
            )
        except ChatStateError:
            payload = {
                "state": "failed",
                "reason": "state-unusable",
                "next_action": "run-doctor",
                "error_type": "chat-state-error",
            }
        except Exception as error:
            payload = _chat_failure(error)
            if payload["reason"] == "claude-open":
                # Signed in before or during the writes: nothing is broken,
                # the switch restart syncs instead.
                payload = {"state": "waiting", "reason": "signed-in"}
        if engine.held_back:
            payload.setdefault("counts", {})["held_back"] = engine.held_back
        if chat_run is not None and chat_run.archive_waiting:
            payload["archive"] = "waiting"
        # Claude reads routines at the next sign-in, so they go before pictures.
        payload.update(
            run_adapters(
                config,
                replace(
                    dependencies,
                    routine_factory=lambda routine_config: (
                        dependencies.live_routine_factory(
                            routine_config, holds, read_only
                        )
                    ),
                ),
                holds,
                names=("routines",),
            )
        )
        if payload.get("state") in ("committed", "noop") and not again():
            # Pictures matter only to a switch with no restart, which then
            # needs every chat synced. A sign-in or a time limit stops the
            # scan; the next one goes on where it ended.
            deadline = dependencies.monotonic() + PICTURES_SCAN_SECONDS
            _fix_chat_pictures(
                config,
                dependencies,
                payload,
                should_stop=lambda: again() or dependencies.monotonic() >= deadline,
            )
        elif config.fix_chat_pictures:
            # The restart after the switch scans them, with Claude closed.
            payload["pictures"] = {
                "state": "deferred",
                "reason": "signed-in" if again() else "chats-not-synced",
            }
        payload["signed_in_during_sync"] = again()
        payload["progress"] = finish_progress(config, payload)
        reasons = restart_reasons(config, payload)
        payload["restart"] = bool(reasons)
        payload["restart_reasons"] = reasons
        _write(payload, as_json=arguments.as_json, stream=output)
        return 0
    except Exception:
        if syncing:
            record_progress(
                config, "needs-attention", reason="sync-error", next_action="run-doctor"
            )
        raise
    finally:
        handoff.release()


def _run_signed_in_groups(
    arguments: Any,
    config: Config,
    dependencies: CliDependencies,
    output: TextIO,
) -> int:
    """Whether a switch still needs its restart for groups, checked after the sign-in.

    Claude loads the new account's groups from Anthropic's servers at sign-in.
    When they already match the main account's, the restart would copy
    nothing, so the new account becomes the main one and Claude stays open.
    Reads only disposable copies of Claude's database. Anything unclear, and a
    sign-in Claude has not settled within the time limit, keeps the restart.
    """

    from .layout import main_account_blocker, read_pending_adoption
    from .layout_records import SAME_GROUPS, LayoutError
    from .live import GROUPS_CONFIRM_SECONDS, wait_for_groups
    from .locking import ExclusiveFileLock, LockUnavailableError
    from .logins import last_known_account, signed_in

    profile = _find_profile(config, arguments.profile)

    def report(reason: str, *, restart: bool = True) -> int:
        state = "restart" if restart else "no-restart"
        payload = {"state": state, "reason": reason, "restart": restart}
        _write(payload, as_json=arguments.as_json, stream=output)
        return 0

    if not config.sync_sidebar_layout:
        # Groups do not sync, so they never need the restart.
        return report("sidebar-sync-off", restart=False)
    blocker = main_account_blocker(config)
    if blocker is not None:
        return report(blocker)
    # A copy of the database can be torn or the helper slow: both keep the restart.
    unreadable = (LayoutError, OSError, subprocess.SubprocessError)
    handoff = ExclusiveFileLock(
        config.state_dir / "switch-handoff.lock", mode="auto", timeout=0
    )
    try:
        handoff.acquire()
    except LockUnavailableError:
        return report("busy")
    try:
        account = last_known_account(profile.data_root)
        if account is None or signed_in(profile.data_root) is False:
            return report("not-signed-in")
        try:
            # A keep-sidebar choice is applied by the switch's copy.
            if read_pending_adoption(config.state_dir) is not None:
                return report("main-account-choice-pending")
        except LayoutError:
            return report("main-account-choice-pending")
        layout = dependencies.layout_factory(config)

        def check() -> tuple[str, str | None, str | None]:
            if not _running_processes(config, dependencies):
                return "claude-closed", None, None
            if (
                last_known_account(profile.data_root) != account
                or signed_in(profile.data_root) is False
            ):
                return "account-changed", None, None
            return layout.groups_after_sign_in(account)

        try:
            found = wait_for_groups(check, dependencies.clock, dependencies.sleeper)
            if found is not None and found[0] == SAME_GROUPS:
                # Chromium writes Local Storage to disk in delayed batches, so
                # a second copy a little later must agree.
                dependencies.sleeper(GROUPS_CONFIRM_SECONDS)
                if check() != found:
                    return report("groups-changed-during-check")
        except unreadable:
            return report("sidebar-unreadable")
        if found is None:
            return report("timed-out")
        verdict, scope, main_scope = found
        if verdict != SAME_GROUPS:
            return report(verdict)
        assert scope is not None and main_scope is not None
        try:
            layout.follow_signed_in_account(scope, main_scope)
        except (LayoutError, OSError):
            return report("main-account-not-saved")
        return report(SAME_GROUPS, restart=False)
    finally:
        handoff.release()


def _launch(profile: Any, dependencies: CliDependencies) -> bool:
    """Start the profile's Claude. False when the launch command failed."""

    from .launcher import Launcher, LaunchError

    try:
        (dependencies.launcher or Launcher()).launch(profile.launch_command)
    except LaunchError:
        return False
    return True


def _find_profile(config: Config, requested_name: str) -> Any:
    for profile in config.profiles:
        if profile.name == requested_name:
            return profile
    raise ValueError(f"unknown profile: {requested_name}")


def _busy_reason(error: Exception) -> str | None:
    from .transaction import AppRunningError, TransactionBusyError

    if isinstance(error, (TransactionBusyError, BlockingIOError, TimeoutError)):
        return "busy"
    if isinstance(error, AppRunningError):
        return "claude-open"
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


def _sidebar_rows(
    config: Config, deps: CliDependencies, helper: Path | None = None
) -> list:
    """The accounts the next sync can copy pins and groups from, signed-in first.

    Callers check main_account_blocker first, so the one profile is the default.
    """

    from .layout import LayoutSynchronizer, scope_sessions

    targets = [
        target
        for target in deps.planner_factory(config).sync_targets(config)
        if target.profile_name == config.profiles[0].name
    ]
    return LayoutSynchronizer(config, helper=helper).accounts(scope_sessions(targets))


def _account_lines(rows: Sequence[Any]) -> list:
    """One line per account. Accounts have no names on disk, so rows that would
    look the same also show a short ID."""

    looks = [(row.is_signed_in, row.group_names, row.pins, row.chats) for row in rows]
    show_id = len(set(looks)) < len(looks)
    lines = []
    for number, row in enumerate(rows, 1):
        columns = [f"{number:>3}", "signed in" if row.is_signed_in else " " * 9]
        if show_id:
            account, _separator, workspace = row.scope.partition("/")
            columns.append(f"{account[:8]}/{workspace[:8]}")
        for count, word in (
            (row.groups, "group"),
            (row.pins, "pin"),
            (row.chats, "chat"),
        ):
            columns.append(
                "{:>4} {}".format(count, word if count == 1 else word + "s").ljust(11)
            )
        columns.append(_group_summary(row.group_names))
        lines.append("  ".join(columns).rstrip())
    return lines


def _group_summary(names: Sequence[str]) -> str:
    """Up to three group names, then how many more."""

    shown = []
    for name in names[:3]:
        printable = "".join(
            character if character.isprintable() else " " for character in name
        )
        shown.append(printable if len(printable) <= 30 else printable[:29] + "\u2026")
    summary = ", ".join(shown)
    if len(names) > 3:
        summary += f" and {len(names) - 3} more"
    return summary


def _keep_sidebar(
    config: Config, deps: CliDependencies, *, number: int | None, apply: bool
) -> tuple:
    """Choose which account's pins and groups the next closed sync copies.

    Returns the payload, the exit code, and the account lines to show.
    """

    from .layout import MAIN_ACCOUNT_BLOCKERS, main_account_blocker, request_adoption

    blocker = main_account_blocker(config)
    if blocker is not None:
        return (
            {"state": "blocked", "reason": blocker},
            1,
            [MAIN_ACCOUNT_BLOCKERS[blocker]],
        )
    rows = _sidebar_rows(config, deps)
    lines = _account_lines(rows)
    if number is None:
        number = next(
            (index for index, row in enumerate(rows, 1) if row.is_signed_in), None
        )
        if number is None:
            return (
                {"state": "blocked", "reason": "signed-in-account-not-synced"},
                1,
                lines,
            )
    if number > len(rows):
        return {"state": "blocked", "reason": "no-such-account"}, 1, lines
    if not rows[number - 1].groups:
        return {"state": "blocked", "reason": "account-has-no-groups"}, 1, lines
    if apply:
        request_adoption(config.state_dir, rows[number - 1].scope)
    payload = {
        "state": "pending" if apply else "planned",
        "account": number,
        "next_action": "restart-claude",
    }
    return payload, 0, lines


def _ask_main_account(
    config: Config,
    deps: CliDependencies,
    helper: Path,
    stdin: TextIO,
    output: TextIO,
) -> None:
    """At install, ask which account's pins and groups the others copy.

    Asks only when both input and output are a terminal, before any account
    was adopted, and when accounts hold different groups. Enter keeps the
    signed-in account. The install never fails here: a problem skips the
    question, and the first sync then waits for a choice instead of guessing.
    """

    from .layout import (
        MAIN_ACCOUNT_BLOCKERS,
        groups_differ,
        load_snapshot,
        main_account_blocker,
        request_adoption,
    )

    if not (stdin.isatty() and output.isatty()) or not config.sync_sidebar_layout:
        return
    blocker = main_account_blocker(config)
    if blocker is not None:
        output.write(
            f"\nSkipped the main-account question: {MAIN_ACCOUNT_BLOCKERS[blocker]}.\n"
        )
        return
    try:
        snapshots = [
            config.state_dir / f"sidebar-layout-{index}.json"
            for index in range(len(config.profiles))
        ]
        if any(load_snapshot(path) is not None for path in snapshots):
            return
        rows = _sidebar_rows(config, deps, helper)
        if not groups_differ(row.group_names for row in rows):
            return
        _discard_typeahead(stdin)
        output.write("\nWhich account is the main one?\n")
        output.write("The other accounts will copy its pins and groups.\n\n")
        output.write("".join(line + "\n" for line in _account_lines(rows)))
        output.write("\n")
        number = _choose_row(rows, stdin, output)
        if number is None:
            output.write(
                "\nNo account chosen. Pins and groups wait until you choose one with "
                "keep-sidebar --account N --apply.\n"
            )
            return
        request_adoption(config.state_dir, rows[number - 1].scope)
    except Exception as error:
        output.write(f"\nSkipped the main-account question: {error}\n")
        return
    output.write(
        f"Account {number} is the main one. The others copy its pins and groups "
        "the next time Claude closes.\n\n"
    )


ARTIFACT_WARNINGS_QUESTION = (
    '\nStop the red "Artifact comment monitor ... didn\'t resume" warnings?\n'
    "Old chats show them after a switch, for pages another account published.\n"
    "The cost: Claude stops answering comments on your pages by itself.\n"
    "You can still ask Claude to read and answer them.\n\n"
    "Type y to stop them, or press Enter to skip: "
)


def _ask_artifact_warnings(source: bytes, stdin: TextIO, output: TextIO) -> bytes:
    """At install, ask once whether to stop the artifact "didn't resume" warnings.

    Asks only when both input and output are a terminal and the config holds
    no answer yet, so an update never asks again. Enter means no, because the
    option has a cost. The answer goes into the config as true or false; the
    end of input leaves the config as it was.
    """

    if not (stdin.isatty() and output.isatty()):
        return source
    document = json.loads(source.decode("utf-8"))
    if not isinstance(document, dict) or "stop_artifact_warnings" in document:
        return source
    _discard_typeahead(stdin)
    output.write(ARTIFACT_WARNINGS_QUESTION)
    output.flush()
    line = stdin.readline()
    if not line:
        output.write("\n")
        return source
    stop = line.strip().lower() in ("y", "yes")
    document["stop_artifact_warnings"] = stop
    output.write(
        "The warnings stop from the next time Claude closes.\n"
        if stop
        else "Skipped. To stop them later: configure --stop-artifact-warnings --apply\n"
    )
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _record_artifact_warnings_no(source: bytes) -> bytes:
    """Save a no given outside a terminal, for example in a Claude Code chat.

    Like Enter at the question, it is saved only while the config holds no
    answer, so it never turns off a fix someone chose before.
    """

    document = json.loads(source.decode("utf-8"))
    if not isinstance(document, dict) or "stop_artifact_warnings" in document:
        return source
    document["stop_artifact_warnings"] = False
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _discard_typeahead(stdin: TextIO) -> None:
    """Drop keys pressed while setup compiled, so they cannot answer the question."""

    import termios

    try:
        termios.tcflush(stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError, termios.error):
        return  # not a real terminal


def _choose_row(rows: Sequence[Any], stdin: TextIO, output: TextIO) -> int | None:
    """Read a row number with groups; Enter means the signed-in row. None at end of input."""

    default = next(
        (index for index, row in enumerate(rows, 1) if row.is_signed_in and row.groups),
        None,
    )
    prompt = (
        "Type a number: "
        if default is None
        else f"Press Enter for {default}, or type a number: "
    )
    while True:
        output.write(prompt)
        output.flush()
        answer = stdin.readline()
        if not answer:
            return None
        answer = answer.strip()
        number = int(answer) if answer.isdecimal() else None
        if not answer:
            number = default
        if number is not None and 1 <= number <= len(rows) and rows[number - 1].groups:
            return number
        output.write("Choose a number from the list that has groups.\n")


def _chat_run_summary(run: Any, duration_ms: int) -> dict:
    plan = run.plan
    if plan.invalid_replicas or run.receipt is None:
        return _chat_plan_summary(run, duration_ms)
    receipt = run.receipt
    counts = {
        "operations": receipt.operation_count,
        "planned": len(plan.operations),
    }
    counts.update(_problem_counts(run))
    if run.recovered_runs:
        counts["recovered_runs"] = run.recovered_runs
    payload = {
        "bytes": receipt.bytes_copied,
        "counts": counts,
        "duration_ms": duration_ms,
        "plan_id": receipt.plan_id,
        "run_id": receipt.run_id,
        "state": receipt.status,
    }
    if any(run.problems.get(kind) for kind in ("tied", "lost", "unreadable", "future")):
        payload["next_action"] = "run-plan-report"
    return payload


def _chat_plan_summary(run: Any, duration_ms: int) -> dict:
    plan = run.plan
    payload = _plan_summary(plan, duration_ms)
    counts = payload["counts"]
    for kind in ("create", "replace", "retire"):
        counts[kind + "s"] = sum(
            1 for operation in plan.operations if operation.kind == kind
        )
    counts.update(_problem_counts(run))
    return payload


def _problem_counts(run: Any) -> dict:
    counts = {
        kind: run.problems[kind]
        for kind in ("tied", "lost", "unreadable", "future")
        if run.problems.get(kind)
    }
    if run.plan.ignored_targets:
        counts["ignored_folders"] = run.plan.ignored_targets
    if run.newly_enrolled:
        counts["new_folders"] = run.newly_enrolled
    archive = getattr(run, "archive", None)
    if archive is not None and archive.archived:
        counts["archived_copies"] = archive.archived
    if archive is not None and archive.opened:
        counts["unarchived_copies"] = archive.opened
    return counts


def _folder_label(path: Path) -> str:
    return f"{path.parent.name[:8]}/{path.name[:8]}"


def _write_plan_report(config: Config, run: Any) -> None:
    """Every planned action and every chat left alone, for a human to review.

    Session ids and short folder labels only: no titles, paths or contents.
    """

    from datetime import datetime, timezone

    from .adapters import save_status

    plan = run.plan
    labels = {
        key: _folder_label(target.path) for key, target in plan.context.targets.items()
    }
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
            "ignored_folders": plan.ignored_targets,
        },
    )


def _chat_payload(arguments: Any, config: Config, deps: CliDependencies) -> dict:
    from .chat_sync import ChatStateError, run_chat_sync

    started = deps.clock()
    try:
        chat_run = run_chat_sync(
            config,
            deps.planner_factory(config),
            deps.engine_factory(config),
            prefer=getattr(arguments, "prefer", None),
            prefer_session=getattr(arguments, "session", None),
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
        if running:
            return _report_waiting(arguments, config, output, "claude-open")
        record_progress(config, "syncing")
        # Before the chat sync, while Claude is surely closed: a restore a
        # switch missed comes back now, else the jar is saved.
        browser_logins = _settle_browser_logins(config, deps, config.profiles)
        artifact_warnings = _stop_artifact_warnings(config, deps)
        try:
            payload = _chat_payload(arguments, config, deps)
        except Exception as error:
            reason = _busy_reason(error)
            if reason is not None:
                return _report_waiting(arguments, config, output, reason)
            payload = _chat_failure(error)
        payload.update(
            run_adapters(
                config,
                deps,
                lambda: bool(_running_processes(config, deps)),
                adopt_current_sidebar=getattr(
                    arguments, "adopt_current_sidebar", False
                ),
                adopt_source_scope=getattr(arguments, "adopt_source_scope", None),
            )
        )
        if _browser_logins_busy(browser_logins):
            browser_logins = _settle_browser_logins(config, deps, config.profiles)
        if browser_logins["state"] != "disabled":
            payload["browser_logins"] = browser_logins
        _report_artifact_warnings(artifact_warnings, payload)
        payload["progress"] = finish_progress(config, payload)
        _resume_chat_pictures(config, deps, payload)
        _write(payload, as_json=arguments.as_json, stream=output)
        return _exit_code(arguments, payload["progress"])
    except Exception:
        record_progress(
            config, "needs-attention", reason="sync-error", next_action="run-doctor"
        )
        raise
    finally:
        handoff.release()


def _report_waiting(arguments, config: Config, output: TextIO, reason: str) -> int:
    """Nothing was written. The next quit, switch, or retry syncs."""

    if reason == "claude-open":
        payload = {
            "state": "waiting",
            "reason": reason,
            "progress": "waiting-for-Claude",
        }
    else:
        payload = {"state": "skipped", "reason": reason, "progress": "waiting-for-sync"}
    record_progress(config, payload["progress"])
    _write(payload, as_json=arguments.as_json, stream=output)
    return _exit_code(arguments, payload["progress"])


def _exit_code(arguments, progress: str) -> int:
    """`auto` succeeds while it waits, because the watcher runs it again.

    A manual `sync` succeeds only when everything finished.
    """

    if progress == "finished":
        return 0
    waiting = progress in ("waiting-for-Claude", "waiting-for-sync")
    return 0 if waiting and arguments.command == "auto" else 1


def _install_report(report: Any, **counts: int) -> dict:
    return {
        "state": report.state,
        "counts": {
            "actions": report.change_count,
            "backups": len(report.backups),
            **counts,
        },
        "bytes": 0,
        "duration_ms": 0,
    }


def _cmd_install(
    arguments, deps: CliDependencies, output: TextIO, answers: TextIO
) -> int:
    installer = deps.installer_factory(arguments.config)
    if arguments.command == "install":
        report = installer.install(dry_run=arguments.dry_run)
    else:
        report = installer.uninstall(dry_run=arguments.dry_run)
    _write(_install_report(report), as_json=False, stream=output)
    if arguments.command == "uninstall" and not arguments.dry_run:
        _release_artifact_switch(arguments.config, deps, output)
    return 0


def _release_artifact_switch(
    config_path: Path, deps: CliDependencies, output: TextIO
) -> None:
    """At uninstall, take out the artifact warning switch a sync put in.

    Nothing syncs after an uninstall, so a switch left in settings.json would
    keep Claude from answering artifact comments with nothing left to undo it.
    A switch the person put in themselves stays.
    """

    from .artifact_warnings import release_switch

    if not config_path.exists():
        return
    try:
        switch = release_switch(deps.config_loader(config_path).state_dir)
    except Exception as error:
        output.write(f"The artifact warning switch stays in settings.json: {error}\n")
        return
    if switch == "removed":
        output.write(
            "Took the artifact warning switch out of ~/.claude/settings.json.\n"
        )


def _cmd_setup(
    arguments, deps: CliDependencies, output: TextIO, answers: TextIO
) -> int:
    installer = deps.installer_factory(arguments.config)
    if arguments.config.exists():
        source = arguments.config.read_bytes()
    else:
        source = installer.default_config_data()
    if arguments.no_stop_artifact_warnings and arguments.apply:
        source = _record_artifact_warnings_no(source)
    elif (
        arguments.ask_artifact_warnings
        and arguments.apply
        and not arguments.stop_artifact_warnings
    ):
        source = _ask_artifact_warnings(source, answers, output)
    desired, config_payload = prepare_config_data(
        source,
        automatic_targets=arguments.automatic_targets,
        sync_layout=arguments.sync_layout,
        sync_routines=arguments.sync_routines,
        sync_browser_logins=arguments.sync_browser_logins,
        stop_artifact_warnings=arguments.stop_artifact_warnings,
    )
    ask = None
    if arguments.ask_main_account and arguments.apply:
        desired_config = validate_config_data(desired)

        def ask(helper: Path) -> None:
            _ask_main_account(desired_config, deps, helper, answers, output)

    report = installer.setup(
        dry_run=arguments.dry_run,
        config_data=desired,
        before_activation=ask,
    )
    _write(
        _install_report(report, **config_payload["counts"]),
        as_json=False,
        stream=output,
    )
    return 0


def _cmd_configure(
    arguments, deps: CliDependencies, output: TextIO, answers: TextIO
) -> int:
    payload = configure_file(
        arguments.config,
        automatic_targets=arguments.automatic_targets,
        sync_layout=arguments.sync_layout,
        sync_routines=arguments.sync_routines,
        sync_browser_logins=arguments.sync_browser_logins,
        stop_artifact_warnings=arguments.stop_artifact_warnings,
        apply=arguments.apply,
    )
    _write(payload, as_json=False, stream=output)
    return 0


def _cmd_restart_check(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
    profile = _find_profile(config, arguments.profile)
    if not profile.is_default or config.target_policy not in (
        "all-configured-profiles",
        "logins",
    ):
        raise ValueError(
            "automatic restart is enabled only for the default profile in automatic mode"
        )
    try:
        processes = _running_processes(config, deps)
    except subprocess.TimeoutExpired as error:
        _write(_chat_failure(error), as_json=True, stream=output)
        return 1
    selected_root = normalized_path(profile.data_root)
    if any(
        normalized_path(process.user_data_dir) != selected_root for process in processes
    ):
        raise ValueError(
            "another managed profile is open; leave Claude open until it is closed"
        )
    _write(
        {"state": "ready", "pids": [process.pid for process in processes]},
        as_json=True,
        stream=output,
    )
    return 0


def _cmd_plan(arguments, config: Config, deps: CliDependencies, output: TextIO) -> int:
    from .chat_sync import plan_chat_sync

    started = deps.clock()
    chat_run = plan_chat_sync(config, deps.planner_factory(config))
    if arguments.report:
        _write_plan_report(config, chat_run)
    payload = _chat_plan_summary(chat_run, round((deps.clock() - started) * 1000))
    _write(payload, as_json=arguments.as_json, stream=output)
    return 0 if _plan_state(chat_run.plan) in ("planned", "noop") else 1


def _cmd_seed_state(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
    from .seeding import seed_from_unenrolled

    _write(
        seed_from_unenrolled(config, apply=arguments.apply),
        as_json=False,
        stream=output,
    )
    return 0


def _cmd_restart_claude(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
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


def _cmd_keep_sidebar(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
    payload, exit_code, lines = _keep_sidebar(
        config, deps, number=arguments.account, apply=arguments.apply
    )
    output.write("".join(line + "\n" for line in lines))
    _write(payload, as_json=False, stream=output)
    return exit_code


def _cmd_forget_lost(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
    from .chat_sync import forget_seen

    folders = forget_seen(config, arguments.session_id)
    _write(
        {"state": "forgotten" if folders else "noop", "counts": {"folders": folders}},
        as_json=False,
        stream=output,
    )
    return 0


def _cmd_rollback(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
    started = deps.clock()
    receipt = deps.engine_factory(config).rollback(arguments.run_id)
    duration_ms = round((deps.clock() - started) * 1000)
    _write(
        _recovery_summary(receipt, duration_ms),
        as_json=arguments.as_json,
        stream=output,
    )
    return 0


def _cmd_browser_logins(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
    from .browser_logins import run_browser_logins

    payload = run_browser_logins(
        config,
        lambda: bool(_running_processes(config, deps)),
        arguments.action,
    )
    _write(payload, as_json=arguments.as_json, stream=output)
    return 0 if payload["state"] in ("done", "disabled") else 1


def _status_state(
    running, layout_failure: int, routine_failure: int, watcher_error: int
) -> str:
    if running:
        return "app-running"
    if layout_failure:
        return "layout-failed"
    if routine_failure:
        return "routines-failed"
    if watcher_error:
        return "watcher-failed"
    return "idle"


def _cmd_status(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
    from .artifact_warnings import status_fields

    running = _running_processes(config, deps)
    warnings = status_fields(config)
    watcher_error = watcher_failure(config.state_dir)
    layout_failure = adapter_failure(config, "layout")
    routine_failure = adapter_failure(config, "routines")
    abandoned = abandoned_preparation_count(config.state_dir)
    progress = current_progress(
        config,
        app_running=bool(running),
        failures=watcher_error + layout_failure + routine_failure + abandoned,
    )
    if abandoned:
        progress["next_action"] = "inspect-preparations"
    _write(
        {
            "bytes": 0,
            "counts": {
                "abandoned_preparations": abandoned,
                "artifact_warnings_cleared": warnings.pop("cleared"),
                "layout_failures": layout_failure,
                "profiles": len(config.profiles),
                "routine_failures": routine_failure,
                "running_processes": len(running),
                "watcher_failures": watcher_error,
            },
            "duration_ms": 0,
            **progress,
            "state": _status_state(
                running, layout_failure, routine_failure, watcher_error
            ),
            **warnings,
        },
        as_json=arguments.as_json,
        stream=output,
    )
    return 0


def _cmd_doctor(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
    payload = doctor_summary(
        config, deps, lambda: bool(_running_processes(config, deps))
    )
    _write(payload, as_json=arguments.as_json, stream=output)
    return 0 if payload["state"] == "healthy" else 1


def _cmd_approve_current_targets(
    arguments, config: Config, deps: CliDependencies, output: TextIO
) -> int:
    payload = approve_current_targets(arguments.config, config, apply=arguments.apply)
    _write(payload, as_json=False, stream=output)
    return 0 if payload["state"] != "blocked" else 1


# Commands that work on the config file itself, before it can be loaded.
_SETUP_COMMANDS: dict[str, Callable[..., int]] = {
    "install": _cmd_install,
    "uninstall": _cmd_install,
    "setup": _cmd_setup,
    "configure": _cmd_configure,
}

_COMMANDS: dict[str, Callable[..., int]] = {
    "restart-check": _cmd_restart_check,
    "plan": _cmd_plan,
    "seed-state": _cmd_seed_state,
    "restart-claude": _cmd_restart_claude,
    "keep-sidebar": _cmd_keep_sidebar,
    "forget-lost": _cmd_forget_lost,
    "sync": _run_sync,
    "auto": _run_sync,
    "switch": _run_switch,
    "signed-out-sync": _run_signed_out,
    "signed-in-groups": _run_signed_in_groups,
    "rollback": _cmd_rollback,
    "browser-logins": _cmd_browser_logins,
    "status": _cmd_status,
    "doctor": _cmd_doctor,
    "approve-current-targets": _cmd_approve_current_targets,
}

# Errors the tool raises on purpose carry a message written for the person.
# Anything else is a bug, so its traceback is shown.
_EXPECTED_ERRORS = (
    ValueError,
    OSError,
    RuntimeError,
    subprocess.SubprocessError,
    sqlite3.Error,
)


def run(
    argv: Sequence[str] | None = None,
    *,
    dependencies: CliDependencies | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    """Run one command and return an exit status without terminating tests."""

    deps = dependencies or CliDependencies()
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    answers = stdin or sys.stdin
    try:
        with contextlib.redirect_stderr(errors):
            arguments = _parser().parse_args(argv)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2

    try:
        setup_command = _SETUP_COMMANDS.get(arguments.command)
        if setup_command is not None:
            return setup_command(arguments, deps, output, answers)
        command = _COMMANDS.get(arguments.command)
        if command is None:
            errors.write("claude-session-sync: unsupported command\n")
            return 2
        config = deps.config_loader(arguments.config)
        if getattr(arguments, "adopt_current_sidebar", False) or (
            getattr(arguments, "adopt_source_scope", None) is not None
        ):
            from .layout import MAIN_ACCOUNT_BLOCKERS, main_account_blocker

            blocker = main_account_blocker(config)
            if blocker is not None:
                raise ValueError(MAIN_ACCOUNT_BLOCKERS[blocker])
        return command(arguments, config, deps, output)
    except _EXPECTED_ERRORS as error:
        errors.write(f"claude-session-sync: {error}\n")
        return 1
    except Exception:
        errors.write(
            "claude-session-sync: unexpected error, please report it with this output\n"
        )
        traceback.print_exc(file=errors)
        return 1


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
