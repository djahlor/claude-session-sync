"""One execution and reporting boundary for optional storage adapters."""

import os
import subprocess
import stat
from pathlib import Path

from . import strict_json as json
from .filesystem import atomic_write_bytes, ensure_private_directory


ADAPTERS = (
    (
        "routines",
        "sync_code_routines",
        "routine_factory",
        "code-routines-status.json",
        {
            "profiles": "profile_count",
            "targets": "target_count",
            "manifests": "manifest_count",
            "tasks": "task_count",
            "writes": "write_count",
        },
    ),
    (
        "layout",
        "sync_sidebar_layout",
        "layout_factory",
        "sidebar-layout-status.json",
        {
            "profiles": "profile_count",
            "records": "record_count",
            "groups": "group_count",
            "assignments": "assignment_count",
            "pins": "pin_count",
        },
    ),
)


def desktop_build(config) -> str:
    try:
        path = config.claude_executable.parent.parent / "Info.plist"
        if not path.is_file():
            return "unknown"
        values = [
            subprocess.run(
                ["/usr/bin/plutil", "-extract", key, "raw", "-o", "-", str(path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            for key in ("CFBundleShortVersionString", "CFBundleVersion")
        ]
        return "{} ({})".format(*values)
    except (OSError, ValueError, subprocess.SubprocessError):
        return "unknown"


def save_status(state_dir: Path, filename: str, payload: dict) -> None:
    ensure_private_directory(state_dir)
    path = state_dir / filename
    atomic_write_bytes(
        path, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    )
    os.chmod(path, 0o600)


def read_status(state_dir: Path, filename: str) -> dict:
    path = state_dir / filename
    if not os.path.lexists(path):
        return {"state": "unknown"}
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > 256 * 1024 * 1024
            ):
                return {"state": "unreadable"}
            document = json.loads(stream.read(256 * 1024 * 1024 + 1))
        return document if isinstance(document, dict) else {"state": "unreadable"}
    except (OSError, ValueError):
        return {"state": "unreadable"}


def _failure(name, error) -> dict:
    from .layout import LayoutBusyError, LayoutError
    from .routines import RoutineBusyError, RoutineError

    if isinstance(error, (LayoutBusyError, RoutineBusyError)):
        return {"state": "skipped", "reason": "writer-busy"}
    if isinstance(error, (LayoutError, RoutineError)):
        reason = getattr(error, "reason", None) or "unsafe-" + name
        return {"state": "skipped", "reason": reason, "detail": str(error)}
    singular = "routine" if name == "routines" else name
    return {"state": "skipped", "reason": singular + "-error"}


def run_adapters(
    config,
    dependencies,
    running,
    *,
    probe_only=False,
    adopt_current_sidebar=False,
    adopt_source_scope=None,
    after_account_switch=False,
    names=None,
) -> dict:
    """A failed adapter cannot prevent the other enabled adapter from running.

    names, when given, runs only those adapters.
    """
    results = {}
    probes = {}
    for name, flag, factory, filename, fields in ADAPTERS:
        if not getattr(config, flag) or (names is not None and name not in names):
            continue
        try:
            if running():
                # Claude locks these stores while it runs. They sync when it
                # quits; the last real status stays on disk until then.
                results[name] = {"state": "deferred", "reason": "app-running"}
                continue
            else:
                adapter = getattr(dependencies, factory)(config)
                if probe_only:
                    result = adapter.probe()
                else:
                    # sync validates after guarded recovery. A probe before recovery
                    # could reject the interrupted state that recovery must repair.
                    if name == "layout" and adopt_source_scope is not None:
                        receipt = adapter.sync(adopt_source_scope=adopt_source_scope)
                    elif name == "layout" and adopt_current_sidebar:
                        receipt = adapter.sync(adopt_current_sidebar=True)
                    elif name == "layout" and after_account_switch:
                        receipt = adapter.sync(after_account_switch=True)
                    else:
                        receipt = adapter.sync()
                    result = {"state": receipt.state}
                    result.update(
                        {key: getattr(receipt, attr) for key, attr in fields.items()}
                    )
                    if getattr(receipt, "reason", None):
                        result["reason"] = receipt.reason
                    probes[name] = {"state": "compatible", "validated_by": "sync"}
        except Exception as error:
            result = _failure(name, error)
        results[name] = result
        if not probe_only:
            try:
                save_status(config.state_dir, filename, result)
            except OSError:
                results[name] = dict(result, reporting_error="status-write-failed")
    if not probe_only and probes:
        if names is not None:
            # A split run keeps what the other part recorded.
            previous = read_status(config.state_dir, "compatibility-status.json").get("adapters")
            if isinstance(previous, dict):
                probes = {**previous, **probes}
        try:
            save_status(
                config.state_dir,
                "compatibility-status.json",
                {
                    "desktop_build": desktop_build(config),
                    "adapters": probes,
                },
            )
        except OSError:
            for result in results.values():
                result["reporting_error"] = "compatibility-status-write-failed"
    return results


def run_layout_first(
    config,
    dependencies,
    running,
    *,
    adopt_current_sidebar=False,
    after_account_switch=False,
) -> dict:
    """Copy pins, groups and section views before a switch syncs chats.

    On builds that keep the sidebar per account, the copy needs nothing from
    the chat sync. Run first, it lands in the second after Claude quits, so
    Claude opening again during a long chat sync can no longer skip it and
    leave the new account on its old server sidebar. Returns the layout
    result when the copy ran; otherwise {} and the switch keeps the old order.
    """
    if not config.sync_sidebar_layout:
        return {}
    try:
        if running():
            return {}
        check = getattr(dependencies.layout_factory(config), "keeps_sidebar_per_account", None)
        if check is None or not check():
            return {}
    except Exception:
        # The run after the chat sync meets and reports the same problem.
        return {}
    status = config.state_dir / next(entry[3] for entry in ADAPTERS if entry[0] == "layout")
    status_before = status.read_bytes() if status.is_file() and not status.is_symlink() else None
    results = run_adapters(
        config,
        dependencies,
        running,
        adopt_current_sidebar=adopt_current_sidebar,
        after_account_switch=after_account_switch,
        names=("layout",),
    )
    layout = results.get("layout", {})
    if layout.get("state") == "deferred" or layout.get("reason") == "writer-busy":
        # Claude came back or another writer is finishing: retry in the old
        # place, after the chat sync has waited for that writer. The status
        # file goes back to what it said, since this attempt changed nothing.
        try:
            if status_before is not None:
                atomic_write_bytes(status, status_before)
                os.chmod(status, 0o600)
            elif os.path.lexists(status):
                os.unlink(status)
        except OSError:
            pass
        return {}
    return results


def adapter_failure(config, name: str) -> int:
    for key, flag, _factory, filename, _fields in ADAPTERS:
        if name == key and getattr(config, flag):
            return int(
                read_status(config.state_dir, filename).get("state")
                not in ("synced", "noop", "unknown", "per-account")
            )
    return 0
