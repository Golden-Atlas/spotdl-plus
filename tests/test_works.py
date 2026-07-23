'''
test_works.py - tier-2 dedupe, pinned to data measured against the live API.

The fixtures below are not invented. They are the real ISRCs and real durations
Spotify returned on 2026-07-10. If a refactor breaks the OK Computer case, it
breaks the exact thing that put two of every song in your library.
'''

from __future__ import annotations

import pytest

from spotdlplus.core.models import Album, ArtistRef, MasterPreference, Track
from spotdlplus.core.works import (
    WORK_DURATION_TOLERANCE_MS,
    collapse,
    group_works,
    pick_master,
    same_work,
    work_key,
)

RADIOHEAD = ArtistRef(name='Radiohead', spotify_id='4Z8W4fKeB5YxbusRsdQVPb')

OK_COMPUTER = Album(title='OK Computer', artists=(RADIOHEAD,), release_date='1997-05-28')
OKNOTOK = Album(title='OK Computer OKNOTOK 1997 2017', artists=(RADIOHEAD,),
                release_date='2017-06-23')
IN_RAINBOWS = Album(title='In Rainbows', artists=(RADIOHEAD,), release_date='2007-10-10')
DISK_2 = Album(title='In Rainbows (Disk 2)', artists=(RADIOHEAD,), release_date='2007-12-03')


def track(title, isrc, ms, album, *, popularity=None) -> Track:
    return Track(title=title, artists=(RADIOHEAD,), album=album, isrc=isrc,
                 duration_ms=ms, popularity=popularity)


# The real thing, byte for byte.
AIRBAG_1997 = track('Airbag', 'GBAYE9701274', 287_900, OK_COMPUTER, popularity=60)
AIRBAG_2017 = track('Airbag - Remastered', 'GBBKS1700107', 283_800, OKNOTOK, popularity=52)


# ----------------------------------------------------------------------------
# the key
# ----------------------------------------------------------------------------

def test_a_remaster_shares_the_work_key_with_its_original():
    assert work_key(AIRBAG_1997) == work_key(AIRBAG_2017)


def test_duration_is_a_guard_not_part_of_the_key():
    '''
    The bug this module exists for. Bucketing duration into 2s bins put 287.9s and
    283.8s on opposite sides of a boundary, so the two masters never met.
    '''
    assert '287' not in work_key(AIRBAG_1997)
    assert AIRBAG_1997.fuzzy_key != AIRBAG_2017.fuzzy_key, 'the old bucket really does split them'
    assert same_work(AIRBAG_1997, AIRBAG_2017), 'the tolerance does not'


def test_four_seconds_apart_is_the_same_work():
    assert abs(AIRBAG_1997.duration_ms - AIRBAG_2017.duration_ms) == 4_100
    assert same_work(AIRBAG_1997, AIRBAG_2017)


def test_a_minute_apart_is_a_different_recording():
    live = track('Airbag', 'GBXXX0000001', 287_900 + 61_000, OK_COMPUTER)
    assert work_key(live) == work_key(AIRBAG_1997)
    assert not same_work(live, AIRBAG_1997), 'a live take is not a remaster'


def test_the_tolerance_is_the_one_the_contract_names():
    assert WORK_DURATION_TOLERANCE_MS == 8_000


# ----------------------------------------------------------------------------
# the negative control: measured, and it must never regress
# ----------------------------------------------------------------------------

def test_in_rainbows_disk_two_is_genuinely_different_music():
    '''No shared ISRCs, no shared titles. Merging these would be data loss.'''
    a = track('Nude', 'GBAYE0700651', 255_000, IN_RAINBOWS)
    b = track('MK 1', 'GBAYE0700661', 71_000, DISK_2)
    assert work_key(a) != work_key(b)
    assert not same_work(a, b)

    kept, collapsed = collapse([a, b], MasterPreference.ORIGINAL)
    assert len(kept) == 2 and collapsed == []


# ----------------------------------------------------------------------------
# clustering
# ----------------------------------------------------------------------------

def test_clustering_pairs_the_masters_and_leaves_strangers_alone():
    exit_music = track('Exit Music (For a Film)', 'GBAYE9701280', 264_000, OK_COMPUTER)
    clusters = group_works([AIRBAG_1997, exit_music, AIRBAG_2017])
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2]


def test_a_chain_of_close_durations_stays_one_work():
    a = track('Song', 'A', 200_000, OK_COMPUTER)
    b = track('Song', 'B', 204_000, OKNOTOK)
    c = track('Song', 'C', 208_000, OKNOTOK)
    assert len(group_works([a, b, c])) == 1, 'single-linkage: a-b and b-c are each within 5s'


