'''
test_engine.py - the retry policy, held to account.

A stage never implements retries. So every retry behaviour in the program is
provable right here, once, with fake stages that do nothing but raise on cue.
Get this file right and no future stage can get backoff wrong, because no future
stage will contain any backoff.
'''

from __future__ import annotations

from dataclasses import replace

import pytest

from spotdlplus.core import errors as E
from spotdlplus.core.config import Config
from spotdlplus.core.engine import Context, Engine, SkipTrack
from spotdlplus.core.events import EventBus, RateLimitHit, RunParked, Stage
from spotdlplus.core.models import ArtistRef, Track, TrackState
from spotdlplus.core.store import Store


# ----------------------------------------------------------------------------
# harness
# ----------------------------------------------------------------------------

FAST = dict(backoff_base_s=0.001, backoff_cap_s=0.01, batch_size=4, concurrency=2)


@pytest.fixture
def ctx(tmp_path):
    store = Store(tmp_path / 'jobs.db')
    cfg = replace(Config(), output_dir=tmp_path / 'out', **FAST)
    run_id = store.create_run('test', 'canonical')
    yield Context(store=store, bus=EventBus(), config=cfg, run_id=run_id)
    store.close()


def seed(ctx, n: int = 1) -> list[str]:
    ids = []
    for i in range(n):
        t = Track(title=f'S{i}', artists=(ArtistRef(name='A'),), isrc=f'ISRC{i}')
        tid, _ = ctx.store.add_track(ctx.run_id, t)
        ids.append(tid)
    return ids


def collect(bus) -> list:
    seen: list = []
    bus.subscribe(seen.append)
    return seen


class _Stage:
    '''Base for the fakes. Consumes DISCOVERED, produces ENRICHED.'''

    name = Stage.ENRICH
    consumes = TrackState.DISCOVERED
    produces = TrackState.ENRICHED


class Succeeds(_Stage):
    def run(self, ctx, row):
        return {'isrc': 'USRC17607839'}


class Skips(_Stage):
    def run(self, ctx, row):
        raise SkipTrack('already in the library')


class AlwaysFatal(_Stage):
    def run(self, ctx, row):
        raise E.NoAcceptableMatch('nothing cleared the floor')


class AlwaysTransient(_Stage):
    def run(self, ctx, row):
        raise E.DownloadFailed('stream died')


class FlakyOnce(_Stage):
    '''Fails the first attempt per track, then succeeds. The common real case.'''

    def __init__(self):
        self.seen: set[str] = set()

    def run(self, ctx, row):
        if row.id not in self.seen:
            self.seen.add(row.id)
            raise E.DownloadFailed('transient')
        return {}


class RateLimits(_Stage):
    def __init__(self):
        self.hits = 0

    def run(self, ctx, row):
        self.hits += 1
        if self.hits == 1:
            raise E.RateLimited('slow down', context={'host': 'api.spotify.com'}, retry_after=0.01)
        return {}


class GoesOffline(_Stage):
    def run(self, ctx, row):
        raise E.Offline('no route to host')


class Explodes(_Stage):
    def run(self, ctx, row):
        raise ValueError('an unclassified escape')


# ----------------------------------------------------------------------------
# the happy path
# ----------------------------------------------------------------------------

def test_a_successful_stage_advances_and_writes_its_fields(ctx):
    (tid,) = seed(ctx)
    stats = Engine(ctx).drive(Succeeds())

    assert stats.advanced == 1 and stats.failed == 0
    row = ctx.store.get_track(tid)
    assert row.state is TrackState.ENRICHED
    assert row.isrc == 'USRC17607839'


def test_a_stage_can_skip_without_being_an_error(ctx):
    (tid,) = seed(ctx)
    stats = Engine(ctx).drive(Skips())

    assert stats.skipped == 1 and stats.failed == 0
    assert ctx.store.get_track(tid).state is TrackState.SKIPPED


def test_the_engine_drains_every_track(ctx):
    seed(ctx, 9)   # more than batch_size, to force several claim rounds
    stats = Engine(ctx).drive(Succeeds())
    assert stats.advanced == 9
    assert ctx.store.counts(ctx.run_id) == {'enriched': 9}


# ----------------------------------------------------------------------------
# retry policy: the whole reason this class exists
# ----------------------------------------------------------------------------

