"""Read-only checks of current storage, not just previous success receipts."""

import os
import shutil
from pathlib import Path

from .config import Config
from .adapters import desktop_build, read_status, run_adapters


def watcher_failure(state_dir: Path) -> int:
    status = read_status(state_dir, "watcher-status.json")
    return int(status.get("state") not in ("ok", "unknown") or status.get("restart_phase") == "needs-attention")


def abandoned_preparation_count(state_dir: Path) -> int:
    from .journal import JournalError, abandoned_preparations

    try:
        return len(abandoned_preparations(state_dir))
    except (JournalError, OSError):
        return 1


def _adapter_recovery_pending(config) -> int:
    count = 0
    for enabled, directory in (
        (config.sync_code_routines, "routine-runs"),
        (config.sync_sidebar_layout, "layout-runs"),
    ):
        root = config.state_dir / directory
        if not enabled or not os.path.lexists(root):
            continue
        if root.is_symlink() or not root.is_dir():
            count += 1
            continue
        try:
            count += sum(
                read_status(root, path.name).get("state")
                not in ("COMMITTED", "ROLLED_BACK")
                for path in root.glob("*.json")
            )
        except OSError:
            count += 1
    return count


def doctor_summary(
    config: Config, dependencies, running, launch_guard_failure: int
) -> dict:
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
    recovery_pending += _adapter_recovery_pending(config)
    errors += recovery_pending
    abandoned = abandoned_preparation_count(config.state_dir)
    errors += abandoned
    watcher_error = watcher_failure(config.state_dir)
    errors += watcher_error
    probes = run_adapters(config, dependencies, running, probe_only=True)
    layout_failure = int(
        probes.get("layout", {}).get("state", "disabled")
        not in ("compatible", "noop", "disabled")
        and probes.get("layout", {}).get("reason") != "app-running"
    )
    routine_failure = int(
        probes.get("routines", {}).get("state", "disabled")
        not in ("compatible", "noop", "disabled")
        and probes.get("routines", {}).get("reason") != "app-running"
    )
    waiting = any(value.get("reason") == "app-running" for value in probes.values())
    errors += layout_failure + routine_failure
    errors += launch_guard_failure
    return {
        "bytes": 0,
        "counts": {
            "checks": 8 + len(config.profiles),
            "abandoned_preparations": abandoned,
            "errors": errors,
            "invalid_replicas": len(discovery.invalid_replicas),
            "launch_guards": launch_guard_failure,
            "layout_failures": layout_failure,
            "profiles": len(config.profiles),
            "recovery_pending": recovery_pending,
            "routine_failures": routine_failure,
            "targets": len(discovery.targets),
            "unapproved_targets": unapproved,
            "watcher_failures": watcher_error,
        },
        "duration_ms": 0,
        "state": "unhealthy"
        if errors
        else "waiting-for-Claude"
        if waiting
        else "healthy",
        "adapters": probes,
        "desktop_build": desktop_build(config),
        **({"next_action": "inspect-preparations"} if abandoned else {}),
    }
