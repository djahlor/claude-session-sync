"""One chat sync run, in the order that keeps a crash harmless.

Load state, plan, write down what was seen, apply under the journal, record
what this run placed, rescan, settle, save. Ported in spirit from
vinlim/claude-desktop-sync (0BSD); the writes go through this project's
journaled transaction engine.
"""

import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .archive import (
    ArchiveReport,
    preview_archive_state,
    remember_archive_state,
    spread_archive_state,
)
from .chat_state import StateUnusable, encode_state, load_state, save_state, state_path
from .filesystem import sweep_stale_stages
from .model import Plan, RunReceipt, SyncRequest


class ChatStateError(RuntimeError):
    """The chat state file cannot be trusted. Nothing was changed."""


@dataclass
class ChatRun:
    plan: Plan
    receipt: Optional[RunReceipt] = None
    problems: dict[str, int] = field(default_factory=dict)
    newly_enrolled: int = 0
    # Runs a killed process left open, closed before this run applied.
    recovered_runs: int = 0
    # Copies whose archive state changed to match the newest archive or unarchive.
    archive: Optional[ArchiveReport] = None


def plan_chat_sync(
    config,
    planner,
    *,
    prefer: Optional[str] = None,
    prefer_session: Optional[str] = None,
) -> ChatRun:
    """Plan without writing anything, including the state file."""

    state = _load(config)
    plan = planner.plan(
        SyncRequest(config), state=state, prefer=prefer, prefer_session=prefer_session
    )
    run = _summary(plan)
    if getattr(config, "sync_archive_state", False) and not plan.invalid_replicas:
        run.archive = preview_archive_state(
            config.state_dir, plan.context.targets, skip=_archive_skips(plan)
        )
    return run


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
    plan = planner.plan(
        SyncRequest(config), state=state, prefer=prefer, prefer_session=prefer_session
    )
    context = plan.context
    if plan.invalid_replicas:
        return _summary(plan)

    # Before anything is recorded: files a half-undone run left must not be
    # remembered as seen, or undoing that run would make them look lost.
    recovered_runs = engine.close_interrupted_runs()
    for snapshot in context.snapshots:
        state.sync.seen.setdefault(snapshot.key, set()).update(snapshot.records)
    if encode_state(state) != on_disk:
        # Durable before the first write: a run that dies must not forget what
        # it saw, or a chat Claude deletes later would look new and come back.
        save_state(path, state)
        on_disk = encode_state(state)

    archive = None
    if getattr(config, "sync_archive_state", False):
        archive, applied = spread_archive_state(
            config.state_dir, context.targets, engine, skip=_archive_skips(plan)
        )
        if applied:
            _remember_archive_writes(state, context, applied)
            if encode_state(state) != on_disk:
                save_state(path, state)
                on_disk = encode_state(state)
            # The plan was made from the files before; plan again from what is there now.
            plan = planner.plan(
                SyncRequest(config),
                state=state,
                prefer=prefer,
                prefer_session=prefer_session,
            )
            context = plan.context
            if plan.invalid_replicas:
                run = _summary(plan)
                run.archive = archive
                return run

    sweep_stale_stages(
        (target.path for target in context.targets.values()), time.time()
    )
    receipt = engine.apply(plan)
    _remember_placements(state, context, receipt)

    state.sync = _settle(state.sync, context.rescan())
    if receipt.status in ("committed", "noop"):
        state.last_success_ms = clock_ms()
    if encode_state(state) != on_disk:
        save_state(path, state)
    if archive is not None:
        remember_archive_state(config.state_dir, context.targets)
    run = _summary(plan)
    run.receipt = receipt
    run.recovered_runs = recovered_runs
    run.archive = archive
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


def _settle(sync, snapshots):
    from .rules import settle

    return settle(sync, snapshots)


def _summary(plan: Plan) -> ChatRun:
    run = ChatRun(plan)
    run.problems = dict(Counter(problem.kind for problem in plan.problems))
    run.newly_enrolled = len(plan.context.newly_enrolled)
    return run


def _archive_skips(plan: Plan) -> set[str]:
    """Chats the archive pass leaves alone: deleted somewhere, or a copy it cannot judge."""

    skips = {
        problem.session_id
        for problem in plan.problems
        if problem.kind in ("unreadable", "future")
    }
    for snapshot in plan.context.snapshots:
        skips.update(snapshot.tombstones)
    return skips


def _remember_archive_writes(state, context, applied) -> None:
    """A copy still as last synced stays so, with only its archive state changed.

    The chat sync then decides as it would have without the archive difference:
    the copy that really changed still wins on its own.
    """
    folders = {str(target.path): key for key, target in context.targets.items()}
    before = {snapshot.key: snapshot.records for snapshot in context.snapshots}
    for operation in applied:
        key = folders.get(str(Path(operation.destination).parent))
        copy = before.get(key, {}).get(operation.session_id)
        synced = state.sync.synced.get(key, {})
        if copy is not None and copy.state_hash is not None:
            if synced.get(operation.session_id) == copy.state_hash:
                synced[operation.session_id] = operation.source_state_hash


def _remember_placements(state, context, receipt: RunReceipt) -> None:
    folders = {str(target.path): key for key, target in context.targets.items()}
    for operation in receipt.applied:
        if operation.artifact != "record" or operation.kind not in (
            "create",
            "replace",
        ):
            continue
        key = folders.get(str(Path(operation.destination).parent))
        if key is None:
            continue
        if operation.source_state_hash is not None:
            # What sync placed is this folder's version in step with the others.
            state.sync.synced.setdefault(key, {})[operation.session_id] = (
                operation.source_state_hash
            )
        if operation.kind == "create":
            state.sync.seen.setdefault(key, set()).add(operation.session_id)