def test_a_terminal_error_fails_once_and_keeps_its_remedy(ctx):
    (tid,) = seed(ctx)
    stats = Engine(ctx).drive(AlwaysFatal())

    assert stats.failed == 1 and stats.deferred == 0
    row = ctx.store.get_track(tid)
    assert row.state is TrackState.FAILED
    assert row.attempts == 1, 'NEVER means never. do not burn three attempts on it'

    (fail,) = ctx.store.failures(ctx.run_id)
    assert fail['code'] == 'MATCH_NONE'
    assert 'relink' in fail['remedy']


def test_a_transient_error_is_retried_then_succeeds(ctx):
    (tid,) = seed(ctx)
    stats = Engine(ctx).drive(FlakyOnce())

    assert stats.advanced == 1 and stats.deferred == 1
    row = ctx.store.get_track(tid)
    assert row.state is TrackState.ENRICHED
    assert row.attempts == 1, 'the failed attempt is remembered'


def test_retries_are_bounded_by_max_attempts(ctx):
    ctx = replace(ctx, config=replace(ctx.config, max_attempts=3))
    (tid,) = seed(ctx)
    stats = Engine(ctx).drive(AlwaysTransient())

    assert stats.deferred == 2 and stats.failed == 1   # try, try, give up
    row = ctx.store.get_track(tid)
    assert row.state is TrackState.FAILED
    assert row.attempts == 3


def test_the_engine_waits_out_deferred_work_instead_of_dropping_it(ctx):
    '''
    A claim that returns nothing does not mean the stage is done. It can mean
    every remaining track is serving a penalty. Exiting there would silently
    lose them, which is exactly the class of bug we are here to not have.
    '''
    seed(ctx, 5)
    stats = Engine(ctx).drive(FlakyOnce())
    assert stats.advanced == 5
    assert ctx.store.pending(ctx.run_id, [TrackState.DISCOVERED])[0] == 0


def test_rate_limiting_is_visible_and_obeys_retry_after(ctx):
    seed(ctx)
    seen = collect(ctx.bus)
    stats = Engine(ctx).drive(RateLimits())

    assert stats.advanced == 1
    hits = [e for e in seen if isinstance(e, RateLimitHit)]
    assert len(hits) == 1
    assert hits[0].host == 'api.spotify.com'
    assert hits[0].wait_s == pytest.approx(0.01, abs=1e-6), 'the server named the delay'


# ----------------------------------------------------------------------------
# parking: the answer to "it breaks on internet issues"
# ----------------------------------------------------------------------------

def test_going_offline_parks_the_run_with_its_queue_intact(ctx):
    ids = seed(ctx, 4)
    seen = collect(ctx.bus)
    engine = Engine(ctx)
    stats = engine.drive(GoesOffline())

    assert engine.parked and stats.parked
    assert stats.failed == 0, 'parking is not failing'

    parks = [e for e in seen if isinstance(e, RunParked)]
    assert len(parks) == 1 and parks[0].resume_hint == 'spotdlp resume'

    for tid in ids:
        row = ctx.store.get_track(tid)
        assert row.state is TrackState.DISCOVERED, 'nothing was consumed'
        assert row.attempts == 0, 'being offline must not burn a life'

    assert ctx.store.resumable(ctx.run_id) == 4


def test_a_parked_run_resumes_exactly_where_it_stopped(ctx):
    seed(ctx, 3)
    Engine(ctx).drive(GoesOffline())          # the network dies
    assert ctx.store.resumable(ctx.run_id) == 3

    stats = Engine(ctx).drive(Succeeds())     # ...and comes back
    assert stats.advanced == 3
    assert ctx.store.counts(ctx.run_id) == {'enriched': 3}


# ----------------------------------------------------------------------------
# unclassified escapes are bugs, not user problems
# ----------------------------------------------------------------------------

def test_an_unexpected_exception_fails_one_track_not_the_run(ctx):
    seed(ctx, 3)
    seen = collect(ctx.bus)
    stats = Engine(ctx).drive(Explodes())

    assert stats.failed == 3 and not any(e for e in seen if isinstance(e, RunParked))
    fails = ctx.store.failures(ctx.run_id)
    assert {f['code'] for f in fails} == {'ERR_UNEXPECTED'}
    assert 'traceback' in fails[0]['context']
    assert 'our bug' in fails[0]['remedy']


