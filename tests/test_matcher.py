'''
test_matcher.py - the wrong-song problem, held to account.

The fixtures are the real search results for "Radiohead - Creep", captured
2026-07-12: the official upload, a lyrics video, a remaster re-up, a live
performance, and a karaoke. The matcher must pick the official one, every time,
and it must refuse rather than hand you the karaoke when the official one is gone.
'''

from __future__ import annotations

import pytest

from spotdlplus.core.errors import MatchAmbiguous, NoAcceptableMatch
from spotdlplus.core.models import Album, ArtistRef, Candidate, Track
from spotdlplus.match.matcher import match_track, search_queries
from spotdlplus.match.score import (
    channel_score,
    duration_score,
    is_topic_channel,
    negative_penalty,
    rank,
)

RADIOHEAD = ArtistRef(name='Radiohead')
CREEP = Track(title='Creep', artists=(RADIOHEAD,),
              album=Album(title='Pablo Honey', artists=(RADIOHEAD,)),
              isrc='GBAYE9200001', duration_ms=238_000)


def cand(title, ms, uploader, *, views=None, topic=False):
    return Candidate(source='youtube', source_id=title[:6], url=f'yt/{title[:8]}',
                     title=title, uploader=uploader, duration_ms=ms,
                     view_count=views, is_topic_channel=topic)


# The actual search, verbatim.
OFFICIAL = cand('Radiohead - Creep', 237_000, 'Radiohead', views=1_506_900_000)
LYRICS = cand('Radiohead - Creep (Lyrics)', 241_000, 'LyricsZone', views=15_600_000)
REMASTER = cand('Radiohead - Creep (Remastered) (Audio)', 240_000, 'Luis Fernando Flores', views=4_300_000)
LIVE = cand('Radiohead Perform "Creep" Live on Conan', 265_000, "Conan O'Brien", views=15_800_000)
KARAOKE = cand('Radiohead - Creep (Karaoke Version)', 258_000, 'Sing King', views=39_200_000)

REAL_SEARCH = [OFFICIAL, LYRICS, REMASTER, LIVE, KARAOKE]


class FakeSearcher:
    def __init__(self, results, *, per_query=None):
        self.results = results
        self.per_query = per_query or {}
        self.queries: list[str] = []

    def search(self, query):
        self.queries.append(query)
        if query in self.per_query:
            return self.per_query[query]
        return list(self.results)


# ----------------------------------------------------------------------------
# duration is the anchor
# ----------------------------------------------------------------------------

def test_duration_within_two_seconds_is_perfect():
    assert duration_score(238_000, 237_000) == 1.0
    assert duration_score(238_000, 238_000) == 1.0


def test_duration_decays_and_bottoms_out():
    assert duration_score(238_000, 265_000) == 0.0    # 27s off -> a different take
    mid = duration_score(238_000, 246_000)            # 8s off
    assert 0.0 < mid < 1.0


def test_a_zero_duration_candidate_scores_zero_not_a_crash():
    assert duration_score(238_000, 0) == 0.0


# ----------------------------------------------------------------------------
# channel signal
# ----------------------------------------------------------------------------

def test_a_topic_channel_is_the_strongest_signal():
    assert is_topic_channel('Radiohead - Topic')
    assert not is_topic_channel('Radiohead')
    assert channel_score(CREEP, cand('Creep', 238_000, 'Whoever - Topic', topic=True)) == 1.0


def test_the_artists_own_channel_scores_high():
    assert channel_score(CREEP, OFFICIAL) >= 0.9


def test_a_stranger_channel_scores_low():
    assert channel_score(CREEP, KARAOKE) < 0.6


# ----------------------------------------------------------------------------
# negative keywords, judged against the target
# ----------------------------------------------------------------------------

def test_karaoke_and_live_are_penalised():
    p_kar, fired_kar = negative_penalty(CREEP, KARAOKE)
    assert p_kar < 0.1 and 'karaoke' in fired_kar
    p_live, fired_live = negative_penalty(CREEP, LIVE)
    assert p_live < 0.5 and 'live' in fired_live


