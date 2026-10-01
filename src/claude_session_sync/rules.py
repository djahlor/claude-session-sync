"""Decide what one chat sync run should do, and what to remember after it. Pure.

Ported from vinlim/claude-desktop-sync (0BSD). Rule numbers follow its DESIGN.md:
R3 one side changed, R4 both changed, R5 new records, R6 deletes, R7 lost
records, R11 unreadable or future copies. R8, kept copies, is the run journal,
which keeps every file a run replaces. R9, live partitions, does not apply
here, because every sync runs with Claude closed.
Actions for one session are emitted in the order they must happen.
"""

from typing import List, Optional

from .chat_model import (
    CreateRecord,
    CreateTombstone,
    Problem,
    ReplaceRecord,
    RetireRecord,
    RetireTmp,
    RetireTombstone,
    RulePlan,
    Snapshot,
    SyncState,
)


def plan(
    snapshots: List[Snapshot],
    state: SyncState,
    prefer: Optional[str] = None,
    prefer_session: Optional[str] = None,
) -> RulePlan:
    """prefer settles ties for one partition, for one session if prefer_session is set."""

    result = RulePlan()
    session_ids = set()
    for snapshot in snapshots:
        session_ids.update(snapshot.records, snapshot.tombstones, snapshot.orphan_tmps)
    for session_id in sorted(session_ids):
        preferred = prefer if prefer_session in (None, session_id) else None
        _plan_session(session_id, snapshots, state, preferred, result)
    return result


def _plan_session(session_id, snapshots, state, prefer, result) -> None:
    holders = [s for s in snapshots if session_id in s.records]
    unusable = [s for s in holders if not s.records[session_id].usable]
    if unusable:  # R11: nothing about this session can be judged
        result.problems.extend(
            Problem(
                "unreadable" if not s.records[session_id].readable else "future",
                session_id,
                s.key,
            )
            for s in unusable
        )
        return

    entombed = [s for s in snapshots if session_id in s.tombstones]
    if not entombed:
        if holders:
            _plan_record(
                session_id, snapshots, holders, state, prefer, result, explained=set()
            )
        return

    if holders and _used_after_delete(session_id, holders, entombed):
        # Only a folder holding the marker has an explained absence. A folder
        # that lost the chat without one is still reported as lost.
        chosen = _plan_record(
            session_id, snapshots, holders, state, prefer, result,
            explained={s.key for s in entombed},
        )
        # With the copies tied nothing can be put back, so the tombstone stays.
        # Without it, the deleting partition would read as having lost the record.
        if chosen:
            result.actions.extend(
                RetireTombstone(session_id, target=snapshot.key) for snapshot in entombed
            )
        return

    _plan_delete(session_id, snapshots, entombed[0], result)


def _used_after_delete(session_id, holders, entombed) -> bool:
    """R6: time alone decides. Claude stamps a re-adopted session with the current time."""

    deleted_at = max(s.tombstones[session_id] for s in entombed)
    return max(s.records[session_id].last_activity_at for s in holders) > deleted_at


def _plan_delete(session_id, snapshots, tombstone_source, result) -> None:
    for snapshot in snapshots:
        if session_id in snapshot.records:
            result.actions.append(RetireRecord(session_id, target=snapshot.key))
        elif session_id in snapshot.orphan_tmps:
            result.actions.append(RetireTmp(session_id, target=snapshot.key))
        if session_id not in snapshot.tombstones:
            result.actions.append(
                CreateTombstone(session_id, source=tombstone_source.key, target=snapshot.key)
            )


