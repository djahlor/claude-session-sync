"""Decide what one chat sync run should do, and what to remember after it. Pure.

Ported from vinlim/claude-desktop-sync (0BSD). Rule numbers follow its DESIGN.md:
R3 one side changed, R4 both changed, R5 new records, R6 deletes, R7 lost
records, R8 kept copies, R9 live partitions, R11 unreadable or future copies.
Actions for one session are emitted in the order they must happen.
"""

from typing import Dict, List, Optional, Set

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
    live: Set[str],
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
        _plan_session(session_id, snapshots, state, live, preferred, result)
    return result


def _plan_session(session_id, snapshots, state, live, prefer, result) -> None:
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
                session_id, snapshots, holders, state, live, prefer, result,
                absence_explained=False,
            )
        return

    if holders and _used_after_delete(session_id, holders, entombed):
        chosen = _plan_record(
            session_id, snapshots, holders, state, live, prefer, result,
            absence_explained=True,
        )
        # With the copies tied nothing can be put back, so the tombstone stays.
        # Without it, the deleting partition would read as having lost the record.
        if chosen:
            for snapshot in entombed:
                _unless_live(
                    snapshot, session_id, live, result,
                    RetireTombstone(session_id, target=snapshot.key),
                )
        return

    _plan_delete(session_id, snapshots, entombed[0], live, result)


def _used_after_delete(session_id, holders, entombed) -> bool:
    """R6: time alone decides. Claude stamps a re-adopted session with the current time."""

    deleted_at = max(s.tombstones[session_id] for s in entombed)
    return max(s.records[session_id].last_activity_at for s in holders) > deleted_at


def _plan_delete(session_id, snapshots, tombstone_source, live, result) -> None:
    for snapshot in snapshots:
        record_remains = False
        if session_id in snapshot.records:
            record_remains = not _unless_live(
                snapshot, session_id, live, result,
                RetireRecord(session_id, target=snapshot.key),
            )
        elif session_id in snapshot.orphan_tmps:
            _unless_live(
                snapshot, session_id, live, result,
                RetireTmp(session_id, target=snapshot.key),
            )
        if session_id not in snapshot.tombstones and not record_remains:
            result.actions.append(
                CreateTombstone(session_id, source=tombstone_source.key, target=snapshot.key)
            )


def _unless_live(snapshot, session_id, live, result, action) -> bool:
    """R9: an existing file in a live partition is left alone."""

    if snapshot.key in live:
        result.problems.append(Problem("live", session_id, snapshot.key))
        return False
    result.actions.append(action)
    return True


def _plan_record(
    session_id, snapshots, holders, state, live, prefer, result, absence_explained
) -> bool:
    """Return whether a version was chosen."""

    untouched = [s for s in holders if _still_as_synced(s, session_id, state)]
    winner = _one_sided_winner(session_id, holders, untouched) or _latest_activity_winner(
        session_id, holders, prefer
    )
    if winner is None:  # R4: a tie nobody settled
        result.problems.extend(
            Problem("tied", session_id, s.key) for s in _most_active(session_id, holders)
        )
        return False

    winning = winner.records[session_id]
    for target in holders:
        held = target.records[session_id]
        if held.state_hash != winning.state_hash:
            # R8: only a copy still as synced and strictly behind holds nothing unique.
            superseded = target in untouched and winning.last_activity_at > held.last_activity_at
            _unless_live(
                target, session_id, live, result,
                ReplaceRecord(session_id, source=winner.key, target=target.key, keep=not superseded),
            )

    for target in snapshots:
        if session_id in target.records:
            continue
        if not absence_explained and session_id in state.seen.get(target.key, ()):  # R7
            result.problems.append(Problem("lost", session_id, target.key))
            continue
        result.actions.append(CreateRecord(session_id, source=winner.key, target=target.key))  # R5
    return True


def _still_as_synced(snapshot, session_id, state) -> bool:
    """The copy is the agreed state, or a version sync placed there and Claude left alone."""

    held = snapshot.records[session_id].state_hash
    return held == state.agreed.get(session_id) or held == state.placed.get(
        snapshot.key, {}
    ).get(session_id)


def _one_sided_winner(session_id, holders, untouched) -> Optional[Snapshot]:
    """R3: every copy that moved on moved to the same state, and that state is not behind.

    A copy can also leave the synced state by going back in time (a restored
    backup, a promoted temp file, a login flushing stale memory). Its activity is
    then lower than what it would replace, and the case falls through to R4.
    """

    if len({s.records[session_id].state_hash for s in holders}) == 1:
        return holders[0]
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
    """Work out what to remember from what the partitions hold now.

    An id leaves ``seen`` only when no partition holds the record any more. A
    record that was there and is gone again was removed by Claude, and R7 must
    know it was there.
    """

    agreed = dict(state.agreed)
    seen = {s.key: set(state.seen.get(s.key, ())) | set(s.records) for s in snapshots}

    known_ids = set(agreed)
    for snapshot in snapshots:
        known_ids.update(snapshot.records, seen[snapshot.key])

    for session_id in known_ids:
        holders = [s for s in snapshots if session_id in s.records]
        if not holders:
            agreed.pop(session_id, None)
            for ids in seen.values():
                ids.discard(session_id)
        elif len(holders) == len(snapshots):
            hashes = {s.records[session_id].state_hash for s in holders}
            if len(hashes) == 1 and None not in hashes:
                agreed[session_id] = hashes.pop()

    return SyncState(agreed=agreed, seen=seen, placed=_still_in_place(state, snapshots, agreed))


def _still_in_place(state, snapshots, agreed) -> Dict[str, Dict[str, str]]:
    """A placement is worth remembering only while untouched and not yet agreed."""

    kept = {}
    for snapshot in snapshots:
        entries = {
            session_id: placed
            for session_id, placed in state.placed.get(snapshot.key, {}).items()
            if session_id in snapshot.records
            and snapshot.records[session_id].state_hash == placed
            and agreed.get(session_id) != placed
        }
        if entries:
            kept[snapshot.key] = entries
    return kept
