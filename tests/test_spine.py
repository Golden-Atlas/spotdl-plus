'''
test_spine.py - the load-bearing claims, held to account.

These are not coverage tests. Each one pins a promise made in the design:
dedupe is the primary key, illegal states are impossible, the core cannot see
the terminal, and a dead worker does not strand its work.
'''

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from spotdlplus.core import errors as E
from spotdlplus.core.events import EventBus, RunStarted, Warned
from spotdlplus.core.models import (
    Album,
    ArtistRef,
    Candidate,
    MatchResult,
    ReleaseType,
    SecondaryType,
    Track,
    TrackState,
    normalize_title,
)
from spotdlplus.core.store import (
    StateTransitionError,
    Store,
    track_from_json,
    track_to_json,
)

CORE = Path(__file__).resolve().parents[1] / 'src' / 'spotdlplus' / 'core'


# ----------------------------------------------------------------------------
# the seam: core must not be able to see a human
# ----------------------------------------------------------------------------

FORBIDDEN = {'rich', 'typer', 'click', 'argparse', 'tqdm'}


def test_core_never_imports_a_renderer():
    '''If this fails, someone printed in the core and a GUI just became impossible.'''
    offenders: list[str] = []
    for py in CORE.glob('*.py'):
        tree = ast.parse(py.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split('.')[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or '').split('.')[0]]
            else:
                continue
            for n in names:
                if n in FORBIDDEN:
                    offenders.append(f'{py.name}: {n}')
    assert not offenders, f'core imports a renderer: {offenders}'


