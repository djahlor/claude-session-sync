"""One chat sync run, in the order that keeps a crash harmless.

Load state, plan, write down what was seen, apply under the journal, record
what this run placed, rescan, settle, save. Ported in spirit from
vinlim/claude-desktop-sync (0BSD); the writes go through this project's
journaled transaction engine.
"""

import inspect
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional

from .chat_state import StateUnusable, encode_state, load_state, save_state, state_path
from .filesystem import sweep_stale_stages
from .model import Plan, RunReceipt, SyncRequest


class ChatStateError(RuntimeError):
    """The chat state file cannot be trusted. Nothing was changed."""


@dataclass
class ChatRun:
    plan: Plan
    receipt: Optional[RunReceipt] = None
    problems: Dict[str, int] = field(default_factory=dict)
    newly_enrolled: int = 0


def plan_chat_sync(
    config,
    planner,
    *,
    prefer: Optional[str] = None,
    prefer_session: Optional[str] = None,
) -> ChatRun:
    """Plan without writing anything, including the state file."""

    state = _load(config)
    plan = _plan(planner, config, state, prefer, prefer_session)
    return _summary(plan)


def run_chat_sync(
    config,
    planner,
    engine,
    *,
    prefer: Optional[str] = None,
    prefer_session: Optional[str] = None,
    clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
) -> ChatRun:
    path = state_path(config.state_dir)
    state = _load(config)
    on_disk = encode_state(state)
    plan = _plan(planner, config, state, prefer, prefer_session)
    context = plan.context
    if context is None:
        # A planner without chat state: apply as the engine always did.
        receipt = engine.apply(plan) if not plan.invalid_replicas else None
        return ChatRun(plan, receipt)
    if plan.invalid_replicas:
        return _summary(plan)

    for snapshot in context.snapshots:
        state.sync.seen.setdefault(snapshot.key, set()).update(snapshot.records)
    if encode_state(state) != on_disk:
        # Durable before the first write: a run that dies must not forget what
        # it saw, or a chat Claude deletes later would look new and come back.
        save_state(path, state)
        on_disk = encode_state(state)

    sweep_stale_stages((target.path for target in context.targets.values()), time.time())
    engine.close_interrupted_runs()
    receipt = engine.apply(plan)
    _remember_placements(state, context, receipt)

    state.sync = _settle(state.sync, context.rescan())
    if receipt.status in ("committed", "noop"):
        state.last_success_ms = clock_ms()
    if encode_state(state) != on_disk:
        save_state(path, state)
    run = _summary(plan)
    run.receipt = receipt
    return run


def forget_seen(config, session_id: str) -> int:
    """Let the next run put back one chat reported as lost. Returns folders changed."""

    with state_writer(config):
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


@contextmanager
def state_writer(config, timeout: float = 30.0):
    """Hold the sync handoff lock, so no sync run saves over this change."""

    from .locking import ExclusiveFileLock

    lock = ExclusiveFileLock(
        config.state_dir / "switch-handoff.lock", mode="auto", timeout=timeout
    )
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _load(config):
    try:
        return load_state(state_path(config.state_dir))
    except StateUnusable as error:
        raise ChatStateError(str(error)) from error


def _plan(planner, config, state, prefer, prefer_session) -> Plan:
    request = SyncRequest(config)
    try:
        parameters = inspect.signature(planner.plan).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "state" not in parameters:
        if prefer is not None or prefer_session is not None:
            raise ValueError("this planner cannot settle ties")
        return planner.plan(request)
    return planner.plan(request, state=state, prefer=prefer, prefer_session=prefer_session)


def _settle(sync, snapshots):
    from .rules import settle

    return settle(sync, snapshots)


def _summary(plan: Plan) -> ChatRun:
    context = plan.context
    run = ChatRun(plan)
    run.problems = dict(Counter(problem.kind for problem in plan.problems))
    if context is not None:
        run.newly_enrolled = len(context.newly_enrolled)
    return run


def _remember_placements(state, context, receipt: RunReceipt) -> None:
    folders = {str(target.path): key for key, target in context.targets.items()}
    for operation in receipt.applied:
        if operation.artifact != "record" or operation.kind not in ("create", "replace"):
            continue
        key = folders.get(str(Path(operation.destination).parent))
        if key is None:
            continue
        if operation.source_state_hash is not None:
            # What sync placed is this folder's version in step with the others.
            state.sync.synced.setdefault(key, {})[operation.session_id] = operation.source_state_hash
        if operation.kind == "create":
            state.sync.seen.setdefault(key, set()).add(operation.session_id)
