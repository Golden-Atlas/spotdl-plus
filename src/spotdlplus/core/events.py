'''
events.py - typed events and the bus

The core never prints. It emits events into a bus and forgets about them. The
CLI subscribes and renders, a GUI would subscribe and draw, and none of them
can see each other.

Handlers can't raise. If one does we swallow it adn keep going, because a
broken renderer shouldn't take down a 4,000 track run.
'''

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .errors import ErrorRecord


class Stage(StrEnum):
    '''Where in the pipeline a thing is happening.'''

    RESOLVE = 'resolve'
    EXPAND = 'expand'
    ENRICH = 'enrich'
    DEDUPE = 'dedupe'
    PLAN = 'plan'
    MATCH = 'match'
    FETCH = 'fetch'
    TRANSCODE = 'transcode'
    TAG = 'tag'
    PLACE = 'place'


# ----------------------------------------------------------------------------
# events. flat, frozen, serializable. no behavior.
# ----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Event:
    '''Base. Every event carries when it happened and which run it belongs to.'''

    run_id: str
    at: float = field(default_factory=time.time, kw_only=True)

    @property
    def kind(self) -> str:
        return type(self).__name__

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d['kind'] = self.kind
        return d


@dataclass(frozen=True, slots=True)
class RunStarted(Event):
    source: str = ''
    profile: str = ''


@dataclass(frozen=True, slots=True)
class RunFinished(Event):
    ok: int = 0
    failed: int = 0
    skipped: int = 0
    elapsed_s: float = 0.0


@dataclass(frozen=True, slots=True)
class RunParked(Event):
    '''
    Not a failure. The run froze itself with everything intact and can pick back
    up.
    '''

    reason: str = ''
    resume_hint: str = ''


@dataclass(frozen=True, slots=True)
class RunThrottled(Event):
    '''
    YouTube pushed back so the run slowed to one download at a time instead of
    parking. It says so out loud because a run that suddenly gets slow should
    explain itself. active=False means one got through and we sped back up.
    '''

    active: bool = True
    streak: int = 0
    pace_s: float = 0.0


@dataclass(frozen=True, slots=True)
class StageStarted(Event):
    stage: Stage = Stage.RESOLVE
    total: int | None = None


@dataclass(frozen=True, slots=True)
class StageFinished(Event):
    stage: Stage = Stage.RESOLVE
    count: int = 0


@dataclass(frozen=True, slots=True)
class EntityDiscovered(Event):
    '''An artist, album, or playlist came into view during expansion.'''

    entity_kind: str = ''
    entity_id: str = ''
    name: str = ''
    child_count: int = 0


@dataclass(frozen=True, slots=True)
class ReleaseFiltered(Event):
    '''
    A release the profile threw out, and why. This is an event instead of a log
    line because 'where did the live album go' deserves an answer.
    '''

    album_id: str = ''
    title: str = ''
    reason: str = ''   # 'appears_on' | 'type:single' | 'secondary:live'


@dataclass(frozen=True, slots=True)
class DuplicatesCollapsed(Event):
    '''The deluxe, the remaster, and the Japan bonus edition became one record.'''

    kept: str = ''
    dropped: tuple[str, ...] = ()
    basis: str = ''  # 'isrc' | 'release_group' | 'title_duration'


@dataclass(frozen=True, slots=True)
class PlanReady(Event):
    '''Everything is known and nothing has been downloaded. The size preview.'''

    track_count: int = 0
    est_bytes: int = 0
    est_bytes_known: int = 0   # how many tracks we have a real size for
    unmatched: int = 0
    already_have: int = 0
    #: tracks that will actually be attempted. Matched, not skipped, not
    #: refused. THE number a progress bar should count to. Deriving it from the
    #: others once produced a bar that ended at 45/47 forever.
    matched: int = 0


@dataclass(frozen=True, slots=True)
class TrackStateChanged(Event):
    track_id: str = ''
    title: str = ''
    #: two recordings can share a title (Cigarettes After Sex has two Sweets);
    #: teh album is what tells a human which one just moved
    album: str = ''
    old_state: str = ''
    new_state: str = ''


@dataclass(frozen=True, slots=True)
class TrackProgress(Event):
    '''Bytes moving. Emitted at a throttled rate. Never once per chunk.'''

    track_id: str = ''
    stage: Stage = Stage.FETCH
    done_bytes: int = 0
    total_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class MatchDecided(Event):
    '''Why we chose what we chose, so a mystery nightcore cover isn't a mystery.'''

    track_id: str = ''
    chosen_url: str = ''
    score: float = 0.0
    basis: str = ''  # 'isrc_exact' | 'scored'
    breakdown: dict[str, float] = field(default_factory=dict)
    runner_up_score: float | None = None
    considered: int = 0


@dataclass(frozen=True, slots=True)
class RateLimitHit(Event):
    '''Visible on purpose. Silent throttling is how tools feel 'randomly slow'.'''

    host: str = ''
    wait_s: float = 0.0


@dataclass(frozen=True, slots=True)
class AuthRefreshed(Event):
    provider: str = ''
    expires_in_s: float = 0.0


@dataclass(frozen=True, slots=True)
class Warned(Event):
    message: str = ''
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Failed(Event):
    '''A structured failure. Carries its own remedy. The renderer just shows it.'''

    error: ErrorRecord | None = None
    track_id: str | None = None
    stage: Stage | None = None
    attempt: int = 1
    will_retry: bool = False


# ----------------------------------------------------------------------------
# the bus
# ----------------------------------------------------------------------------

Handler = Callable[[Event], None]


class EventBus:
    '''
    Fans out synchronously under a lock. Handlers run on the emitting thread so
    they have to be fast and can't raise. A slow renderer is the renderer's
    problem.
    '''

    def __init__(self) -> None:
        self._handlers: dict[int, Handler] = {}
        self._lock = threading.RLock()
        self._next = 0
        self._dropped = 0

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        '''Register a handler. Returns the function that unregisters it.'''
        with self._lock:
            token = self._next
            self._next += 1
            self._handlers[token] = handler

        def unsubscribe() -> None:
            with self._lock:
                self._handlers.pop(token, None)

        return unsubscribe

    def emit(self, event: Event) -> None:
        '''Fan out. A handler that raises is isolated and counted, never fatal.'''
        with self._lock:
            handlers = list(self._handlers.values())
        for h in handlers:
            try:
                h(event)
            except Exception:  # noqa: BLE001  # A bad renderer must not kill a run
                self._dropped += 1

    @property
    def dropped(self) -> int:
        '''How many handler explosions we absorbed. Surfaced by `doctor`.'''
        return self._dropped


class NullBus(EventBus):
    '''For tests and library callers who don't care. Emits into the void.'''

    def emit(self, event: Event) -> None:  # noqa: D102
        return


def new_run_id() -> str:
    '''Short, sortable-enough, collision-free in any realistic universe.'''
    return uuid.uuid4().hex[:12]