def test_core_never_prints():
    '''print() in the core is a bug, not a debug aid.'''
    offenders = []
    for py in CORE.glob('*.py'):
        tree = ast.parse(py.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == 'print':
                    offenders.append(f'{py.name}:{node.lineno}')
    assert not offenders, f'core prints: {offenders}'


# ----------------------------------------------------------------------------
# errors
# ----------------------------------------------------------------------------

def test_every_error_has_a_real_remedy():
    base = E.SpotdlPlusError.remedy
    for code, cls in E.all_codes().items():
        assert cls.remedy != base, f'{code} has no remedy'
        assert len(cls.remedy) > 30, f'{code} remedy is a shrug'


def test_lookup_powers_explain():
    cls = E.lookup('rate_limited')
    assert cls is E.RateLimited
    assert cls.retry is E.Retry.AFTER
    assert E.lookup('NOPE_NOT_REAL') is None


def test_error_record_round_trips():
    err = E.RateLimited('slow down', context={'host': 'api.spotify.com'}, retry_after=4.5)
    rec = err.record()
    assert rec.code == 'RATE_LIMITED'
    assert rec.retry_after == 4.5
    assert rec.to_dict()['context']['host'] == 'api.spotify.com'
    assert err.retryable


def test_auth_loop_is_terminal():
    '''The 401 spin must end the run, not feed it. This is the whole point.'''
    assert E.AuthRefreshLoop.retry is E.Retry.NEVER
    assert not E.AuthRefreshLoop('x').retryable


def test_offline_parks_rather_than_dies():
    assert E.Offline.retry is E.Retry.PARK
    assert E.NoSpace.retry is E.Retry.PARK


# ----------------------------------------------------------------------------
# events
# ----------------------------------------------------------------------------

def test_bus_fans_out_and_unsubscribes():
    bus = EventBus()
    seen = []
    off = bus.subscribe(seen.append)
    bus.emit(RunStarted(run_id='r1', source='x', profile='canonical'))
    assert len(seen) == 1
    off()
    bus.emit(RunStarted(run_id='r1'))
    assert len(seen) == 1


def test_a_broken_renderer_cannot_kill_a_run():
    bus = EventBus()
    good = []

    def explodes(_):
        raise RuntimeError('my terminal is on fire')

    bus.subscribe(explodes)
    bus.subscribe(good.append)
    bus.emit(Warned(run_id='r1', message='still fine'))
    assert len(good) == 1
    assert bus.dropped == 1


def test_events_serialize():
    ev = RunStarted(run_id='r1', source='spotify:artist:x', profile='canonical')
    d = ev.to_dict()
    assert d['kind'] == 'RunStarted'
    assert d['source'] == 'spotify:artist:x'


# ----------------------------------------------------------------------------
# normalization: where duplicate discographies are prevented
# ----------------------------------------------------------------------------

@pytest.mark.parametrize('a, b', [
    ('Bohemian Rhapsody (2011 Remaster)', 'Bohemian Rhapsody'),
    ('Bohemian Rhapsody - Remastered 2011', 'Bohemian Rhapsody'),
    ('Dreams (Deluxe Edition)', 'Dreams'),
    ('Runaway (Explicit)', 'Runaway'),
    ('Nightcall (feat. Someone)', 'Nightcall'),
    ('Café Racer', 'Cafe Racer'),
])
def test_editions_collapse_to_the_same_recording(a, b):
    assert normalize_title(a) == normalize_title(b)


@pytest.mark.parametrize('a, b', [
    ('Wish You Were Here', 'Wish You Were Here (Live)'),
    ('Midnight City', 'Midnight City (Remix)'),
    ('Hallelujah', 'Hallelujah (Acoustic)'),
])
def test_different_recordings_stay_different(a, b):
    '''Live is not a remaster. Collapsing these would be data loss.'''
    assert normalize_title(a) != normalize_title(b)


# ----------------------------------------------------------------------------
# identity
# ----------------------------------------------------------------------------

def _track(title='Song', isrc=None, sp=None, ms=210_000, album=None):
    return Track(
        title=title,
        artists=(ArtistRef(name='An Artist', spotify_id='a1'),),
        album=album,
        isrc=isrc,
        spotify_id=sp,
        duration_ms=ms,
    )


def test_isrc_wins_identity():
    t = _track(isrc='USRC17607839', sp='abc')
    assert t.identity == 'isrc:USRC17607839'


def test_identity_falls_back_gracefully():
    assert _track(sp='abc').identity == 'sp:abc'
    assert _track().identity.startswith('fuzzy:')


def test_fuzzy_key_buckets_duration():
    assert _track(ms=210_000).fuzzy_key == _track(ms=211_400).fuzzy_key
    assert _track(ms=210_000).fuzzy_key != _track(ms=260_000).fuzzy_key


# ----------------------------------------------------------------------------
# the store
# ----------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'jobs.db')
    yield s
    s.close()


def test_same_recording_from_two_releases_lands_once(store):
    '''
    The headline claim. The album cut, the deluxe reissue, and a greatest-hits
    compilation are the same ISRC. Naive tools give you three files.
    '''
    run = store.create_run('spotify:artist:x', 'canonical')

    original = Album(title='The Record', release_type=ReleaseType.ALBUM)
    deluxe = Album(title='The Record (Deluxe Edition)', release_type=ReleaseType.ALBUM)
    comp = Album(title='Greatest Hits', release_type=ReleaseType.ALBUM,
                 secondary_types=frozenset({SecondaryType.COMPILATION}))

    ids = set()
    for al in (original, deluxe, comp):
        tid, was_new = store.add_track(run, _track(isrc='USRC17607839', album=al))
        ids.add(tid)
        assert was_new is (al is original)

    assert len(ids) == 1
    assert store.counts(run) == {'discovered': 1}


def test_illegal_transition_raises(store):
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, _track(isrc='A'))
    with pytest.raises(StateTransitionError):
        store.advance(tid, TrackState.TAGGED)   # skipped four stages


def test_the_happy_walk(store):
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, _track(isrc='A'))
    walk = [
        TrackState.ENRICHED, TrackState.MATCHED, TrackState.FETCHED,
        TrackState.TRANSCODED, TrackState.TAGGED, TrackState.PLACED, TrackState.DONE,
    ]
    for st in walk:
        store.advance(tid, st)
    assert store.get_track(tid).state is TrackState.DONE
    assert store.resumable(run) == 0


