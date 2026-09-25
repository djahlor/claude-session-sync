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
            "ambiguous_assignments": "ambiguous_assignments",
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
        return {"state": "skipped", "reason": "unsafe-" + name, "detail": str(error)}
    singular = "routine" if name == "routines" else name
    return {"state": "skipped", "reason": singular + "-error"}


def run_adapters(
    config,
    dependencies,
    running,
    *,
    probe_only=False,
    prefer_current_sidebar=False,
    adopt_current_sidebar=False,
    adopt_source_scope=None,
) -> dict:
    """A failed adapter cannot prevent the other enabled adapter from running."""
    results = {}
    probes = {}
    for name, flag, factory, filename, fields in ADAPTERS:
        if not getattr(config, flag):
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
                    elif name == "layout" and prefer_current_sidebar:
                        receipt = adapter.sync(prefer_current_sidebar=True)
                    else:
                        receipt = adapter.sync()
                    result = {"state": receipt.state}
                    result.update(
                        {key: getattr(receipt, attr) for key, attr in fields.items()}
                    )
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


def adapter_failure(config, name: str) -> int:
    for key, flag, _factory, filename, _fields in ADAPTERS:
        if name == key and getattr(config, flag):
            return int(
                read_status(config.state_dir, filename).get("state")
                not in ("synced", "noop", "unknown")
            )
    return 0
