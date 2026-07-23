'''
run.py - the pipeline in order

resolve -> expand -> ingest -> collapse -> enrich+match -> PLAN -> download

The plan gate is the deal. By the time PlanReady fires every track is known,
deduped, and matched, and nothing has downloaded yet. You see the count and the
size before a single byte moves.
'''

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..core.config import Config
from ..core.engine import Context, Engine, StageStats
from ..core.events import EventBus, PlanReady, RunFinished, RunParked, RunStarted
from ..core.models import PROFILES, SelectionProfile
from ..core.store import PlanSummary, Store
from ..match.matcher import Searcher
from ..net.http import HttpClient
from ..providers.musicbrainz import MusicBrainzProvider
from ..providers.spotify import SpotifyProvider
from .expand import expand
from .ingest import IngestStats, collapse_works, ingest
from .resolve import Picker, Resolution, resolve
from .stages import download_stages, resolve_stages

#: Never fill the destination volume to the last byte. A music library that
#: leaves its disk gasping is a disservice to whatever else lives there.
FREE_SPACE_RESERVE_BYTES = 2 * 1024**3


def resume_run(
    run_id: str,
    *,
    config: Config,
    store: Store,
    bus: EventBus,
    searcher: Searcher,
    http: HttpClient,
    retry_failed: bool = False,
) -> StageStats:
    '''
    Picks a parked or interrupted run up where it stopped. Every stage claims
    whatever is left in its input state, so this is just re-driving the walk since
    the store already knows where each track is. `retry_failed` rewinds the failed
    ones too.
    '''
    started = time.monotonic()
    if retry_failed:
        store.rewind_failed(run_id)
    store.set_run_status(run_id, 'active')

    ctx = Context(store=store, bus=bus, config=config, run_id=run_id)
    engine = Engine(ctx)
    stats = engine.drive_all(resolve_stages(searcher) + download_stages(searcher, http))

    _refresh_genres(store, config)
    if not engine.parked:
        store.set_run_status(run_id, 'finished')
    # Session numbers, not run totals. A resume that downloaded 2 tracks once
    # said 'done 13 ok' because the run had 11 older successes on its books. It
    # counts completed and not advanced, or five stage hops per track would
    # read as five successes.
    bus.emit(RunFinished(run_id=run_id, ok=stats.completed,
                         failed=stats.failed,
                         skipped=stats.skipped,
                         elapsed_s=time.monotonic() - started))
    return stats

def source_key(resolution: Resolution) -> str:
    '''The snapshot's identity for a source: kind + spotify id, stable forever.'''
    return f'{resolution.kind}:{resolution.spotify_id}'


def _refresh_genres(store: Store, config: Config) -> None:
    '''Rebuild genres.json after the walk. Decoration. It never fails a run.'''
    try:
        from .genres import write_genres_json
        write_genres_json(store, config.output_dir)
    except Exception:  # noqa: BLE001  # A broken map is not a broken library
        pass


#: Given the plan, decide whether to download. The CLI wires a prompt in here;
#: headless runs accept automatically.
PlanGate = Callable[[PlanSummary], bool]


@dataclass(slots=True)
class RunReport:
    '''What happened, in numbers a human would actually ask for.'''

    run_id: str
    resolution: Resolution
    ingest: IngestStats
    collapsed: int
    plan: PlanSummary
    stats: StageStats = field(default_factory=StageStats)
    #: every recording the source contained on this walk, what `sync` diffs
    identities: frozenset[str] = frozenset()
    parked: bool = False
    declined: bool = False
    elapsed_s: float = 0.0


