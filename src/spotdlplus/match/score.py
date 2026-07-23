'''
score.py - candidate scoring

Pure math. No network and no clock. A Track and a Candidate go in, a number
between 0 and 1 comes out along with a breakdown of how it got there.

Duration carries the most weight because it's the one signal a title can't
fake. A live take really is longer and a sped-up edit really is shorter.
Titles lie, channels flatter, and view counts follow whatever is popular.
'''

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from ..core.models import Candidate, Track, normalize_artist, normalize_title

# -- weights. they sum to 1.0, and duration owns half on purpose. ------------
W_DURATION = 0.50
W_TITLE = 0.30
W_CHANNEL = 0.20

#: Within this, teh durations are "the same" and score a full 1.0. Mastering
#: and silent lead-in/out routinely move a true match by a second or two.
DURATION_PERFECT_S = 2.0
#: Beyond this, a candidate is a different recording. Score 0, and let the
#: multiplier and the confidence floor finish the job.
DURATION_ZERO_S = 15.0

#: Multiplicative penalties. A value of 0.05 means 'this is almost certainly not
#: what you want, but if it's somehow the only option, don't hard-crash'. The
#: real gate is the confidence floor in MatchResult.
NEGATIVE_PENALTIES: dict[str, float] = {
    'karaoke': 0.03,
    'instrumental': 0.10,
    'nightcore': 0.05,
    'sped up': 0.06,
    'spedup': 0.06,
    'speed up': 0.06,
    'slowed': 0.06,
    'reverb': 0.15,
    '8d audio': 0.10,
    '8d': 0.20,
    'cover': 0.15,
    'remix': 0.25,
    'live': 0.30,
    'concert': 0.35,
    'acoustic': 0.40,
    'demo': 0.45,
    'lyric': 0.85,     # a lyrics video is usually the right audio, lightly dinged
    'reaction': 0.02,
    'tutorial': 0.02,
    'how to play': 0.02,
    'guitar lesson': 0.02,
}

#: Longest keyword first, so 'sped up' matches before 'up' could. Word-boundaried
#: to stop 'live' firing inside 'Olive' or 'deliver'.
_NEG_PATTERNS = {
    term: re.compile(rf'\b{re.escape(term)}\b', re.IGNORECASE)
    for term in NEGATIVE_PENALTIES
}

#: Marks YouTube's auto-generated "Art Track" uploads. A topic channel is the
#: platform's own machine-made official audio, and it's the single best signal
#: that a result is the real recording rather than a re-upload.
_TOPIC = re.compile(r'\s-\s*topic\s*$', re.IGNORECASE)

#: Scaffolding in a candidate title that isn't part of the song name. Stripped
#: before the title similarity check so '(Official Audio)' doesn't cost points.
_TITLE_NOISE = re.compile(
    r'[\(\[]\s*(official\s*(music\s*)?(video|audio)|audio|visualizer|lyric[s]?|'
    r'hd|hq|4k|remaster(ed)?|full\s*album|mv)\s*[^)\]]*[\)\]]',
    re.IGNORECASE,
)


def is_topic_channel(channel: str) -> bool:
    return bool(_TOPIC.search(channel or ''))


def clean_candidate_title(title: str) -> str:
    '''Drop production scaffolding so the title compares on the song name alone.'''
    return _TITLE_NOISE.sub(' ', title or '')


def duration_score(target_ms: int, cand_ms: int) -> float:
    '''1.0 when the two agree, decaying linearly to 0 by DURATION_ZERO_S apart.'''
    if cand_ms <= 0:
        return 0.0
    delta_s = abs(target_ms - cand_ms) / 1000.0
    if delta_s <= DURATION_PERFECT_S:
        return 1.0
    if delta_s >= DURATION_ZERO_S:
        return 0.0
    return 1.0 - (delta_s - DURATION_PERFECT_S) / (DURATION_ZERO_S - DURATION_PERFECT_S)


def title_score(target: Track, cand: Candidate) -> float:
    '''
    Scores how much the candidate title looks liek 'artist title'. YouTube titles
    are usually 'Artist - Song (Official Video)', so we compare against both the
    bare song and the artist plus song version and keep whichever scores better.
    '''
    cand_title = normalize_title(clean_candidate_title(cand.title))
    want_song = normalize_title(target.title)
    want_full = f'{normalize_artist(target.artist)} {want_song}'.strip()
    bare = fuzz.ratio(want_song, cand_title) / 100.0
    full = fuzz.token_sort_ratio(want_full, cand_title) / 100.0
    return max(bare, full)


def channel_score(target: Track, cand: Candidate) -> float:
    '''
    Scores how confident we are that this uploader is the right source. A topic
    channel is the platform's own official audio, which is the best signal short of
    an ISRC. Otherwise we look for the artist's name in the channel name.
    '''
    if cand.is_topic_channel or is_topic_channel(cand.uploader):
        return 1.0

    channel = normalize_artist(cand.uploader)
    artist = normalize_artist(target.artist)
    if not channel or not artist:
        return 0.4   # unknown, not damning

    sim = fuzz.partial_ratio(artist, channel) / 100.0
    if sim >= 0.9:
        return 0.9   # "Radiohead" == channel "Radiohead"
    if sim >= 0.6:
        return 0.6
    return 0.35


def negative_penalty(target: Track, cand: Candidate) -> tuple[float, list[str]]:
    '''
    Product of penalties for every red-flag word the candidate has and the target
    doesn't. Returns (multiplier, terms_that_fired) so the receipt can name them.
    '''
    target_text = f'{target.title} {target.album.title if target.album else ""}'
    penalty = 1.0
    fired: list[str] = []
    for term, pattern in _NEG_PATTERNS.items():
        if pattern.search(cand.title) and not pattern.search(target_text):
            penalty *= NEGATIVE_PENALTIES[term]
            fired.append(term)
    return penalty, fired


@dataclass(frozen=True, slots=True)
class Scored:
    '''A candidate and its verdict. Sorts high-to-low by score.'''

    candidate: Candidate
    score: float
    breakdown: dict[str, float]
    penalized_by: tuple[str, ...]

    @property
    def rejected_reason(self) -> str:
        if self.penalized_by:
            return 'negative keyword: ' + ', '.join(self.penalized_by)
        if self.breakdown.get('duration', 1.0) < 0.5:
            return f'duration off by {self.breakdown["duration_delta_s"]:.0f}s'
        return f'scored {self.score:.2f}'


def score_candidate(target: Track, cand: Candidate) -> Scored:
    '''The whole verdict for one candidate, breakdown included.'''
    dur = duration_score(target.duration_ms, cand.duration_ms)
    ttl = title_score(target, cand)
    chan = channel_score(target, cand)
    base = W_DURATION * dur + W_TITLE * ttl + W_CHANNEL * chan

    penalty, fired = negative_penalty(target, cand)
    final = base * penalty

    return Scored(
        candidate=cand,
        score=round(final, 4),
        breakdown={
            'duration': round(dur, 3),
            'duration_delta_s': round(abs(target.duration_ms - cand.duration_ms) / 1000.0, 1),
            'title': round(ttl, 3),
            'channel': round(chan, 3),
            'base': round(base, 3),
            'penalty': round(penalty, 3),
        },
        penalized_by=tuple(fired),
    )


def rank(target: Track, candidates: list[Candidate]) -> list[Scored]:
    '''Every candidate scored, best first. Ties broken by view count, hten title.'''
    scored = [score_candidate(target, c) for c in candidates]
    scored.sort(
        key=lambda s: (s.score, s.candidate.view_count or 0, s.breakdown['title']),
        reverse=True,
    )
    return scored
