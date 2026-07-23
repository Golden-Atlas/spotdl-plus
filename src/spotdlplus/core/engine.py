'''
engine.py - stage driver and retry handling

A stage does one thing. It says what state it takes, what state it produces,
and has a run() that either returns fields or raises. It never retries, sleeps,
or touches the state machine.

The Engine handles the rest in one place: claim a batch, run it, advance teh
state, read the error's retry policy, then defer or fail or park. If every
stage had its own retry logic they would drift apart, so stages never see it.
'''

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from .backoff import backoff_delay, retry_after_delay
from .config import Config
from .errors import ErrorRecord, Retry, SpotdlPlusError
from .events import (
    EventBus,
    Failed,
    RateLimitHit,
    RunParked,
    RunThrottled,
    Stage,
    StageFinished,
    StageStarted,
    TrackStateChanged,
)
from .models import TrackState
from .store import Store, TrackRow


@dataclass(frozen=True, slots=True)
class Context:
    '''Everything a stage is allowed to reach for. Note the absence of a terminal.'''

    store: Store
    bus: EventBus
    config: Config
    run_id: str


class SkipTrack(Exception):
    '''
    Control flow, not failure. 'We already have this' or 'you pruned it'. Lands in
    SKIPPED and nobody counts it as an error, because it isn't one.
    '''

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@runtime_checkable
class PipelineStage(Protocol):
    '''The entire contract: 3 attributes and a method.'''

    name: ClassVar[Stage]
    consumes: ClassVar[TrackState]
    produces: ClassVar[TrackState]

    def run(self, ctx: Context, row: TrackRow) -> dict[str, Any]:
        '''
        Do the work. Return the columns to write, or raise and let the retry policy
        decide.
        '''
        ...


@dataclass(slots=True)
class StageStats:
    advanced: int = 0        # every stage transition (fetch, transcode, …)
    skipped: int = 0
    failed: int = 0
    deferred: int = 0
    #: tracks that crossed the FINISH line this session. Reached DONE. Distinct
    #: from `advanced`, which counts each of a tarck's ~5 stage hops: reporting
    #: `advanced` as "N ok" once announced a resume of ~230 tracks as "1155
    #: ok".
    completed: int = 0
    parked: bool = False

    def __add__(self, other: 'StageStats') -> 'StageStats':
        return StageStats(
            self.advanced + other.advanced, self.skipped + other.skipped,
            self.failed + other.failed, self.deferred + other.deferred,
            self.completed + other.completed,
            self.parked or other.parked,
        )


