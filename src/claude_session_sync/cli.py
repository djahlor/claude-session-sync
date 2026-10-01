"""Command-line boundary for safe session synchronization."""

from __future__ import annotations

import argparse
import contextlib
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence, TextIO

from .config import Config, load_config
from . import __version__
from . import strict_json as json
from .adapters import adapter_failure, run_adapters
from .config_commands import (
    _approve_current_targets,
    _configure,
    _prepare_config_data,
    _validate_config_data,
)
from .health import abandoned_preparation_count, doctor_summary, watcher_failure
from .progress import current_progress, finish_progress, record_progress
from .model import Plan


ConfigLoader = Callable[[Path], Config]
PlannerFactory = Callable[[Config], Any]
EngineFactory = Callable[[Config], Any]
InstallerFactory = Callable[[Path], Any]
LayoutFactory = Callable[[Config], Any]
RoutineFactory = Callable[[Config], Any]
PROCESS_EXIT_POLL_SECONDS = 0.1
PROCESS_PROBE_TIMEOUT_SECONDS = 15.0
SWITCH_WRITER_WAIT_SECONDS = 15.0


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
        "--dry-run", action="store_true", help="list the accounts and the one --apply keeps"
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
    configure_mode = configure.add_mutually_exclusive_group(required=True)
    configure_mode.add_argument("--dry-run", action="store_true")
    configure_mode.add_argument("--apply", action="store_true")
    setup = commands.add_parser(
        "setup", help="configure and install macOS adapters atomically"
    )
    setup.add_argument("--automatic-targets", action="store_true")
    setup.add_argument("--sync-layout", action="store_true")
    setup.add_argument("--sync-routines", action="store_true")
    setup.add_argument(
        "--ask-main-account",
        action="store_true",
        help="on a terminal, ask whose pins and groups the other accounts copy",
    )
    setup_mode = setup.add_mutually_exclusive_group(required=True)
    setup_mode.add_argument("--dry-run", action="store_true")
    setup_mode.add_argument("--apply", action="store_true")
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


def _switch_chat_result(config: Config, dependencies: CliDependencies) -> dict:
    writer_deadline = dependencies.monotonic() + SWITCH_WRITER_WAIT_SECONDS
    while True:
        try:
            return _chat_payload(None, config, dependencies)
        except Exception as error:
            remaining = writer_deadline - dependencies.monotonic()
            if _busy_reason(error) == "busy" and remaining > 0:
                dependencies.sleeper(min(PROCESS_EXIT_POLL_SECONDS, remaining))
                continue
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
        try:
            payload = _switch_chat_result(config, dependencies)
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
        finally:
            # Claude was closed for this switch. It reopens even when the sync
            # failed, so a failed sync never leaves Claude closed.
            launch_failed = not arguments.no_launch and not _launch(profile, dependencies)
        if launch_failed:
            _write(
                {"state": "launch_failed", "reason": "launch-command-failed"},
                as_json=as_json,
                stream=output,
            )
            return 1
        if not arguments.no_launch:
            payload["launch"] = "started"
        _write(payload, as_json=as_json, stream=output)
        return 0 if payload["progress"] == "finished" else 1
    finally:
        handoff.release()


def _launch(profile: Any, dependencies: CliDependencies) -> bool:
    """Start the profile's Claude. False when the launch command failed."""

    from .launcher import LaunchError, Launcher

    try:
        (dependencies.launcher or Launcher()).launch(profile.launch_command)
    except LaunchError:
        return False
    return True


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
    config: Config, deps: CliDependencies, helper: Optional[Path] = None
) -> list:
    """The accounts the next sync can copy pins and groups from, signed-in first."""

    from .layout import LayoutSynchronizer, scope_sessions

    default = next(profile for profile in config.profiles if profile.is_default)
    targets = [
        target
        for target in deps.planner_factory(config).sync_targets(config)
        if target.profile_name == default.name
    ]
    return LayoutSynchronizer(config, helper=helper).accounts(scope_sessions(targets))


def _account_lines(rows: Sequence[Any]) -> list:
    """One line per account. Accounts have no names on disk, so rows that would
    look the same also show a short ID."""

    looks = [(row.is_signed_in, row.groups, row.pins, row.chats) for row in rows]
    show_id = len(set(looks)) < len(looks)
    lines = []
    for number, row in enumerate(rows, 1):
        columns = ["{:>3}".format(number), "signed in" if row.is_signed_in else " " * 9]
        if show_id:
            account, _separator, workspace = row.scope.partition("/")
            columns.append("{}/{}".format(account[:8], workspace[:8]))
        for count, word in ((row.groups, "group"), (row.pins, "pin"), (row.chats, "chat")):
            columns.append("{:>4} {}".format(count, word if count == 1 else word + "s").ljust(11))
        lines.append("  ".join(columns).rstrip())
    return lines