def test_a_broken_subscriber_cannot_stop_the_pipeline(ctx):
    seed(ctx, 3)

    def explodes(_):
        raise RuntimeError('the renderer is on fire')

    ctx.bus.subscribe(explodes)
    stats = Engine(ctx).drive(Succeeds())
    assert stats.advanced == 3
    assert ctx.bus.dropped > 0


# ----------------------------------------------------------------------------
# the stage contract itself
# ----------------------------------------------------------------------------

def test_stages_never_reference_retries_or_sleeping():
    '''
    ARCHITECTURE.md §6 / §6a. If a stage ever grows its own backoff, the retry
    policy has two homes and they will drift. Checked against the real source tree.

    `net/` is exempt from the sleep ban because it is infrastructure and not a stage,
    but only for bounded pacing, which `test_net.py` pins separately. It is never
    exempt from the backoff ban: expensive waits belong to the Engine.
    '''
    import ast
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[1] / 'src' / 'spotdlplus'
    STAGE_LAYERS = {'providers', 'match', 'media', 'pipeline'}
    offenders: list[str] = []

    for py in pkg.rglob('*.py'):
        layer = py.parent.name
        if layer in {'core', 'cli'}:
            continue
        # net/ may pace. nobody may own retry timing but the Engine.
        banned = {'backoff_delay', 'retry_after_delay'}
        if layer in STAGE_LAYERS:
            banned |= {'sleep'}

        tree = ast.parse(py.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
                if nm in banned:
                    offenders.append(f'{py.relative_to(pkg)}:{node.lineno} calls {nm}()')

    assert not offenders, 'retry logic grew a second home:\n  ' + '\n  '.join(offenders)


# ----------------------------------------------------------------------------
# mass-block park: a run YouTube is wholesale bot-walling should freeze with its
# queue intact, not grind every track to FAILED
# ----------------------------------------------------------------------------

class AlwaysBlocked(_Stage):
    # Blocks happen at the download stage. The breaker is scoped to it, so the
    # fake must live there too to model the real thing.
    name = Stage.FETCH

    def run(self, ctx, row):
        raise E.SourceBlocked('youtube is bot-walling this IP')


class BlocksTwiceThenOk(_Stage):
    '''The first two calls (any track) block. Everything after succeeds.'''

    name = Stage.FETCH

    def __init__(self):
        self.calls = 0

    def run(self, ctx, row):
        self.calls += 1
        if self.calls <= 2:
            raise E.SourceBlocked('one-off age gate')
        return {}


def test_a_wall_of_blocks_parks_the_run_not_every_track(ctx):
    ctx = replace(ctx, config=replace(ctx.config, mass_block_streak=3))
    seed(ctx, 6)
    seen = collect(ctx.bus)
    engine = Engine(ctx)
    stats = engine.drive(AlwaysBlocked())

    assert engine.parked and stats.parked
    parked = [e for e in seen if isinstance(e, RunParked)]
    assert parked and 'rate-limit' in parked[0].reason.lower()
    # queue intact, nothing was burned to FAILED
    counts = ctx.store.counts(ctx.run_id)
    assert counts.get('failed', 0) == 0
    assert counts.get('discovered', 0) >= 1


def test_mass_block_disabled_never_parks(ctx):
    ctx = replace(ctx, config=replace(ctx.config, mass_block_streak=0))
    seed(ctx, 4)
    engine = Engine(ctx)
    engine.drive(AlwaysBlocked())
    assert not engine.parked
    # with the guard off, blocks just retry then fail honestly
    assert ctx.store.counts(ctx.run_id).get('failed', 0) == 4


def test_completed_counts_the_finish_line_not_every_stage_hop(ctx):
    # A track hops DISCOVERED -> ... -> DONE (seven moves). `advanced` counts all
    # seven per track. `completed` counts only the one that reaches DONE. Reporting
    # `advanced` as "N ok" is what turned ~230 finished tracks into "1155 ok".
    from spotdlplus.core.models import TrackState as T

    class _Hop(_Stage):
        def run(self, ctx, row):
            return {}

    def hop(consumes, produces):
        h = _Hop()
        h.consumes, h.produces = consumes, produces
        return h

    stages = [
        hop(T.DISCOVERED, T.ENRICHED), hop(T.ENRICHED, T.MATCHED),
        hop(T.MATCHED, T.FETCHED), hop(T.FETCHED, T.TRANSCODED),
        hop(T.TRANSCODED, T.TAGGED), hop(T.TAGGED, T.PLACED), hop(T.PLACED, T.DONE),
    ]
    seed(ctx, 5)
    stats = Engine(ctx).drive_all(stages)
    assert stats.completed == 5              # five tracks crossed the line
    assert stats.advanced == 5 * len(stages)  # 35 stage hops in total


class AlwaysCookiesUnreadable(_Stage):
    '''A whole-run-fatal setup problem at the download stage. Every track hits it.'''

    name = Stage.FETCH

    def run(self, ctx, row):
        raise E.CookiesUnreadable('could not read the browser cookies you configured')


class RefusesInSelection(_Stage):
    '''A legitimate per-track refusal, in a selection stage (not download-side).'''

    name = Stage.MATCH

    def run(self, ctx, row):
        raise E.NoAcceptableMatch('nothing cleared the floor')


def test_a_wall_of_the_same_download_error_parks_with_its_remedy(ctx):
    # Not just FETCH_BLOCKED: any download-side failure repeating identically is
    # systemic. The park must carry that code's own remedy, not a generic line.
    ctx = replace(ctx, config=replace(ctx.config, mass_block_streak=3))
    seed(ctx, 8)
    seen = collect(ctx.bus)
    engine = Engine(ctx)
    stats = engine.drive(AlwaysCookiesUnreadable())

    assert engine.parked and stats.parked
    parked = [e for e in seen if isinstance(e, RunParked)]
    assert parked and 'COOKIES_UNREADABLE' in parked[0].reason
    assert 'cookie' in parked[0].reason.lower()
    # at most threshold-1 tracks burned before it caught on, not all 8
    assert ctx.store.counts(ctx.run_id).get('failed', 0) == 2


def test_selection_refusals_never_trip_the_breaker(ctx):
    # Eight obscure tracks that can't be matched must each fail on their own. The
    # breaker is for the download side, not for legitimate per-track refusals.
    ctx = replace(ctx, config=replace(ctx.config, mass_block_streak=3))
    seed(ctx, 8)
    engine = Engine(ctx)
    engine.drive(RefusesInSelection())
    assert not engine.parked
    assert ctx.store.counts(ctx.run_id).get('failed', 0) == 8


def test_rate_limit_downshifts_to_a_crawl_before_parking(ctx):
    from spotdlplus.core.events import RunThrottled
    ctx = replace(ctx, config=replace(ctx.config, mass_block_streak=6))
    seed(ctx, 12)
    seen = collect(ctx.bus)
    engine = Engine(ctx)
    engine._THROTTLE_PACE_S = 0.0   # don't actually sleep 8s in a unit test
    engine.drive(AlwaysBlocked())

    engaged = [e for e in seen if isinstance(e, RunThrottled) and e.active]
    assert engaged, 'the crawl gear should engage before the park'
    assert engaged[0].streak == 3          # halfway to the park (max(2, 6//2))
    assert engine.parked                   # ...and still parks if the crawl can't break through


def test_the_crawl_lifts_once_a_download_gets_through(ctx):
    from spotdlplus.core.events import RunThrottled
    ctx = replace(ctx, config=replace(ctx.config, mass_block_streak=4,
                                      concurrency=1, batch_size=8))
    seed(ctx, 6)
    seen = collect(ctx.bus)
    engine = Engine(ctx)
    engine._THROTTLE_PACE_S = 0.0
    engine.drive(BlocksTwiceThenOk())

    actives = [e.active for e in seen if isinstance(e, RunThrottled)]
    assert True in actives and False in actives   # engaged, then lifted by a success
    assert not engine.parked
    # the two blocks eventually retry into success, nothing FAILED
    assert ctx.store.counts(ctx.run_id).get('failed', 0) == 0


def test_a_success_resets_the_streak_so_a_healthy_run_does_not_park(ctx):
    # serial, so the block/ok interleaving is deterministic
    ctx = replace(ctx, config=replace(ctx.config, mass_block_streak=3, concurrency=1))
    seed(ctx, 5)
    engine = Engine(ctx)
    engine.drive(BlocksTwiceThenOk())
    # streak peaks at 2 (two blocks), then a success resets it, never trips 3
    assert not engine.parked
    assert ctx.store.counts(ctx.run_id).get('failed', 0) == 0