def test_a_penalty_word_the_target_also_has_is_not_penalised():
    '''If you asked for the live cut, "live" in the candidate is correct.'''
    live_target = Track(title='Creep - Live at the BBC', artists=(RADIOHEAD,), duration_ms=250_000)
    penalty, fired = negative_penalty(live_target, cand('Creep (Live)', 250_000, 'Radiohead'))
    assert penalty == 1.0 and fired == []


def test_penalty_words_are_word_boundaried():
    '''"live" must not fire inside "deliver". "8d" must not fire inside "18days".'''
    p, fired = negative_penalty(CREEP, cand('Creep (Deliverance Mix)', 238_000, 'x'))
    assert 'live' not in fired


# ----------------------------------------------------------------------------
# the whole ranking, on the real search
# ----------------------------------------------------------------------------

def test_the_official_upload_wins_the_real_creep_search():
    ranked = rank(CREEP, REAL_SEARCH)
    assert ranked[0].candidate is OFFICIAL
    assert ranked[0].score >= 0.9


def test_the_karaoke_loses_despite_having_more_views_than_most():
    '''39M views must not buy a karaoke the top slot.'''
    ranked = rank(CREEP, REAL_SEARCH)
    karaoke_rank = next(i for i, s in enumerate(ranked) if s.candidate is KARAOKE)
    assert karaoke_rank == len(ranked) - 1, 'karaoke belongs dead last'


def test_the_live_version_cannot_win_on_duration_alone():
    ranked = rank(CREEP, REAL_SEARCH)
    live = next(s for s in ranked if s.candidate is LIVE)
    assert live.breakdown['duration'] == 0.0
    assert live.score < 0.4


def test_every_candidate_carries_a_readable_reason_for_losing():
    ranked = rank(CREEP, REAL_SEARCH)
    reasons = {s.candidate.title: s.rejected_reason for s in ranked[1:]}
    assert 'karaoke' in reasons['Radiohead - Creep (Karaoke Version)']
    # the live take is both off-duration and keyworded. naming the keyword is
    # the more useful of the two truths, so that is what we lead with.
    assert 'live' in reasons['Radiohead Perform "Creep" Live on Conan']


def test_a_purely_off_duration_loser_says_so():
    '''A candidate with no bad words, only a wrong length, reports the length.'''
    # 'mix'/'remix' isn't in this title, so the only fault is duration.
    ranked = rank(CREEP, [OFFICIAL, cand('Creep', 400_000, 'Someone')])
    tail = ranked[-1]
    assert 'duration off' in tail.rejected_reason


# ----------------------------------------------------------------------------
# the matcher's verdict and its refusals
# ----------------------------------------------------------------------------

def test_a_confident_match_returns_the_winner_and_the_full_scoreboard():
    result = match_track(CREEP, FakeSearcher(REAL_SEARCH))
    assert result.chosen is OFFICIAL
    assert result.basis == 'scored' and result.confident
    assert len(result.rejected) == 4, 'the losers are kept, with reasons'
    assert result.breakdown['duration'] == 1.0


def test_the_matcher_refuses_when_only_junk_is_left():
    '''The official upload is gone. We must not settle for the karaoke.'''
    junk = [LIVE, KARAOKE, cand('Creep - Nightcore', 190_000, 'NightcoreWorld')]
    with pytest.raises(NoAcceptableMatch) as exc:
        match_track(CREEP, FakeSearcher(junk))
    assert exc.value.retry.value == 'never', 'searching again finds the same junk'
    assert 'relink' in exc.value.remedy
    assert exc.value.context['best_score'] < 0.72


def test_the_matcher_refuses_when_there_are_no_candidates_at_all():
    with pytest.raises(NoAcceptableMatch):
        match_track(CREEP, FakeSearcher([]))


def test_a_tie_between_different_length_takes_stops_rather_than_guessing():
    '''
    Two takes equidistant from the target on opposite sides: identical duration
    scores, but 12s apart from each other, so plausibly a studio cut and an extended
    mix. That is the tie worth a human's eyes.
    '''
    take_a = cand('Artist - Song', 196_000, 'Artist - Topic', views=1000, topic=True)   # 4s short
    take_b = cand('Artist - Song', 204_000, 'Other - Topic', views=1000, topic=True)    # 4s long
    target = Track(title='Song', artists=(ArtistRef(name='Artist'),), duration_ms=200_000)

    with pytest.raises(MatchAmbiguous) as exc:
        match_track(target, FakeSearcher([take_a, take_b]))
    assert exc.value.retry.value == 'never'
    assert exc.value.context['a_len_s'] != exc.value.context['b_len_s']


