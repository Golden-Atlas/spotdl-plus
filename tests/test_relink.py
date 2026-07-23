'''
test_relink.py - the human override, held to its two promises.

One: a relinked track really re-downloads (the library row is revoked, so the
owned-skip cannot swallow the correction). Two: the override is the ONLY way
around the state machine, and it leaves the track in a state the pipeline can
actually drive.
'''

from __future__ import annotations

import pytest

from spotdlplus.core.models import Album, ArtistRef, Track, TrackState
from spotdlplus.core.store import Store

RADIOHEAD = ArtistRef(name='Radiohead')


def track(title='Creep', isrc='GBAYE9200001'):
    return Track(title=title, artists=(RADIOHEAD,),
                 album=Album(title='Pablo Honey', artists=(RADIOHEAD,)),
                 isrc=isrc, duration_ms=238_000)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / 'jobs.db')
    yield s
    s.close()


def _walk_to_done(store, run, tid):
    for st in (TrackState.ENRICHED, TrackState.MATCHED, TrackState.FETCHED,
               TrackState.TRANSCODED, TrackState.TAGGED, TrackState.PLACED,
               TrackState.DONE):
        kw = {'chosen_url': 'yt/wrong', 'match_score': 0.9} if st is TrackState.MATCHED else {}
        kw = {'final_path': '/music/creep.opus'} if st is TrackState.PLACED else kw
        store.advance(tid, st, **kw)


def test_relinking_a_done_track_revokes_its_library_row(store):
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, track())
    _walk_to_done(store, run, tid)
    store.remember('isrc:GBAYE9200001', '/music/creep.opus', 'opus', 4_000_000, 'sha')

    store.force_relink(tid, 'yt/right')

    row = store.get_track(tid)
    assert row.state is TrackState.MATCHED
    assert row.chosen_url == 'yt/right'
    assert row.final_path is None
    assert store.own('isrc:GBAYE9200001') is None, \
        'a distrusted file must never justify a skip again'


def test_a_relinked_track_is_claimable_by_the_fetch_stage(store):
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, track())
    _walk_to_done(store, run, tid)
    store.force_relink(tid, 'yt/right')

    claimed = store.claim(run, [TrackState.MATCHED])
    assert [r.id for r in claimed] == [tid]
    assert claimed[0].attempts == 0, 'the human gets fresh attempts'


def test_relink_reactivates_a_finished_run_so_resume_finds_it(store):
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, track())
    _walk_to_done(store, run, tid)
    store.set_run_status(run, 'finished')
    assert store.latest_run() is None   # nothing active or parked

    store.force_relink(tid, 'yt/right')
    assert store.latest_run() == run, 'resume must be able to see it again'


def test_find_tracks_searches_title_artist_and_id(store):
    run = store.create_run('x', 'canonical')
    tid, _ = store.add_track(run, track())
    store.add_track(run, track(title='Anyone Can Play Guitar', isrc='GBAYE9200002'))

    assert {r['title'] for r in store.find_tracks('creep')} == {'Creep'}
    assert len(store.find_tracks('Radiohead')) == 2
    assert store.find_tracks(tid)[0]['id'] == tid
    assert store.find_tracks('zzz-nothing') == []


def test_relink_of_a_missing_track_is_loud(store):
    with pytest.raises(KeyError):
        store.force_relink('no-such-id', 'yt/x')


# ----------------------------------------------------------------------------
# the candidate picker: full detail on demand, and free movement between
# looking, picking, pasting a URL, and skipping, and never a dead end.
# ----------------------------------------------------------------------------

import spotdlplus.cli.main as _cli   # noqa: E402

_CANDS = [
    {'chosen': 1, 'score': 0.98, 'title': 'Creep', 'uploader': 'Radiohead',
     'duration_ms': 219000, 'size_bytes': 4200000,
     'breakdown_json': '{"title": 0.71, "artist": 0.90}',
     'rejected_why': None, 'url': 'u://a'},
    {'chosen': 0, 'score': 0.66, 'title': 'ATUM Full Album', 'uploader': 'VEVO',
     'duration_ms': 3492000, 'size_bytes': None,     # missing size is fine
     'breakdown_json': None,                          # missing breakdown is fine
     'rejected_why': 'below the 0.72 floor', 'url': 'u://b'},
]


def _script(monkeypatch, answers):
    it = iter(answers)
    monkeypatch.setattr(_cli.typer, 'prompt', lambda *a, **k: next(it))


def test_a_number_picks_straight_away(monkeypatch):
    _script(monkeypatch, ['2'])
    assert _cli._decide_one(_CANDS, 219000) == ('pick', 'u://b')


def test_open_details_back_out_then_pick(monkeypatch):
    _script(monkeypatch, ['c', 'b', '1'])   # look, change your mind, pick
    assert _cli._decide_one(_CANDS, 219000) == ('pick', 'u://a')


def test_url_entry_can_back_out_and_vice_versa(monkeypatch):
    _script(monkeypatch, ['u', 'b', 'u', 'http://x'])
    assert _cli._decide_one(_CANDS, 219000) == ('pick', 'http://x')


def test_skip_abandon_and_quit(monkeypatch):
    _script(monkeypatch, [''])
    assert _cli._decide_one(_CANDS, 219000) == ('skip',)
    _script(monkeypatch, ['x'])
    assert _cli._decide_one(_CANDS, 219000) == ('abandon',)
    _script(monkeypatch, ['q'])
    assert _cli._decide_one(_CANDS, 219000) == ('quit',)


