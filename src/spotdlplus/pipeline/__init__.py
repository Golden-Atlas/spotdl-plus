'''
pipeline. The part that knows what we are trying to do.

Resolve an input to one entity. Expand that entity into a stream of tracks.
Dedupe them. Plan. Then hand the queue to the Engine, which does not care what
any of it means.

May import every layer beneath it. Imported only by `cli`.
'''

from .expand import expand
from .ingest import SKIP_DUPLICATE, SKIP_OWNED, IngestStats, collapse_works, ingest
from .resolve import Picker, Resolution, ResolutionCandidate, candidates, resolve
from .run import RunReport, run_source
from .selection import exclusion_reason, needs_enrichment, spotify_album_groups
from .stages import download_stages, resolve_stages

__all__ = [
    'SKIP_DUPLICATE', 'SKIP_OWNED', 'IngestStats', 'Picker', 'Resolution',
    'ResolutionCandidate', 'RunReport', 'candidates', 'collapse_works',
    'download_stages', 'exclusion_reason', 'expand', 'ingest', 'needs_enrichment',
    'resolve', 'resolve_stages', 'run_source', 'spotify_album_groups',
]
