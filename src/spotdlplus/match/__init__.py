'''
match. From a track to a source, explainably.

Search yields candidates. The scorer ranks them. The matcher packages the
winner and every loser into a MatchResult that gets persisted. The one hard
rule: below the confidence floor we refuse rather than guess, because a wrong
song that looks right is worse than a gap you can see.

May import core and net. Never blocks. A refusal is a typed, non-retryable
error.
'''

from .matcher import TIE_MARGIN, Searcher, match_track, search_queries
from .score import Scored, is_topic_channel, rank, score_candidate
from .search import YtDlpSearcher

__all__ = [
    'TIE_MARGIN', 'Scored', 'Searcher', 'YtDlpSearcher', 'is_topic_channel',
    'match_track', 'rank', 'score_candidate', 'search_queries',
]