def test_out_of_range_number_reprompts_rather_than_mispicking(monkeypatch):
    _script(monkeypatch, ['9', '1'])
    assert _cli._decide_one(_CANDS, 219000) == ('pick', 'u://a')


def test_zero_candidates_c_gets_guidance_not_a_shrug(monkeypatch):
    # 192 old refusals in a real queue had no recorded scoreboards. pressing
    # `c` there got a flat 'not one of the options', which taught nothing.
    _script(monkeypatch, ['c', 'p', 's'])
    assert _cli._decide_one([], 219000) == ('skip',)


def test_candidate_details_renders_with_missing_fields():
    _cli._candidate_details(_CANDS, 219000)   # must not raise
    _cli._candidate_details(_CANDS, None)


# ----------------------------------------------------------------------------
# 1.1.5: re-search, preview, and --auto
# ----------------------------------------------------------------------------

class _FakeSearcher:
    '''Returns two fixed hits. Remembers what was searched.'''

    def __init__(self):
        self.queries = []

    def search(self, query):
        from spotdlplus.core.models import Candidate
        self.queries.append(query)
        return [
            Candidate(source='youtube', source_id='f1', url='yt://fresh1',
                      title='Creep (Official)', uploader='Radiohead',
                      duration_ms=219000, view_count=1000),
            Candidate(source='youtube', source_id='f2', url='yt://fresh2',
                      title='Creep (Live)', uploader='SomeChannel',
                      duration_ms=230000, view_count=50),
        ]


def test_research_shows_results_and_picks(monkeypatch):
    s = _FakeSearcher()
    # r -> accept default query -> pick #2 from the fresh results
    _script(monkeypatch, ['r', 'Radiohead Creep', '2'])
    verdict = _cli._decide_one(_CANDS, 219000, searcher=s,
                               default_query='Radiohead Creep')
    assert verdict == ('pick', 'yt://fresh2')
    assert s.queries == ['Radiohead Creep']


def test_research_backs_out_cleanly_to_the_main_menu(monkeypatch):
    s = _FakeSearcher()
    # r -> search -> b (back from results) -> 1 (pick from ORIGINAL candidates)
    _script(monkeypatch, ['r', 'anything', 'b', '1'])
    assert _cli._decide_one(_CANDS, 219000, searcher=s,
                            default_query='x') == ('pick', 'u://a')


def test_research_survives_a_search_failure(monkeypatch):
    from spotdlplus.core import errors as E

    class Angry:
        def search(self, q):
            raise E.SourceBlocked('bot wall')

    # r -> search fails (message, loop) -> b back out -> s skip. No dead end.
    _script(monkeypatch, ['r', 'anything', 'b', 's'])
    assert _cli._decide_one(_CANDS, 219000, searcher=Angry(),
                            default_query='x') == ('skip',)


def test_preview_opens_the_browser_then_returns_to_the_menu(monkeypatch):
    opened = []
    monkeypatch.setattr('webbrowser.open', lambda u: opened.append(u))
    # p2 -> preview second candidate -> then pick 1
    _script(monkeypatch, ['p2', '1'])
    assert _cli._decide_one(_CANDS, 219000) == ('pick', 'u://a')
    assert opened == ['u://b']


class _ScoredLike:
    '''Duck-typed stand-in for match.score.Scored, which is what the store reads.'''

    def __init__(self, url, score, why='duration off'):
        from spotdlplus.core.models import Candidate
        self.candidate = Candidate(source='youtube', source_id=url[-2:], url=url,
                                   title='Creep', uploader='X', duration_ms=238000)
        self.score = score
        self.breakdown = {'title': score}
        self.rejected_reason = why


def _refused_track(s, run, title, isrc, best_score):
    from spotdlplus.core import errors as E

    tid, _ = s.add_track(run, track(title=title, isrc=isrc))
    s.advance(tid, TrackState.ENRICHED)
    s.record_scoreboard(tid, [_ScoredLike(f'yt://{isrc}-a', best_score),
                              _ScoredLike(f'yt://{isrc}-b', best_score - 0.2)])
    s.fail(tid, E.NoAcceptableMatch('below the floor').record())
    return tid


def test_refusal_scoreboard_is_persisted_with_real_scores(store):
    run = store.create_run('x', 'canonical')
    tid = _refused_track(store, run, 'Creep', 'GBAYE9200001', 0.66)
    rows = store.candidates(tid)
    assert len(rows) == 2
    assert rows[0]['score'] == 0.66 and rows[1]['score'] == pytest.approx(0.46)
    assert all(r['chosen'] == 0 for r in rows), 'a refusal has no winner'


def test_auto_queue_accepts_above_the_bar_and_leaves_the_rest(store, monkeypatch):
    from spotdlplus.core.config import Config

    run = store.create_run('x', 'canonical')
    good = _refused_track(store, run, 'Creep', 'GBAYE9200001', 0.68)
    bad = _refused_track(store, run, 'Obscurity', 'GBAYE9200099', 0.31)
    store.set_run_status(run, 'finished')

    monkeypatch.setattr(_cli.typer, 'confirm', lambda *a, **k: False)  # no download
    _cli._auto_queue(store, Config(), None, threshold=0.65)

    assert store.get_track(good).state is TrackState.MATCHED, 'relinked to its best'
    assert store.get_track(good).chosen_url == 'yt://GBAYE9200001-a'
    assert store.get_track(bad).state is TrackState.FAILED, 'still a human call'
