'''
matcher.py - picking a source, or refusing to

Puts search and scoring together into one decision and returns a MatchResult
with the winner, its breakdown, and every loser and why it lost. All of that
gets saved, so a bad match is a receipt you can read instead of a re-run.

There are two ways to refuse. Nothing cleared the floor, or the top two are
close enough that picking would be a coin flip.
'''

from __future__ import annotations

from ..core.errors import MatchAmbiguous, NoAcceptableMatch
from ..core.models import Candidate, MatchResult, Track
from .score import Scored, rank

#: If the best 2 both clear teh floor but sit within this of each other, we
#: might have a genuine ambiguity. Small, because a real winner usually clears
#: the runner-up by a lot.
TIE_MARGIN = 0.05

#: How far two TIED durations can drift and still be the same recording hosted
#: twice. Looser than the scoring anchor on purpose. Re-uploads drift a few
#: seconds, and a remaster-heavy artist threw off a pile of MATCH_AMBIGUOUS at
#: 3-5s that were plainly teh same audio. The red-flag guard still stands.
_TIE_SAME_RECORDING_MS = 6_000


def _same_recording(a: Scored, b: Scored) -> bool:
    '''
    Checks whether two candidates are the same audio hosted twice.

    I learned this one on 'bad guy'. Three re-uploads tied at 0.87 and all of them
    were the correct 195s album track, while the official video sat lower because
    its title has extra words in it. Stopping to ask which identical copy you want
    is worse than just picking one, so matching durations with no red-flag words
    means we pick by view count.
    '''
    delta_ms = abs(a.candidate.duration_ms - b.candidate.duration_ms)
    same_length = delta_ms <= _TIE_SAME_RECORDING_MS
    same_penalty_status = bool(a.penalized_by) == bool(b.penalized_by)
    return same_length and same_penalty_status


class Searcher:
    '''Structural type the matcher needs. Real one is YtDlpSearcher. Tests pass a fake.'''

    def search(self, query: str) -> list[Candidate]: ...


def search_queries(track: Track) -> list[str]:
    '''
    A couple of phrasings, tried until one comes back with candidates. 'Artist -
    Title' is how official uploads and topic channels get named, and adding the
    album sometimes rescues a generic title.
    '''
    artist = track.artist
    title = track.title
    queries = [f'{artist} - {title}']
    if track.album and track.album.title and track.album.title.lower() not in title.lower():
        queries.append(f'{artist} {title} {track.album.title}')
    return queries


def match_track(
    track: Track,
    searcher: Searcher,
    *,
    floor: float = MatchResult.CONFIDENCE_FLOOR,
    tie_margin: float = TIE_MARGIN,
) -> MatchResult:
    '''
    Finds the best source for one track or raises a typed refusal. Returns a
    MatchResult with the winner and the full scoreboard, and raises
    NoAcceptableMatch or MatchAmbiguous when it shouldn't guess.
    '''
    # Two recordings can share a title within one artist (Cigarettes After Sex
    # has two Sweets). The album is what lets a human tell refusals apart.
    label = f'{track.artist} - {track.title}'
    if track.album is not None:
        label += f' [{track.album.title}]'

    scored: list[Scored] = []
    for query in search_queries(track):
        candidates = searcher.search(query)
        if candidates:
            scored = rank(track, candidates)
            break

    if not scored:
        raise NoAcceptableMatch(
            f'no candidates at all for {label}',
            context={'track': track.title, 'artist': track.artist},
        )

    best = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None
    losers = tuple((s.candidate, s.rejected_reason, s.score) for s in scored[1:])

    if best.score < floor:
        err = NoAcceptableMatch(
            f'best candidate for {label} scored '
            f'{best.score:.2f}, below the {floor:.2f} floor',
            context={
                'track': track.title, 'artist': track.artist,
                'best_score': best.score, 'best_url': best.candidate.url,
                'best_title': best.candidate.title, 'floor': floor,
                'why_not': best.rejected_reason,
            },
        )
        # The full scoreboard rides on the refusal so the stage can persist it.
        # A refusal with no receipts once left the relink picker staring at an
        # empty table, and `--auto` with nothing to read.
        err.scoreboard = scored
        raise err

    close = (runner_up is not None and runner_up.score >= floor
             and (best.score - runner_up.score) < tie_margin)
    if close and not _same_recording(best, runner_up):
        # Close scores AND plausibly different recordings. Name the axis that
        # actually differed: 'different lengths (154s vs 154s)' once shipped because
        # the real divergence was a penalty keyword, and the message olny knew
        # how to blame duration. Nonsense on its face.
        delta_ms = abs(best.candidate.duration_ms - runner_up.candidate.duration_ms)
        if delta_ms > _TIE_SAME_RECORDING_MS:
            why = (f'different lengths ({best.candidate.duration_ms // 1000}s vs '
                   f'{runner_up.candidate.duration_ms // 1000}s)')
        else:
            flagged = best if best.penalized_by else runner_up
            why = (f'same length but one carries a red-flag word '
                   f'({", ".join(flagged.penalized_by)})')
        err = MatchAmbiguous(
            f'{label}: top two within {tie_margin:.2f} '
            f'({best.score:.2f} vs {runner_up.score:.2f}), {why}',
            context={
                'track': track.title,
                'a': best.candidate.url, 'a_score': best.score,
                'a_len_s': best.candidate.duration_ms // 1000,
                'a_flags': list(best.penalized_by),
                'b': runner_up.candidate.url, 'b_score': runner_up.score,
                'b_len_s': runner_up.candidate.duration_ms // 1000,
                'b_flags': list(runner_up.penalized_by),
            },
        )
        err.scoreboard = scored
        raise err
    # Otherwise `best` wins: either a clear lead, or a tie between interchangeable
    # copies of the same recording, resolved deterministically by rank() (which
    # orders equal scores by view count).

    return MatchResult(
        chosen=best.candidate,
        score=best.score,
        basis='scored',
        breakdown=best.breakdown,
        rejected=losers,
        runner_up_score=runner_up.score if runner_up else None,
    )