def test_a_dead_worker_does_not_strand_its_work(store):
    run = store.create_run('x', 'canonical')
    store.add_track(run, _track(isrc='A'))

    # a worker takes the track, then (as far as anyone else knows) drops dead.
    first = store.claim(run, [TrackState.DISCOVERED], lease_s=0.05)
    assert len(first) == 1
    assert store.claim(run, [TrackState.DISCOVERED]) == []   # leased, invisible

    # nobody unsticks anything by hand. the lease simply runs out.
    time.sleep(0.08)
    recovered = store.claim(run, [TrackState.DISCOVERED], lease_s=60.0)
    assert len(recovered) == 1 and recovered[0].id == first[0].id
    assert recovered[0].state is TrackState.DISCOVERED   # rewound, not consumed


def test_failure_persists_its_remedy(store):
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, _track(isrc='A'))
    store.advance(tid, TrackState.ENRICHED)
    store.fail(tid, E.NoAcceptableMatch('nothing scored above the floor').record())

    fails = store.failures(run)
    assert len(fails) == 1
    assert fails[0]['code'] == 'MATCH_NONE'
    assert fails[0]['attempts'] == 1
    assert 'relink' in fails[0]['remedy']


def test_retryable_failure_rewinds_instead_of_dying(store):
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, _track(isrc='A'))
    store.advance(tid, TrackState.ENRICHED)
    store.advance(tid, TrackState.MATCHED)
    store.fail(tid, E.DownloadFailed('stream died').record(), retryable_to=TrackState.MATCHED)

    row = store.get_track(tid)
    assert row.state is TrackState.MATCHED
    assert row.attempts == 1
    assert store.claim(run, [TrackState.MATCHED])   # back in the pool


def test_match_scoreboard_survives_the_process(store):
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, _track(isrc='A'))
    winner = Candidate(source='ytmusic', source_id='w', url='u/w', title='Song',
                       duration_ms=210_000, size_bytes=4_000_000, is_topic_channel=True)
    loser = Candidate(source='youtube', source_id='l', url='u/l', title='Song (Sped Up)',
                      duration_ms=170_000)
    store.record_match(tid, MatchResult(
        chosen=winner, score=0.94, basis='scored',
        breakdown={'duration': 1.0, 'title': 0.9, 'topic': 1.0},
        rejected=((loser, 'negative keyword: sped up', 0.41),),
    ))

    rows = store.candidates(tid)
    assert rows[0]['chosen'] == 1 and rows[0]['url'] == 'u/w'
    assert rows[1]['rejected_why'] == 'negative keyword: sped up'
    assert rows[1]['score'] == 0.41, 'losers keep their real score now'


def test_plan_summary_extrapolates_unknown_sizes(store):
    run = store.create_run('x', 'canonical')
    for i in range(4):
        tid, _ = store.add_track(run, _track(title=f'S{i}', isrc=f'ISRC{i}'))
        store.advance(tid, TrackState.ENRICHED)
        size = 4_000_000 if i < 2 else None
        store.advance(tid, TrackState.MATCHED, chosen_url=f'u/{i}', est_bytes=size)

    plan = store.plan_summary(run)
    assert plan.total == 4 and plan.matched == 4
    assert plan.est_bytes == 8_000_000
    assert plan.est_bytes_known == 2
    assert plan.est_bytes_total == 16_000_000   # mean of the known two, applied to all four


def test_metadata_blob_round_trips_without_refetching(store):
    album = Album(
        title='The Record', artists=(ArtistRef(name='An Artist'),),
        release_type=ReleaseType.EP,
        secondary_types=frozenset({SecondaryType.LIVE, SecondaryType.REMIX}),
        release_group_id='rg-1', label='A Label', original_date='1997-04-01',
    )
    t = _track(isrc='A', album=album)
    back = track_from_json(track_to_json(t))
    assert back.album.release_type is ReleaseType.EP
    assert back.album.secondary_types == {SecondaryType.LIVE, SecondaryType.REMIX}
    assert back.album.year == 1997
    assert back.identity == t.identity


def test_library_remembers_across_runs(store):
    assert store.own('isrc:A') is None
    store.remember('isrc:A', '/music/a.opus', 'opus', 4_000_000, 'deadbeef')
    got = store.own('isrc:A')
    assert got['final_path'] == '/music/a.opus'
    store.remember('isrc:A', '/music/a.flac', 'flac', 30_000_000, 'cafe')
    assert store.own('isrc:A')['format'] == 'flac'   # upsert, not duplicate
