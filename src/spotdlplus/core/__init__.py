'''
core, the headless half.

Import rule, enforced by tests: nothing under `core` may import `rich`,
`typer`, or anything else taht assumes a human is watching. If you need to tell
someone something, emit an event.
'''

from .errors import ErrorRecord, Retry, SpotdlPlusError, all_codes, lookup
from .events import Event, EventBus, NullBus, Stage, new_run_id
from .models import (
    CANONICAL,
    COMPLETIONIST,
    PROFILES,
    STUDIO_ONLY,
    Album,
    Artist,
    ArtistRef,
    Candidate,
    EntityKind,
    MatchResult,
    ReleaseType,
    SecondaryType,
    SelectionProfile,
    Track,
    TrackState,
)
from .store import PlanSummary, StateTransitionError, Store, TrackRow

__all__ = [
    'CANONICAL', 'COMPLETIONIST', 'PROFILES', 'STUDIO_ONLY',
    'Album', 'Artist', 'ArtistRef', 'Candidate', 'EntityKind', 'ErrorRecord',
    'Event', 'EventBus', 'MatchResult', 'NullBus', 'PlanSummary', 'ReleaseType',
    'Retry', 'SecondaryType', 'SelectionProfile', 'SpotdlPlusError', 'Stage',
    'StateTransitionError', 'Store', 'Track', 'TrackRow', 'TrackState',
    'all_codes', 'lookup', 'new_run_id',
]