def test_a_gap_wider_than_the_tolerance_splits_the_cluster():
    a = track('Song', 'A', 200_000, OK_COMPUTER)
    b = track('Song', 'B', 240_000, OKNOTOK)
    assert len(group_works([a, b])) == 2


# ----------------------------------------------------------------------------
# picking the survivor
# ----------------------------------------------------------------------------

def test_original_wins_by_default_because_this_is_an_archive():
    keeper, dropped = pick_master([AIRBAG_2017, AIRBAG_1997], MasterPreference.ORIGINAL)
    assert keeper.isrc == 'GBAYE9701274'
    assert [d.isrc for d in dropped] == ['GBBKS1700107']


def test_remaster_wins_when_asked():
    keeper, _ = pick_master([AIRBAG_1997, AIRBAG_2017], MasterPreference.REMASTER)
    assert keeper.isrc == 'GBBKS1700107'


def test_popularity_wins_when_asked():
    keeper, _ = pick_master([AIRBAG_2017, AIRBAG_1997], MasterPreference.POPULARITY)
    assert keeper.popularity == 60


def test_a_track_with_no_release_date_does_not_win_by_accident():
    orphan = track('Airbag', 'GBZZZ0000001', 285_000, None)
    keeper, _ = pick_master([orphan, AIRBAG_1997], MasterPreference.ORIGINAL)
    assert keeper.isrc == 'GBAYE9701274', 'a missing date sorts last, not first'


def test_picking_is_deterministic_across_runs():
    twin_a = track('Song', 'AAA', 200_000, OK_COMPUTER)
    twin_b = track('Song', 'BBB', 200_000, OK_COMPUTER)   # identical dates
    first, _ = pick_master([twin_a, twin_b], MasterPreference.ORIGINAL)
    second, _ = pick_master([twin_b, twin_a], MasterPreference.ORIGINAL)
    assert first.isrc == second.isrc, 'the identity tiebreak must be stable'


def test_both_refuses_to_pick_rather_than_pretending():
    with pytest.raises(ValueError, match='collapses nothing'):
        pick_master([AIRBAG_1997, AIRBAG_2017], MasterPreference.BOTH)


def test_an_empty_cluster_is_a_bug_not_a_none():
    with pytest.raises(ValueError):
        pick_master([], MasterPreference.ORIGINAL)


# ----------------------------------------------------------------------------
# the whole pass
# ----------------------------------------------------------------------------

def test_the_ok_computer_case_end_to_end():
    '''
    Measured: 12 shared titles, 0 shared ISRCs, 11 OKNOTOK-exclusive b-sides.
    ISRC-only dedupe keeps 35. We must keep 23.
    '''
    originals = [track(f'S{i}', f'GBAYE97012{i:02d}', 200_000 + i * 1000, OK_COMPUTER)
                 for i in range(12)]
    remasters = [track(f'S{i} - Remastered', f'GBBKS17001{i:02d}', 200_000 + i * 1000 - 4_100,
                       OKNOTOK) for i in range(12)]
    bsides = [track(f'B{i}', f'GBBKS17002{i:02d}', 180_000 + i * 1000, OKNOTOK)
              for i in range(11)]

    everything = originals + remasters + bsides
    assert len({t.identity for t in everything}) == 35, 'ISRC alone would keep 35'

    kept, collapsed = collapse(everything, MasterPreference.ORIGINAL)
    assert len(kept) == 23
    assert len(collapsed) == 12

    kept_isrcs = {t.isrc for t in kept}
    assert all(i.isrc in kept_isrcs for i in originals), 'every 1997 master survives'
    assert not any(r.isrc in kept_isrcs for r in remasters), 'every 2017 remaster is gone'
    assert all(b.isrc in kept_isrcs for b in bsides), 'the b-sides are not duplicates'


def test_collapsing_records_who_replaced_whom():
    '''So `explain` can say why your remaster is missing, instead of you wondering.'''
    _, collapsed = collapse([AIRBAG_1997, AIRBAG_2017], MasterPreference.ORIGINAL)
    dropped, keeper = collapsed[0]
    assert dropped.isrc == 'GBBKS1700107'
    assert keeper.isrc == 'GBAYE9701274'


def test_both_keeps_everything_and_collapses_nothing():
    kept, collapsed = collapse([AIRBAG_1997, AIRBAG_2017], MasterPreference.BOTH)
    assert len(kept) == 2 and collapsed == []


def test_completionist_keeps_both_masters_and_canonical_does_not():
    from spotdlplus.core.models import CANONICAL, COMPLETIONIST
    assert CANONICAL.master_preference is MasterPreference.ORIGINAL
    assert COMPLETIONIST.master_preference is MasterPreference.BOTH
