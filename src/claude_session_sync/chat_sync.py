"""One chat sync run, in the order that keeps a crash harmless.

Load state, plan, write down what was seen, apply under the journal, record
what this run placed, rescan, settle, save. Ported in spirit from
vinlim/claude-desktop-sync (0BSD); the writes go through this project's
journaled transaction engine.
"""

import inspect
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .chat_state import StateUnusable, encode_state, load_state, save_state, state_path
from .liveness import last_known_account
from .model import Plan, RunReceipt, SyncRequest


class ChatStateError(RuntimeError):
    """The chat state file cannot be trusted. Nothing was changed."""


@dataclass
class ChatRun:
    plan: Plan
    receipt: Optional[RunReceipt] = None
    problems: Dict[str, int] = field(default_factory=dict)
    restart_suggested: int = 0
    newly_enrolled: int = 0


def plan_chat_sync(
    config,
    planner,
    *,
    prefer: Optional[str] = None,
    prefer_session: Optional[str] = None,
    running_processes: Optional[Callable] = None,
) -> ChatRun:
    """Plan without writing anything, including the state file."""

    state = _load(config)
    plan = _plan(planner, config, state, prefer, prefer_session, running_processes)
    return _summary(plan, state)


def run_chat_sync(
    config,
    planner,
    engine,
    *,
    prefer: Optional[str] = None,
    prefer_session: Optional[str] = None,
    running_processes: Optional[Callable] = None,
    clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
) -> ChatRun:
    path = state_path(config.state_dir)
    state = _load(config)
    on_disk = encode_state(state)
    plan = _plan(planner, config, state, prefer, prefer_session, running_processes)
    context = plan.context
    if context is None:
        # A planner without chat state: apply as the engine always did.
        receipt = engine.apply(plan) if not plan.invalid_replicas else None
        return ChatRun(plan, receipt)
    if plan.invalid_replicas:
        return _summary(plan, state)

    _forget_stale_live_creates(state, context)
    for snapshot in context.snapshots:
        state.sync.seen.setdefault(snapshot.key, set()).update(snapshot.records)
    if encode_state(state) != on_disk:
        # Durable before the first write: a run that dies must not forget what
        # it saw, or a chat Claude deletes later would look new and come back.
        save_state(path, state)
        on_disk = encode_state(state)

    receipt = engine.apply(plan, live_guard=context.is_live_path)
    _remember_placements(state, context, receipt)

    state.sync = _settle(state.sync, context.rescan())
    if receipt.status in ("committed", "partial", "noop"):
        state.last_success_ms = clock_ms()
    if encode_state(state) != on_disk:
        save_state(path, state)
    run = _summary(plan, state)
    run.receipt = receipt
    return run


def forget_seen(config, session_id: str) -> int:
    """Let the next run put back one chat reported as lost. Returns folders changed."""

    path = state_path(config.state_dir)
    state = _load(config)
    changed = 0
    for ids in state.sync.seen.values():
        if session_id in ids:
            ids.discard(session_id)
            changed += 1
    if changed:
        save_state(path, state)
    return changed


def _load(config):
    try:
        return load_state(state_path(config.state_dir))
    except StateUnusable as error:
        raise ChatStateError(str(error)) from error


def _plan(planner, config, state, prefer, prefer_session, running_processes) -> Plan:
    request = SyncRequest(config)
    try:
        parameters = inspect.signature(planner.plan).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "state" not in parameters:
        if prefer is not None or prefer_session is not None:
            raise ValueError("this planner cannot settle ties")
        return planner.plan(request)
    options: Dict[str, Any] = {"state": state, "prefer": prefer, "prefer_session": prefer_session}
    if running_processes is not None and "running_processes" in parameters:
        options["running_processes"] = running_processes
    return planner.plan(request, **options)


def _settle(sync, snapshots):
    from .rules import settle

    return settle(sync, snapshots)


def _summary(plan: Plan, state) -> ChatRun:
    context = plan.context
    run = ChatRun(plan)
    run.problems = dict(Counter(problem.kind for problem in plan.problems))
    if context is not None:
        run.newly_enrolled = len(context.newly_enrolled)
        run.restart_suggested = sum(
            len(entry.get("ids", ()))
            for key, entry in state.live_creates.items()
            if key in context.data_roots
            and context.running_pids.get(str(context.data_roots[key]))
        )
    return run


def _signed_in(state, context, key: str):
    """(account, login time, pids) for a folder of the signed-in account, else None."""

    root = context.data_roots.get(key)
    if root is None:
        return None
    pids = context.running_pids.get(str(root))
    login = state.logins.get(str(root))
    account = last_known_account(Path(root))
    target = context.targets[key]
    if not pids or login is None or account is None or login[0] != account:
        return None
    if target.account_id.lower() != account:
        return None
    return account, login[1], list(pids)


def _forget_stale_live_creates(state, context) -> None:
    """Claude reads a folder again when it restarts or the login changes."""

    for key in list(state.live_creates):
        entry = state.live_creates[key]
        current = _signed_in(state, context, key)
        if current is None or (
            current[0] != entry["account"]
            or current[1] != entry["login_ms"]
            or sorted(current[2]) != sorted(entry["pids"])
        ):
            del state.live_creates[key]


def _remember_placements(state, context, receipt: RunReceipt) -> None:
    folders = {str(target.path): key for key, target in context.targets.items()}
    for operation in receipt.applied:
        if operation.artifact != "record" or operation.kind not in ("create", "replace"):
            continue
        key = folders.get(str(Path(operation.destination).parent))
        if key is None:
            continue
        if operation.source_state_hash is not None:
            state.sync.placed.setdefault(key, {})[operation.session_id] = operation.source_state_hash
        if operation.kind != "create":
            continue
        state.sync.seen.setdefault(key, set()).add(operation.session_id)
        if key not in context.live:
            continue
        current = _signed_in(state, context, key)
        if current is None:
            continue  # a folder of another account, read when that account signs in
        entry = state.live_creates.get(key)
        if entry is None or (entry["account"], entry["login_ms"]) != current[:2]:
            entry = {"account": current[0], "login_ms": current[1], "pids": current[2], "ids": []}
            state.live_creates[key] = entry
        entry["ids"] = sorted(set(entry["ids"]) | {operation.session_id})