def run_source(
    source: str,
    *,
    config: Config,
    store: Store,
    bus: EventBus,
    sp: SpotifyProvider,
    mb: MusicBrainzProvider | None,
    searcher: Searcher,
    http: HttpClient,
    pick: Picker | None = None,
    gate: PlanGate | None = None,
    profile: SelectionProfile | None = None,
) -> RunReport:
    '''One source, end to end. The only function a frontend needs to call.'''
    started = time.monotonic()
    prof = profile or PROFILES[config.profile]

    resolution = resolve(source, sp, pick=pick)
    run_id = store.create_run(source, prof.name)
    bus.emit(RunStarted(run_id=run_id, source=resolution.label, profile=prof.name))

    # -- discovery: stream tracks into the store, collapse the duplicates -----
    tracks = expand(resolution, sp, profile=prof, mb=mb, bus=bus, run_id=run_id)
    # tap teh stream for identities as it flows. The snapshot `sync` diffs
    # against. A set of strings. The generator stays a generator.
    seen_identities: set[str] = set()

    def _tapped():
        for t in tracks:
            seen_identities.add(t.identity)
            yield t

    ingest_stats = ingest(_tapped(), store, run_id, bus=bus)
    collapsed = collapse_works(store, run_id, preference=prof.master_preference, bus=bus)

    # -- enrich + match: everything knowable befoer a byte moves --------------
    ctx = Context(store=store, bus=bus, config=config, run_id=run_id)
    engine = Engine(ctx)
    stats = engine.drive_all(resolve_stages(searcher))

    plan = store.plan_summary(run_id)
    bus.emit(PlanReady(
        run_id=run_id, track_count=plan.total, est_bytes=plan.est_bytes,
        est_bytes_known=plan.est_bytes_known, unmatched=plan.unmatched,
        already_have=plan.already_have, matched=plan.matched,
    ))

    report = RunReport(run_id=run_id, resolution=resolution, ingest=ingest_stats,
                       collapsed=collapsed, plan=plan, stats=stats,
                       identities=frozenset(seen_identities))

    if engine.parked:
        report.parked = True
        report.elapsed_s = time.monotonic() - started
        return report

    # -- the gate --------------------------------------------------------------
    if gate is not None and not gate(plan):
        store.set_run_status(run_id, 'finished', 'declined at plan')
        report.declined = True
        report.elapsed_s = time.monotonic() - started
        # A declined run still ENDS. This path once returned without the
        # closing event, and every live renderer was left holding an open
        # display for a run taht was never going to speak again. Awkward.
        bus.emit(RunFinished(run_id=run_id, ok=0, failed=report.stats.failed,
                             skipped=plan.already_have + plan.collapsed,
                             elapsed_s=report.elapsed_s))
        return report

    free = shutil.disk_usage(config.output_dir).free
    if plan.est_bytes_total + FREE_SPACE_RESERVE_BYTES > free:
        msg = (f'plan needs ~{plan.est_bytes_total / 1e9:.1f} GB but '
               f'{config.output_dir} has {free / 1e9:.1f} GB free '
               f'(keeping a {FREE_SPACE_RESERVE_BYTES / 1e9:.0f} GB reserve)')
        store.set_run_status(run_id, 'parked', msg)
        bus.emit(RunParked(run_id=run_id, reason=msg,
                           resume_hint='free space, then: spotdlp resume'))
        report.parked = True
        report.elapsed_s = time.monotonic() - started
        return report

    # -- the bytes ---------------------------------------------------------------
    config.output_dir.mkdir(parents=True, exist_ok=True)
    stats = stats + engine.drive_all(download_stages(searcher, http))
    report.stats = stats
    report.parked = engine.parked

    # The snapshot lands only when downloads actually ran: a plan-only look
    # must not overwrite what `sync` remembers about teh last REAL walk.
    if seen_identities:
        store.save_snapshot(
            source_key(resolution), label=resolution.label,
            kind=str(resolution.kind), identities=seen_identities)

    _refresh_genres(store, config)
    if not engine.parked:
        store.set_run_status(run_id, 'finished')
    counts = store.counts(run_id)
    report.elapsed_s = time.monotonic() - started
    bus.emit(RunFinished(
        run_id=run_id,
        ok=counts.get('done', 0),
        failed=counts.get('failed', 0),
        skipped=counts.get('skipped', 0),
        elapsed_s=report.elapsed_s,
    ))
    return report
