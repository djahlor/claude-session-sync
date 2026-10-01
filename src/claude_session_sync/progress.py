"""Durable, aggregate progress for the CLI and quit watcher."""

import fcntl
import os
import stat
from datetime import datetime, timezone

from .adapters import desktop_build, read_status, save_status


def record_progress(config, state: str, **fields) -> dict:
    previous = read_status(config.state_dir, "sync-progress.json")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "state": state,
        "updated_at": now,
        "last_success_at": previous.get("last_success_at"),
        "desktop_build": desktop_build(config),
    }
    if state == "finished":
        payload["last_success_at"] = now
    payload.update(fields)
    save_status(config.state_dir, "sync-progress.json", payload)
    return payload


def finish_progress(config, payload: dict) -> str:
    """Record how a sync ended. A component deferred because Claude reopened is unfinished."""

    adapters = [payload[key] for key in ("routines", "layout") if key in payload]
    outcomes = [payload.get("state")] + [adapter.get("state") for adapter in adapters]
    attention = any(
        value not in ("committed", "synced", "noop", "disabled", "deferred")
        for value in outcomes
    )
    attention = attention or any(adapter.get("reporting_error") for adapter in adapters)
    if attention:
        state = "needs-attention"
    elif any(adapter.get("state") == "deferred" for adapter in adapters):
        state = "waiting-for-Claude"
    else:
        state = "finished"
    record_progress(config, state, result=payload)
    return state


def _writer_active(config) -> bool:
    path = config.state_dir / "switch-handoff.lock"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def current_progress(config, *, app_running: bool, failures: int = 0) -> dict:
    """Aggregate status. Nothing syncs while Claude is open, so an open Claude is a wait."""

    payload = read_status(config.state_dir, "sync-progress.json")
    watcher = read_status(config.state_dir, "watcher-status.json")
    restart_phase = watcher.get("restart_phase")
    restart_active = False
    saved_timestamp = watcher.get("timestamp")
    if restart_phase in ("checking", "quitting", "syncing") and isinstance(saved_timestamp, str):
        try:
            timestamp = datetime.fromisoformat(saved_timestamp.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - timestamp).total_seconds()
            restart_active = 0 <= age < 45
        except (KeyError, TypeError, ValueError):
            pass
    saved_state = payload.get("state")
    if restart_active and not failures:
        state = {"checking": "checking-account", "quitting": "quitting-Claude", "syncing": "syncing"}[restart_phase]
    elif saved_state == "syncing":
        try:
            state = "syncing" if _writer_active(config) else "needs-attention"
        except OSError:
            state = "needs-attention"
    elif restart_phase == "needs-attention":
        state = "needs-attention"
    elif failures or saved_state in ("needs-attention", "unreadable"):
        state = "needs-attention"
    elif app_running:
        state = "waiting-for-Claude"
    elif saved_state == "finished":
        state = "finished"
    elif saved_state in ("waiting-for-Claude", "waiting-for-sync"):
        state = "waiting-for-sync"
    else:
        state = "not-synced-yet"
    actions = {
        "waiting-for-Claude": "quit-Claude-to-sync",
        "waiting-for-sync": "wait-for-watcher-or-run-sync",
        "needs-attention": "run-doctor",
        "not-synced-yet": "run-sync",
        "quitting-Claude": "wait-for-automatic-restart",
        "checking-account": "wait-for-automatic-restart",
    }
    result = {"progress": state, "last_success_at": payload.get("last_success_at")}
    if watcher.get("automatic_restart") is True:
        result["automatic_restart"] = True
        result["restart_phase"] = restart_phase
        actions["waiting-for-Claude"] = "switch-account-or-quit-to-sync"
    if state in actions:
        result["next_action"] = actions[state]
    return result