class Engine:
    '''Drives stages over a run. One instance per run. Safe across threads.'''

    #: Rate limiter, waits up to 5 seconds to stay responsive
    _MAX_NAP_S = 5.0

    #: Seconds between downloads once we're crawling. Long enough to matter to
    #: YouTube's rate counter, short enough taht a big run still finishes.
    _THROTTLE_PACE_S = 8.0

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self._park = threading.Event()
        self._park_reason = ''
        self._lock = threading.Lock()
        #: The same failure code in an unbroken row (any success resets it).
        #: Past config.mass_block_streak the run parks. One error repeating is
        #: a systemic problem, and grinding on just prints a wall of it.
        self._streak = 0
        self._streak_code = ''
        #: Halfway to the park, a rate-limit streak downshifts to a one-at-a-time
        #: paced crawl to try to slip under the limit before giving up entirely.
        #: A single success clears this and the streak, shifting back up to speed.
        self._throttled = False

    #: Stages where a repeated identical failure means the setup is broken, not
    #: teh track. Refusals in selection (MATCH_NONE on an obscure track) are
    #: per-track and legitimate, so the breaker leaves the earlier stages
    #: alone.
    _BREAKER_STAGES = frozenset({Stage.FETCH, Stage.TRANSCODE, Stage.TAG, Stage.PLACE})

    @property
    def _throttle_at(self) -> int:
        '''Blocks-in-a-row that engage teh crawl: halfway to the park, min 2.'''
        cap = self.ctx.config.mass_block_streak
        return max(2, cap // 2) if cap > 0 else 0

    # -- public -------------------------------------------------------------

    @property
    def parked(self) -> bool:
        return self._park.is_set()

    def drive(self, stage: PipelineStage) -> StageStats:
        '''Run one stage to exhaustion, or until the run parks.'''
        ctx = self.ctx
        total, _ = ctx.store.pending(ctx.run_id, [stage.consumes])
        ctx.bus.emit(StageStarted(run_id=ctx.run_id, stage=stage.name, total=total))

        stats = StageStats()
        while not self._park.is_set():
            rows = ctx.store.claim(
                ctx.run_id, [stage.consumes],
                limit=ctx.config.batch_size, lease_s=ctx.config.lease_s,
            )
            if not rows:
                remaining, wait = ctx.store.pending(ctx.run_id, [stage.consumes])
                if remaining == 0:
                    break
                # Checks if everything is actually done or if the tracks are
                # just waiting out a penalty, otherwise we can't tell the
                # difference adn would drop them.
                self._park.wait(min(max(wait, 0.05) + 0.01, self._MAX_NAP_S))
                continue
            stats = stats + self._run_batch(stage, rows)

        stats.parked = self._park.is_set()
        ctx.bus.emit(StageFinished(run_id=ctx.run_id, stage=stage.name, count=stats.advanced))
        return stats

    def drive_all(self, stages: list[PipelineStage]) -> StageStats:
        '''Walk the pipeline in order. A park stops the whole thing, intact.'''
        total = StageStats()
        for stage in stages:
            total = total + self.drive(stage)
            if self._park.is_set():
                break
        return total

    # -- internals ----------------------------------------------------------

    def _run_batch(self, stage: PipelineStage, rows: list[TrackRow]) -> StageStats:
        with self._lock:
            throttled = self._throttled
        if throttled:
            return self._run_batch_throttled(stage, rows)
        workers = max(1, min(self.ctx.config.concurrency, len(rows)))
        stats = StageStats()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='spdl') as pool:
            futures = [pool.submit(self._run_one, stage, row) for row in rows]
            for fut in as_completed(futures):
                stats = stats + fut.result()   # _run_one never raises
        return stats

    def _run_batch_throttled(self, stage: PipelineStage, rows: list[TrackRow]) -> StageStats:
        '''
        The crawl gear: one track at a time, spaced out, so we stop looking like a
        scraper. One success clears the throttle and the next claim runs full speed.
        '''
        stats = StageStats()
        for i, row in enumerate(rows):
            if self._park.is_set():
                self.ctx.store.release(row.id)
                continue
            stats = stats + self._run_one(stage, row)
            with self._lock:
                still = self._throttled
            if still and i < len(rows) - 1:
                self._park.wait(self._THROTTLE_PACE_S)   # interruptible cool-down
        return stats

    def _run_one(self, stage: PipelineStage, row: TrackRow) -> StageStats:
        ctx = self.ctx
        if self._park.is_set():
            ctx.store.release(row.id)
            return StageStats()

        try:
            fields = stage.run(ctx, row)
        except SkipTrack as skip:
            # The reason rides along. 'owned' is how plan summaries and status
            # tell an already-have apart from a prune.
            ctx.store.advance(row.id, TrackState.SKIPPED, skip_reason=skip.reason)
            self._emit_move(row, TrackState.SKIPPED)
            return StageStats(skipped=1)
        except SpotdlPlusError as err:
            return self._handle(stage, row, err)
        except Exception as exc:  # noqa: BLE001  # An unclassified escape is our bug
            rec = ErrorRecord(
                code='ERR_UNEXPECTED',
                message=f'{type(exc).__name__}: {exc}',
                retry=Retry.NEVER,
                remedy=_UNEXPECTED_REMEDY,
                context={'stage': str(stage.name), 'traceback': traceback.format_exc()},
            )
            ctx.store.fail(row.id, rec)
            ctx.bus.emit(Failed(run_id=ctx.run_id, error=rec, track_id=row.id,
                                stage=stage.name, attempt=row.attempts + 1, will_retry=False))
            return StageStats(failed=1)

        ctx.store.advance(row.id, stage.produces, **fields)
        # Any forward progress means the failure wasn't wholesale. Reset the
        # streak so a run that's mostly succeeding never trips the breaker, and
        # shift back up out of the crawl if we were in it.
        if self._streak or self._throttled:
            with self._lock:
                recovered = self._throttled
                self._streak = 0
                self._streak_code = ''
                self._throttled = False
            if recovered:
                ctx.bus.emit(RunThrottled(run_id=ctx.run_id, active=False,
                                          streak=0, pace_s=0.0))
        self._emit_move(row, stage.produces)
        # A track only truly finishes when it reaches the terminal DONE state
        # (the verify stage produces it). every earlier hop is progress, not a
        # completion. This is teh honest "N ok" for a session.
        done = 1 if stage.produces is TrackState.DONE else 0
        return StageStats(advanced=1, completed=done)

    def _trip_park(self, reason: str, *, resume_hint: str = 'spotdlp resume') -> None:
        '''Freeze the run once, queue intact. Idempotent across threads.'''
        ctx = self.ctx
        with self._lock:
            if self._park.is_set():
                return
            self._park_reason = reason
            self._park.set()
        ctx.store.set_run_status(ctx.run_id, 'parked', reason)
        ctx.bus.emit(RunParked(run_id=ctx.run_id, reason=reason, resume_hint=resume_hint))

    def _handle(self, stage: PipelineStage, row: TrackRow, err: SpotdlPlusError) -> StageStats:
        '''Read the policy off the error. The stage never learns any of this happened.'''
        ctx = self.ctx
        rec = err.record()
        attempt = row.attempts + 1

        # PARK: not the track's fault, and not fixable by trying harder. Freeze
        # the run with its queue intact. No attempt is burned.
        if err.retry is Retry.PARK:
            ctx.store.release(row.id)
            self._trip_park(err.message)
            ctx.bus.emit(Failed(run_id=ctx.run_id, error=rec, track_id=row.id,
                                stage=stage.name, attempt=row.attempts, will_retry=True))
            return StageStats(parked=True)

        # The same download-side failure over and over, nothing getting
        # through: that's the setup, not the tracks. Park with the queue intact
        # rather than print one identical error per remaining track. Any
        # success resets the streak, so this only fires on a real stall.
        if (ctx.config.mass_block_streak > 0
                and stage.name in self._BREAKER_STAGES):
            with self._lock:
                if err.code == self._streak_code:
                    self._streak += 1
                else:
                    self._streak_code = err.code
                    self._streak = 1
                streak = self._streak
                tripped = streak >= ctx.config.mass_block_streak
                # A rate-limit streak gets the crawl gear halfway to the park. A
                # slower request rate can genuinely slip under the limit. Other
                # causes (cookies, a broken tool) don't improve with waiting, so
                # they head straight for the park.
                engage = (err.code == 'FETCH_BLOCKED' and not self._throttled
                          and not tripped and streak >= self._throttle_at)
                if engage:
                    self._throttled = True
            if engage:
                ctx.bus.emit(RunThrottled(run_id=ctx.run_id, active=True,
                                          streak=streak, pace_s=self._THROTTLE_PACE_S))
            if tripped:
                ctx.store.release(row.id)   # hand the track back untouched
                if err.code == 'FETCH_BLOCKED':
                    self._trip_park(
                        f'YouTube is rate-limiting this IP. {streak} downloads blocked '
                        f'in a row with nothing getting through. Parked with the queue '
                        f'intact. Nothing is lost.',
                        resume_hint='wait ~30 min, then: spotdlp resume --retry-failed '
                                    '(or set youtube_cookies_from_browser in config)')
                else:
                    self._trip_park(
                        f'every download is failing the same way. {streak} in a row '
                        f'with [{err.code}], so it\'s the setup, not the tracks. '
                        f'{rec.remedy}',
                        resume_hint='fix the above, then: spotdlp resume --retry-failed')
                ctx.bus.emit(Failed(run_id=ctx.run_id, error=rec, track_id=row.id,
                                    stage=stage.name, attempt=row.attempts, will_retry=True))
                return StageStats(parked=True)

        exhausted = attempt >= ctx.config.max_attempts
        if err.retry is Retry.NEVER or exhausted:
            ctx.store.fail(row.id, rec)
            ctx.bus.emit(Failed(run_id=ctx.run_id, error=rec, track_id=row.id,
                                stage=stage.name, attempt=attempt, will_retry=False))
            return StageStats(failed=1)

        delay = self._delay_for(err, attempt)
        if err.code == 'RATE_LIMITED':
            # Surfaced on purpose. Silent throttling is how a tool earns a
            # reputation for being 'randomly slow' instead of 'correctly polite'.
            ctx.bus.emit(RateLimitHit(run_id=ctx.run_id,
                                      host=str(err.context.get('host', '?')), wait_s=delay))

        # Rewind to the state this stage consumes, and hold the lease into the
        # future. The track vanishes from the pool until its penalty elapses.
        ctx.store.fail(row.id, rec, retryable_to=stage.consumes, defer_s=delay)
        ctx.bus.emit(Failed(run_id=ctx.run_id, error=rec, track_id=row.id,
                            stage=stage.name, attempt=attempt, will_retry=True))
        return StageStats(deferred=1)

    def _delay_for(self, err: SpotdlPlusError, attempt: int) -> float:
        cfg = self.ctx.config
        if err.retry is Retry.NOW:
            return 0.0
        if err.retry is Retry.AFTER:
            return retry_after_delay(err.retry_after, fallback_attempt=attempt)
        return backoff_delay(attempt, base_s=cfg.backoff_base_s, cap_s=cfg.backoff_cap_s)

    def _emit_move(self, row: TrackRow, to: TrackState) -> None:
        self.ctx.bus.emit(TrackStateChanged(
            run_id=self.ctx.run_id, track_id=row.id, title=row.title,
            album=row.album_title or '',
            old_state=str(row.state), new_state=str(to),
        ))


_UNEXPECTED_REMEDY = (
    'An exception escaped a stage without being classified. That\'s our bug, not '
    'yours. The run kept going and this one track is marked FAILED. The traceback '
    'is in the error context. Please file it.'
)
