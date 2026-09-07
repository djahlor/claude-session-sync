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
    outcomes = [payload.get("state")]
    outcomes.extend(
        payload[key].get("state") for key in ("routines", "layout") if key in payload
    )
    attention = any(
        value not in ("committed", "synced", "noop", "disabled") for value in outcomes
    )
    attention = attention or any(
        payload.get(key, {}).get("reporting_error") for key in ("routines", "layout")
    )
    state = "needs-attention" if attention else "finished"
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
    payload = read_status(config.state_dir, "sync-progress.json")
    saved_state = payload.get("state")
    if saved_state == "syncing":
        try:
            state = "syncing" if _writer_active(config) else "needs-attention"
        except OSError:
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
    }
    result = {"progress": state, "last_success_at": payload.get("last_success_at")}
    if state in actions:
        result["next_action"] = actions[state]
    return result