def _keep_sidebar(
    config: Config, deps: CliDependencies, *, number: Optional[int], apply: bool
) -> tuple:
    """Choose which account's pins and groups the next closed sync copies.

    Returns the payload, the exit code, and the account lines to show.
    """

    from .layout import request_adoption

    if not config.sync_sidebar_layout:
        return {"state": "blocked", "reason": "sidebar-sync-off"}, 1, []
    if sum(profile.is_default for profile in config.profiles) != 1:
        return {"state": "blocked", "reason": "needs-one-default-profile"}, 1, []
    rows = _sidebar_rows(config, deps)
    lines = _account_lines(rows)
    if number is None:
        number = next((index for index, row in enumerate(rows, 1) if row.is_signed_in), None)
        if number is None:
            return {"state": "blocked", "reason": "signed-in-account-not-synced"}, 1, lines
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
    was adopted, and when two or more accounts have groups. Enter keeps the
    signed-in account. The install never fails here: a problem skips the
    question, and the first sync then follows its own rules.
    """

    from .layout import load_snapshot, request_adoption

    if not (stdin.isatty() and output.isatty()) or not config.sync_sidebar_layout:
        return
    if sum(profile.is_default for profile in config.profiles) != 1:
        return
    try:
        snapshots = [
            config.state_dir / "sidebar-layout-{}.json".format(index)
            for index in range(len(config.profiles))
        ]
        if any(load_snapshot(path) is not None for path in snapshots):
            return
        rows = _sidebar_rows(config, deps, helper)
        if sum(1 for row in rows if row.groups) < 2:
            return
        _discard_typeahead(stdin)
        output.write("\nWhich account is the main one?\n")
        output.write("The other accounts will copy its pins and groups.\n\n")
        output.write("".join(line + "\n" for line in _account_lines(rows)))
        output.write("\n")
        number = _choose_row(rows, stdin, output)
        if number is None:
            output.write("\nNo account chosen. The first sync decides.\n")
            return
        request_adoption(config.state_dir, rows[number - 1].scope)
    except Exception as error:
        output.write("\nSkipped the main-account question: {}\n".format(error))
        return
    output.write(
        "Account {} is the main one. The others copy its pins and groups "
        "the next time Claude closes.\n\n".format(number)
    )


def _discard_typeahead(stdin: TextIO) -> None:
    """Drop keys pressed while setup compiled, so they cannot answer the question."""

    import termios

    try:
        termios.tcflush(stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError, termios.error):
        return  # not a real terminal


def _choose_row(rows: Sequence[Any], stdin: TextIO, output: TextIO) -> Optional[int]:
    """Read a row number with groups; Enter means the signed-in row. None at end of input."""

    default = next(
        (index for index, row in enumerate(rows, 1) if row.is_signed_in and row.groups), None
    )
    prompt = "Type a number: " if default is None else "Press Enter for {}, or type a number: ".format(default)
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
        counts[kind + "s"] = sum(1 for operation in plan.operations if operation.kind == kind)
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
    labels = {key: _folder_label(target.path) for key, target in plan.context.targets.items()}
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
        try:
            payload = _chat_payload(arguments, config, deps)
        except Exception as error:
            reason = _busy_reason(error)
            if reason is not None:
                return _report_waiting(arguments, config, output, reason)
            payload = _chat_failure(error)
        payload.update(
            run_adapters(
                config, deps, lambda: bool(_running_processes(config, deps)),
                adopt_current_sidebar=getattr(arguments, "adopt_current_sidebar", False),
                adopt_source_scope=getattr(arguments, "adopt_source_scope", None),
            )
        )
        payload["progress"] = finish_progress(config, payload)
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
        payload = {"state": "waiting", "reason": reason, "progress": "waiting-for-Claude"}
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


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    dependencies: Optional[CliDependencies] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    stdin: Optional[TextIO] = None,
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
                sync_layout=arguments.sync_layout,
                sync_routines=arguments.sync_routines,
            )
            ask = None
            if arguments.ask_main_account and arguments.apply:
                desired_config = _validate_config_data(desired)

                def ask(helper: Path) -> None:
                    _ask_main_account(desired_config, deps, helper, answers, output)

            report = installer.setup(
                dry_run=arguments.dry_run,
                config_data=desired,
                before_activation=ask,
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
                sync_layout=arguments.sync_layout,
                sync_routines=arguments.sync_routines,
                apply=arguments.apply,
            )
            _write(payload, as_json=False, stream=output)
            return 0
        config = deps.config_loader(arguments.config)
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
            from .chat_sync import plan_chat_sync

            started = deps.clock()
            chat_run = plan_chat_sync(config, deps.planner_factory(config))
            if arguments.report:
                _write_plan_report(config, chat_run)
            payload = _chat_plan_summary(chat_run, round((deps.clock() - started) * 1000))
            _write(payload, as_json=arguments.as_json, stream=output)
            return 0 if _plan_state(chat_run.plan) in ("planned", "noop") else 1
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
            payload, exit_code, lines = _keep_sidebar(
                config, deps, number=arguments.account, apply=arguments.apply
            )
            output.write("".join(line + "\n" for line in lines))
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
            layout_failure = adapter_failure(config, "layout")
            routine_failure = adapter_failure(config, "routines")
            abandoned = abandoned_preparation_count(config.state_dir)
            progress = current_progress(
                config,
                app_running=bool(running),
                failures=watcher_error
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