def test_tied_copies_of_the_same_recording_do_not_stop_the_run():
    '''
    Learned live from "bad guy": three audio re-uploads all at 195s tie on score.
    Any of them is the correct audio. Picking one is right. Asking a human is not.
    '''
    a = cand('Billie Eilish - bad guy (Lyrics)', 195_000, '7clouds', views=500_000_000)
    b = cand('Billie Eilish - bad guy (Audio)', 195_000, 'Music Topic BR', views=12_000_000)
    c = cand('Billie Eilish - bad guy (Lyrics)', 195_000, 'DopeMusic', views=8_000_000)
    target = Track(title='bad guy', artists=(ArtistRef(name='Billie Eilish'),), duration_ms=194_000)

    result = match_track(target, FakeSearcher([b, a, c]))   # deliberately unsorted
    assert result.chosen is a, 'the highest-view interchangeable copy is chosen'
    assert result.score >= 0.72


def test_a_clear_winner_over_a_floor_clearing_runner_up_is_not_a_tie():
    strong = cand('Radiohead - Creep', 238_000, 'Radiohead - Topic', views=1e9, topic=True)
    weaker = cand('Radiohead - Creep (Lyrics)', 241_000, 'LyricsZone', views=1e6)
    result = match_track(CREEP, FakeSearcher([strong, weaker]))
    assert result.chosen is strong


# ----------------------------------------------------------------------------
# query construction
# ----------------------------------------------------------------------------

def test_the_primary_query_is_artist_dash_title():
    assert search_queries(CREEP)[0] == 'Radiohead - Creep'


def test_we_do_not_stuff_official_or_audio_into_the_query():
    '''Those words bias the search toward re-uploads that game them.'''
    for q in search_queries(CREEP):
        assert 'official' not in q.lower() and 'audio' not in q.lower()


def test_the_album_is_a_fallback_query_when_it_adds_information():
    qs = search_queries(CREEP)
    assert any('Pablo Honey' in q for q in qs)


def test_the_second_query_is_only_used_if_the_first_finds_nothing():
    searcher = FakeSearcher([], per_query={'Radiohead - Creep': [], 'x': [OFFICIAL]})
    searcher.per_query['Radiohead Creep Pablo Honey'] = [OFFICIAL]
    result = match_track(CREEP, searcher)
    assert result.chosen is OFFICIAL
    assert len(searcher.queries) == 2, 'the fallback ran because the first was empty'


def test_tied_reuploads_a_few_seconds_apart_now_auto_resolve():
    '''
    A remaster-heavy artist threw off a pile of MATCH_AMBIGUOUS at 3-5s apart that
    were plainly the same audio, just re-encoded. Within the (looser) tie window
    and both clean, pick the popular one instead of stopping a human. Would have
    stopped at the old 2s threshold.
    '''
    a = cand('Smashing Pumpkins - Wrath', 224_000, 'The Smashing Pumpkins - Topic',
             views=5_000_000, topic=True)
    b = cand('Smashing Pumpkins - Wrath', 228_000, 'SP Reupload - Topic',
             views=900_000, topic=True)   # 4s from a
    target = Track(title='Wrath', artists=(ArtistRef(name='Smashing Pumpkins'),),
                   duration_ms=226_000)
    result = match_track(target, FakeSearcher([b, a]))
    assert result.chosen is a, 'the higher-view interchangeable copy wins, no prompt'


def test_a_variant_beyond_the_window_still_stops_for_a_human():
    '''The guard holds: genuinely different-length takes (>6s) still ask.'''
    a = cand('Artist - Song', 220_000, 'Artist - Topic', views=1_000_000, topic=True)
    b = cand('Artist - Song', 232_000, 'Other - Topic', views=1_000_000, topic=True)  # 12s
    target = Track(title='Song', artists=(ArtistRef(name='Artist'),), duration_ms=226_000)
    with pytest.raises(MatchAmbiguous):
        match_track(target, FakeSearcher([a, b]))
