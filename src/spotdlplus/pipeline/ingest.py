'''
ingest.py - queue loading and duplicate removal

Two tiers for the 2 major issues.

Tier 1 is very exact. The same recording showing up twice shares an ISRC, so it
collides on a UNIQUE constraint in the schema and so nothing has to run later
to clean it.

Tier 2 is more judgement-based. A remaster is typically a very different
recording of the same song, so it gets its own ISRC and they both get clustered
together until you pick whcih one to keep.
'''

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..core.events import DuplicatesCollapsed, EventBus, Stage, StageFinished, StageStarted, Warned
from ..core.models import MasterPreference, Track, TrackState
from ..core.store import Store
from ..core.works import group_works, pick_master

SKIP_OWNED = 'owned'
SKIP_DUPLICATE = 'duplicate:work'


@dataclass(frozen=True, slots=True)
class IngestStats:
    '''What teh stream turned into. Every number here is a question someone asks.'''

    seen: int = 0            # tracks the expander produced
    inserted: int = 0        # distinct recordings that reached the queue
    duplicate_isrc: int = 0  # tier 1: the same recording, twice
    already_owned: int = 0   # in your library from a previous run
    healed: int = 0          # was "owned" but the file is gone. Requeued

    @property
    def queued(self) -> int:
        return self.inserted - self.already_owned


def ingest(
    tracks: Iterable[Track],
    store: Store,
    run_id: str,
    *,
    bus: EventBus,
    skip_owned: bool = True,
) -> IngestStats:
    '''
    Drain the expander into the store. Streaming, bounded, idempotent.

    Re-running over the same source is safe: identical recordings collide on the
    primary key and anything already owned is marked skipped rather than
    re-downloaded. That's the answer to 'it re-downloads my whole playlist when I
    add one song'.
    '''
    bus.emit(StageStarted(run_id=run_id, stage=Stage.RESOLVE))

    seen = inserted = dupes = owned = healed = 0
    for track in tracks:
        seen += 1
        track_id, was_new = store.add_track(run_id, track)

        if not was_new:
            dupes += 1
            bus.emit(DuplicatesCollapsed(
                run_id=run_id, kept=track_id, dropped=(track.identity,), basis='isrc',
            ))
            continue

        inserted += 1
        if skip_owned:
            owned_row = store.own(track.identity)
            if owned_row is not None:
                # Ownership is a promise about a FILE, so the file gets a say.
                # A human deleting tracks outside the tool is a legitimate way
                # to curate. A database that keeps skipping against the ghost
                # of a deleted file can never heal. Verify, then believe.
                if Path(owned_row['final_path']).is_file():
                    owned += 1
                    store.mark_skipped(track_id, SKIP_OWNED)
                else:
                    store.revoke_ownership(track.identity)
                    healed += 1
                    bus.emit(Warned(
                        run_id=run_id,
                        message=f'{track.title!r} was owned but its file is gone '
                                f'from disk, re-downloading',
                        context={'identity': track.identity,
                                 'was_at': owned_row['final_path']},
                    ))

    bus.emit(StageFinished(run_id=run_id, stage=Stage.RESOLVE, count=inserted))
    return IngestStats(seen=seen, inserted=inserted, duplicate_isrc=dupes,
                       already_owned=owned, healed=healed)


def collapse_works(
    store: Store,
    run_id: str,
    *,
    preference: MasterPreference,
    bus: EventBus,
) -> int:
    '''
    Tier 2. Fold every reissue into the master the profile prefers, and return how
    many collapsed. Losers are marked SKIPPED with `duplicate:work` rather than
    deleted, so `explain` can say exactly which master won and why.
    '''
    if preference is MasterPreference.BOTH:
        return 0

    bus.emit(StageStarted(run_id=run_id, stage=Stage.DEDUPE))
    collapsed = 0

    for _key, group in store.iter_work_groups(run_id):
        if len(group) < 2:
            continue

        by_identity = {track.identity: track_id for track_id, track, _st in group}
        decided = {track.identity for _tid, track, st in group
                   if st is TrackState.SKIPPED}

        for cluster in group_works([track for _tid, track, _st in group]):
            if len(cluster) < 2:
                continue

            settled = [t for t in cluster if t.identity in decided]
            fresh = [t for t in cluster if t.identity not in decided]
            if settled:
                # The archive already has a master of this work, or already
                # ruled on one, so newcomers collapse into that decision.
                # Without it a re-run of a reissue-heavy artist slowly doubles
                # the library, whcih it did once with 11 works twice each.
                keeper, dropped = settled[0], fresh
            else:
                keeper, dropped = pick_master(cluster, preference)

            actually_dropped = []
            for loser in dropped:
                if loser.identity in decided:
                    continue   # already skipped. nothing to re-decide
                store.mark_skipped(by_identity[loser.identity], SKIP_DUPLICATE)
                collapsed += 1
                actually_dropped.append(loser)

            if actually_dropped:
                bus.emit(DuplicatesCollapsed(
                    run_id=run_id,
                    kept=keeper.identity,
                    dropped=tuple(d.identity for d in actually_dropped),
                    basis='work',
                ))

    bus.emit(StageFinished(run_id=run_id, stage=Stage.DEDUPE, count=collapsed))
    return collapsed