def _plan_record(session_id, snapshots, holders, state, prefer, result, explained) -> bool:
    """Return whether a version was chosen."""

    untouched = [s for s in holders if _still_as_synced(s, session_id, state)]
    if len({s.records[session_id].state_hash for s in holders}) == 1:
        winner = holders[0]
    elif all(_known(s, session_id, state) for s in holders):
        winner = _one_sided_winner(session_id, holders, untouched) or _latest_activity_winner(
            session_id, holders, prefer
        )
    else:
        # A copy with no remembered version cannot be told apart from a stale
        # one, so only activity decides, and a tie is left alone.
        winner = _latest_activity_winner(session_id, holders, prefer)
    if winner is None:  # R4: a tie nobody settled
        result.problems.extend(
            Problem("tied", session_id, s.key) for s in _most_active(session_id, holders)
        )
        return False

    winning = winner.records[session_id]
    for target in holders:
        if target.records[session_id].state_hash != winning.state_hash:
            result.actions.append(ReplaceRecord(session_id, source=winner.key, target=target.key))

    for target in snapshots:
        if session_id in target.records:
            continue
        if target.key not in explained and session_id in state.seen.get(target.key, ()):  # R7
            result.problems.append(Problem("lost", session_id, target.key))
            continue
        result.actions.append(CreateRecord(session_id, source=winner.key, target=target.key))  # R5
    return True


def _still_as_synced(snapshot, session_id, state) -> bool:
    """The copy is still the version this folder last held in step with the others."""

    remembered = state.synced.get(snapshot.key, {}).get(session_id)
    return remembered is not None and snapshot.records[session_id].state_hash == remembered


def _known(snapshot, session_id, state) -> bool:
    return session_id in state.synced.get(snapshot.key, {})


def _one_sided_winner(session_id, holders, untouched) -> Optional[Snapshot]:
    """R3: every copy that moved on moved to the same state, and that state is not behind.

    A copy can also leave the synced state by going back in time (a restored
    backup, a promoted temp file, a login flushing stale memory). Its activity is
    then lower than what it would replace, and the case falls through to R4.
    """

    changed = [s for s in holders if s not in untouched]
    if len({s.records[session_id].state_hash for s in changed}) != 1:
        return None
    candidate = changed[0]
    behind = any(
        candidate.records[session_id].last_activity_at < s.records[session_id].last_activity_at
        for s in untouched
    )
    return None if behind else candidate


def _most_active(session_id, holders) -> List[Snapshot]:
    latest = max(s.records[session_id].last_activity_at for s in holders)
    return [s for s in holders if s.records[session_id].last_activity_at == latest]


def _latest_activity_winner(session_id, holders, prefer) -> Optional[Snapshot]:
    """R4: later activity wins. Equal activity on different states is a tie."""

    leaders = _most_active(session_id, holders)
    if len({s.records[session_id].state_hash for s in leaders}) == 1:
        return leaders[0]
    return next((s for s in leaders if s.key == prefer), None)


def settle(state: SyncState, snapshots: List[Snapshot]) -> SyncState:
    """Work out what to remember from what the given folders hold now.

    Folders outside this run keep what they had. An id leaves ``seen`` only when
    none of these folders holds the record any more: a record that was there
    and is gone again was removed by Claude, and R7 must know it was there.
    Agreement needs at least two folders; one folder alone agrees with nothing.
    """

    synced = {key: dict(entries) for key, entries in state.synced.items()}
    seen = {key: set(ids) for key, ids in state.seen.items()}
    keys = [s.key for s in snapshots]
    for snapshot in snapshots:
        seen.setdefault(snapshot.key, set()).update(snapshot.records)

    known_ids = set()
    for snapshot in snapshots:
        known_ids.update(snapshot.records, seen[snapshot.key], synced.get(snapshot.key, {}))

    for session_id in known_ids:
        holders = [s for s in snapshots if session_id in s.records]
        if not holders:
            # Gone everywhere. Forgetting it keeps a later re-adoption from looking lost.
            for key in keys:
                seen[key].discard(session_id)
                synced.get(key, {}).pop(session_id, None)
        elif len(snapshots) >= 2 and len(holders) == len(snapshots):
            # Agreement is about content alone, so a copy with an untrustworthy time still counts.
            hashes = {s.records[session_id].state_hash for s in holders}
            if len(hashes) == 1 and None not in hashes:
                agreed = hashes.pop()
                for key in keys:
                    synced.setdefault(key, {})[session_id] = agreed

    return SyncState(
        synced={key: entries for key, entries in synced.items() if entries},
        seen=seen,
    )
